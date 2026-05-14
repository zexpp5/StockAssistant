#!/usr/bin/env python3
"""Remove accidental universe-seeded rows from the user watchlist.

The watchlist is user-curated state.  Broad universes belong in discovery /
AI recommendation surfaces, not in watchlist.  This tool removes rows whose
source starts with "universe:" and also removes derived picks/reviews for those
same codes so the dashboard does not keep showing stale self-selected picks.

Default is dry-run.  Use --yes to execute; the DuckDB file is backed up first.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import DB_PATH, get_db  # noqa: E402


AUTO_WHERE = """
    COALESCE(source, '') LIKE 'universe:%'
    OR COALESCE(notes, '') LIKE '%bootstrap_watchlist_from_universes.py inserted%'
"""


def _backup_db() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = REPO / "data" / "reset_backups" / f"cleanup_auto_watchlist_seeds_{ts}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / Path(DB_PATH).name
    shutil.copy2(DB_PATH, backup_path)
    return backup_path


def run(execute: bool) -> dict:
    conn = get_db()
    rows = conn.execute(
        f"SELECT code, name, market, source FROM watchlist WHERE {AUTO_WHERE} ORDER BY code"
    ).fetchall()
    codes = [r[0] for r in rows]
    before = {
        "watchlist": conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0],
        "picks": conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0],
        "reviews": conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
    }
    derived = {"picks": 0, "reviews": 0}
    if codes:
        derived["picks"] = conn.execute(
            "SELECT COUNT(*) FROM picks WHERE code IN (SELECT code FROM watchlist WHERE " + AUTO_WHERE + ")"
        ).fetchone()[0]
        derived["reviews"] = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE code IN (SELECT code FROM watchlist WHERE " + AUTO_WHERE + ")"
        ).fetchone()[0]

    backup = None
    if execute and codes:
        backup = _backup_db()
        conn.execute("CREATE TEMP TABLE auto_seed_codes AS SELECT code FROM watchlist WHERE " + AUTO_WHERE)
        conn.execute("DELETE FROM picks WHERE code IN (SELECT code FROM auto_seed_codes)")
        conn.execute("DELETE FROM reviews WHERE code IN (SELECT code FROM auto_seed_codes)")
        conn.execute("DELETE FROM watchlist WHERE code IN (SELECT code FROM auto_seed_codes)")

    after = {
        "watchlist": conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0],
        "picks": conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0],
        "reviews": conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
    }
    conn.close()
    return {
        "executed": execute,
        "auto_watchlist_rows": len(codes),
        "derived_rows": derived,
        "before": before,
        "after": after,
        "backup": str(backup.relative_to(REPO)) if backup else None,
        "sample": rows[:20],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="execute cleanup; default is dry-run")
    args = parser.parse_args()

    result = run(args.yes)
    print("cleanup_auto_watchlist_seeds:", "EXECUTED" if result["executed"] else "DRY-RUN")
    print(f"  auto watchlist rows: {result['auto_watchlist_rows']}")
    print(f"  derived rows: {result['derived_rows']}")
    print(f"  before: {result['before']}")
    print(f"  after:  {result['after']}")
    if result["backup"]:
        print(f"  backup: {result['backup']}")
    if not result["executed"]:
        print("  sample:")
        for code, name, market, source in result["sample"]:
            print(f"    {code:>10} {name} · {market} · {source}")
        print("  No data changed. Re-run with --yes to execute.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
