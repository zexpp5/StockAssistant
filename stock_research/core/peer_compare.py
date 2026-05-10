"""同业横向对标（B 路线 Phase 1B）

输出 12 项财务指标 vs 同业的分位排名：

  估值 (3): P/E, EV/EBITDA, P/B
  盈利 (3): 毛利率, 净利率, ROE
  质量 (2): CFO/NI, FCF margin
  成长 (2): 营收增速, 净利润增速
  财务 (2): 净负债/EBITDA, 流动比率

同业来源：
  1) FMP /stock-peers（按 sector/industry/market cap 推荐）— 默认
  2) 用户传入 peers list — 手动指定
  3) GICS industry 匹配 — 兜底（待扩展）

分位排名解读：
  优秀指标（高=好）：percentile 越大越靠前
    毛利率/净利率/ROE/CFO_NI/FCF_margin/营收增速/净利润增速/流动比率
  反向指标（低=好）：percentile 反向（用 1-pct）
    P/E/EV_EBITDA/P/B/净负债_EBITDA
"""
from __future__ import annotations
import logging
import time
from typing import Any

from . import fmp_client

logger = logging.getLogger(__name__)


# 哪些指标"越大越好"（默认 True 表示高分位 = 好）
HIGHER_IS_BETTER = {
    "pe": False,                # 越低越好
    "ev_ebitda": False,
    "pb": False,
    "gross_margin": True,
    "net_margin": True,
    "roe": True,
    "cfo_to_ni": True,
    "fcf_margin": True,
    "revenue_growth": True,
    "net_income_growth": True,
    "net_debt_to_ebitda": False,  # 杠杆越低越好
    "current_ratio": True,
}

METRIC_LABELS = {
    "pe": "P/E (TTM)",
    "ev_ebitda": "EV/EBITDA",
    "pb": "P/B",
    "gross_margin": "毛利率%",
    "net_margin": "净利率%",
    "roe": "ROE%",
    "cfo_to_ni": "CFO/NI",
    "fcf_margin": "FCF 利润率%",
    "revenue_growth": "营收增速%",
    "net_income_growth": "净利润增速%",
    "net_debt_to_ebitda": "净负债/EBITDA",
    "current_ratio": "流动比率",
}


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


def _safe_growth(cur, prev):
    if cur is None or prev is None:
        return None
    try:
        cur, prev = float(cur), float(prev)
        if prev <= 0:
            return None
        return cur / prev - 1
    except (TypeError, ValueError):
        return None


