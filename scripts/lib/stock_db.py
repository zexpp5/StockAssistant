"""
DuckDB 持久化层 — 股票时间序列本地仓
─────────────────────────────────────────
设计原则：
  • 飞书 Base 仍是「人工编辑入口 + 当前快照」
  • DuckDB 是「历史时间序列 + 回测分析」的 single source of truth
  • 三张表，主键都含日期 → 同日重跑覆盖、跨日累积

表结构：
  prices   每日 watchlist 的全字段快照（按日 + 代码主键）
  picks    每日入选记录（按入选日 + 代码主键）
  reviews  对入选记录的跟踪刷新（按 review_date + pick_date + 代码主键）

调用方式（其它脚本里）：
  from stock_db import get_db, upsert_prices, upsert_picks, upsert_reviews
  upsert_prices(price_results)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, date
from typing import Iterable, Mapping, Any

import duckdb

# 2026-05-15: 默认切到 clean v2 DB；旧 stock_history.duckdb 已不再作为生产入口。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.environ.get(
    "STOCK_DB_PATH",
    os.path.join(_REPO_ROOT, "stock_history_v2.duckdb"),
)


# ============================================================
# Schema
# ============================================================

SCHEMA_SQL = """
-- 2026-05-21 V1 cutover：删除 prices / picks / reviews / watchlist /
-- discovery_history / discovery_tracking / earnings_history 等 V1 表 CREATE 语句。
-- V2 schema (system_universe / pool_membership / price_daily / recommendation_runs /
-- recommendation_picks / portfolio_plans / pick_outcomes / manual_watchlist /
-- holdings / source_raw_snapshots / 等) 由 scripts/tools/init_stock_db_v2.py 管理。
-- stock_db.py 这里只保留两个共享的小表（user_config + holdings）。
-- 其它 V2 表全部由 scripts/tools/init_stock_db_v2.py 管理。

CREATE TABLE IF NOT EXISTS user_config (
    key        VARCHAR PRIMARY KEY,
    value      VARCHAR NOT NULL,  -- 任意 JSON 字符串
    updated_at TIMESTAMP
);

