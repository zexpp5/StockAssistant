"""快照存储适配器：JSON 文件 + DuckDB 双写（迁移期）。

迁移策略：
  阶段一（当前）：每次保存同时写 JSON 文件 + DuckDB `snapshots` 表（双写）。
                  读路径仍然走 JSON 文件，确保零风险。
  阶段二（待切换）：读路径切到 DuckDB，删除 JSON 文件。

DuckDB `snapshots` 表 schema：
  id        BIGINT  PK (从 snap_id_seq 自增)
  category  VARCHAR 由 dirpath 相对 SNAPSHOT_DIR 推导，例如 'audit' / '13f/0001029160'
  name      VARCHAR 调用方传入的 name_prefix
  taken_at  TIMESTAMP 写入时刻
  payload   JSON      原始 JSON 内容
"""
from __future__ import annotations
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import config

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────
# JSON 文件层（仍是当前的读路径，不变）
# ────────────────────────────────────────────────────────

def save_json(payload: dict | list, dirpath: Path, name_prefix: str) -> Path:
    """把数据存为 JSON 文件，并同步写入 DuckDB `snapshots` 表。返回文件路径。"""
    dirpath.mkdir(parents=True, exist_ok=True)
    taken_at = datetime.now()
    fn = f"{name_prefix}_{taken_at.strftime('%Y-%m-%d_%H%M%S')}.json"
    out = dirpath / fn
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    logger.info("saved snapshot: %s", out)

    try:
        _duckdb_insert_snapshot(dirpath, name_prefix, taken_at, payload)
    except Exception as e:
        logger.warning("dual-write to duckdb failed (file saved OK): %s", e)
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
# DuckDB 层
# ────────────────────────────────────────────────────────

def duckdb_available() -> bool:
    try:
        import duckdb  # noqa: F401
        return True
    except ImportError:
        return False


def _category_from_dirpath(dirpath: Path) -> str:
    """把绝对/相对 dirpath 转成 snapshots.category。

    'data/snapshots/audit'                     → 'audit'
    'data/snapshots/13f/0001029160'            → '13f/0001029160'
    'data/snapshots/optimize'                  → 'optimize'
    """
    p = Path(dirpath).resolve()
    snap_root = config.SNAPSHOT_DIR.resolve()
    try:
        rel = p.relative_to(snap_root)
        return str(rel).replace("\\", "/")
    except ValueError:
        return p.name


def _ensure_snapshots_schema(con) -> None:
    con.execute("CREATE SEQUENCE IF NOT EXISTS snap_id_seq START 1")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id        BIGINT DEFAULT nextval('snap_id_seq') PRIMARY KEY,
            category  VARCHAR NOT NULL,
            name      VARCHAR NOT NULL,
            taken_at  TIMESTAMP NOT NULL,
            payload   JSON NOT NULL
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_snap_lookup ON snapshots(category, name, taken_at)"
    )


def _duckdb_insert_snapshot(
    dirpath: Path, name_prefix: str, taken_at: datetime, payload: Any
) -> None:
    """把一条快照写入 DuckDB（双写路径或迁移用）。"""
    import duckdb

    category = _category_from_dirpath(dirpath)
    payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    con = duckdb.connect(str(config.DUCKDB_PATH))
    try:
        _ensure_snapshots_schema(con)
        con.execute(
            "INSERT INTO snapshots(category, name, taken_at, payload) VALUES (?, ?, ?, ?)",
            [category, name_prefix, taken_at, payload_json],
        )
    finally:
        con.close()


# ────────────────────────────────────────────────────────
# DuckDB 读路径（阶段二切换前先用作验证；阶段二会替换 load_*_json）
# ────────────────────────────────────────────────────────

def db_load_latest(dirpath: Path, name_prefix: str) -> dict | list | None:
    import duckdb

    category = _category_from_dirpath(dirpath)
    con = duckdb.connect(str(config.DUCKDB_PATH), read_only=True)
    try:
        row = con.execute(
            "SELECT payload FROM snapshots "
            "WHERE category=? AND name=? "
            "ORDER BY taken_at DESC LIMIT 1",
            [category, name_prefix],
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    return json.loads(row[0]) if isinstance(row[0], str) else row[0]


def db_load_all(dirpath: Path, name_prefix: str = "") -> list[dict | list]:
    import duckdb

    category = _category_from_dirpath(dirpath)
    con = duckdb.connect(str(config.DUCKDB_PATH), read_only=True)
    try:
        if name_prefix:
            rows = con.execute(
                "SELECT payload FROM snapshots "
                "WHERE category=? AND name LIKE ? "
                "ORDER BY taken_at DESC",
                [category, name_prefix + "%"],
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT payload FROM snapshots "
                "WHERE category=? "
                "ORDER BY taken_at DESC",
                [category],
            ).fetchall()
    finally:
        con.close()
    return [
        json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in rows
    ]


# ────────────────────────────────────────────────────────
# pipeline 数据（根目录数据 JSON 的 DuckDB 镜像）
# 复用 snapshots 表，category='pipeline'，name=去掉 .json 后缀的文件名
# ────────────────────────────────────────────────────────

def pipeline_save(name: str, payload: Any) -> None:
    """把 pipeline 数据写入 DuckDB（category='pipeline'）。"""
    import duckdb

    payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    con = duckdb.connect(str(config.DUCKDB_PATH))
    try:
        _ensure_snapshots_schema(con)
        con.execute(
            "INSERT INTO snapshots(category, name, taken_at, payload) VALUES (?, ?, ?, ?)",
            ["pipeline", name, datetime.now(), payload_json],
        )
    finally:
        con.close()


def pipeline_load_latest(name: str) -> dict | list | None:
    """读最新一条 pipeline 数据。"""
    import duckdb

    con = duckdb.connect(str(config.DUCKDB_PATH), read_only=True)
    try:
        row = con.execute(
            "SELECT payload FROM snapshots "
            "WHERE category='pipeline' AND name=? "
            "ORDER BY taken_at DESC LIMIT 1",
            [name],
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    return json.loads(row[0]) if isinstance(row[0], str) else row[0]


# ────────────────────────────────────────────────────────
# enrichment 表（保留原有逻辑，与 snapshots 独立）
# ────────────────────────────────────────────────────────

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
