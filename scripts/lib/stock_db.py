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
CREATE TABLE IF NOT EXISTS prices (
    date                 DATE         NOT NULL,
    code                 VARCHAR      NOT NULL,
    name                 VARCHAR,
    yf_ticker            VARCHAR,
    price                DOUBLE,
    prev_close           DOUBLE,
    currency             VARCHAR,
    market_cap           DOUBLE,
    forward_pe           DOUBLE,
    trailing_pe          DOUBLE,
    peg_ratio            DOUBLE,
    earnings_growth_pct  DOUBLE,
    revenue_growth_pct   DOUBLE,
    ytd_pct              DOUBLE,
    one_year_pct         DOUBLE,
    one_month_pct        DOUBLE,
    one_week_pct         DOUBLE,
    fetched_at           TIMESTAMP,
    PRIMARY KEY (date, code)
);

CREATE TABLE IF NOT EXISTS picks (
    pick_date         DATE      NOT NULL,
    code              VARCHAR   NOT NULL,
    name              VARCHAR,
    market            VARCHAR,
    rating            VARCHAR,
    total_score       DOUBLE,
    ai_score          DOUBLE,
    val_score         DOUBLE,
    trend_score       DOUBLE,
    cred_score        DOUBLE,
    ai_relevance      VARCHAR,
    theme             VARCHAR,
    entry_price       DOUBLE,
    entry_currency    VARCHAR,
    peg_at_pick       DOUBLE,
    fpe_at_pick       DOUBLE,
    ytd_at_pick       DOUBLE,
    one_week_at_pick  DOUBLE,
    one_year_at_pick  DOUBLE,
    model_source      VARCHAR,
    signal            VARCHAR,   -- buy / avoid / watch（结构化分流，不再靠 rating 文本前缀）
    coverage_score    DOUBLE,    -- 因子有效覆盖率；低覆盖推荐会被降权/降级
    missing_factors   VARCHAR,   -- 逗号分隔的缺失因子名
    factor_weights_used VARCHAR, -- JSON: IC gate 后实际参与 composite 的因子权重
    universe_scope    VARCHAR,   -- manual_watchlist / system_tech_universe
    source_origin     VARCHAR,   -- self_pool / system_pool
    PRIMARY KEY (pick_date, code)
);

CREATE TABLE IF NOT EXISTS reviews (
    review_date        DATE      NOT NULL,
    pick_date          DATE      NOT NULL,
    code               VARCHAR   NOT NULL,
    name               VARCHAR,
    entry_price        DOUBLE,
    current_price      DOUBLE,
    pct                DOUBLE,
    days_held          INTEGER,
    grade              VARCHAR,
    rating             VARCHAR,
    theme              VARCHAR,
    entry_spy_price    DOUBLE,    -- 入选日 SPY 收盘
    current_spy_price  DOUBLE,    -- review_date 当天 SPY 最新
    alpha_pct          DOUBLE,    -- pct - spy_pct（超额收益）
    model_source       VARCHAR,
    signal             VARCHAR,   -- buy / avoid
    benchmark_code     VARCHAR,
    benchmark_pct      DOUBLE,
    is_success         BOOLEAN,
    PRIMARY KEY (review_date, pick_date, code)
);

-- 2026-05-11 起 watchlist 从飞书迁移到 DuckDB，dashboard 提供 CRUD UI
-- 2026-05-11 PM: 增加 chain / chain_tier / chain_role / layman_intro 四字段
--   chain        产业链名（"HBM"、"AI 算力"、"数据中心电力"...），多链用逗号分隔
--   chain_tier   链条层级："核心" / "一线" / "二线" / "三线" / "N/A"
--   chain_role   链条角色："IDM" / "GPU" / "设备" / "材料" / "封测" / "EDA" /
--                "网络芯片" / "服务器" / "应用层" / "基础设施" / "服务" / "对照"
--   layman_intro 新手 1 句话解释，<60 字
CREATE TABLE IF NOT EXISTS watchlist (
    code           VARCHAR    PRIMARY KEY,
    name           VARCHAR,
    market         VARCHAR,
    business       VARCHAR,
    industry       VARCHAR,
    ai_relevance   VARCHAR,
    ai_logic       VARCHAR,
    theme          VARCHAR,
    conclusion     VARCHAR,
    risks          VARCHAR,
    peers          VARCHAR,
    rhythm         VARCHAR,
    status         VARCHAR,
    source         VARCHAR,
    credibility    VARCHAR,
    notes          VARCHAR,
    chain          VARCHAR,
    chain_tier     VARCHAR,
    chain_role     VARCHAR,
    layman_intro   VARCHAR,
    -- 2026-05-11 PM 第二轮:飞书 100% 退役,人工研究字段迁入 DuckDB
    earnings        VARCHAR,
    verification    VARCHAR,
    info_breakdown  VARCHAR,
    created_at     TIMESTAMP  DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP  DEFAULT CURRENT_TIMESTAMP
);

