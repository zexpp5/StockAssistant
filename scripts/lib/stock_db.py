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

import hashlib
import json
import os
import time
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
-- 账本 v2 聚合缓存列：real_holdings 升级为「交易流水聚合结果」。
-- 旧列 entry_price/shares/entry_date/cost_rmb_locked 继续作为兼容字段，由 rebuild 回写。
ALTER TABLE real_holdings ADD COLUMN IF NOT EXISTS total_buy_shares DOUBLE;
ALTER TABLE real_holdings ADD COLUMN IF NOT EXISTS remaining_shares DOUBLE;
ALTER TABLE real_holdings ADD COLUMN IF NOT EXISTS avg_cost_local_per_share DOUBLE;
ALTER TABLE real_holdings ADD COLUMN IF NOT EXISTS avg_cost_rmb_per_share DOUBLE;
ALTER TABLE real_holdings ADD COLUMN IF NOT EXISTS remaining_cost_rmb DOUBLE;
ALTER TABLE real_holdings ADD COLUMN IF NOT EXISTS position_epoch INTEGER;
ALTER TABLE real_holdings ADD COLUMN IF NOT EXISTS first_entry_date DATE;
ALTER TABLE real_holdings ADD COLUMN IF NOT EXISTS last_trade_date DATE;
ALTER TABLE real_holdings ADD COLUMN IF NOT EXISTS close_status VARCHAR;

