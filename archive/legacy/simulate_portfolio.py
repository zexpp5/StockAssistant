"""
方案 A · 50 万 RMB 组合 · 蒙特卡洛模拟（5 个交易日）
─────────────────────────────────────────
基于每只股票过去 90 天的日收益率（均值+标准差），假设服从对数正态分布，
用 1000 次模拟生成未来 5 天的可能价格路径。

输出：
  • simulation_plan_a.json — HTML 端读取
  • 终端打印 + JSON 文件 + DuckDB 缓存（可选）

⚠️ 这是基于历史波动的统计模拟，不是对未来的预测
"""
import sys, os, json
import numpy as np
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yfinance as yf

# ============================================================
# 方案 A 组合（50 万 RMB）
# 仓位顺序：核心 50% → 卫星 25% → 高赔率 10% → A股/港股 10% → 现金 5%
# ============================================================
PORTFOLIO = [
    # (display_name, yfinance_ticker, RMB amount, currency, target pct)
    ("NVDA",        "NVDA",       60000, "USD", 0.12),
    ("TSM",         "TSM",        50000, "USD", 0.10),
    ("GOOGL",       "GOOGL",      50000, "USD", 0.10),
    ("MSFT",        "MSFT",       50000, "USD", 0.10),
    ("AMD",         "AMD",        40000, "USD", 0.08),
    ("Vertiv",      "VRT",        50000, "USD", 0.10),
    ("Lynas",       "LYC.AX",     40000, "AUD", 0.08),
    ("Cameco",      "CCJ",        35000, "USD", 0.07),
    ("Datadog",     "DDOG",       25000, "USD", 0.05),
    ("中际旭创",      "300308.SZ",  25000, "CNY", 0.05),
    ("阿里巴巴",      "9988.HK",    25000, "HKD", 0.05),
    ("海光信息",      "688041.SS",  25000, "CNY", 0.05),
]
CASH_RMB = 25000
TOTAL_CAPITAL = 500000

# 简化汇率（实际使用应该实时拉，这里用近似值）
FX_TO_RMB = {"USD": 7.10, "HKD": 0.91, "AUD": 4.60, "CNY": 1.0, "KRW": 0.0052, "JPY": 0.046, "GBP": 9.0}

N_SIMULATIONS = 1000
DAYS = 5

STOPLOSS_LINE = 300000
WARNING_LINE = 400000
TARGET_LINE = 550000


def fetch_returns(ticker, lookback_days=90):
    """拉过去 ~90 个交易日的日收益率统计"""
    end = datetime.now()
    start = end - timedelta(days=lookback_days + 30)
    try:
        t = yf.Ticker(ticker)
        hist = t.history(start=start, end=end)
        if len(hist) < 20:
            return None
        prices = hist["Close"]
        returns = prices.pct_change().dropna()
        return {
            "mean": float(returns.mean()),
            "std": float(returns.std()),
            "last_price": float(prices.iloc[-1]),
            "last_date": str(prices.index[-1].date()),
            "samples": int(len(returns)),
            "min_return": float(returns.min()),
            "max_return": float(returns.max()),
        }
    except Exception as e:
        print(f"  ! {ticker}: {e}")
        return None


