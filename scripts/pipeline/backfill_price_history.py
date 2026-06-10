"""一次性/低频回填 price_daily 历史日 K（close 序列）。

背景（2026-06-11）：price_daily 本地只存了最近 ~16 天，动量（YTD/1W/1M/1Y）一直靠
fetch 每天向外部源下载全序列来算。FMP 历史端点限额低（~7-8/分钟即 402），扛不住批量；
回填后动量可改从 price_daily 本地序列算，每天只拉 quote。

策略：
- 只补 price_daily 里**缺失**的 (market,symbol,trade_date) bar（ON CONFLICT DO NOTHING），
  绝不覆盖 fetch 每天维护的最新行（含动量/估值字段）。可反复跑、可定期校验补缺。
- 历史源用 yfinance period（一次性、免费、不占 FMP 限额）。

用法：
  python3 backfill_price_history.py                      # 美股科技/AI universe 近 2 年
  python3 backfill_price_history.py --years 2 --limit 5  # 测试少量
  python3 backfill_price_history.py --market ALL         # 含港股/A 股
"""
import sys
import os

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))
sys.path.insert(0, os.path.join(_REPO, "scripts", "pipeline"))

import argparse
from datetime import datetime

import duckdb
import pandas as pd
import yfinance as yf

from stock_db import DB_PATH
from fetch_stock_prices import _load_price_items, to_yfinance_ticker, _market_code


def _download_history(yf_codes: list[str], years: int) -> dict[str, pd.DataFrame]:
    """批量下载 N 年历史价（复用 fetch 的 MultiIndex 拆分逻辑）。"""
    if not yf_codes:
        return {}
    unique = sorted(set(yf_codes))
    try:
        df = yf.download(
            " ".join(unique), period=f"{years}y", progress=False,
            group_by="ticker", threads=True, auto_adjust=False,
        )
    except Exception as e:
        print(f"  ⚠️ 批量历史下载失败：{e}")
        return {}
    out: dict[str, pd.DataFrame] = {}
    if df is None or df.empty:
        return out
    if isinstance(df.columns, pd.MultiIndex):
        lvl0 = set(df.columns.get_level_values(0))
        for tk in unique:
            if tk in lvl0:
                sub = df[tk].dropna(how="all")
                if not sub.empty:
                    out[tk] = sub
    elif len(unique) == 1:
        out[unique[0]] = df.dropna(how="all")
    return out


def backfill(source: str = "tech-universe", years: int = 2,
             market_filter: str | None = "US", limit: int = 0,
             db_path: str = DB_PATH) -> dict:
    items = _load_price_items(source)
    jobs = []
    for it in items:
        code = str(it.get("code") or "").strip()
        if not code:
            continue
        market = _market_code(code, str(it.get("market") or ""))
        if market_filter and market != market_filter:
            continue
        yf_code = to_yfinance_ticker(code, it.get("market"))
        if not yf_code:
            continue
        jobs.append((code.upper(), market, yf_code))
    if limit:
        jobs = jobs[:limit]
    print(f"回填目标：{len(jobs)} 只（market={market_filter or 'ALL'}, {years}y）")

    hist = _download_history([j[2] for j in jobs], years)
    print(f"历史下载完成：{len(hist)}/{len(jobs)} 只有数据")

    now = datetime.now()
    con = duckdb.connect(db_path)
    total_new = 0
    done = 0
    for symbol, market, yf_code in jobs:
        df = hist.get(yf_code)
        if df is None or df.empty or "Close" not in df:
            continue
        close = df["Close"].dropna()
        currency = "USD" if market == "US" else ("HKD" if market == "HK" else "CNY")
        rows = []
        prev = None
        for ts, c in close.items():
            try:
                d = ts.date()
            except Exception:
                continue
            rows.append((
                market, symbol, d, "1d", float(c),
                float(prev) if prev is not None else None, currency,
                None, None, None, None, None, None, None, None,
                "yfinance_backfill", now, now,
            ))
            prev = float(c)
        if not rows:
            continue
        before = con.execute(
            "SELECT COUNT(*) FROM price_daily WHERE market=? AND symbol=? AND interval='1d'",
            [market, symbol]).fetchone()[0]
        con.executemany(
            """
            INSERT INTO price_daily (
                market, symbol, trade_date, interval, close, prev_close, currency,
                market_cap, forward_pe, trailing_pe, peg_ratio, ytd_pct,
                one_week_pct, one_month_pct, one_year_pct, source,
                source_updated_at, fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (market, symbol, trade_date, interval) DO NOTHING
            """, rows)
        after = con.execute(
            "SELECT COUNT(*) FROM price_daily WHERE market=? AND symbol=? AND interval='1d'",
            [market, symbol]).fetchone()[0]
        total_new += (after - before)
        done += 1
    con.close()
    print(f"回填完成：{done} 只写入 · 新增 {total_new} bars（已存的 bar 跳过，不覆盖现有动量字段）")
    return {"symbols": done, "new_bars": total_new}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", default="tech-universe")
    p.add_argument("--years", type=int, default=2)
    p.add_argument("--market", default="US", help="US/HK/CN，或 ALL 全市场")
    p.add_argument("--limit", type=int, default=0, help="只回填前 N 只（测试用）")
    a = p.parse_args()
    mkt = None if str(a.market).upper() == "ALL" else a.market
    backfill(a.source, a.years, mkt, a.limit)