-- 交易流水：账本 v2 的唯一事实源。real_holdings 由本表 rebuild 聚合得到。
-- 一笔 buy/sell 一行；加权平均成本、已实现盈亏、持仓轮次都从这里回放。
CREATE SEQUENCE IF NOT EXISTS real_holding_trades_id_seq;
CREATE TABLE IF NOT EXISTS real_holding_trades (
    trade_id          INTEGER PRIMARY KEY DEFAULT nextval('real_holding_trades_id_seq'),
    account           VARCHAR DEFAULT 'default',
    market            VARCHAR NOT NULL,
    symbol            VARCHAR NOT NULL,
    name              VARCHAR,
    side              VARCHAR NOT NULL,           -- buy | sell
    trade_price       DOUBLE  NOT NULL,           -- 原币成交价
    quantity          DOUBLE  NOT NULL,           -- 股数（支持小数股）
    trade_date        DATE    NOT NULL,           -- 真实成交日（rebuild 主排序键）
    executed_at       TIMESTAMP,                  -- 可选：同日多笔成交排序
    order_in_day      INTEGER,                    -- 可选：无成交时间时的人工同日顺序
    currency          VARCHAR,
    fx_rate           DOUBLE,                     -- 成交日锁定汇率：1 本币 = ? RMB
    fx_as_of          DATE,
    fx_source         VARCHAR,
    gross_amount_rmb  DOUBLE,                     -- trade_price * quantity * fx_rate
    fee_amount        DOUBLE  DEFAULT 0,
    fee_currency      VARCHAR,
    fee_fx_rate       DOUBLE,
    fee_rmb           DOUBLE  DEFAULT 0,
    client_request_id VARCHAR,                    -- 幂等键：同键重复提交返回同一笔
    position_epoch    INTEGER,                    -- 由 rebuild 按轮次赋值
    status            VARCHAR NOT NULL DEFAULT 'active',  -- active | voided
    realized_pnl_rmb  DOUBLE,                     -- sell 专用派生快照（rebuild 重算）
    realized_pnl_pct  DOUBLE,                     -- sell 专用派生快照
    cost_basis_rmb    DOUBLE,                     -- sell 专用派生快照：本次卖出对应成本(RMB)
    cost_basis_local  DOUBLE,                     -- sell 专用派生快照：本次卖出对应成本(原币)
    notes             VARCHAR,
    source            VARCHAR DEFAULT 'manual',
    voided_at         TIMESTAMP,
    void_reason       VARCHAR,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE real_holding_trades ADD COLUMN IF NOT EXISTS cost_basis_local DOUBLE;
ALTER TABLE real_holding_trades ADD COLUMN IF NOT EXISTS holding_days INTEGER;
-- 幂等键唯一：同 account 下同一 client_request_id 只入账一次（NULL 视为彼此相异，允许多条）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_rht_client_request
    ON real_holding_trades(account, client_request_id);
CREATE INDEX IF NOT EXISTS idx_rht_key
    ON real_holding_trades(account, market, symbol, status);

-- 现金流水：入金/出金。买入/卖出占用/回笼的现金从 real_holding_trades 自动算，
-- 这里只记用户手动的入金、出金。现金余额 = 入金 - 出金 - 买入(含费) + 卖出(扣费)。
CREATE SEQUENCE IF NOT EXISTS real_holding_cash_flows_id_seq;
CREATE TABLE IF NOT EXISTS real_holding_cash_flows (
    flow_id     INTEGER PRIMARY KEY DEFAULT nextval('real_holding_cash_flows_id_seq'),
    account     VARCHAR DEFAULT 'default',
    flow_type   VARCHAR NOT NULL,        -- deposit 入金 | withdraw 出金
    amount_rmb  DOUBLE  NOT NULL,        -- 恒正，方向由 flow_type 决定
    flow_date   DATE,
    notes       VARCHAR,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

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

-- 2026-05-29 lot accounting: PK 改为 (review_run_id, holding_id) 支持同 symbol 多 row 独立追踪
-- holding_id 引用 real_holdings.id (broker 业界标准 per-lot tracking)
CREATE TABLE IF NOT EXISTS real_holding_review_items (
    review_run_id      VARCHAR NOT NULL,
    holding_id         INTEGER NOT NULL,
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
    price_trade_date   VARCHAR,
    prev_close         DOUBLE,
    prev_trade_date    VARCHAR,
    day_change_basis   VARCHAR,
    day_change_rmb     DOUBLE,
    day_change_pct     DOUBLE,
    price_is_prior_session BOOLEAN,
    size_advisory_json VARCHAR,
    industry_heat_json VARCHAR,
    discipline_json    VARCHAR,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (review_run_id, holding_id)
);
ALTER TABLE real_holding_review_items ADD COLUMN IF NOT EXISTS price_trade_date VARCHAR;
ALTER TABLE real_holding_review_items ADD COLUMN IF NOT EXISTS day_change_basis VARCHAR;
ALTER TABLE real_holding_review_items ADD COLUMN IF NOT EXISTS price_is_prior_session BOOLEAN;
ALTER TABLE real_holding_review_items ADD COLUMN IF NOT EXISTS discipline_json VARCHAR;

-- 真实持仓纪律计划：只绑定 real_holdings，不写 watchlist / AI 推荐 / 模拟仓。
CREATE TABLE IF NOT EXISTS real_holding_discipline_plans (
    plan_id           VARCHAR PRIMARY KEY,
    holding_id        INTEGER NOT NULL,
    account           VARCHAR,
    market            VARCHAR NOT NULL,
    symbol            VARCHAR NOT NULL,
    plan_type         VARCHAR NOT NULL DEFAULT 'manual_price_plan',
    source_type       VARCHAR NOT NULL DEFAULT 'manual_confirmed',
    validation_status VARCHAR NOT NULL DEFAULT 'manual_guardrail_unvalidated',
    status            VARCHAR NOT NULL DEFAULT 'active',
    cost_basis_price  DOUBLE,
    shares_snapshot   DOUBLE,
    thesis            VARCHAR,
    invalidation_note VARCHAR,
    notes             VARCHAR,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    confirmed_at      TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS real_holding_discipline_triggers (
    trigger_id          VARCHAR PRIMARY KEY,
    plan_id             VARCHAR NOT NULL,
    trigger_type        VARCHAR NOT NULL,
    comparator          VARCHAR NOT NULL,
    price_min           DOUBLE,
    price_max           DOUBLE,
    severity            VARCHAR NOT NULL DEFAULT 'info',
    priority            INTEGER NOT NULL DEFAULT 99,
    action_label        VARCHAR NOT NULL,
    suggested_size_text VARCHAR,
    rationale           VARCHAR,
    auto_trade_allowed  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS real_holding_discipline_events (
    event_id        VARCHAR PRIMARY KEY,
    plan_id         VARCHAR NOT NULL,
    trigger_id      VARCHAR NOT NULL,
    holding_id      INTEGER NOT NULL,
    account         VARCHAR,
    market          VARCHAR NOT NULL,
    symbol          VARCHAR NOT NULL,
    current_price   DOUBLE,
    price_trade_date VARCHAR,
    severity        VARCHAR,
    action_label    VARCHAR,
    message         VARCHAR,
    evaluation_json VARCHAR,
    triggered_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    user_ack_status VARCHAR DEFAULT 'new',
    user_ack_at     TIMESTAMP,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- 注：manual_watchlist 的 industry / business 字段升级 ALTER 已从这里移除，
-- 因为 manual_watchlist 表由 init_stock_db_v2.py 建（已含这两列）。
-- 在不跑 init_stock_db_v2 的新 DB（test/临时）上，ALTER 不存在的表会 crash。
-- 老 DB 兼容 ALTER 移到 _ensure_manual_watchlist_columns（带 try-except 兜底）。

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

-- ============ AI 主题雷达 · 证据系统（docs/V2/AI主题雷达_产品定位.md §八）============
-- 设计原则：
--   1. 任何证据必须有 source_url，否则不能入库（seed 脚本会 assert）
--   2. source_tier ∈ {A, B, C}，A=政府/监管/公司财报，B=ETF/行业协会，C=新闻
--   3. evidence_status ∈ {candidate, confirmed, stale, needs_review, rejected}
--   4. 首版只建表，不写 SEC/PDF 抓取；用 seed 脚本灌官方 URL 种子。
CREATE TABLE IF NOT EXISTS ai_theme_evidence_sources (
    source_id        VARCHAR PRIMARY KEY,
    source_name      VARCHAR NOT NULL,
    source_tier      VARCHAR NOT NULL,    -- A | B | C
    source_type      VARCHAR NOT NULL,    -- government | regulator | company_filing | company_ir | industry | etf | news | open_dataset
    source_url       VARCHAR NOT NULL,
    update_cadence   VARCHAR,
    license_note     VARCHAR,
    last_checked_at  TIMESTAMP,
    last_check_status VARCHAR,            -- ok | http_404 | http_5xx | timeout | network_err
    last_check_http  INTEGER,
    active           BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS ai_theme_company_evidence (
    evidence_id      VARCHAR PRIMARY KEY,
    theme            VARCHAR NOT NULL,    -- liquid_cooling | rare_earths | uranium | smr | ai_data
    market           VARCHAR,
    symbol           VARCHAR,
    company_name     VARCHAR,
    evidence_status  VARCHAR NOT NULL,    -- candidate | confirmed | stale | needs_review | rejected
    source_id        VARCHAR NOT NULL,
    source_tier      VARCHAR NOT NULL,
    source_url       VARCHAR NOT NULL,
    source_title     VARCHAR,
    source_date      DATE,
    captured_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    evidence_text    VARCHAR,
    evidence_kind    VARCHAR,             -- keyword_hit | filing_metric | contract | project_status | macro_metric | holdings_seed
    metric_json      VARCHAR,
    confidence_score DOUBLE,
    expires_at       DATE,
    reviewer_note    VARCHAR
);

CREATE TABLE IF NOT EXISTS ai_theme_company_tags (
    theme              VARCHAR NOT NULL,
    market             VARCHAR NOT NULL,
    symbol             VARCHAR NOT NULL,
    company_name       VARCHAR,
    theme_role         VARCHAR,
    ai_strength        VARCHAR,           -- 强 | 中 | 弱 | 无
    evidence_status    VARCHAR,           -- confirmed | candidate | stale | needs_review
    evidence_score     DOUBLE,
    source_count_a     INTEGER,
    source_count_b     INTEGER,
    source_count_c     INTEGER,
    latest_source_date DATE,
    rationale          VARCHAR,
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (theme, market, symbol)
);

CREATE TABLE IF NOT EXISTS ai_theme_topic_metrics (
    theme             VARCHAR NOT NULL,
    metric_date       DATE NOT NULL,
    metric_name       VARCHAR NOT NULL,
    metric_value      DOUBLE,
    metric_unit       VARCHAR,
    source_id         VARCHAR NOT NULL,
    source_url        VARCHAR,
    captured_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (theme, metric_date, metric_name, source_id)
);

-- 主题 ↔ 数据源 多对多映射（一个 sec_edgar_api 同时服务"水冷"和"AI 数据"）
-- 用于 dashboard 按主题聚合"该主题有几个 A/B 类源，几个 ok / degraded"
CREATE TABLE IF NOT EXISTS ai_theme_source_mapping (
    theme       VARCHAR NOT NULL,    -- liquid_cooling | rare_earths | uranium | smr | ai_data
    source_id   VARCHAR NOT NULL,    -- 引用 ai_theme_evidence_sources.source_id
    note        VARCHAR,             -- 在该主题下这个源具体用途
    PRIMARY KEY (theme, source_id)
);

-- 主题 ↔ chain 多对多映射（粗-细两套分类的桥）
-- chain_metadata 是粗分类（"数据中心电力"包了电力+液冷），5 主题是细分类
-- 通过这张表，主题卡能拉到该主题关联 chain 下的 picks 数
CREATE TABLE IF NOT EXISTS ai_theme_chain_mapping (
    theme       VARCHAR NOT NULL,
    chain       VARCHAR NOT NULL,    -- 引用 chain_metadata.chain 的值
    relevance   VARCHAR,             -- direct | partial | indirect
    note        VARCHAR,
    PRIMARY KEY (theme, chain)
);

-- ETF 驱动的主题发现 — 抄市场共识，避免 hardcode 主题清单
-- 每个主题 ETF 持仓 Top N 就是"市场公认这个主题里值得关注的标的"
-- 见 docs/V2/AI主题雷达_产品定位.md（ETF 数据源补充）
CREATE TABLE IF NOT EXISTS ai_theme_etf_universe (
    etf_ticker      VARCHAR PRIMARY KEY,
    etf_name        VARCHAR NOT NULL,
    issuer          VARCHAR NOT NULL,    -- Global X | VanEck | iShares | Range
    theme_label     VARCHAR NOT NULL,    -- 机器人 + AI | 稀土 | 铀 | 半导体 | ...
    theme_id        VARCHAR,             -- 关联到 ai_theme_evidence_sources 的 theme（若可对应），否则 NULL
    holdings_url    VARCHAR NOT NULL,
    note            VARCHAR,
    active          BOOLEAN DEFAULT TRUE,
    last_fetched_at TIMESTAMP
);

-- ETF 持仓快照 — 每次 fetch 覆盖该 ETF 的全量持仓
-- rank 是 ETF 内排名（1=最大权重），weight 是百分比
CREATE TABLE IF NOT EXISTS ai_theme_etf_holdings (
    etf_ticker      VARCHAR NOT NULL,
    rank            INTEGER NOT NULL,
    raw_ticker      VARCHAR NOT NULL,    -- ETF 网站原始 ticker（如 "6954 JP", "ABBN SW", "300124 C2"）
    company_name    VARCHAR,
    weight          DOUBLE,              -- 百分比，0-100
    market_inferred VARCHAR,             -- 从 raw_ticker 推断的市场（JP/SW/US/CN/HK）
    universe_match  VARCHAR,             -- 若在我们 system_universe 命中，记 symbol；否则 NULL
    captured_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (etf_ticker, rank)
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


def _connect_with_lock_retry(path: str, read_only: bool, *, retry: bool,
                             attempts: int = 8, backoff_s: float = 0.75) -> duckdb.DuckDBPyConnection:
    """打开 DuckDB 连接;retry=True 时对"被写锁挡住"做 backoff 重试。

    DuckDB 单写者文件锁:写连接持独占锁期间,只读连接也打不开(Conflicting lock)。
    retry 仅用于 force_read_only 的独立只读 CLI/cron 进程 —— 等正在写库的步骤释放锁。
    非锁错误立刻抛;API/写路径 retry=False,绝不阻塞。
    """
    if not retry:
        return duckdb.connect(path, read_only=read_only)
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return duckdb.connect(path, read_only=read_only)
        except Exception as exc:  # duckdb.IOException 等;按消息判定是否锁冲突
            if "lock" not in str(exc).lower():
                raise  # 非锁错误不重试
            last_exc = exc
            if i < attempts - 1:
                time.sleep(backoff_s)
    raise RuntimeError(
        f"DuckDB 被写锁挡住,{attempts} 次只读重试(每次 {backoff_s}s)仍失败 —— "
        f"很可能有写库步骤正在跑,请错开并发或稍后重试。原始错误: {last_exc}"
    )


def get_db(path: str = DB_PATH, *, read_only: bool = False,
           force_read_only: bool = False,
           ensure_schema: bool | None = None) -> duckdb.DuckDBPyConnection:
    """打开 DuckDB 连接并保证用户态共享表存在。

    2026-05-21 V1 cutover：删除原 4 个 UPDATE picks legacy 数据修复 SQL（picks 表已删）。
    V2 表 schema 由 init_stock_db_v2.py 负责。

    2026-05-22 多线程兼容修复:
      DuckDB 不允许同一进程内 read_only=True 与 read_only=False 两种 conn 同时存在
      (报 "Can't open a connection ... different configuration")。API 进程是多线程,
      不同 endpoint thread 拿不同 mode 会撞上,产生 HTTP 500。
      → 默认调用方传 read_only=True 仍强制用 False 打开 connection（兼容 API）；
        read_only 参数保留作为是否跳过 SCHEMA_SQL 的提示。

    2026-06-01 force_read_only:
      独立 cron job（审计、报表、只读分析脚本）跑独立进程，不与 API 共享 connection pool，
      可以传 force_read_only=True 真正以 DuckDB 只读模式打开。
      使用场景：ai_theme_coverage_audit / aggregate_theme_tags / 各类 *_check / *_gate 等纯只读 job。
      ⚠️ 不要在 API/FastAPI/server 进程里传 force_read_only=True，会跟 write conn 撞 mode。

      ⚠️ 锁的真相(2026-06-01 复现修正)：DuckDB 是单写者文件锁 —— 某进程持写连接(独占锁)时,
         别的进程**连只读都打不开**(报 "Could not set lock ... Conflicting lock is held")。
         所以 force_read_only **并不能**绕开正在写库的进程。为此在 force_read_only 路径上加了
         锁感知 retry/backoff：撞上写锁就等几秒重试,让 nightly 写库步骤先释放锁。
         (API/写路径不 retry —— 绝不在请求线程里阻塞。)
    """
    actual_read_only = bool(force_read_only)
    conn = _connect_with_lock_retry(path, actual_read_only, retry=force_read_only)
    if ensure_schema is None:
        ensure_schema = not (read_only or force_read_only)
    if ensure_schema:
        conn.execute(SCHEMA_SQL)
        _ensure_manual_watchlist_columns(conn)
    return conn


def _ensure_manual_watchlist_columns(conn: duckdb.DuckDBPyConnection) -> None:
    """老 DB 兼容：manual_watchlist 的 industry/business 字段升级 ALTER。

    新 DB 由 init_stock_db_v2.py 建表时直接含这两列；老 DB 没有。
    表本身若不存在（test 临时 DB / 没跑 init_stock_db_v2），静默跳过——
    后续 init_stock_db_v2 / API 写入会自然建表。
    """
    for stmt in (
        "ALTER TABLE manual_watchlist ADD COLUMN IF NOT EXISTS industry VARCHAR",
        "ALTER TABLE manual_watchlist ADD COLUMN IF NOT EXISTS business VARCHAR",
    ):
        try:
            conn.execute(stmt)
        except duckdb.CatalogException:
            return  # 表不存在 → 老 DB 兼容路径 N/A，直接跳过


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
    if isinstance(v, date):
        return datetime.combine(v, datetime.min.time())
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


_LEGACY_FLAG_REPR_RE = None  # 延迟编译,见 _sanitize_legacy_flag_repr


def _sanitize_legacy_flag_repr(text):
    """旧版 enrich 曾把 V2 risk_flag dict 直接 str() 写进 source_raw_snapshots 的
    risks 字段;快照是审计表不改写历史,读取端把 repr 翻译回 message。"""
    if not text or "{'code':" not in str(text):
        return text
    global _LEGACY_FLAG_REPR_RE
    if _LEGACY_FLAG_REPR_RE is None:
        import re as _re
        _LEGACY_FLAG_REPR_RE = _re.compile(
            r"\{'code':\s*'[^']*',\s*'severity':\s*'[^']*',\s*'message':\s*'([^}]*?)'\}"
        )
    return _LEGACY_FLAG_REPR_RE.sub(r"\1", str(text))


def fetch_research_records_v2(*, conn: duckdb.DuckDBPyConnection | None = None) -> list[dict]:
    """V2 路径：从 system_universe + price_daily + 最新 recommendation_picks 拼出
    给「个股研究 / 产业链地图 / 买前审查」的展示用 records。

    与 fetch_records_view 形状对齐（同字段名），但纯 V2 表，没有任何 V1 watchlist/prices 依赖。
    缺失的 V1 主观字段（business / ai_logic / conclusion / risks / peers / rhythm /
    chain / chain_tier / chain_role / layman_intro 等）填 None — 前端做空值处理或隐藏。

    2026-06-01 评审 #2：own=True 时用 force_read_only=True 真只读，避免 dashboard
    build 因 DuckDB 写锁失败覆盖旧 HTML。
    """
    own = conn is None
    if own:
        conn = get_db(force_read_only=True)
    rows = conn.execute("""
        WITH latest_price AS (
            SELECT * FROM price_daily
            QUALIFY ROW_NUMBER() OVER (PARTITION BY market, symbol ORDER BY trade_date DESC, fetched_at DESC) = 1
        ),
        latest_metrics AS (
            -- 盘中/小时级行只有收盘价,动量/估值列为空;展示口径回退到最近一个有数的行
            -- (与打分端 *_REUSED_RECENT_V2_SNAPSHOT 的回退语义一致)
            SELECT * FROM price_daily
            WHERE one_month_pct IS NOT NULL OR ytd_pct IS NOT NULL
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
            COALESCE(lp.market_cap, lm.market_cap)       AS yf_market_cap,
            COALESCE(lp.forward_pe, lm.forward_pe)       AS forward_pe,
            COALESCE(lp.peg_ratio, lm.peg_ratio)         AS peg,
            NULL              AS earnings_growth_pct,
            COALESCE(lp.ytd_pct, lm.ytd_pct)             AS ytd_pct,
            COALESCE(lp.one_year_pct, lm.one_year_pct)   AS one_year_pct,
            COALESCE(lp.one_month_pct, lm.one_month_pct) AS one_month_pct,
            COALESCE(lp.one_week_pct, lm.one_week_pct)   AS one_week_pct,
            lp.trade_date     AS price_date,
            lp.fetched_at     AS price_fetched_at,
            COALESCE(ls.fetched_at, u.last_seen_at) AS analysis_updated_at,
            lpk.rating        AS pick_rating,
            lpk.signal        AS pick_signal,
            lpk.total_score   AS pick_total_score,
            lpk.factor_scores_json AS pick_factor_scores_json
        FROM system_universe u
        LEFT JOIN latest_price lp ON lp.market = u.market AND lp.symbol = u.symbol
        LEFT JOIN latest_metrics lm ON lm.market = u.market AND lm.symbol = u.symbol
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
    for r in out:
        if r.get("risks"):
            r["risks"] = _sanitize_legacy_flag_repr(r["risks"])
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
    # 2026-06-02: chain_metadata 同一 symbol 可能存多行（manual_override 用 market='美股'，
    # rule_classify 用 market='US'，(market,symbol) PK 不互相覆盖）→ 直接 LEFT JOIN 会一对多
    # 放大成重复自选股行。先按 symbol 去重再 JOIN：manual_override 优先，再取最新 classified_at。
    chain_join = (
        "LEFT JOIN ("
        "  SELECT symbol, chain, chain_tier, chain_role, layman_intro,"
        "         ROW_NUMBER() OVER ("
        "           PARTITION BY symbol"
        "           ORDER BY CASE WHEN source = 'manual_override' THEN 0 ELSE 1 END,"
        "                    classified_at DESC"
        "         ) AS _rn"
        "  FROM chain_metadata"
        ") cm ON cm.symbol = mw.symbol AND cm._rn = 1"
    ) if has_chain else ""
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
        # 2026-06-02: market 一律从 ticker 后缀推断成 ISO(US/HK/CN)，与 rule_classify 同源。
        # 不能用前端传的展示值('美股'/'港股'/'A股·沪交所')，否则会和 rule_classify 的 ISO 行
        # 按 (market,symbol) PK 撞不上 → 同一只票留两行 → 自选股重复(见 fetch_manual_watchlist
        # 去重注释)。symbol 带后缀全局唯一，后缀即市场的单一可信来源。
        market = _infer_market_from_ticker(symbol)
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
        ),
        latest_metrics AS (
            -- 盘中/小时级行只有收盘价;展示口径回退到最近一个有数的行
            SELECT * FROM price_daily
            WHERE one_month_pct IS NOT NULL OR ytd_pct IS NOT NULL
            QUALIFY ROW_NUMBER() OVER (PARTITION BY market, symbol ORDER BY trade_date DESC, fetched_at DESC) = 1
        )
        SELECT
            w.symbol AS code, COALESCE(NULLIF(u.name, w.symbol), w.name, w.symbol) AS name,
            w.market, u.industry, u.theme,
            lp.close AS latest_price,
            COALESCE(lp.ytd_pct, lm.ytd_pct) AS ytd_pct,
            COALESCE(lp.one_year_pct, lm.one_year_pct) AS one_year_pct,
            COALESCE(lp.one_month_pct, lm.one_month_pct) AS one_month_pct,
            COALESCE(lp.one_week_pct, lm.one_week_pct) AS one_week_pct,
            COALESCE(lp.forward_pe, lm.forward_pe) AS forward_pe,
            COALESCE(lp.peg_ratio, lm.peg_ratio) AS peg, NULL AS earnings_growth_pct,
            lp.currency, COALESCE(lp.market_cap, lm.market_cap) AS market_cap
        FROM manual_watchlist w
        LEFT JOIN system_universe u ON u.symbol = w.symbol
        LEFT JOIN latest_price lp ON lp.symbol = w.symbol
        LEFT JOIN latest_metrics lm ON lm.symbol = w.symbol
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
    universe_scope, run_id, run_date, strategy_version, model_version。
    新字段存在时也会返回 eligibility/action/evidence_status/primary_layer，
    旧库没有这些列时返回 None，保持历史兼容。
    无最新 run 时返回 []。
    """
    own = conn is None
    if own:
        conn = get_db(read_only=True)
    row = conn.execute(
        """
        SELECT run_id, run_date, strategy_version, model_version FROM recommendation_runs
        WHERE universe_scope = ? AND status = 'generated'
        ORDER BY generated_at DESC LIMIT 1
        """,
        [universe_scope],
    ).fetchone()
    if not row:
        if own:
            conn.close()
        return []
    run_id, run_date, strategy_version, model_version = row
    base_cols = [
        "market", "symbol", "name", "rank", "rating", "signal", "total_score",
        "factor_scores_json", "entry_price", "universe_scope",
    ]
    optional_cols = [
        "eligibility", "action", "evidence_status", "eligibility_migration_status",
        "primary_layer", "secondary_layers_json", "ai_relevance_level",
        "classification_version", "risk_flags_json",
    ]
    select_cols = base_cols + [
        col if _table_has_column(conn, "recommendation_picks", col) else f"NULL AS {col}"
        for col in optional_cols
    ]
    rows = conn.execute(
        f"""
        SELECT {', '.join(select_cols)}
        FROM recommendation_picks
        WHERE run_id = ?
        ORDER BY rank
        """,
        [run_id],
    ).fetchall()
    import json as _json
    out = []
    cols = base_cols + optional_cols
    for r in rows:
        rec = dict(zip(cols, r))
        try:
            fs = _json.loads(rec.get("factor_scores_json")) if rec.get("factor_scores_json") else {}
        except Exception:
            fs = {}
        try:
            risks = _json.loads(rec.get("risk_flags_json")) if rec.get("risk_flags_json") else []
        except Exception:
            risks = []
        out.append({
            "market": rec.get("market"),
            "symbol": rec.get("symbol"),
            "code": rec.get("symbol"),
            "name": rec.get("name"),
            "rank": rec.get("rank"),
            "rating": rec.get("rating"),
            "signal": rec.get("signal"),
            "total_score": rec.get("total_score"),
            "factor_scores": fs,
            "risk_flags": risks,
            "entry_price": rec.get("entry_price"),
            "universe_scope": rec.get("universe_scope"),
            "eligibility": rec.get("eligibility"),
            "action": rec.get("action"),
            "evidence_status": rec.get("evidence_status"),
            "eligibility_migration_status": rec.get("eligibility_migration_status"),
            "primary_layer": rec.get("primary_layer"),
            "secondary_layers_json": rec.get("secondary_layers_json"),
            "ai_relevance_level": rec.get("ai_relevance_level"),
            "classification_version": rec.get("classification_version"),
            "run_id": run_id,
            "run_date": run_date,
            "strategy_version": strategy_version,
            "model_version": model_version,
        })
    if own:
        conn.close()
    return out


def fetch_recommendation_runs_between(
    start_date: date,
    end_date: date,
    *,
    universe_scope: str | list[str] | None = "system_tech_universe",
    conn: duckdb.DuckDBPyConnection | None = None,
) -> list[dict[str, Any]]:
    """按 run_date 区间读取 recommendation_runs（PIT 复盘用）。

    universe_scope:
        - str: 单 scope（向后兼容）
        - list[str]: 多 scope IN (...)
        - None: 不过滤 scope（取所有；周末复盘默认此项以兼容未来 HK/US 接入 PIT 表）
    """
    own = conn is None
    if own:
        conn = get_db(read_only=True)
    sql = """
        SELECT run_id, run_date, strategy_version, model_version, universe_scope,
               data_cutoff_at, generated_at, status, notes
        FROM recommendation_runs
        WHERE run_date >= ? AND run_date <= ?
          AND status = 'generated'
    """
    params: list[Any] = [start_date, end_date]
    if isinstance(universe_scope, str):
        sql += " AND universe_scope = ?"
        params.append(universe_scope)
    elif isinstance(universe_scope, (list, tuple, set)) and universe_scope:
        placeholders = ",".join(["?"] * len(universe_scope))
        sql += f" AND universe_scope IN ({placeholders})"
        params.extend(list(universe_scope))
    # universe_scope is None → no scope filter
    sql += " ORDER BY run_date ASC, generated_at ASC"
    rows = conn.execute(sql, params).fetchall()
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
    per_market_top_n: int | None = None,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> list[dict[str, Any]]:
    """读取某次 run 的 recommendation_picks（按 rank 排序）。

    top_n: 全局裁切。注意 build_v2_recommendations 写入 rank 时按 market 分段
        （CN=1..20、HK=21..40、US=41..60），所以全局 LIMIT 10 会只剩 CN。
    per_market_top_n: 每个市场各取前 N（用 DuckDB QUALIFY + ROW_NUMBER 实现）。
        给 weekly_self_review / 多市场复盘场景用，避免 global rank 误判。
    """
    own = conn is None
    if own:
        conn = get_db(read_only=True)
    params: list[Any] = [run_id]
    base_cols = [
        "market", "symbol", "name", "rank", "rating", "signal", "total_score",
        "factor_scores_json", "entry_price", "universe_scope", "source_origin",
    ]
    optional_cols = [
        "eligibility", "action", "evidence_status", "eligibility_migration_status",
        "primary_layer", "secondary_layers_json", "ai_relevance_level",
        "classification_version",
    ]
    select_cols = base_cols + [
        col if _table_has_column(conn, "recommendation_picks", col) else f"NULL AS {col}"
        for col in optional_cols
    ]
    if per_market_top_n is not None and per_market_top_n > 0:
        sql = f"""
            SELECT {', '.join(select_cols)}
            FROM recommendation_picks
            WHERE run_id = ?
            QUALIFY ROW_NUMBER() OVER (PARTITION BY market ORDER BY rank) <= ?
            ORDER BY rank
        """
        params.append(int(per_market_top_n))
    else:
        sql = f"""
            SELECT {', '.join(select_cols)}
            FROM recommendation_picks
            WHERE run_id = ?
            ORDER BY rank
        """
        if top_n is not None and top_n > 0:
            sql += " LIMIT ?"
            params.append(int(top_n))
    rows = conn.execute(sql, params).fetchall()
    import json as _json
    out = []
    cols = base_cols + optional_cols
    for r in rows:
        rec = dict(zip(cols, r))
        try:
            fs = _json.loads(rec.get("factor_scores_json")) if rec.get("factor_scores_json") else {}
        except Exception:
            fs = {}
        out.append({
            "market": rec.get("market"),
            "symbol": rec.get("symbol"),
            "code": rec.get("symbol"),
            "name": rec.get("name"),
            "rank": rec.get("rank"),
            "rating": rec.get("rating"),
            "signal": rec.get("signal"),
            "total_score": rec.get("total_score"),
            "factor_scores": fs,
            "entry_price": rec.get("entry_price"),
            "universe_scope": rec.get("universe_scope"),
            "source_origin": rec.get("source_origin"),
            "eligibility": rec.get("eligibility"),
            "action": rec.get("action"),
            "evidence_status": rec.get("evidence_status"),
            "eligibility_migration_status": rec.get("eligibility_migration_status"),
            "primary_layer": rec.get("primary_layer"),
            "secondary_layers_json": rec.get("secondary_layers_json"),
            "ai_relevance_level": rec.get("ai_relevance_level"),
            "classification_version": rec.get("classification_version"),
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
# 账本 v2 聚合列（rebuild 回写）。读路径在 FULL_COLS 之外额外暴露这些，供下游和前端用显式字段。
REAL_HOLDINGS_AGG_COLS = [
    "total_buy_shares", "remaining_shares", "avg_cost_local_per_share",
    "avg_cost_rmb_per_share", "remaining_cost_rmb", "position_epoch",
    "first_entry_date", "last_trade_date", "close_status",
]
REAL_HOLDINGS_READ_COLS = REAL_HOLDINGS_FULL_COLS + REAL_HOLDINGS_AGG_COLS
REAL_HOLDING_TRADES_COLS = [
    "account", "market", "symbol", "name", "side", "trade_price", "quantity",
    "trade_date", "executed_at", "order_in_day", "currency", "fx_rate", "fx_as_of",
    "fx_source", "gross_amount_rmb", "fee_amount", "fee_currency", "fee_fx_rate",
    "fee_rmb", "client_request_id", "position_epoch", "status",
    "realized_pnl_rmb", "realized_pnl_pct", "cost_basis_rmb", "cost_basis_local",
    "holding_days", "notes", "source",
]
REAL_HOLDING_TRADES_FULL_COLS = (
    ["trade_id"] + REAL_HOLDING_TRADES_COLS + ["voided_at", "void_reason", "created_at", "updated_at"]
)
_SHARE_EPS = 1e-9
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
    "review_run_id", "holding_id", "account", "market", "symbol", "asset_class", "treatment_class",
    "score", "coverage_score", "rating", "action_label", "action_priority",
    "current_price", "current_currency", "current_value_rmb", "cost_rmb_locked",
    "pnl_rmb", "pnl_pct", "current_weight", "target_weight", "weight_gap_pt",
    "reasons_json", "risk_flags_json", "data_flags_json",
    "price_trade_date", "prev_close", "prev_trade_date", "day_change_basis",
    "day_change_rmb", "day_change_pct", "price_is_prior_session",
    "size_advisory_json",
    "industry_heat_json",
    "discipline_json",
]
REAL_HOLDING_REVIEW_ITEM_FULL_COLS = REAL_HOLDING_REVIEW_ITEM_COLS + ["created_at"]
REAL_HOLDING_DISCIPLINE_PLAN_COLS = [
    "plan_id", "holding_id", "account", "market", "symbol", "plan_type", "source_type",
    "validation_status", "status", "cost_basis_price", "shares_snapshot", "thesis",
    "invalidation_note", "notes", "confirmed_at",
]
REAL_HOLDING_DISCIPLINE_PLAN_FULL_COLS = REAL_HOLDING_DISCIPLINE_PLAN_COLS + ["created_at", "updated_at"]
REAL_HOLDING_DISCIPLINE_TRIGGER_COLS = [
    "trigger_id", "plan_id", "trigger_type", "comparator", "price_min", "price_max",
    "severity", "priority", "action_label", "suggested_size_text", "rationale",
    "auto_trade_allowed",
]
REAL_HOLDING_DISCIPLINE_TRIGGER_FULL_COLS = REAL_HOLDING_DISCIPLINE_TRIGGER_COLS + ["created_at", "updated_at"]
REAL_HOLDING_DISCIPLINE_EVENT_COLS = [
    "event_id", "plan_id", "trigger_id", "holding_id", "account", "market", "symbol",
    "current_price", "price_trade_date", "severity", "action_label", "message",
    "evaluation_json", "user_ack_status", "user_ack_at",
]
REAL_HOLDING_DISCIPLINE_EVENT_FULL_COLS = REAL_HOLDING_DISCIPLINE_EVENT_COLS + ["triggered_at", "created_at"]


class DisciplinePlanError(ValueError):
    """Base error for real-holding discipline plans."""


class DisciplinePlanConflict(DisciplinePlanError):
    """Raised when a holding already has an active discipline plan."""


class DisciplinePlanNotFound(DisciplinePlanError):
    """Raised when a requested holding or plan cannot be found."""


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


def _table_has_column(conn: duckdb.DuckDBPyConnection, table: str, column: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    except Exception:
        return False
    return any(str(r[1]).lower() == column.lower() for r in rows)


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
                "CNY": 1.0, "USD": 6.7645, "HKD": 0.8627, "JPY": 0.0423,
                "KRW": 0.0045, "TWD": 0.2151, "EUR": 7.8698, "AUD": 4.8450, "GBP": 9.1041,
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
        f"SELECT {','.join(REAL_HOLDINGS_READ_COLS)} "
        "FROM real_holdings ORDER BY entry_date DESC NULLS LAST, symbol"
    ).fetchall()
    out = [_rowdict(REAL_HOLDINGS_READ_COLS, r) for r in rows]
    if own:
        conn.close()
    return out


def _recent_duplicate_real_holding_id(
    vals: list,
    *,
    conn: duckdb.DuckDBPyConnection,
    window_minutes: int = 10,
) -> int | None:
    """Return an id for a recent identical lot submission, if one exists.

    This is deliberately time-windowed: it catches double-click/retry duplicate
    POSTs without blocking a user from intentionally adding a separate identical
    lot later.
    """
    account, market, symbol, _name, entry_price, shares, entry_date, currency, *_ = vals
    minutes = max(1, min(int(window_minutes or 10), 60))
    row = conn.execute(
        f"""
        SELECT id
        FROM real_holdings
        WHERE account = ?
          AND market = ?
          AND UPPER(symbol) = UPPER(?)
          AND entry_date IS NOT DISTINCT FROM ?
          AND UPPER(COALESCE(currency, '')) = UPPER(?)
          AND ABS(entry_price - ?) < 0.000001
          AND ABS(shares - ?) < 0.000001
          AND created_at >= CURRENT_TIMESTAMP - INTERVAL '{minutes} minutes'
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        """,
        [account, market, symbol, entry_date, currency or "", entry_price, shares],
    ).fetchone()
    return int(row[0]) if row else None


def insert_real_holding_result(
    item: Mapping[str, Any],
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
    dedupe_window_minutes: int = 10,
) -> dict[str, Any]:
    own = conn is None
    if own:
        conn = get_db()
    vals = _normalize_real_holding(item)
    existing_id = _recent_duplicate_real_holding_id(
        vals,
        conn=conn,
        window_minutes=dedupe_window_minutes,
    )
    if existing_id is not None:
        if own:
            conn.close()
        return {"id": existing_id, "created": False, "deduped": True}
    conn.execute(
        f"INSERT INTO real_holdings ({','.join(REAL_HOLDINGS_COLS)}, updated_at) "
        f"VALUES ({','.join(['?'] * len(REAL_HOLDINGS_COLS))}, CURRENT_TIMESTAMP)",
        vals,
    )
    new_id = int(conn.execute("SELECT currval('real_holdings_id_seq')").fetchone()[0])
    canonical_id = _recent_duplicate_real_holding_id(
        vals,
        conn=conn,
        window_minutes=dedupe_window_minutes,
    )
    if canonical_id is not None and canonical_id != new_id:
        # Covers concurrent double-submit races: two requests may both pass the
        # pre-insert check, but only the earliest lot should survive.
        conn.execute("DELETE FROM real_holdings WHERE id = ?", [new_id])
        if own:
            conn.close()
        return {
            "id": canonical_id,
            "created": False,
            "deduped": True,
            "dedupe_deleted_id": new_id,
        }
    if own:
        conn.close()
    return {"id": new_id, "created": True, "deduped": False}


def insert_real_holding(item: Mapping[str, Any], *, conn: duckdb.DuckDBPyConnection | None = None) -> int:
    own = conn is None
    if own:
        conn = get_db()
    result = insert_real_holding_result(item, conn=conn)
    new_id = int(result["id"])
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


def rename_real_holding_symbol(holding_id: int, new_symbol: str, *, conn=None) -> dict[str, Any]:
    """改 ticker（typo 修正）：把该持仓的 symbol 连同它的交易流水一起改名，再 rebuild。

    拒绝改成同 account+market 下已存在的另一只持仓的代码（避免意外合并）。
    """
    own = conn is None
    if own:
        conn = get_db()
    try:
        h = fetch_real_holding_by_id(holding_id, conn=conn)
        if not h:
            raise LedgerError(f"holding not found: {holding_id}")
        account, market = h.get("account") or "default", h.get("market")
        old_sym = (h.get("symbol") or "")
        new_sym = (new_symbol or "").strip().upper()
        if not new_sym or new_sym == old_sym.upper():
            return {"renamed": False}
        clash = conn.execute(
            "SELECT 1 FROM real_holdings WHERE account=? AND market=? AND UPPER(symbol)=? AND id<>? LIMIT 1",
            [account, market, new_sym, int(holding_id)],
        ).fetchone()
        if clash:
            raise LedgerError(f"已存在 {market}:{new_sym} 持仓，不能改成同代码（避免意外合并）")
        conn.execute(
            "UPDATE real_holding_trades SET symbol=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE account=? AND market=? AND UPPER(symbol)=UPPER(?)",
            [new_sym, account, market, old_sym],
        )
        conn.execute(
            "UPDATE real_holdings SET symbol=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [new_sym, int(holding_id)],
        )
        rebuild_real_holdings_from_trades(conn=conn, account=account, market=market, symbol=new_sym)
        return {"renamed": True, "from": old_sym, "to": new_sym}
    finally:
        if own:
            conn.close()


def update_real_holding_meta(holding_id: int, *, name=None, notes=None, conn=None) -> int:
    """只更新账本持仓的名称/备注；数量与成本由交易流水决定，不在此处改。"""
    own = conn is None
    if own:
        conn = get_db()
    try:
        sets, params = [], []
        if name is not None:
            sets.append("name=?"); params.append(str(name).strip() or None)
        if notes is not None:
            sets.append("notes=?"); params.append(notes)
        if not sets:
            return 0
        params.append(int(holding_id))
        n = conn.execute(
            f"UPDATE real_holdings SET {', '.join(sets)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            params,
        ).fetchall()
        exists = conn.execute("SELECT 1 FROM real_holdings WHERE id=?", [int(holding_id)]).fetchone()
        return 1 if exists else 0
    finally:
        if own:
            conn.close()


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


def fetch_real_holding_by_id(
    holding_id: int,
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, Any] | None:
    own = conn is None
    if own:
        conn = get_db()
    row = conn.execute(
        f"SELECT {','.join(REAL_HOLDINGS_READ_COLS)} FROM real_holdings WHERE id = ?",
        [int(holding_id)],
    ).fetchone()
    out = _rowdict(REAL_HOLDINGS_READ_COLS, row) if row else None
    if own:
        conn.close()
    return out


# ---------------------------------------------------------------------------
# 账本 v2：交易流水（real_holding_trades）+ rebuild 聚合
# ---------------------------------------------------------------------------


class LedgerError(ValueError):
    """账本写入/重建错误基类。"""


class LedgerConflict(LedgerError):
    """交易导致任一时点剩余股数为负，或卖出无对应持仓轮次。"""


def _normalize_trade(item: Mapping[str, Any], side: str) -> dict[str, Any]:
    """把买入/卖出 payload 规整为 real_holding_trades 一行（不含派生 PnL）。

    汇率口径与 _normalize_real_holding 同源：成交日锁定汇率。买入手续费在
    rebuild 时计入成本基；这里只把 fee 折成 RMB 存好。
    """
    side = (side or "").strip().lower()
    if side not in ("buy", "sell"):
        raise LedgerError(f"trade side must be buy|sell, got {side!r}")
    symbol = item.get("symbol") or item.get("code")
    if not symbol:
        raise LedgerError("trade requires symbol (or legacy code)")
    market = item.get("market") or _infer_market_from_ticker(symbol)
    currency = (item.get("currency") or "").strip().upper() or _infer_currency_from_ticker(symbol)
    trade_price = float(item.get("trade_price") if item.get("trade_price") is not None else item.get("entry_price") or item.get("price") or 0)
    quantity = float(item.get("quantity") if item.get("quantity") is not None else item.get("shares") or 0)
    if trade_price <= 0:
        raise LedgerError("trade_price must be > 0")
    if quantity <= 0:
        raise LedgerError("quantity must be > 0")
    trade_date = _to_date(item.get("trade_date") or item.get("entry_date") or item.get("date"))
    if trade_date is None:
        raise LedgerError("trade_date is required")
    if trade_date > date.today():
        raise LedgerError("trade_date cannot be in the future")
    # 成交汇率：复用真实持仓的锁定 FX 解析（manual > fx_rates > static fallback）。
    fx_rate, fx_as_of, fx_source, _cost = _resolve_real_holding_entry_fx(
        item, currency=currency, entry_date=trade_date, entry_price=trade_price, shares=quantity,
    )
    gross_amount_rmb = trade_price * quantity * fx_rate
    fee_amount = item.get("fee_amount")
    fee_amount = float(fee_amount) if fee_amount not in (None, "") else 0.0
    if fee_amount < 0:
        raise LedgerError("fee_amount must be >= 0")
    fee_currency = (item.get("fee_currency") or "").strip().upper() or currency
    if fee_amount == 0:
        fee_fx_rate, fee_rmb = (fx_rate if fee_currency == currency else None), 0.0
    elif fee_currency == currency:
        fee_fx_rate, fee_rmb = fx_rate, fee_amount * fx_rate
    else:
        ffx = _as_float_or_none(item.get("fee_fx_rate"))
        if ffx is None:
            _ffx, _a, _s, _c = _resolve_real_holding_entry_fx(
                {"currency": fee_currency}, currency=fee_currency, entry_date=trade_date,
                entry_price=fee_amount, shares=1,
            )
            ffx = _ffx
        fee_fx_rate, fee_rmb = ffx, fee_amount * ffx
    name = item.get("name")
    name = (str(name).strip() or None) if name is not None else None
    executed_at = item.get("executed_at")
    order_in_day = item.get("order_in_day")
    order_in_day = int(order_in_day) if order_in_day not in (None, "") else None
    return {
        "account": item.get("account") or "default",
        "market": market,
        "symbol": symbol,
        "name": name,
        "side": side,
        "trade_price": trade_price,
        "quantity": quantity,
        "trade_date": trade_date,
        "executed_at": executed_at,
        "order_in_day": order_in_day,
        "currency": currency,
        "fx_rate": fx_rate,
        "fx_as_of": fx_as_of,
        "fx_source": fx_source,
        "gross_amount_rmb": gross_amount_rmb,
        "fee_amount": fee_amount,
        "fee_currency": fee_currency,
        "fee_fx_rate": fee_fx_rate,
        "fee_rmb": fee_rmb,
        "client_request_id": _clean_text(item.get("client_request_id") or item.get("idempotency_key")),
        "position_epoch": None,
        "status": "active",
        "realized_pnl_rmb": None,
        "realized_pnl_pct": None,
        "cost_basis_rmb": None,
        "cost_basis_local": None,
        "holding_days": None,
        "notes": item.get("notes"),
        "source": item.get("source") or "manual",
    }


def _existing_trade_by_request_id(account: str, client_request_id: str, *, conn) -> dict | None:
    if not client_request_id:
        return None
    row = conn.execute(
        f"SELECT {','.join(REAL_HOLDING_TRADES_FULL_COLS)} FROM real_holding_trades "
        "WHERE account = ? AND client_request_id = ? LIMIT 1",
        [account, client_request_id],
    ).fetchone()
    return _rowdict(REAL_HOLDING_TRADES_FULL_COLS, row) if row else None


def _insert_trade(item: Mapping[str, Any], side: str, *, conn) -> dict[str, Any]:
    vals = _normalize_trade(item, side)
    # 幂等：同 account 同 client_request_id 已有交易则直接返回，不重复记账。
    existing = _existing_trade_by_request_id(vals["account"], vals["client_request_id"], conn=conn)
    if existing is not None:
        holding = _current_holding_id(vals["account"], vals["market"], vals["symbol"], conn=conn)
        return {"trade_id": int(existing["trade_id"]), "holding_id": holding,
                "created": False, "deduped": True}
    insert_cols = REAL_HOLDING_TRADES_COLS
    conn.execute(
        f"INSERT INTO real_holding_trades ({','.join(insert_cols)}, updated_at) "
        f"VALUES ({','.join(['?'] * len(insert_cols))}, CURRENT_TIMESTAMP)",
        [vals[c] for c in insert_cols],
    )
    trade_id = int(conn.execute("SELECT currval('real_holding_trades_id_seq')").fetchone()[0])
    try:
        rebuild_real_holdings_from_trades(
            conn=conn, account=vals["account"], market=vals["market"], symbol=vals["symbol"],
        )
    except LedgerConflict:
        # rebuild 是权威校验：冲突时回滚刚插入的这一笔再抛出。
        conn.execute("DELETE FROM real_holding_trades WHERE trade_id = ?", [trade_id])
        raise
    holding = _current_holding_id(vals["account"], vals["market"], vals["symbol"], conn=conn)
    return {"trade_id": trade_id, "holding_id": holding, "created": True, "deduped": False}


def insert_real_holding_trade_raw(item: Mapping[str, Any], side: str, *, conn) -> dict[str, Any]:
    """插入一笔 trade 但**不** rebuild（批量/迁移用）。带 client_request_id 幂等。

    调用方负责在批量结束后调用 rebuild_real_holdings_from_trades()。
    """
    vals = _normalize_trade(item, side)
    existing = _existing_trade_by_request_id(vals["account"], vals["client_request_id"], conn=conn)
    if existing is not None:
        return {"trade_id": int(existing["trade_id"]), "created": False, "deduped": True}
    conn.execute(
        f"INSERT INTO real_holding_trades ({','.join(REAL_HOLDING_TRADES_COLS)}, updated_at) "
        f"VALUES ({','.join(['?'] * len(REAL_HOLDING_TRADES_COLS))}, CURRENT_TIMESTAMP)",
        [vals[c] for c in REAL_HOLDING_TRADES_COLS],
    )
    trade_id = int(conn.execute("SELECT currval('real_holding_trades_id_seq')").fetchone()[0])
    return {"trade_id": trade_id, "created": True, "deduped": False}


def insert_real_holding_buy(item: Mapping[str, Any], *, conn=None) -> dict[str, Any]:
    """记录一笔买入/加仓成交，并 rebuild 当前聚合持仓。"""
    own = conn is None
    if own:
        conn = get_db()
    try:
        return _insert_trade(item, "buy", conn=conn)
    finally:
        if own:
            conn.close()


def insert_real_holding_sell(item: Mapping[str, Any], *, conn=None) -> dict[str, Any]:
    """记录一笔卖出/平仓成交，并 rebuild 当前聚合持仓。"""
    own = conn is None
    if own:
        conn = get_db()
    try:
        return _insert_trade(item, "sell", conn=conn)
    finally:
        if own:
            conn.close()


def void_latest_real_holding_trade(
    account: str, market: str, symbol: str, *, reason: str | None = None, conn=None,
) -> dict[str, Any]:
    """P0 纠错：撤销该标的当前轮次最近一笔 active trade（软删 status=voided）后 rebuild。"""
    own = conn is None
    if own:
        conn = get_db()
    try:
        account = account or "default"
        rows = conn.execute(
            f"SELECT {','.join(REAL_HOLDING_TRADES_FULL_COLS)} FROM real_holding_trades "
            "WHERE account = ? AND market = ? AND UPPER(symbol) = UPPER(?) AND status = 'active'",
            [account, market, symbol],
        ).fetchall()
        trades = [_rowdict(REAL_HOLDING_TRADES_FULL_COLS, r) for r in rows]
        if not trades:
            raise LedgerError("no active trade to void for this holding")
        latest = sorted(trades, key=_trade_sort_key)[-1]
        conn.execute(
            "UPDATE real_holding_trades SET status='voided', voided_at=CURRENT_TIMESTAMP, "
            "void_reason=?, updated_at=CURRENT_TIMESTAMP WHERE trade_id = ?",
            [reason or "user_void", int(latest["trade_id"])],
        )
        rebuild_real_holdings_from_trades(conn=conn, account=account, market=market, symbol=symbol)
        return {"voided_trade_id": int(latest["trade_id"]),
                "holding_id": _current_holding_id(account, market, symbol, conn=conn)}
    finally:
        if own:
            conn.close()


class LedgerNotLatest(LedgerError):
    """P0 只允许撤销该标的当前轮次最近一笔 active trade。"""


def fetch_real_holding_trade_by_id(trade_id: int, *, conn=None) -> dict | None:
    own = conn is None
    if own:
        conn = get_db()
    try:
        row = conn.execute(
            f"SELECT {','.join(REAL_HOLDING_TRADES_FULL_COLS)} FROM real_holding_trades WHERE trade_id = ?",
            [int(trade_id)],
        ).fetchone()
        return _rowdict(REAL_HOLDING_TRADES_FULL_COLS, row) if row else None
    finally:
        if own:
            conn.close()


def void_real_holding_trade(trade_id: int, *, reason: str | None = None, conn=None) -> dict[str, Any]:
    """按 trade_id 撤销，但仅当它是该标的当前最近一笔 active trade，否则抛 LedgerNotLatest(→409)。"""
    own = conn is None
    if own:
        conn = get_db()
    try:
        t = fetch_real_holding_trade_by_id(trade_id, conn=conn)
        if not t:
            raise LedgerError(f"trade not found: {trade_id}")
        if t.get("status") != "active":
            raise LedgerError(f"trade {trade_id} is not active")
        rows = conn.execute(
            f"SELECT {','.join(REAL_HOLDING_TRADES_FULL_COLS)} FROM real_holding_trades "
            "WHERE account = ? AND market = ? AND symbol = ? AND status = 'active'",
            [t["account"], t["market"], t["symbol"]],
        ).fetchall()
        trades = [_rowdict(REAL_HOLDING_TRADES_FULL_COLS, r) for r in rows]
        latest = sorted(trades, key=_trade_sort_key)[-1]
        if int(latest["trade_id"]) != int(trade_id):
            raise LedgerNotLatest("P0 仅支持撤销该标的当前轮次的最近一笔交易")
        return void_latest_real_holding_trade(
            t["account"], t["market"], t["symbol"], reason=reason, conn=conn,
        )
    finally:
        if own:
            conn.close()


def correct_real_holding_buy_price(holding_id: int, new_price: float, *, conn=None) -> dict[str, Any]:
    """纠正录错的买入成本价：仅限「单一买入、尚未卖出」的干净记录。

    直接改那唯一一笔买入成交的 trade_price（并同步 gross_amount_rmb）后 rebuild，
    聚合行的 entry_price / cost_rmb_locked 由 rebuild 重算（单一来源，不在前端换算）。
    多笔买入 / 已部分卖出 → 抛 LedgerError(→409)：此时成本是加权平均，不能直接反推，
    应走持仓行的「加仓 / 卖出」或撤销具体交易后重录。
    """
    own = conn is None
    if own:
        conn = get_db()
    try:
        new_price = float(new_price)
        if not (new_price > 0):
            raise LedgerError("买入价必须是正数")
        h = fetch_real_holding_by_id(int(holding_id), conn=conn)
        if not h:
            raise LedgerError(f"持仓不存在: {holding_id}")
        acct = h.get("account") or "default"
        mkt, sym = h.get("market"), h.get("symbol")
        rows = conn.execute(
            f"SELECT {','.join(REAL_HOLDING_TRADES_FULL_COLS)} FROM real_holding_trades "
            "WHERE account = ? AND market = ? AND UPPER(symbol) = UPPER(?) AND status = 'active'",
            [acct, mkt, sym],
        ).fetchall()
        trades = [_rowdict(REAL_HOLDING_TRADES_FULL_COLS, r) for r in rows]
        buys = [t for t in trades if t["side"] == "buy"]
        sells = [t for t in trades if t["side"] == "sell"]
        if sells:
            raise LedgerError("该持仓已有卖出/平仓记录，成本价是加权平均，不能直接改；请撤销对应卖出或用「加仓/卖出」修正。")
        if len(buys) != 1:
            raise LedgerError(f"该持仓有 {len(buys)} 笔买入，成本价是加权平均，不能直接改；请用持仓行的「加仓/卖出」或撤销具体交易后重录。")
        buy = buys[0]
        tid = int(buy["trade_id"])
        old_price = float(buy["trade_price"])
        qty = float(buy["quantity"])
        fx = float(buy["fx_rate"] or 0)
        gross_rmb = new_price * qty * fx if fx else None
        conn.execute(
            "UPDATE real_holding_trades SET trade_price = ?, gross_amount_rmb = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE trade_id = ?",
            [new_price, gross_rmb, tid],
        )
        rebuild_real_holdings_from_trades(conn=conn, account=acct, market=mkt, symbol=sym)
        return {"corrected_trade_id": tid, "old_price": old_price, "new_price": new_price,
                "holding_id": _current_holding_id(acct, mkt, sym, conn=conn)}
    finally:
        if own:
            conn.close()


def _trade_sort_key(t: Mapping[str, Any]):
    """权威排序键：trade_date > executed_at > order_in_day > created_at > trade_id。"""
    td = _to_date(t.get("trade_date"))
    td_ord = td.toordinal() if td else 0
    ex = t.get("executed_at")
    ex_key = str(ex) if ex is not None else "~"          # None 排最后
    oid = t.get("order_in_day")
    oid_key = oid if oid is not None else float("inf")
    created = str(t.get("created_at") or "")
    return (td_ord, ex_key, oid_key, created, int(t.get("trade_id") or 0))


def _current_holding_id(account: str, market: str, symbol: str, *, conn) -> int | None:
    row = conn.execute(
        "SELECT id FROM real_holdings WHERE account = ? AND market = ? AND UPPER(symbol) = UPPER(?) "
        "AND close_status IN ('open','partial') ORDER BY id LIMIT 1",
        [account or "default", market, symbol],
    ).fetchone()
    return int(row[0]) if row else None


def _distinct_trade_keys(conn, account=None, market=None, symbol=None) -> list[tuple]:
    where, params = ["status = 'active'"], []
    if account:
        where.append("account = ?"); params.append(account)
    if market:
        where.append("market = ?"); params.append(market)
    if symbol:
        where.append("UPPER(symbol) = UPPER(?)"); params.append(symbol)
    rows = conn.execute(
        f"SELECT DISTINCT account, market, symbol FROM real_holding_trades WHERE {' AND '.join(where)}",
        params,
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def rebuild_real_holdings_from_trades(
    *, conn, account=None, market=None, symbol=None,
) -> dict[tuple, dict]:
    """从 active 交易流水按持仓轮次回放，重算 sell 派生 PnL，并 upsert 当前聚合持仓。

    返回 {(account,market,symbol): {holding_id, merged_from}}，供迁移做 holding_id remap。
    """
    remap: dict[tuple, dict] = {}
    for acct, mkt, sym in _distinct_trade_keys(conn, account, market, symbol):
        rows = conn.execute(
            f"SELECT {','.join(REAL_HOLDING_TRADES_FULL_COLS)} FROM real_holding_trades "
            "WHERE account = ? AND market = ? AND symbol = ? AND status = 'active'",
            [acct, mkt, sym],
        ).fetchall()
        trades = [_rowdict(REAL_HOLDING_TRADES_FULL_COLS, r) for r in rows]
        trades.sort(key=_trade_sort_key)

        epoch = 0
        rem_shares = 0.0
        rem_cost_rmb = 0.0           # 剩余 RMB 成本（含买入手续费）
        rem_local_basis = 0.0        # 剩余原币展示成本（不含手续费）
        epoch_buy_shares = 0.0
        epoch_first_date = None
        epoch_had_sell = False
        cur = None  # 当前轮次状态快照
        updates = []  # (trade_id, epoch, cost_basis_rmb, realized_pnl_rmb, realized_pnl_pct)

        for t in trades:
            side = t["side"]
            qty = float(t["quantity"])
            price = float(t["trade_price"])
            fx = float(t["fx_rate"] or 0)
            fee_rmb = float(t["fee_rmb"] or 0)
            if side == "buy":
                if rem_shares <= _SHARE_EPS:
                    epoch += 1
                    rem_shares = 0.0; rem_cost_rmb = 0.0; rem_local_basis = 0.0
                    epoch_buy_shares = 0.0; epoch_first_date = t["trade_date"]; epoch_had_sell = False
                rem_shares += qty
                rem_local_basis += price * qty
                rem_cost_rmb += price * qty * fx + fee_rmb
                epoch_buy_shares += qty
                updates.append((int(t["trade_id"]), epoch, None, None, None, None, None))
            else:  # sell
                if rem_shares <= _SHARE_EPS:
                    raise LedgerConflict(
                        f"sell trade {t['trade_id']} ({sym}) has no open position at {t['trade_date']}"
                    )
                if qty > rem_shares + 1e-6:
                    raise LedgerConflict(
                        f"sell trade {t['trade_id']} ({sym}) oversells: {qty} > remaining {rem_shares}"
                    )
                avg_rmb = rem_cost_rmb / rem_shares
                avg_local = rem_local_basis / rem_shares
                cost_basis_rmb = avg_rmb * qty
                cost_basis_local = avg_local * qty
                income_rmb = price * qty * fx
                realized = income_rmb - cost_basis_rmb - fee_rmb
                pct = (realized / cost_basis_rmb) if cost_basis_rmb else None
                # 持有天数：本轮首买日 → 本次卖出日
                _sd, _fd = _to_date(t["trade_date"]), _to_date(epoch_first_date)
                hold_days = (_sd - _fd).days if (_sd and _fd) else None
                updates.append((int(t["trade_id"]), epoch, cost_basis_rmb, realized, pct, cost_basis_local, hold_days))
                rem_shares -= qty
                rem_cost_rmb -= avg_rmb * qty
                rem_local_basis -= avg_local * qty
                epoch_had_sell = True
                if rem_shares <= _SHARE_EPS:
                    rem_shares = 0.0; rem_cost_rmb = 0.0; rem_local_basis = 0.0
            cur = {
                "epoch": epoch, "rem_shares": rem_shares, "rem_cost_rmb": rem_cost_rmb,
                "rem_local_basis": rem_local_basis, "buy_shares": epoch_buy_shares,
                "first_date": epoch_first_date, "had_sell": epoch_had_sell,
                "last_date": t["trade_date"], "name": t.get("name"), "currency": t.get("currency"),
            }

        # 回写每笔 trade 的 epoch + sell 派生快照
        for tid, ep, cb, rl, pct, cb_local, hold_days in updates:
            conn.execute(
                "UPDATE real_holding_trades SET position_epoch=?, cost_basis_rmb=?, "
                "realized_pnl_rmb=?, realized_pnl_pct=?, cost_basis_local=?, holding_days=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE trade_id=?",
                [ep, cb, rl, pct, cb_local, hold_days, tid],
            )

        # 协调 real_holdings 聚合行
        existing_ids = [int(r[0]) for r in conn.execute(
            "SELECT id FROM real_holdings WHERE account = ? AND market = ? AND UPPER(symbol) = UPPER(?) ORDER BY id",
            [acct, mkt, sym],
        ).fetchall()]
        has_open = cur is not None and cur["rem_shares"] > _SHARE_EPS
        if has_open:
            target_id = existing_ids[0] if existing_ids else int(
                conn.execute("SELECT nextval('real_holdings_id_seq')").fetchone()[0]
            )
            avg_rmb = cur["rem_cost_rmb"] / cur["rem_shares"]
            avg_local = cur["rem_local_basis"] / cur["rem_shares"]
            close_status = "partial" if cur["had_sell"] else "open"
            payload = {
                "id": target_id, "account": acct, "market": mkt, "symbol": sym,
                "name": cur["name"], "currency": cur["currency"],
                "entry_price": avg_local, "shares": cur["rem_shares"],
                "entry_date": cur["first_date"], "cost_rmb_locked": cur["rem_cost_rmb"],
                "total_buy_shares": cur["buy_shares"], "remaining_shares": cur["rem_shares"],
                "avg_cost_local_per_share": avg_local, "avg_cost_rmb_per_share": avg_rmb,
                "remaining_cost_rmb": cur["rem_cost_rmb"], "position_epoch": cur["epoch"],
                "first_entry_date": cur["first_date"], "last_trade_date": cur["last_date"],
                "close_status": close_status,
            }
            _upsert_real_holding_row(payload, exists=bool(existing_ids), conn=conn)
            for dead in existing_ids[1:]:
                conn.execute("DELETE FROM real_holdings WHERE id = ?", [dead])
            remap[(acct, mkt, sym)] = {"holding_id": target_id, "merged_from": existing_ids}
        else:
            for dead in existing_ids:
                conn.execute("DELETE FROM real_holdings WHERE id = ?", [dead])
            remap[(acct, mkt, sym)] = {"holding_id": None, "merged_from": existing_ids}
    return remap


_REAL_HOLDINGS_UPSERT_COLS = [
    "account", "market", "symbol", "name", "entry_price", "shares", "entry_date",
    "currency", "cost_rmb_locked", "total_buy_shares", "remaining_shares",
    "avg_cost_local_per_share", "avg_cost_rmb_per_share", "remaining_cost_rmb",
    "position_epoch", "first_entry_date", "last_trade_date", "close_status",
]


def _upsert_real_holding_row(payload: Mapping[str, Any], *, exists: bool, conn) -> None:
    cols = _REAL_HOLDINGS_UPSERT_COLS
    if exists:
        set_clause = ", ".join(f"{c}=?" for c in cols)
        conn.execute(
            f"UPDATE real_holdings SET {set_clause}, updated_at=CURRENT_TIMESTAMP WHERE id = ?",
            [payload.get(c) for c in cols] + [payload["id"]],
        )
    else:
        conn.execute(
            f"INSERT INTO real_holdings (id, {','.join(cols)}, updated_at) "
            f"VALUES (?, {','.join(['?'] * len(cols))}, CURRENT_TIMESTAMP)",
            [payload["id"]] + [payload.get(c) for c in cols],
        )


def fetch_real_holding_records(
    *, account=None, market=None, symbol=None, holding_id=None,
    include_voided=False, conn=None,
) -> list[dict]:
    """某标的的完整买卖时间线（默认只 active），按权威顺序升序。"""
    own = conn is None
    if own:
        conn = get_db()
    try:
        if holding_id is not None:
            h = fetch_real_holding_by_id(int(holding_id), conn=conn)
            if not h:
                return []
            account, market, symbol = h.get("account"), h.get("market"), h.get("symbol")
        where, params = ["account = ?", "market = ?", "UPPER(symbol) = UPPER(?)"], [account or "default", market, symbol]
        if not include_voided:
            where.append("status = 'active'")
        rows = conn.execute(
            f"SELECT {','.join(REAL_HOLDING_TRADES_FULL_COLS)} FROM real_holding_trades "
            f"WHERE {' AND '.join(where)}",
            params,
        ).fetchall()
        trades = [_rowdict(REAL_HOLDING_TRADES_FULL_COLS, r) for r in rows]
        trades.sort(key=_trade_sort_key)
        return trades
    finally:
        if own:
            conn.close()


def fetch_real_holding_trade_history(*, include_voided=False, conn=None) -> list[dict]:
    """全部卖出成交，按卖出日期倒序，供「已卖出/交易历史」区。"""
    own = conn is None
    if own:
        conn = get_db()
    try:
        where = ["side = 'sell'"]
        if not include_voided:
            where.append("status = 'active'")
        rows = conn.execute(
            f"SELECT {','.join(REAL_HOLDING_TRADES_FULL_COLS)} FROM real_holding_trades "
            f"WHERE {' AND '.join(where)} ORDER BY trade_date DESC, trade_id DESC",
            [],
        ).fetchall()
        return [_rowdict(REAL_HOLDING_TRADES_FULL_COLS, r) for r in rows]
    finally:
        if own:
            conn.close()


def fetch_active_trade_counts(*, conn=None) -> dict[tuple, int]:
    """{(account, market, UPPER(symbol)): 活跃交易笔数}，给前端决定是否显示展开三角。"""
    own = conn is None
    if own:
        conn = get_db(read_only=True)
    try:
        rows = conn.execute(
            "SELECT account, market, UPPER(symbol), COUNT(*) FROM real_holding_trades "
            "WHERE status = 'active' GROUP BY account, market, UPPER(symbol)"
        ).fetchall()
        return {(r[0], r[1], r[2]): int(r[3]) for r in rows}
    finally:
        if own:
            conn.close()


def fetch_pnl_summary(*, price_lookup=None, as_of=None, conn=None) -> dict[str, Any]:
    """收益摘要：已实现（成交日锁定汇率）+ 未实现（当前价/当前汇率盯市）+ 合计。

    price_lookup(market, symbol) -> (current_price_local, current_fx_rate) 或 None。
    不提供时只算已实现，unrealized 记为 None（前端/调用方决定是否盯市）。
    """
    own = conn is None
    if own:
        conn = get_db()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl_rmb),0), MIN(trade_date) FROM real_holding_trades "
            "WHERE side='sell' AND status='active'"
        ).fetchone()
        realized = float(row[0] or 0.0)
        realized_since = row[1]
        unrealized = None
        if price_lookup is not None:
            unrealized = 0.0
            for h in conn.execute(
                "SELECT market, symbol, remaining_shares, remaining_cost_rmb FROM real_holdings "
                "WHERE close_status IN ('open','partial')"
            ).fetchall():
                mkt, sym, rem_sh, rem_cost = h
                got = price_lookup(mkt, sym)
                if not got:
                    continue
                cur_price, cur_fx = got
                mv = float(cur_price) * float(rem_sh or 0) * float(cur_fx or 0)
                unrealized += mv - float(rem_cost or 0)
        total = realized + (unrealized or 0.0)
        return {
            "realized_pnl_rmb": realized,
            "unrealized_pnl_rmb": unrealized,
            "total_pnl_rmb": (realized + unrealized) if unrealized is not None else None,
            "as_of": as_of,
            "realized_since": realized_since,
        }
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 现金账本：入金/出金 + 由交易流水自动算的买卖现金进出
# ---------------------------------------------------------------------------

REAL_HOLDING_CASH_FLOW_COLS = ["account", "flow_type", "amount_rmb", "flow_date", "notes"]
REAL_HOLDING_CASH_FLOW_FULL_COLS = ["flow_id"] + REAL_HOLDING_CASH_FLOW_COLS + ["created_at"]


def insert_cash_flow(item: Mapping[str, Any], *, conn=None) -> int:
    """记一笔入金/出金。flow_type=deposit|withdraw，amount_rmb 恒正。"""
    own = conn is None
    if own:
        conn = get_db()
    try:
        ftype = (item.get("flow_type") or "").strip().lower()
        if ftype not in ("deposit", "withdraw"):
            raise LedgerError("flow_type must be deposit|withdraw")
        amt = float(item.get("amount_rmb") or 0)
        if amt <= 0:
            raise LedgerError("amount_rmb must be > 0")
        conn.execute(
            f"INSERT INTO real_holding_cash_flows ({','.join(REAL_HOLDING_CASH_FLOW_COLS)}) "
            f"VALUES ({','.join(['?'] * len(REAL_HOLDING_CASH_FLOW_COLS))})",
            [item.get("account") or "default", ftype, amt,
             _to_date(item.get("flow_date") or item.get("date")), item.get("notes")],
        )
        return int(conn.execute("SELECT currval('real_holding_cash_flows_id_seq')").fetchone()[0])
    finally:
        if own:
            conn.close()


def fetch_cash_flows(*, account=None, conn=None) -> list[dict]:
    own = conn is None
    if own:
        conn = get_db()
    try:
        where, params = [], []
        if account:
            where.append("account = ?"); params.append(account)
        sql = f"SELECT {','.join(REAL_HOLDING_CASH_FLOW_FULL_COLS)} FROM real_holding_cash_flows"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY flow_date DESC NULLS LAST, flow_id DESC"
        return [_rowdict(REAL_HOLDING_CASH_FLOW_FULL_COLS, r) for r in conn.execute(sql, params).fetchall()]
    finally:
        if own:
            conn.close()


def delete_cash_flow(flow_id: int, *, conn=None) -> int:
    own = conn is None
    if own:
        conn = get_db()
    try:
        exists = conn.execute("SELECT 1 FROM real_holding_cash_flows WHERE flow_id=?", [int(flow_id)]).fetchone()
        if exists:
            conn.execute("DELETE FROM real_holding_cash_flows WHERE flow_id=?", [int(flow_id)])
            return 1
        return 0
    finally:
        if own:
            conn.close()


def fetch_cash_summary(*, account=None, conn=None) -> dict[str, Any]:
    """现金余额 = 累计入金 − 累计出金 − 买入(含费) + 卖出(扣费)。

    买卖现金进出从 active 交易流水自动算，用户只需记入金/出金。
    """
    own = conn is None
    if own:
        conn = get_db()
    try:
        acct_clause = "WHERE account = ?" if account else ""
        params = [account] if account else []
        dep = conn.execute(
            f"SELECT COALESCE(SUM(CASE WHEN flow_type='deposit' THEN amount_rmb ELSE 0 END),0), "
            f"COALESCE(SUM(CASE WHEN flow_type='withdraw' THEN amount_rmb ELSE 0 END),0) "
            f"FROM real_holding_cash_flows {acct_clause}", params,
        ).fetchone()
        deposits, withdrawals = float(dep[0] or 0), float(dep[1] or 0)
        tparams = [account] if account else []
        tclause = "AND account = ?" if account else ""
        buy_out = float(conn.execute(
            f"SELECT COALESCE(SUM(COALESCE(gross_amount_rmb,0)+COALESCE(fee_rmb,0)),0) FROM real_holding_trades "
            f"WHERE side='buy' AND status='active' {tclause}", tparams,
        ).fetchone()[0] or 0)
        sell_in = float(conn.execute(
            f"SELECT COALESCE(SUM(COALESCE(gross_amount_rmb,0)-COALESCE(fee_rmb,0)),0) FROM real_holding_trades "
            f"WHERE side='sell' AND status='active' {tclause}", tparams,
        ).fetchone()[0] or 0)
        cash = deposits - withdrawals - buy_out + sell_in
        if abs(cash) < 0.005:        # 抹掉浮点尾巴，避免显示成 -0
            cash = 0.0
        return {
            "cash_rmb": cash,
            "deposits_rmb": deposits,
            "withdrawals_rmb": withdrawals,
            "buy_outflow_rmb": buy_out,
            "sell_inflow_rmb": sell_in,
            "has_deposits": (deposits > 0 or withdrawals > 0),
        }
    finally:
        if own:
            conn.close()


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _generate_discipline_id(prefix: str, *parts: Any) -> str:
    stamp = int(time.time() * 1000)
    clean_parts = [str(p).replace(" ", "_") for p in parts if p is not None and str(p) != ""]
    base = "_".join([prefix] + clean_parts + [str(stamp)])
    if len(base) <= 96:
        return base
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
    return "_".join([prefix] + clean_parts[:2] + [digest])


def _normalize_discipline_trigger(
    trigger: Mapping[str, Any],
    *,
    plan_id: str,
    idx: int,
) -> list[Any]:
    threshold = _as_float_or_none(trigger.get("threshold_price") or trigger.get("price"))
    price_min = _as_float_or_none(trigger.get("price_min") or trigger.get("min_price"))
    price_max = _as_float_or_none(trigger.get("price_max") or trigger.get("max_price"))
    comparator = str(trigger.get("comparator") or "").strip().lower()
    if not comparator:
        if price_min is not None and price_max is not None:
            comparator = "between"
        elif price_max is not None or threshold is not None:
            comparator = "lte"
        else:
            comparator = "gte"
    if threshold is not None:
        if price_min is None and comparator in {"gte", "gt", "above"}:
            price_min = threshold
        elif price_max is None and comparator in {"lte", "lt", "below"}:
            price_max = threshold
        elif price_min is None and price_max is None:
            price_min = threshold
    if price_min is None and price_max is None:
        raise ValueError("discipline trigger requires price_min/price_max/threshold_price")
    trigger_id = _clean_text(trigger.get("trigger_id")) or f"{plan_id}_t{idx:02d}"
    trigger_type = _clean_text(trigger.get("trigger_type") or trigger.get("kind")) or "price"
    severity = (_clean_text(trigger.get("severity")) or "info").lower()
    action_label = _clean_text(trigger.get("action_label") or trigger.get("action")) or trigger_type
    priority = int(trigger.get("priority") or idx)
    return [
        trigger_id,
        plan_id,
        trigger_type,
        comparator,
        price_min,
        price_max,
        severity,
        priority,
        action_label,
        _clean_text(trigger.get("suggested_size_text") or trigger.get("size_text")),
        _clean_text(trigger.get("rationale") or trigger.get("reason")),
        False,
    ]


def create_real_holding_discipline_plan(
    holding_id: int,
    payload: Mapping[str, Any],
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
    replace_active: bool = False,
) -> dict[str, Any]:
    """Create a user-confirmed discipline plan for one real holding.

    The plan is advisory only. It never writes watchlist, recommendations,
    model_sim_holdings, or any trade table.
    """
    own = conn is None
    if own:
        conn = get_db()
    holding = fetch_real_holding_by_id(int(holding_id), conn=conn)
    if not holding:
        if own:
            conn.close()
        raise DisciplinePlanNotFound(f"real holding id not found: {holding_id}")

    existing = conn.execute(
        "SELECT plan_id FROM real_holding_discipline_plans WHERE holding_id = ? AND status = 'active' LIMIT 1",
        [int(holding_id)],
    ).fetchone()
    if existing and not replace_active:
        if own:
            conn.close()
        raise DisciplinePlanConflict(f"active discipline plan already exists for holding {holding_id}: {existing[0]}")
    if existing and replace_active:
        conn.execute(
            "UPDATE real_holding_discipline_plans SET status = 'archived', updated_at = CURRENT_TIMESTAMP WHERE holding_id = ? AND status = 'active'",
            [int(holding_id)],
        )

    triggers = list(payload.get("triggers") or payload.get("rules") or [])
    if not triggers:
        if own:
            conn.close()
        raise ValueError("discipline plan requires at least one trigger")

    plan_id = _clean_text(payload.get("plan_id")) or _generate_discipline_id("disc", holding_id, holding.get("symbol"))
    plan_type = _clean_text(payload.get("plan_type")) or "manual_price_plan"
    source_type = _clean_text(payload.get("source_type")) or "manual_confirmed"
    validation_status = _clean_text(payload.get("validation_status")) or "manual_guardrail_unvalidated"
    confirmed_at = payload.get("confirmed_at")
    if confirmed_at is None and source_type == "manual_confirmed":
        confirmed_at = datetime.now()
    cost_basis_price = _as_float_or_none(payload.get("cost_basis_price")) or _as_float_or_none(holding.get("entry_price"))
    shares_snapshot = _as_float_or_none(payload.get("shares_snapshot")) or _as_float_or_none(holding.get("shares"))
    vals = [
        plan_id,
        int(holding_id),
        holding.get("account") or "default",
        holding.get("market") or _infer_market_from_ticker(str(holding.get("symbol"))),
        holding.get("symbol"),
        plan_type,
        source_type,
        validation_status,
        _clean_text(payload.get("status")) or "active",
        cost_basis_price,
        shares_snapshot,
        _clean_text(payload.get("thesis")),
        _clean_text(payload.get("invalidation_note")),
        _clean_text(payload.get("notes")),
        _to_ts(confirmed_at) if confirmed_at is not None else None,
    ]
    conn.execute(
        f"INSERT INTO real_holding_discipline_plans ({','.join(REAL_HOLDING_DISCIPLINE_PLAN_COLS)}) "
        f"VALUES ({','.join(['?'] * len(REAL_HOLDING_DISCIPLINE_PLAN_COLS))})",
        vals,
    )
    for idx, trigger in enumerate(triggers, start=1):
        tvals = _normalize_discipline_trigger(trigger, plan_id=plan_id, idx=idx)
        conn.execute(
            f"INSERT INTO real_holding_discipline_triggers ({','.join(REAL_HOLDING_DISCIPLINE_TRIGGER_COLS)}) "
            f"VALUES ({','.join(['?'] * len(REAL_HOLDING_DISCIPLINE_TRIGGER_COLS))})",
            tvals,
        )

    plan = fetch_real_holding_discipline_plan(plan_id, conn=conn)
    if own:
        conn.close()
    return plan or {"plan_id": plan_id, "triggers": []}


def fetch_real_holding_discipline_plan(
    plan_id: str,
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, Any] | None:
    plans = fetch_real_holding_discipline_plans(plan_id=plan_id, status=None, conn=conn)
    return plans[0] if plans else None


def fetch_real_holding_discipline_plans(
    *,
    holding_id: int | None = None,
    plan_id: str | None = None,
    status: str | None = "active",
    conn: duckdb.DuckDBPyConnection | None = None,
) -> list[dict[str, Any]]:
    own = conn is None
    if own:
        conn = get_db()
    where = []
    params: list[Any] = []
    if holding_id is not None:
        where.append("holding_id = ?")
        params.append(int(holding_id))
    if plan_id:
        where.append("plan_id = ?")
        params.append(str(plan_id))
    if status:
        where.append("status = ?")
        params.append(str(status))
    sql = (
        f"SELECT {','.join(REAL_HOLDING_DISCIPLINE_PLAN_FULL_COLS)} "
        "FROM real_holding_discipline_plans"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, plan_id DESC"
    rows = conn.execute(sql, params).fetchall()
    plans = [_rowdict(REAL_HOLDING_DISCIPLINE_PLAN_FULL_COLS, r) for r in rows]
    for plan in plans:
        trigger_rows = conn.execute(
            f"SELECT {','.join(REAL_HOLDING_DISCIPLINE_TRIGGER_FULL_COLS)} "
            "FROM real_holding_discipline_triggers WHERE plan_id = ? ORDER BY priority ASC, trigger_id ASC",
            [plan["plan_id"]],
        ).fetchall()
        plan["triggers"] = [_rowdict(REAL_HOLDING_DISCIPLINE_TRIGGER_FULL_COLS, r) for r in trigger_rows]
    if own:
        conn.close()
    return plans


def update_real_holding_discipline_plan_status(
    plan_id: str,
    status: str,
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    own = conn is None
    if own:
        conn = get_db()
    exists = conn.execute(
        "SELECT 1 FROM real_holding_discipline_plans WHERE plan_id = ?",
        [str(plan_id)],
    ).fetchone()
    if not exists:
        if own:
            conn.close()
        return 0
    n = conn.execute(
        "UPDATE real_holding_discipline_plans SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE plan_id = ?",
        [str(status), str(plan_id)],
    ).rowcount
    if own:
        conn.close()
    return 1 if n is None or n < 0 else int(n)


def _trigger_threshold_text(trigger: Mapping[str, Any]) -> str:
    comp = str(trigger.get("comparator") or "").lower()
    lo = trigger.get("price_min")
    hi = trigger.get("price_max")
    if comp in {"between", "range"} and lo is not None and hi is not None:
        return f"{float(lo):.2f}-{float(hi):.2f}"
    if comp in {"between_open_high", "left_closed_right_open"} and lo is not None and hi is not None:
        return f"{float(lo):.2f}-<{float(hi):.2f}"
    if comp in {"gte", "gt", "above"}:
        v = lo if lo is not None else hi
        return f"{'>=' if comp == 'gte' else '>'}{float(v):.2f}" if v is not None else "—"
    if comp in {"lte", "lt", "below"}:
        v = hi if hi is not None else lo
        return f"{'<=' if comp == 'lte' else '<'}{float(v):.2f}" if v is not None else "—"
    if lo is not None:
        return f"{float(lo):.2f}"
    if hi is not None:
        return f"{float(hi):.2f}"
    return "—"


def _discipline_trigger_matches(trigger: Mapping[str, Any], price: float) -> bool:
    comp = str(trigger.get("comparator") or "").lower()
    lo = _as_float_or_none(trigger.get("price_min"))
    hi = _as_float_or_none(trigger.get("price_max"))
    if comp in {"between", "range"}:
        return (lo is None or price >= lo) and (hi is None or price <= hi)
    if comp in {"between_open_high", "left_closed_right_open"}:
        return (lo is None or price >= lo) and (hi is None or price < hi)
    if comp in {"gte", "above"}:
        threshold = lo if lo is not None else hi
        return threshold is not None and price >= threshold
    if comp == "gt":
        threshold = lo if lo is not None else hi
        return threshold is not None and price > threshold
    if comp in {"lte", "below"}:
        threshold = hi if hi is not None else lo
        return threshold is not None and price <= threshold
    if comp == "lt":
        threshold = hi if hi is not None else lo
        return threshold is not None and price < threshold
    return False


def _discipline_trigger_distance(trigger: Mapping[str, Any], price: float) -> float:
    comp = str(trigger.get("comparator") or "").lower()
    lo = _as_float_or_none(trigger.get("price_min"))
    hi = _as_float_or_none(trigger.get("price_max"))
    if comp in {"between", "range", "between_open_high", "left_closed_right_open"}:
        if lo is not None and price < lo:
            return lo - price
        if hi is not None and price > hi:
            return price - hi
        return 0.0
    if comp in {"gte", "gt", "above"}:
        threshold = lo if lo is not None else hi
        return abs(price - threshold) if threshold is not None else float("inf")
    if comp in {"lte", "lt", "below"}:
        threshold = hi if hi is not None else lo
        return abs(price - threshold) if threshold is not None else float("inf")
    threshold = lo if lo is not None else hi
    return abs(price - threshold) if threshold is not None else float("inf")


def evaluate_real_holding_discipline_plan(
    plan: Mapping[str, Any] | None,
    *,
    current_price: Any,
    price_trade_date: Any = None,
    price_is_stale: bool = False,
) -> dict[str, Any]:
    """Evaluate a discipline plan against one quote snapshot.

    This pure function is intentionally advisory-only. Stale or missing prices
    block triggers so prior-session quotes cannot produce trade guidance.
    """
    if not plan:
        return {"status": "no_plan", "triggered": False}
    symbol = str(plan.get("symbol") or "")
    base = {
        "plan_id": plan.get("plan_id"),
        "holding_id": plan.get("holding_id"),
        "market": plan.get("market"),
        "symbol": symbol,
        "plan_type": plan.get("plan_type"),
        "source_type": plan.get("source_type"),
        "validation_status": plan.get("validation_status"),
        "current_price": None,
        "price_trade_date": str(price_trade_date)[:10] if price_trade_date else None,
        "auto_trade_allowed": False,
        "triggered": False,
    }
    if str(plan.get("status") or "active") != "active":
        return {**base, "status": "inactive", "message": "纪律计划未启用"}
    price = _as_float_or_none(current_price)
    triggers = list(plan.get("triggers") or [])
    # 行情缺失/过期时也要把纪律线带给前端展示（规则可见，但 data_blocked 不触发）
    all_triggers = [
        {
            "trigger_id": t.get("trigger_id"),
            "trigger_type": t.get("trigger_type"),
            "threshold_text": _trigger_threshold_text(t),
            "action_label": t.get("action_label"),
            "suggested_size_text": t.get("suggested_size_text"),
            "severity": t.get("severity"),
            "priority": int(t.get("priority") or 99),
            "distance": _discipline_trigger_distance(t, price) if price is not None else None,
        }
        for t in triggers
    ]
    if price is None:
        return {
            **base,
            "status": "missing_price",
            "action_label": "先刷新行情",
            "data_blocked": True,
            "message": f"{symbol or '该持仓'} 暂无可用行情，纪律规则不触发",
            "all_triggers": all_triggers,
        }
    base["current_price"] = price
    if price_is_stale:
        return {
            **base,
            "status": "stale_price",
            "action_label": "先刷新行情",
            "data_blocked": True,
            "message": f"{symbol or '该持仓'} 行情停留在 {base['price_trade_date'] or '上一交易日'}，先刷新，暂不触发纪律规则",
            "all_triggers": all_triggers,
        }

    ordered_next = sorted(
        triggers,
        key=lambda t: (
            _discipline_trigger_distance(t, price),
            int(t.get("priority") or 99),
            str(t.get("trigger_id") or ""),
        ),
    )
    next_triggers = [
        {
            "trigger_id": t.get("trigger_id"),
            "trigger_type": t.get("trigger_type"),
            "threshold_text": _trigger_threshold_text(t),
            "action_label": t.get("action_label"),
            "suggested_size_text": t.get("suggested_size_text"),
            "severity": t.get("severity"),
            "priority": int(t.get("priority") or 99),
        }
        for t in ordered_next[:4]
    ]

    matches = [t for t in triggers if _discipline_trigger_matches(t, price)]
    matches.sort(key=lambda t: (int(t.get("priority") or 99), str(t.get("trigger_id") or "")))
    if matches:
        t = matches[0]
        action = str(t.get("action_label") or t.get("trigger_type") or "纪律提醒")
        severity = str(t.get("severity") or "info")
        msg = f"{symbol or '该持仓'} {price:.2f} 触发纪律：{action}"
        if t.get("suggested_size_text"):
            msg += f" · {t.get('suggested_size_text')}"
        return {
            **base,
            "status": "triggered",
            "triggered": True,
            "trigger_id": t.get("trigger_id"),
            "trigger_type": t.get("trigger_type"),
            "comparator": t.get("comparator"),
            "threshold_text": _trigger_threshold_text(t),
            "severity": severity,
            "priority": int(t.get("priority") or 99),
            "action_label": action,
            "suggested_size_text": t.get("suggested_size_text"),
            "rationale": t.get("rationale"),
            "message": msg,
            "next_triggers": next_triggers,
            "all_triggers": all_triggers,
        }

    return {
        **base,
        "status": "watching",
        "action_label": "未触发",
        "message": f"{symbol or '该持仓'} {price:.2f} 尚未触发纪律价位",
        "next_triggers": next_triggers,
        "all_triggers": all_triggers,
    }


def save_real_holding_discipline_event(
    evaluation: Mapping[str, Any],
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, Any] | None:
    if not evaluation or evaluation.get("status") != "triggered" or evaluation.get("data_blocked"):
        return None
    plan_id = _clean_text(evaluation.get("plan_id"))
    trigger_id = _clean_text(evaluation.get("trigger_id"))
    holding_id = evaluation.get("holding_id")
    symbol = _clean_text(evaluation.get("symbol"))
    if not plan_id or not trigger_id or holding_id is None or not symbol:
        return None
    price_trade_date = _clean_text(evaluation.get("price_trade_date"))
    fingerprint = json.dumps(
        [plan_id, trigger_id, int(holding_id), symbol, price_trade_date, evaluation.get("current_price")],
        ensure_ascii=False,
        sort_keys=True,
    )
    event_id = "disc_evt_" + hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:20]
    own = conn is None
    if own:
        conn = get_db()
    existing = conn.execute(
        "SELECT event_id FROM real_holding_discipline_events WHERE event_id = ?",
        [event_id],
    ).fetchone()
    if existing:
        if own:
            conn.close()
        return {"event_id": event_id, "created": False}
    vals = [
        event_id,
        plan_id,
        trigger_id,
        int(holding_id),
        _clean_text(evaluation.get("account")),
        _clean_text(evaluation.get("market")) or _infer_market_from_ticker(symbol),
        symbol,
        evaluation.get("current_price"),
        price_trade_date,
        _clean_text(evaluation.get("severity")) or "info",
        _clean_text(evaluation.get("action_label")) or "纪律提醒",
        _clean_text(evaluation.get("message")),
        json.dumps(evaluation, ensure_ascii=False, sort_keys=True),
        "new",
        None,
    ]
    conn.execute(
        f"INSERT INTO real_holding_discipline_events ({','.join(REAL_HOLDING_DISCIPLINE_EVENT_COLS)}) "
        f"VALUES ({','.join(['?'] * len(REAL_HOLDING_DISCIPLINE_EVENT_COLS))})",
        vals,
    )
    if own:
        conn.close()
    return {"event_id": event_id, "created": True}


def fetch_real_holding_discipline_events(
    *,
    holding_id: int | None = None,
    plan_id: str | None = None,
    limit: int = 100,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> list[dict[str, Any]]:
    own = conn is None
    if own:
        conn = get_db()
    where = []
    params: list[Any] = []
    if holding_id is not None:
        where.append("holding_id = ?")
        params.append(int(holding_id))
    if plan_id:
        where.append("plan_id = ?")
        params.append(str(plan_id))
    sql = (
        f"SELECT {','.join(REAL_HOLDING_DISCIPLINE_EVENT_FULL_COLS)} "
        "FROM real_holding_discipline_events"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    safe_limit = max(1, min(int(limit or 100), 500))
    sql += f" ORDER BY triggered_at DESC, event_id DESC LIMIT {safe_limit}"
    rows = conn.execute(sql, params).fetchall()
    events = []
    for r in rows:
        d = _rowdict(REAL_HOLDING_DISCIPLINE_EVENT_FULL_COLS, r)
        d["evaluation"] = _load_json_field(d.pop("evaluation_json", None), None)
        events.append(d)
    if own:
        conn.close()
    return events


def _dump_json_field(value: Any) -> str:
    if value is None:
        value = []
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _dump_json_value(value: Any) -> str:
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
        holding_id = item.get("holding_id")
        if holding_id is None:
            # 兜底:旧 caller 没传 holding_id → 跳过(不再支持按 symbol 单条写入,
            # 避免 PK constraint 与 lot 独立追踪需求冲突)
            continue
        vals = [
            review_run_id,
            int(holding_id),
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
            item.get("price_trade_date") or item.get("trade_date"),
            item.get("prev_close"),
            item.get("prev_trade_date"),
            item.get("day_change_basis"),
            item.get("day_change_rmb"),
            item.get("day_change_pct"),
            bool(item.get("price_is_prior_session")),
            _dump_json_field(item.get("size_advisory")),
            _dump_json_field(item.get("industry_heat")),
            _dump_json_value(item.get("discipline")),
        ]
        conn.execute(
            f"INSERT INTO real_holding_review_items ({','.join(REAL_HOLDING_REVIEW_ITEM_COLS)}) "
            f"VALUES ({','.join(['?'] * len(REAL_HOLDING_REVIEW_ITEM_COLS))})",
            vals,
        )
        discipline = item.get("discipline")
        if isinstance(discipline, Mapping):
            enriched_discipline = dict(discipline)
            enriched_discipline.setdefault("holding_id", int(holding_id))
            enriched_discipline.setdefault("account", item.get("account") or "default")
            enriched_discipline.setdefault("market", item.get("market") or _infer_market_from_ticker(str(symbol)))
            enriched_discipline.setdefault("symbol", symbol)
            save_real_holding_discipline_event(enriched_discipline, conn=conn)
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
    item_cols = list(REAL_HOLDING_REVIEW_ITEM_FULL_COLS)
    if not _table_has_column(conn, "real_holding_review_items", "discipline_json"):
        item_cols = [c for c in item_cols if c != "discipline_json"]
    item_rows = conn.execute(
        f"SELECT {','.join(item_cols)} "
        "FROM real_holding_review_items WHERE review_run_id = ? "
        "ORDER BY action_priority ASC, symbol ASC",
        [run["review_run_id"]],
    ).fetchall()
    items = []
    for r in item_rows:
        d = _rowdict(item_cols, r)
        d["reasons"] = _load_json_field(d.pop("reasons_json", None), [])
        d["risk_flags"] = _load_json_field(d.pop("risk_flags_json", None), [])
        d["data_flags"] = _load_json_field(d.pop("data_flags_json", None), [])
        d["size_advisory"] = _load_json_field(d.pop("size_advisory_json", None), None)
        d["industry_heat"] = _load_json_field(d.pop("industry_heat_json", None), None)
        d["discipline"] = _load_json_field(d.pop("discipline_json", None), None)
        d["trade_date"] = d.get("price_trade_date")
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
