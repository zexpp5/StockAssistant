"""Forward 估值倍数（B 路线 P0 — review 反馈第 ❶ 条）

研报核心问题不是"过去财务好不好"，而是"现价对不对"。Trailing P/E 对成长股
是反向信号 —— NVDA TTM P/E 43 看着贵，但 FY27/FY28 forward P/E 25-28，市场
根本不是按 trailing 定价。本模块计算 forward 估值，把这个修正出来。

输出：
  - FY1 / FY2 forward P/E（基于分析师一致 EPS 预期）
  - FY1 / FY2 forward EV/Sales（基于一致 Revenue 预期 + 当前 EV）
  - PEG (forward P/E / 未来 EPS CAGR)

为什么不算 forward EV/EBITDA：
  FMP 免费层不给 forward EBITDA 预期，需自己估算 EBITDA margin 假设。
  与其给一个低质量数字，不如不给 — 用 EV/Sales + 当前 EBITDA margin 反推更诚实。

数据：FMP /analyst-estimates (annual) + /profile (market cap)。
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


def _sorted_estimates_asc(estimates: list[dict]) -> list[dict]:
    """按 date 升序排（FY1 在前，FY4 在后）。FMP 返回顺序不稳定。"""
    return sorted([e for e in estimates if e.get("date")], key=lambda e: e["date"])


def forward_multiples(ticker: str) -> dict[str, Any]:
    """计算 FY1 / FY2 forward P/E 和 forward EV/Sales。

    返回：{
      'current_price', 'market_cap', 'enterprise_value',
      'fy1': {date, eps_fwd, revenue_fwd, pe_fwd, ev_sales_fwd, n_analysts_eps},
      'fy2': {...},
      'eps_cagr_3y': 3 年 EPS 复合增速 (FY1→FY3 implied),
      'peg_fy1': forward P/E / EPS CAGR (Lynch-style),
      'verdict': 估值合理性判断
    }
    """
    profile = fmp_client.fetch_company_profile(ticker)
    if not profile:
        return {"error": "no profile", "ticker": ticker}

    market_cap = profile.get("market_cap")
    if not market_cap:
        return {"error": "no market cap", "ticker": ticker}

    # 当前股价：从 DCF endpoint 拿（也包含股价）
    dcf = fmp_client.fetch_dcf(ticker)
    current_price = (dcf or {}).get("current_price")

    # EV = Market Cap + Total Debt - Cash（用最新一期 BS）
    bs = fmp_client.fetch_balance_sheet(ticker, years=1)
    enterprise_value = None
    if bs:
        b = bs[0]
        ld = b.get("long_term_debt") or 0
        sd = b.get("short_term_debt") or 0
        cash = b.get("cash_and_equivalents") or 0
        enterprise_value = market_cap + ld + sd - cash

    est_data = fmp_client.fetch_analyst_estimates(ticker)
    estimates = (est_data or {}).get("estimates") or []
    if not estimates:
        return {"error": "no analyst estimates", "ticker": ticker}

    sorted_est = _sorted_estimates_asc(estimates)
    if not sorted_est:
        return {"error": "estimates have no usable dates", "ticker": ticker}

    def _fy_metrics(est: dict) -> dict:
        eps = est.get("eps_avg")
        rev = est.get("revenue_avg")
        return {
            "date": est.get("date"),
            "eps_fwd": eps,
            "revenue_fwd": rev,
            # forward P/E 在亏损预期下无意义 → 直接 None
            "pe_fwd": round(_safe_div(current_price, eps), 2) if (eps and eps > 0) else None,
            "ev_sales_fwd": round(_safe_div(enterprise_value, rev), 2) if rev else None,
            "n_analysts_eps": est.get("analysts_eps"),
            "n_analysts_revenue": est.get("analysts_revenue"),
        }

    fy1 = _fy_metrics(sorted_est[0]) if len(sorted_est) >= 1 else None
    fy2 = _fy_metrics(sorted_est[1]) if len(sorted_est) >= 2 else None
    fy3 = _fy_metrics(sorted_est[2]) if len(sorted_est) >= 3 else None

    # EPS CAGR (FY1 → FY3，2 年 CAGR；做 PEG 用)
    eps_cagr = None
    if fy1 and fy3 and fy1.get("eps_fwd") and fy3.get("eps_fwd"):
        if fy1["eps_fwd"] > 0 and fy3["eps_fwd"] > 0:
            eps_cagr = (fy3["eps_fwd"] / fy1["eps_fwd"]) ** (1 / 2) - 1

    peg = None
    if fy1 and fy1.get("pe_fwd") and eps_cagr and eps_cagr > 0:
        peg = round(fy1["pe_fwd"] / (eps_cagr * 100), 2)

    # 估值合理性判断（PEG 经验区间，Lynch 1989）
    if peg is None:
        verdict = "❓ 无法计算 PEG"
    elif peg < 1.0:
        verdict = f"🟢 PEG {peg} < 1.0 — 增速覆盖估值（Lynch 标准）"
    elif peg < 1.5:
        verdict = f"🟡 PEG {peg} ∈ [1.0, 1.5) — 估值合理"
    elif peg < 2.0:
        verdict = f"🟠 PEG {peg} ∈ [1.5, 2.0) — 估值偏高"
    else:
        verdict = f"🔴 PEG {peg} ≥ 2.0 — 估值已显著高于增速"

    return {
        "ticker": ticker,
        "current_price": current_price,
        "market_cap": market_cap,
        "enterprise_value": enterprise_value,
        "fy1": fy1,
        "fy2": fy2,
        "fy3": fy3,
        "eps_cagr_2y_implied": round(eps_cagr * 100, 2) if eps_cagr else None,
        "peg_fy1": peg,
        "verdict": verdict,
        "note": "forward EBITDA 预期 FMP 免费层不可得；以 EV/Sales 替代",
        "source": "FMP/analyst-estimates (annual) + /profile + /balance-sheet",
    }


def _print_report(r: dict[str, Any]) -> None:
    if r.get("error"):
        print(f"⚠️ {r['ticker']}: {r['error']}")
        return
    print("=" * 80)
    print(f"  📈 Forward 估值倍数 — {r['ticker']}")
    print("=" * 80)
    print(f"  当前价格: ${r.get('current_price')} · 市值: ${(r.get('market_cap') or 0)/1e9:.1f}B · "
          f"EV: ${(r.get('enterprise_value') or 0)/1e9:.1f}B")
    print()
    print(f"  {'':6}{'FY 末':>14}{'EPS 预期':>10}{'营收预期':>14}{'Fwd P/E':>10}{'Fwd EV/Sales':>14}{'分析师':>8}")
    for label, fy in (("FY1", r.get("fy1")), ("FY2", r.get("fy2")), ("FY3", r.get("fy3"))):
        if not fy:
            continue
        rev_b = (fy.get('revenue_fwd') or 0) / 1e9
        print(f"  {label:6}{fy.get('date', '?'):>14}{fy.get('eps_fwd', '—'):>10}{rev_b:>13.1f}B"
              f"{fy.get('pe_fwd', '—') or '—':>10}{fy.get('ev_sales_fwd', '—') or '—':>14}"
              f"{fy.get('n_analysts_eps', '—') or '—':>8}")
    print()
    if r.get("eps_cagr_2y_implied"):
        print(f"  隐含 EPS 2 年 CAGR: {r['eps_cagr_2y_implied']}%")
    if r.get("peg_fy1"):
        print(f"  PEG (FY1): {r['peg_fy1']}")
    print(f"  判定: {r.get('verdict')}")
    print(f"  ⓘ {r.get('note')}")
    print("=" * 80)


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Forward 估值倍数")
    parser.add_argument("ticker")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not fmp_client.is_available():
        print("⚠️ FMP_API_KEY 未配置")
        return 1

    r = forward_multiples(args.ticker)
    if args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        _print_report(r)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
