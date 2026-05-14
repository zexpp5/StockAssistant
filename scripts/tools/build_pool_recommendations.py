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
sys.path.insert(0, os.path.join(REPO, "scripts", "lib"))

from stock_db import get_db, upsert_discovery_history  # noqa: E402


FX_TO_USD = {
    "USD": 1.0,
    "CNY": 0.139,
    "HKD": 0.128,
    "TWD": 0.031,
    "KRW": 0.00074,
    "JPY": 0.0067,
    "EUR": 1.07,
    "GBP": 1.27,
    "AUD": 0.66,
}


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


def _market_label(code: str, market: str | None) -> str:
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
    conn = get_db()
    try:
        rows = conn.execute(
            """
            WITH latest_prices AS (
              SELECT *
              FROM prices
              WHERE (code, date) IN (SELECT code, MAX(date) FROM prices GROUP BY code)
            ),
            latest_picks AS (
              SELECT *
              FROM picks
              WHERE (code, pick_date) IN (SELECT code, MAX(pick_date) FROM picks GROUP BY code)
            )
            SELECT
              p.code, COALESCE(p.name, w.name) AS name,
              COALESCE(w.market, '') AS market,
              COALESCE(w.industry, '') AS industry,
              COALESCE(w.theme, '') AS theme,
              COALESCE(w.ai_relevance, '') AS ai_relevance,
              p.currency, p.market_cap, p.forward_pe, p.peg_ratio,
              p.earnings_growth_pct, p.revenue_growth_pct,
              p.ytd_pct, p.one_year_pct, p.one_month_pct, p.one_week_pct,
              lp.rating AS pick_rating,
              lp.total_score AS pick_total_score,
              lp.ai_score AS pick_ai_score,
              lp.val_score AS pick_val_score,
              lp.trend_score AS pick_trend_score,
              lp.cred_score AS pick_cred_score,
              lp.model_source AS pick_model_source,
              COALESCE(lp.signal, 'buy') AS pick_signal,
              lp.coverage_score AS pick_coverage_score,
              lp.missing_factors AS pick_missing_factors
            FROM latest_prices p
            LEFT JOIN watchlist w ON w.code = p.code
            LEFT JOIN latest_picks lp ON lp.code = p.code
            """
        ).fetchall()
    finally:
        conn.close()

    cols = [
        "code", "name", "market", "industry", "theme", "ai_relevance",
        "currency", "market_cap", "forward_pe", "peg_ratio",
        "earnings_growth_pct", "revenue_growth_pct",
        "ytd_pct", "one_year_pct", "one_month_pct", "one_week_pct",
        "pick_rating", "pick_total_score", "pick_ai_score", "pick_val_score",
        "pick_trend_score", "pick_cred_score", "pick_model_source",
        "pick_signal", "pick_coverage_score", "pick_missing_factors",
    ]
    return [dict(zip(cols, r)) for r in rows]


def build_recommendations(top: int = 20, out_path: str = "data/discovery_candidates.json") -> dict:
    rows = _load_pool_rows()
    if not rows:
        raise RuntimeError("prices 表没有最新快照，无法生成全池 AI 推荐")

    # Local derived fields used for cross-sectional z-scores.
    for r in rows:
        cap = _num(r.get("market_cap"))
        fx = FX_TO_USD.get((r.get("currency") or "USD").upper(), 1.0)
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
        "universe_scope": "all_db_prices",
        "exclude_watchlist": False,
        "etf_sources": ["DuckDB prices", "latest picks when available"],
        "method": "Fast full-pool score: latest v6 pick score + momentum/growth/valuation/AI-theme proxies",
        "min_market_cap_usd": None,
        "candidates": candidates,
    }

    abs_out = out_path if os.path.isabs(out_path) else os.path.join(REPO, out_path)
    os.makedirs(os.path.dirname(abs_out), exist_ok=True)
    with open(abs_out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

    conn = get_db()
    try:
        today = date.today()
        conn.execute("DELETE FROM discovery_tracking WHERE generated_date = ?", [today])
        conn.execute("DELETE FROM discovery_history WHERE generated_date = ?", [today])
        upsert_discovery_history(candidates, generated_date=today, conn=conn)
    finally:
        conn.close()

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
