"""
方案 A · 专业风险指标计算器
─────────────────────────────────────────
基于过去 1 年（约 252 个交易日）的历史日收益，计算华尔街标准指标：

  • 年化收益率
  • 年化波动率
  • Sharpe 比率（rf 假设 4.5%）
  • Sortino 比率（仅下行波动）
  • Calmar 比率（年化收益 / 最大回撤）
  • Max Drawdown（最大回撤）
  • VaR 95% / 99%（在险价值）
  • CVaR 95% / 99%（条件 VaR / Expected Shortfall）
  • Beta（vs SPY）

⚠️ 历史数据不代表未来；这些指标只是统计描述
"""
import sys, os, json, argparse
import numpy as np
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yfinance as yf

# 方案 A v2（北方稀土版）
PORTFOLIO = [
    ("NVDA",       "NVDA",       60000, "USD", 0.12),
    ("TSM",        "TSM",        50000, "USD", 0.10),
    ("GOOGL",      "GOOGL",      50000, "USD", 0.10),
    ("MSFT",       "MSFT",       50000, "USD", 0.10),
    ("AMD",        "AMD",        40000, "USD", 0.08),
    ("Vertiv",     "VRT",        50000, "USD", 0.10),
    ("北方稀土",     "600111.SS",  40000, "CNY", 0.08),
    ("Cameco",     "CCJ",        35000, "USD", 0.07),
    ("Datadog",    "DDOG",       25000, "USD", 0.05),
    ("中际旭创",     "300308.SZ",  25000, "CNY", 0.05),
    ("阿里巴巴",     "9988.HK",    25000, "HKD", 0.05),
    ("海光信息",     "688041.SS",  25000, "CNY", 0.05),
]
CASH_RMB = 25000
TOTAL_CAPITAL = 500000

FX_TO_RMB = {"USD": 7.10, "HKD": 0.91, "AUD": 4.60, "CNY": 1.0}
RISK_FREE_RATE = 0.045  # 美国 10Y 国债收益率 ~4.5%
TRADING_DAYS = 252


def fetch_history(ticker, lookback_days=400):
    end = datetime.now()
    start = end - timedelta(days=lookback_days)
    try:
        t = yf.Ticker(ticker)
        hist = t.history(start=start, end=end)
        if len(hist) < 100:
            return None
        return hist["Close"]
    except Exception as e:
        print(f"  ! {ticker}: {e}")
        return None


