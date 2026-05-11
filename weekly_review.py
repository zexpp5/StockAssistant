"""
每日优选 · 回顾刷新器
─────────────────────────────────────────
扫描「每日优选 · AI 投资」表的所有记录，对每只股票：
1. 用 yfinance 拉当前价
2. 计算累计涨跌%（vs 入选时价格）
3. 更新「持有天数」
4. 自动判断「命中评级」
5. 输出本周 / 本月 / 全部回顾报告

用法：
  python3 weekly_review.py              # 刷新所有记录 + 终端打印报告
  python3 weekly_review.py --period 7   # 仅看最近 7 天的入选
  python3 weekly_review.py --dry-run    # 不写飞书
"""
import sys
import os
import re
import json
import time
import argparse
import requests
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from feishu_auth import feishu_token, FEISHU_APP_TOKEN  # noqa: E402
from stock_db import upsert_reviews  # noqa: E402

import yfinance as yf  # noqa: E402

PICKS_TABLE_ID = "tbl7K88JZ0ZMqPIE"
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


def parse_price(price_str):
    """从 '215.2 USD' 这样的字符串提取数字。"""
    if not price_str:
        return None
    m = re.search(r"([\d,]+\.?\d*)", price_str.replace(",", ""))
    if m:
        try:
            return float(m.group(1))
        except (ValueError, TypeError):
            return None
    return None


def to_yfinance_ticker(code, market):
    """与 fetch_stock_prices.py 中的逻辑一致。"""
    code = (code or "").strip()
    market = market or ""
    if not code:
        return None
    if "." in code:
        return code
    if "港股" in market:
        return f"{code}.HK"
    if "韩股" in market:
        return f"{code}.KS"
    if code.replace("-", "").replace(".", "").isalpha():
        return code
    if code.isdigit() and len(code) == 6:
        if "深交所" in market or code.startswith(("00", "30", "20")):
            return f"{code}.SZ"
        elif "北交所" in market or code.startswith(("8", "9")):
            return f"{code}.BJ"
        return f"{code}.SS"
    return None


def fetch_picks(token):
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


def update_record(token, record_id, fields):
    # 2026-05-11 起默认跳过飞书写入（DuckDB 是 single source of truth）
    # FEISHU_WRITE_TABLES=1 时启用（应急更新飞书 picks 表跟踪状态）
    if os.environ.get("FEISHU_WRITE_TABLES", "0") != "1":
        return {"code": 0, "_skipped": "FEISHU_WRITE_TABLES=0"}
    url = f"{PICKS_BASE}/records/{record_id}"
    r = requests.put(url, headers=headers(token), json={"fields": fields})
    return r.json()


def fetch_current_price(yf_ticker):
    try:
        t = yf.Ticker(yf_ticker)
        info = t.info
        return info.get("currentPrice") or info.get("regularMarketPrice")
    except Exception as e:
        print(f"      yfinance 失败: {e}")
        return None


