"""
方案 A · 三种专业仓位优化方法对比
─────────────────────────────────────────
基于过去 1 年历史日收益率，给出三种专业方法的推荐仓位：

1. Kelly Criterion（凯利公式）
   - 基于胜率+赔率算最优仓位
   - 实战用 Half-Kelly（除以 2 减半）

2. Risk Parity（风险平价）
   - 让每只资产对组合贡献相等风险
   - 桥水的核心方法

3. Markowitz Mean-Variance（均值-方差）
   - 最大化 Sharpe 比率
   - 现代组合理论基础

输出：optimization_result.json
"""
import sys, os, json
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))  # 2026-05-11 lib 迁移
import numpy as np
from datetime import datetime, timedelta

import yfinance as yf

# 方案 A 当前仓位（v2 含北方稀土）
CURRENT_PLAN = [
    ("NVDA",       "NVDA",       0.12),
    ("TSM",        "TSM",        0.10),
    ("GOOGL",      "GOOGL",      0.10),
    ("MSFT",       "MSFT",       0.10),
    ("AMD",        "AMD",        0.08),
    ("Vertiv",     "VRT",        0.10),
    ("北方稀土",     "600111.SS",  0.08),
    ("Cameco",     "CCJ",        0.07),
    ("Datadog",    "DDOG",       0.05),
    ("中际旭创",     "300308.SZ",  0.05),
    ("阿里巴巴",     "9988.HK",    0.05),
    ("海光信息",     "688041.SS",  0.05),
]
CASH_PCT = 0.05
TOTAL = 500000

LOOKBACK_DAYS = 250


def fetch_returns(ticker, days=300):
    end = datetime.now()
    start = end - timedelta(days=days + 30)
    try:
        t = yf.Ticker(ticker)
        hist = t.history(start=start, end=end)
        if len(hist) < 100:
            return None
        return hist["Close"].pct_change().dropna().values
    except Exception:
        return None


def kelly_position(returns, risk_free_daily=0.045/252):
    """Kelly Criterion 仓位

    单资产 Kelly = (μ - rf) / σ²
    (针对正态分布假设)
    """
    excess = returns.mean() - risk_free_daily
    var = returns.var()
    if var <= 0:
        return 0
    kelly = excess / var
    return kelly


def risk_parity_weights(cov_matrix, max_iter=100):
    """Risk Parity: 让每只贡献相等风险

    解迭代：w_i 使得 (Σw)_i × w_i 相等
    简化版：用 inverse volatility 作为近似
    """
    vols = np.sqrt(np.diag(cov_matrix))
    inv_vol = 1 / vols
    weights = inv_vol / inv_vol.sum()
    return weights


def markowitz_weights(mean_returns, cov_matrix, risk_free=0.045/252, n_iter=10000):
    """Markowitz Mean-Variance Optimization

    最大化 Sharpe 比率（蒙特卡洛搜索）
    """
    n = len(mean_returns)
    best_sharpe = -np.inf
    best_w = None

    np.random.seed(42)
    for _ in range(n_iter):
        # Dirichlet 分布生成随机仓位（各仓位非负、和为 1）
        w = np.random.dirichlet(np.ones(n) * 2)
        port_return = np.dot(w, mean_returns)
        port_var = w @ cov_matrix @ w
        if port_var <= 0:
            continue
        sharpe = (port_return - risk_free) / np.sqrt(port_var)
        if sharpe > best_sharpe:
            best_sharpe = sharpe
            best_w = w
    return best_w, best_sharpe