def main():
    print("=" * 70)
    print(f"  📊 方案 A · 50 万 RMB · 蒙特卡洛模拟（{N_SIMULATIONS} 次 × {DAYS} 天）")
    print("=" * 70)

    # 1. 拉每只股票的历史收益率
    print("\n[1/3] 拉取过去 90 天历史价格...")
    stats = {}
    for name, ticker, rmb, ccy, pct in PORTFOLIO:
        info = fetch_returns(ticker)
        if info:
            stats[ticker] = {**info, "name": name, "rmb_amount": rmb, "currency": ccy, "target_pct": pct}
            ann_vol = info["std"] * np.sqrt(252) * 100
            print(f"  ✅ {name:<14}{ticker:<14}最新 {info['last_price']:.2f} {ccy} · 日波动 {info['std']*100:.2f}% · 年化波动 {ann_vol:.1f}%")
        else:
            print(f"  ❌ {name:<14}{ticker:<14}失败")

    # 2. 蒙特卡洛模拟
    print(f"\n[2/3] 蒙特卡洛 {N_SIMULATIONS} 次 × {DAYS} 天...")
    np.random.seed(42)  # 固定种子让结果可复现
    paths = {}
    for ticker, info in stats.items():
        # N x DAYS 矩阵：每行是一次模拟
        random_returns = np.random.normal(info["mean"], info["std"], (N_SIMULATIONS, DAYS))
        price_paths = info["last_price"] * np.cumprod(1 + random_returns, axis=1)
        # 加 D0（今天）
        d0 = np.full((N_SIMULATIONS, 1), info["last_price"])
        paths[ticker] = np.concatenate([d0, price_paths], axis=1)  # N x (DAYS+1)

    # 3. 计算每天组合总价值（RMB）
    portfolio_values = np.zeros((N_SIMULATIONS, DAYS + 1))
    portfolio_values += CASH_RMB

    for name, ticker, rmb, ccy, pct in PORTFOLIO:
        if ticker not in stats:
            portfolio_values += rmb
            continue
        info = stats[ticker]
        fx = FX_TO_RMB.get(ccy, 1.0)
        # 持仓：rmb 投资换算成"本币股数"，再用未来价格反推
        local_shares = rmb / (info["last_price"] * fx)
        # 每个时点的本币市值（N x DAYS+1）
        local_value = local_shares * paths[ticker]
        # 转成 RMB（汇率不变假设）
        rmb_value = local_value * fx
        portfolio_values += rmb_value

    # 减掉初始的"持仓 RMB 现金"，因为已经被股价覆盖
    # 实际上：上面已经把每只股票的 rmb 投资额转成了股价乘以股数，
    # 但 D0 时刻 paths[ticker][0] = last_price，所以 D0 = rmb 投资额，正确
    # 加上 CASH_RMB 后 D0 = TOTAL_CAPITAL ✅

    # 验证 D0 价值 ≈ 50 万
    d0_avg = portfolio_values[:, 0].mean()
    print(f"\n  D0 平均组合价值（应≈500,000）：{d0_avg:,.0f} RMB")

    # 4. 报告
    print(f"\n[3/3] 生成报告...")
    print("\n" + "=" * 70)
    print(f"  📊 5 天后组合价值分布（蒙特卡洛 {N_SIMULATIONS} 次）")
    print("=" * 70)

    final = portfolio_values[:, -1]
    p5, p25, p50, p75, p95 = np.percentile(final, [5, 25, 50, 75, 95])

    def fmt(label, v):
        pct = (v / TOTAL_CAPITAL - 1) * 100
        sign = "+" if pct >= 0 else ""
        return f"  {label:<22}{v:>14,.0f} RMB    {sign}{pct:.2f}%"

    print(fmt("最差 5%（熊市）", p5))
    print(fmt("下四分位", p25))
    print(fmt("⭐ 中位数（base case）", p50))
    print(fmt("上四分位", p75))
    print(fmt("最好 5%（牛市）", p95))

    n = N_SIMULATIONS
    p_stop = (portfolio_values <= STOPLOSS_LINE).any(axis=1).sum() / n * 100
    p_warn = (portfolio_values <= WARNING_LINE).any(axis=1).sum() / n * 100
    p_target = (portfolio_values >= TARGET_LINE).any(axis=1).sum() / n * 100
    p_profit = (final > TOTAL_CAPITAL).sum() / n * 100

    print(f"\n🎯 关键概率（{DAYS} 天内任一时点触及）")
    print(f"  跌至 30 万止损线 (-40%)：     {p_stop:>6.2f}%")
    print(f"  跌至 40 万预警线 (-20%)：     {p_warn:>6.2f}%")
    print(f"  涨至 55 万 (+10%)：            {p_target:>6.2f}%")
    print(f"  5 天后保持盈利：               {p_profit:>6.2f}%")

    print(f"\n📅 三种情景下每天的组合价值")
    print(f"  {'天数':<10}{'熊市 5%':>14}{'中位 50%':>14}{'牛市 95%':>14}")
    print(f"  {'-'*52}")
    for d in range(DAYS + 1):
        c5 = np.percentile(portfolio_values[:, d], 5)
        c50 = np.percentile(portfolio_values[:, d], 50)
        c95 = np.percentile(portfolio_values[:, d], 95)
        label = "D0（今天）" if d == 0 else f"D+{d} 天"
        print(f"  {label:<10}{c5:>14,.0f}{c50:>14,.0f}{c95:>14,.0f}")

    print(f"\n💼 中位情景下每只股票 D5 的盈亏")
    print(f"  {'股票':<14}{'起价':>10}{'D5 中位':>10}{'涨跌%':>10}{'仓位 RMB 盈亏':>16}")
    print(f"  {'-'*65}")
    median_pnl_per_stock = []
    total_stock_pnl = 0
    for name, ticker, rmb, ccy, pct in PORTFOLIO:
        if ticker not in stats:
            continue
        info = stats[ticker]
        d5_median = float(np.percentile(paths[ticker][:, -1], 50))
        delta_pct = (d5_median / info["last_price"] - 1) * 100
        pnl = rmb * (d5_median / info["last_price"] - 1)
        total_stock_pnl += pnl
        sign = "+" if delta_pct >= 0 else ""
        print(f"  {name:<14}{info['last_price']:>10.2f}{d5_median:>10.2f}{sign}{delta_pct:>9.2f}%  {pnl:>+13,.0f}")
        median_pnl_per_stock.append({
            "name": name, "ticker": ticker, "rmb_amount": rmb,
            "entry": info["last_price"], "d5_median": d5_median,
            "delta_pct": delta_pct, "pnl_rmb": pnl,
        })
    print(f"  {'-'*65}")
    print(f"  {'股票仓位盈亏（中位）':<35}                      {total_stock_pnl:>+13,.0f}")

    # 输出 JSON
    out = {
        "generated_at": datetime.now().isoformat(),
        "n_simulations": N_SIMULATIONS,
        "days": DAYS,
        "total_capital": TOTAL_CAPITAL,
        "cash_rmb": CASH_RMB,
        "stoploss_line": STOPLOSS_LINE,
        "warning_line": WARNING_LINE,
        "target_line": TARGET_LINE,

        "stock_stats": {
            ticker: {
                "name": info["name"],
                "ticker": ticker,
                "currency": info["currency"],
                "rmb_amount": info["rmb_amount"],
                "target_pct": info["target_pct"],
                "last_price": info["last_price"],
                "last_date": info["last_date"],
                "daily_mean_pct": round(info["mean"] * 100, 4),
                "daily_std_pct": round(info["std"] * 100, 4),
                "annual_vol_pct": round(info["std"] * np.sqrt(252) * 100, 2),
                "samples": info["samples"],
                "shares": round(info["rmb_amount"] / (info["last_price"] * FX_TO_RMB.get(info["currency"], 1.0)), 4),
            }
            for ticker, info in stats.items()
        },

        "value_distribution_d5": {
            "p5": float(p5), "p25": float(p25), "p50": float(p50),
            "p75": float(p75), "p95": float(p95),
        },
        "probabilities": {
            "stoploss_30w": round(float(p_stop), 2),
            "warning_40w": round(float(p_warn), 2),
            "target_55w": round(float(p_target), 2),
            "profit_after_5d": round(float(p_profit), 2),
        },
        "daily_paths": {
            "p5": [float(np.percentile(portfolio_values[:, d], 5)) for d in range(DAYS + 1)],
            "p25": [float(np.percentile(portfolio_values[:, d], 25)) for d in range(DAYS + 1)],
            "p50": [float(np.percentile(portfolio_values[:, d], 50)) for d in range(DAYS + 1)],
            "p75": [float(np.percentile(portfolio_values[:, d], 75)) for d in range(DAYS + 1)],
            "p95": [float(np.percentile(portfolio_values[:, d], 95)) for d in range(DAYS + 1)],
        },
        "median_per_stock": median_pnl_per_stock,
        "fx_assumed": FX_TO_RMB,
    }

    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "simulation_plan_a.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 完整模拟数据：{out_file}")


if __name__ == "__main__":
    main()
