"""为 dashboard 历史 tab 预拉所有股票 2 年历史价格（美股 + A 股 + 港股）。

为什么：浏览器直接 fetch Yahoo Finance 会被 CORS 拦。
方案：后端拉好写到 history_data.json，前端直接读。

数据源：
  - 美股：yfinance（NVDA / AAPL 等纯字母代码）
  - A 股：yfinance（用 .SS / .SZ / .BJ 后缀）
  - 港股：yfinance（用 .HK 后缀，4 位数字补 0 到 4 位）
  - 澳股 / 英股：yfinance（用 .AX / 原 ADR 代码）

输出：history_data.json
{
  "fetched_at": "...",
  "tickers": {
    "NVDA":      {"name": "Nvidia", "market": "US", "ts": [...], "close": [...]},
    "300308":    {"name": "中际旭创", "market": "A股·深圳", "ts": [...], "close": [...]},
    "3690.HK":   {"name": "美团", "market": "港股", "ts": [...], "close": [...]},
    ...
  }
}
"""
import json
import sys
import os
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))  # 2026-05-11 lib 迁移
from datetime import datetime
from pathlib import Path

import yfinance as yf
from feishu_auth import feishu_token, FEISHU_APP_TOKEN
import requests

WATCHLIST_TABLE_ID = "tblaEuCPOlXBlSvP"


def to_yfinance_ticker(code: str, market: str) -> str | None:
    """飞书代码 + 市场 → yfinance ticker。"""
    code = (code or "").strip()
    if not code:
        return None
    # 已带后缀
    if "." in code:
        return code
    # 纯字母 = 美股
    if code.replace("-", "").isalpha() and len(code) <= 6:
        return code.upper()
    # 6 位数字 = A 股
    if code.isdigit() and len(code) == 6:
        if "深交所" in market or code.startswith(("00", "30", "20")):
            return f"{code}.SZ"
        if "北交所" in market or code.startswith(("8", "9")):
            return f"{code}.BJ"
        return f"{code}.SS"  # 默认上交所
    # 4-5 位数字 = 港股
    if code.isdigit() and 4 <= len(code) <= 5:
        return f"{code.zfill(4)}.HK"
    return None


def fetch_codes_from_feishu():
    """读飞书 watchlist 拿所有股票（含美股/A股/港股）。"""
    token = feishu_token()
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{WATCHLIST_TABLE_ID}/records"
    headers = {"Authorization": f"Bearer {token}"}
    out = []
    page = None
    while True:
        params = {"page_size": 100}
        if page:
            params["page_token"] = page
        r = requests.get(url, headers=headers, params=params)
        d = r.json().get("data", {})
        for it in d.get("items", []):
            f = it.get("fields", {}) or {}
            code_v = f.get("代码")
            if isinstance(code_v, list) and code_v:
                code = code_v[0].get("text", "")
            else:
                code = str(code_v or "")
            name_v = f.get("股票名称")
            if isinstance(name_v, list) and name_v:
                name = name_v[0].get("text", "")
            else:
                name = str(name_v or "")
            market_v = f.get("市场")
            if isinstance(market_v, list) and market_v:
                market = market_v[0].get("text", "") if isinstance(market_v[0], dict) else str(market_v[0])
            elif isinstance(market_v, dict):
                market = market_v.get("text", "") or market_v.get("name", "")
            else:
                market = str(market_v or "")
            code = code.strip()
            yf_ticker = to_yfinance_ticker(code, market)
            if yf_ticker:
                # 用原始飞书代码作为 dashboard key，但用 yfinance ticker 拉数据
                out.append({"feishu_code": code.upper() if yf_ticker == code.upper() else code,
                            "yf_ticker": yf_ticker, "name": name, "market": market})
        if not d.get("has_more"):
            break
        page = d.get("page_token")
    return out


def fetch_history(ticker: str, period: str = "2y") -> dict | None:
    """拉单只股票 2 年日 K。"""
    try:
        t = yf.Ticker(ticker)
        h = t.history(period=period, interval="1d")
        if h is None or h.empty:
            return None
        return {
            "ts": [d.strftime("%Y-%m-%d") for d in h.index],
            "close": [None if v != v else round(float(v), 4) for v in h["Close"].tolist()],
        }
    except Exception as e:
        print(f"  ❌ {ticker}: {e}")
        return None


