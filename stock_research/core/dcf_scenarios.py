"""自建 DCF + 三档场景 + WACC × TGR 敏感度（B 路线 P0 — review 反馈第 ❷ 条）

FMP 的 DCF 是黑盒：WACC、TGR、FCF 假设都不知道。对周期顶部 + AI capex 拐点
风险的股票，单点 DCF 可信度接近零。本模块自建透明 DCF，关键假设全部显式：

模型结构：
  Phase 1 (5 年显式预测): 用分析师一致 Revenue 预期 → 推 FCF 路径
  Phase 2 (终值): Gordon growth model
                FV = FCF_terminal × (1 + TGR) / (WACC - TGR)
  现值 = ΣFCF / (1+WACC)^t  +  TV / (1+WACC)^N

三档场景差异（保守 / 基准 / 乐观）：
  - 营收增速：bear -2% / base 0 / bull +2% 偏移分析师一致预期
  - 终值 FCF margin：bear / base / bull 不同假设
  - WACC：bear 高 / base 中 / bull 低（折现率敏感反映风险偏好）
  - 终值增长率 (TGR)：固定 2.5% (~长期 GDP 名义增速)

敏感度矩阵（base case 下）：
  WACC ∈ {-100bp, -50bp, base, +50bp, +100bp}
  TGR ∈ {1.5%, 2.0%, 2.5%, 3.0%, 3.5%}
  → 25 格 fair value 表

数据：FMP /analyst-estimates, /cash-flow, /balance-sheet, /profile。
"""
from __future__ import annotations
import logging
from typing import Any

from . import fmp_client

logger = logging.getLogger(__name__)


def _safe_div(a, b):
    if a is None or b is None:
        return None
    try:
        a, b = float(a), float(b)
        if abs(b) < 1e-9:
            return None
        return a / b
    except (TypeError, ValueError):
        return None


def _gather_baseline(ticker: str) -> dict[str, Any]:
    """拉一次性数据：当前价、市值、债务、最新 FCF/Revenue、3 年平均 FCF margin、分析师一致预期。

    周期股关键修正：FCF margin 用近 3 年平均（normalized），单年值在半导体/能源/材料等
    周期行业波动 >50%，单点 baseline 会让 DCF fair value 巨幅偏离均值回归位置。
    """
    profile = fmp_client.fetch_company_profile(ticker)
    if not profile:
        return {"error": "no profile"}
    market_cap = profile.get("market_cap")

    bs = fmp_client.fetch_balance_sheet(ticker, years=1)
    cf = fmp_client.fetch_cash_flow(ticker, years=3)         # 拉 3 年用于 normalize
    inc = fmp_client.fetch_income_full(ticker, years=3)
    est_data = fmp_client.fetch_analyst_estimates(ticker)
    dcf_ref = fmp_client.fetch_dcf(ticker)  # 取当前股价

    if not bs or not cf or not inc:
        return {"error": "insufficient statements"}

    b = bs[0]
    c0 = cf[0]
    i0 = inc[0]

    debt = (b.get("long_term_debt") or 0) + (b.get("short_term_debt") or 0)
    cash = b.get("cash_and_equivalents") or 0
    net_debt = debt - cash

    fcf_latest = c0.get("free_cash_flow")
    revenue_latest = i0.get("revenue")
    fcf_margin_latest = _safe_div(fcf_latest, revenue_latest)

    # 3 年平均 FCF margin — normalized baseline
    pairs = []
    for ci, ii in zip(cf[:3], inc[:3]):
        m = _safe_div(ci.get("free_cash_flow"), ii.get("revenue"))
        if m is not None:
            pairs.append(m)
    fcf_margin_3y_avg = (sum(pairs) / len(pairs)) if pairs else fcf_margin_latest
    n_years_normalized = len(pairs)

    # 分析师 Revenue 路径（升序排，FY1→FYn）
    estimates = (est_data or {}).get("estimates") or []
    sorted_est = sorted([e for e in estimates if e.get("date") and e.get("revenue_avg")],
                        key=lambda e: e["date"])

    shares_diluted = i0.get("weighted_average_shares_diluted") or i0.get("weighted_average_shares")

    return {
        "ticker": ticker,
        "current_price": (dcf_ref or {}).get("current_price"),
        "market_cap": market_cap,
        "net_debt": net_debt,
        "cash": cash,
        "shares_diluted": shares_diluted,
        "revenue_latest": revenue_latest,
        "fcf_latest": fcf_latest,
        "fcf_margin_latest": fcf_margin_latest,
        "fcf_margin_normalized": fcf_margin_3y_avg,            # 下游 _scenario_dcf 优先用
        "fcf_margin_normalized_n": n_years_normalized,         # 实际用了几年（≤3）
        "analyst_revenue_path": [(e["date"], e["revenue_avg"]) for e in sorted_est],
    }


