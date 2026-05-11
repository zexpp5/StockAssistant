"""
yfinance 价格抓取器
─────────────────────────────────────────
功能：
1. 从飞书 watchlist 拉所有股票代码
2. 用 yfinance 抓取实时价格、YTD 涨幅、一年涨幅、市值、PE
3. 自动处理跨市场代码（美股/A股/港股/韩股）
4. 写回飞书表，并保存 JSON 快照

用法：
  python3 fetch_stock_prices.py              # 全量更新
  python3 fetch_stock_prices.py --code NVDA  # 仅更新单只
"""
import sys
import os
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, _REPO)
import json
import time
import argparse
import requests
from datetime import datetime

from feishu_auth import feishu_token, FEISHU_APP_TOKEN  # noqa: E402
from stock_db import upsert_prices  # noqa: E402

import yfinance as yf  # noqa: E402

TABLE_ID = "tblaEuCPOlXBlSvP"
BASE_URL = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{TABLE_ID}"
DATA_DIR = _REPO


def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ============================================================
# yfinance 代码格式转换（关键）
# ============================================================

def to_yfinance_ticker(code, market):
    """把飞书表里的代码转换成 yfinance 能识别的 ticker。
    优先看 market，市场字段缺失时根据代码格式自动判断。"""
    code = (code or "").strip()
    market = market or ""
    if not code:
        return None

    # 1) 已有交易所后缀 → 直接返回
    if "." in code:
        # 韩股 000660.KS、港股 3690.HK 等
        return code

    # 2) 港股提示
    if "港股" in market:
        return f"{code}.HK"

    # 3) 韩股
    if "韩股" in market or "其他" in market and code.startswith("00"):
        return f"{code}.KS"

    # 4) 美股：纯字母（含连字符），不论 market 字段
    if code.replace("-", "").replace(".", "").isalpha():
        return code

    # 5) A 股 6 位数字代码
    clean = code
    if clean.isdigit() and len(clean) == 6:
        if "深交所" in market or clean.startswith(("00", "30", "20")):
            return f"{clean}.SZ"
        elif "北交所" in market or clean.startswith(("8", "9")):
            return f"{clean}.BJ"
        else:
            # 默认上交所（含 60、68、78、73、603 等）
            return f"{clean}.SS"

    return None


# ============================================================
# 拉飞书数据 + 写回飞书
# ============================================================

def fetch_watchlist(token):
    all_items = []
    page_token = None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(f"{BASE_URL}/records", headers=headers(token), params=params)
        d = r.json()
        all_items.extend(d.get("data", {}).get("items", []))
        if not d.get("data", {}).get("has_more"):
            break
        page_token = d["data"]["page_token"]
    return all_items


def normalize_field(v):
    if v is None:
        return ""
    if isinstance(v, list):
        return v[0].get("text", "") if v and isinstance(v[0], dict) else ""
    if isinstance(v, dict):
        return v.get("name", "") or v.get("text", "")
    return str(v)


def update_record(token, record_id, fields):
    url = f"{BASE_URL}/records/{record_id}"
    r = requests.put(url, headers=headers(token), json={"fields": fields})
    return r.json()


# ============================================================
# yfinance 抓取
# ============================================================

def fetch_price_data(yf_ticker):
    """对一个 yfinance ticker 抓数据，返回标准化字典。失败返回 None。"""
    try:
        t = yf.Ticker(yf_ticker)
        info = t.info
        if not info or "regularMarketPrice" not in info and "currentPrice" not in info:
            return None

        # 价格相关
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        prev_close = info.get("previousClose")
        currency = info.get("currency", "USD")
        market_cap = info.get("marketCap")
        forward_pe = info.get("forwardPE")
        trailing_pe = info.get("trailingPE")
        peg_ratio = info.get("pegRatio") or info.get("trailingPegRatio")
        earnings_growth = info.get("earningsGrowth")  # 季度 YoY 利润增速（小数，如 0.5 = 50%）
        revenue_growth = info.get("revenueGrowth")    # 季度 YoY 营收增速

        # YTD / 1Y / 1月 / 1周 涨幅
        ytd_pct = None
        one_year_pct = None
        one_month_pct = None
        one_week_pct = None
        try:
            hist = t.history(period="1y")
            if len(hist) > 0 and price:
                this_year = datetime.now().year
                year_start = hist[hist.index.year == this_year]
                if len(year_start) > 0:
                    ytd_start = year_start.iloc[0]["Close"]
                    if ytd_start > 0:
                        ytd_pct = round((price - ytd_start) / ytd_start * 100, 2)

                one_year_start = hist.iloc[0]["Close"]
                if one_year_start > 0:
                    one_year_pct = round((price - one_year_start) / one_year_start * 100, 2)

                # 1 月涨幅：取 ~22 个交易日前
                if len(hist) >= 22:
                    m_ago = hist.iloc[-22]["Close"]
                    if m_ago > 0:
                        one_month_pct = round((price - m_ago) / m_ago * 100, 2)
                # 1 周涨幅：取 ~5 个交易日前
                if len(hist) >= 5:
                    w_ago = hist.iloc[-5]["Close"]
                    if w_ago > 0:
                        one_week_pct = round((price - w_ago) / w_ago * 100, 2)
        except Exception as e:
            print(f"      历史数据失败: {e}")

        # PEG 兜底计算（pegRatio 不可用时用 forward PE / 利润增速）
        peg_calculated = None
        if peg_ratio is None and forward_pe and earnings_growth and earnings_growth > 0:
            peg_calculated = round(forward_pe / (earnings_growth * 100), 2)

        return {
            "price": price,
            "prev_close": prev_close,
            "currency": currency,
            "market_cap": market_cap,
            "forward_pe": round(forward_pe, 2) if forward_pe else None,
            "trailing_pe": round(trailing_pe, 2) if trailing_pe else None,
            "peg_ratio": round(peg_ratio, 2) if peg_ratio else peg_calculated,
            "earnings_growth_pct": round(earnings_growth * 100, 2) if earnings_growth else None,
            "revenue_growth_pct": round(revenue_growth * 100, 2) if revenue_growth else None,
            "ytd_pct": ytd_pct,
            "one_year_pct": one_year_pct,
            "one_month_pct": one_month_pct,
            "one_week_pct": one_week_pct,
        }
    except Exception as e:
        print(f"      yfinance 失败: {e}")
        return None