def main():
    print("[1/3] 拉飞书 watchlist 全市场代码...")
    stocks = fetch_codes_from_feishu()
    # 主题按钮 + 核心标的兜底（防止 watchlist 漏录）
    extras = [
        ("NVDA", "Nvidia", "美股"), ("TSM", "TSMC", "美股"), ("AMD", "AMD", "美股"), ("AVGO", "Broadcom", "美股"),
        ("AAPL", "Apple", "美股"), ("MSFT", "Microsoft", "美股"), ("GOOGL", "Alphabet", "美股"), ("META", "Meta", "美股"),
        ("AMZN", "Amazon", "美股"), ("TSLA", "Tesla", "美股"), ("INTC", "Intel", "美股"), ("MRVL", "Marvell", "美股"),
        ("VRT", "Vertiv", "美股"), ("ETN", "Eaton", "美股"), ("GEV", "GE Vernova", "美股"), ("MTZ", "MasTec", "美股"),
        ("PWR", "Quanta", "美股"), ("VST", "Vistra", "美股"),
        ("XYL", "Xylem", "美股"), ("MP", "MP Materials", "美股"), ("CCJ", "Cameco", "美股"),
        ("BWXT", "BWX Technologies", "美股"), ("RDDT", "Reddit", "美股"),
        ("EQIX", "Equinix", "美股"), ("ORCL", "Oracle", "美股"), ("LRCX", "Lam Research", "美股"),
        ("NET", "Cloudflare", "美股"), ("CDNS", "Cadence", "美股"), ("CRWD", "CrowdStrike", "美股"),
        ("SYM", "Symbotic", "美股"), ("KO", "Coca-Cola", "美股"), ("MCD", "McDonald's", "美股"),
        ("OKLO", "Oklo", "美股"), ("SMR", "NuScale", "美股"), ("NNE", "NANO Nuclear", "美股"),
        ("LEU", "Centrus", "美股"), ("UUUU", "Energy Fuels", "美股"),
        ("SPY", "S&P 500 ETF", "美股"), ("QQQ", "Nasdaq 100 ETF", "美股"),
        # A 股核心
        ("300308", "中际旭创", "A股·深交所"), ("300502", "新易盛", "A股·深交所"),
        ("002230", "科大讯飞", "A股·深交所"), ("002837", "英维克", "A股·深交所"),
        ("688256", "寒武纪", "A股·上交所"), ("688041", "海光信息", "A股·上交所"),
        ("688111", "金山办公", "A股·上交所"), ("600111", "北方稀土", "A股·上交所"),
        # 港股核心
        ("3690", "美团", "港股"), ("9988", "阿里巴巴", "港股"),
        ("0700", "腾讯", "港股"), ("9992", "泡泡玛特", "港股"),
        ("0020", "商汤", "港股"),
    ]
    existing_keys = {s["feishu_code"] for s in stocks}
    for code, name, market in extras:
        if code not in existing_keys:
            yf_ticker = to_yfinance_ticker(code, market)
            if yf_ticker:
                stocks.append({"feishu_code": code, "yf_ticker": yf_ticker, "name": name, "market": market})

    by_market = {}
    for s in stocks:
        m = "美股" if s["market"] == "" or "美股" in s["market"] else ("A股" if "A股" in s["market"] else ("港股" if "港股" in s["market"] else "其他"))
        by_market[m] = by_market.get(m, 0) + 1
    print(f"  共 {len(stocks)} 只: " + ", ".join(f"{k} {v}" for k, v in by_market.items()))

    print(f"\n[2/3] 拉每只 2 年日 K...")
    out = {"fetched_at": datetime.now().isoformat(timespec="seconds"), "tickers": {}}
    success = 0
    failed = []
    for s in stocks:
        h = fetch_history(s["yf_ticker"])
        if h and h["close"]:
            # dashboard 用 feishu_code 作为 key（与 RECORDS 对齐）
            out["tickers"][s["feishu_code"]] = {
                "name": s["name"],
                "market": s["market"],
                "yf_ticker": s["yf_ticker"],
                **h,
            }
            success += 1
            print(f"  ✓ {s['feishu_code']:10} {s['yf_ticker']:12} {s['name']:25} {len(h['ts']):4d} 天")
        else:
            failed.append(f"{s['feishu_code']}({s['yf_ticker']})")

    print(f"\n[3/3] 写文件...")
    out_path = Path(_REPO) / "data" / "latest" / "history_data.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    size_kb = out_path.stat().st_size / 1024
    print(f"  ✅ {out_path}: {size_kb:,.0f} KB")
    print(f"  成功 {success} / 失败 {len(failed)}")
    if failed:
        print(f"  失败：{', '.join(failed)}")


if __name__ == "__main__":
    main()
