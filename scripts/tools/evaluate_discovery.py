"""evaluate_discovery.py — 候选发现推荐的准确度评估
─────────────────────────────────────────────────────
每天跑一次。读 discovery_history 过去 70 天的所有推荐,用 yfinance 拉价格,
算每个推荐在 1d/5d/20d/60d 后的涨跌 + 同期 benchmark 的涨跌 + alpha,
UPSERT 到 discovery_tracking 表。

为什么 70 天:60 交易日 ≈ 90 自然日, 多 10 天 buffer 避免边界。

数据流:
  discovery_history (静态快照)
       +
  yfinance 历史价 (entry_price ~ entry+60d)
       +
  benchmark 同期价 (SPY/000300.SS/^HSI 等)
       ↓
  discovery_tracking (entry_price/pct_*/alpha_*)
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))

import yfinance as yf
from stock_db import get_db, upsert_discovery_tracking

WINDOWS = [1, 5, 20, 60]  # 交易日窗口


def pick_benchmark(ticker: str) -> str:
    """根据 ticker 后缀返回 benchmark code"""
    t = (ticker or "").upper()
    if t.endswith(".SS") or t.endswith(".SZ"):
        return "000300.SS"   # 沪深 300
    if t.endswith(".HK"):
        return "^HSI"         # 恒生指数
    if t.endswith(".T"):
        return "^N225"        # 日经 225
    if t.endswith(".KS") or t.endswith(".KQ"):
        return "^KS11"        # KOSPI
    if t.endswith(".AX"):
        return "^AXJO"        # ASX 200
    if t.endswith(".L") or t.endswith(".IL"):
        return "^FTSE"        # FTSE 100
    return "SPY"              # 美股默认


def fetch_close_series(ticker: str, start: date, end: date) -> dict[date, float]:
    """yfinance 拉日 K → {date: close}。end 是 inclusive 的语义。"""
    try:
        df = yf.Ticker(ticker).history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),  # yfinance end 是 exclusive
            auto_adjust=False,
        )
        if df.empty:
            return {}
        return {d.date(): float(c) for d, c in zip(df.index, df["Close"]) if c == c}
    except Exception as e:
        print(f"    ⚠️ {ticker} 拉价失败: {e}")
        return {}


def evaluate_one(gen_date: date, ticker: str) -> dict | None:
    """对一个 (generated_date, ticker) 算 entry_price + 4 个窗口的 pct/alpha。
    返回 None 表示价格拉不到或数据不足。
    """
    bench_code = pick_benchmark(ticker)
    today = date.today()
    end = min(today, gen_date + timedelta(days=100))  # 60 交易日 ≈ 90 自然日, 留点 buffer

    prices_t = fetch_close_series(ticker, gen_date, end)
    if not prices_t:
        return None
    prices_b = fetch_close_series(bench_code, gen_date, end)
    if not prices_b:
        return None

    sorted_t = sorted(prices_t.keys())
    sorted_b = sorted(prices_b.keys())
    if not sorted_t or not sorted_b:
        return None

    # entry_price = gen_date 之后第一个有数据的交易日的 close
    entry_t = prices_t[sorted_t[0]]
    entry_b = prices_b[sorted_b[0]]

    out: dict = {
        "generated_date": gen_date,
        "ticker": ticker,
        "entry_price": entry_t,
        "benchmark_code": bench_code,
        "last_refreshed_at": datetime.now(),
    }
    for n in WINDOWS:
        # entry 之后 +n 个交易日 (index n)
        if n < len(sorted_t):
            pct_t = (prices_t[sorted_t[n]] / entry_t - 1) * 100
        else:
            pct_t = None
        if n < len(sorted_b):
            pct_b = (prices_b[sorted_b[n]] / entry_b - 1) * 100
        else:
            pct_b = None
        alpha = (pct_t - pct_b) if (pct_t is not None and pct_b is not None) else None
        out[f"pct_{n}d"] = pct_t
        out[f"benchmark_pct_{n}d"] = pct_b
        out[f"alpha_{n}d"] = alpha
    return out


def main():
    conn = get_db()
    rows = conn.execute("""
        SELECT generated_date, ticker FROM discovery_history
        WHERE generated_date >= CURRENT_DATE - INTERVAL '70' DAY
        ORDER BY generated_date DESC, ticker
    """).fetchall()

    print(f"📊 evaluate_discovery: 待评估 {len(rows)} 条 (generated_date, ticker) 历史推荐")
    if not rows:
        print("  (无数据,等 discover_candidates.py 跑过几天再来)")
        conn.close()
        return

    n_ok, n_fail = 0, 0
    updates = []
    # benchmark 缓存:同 ticker 同时段重复拉是浪费 — 但为了简单先不缓存
    for i, (gen_date, ticker) in enumerate(rows, 1):
        result = evaluate_one(gen_date, ticker)
        if result:
            updates.append(result)
            n_ok += 1
        else:
            n_fail += 1
        if i % 20 == 0:
            print(f"  进度 {i}/{len(rows)} (成功 {n_ok} 失败 {n_fail})")

    if updates:
        upsert_discovery_tracking(updates, conn=conn)
    print(f"✅ evaluate_discovery 完成: 成功 {n_ok} 条 / 失败 {n_fail} 条")
    conn.close()


if __name__ == "__main__":
    main()
