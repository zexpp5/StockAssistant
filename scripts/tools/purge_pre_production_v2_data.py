#!/usr/bin/env python3
"""Purge pre-production V2 rows from DuckDB.

Rows before the production metrics start date were generated during cutover
drills. They should not remain in the production database because they are easy
to confuse with real model history.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import DB_PATH  # noqa: E402

DEFAULT_CUTOFF = os.environ.get("STOCK_ASSISTANT_METRICS_START_DATE", "2026-05-25")
OUT_PATH = REPO / "data" / "latest" / "pre_production_purge.json"


def _tables(conn: duckdb.DuckDBPyConnection) -> set[str]:
    return {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}


def _count(conn: duckdb.DuckDBPyConnection, table: str, where_sql: str, params: list[Any]) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where_sql}", params).fetchone()[0] or 0)


def _delete(conn: duckdb.DuckDBPyConnection, table: str, where_sql: str, params: list[Any]) -> None:
    conn.execute(f"DELETE FROM {table} WHERE {where_sql}", params)


def _rules(cutoff: date) -> list[tuple[str, str, list[Any], str]]:
    old_rec_runs = "run_id IN (SELECT run_id FROM recommendation_runs WHERE run_date < ?)"
    old_pipeline_runs = (
        "run_id IN (SELECT run_id FROM pipeline_runs "
        "WHERE COALESCE(started_at, planned_at, created_at)::DATE < ?)"
    )
    return [
        ("portfolio_performance", old_rec_runs, [cutoff], "model portfolio performance before production start"),
        ("pick_outcomes", old_rec_runs, [cutoff], "recommendation outcomes before production start"),
        ("portfolio_plans", old_rec_runs, [cutoff], "model portfolio plans before production start"),
        ("recommendation_picks", old_rec_runs, [cutoff], "recommendation picks before production start"),
        ("recommendation_runs", "run_date < ?", [cutoff], "recommendation runs before production start"),
        ("factor_attribution", "period_start < ?", [cutoff], "strategy attribution before production start"),
        ("strategy_review_reports", "period_start < ?", [cutoff], "strategy review reports before production start"),
        ("pipeline_steps", old_pipeline_runs, [cutoff], "pipeline step logs before production start"),
        ("pipeline_runs", "COALESCE(started_at, planned_at, created_at)::DATE < ?", [cutoff], "pipeline runs before production start"),
        ("data_quality_checks", "generated_at::DATE < ?", [cutoff], "quality checks before production start"),
        ("source_fetch_log", "fetched_at::DATE < ?", [cutoff], "source fetch logs before production start"),
        ("snapshots", "CAST(taken_at AS DATE) < ?", [cutoff], "runtime snapshots before production start"),
        (
            "source_raw_snapshots",
            "(business_date < ? OR fetched_at::DATE < ?)",
            [cutoff, cutoff],
            "raw source snapshots before production start",
        ),
        ("financial_statements", "fetched_at::DATE < ?", [cutoff], "financial statement fetches before production start"),
        ("financial_statement_versions", "archived_at::DATE < ?", [cutoff], "archived statement versions before production start"),
        ("price_daily", "trade_date < ?", [cutoff], "price rows before production start"),
    ]


def purge(db_path: Path, cutoff: date, *, execute: bool) -> dict[str, Any]:
    conn = duckdb.connect(str(db_path))
    try:
        tables = _tables(conn)
        rows: list[dict[str, Any]] = []
        for table, where_sql, params, reason in _rules(cutoff):
            if table not in tables:
                rows.append({"table": table, "status": "missing", "matched": 0, "deleted": 0, "reason": reason})
                continue
            matched = _count(conn, table, where_sql, params)
            rows.append({"table": table, "status": "matched", "matched": matched, "deleted": 0, "reason": reason})

        if execute:
            conn.execute("BEGIN TRANSACTION")
            try:
                for row, (table, where_sql, params, _reason) in zip(rows, _rules(cutoff)):
                    if row["status"] != "matched" or int(row["matched"]) <= 0:
                        continue
                    _delete(conn, table, where_sql, params)
                    row["deleted"] = row["matched"]
                    row["status"] = "deleted"
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "db_path": str(db_path),
            "cutoff_date": cutoff.isoformat(),
            "mode": "execute" if execute else "dry_run",
            "total_matched": sum(int(r["matched"]) for r in rows),
            "total_deleted": sum(int(r["deleted"]) for r in rows),
            "rows": rows,
            "policy": (
                "Delete V2 fact/artifact rows dated before the production start. "
                "Static metadata tables such as system_universe, pool_membership, "
                "chain_metadata, manual_watchlist, holdings, schema_meta and user_config are untouched."
            ),
        }
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return payload
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Purge pre-production V2 DuckDB rows.")
    parser.add_argument("--db", default=os.environ.get("STOCK_DB_PATH") or str(DB_PATH))
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    parser.add_argument("--execute", action="store_true", help="Actually delete rows. Without this flag the script is a dry run.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cutoff = date.fromisoformat(str(args.cutoff)[:10])
    payload = purge(Path(args.db).expanduser().resolve(), cutoff, execute=args.execute)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Pre-production purge: {payload['mode']} cutoff={payload['cutoff_date']}")
        print(f"  matched={payload['total_matched']} deleted={payload['total_deleted']}")
        for row in payload["rows"]:
            if row["matched"] or row["deleted"]:
                print(f"  {row['table']}: matched={row['matched']} deleted={row['deleted']}")
        print(f"  JSON: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
