"""
国际新闻 v2: yfinance（权威英文一手源）+ Claude Haiku 翻译
─────────────────────────────────────────
数据源（yfinance 聚合的全球财经权威媒体）:
  · Yahoo Finance
  · Reuters
  · Bloomberg
  · MarketWatch
  · CNBC
  · Barron's
  · Motley Fool
  · 24/7 Wall St.
  · Investor's Business Daily
  · 等

覆盖 tickers（拉全球主流市场 + 关键资产）:
  · 美股指数: ^GSPC ^IXIC ^DJI
  · 国际指数: ^FTSE ^N225 ^HSI ^STOXX50E
  · 商品: GC=F (黄金) CL=F (原油) SI=F (白银)
  · 利率/汇率: ^TNX (10Y) DX-Y.NYB (DXY)
  · 加密: BTC-USD
  · 关键大盘股: NVDA AAPL TSLA META MSFT

翻译: Anthropic Claude Haiku 4.5
  · 一次性 batch 翻译所有标题 + 摘要
  · ~50 条新闻成本 < $0.01

写入飞书国际热点新闻表:
  · 标题（原文）+ 标题（中文）
  · 摘要（英文）+ 中文总结
  · 来源（媒体名）
  · 抓取日期
  · 链接 → 原文 URL
"""
import sys
import os
import time
import json
import requests
import hashlib
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from feishu_auth import feishu_token, FEISHU_APP_TOKEN

import yfinance as yf
from anthropic import Anthropic


INTL_TABLE = "tblhKE2rBoOGe82j"

# 覆盖全球主流市场的 tickers
NEWS_TICKERS = [
    # 美股指数
    ("^GSPC", "S&P 500"),
    ("^IXIC", "Nasdaq"),
    ("^DJI", "Dow"),
    # 国际指数
    ("^FTSE", "FTSE 100"),
    ("^N225", "Nikkei 225"),
    ("^HSI", "Hang Seng"),
    ("^STOXX50E", "Euro Stoxx 50"),
    # 商品
    ("GC=F", "Gold"),
    ("CL=F", "Crude Oil"),
    ("SI=F", "Silver"),
    # 利率/汇率/加密
    ("^TNX", "US 10Y Treasury"),
    ("DX-Y.NYB", "USD Index"),
    ("BTC-USD", "Bitcoin"),
    # 关键 mega-cap
    ("NVDA", "Nvidia"),
    ("AAPL", "Apple"),
    ("TSLA", "Tesla"),
    ("META", "Meta"),
    ("MSFT", "Microsoft"),
]


def fetch_news_for_ticker(ticker, days=2):
    """拉 yfinance 新闻，过滤到最近 N 天"""
    try:
        items = yf.Ticker(ticker).news or []
        cutoff = datetime.now() - timedelta(days=days)
        out = []
        for it in items:
            c = it.get("content", it)
            pub = c.get("pubDate", "")
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00")).replace(tzinfo=None)
                if pub_dt < cutoff:
                    continue
            except Exception:
                continue
            url = c.get("canonicalUrl", {}).get("url") if isinstance(c.get("canonicalUrl"), dict) else c.get("clickThroughUrl", {}).get("url") if isinstance(c.get("clickThroughUrl"), dict) else ""
            provider = "?"
            if isinstance(c.get("provider"), dict):
                provider = c["provider"].get("displayName", "?")
            out.append({
                "title": c.get("title", ""),
                "summary": c.get("summary", "")[:500],
                "provider": provider,
                "pub_date": pub[:10],
                "url": url,
                "source_ticker": ticker,
            })
        return out
    except Exception as e:
        print(f"  ! {ticker}: {e}")
        return []


