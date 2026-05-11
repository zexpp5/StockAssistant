"""把根目录数据 JSON 一次性导入 DuckDB `snapshots` 表（category='pipeline'）。

涉及文件（如存在则迁移）：
    factor_scores_today.json
    history_data.json
    risk_metrics.json
    track_13f.json
    optimization_result.json
    plan_a_v5.json
    simulation_plan_a.json
    reverse_validation.json
    reverse_validation_v3.json … reverse_validation_v6.json
    data/factor_weights.json

每个文件：
    category = 'pipeline'
    name     = 文件 basename 去掉 .json
    taken_at = 文件 mtime
    payload  = JSON 内容

幂等可重跑：以 (category, name, taken_at) 做去重。
"""
from __future__ import annotations
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# 让 stock_research package 可 import（脚本在 scripts/migrate/ 下）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import duckdb

from stock_research import config
from stock_research.adapters.store import _ensure_snapshots_schema

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate_pipeline")

REPO = Path(__file__).resolve().parents[2]  # repo root

ROOT_FILES = [
    # 2026-05-11 起 latest JSON 集中到 data/latest/
    "data/latest/factor_scores_today.json",
    "data/latest/history_data.json",
    "data/latest/risk_metrics.json",
    "data/latest/track_13f.json",
    "data/latest/optimization_result.json",
    "data/latest/plan_a_v5.json",
    "data/latest/trade_delta.json",
    "data/latest/plan_a_v5_constrained.json",
    # 历史遗留（不一定存在，存在则归档）
    "simulation_plan_a.json",
    "archive/legacy/reverse_validation.json",
    "archive/legacy/reverse_validation_v3.json",
    "archive/legacy/reverse_validation_v4.json",
    "archive/legacy/reverse_validation_v5.json",
    "archive/legacy/reverse_validation_v6.json",
    "data/factor_weights.json",
]


def migrate(dry_run: bool = False) -> dict:
    con = duckdb.connect(str(config.DUCKDB_PATH))
    try:
        _ensure_snapshots_schema(con)
        existing = {
            (cat, name, ts)
            for cat, name, ts in con.execute(
                "SELECT category, name, taken_at FROM snapshots WHERE category='pipeline'"
            ).fetchall()
        }
        logger.info("existing pipeline rows: %d", len(existing))

        stats = {"scanned": 0, "inserted": 0, "skipped": 0, "missing": 0, "errors": 0}

        for relpath in ROOT_FILES:
            stats["scanned"] += 1
            p = REPO / relpath
            if not p.exists():
                stats["missing"] += 1
                logger.info("missing (skipped): %s", relpath)
                continue
            try:
                taken_at = datetime.fromtimestamp(p.stat().st_mtime)
                name = p.stem  # factor_scores_today, history_data, etc.

                if (("pipeline", name, taken_at)) in existing:
                    stats["skipped"] += 1
                    continue

                with open(p, encoding="utf-8") as f:
                    payload = json.load(f)
                payload_json = json.dumps(payload, ensure_ascii=False, default=str)

                if not dry_run:
                    con.execute(
                        "INSERT INTO snapshots(category, name, taken_at, payload) "
                        "VALUES (?, ?, ?, ?)",
                        ["pipeline", name, taken_at, payload_json],
                    )
                stats["inserted"] += 1
                existing.add(("pipeline", name, taken_at))
                size_kb = p.stat().st_size / 1024
                logger.info("ok: %s (%.1f KB) → name=%s @ %s",
                            relpath, size_kb, name, taken_at.isoformat(timespec="seconds"))
            except Exception as e:
                stats["errors"] += 1
                logger.warning("failed on %s: %s", relpath, e)

        logger.info("stats: %s", stats)
        if dry_run:
            logger.info("(dry-run; no rows actually inserted)")
        return stats
    finally:
        con.close()


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    migrate(dry_run=dry)
