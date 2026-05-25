#!/usr/bin/env python3
"""Ingest benchmark index closes (SPY / ^HSI / 000300.SS) into price_daily.

evaluate_v2_picks 算 alpha 时需要每个 market 的同窗口 benchmark close。原实现
在线走 yfinance，跑批环境 import / 网络失败就静默吞，导致 alpha_pct 全 NULL。
本脚本把基准提前灌进 price_daily（market=US/HK/CN, symbol=SPY/^HSI/000300.SS），
evaluate 只读本地。

调度：daily_refresh.sh 在 evaluate_v2_picks (step 23a) 之前跑一次。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import duckdb  # noqa: E402

BENCHMARKS = [
    # (market, symbol stored in price_daily, currency)
    ("US", "SPY", "USD"),
    ("HK", "^HSI", "HKD"),
    ("CN", "000300.SS", "CNY"),
]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", help="YYYY-MM-DD; default: today - 90 days")
    p.add_argument("--end", help="YYYY-MM-DD (exclusive); default: today + 2 days")
    args = p.parse_args()

    today = date.today()
    start = date.fromisoformat(args.start) if args.start else today - timedelta(days=90)
    end = date.fromisoformat(args.end) if args.end else today + timedelta(days=2)

    db_path = os.environ.get("STOCK_DB_PATH") or str(REPO / "stock_history_v2.duckdb")
    print(f"ingest_benchmark_prices: window={start}..{end} db={db_path}")

    try:
        import yfinance as yf
    except ImportError:
        print("  ✗ yfinance not installed — abort (evaluate_v2_picks 将继续走 yfinance fallback)")
        return 1

    con = duckdb.connect(db_path)
    tables = {str(r[0]) for r in con.execute("SHOW TABLES").fetchall()}
    if "price_daily" not in tables:
        con.close()
        print("  ✗ price_daily 表不存在")
        return 1

    now = datetime.now()
    total_written = 0
    for market, symbol, currency in BENCHMARKS:
        try:
            df = yf.Ticker(symbol).history(start=start, end=end, auto_adjust=False)
        except Exception as e:
            print(f"  ⚠️ {market}/{symbol}: yfinance error {e}")
            continue
        if df.empty:
            print(f"  ⚠️ {market}/{symbol}: empty range")
            continue

        rows = []
        for ts, row in df.iterrows():
            d = ts.date() if hasattr(ts, "date") else ts
            close = float(row["Close"])
            rows.append((market, symbol, d, "1d", close, currency, "yfinance_benchmark", now, now))

        con.executemany(
            "DELETE FROM price_daily WHERE market=? AND symbol=? AND trade_date=? AND interval=?",
            [(r[0], r[1], r[2], r[3]) for r in rows],
        )
        con.executemany(
            """
            INSERT INTO price_daily
              (market, symbol, trade_date, interval, close, currency,
               source, source_updated_at, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        total_written += len(rows)
        print(f"  ✓ {market}/{symbol}: wrote {len(rows)} rows ({rows[0][2]}..{rows[-1][2]})")

    con.close()
    print(f"ingest_benchmark_prices: total_written={total_written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
