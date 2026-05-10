"""8 季财务 trend（B 路线 P0 — review 反馈第 ❻ 条）

静态指标（cur vs prev）丢失方向信息：ROE 101% 是从 60% 涨上来还是 130% 跌下来，
含义完全相反。本模块输出 8 季时序，让人一眼看出"在改善 / 在恶化"。

输出 8 季 trend 表（最新→最早）：
  ── 盈利侧 ──
  毛利率%、营业利润率%、净利率%
  ── 增长侧 ──
  营收 YoY%、营收 QoQ%
  ── 质量侧（最关键）──
  应收周转天数 (DSO)、存货周转天数 (DIO)
  应计盈余/资产 (Sloan TATA)
  ── 现金侧 ──
  CFO/NI、SBC/营收%

每个指标自动出 trend verdict：稳定 / 改善 / 恶化（基于最近 4 季 vs 前 4 季）。

数据：FMP /income-statement, /balance-sheet, /cash-flow，period=quarter, limit=8。
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


def _trend_direction(values: list[float], min_change: float = 0.05) -> str:
    """前后两段均值对比的趋势判定。

    取序列前一半 vs 后一半（按 values[0]=最新 顺序拆分），相对变化 >5% 标改善/恶化。
    需要每段至少 2 个有效值，否则返回 'insufficient_data'。
    """
    valid_idx = [i for i, v in enumerate(values) if v is not None]
    if len(valid_idx) < 4:
        return "insufficient_data"
    mid = len(values) // 2
    recent = [values[i] for i in valid_idx if i < mid]
    older = [values[i] for i in valid_idx if i >= mid]
    if len(recent) < 2 or len(older) < 2:
        return "insufficient_data"
    rm = sum(recent) / len(recent)
    om = sum(older) / len(older)
    if abs(om) < 1e-9:
        return "stable"
    rel = (rm - om) / abs(om)
    if rel > min_change:
        return "improving"
    if rel < -min_change:
        return "deteriorating"
    return "stable"


def quarterly_trends(ticker: str, n_quarters: int = 8) -> dict[str, Any]:
    """拉 8 季三表，输出 trend 矩阵。

    返回 {
      'periods': ['2026-01-25', ...],  # 期末日期，新→旧
      'metrics': {
        'gross_margin': {values: [...], trend: 'improving' | ...},
        ...
      }
    }
    """
    inc = fmp_client.fetch_income_full(ticker, years=n_quarters, period="quarter")
    bs = fmp_client.fetch_balance_sheet(ticker, years=n_quarters, period="quarter")
    cf = fmp_client.fetch_cash_flow(ticker, years=n_quarters, period="quarter")

    if not inc or not bs or not cf:
        return {"error": "insufficient quarterly data", "ticker": ticker}

    # 三表对齐到相同日期序列
    inc_by_date = {x["date"]: x for x in inc if x.get("date")}
    bs_by_date = {x["date"]: x for x in bs if x.get("date")}
    cf_by_date = {x["date"]: x for x in cf if x.get("date")}
    common_dates = sorted(
        set(inc_by_date) & set(bs_by_date) & set(cf_by_date),
        reverse=True,
    )[:n_quarters]
    if len(common_dates) < 4:
        return {"error": f"only {len(common_dates)} aligned quarters", "ticker": ticker}

    # 构建每季原始数据
    rows = []
    for d in common_dates:
        i, b, c = inc_by_date[d], bs_by_date[d], cf_by_date[d]
        rows.append({
            "date": d,
            "revenue": i.get("revenue"),
            "gross_profit": i.get("gross_profit"),
            "operating_income": i.get("operating_income"),
            "net_income": i.get("net_income"),
            "ar": b.get("net_receivables"),
            "inv": b.get("inventory"),
            "total_assets": b.get("total_assets"),
            "cfo": c.get("operating_cash_flow"),
            "sbc": c.get("stock_based_compensation"),
        })

    # 派生指标
    n = len(rows)
    metrics: dict[str, list] = {
        "gross_margin_pct": [],
        "operating_margin_pct": [],
        "net_margin_pct": [],
        "revenue_yoy_pct": [],
        "revenue_qoq_pct": [],
        "dso_days": [],         # 应收周转天数 (AR / quarterly_revenue * 90)
        "dio_days": [],         # 存货周转天数 (Inv / quarterly_revenue * 90)  注：粗算用营收，准确版用 COGS
        "accruals_to_ta_pct": [],  # Sloan TATA: (NI - CFO) / TA
        "cfo_to_ni": [],
        "sbc_pct_revenue": [],
    }
    for idx, r in enumerate(rows):
        rev = r["revenue"]
        ni = r["net_income"]
        ta = r["total_assets"]
        cfo = r["cfo"]

        metrics["gross_margin_pct"].append(_safe_div(r["gross_profit"], rev) and round(_safe_div(r["gross_profit"], rev) * 100, 2))
        metrics["operating_margin_pct"].append(_safe_div(r["operating_income"], rev) and round(_safe_div(r["operating_income"], rev) * 100, 2))
        metrics["net_margin_pct"].append(_safe_div(ni, rev) and round(_safe_div(ni, rev) * 100, 2))

        # YoY: idx + 4 是 4 季前
        if idx + 4 < n and rows[idx + 4]["revenue"]:
            yoy = (rev / rows[idx + 4]["revenue"] - 1) * 100 if rev else None
            metrics["revenue_yoy_pct"].append(round(yoy, 2) if yoy is not None else None)
        else:
            metrics["revenue_yoy_pct"].append(None)
        # QoQ: idx + 1 是上一季
        if idx + 1 < n and rows[idx + 1]["revenue"]:
            qoq = (rev / rows[idx + 1]["revenue"] - 1) * 100 if rev else None
            metrics["revenue_qoq_pct"].append(round(qoq, 2) if qoq is not None else None)
        else:
            metrics["revenue_qoq_pct"].append(None)

        dso = _safe_div(r["ar"], rev) and round(_safe_div(r["ar"], rev) * 90, 1)
        dio = _safe_div(r["inv"], rev) and round(_safe_div(r["inv"], rev) * 90, 1)
        metrics["dso_days"].append(dso)
        metrics["dio_days"].append(dio)

        accruals = (ni - cfo) if (ni is not None and cfo is not None) else None
        metrics["accruals_to_ta_pct"].append(_safe_div(accruals, ta) and round(_safe_div(accruals, ta) * 100, 2))
        metrics["cfo_to_ni"].append(_safe_div(cfo, ni) and round(_safe_div(cfo, ni), 2))
        metrics["sbc_pct_revenue"].append(_safe_div(r["sbc"], rev) and round(_safe_div(r["sbc"], rev) * 100, 2))

    # 趋势判定 + 方向解读（哪些指标"改善 = 好"）
    HIGHER_IS_BETTER = {
        "gross_margin_pct": True, "operating_margin_pct": True, "net_margin_pct": True,
        "revenue_yoy_pct": True, "revenue_qoq_pct": True,
        "dso_days": False, "dio_days": False,    # 周转天数越短越好
        "accruals_to_ta_pct": False,             # Sloan：越大越差
        "cfo_to_ni": True,
        "sbc_pct_revenue": False,                # SBC 占比越低越好
    }

    out_metrics = {}
    for key, values in metrics.items():
        direction = _trend_direction(values)
        # "improving" 在反向指标上的语义需翻转
        if direction in ("improving", "deteriorating") and not HIGHER_IS_BETTER[key]:
            direction = "deteriorating" if direction == "improving" else "improving"
        out_metrics[key] = {
            "values": values,  # 新→旧
            "trend": direction,
            "latest": values[0] if values else None,
            "higher_is_better": HIGHER_IS_BETTER[key],
        }

    return {
        "ticker": ticker,
        "periods": common_dates,           # 新→旧
        "n_quarters": len(common_dates),
        "metrics": out_metrics,
        "source": "FMP/quarterly statements",
    }


# ────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────

METRIC_LABELS = {
    "gross_margin_pct": "毛利率%",
    "operating_margin_pct": "营业利润率%",
    "net_margin_pct": "净利率%",
    "revenue_yoy_pct": "营收 YoY%",
    "revenue_qoq_pct": "营收 QoQ%",
    "dso_days": "应收周转天数",
    "dio_days": "存货周转天数",
    "accruals_to_ta_pct": "应计盈余/资产% (Sloan)",
    "cfo_to_ni": "CFO/NI",
    "sbc_pct_revenue": "SBC/营收%",
}

TREND_EMOJI = {"improving": "🟢↑", "deteriorating": "🔴↓",
               "stable": "🟡→", "insufficient_data": "⚪—"}


def _print_report(r: dict[str, Any]) -> None:
    if r.get("error"):
        print(f"⚠️ {r['ticker']}: {r['error']}")
        return
    print("=" * 110)
    print(f"  📈 季度财务 Trend — {r['ticker']} ({r['n_quarters']} 季)")
    print("=" * 110)
    periods = r["periods"]
    header = f"  {'指标':<24}{'趋势':>10}  " + "  ".join(f"{p[2:]:>9}" for p in periods)
    print(header)
    print("-" * 110)
    for key, label in METRIC_LABELS.items():
        m = r["metrics"].get(key, {})
        vals = m.get("values", [])
        trend = m.get("trend", "?")
        emoji = TREND_EMOJI.get(trend, "?")
        cells = "  ".join(f"{(v if v is not None else '—'):>9}" for v in vals)
        print(f"  {label:<24}{emoji:>10}  {cells}")
    print("=" * 110)
    print("  ⓘ 趋势：最近 4 季均值 vs 前 4 季均值，相对变化 >5% 标改善 / 恶化")


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description="8 季财务 trend")
    parser.add_argument("ticker")
    parser.add_argument("-n", "--n-quarters", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not fmp_client.is_available():
        print("⚠️ FMP_API_KEY 未配置")
        return 1

    r = quarterly_trends(args.ticker, n_quarters=args.n_quarters)
    if args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        _print_report(r)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
