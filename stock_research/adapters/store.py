"""快照存储适配器：JSON 文件 + DuckDB（可选）。

JSON 快照走文件系统，是单一事实来源；DuckDB 是查询加速层（可选，没有 duckdb 包也能用）。
"""
from __future__ import annotations
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import config

logger = logging.getLogger(__name__)


def save_json(payload: dict | list, dirpath: Path, name_prefix: str) -> Path:
    """把数据存为 JSON 文件，文件名带时间戳。返回路径。"""
    dirpath.mkdir(parents=True, exist_ok=True)
    fn = f"{name_prefix}_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.json"
    out = dirpath / fn
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    logger.info("saved snapshot: %s", out)
    return out


def load_latest_json(dirpath: Path, name_prefix: str) -> dict | list | None:
    """读取某 prefix 最新的 JSON 快照。"""
    if not dirpath.exists():
        return None
    matches = sorted(dirpath.glob(f"{name_prefix}_*.json"), reverse=True)
    if not matches:
        return None
    with open(matches[0], encoding="utf-8") as f:
        return json.load(f)


def load_all_json(dirpath: Path, name_prefix: str = "") -> list[dict | list]:
    """读取目录下所有匹配的 JSON 快照（按文件名倒序）。"""
    if not dirpath.exists():
        return []
    if name_prefix:
        matches = sorted(dirpath.glob(f"{name_prefix}*.json"), reverse=True)
    else:
        matches = sorted(dirpath.glob("*.json"), reverse=True)
    out = []
    for p in matches:
        try:
            with open(p, encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception as e:
            logger.warning("skip corrupted snapshot %s: %s", p, e)
    return out


# ────────────────────────────────────────────────────────
# DuckDB（可选加速层；缺包就静默跳过）
# ────────────────────────────────────────────────────────

def duckdb_available() -> bool:
    try:
        import duckdb  # noqa: F401
        return True
    except ImportError:
        return False


def upsert_enrichment(rows: list[dict[str, Any]]) -> int:
    """把 enrichment 数据写入 DuckDB enrichment 表。无 duckdb 包则跳过。"""
    if not rows:
        return 0
    try:
        import duckdb
    except ImportError:
        logger.debug("duckdb not installed, skipping")
        return 0

    con = duckdb.connect(str(config.DUCKDB_PATH))
    con.execute("""
        CREATE TABLE IF NOT EXISTS enrichment (
            code         TEXT,
            name         TEXT,
            source       TEXT,
            field        TEXT,
            value        TEXT,
            value_num    DOUBLE,
            fetched_at   TIMESTAMP,
            PRIMARY KEY (code, source, field, fetched_at)
        )
    """)
    n = 0
    for r in rows:
        try:
            con.execute(
                "INSERT OR REPLACE INTO enrichment VALUES (?,?,?,?,?,?,?)",
                [r.get("code"), r.get("name"), r.get("source"),
                 r.get("field"), str(r.get("value", "")),
                 r.get("value_num"), r.get("fetched_at") or datetime.now()],
            )
            n += 1
        except Exception as e:
            logger.warning("duckdb insert failed: %s", e)
    con.close()
    return n
