#!/usr/bin/env python3
"""Create a clean StockAssistant v2 DuckDB database.

This is intentionally not a migration script. It creates schema from zero and,
optionally, seeds only the system universe from code-defined universe sources.
It must not read old DuckDB rows or old data/latest artifacts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key        VARCHAR PRIMARY KEY,
    value      VARCHAR NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS manual_watchlist (
    market      VARCHAR NOT NULL,
    symbol      VARCHAR NOT NULL,
    name        VARCHAR,
    notes       VARCHAR,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (market, symbol)
);

CREATE SEQUENCE IF NOT EXISTS holdings_id_seq;
CREATE TABLE IF NOT EXISTS holdings (
    id          BIGINT PRIMARY KEY DEFAULT nextval('holdings_id_seq'),
    market      VARCHAR NOT NULL,
    symbol      VARCHAR NOT NULL,
    entry_price DOUBLE NOT NULL,
    shares      DOUBLE NOT NULL,
    entry_date  DATE,
    source      VARCHAR NOT NULL DEFAULT 'manual',
    notes       VARCHAR,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_universe (
    pool_id       VARCHAR NOT NULL,
    pool_name     VARCHAR,
    market        VARCHAR NOT NULL,
    symbol        VARCHAR NOT NULL,
    raw_symbol    VARCHAR,
    name          VARCHAR,
    theme         VARCHAR,
    industry      VARCHAR,
    source        VARCHAR NOT NULL,
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (pool_id, market, symbol)
);

CREATE TABLE IF NOT EXISTS pool_membership (
    pool_id       VARCHAR NOT NULL,
    market        VARCHAR NOT NULL,
    symbol        VARCHAR NOT NULL,
    pool_type     VARCHAR NOT NULL,
    source        VARCHAR NOT NULL,
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (pool_id, market, symbol)
);

CREATE TABLE IF NOT EXISTS price_daily (
    market            VARCHAR NOT NULL,
    symbol            VARCHAR NOT NULL,
    trade_date        DATE NOT NULL,
    interval          VARCHAR NOT NULL DEFAULT '1d',
    close             DOUBLE,
    prev_close        DOUBLE,
    currency          VARCHAR,
    market_cap        DOUBLE,
    forward_pe        DOUBLE,
    trailing_pe       DOUBLE,
    peg_ratio         DOUBLE,
    ytd_pct           DOUBLE,
    one_week_pct      DOUBLE,
    one_month_pct     DOUBLE,
    one_year_pct      DOUBLE,
    source            VARCHAR,
    source_updated_at TIMESTAMP,
    fetched_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (market, symbol, trade_date, interval)
);

CREATE TABLE IF NOT EXISTS financial_statements (
    market            VARCHAR NOT NULL,
    symbol            VARCHAR NOT NULL,
    fiscal_year       INTEGER NOT NULL,
    fiscal_quarter    VARCHAR NOT NULL,
    period_end_date   DATE,
    statement_type    VARCHAR NOT NULL,
    source            VARCHAR NOT NULL,
    reported_at       TIMESTAMP,
    source_updated_at TIMESTAMP,
    fetched_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    content_hash      VARCHAR,
    payload_json      VARCHAR,
    is_current        BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (market, symbol, fiscal_year, fiscal_quarter, statement_type, source)
);

CREATE TABLE IF NOT EXISTS financial_statement_versions (
    id                BIGINT,
    market            VARCHAR NOT NULL,
    symbol            VARCHAR NOT NULL,
    fiscal_year       INTEGER NOT NULL,
    fiscal_quarter    VARCHAR NOT NULL,
    statement_type    VARCHAR NOT NULL,
    source            VARCHAR NOT NULL,
    content_hash      VARCHAR,
    payload_json      VARCHAR,
    archived_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    strategy_version VARCHAR PRIMARY KEY,
    status           VARCHAR NOT NULL DEFAULT 'draft',
    description      VARCHAR,
    params_json      VARCHAR,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    activated_at     TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recommendation_runs (
    run_id            VARCHAR PRIMARY KEY,
    run_date          DATE NOT NULL,
    strategy_version  VARCHAR NOT NULL,
    model_version     VARCHAR NOT NULL,
    universe_scope    VARCHAR NOT NULL,
    data_cutoff_at    TIMESTAMP NOT NULL,
    generated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status            VARCHAR NOT NULL DEFAULT 'generated',
    notes             VARCHAR
);

CREATE TABLE IF NOT EXISTS recommendation_picks (
    run_id            VARCHAR NOT NULL,
    market            VARCHAR NOT NULL,
    symbol            VARCHAR NOT NULL,
    name              VARCHAR,
    rank              INTEGER,
    rating            VARCHAR,
    signal            VARCHAR,
    total_score       DOUBLE,
    factor_scores_json VARCHAR,
    recommendation_reason VARCHAR,
    risk_flags_json   VARCHAR,
    entry_price       DOUBLE,
    entry_currency    VARCHAR,
    universe_scope    VARCHAR NOT NULL,
    source_origin     VARCHAR NOT NULL,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, market, symbol)
);

CREATE TABLE IF NOT EXISTS portfolio_plans (
    run_id             VARCHAR NOT NULL,
    plan_version       VARCHAR NOT NULL,
    strategy_scope     VARCHAR NOT NULL,
    market             VARCHAR NOT NULL,
    symbol             VARCHAR NOT NULL,
    target_weight      DOUBLE,
    action             VARCHAR,
    risk_limit_json    VARCHAR,
    transaction_cost_bps DOUBLE,
    benchmark_symbol   VARCHAR,
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, plan_version, strategy_scope, market, symbol)
);

CREATE TABLE IF NOT EXISTS pick_outcomes (
    run_id          VARCHAR NOT NULL,
    market          VARCHAR NOT NULL,
    symbol          VARCHAR NOT NULL,
    horizon         VARCHAR NOT NULL,
    outcome_date    DATE NOT NULL,
    return_pct      DOUBLE,
    benchmark_symbol VARCHAR,
    benchmark_pct   DOUBLE,
    alpha_pct       DOUBLE,
    is_success      BOOLEAN,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, market, symbol, horizon)
);

CREATE TABLE IF NOT EXISTS portfolio_performance (
    run_id             VARCHAR NOT NULL,
    plan_version       VARCHAR NOT NULL,
    as_of_date         DATE NOT NULL,
    nav                DOUBLE,
    return_pct         DOUBLE,
    max_drawdown_pct   DOUBLE,
    sharpe             DOUBLE,
    calmar             DOUBLE,
    turnover_pct       DOUBLE,
    transaction_cost_pct DOUBLE,
    updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, plan_version, as_of_date)
);

CREATE TABLE IF NOT EXISTS factor_attribution (
    strategy_version VARCHAR NOT NULL,
    market           VARCHAR NOT NULL,
    factor_name      VARCHAR NOT NULL,
    period_start     DATE NOT NULL,
    period_end       DATE NOT NULL,
    contribution_pct DOUBLE,
    sample_size      INTEGER,
    updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (strategy_version, market, factor_name, period_start, period_end)
);

CREATE TABLE IF NOT EXISTS strategy_review_reports (
    strategy_version VARCHAR NOT NULL,
    market           VARCHAR NOT NULL,
    period_type      VARCHAR NOT NULL,
    period_start     DATE NOT NULL,
    period_end       DATE NOT NULL,
    sample_size      INTEGER,
    win_rate         DOUBLE,
    avg_alpha        DOUBLE,
    max_drawdown_pct DOUBLE,
    conclusion       VARCHAR,
    recommended_action VARCHAR,
    generated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payload_json     VARCHAR,
    PRIMARY KEY (strategy_version, market, period_type, period_start, period_end)
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id         VARCHAR PRIMARY KEY,
    mode           VARCHAR NOT NULL,
    status         VARCHAR NOT NULL,
    planned_at     TIMESTAMP,
    started_at     TIMESTAMP,
    completed_at   TIMESTAMP,
    trigger_source VARCHAR,
    created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pipeline_steps (
    run_id           VARCHAR NOT NULL,
    step_name        VARCHAR NOT NULL,
    status           VARCHAR NOT NULL,
    started_at       TIMESTAMP,
    ended_at         TIMESTAMP,
    duration_seconds INTEGER,
    sink             VARCHAR,
    error_summary    VARCHAR,
    PRIMARY KEY (run_id, step_name)
);

CREATE TABLE IF NOT EXISTS source_fetch_log (
    run_id            VARCHAR,
    source            VARCHAR NOT NULL,
    market            VARCHAR,
    status            VARCHAR NOT NULL,
    status_code       VARCHAR,
    fallback_source   VARCHAR,
    fetched_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    message           VARCHAR,
    PRIMARY KEY (source, market, fetched_at)
);

CREATE TABLE IF NOT EXISTS data_quality_checks (
    check_id       VARCHAR PRIMARY KEY,
    run_id         VARCHAR,
    check_name     VARCHAR NOT NULL,
    market         VARCHAR,
    status         VARCHAR NOT NULL,
    severity       VARCHAR NOT NULL,
    message        VARCHAR,
    generated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_raw_snapshots (
    snapshot_id      VARCHAR PRIMARY KEY,
    source           VARCHAR NOT NULL,
    market           VARCHAR,
    business_date    DATE,
    source_updated_at TIMESTAMP,
    fetched_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payload_json     VARCHAR
);

CREATE TABLE IF NOT EXISTS snapshots (
    id        BIGINT,
    category  VARCHAR NOT NULL,
    name      VARCHAR NOT NULL,
    taken_at  TIMESTAMP NOT NULL,
    payload   JSON NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snap_lookup ON snapshots(category, name, taken_at);

-- 产业链元数据（V2 新增 · 2026-05-21）
-- 把 system_universe 里的 theme/industry 进一步细化成"产业链/上中下游/具体角色"
-- chain        : 一级链条标签，如 "AI 算力" / "创新药" / "新能源车"
-- chain_tier   : 上中下游分级，如 "上游" / "中游" / "下游"（也可为空）
-- chain_role   : 具体角色，如 "HBM 内存" / "光模块" / "CDMO"
-- layman_intro : 新手能看懂的一句话，供 dashboard 解释 pill
-- source       : "rule_classify"（基于规则自动分类）/ "manual_override"（人工 overrides）
CREATE TABLE IF NOT EXISTS chain_metadata (
    market       VARCHAR NOT NULL,
    symbol       VARCHAR NOT NULL,
    chain        VARCHAR,
    chain_tier   VARCHAR,
    chain_role   VARCHAR,
    layman_intro VARCHAR,
    source       VARCHAR DEFAULT 'rule_classify',
    classified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (market, symbol)
);

CREATE INDEX IF NOT EXISTS idx_chain_meta_chain ON chain_metadata(chain);

-- 因子元数据（V2 新增 · 2026-05-21）
-- 承载 V2 pipeline 之外算出来的基本面/事件型因子（F-Score、PEAD、北向、龙虎榜 等）
-- 设计为"宽表 + JSON details"，方便新增因子不改 schema
--   f_score          : Piotroski F-Score 0..9（A 股 akshare / 美港股 待付费源）
--   value_score      : 估值复合分（v6 美股 GR 残差，与 V2 pipeline 自己算的 valuation 互补）
--   quality_score    : 质量分（ROIC/经营现金流稳定性等）
--   composite_details: JSON dict，存子项明细（流动比率、净利率趋势 等）
--   source           : 计算来源（'akshare_a_share' / 'yfinance_us' / 'fmp' / 'manual'）
--   computed_at      : 计算时间，> 30 天视为 stale
CREATE TABLE IF NOT EXISTS factor_metadata (
    market            VARCHAR NOT NULL,
    symbol            VARCHAR NOT NULL,
    f_score           DOUBLE,
    value_score       DOUBLE,
    quality_score     DOUBLE,
    composite_details JSON,
    source            VARCHAR,
    computed_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (market, symbol)
);

CREATE INDEX IF NOT EXISTS idx_factor_meta_lookup ON factor_metadata(market, symbol);


INSERT INTO schema_meta (key, value, updated_at)
VALUES
  ('schema_version', 'v2', CURRENT_TIMESTAMP),
  ('created_by', 'scripts/tools/init_stock_db_v2.py', CURRENT_TIMESTAMP),
  ('migration_policy', 'clean_start_no_old_data_migration', CURRENT_TIMESTAMP)
ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at;
"""


