"""
DuckDB 持久化层 — 股票时间序列本地仓（V2）
─────────────────────────────────────────
设计原则：
  • DuckDB 是 single source of truth（持仓/自选/推荐历史/价格历史）
  • 用户状态走 state_backup/*.json，DuckDB 是衍生物（74MB 不入仓）

V2 核心表：
  system_universe        系统选股池（141 只 = US 66 + CN 42 + HK 33）
  pool_membership        股票池历史成员
  price_daily            每日行情（按日 + 代码主键）
  manual_watchlist       用户自选股（V1 watchlist 表的接位者）
  recommendation_runs    每日推荐 run 元数据
  recommendation_picks   每日推荐结果
  pick_outcomes          推荐 alpha 实现值（1d / 5d / 20d）
  real_holdings          用户真实持仓
  model_sim_holdings     模型推荐模拟仓
  holdings               legacy 兼容表（逐步退场）
  portfolio_plans        组合方案
  factor_attribution     因子归因

调用方式：
  from stock_db import (get_db, fetch_universe_for_ai_recommendations,
                        fetch_manual_watchlist, fetch_latest_recommendation_picks,
                        fetch_research_records_v2, fetch_picks_normalized)
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
-- real_holdings / model_sim_holdings / source_raw_snapshots / 等)
-- 由 scripts/tools/init_stock_db_v2.py 管理。
-- stock_db.py 这里只保留用户态共享表（user_config + real_holdings /
-- model_sim_holdings），以及 legacy holdings 兼容表。
-- 其它 V2 表全部由 scripts/tools/init_stock_db_v2.py 管理。

CREATE TABLE IF NOT EXISTS user_config (
    key        VARCHAR PRIMARY KEY,
    value      VARCHAR NOT NULL,  -- 任意 JSON 字符串
    updated_at TIMESTAMP
);

-- legacy 持仓表：只为旧数据迁移/旧接口退场保留，不再承载真实持仓或模型模拟仓。
-- 新代码必须读写 real_holdings / model_sim_holdings。
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
ALTER TABLE holdings ADD COLUMN IF NOT EXISTS currency VARCHAR;

-- 真实持仓：只记录用户实际买入/卖出后手动维护的真钱仓位。
CREATE SEQUENCE IF NOT EXISTS real_holdings_id_seq;
CREATE TABLE IF NOT EXISTS real_holdings (
    id          INTEGER   PRIMARY KEY DEFAULT nextval('real_holdings_id_seq'),
    account     VARCHAR   DEFAULT 'default',
    market      VARCHAR   NOT NULL,
    symbol      VARCHAR   NOT NULL,
    name        VARCHAR,           -- 用户录入时填的中文名/备注名（系统不会自动覆盖）
    entry_price DOUBLE    NOT NULL,
    shares      DOUBLE    NOT NULL,
    entry_date  DATE,
    currency    VARCHAR,
    entry_fx_rate   DOUBLE,   -- 买入日锁定汇率：1 单位本币 = ? RMB
    entry_fx_as_of  DATE,
    entry_fx_source VARCHAR,
    cost_rmb_locked DOUBLE,   -- entry_price * shares * entry_fx_rate，真实账户成本锁定值
    notes       VARCHAR,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE real_holdings ADD COLUMN IF NOT EXISTS entry_fx_rate DOUBLE;
ALTER TABLE real_holdings ADD COLUMN IF NOT EXISTS entry_fx_as_of DATE;
ALTER TABLE real_holdings ADD COLUMN IF NOT EXISTS entry_fx_source VARCHAR;
ALTER TABLE real_holdings ADD COLUMN IF NOT EXISTS cost_rmb_locked DOUBLE;
ALTER TABLE real_holdings ADD COLUMN IF NOT EXISTS name VARCHAR;

-- 模型模拟仓：只承载 AI 组合方案推演，不代表真实成交。
CREATE SEQUENCE IF NOT EXISTS model_sim_holdings_id_seq;
CREATE TABLE IF NOT EXISTS model_sim_holdings (
    id            INTEGER   PRIMARY KEY DEFAULT nextval('model_sim_holdings_id_seq'),
    plan_run_id   VARCHAR,
    plan_version  VARCHAR,
    market        VARCHAR   NOT NULL,
    symbol        VARCHAR   NOT NULL,
    target_weight DOUBLE,
    amount_rmb    DOUBLE,
    entry_price   DOUBLE    NOT NULL,
    shares        DOUBLE    NOT NULL,
    entry_date    DATE,
    currency      VARCHAR,
    notes         VARCHAR,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 真实持仓市值快照（后续画“我的账户真实曲线”用）。
CREATE TABLE IF NOT EXISTS real_holding_snapshots (
    snapshot_id     VARCHAR PRIMARY KEY,
    as_of_date      DATE NOT NULL,
    total_cost_rmb  DOUBLE,
    total_value_rmb DOUBLE,
    cash_rmb        DOUBLE,
    payload_json    VARCHAR,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 真实持仓每日体检：只评价用户已录入的 real_holdings，不产生股票池，也不写模拟仓。
CREATE TABLE IF NOT EXISTS real_holding_review_runs (
    review_run_id   VARCHAR PRIMARY KEY,
    as_of_date      DATE NOT NULL,
    status          VARCHAR,
    holding_count   INTEGER,
    data_quality    VARCHAR,
    notes           VARCHAR,
    generated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS real_holding_review_items (
    review_run_id      VARCHAR NOT NULL,
    account            VARCHAR,
    market             VARCHAR,
    symbol             VARCHAR NOT NULL,
    asset_class        VARCHAR,
    treatment_class    VARCHAR,
    score              DOUBLE,
    coverage_score     DOUBLE,
    rating             VARCHAR,
    action_label       VARCHAR,
    action_priority    INTEGER,
    current_price      DOUBLE,
    current_currency   VARCHAR,
    current_value_rmb  DOUBLE,
    cost_rmb_locked    DOUBLE,
    pnl_rmb            DOUBLE,
    pnl_pct            DOUBLE,
    current_weight     DOUBLE,
    target_weight      DOUBLE,
    weight_gap_pt      DOUBLE,
    reasons_json       VARCHAR,
    risk_flags_json    VARCHAR,
    data_flags_json    VARCHAR,
    prev_close         DOUBLE,
    prev_trade_date    VARCHAR,
    day_change_rmb     DOUBLE,
    day_change_pct     DOUBLE,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (review_run_id, symbol)
);
ALTER TABLE real_holding_review_items ADD COLUMN IF NOT EXISTS prev_close DOUBLE;
ALTER TABLE real_holding_review_items ADD COLUMN IF NOT EXISTS prev_trade_date VARCHAR;
ALTER TABLE real_holding_review_items ADD COLUMN IF NOT EXISTS day_change_rmb DOUBLE;
ALTER TABLE real_holding_review_items ADD COLUMN IF NOT EXISTS day_change_pct DOUBLE;
ALTER TABLE real_holding_review_items ADD COLUMN IF NOT EXISTS size_advisory_json VARCHAR;
ALTER TABLE real_holding_review_items ADD COLUMN IF NOT EXISTS industry_heat_json VARCHAR;
-- 自选股配置 editor 的"行业归类 / 主营业务"以前没字段可落, 现在补上
ALTER TABLE manual_watchlist ADD COLUMN IF NOT EXISTS industry VARCHAR;
ALTER TABLE manual_watchlist ADD COLUMN IF NOT EXISTS business VARCHAR;

-- 产业链元数据: 由 daily_picks_v5 rule_classify 自动产出 +「自选股配置」editor 人工 override。
-- source='manual_override' 优先级高于 'rule_classify'。前端 stockPill 的 chain badge 读这里。
CREATE TABLE IF NOT EXISTS chain_metadata (
    market        VARCHAR NOT NULL,
    symbol        VARCHAR NOT NULL,
    chain         VARCHAR,
    chain_tier    VARCHAR,
    chain_role    VARCHAR,
    layman_intro  VARCHAR,
    source        VARCHAR DEFAULT 'rule_classify',
    classified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (market, symbol)
);
"""

