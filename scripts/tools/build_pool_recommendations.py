#!/usr/bin/env python3
"""Build fast full-pool AI recommendations from today's DuckDB data.

This is the dashboard-facing "AI 推荐" feed.  It ranks every ticker that has a
latest prices snapshot, without excluding watchlist rows.  Deep discovery
(`discover_candidates.py`) can still run offline, but it is too slow to block a
daily dashboard refresh.
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import date, datetime
from statistics import mean, pstdev
from typing import Any

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts", "lib"))

from stock_db import (  # noqa: E402
    get_db,
    fetch_universe_for_ai_recommendations,
    fetch_latest_recommendation_picks,
)
from fx_rates import get_all_fx_to_rmb  # noqa: E402


def _fx_to_usd(ccy: str | None) -> float:
    rates = get_all_fx_to_rmb()
    ccy = (ccy or "USD").strip().upper()
    if ccy == "USD":
        return 1.0
    usd_rmb = rates.get("USD") or 1.0
    ccy_rmb = rates.get(ccy)
    if not ccy_rmb:
        return 1.0
    return ccy_rmb / usd_rmb


def _num(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def _clip(v: float | None, lo: float, hi: float) -> float | None:
    if v is None:
        return None
    return max(lo, min(hi, v))


def _z_map(rows: list[dict], field: str, *, inverse: bool = False) -> dict[str, float]:
    vals = []
    for r in rows:
        v = _num(r.get(field))
        if v is not None:
            vals.append(v)
    if len(vals) < 3:
        return {}
    mu = mean(vals)
    sd = pstdev(vals) or 1.0
    out = {}
    for r in rows:
        v = _num(r.get(field))
        if v is None:
            continue
        z = (v - mu) / sd
        out[r["code"]] = -z if inverse else z
    return out


def _ai_text_score(text: str | None) -> float | None:
    s = (text or "").lower()
    if not s:
        return None
    if any(k in s for k in ["核心", "强", "high", "ai", "gpu", "hbm", "半导体", "算力"]):
        return 1.0
    if any(k in s for k in ["中", "medium", "相关", "待验证"]):
        return 0.25
    if any(k in s for k in ["低", "弱", "low"]):
        return -0.5
    return 0.0


_MARKET_LABEL_BY_V2 = {"US": "United States", "HK": "Hong Kong", "CN": "China"}


def _market_label(code: str, market: str | None) -> str:
    # V2 优先：market 直接来自 system_universe 的 US/HK/CN 标签
    if market and market.upper() in _MARKET_LABEL_BY_V2:
        return _MARKET_LABEL_BY_V2[market.upper()]
    # 后缀兜底（旧调用方传 ticker）
    c = (code or "").upper()
    if c.endswith(".HK") or "港" in (market or ""):
        return "Hong Kong"
    if c.endswith(".TW"):
        return "Taiwan"
    if c.endswith(".KS"):
        return "Korea"
    if c.endswith(".T"):
        return "Japan"
    if c.endswith(".SS") or c.endswith(".SZ") or c.isdigit():
        return "China"
    return "United States"


def _load_pool_rows() -> list[dict]:
    """V2 路径：system_universe + pool_membership 作为候选池；price_daily 取最新行情；
    最新 recommendation_picks 作为 pick_* 字段来源。
    V2 design (docs/V2/产品基线.md)：AI 推荐严禁读 watchlist。"""
    universe = fetch_universe_for_ai_recommendations()
    if not universe:
        return []
    picks = fetch_latest_recommendation_picks()
    picks_by_symbol = {(p["market"], p["symbol"]): p for p in picks}

    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT pd.market, pd.symbol,
                   pd.currency, pd.market_cap, pd.forward_pe, pd.peg_ratio,
                   pd.ytd_pct, pd.one_year_pct, pd.one_month_pct, pd.one_week_pct,
                   pd.close, pd.trade_date
            FROM price_daily pd
            JOIN (
                SELECT market, symbol, MAX(trade_date) AS d
                FROM price_daily
                GROUP BY market, symbol
            ) latest
              ON pd.market = latest.market AND pd.symbol = latest.symbol
             AND pd.trade_date = latest.d
            """
        ).fetchall()
    finally:
        conn.close()

    price_cols = [
        "market", "symbol", "currency", "market_cap", "forward_pe", "peg_ratio",
        "ytd_pct", "one_year_pct", "one_month_pct", "one_week_pct",
        "close", "trade_date",
    ]
    price_by_symbol = {(r[0], r[1]): dict(zip(price_cols, r)) for r in rows}

    out = []
    for u in universe:
        key = (u["market"], u["symbol"])
        price = price_by_symbol.get(key) or {}
        pick = picks_by_symbol.get(key) or {}
        factor_scores = pick.get("factor_scores") or {}
        row = {
            "code": u["symbol"],
            "name": u.get("name") or u["symbol"],
            "market": u.get("market") or "",
            "industry": u.get("industry") or "",
            "theme": u.get("theme") or "",
            "ai_relevance": "科技/AI universe",
            "pool_source": u.get("source") or "",
            # 行情字段（price_daily 没有的列保持 None）
            "currency": price.get("currency"),
            "market_cap": price.get("market_cap"),
            "forward_pe": price.get("forward_pe"),
            "peg_ratio": price.get("peg_ratio"),
            "earnings_growth_pct": None,  # V2 price_daily 不再存 growth 字段
            "revenue_growth_pct": None,
            "ytd_pct": price.get("ytd_pct"),
            "one_year_pct": price.get("one_year_pct"),
            "one_month_pct": price.get("one_month_pct"),
            "one_week_pct": price.get("one_week_pct"),
            # V2 recommendation_picks 字段映射
            "pick_rating": pick.get("rating"),
            "pick_total_score": pick.get("total_score"),
            "pick_ai_score": None,  # V2 没有 ai_score 细分
            "pick_val_score": factor_scores.get("valuation"),
            "pick_trend_score": factor_scores.get("momentum"),
            "pick_cred_score": factor_scores.get("data_quality"),
            "pick_model_source": pick.get("run_id"),
            "pick_signal": pick.get("signal") or "buy",
            "pick_coverage_score": factor_scores.get("coverage"),
            "pick_missing_factors": None,
        }
        out.append(row)
    return out