def _build_revenue_path(baseline: dict, growth_offset: float, n_years: int = 5) -> list[float]:
    """构建 5 年 Revenue 路径。

    取分析师一致 Revenue 路径，再叠加 growth_offset（bear/base/bull）。
    超出分析师覆盖年（FY3 之后）按 fade 增速线性向终值增速过渡。
    """
    analyst = baseline.get("analyst_revenue_path") or []
    rev_latest = baseline.get("revenue_latest")
    if not rev_latest:
        return []

    # 用分析师覆盖年的 implied growth → 应用 offset → 推 revenue
    revs: list[float] = []
    last_rev = rev_latest
    for i in range(n_years):
        if i < len(analyst):
            analyst_rev = analyst[i][1]
            implied_growth = (analyst_rev / last_rev) - 1 if last_rev else 0
            adjusted = implied_growth + growth_offset
        else:
            # 超出分析师覆盖：用上一年增速 × fade 0.7
            implied_growth = ((revs[-1] / revs[-2]) - 1) if len(revs) >= 2 else 0.10
            adjusted = max(implied_growth * 0.7, 0.03)  # 不低于 3%
        new_rev = last_rev * (1 + adjusted)
        revs.append(new_rev)
        last_rev = new_rev
    return revs


def _scenario_dcf(baseline: dict, growth_offset: float,
                  terminal_fcf_margin: float, wacc: float,
                  tgr: float = 0.025, n_years: int = 5) -> dict[str, Any]:
    """单场景 DCF。

    growth_offset: ±0.02 类，叠加到分析师一致增速上
    terminal_fcf_margin: 终值年的 FCF / Revenue
    wacc: 折现率
    tgr: 终值增长率
    """
    revs = _build_revenue_path(baseline, growth_offset, n_years=n_years)
    if not revs:
        return {"error": "cannot build revenue path"}

    # FCF margin 起点用 normalized（3 年均值）而非单年最新值
    # 周期股单年 FCF margin 可能偏离长期中枢 ±20pp，用 normalized 起点更稳
    cur_margin = (baseline.get("fcf_margin_normalized")
                  or baseline.get("fcf_margin_latest") or 0.20)
    margins = []
    for i in range(n_years):
        # 线性插值 cur_margin → terminal_fcf_margin
        m = cur_margin + (terminal_fcf_margin - cur_margin) * (i + 1) / n_years
        margins.append(m)

    fcfs = [r * m for r, m in zip(revs, margins)]

    # 显式期 PV
    pv_explicit = sum(fcf / ((1 + wacc) ** (t + 1)) for t, fcf in enumerate(fcfs))

    # 终值 FCF（最后一年的 FCF × (1 + TGR)）
    fcf_terminal = fcfs[-1] * (1 + tgr)
    if wacc <= tgr:
        return {"error": f"WACC {wacc} ≤ TGR {tgr}, terminal undefined"}
    terminal_value = fcf_terminal / (wacc - tgr)
    pv_terminal = terminal_value / ((1 + wacc) ** n_years)

    enterprise_value = pv_explicit + pv_terminal
    equity_value = enterprise_value - (baseline.get("net_debt") or 0)
    shares = baseline.get("shares_diluted")
    fair_value_per_share = _safe_div(equity_value, shares)

    current_price = baseline.get("current_price")
    upside = None
    if fair_value_per_share is not None and current_price:
        upside = (fair_value_per_share / current_price - 1) * 100

    return {
        "assumptions": {
            "growth_offset_pp": round(growth_offset * 100, 2),
            "terminal_fcf_margin_pct": round(terminal_fcf_margin * 100, 2),
            "wacc_pct": round(wacc * 100, 2),
            "tgr_pct": round(tgr * 100, 2),
            "n_explicit_years": n_years,
        },
        "revenue_path": [round(r / 1e9, 1) for r in revs],   # $B
        "fcf_path": [round(f / 1e9, 1) for f in fcfs],
        "pv_explicit_b": round(pv_explicit / 1e9, 1),
        "pv_terminal_b": round(pv_terminal / 1e9, 1),
        "terminal_value_pct": round(pv_terminal / enterprise_value * 100, 1) if enterprise_value else None,
        "enterprise_value_b": round(enterprise_value / 1e9, 1),
        "equity_value_b": round(equity_value / 1e9, 1),
        "fair_value_per_share": round(fair_value_per_share, 2) if fair_value_per_share else None,
        "current_price": current_price,
        "upside_pct": round(upside, 2) if upside is not None else None,
    }


