"""一次性把飞书 watchlist 表迁移到 DuckDB。

用法：
    python3 migrate_watchlist_to_duckdb.py           # 跑迁移
    python3 migrate_watchlist_to_duckdb.py --dry-run # 看会写哪些股，不真的写

2026-05-11 起 watchlist 数据库化：DuckDB 是权威，dashboard 提供 CRUD UI。
飞书 watchlist 表 → 只读历史快照（暂不删除，方便回滚）。
"""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root（让 feishu_auth / stock_db 可 import）

import requests

from feishu_auth import feishu_token, FEISHU_APP_TOKEN
from stock_db import get_db, upsert_watchlist, fetch_all_watchlist

WATCHLIST_TABLE_ID = "tblaEuCPOlXBlSvP"
WATCHLIST_BASE = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{WATCHLIST_TABLE_ID}"


def _headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def fetch_watchlist_raw(token):
    """直接拉飞书 watchlist 原始 items（含 fields），不做字段裁剪。"""
    all_items = []
    page_token = None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(f"{WATCHLIST_BASE}/records", headers=_headers(token), params=params)
        d = r.json()
        all_items.extend(d.get("data", {}).get("items", []))
        if not d.get("data", {}).get("has_more"):
            break
        page_token = d["data"]["page_token"]
    return all_items


def _norm(v):
    """飞书 list/dict 字段拍平为字符串。"""
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
        return "\n".join(p for p in parts if p) or None
    if isinstance(v, dict):
        return v.get("text") or v.get("name") or None
    s = str(v).strip()
    return s or None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写 DuckDB")
    args = parser.parse_args()

    print("[1/3] 从飞书拉 watchlist 原始 items...")
    token = feishu_token()
    items = fetch_watchlist_raw(token)
    print(f"  共 {len(items)} 条")

    print("\n[2/3] 字段映射 → watchlist 表 schema...")
    rows = []
    for it in items:
        f = it.get("fields", {})
        rows.append({
            "code": _norm(f.get("代码")),
            "name": _norm(f.get("股票名称")),
            "market": _norm(f.get("市场")),
            "business": _norm(f.get("主营业务")),
            "industry": _norm(f.get("行业归类")),
            "ai_relevance": _norm(f.get("AI关联度")),
            "ai_logic": _norm(f.get("AI关联逻辑")),
            "theme": None,  # 主题分类是 daily_picks 阶段 THEME_MAPPING 决定的，watchlist 表不存
            "conclusion": _norm(f.get("研究结论")),
            "risks": _norm(f.get("关键风险")),
            "peers": _norm(f.get("可比公司")),
            "rhythm": _norm(f.get("跟踪节奏")),
            "status": _norm(f.get("研究状态")),
            "source": _norm(f.get("数据来源")),
            "credibility": _norm(f.get("数据可信度")),
            "notes": None,  # 自由备注新字段，旧数据没有
        })
    rows = [r for r in rows if r["code"]]  # 过滤无 code 的脏行
    print(f"  有效记录 {len(rows)} 条（含 code）")
    sample = rows[:3]
    for s in sample:
        print(f"    · {s['code']:<12} {s['name'] or '-'}")
    if len(rows) > 3:
        print(f"    · ... 还有 {len(rows) - 3} 条")

    if args.dry_run:
        print("\n[Dry-Run] 未写 DuckDB")
        return 0

    print("\n[3/3] 写 DuckDB watchlist 表...")
    conn = get_db()
    n = upsert_watchlist(rows, conn=conn)
    total = len(fetch_all_watchlist(conn=conn))
    conn.close()
    print(f"  ✅ 写入 {n} 条（含已存在 upsert 更新）")
    print(f"  📊 DuckDB watchlist 表当前总记录数：{total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