def _norm_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _infer_market(symbol: str, location: str | None = None) -> str:
    text = f"{symbol} {location or ''}".upper()
    if symbol.endswith(".HK") or "HONG KONG" in text:
        return "HK"
    if symbol.endswith((".SS", ".SZ", ".BJ")) or "CHINA" in text or "SHANGHAI" in text or "SHENZHEN" in text:
        return "CN"
    return "US"


def _seed_universe(conn: duckdb.DuckDBPyConnection) -> int:
    rows: list[dict[str, Any]] = []

    def add(items: list[dict[str, Any]], *, default_source: str) -> None:
        for item in items:
            symbol = _norm_symbol(item.get("ticker") or item.get("code"))
            if not symbol:
                continue
            market = _infer_market(symbol, str(item.get("location") or item.get("market") or ""))
            rows.append({
                "pool_id": "system_tech_universe",
                "pool_name": "系统科技/AI 股票池",
                "market": market,
                "symbol": symbol,
                "raw_symbol": str(item.get("raw_ticker") or symbol.split(".")[0]),
                "name": item.get("name") or symbol,
                "theme": item.get("theme") or item.get("sector") or "",
                "industry": item.get("industry") or item.get("sector") or "",
                "source": item.get("source") or default_source,
            })

    try:
        from scripts.tools.discover_candidates import build_universe as build_dynamic_universe
        discovered = build_dynamic_universe(skip_codes=set())
        if discovered:
            add(discovered, default_source="discover_candidates")
    except Exception as e:
        print(f"  动态 universe 发现失败，稍后使用市场数据源补齐: {e}")

    seen_markets = {row["market"] for row in rows}
    if "US" not in seen_markets or "HK" not in seen_markets or "CN" not in seen_markets:
        from stock_research.core.a_share_universe import fetch_a_share_tech_universe
        from stock_research.core.hk_universe import fetch_hk_tech_universe
        from stock_research.core.us_universe import fetch_us_ai_tech_universe

        if "US" not in seen_markets:
            add(fetch_us_ai_tech_universe(), default_source="us_ai_tech_fallback")
        if "HK" not in seen_markets:
            add(fetch_hk_tech_universe(), default_source="hk_tech_fallback")
        if "CN" not in seen_markets:
            cn_dynamic = fetch_a_share_tech_universe()
            if cn_dynamic:
                add(cn_dynamic, default_source="cn_tech_fallback")
            else:
                print("  ⚠️ A 股动态 universe 为空：不再使用静态种子补齐")

    now = datetime.now()
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["market"], row["symbol"])
        dedup[key] = row
    for row in dedup.values():
        conn.execute(
            """
            INSERT INTO system_universe (
                pool_id, pool_name, market, symbol, raw_symbol, name, theme,
                industry, source, active, first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?, ?)
            ON CONFLICT (pool_id, market, symbol) DO UPDATE SET
                pool_name=excluded.pool_name,
                raw_symbol=excluded.raw_symbol,
                name=excluded.name,
                theme=excluded.theme,
                industry=excluded.industry,
                source=excluded.source,
                active=TRUE,
                last_seen_at=excluded.last_seen_at
            """,
            [
                row["pool_id"], row["pool_name"], row["market"], row["symbol"],
                row["raw_symbol"], row["name"], row["theme"], row["industry"],
                row["source"], now, now,
            ],
        )
        conn.execute(
            """
            INSERT INTO pool_membership (
                pool_id, market, symbol, pool_type, source, active, first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, 'system_tech_universe', ?, TRUE, ?, ?)
            ON CONFLICT (pool_id, market, symbol) DO UPDATE SET
                pool_type='system_tech_universe',
                source=excluded.source,
                active=TRUE,
                last_seen_at=excluded.last_seen_at
            """,
            [row["pool_id"], row["market"], row["symbol"], row["source"], now, now],
        )
    return len(dedup)