-- 幂等 ALTER：兼容 schema 升级前已存在的库
ALTER TABLE picks     ADD COLUMN IF NOT EXISTS model_source      VARCHAR;
ALTER TABLE picks     ADD COLUMN IF NOT EXISTS signal            VARCHAR;
ALTER TABLE picks     ADD COLUMN IF NOT EXISTS coverage_score    DOUBLE;
ALTER TABLE picks     ADD COLUMN IF NOT EXISTS missing_factors   VARCHAR;
ALTER TABLE picks     ADD COLUMN IF NOT EXISTS factor_weights_used VARCHAR;
ALTER TABLE picks     ADD COLUMN IF NOT EXISTS universe_scope    VARCHAR;
ALTER TABLE picks     ADD COLUMN IF NOT EXISTS source_origin     VARCHAR;
ALTER TABLE reviews   ADD COLUMN IF NOT EXISTS entry_spy_price   DOUBLE;
ALTER TABLE reviews   ADD COLUMN IF NOT EXISTS current_spy_price DOUBLE;
ALTER TABLE reviews   ADD COLUMN IF NOT EXISTS alpha_pct         DOUBLE;
ALTER TABLE reviews   ADD COLUMN IF NOT EXISTS model_source      VARCHAR;
ALTER TABLE reviews   ADD COLUMN IF NOT EXISTS signal            VARCHAR;
ALTER TABLE reviews   ADD COLUMN IF NOT EXISTS benchmark_code    VARCHAR;
ALTER TABLE reviews   ADD COLUMN IF NOT EXISTS benchmark_pct     DOUBLE;
ALTER TABLE reviews   ADD COLUMN IF NOT EXISTS is_success        BOOLEAN;
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS chain          VARCHAR;
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS chain_tier     VARCHAR;
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS chain_role     VARCHAR;
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS layman_intro   VARCHAR;
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS earnings       VARCHAR;
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS verification   VARCHAR;
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS info_breakdown VARCHAR;

-- 2026-05-11 PM: 候选发现历史 + 推荐准确度跟踪
--   discovery_history  每天 discover_candidates 跑完追加(不覆盖) → 永久快照
--   discovery_tracking 每天 evaluate_discovery 刷新一次 → N 天后的 alpha
CREATE TABLE IF NOT EXISTS discovery_history (
    generated_date  DATE     NOT NULL,
    rank            INTEGER  NOT NULL,
    ticker          VARCHAR  NOT NULL,
    name            VARCHAR,
    sector          VARCHAR,
    market          VARCHAR,
    composite_z     DOUBLE,
    f_score         DOUBLE,
    momentum_12_1   DOUBLE,
    pead            DOUBLE,
    analyst_score   DOUBLE,
    market_cap_usd  DOUBLE,
    PRIMARY KEY (generated_date, ticker)
);

CREATE TABLE IF NOT EXISTS discovery_tracking (
    generated_date     DATE     NOT NULL,
    ticker             VARCHAR  NOT NULL,
    entry_price        DOUBLE,
    pct_1d             DOUBLE,
    pct_5d             DOUBLE,
    pct_20d            DOUBLE,
    pct_60d            DOUBLE,
    benchmark_code     VARCHAR,
    benchmark_pct_1d   DOUBLE,
    benchmark_pct_5d   DOUBLE,
    benchmark_pct_20d  DOUBLE,
    benchmark_pct_60d  DOUBLE,
    alpha_1d           DOUBLE,
    alpha_5d           DOUBLE,
    alpha_20d          DOUBLE,
    alpha_60d          DOUBLE,
    last_refreshed_at  TIMESTAMP,
    PRIMARY KEY (generated_date, ticker)
);

-- 2026-05-11 PM: 用户级配置 key-value 表（投资方案总资金/止损线等可调参数）
CREATE TABLE IF NOT EXISTS user_config (
    key        VARCHAR PRIMARY KEY,
    value      VARCHAR NOT NULL,  -- 任意 JSON 字符串
    updated_at TIMESTAMP
);

