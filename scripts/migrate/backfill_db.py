"""
DuckDB 历史回填
─────────────────────────────────────────
从飞书「每日优选 · AI 投资」表读所有历史入选记录写入 picks 表，
从今天的 prices_*.json 取最大快照写入 prices 表。

只跑一次（一次性补历史），之后每日 daily_refresh.sh 自然累积。
"""
import sys
import os
import re
import glob
import json
import requests
from datetime import datetime

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # scripts/migrate/X.py → repo root
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))  # 2026-05-11 lib 迁移
from feishu_auth import feishu_token, FEISHU_APP_TOKEN  # noqa: E402
from stock_db import upsert_picks, upsert_prices, stats  # noqa: E402

PICKS_TABLE_ID = "tbl7K88JZ0ZMqPIE"
WATCHLIST_TABLE_ID = "tblaEuCPOlXBlSvP"  # noqa: F841 — kept for symmetry
PICKS_BASE = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{PICKS_TABLE_ID}"
DATA_DIR = os.path.dirname(os.path.abspath(__file__))


def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def normalize_field(v):
    if v is None:
        return ""
    if isinstance(v, list):
        if not v:
            return ""
        if isinstance(v[0], dict):
            return v[0].get("text", "") or v[0].get("name", "")
        return str(v[0])
    if isinstance(v, dict):
        return v.get("name", "") or v.get("text", "")
    return str(v)


def safe_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def fetch_all_picks(token):
    all_items = []
    page_token = None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(f"{PICKS_BASE}/records", headers=headers(token), params=params)
        d = r.json()
        all_items.extend(d.get("data", {}).get("items", []))
        if not d.get("data", {}).get("has_more"):
            break
        page_token = d["data"]["page_token"]
    return all_items


def backfill_picks_from_feishu():
    print("[1] 从飞书拉所有历史入选记录...")
    token = feishu_token()
    items = fetch_all_picks(token)
    print(f"    共 {len(items)} 条")

    rows = []
    for it in items:
        f = it.get("fields", {})
        pick_date_ts = f.get("入选日期")
        if not pick_date_ts:
            continue
        try:
            pick_date = datetime.fromtimestamp(pick_date_ts / 1000).date()
        except Exception:
            continue

        code = normalize_field(f.get("代码"))
        if not code:
            continue

        entry_price_str = normalize_field(f.get("入选时价格"))
        m_p = re.search(r"([\d,]+\.?\d*)", entry_price_str.replace(",", ""))
        m_c = re.search(r"\b([A-Z]{3})\b", entry_price_str)

        rows.append({
            "pick_date": pick_date,
            "code": code,
            "name": normalize_field(f.get("股票名称")),
            "market": normalize_field(f.get("市场")),
            "rating": normalize_field(f.get("入选评分")),
            "total_score": safe_float(f.get("综合得分")),
            "ai_relevance": normalize_field(f.get("AI关联度")),
            "theme": normalize_field(f.get("主题分类")),
            "entry_price": float(m_p.group(1)) if m_p else None,
            "entry_currency": m_c.group(1) if m_c else None,
            "peg_at_pick": safe_float(f.get("入选时PEG")),
            "fpe_at_pick": safe_float(f.get("入选时远期PE")),
            "ytd_at_pick": safe_float(f.get("入选时YTD%")),
            "one_week_at_pick": safe_float(f.get("入选时1周%")),
            "one_year_at_pick": safe_float(f.get("入选时1Y%")),
        })

    print(f"    解析有效 {len(rows)} 条")
    n = upsert_picks(rows)
    print(f"    已写入 picks: {n} 行")

    # 按入选日期分布
    from collections import Counter
    dist = Counter(r["pick_date"] for r in rows)
    print(f"    日期分布: {len(dist)} 个不同日期")
    for d in sorted(dist):
        print(f"      {d}: {dist[d]} 只")


def backfill_prices_from_snapshots():
    print("\n[2] 从本地 prices_*.json 快照回填 prices 表...")
    # 2026-05-20: snapshots 已挪到 data/snapshots/prices/；同时扫旧根目录（兼容）
    snapshot_dir = os.path.join(DATA_DIR, "data", "snapshots", "prices")
    files = sorted(
        glob.glob(os.path.join(snapshot_dir, "prices_*.json"))
        + glob.glob(os.path.join(DATA_DIR, "prices_*.json"))
    )
    # 按日期分组，取每天文件最大的
    by_date = {}
    for f in files:
        m = re.search(r"prices_(\d{4}-\d{2}-\d{2})_", f)
        if not m:
            continue
        d = m.group(1)
        size = os.path.getsize(f)
        if d not in by_date or size > by_date[d][1]:
            by_date[d] = (f, size)

    total = 0
    for d, (f, _) in sorted(by_date.items()):
        with open(f, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not data:
            continue
        n = upsert_prices(data, snapshot_date=d)
        total += n
        print(f"    {d}: {n} 行  ← {os.path.basename(f)}")
    print(f"    合计 {total} 行")


def main():
    backfill_picks_from_feishu()
    backfill_prices_from_snapshots()
    print("\n[3] 当前库状态：")
    for tbl, info in stats().items():
        print(f"  {tbl:8s} {info['rows']:>5} rows  "
              f"{info['min_date']} ~ {info['max_date']}")


if __name__ == "__main__":
    main()
