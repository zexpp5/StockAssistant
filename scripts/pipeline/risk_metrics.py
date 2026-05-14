"""
方案 A v6 · 专业风险指标计算器
─────────────────────────────────────────
读 plan_a_v5.json 里 v6 risk-aware 当前推荐组合（保留旧文件名兼容下游），
基于过去 ~1 年历史日收益，计算华尔街标准指标：

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
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))  # 2026-05-11 lib 迁移
import numpy as np
from datetime import datetime, timedelta

import yfinance as yf

# 运行时从 plan_a_v5.json 动态加载（v6 risk-aware 当前推荐组合；文件名保留兼容）
PORTFOLIO = []
CASH_RMB = 25000  # 默认 5%，main() 会按 plan 的 cash_pct 重写
try:
    import stock_db
    TOTAL_CAPITAL = stock_db.get_config("total_capital")
except Exception:
    TOTAL_CAPITAL = 500000

FX_TO_RMB = {"USD": 7.10, "HKD": 0.91, "AUD": 4.60, "CNY": 1.0}
RISK_FREE_RATE = 0.045  # 美国 10Y 国债收益率 ~4.5%
TRADING_DAYS = 252


def _ccy_from_ticker(ticker: str) -> str:
    """根据 ticker 后缀推断币种。"""
    if ticker.endswith((".SS", ".SZ", ".BJ")):
        return "CNY"
    if ticker.endswith(".HK"):
        return "HKD"
    if ticker.endswith(".AX"):
        return "AUD"
    return "USD"


def load_portfolio_from_holdings():
    """优先读 DuckDB holdings 表（用户真实持仓）。

    返回 (portfolio, cash_pct, source)；无持仓时返回 (None, None, None)，
    上层 fallback 到 plan_a_v5.json。

    portfolio 格式：[(name, ticker, amount_rmb_at_cost, ccy, weight, shares_override), ...]
      - amount_rmb_at_cost = shares * entry_price * fx (用户实际投入)
      - weight = amount_rmb / total_cost
      - shares_override = 用户实际股数；真实持仓用它，plan fallback 才按 D0 价反推。
    """
    try:
        import stock_db
        holdings = stock_db.fetch_all_holdings()
    except Exception as e:
        print(f"  ⚠️  holdings 表读取失败，fallback plan: {e}")
        return None, None, None
    if not holdings:
        return None, None, None
    # 同 code 多笔合并：cost_rmb 累加
    agg: dict[str, dict] = {}
    for h in holdings:
        code = h.get("code")
        shares = float(h.get("shares") or 0)
        ep = float(h.get("entry_price") or 0)
        if not code or shares <= 0 or ep <= 0:
            continue
        ccy = _ccy_from_ticker(code)
        fx = FX_TO_RMB.get(ccy, 1.0)
        cost_rmb = shares * ep * fx
        if code not in agg:
            agg[code] = {"cost_rmb": 0.0, "ccy": ccy, "shares": 0.0}
        agg[code]["cost_rmb"] += cost_rmb
        agg[code]["shares"] += shares
    total_cost = sum(v["cost_rmb"] for v in agg.values())
    if total_cost <= 0:
        return None, None, None
    portfolio = []
    for code, v in agg.items():
        weight = v["cost_rmb"] / total_cost
        portfolio.append((code, code, v["cost_rmb"], v["ccy"], weight, v["shares"]))
    cash_pct = max(0.0, 1.0 - total_cost / TOTAL_CAPITAL)
    return portfolio, cash_pct, "holdings"


def load_portfolio_from_plan():
    """读 plan_a_v5.json，返回 (portfolio, cash_pct)。

    portfolio 格式：
      [(name, ticker, amount_rmb, ccy, weight, shares_override), ...]
    其中 name 用 ticker 顶替（plan_a_v5.json 无中文名字段），ccy 由后缀推断。
    """
    plan_file = os.path.join(_REPO, "data", "latest", "plan_a_v5.json")
    if not os.path.exists(plan_file):
        print(f"❌ {plan_file} 不存在 — 请先跑：python3 -m stock_research.jobs.optimize_portfolio")
        sys.exit(1)
    with open(plan_file, "r", encoding="utf-8") as f:
        plan = json.load(f)
    plan_list = plan.get("plan_v5") or plan.get("plan_v6") or plan.get("plan") or []
    constraints = plan.get("constraints", {})
    cash_pct = (
        constraints.get("cash_pct_effective")
        if constraints.get("cash_pct_effective") is not None
        else constraints.get("cash_pct", 0.05)
    )
    portfolio = []
    for p in plan_list:
        ticker = p.get("ticker")
        amount_rmb = p.get("amount_rmb", 0)
        weight = p.get("v5_weight") or p.get("v6_weight") or 0
        if not ticker or amount_rmb < 100:
            continue
        ccy = _ccy_from_ticker(ticker)
        portfolio.append((ticker, ticker, amount_rmb, ccy, weight, None))
    return portfolio, cash_pct


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
    global PORTFOLIO, CASH_RMB
    # 优先读真实持仓（holdings 表），无持仓时 fallback 到 plan_a_v5.json
    holdings_result = load_portfolio_from_holdings()
    if holdings_result[0]:
        PORTFOLIO, cash_pct, _ = holdings_result
        source_label = "DuckDB holdings · 用户真实持仓"
    else:
        PORTFOLIO, cash_pct = load_portfolio_from_plan()
        source_label = "plan_a_v5.json · v6 risk-aware 当前推荐"
    CASH_RMB = int(TOTAL_CAPITAL * cash_pct)

    print("=" * 70)
    print("  📊 方案 A v6 · 华尔街标准风险指标")
    print("=" * 70)
    print(f"\n📋 组合：{len(PORTFOLIO)} 只（来自 {source_label}）· 现金 ¥{CASH_RMB:,.0f} ({cash_pct*100:.0f}%)")
    if PORTFOLIO:
        tickers_preview = " / ".join(p[1] for p in PORTFOLIO[:6])
        if len(PORTFOLIO) > 6:
            tickers_preview += f" / ... ({len(PORTFOLIO) - 6} more)"
        print(f"   {tickers_preview}")

    # 1) 拉所有标的历史
    print("\n[1/3] 拉过去 ~1 年历史...")
    series = {}
    for name, ticker, rmb, ccy, pct, shares_override in PORTFOLIO:
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
        for name, ticker, rmb, ccy, _pct, shares_override in PORTFOLIO:
            if ticker not in first_prices or last_price[ticker] is None:
                all_have_price = False
                break
            fx = FX_TO_RMB.get(ccy, 1.0)
            shares = shares_override if shares_override is not None else rmb / (first_prices[ticker] * fx)
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

    # Beta（vs SPY）+ Tracking Error + Information Ratio（二审 P0.5-B）
    beta = None
    tracking_error = None
    info_ratio = None
    alpha_annual = None
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

            # 组合层 Tracking Error / Information Ratio (Grinold-Kahn 2000)
            #   TE = std(r_portfolio - r_benchmark) × √252
            #   IR = (μ_portfolio - μ_benchmark) × 252 / TE
            # 当前是绝对收益策略，这两个指标作"如果未来转指数增强"的预备数据
            excess_daily = r - sr
            tracking_error = float(excess_daily.std() * np.sqrt(TRADING_DAYS))
            mean_excess_annual = float(excess_daily.mean() * TRADING_DAYS)
            alpha_annual = mean_excess_annual
            info_ratio = (mean_excess_annual / tracking_error) if tracking_error > 0 else None

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
        "tracking_error_annual_pct": round(tracking_error * 100, 2) if tracking_error else None,
        "alpha_annual_pct": round(alpha_annual * 100, 2) if alpha_annual is not None else None,
        "information_ratio": round(info_ratio, 2) if info_ratio is not None else None,
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

    out_file = os.path.join(_REPO, "data", "latest", "risk_metrics.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 完整数据：{out_file}")


if __name__ == "__main__":
    main()
