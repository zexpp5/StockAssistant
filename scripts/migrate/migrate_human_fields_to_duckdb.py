"""一次性把飞书 watchlist 表的 3 个人工字段回填到 DuckDB:
- earnings        ← "最近季度业绩"
- verification    ← "双源验证"
- info_breakdown  ← "信息构成"

为什么单独一个脚本(而不是复用 migrate_watchlist_to_duckdb.py):
upsert_watchlist 会用 None 覆盖整行其它字段。本脚本只 UPDATE 3 列,不动其它。

用法:
    python3 scripts/migrate/migrate_human_fields_to_duckdb.py
    python3 scripts/migrate/migrate_human_fields_to_duckdb.py --dry-run
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "lib"))

import requests
from feishu_auth import feishu_token, FEISHU_APP_TOKEN
from stock_db import get_db

WATCHLIST_TABLE_ID = "tblaEuCPOlXBlSvP"
WATCHLIST_BASE = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{WATCHLIST_TABLE_ID}"


def _norm(v):
    if v is None:
        return None
    if isinstance(v, list):
        if not v:
            return None
        parts = []
        for it in v:
            if isinstance(it, dict):
                parts.append(it.get("text") or it.get("name") or "")
            else:
                parts.append(str(it))
        s = "\n".join(p for p in parts if p)
        return s or None
    if isinstance(v, dict):
        return v.get("text") or v.get("name") or None
    s = str(v).strip()
    return s or None


def fetch_raw(token):
    items = []
    page = None
    while True:
        params = {"page_size": 100}
        if page:
            params["page_token"] = page
        r = requests.get(
            f"{WATCHLIST_BASE}/records",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        d = r.json()
        items.extend(d.get("data", {}).get("items", []))
        if not d.get("data", {}).get("has_more"):
            break
        page = d["data"]["page_token"]
    return items


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("[1/3] 拉飞书 watchlist 原始 items...")
    token = feishu_token()
    items = fetch_raw(token)
    print(f"  共 {len(items)} 条")

    print("\n[2/3] 提取 earnings / verification / info_breakdown...")
    targets = []
    for it in items:
        f = it.get("fields", {})
        code = _norm(f.get("代码"))
        if not code:
            continue
        targets.append({
            "code": code,
            "earnings": _norm(f.get("最近季度业绩")),
            "verification": _norm(f.get("双源验证")),
            "info_breakdown": _norm(f.get("信息构成")),
        })
    n_with_data = sum(
        1 for t in targets
        if t["earnings"] or t["verification"] or t["info_breakdown"]
    )
    print(f"  有效记录 {len(targets)} 条 · 其中 {n_with_data} 条至少有 1 个字段非空")
    sample = [t for t in targets if t["earnings"] or t["verification"] or t["info_breakdown"]][:3]
    for s in sample:
        print(f"    · {s['code']}: earnings={(s['earnings'] or '')[:30]!r}, "
              f"verification={(s['verification'] or '')[:20]!r}, "
              f"info_breakdown={(s['info_breakdown'] or '')[:20]!r}")

    if args.dry_run:
        print("\n[Dry-Run] 未写 DuckDB")
        return 0

    print("\n[3/3] UPDATE DuckDB watchlist (仅这 3 列)...")
    conn = get_db()
    updated = 0
    missing_in_db = 0
    for t in targets:
        exists = conn.execute("SELECT 1 FROM watchlist WHERE code=?", [t["code"]]).fetchone()
        if not exists:
            missing_in_db += 1
            continue
        conn.execute(
            "UPDATE watchlist SET earnings=?, verification=?, info_breakdown=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE code=?",
            [t["earnings"], t["verification"], t["info_breakdown"], t["code"]],
        )
        updated += 1
    conn.close()
    print(f"  ✅ UPDATE {updated} 条")
    if missing_in_db:
        print(f"  ⚠️  飞书有但 DuckDB watchlist 没有的 code: {missing_in_db} 条 (跳过)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