def fetch_metrics_for_one(ticker: str) -> dict[str, Any]:
    """拉单只股票的 12 项指标（用于横截面对比）。"""
    inc = fmp_client.fetch_income_full(ticker, years=2)
    bs = fmp_client.fetch_balance_sheet(ticker, years=1)
    cf = fmp_client.fetch_cash_flow(ticker, years=1)
    km = fmp_client.fetch_key_metrics(ticker)
    prof = fmp_client.fetch_company_profile(ticker)

    out: dict[str, Any] = {"ticker": ticker, "metrics": {}}

    if not inc or len(inc) < 2 or not bs or not cf:
        out["error"] = "insufficient data"
        return out

    i_t, i_p = inc[0], inc[1]
    b = bs[0]
    c = cf[0]
    market_cap = (prof or {}).get("market_cap")

    revenue = i_t.get("revenue")
    ni = i_t.get("net_income")
    cfo = c.get("operating_cash_flow")
    fcf = c.get("free_cash_flow")
    ebitda = i_t.get("ebitda")
    eps = i_t.get("eps")
    equity = b.get("total_equity")
    shares = i_t.get("weighted_average_shares")

    # 估值指标（部分用 key-metrics-ttm 更准）
    pe = None
    if km and km.get("market_cap") and ni and ni > 0:
        pe = _safe_div(km.get("market_cap"), ni)
    elif eps and eps > 0 and shares:
        # 兜底：从市值/年化 NI 推
        pe = _safe_div(market_cap, ni) if (market_cap and ni and ni > 0) else None

    out["metrics"]["pe"] = round(pe, 2) if pe else None
    out["metrics"]["ev_ebitda"] = (km or {}).get("ev_to_ebitda_ttm")
    out["metrics"]["pb"] = round(_safe_div(market_cap, equity), 2) if (market_cap and equity and equity > 0) else None

    # 盈利
    out["metrics"]["gross_margin"] = round(_safe_div(i_t.get("gross_profit"), revenue) * 100, 2) if (i_t.get("gross_profit") and revenue) else None
    out["metrics"]["net_margin"] = round(_safe_div(ni, revenue) * 100, 2) if (ni is not None and revenue) else None
    out["metrics"]["roe"] = round(_safe_div(ni, equity) * 100, 2) if (ni is not None and equity and equity > 0) else None

    # 质量
    out["metrics"]["cfo_to_ni"] = round(_safe_div(cfo, ni), 2) if (cfo and ni and ni != 0) else None
    out["metrics"]["fcf_margin"] = round(_safe_div(fcf, revenue) * 100, 2) if (fcf is not None and revenue) else None

    # 成长
    out["metrics"]["revenue_growth"] = round(_safe_growth(revenue, i_p.get("revenue")) * 100, 2) if _safe_growth(revenue, i_p.get("revenue")) is not None else None
    out["metrics"]["net_income_growth"] = round(_safe_growth(ni, i_p.get("net_income")) * 100, 2) if _safe_growth(ni, i_p.get("net_income")) is not None else None

    # 财务健康
    net_debt = None
    ld = b.get("long_term_debt") or 0
    sd = b.get("short_term_debt") or 0
    cash = b.get("cash_and_equivalents") or 0
    net_debt = ld + sd - cash
    out["metrics"]["net_debt_to_ebitda"] = round(_safe_div(net_debt, ebitda), 2) if (ebitda and ebitda > 0) else None
    out["metrics"]["current_ratio"] = round(_safe_div(b.get("total_current_assets"), b.get("total_current_liabilities")), 2)

    out["sector"] = (prof or {}).get("sector")
    out["industry"] = (prof or {}).get("industry")
    return out


# 部分指标在负值/异常区间无意义（亏损公司 P/E、净现金公司净负债）
# 这些值应被排除出 percentile 排序，单独标记
INVALID_RANGES = {
    "pe": lambda v: v is not None and v <= 0,                # 亏损 → P/E 负，无意义
    "ev_ebitda": lambda v: v is not None and v <= 0,         # EBITDA 负 → 同上
    "pb": lambda v: v is not None and v <= 0,                # 负净资产
    "net_debt_to_ebitda": lambda v: False,                   # 负值合理（净现金）
    "current_ratio": lambda v: v is not None and v <= 0,
}


def _percentile_rank(values: list[float], target: float) -> float:
    """target 在 values 中的百分位（0-100，越大越靠前）。"""
    if target is None or not values:
        return None
    valid = [v for v in values if v is not None]
    if len(valid) < 2:
        return None
    below = sum(1 for v in valid if v < target)
    eq = sum(1 for v in valid if v == target)
    # 标准 percent rank：(below + 0.5*eq) / total * 100
    return round((below + 0.5 * eq) / len(valid) * 100, 1)