-- 2026-05-12: 持仓表 — 从 dashboard 的 localStorage 迁过来
--   动机：让后端脚本 (trade_delta / risk_metrics / 等) 能读到真实持仓
--   主键：一只股可以分批建仓(不同时间不同价)，所以用自增 id 而非 code
--   source：'manual'(用户手填) / 'ai_plan'(从 AI 组合方案抄进来)
CREATE SEQUENCE IF NOT EXISTS holdings_id_seq;
CREATE TABLE IF NOT EXISTS holdings (
    id          INTEGER   PRIMARY KEY DEFAULT nextval('holdings_id_seq'),
    code        VARCHAR   NOT NULL,
    entry_price DOUBLE    NOT NULL,
    shares      DOUBLE    NOT NULL,
    entry_date  DATE,
    source      VARCHAR   DEFAULT 'manual',
    notes       VARCHAR,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- earnings_history（季报历史归档）
--   每只股票每个 fiscal_period 一行；重复拉同一季度按 (code, fiscal_period) upsert
--   watchlist.earnings 字段仍存"最新一句话摘要"给看板用；本表存结构化纵深，画趋势 / 看历次同比
--   source = 'yfinance_quarterly' 或 'yfinance_ttm_fallback'（季报不可用时降级到 info TTM）
CREATE TABLE IF NOT EXISTS earnings_history (
    code               VARCHAR   NOT NULL,
    fiscal_period      DATE      NOT NULL,     -- 季度末日期，如 2026-03-31
    revenue            DOUBLE,
    net_income         DOUBLE,
    diluted_eps        DOUBLE,
    revenue_yoy_pct    DOUBLE,
    net_income_yoy_pct DOUBLE,
    eps_yoy_pct        DOUBLE,
    currency           VARCHAR,
    source             VARCHAR,
    fetched_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (code, fiscal_period)
);
"""

# user_config 已知 key 的默认值（首次读取或被删除时返回）
USER_CONFIG_DEFAULTS = {
    "total_capital": 500000,   # 进场本金，跑批 + 前端共用
    "stoploss_line": 300000,   # 止损红线（组合市值跌至此则强制清仓）
}


def get_db(path: str = DB_PATH) -> duckdb.DuckDBPyConnection:
    """打开 DuckDB 连接并保证 schema 存在。"""
    conn = duckdb.connect(path)
    conn.execute(SCHEMA_SQL)
    # Do not infer production model source from market. Legacy and v6 pipelines
    # can write the same market, so old NULL rows must stay out of production
    # views until a writer explicitly tags them.
    conn.execute("""
        UPDATE picks
        SET model_source = 'legacy_unknown'
        WHERE model_source IS NULL
    """)
    conn.execute("""
        UPDATE picks
        SET model_source = 'legacy'
        WHERE model_source = 'v6_us'
          AND (
              rating IS NULL
              OR (rating NOT LIKE '%z %' AND rating NOT LIKE '%不建议%' AND rating NOT LIKE '%关注%')
          )
    """)
    conn.execute("""
        UPDATE picks
        SET model_source = 'legacy'
        WHERE model_source IN ('v6_hk', 'v6_cn')
          AND (
              rating IS NULL
              OR (rating NOT LIKE '%综合%' AND rating NOT LIKE '%关注%')
          )
    """)
    conn.execute("""
        UPDATE picks AS p
        SET entry_price = (
            SELECT pr.price
            FROM prices pr
            WHERE pr.code = p.code
              AND pr.price IS NOT NULL
              AND pr.date <= p.pick_date
            ORDER BY pr.date DESC
            LIMIT 1
        )
        WHERE p.model_source IN ('v6_us', 'v6_hk', 'v6_cn')
          AND p.entry_price IS NULL
    """)
    conn.execute("""
        UPDATE picks AS p
        SET entry_currency = (
            SELECT pr.currency
            FROM prices pr
            WHERE pr.code = p.code
              AND pr.currency IS NOT NULL
              AND pr.date <= p.pick_date
            ORDER BY pr.date DESC
            LIMIT 1
        )
        WHERE p.model_source IN ('v6_us', 'v6_hk', 'v6_cn')
          AND p.entry_currency IS NULL
    """)
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


def upsert_prices(
    rows: Iterable[Mapping[str, Any]],
    *,
    snapshot_date: date | str | None = None,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    """落价格快照。每只股票每天一行；同日重跑覆盖。

    rows 期望与 fetch_stock_prices.py 的 results 格式一致。
    snapshot_date 默认取每行 fetched_at 的日期，缺失则取今天。
    """
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    snap_default = _to_date(snapshot_date) if snapshot_date else date.today()
    n = 0
    for r in rows:
        fetched = _to_ts(r.get("fetched_at"))
        d = snap_default if snapshot_date else (
            fetched.date() if fetched else date.today()
        )
        values = [
            d,
            r.get("code"),
            r.get("name"),
            r.get("yf_ticker"),
            r.get("price"),
            r.get("prev_close"),
            r.get("currency"),
            r.get("market_cap"),
            r.get("forward_pe"),
            r.get("trailing_pe"),
            r.get("peg_ratio"),
            r.get("earnings_growth_pct"),
            r.get("revenue_growth_pct"),
            r.get("ytd_pct"),
            r.get("one_year_pct"),
            r.get("one_month_pct"),
            r.get("one_week_pct"),
            fetched,
        ]
        placeholders = ",".join(["?"] * len(PRICE_COLS))
        update_set = ",".join(f"{c}=excluded.{c}" for c in PRICE_COLS if c not in ("date", "code"))
        conn.execute(
            f"INSERT INTO prices ({','.join(PRICE_COLS)}) VALUES ({placeholders}) "
            f"ON CONFLICT (date, code) DO UPDATE SET {update_set}",
            values,
        )
        n += 1
    if own_conn:
        conn.close()
    return n


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


def upsert_picks(
    rows: Iterable[Mapping[str, Any]],
    *,
    pick_date: date | str | None = None,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    """落入选记录。同日同代码重跑覆盖。"""
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    d = _to_date(pick_date) if pick_date else date.today()
    n = 0
    for r in rows:
        sig = r.get("signal") or _infer_signal_from_rating(r.get("rating"))
        values = [
            _to_date(r.get("pick_date")) or d,
            r.get("code"),
            r.get("name"),
            r.get("market"),
            r.get("rating"),
            r.get("total_score"),
            r.get("ai_score"),
            r.get("val_score"),
            r.get("trend_score"),
            r.get("cred_score"),
            r.get("ai_relevance"),
            r.get("theme"),
            r.get("entry_price"),
            r.get("entry_currency"),
            r.get("peg_at_pick"),
            r.get("fpe_at_pick"),
            r.get("ytd_at_pick"),
            r.get("one_week_at_pick"),
            r.get("one_year_at_pick"),
            r.get("model_source"),
            sig,
            r.get("coverage_score"),
            r.get("missing_factors"),
            r.get("factor_weights_used"),
            r.get("universe_scope"),
            r.get("source_origin"),
        ]
        placeholders = ",".join(["?"] * len(PICK_COLS))
        update_set = ",".join(f"{c}=excluded.{c}" for c in PICK_COLS if c not in ("pick_date", "code"))
        conn.execute(
            f"INSERT INTO picks ({','.join(PICK_COLS)}) VALUES ({placeholders}) "
            f"ON CONFLICT (pick_date, code) DO UPDATE SET {update_set}",
            values,
        )
        n += 1
    if own_conn:
        conn.close()
    return n


REVIEW_COLS = [
    "review_date", "pick_date", "code", "name",
    "entry_price", "current_price", "pct", "days_held",
    "grade", "rating", "theme",
    "entry_spy_price", "current_spy_price", "alpha_pct",
    "model_source", "signal", "benchmark_code", "benchmark_pct", "is_success",
]


def upsert_reviews(
    rows: Iterable[Mapping[str, Any]],
    *,
    review_date: date | str | None = None,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    d = _to_date(review_date) if review_date else date.today()
    n = 0
    for r in rows:
        values = [
            _to_date(r.get("review_date")) or d,
            _to_date(r.get("pick_date")),
            r.get("code"),
            r.get("name"),
            r.get("entry_price"),
            r.get("current_price"),
            r.get("pct"),
            r.get("days_held"),
            r.get("grade"),
            r.get("rating"),
            r.get("theme"),
            r.get("entry_spy_price"),
            r.get("current_spy_price"),
            r.get("alpha_pct"),
            r.get("model_source"),
            r.get("signal"),
            r.get("benchmark_code"),
            r.get("benchmark_pct"),
            r.get("is_success"),
        ]
        placeholders = ",".join(["?"] * len(REVIEW_COLS))
        update_set = ",".join(
            f"{c}=excluded.{c}" for c in REVIEW_COLS
            if c not in ("review_date", "pick_date", "code")
        )
        conn.execute(
            f"INSERT INTO reviews ({','.join(REVIEW_COLS)}) VALUES ({placeholders}) "
            f"ON CONFLICT (review_date, pick_date, code) DO UPDATE SET {update_set}",
            values,
        )
        n += 1
    if own_conn:
        conn.close()
    return n


# ============================================================
# 飞书 picks/records 兼容读 — 2026-05-11 PM 第二轮:飞书 100% 退役
# 这两个函数返回与原 extract_picks / extract_records 同 shape 的 dict,
# 调用方(dashboard build / jobs) 改一行 import 即可切走飞书。
# ============================================================


def fetch_picks_view(*, conn: duckdb.DuckDBPyConnection | None = None) -> list[dict]:
    """返回自选股 picks 看板视图.

    字段对齐原 extract_picks: code/name/rating/score/entry_price/current_price/
    pct/days_held/grade/theme/ai_relevance/pick_date.

    这是「自选股·AI 优选」视图，只展示仍在 watchlist 里的人工自选股。
    picks-only / discovery-only 候选应留在 AI 推荐或 DB 全库页，不能混进自选股。

    reviews 是收益跟踪表, picks 是当天选股事实表。daily_refresh 里 reviews 可能早于
    后续市场专线写入, 所以这里在最新 review 快照之外, 额外补齐最新 pick_date 的
    picks 行, 避免 dashboard 今日入选依赖滞后的 reviews/JSON 产物。
    """
    own = conn is None
    if own:
        conn = get_db()
    rows = conn.execute("""
        WITH latest_price AS (
            SELECT * FROM prices
            WHERE (code, date) IN (
                SELECT code, MAX(date) FROM prices GROUP BY code
            )
        ),
        review_rows AS (
            SELECT
                r.code, COALESCE(w.name, r.name) AS name, COALESCE(w.market, p.market, '') AS market,
                r.rating, r.pick_date, r.entry_price, r.current_price,
                r.pct, r.days_held, r.grade, r.theme,
                p.total_score AS score, p.ai_relevance,
                p.total_score, p.ai_score, p.val_score, p.trend_score, p.cred_score,
                COALESCE(r.model_source, p.model_source, 'legacy_unknown') AS model_source,
                COALESCE(r.signal, p.signal,
                    CASE
                      WHEN r.rating LIKE '%⛔%' OR r.rating LIKE '%不建议%' THEN 'avoid'
                      WHEN r.rating LIKE '%观察%' THEN 'watch'
                      WHEN r.rating IS NULL OR TRIM(r.rating) = '' THEN 'watch'
                      ELSE 'buy'
                    END) AS signal
            FROM reviews r
            INNER JOIN watchlist w ON w.code = r.code
            LEFT JOIN picks p ON p.code = r.code AND p.pick_date = r.pick_date
            WHERE r.review_date = (SELECT MAX(review_date) FROM reviews)
              AND COALESCE(r.model_source, p.model_source, 'legacy_unknown')
                  IN ('v6_us', 'v6_hk', 'v6_cn')
        ),
        v6_market_latest AS (
            -- 每个 v6 pipeline 各自的最新 pick_date；legacy 行被排除
            SELECT model_source, MAX(pick_date) AS max_date
            FROM picks
            WHERE model_source IN ('v6_us', 'v6_hk', 'v6_cn')
            GROUP BY model_source
        ),
        latest_pick_rows AS (
            SELECT
                p.code, COALESCE(w.name, p.name) AS name, COALESCE(w.market, p.market, '') AS market,
                p.rating, p.pick_date, p.entry_price, lp.price AS current_price,
                CASE
                    WHEN p.entry_price IS NOT NULL
                      AND p.entry_price != 0
                      AND lp.price IS NOT NULL
                      AND lp.date > p.pick_date
                    THEN (lp.price / p.entry_price - 1) * 100
                    ELSE NULL
                END AS pct,
                CASE
                    WHEN lp.date IS NOT NULL THEN date_diff('day', p.pick_date, lp.date)
                    ELSE 0
                END AS days_held,
                NULL AS grade, p.theme,
                p.total_score AS score, p.ai_relevance,
                p.total_score, p.ai_score, p.val_score, p.trend_score, p.cred_score,
                p.model_source,
                COALESCE(p.signal,
                    CASE
                      WHEN p.rating LIKE '%⛔%' OR p.rating LIKE '%不建议%' THEN 'avoid'
                      WHEN p.rating LIKE '%观察%' THEN 'watch'
                      WHEN p.rating IS NULL OR TRIM(p.rating) = '' THEN 'watch'
                      ELSE 'buy'
                    END) AS signal
            FROM picks p
            INNER JOIN watchlist w ON w.code = p.code
            INNER JOIN v6_market_latest v
              ON v.model_source = p.model_source AND v.max_date = p.pick_date
            LEFT JOIN latest_price lp ON lp.code = p.code
            WHERE NOT EXISTS (
                  SELECT 1 FROM review_rows r
                  WHERE r.code = p.code AND r.pick_date = p.pick_date
              )
        )
        SELECT
            code, name, market, rating, pick_date, entry_price, current_price,
            pct, days_held, grade, theme, score, ai_relevance,
            total_score, ai_score, val_score, trend_score, cred_score, model_source, signal
        FROM review_rows
        UNION ALL
        SELECT
            code, name, market, rating, pick_date, entry_price, current_price,
            pct, days_held, grade, theme, score, ai_relevance,
            total_score, ai_score, val_score, trend_score, cred_score, model_source, signal
        FROM latest_pick_rows
        ORDER BY code, pick_date
    """).fetchall()
    cols = ["code", "name", "market", "rating", "pick_date", "entry_price", "current_price",
            "pct", "days_held", "grade", "theme", "score", "ai_relevance",
            "total_score", "ai_score", "val_score", "trend_score", "cred_score", "model_source", "signal"]
    out = [dict(zip(cols, r)) for r in rows]
    if own:
        conn.close()
    return out


def fetch_records_view(*, conn: duckdb.DuckDBPyConnection | None = None) -> list[dict]:
    """返回 watchlist LEFT JOIN 最新 prices 的视图.

    字段对齐原 extract_records: 89 条 watchlist + 实时 enrichment(market_cap /
    forward_pe / peg / ytd_pct 等) 来自 prices 表的最新一行.
    """
    own = conn is None
    if own:
        conn = get_db()
    rows = conn.execute("""
        WITH latest_price AS (
            SELECT * FROM prices
            WHERE (code, date) IN (
                SELECT code, MAX(date) FROM prices GROUP BY code
            )
        )
        SELECT
            w.code, w.name, w.market, w.business, w.industry,
            w.ai_relevance, w.ai_logic, w.conclusion, w.risks, w.peers,
            w.rhythm, w.status, w.source, w.credibility,
            w.earnings, w.verification, w.info_breakdown,
            lp.price          AS latest_price,
            lp.market_cap     AS yf_market_cap,
            lp.forward_pe,
            lp.peg_ratio      AS peg,
            lp.earnings_growth_pct,
            lp.ytd_pct,
            lp.one_year_pct,
            lp.one_month_pct,
            lp.one_week_pct,
            lp.date           AS price_date,
            lp.fetched_at     AS price_fetched_at,
            w.updated_at      AS analysis_updated_at
        FROM watchlist w
        LEFT JOIN latest_price lp ON lp.code = w.code
        ORDER BY w.code
    """).fetchall()
    cols = ["code", "name", "market", "business", "industry",
            "ai_relevance", "ai_logic", "conclusion", "risks", "peers",
            "rhythm", "status", "source", "credibility",
            "earnings", "verification", "info_breakdown",
            "latest_price", "yf_market_cap", "forward_pe", "peg",
            "earnings_growth_pct", "ytd_pct", "one_year_pct",
            "one_month_pct", "one_week_pct", "price_date",
            "price_fetched_at", "analysis_updated_at"]
    out = [dict(zip(cols, r)) for r in rows]
    # 注:旧 extract_records 还返回 market_cap (人工填写的"当前市值"字符串)
    # DuckDB 不存,统一用 yf_market_cap 替代.调用方需要的话自己 fallback.
    if own:
        conn.close()
    return out


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

WATCHLIST_COLS = [
    "code", "name", "market", "business", "industry",
    "ai_relevance", "ai_logic", "theme", "conclusion", "risks",
    "peers", "rhythm", "status", "source", "credibility", "notes",
    "chain", "chain_tier", "chain_role", "layman_intro",
    "earnings", "verification", "info_breakdown",
]


def _normalize_watchlist_market(value: Any, code: str) -> str | None:
    s = value.strip() if isinstance(value, str) else ""
    if s in ("", "其他", "未知", "N/A"):
        try:
            from stock_research.core.watchlist_enrich import _infer_market
            s = _infer_market(code or "")
        except Exception:
            s = ""
    if not s:
        return None
    s = s.replace(" NASDAQ", "·NASDAQ").replace(" NYSE", "·NYSE")
    s = s.replace("·深交所主板", "·深交所").replace("·上交所主板", "·上交所")
    s = s.replace("·沪交所", "·上交所")
    return s


def fetch_all_watchlist(*, conn: duckdb.DuckDBPyConnection | None = None) -> list[dict]:
    """读全部 watchlist 记录，按 code 升序。"""
    own = conn is None
    if own:
        conn = get_db()
    rows = conn.execute(
        f"SELECT {','.join(WATCHLIST_COLS)}, created_at, updated_at "
        "FROM watchlist ORDER BY code"
    ).fetchall()
    cols = WATCHLIST_COLS + ["created_at", "updated_at"]
    out = [dict(zip(cols, r)) for r in rows]
    if own:
        conn.close()
    return out


def get_watchlist_item(code: str, *, conn: duckdb.DuckDBPyConnection | None = None) -> dict | None:
    own = conn is None
    if own:
        conn = get_db()
    row = conn.execute(
        f"SELECT {','.join(WATCHLIST_COLS)}, created_at, updated_at "
        "FROM watchlist WHERE code = ?", [code]
    ).fetchone()
    if own:
        conn.close()
    if not row:
        return None
    return dict(zip(WATCHLIST_COLS + ["created_at", "updated_at"], row))


def upsert_watchlist(
    rows: Iterable[Mapping[str, Any]],
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    """新增 / 更新 watchlist 记录（按 code 主键）。

    rows 字段同 WATCHLIST_COLS；缺失字段以 None 兜底；自动维护 updated_at = now()。
    """
    own = conn is None
    if own:
        conn = get_db()
    n = 0
    now = datetime.now()
    for r in rows:
        r = dict(r)
        r["market"] = _normalize_watchlist_market(r.get("market"), r.get("code") or "")
        values = [r.get(c) for c in WATCHLIST_COLS] + [now]
        placeholders = ",".join(["?"] * (len(WATCHLIST_COLS) + 1))
        update_set = ",".join(f"{c}=excluded.{c}" for c in WATCHLIST_COLS if c != "code")
        conn.execute(
            f"INSERT INTO watchlist ({','.join(WATCHLIST_COLS)}, updated_at) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT (code) DO UPDATE SET {update_set}, updated_at=excluded.updated_at",
            values,
        )
        n += 1
    if own:
        conn.close()
    return n


def update_watchlist_fields(
    code: str,
    fields: Mapping[str, Any],
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    """部分字段更新(只更新传入的 keys,不动其它).

    用于 enrich_watchlist / daily_audit 等 job 把分析结果落到 DuckDB 而非飞书。
    返回更新的行数(0 表示 code 不在 watchlist 表里).
    """
    if not fields:
        return 0
    valid = {k: v for k, v in fields.items() if k in WATCHLIST_COLS and k != "code"}
    if not valid:
        return 0
    own = conn is None
    if own:
        conn = get_db()
    exists = conn.execute("SELECT 1 FROM watchlist WHERE code=?", [code]).fetchone()
    if not exists:
        if own:
            conn.close()
        return 0
    set_clause = ", ".join(f"{c}=?" for c in valid)
    values = list(valid.values()) + [code]
    conn.execute(
        f"UPDATE watchlist SET {set_clause}, updated_at=CURRENT_TIMESTAMP WHERE code=?",
        values,
    )
    if own:
        conn.close()
    return 1


def delete_watchlist_item(code: str, *, conn: duckdb.DuckDBPyConnection | None = None) -> int:
    own = conn is None
    if own:
        conn = get_db()
    exists = conn.execute("SELECT 1 FROM watchlist WHERE code = ?", [code]).fetchone()
    n = 0
    if exists:
        conn.execute("DELETE FROM watchlist WHERE code = ?", [code])
        n = 1
    if own:
        conn.close()
    return n


# ============================================================
# V2 AI 推荐 / 组合方案候选池（2026-05-20）
# 按 docs/V2/产品基线.md：AI 推荐 / AI 组合方案严禁读 watchlist；
# 它们的候选范围是 system_universe + 最新 recommendation_picks。
# 这里两个 helper 提供 V1-watchlist 兼容形状（code/name/market/industry/theme），
# 让 optimize_portfolio / build_pool_recommendations / apply_a_share_constraints
# 直接迁移过来，不依赖 watchlist 是否被用户填充。
# ============================================================


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
# Discovery 历史 + 准确度跟踪(2026-05-11 PM 新增)
# ============================================================

DISCOVERY_HISTORY_COLS = [
    "generated_date", "rank", "ticker", "name", "sector", "market",
    "composite_z", "f_score", "momentum_12_1", "pead", "analyst_score",
    "market_cap_usd",
]


def upsert_discovery_history(
    rows: Iterable[Mapping[str, Any]],
    *,
    generated_date: date | str | None = None,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    """落候选发现快照。同日同 ticker 重跑覆盖,跨日累积。

    rows 每条字段对齐 discover_candidates.py 的 candidates(ticker/name/sector/composite_z 等)。
    """
    own = conn is None
    if own:
        conn = get_db()
    default_date = _to_date(generated_date) if generated_date else date.today()
    n = 0
    for r in rows:
        values = [
            _to_date(r.get("generated_date")) or default_date,
            r.get("rank"),
            r.get("ticker"),
            r.get("name"),
            r.get("sector"),
            r.get("market") or r.get("location"),
            r.get("composite_z"),
            r.get("f_score"),
            r.get("momentum_12_1"),
            r.get("pead"),
            r.get("analyst_score"),
            r.get("market_cap_usd"),
        ]
        placeholders = ",".join(["?"] * len(DISCOVERY_HISTORY_COLS))
        update_set = ",".join(
            f"{c}=excluded.{c}" for c in DISCOVERY_HISTORY_COLS
            if c not in ("generated_date", "ticker")
        )
        conn.execute(
            f"INSERT INTO discovery_history ({','.join(DISCOVERY_HISTORY_COLS)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT (generated_date, ticker) DO UPDATE SET {update_set}",
            values,
        )
        n += 1
    if own:
        conn.close()
    return n


DISCOVERY_TRACKING_COLS = [
    "generated_date", "ticker", "entry_price",
    "pct_1d", "pct_5d", "pct_20d", "pct_60d",
    "benchmark_code",
    "benchmark_pct_1d", "benchmark_pct_5d", "benchmark_pct_20d", "benchmark_pct_60d",
    "alpha_1d", "alpha_5d", "alpha_20d", "alpha_60d",
    "last_refreshed_at",
]


def upsert_discovery_tracking(
    rows: Iterable[Mapping[str, Any]],
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    """落 / 更新候选发现的绩效跟踪。同 (date, ticker) 重跑覆盖。"""
    own = conn is None
    if own:
        conn = get_db()
    now = datetime.now()
    n = 0
    for r in rows:
        values = [
            _to_date(r.get("generated_date")),
            r.get("ticker"),
            r.get("entry_price"),
            r.get("pct_1d"), r.get("pct_5d"), r.get("pct_20d"), r.get("pct_60d"),
            r.get("benchmark_code"),
            r.get("benchmark_pct_1d"), r.get("benchmark_pct_5d"),
            r.get("benchmark_pct_20d"), r.get("benchmark_pct_60d"),
            r.get("alpha_1d"), r.get("alpha_5d"),
            r.get("alpha_20d"), r.get("alpha_60d"),
            r.get("last_refreshed_at") or now,
        ]
        placeholders = ",".join(["?"] * len(DISCOVERY_TRACKING_COLS))
        update_set = ",".join(
            f"{c}=excluded.{c}" for c in DISCOVERY_TRACKING_COLS
            if c not in ("generated_date", "ticker")
        )
        conn.execute(
            f"INSERT INTO discovery_tracking ({','.join(DISCOVERY_TRACKING_COLS)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT (generated_date, ticker) DO UPDATE SET {update_set}",
            values,
        )
        n += 1
    if own:
        conn.close()
    return n


def fetch_discovery_history(
    *, days: int | None = 60,
    conn: duckdb.DuckDBPyConnection | None = None
) -> list[dict]:
    """读 discovery_history JOIN discovery_tracking,返回每个 (date, ticker) 的完整记录。
    days=None 表示读全部。
    """
    own = conn is None
    if own:
        conn = get_db()
    sql = """
        SELECT h.*,
               t.entry_price, t.pct_1d, t.pct_5d, t.pct_20d, t.pct_60d,
               t.benchmark_code,
               t.benchmark_pct_1d, t.benchmark_pct_5d,
               t.benchmark_pct_20d, t.benchmark_pct_60d,
               t.alpha_1d, t.alpha_5d, t.alpha_20d, t.alpha_60d,
               t.last_refreshed_at
        FROM discovery_history h
        LEFT JOIN discovery_tracking t
          ON h.generated_date = t.generated_date AND h.ticker = t.ticker
    """
    if days is not None:
        sql += f" WHERE h.generated_date >= CURRENT_DATE - INTERVAL '{int(days)}' DAY"
    sql += " ORDER BY h.generated_date DESC, h.rank ASC"
    rows = conn.execute(sql).fetchall()
    cols = [d[0] for d in conn.description]
    out = [dict(zip(cols, r)) for r in rows]
    if own:
        conn.close()
    return out


# ============================================================
# 便捷查询
# ============================================================

def latest_price(code: str, *, conn: duckdb.DuckDBPyConnection | None = None) -> dict | None:
    own = conn is None
    if own:
        conn = get_db()
    row = conn.execute(
        "SELECT * FROM prices WHERE code = ? ORDER BY date DESC LIMIT 1", [code]
    ).fetchone()
    cols = [d[0] for d in conn.description] if row else []
    if own:
        conn.close()
    return dict(zip(cols, row)) if row else None


# ============================================================
# user_config（投资方案等用户级配置）
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


def upsert_earnings_history(
    code: str,
    quarters: Iterable[Mapping[str, Any]],
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    """upsert earnings_history 多行（按 (code, fiscal_period) 主键）。

    quarters 每条字段：fiscal_period (date|str), revenue, net_income, diluted_eps,
                       revenue_yoy_pct, net_income_yoy_pct, eps_yoy_pct, currency, source
    返回写入行数。
    """
    own = conn is None
    if own:
        conn = get_db()
    n = 0
    now = datetime.now()
    for q in quarters:
        fp = _to_date(q.get("fiscal_period"))
        if not fp:
            continue
        conn.execute(
            """
            INSERT INTO earnings_history
              (code, fiscal_period, revenue, net_income, diluted_eps,
               revenue_yoy_pct, net_income_yoy_pct, eps_yoy_pct, currency, source, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (code, fiscal_period) DO UPDATE SET
              revenue            = excluded.revenue,
              net_income         = excluded.net_income,
              diluted_eps        = excluded.diluted_eps,
              revenue_yoy_pct    = excluded.revenue_yoy_pct,
              net_income_yoy_pct = excluded.net_income_yoy_pct,
              eps_yoy_pct        = excluded.eps_yoy_pct,
              currency           = excluded.currency,
              source             = excluded.source,
              fetched_at         = excluded.fetched_at
            """,
            [
                code, fp,
                q.get("revenue"), q.get("net_income"), q.get("diluted_eps"),
                q.get("revenue_yoy_pct"), q.get("net_income_yoy_pct"), q.get("eps_yoy_pct"),
                q.get("currency"), q.get("source"), now,
            ],
        )
        n += 1
    if own:
        conn.close()
    return n


def fetch_earnings_history(
    code: str,
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> list[dict]:
    """返回某只股票的全部 earnings_history，按 fiscal_period 倒序。"""
    own = conn is None
    if own:
        conn = get_db()
    cur = conn.execute(
        "SELECT * FROM earnings_history WHERE code = ? ORDER BY fiscal_period DESC",
        [code],
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    if own:
        conn.close()
    return rows


def stats() -> dict:
    """快速看库状态：表行数 + 时间跨度。"""
    conn = get_db()
    out = {}
    for tbl, date_col in [("prices", "date"), ("picks", "pick_date"), ("reviews", "review_date")]:
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
