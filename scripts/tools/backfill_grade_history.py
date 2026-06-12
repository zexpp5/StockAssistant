#!/usr/bin/env python3
"""分析师评级变动历史回填（一次性，SHADOW_RESEARCH_ONLY）。

主源 yfinance Ticker.upgrades_downgrades:每只全量评级事件史(NVDA ~986 条
回溯到 2016),带日期 PIT 正确,零限额,全宇宙 ~4 分钟。
(曾试 FMP /grades:数据等价但历史端点限额死循环 — 9s/10s/15s 间隔都 402,
 2026-06-12 实测放弃,见 memory: fmp_historical_quota_limit。)

是「盈利预期上修」的长历史近亲,给 validate_grade_ic.py 提供截面 IC 原料。
表 analyst_grade_events 每次全量重建(单一来源,避免 FMP 残留混源)。
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
CREATE TABLE IF NOT EXISTS analyst_grade_events (
    market VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    event_date DATE NOT NULL,
    grading_company VARCHAR NOT NULL,
    previous_grade VARCHAR,
    new_grade VARCHAR NOT NULL,
    action VARCHAR,
    price_target_action VARCHAR,
    price_target DOUBLE,
    prior_price_target DOUBLE,
    source VARCHAR DEFAULT 'yfinance/upgrades_downgrades',
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (market, symbol, event_date, grading_company, new_grade)
)
"""

# yfinance Action → 归一化(validate_grade_ic 只认 upgrade/downgrade)
ACTION_MAP = {"up": "upgrade", "down": "downgrade", "init": "initiate",
              "main": "maintain", "reit": "reiterate"}


def fetch_symbol(symbol: str) -> list[dict]:
    import yfinance as yf

    try:
        df = yf.Ticker(symbol).upgrades_downgrades
    except Exception:
        return []
    if df is None or getattr(df, "empty", True):
        return []
    rows = []
    for ts, r in df.iterrows():
        firm = str(r.get("Firm") or "unknown").strip() or "unknown"
        new_grade = str(r.get("ToGrade") or "").strip()
        if not new_grade:
            continue
        action_raw = str(r.get("Action") or "").strip().lower()

        def _f(key: str) -> float | None:
            try:
                v = float(r.get(key))
                return v if v == v and v > 0 else None
            except Exception:
                return None

        rows.append({
            "event_date": ts.date() if hasattr(ts, "date") else ts,
            "grading_company": firm,
            "previous_grade": str(r.get("FromGrade") or "").strip() or None,
            "new_grade": new_grade,
            "action": ACTION_MAP.get(action_raw, action_raw or None),
            "price_target_action": str(r.get("priceTargetAction") or "").strip().lower() or None,
            "price_target": _f("currentPriceTarget"),
            "prior_price_target": _f("priorPriceTarget"),
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
                print(f"[{i+1}/{len(symbols)}] 进度: {symbol}, 累计 {len(all_events)} 行", flush=True)
        time.sleep(args.sleep_sec)

    # 写入短窗:全量重建(单一来源),撞锁重试等瞬态写入器走人
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
        conn.execute("DROP TABLE IF EXISTS analyst_grade_events")
        conn.execute(TABLE_DDL)
        for symbol, ev in all_events:
            conn.execute(
                """
                INSERT OR REPLACE INTO analyst_grade_events
                (market, symbol, event_date, grading_company, previous_grade,
                 new_grade, action, price_target_action, price_target,
                 prior_price_target, source, fetched_at)
                VALUES ('US', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'yfinance/upgrades_downgrades', ?)
                """,
                [symbol, ev["event_date"], ev["grading_company"], ev["previous_grade"],
                 ev["new_grade"], ev["action"], ev["price_target_action"],
                 ev["price_target"], ev["prior_price_target"], datetime.now()],
            )
            inserted += 1
        span = conn.execute(
            "SELECT min(event_date), max(event_date), count(*) FROM analyst_grade_events"
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