def _resolve_peers(ticker: str, target_industry: str | None,
                   target_sector: str | None, max_peers: int,
                   min_peers: int, sleep_sec: float) -> tuple[list[str], str]:
    """二级回退选 peers：先 industry → 退到 sector → 退到 FMP 默认。

    返回 (peers, scope_label)，scope_label 用于报告标注 peer 群口径。
    """
    raw_peers = fmp_client.fetch_peers(ticker) or []
    candidates = [p for p in raw_peers if p and p != ticker]
    if not candidates:
        return [], "fmp_default_empty"

    # 一次拉所有候选 profile，避免重复请求（缓存到 dict）
    profiles: dict[str, dict] = {}
    for p in candidates[:max_peers * 4]:
        pp = fmp_client.fetch_company_profile(p)
        if pp:
            profiles[p] = pp
        time.sleep(sleep_sec)

    # 一级：同 industry
    industry_match = [p for p in candidates if profiles.get(p, {}).get("industry") == target_industry]
    if len(industry_match) >= min_peers:
        return industry_match[:max_peers], f"same_industry: {target_industry}"

    # 二级：同 sector（industry 候选不足时补足）
    sector_match = [p for p in candidates
                    if profiles.get(p, {}).get("sector") == target_sector
                    and p not in industry_match]
    combined = industry_match + sector_match
    if len(combined) >= min_peers:
        return combined[:max_peers], f"industry+sector_fallback: {target_industry} → {target_sector}"

    # 三级：FMP 默认（无视行业，市值匹配）
    return candidates[:max_peers], "fmp_default_marketcap"


def compare_with_peers(ticker: str, peers: list[str] | None = None,
                       max_peers: int = 8, min_peers: int = 4,
                       sleep_sec: float = 0.3,
                       filter_same_industry: bool = True) -> dict[str, Any]:
    """对比 ticker 与同业 12 项指标。

    peers 为 None 时自动二级回退选取：
      industry → sector → FMP 默认。任何级别下，达到 min_peers 即停止。
    单一 peer 时分位排名只能 0/50/100，无统计意义 → min_peers 默认 4。
    """
    target_prof = fmp_client.fetch_company_profile(ticker)
    target_industry = (target_prof or {}).get("industry")
    target_sector = (target_prof or {}).get("sector")

    if peers is None:
        if filter_same_industry:
            peers, scope = _resolve_peers(ticker, target_industry, target_sector,
                                          max_peers, min_peers, sleep_sec)
        else:
            raw = fmp_client.fetch_peers(ticker) or []
            peers = [p for p in raw if p and p != ticker][:max_peers]
            scope = "fmp_default_marketcap"
    else:
        scope = "user_specified"

    target_data = fetch_metrics_for_one(ticker)
    time.sleep(sleep_sec)

    peers_data = []
    for p in peers:
        try:
            d = fetch_metrics_for_one(p)
            if not d.get("error"):
                peers_data.append(d)
        except Exception as e:
            logger.warning("peer %s fetch failed: %s", p, e)
        time.sleep(sleep_sec)

    if not peers_data:
        return {"ticker": ticker, "error": "no peer data available", "peers_tried": peers}

    # 算每个指标的分位
    rankings = {}
    for metric in METRIC_LABELS:
        target_raw = target_data.get("metrics", {}).get(metric)
        peer_raw = [d["metrics"].get(metric) for d in peers_data]

        # 排除该指标的无意义值（如亏损公司的负 P/E）
        invalid_check = INVALID_RANGES.get(metric, lambda v: False)
        target_invalid = invalid_check(target_raw)
        target_v = None if target_invalid else target_raw
        peer_vals_clean = [v for v in peer_raw if v is not None and not invalid_check(v)]

        # 样本不足以做分位（包括 target 自己在内 < min_peers+1）
        n_for_rank = len(peer_vals_clean) + (1 if target_v is not None else 0)
        if target_v is None or n_for_rank < min_peers + 1:
            pct = None
            pct_better = None
        else:
            all_vals = peer_vals_clean + [target_v]
            pct = _percentile_rank(all_vals, target_v)
            # 若指标越低越好，反向显示（"行业领先程度"）
            if pct is not None and not HIGHER_IS_BETTER[metric]:
                pct_better = 100 - pct
            else:
                pct_better = pct

        valid_peer_vals = [v for v in peer_raw if v is not None]
        rankings[metric] = {
            "target_value": target_raw,  # 原始值（含负值），方便人审
            "target_excluded_reason": "invalid_range" if target_invalid else None,
            "peer_median": round(_median(valid_peer_vals), 2) if valid_peer_vals else None,
            "peer_min": round(min(valid_peer_vals), 2) if valid_peer_vals else None,
            "peer_max": round(max(valid_peer_vals), 2) if valid_peer_vals else None,
            "percentile_better": pct_better,
            "n_peers_valid": len(peer_vals_clean),  # 实际参与排序的同业数
            "n_peers_total": len(valid_peer_vals),  # 含被排除的无效值
        }

    valid_pcts = [r["percentile_better"] for r in rankings.values() if r["percentile_better"] is not None]
    composite = round(sum(valid_pcts) / len(valid_pcts), 1) if valid_pcts else None

    return {
        "ticker": ticker,
        "sector": target_data.get("sector"),
        "industry": target_data.get("industry"),
        "peers": [d["ticker"] for d in peers_data],
        "n_peers": len(peers_data),
        "peer_scope": scope,  # 标注口径：industry / sector_fallback / fmp_default
        "min_peers_required": min_peers,
        "rankings": rankings,
        "composite_percentile": composite,
        "verdict": _composite_verdict(composite),
        "source": "FMP/stock-peers + key-metrics + statements",
    }