# user_config 已知 key 的默认值（首次读取或被删除时返回）
USER_CONFIG_DEFAULTS = {
    "total_capital": 500000,   # 进场本金，跑批 + 前端共用
    "stoploss_line": 300000,   # 止损红线（组合市值跌至此则强制清仓）
    "real_holding_review_rules": {
        "version": "v1_guardrail_2026_05_22",
        "source": "manual_guardrail_pending_backtest",
        "notes": "真实持仓页第一版保守体检阈值；用于防误导，不代表已回测验证的交易 alpha。",
        "loss_review_pct": -12.0,
        "tracking_loss_review_pct": -8.0,
        "stop_breach_score_cap": 30.0,
        "loss_score_cap": 45.0,
        "watch_score_cap": 55.0,
        "near_event_score_penalty": 5.0,
        "missing_price_score_penalty": 15.0,
        "weak_score_threshold": 55.0,
        "add_watch_min_score": 70.0,
        "underweight_add_gap_pt": -2.5,
        "score_snapshot_max_age_days": 3.0,
        "coverage_base": 0.20,
        "coverage_price": 0.30,
        "coverage_model_score": 0.30,
        "coverage_target_or_tracking": 0.20,
        "kelly_fraction": 0.5,
        "max_single_pct": 0.15,
        "hard_single_cap_pct": 0.25,
        "suggested_batches": 3,
    },
}


def get_db(path: str = DB_PATH, *, read_only: bool = False,
           ensure_schema: bool | None = None) -> duckdb.DuckDBPyConnection:
    """打开 DuckDB 连接并保证用户态共享表存在。

    2026-05-21 V1 cutover：删除原 4 个 UPDATE picks legacy 数据修复 SQL（picks 表已删）。
    V2 表 schema 由 init_stock_db_v2.py 负责。

    2026-05-22 多线程兼容修复:
      DuckDB 不允许同一进程内 read_only=True 与 read_only=False 两种 conn 同时存在
      (报 "Can't open a connection ... different configuration")。API 进程是多线程,
      不同 endpoint thread 拿不同 mode 会撞上,产生 HTTP 500。
      → 即使调用方传 read_only=True,这里强制用 False 打开 connection;
        read_only 参数仅保留作为是否跳过 SCHEMA_SQL 的提示。
    """
    conn = duckdb.connect(path, read_only=False)
    if ensure_schema is None:
        ensure_schema = not read_only
    if ensure_schema:
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

# 2026-05-21 V1 cutover：PRICE_COLS / PICK_COLS / REVIEW_COLS 三个列名常量已删
# (V1 prices / picks / reviews 表已 DROP，无任何 INSERT 站点引用这些常量)
# V2 列名定义在 init_stock_db_v2.py 的 CREATE TABLE 语句里，是 single source of truth。


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