def main():
    print("=" * 70)
    print("  📊 方案 A · 三种专业仓位优化")
    print("=" * 70)

    print("\n[1/3] 拉过去 ~1 年历史收益率...")
    returns_dict = {}
    for name, ticker, _w in CURRENT_PLAN:
        r = fetch_returns(ticker)
        if r is not None and len(r) > 50:
            returns_dict[ticker] = r[:LOOKBACK_DAYS] if len(r) > LOOKBACK_DAYS else r
            print(f"  ✅ {name:<14}{ticker:<14}{len(r)} 天")
        else:
            print(f"  ❌ {name:<14}{ticker:<14}失败")

    # 对齐长度（取最短）
    min_len = min(len(r) for r in returns_dict.values())
    aligned = {ticker: r[-min_len:] for ticker, r in returns_dict.items()}
    tickers = list(aligned.keys())
    matrix = np.array([aligned[t] for t in tickers])  # n_assets x n_days
    print(f"\n  对齐后：{len(tickers)} 只股票 × {min_len} 个交易日")

    mean_returns = matrix.mean(axis=1)  # 日均收益
    cov_matrix = np.cov(matrix)  # 协方差矩阵
    vols = np.sqrt(np.diag(cov_matrix))  # 日波动率

    name_map = {ticker: name for name, ticker, _ in CURRENT_PLAN}
    current_w = {ticker: w for _, ticker, w in CURRENT_PLAN}

    # ============================================================
    # 方法 1: Kelly
    # ============================================================
    print(f"\n[2/3] 方法 1: Kelly Criterion（实战用 Half-Kelly）")
    print(f"  {'股票':<14}{'日均%':>8}{'日波动%':>10}{'Full Kelly':>12}{'Half Kelly':>12}{'当前仓位':>12}")
    print(f"  {'-'*70}")

    kelly_full = {}
    for i, ticker in enumerate(tickers):
        kf = kelly_position(matrix[i])
        kelly_full[ticker] = kf
        kh = kf / 2
        cur = current_w.get(ticker, 0)
        flag = "↑加仓" if kh > cur + 0.02 else ("↓减仓" if kh < cur - 0.02 else "≈持平")
        print(f"  {name_map[ticker]:<14}{mean_returns[i]*100:>+7.3f}%{vols[i]*100:>+9.3f}%{kf*100:>+11.1f}%{kh*100:>+11.1f}%{cur*100:>11.1f}%  {flag}")

    # 归一化 Half Kelly
    kh_total = sum(max(0, kelly_full[t] / 2) for t in tickers)
    if kh_total > 0:
        kelly_normalized = {t: max(0, kelly_full[t] / 2) / kh_total * 0.95 for t in tickers}  # 留 5% 现金
    else:
        kelly_normalized = current_w.copy()

    # ============================================================
    # 方法 2: Risk Parity
    # ============================================================
    print(f"\n[3/3] 方法 2: Risk Parity（让每只贡献相等风险）")
    rp_w = risk_parity_weights(cov_matrix)
    rp_w = rp_w * 0.95  # 留 5% 现金

    # ============================================================
    # 方法 3: Markowitz Max Sharpe
    # ============================================================
    print(f"\n         方法 3: Markowitz Max Sharpe（10000 次蒙特卡洛搜索）")
    mv_w, mv_sharpe = markowitz_weights(mean_returns, cov_matrix)
    if mv_w is not None:
        mv_w = mv_w * 0.95
    else:
        mv_w = np.array([1/len(tickers)] * len(tickers)) * 0.95
    print(f"  最大化 Sharpe = {mv_sharpe * np.sqrt(252):.2f}（年化）")

    # ============================================================
    # 三种方法 vs 当前仓位
    # ============================================================
    print(f"\n📊 三种方法 vs 当前仓位（仓位单位：% / 50 万 RMB）")
    print(f"  {'股票':<14}{'当前':>10}{'Kelly':>10}{'RiskParity':>12}{'Markowitz':>12}{'差异-Mark':>12}")
    print(f"  {'-'*72}")
    for i, ticker in enumerate(tickers):
        cur = current_w.get(ticker, 0) * 100
        k = kelly_normalized.get(ticker, 0) * 100
        rp = rp_w[i] * 100
        mv = mv_w[i] * 100
        diff = mv - cur
        print(f"  {name_map[ticker]:<14}{cur:>9.1f}%{k:>9.1f}%{rp:>11.1f}%{mv:>11.1f}%{diff:>+11.1f}%")
    print(f"  {'现金':<14}{CASH_PCT*100:>9.1f}%{5:>9.1f}%{5:>11.1f}%{5:>11.1f}%")

    # ============================================================
    # 关键洞察
    # ============================================================
    print(f"\n💡 关键洞察")

    # 比较三种方法的特点
    print(f"\n  方法对比：")
    cur_arr = np.array([current_w.get(t, 0) for t in tickers])
    cur_return = (cur_arr * mean_returns).sum() * 252
    cur_vol = np.sqrt(cur_arr @ cov_matrix @ cur_arr) * np.sqrt(252)
    cur_sharpe = (cur_return - 0.045) / cur_vol if cur_vol > 0 else 0

    for method_name, w_arr in [
        ("当前方案 A", cur_arr),
        ("Kelly Half", np.array([kelly_normalized.get(t, 0) for t in tickers])),
        ("Risk Parity", rp_w),
        ("Markowitz", mv_w),
    ]:
        port_return = (w_arr * mean_returns).sum() * 252
        port_vol = np.sqrt(w_arr @ cov_matrix @ w_arr) * np.sqrt(252)
        port_sharpe = (port_return - 0.045) / port_vol if port_vol > 0 else 0
        print(f"  {method_name:<14}年化收益 {port_return*100:>+6.1f}%  年化波动 {port_vol*100:>5.1f}%  Sharpe {port_sharpe:>5.2f}")

    print(f"\n💡 结论：")
    print(f"  • Kelly 给的仓位通常很激进（很多股 > 30%），需 Half-Kelly 减半")
    print(f"  • Risk Parity 偏好低波动股，会减仓 AMD/Datadog 这种高波动")
    print(f"  • Markowitz Max Sharpe 找历史最优，但 future ≠ past")
    print(f"  • 现实建议：当前方案 A + Kelly/RP 调整 ±5% 即可，不要全盘照搬")

    # 输出 JSON
    out = {
        "generated_at": datetime.now().isoformat(),
        "lookback_days": min_len,
        "current_plan": [
            {"name": name_map[t], "ticker": t, "current_pct": current_w.get(t, 0),
             "kelly_full_pct": kelly_full[t], "kelly_half_norm_pct": kelly_normalized.get(t, 0),
             "risk_parity_pct": float(rp_w[i]), "markowitz_pct": float(mv_w[i]),
             "daily_mean_pct": float(mean_returns[i] * 100),
             "daily_vol_pct": float(vols[i] * 100)}
            for i, t in enumerate(tickers)
        ],
        "method_comparison": {
            "current": {
                "annual_return": round(cur_return * 100, 2),
                "annual_vol": round(cur_vol * 100, 2),
                "sharpe": round(cur_sharpe, 2),
            },
        },
    }

    out_file = os.path.join(_REPO, "optimization_result.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n✅ 完整数据：{out_file}")


if __name__ == "__main__":
    main()
