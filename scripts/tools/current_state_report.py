#!/usr/bin/env python3
"""Print current StockAssistant data state.

This replaces static row counts in product docs. It reads the configured
DuckDB path and selected latest artifacts, then prints or emits JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from stock_research import config  # noqa: E402

LEGACY_TABLES = [
    "watchlist", "prices", "picks", "reviews", "discovery_history",
    "discovery_tracking", "earnings_history", "snapshots",
]

V2_TABLES = [
    "manual_watchlist", "holdings", "system_universe", "pool_membership",
    "price_daily", "financial_statements", "recommendation_runs",
    "recommendation_picks", "portfolio_plans", "pick_outcomes",
    "portfolio_performance", "factor_attribution", "strategy_versions",
    "strategy_review_reports", "pipeline_runs", "pipeline_steps",
    "source_fetch_log", "data_quality_checks", "source_raw_snapshots",
]

ARTIFACTS = [
    "data/latest/pipeline_status.json",
    "data/latest/recommendation_quality_gate.json",
    "data/latest/production_acceptance_check.json",
    "data/latest/recommendation_evidence.json",
    "data/latest/source_health.json",
    "data/discovery_candidates.json",
]


def _table_counts(conn: duckdb.DuckDBPyConnection, tables: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    existing = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    for table in tables:
        if table not in existing:
            out[table] = {"exists": False}
            continue
        try:
            out[table] = {
                "exists": True,
                "rows": int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]),
            }
        except Exception as e:
            out[table] = {"exists": True, "error": str(e)}
    return out


def _recent_runtime(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    existing = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    out: dict[str, Any] = {"source_fetch_log": [], "pipeline_steps": []}
    if "source_fetch_log" in existing:
        rows = conn.execute(
            """
            SELECT source, market, status, status_code, fetched_at, message
            FROM source_fetch_log
            ORDER BY fetched_at DESC
            LIMIT 8
            """
        ).fetchall()
        out["source_fetch_log"] = [
            {
                "source": r[0],
                "market": r[1],
                "status": r[2],
                "status_code": r[3],
                "fetched_at": str(r[4]) if r[4] is not None else None,
                "message": r[5],
            }
            for r in rows
        ]
    if "pipeline_steps" in existing:
        rows = conn.execute(
            """
            SELECT run_id, step_name, status, ended_at, sink, error_summary
            FROM pipeline_steps
            ORDER BY COALESCE(ended_at, started_at) DESC
            LIMIT 8
            """
        ).fetchall()
        out["pipeline_steps"] = [
            {
                "run_id": r[0],
                "step_name": r[1],
                "status": r[2],
                "ended_at": str(r[3]) if r[3] is not None else None,
                "sink": r[4],
                "error_summary": r[5],
            }
            for r in rows
        ]
    return out


def _artifact_status() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for rel in ARTIFACTS:
        path = REPO / rel
        item: dict[str, Any] = {"exists": path.exists()}
        if path.exists():
            item["mtime"] = path.stat().st_mtime
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                item["valid_json"] = True
                if isinstance(payload, dict):
                    for key in ("status", "generated_at", "updated_at", "run_id", "universe_scope", "universe_size"):
                        if key in payload:
                            item[key] = payload.get(key)
                    if rel.endswith("source_health.json") and "markets" in payload:
                        item["markets"] = payload.get("markets")
            except Exception as e:
                item["valid_json"] = False
                item["error"] = str(e)
        out[rel] = item
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Report current DB and latest artifact state.")
    parser.add_argument("--db", default=str(config.DUCKDB_PATH), help="DuckDB path; defaults to STOCK_DB_PATH or stock_history_v2.duckdb.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    report: dict[str, Any] = {
        "db_path": str(db_path),
        "stock_db_path_env": os.environ.get("STOCK_DB_PATH"),
        "db_exists": db_path.exists(),
        "db_status": "ok" if db_path.exists() else "missing",
        "legacy_tables": {},
        "v2_tables": {},
        "runtime": {},
        "artifacts": _artifact_status(),
    }

    if db_path.exists():
        try:
            conn = duckdb.connect(str(db_path), read_only=True)
            report["legacy_tables"] = _table_counts(conn, LEGACY_TABLES)
            report["v2_tables"] = _table_counts(conn, V2_TABLES)
            report["runtime"] = _recent_runtime(conn)
            conn.close()
        except duckdb.IOException as e:
            message = str(e)
            report["db_status"] = "duckdb_locked" if "Conflicting lock" in message or "Could not set lock" in message else "duckdb_error"
            report["db_error"] = message
        except Exception as e:
            report["db_status"] = "duckdb_error"
            report["db_error"] = str(e)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"DB: {db_path}")
        print(f"STOCK_DB_PATH: {os.environ.get('STOCK_DB_PATH') or '(unset)'}")
        print(f"Exists: {report['db_exists']}")
        print(f"DB status: {report['db_status']}")
        if report.get("db_error"):
            print(f"DB error: {report['db_error']}")
        print("\nLegacy tables:")
        for table, item in report["legacy_tables"].items():
            text = item.get("rows") if item.get("exists") else "missing"
            print(f"  {table:22s} {text}")
        print("\nV2 tables:")
        for table, item in report["v2_tables"].items():
            text = item.get("rows") if item.get("exists") else "missing"
            print(f"  {table:22s} {text}")
        print("\nArtifacts:")
        for rel, item in report["artifacts"].items():
            text = "ok" if item.get("exists") and item.get("valid_json", True) else ("missing" if not item.get("exists") else "invalid")
            extra = item.get("status") or item.get("run_id") or item.get("universe_scope") or ""
            if item.get("markets"):
                market_bits = []
                for mk, mk_item in item["markets"].items():
                    market_bits.append(f"{mk}:{mk_item.get('success', 0)}/{mk_item.get('total', 0)}")
                if market_bits:
                    extra = f"{extra} {' '.join(market_bits)}".strip()
            print(f"  {rel:52s} {text} {extra}")
        if report.get("runtime"):
            print("\nRecent source fetch:")
            for item in report["runtime"].get("source_fetch_log", []):
                print(
                    f"  {item.get('fetched_at') or '—'} "
                    f"{item.get('source')}/{item.get('market')} "
                    f"{item.get('status')} {item.get('message') or ''}"
                )
            print("\nRecent pipeline steps:")
            for item in report["runtime"].get("pipeline_steps", []):
                print(
                    f"  {item.get('ended_at') or '—'} "
                    f"{item.get('step_name')} {item.get('status')} -> {item.get('sink') or '—'}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
