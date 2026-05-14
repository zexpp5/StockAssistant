"""Reset derived data for a clean proof run.

Default behavior is safe:
  - dry-run unless --yes is passed
  - backs up the current DuckDB and generated artifacts first
  - preserves watchlist, user_config, holdings, model/static data, and docs

The goal is to restart the evidence trail from clean, correct data without
losing the manually curated stock universe.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import DB_PATH, get_db  # noqa: E402

DERIVED_TABLES = [
    "prices",
    "picks",
    "reviews",
    "discovery_history",
    "discovery_tracking",
    "earnings_history",
    "snapshots",
]

DERIVED_PATHS = [
    "data/latest",
    "data/reports",
    "data/snapshots",
    "data/a_share_picks.json",
    "data/discovery_candidates.json",
    "data/event_calendar.json",
    "data/ipo_calendar.json",
    "data/policy_events.json",
    "data/defense_watcher.log",
    "data/defense_watcher_state.json",
    "data/walk_forward_2025-05_to_2026-05.json",
    "morning_brief.md",
    "stock_dashboard.html",
]

CACHE_PATHS = [
    "factor_scores_today.json",
    "data/cache",
]

STATIC_DATA_PATHS = [
    "data/factor_weights.json",
    "data/calibrated_factor_weights.json",
    "data/stock_chain_overrides.json",
]


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
        [table],
    ).fetchone()
    return bool(row and row[0])


def _row_count(conn, table: str) -> int | None:
    if not _table_exists(conn, table):
        return None
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _move_to_backup(path: Path, backup_dir: Path, execute: bool) -> dict:
    rel = path.relative_to(REPO)
    item = {"path": str(rel), "exists": path.exists(), "action": "move_to_backup"}
    if not path.exists():
        item["action"] = "skip_missing"
        return item
    dest = backup_dir / "files" / rel
    item["backup_path"] = str(dest.relative_to(REPO))
    if execute:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
    return item


def run(args: argparse.Namespace) -> dict:
    execute = bool(args.yes)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = REPO / "data" / "reset_backups" / ts

    conn = get_db()
    tables = list(DERIVED_TABLES)
    if args.include_holdings:
        tables.append("holdings")
    if args.include_watchlist:
        tables.append("watchlist")
    if args.include_user_config:
        tables.append("user_config")

    before = {t: _row_count(conn, t) for t in tables}
    backup_info: dict = {"backup_dir": str(backup_dir.relative_to(REPO)), "db": None}

    if execute:
        backup_dir.mkdir(parents=True, exist_ok=True)
        db_path = Path(DB_PATH)
        if db_path.exists():
            db_backup = backup_dir / db_path.name
            shutil.copy2(db_path, db_backup)
            backup_info["db"] = str(db_backup.relative_to(REPO))

    table_actions = []
    for table in tables:
        if not _table_exists(conn, table):
            table_actions.append({"table": table, "action": "skip_missing"})
            continue
        table_actions.append({"table": table, "action": "delete_all", "rows_before": before.get(table)})
        if execute:
            conn.execute(f"DELETE FROM {table}")
    after = {t: _row_count(conn, t) for t in tables}
    conn.close()

    paths = list(DERIVED_PATHS)
    if args.include_cache:
        paths += CACHE_PATHS
    if args.include_static_data:
        paths += STATIC_DATA_PATHS
    file_actions = [_move_to_backup(REPO / p, backup_dir, execute) for p in paths]

    if execute:
        (REPO / "data").mkdir(exist_ok=True)
        (REPO / "data" / "latest").mkdir(parents=True, exist_ok=True)
        (REPO / "data" / "reports").mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at": datetime.now().isoformat(),
        "executed": execute,
        "backup": backup_info,
        "preserved_by_default": [
            "watchlist unless --include-watchlist",
            "user_config unless --include-user-config",
            "holdings unless --include-holdings",
            "static model data unless --include-static-data",
            "docs",
        ],
        "tables_before": before,
        "tables_after": after,
        "table_actions": table_actions,
        "file_actions": file_actions,
    }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset StockAssistant derived data with backup.")
    parser.add_argument("--yes", action="store_true", help="Execute reset. Without this, only dry-run.")
    parser.add_argument("--include-cache", action="store_true", help="Also clear factor/cache files.")
    parser.add_argument("--include-holdings", action="store_true", help="Also clear manually entered holdings.")
    parser.add_argument("--include-watchlist", action="store_true", help="Danger: also clear the curated watchlist.")
    parser.add_argument("--include-user-config", action="store_true", help="Also clear user_config such as total capital.")
    parser.add_argument("--include-static-data", action="store_true", help="Also clear factor_weights/calibrated weights/chain overrides.")
    args = parser.parse_args()

    payload = run(args)
    print("Reset for clean proof:", "EXECUTED" if payload["executed"] else "DRY-RUN")
    print(f"  backup_dir: {payload['backup']['backup_dir']}")
    for item in payload["table_actions"]:
        rows = item.get("rows_before")
        rows_txt = "" if rows is None else f" rows_before={rows}"
        print(f"  table {item['table']}: {item['action']}{rows_txt}")
    changed_files = [x for x in payload["file_actions"] if x["exists"]]
    print(f"  files/dirs to move: {len(changed_files)}")
    if not payload["executed"]:
        print("  No data changed. Re-run with --yes to execute after reviewing this dry-run.")
    else:
        manifest = REPO / payload["backup"]["backup_dir"] / "manifest.json"
        manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"  manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