def three_scenario_dcf(ticker: str) -> dict[str, Any]:
    """生成三档场景 DCF（保守 / 基准 / 乐观）+ 敏感度矩阵。"""
    baseline = _gather_baseline(ticker)
    if baseline.get("error"):
        return {"ticker": ticker, "error": baseline["error"]}

    # 终值 FCF margin 也以 normalized 为锚点，避免周期顶/底单年扭曲
    cur_margin = (baseline.get("fcf_margin_normalized")
                  or baseline.get("fcf_margin_latest") or 0.20)

    # 三档假设
    # bear: 增速 -2pp / 终值 margin = 当前 × 0.7 / WACC 11% / TGR 2%
    # base: 增速 +0pp / 终值 margin = 当前 × 0.85 / WACC 9% / TGR 2.5%
    # bull: 增速 +2pp / 终值 margin = 当前 × 1.0 / WACC 8% / TGR 3%
    scenarios = {
        "bear": _scenario_dcf(baseline, growth_offset=-0.02,
                              terminal_fcf_margin=cur_margin * 0.7,
                              wacc=0.11, tgr=0.02),
        "base": _scenario_dcf(baseline, growth_offset=0.0,
                              terminal_fcf_margin=cur_margin * 0.85,
                              wacc=0.09, tgr=0.025),
        "bull": _scenario_dcf(baseline, growth_offset=0.02,
                              terminal_fcf_margin=cur_margin * 1.0,
                              wacc=0.08, tgr=0.03),
    }

    # 敏感度矩阵：base case 假设下，WACC × TGR 网格
    sensitivity_grid = []
    waccs = [0.07, 0.08, 0.09, 0.10, 0.11]   # ±100bp around base 9%
    tgrs = [0.015, 0.020, 0.025, 0.030, 0.035]
    for w in waccs:
        row = []
        for t in tgrs:
            if w <= t:
                row.append(None)
                continue
            res = _scenario_dcf(baseline, growth_offset=0.0,
                                terminal_fcf_margin=cur_margin * 0.85,
                                wacc=w, tgr=t)
            row.append(res.get("fair_value_per_share"))
        sensitivity_grid.append(row)

    return {
        "ticker": ticker,
        "baseline": {
            "current_price": baseline.get("current_price"),
            "revenue_latest_b": round((baseline.get("revenue_latest") or 0) / 1e9, 1),
            "fcf_latest_b": round((baseline.get("fcf_latest") or 0) / 1e9, 1),
            "fcf_margin_latest_pct": round((baseline.get("fcf_margin_latest") or 0) * 100, 2),
            "fcf_margin_normalized_pct": round((baseline.get("fcf_margin_normalized") or 0) * 100, 2),
            "fcf_margin_normalized_n_years": baseline.get("fcf_margin_normalized_n"),
            "shares_diluted_b": round((baseline.get("shares_diluted") or 0) / 1e9, 2),
            "net_debt_b": round((baseline.get("net_debt") or 0) / 1e9, 1),
            "analyst_coverage_years": len(baseline.get("analyst_revenue_path") or []),
        },
        "scenarios": scenarios,
        "sensitivity": {
            "wacc_axis_pct": [round(w * 100, 1) for w in waccs],
            "tgr_axis_pct": [round(t * 100, 1) for t in tgrs],
            "fair_value_grid": sensitivity_grid,
            "current_price": baseline.get("current_price"),
        },
        "source": "self-built DCF / FMP analyst-estimates + statements",
    }