def fetch_research_records_v2(*, conn: duckdb.DuckDBPyConnection | None = None) -> list[dict]:
    """V2 路径：从 system_universe + price_daily + 最新 recommendation_picks 拼出
    给「个股研究 / 产业链地图 / 买前审查」的展示用 records。

    与 fetch_records_view 形状对齐（同字段名），但纯 V2 表，没有任何 V1 watchlist/prices 依赖。
    缺失的 V1 主观字段（business / ai_logic / conclusion / risks / peers / rhythm /
    chain / chain_tier / chain_role / layman_intro 等）填 None — 前端做空值处理或隐藏。
    """
    own = conn is None
    if own:
        conn = get_db(read_only=True)
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
            NULL AS peers, NULL AS rhythm, u.source, ls.credibility,
            ls.earnings, ls.verification, ls.info_breakdown,
            cm.chain, cm.chain_tier, cm.chain_role, cm.layman_intro,
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
        LEFT JOIN chain_metadata cm ON cm.market = u.market AND cm.symbol = u.symbol
        WHERE u.active = TRUE
        ORDER BY u.market, u.symbol
    """).fetchall()
    cols = [
        "code", "name", "market", "business", "industry",
        "ai_relevance", "ai_logic", "conclusion", "risks", "peers",
        "rhythm", "source", "credibility",
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
    # chain/chain_tier/chain_role/layman_intro 已在 SQL 里 JOIN chain_metadata 拿到（2026-05-21 V2 表）
    # 若 chain_metadata 没记录则用 watchlist_enrich._infer_chain 兜底
    import logging as _lg
    _logger = _lg.getLogger(__name__)
    missing_chain = [r for r in out if not r.get("chain")]
    if missing_chain:
        try:
            from stock_research.core.watchlist_enrich import _infer_chain
            inferred = 0
            for row in missing_chain:
                chain, chain_tier, chain_role = _infer_chain(row.get("industry") or "", row.get("theme") or "")
                if chain:
                    row["chain"] = chain
                    inferred += 1
                if chain_role:
                    row["chain_role"] = chain_role
                if chain_tier:
                    row["chain_tier"] = chain_tier
            if inferred:
                _logger.info(f"_infer_chain 兜底命中 {inferred}/{len(missing_chain)} 只（chain_metadata 未覆盖）")
        except ImportError as e:
            _logger.warning(f"_infer_chain 兜底失败（{len(missing_chain)} 只无 chain）：{e}")

    # A 股 theme 友好化（产业链地图专用）：证监会代码 → 业务子主题
    # 不在白名单的 A 股 theme=None，前端不渲染（医药/化工/食品/家电/工程机械 等）
    try:
        from a_share_theme_map import get_a_share_theme
        for row in out:
            if row.get("market") == "CN":
                row["theme"] = get_a_share_theme(row["code"], row.get("industry"))
    except Exception:
        pass

    # 美股 theme 英文 → 中文（产业链地图 / AI 助手 tab 用）
    try:
        from us_theme_zh import get_us_theme_zh
        for row in out:
            if row.get("market") == "US":
                row["theme"] = get_us_theme_zh(row.get("theme"))
    except Exception:
        pass

    # 美股 name 英文 → 中文短名（持仓页 / AI 推荐 / 产业链地图通用）
    try:
        from us_company_zh import get_us_company_zh
        for row in out:
            if row.get("market") == "US":
                row["name"] = get_us_company_zh(row.get("name"))
    except Exception:
        pass

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
        conn = get_db(read_only=True)
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

    返回 dict: market / symbol / name / notes / created_at / updated_at + code(=symbol) alias
              + chain / chain_tier / chain_role / layman_intro (LEFT JOIN chain_metadata)。
    name 字段：优先 system_universe.name（中文），fallback manual_watchlist.name（可能是英文）。
    可选按 market 过滤。

    Why JOIN system_universe: A 股 manual_watchlist 入库时常存英文公司名
    (akshare/yfinance 返回的 longName)，system_universe 走中文官方名，dashboard 需中文显示。
    Why JOIN chain_metadata: 自选股配置页直接显示链条/层级/角色 badge，避免前端二次合并。
    """
    own = conn is None
    if own:
        conn = get_db(read_only=True)
    # chain_metadata / system_universe 可能不存在（旧 DB），用 SHOW TABLES 兜底
    tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
    has_su = "system_universe" in tables
    has_chain = "chain_metadata" in tables
    # 注：manual_watchlist.market 用前端展示值（"A股·沪交所" / "美股"），
    # system_universe.market 用 ISO 风格（"CN" / "US" / "HK"），两者不直接相等。
    # symbol 带后缀已全局唯一（NVDA / 605117.SS / 0700.HK），故 JOIN 只按 symbol 匹配。
    # name: 用户手填的 mw.name 优先, 然后 system_universe 的中文官方名兜底
    cn_sel = "COALESCE(NULLIF(mw.name, ''), NULLIF(u.name, mw.symbol)) AS name" if has_su else "mw.name AS name"
    # industry/business: 用户手填的 mw 优先, system_universe.industry 兜底
    industry_sel = "COALESCE(mw.industry, u.industry) AS industry" if has_su else "mw.industry AS industry"
    business_sel = "mw.business AS business"
    su_join = ("LEFT JOIN system_universe u "
               "  ON u.symbol = mw.symbol") if has_su else ""
    chain_sel = ("cm.chain, cm.chain_tier, cm.chain_role, cm.layman_intro"
                 if has_chain else "NULL AS chain, NULL AS chain_tier, NULL AS chain_role, NULL AS layman_intro")
    chain_join = ("LEFT JOIN chain_metadata cm "
                  "  ON cm.symbol = mw.symbol") if has_chain else ""
    sql = (
        f"SELECT mw.market, mw.symbol, {cn_sel}, {industry_sel}, {business_sel}, mw.notes, mw.created_at, mw.updated_at, "
        f"{chain_sel} "
        f"FROM manual_watchlist mw "
        f"{su_join} {chain_join}"
    )
    params: list = []
    if market:
        sql += " WHERE UPPER(mw.market) = ?"
        params.append(market.upper())
    sql += " ORDER BY mw.market, mw.symbol"
    rows = conn.execute(sql, params).fetchall()
    cols = ["market", "symbol", "name", "industry", "business", "notes", "created_at", "updated_at",
            "chain", "chain_tier", "chain_role", "layman_intro"]
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
        industry = (r.get("industry") or "").strip() or None
        business = (r.get("business") or "").strip() or None
        conn.execute(
            """
            INSERT INTO manual_watchlist (market, symbol, name, industry, business, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (market, symbol) DO UPDATE SET
              name       = excluded.name,
              industry   = excluded.industry,
              business   = excluded.business,
              notes      = excluded.notes,
              updated_at = excluded.updated_at
            """,
            [market, symbol, r.get("name"), industry, business, r.get("notes"), now, now],
        )
        n += 1
    if own:
        conn.close()
    return n


