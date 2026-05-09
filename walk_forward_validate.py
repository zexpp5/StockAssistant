"""
Walk-Forward 验证（解决问题 #3）
─────────────────────────────────────────
目标：证明 v6 模型 Sharpe 3.0 不是"只在 2025 牛市 over-fit"

方法（Pardo 1992《Design, Testing & Optimization of Trading Systems》）：
  1. 选 6 个不同 regime 的时点
  2. 每个时点用截止当时的因子选 Top N
  3. 持有 90 天，看组合表现 vs SPY

测试 Regime（涵盖牛/熊/震荡）：
  - 2018-10-01 → 2018-12-31  贸易战 + 加息 双杀（熊）
  - 2020-04-01 → 2020-06-30  疫情后反弹
  - 2022-01-01 → 2022-03-31  加息熊市
  - 2023-04-01 → 2023-06-30  AI 主题崛起
  - 2024-07-01 → 2024-09-30  震荡
  - 2025-12-31 → 2026-05-08  当前（已知结果）

只用价格因子（12-1 动量 + 1 月反转），因为 yfinance 历史财报有限制。
分析师/Piotroski 在 walk-forward 中数据可获取性差。

输出: walk_forward_results.json + 屏幕表格
"""
import sys
import os
import json
import pandas as pd
import numpy as np
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yfinance as yf

# 测试样本（要求所有股票在最早测试日已上市）
SAMPLES = [
    "AMD", "INTC", "DDOG", "VRT", "LRCX", "MRVL",
    "AVGO", "AAPL", "MSFT", "CRM", "SNOW", "TSLA",
    "NVDA", "GOOGL", "TSM", "META",  # 加更大样本
]

REGIMES = [
    ("2018-10-01", "2018-12-31", "贸易战 + 加息双杀（熊）"),
    ("2020-04-01", "2020-06-30", "疫情后反弹（强牛）"),
    ("2022-01-01", "2022-03-31", "加息熊市"),
    ("2023-04-01", "2023-06-30", "AI 主题崛起"),
    ("2024-07-01", "2024-09-30", "震荡（中性）"),
    ("2025-12-31", "2026-05-08", "当前（已知结果）"),
]


def fetch_price(ticker, start, end):
    try:
        return yf.Ticker(ticker).history(start=start, end=end)
    except Exception:
        return None


def calc_factors_at(ticker, as_of):
    """计算到 as_of 为止的 12-1 月动量 + 1 月反转"""
    target = pd.to_datetime(as_of)
    start = target - pd.Timedelta(days=400)
    end = target + pd.Timedelta(days=2)
    hist = fetch_price(ticker, start, end)
    if hist is None or len(hist) < 252:
        return None, None
    if hist.index.tz:
        hist = hist[hist.index.tz_localize(None) <= target]
    else:
        hist = hist[hist.index <= target]
    if len(hist) < 252:
        return None, None
    close = hist["Close"]
    t_now = float(close.iloc[-1])
    t_minus_21 = float(close.iloc[-22])
    t_minus_252 = float(close.iloc[-253])
    mom = (t_minus_21 / t_minus_252 - 1) * 100
    rev = -((t_now / t_minus_21 - 1) * 100)
    return mom, rev


def forward_return(ticker, start, end):
    """从 start 到 end 的实际收益率"""
    hist = fetch_price(ticker, start, end)
    if hist is None or len(hist) < 5:
        return None
    return (hist["Close"].iloc[-1] / hist["Close"].iloc[0] - 1) * 100