def grade_hit(pct):
    if pct is None:
        return None
    if pct > 15:
        return "🚀 大涨（>15%）"
    if pct > 5:
        return "✅ 命中（5%-15%）"
    if pct >= -5:
        return "🟢 跟随（-5%~+5%）"
    if pct >= -15:
        return "⚠️ 不及预期（-5%~-15%）"
    return "❌ 大跌（<-15%）"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", type=int, default=None, help="只看最近 N 天的入选")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = feishu_token()
    print("[1/3] 拉取「每日优选」所有记录...")
    items = fetch_picks(token)
    print(f"  共 {len(items)} 条入选记录")

    print("\n[2/3] 抓当前价格 + 计算回顾...")
    today_ts = int(datetime.now().timestamp() * 1000)
    today_date = datetime.now().date()
    period_cutoff = None
    if args.period:
        period_cutoff = (datetime.now() - timedelta(days=args.period)).timestamp() * 1000

    results = []
    for item in items:
        f = item.get("fields", {})
        record_id = item["record_id"]
        name = normalize_field(f.get("股票名称"))
        code = normalize_field(f.get("代码"))
        market = normalize_field(f.get("市场"))
        pick_date_ts = f.get("入选日期")
        entry_price_str = normalize_field(f.get("入选时价格"))

        if not pick_date_ts or not code:
            continue

        # 期限过滤
        if period_cutoff and pick_date_ts < period_cutoff:
            continue

        entry_price = parse_price(entry_price_str)
        if not entry_price:
            print(f"  [跳过] {name} — 无入选价")
            continue

        yf_code = to_yfinance_ticker(code, market)
        if not yf_code:
            print(f"  [跳过] {name} — 无法转换 ticker")
            continue

        print(f"  抓取 {name} ({yf_code})...", end=" ")
        current = fetch_current_price(yf_code)
        if not current:
            print("❌ 失败")
            continue

        # 计算
        pct = round((current - entry_price) / entry_price * 100, 2)
        pick_date = datetime.fromtimestamp(pick_date_ts / 1000).date()
        days_held = (today_date - pick_date).days
        grade = grade_hit(pct)

        # 货币：从入选价里提取
        currency_match = re.search(r"\b([A-Z]{3})\b", entry_price_str)
        currency = currency_match.group(1) if currency_match else "USD"

        result = {
            "record_id": record_id,
            "name": name,
            "code": code,
            "entry_price": entry_price,
            "current_price": current,
            "pct": pct,
            "days_held": days_held,
            "grade": grade,
            "pick_date": pick_date.strftime("%Y-%m-%d"),
            "rating": normalize_field(f.get("入选评分")),
            "theme": normalize_field(f.get("主题分类")),
            "ai_relevance": normalize_field(f.get("AI关联度")),
        }
        results.append(result)
        sign = "+" if pct > 0 else ""
        print(f"{current} {currency} · {sign}{pct:.1f}% · {days_held} 天 · {grade}")

        if not args.dry_run:
            update_fields = {
                "当前价格": f"{current} {currency}",
                "累计涨跌%": pct,
                "持有天数": days_held,
                "命中评级": grade,
                "最近回顾时间": today_ts,
            }
            update_record(token, record_id, update_fields)
        time.sleep(0.4)

    print(f"\n[3/3] 回顾报告")
    print("=" * 60)

    if not results:
        print("  无可回顾的记录")
        return

    # 整体表现
    avg_pct = sum(r["pct"] for r in results) / len(results)
    win_count = sum(1 for r in results if r["pct"] > 5)
    flat_count = sum(1 for r in results if -5 <= r["pct"] <= 5)
    loss_count = sum(1 for r in results if r["pct"] < -5)
    win_rate = win_count / len(results) * 100

    print(f"\n📊 整体表现：")
    print(f"  • 入选总数：{len(results)} 只")
    print(f"  • 平均涨跌：{'+' if avg_pct > 0 else ''}{avg_pct:.2f}%")
    print(f"  • 命中（>+5%）：{win_count} 只 ({win_rate:.1f}%)")
    print(f"  • 跟随（-5%~+5%）：{flat_count} 只")
    print(f"  • 失败（<-5%）：{loss_count} 只")

    # TOP / BOTTOM
    sorted_by_pct = sorted(results, key=lambda x: x["pct"], reverse=True)
    print(f"\n🚀 表现最好 Top 5：")
    for r in sorted_by_pct[:5]:
        sign = "+" if r["pct"] > 0 else ""
        print(f"  • {r['name']:<22} {sign}{r['pct']:>6.2f}% ({r['days_held']} 天) [{r['rating']}]")

    print(f"\n📉 表现最差 Top 5：")
    for r in sorted_by_pct[-5:]:
        sign = "+" if r["pct"] > 0 else ""
        print(f"  • {r['name']:<22} {sign}{r['pct']:>6.2f}% ({r['days_held']} 天) [{r['rating']}]")

    # 评分组别表现
    by_rating = defaultdict(list)
    for r in results:
        if r["rating"]:
            by_rating[r["rating"]].append(r["pct"])

    print(f"\n⭐ 评分 vs 实际表现：")
    for rating, pcts in sorted(by_rating.items(), reverse=True):
        avg = sum(pcts) / len(pcts)
        sign = "+" if avg > 0 else ""
        print(f"  • {rating}（{len(pcts)} 只）：平均 {sign}{avg:.2f}%")

    # 主题表现
    by_theme = defaultdict(list)
    for r in results:
        if r["theme"]:
            by_theme[r["theme"]].append(r["pct"])

    print(f"\n🗂  主题表现：")
    theme_avg = [(t, sum(p)/len(p), len(p)) for t, p in by_theme.items()]
    theme_avg.sort(key=lambda x: x[1], reverse=True)
    for theme, avg, n in theme_avg:
        sign = "+" if avg > 0 else ""
        print(f"  • {theme:<26} 平均 {sign}{avg:>6.2f}% ({n} 只)")

    # 落 DuckDB（每条 review 一行，按 review_date + pick_date + code 去重覆盖）
    try:
        db_rows = [{
            "pick_date": r["pick_date"],
            "code": r["code"],
            "name": r["name"],
            "entry_price": r["entry_price"],
            "current_price": r["current_price"],
            "pct": r["pct"],
            "days_held": r["days_held"],
            "grade": r["grade"],
            "rating": r["rating"],
            "theme": r["theme"],
        } for r in results]
        n = upsert_reviews(db_rows)
        print(f"\n  DuckDB：已写入 {n} 行 (stock_history.duckdb · reviews)")
    except Exception as e:
        print(f"\n  DuckDB 写入失败（不阻塞主流程）：{e}")

    # 保存 JSON 快照
    out_file = os.path.join(DATA_DIR, f"review_{datetime.now().strftime('%Y-%m-%d_%H%M')}.json")
    with open(out_file, "w", encoding="utf-8") as fout:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "summary": {
                "total": len(results),
                "avg_pct": round(avg_pct, 2),
                "win_count": win_count,
                "flat_count": flat_count,
                "loss_count": loss_count,
                "win_rate": round(win_rate, 1),
            },
            "by_rating": {k: round(sum(v)/len(v), 2) for k, v in by_rating.items()},
            "by_theme": {k: round(sum(v)/len(v), 2) for k, v in by_theme.items()},
            "results": results,
        }, fout, ensure_ascii=False, indent=2, default=str)
    print(f"\n  快照已保存：{out_file}")
    print(f"  飞书表：https://w5scrwkn9y.feishu.cn/base/{FEISHU_APP_TOKEN}?table={PICKS_TABLE_ID}")


if __name__ == "__main__":
    main()
