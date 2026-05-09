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

import os
from datetime import datetime, date
from typing import Iterable, Mapping, Any

import duckdb

DB_PATH = os.environ.get(
    "STOCK_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_history.duckdb"),
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
    PRIMARY KEY (pick_date, code)
);

CREATE TABLE IF NOT EXISTS reviews (
    review_date    DATE      NOT NULL,
    pick_date      DATE      NOT NULL,
    code           VARCHAR   NOT NULL,
    name           VARCHAR,
    entry_price    DOUBLE,
    current_price  DOUBLE,
    pct            DOUBLE,
    days_held      INTEGER,
    grade          VARCHAR,
    rating         VARCHAR,
    theme          VARCHAR,
    PRIMARY KEY (review_date, pick_date, code)
);
"""


def get_db(path: str = DB_PATH) -> duckdb.DuckDBPyConnection:
    """打开 DuckDB 连接并保证 schema 存在。"""
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
]


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