def format_market_cap(mc, currency):
    if not mc:
        return ""
    units = {"USD": "美元", "CNY": "人民币", "HKD": "港元", "KRW": "韩元"}
    unit = units.get(currency, currency)
    if currency == "KRW":
        if mc >= 1e12:
            return f"₩{mc/1e12:.2f}万亿（{unit}）"
        return f"₩{mc/1e9:.0f}亿（{unit}）"
    if mc >= 1e12:
        return f"${mc/1e12:.2f}T（{unit}）"
    if mc >= 1e9:
        return f"${mc/1e9:.1f}B（{unit}）"
    return f"${mc/1e6:.0f}M（{unit}）"


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", help="只更新某只股票")
    parser.add_argument("--dry-run", action="store_true", help="不写飞书，只打印")
    args = parser.parse_args()

    token = feishu_token()
    print("[1/3] 拉取 watchlist...")
    items = fetch_watchlist(token)
    print(f"  共 {len(items)} 条")

    print("\n[2/3] 抓取价格（yfinance）...")
    results = []
    success_count = 0
    fail_codes = []

    for item in items:
        f = item.get("fields", {})
        name = normalize_field(f.get("股票名称"))
        code = normalize_field(f.get("代码"))
        market = normalize_field(f.get("市场"))
        record_id = item["record_id"]

        if args.code and args.code != code:
            continue

        yf_code = to_yfinance_ticker(code, market)
        if not yf_code:
            print(f"  [跳过] {name} ({code}) — 无法转换 ticker")
            fail_codes.append(code)
            continue

        print(f"  抓取 {name} ({yf_code})...", end=" ")
        data = fetch_price_data(yf_code)
        if not data:
            print("❌ 失败")
            fail_codes.append(code)
            continue

        success_count += 1
        price_str = f"{data['price']} {data['currency']}"
        ytd_str = f"{data['ytd_pct']:+.1f}%" if data["ytd_pct"] is not None else "N/A"
        oy_str = f"{data['one_year_pct']:+.1f}%" if data["one_year_pct"] is not None else "N/A"
        wk_str = f"{data['one_week_pct']:+.1f}%" if data["one_week_pct"] is not None else "N/A"
        peg_str = f"{data['peg_ratio']}" if data["peg_ratio"] else "N/A"
        print(f"{price_str} · 1W {wk_str} · YTD {ytd_str} · 1Y {oy_str} · PEG {peg_str}")

        result = {
            "code": code,
            "name": name,
            "yf_ticker": yf_code,
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            **data,
        }
        results.append(result)

        # 写回飞书 watchlist 表（2026-05-11 起默认跳过，DuckDB 是 single source of truth）
        # FEISHU_WRITE_TABLES=1 时启用（应急更新 watchlist 展示字段用）
        if not args.dry_run and os.environ.get("FEISHU_WRITE_TABLES", "0") == "1":
            update_fields = {
                "最新价格": f"{data['price']} {data['currency']}" if data["price"] else "",
                "YTD涨幅%": data["ytd_pct"] if data["ytd_pct"] is not None else None,
                "一年涨幅%": data["one_year_pct"] if data["one_year_pct"] is not None else None,
                "1月涨幅%": data["one_month_pct"] if data["one_month_pct"] is not None else None,
                "1周涨幅%": data["one_week_pct"] if data["one_week_pct"] is not None else None,
                "远期PE": data["forward_pe"] if data["forward_pe"] is not None else None,
                "PEG": data["peg_ratio"] if data["peg_ratio"] is not None else None,
                "利润增速%": data["earnings_growth_pct"] if data["earnings_growth_pct"] is not None else None,
                "yf市值": format_market_cap(data["market_cap"], data["currency"]),
                "价格更新时间": int(datetime.now().timestamp() * 1000),
            }
            update_fields = {k: v for k, v in update_fields.items() if v not in (None, "")}
            update_record(token, record_id, update_fields)

        time.sleep(0.5)  # 别太快

    print(f"\n[3/3] 完成：成功 {success_count} / 总 {len(items)}")
    if fail_codes:
        print(f"  失败标的：{', '.join(fail_codes)}")

    # 保存 JSON 快照
    out_file = os.path.join(DATA_DIR, f"prices_{datetime.now().strftime('%Y-%m-%d_%H%M')}.json")
    with open(out_file, "w", encoding="utf-8") as f_out:
        json.dump(results, f_out, ensure_ascii=False, indent=2, default=str)
    print(f"  快照已保存：{out_file}")

    # 落 DuckDB（按 fetched_at 的日期，同日多次抓取会覆盖）
    if results:
        try:
            n = upsert_prices(results)
            print(f"  DuckDB：已写入 {n} 行 (stock_history.duckdb · prices)")
        except Exception as e:
            print(f"  DuckDB 写入失败（不阻塞主流程）：{e}")


if __name__ == "__main__":
    main()
