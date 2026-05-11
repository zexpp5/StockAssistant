"""把 data/snapshots/**/*.json 历史快照一次性导入 DuckDB `snapshots` 表。

策略：
  - 文件名解析两种 timestamp 形式：
      <name>_YYYY-MM-DD_HHMMSS.json    (主流格式，store.save_json 产物)
      <name>_YYYY-MM-DD.json            (factor_tearsheet 等直接 dump 的格式)
  - 解析失败的文件用 mtime 兜底
  - category = 文件相对 SNAPSHOT_DIR 的目录路径
  - 用 (category, name, taken_at) 做去重判断（已存在则跳过）

幂等可重跑。
"""
from __future__ import annotations
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

# 让 stock_research package 可 import（脚本在 scripts/migrate/ 下）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import duckdb

from stock_research import config
from stock_research.adapters.store import (
    _category_from_dirpath,
    _ensure_snapshots_schema,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate_snapshots")

PATTERN_FULL = re.compile(r"^(?P<name>.+)_(?P<ts>\d{4}-\d{2}-\d{2}_\d{6})\.json$")
PATTERN_DATE = re.compile(r"^(?P<name>.+)_(?P<ts>\d{4}-\d{2}-\d{2})\.json$")


def parse_filename(p: Path) -> tuple[str, datetime, str]:
    """返回 (name_prefix, taken_at, parse_method)。"""
    fn = p.name
    m = PATTERN_FULL.match(fn)
    if m:
        ts = datetime.strptime(m["ts"], "%Y-%m-%d_%H%M%S")
        return m["name"], ts, "full_ts"
    m = PATTERN_DATE.match(fn)
    if m:
        ts = datetime.strptime(m["ts"], "%Y-%m-%d")
        return m["name"], ts, "date_only"
    mtime = datetime.fromtimestamp(p.stat().st_mtime)
    return p.stem, mtime, "mtime_fallback"


def migrate(dry_run: bool = False) -> dict:
    snap_root = config.SNAPSHOT_DIR.resolve()
    if not snap_root.exists():
        logger.warning("SNAPSHOT_DIR not found: %s", snap_root)
        return {"scanned": 0, "inserted": 0, "skipped": 0, "errors": 0}

    files = sorted(snap_root.rglob("*.json"))
    logger.info("found %d JSON files under %s", len(files), snap_root)

    con = duckdb.connect(str(config.DUCKDB_PATH))
    try:
        _ensure_snapshots_schema(con)
        existing = {
            (cat, name, ts)
            for cat, name, ts in con.execute(
                "SELECT category, name, taken_at FROM snapshots"
            ).fetchall()
        }
        logger.info("existing rows in snapshots: %d", len(existing))

        stats = {"scanned": 0, "inserted": 0, "skipped": 0, "errors": 0}
        method_counts = {"full_ts": 0, "date_only": 0, "mtime_fallback": 0}

        for f in files:
            stats["scanned"] += 1
            try:
                name, taken_at, method = parse_filename(f)
                method_counts[method] = method_counts.get(method, 0) + 1
                category = _category_from_dirpath(f.parent)

                if (category, name, taken_at) in existing:
                    stats["skipped"] += 1
                    continue

                with open(f, encoding="utf-8") as fp:
                    payload = json.load(fp)

                payload_json = json.dumps(payload, ensure_ascii=False, default=str)

                if not dry_run:
                    con.execute(
                        "INSERT INTO snapshots(category, name, taken_at, payload) "
                        "VALUES (?, ?, ?, ?)",
                        [category, name, taken_at, payload_json],
                    )
                stats["inserted"] += 1
                existing.add((category, name, taken_at))
            except Exception as e:
                stats["errors"] += 1
                logger.warning("failed on %s: %s", f, e)

        logger.info("parse methods: %s", method_counts)
        logger.info("stats: %s", stats)
        if dry_run:
            logger.info("(dry-run; no rows actually inserted)")
        return stats
    finally:
        con.close()


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    migrate(dry_run=dry)
