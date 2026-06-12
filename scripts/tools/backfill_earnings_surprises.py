#!/usr/bin/env python3
"""财报惊喜(EPS surprise)历史回填（一次性，SHADOW_RESEARCH_ONLY）。

主源 yfinance Ticker.get_earnings_dates:每季「实际 vs 当时预期」EPS,
公布当天定格(PIT),零限额,~24 个季度史。(FMP /earnings 数据等价但
历史端点限额死循环,2026-06-12 实测放弃,同 backfill_grade_history。)

PEAD(业绩公告漂移)因子的原料;A 股打分已有 pead 因子,美股一直缺。
表 earnings_surprise_events 每次全量重建(单一来源)。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import get_db  # noqa: E402

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS earnings_surprise_events (
    market VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    announce_date DATE NOT NULL,
    eps_actual DOUBLE,
    eps_estimated DOUBLE,
    surprise_pct DOUBLE,
    source VARCHAR DEFAULT 'yfinance/earnings_dates',
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (market, symbol, announce_date)
)
"""


def fetch_symbol(symbol: str) -> list[dict]:
    import yfinance as yf

    try:
        df = yf.Ticker(symbol).get_earnings_dates(limit=24)
    except Exception:
        return []
    if df is None or getattr(df, "empty", True):
        return []
    rows = []
    for ts, r in df.iterrows():
        try:
            actual = float(r.get("Reported EPS"))
            estimated = float(r.get("EPS Estimate"))
        except Exception:
            continue
        if actual != actual or estimated != estimated:  # NaN(未来场次或缺数)
            continue
        surprise = r.get("Surprise(%)")
        try:
            surprise = float(surprise)
            if surprise != surprise:
                surprise = None
        except Exception:
            surprise = None
        rows.append({
            "announce_date": ts.date() if hasattr(ts, "date") else ts,
            "eps_actual": actual,
            "eps_estimated": estimated,
            "surprise_pct": surprise,
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sleep-sec", type=float, default=0.3)
    parser.add_argument("--limit", type=int, default=0, help=">0 只回填前 N 只(冒烟)")
    args = parser.parse_args()

    conn = get_db(force_read_only=True)
    try:
        symbols = [str(r[0]) for r in conn.execute(
            "SELECT DISTINCT symbol FROM system_universe WHERE market='US' AND active ORDER BY symbol"
        ).fetchall()]
    finally:
        conn.close()
    if args.limit > 0:
        symbols = symbols[: args.limit]

    fetched = failed = 0
    all_events: list[tuple[str, dict]] = []
    for i, symbol in enumerate(symbols):
        rows = fetch_symbol(symbol)
        if not rows:
            failed += 1
            print(f"[{i+1}/{len(symbols)}] {symbol}: 无数据", flush=True)
        else:
            fetched += 1
            all_events.extend((symbol, ev) for ev in rows)
            if (i + 1) % 25 == 0:
                print(f"[{i+1}/{len(symbols)}] 进度: {symbol}, 累计 {len(all_events)} 事件", flush=True)
        time.sleep(args.sleep_sec)

    conn = None
    for attempt in range(10):
        try:
            conn = get_db()
            break
        except Exception as exc:
            print(f"写锁被占(第 {attempt+1} 次): {str(exc)[:120]},60s 后重试", flush=True)
            time.sleep(60)
    if conn is None:
        print(json.dumps({"status": "FETCHED_NOT_WRITTEN", "fetched": fetched}, ensure_ascii=False))
        return 1
    inserted = 0
    try:
        conn.execute("DROP TABLE IF EXISTS earnings_surprise_events")
        conn.execute(TABLE_DDL)
        for symbol, ev in all_events:
            conn.execute(
                """
                INSERT OR REPLACE INTO earnings_surprise_events
                (market, symbol, announce_date, eps_actual, eps_estimated,
                 surprise_pct, source, fetched_at)
                VALUES ('US', ?, ?, ?, ?, ?, 'yfinance/earnings_dates', ?)
                """,
                [symbol, ev["announce_date"], ev["eps_actual"], ev["eps_estimated"],
                 ev["surprise_pct"], datetime.now()],
            )
            inserted += 1
        span = conn.execute(
            "SELECT min(announce_date), max(announce_date), count(*) FROM earnings_surprise_events"
        ).fetchone()
    finally:
        conn.close()

    print(json.dumps({
        "status": "OK", "symbols": len(symbols), "fetched": fetched, "failed": failed,
        "rows_inserted": inserted,
        "table_span": [str(span[0]), str(span[1])], "table_rows": span[2],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
