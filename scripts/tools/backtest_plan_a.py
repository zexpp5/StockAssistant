"""
方案 A · 5/1 → 5/9 真实数据回测
─────────────────────────────────────────
假设 2026-05-01 按方案 A 建仓 50 万 RMB，看 9 天后真实表现。

每只股票用 yfinance 拉 5/1 → 5/9 的真实收盘价。
A 股 / 港股 5/1-5/5 假期，自动取最近交易日。

输出 backtest_plan_a.json，HTML 端可显示。
"""
import sys, os, json, argparse
import numpy as np
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts", "lib"))  # 2026-05-11 lib 迁移
import yfinance as yf

PORTFOLIO = [
    ("NVDA",        "NVDA",       60000, "USD", 0.12),
    ("TSM",         "TSM",        50000, "USD", 0.10),
    ("GOOGL",       "GOOGL",      50000, "USD", 0.10),
    ("MSFT",        "MSFT",       50000, "USD", 0.10),
    ("AMD",         "AMD",        40000, "USD", 0.08),
    ("Vertiv",      "VRT",        50000, "USD", 0.10),
    ("北方稀土",     "600111.SS",  40000, "CNY", 0.08),  # v2: Lynas → 北方稀土
    ("Cameco",      "CCJ",        35000, "USD", 0.07),
    ("Datadog",     "DDOG",       25000, "USD", 0.05),
    ("中际旭创",      "300308.SZ",  25000, "CNY", 0.05),
    ("阿里巴巴",      "9988.HK",    25000, "HKD", 0.05),
    ("海光信息",      "688041.SS",  25000, "CNY", 0.05),
]
CASH_RMB = 25000
TOTAL_CAPITAL = 500000

FX_TO_RMB = {"USD": 7.10, "HKD": 0.91, "AUD": 4.60, "CNY": 1.0}

# 命令行参数（默认 5/1 → 5/9，可改 --start-date 跑其他周期）
_parser = argparse.ArgumentParser()
_parser.add_argument("--start-date", default="2026-05-01", help="回测起始日 YYYY-MM-DD")
_parser.add_argument("--end-date", default="2026-05-10", help="回测结束日（exclusive）")
_parser.add_argument("--label", default="", help="结果文件后缀（如 1week / 1month）")
_args, _ = _parser.parse_known_args()

START_DATE = _args.start_date
END_DATE = _args.end_date
LABEL = _args.label


def fetch_period(ticker):
    """拉 5/1 → 5/9 的每日收盘价"""
    try:
        t = yf.Ticker(ticker)
        hist = t.history(start=START_DATE, end=END_DATE)
        if len(hist) == 0:
            return None
        return {
            "dates": [str(d.date()) for d in hist.index],
            "closes": [float(c) for c in hist["Close"]],
            "first_date": str(hist.index[0].date()),
            "last_date": str(hist.index[-1].date()),
            "first_price": float(hist["Close"].iloc[0]),
            "last_price": float(hist["Close"].iloc[-1]),
            "n_days": len(hist),
        }
    except Exception as e:
        print(f"  ! {ticker}: {e}")
        return None