-- 持仓表 (V2 schema)
-- 主键：一只股可以分批建仓(不同时间不同价)，所以用自增 id 而非 code
-- source：'manual'(用户手填) / 'ai_plan'(从 AI 组合方案抄进来)
CREATE SEQUENCE IF NOT EXISTS holdings_id_seq;
CREATE TABLE IF NOT EXISTS holdings (
    id          INTEGER   PRIMARY KEY DEFAULT nextval('holdings_id_seq'),
    market      VARCHAR   NOT NULL,
    symbol      VARCHAR   NOT NULL,
    entry_price DOUBLE    NOT NULL,
    shares      DOUBLE    NOT NULL,
    entry_date  DATE,
    source      VARCHAR   DEFAULT 'manual',
    notes       VARCHAR,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# user_config 已知 key 的默认值（首次读取或被删除时返回）
USER_CONFIG_DEFAULTS = {
    "total_capital": 500000,   # 进场本金，跑批 + 前端共用
    "stoploss_line": 300000,   # 止损红线（组合市值跌至此则强制清仓）
}


def get_db(path: str = DB_PATH) -> duckdb.DuckDBPyConnection:
    """打开 DuckDB 连接并保证 user_config + holdings 表存在。

    2026-05-21 V1 cutover：删除原 4 个 UPDATE picks legacy 数据修复 SQL（picks 表已删）。
    V2 表 schema 由 init_stock_db_v2.py 负责。
    """
    conn = duckdb.connect(path)
    conn.execute(SCHEMA_SQL)
    return conn


# ============================================================
# Helpers
# ============================================================

def _to_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        # 支持 "2026-05-09" 或 "2026-05-09 12:06"
        return datetime.strptime(v[:10], "%Y-%m-%d").date()
    if isinstance(v, (int, float)):
        # 飞书时间戳是毫秒
        ts = v / 1000 if v > 1e12 else v
        return datetime.fromtimestamp(ts).date()
    raise TypeError(f"unsupported date input: {v!r}")


def _to_ts(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        # 支持 "2026-05-09 12:06" 或 ISO
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(v, fmt)
            except ValueError:
                continue
        return datetime.fromisoformat(v)
    if isinstance(v, (int, float)):
        ts = v / 1000 if v > 1e12 else v
        return datetime.fromtimestamp(ts)
    raise TypeError(f"unsupported timestamp input: {v!r}")


# ============================================================
# Upsert helpers
# ============================================================

PRICE_COLS = [
    "date", "code", "name", "yf_ticker", "price", "prev_close", "currency",
    "market_cap", "forward_pe", "trailing_pe", "peg_ratio",
    "earnings_growth_pct", "revenue_growth_pct",
    "ytd_pct", "one_year_pct", "one_month_pct", "one_week_pct", "fetched_at",
]


PICK_COLS = [
    "pick_date", "code", "name", "market", "rating",
    "total_score", "ai_score", "val_score", "trend_score", "cred_score",
    "ai_relevance", "theme", "entry_price", "entry_currency",
    "peg_at_pick", "fpe_at_pick", "ytd_at_pick", "one_week_at_pick", "one_year_at_pick",
    "model_source", "signal", "coverage_score", "missing_factors", "factor_weights_used",
    "universe_scope", "source_origin",
]


def _infer_signal_from_rating(rating: str | None) -> str:
    """fallback：写入方未填 signal 时从 rating 文本推断。

    新代码应直接传 signal=buy/avoid/watch；本函数仅给历史 row 兜底。
    daily_picks_v5 的四类 rating（line 420-429）和 signal 的对应：
      - "⛔ 不建议（z ≤ -0.5）"                       → avoid
      - "⭐ 观察（-0.5 < z < cutoff）"                → watch（z 在中性区，不入选也不淘汰）
      - "⭐ 关注 / ⭐⭐ 推荐 / ⭐⭐⭐ 强烈推荐"           → buy
      - 空 rating                                    → watch（最保守，避免误标 buy）
    """
    text = (rating or "").strip()
    if not text:
        return "watch"
    if "⛔" in text or "不建议" in text:
        return "avoid"
    if "观察" in text:
        return "watch"
    return "buy"


REVIEW_COLS = [
    "review_date", "pick_date", "code", "name",
    "entry_price", "current_price", "pct", "days_held",
    "grade", "rating", "theme",
    "entry_spy_price", "current_spy_price", "alpha_pct",
    "model_source", "signal", "benchmark_code", "benchmark_pct", "is_success",
]


def fetch_research_records_v2(*, conn: duckdb.DuckDBPyConnection | None = None) -> list[dict]:
    """V2 路径：从 system_universe + price_daily + 最新 recommendation_picks 拼出
    给「个股研究 / 产业链地图 / 买前审查」的展示用 records。

    与 fetch_records_view 形状对齐（同字段名），但纯 V2 表，没有任何 V1 watchlist/prices 依赖。
    缺失的 V1 主观字段（business / ai_logic / conclusion / risks / peers / rhythm /
    chain / chain_tier / chain_role / layman_intro 等）填 None — 前端做空值处理或隐藏。
    """
    own = conn is None
    if own:
        conn = get_db()
    rows = conn.execute("""
        WITH latest_price AS (
            SELECT * FROM price_daily
            QUALIFY ROW_NUMBER() OVER (PARTITION BY market, symbol ORDER BY trade_date DESC, fetched_at DESC) = 1
        ),
        latest_run AS (
            SELECT run_id, generated_at
            FROM recommendation_runs
            WHERE universe_scope = 'system_tech_universe' AND status = 'generated'
            ORDER BY generated_at DESC LIMIT 1
        ),
        latest_picks AS (
            SELECT rp.market, rp.symbol, rp.rating, rp.signal, rp.total_score, rp.factor_scores_json
            FROM recommendation_picks rp JOIN latest_run lr USING(run_id)
        ),
        latest_snap AS (
            SELECT
                market,
                json_extract_string(payload_json, '$.symbol') AS symbol,
                json_extract_string(payload_json, '$.business') AS business,
                json_extract_string(payload_json, '$.ai_logic') AS ai_logic,
                json_extract_string(payload_json, '$.earnings') AS earnings,
                json_extract_string(payload_json, '$.conclusion') AS conclusion,
                json_extract_string(payload_json, '$.risks') AS risks,
                json_extract_string(payload_json, '$.info_breakdown') AS info_breakdown,
                json_extract_string(payload_json, '$.source_text') AS source_text,
                json_extract_string(payload_json, '$.credibility') AS credibility,
                json_extract_string(payload_json, '$.verification') AS verification,
                fetched_at
            FROM source_raw_snapshots
            WHERE source = 'v2_system_enrichment'
              AND json_extract_string(payload_json, '$.symbol') IS NOT NULL
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY market, json_extract_string(payload_json, '$.symbol')
                ORDER BY business_date DESC, fetched_at DESC
            ) = 1
        )
        SELECT
            u.symbol AS code, u.name, u.market,
            ls.business,
            u.industry,
            COALESCE(u.theme, '科技/AI universe') AS ai_relevance,
            COALESCE(ls.ai_logic, ls.source_text) AS ai_logic,
            ls.conclusion,
            ls.risks,
            NULL AS peers, NULL AS rhythm, NULL AS status, u.source, ls.credibility,
            ls.earnings, ls.verification, ls.info_breakdown,
            NULL AS chain, NULL AS chain_tier, NULL AS chain_role, NULL AS layman_intro,
            u.theme,
            lp.close          AS latest_price,
            lp.market_cap     AS yf_market_cap,
            lp.forward_pe,
            lp.peg_ratio      AS peg,
            NULL              AS earnings_growth_pct,
            lp.ytd_pct,
            lp.one_year_pct,
            lp.one_month_pct,
            lp.one_week_pct,
            lp.trade_date     AS price_date,
            lp.fetched_at     AS price_fetched_at,
            COALESCE(ls.fetched_at, u.last_seen_at) AS analysis_updated_at,
            lpk.rating        AS pick_rating,
            lpk.signal        AS pick_signal,
            lpk.total_score   AS pick_total_score,
            lpk.factor_scores_json AS pick_factor_scores_json
        FROM system_universe u
        LEFT JOIN latest_price lp ON lp.market = u.market AND lp.symbol = u.symbol
        LEFT JOIN latest_picks lpk ON lpk.market = u.market AND lpk.symbol = u.symbol
        LEFT JOIN latest_snap ls ON ls.market = u.market AND ls.symbol = u.symbol
        WHERE u.active = TRUE
        ORDER BY u.market, u.symbol
    """).fetchall()
    cols = [
        "code", "name", "market", "business", "industry",
        "ai_relevance", "ai_logic", "conclusion", "risks", "peers",
        "rhythm", "status", "source", "credibility",
        "earnings", "verification", "info_breakdown",
        "chain", "chain_tier", "chain_role", "layman_intro",
        "theme",
        "latest_price", "yf_market_cap", "forward_pe", "peg",
        "earnings_growth_pct", "ytd_pct", "one_year_pct",
        "one_month_pct", "one_week_pct", "price_date",
        "price_fetched_at", "analysis_updated_at",
        "pick_rating", "pick_signal", "pick_total_score", "pick_factor_scores_json",
    ]
    out = [dict(zip(cols, r)) for r in rows]
    if own:
        conn.close()
    return out


# ============================================================
# Watchlist CRUD（2026-05-11 起：从飞书迁移到 DuckDB，权威源）
# ============================================================

def fetch_picks_normalized(
    *,
    universe_scope: str = "system_tech_universe",
    conn: duckdb.DuckDBPyConnection | None = None,
) -> list[dict]:
    """V2 picks 包装成 V1-normalized shape，给 defense_signals / realtime_defense /
    pyfolio_tearsheet / monthly_letter 等遗留消费者使用。

    每条返回：
      {"record_id": "", "fields": {}, "normalized": {
          name, code, rating, score, theme, ai_level, pick_date(ms),
          peg_at_pick, pe_at_pick, y1_at_pick, cum_pct, days_held,
      }}
    cum_pct = (price_daily.close 今天 / recommendation_picks.entry_price - 1) × 100
    days_held = today - run_date
    """
    own = conn is None
    if own:
        conn = get_db()
    run_row = conn.execute(
        """
        SELECT run_id, run_date FROM recommendation_runs
        WHERE universe_scope = ? AND status = 'generated'
        ORDER BY generated_at DESC LIMIT 1
        """,
        [universe_scope],
    ).fetchone()
    if not run_row:
        if own:
            conn.close()
        return []
    run_id, run_date = run_row
    rows = conn.execute(
        """
        WITH latest_close AS (
            SELECT market, symbol, close, trade_date FROM price_daily
            QUALIFY ROW_NUMBER() OVER (PARTITION BY market, symbol ORDER BY trade_date DESC, fetched_at DESC) = 1
        )
        SELECT p.market, p.symbol, p.name, p.rating, p.signal,
               p.total_score, p.entry_price, lc.close AS current_close, lc.trade_date
        FROM recommendation_picks p
        LEFT JOIN latest_close lc ON lc.market = p.market AND lc.symbol = p.symbol
        WHERE p.run_id = ?
        ORDER BY p.rank
        """,
        [run_id],
    ).fetchall()
    from datetime import date as _date
    today = _date.today()
    out = []
    for market, symbol, name, rating, signal, total_score, entry_price, current_close, trade_date in rows:
        cum_pct = None
        if entry_price and current_close and float(entry_price) > 0:
            cum_pct = round((float(current_close) / float(entry_price) - 1) * 100, 2)
        days_held = (today - run_date).days if run_date else 0
        out.append({
            "record_id": "",
            "fields": {},
            "normalized": {
                "name": name or symbol, "code": symbol,
                "rating": rating or "",
                "score": total_score, "theme": "科技/AI", "ai_level": "科技/AI universe",
                "pick_date": int(datetime.combine(run_date, datetime.min.time()).timestamp() * 1000) if run_date else None,
                "peg_at_pick": None, "pe_at_pick": None, "y1_at_pick": None,
                "cum_pct": cum_pct, "days_held": days_held,
                "signal": signal,
            },
        })
    if own:
        conn.close()
    return out


MANUAL_WATCHLIST_COLS = ["market", "symbol", "name", "notes"]


def fetch_manual_watchlist(
    *,
    market: str | None = None,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> list[dict]:
    """V2 manual_watchlist（dashboard 手动添加的自选股）。

    返回 dict: market / symbol / name / notes / created_at / updated_at + code(=symbol) alias。
    可选按 market 过滤。
    """
    own = conn is None
    if own:
        conn = get_db()
    sql = ("SELECT market, symbol, name, notes, created_at, updated_at "
           "FROM manual_watchlist")
    params: list = []
    if market:
        sql += " WHERE UPPER(market) = ?"
        params.append(market.upper())
    sql += " ORDER BY market, symbol"
    rows = conn.execute(sql, params).fetchall()
    cols = ["market", "symbol", "name", "notes", "created_at", "updated_at"]
    out = [dict(zip(cols, r)) for r in rows]
    for r in out:
        r["code"] = r["symbol"]
    if own:
        conn.close()
    return out


def upsert_manual_watchlist(
    rows: Iterable[Mapping[str, Any]],
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    """新增 / 更新 manual_watchlist（按 (market, symbol) PK）。"""
    own = conn is None
    if own:
        conn = get_db()
    n = 0
    now = datetime.now()
    for r in rows:
        symbol = r.get("symbol") or r.get("code")
        if not symbol:
            continue
        market = r.get("market") or _infer_market_from_ticker(symbol)
        conn.execute(
            """
            INSERT INTO manual_watchlist (market, symbol, name, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (market, symbol) DO UPDATE SET
              name = excluded.name,
              notes = excluded.notes,
              updated_at = excluded.updated_at
            """,
            [market, symbol, r.get("name"), r.get("notes"), now, now],
        )
        n += 1
    if own:
        conn.close()
    return n


def fetch_manual_watchlist_enriched(
    *,
    market: str | None = None,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> list[dict]:
    """manual_watchlist JOIN system_universe + price_daily 拿出富字段，
    供「自选股·AI 优选」三个 jobs（daily_picks_v5 / hk_picks / a_share_picks）使用。

    返回 dict 字段（与 V1 fetch_watchlist 兼容）：
      code, name, market, industry, theme, ai_relevance, ai_logic, conclusion,
      risks, credibility, latest_price, ytd_pct, one_year_pct, one_month_pct,
      one_week_pct, forward_pe, peg, earnings_growth_pct
    """
    own = conn is None
    if own:
        conn = get_db()
    sql = """
        WITH latest_price AS (
            SELECT * FROM price_daily
            QUALIFY ROW_NUMBER() OVER (PARTITION BY market, symbol ORDER BY trade_date DESC, fetched_at DESC) = 1
        )
        SELECT
            w.symbol AS code, COALESCE(w.name, u.name, w.symbol) AS name,
            w.market, u.industry, u.theme,
            NULL AS ai_relevance, NULL AS ai_logic, NULL AS conclusion,
            NULL AS risks, NULL AS credibility,
            lp.close AS latest_price,
            lp.ytd_pct, lp.one_year_pct, lp.one_month_pct, lp.one_week_pct,
            lp.forward_pe, lp.peg_ratio AS peg, NULL AS earnings_growth_pct,
            lp.currency, lp.market_cap
        FROM manual_watchlist w
        LEFT JOIN system_universe u ON u.market = w.market AND u.symbol = w.symbol
        LEFT JOIN latest_price lp ON lp.market = w.market AND lp.symbol = w.symbol
    """
    params: list = []
    if market:
        sql += " WHERE UPPER(w.market) = ?"
        params.append(market.upper())
    sql += " ORDER BY w.market, w.symbol"
    rows = conn.execute(sql, params).fetchall()
    cols = ["code", "name", "market", "industry", "theme",
            "ai_relevance", "ai_logic", "conclusion", "risks", "credibility",
            "latest_price", "ytd_pct", "one_year_pct", "one_month_pct", "one_week_pct",
            "forward_pe", "peg", "earnings_growth_pct", "currency", "market_cap"]
    out = [dict(zip(cols, r)) for r in rows]
    if own:
        conn.close()
    return out


def delete_manual_watchlist(
    market: str, symbol: str,
    *, conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    own = conn is None
    if own:
        conn = get_db()
    exists = conn.execute(
        "SELECT 1 FROM manual_watchlist WHERE market=? AND symbol=?",
        [market, symbol],
    ).fetchone()
    n = 0
    if exists:
        conn.execute("DELETE FROM manual_watchlist WHERE market=? AND symbol=?", [market, symbol])
        n = 1
    if own:
        conn.close()
    return n


def fetch_universe_for_ai_recommendations(
    *,
    pool_type: str = "system_tech_universe",
    conn: duckdb.DuckDBPyConnection | None = None,
) -> list[dict]:
    """读 system_universe ∩ pool_membership.active 作为 AI 推荐候选池。

    返回 dict 字段：code (=symbol), raw_symbol, market, name, industry, theme,
    pool_id, source。code 与 symbol 同值，便于既读 V1 风格 code 又读 V2 symbol 的调用方。
    """
    own = conn is None
    if own:
        conn = get_db()
    rows = conn.execute(
        """
        SELECT u.symbol, u.raw_symbol, u.market, u.name, u.industry, u.theme,
               u.pool_id, u.source
        FROM system_universe u
        JOIN pool_membership m
          ON u.pool_id = m.pool_id AND u.market = m.market AND u.symbol = m.symbol
        WHERE m.active = TRUE AND m.pool_type = ?
        ORDER BY u.market, u.symbol
        """,
        [pool_type],
    ).fetchall()
    out = []
    for r in rows:
        symbol, raw_symbol, market, name, industry, theme, pool_id, source = r
        out.append({
            "code": symbol,
            "symbol": symbol,
            "raw_symbol": raw_symbol,
            "market": market,
            "name": name,
            "industry": industry,
            "theme": theme,
            "pool_id": pool_id,
            "source": source,
        })
    if own:
        conn.close()
    return out


def fetch_latest_portfolio_plan_baseline(
    *,
    universe_scope: str = "system_tech_universe",
    plan_version: str | None = None,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> list[tuple[str, str, float]]:
    """返回最新 portfolio_plans 的 (name, ticker, target_weight) 列表，给 legacy 对比脚本用。

    替代旧的 CURRENT_PLAN_A 硬编码。当 V2 还没产 portfolio_plans 时返回空 list，
    调用方应该 graceful exit 而不是用 NVDA 等默认值兜底。
    """
    own = conn is None
    if own:
        conn = get_db()
    run_row = conn.execute(
        """
        SELECT run_id FROM recommendation_runs
        WHERE universe_scope = ? AND status = 'generated'
        ORDER BY generated_at DESC LIMIT 1
        """,
        [universe_scope],
    ).fetchone()
    if not run_row:
        if own:
            conn.close()
        return []
    run_id = run_row[0]
    sql = """
        SELECT COALESCE(u.name, pp.symbol) AS name, pp.symbol AS ticker, pp.target_weight
        FROM portfolio_plans pp
        LEFT JOIN system_universe u
          ON pp.market = u.market AND pp.symbol = u.symbol AND u.pool_id = ?
        WHERE pp.run_id = ?
    """
    params: list = [universe_scope, run_id]
    if plan_version:
        sql += " AND pp.plan_version = ?"
        params.append(plan_version)
    sql += " ORDER BY pp.target_weight DESC, pp.symbol"
    rows = conn.execute(sql, params).fetchall()
    if own:
        conn.close()
    return [(name, ticker, float(w)) for name, ticker, w in rows]


def fetch_latest_recommendation_picks(
    *,
    universe_scope: str = "system_tech_universe",
    conn: duckdb.DuckDBPyConnection | None = None,
) -> list[dict]:
    """读最新一次 recommendation_runs（status='generated'）下的全部 recommendation_picks。

    返回字段：market, symbol, code(=symbol), name, rank, rating, signal,
    total_score, factor_scores(dict from factor_scores_json), entry_price,
    universe_scope, run_id, run_date。
    无最新 run 时返回 []。
    """
    own = conn is None
    if own:
        conn = get_db()
    row = conn.execute(
        """
        SELECT run_id, run_date FROM recommendation_runs
        WHERE universe_scope = ? AND status = 'generated'
        ORDER BY generated_at DESC LIMIT 1
        """,
        [universe_scope],
    ).fetchone()
    if not row:
        if own:
            conn.close()
        return []
    run_id, run_date = row
    rows = conn.execute(
        """
        SELECT market, symbol, name, rank, rating, signal, total_score,
               factor_scores_json, entry_price, universe_scope
        FROM recommendation_picks
        WHERE run_id = ?
        ORDER BY rank
        """,
        [run_id],
    ).fetchall()
    import json as _json
    out = []
    for r in rows:
        market, symbol, name, rank, rating, signal, total_score, fs_json, entry_price, scope = r
        try:
            fs = _json.loads(fs_json) if fs_json else {}
        except Exception:
            fs = {}
        out.append({
            "market": market,
            "symbol": symbol,
            "code": symbol,
            "name": name,
            "rank": rank,
            "rating": rating,
            "signal": signal,
            "total_score": total_score,
            "factor_scores": fs,
            "entry_price": entry_price,
            "universe_scope": scope,
            "run_id": run_id,
            "run_date": run_date,
        })
    if own:
        conn.close()
    return out


# ============================================================
# Holdings (2026-05-20: V2 schema (market, symbol) replaces V1 `code`)
# 兼容：fetch_* 返回字典里仍提供 code=symbol 别名给老调用方（risk_metrics 等）。
# ============================================================

HOLDINGS_COLS = ["market", "symbol", "entry_price", "shares", "entry_date", "source", "notes"]
HOLDINGS_FULL_COLS = ["id"] + HOLDINGS_COLS + ["created_at", "updated_at"]


def _infer_market_from_ticker(ticker: str) -> str:
    s = (ticker or "").upper().strip()
    if s.endswith(".SS") or s.endswith(".SZ") or s.endswith(".SH"):
        return "CN"
    if s.endswith(".HK"):
        return "HK"
    return "US"


def fetch_all_holdings(*, conn: duckdb.DuckDBPyConnection | None = None) -> list[dict]:
    """读全部持仓，按 entry_date 倒序。返回字段含 symbol 与 code(alias=symbol) 双形态。"""
    own = conn is None
    if own:
        conn = get_db()
    rows = conn.execute(
        f"SELECT {','.join(HOLDINGS_FULL_COLS)} "
        "FROM holdings ORDER BY entry_date DESC NULLS LAST, symbol"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(zip(HOLDINGS_FULL_COLS, r))
        d["code"] = d.get("symbol")
        out.append(d)
    if own:
        conn.close()
    return out


def get_holding(holding_id: int, *, conn: duckdb.DuckDBPyConnection | None = None) -> dict | None:
    own = conn is None
    if own:
        conn = get_db()
    row = conn.execute(
        f"SELECT {','.join(HOLDINGS_FULL_COLS)} FROM holdings WHERE id = ?",
        [holding_id],
    ).fetchone()
    if own:
        conn.close()
    if not row:
        return None
    d = dict(zip(HOLDINGS_FULL_COLS, row))
    d["code"] = d.get("symbol")
    return d


def _normalize_holding(item: Mapping[str, Any]) -> list:
    """item → SQL values 对齐 HOLDINGS_COLS。接受 V1 `code` 输入：自动按后缀派生 market。"""
    symbol = item.get("symbol") or item.get("code")
    if not symbol:
        raise ValueError("holding requires symbol (or legacy code)")
    market = item.get("market") or _infer_market_from_ticker(symbol)
    return [
        market,
        symbol,
        float(item.get("entry_price") or 0),
        float(item.get("shares") or 0),
        _to_date(item.get("entry_date") or item.get("date")),
        item.get("source") or "manual",
        item.get("notes"),
    ]


def insert_holding(item: Mapping[str, Any], *, conn: duckdb.DuckDBPyConnection | None = None) -> int:
    """新增持仓，返回生成的 id。"""
    own = conn is None
    if own:
        conn = get_db()
    vals = _normalize_holding(item)
    conn.execute(
        f"INSERT INTO holdings ({','.join(HOLDINGS_COLS)}, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
        vals,
    )
    new_id = int(conn.execute("SELECT currval('holdings_id_seq')").fetchone()[0])
    if own:
        conn.close()
    return new_id


def update_holding(
    holding_id: int,
    item: Mapping[str, Any],
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    """更新指定 id 持仓。返回受影响行数。"""
    own = conn is None
    if own:
        conn = get_db()
    exists = conn.execute("SELECT 1 FROM holdings WHERE id = ?", [holding_id]).fetchone()
    n = 0
    if exists:
        vals = _normalize_holding(item)
        conn.execute(
            "UPDATE holdings SET market=?, symbol=?, entry_price=?, shares=?, entry_date=?, "
            "source=?, notes=?, updated_at=CURRENT_TIMESTAMP WHERE id = ?",
            vals + [holding_id],
        )
        n = 1
    if own:
        conn.close()
    return n


def delete_holding(holding_id: int, *, conn: duckdb.DuckDBPyConnection | None = None) -> int:
    own = conn is None
    if own:
        conn = get_db()
    exists = conn.execute("SELECT 1 FROM holdings WHERE id = ?", [holding_id]).fetchone()
    n = 0
    if exists:
        conn.execute("DELETE FROM holdings WHERE id = ?", [holding_id])
        n = 1
    if own:
        conn.close()
    return n


def bulk_replace_holdings(
    items: Iterable[Mapping[str, Any]],
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    """整批替换持仓（清空 + 重插，用于从 localStorage 一次性导入）。返回插入条数。"""
    own = conn is None
    if own:
        conn = get_db()
    conn.execute("DELETE FROM holdings")
    n = 0
    for item in items:
        insert_holding(item, conn=conn)
        n += 1
    if own:
        conn.close()
    return n


# ============================================================
# 用户配置（user_config 表 helpers）
# ============================================================


def get_config(key: str, *, conn: duckdb.DuckDBPyConnection | None = None) -> Any:
    """读单个配置值；未设置时回退到 USER_CONFIG_DEFAULTS。"""
    own = conn is None
    if own:
        conn = get_db()
    row = conn.execute("SELECT value FROM user_config WHERE key = ?", [key]).fetchone()
    if own:
        conn.close()
    if row is None:
        return USER_CONFIG_DEFAULTS.get(key)
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return row[0]


def get_all_config(*, conn: duckdb.DuckDBPyConnection | None = None) -> dict[str, Any]:
    """读全部配置；缺失的 key 用默认值补齐。"""
    own = conn is None
    if own:
        conn = get_db()
    rows = conn.execute("SELECT key, value FROM user_config").fetchall()
    if own:
        conn.close()
    out = dict(USER_CONFIG_DEFAULTS)
    for k, v in rows:
        try:
            out[k] = json.loads(v)
        except (json.JSONDecodeError, TypeError):
            out[k] = v
    return out


def set_config(key: str, value: Any, *, conn: duckdb.DuckDBPyConnection | None = None) -> None:
    """写单个配置值（upsert）。"""
    own = conn is None
    if own:
        conn = get_db()
    payload = json.dumps(value, ensure_ascii=False)
    conn.execute(
        "INSERT INTO user_config (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        [key, payload, datetime.now()],
    )
    if own:
        conn.close()


def stats() -> dict:
    """快速看库状态：V2 表行数 + 时间跨度。"""
    conn = get_db()
    out = {}
    v2_tables = [
        ("price_daily", "trade_date"),
        ("recommendation_runs", "run_date"),
        ("recommendation_picks", "created_at"),
        ("portfolio_plans", "created_at"),
        ("pick_outcomes", "outcome_date"),
        ("manual_watchlist", "updated_at"),
        ("holdings", "entry_date"),
        ("system_universe", "last_seen_at"),
    ]
    tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
    for tbl, date_col in v2_tables:
        if tbl not in tables:
            continue
        n, dmin, dmax = conn.execute(
            f"SELECT COUNT(*), MIN({date_col}), MAX({date_col}) FROM {tbl}"
        ).fetchone()
        out[tbl] = {"rows": n, "min_date": dmin, "max_date": dmax}
    conn.close()
    return out


if __name__ == "__main__":
    # 自检：建库并打印状态
    conn = get_db()
    conn.close()
    print(f"DB: {DB_PATH}")
    for tbl, info in stats().items():
        print(f"  {tbl:8s} {info['rows']:>5} rows  "
              f"{info['min_date']} ~ {info['max_date']}")