def _median(vals: list[float]) -> float:
    vals = sorted(vals)
    n = len(vals)
    if n == 0:
        return None
    if n % 2 == 1:
        return vals[n // 2]
    return (vals[n // 2 - 1] + vals[n // 2]) / 2


def _composite_verdict(p: float | None) -> str:
    if p is None:
        return "❓ insufficient data"
    if p >= 75:
        return "🟢 行业领先（综合分位 ≥ 75%）"
    if p >= 50:
        return "🟡 优于中位（50-75%）"
    if p >= 25:
        return "🟠 行业落后（25-50%）"
    return "🔴 行业末端（< 25%）"


# ────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────

def _print_report(r: dict[str, Any]) -> None:
    if r.get("error"):
        print(f"⚠️ {r['error']}")
        return
    print("=" * 90)
    print(f"  📊 同业横向对标 — {r['ticker']} ({r.get('industry') or r.get('sector') or '?'})")
    print(f"  Peer 口径: {r.get('peer_scope', '?')} (min_peers={r.get('min_peers_required')})")
    print(f"  对标 {r['n_peers']} 家同业: {', '.join(r['peers'])}")
    print(f"  综合分位: {r['composite_percentile']}% → {r['verdict']}")
    print("=" * 90)
    print(f"\n{'指标':<18}{'本公司':>14}{'同业中位':>14}{'同业最低':>14}{'同业最高':>14}{'分位':>10}")
    print("-" * 90)
    for metric, label in METRIC_LABELS.items():
        rk = r["rankings"].get(metric, {})
        tv = rk.get("target_value")
        med = rk.get("peer_median")
        lo = rk.get("peer_min")
        hi = rk.get("peer_max")
        pct = rk.get("percentile_better")
        pct_str = f"{pct}%" if pct is not None else "—"
        # 分位染色
        if pct is None:
            mark = "⚪"
        elif pct >= 75:
            mark = "🟢"
        elif pct >= 50:
            mark = "🟡"
        elif pct >= 25:
            mark = "🟠"
        else:
            mark = "🔴"
        def fmt(v):
            return f"{v:.2f}" if v is not None else "—"
        print(f"{label:<18}{fmt(tv):>14}{fmt(med):>14}{fmt(lo):>14}{fmt(hi):>14}  {mark} {pct_str:>6}")
    print("=" * 90)


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description="同业 12 项财务指标对标")
    parser.add_argument("ticker", help="目标股票")
    parser.add_argument("--peers", nargs="+", help="自定义同业列表")
    parser.add_argument("--max-peers", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out")
    args = parser.parse_args()

    if not fmp_client.is_available():
        print("⚠️ FMP_API_KEY 未配置")
        return 1

    r = compare_with_peers(args.ticker, peers=args.peers, max_peers=args.max_peers)
    if args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        _print_report(r)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(r, f, indent=2, ensure_ascii=False)
        print(f"\n💾 已保存: {args.out}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