def main():
    print("=" * 70)
    print(f"  📊 方案 A · 真实回测：{START_DATE} → 2026-05-09")
    print("=" * 70)

    print("\n[1/2] 拉取期间真实价格...")
    data = {}
    for name, ticker, rmb, ccy, pct in PORTFOLIO:
        info = fetch_period(ticker)
        if info:
            print(f"  ✅ {name:<14}{ticker:<14}{info['first_date']} → {info['last_date']} ({info['n_days']} 天)")
            print(f"     起 {info['first_price']:.2f} → 终 {info['last_price']:.2f} = {(info['last_price']/info['first_price']-1)*100:+.2f}%")
            data[ticker] = info
        else:
            print(f"  ❌ {name:<14}{ticker:<14}失败")

    # 计算每只股票的 RMB 仓位变化
    print("\n[2/2] 计算组合表现...")

    # 按每个交易日聚合（不同市场交易日不同，需要对齐）
    # 找到所有出现过的日期
    all_dates = set()
    for info in data.values():
        all_dates.update(info["dates"])
    sorted_dates = sorted(all_dates)

    # 按日期构建价格表（每只股票 forward-fill）
    prices_by_date = {}  # {date: {ticker: price}}
    for ticker, info in data.items():
        date_to_price = dict(zip(info["dates"], info["closes"]))
        last_known = info["closes"][0]
        for d in sorted_dates:
            if d in date_to_price:
                last_known = date_to_price[d]
            prices_by_date.setdefault(d, {})[ticker] = last_known

    # 计算每天组合 RMB 价值
    daily_portfolio = []
    holdings = {}  # ticker → 本币股数（按 5/1 第一个有效价计算）

    for name, ticker, rmb, ccy, pct in PORTFOLIO:
        if ticker not in data:
            continue
        info = data[ticker]
        fx = FX_TO_RMB.get(ccy, 1.0)
        # 5/1（或最近交易日）建仓：用 first_price 作为成本
        local_shares = rmb / (info["first_price"] * fx)
        holdings[ticker] = {"shares": local_shares, "fx": fx, "cost_rmb": rmb, "name": name, "ccy": ccy}

    for d in sorted_dates:
        total_value = CASH_RMB
        per_stock = {}
        for ticker, h in holdings.items():
            price = prices_by_date[d].get(ticker)
            if price is None:
                continue
            local_value = price * h["shares"]
            rmb_value = local_value * h["fx"]
            total_value += rmb_value
            per_stock[ticker] = {
                "price": price,
                "rmb_value": rmb_value,
                "pnl_rmb": rmb_value - h["cost_rmb"],
                "pct": (rmb_value / h["cost_rmb"] - 1) * 100,
            }
        daily_portfolio.append({
            "date": d,
            "total_value": total_value,
            "pnl": total_value - TOTAL_CAPITAL,
            "pct": (total_value / TOTAL_CAPITAL - 1) * 100,
            "per_stock": per_stock,
        })

    # 打印日表
    print("\n📅 每日组合价值变化：")
    print(f"  {'日期':<12}{'组合价值':>14}{'盈亏 RMB':>14}{'累计涨跌%':>12}")
    print(f"  {'-'*52}")
    for day in daily_portfolio:
        sign = "+" if day["pnl"] >= 0 else ""
        print(f"  {day['date']:<12}{day['total_value']:>14,.0f}{sign}{day['pnl']:>+13,.0f}{sign}{day['pct']:>+10.2f}%")

    # 最终结果
    final = daily_portfolio[-1]
    print("\n" + "=" * 70)
    print(f"  💼 最终结果（5/1 → {final['date']}）")
    print("=" * 70)
    sign = "+" if final["pnl"] >= 0 else ""
    print(f"  起始资金：500,000 RMB")
    print(f"  当前价值：{final['total_value']:,.0f} RMB")
    print(f"  盈亏：    {sign}{final['pnl']:,.0f} RMB（{sign}{final['pct']:.2f}%）")

    # 每只股票回测结果
    print(f"\n💼 每只股票期间表现")
    print(f"  {'股票':<14}{'起价':>10}{'终价':>10}{'涨跌%':>10}{'RMB 盈亏':>14}")
    print(f"  {'-'*65}")
    sorted_stocks = sorted(
        [(name, t, holdings[t], data[t]) for name, t, _, _, _ in PORTFOLIO if t in data],
        key=lambda x: (x[3]["last_price"] / x[3]["first_price"] - 1),
        reverse=True,
    )
    for name, ticker, h, info in sorted_stocks:
        delta_pct = (info["last_price"] / info["first_price"] - 1) * 100
        pnl = h["cost_rmb"] * (info["last_price"] / info["first_price"] - 1)
        sign = "+" if delta_pct >= 0 else ""
        print(f"  {name:<14}{info['first_price']:>10.2f}{info['last_price']:>10.2f}{sign}{delta_pct:>9.2f}%  {pnl:>+13,.0f}")

    # 输出 JSON
    out = {
        "generated_at": datetime.now().isoformat(),
        "start_date": START_DATE,
        "end_date": daily_portfolio[-1]["date"] if daily_portfolio else END_DATE,
        "total_capital": TOTAL_CAPITAL,
        "cash_rmb": CASH_RMB,
        "final": {
            "total_value": final["total_value"],
            "pnl_rmb": final["pnl"],
            "pct": final["pct"],
        },
        "daily_portfolio": daily_portfolio,
        "per_stock_summary": [
            {
                "name": name, "ticker": ticker, "currency": h["ccy"],
                "rmb_amount": h["cost_rmb"],
                "first_price": data[ticker]["first_price"],
                "last_price": data[ticker]["last_price"],
                "delta_pct": (data[ticker]["last_price"] / data[ticker]["first_price"] - 1) * 100,
                "pnl_rmb": h["cost_rmb"] * (data[ticker]["last_price"] / data[ticker]["first_price"] - 1),
                "first_date": data[ticker]["first_date"],
                "last_date": data[ticker]["last_date"],
            }
            for name, ticker, _, _, _ in PORTFOLIO if ticker in data
            for h in [holdings[ticker]]
        ],
        "fx_assumed": FX_TO_RMB,
    }

    suffix = f"_{LABEL}" if LABEL else ""
    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"backtest_plan_a{suffix}.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 完整回测数据：{out_file}")


if __name__ == "__main__":
    main()
