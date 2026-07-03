#!/usr/bin/env python3
"""Scan and repair obvious price_daily daily-drop anomalies.

This is intentionally a narrow, auditable repair for corporate-action /
bad-prev-close rows such as HON 2026-06-29. It never deletes or reloads
price_daily. In --apply mode it only updates the exact primary-key row
(prev_close, one_week_pct, source_updated_at) when:

1. close / prev_close shows a single-day drop <= threshold (default -40%);
2. the previous DB trading row has a materially different close from prev_close;
3. using that previous close removes the extreme drop.

Rows that do not satisfy those conditions stay untouched and are reported as
unresolved / likely true drops for manual review.
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

from stock_research import config  # noqa: E402

REPORT_PATH = REPO / "data" / "latest" / "price_anomaly_scan.json"


def _pct(close: float | None, base: float | None) -> float | None:
    if close is None or base is None or base == 0:
        return None
    return (float(close) / float(base) - 1.0) * 100.0


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _prior_closes(conn: duckdb.DuckDBPyConnection, market: str, symbol: str, interval: str, trade_date: Any) -> list[tuple[Any, float]]:
    return conn.execute(
        """
        SELECT trade_date, close
        FROM price_daily
        WHERE market = ?
          AND symbol = ?
          AND interval = ?
          AND trade_date < ?
          AND close IS NOT NULL
        ORDER BY trade_date DESC
        LIMIT 5
        """,
        [market, symbol, interval, trade_date],
    ).fetchall()


def scan(conn: duckdb.DuckDBPyConnection, *, threshold_pct: float, source_prefix: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT market, symbol, trade_date, interval, close, prev_close, one_week_pct, source
        FROM price_daily
        WHERE close IS NOT NULL
          AND prev_close IS NOT NULL
          AND prev_close > 0
          AND ((close / prev_close) - 1.0) * 100.0 <= ?
        ORDER BY market, symbol, trade_date
        """,
        [threshold_pct],
    ).fetchall()

    anomalies: list[dict[str, Any]] = []
    for market, symbol, trade_date, interval, close, prev_close, one_week_pct, source in rows:
        priors = _prior_closes(conn, market, symbol, interval or "1d", trade_date)
        previous_date = priors[0][0] if priors else None
        previous_close = float(priors[0][1]) if priors else None
        week_base_date = priors[4][0] if len(priors) >= 5 else None
        week_base_close = float(priors[4][1]) if len(priors) >= 5 else None
        old_daily_pct = _pct(close, prev_close)
        new_daily_pct = _pct(close, previous_close)
        new_one_week_pct = _pct(close, week_base_close)
        source_ok = str(source or "").startswith(source_prefix)
        prev_mismatch = (
            previous_close is not None
            and prev_close
            and abs(float(prev_close) - previous_close) / max(abs(previous_close), 1e-9) >= 0.20
        )
        repaired_drop = new_daily_pct is not None and new_daily_pct > threshold_pct / 2.0
        repairable = bool(source_ok and prev_mismatch and repaired_drop)
        status = "repairable_bad_prev_close" if repairable else "true_or_unresolved_drop"
        anomalies.append({
            "market": market,
            "symbol": symbol,
            "trade_date": str(trade_date),
            "interval": interval or "1d",
            "source": source,
            "source_prefix_required": source_prefix,
            "close": _round(close),
            "old_prev_close": _round(prev_close),
            "db_previous_trade_date": str(previous_date) if previous_date else None,
            "db_previous_close": _round(previous_close),
            "old_daily_pct": _round(old_daily_pct, 2),
            "new_daily_pct": _round(new_daily_pct, 2),
            "old_one_week_pct": _round(one_week_pct, 2),
            "week_base_trade_date": str(week_base_date) if week_base_date else None,
            "week_base_close": _round(week_base_close),
            "new_one_week_pct": _round(new_one_week_pct, 2),
            "status": status,
            "applied": False,
        })
    return anomalies


def apply_repairs(conn: duckdb.DuckDBPyConnection, anomalies: list[dict[str, Any]]) -> int:
    applied = 0
    for row in anomalies:
        if row.get("status") != "repairable_bad_prev_close":
            continue
        conn.execute(
            """
            UPDATE price_daily
            SET prev_close = ?,
                one_week_pct = ?,
                source_updated_at = CURRENT_TIMESTAMP
            WHERE market = ?
              AND symbol = ?
              AND trade_date = ?
              AND interval = ?
            """,
            [
                row["db_previous_close"],
                row["new_one_week_pct"],
                row["market"],
                row["symbol"],
                row["trade_date"],
                row["interval"],
            ],
        )
        row["status"] = "fixed_bad_prev_close"
        row["applied"] = True
        applied += 1
    return applied


def write_report(payload: dict[str, Any], path: Path = REPORT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan / repair price_daily single-day crash anomalies.")
    parser.add_argument("--db", default=os.environ.get("STOCK_DB_PATH") or str(config.DUCKDB_PATH))
    parser.add_argument("--threshold-pct", type=float, default=-40.0)
    parser.add_argument("--source-prefix", default="yfinance", help="Only repair rows whose source starts with this prefix.")
    parser.add_argument("--apply", action="store_true", help="Apply repairable row updates. Default is scan-only.")
    args = parser.parse_args()

    conn = duckdb.connect(str(args.db), read_only=not args.apply)
    try:
        anomalies = scan(conn, threshold_pct=args.threshold_pct, source_prefix=args.source_prefix)
        applied = apply_repairs(conn, anomalies) if args.apply else 0
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "db": str(args.db),
            "threshold_pct": args.threshold_pct,
            "source_prefix": args.source_prefix,
            "applied": bool(args.apply),
            "applied_count": applied,
            "anomaly_count": len(anomalies),
            "status_counts": {
                status: sum(1 for row in anomalies if row.get("status") == status)
                for status in sorted({str(row.get("status")) for row in anomalies})
            },
            "anomalies": anomalies,
        }
        write_report(payload)
        print(json.dumps({
            "anomaly_count": len(anomalies),
            "applied": bool(args.apply),
            "applied_count": applied,
            "status_counts": payload["status_counts"],
            "report": str(REPORT_PATH),
        }, ensure_ascii=False))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
