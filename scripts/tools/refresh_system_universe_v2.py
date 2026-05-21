#!/usr/bin/env python3
"""Refresh the active V2 system universe from live/code-defined sources.

This is not a migration tool. It only refreshes `system_universe` and
`pool_membership` from the same V2 source path used by clean DB initialization.
It never reads legacy DuckDB rows as an input and never writes manual watchlist.
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

from stock_db import DB_PATH  # noqa: E402
from scripts.tools.init_stock_db_v2 import SCHEMA_SQL, _seed_universe  # noqa: E402


def _active_counts(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT market, COUNT(*) AS n
        FROM system_universe
        WHERE active = TRUE
        GROUP BY market
        ORDER BY market
        """
    ).fetchall()
    by_market = {str(market): int(n or 0) for market, n in rows}
    return {"by_market": by_market, "total": sum(by_market.values())}


def _deactivate_missing_current_refresh(
    conn: duckdb.DuckDBPyConnection,
    refresh_started_at: datetime,
) -> int:
    """Deactivate rows in markets that were successfully refreshed this run."""
    markets = [
        str(r[0])
        for r in conn.execute(
            """
            SELECT DISTINCT market
            FROM system_universe
            WHERE pool_id = 'system_tech_universe'
              AND last_seen_at >= ?
            """,
            [refresh_started_at],
        ).fetchall()
        if r[0]
    ]
    if not markets:
        return 0
    placeholders = ", ".join(["?"] * len(markets))
    params = [refresh_started_at, *markets]
    stale = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM system_universe
            WHERE pool_id = 'system_tech_universe'
              AND active = TRUE
              AND last_seen_at < ?
              AND market IN ({placeholders})
            """,
            params,
        ).fetchone()[0]
        or 0
    )
    conn.execute(
        f"""
        UPDATE system_universe
        SET active = FALSE
        WHERE pool_id = 'system_tech_universe'
          AND last_seen_at < ?
          AND market IN ({placeholders})
        """,
        params,
    )
    conn.execute(
        f"""
        UPDATE pool_membership
        SET active = FALSE
        WHERE pool_id = 'system_tech_universe'
          AND pool_type = 'system_tech_universe'
          AND last_seen_at < ?
          AND market IN ({placeholders})
        """,
        params,
    )
    return stale


def _record_fetch_log(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    fetched_at: datetime,
    summary: dict[str, Any],
) -> None:
    tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
    if "source_fetch_log" not in tables:
        return
    status = "success" if int(summary["after"]["total"] or 0) > 0 else "error"
    conn.execute(
        """
        INSERT INTO source_fetch_log (
            run_id, source, market, status, status_code, fallback_source,
            fetched_at, message
        )
        VALUES (?, 'system_universe_refresh', 'ALL', ?, ?, NULL, ?, ?)
        """,
        [
            run_id,
            status,
            "ok" if status == "success" else "error",
            fetched_at,
            (
                f"active system_universe {summary['before']['total']} → {summary['after']['total']}；"
                f"seeded_current={summary['seeded_current']}；deactivated={summary['deactivated']}"
            ),
        ],
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db or DB_PATH).expanduser().resolve()
    conn = duckdb.connect(str(db_path))
    refresh_started_at = datetime.now()
    run_id = f"universe_refresh_{refresh_started_at.strftime('%Y%m%d_%H%M%S')}"
    try:
        conn.execute(SCHEMA_SQL)
        before = _active_counts(conn)
        seeded = _seed_universe(conn)
        deactivated = 0
        if not args.keep_stale:
            deactivated = _deactivate_missing_current_refresh(conn, refresh_started_at)
        after = _active_counts(conn)
        summary = {
            "run_id": run_id,
            "db_path": str(db_path),
            "started_at": refresh_started_at.isoformat(timespec="seconds"),
            "migration_policy": "clean_start_no_old_data_migration",
            "before": before,
            "after": after,
            "seeded_current": seeded,
            "deactivated": deactivated,
            "manual_watchlist_count": int(conn.execute("SELECT COUNT(*) FROM manual_watchlist").fetchone()[0]),
            # 2026-05-21 V1 cutover：旧 watchlist 表已 DROP，count 字段保留兼容下游
            "watchlist_count": 0,
        }
        _record_fetch_log(conn, run_id, datetime.now(), summary)
        return summary
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh V2 system universe from live/code-defined sources.")
    parser.add_argument("--db", default=os.environ.get("STOCK_DB_PATH") or str(DB_PATH))
    parser.add_argument("--keep-stale", action="store_true", help="Do not deactivate rows missing from successfully refreshed markets.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    summary = run(args)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"V2 system universe refresh: {summary['before']['total']} → {summary['after']['total']}")
        print(f"  by_market={summary['after']['by_market']} deactivated={summary['deactivated']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