def dedupe_by_url(news_list):
    seen = set()
    out = []
    for n in news_list:
        key = n["url"] or hashlib.md5(n["title"].encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def translate_batch(news_list, batch_size=20):
    """Claude Haiku batch 翻译标题+摘要"""
    client = Anthropic()
    translated = []

    for i in range(0, len(news_list), batch_size):
        batch = news_list[i:i+batch_size]
        # 构造 prompt
        items_text = ""
        for j, n in enumerate(batch, 1):
            items_text += f"\n--- #{j} ---\n"
            items_text += f"Title: {n['title']}\n"
            items_text += f"Summary: {n['summary']}\n"

        prompt = (
            "Translate each numbered news item below to Chinese. "
            "Return ONLY a JSON array, one object per item with keys 'title_cn' and 'summary_cn'. "
            "Keep brand names (Nvidia/Apple/...) and tickers as-is. Be concise but faithful.\n"
            f"{items_text}"
        )

        for attempt in range(3):
            try:
                msg = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=4000,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = msg.content[0].text.strip()
                # 提取 JSON
                if "```" in text:
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                trans = json.loads(text.strip())
                for n, t in zip(batch, trans):
                    n["title_cn"] = t.get("title_cn", n["title"])
                    n["summary_cn"] = t.get("summary_cn", n["summary"])
                    translated.append(n)
                print(f"  ✅ batch {i//batch_size + 1} ({len(batch)} 条)")
                break
            except Exception as e:
                print(f"  ! batch {i//batch_size + 1} 翻译失败 (尝试 {attempt+1}/3): {e}")
                if attempt < 2:
                    time.sleep(3)
                else:
                    # 翻译失败 → 保留英文原文当中文用
                    for n in batch:
                        n["title_cn"] = n["title"]
                        n["summary_cn"] = n["summary"]
                        translated.append(n)
    return translated


def delete_all_records(token, table_id):
    base = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{table_id}"
    all_ids = []
    page_token = None
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(f"{base}/records", headers={"Authorization": f"Bearer {token}"},
                        params=params, timeout=30)
        d = r.json()
        items = d.get("data", {}).get("items", [])
        all_ids.extend(item["record_id"] for item in items)
        if not d.get("data", {}).get("has_more"):
            break
        page_token = d["data"].get("page_token")
        if not page_token:
            break
    if not all_ids:
        return 0
    deleted = 0
    for i in range(0, len(all_ids), 500):
        batch = all_ids[i:i+500]
        r = requests.post(f"{base}/records/batch_delete",
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                         json={"records": batch}, timeout=30)
        if r.json().get("code") == 0:
            deleted += len(batch)
        time.sleep(0.3)
    return deleted


def batch_write(token, table_id, records):
    base = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{table_id}"
    success = 0
    for i in range(0, len(records), 500):
        batch = records[i:i+500]
        body = {"records": [{"fields": {k: v for k, v in r.items() if v not in (None, "")}} for r in batch]}
        r = requests.post(f"{base}/records/batch_create",
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                         json=body, timeout=30)
        d = r.json()
        if d.get("code") == 0:
            success += len(batch)
        else:
            print(f"  ! 写入失败: {d.get('msg')}")
        time.sleep(0.3)
    return success


def main():
    today_str = datetime.now().strftime("%Y-%m-%d")
    print("=" * 80)
    print(f"  🌍 国际新闻 v2 · yfinance + Claude 翻译 · {today_str}")
    print("=" * 80)

    print(f"\n[1/4] 拉 {len(NEWS_TICKERS)} 个 ticker 的最近 2 天新闻...")
    all_news = []
    for tk, name in NEWS_TICKERS:
        news = fetch_news_for_ticker(tk, days=2)
        if news:
            print(f"  {tk:14} {name:18} {len(news)} 条")
            all_news.extend(news)
        time.sleep(0.5)

    print(f"\n  总共 {len(all_news)} 条（去重前）")
    all_news = dedupe_by_url(all_news)
    print(f"  去重后 {len(all_news)} 条")

    # 限 60 条，避免翻译 token 太多
    all_news = sorted(all_news, key=lambda x: x["pub_date"], reverse=True)[:60]
    print(f"  保留最近 60 条")

    print(f"\n[2/4] Claude Haiku 批量翻译...")
    translated = translate_batch(all_news, batch_size=15)

    print(f"\n[3/4] 删除飞书国际表历史...")
    token = feishu_token()
    deleted = delete_all_records(token, INTL_TABLE)
    print(f"  删除 {deleted} 条")

    print(f"\n[4/4] 写入今日国际新闻...")
    records = []
    for n in translated:
        records.append({
            "标题（中文）": n.get("title_cn", "")[:200],
            "标题（原文）": n["title"][:200],
            "中文总结": n.get("summary_cn", "")[:500],
            "摘要": n["summary"][:500],
            "分类": n.get("source_ticker", ""),
            "来源": n["provider"],
            "来源分类": "yfinance/Yahoo",
            "链接": {"link": n["url"], "text": "原文"} if n["url"] else None,
            "抓取日期": today_str,
        })
    written = batch_write(token, INTL_TABLE, records)
    print(f"  写入 {written} / {len(records)} 条")

    print(f"\n✅ 完成")
    print(f"  链接: https://w5scrwkn9y.feishu.cn/base/{FEISHU_APP_TOKEN}?table={INTL_TABLE}")


if __name__ == "__main__":
    main()
