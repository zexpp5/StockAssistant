"""验证 data/snapshots/**/*.json ↔ DuckDB `snapshots` 表的同步状态。

检查项：
  1. 文件数 vs 表行数
  2. 按 (category, name) 分组的计数对账
  3. 逐文件内容 round-trip 比对（JSON 解析后语义相等）
  4. 报告找不到对应行的文件 / 找不到对应文件的行

退出码：0 = 完全一致；1 = 有不一致或错误
"""
from __future__ import annotations
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# 让 stock_research package 可 import
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import duckdb

from stock_research import config
from stock_research.adapters.store import _category_from_dirpath

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("verify")

PATTERN_FULL = re.compile(r"^(?P<name>.+)_(?P<ts>\d{4}-\d{2}-\d{2}_\d{6})\.json$")
PATTERN_DATE = re.compile(r"^(?P<name>.+)_(?P<ts>\d{4}-\d{2}-\d{2})\.json$")


def parse_filename(p: Path) -> tuple[str, datetime] | None:
    fn = p.name
    m = PATTERN_FULL.match(fn)
    if m:
        return m["name"], datetime.strptime(m["ts"], "%Y-%m-%d_%H%M%S")
    m = PATTERN_DATE.match(fn)
    if m:
        return m["name"], datetime.strptime(m["ts"], "%Y-%m-%d")
    return None


def normalize(payload):
    """把 payload 序列化再反序列化，消除字段顺序/格式差异，得到可比较结构。"""
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True))


def main() -> int:
    snap_root = config.SNAPSHOT_DIR.resolve()
    files = sorted(snap_root.rglob("*.json"))
    file_keys: dict[tuple[str, str, datetime], Path] = {}
    unparsed: list[Path] = []
    for f in files:
        parsed = parse_filename(f)
        if parsed is None:
            unparsed.append(f)
            continue
        name, ts = parsed
        cat = _category_from_dirpath(f.parent)
        file_keys[(cat, name, ts)] = f

    con = duckdb.connect(str(config.DUCKDB_PATH), read_only=True)
    rows = con.execute(
        "SELECT category, name, taken_at, payload FROM snapshots"
    ).fetchall()
    con.close()
    db_keys: dict[tuple[str, str, datetime], str] = {
        (r[0], r[1], r[2]): r[3] for r in rows
    }

    print()
    print(f"=== 同步对账报告 ===")
    print(f"  JSON 文件总数:    {len(files)}")
    print(f"    可解析文件:    {len(file_keys)}")
    print(f"    解析失败文件:  {len(unparsed)}")
    print(f"  DuckDB 行数:      {len(rows)}")
    print()

    if unparsed:
        print("⚠️  解析失败的文件（无法对账）:")
        for p in unparsed:
            print(f"    {p}")
        print()

    file_only = set(file_keys) - set(db_keys)
    db_only = set(db_keys) - set(file_keys)

    print("=== 分类对账 ===")
    cats_files = Counter((cat, name) for cat, name, _ in file_keys)
    cats_db = Counter((cat, name) for cat, name, _ in db_keys)
    all_groups = sorted(set(cats_files) | set(cats_db))
    fmt = "  {:<35}  {:>5}  {:>5}  {}"
    print(fmt.format("category/name", "files", "db", "status"))
    print("  " + "-" * 70)
    mismatch_groups = 0
    for k in all_groups:
        nf, nd = cats_files.get(k, 0), cats_db.get(k, 0)
        status = "OK" if nf == nd else "❌ DIFF"
        if nf != nd:
            mismatch_groups += 1
        print(fmt.format(f"{k[0]}/{k[1]}", nf, nd, status))
    print()

    if file_only:
        print(f"⚠️  仅在文件存在、DB 缺失（{len(file_only)}）:")
        for k in sorted(file_only)[:20]:
            print(f"    {k}")
        if len(file_only) > 20:
            print(f"    ... and {len(file_only) - 20} more")
        print()

    if db_only:
        print(f"⚠️  仅在 DB 存在、文件缺失（{len(db_only)}）:")
        for k in sorted(db_only)[:20]:
            print(f"    {k}")
        if len(db_only) > 20:
            print(f"    ... and {len(db_only) - 20} more")
        print()

    print("=== 逐文件内容比对 ===")
    common = set(file_keys) & set(db_keys)
    content_mismatch: list[tuple] = []
    for k in sorted(common):
        f = file_keys[k]
        with open(f, encoding="utf-8") as fp:
            file_payload = json.load(fp)
        db_payload_raw = db_keys[k]
        db_payload = json.loads(db_payload_raw) if isinstance(db_payload_raw, str) else db_payload_raw
        if normalize(file_payload) != normalize(db_payload):
            content_mismatch.append((k, f))
    print(f"  共对比 {len(common)} 对，内容不一致 {len(content_mismatch)} 对")
    if content_mismatch:
        print("  ❌ 不一致样例（最多 10 条）:")
        for k, f in content_mismatch[:10]:
            print(f"    {k}  ←→  {f}")
    print()

    ok = (
        not unparsed
        and not file_only
        and not db_only
        and not content_mismatch
        and len(files) == len(rows)
    )
    if ok:
        print("✅ 完全同步：JSON 与 DuckDB 一一对应、内容一致")
        return 0
    print("❌ 存在不一致，详见上方报告")
    return 1


if __name__ == "__main__":
    sys.exit(main())
