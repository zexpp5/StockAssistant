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
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))  # 2026-05-11 lib 迁移
import json
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from stock_db import upsert_prices, fetch_all_watchlist  # noqa: E402

import yfinance as yf  # noqa: E402
import pandas as pd  # noqa: E402

DATA_DIR = _REPO
INFO_CACHE_FILE = os.path.join(DATA_DIR, "data", "latest", "yf_info_cache.json")
DEFAULT_INFO_TTL_HOURS = 12


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

# ============================================================
# yfinance 抓取
# ============================================================

def _load_info_cache() -> dict:
    try:
        with open(INFO_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"items": {}}
    except Exception:
        return {"items": {}}


def _save_info_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(INFO_CACHE_FILE), exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "items": cache.get("items", {}),
    }
    tmp = INFO_CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, INFO_CACHE_FILE)


def _cache_entry_fresh(entry: dict | None, ttl_hours: float) -> bool:
    if not entry:
        return False
    fetched_at = entry.get("fetched_at")
    if not fetched_at:
        return False
    try:
        ts = datetime.fromisoformat(str(fetched_at))
    except Exception:
        return False
    return datetime.now() - ts <= timedelta(hours=ttl_hours)


def _fetch_info_fields(yf_ticker: str) -> dict:
    """拉估值/基本面快照；这些字段变化慢，允许用本地 TTL cache。"""
    try:
        t = yf.Ticker(yf_ticker)
        info = t.info
        if not info:
            return {"error": "empty info"}

        fields = {
            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "prev_close": info.get("previousClose"),
            "currency": info.get("currency", "USD"),
            "market_cap": info.get("marketCap"),
            "forward_pe": info.get("forwardPE"),
            "trailing_pe": info.get("trailingPE"),
            "peg_ratio": info.get("pegRatio") or info.get("trailingPegRatio"),
            "earnings_growth": info.get("earningsGrowth"),
            "revenue_growth": info.get("revenueGrowth"),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
    except Exception as e:
        return {"error": str(e), "fetched_at": datetime.now().isoformat(timespec="seconds")}

    return fields


def _download_history_batch(yf_tickers: list[str]) -> dict[str, pd.DataFrame]:
    """一次下载 1 年历史价，替代每只股票单独 history(period='1y')。"""
    if not yf_tickers:
        return {}
    unique = sorted(set(yf_tickers))
    try:
        df = yf.download(
            " ".join(unique),
            period="1y",
            progress=False,
            group_by="ticker",
            threads=True,
            auto_adjust=False,
        )
    except Exception as e:
        print(f"  ⚠️ 批量历史价失败，将退化为空历史：{e}")
        return {}
    if df is None or df.empty:
        return {}

    out: dict[str, pd.DataFrame] = {}
    if isinstance(df.columns, pd.MultiIndex):
        level0 = set(df.columns.get_level_values(0))
        for tk in unique:
            if tk in level0:
                sub = df[tk].dropna(how="all")
                if not sub.empty:
                    out[tk] = sub
    elif len(unique) == 1:
        out[unique[0]] = df.dropna(how="all")
    return out


def _history_metrics(hist: pd.DataFrame | None, price: float | None) -> dict:
    ytd_pct = one_year_pct = one_month_pct = one_week_pct = None
    prev_close = None
    hist_price = None
    if hist is None or hist.empty or "Close" not in hist:
        return {
            "history_price": None, "history_prev_close": None,
            "ytd_pct": None, "one_year_pct": None,
            "one_month_pct": None, "one_week_pct": None,
        }

    close = hist["Close"].dropna()
    if close.empty:
        return {
            "history_price": None, "history_prev_close": None,
            "ytd_pct": None, "one_year_pct": None,
            "one_month_pct": None, "one_week_pct": None,
        }

    try:
        hist_price = float(close.iloc[-1])
        if len(close) >= 2:
            prev_close = float(close.iloc[-2])
        effective_price = float(price) if price is not None else hist_price

        this_year = datetime.now().year
        year_start = close[close.index.year == this_year]
        if len(year_start) > 0:
            ytd_start = float(year_start.iloc[0])
            if ytd_start > 0:
                ytd_pct = round((effective_price - ytd_start) / ytd_start * 100, 2)

        one_year_start = float(close.iloc[0])
        if one_year_start > 0:
            one_year_pct = round((effective_price - one_year_start) / one_year_start * 100, 2)

        if len(close) >= 22:
            m_ago = float(close.iloc[-22])
            if m_ago > 0:
                one_month_pct = round((effective_price - m_ago) / m_ago * 100, 2)
        if len(close) >= 5:
            w_ago = float(close.iloc[-5])
            if w_ago > 0:
                one_week_pct = round((effective_price - w_ago) / w_ago * 100, 2)
    except Exception:
        pass

    return {
        "history_price": hist_price,
        "history_prev_close": prev_close,
        "ytd_pct": ytd_pct,
        "one_year_pct": one_year_pct,
        "one_month_pct": one_month_pct,
        "one_week_pct": one_week_pct,
    }


def fetch_price_data(yf_ticker: str, *, hist: pd.DataFrame | None = None,
                     info_fields: dict | None = None):
    """组合批量历史价 + 缓存估值字段，返回标准化字典。失败返回 None。"""
    info_fields = info_fields or {}
    if info_fields.get("error") and (hist is None or hist.empty):
        return None

    price = info_fields.get("price")
    try:
        price = float(price) if price is not None else None
    except Exception:
        price = None

    hist_metrics = _history_metrics(hist, price)
    if price is None:
        price = hist_metrics.get("history_price")
    if price is None:
        return None

    prev_close = info_fields.get("prev_close") or hist_metrics.get("history_prev_close")
    currency = info_fields.get("currency") or "USD"
    market_cap = info_fields.get("market_cap")
    forward_pe = info_fields.get("forward_pe")
    trailing_pe = info_fields.get("trailing_pe")
    peg_ratio = info_fields.get("peg_ratio")
    earnings_growth = info_fields.get("earnings_growth")
    revenue_growth = info_fields.get("revenue_growth")

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
        "ytd_pct": hist_metrics["ytd_pct"],
        "one_year_pct": hist_metrics["one_year_pct"],
        "one_month_pct": hist_metrics["one_month_pct"],
        "one_week_pct": hist_metrics["one_week_pct"],
    }


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
    parser.add_argument("--dry-run", action="store_true", help="只打印,不写 DuckDB")
    parser.add_argument("--workers", type=int, default=int(os.getenv("STOCK_ASSISTANT_PRICE_WORKERS", "8")),
                        help="并发拉 yfinance info 的线程数（默认 8）")
    parser.add_argument("--info-ttl-hours", type=float,
                        default=float(os.getenv("STOCK_ASSISTANT_INFO_TTL_HOURS", DEFAULT_INFO_TTL_HOURS)),
                        help="估值/基本面 info 缓存小时数（默认 12）")
    parser.add_argument("--refresh-fundamentals", action="store_true",
                        help="忽略 info 缓存，强制刷新 PE/PEG/市值/增长率")
    args = parser.parse_args()

    print("[1/3] 拉取 watchlist [DuckDB]...")
    items = fetch_all_watchlist()
    print(f"  共 {len(items)} 条")

    jobs = []
    fail_codes = []
    for item in items:
        name = item.get("name") or ""
        code = item.get("code") or ""
        market = item.get("market") or ""

        if args.code and args.code != code:
            continue

        yf_code = to_yfinance_ticker(code, market)
        if not yf_code:
            print(f"  [跳过] {name} ({code}) — 无法转换 ticker")
            fail_codes.append(code)
            continue
        jobs.append({"item": item, "name": name, "code": code, "yf_code": yf_code})

    print("\n[2/3] 抓取价格（yfinance 批量历史价 + 并发 info）...")
    yf_codes = [j["yf_code"] for j in jobs]
    history_by_ticker = _download_history_batch(yf_codes)
    print(f"  历史价批量完成：{len(history_by_ticker)} / {len(set(yf_codes))} 个 ticker")

    info_cache = _load_info_cache()
    cache_items = info_cache.setdefault("items", {})
    info_by_ticker: dict[str, dict] = {}
    missing_info = []
    for tk in sorted(set(yf_codes)):
        cached = cache_items.get(tk)
        if not args.refresh_fundamentals and _cache_entry_fresh(cached, args.info_ttl_hours):
            info_by_ticker[tk] = cached
        else:
            missing_info.append(tk)

    if missing_info:
        workers = max(1, min(args.workers, len(missing_info)))
        print(f"  刷新 info：{len(missing_info)} 个 ticker · workers={workers}")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_fetch_info_fields, tk): tk for tk in missing_info}
            for fut in as_completed(futures):
                tk = futures[fut]
                fields = fut.result()
                if fields.get("error") and cache_items.get(tk):
                    fields = cache_items[tk]
                else:
                    cache_items[tk] = fields
                info_by_ticker[tk] = fields
        _save_info_cache(info_cache)
    else:
        print(f"  info 全部命中缓存（TTL={args.info_ttl_hours:g}h）")

    results = []
    success_count = 0
    for j in jobs:
        name = j["name"]
        code = j["code"]
        yf_code = j["yf_code"]

        print(f"  抓取 {name} ({yf_code})...", end=" ")
        data = fetch_price_data(
            yf_code,
            hist=history_by_ticker.get(yf_code),
            info_fields=info_by_ticker.get(yf_code),
        )
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

        results.append({
            "code": code,
            "name": name,
            "yf_ticker": yf_code,
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            **data,
        })

    print(f"\n[3/3] 完成：成功 {success_count} / 总 {len(items)}")
    if fail_codes:
        print(f"  失败标的：{', '.join(fail_codes)}")

    # 保存 JSON 快照
    out_file = os.path.join(DATA_DIR, f"prices_{datetime.now().strftime('%Y-%m-%d_%H%M')}.json")
    with open(out_file, "w", encoding="utf-8") as f_out:
        json.dump(results, f_out, ensure_ascii=False, indent=2, default=str)
    print(f"  快照已保存：{out_file}")

    if args.dry_run:
        print("  [Dry-Run] 跳过 DuckDB 写入")
        return

    # 落 DuckDB（按 fetched_at 的日期，同日多次抓取会覆盖）
    if results:
        try:
            n = upsert_prices(results)
            print(f"  DuckDB：已写入 {n} 行 (stock_history.duckdb · prices)")
        except Exception as e:
            print(f"  DuckDB 写入失败（不阻塞主流程）：{e}")


if __name__ == "__main__":
    main()
