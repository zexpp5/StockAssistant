#!/usr/bin/env python3
"""把 DuckDB 里的"用户状态"导出为 JSON，落 state_backup/。

DB 文件 74MB 已超 GitHub 50MB 推荐上限，不再入仓；用户状态改由 JSON 持久化。
state_backup/ 是 source of truth，DuckDB 是衍生物。

state-bearing tables（必须备份）：
  - holdings                   用户实际持仓
  - manual_watchlist           用户加的自选股
  - portfolio_plans            组合方案决策
  - recommendation_runs        每日推荐 run 元数据
  - recommendation_picks       每日推荐结果（alpha 追踪依赖）
  - pick_outcomes              alpha 实现值

derived tables（不备份，可由数据源 + 代码 replay）：
  price_daily / system_universe / pool_membership / factor_attribution /
  strategy_review_reports / pipeline_runs / pipeline_steps / data_quality_checks /
  source_fetch_log / source_raw_snapshots / financial_statements /
  financial_statement_versions / snapshots / portfolio_performance / strategy_versions /
  user_config / schema_meta

CLI:
  python3 scripts/tools/backup_state_to_json.py
  python3 scripts/tools/backup_state_to_json.py --out state_backup/snapshot_2026-05-21.json
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import duckdb  # noqa: E402

DB_PATH = REPO / "stock_history_v2.duckdb"
DEFAULT_OUT_DIR = REPO / "state_backup"

STATE_TABLES = [
    "holdings",
    "manual_watchlist",
    "portfolio_plans",
    "recommendation_runs",
    "recommendation_picks",
    "pick_outcomes",
]


def _json_default(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def export_state(db_path: Path, tables: list[str]) -> dict:
    conn = duckdb.connect(str(db_path), read_only=True)
    payload: dict = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "db_path": str(db_path),
        "tables": {},
    }
    existing = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    for t in tables:
        if t not in existing:
            payload["tables"][t] = {"missing": True, "rows": []}
            continue
        cols = [c[0] for c in conn.execute(f"DESCRIBE {t}").fetchall()]
        rows = conn.execute(f"SELECT * FROM {t}").fetchall()
        payload["tables"][t] = {
            "columns": cols,
            "row_count": len(rows),
            "rows": [dict(zip(cols, r)) for r in rows],
        }
    conn.close()
    return payload


def main() -> int:
    p = argparse.ArgumentParser(description="Backup DuckDB state-bearing tables to JSON")
    p.add_argument("--db", default=str(DB_PATH), help="DuckDB path")
    p.add_argument("--out", default=None, help="Output JSON path (default: state_backup/state_YYYY-MM-DD.json)")
    p.add_argument("--tables", nargs="+", default=STATE_TABLES, help="Tables to back up")
    args = p.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"ERROR: DB not found at {db}", file=sys.stderr)
        return 2

    out_path = Path(args.out) if args.out else DEFAULT_OUT_DIR / f"state_{date.today():%Y-%m-%d}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = export_state(db, args.tables)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))

    print(f"✅ State backup → {out_path}")
    for t, info in payload["tables"].items():
        if info.get("missing"):
            print(f"  · {t}: (table missing)")
        else:
            print(f"  · {t}: {info['row_count']} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