def _table_counts(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    tables = [
        "manual_watchlist", "holdings", "system_universe", "pool_membership",
        "price_daily", "recommendation_runs", "recommendation_picks",
        "portfolio_plans", "strategy_review_reports", "pipeline_runs",
    ]
    out: dict[str, int] = {}
    for table in tables:
        out[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a clean StockAssistant v2 DuckDB database.")
    parser.add_argument("--db", default=str(REPO / "stock_history_v2.duckdb"), help="Target DuckDB path.")
    parser.add_argument("--seed-universe", action="store_true", help="Seed system universe from dynamic market discovery and live market universe sources only.")
    parser.add_argument("--force", action="store_true", help="Allow using an existing DB path.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON summary.")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    if db_path.exists() and not args.force:
        print(f"Refusing to touch existing DB without --force: {db_path}", file=sys.stderr)
        return 2
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(str(db_path))
    conn.execute(SCHEMA_SQL)
    seeded = _seed_universe(conn) if args.seed_universe else 0
    counts = _table_counts(conn)
    conn.close()

    summary = {
        "db_path": str(db_path),
        "schema_version": "v2",
        "migration_policy": "clean_start_no_old_data_migration",
        "seeded_universe_rows": seeded,
        "counts": counts,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"Created clean v2 DB: {db_path}")
        print(f"Migration policy: {summary['migration_policy']}")
        print(f"Seeded universe rows: {seeded}")
        for table, count in counts.items():
            print(f"  {table:24s} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