def main():
    print("=" * 70)
    print("  📊 方案 A · 华尔街标准风险指标")
    print("=" * 70)

    # 1) 拉所有标的历史
    print("\n[1/3] 拉过去 ~1 年历史...")
    series = {}
    for name, ticker, rmb, ccy, pct in PORTFOLIO:
        s = fetch_history(ticker)
        if s is not None:
            series[ticker] = s
            print(f"  ✅ {name:<14}{ticker:<14}{len(s)} 个数据点")
        else:
            print(f"  ❌ {name:<14}{ticker:<14}失败")

    # 同时拉 SPY 作为基准
    spy = fetch_history("SPY")

    # 2) 构造组合每日 RMB 价值序列
    print("\n[2/3] 构造组合每日 RMB 价值...")

    # 关键：用「所有 12 只股票都有数据的最早日期」作为统一起点 D0
    # 这样每只股票的「first_price」是 D0 那天的价格（一致基准）
    earliest_dates = [s.index[0].date() for s in series.values()]
    D0 = max(earliest_dates)
    print(f"  统一起点 D0 = {D0}（所有 12 只都有数据的最早一天）")

    # 找出 D0 当天每只的价格作为起始成本（用 .asof 或近似）
    first_prices = {}
    for ticker, s in series.items():
        # 取 D0 当天或之后第一天的价格
        s_after = s[s.index.date >= D0]
        if len(s_after) == 0:
            print(f"  ! {ticker} 在 D0 后无数据")
            continue
        first_prices[ticker] = float(s_after.iloc[0])

    # 找所有日期 ≥ D0 的并集
    all_dates = set()
    for s in series.values():
        all_dates.update([d for d in s.index.date if d >= D0])
    sorted_dates = sorted(all_dates)
    print(f"  共 {len(sorted_dates)} 个交易日（D0 之后）")

    # 每日组合价值（forward fill 跨市场）
    portfolio_values = []
    last_price = {ticker: first_prices.get(ticker) for ticker in series}

    for d in sorted_dates:
        for ticker, s in series.items():
            for ts, p in s.items():
                if ts.date() == d:
                    last_price[ticker] = float(p)
                    break

        total = CASH_RMB
        all_have_price = True
        for name, ticker, rmb, ccy, _pct in PORTFOLIO:
            if ticker not in first_prices or last_price[ticker] is None:
                all_have_price = False
                break
            fx = FX_TO_RMB.get(ccy, 1.0)
            shares = rmb / (first_prices[ticker] * fx)
            total += shares * last_price[ticker] * fx

        if all_have_price:
            portfolio_values.append((d, total))

    if len(portfolio_values) < 50:
        print("  数据不足，无法计算")
        return

    print(f"  共 {len(portfolio_values)} 天有完整数据")

    # 3) 计算指标
    print("\n[3/3] 计算专业指标...")
    values = np.array([v for _, v in portfolio_values])
    returns = np.diff(values) / values[:-1]  # 日收益率

    n_days = len(returns)
    annual_factor = TRADING_DAYS / n_days

    # 年化收益率（CAGR）
    total_return = values[-1] / values[0] - 1
    years = n_days / TRADING_DAYS
    cagr = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

    # 年化波动率
    annual_vol = returns.std() * np.sqrt(TRADING_DAYS)

    # Sharpe
    excess_return = cagr - RISK_FREE_RATE
    sharpe = excess_return / annual_vol if annual_vol > 0 else 0

    # Sortino（仅下行）
    downside_returns = returns[returns < 0]
    downside_vol = downside_returns.std() * np.sqrt(TRADING_DAYS) if len(downside_returns) > 0 else annual_vol
    sortino = excess_return / downside_vol if downside_vol > 0 else 0

    # Max Drawdown
    cumulative_max = np.maximum.accumulate(values)
    drawdowns = (values - cumulative_max) / cumulative_max
    max_dd = drawdowns.min()
    max_dd_idx = drawdowns.argmin()
    max_dd_date = portfolio_values[max_dd_idx][0]

    # Calmar
    calmar = cagr / abs(max_dd) if max_dd < 0 else 0

    # VaR / CVaR
    # 转成 RMB 损失
    daily_pnl = returns * TOTAL_CAPITAL  # 假设当前组合 50 万
    var_95 = np.percentile(daily_pnl, 5)  # 5% 分位（最差 5%）
    var_99 = np.percentile(daily_pnl, 1)
    cvar_95 = daily_pnl[daily_pnl <= var_95].mean()
    cvar_99 = daily_pnl[daily_pnl <= var_99].mean()

    # Beta（vs SPY）
    beta = None
    if spy is not None and len(spy) > 50:
        spy_returns = spy.pct_change().dropna().values
        # 对齐长度
        min_len = min(len(returns), len(spy_returns))
        if min_len > 50:
            r = returns[-min_len:]
            sr = spy_returns[-min_len:]
            cov = np.cov(r, sr)[0, 1]
            var_spy = sr.var()
            beta = cov / var_spy if var_spy > 0 else None

    # 4) 报告
    print("\n" + "=" * 70)
    print("  📊 方案 A 风险指标报告")
    print("=" * 70)

    period = f"{portfolio_values[0][0]} → {portfolio_values[-1][0]}（{n_days} 个交易日）"
    print(f"\n📅 回顾期间：{period}")

    print("\n💼 收益指标")
    print(f"  起点价值：       500,000 RMB")
    print(f"  终点价值：       {values[-1]:,.0f} RMB")
    print(f"  累计收益：       {total_return*100:+.2f}%")
    print(f"  年化收益（CAGR）：{cagr*100:+.2f}%")

    print("\n📉 风险指标")
    print(f"  年化波动率：     {annual_vol*100:.2f}%")
    print(f"  最大回撤：       {max_dd*100:.2f}%（{max_dd_date}）")
    print(f"  下行波动率：     {downside_vol*100:.2f}%")
    if beta is not None:
        print(f"  Beta（vs SPY）： {beta:.2f}")

    print("\n🎯 风险调整收益指标")
    print(f"  Sharpe 比率：    {sharpe:.2f}  {'✅ 优秀' if sharpe > 1.5 else '🟢 良好' if sharpe > 1 else '🟡 一般' if sharpe > 0.5 else '🔴 差'}")
    print(f"  Sortino 比率：   {sortino:.2f}  {'✅ 优秀' if sortino > 2 else '🟢 良好' if sortino > 1.5 else '🟡 一般'}")
    print(f"  Calmar 比率：    {calmar:.2f}  {'✅ 优秀' if calmar > 3 else '🟢 良好' if calmar > 1 else '🟡 一般'}")

    print(f"\n⚠️ VaR / CVaR（基于 {TOTAL_CAPITAL:,} RMB 当前规模）")
    print(f"  1 日 95% VaR：   {var_95:>14,.0f} RMB（最差 5%）")
    print(f"  1 日 99% VaR：   {var_99:>14,.0f} RMB（最差 1%）")
    print(f"  1 日 95% CVaR：  {cvar_95:>14,.0f} RMB（最差 5% 的平均）")
    print(f"  1 日 99% CVaR：  {cvar_99:>14,.0f} RMB（最差 1% 的平均）")

    # 解读
    print("\n💡 关键解读")
    if max_dd < -0.40:
        print(f"  ⚠️ 最大回撤 {max_dd*100:.2f}% 已超你 -40% 红线 — 警告！")
    elif max_dd < -0.25:
        print(f"  ⚠️ 最大回撤 {max_dd*100:.2f}% 接近 -40% 红线 — 注意")
    else:
        print(f"  🟢 最大回撤 {max_dd*100:.2f}% 在 -40% 红线内")

    print(f"  📊 95% 置信度，1 天最多损失 {abs(var_95):.0f} RMB（{abs(var_95)/TOTAL_CAPITAL*100:.2f}%）")
    print(f"  💀 真正坏的日子（最差 1%）平均损失 {abs(cvar_99):.0f} RMB（{abs(cvar_99)/TOTAL_CAPITAL*100:.2f}%）")

    if sharpe > 1.5:
        print(f"  ✅ Sharpe {sharpe:.2f} 优秀（>1.5），风险调整后回报很好")
    elif sharpe < 0.5:
        print(f"  🔴 Sharpe {sharpe:.2f} 偏低，风险/回报比不理想")

    # 输出 JSON
    out = {
        "generated_at": datetime.now().isoformat(),
        "period_start": str(portfolio_values[0][0]),
        "period_end": str(portfolio_values[-1][0]),
        "n_days": n_days,
        "total_return_pct": round(total_return * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "annual_vol_pct": round(annual_vol * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "max_dd_date": str(max_dd_date),
        "downside_vol_pct": round(downside_vol * 100, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "calmar": round(calmar, 2),
        "beta_vs_spy": round(beta, 2) if beta else None,
        "var_95_rmb": round(float(var_95), 0),
        "var_99_rmb": round(float(var_99), 0),
        "cvar_95_rmb": round(float(cvar_95), 0),
        "cvar_99_rmb": round(float(cvar_99), 0),
        "var_95_pct": round(float(var_95) / TOTAL_CAPITAL * 100, 2),
        "var_99_pct": round(float(var_99) / TOTAL_CAPITAL * 100, 2),
        "cvar_95_pct": round(float(cvar_95) / TOTAL_CAPITAL * 100, 2),
        "cvar_99_pct": round(float(cvar_99) / TOTAL_CAPITAL * 100, 2),
        "risk_free_rate": RISK_FREE_RATE,
        "daily_values": [{"date": str(d), "value": float(v)} for d, v in portfolio_values],
    }

    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "risk_metrics.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 完整数据：{out_file}")


if __name__ == "__main__":
    main()
