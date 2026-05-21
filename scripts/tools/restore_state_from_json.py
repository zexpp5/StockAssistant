#!/usr/bin/env python3
"""从 state_backup/*.json 恢复用户状态到 DuckDB。

通常流程（在新机器或 DB 损坏后）：
  1. python3 scripts/tools/init_stock_db_v2.py --reset --seed-universe
     → 重建 schema + system_universe
  2. python3 scripts/tools/restore_state_from_json.py
     → 从最新 state_backup/*.json 恢复 holdings/manual_watchlist/...
  3. bash daily_refresh.sh --morning
     → 重新填充 price_daily + 跑当日推荐

CLI:
  python3 scripts/tools/restore_state_from_json.py          # 最新 state_backup
  python3 scripts/tools/restore_state_from_json.py --file state_backup/state_2026-05-20.json
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import duckdb  # noqa: E402

DB_PATH = REPO / "stock_history_v2.duckdb"
DEFAULT_BACKUP_DIR = REPO / "state_backup"


def find_latest_backup(directory: Path) -> Path | None:
    if not directory.exists():
        return None
    files = sorted(directory.glob("state_*.json"))
    return files[-1] if files else None


def restore(db_path: Path, backup_file: Path, replace: bool) -> dict:
    payload = json.loads(backup_file.read_text())
    conn = duckdb.connect(str(db_path))
    summary = {}
    for table, info in payload.get("tables", {}).items():
        if info.get("missing"):
            summary[table] = "source_missing"
            continue
        cols = info.get("columns") or []
        rows = info.get("rows") or []
        if not rows:
            summary[table] = "empty"
            continue
        # 确认目标表存在
        existing = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
        if table not in existing:
            summary[table] = "target_missing (run init_stock_db_v2.py first)"
            continue
        if replace:
            conn.execute(f"DELETE FROM {table}")
        # 用 INSERT OR REPLACE 半幂等；列以 backup 为准，目标表多余字段保持 NULL
        placeholders = ", ".join(["?"] * len(cols))
        col_list = ", ".join([f'"{c}"' for c in cols])
        sql = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"
        for r in rows:
            conn.execute(sql, [r.get(c) for c in cols])
        summary[table] = f"restored {len(rows)} rows"
    conn.close()
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description="Restore state from state_backup/*.json into DuckDB")
    p.add_argument("--db", default=str(DB_PATH), help="DuckDB path (must exist with V2 schema)")
    p.add_argument("--file", default=None, help="Specific backup JSON (default: latest in state_backup/)")
    p.add_argument("--replace", action="store_true",
                   help="DELETE FROM 目标表 before restore（避免和已有行冲突；高危）")
    args = p.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"ERROR: DB {db} not found. Run init_stock_db_v2.py --reset first.", file=sys.stderr)
        return 2

    if args.file:
        bf = Path(args.file)
    else:
        bf = find_latest_backup(DEFAULT_BACKUP_DIR)
    if not bf or not bf.exists():
        print(f"ERROR: No backup file found in {DEFAULT_BACKUP_DIR}", file=sys.stderr)
        return 3

    print(f"📥 Restoring from {bf} → {db}")
    if args.replace:
        print("⚠️  --replace: DELETE FROM 目标表 will execute before insert")
    summary = restore(db, bf, args.replace)
    for t, status in summary.items():
        print(f"  · {t}: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