def upsert_chain_metadata(
    rows: Iterable[Mapping[str, Any]],
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    """新增 / 更新 chain_metadata（按 (market, symbol) PK）。

    用户在「自选股配置」里编辑的链条信息（chain / chain_tier / chain_role / layman_intro）
    走这里;source 强制为 'manual_override',覆盖 daily_picks 跑的 'rule_classify'。
    """
    own = conn is None
    if own:
        conn = get_db()
    # 兼容老 DB:没建表就先建(DDL 已在模块加载时跑过,这里只是兜底)
    n = 0
    now = datetime.now()
    for r in rows:
        symbol = r.get("symbol") or r.get("code")
        if not symbol:
            continue
        market = r.get("market") or _infer_market_from_ticker(symbol)
        chain = (r.get("chain") or "").strip() or None
        tier = (r.get("chain_tier") or "").strip() or None
        role = (r.get("chain_role") or "").strip() or None
        intro = (r.get("layman_intro") or "").strip() or None
        # 全空就 skip(不插入纯空行)
        if not any([chain, tier, role, intro]):
            continue
        conn.execute(
            """
            INSERT INTO chain_metadata (market, symbol, chain, chain_tier, chain_role, layman_intro, source, classified_at)
            VALUES (?, ?, ?, ?, ?, ?, 'manual_override', ?)
            ON CONFLICT (market, symbol) DO UPDATE SET
              chain        = excluded.chain,
              chain_tier   = excluded.chain_tier,
              chain_role   = excluded.chain_role,
              layman_intro = excluded.layman_intro,
              source       = 'manual_override',
              classified_at= excluded.classified_at
            """,
            [market, symbol, chain, tier, role, intro, now],
        )
        n += 1
    if own:
        conn.close()
    return n


def fetch_chain_metadata_all(
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> list[dict]:
    """全量取 chain_metadata,供前端保存后热更新 WATCHLIST_CHAIN_INFO。"""
    own = conn is None
    if own:
        conn = get_db(read_only=True)
    out: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT market, symbol, chain, chain_tier, chain_role, layman_intro, source "
            "FROM chain_metadata WHERE chain IS NOT NULL"
        ).fetchall()
        for market, symbol, chain, tier, role, intro, source in rows:
            out.append({
                "market": market, "symbol": symbol,
                "chain": chain, "chain_tier": tier, "chain_role": role,
                "layman_intro": intro, "source": source,
            })
    finally:
        if own:
            conn.close()
    return out


def fetch_manual_watchlist_enriched(
    *,
    market: str | None = None,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> list[dict]:
    """manual_watchlist JOIN system_universe + price_daily 拿出富字段，
    供「自选股·AI 优选」三个 jobs（daily_picks_v5 / hk_picks / a_share_picks）使用。

    返回 dict 字段：
      code, name, market, industry, theme,
      latest_price, ytd_pct, one_year_pct, one_month_pct, one_week_pct,
      forward_pe, peg, earnings_growth_pct, currency, market_cap

    2026-05-21 V1 cutover：
    - 删 5 个永空字段 (ai_relevance / ai_logic / conclusion / risks / credibility) ——
      V2 manual_watchlist 不存这些；3 个消费 jobs 自己重新填 ai_relevance/ai_logic
      或忽略；保留 NULL 字段只会让 schema 看着有用却没数据。
    - JOIN 只按 symbol（manual_watchlist.market="A股·沪交所"/"美股" 与
      system_universe.market="CN"/"US" 值不同，按 market JOIN 永远 false，导致
      industry/theme 也空。symbol 带后缀已全局唯一）。
    """
    own = conn is None
    if own:
        conn = get_db(read_only=True)
    sql = """
        WITH latest_price AS (
            SELECT * FROM price_daily
            QUALIFY ROW_NUMBER() OVER (PARTITION BY market, symbol ORDER BY trade_date DESC, fetched_at DESC) = 1
        )
        SELECT
            w.symbol AS code, COALESCE(NULLIF(u.name, w.symbol), w.name, w.symbol) AS name,
            w.market, u.industry, u.theme,
            lp.close AS latest_price,
            lp.ytd_pct, lp.one_year_pct, lp.one_month_pct, lp.one_week_pct,
            lp.forward_pe, lp.peg_ratio AS peg, NULL AS earnings_growth_pct,
            lp.currency, lp.market_cap
        FROM manual_watchlist w
        LEFT JOIN system_universe u ON u.symbol = w.symbol
        LEFT JOIN latest_price lp ON lp.symbol = w.symbol
    """
    params: list = []
    if market:
        sql += " WHERE UPPER(w.market) = ?"
        params.append(market.upper())
    sql += " ORDER BY w.market, w.symbol"
    rows = conn.execute(sql, params).fetchall()
    cols = ["code", "name", "market", "industry", "theme",
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
        conn = get_db(read_only=True)
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
        conn = get_db(read_only=True)
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
        conn = get_db(read_only=True)
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


def fetch_recommendation_runs_between(
    start_date: date,
    end_date: date,
    *,
    universe_scope: str = "system_tech_universe",
    conn: duckdb.DuckDBPyConnection | None = None,
) -> list[dict[str, Any]]:
    """按 run_date 区间读取 recommendation_runs（PIT 复盘用）。"""
    own = conn is None
    if own:
        conn = get_db(read_only=True)
    rows = conn.execute(
        """
        SELECT run_id, run_date, strategy_version, model_version, universe_scope,
               data_cutoff_at, generated_at, status, notes
        FROM recommendation_runs
        WHERE universe_scope = ?
          AND run_date >= ? AND run_date <= ?
          AND status = 'generated'
        ORDER BY run_date ASC, generated_at ASC
        """,
        [universe_scope, start_date, end_date],
    ).fetchall()
    cols = [
        "run_id", "run_date", "strategy_version", "model_version", "universe_scope",
        "data_cutoff_at", "generated_at", "status", "notes",
    ]
    out = [_rowdict(cols, r) for r in rows]
    if own:
        conn.close()
    return out


def fetch_recommendation_picks_for_run(
    run_id: str,
    *,
    top_n: int | None = None,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> list[dict[str, Any]]:
    """读取某次 run 的 recommendation_picks（按 rank 排序）。"""
    own = conn is None
    if own:
        conn = get_db(read_only=True)
    sql = """
        SELECT market, symbol, name, rank, rating, signal, total_score,
               factor_scores_json, entry_price, universe_scope, source_origin
        FROM recommendation_picks
        WHERE run_id = ?
        ORDER BY rank
    """
    params: list[Any] = [run_id]
    if top_n is not None and top_n > 0:
        sql += " LIMIT ?"
        params.append(int(top_n))
    rows = conn.execute(sql, params).fetchall()
    import json as _json
    out = []
    for r in rows:
        market, symbol, name, rank, rating, signal, total_score, fs_json, entry_price, scope, origin = r
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
            "source_origin": origin,
            "run_id": run_id,
        })
    if own:
        conn.close()
    return out


def fetch_pick_outcomes_for_symbols(
    symbols: list[str],
    *,
    horizon: str = "5d",
    conn: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, dict[str, Any]]:
    """每只 symbol 取最近一次 pick_outcomes（按 outcome_date 降序）。"""
    if not symbols:
        return {}
    own = conn is None
    if own:
        conn = get_db(read_only=True)
    placeholders = ",".join(["?"] * len(symbols))
    rows = conn.execute(
        f"""
        SELECT run_id, market, symbol, horizon, outcome_date, return_pct,
               benchmark_pct, alpha_pct, is_success
        FROM (
          SELECT *, ROW_NUMBER() OVER (
            PARTITION BY symbol ORDER BY outcome_date DESC
          ) AS rn
          FROM pick_outcomes
          WHERE symbol IN ({placeholders}) AND horizon = ?
        ) t WHERE rn = 1
        """,
        [*symbols, horizon],
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = _rowdict(
            ["run_id", "market", "symbol", "horizon", "outcome_date",
             "return_pct", "benchmark_pct", "alpha_pct", "is_success"],
            r,
        )
        out[str(d["symbol"])] = d
    if own:
        conn.close()
    return out


# ============================================================
# Holdings (2026-05-20: V2 schema (market, symbol) replaces V1 `code`)
# 兼容：fetch_* 返回字典里仍提供 code=symbol 别名给老调用方（risk_metrics 等）。
# ============================================================

HOLDINGS_COLS = ["market", "symbol", "entry_price", "shares", "entry_date", "source", "notes", "currency"]
HOLDINGS_FULL_COLS = ["id"] + HOLDINGS_COLS + ["created_at", "updated_at"]
REAL_HOLDINGS_COLS = [
    "account", "market", "symbol", "name", "entry_price", "shares", "entry_date", "currency",
    "entry_fx_rate", "entry_fx_as_of", "entry_fx_source", "cost_rmb_locked", "notes",
]
REAL_HOLDINGS_FULL_COLS = ["id"] + REAL_HOLDINGS_COLS + ["created_at", "updated_at"]
MODEL_SIM_HOLDINGS_COLS = [
    "plan_run_id", "plan_version", "market", "symbol", "target_weight", "amount_rmb",
    "entry_price", "shares", "entry_date", "currency", "notes",
]
MODEL_SIM_HOLDINGS_FULL_COLS = ["id"] + MODEL_SIM_HOLDINGS_COLS + ["created_at", "updated_at"]
REAL_HOLDING_REVIEW_RUN_COLS = [
    "review_run_id", "as_of_date", "status", "holding_count", "data_quality", "notes",
]
REAL_HOLDING_REVIEW_RUN_FULL_COLS = REAL_HOLDING_REVIEW_RUN_COLS + ["generated_at"]
REAL_HOLDING_REVIEW_ITEM_COLS = [
    "review_run_id", "account", "market", "symbol", "asset_class", "treatment_class",
    "score", "coverage_score", "rating", "action_label", "action_priority",
    "current_price", "current_currency", "current_value_rmb", "cost_rmb_locked",
    "pnl_rmb", "pnl_pct", "current_weight", "target_weight", "weight_gap_pt",
    "reasons_json", "risk_flags_json", "data_flags_json",
    "prev_close", "prev_trade_date", "day_change_rmb", "day_change_pct",
    "size_advisory_json",
    "industry_heat_json",
]
REAL_HOLDING_REVIEW_ITEM_FULL_COLS = REAL_HOLDING_REVIEW_ITEM_COLS + ["created_at"]


def _infer_market_from_ticker(ticker: str) -> str:
    s = (ticker or "").upper().strip()
    if s.endswith(".SS") or s.endswith(".SZ") or s.endswith(".SH"):
        return "CN"
    if s.endswith(".HK"):
        return "HK"
    return "US"


def _infer_currency_from_ticker(ticker: str) -> str:
    """按 ticker 后缀推断买入价本币（与前端 _currencyForTicker 同一套规则）。"""
    s = (ticker or "").upper().strip()
    if s.endswith(".SS") or s.endswith(".SZ") or s.endswith(".BJ") or s.endswith(".SH"):
        return "CNY"
    if s.endswith(".HK"):
        return "HKD"
    if s.endswith(".T"):
        return "JPY"
    if s.endswith(".KS"):
        return "KRW"
    if s.endswith(".AX"):
        return "AUD"
    if s.endswith(".IL"):
        return "GBP"
    return "USD"  # 裸 ticker 默认美股


def fetch_all_holdings(*, conn: duckdb.DuckDBPyConnection | None = None) -> list[dict]:
    """读全部持仓，按 entry_date 倒序。返回字段含 symbol 与 code(alias=symbol) 双形态。"""
    own = conn is None
    if own:
        conn = get_db(read_only=True)
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


def _rowdict(cols: list[str], row: tuple) -> dict:
    d = dict(zip(cols, row))
    if "symbol" in d:
        d["code"] = d.get("symbol")
    return d


def _normalize_real_holding(item: Mapping[str, Any]) -> list:
    symbol = item.get("symbol") or item.get("code")
    if not symbol:
        raise ValueError("real holding requires symbol (or legacy code)")
    market = item.get("market") or _infer_market_from_ticker(symbol)
    currency = (item.get("currency") or "").strip().upper() or _infer_currency_from_ticker(symbol)
    entry_price = float(item.get("entry_price") or 0)
    shares = float(item.get("shares") or 0)
    entry_date = _to_date(item.get("entry_date") or item.get("date"))
    entry_fx_rate, entry_fx_as_of, entry_fx_source, cost_rmb_locked = _resolve_real_holding_entry_fx(
        item,
        currency=currency,
        entry_date=entry_date,
        entry_price=entry_price,
        shares=shares,
    )
    name = item.get("name")
    if name is not None:
        name = str(name).strip() or None
    return [
        item.get("account") or "default",
        market,
        symbol,
        name,
        entry_price,
        shares,
        entry_date,
        currency,
        entry_fx_rate,
        entry_fx_as_of,
        entry_fx_source,
        cost_rmb_locked,
        item.get("notes"),
    ]


def _as_float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except Exception:
        return None
    if v <= 0:
        return None
    return v


def _resolve_real_holding_entry_fx(
    item: Mapping[str, Any],
    *,
    currency: str,
    entry_date: date | None,
    entry_price: float,
    shares: float,
) -> tuple[float, date | None, str, float]:
    manual_rate = _as_float_or_none(item.get("entry_fx_rate") or item.get("fx_rate"))
    if manual_rate is not None:
        fx_rate = manual_rate
        fx_as_of = _to_date(item.get("entry_fx_as_of") or entry_date)
        fx_source = str(item.get("entry_fx_source") or "manual")
    else:
        try:
            import fx_rates
            payload = fx_rates.get_historical_fx_payload(currency, entry_date)
            fx_rate = float(payload.get("rate") or fx_rates.get_fx_to_rmb(currency))
            fx_as_of = _to_date(payload.get("as_of") or entry_date)
            fx_source = str(payload.get("source") or "fx_rates")
        except Exception:
            fallback = {
                "CNY": 1.0, "USD": 7.10, "HKD": 0.917, "JPY": 0.046,
                "KRW": 0.0052, "TWD": 0.22, "EUR": 7.80, "AUD": 4.60, "GBP": 9.00,
            }
            fx_rate = fallback.get(currency, 1.0)
            fx_as_of = entry_date
            fx_source = "static_exception_fallback"

    manual_cost = _as_float_or_none(item.get("cost_rmb_locked"))
    cost_rmb_locked = manual_cost if manual_cost is not None else entry_price * shares * fx_rate
    return fx_rate, fx_as_of, fx_source, cost_rmb_locked


def fetch_all_real_holdings(*, conn: duckdb.DuckDBPyConnection | None = None) -> list[dict]:
    """读真实持仓。只来自 real_holdings，不混入模型模拟仓。"""
    own = conn is None
    if own:
        # Own connections ensure the user-state schema is current before
        # selecting newly-added locked FX columns.
        conn = get_db()
    rows = conn.execute(
        f"SELECT {','.join(REAL_HOLDINGS_FULL_COLS)} "
        "FROM real_holdings ORDER BY entry_date DESC NULLS LAST, symbol"
    ).fetchall()
    out = [_rowdict(REAL_HOLDINGS_FULL_COLS, r) for r in rows]
    if own:
        conn.close()
    return out


def insert_real_holding(item: Mapping[str, Any], *, conn: duckdb.DuckDBPyConnection | None = None) -> int:
    own = conn is None
    if own:
        conn = get_db()
    vals = _normalize_real_holding(item)
    conn.execute(
        f"INSERT INTO real_holdings ({','.join(REAL_HOLDINGS_COLS)}, updated_at) "
        f"VALUES ({','.join(['?'] * len(REAL_HOLDINGS_COLS))}, CURRENT_TIMESTAMP)",
        vals,
    )
    new_id = int(conn.execute("SELECT currval('real_holdings_id_seq')").fetchone()[0])
    if own:
        conn.close()
    return new_id


def update_real_holding(holding_id: int, item: Mapping[str, Any], *, conn: duckdb.DuckDBPyConnection | None = None) -> int:
    own = conn is None
    if own:
        conn = get_db()
    existing_row = conn.execute(
        f"SELECT {','.join(REAL_HOLDINGS_FULL_COLS)} FROM real_holdings WHERE id = ?",
        [holding_id],
    ).fetchone()
    n = 0
    if existing_row:
        merged = dict(item)
        existing = _rowdict(REAL_HOLDINGS_FULL_COLS, existing_row)
        new_symbol = merged.get("symbol") or merged.get("code") or existing.get("symbol")
        new_currency = (merged.get("currency") or existing.get("currency") or "").strip().upper() or _infer_currency_from_ticker(new_symbol)
        new_entry_date = _to_date(merged.get("entry_date") or merged.get("date") or existing.get("entry_date"))
        has_explicit_fx = _as_float_or_none(merged.get("entry_fx_rate") or merged.get("fx_rate")) is not None
        same_fx_context = (
            new_currency == (existing.get("currency") or "").strip().upper()
            and new_entry_date == existing.get("entry_date")
        )
        if not has_explicit_fx and same_fx_context and existing.get("entry_fx_rate"):
            # Editing shares/price/notes should preserve the original locked
            # entry-date FX; cost_rmb_locked will be recomputed with that rate.
            merged["entry_fx_rate"] = existing.get("entry_fx_rate")
            merged["entry_fx_as_of"] = existing.get("entry_fx_as_of")
            merged["entry_fx_source"] = existing.get("entry_fx_source")
        vals = _normalize_real_holding(merged)
        set_clause = ", ".join(f"{c}=?" for c in REAL_HOLDINGS_COLS)
        conn.execute(
            f"UPDATE real_holdings SET {set_clause}, updated_at=CURRENT_TIMESTAMP WHERE id = ?",
            vals + [holding_id],
        )
        n = 1
    if own:
        conn.close()
    return n


def delete_real_holding(holding_id: int, *, conn: duckdb.DuckDBPyConnection | None = None) -> int:
    own = conn is None
    if own:
        conn = get_db()
    exists = conn.execute("SELECT 1 FROM real_holdings WHERE id = ?", [holding_id]).fetchone()
    n = 0
    if exists:
        conn.execute("DELETE FROM real_holdings WHERE id = ?", [holding_id])
        n = 1
    if own:
        conn.close()
    return n


def backfill_real_holding_entry_fx(*, conn: duckdb.DuckDBPyConnection | None = None) -> int:
    """Fill locked entry FX/cost fields for existing real holdings."""
    own = conn is None
    if own:
        conn = get_db()
    rows = conn.execute(
        """
        SELECT id, account, market, symbol, entry_price, shares, entry_date, currency, notes
        FROM real_holdings
        WHERE entry_fx_rate IS NULL OR cost_rmb_locked IS NULL
        ORDER BY id
        """
    ).fetchall()
    n = 0
    for row in rows:
        holding_id, account, market, symbol, entry_price, shares, entry_date, currency, notes = row
        item = {
            "account": account,
            "market": market,
            "symbol": symbol,
            "entry_price": entry_price,
            "shares": shares,
            "entry_date": entry_date,
            "currency": currency,
            "notes": notes,
        }
        vals = _normalize_real_holding(item)
        set_clause = ", ".join(f"{c}=?" for c in REAL_HOLDINGS_COLS)
        conn.execute(
            f"UPDATE real_holdings SET {set_clause}, updated_at=CURRENT_TIMESTAMP WHERE id = ?",
            vals + [holding_id],
        )
        n += 1
    if own:
        conn.close()
    return n


def _dump_json_field(value: Any) -> str:
    if value is None:
        value = []
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load_json_field(value: Any, fallback: Any) -> Any:
    if value is None or value == "":
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def save_real_holding_review(
    run: Mapping[str, Any],
    items: Iterable[Mapping[str, Any]],
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    """Persist one real-holding daily review run.

    This is intentionally separate from recommendation_picks and model_sim_holdings:
    it evaluates only the user's current real_holdings snapshot.
    """
    own = conn is None
    if own:
        conn = get_db()

    review_run_id = str(run.get("review_run_id") or "")
    if not review_run_id:
        raise ValueError("review_run_id is required")

    conn.execute("DELETE FROM real_holding_review_items WHERE review_run_id = ?", [review_run_id])
    conn.execute("DELETE FROM real_holding_review_runs WHERE review_run_id = ?", [review_run_id])

    run_vals = [
        review_run_id,
        _to_date(run.get("as_of_date") or run.get("as_of")),
        run.get("status") or "generated",
        int(run.get("holding_count") or 0),
        run.get("data_quality") or "unknown",
        run.get("notes"),
    ]
    conn.execute(
        f"INSERT INTO real_holding_review_runs ({','.join(REAL_HOLDING_REVIEW_RUN_COLS)}) "
        f"VALUES ({','.join(['?'] * len(REAL_HOLDING_REVIEW_RUN_COLS))})",
        run_vals,
    )

    n = 0
    for item in items:
        symbol = item.get("symbol") or item.get("code")
        if not symbol:
            continue
        vals = [
            review_run_id,
            item.get("account") or "default",
            item.get("market") or _infer_market_from_ticker(str(symbol)),
            symbol,
            item.get("asset_class"),
            item.get("treatment_class"),
            item.get("score"),
            item.get("coverage_score"),
            item.get("rating"),
            item.get("action_label"),
            int(item.get("action_priority") or 99),
            item.get("current_price"),
            item.get("current_currency"),
            item.get("current_value_rmb"),
            item.get("cost_rmb_locked"),
            item.get("pnl_rmb"),
            item.get("pnl_pct"),
            item.get("current_weight"),
            item.get("target_weight"),
            item.get("weight_gap_pt"),
            _dump_json_field(item.get("reasons")),
            _dump_json_field(item.get("risk_flags")),
            _dump_json_field(item.get("data_flags")),
            item.get("prev_close"),
            item.get("prev_trade_date"),
            item.get("day_change_rmb"),
            item.get("day_change_pct"),
            _dump_json_field(item.get("size_advisory")),
            _dump_json_field(item.get("industry_heat")),
        ]
        conn.execute(
            f"INSERT INTO real_holding_review_items ({','.join(REAL_HOLDING_REVIEW_ITEM_COLS)}) "
            f"VALUES ({','.join(['?'] * len(REAL_HOLDING_REVIEW_ITEM_COLS))})",
            vals,
        )
        n += 1

    if own:
        conn.close()
    return n


def fetch_real_holding_review_history(
    *,
    symbols: list[str] | None = None,
    days: int = 14,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """每只持仓近 N 日体检轨迹 (按 as_of_date 升序)。

    回答「这只票上周还是仅跟踪、今天为什么变风险复查」——是历史时间线视图,
    不替代 fetch_latest_real_holding_review。同一 as_of_date 有多次 run 时取最新 run。
    """
    own = conn is None
    if own:
        conn = get_db(read_only=True)

    days = max(1, min(int(days), 365))
    params: list[Any] = []
    sql = f"""
    SELECT as_of_date, symbol, action_label, action_priority,
           pnl_pct, current_weight, score, treatment_class, rating
    FROM (
      SELECT r.as_of_date, i.symbol, i.action_label, i.action_priority,
             i.pnl_pct, i.current_weight, i.score, i.treatment_class, i.rating,
             ROW_NUMBER() OVER (
               PARTITION BY i.symbol, r.as_of_date
               ORDER BY r.generated_at DESC
             ) AS rn
      FROM real_holding_review_items i
      JOIN real_holding_review_runs r ON i.review_run_id = r.review_run_id
      WHERE r.as_of_date >= CURRENT_DATE - {days}
    """
    if symbols:
        placeholders = ",".join(["?"] * len(symbols))
        sql += f"  AND i.symbol IN ({placeholders})\n"
        params.extend(symbols)
    sql += """
    )
    WHERE rn = 1
    ORDER BY symbol, as_of_date ASC
    """

    rows = conn.execute(sql, params).fetchall()
    out: dict[str, list[dict[str, Any]]] = {}
    for as_of_date, symbol, action_label, action_priority, pnl_pct, current_weight, score, treatment_class, rating in rows:
        out.setdefault(str(symbol), []).append({
            "as_of_date": as_of_date.isoformat() if hasattr(as_of_date, "isoformat") else as_of_date,
            "action_label": action_label,
            "action_priority": action_priority,
            "pnl_pct": pnl_pct,
            "current_weight": current_weight,
            "score": score,
            "treatment_class": treatment_class,
            "rating": rating,
        })

    if own:
        conn.close()
    return out


def fetch_latest_real_holding_review(
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, Any] | None:
    """Fetch the newest persisted real-holding daily review run."""
    own = conn is None
    if own:
        conn = get_db(read_only=True)

    row = conn.execute(
        f"SELECT {','.join(REAL_HOLDING_REVIEW_RUN_FULL_COLS)} "
        "FROM real_holding_review_runs ORDER BY generated_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        if own:
            conn.close()
        return None

    run = _rowdict(REAL_HOLDING_REVIEW_RUN_FULL_COLS, row)
    item_rows = conn.execute(
        f"SELECT {','.join(REAL_HOLDING_REVIEW_ITEM_FULL_COLS)} "
        "FROM real_holding_review_items WHERE review_run_id = ? "
        "ORDER BY action_priority ASC, symbol ASC",
        [run["review_run_id"]],
    ).fetchall()
    items = []
    for r in item_rows:
        d = _rowdict(REAL_HOLDING_REVIEW_ITEM_FULL_COLS, r)
        d["reasons"] = _load_json_field(d.pop("reasons_json", None), [])
        d["risk_flags"] = _load_json_field(d.pop("risk_flags_json", None), [])
        d["data_flags"] = _load_json_field(d.pop("data_flags_json", None), [])
        d["size_advisory"] = _load_json_field(d.pop("size_advisory_json", None), None)
        d["industry_heat"] = _load_json_field(d.pop("industry_heat_json", None), None)
        items.append(d)

    if own:
        conn.close()
    return {"run": run, "items": items}


def _normalize_model_sim_holding(item: Mapping[str, Any]) -> list:
    symbol = item.get("symbol") or item.get("code")
    if not symbol:
        raise ValueError("model sim holding requires symbol (or legacy code)")
    market = item.get("market") or _infer_market_from_ticker(symbol)
    currency = (item.get("currency") or "").strip().upper() or _infer_currency_from_ticker(symbol)
    return [
        item.get("plan_run_id") or item.get("run_id"),
        item.get("plan_version") or "v6_risk_aware",
        market,
        symbol,
        float(item.get("target_weight") or 0),
        float(item.get("amount_rmb") or item.get("amount") or 0),
        float(item.get("entry_price") or 0),
        float(item.get("shares") or 0),
        _to_date(item.get("entry_date") or item.get("date")),
        currency,
        item.get("notes"),
    ]


def fetch_all_model_sim_holdings(*, conn: duckdb.DuckDBPyConnection | None = None) -> list[dict]:
    """读模型推荐模拟仓。它不代表真实成交。"""
    own = conn is None
    if own:
        conn = get_db(read_only=True)
    rows = conn.execute(
        f"SELECT {','.join(MODEL_SIM_HOLDINGS_FULL_COLS)} "
        "FROM model_sim_holdings ORDER BY target_weight DESC NULLS LAST, symbol"
    ).fetchall()
    out = [_rowdict(MODEL_SIM_HOLDINGS_FULL_COLS, r) for r in rows]
    if own:
        conn.close()
    return out


def bulk_replace_model_sim_holdings(items: Iterable[Mapping[str, Any]], *, conn: duckdb.DuckDBPyConnection | None = None) -> int:
    own = conn is None
    if own:
        conn = get_db()
    conn.execute("DELETE FROM model_sim_holdings")
    n = 0
    for item in items:
        vals = _normalize_model_sim_holding(item)
        conn.execute(
            f"INSERT INTO model_sim_holdings ({','.join(MODEL_SIM_HOLDINGS_COLS)}, updated_at) "
            f"VALUES ({','.join(['?'] * len(MODEL_SIM_HOLDINGS_COLS))}, CURRENT_TIMESTAMP)",
            vals,
        )
        n += 1
    if own:
        conn.close()
    return n


def delete_model_sim_holding(holding_id: int, *, conn: duckdb.DuckDBPyConnection | None = None) -> int:
    own = conn is None
    if own:
        conn = get_db()
    exists = conn.execute("SELECT 1 FROM model_sim_holdings WHERE id = ?", [holding_id]).fetchone()
    n = 0
    if exists:
        conn.execute("DELETE FROM model_sim_holdings WHERE id = ?", [holding_id])
        n = 1
    if own:
        conn.close()
    return n


def get_holding(holding_id: int, *, conn: duckdb.DuckDBPyConnection | None = None) -> dict | None:
    own = conn is None
    if own:
        conn = get_db(read_only=True)
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
    """item → SQL values 对齐 HOLDINGS_COLS。接受 V1 `code` 输入：自动按后缀派生 market。

    currency：item 显式传入优先；否则按 symbol 后缀推断（USD/CNY/HKD/JPY/KRW/AUD/GBP）。
    """
    symbol = item.get("symbol") or item.get("code")
    if not symbol:
        raise ValueError("holding requires symbol (or legacy code)")
    market = item.get("market") or _infer_market_from_ticker(symbol)
    currency = (item.get("currency") or "").strip().upper() or _infer_currency_from_ticker(symbol)
    return [
        market,
        symbol,
        float(item.get("entry_price") or 0),
        float(item.get("shares") or 0),
        _to_date(item.get("entry_date") or item.get("date")),
        item.get("source") or "manual",
        item.get("notes"),
        currency,
    ]


def insert_holding(item: Mapping[str, Any], *, conn: duckdb.DuckDBPyConnection | None = None) -> int:
    """新增持仓，返回生成的 id。"""
    own = conn is None
    if own:
        conn = get_db()
    vals = _normalize_holding(item)
    placeholders = ",".join(["?"] * len(HOLDINGS_COLS))
    conn.execute(
        f"INSERT INTO holdings ({','.join(HOLDINGS_COLS)}, updated_at) "
        f"VALUES ({placeholders}, CURRENT_TIMESTAMP)",
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
        set_clause = ", ".join(f"{c}=?" for c in HOLDINGS_COLS)
        conn.execute(
            f"UPDATE holdings SET {set_clause}, updated_at=CURRENT_TIMESTAMP WHERE id = ?",
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
        conn = get_db(read_only=True)
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
        conn = get_db(read_only=True)
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
    conn = get_db(read_only=True)
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
