"""AI 长期主线 · 真实数据适配器（薄层，全程 force_read_only 只读，绝不写）。

把 DuckDB 取数喂给 ai_long_term_thesis_store 的纯函数。单一来源 stock_db；
独立只读进程用 get_db(force_read_only=True)（带锁重试，避开写库进程）——
绝不在 API/server 进程里调用本模块（会和 write conn 撞 mode，见 stock_db 注释）。

每个 fetch_* 接受可选 con 便于注入 mock 做单测；不传则自开只读连接。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

_LIB = str(Path(__file__).resolve().parents[1] / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

try:
    from stock_research.core.bottleneck_signals import ALL_TICKERS as _BOTTLENECK
except Exception:  # 兜底：与注册表保持一致的 7 只
    _BOTTLENECK = ("GEV", "VRT", "MU", "MSFT", "GOOGL", "AMZN", "META")

# 持仓 symbol → 风险因子：只映有把握的（capex 链/HBM/电力），其余 unclassified。
# 刻意保守、可扩展——宁可标 unclassified，也不乱给因子（overlay 会据此算合并暴露）。
_HOLDING_FACTOR = {
    "GOOGL": "ai_capex_cycle", "MSFT": "ai_capex_cycle", "AMZN": "ai_capex_cycle",
    "META": "ai_capex_cycle", "NVDA": "ai_capex_cycle", "AVGO": "ai_capex_cycle",
    "TSM": "ai_capex_cycle", "ORCL": "ai_capex_cycle",
    "MU": "hbm_cycle", "VRT": "power_buildout", "GEV": "power_buildout",
}

# US AI 可交易 universe 口径（§九.4：system_universe AI/科技链、US/ADR、排除非 AI）
_US_AI_LEVELS = ("core", "adjacent", "enabler")


def _con(con=None):
    if con is not None:
        return con
    import stock_db  # 惰性导入：只有真要开连接才需要 duckdb，便于注入 mock 做单测
    return stock_db.get_db(force_read_only=True)


def fetch_universe_snapshot(con=None, *, as_of: str | None = None) -> dict:
    """同一可交易 universe 的 point-in-time 快照（§九.4 主基准/资格依据）。"""
    c = _con(con)
    as_of = as_of or date.today().isoformat()
    placeholders = ", ".join("?" * len(_US_AI_LEVELS))
    rows = c.execute(
        f"SELECT DISTINCT symbol FROM system_universe "
        f"WHERE active AND market = 'US' AND ai_relevance_level IN ({placeholders}) "
        f"ORDER BY symbol", list(_US_AI_LEVELS)).fetchall()
    return {
        "snapshot_id": f"univ-US-AI-{as_of}",
        "as_of_date": as_of,
        "symbols": [r[0] for r in rows],
        "source_query": "system_universe active US ai_relevance_level in core/adjacent/enabler",
        "excluded_reason": "非US / ai_relevance_level none|unknown / inactive / manual-only watchlist",
    }


def fetch_candidate_sources(con=None, *, recent_runs: int = 5, min_hits: int = 2) -> dict:
    """P0a 候选来源 → 喂 build_candidate_drafts（仍需人工确认）。"""
    c = _con(con)
    theme = [r[0] for r in c.execute(
        "SELECT DISTINCT symbol FROM ai_theme_company_tags "
        "WHERE market = 'US' AND evidence_status IN ('confirmed','candidate') "
        "ORDER BY symbol").fetchall()]
    # 文档 P0a：ETF 共识候选必须“且在系统 universe 内”——join 过滤到 US-AI，剔除 A股/港股
    placeholders = ", ".join("?" * len(_US_AI_LEVELS))
    etf = [r[0] for r in c.execute(
        f"SELECT DISTINCT h.universe_match FROM ai_theme_etf_holdings h "
        f"JOIN system_universe u ON u.symbol = h.universe_match "
        f"WHERE h.universe_match IS NOT NULL AND h.universe_match <> '' "
        f"AND u.active AND u.market = 'US' AND u.ai_relevance_level IN ({placeholders}) "
        f"ORDER BY 1", list(_US_AI_LEVELS)).fetchall()]
    recent = [r[0] for r in c.execute(
        "WITH r AS (SELECT DISTINCT run_id FROM recommendation_picks "
        "           WHERE market = 'US' ORDER BY run_id DESC LIMIT ?) "
        "SELECT symbol FROM recommendation_picks "
        "WHERE market = 'US' AND run_id IN (SELECT run_id FROM r) "
        "GROUP BY symbol HAVING count(DISTINCT run_id) >= ? ORDER BY symbol",
        [recent_runs, min_hits]).fetchall()]
    return {
        "theme_evidence": theme,
        "bottleneck_signal": list(_BOTTLENECK),
        "etf_consensus": etf,
        "recent_recommendation": recent,
    }


def fetch_daily_state_inputs(con=None) -> dict:
    """P2 日变输入 → 喂 build_daily_state。过热取自最新 US run 的 risk_flags_json。"""
    c = _con(con)
    row = c.execute(
        "SELECT run_id FROM recommendation_picks WHERE market = 'US' "
        "ORDER BY run_id DESC LIMIT 1").fetchone()
    latest_run = row[0] if row else None

    hits, price_state = [], {}
    if latest_run:
        for sym, flags in c.execute(
            "SELECT symbol, risk_flags_json FROM recommendation_picks "
            "WHERE run_id = ? AND market = 'US'", [latest_run]).fetchall():
            hits.append(sym)
            # risk_flags_json 是 [{"code","severity","message"}, ...]，取 code
            flagset = {f.get("code") for f in json.loads(flags)} if flags else set()
            if {"OVERHEATED_1Y", "SHORT_TERM_RUNUP_CHASE_RISK"} & flagset:
                price_state[sym] = "过热"
    return {
        "latest_run_id": latest_run,
        "recommendation_hits": hits,
        "price_state_by_symbol": price_state,
    }


def fetch_holdings_for_overlay(con=None, *, thesis: dict | None = None) -> list[dict]:
    """最新一轮真实持仓 → 喂 compute_portfolio_overlay。current_weight 是小数，×100 转 pct。

    风险因子优先用名单成员的口径，其次保守显式映射，否则 unclassified。只读不写回。
    """
    c = _con(con)
    row = c.execute(
        "SELECT review_run_id FROM real_holding_review_items "
        "ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row:
        return []
    run = row[0]
    member_factor = {m["symbol"]: m.get("risk_factor")
                     for m in ((thesis or {}).get("members") or [])}
    out = []
    for sym, weight in c.execute(
        "SELECT symbol, current_weight FROM real_holding_review_items "
        "WHERE review_run_id = ?", [run]).fetchall():
        factor = member_factor.get(sym) or _HOLDING_FACTOR.get(sym, "unclassified")
        out.append({"symbol": sym, "weight_pct": round(float(weight or 0) * 100, 1),
                    "risk_factor": factor})
    return sorted(out, key=lambda h: -h["weight_pct"])