def main():
    print("=" * 110)
    print(f"  🚀 Walk-Forward Validation: 6 个 Regime × 学术因子（动量 + 反转）")
    print("=" * 110)

    spy_returns = {}
    for start, end, label in REGIMES:
        spy_ret = forward_return("SPY", start, end)
        spy_returns[(start, end)] = spy_ret
        print(f"  {start} → {end}  SPY {spy_ret:+.2f}%  [{label}]")

    print(f"\n[1/2] 在每个 regime 起点，按因子选 Top 1/3，持有到 regime 结束：\n")

    results_by_regime = {}
    for start, end, label in REGIMES:
        print(f"\n{'='*100}")
        print(f"  📅 {label}: {start} → {end}")
        print(f"{'='*100}")

        # 在 start 点计算因子
        factor_data = []
        for tk in SAMPLES:
            mom, rev = calc_factors_at(tk, start)
            if mom is None:
                continue
            fwd = forward_return(tk, start, end)
            if fwd is None:
                continue
            factor_data.append({"ticker": tk, "momentum": mom, "reversal": rev, "forward": fwd})

        if not factor_data:
            print("  ❌ 无可用样本")
            continue

        df = pd.DataFrame(factor_data)
        # z-score 等权
        for col in ["momentum", "reversal"]:
            mean = df[col].mean()
            std = df[col].std(ddof=0)
            df[f"z_{col}"] = (df[col] - mean) / std if std > 0 else 0
        df["composite"] = (df["z_momentum"] + df["z_reversal"]) / 2
        df = df.sort_values("composite", ascending=False).reset_index(drop=True)

        # Top 1/3 推荐
        n_pick = max(1, len(df) // 3)
        picks = df.head(n_pick)
        portfolio_return = picks["forward"].mean()
        spy_ret = spy_returns[(start, end)]
        alpha = portfolio_return - spy_ret

        print(f"\n  {'股票':<8}{'动量':>9}{'反转':>9}{'综合z':>8}{'前向收益':>12}")
        print(f"  {'-'*55}")
        for _, r in df.iterrows():
            mark = "✅" if r["ticker"] in picks["ticker"].values else "  "
            print(f"  {mark}{r['ticker']:<6}{r['momentum']:>+8.1f}%{r['reversal']:>+8.1f}%"
                  f"{r['composite']:>+7.2f}{r['forward']:>+11.1f}%")

        print(f"\n  推荐 Top {n_pick} 平均收益: {portfolio_return:+.2f}%")
        print(f"  SPY 同期收益:             {spy_ret:+.2f}%")
        print(f"  Alpha (组合 - SPY):       {alpha:+.2f}%   {'✅ 跑赢' if alpha > 0 else '❌ 跑输'}")

        results_by_regime[label] = {
            "start": start,
            "end": end,
            "portfolio_return": round(portfolio_return, 2),
            "spy_return": round(spy_ret, 2),
            "alpha": round(alpha, 2),
            "picks": picks["ticker"].tolist(),
            "n_universe": len(df),
        }

    # 汇总
    print(f"\n\n{'='*110}")
    print(f"  📊 跨 Regime 汇总（{len(results_by_regime)} 个时段）")
    print(f"{'='*110}")
    print(f"\n  {'Regime':<35}{'组合收益':>10}{'SPY':>10}{'Alpha':>10}{'结论'}")
    print(f"  {'-'*80}")

    alphas = []
    wins = 0
    for label, r in results_by_regime.items():
        a = r["alpha"]
        alphas.append(a)
        if a > 0:
            wins += 1
        verdict = "✅ 跑赢" if a > 0 else "❌ 跑输"
        print(f"  {label:<35}{r['portfolio_return']:>+9.1f}%{r['spy_return']:>+9.1f}%{a:>+9.1f}%   {verdict}")

    avg_alpha = np.mean(alphas)
    print(f"\n  📌 平均 Alpha: {avg_alpha:+.2f}%/regime   ({wins}/{len(results_by_regime)} 跑赢)")

    if avg_alpha > 0 and wins >= len(results_by_regime) * 0.6:
        print(f"  ✅ 通过 walk-forward：模型在多个 regime 都能跑赢，不是只在牛市 work")
    else:
        print(f"  ⚠️ 警示：模型在某些 regime 跑输 SPY，单一 backtest Sharpe 不可信")

    out = {
        "generated_at": datetime.now().isoformat(),
        "regimes": results_by_regime,
        "avg_alpha": round(avg_alpha, 2),
        "wins": wins,
        "total": len(results_by_regime),
    }
    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "walk_forward_results.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n✅ {out_file}")


if __name__ == "__main__":
    main()