# ────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────

def _print_scenario(label: str, s: dict) -> None:
    if s.get("error"):
        print(f"  [{label}] ⚠️ {s['error']}")
        return
    a = s["assumptions"]
    print(f"  [{label}] 假设: 增速 offset {a['growth_offset_pp']:+}pp / "
          f"终值 FCF margin {a['terminal_fcf_margin_pct']}% / "
          f"WACC {a['wacc_pct']}% / TGR {a['tgr_pct']}%")
    print(f"        FCF 路径 ($B): {s.get('fcf_path')}")
    print(f"        PV 显式 ${s.get('pv_explicit_b')}B + 终值 ${s.get('pv_terminal_b')}B "
          f"(终值占比 {s.get('terminal_value_pct')}%)")
    print(f"        → Fair Value: ${s.get('fair_value_per_share')} / 股 "
          f"vs 现价 ${s.get('current_price')} → {s.get('upside_pct')}% upside")


def _print_report(r: dict[str, Any]) -> None:
    if r.get("error"):
        print(f"⚠️ {r['ticker']}: {r['error']}")
        return
    print("=" * 100)
    print(f"  💰 自建 DCF — {r['ticker']}")
    print("=" * 100)
    bl = r["baseline"]
    print(f"  当前: 价 ${bl['current_price']} · 市值 ${bl['revenue_latest_b']}B 营收 · "
          f"${bl['fcf_latest_b']}B FCF (margin {bl['fcf_margin_latest_pct']}%)")
    print(f"  分析师覆盖: {bl['analyst_coverage_years']} 年 · 摊薄股本 {bl['shares_diluted_b']}B")
    print()
    print("【三档场景】")
    for label, key in [("保守", "bear"), ("基准", "base"), ("乐观", "bull")]:
        _print_scenario(label, r["scenarios"][key])
    print()
    print("【敏感度矩阵】(base case，每格 = Fair Value $/股)")
    s = r["sensitivity"]
    cur = s.get("current_price") or 0
    print(f"  {'':12}" + "".join(f"  TGR {t}%".rjust(11) for t in s["tgr_axis_pct"]))
    for i, w in enumerate(s["wacc_axis_pct"]):
        cells = []
        for v in s["fair_value_grid"][i]:
            if v is None:
                cells.append(f"{'—':>10}")
            else:
                # 标注 vs 现价：> 现价 涂绿，< 涂红
                marker = "🟢" if v > cur * 1.1 else ("🔴" if v < cur * 0.9 else "🟡")
                cells.append(f"{marker}${v:>7.0f}")
        print(f"  WACC {w}%   " + "  ".join(cells))
    print(f"  (绿: > 现价 +10%；红: < 现价 -10%；黄: ±10% 内)")
    print("=" * 100)


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description="自建 DCF 三档场景 + 敏感度")
    parser.add_argument("ticker")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not fmp_client.is_available():
        print("⚠️ FMP_API_KEY 未配置")
        return 1

    r = three_scenario_dcf(args.ticker)
    if args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        _print_report(r)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
