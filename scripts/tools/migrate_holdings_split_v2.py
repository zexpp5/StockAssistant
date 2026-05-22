#!/usr/bin/env python3
"""Copy legacy holdings rows into split V2 tables.

This is a non-destructive bridge:
- holdings.source='ai_plan' -> model_sim_holdings
- everything else -> real_holdings

The legacy holdings table is left untouched for compatibility, but new code
should read real_holdings / model_sim_holdings directly.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import stock_db  # noqa: E402


def migrate(*, clear_targets: bool = False, clear_legacy: bool = False) -> dict[str, int]:
    conn = stock_db.get_db()
    try:
        rows = stock_db.fetch_all_holdings(conn=conn)
        if clear_targets:
            conn.execute("DELETE FROM real_holdings")
            conn.execute("DELETE FROM model_sim_holdings")
        real_n = 0
        sim_rows = []
        for row in rows:
            item = {
                "code": row.get("code") or row.get("symbol"),
                "market": row.get("market"),
                "entry_price": row.get("entry_price"),
                "shares": row.get("shares"),
                "date": row.get("entry_date"),
                "currency": row.get("currency"),
                "notes": row.get("notes"),
            }
            if (row.get("source") or "manual") == "ai_plan":
                sim_rows.append({
                    **item,
                    "plan_version": "legacy_ai_plan_split",
                    "target_weight": 0,
                    "amount_rmb": 0,
                })
            else:
                stock_db.insert_real_holding(item, conn=conn)
                real_n += 1
        if sim_rows:
            stock_db.bulk_replace_model_sim_holdings(sim_rows, conn=conn)
        if clear_legacy:
            conn.execute("DELETE FROM holdings")
        return {
            "legacy_rows": len(rows),
            "real_copied": real_n,
            "sim_copied": len(sim_rows),
            "legacy_cleared": len(rows) if clear_legacy else 0,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clear-targets", action="store_true", help="Clear split target tables before copying.")
    parser.add_argument("--clear-legacy", action="store_true", help="Delete copied rows from legacy holdings after migration.")
    args = parser.parse_args()
    result = migrate(clear_targets=args.clear_targets, clear_legacy=args.clear_legacy)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