def build_recommendations(top: int = 20, out_path: str = "data/discovery_candidates.json") -> dict:
    rows = _load_pool_rows()
    if not rows:
        raise RuntimeError(
            "system_universe / price_daily 无数据，无法生成全池 AI 推荐 — "
            "确认 daily_refresh 的 step 1 (fetch_stock_prices) 已成功跑过"
        )

    # Local derived fields used for cross-sectional z-scores.
    for r in rows:
        cap = _num(r.get("market_cap"))
        fx = _fx_to_usd(r.get("currency"))
        r["market_cap_usd"] = cap * fx if cap is not None else None
        one_year = _clip(_num(r.get("one_year_pct")), -80, 250)
        one_month = _clip(_num(r.get("one_month_pct")), -50, 80)
        ytd = _clip(_num(r.get("ytd_pct")), -80, 180)
        parts = [x for x in [one_year, one_month, ytd] if x is not None]
        r["momentum_proxy"] = mean(parts) if parts else None
        growth_parts = [
            _clip(_num(r.get("earnings_growth_pct")), -100, 120),
            _clip(_num(r.get("revenue_growth_pct")), -60, 120),
        ]
        growth_parts = [x for x in growth_parts if x is not None]
        r["growth_proxy"] = mean(growth_parts) if growth_parts else None
        peg = _num(r.get("peg_ratio"))
        fpe = _num(r.get("forward_pe"))
        r["valuation_proxy"] = mean([
            x for x in [
                -math.log(max(peg, 0.05)) if peg and peg > 0 else None,
                -math.log(max(fpe, 1.0)) if fpe and fpe > 0 else None,
            ] if x is not None
        ]) if (peg and peg > 0) or (fpe and fpe > 0) else None
        r["ai_text_proxy"] = _ai_text_score(" ".join([
            str(r.get("ai_relevance") or ""),
            str(r.get("theme") or ""),
            str(r.get("industry") or ""),
        ]))
        pick_total = _num(r.get("pick_total_score"))
        pick_signal = str(r.get("pick_signal") or "").lower()
        pick_coverage = _num(r.get("pick_coverage_score"))
        r["pick_score_proxy"] = (
            (pick_total - 60.0) / 18.0
            if pick_signal == "buy"
            and pick_total is not None
            and (pick_coverage is None or pick_coverage >= 0.50)
            else None
        )

    z_mom = _z_map(rows, "momentum_proxy")
    z_growth = _z_map(rows, "growth_proxy")
    z_val = _z_map(rows, "valuation_proxy")
    z_ai = _z_map(rows, "ai_text_proxy")

    scored = []
    skipped_avoid = 0
    for r in rows:
        if str(r.get("pick_signal") or "").lower() == "avoid":
            skipped_avoid += 1
            continue
        code = r["code"]
        pieces = []
        weights = []
        if r.get("pick_score_proxy") is not None:
            pieces.append(r["pick_score_proxy"])
            weights.append(0.40)
        for zmap, weight in [(z_mom, 0.25), (z_growth, 0.15), (z_val, 0.10), (z_ai, 0.10)]:
            if code in zmap:
                pieces.append(zmap[code])
                weights.append(weight)
        if not pieces:
            continue
        composite = sum(v * w for v, w in zip(pieces, weights)) / sum(weights)
        f_score = None
        val_score = _num(r.get("pick_val_score"))
        if val_score is not None and val_score > 0:
            f_score = round(val_score / 3)
        scored.append((composite, r, f_score))

    scored.sort(key=lambda x: x[0], reverse=True)
    candidates = []
    for rank, (score, r, f_score) in enumerate(scored[:top], 1):
        code = r["code"]
        momentum = _num(r.get("momentum_proxy"))
        candidate = {
            "rank": rank,
            "ticker": code,
            "name": r.get("name") or code,
            "sector": r.get("industry") or r.get("theme") or "",
            "location": _market_label(code, r.get("market")),
            "market": _market_label(code, r.get("market")),
            "etfs": ["DB_POOL"],
            "market_cap_usd": r.get("market_cap_usd"),
            "f_score": f_score,
            "momentum_12_1": momentum,
            "pead": _num(r.get("growth_proxy")),
            "analyst_score": _num(r.get("pick_cred_score")) or 0.0,
            "composite_z": round(score, 4),
            "detail": {
                "fast_pool": True,
                "pick_rating": r.get("pick_rating"),
                "pick_total_score": r.get("pick_total_score"),
                "pick_model_source": r.get("pick_model_source"),
                "pick_signal": r.get("pick_signal"),
                "pick_coverage_score": r.get("pick_coverage_score"),
                "pick_missing_factors": r.get("pick_missing_factors"),
                "momentum_proxy": momentum,
                "growth_proxy": r.get("growth_proxy"),
                "valuation": {
                    "forward_pe": r.get("forward_pe"),
                    "peg_ratio": r.get("peg_ratio"),
                },
                "ai_relevance": r.get("ai_relevance"),
                "theme": r.get("theme"),
                "one_year_pct": r.get("one_year_pct"),
                "one_month_pct": r.get("one_month_pct"),
                "earnings_growth_pct": r.get("earnings_growth_pct"),
                "revenue_growth_pct": r.get("revenue_growth_pct"),
            },
        }
        candidates.append(candidate)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "universe_size": len(rows),
        "watchlist_excluded": 0,
        "avoid_excluded": skipped_avoid,
        "universe_scope": "tech_universe_db_prices",
        "exclude_watchlist": False,
        "etf_sources": ["tech/AI universe", "DuckDB prices", "latest picks when available"],
        "method": "Fast full-pool score: latest v6 pick score + momentum/growth/valuation/AI-theme proxies",
        "min_market_cap_usd": None,
        "candidates": candidates,
    }

    abs_out = out_path if os.path.isabs(out_path) else os.path.join(REPO, out_path)
    os.makedirs(os.path.dirname(abs_out), exist_ok=True)
    with open(abs_out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

    # 2026-05-21 V1 cutover：V1 discovery_history/tracking 表已删；
    # V2 历史在 recommendation_runs + pick_outcomes 由 build_v2_recommendations + evaluate_v2_picks 维护
    return payload


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--out", default="data/discovery_candidates.json")
    args = parser.parse_args()
    payload = build_recommendations(top=args.top, out_path=args.out)
    print(
        f"✅ 全池 AI 推荐已生成: {len(payload['candidates'])} 只 / "
        f"universe {payload['universe_size']} 只 → {args.out}"
    )
    for c in payload["candidates"][:10]:
        print(f"  {c['rank']:>2}. {c['ticker']:<10} {c['composite_z']:+.2f} {c['name']}")


if __name__ == "__main__":
    main()
