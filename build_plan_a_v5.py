"""
方案 A v5 - 全客观化仓位
─────────────────────────────────────────
选股：v5 学术因子模型（Piotroski + 动量 + 反转 + 分析师）→ 取 Top N
仓位：Markowitz Max Sharpe（学术经典 1952 论文）+ 风险约束

约束（避免"仓位过度集中"和"看似合理实操不便"）：
  · 单只 max 15%（防止过度集中风险）
  · 单只 min 2%（小于此意义不大）
  · 现金 5%（保留流动性）
  · 协方差用过去 1 年日度收益估计

对比：
  · v5 客观方案 A vs 用户当前手编方案 A
  · 输出 build_plan_a_v5.json + 屏幕表格
"""
import sys
import os
import json
import numpy as np
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yfinance as yf

# 用户当前手编方案 A（用于对比）
CURRENT_PLAN_A = [
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
TOTAL_CAPITAL = 500000
MAX_WEIGHT = 0.15
MIN_WEIGHT = 0.02
TOP_N = 12
LOOKBACK_DAYS = 252


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


def markowitz_constrained(mean_returns, cov_matrix, risk_free=0.045/252,
                          n_iter=20000, max_w=MAX_WEIGHT, min_w=MIN_WEIGHT,
                          cash_pct=CASH_PCT):
    """带 max/min 仓位约束的 Markowitz Max Sharpe"""
    n = len(mean_returns)
    invest_pct = 1 - cash_pct
    best_sharpe = -np.inf
    best_w = None

    np.random.seed(42)
    for _ in range(n_iter):
        # Dirichlet 加约束的方式：先随机生成，再 clip + renormalize
        w = np.random.dirichlet(np.ones(n) * 1.5)
        w = np.clip(w, min_w / invest_pct, max_w / invest_pct)
        w = w / w.sum()  # 归一化
        # 再次检查 max
        if w.max() > max_w / invest_pct + 1e-6:
            continue
        port_return = np.dot(w, mean_returns)
        port_var = w @ cov_matrix @ w
        if port_var <= 0:
            continue
        sharpe = (port_return - risk_free) / np.sqrt(port_var)
        if sharpe > best_sharpe:
            best_sharpe = sharpe
            best_w = w

    return best_w * invest_pct if best_w is not None else None, best_sharpe


def main():
    # ============================================================
    # 1. 从 v5 daily_picks 缓存里拿 Top N 美股
    # ============================================================
    cache_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factor_scores_today.json")
    if not os.path.exists(cache_file):
        print(f"❌ 找不到 {cache_file}，先运行 daily_picks_v5.py")
        return

    cached = json.load(open(cache_file, encoding="utf-8"))
    factors = cached["factors"]

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from factor_model import combine_factors
    from early_signals import score_analyst

    sig_map = {s["ticker"]: s for s in cached["signals"]}
    analyst_scores = {tk: score_analyst(s.get("analyst"))[0] for tk, s in sig_map.items()}
    composite_df = combine_factors(factors, analyst_signals=analyst_scores, include_reversal=True)
    top = composite_df.head(TOP_N)
    tickers = top["ticker"].tolist()

    print("=" * 95)
    print(f"  📊 方案 A v5（学术因子选股 + Markowitz 客观仓位）")
    print("=" * 95)
    print(f"\n  选股：v5 4 因子模型 Top {TOP_N}")
    print(f"  仓位：Markowitz Max Sharpe（1952），约束 max={MAX_WEIGHT*100:.0f}% / min={MIN_WEIGHT*100:.0f}% / 现金={CASH_PCT*100:.0f}%")

    # ============================================================
    # 2. 拉历史收益率
    # ============================================================
    print(f"\n[1/3] 拉 {len(tickers)} 只标的过去 1 年日度收益率...")
    returns_dict = {}
    for tk in tickers:
        r = fetch_returns(tk, days=LOOKBACK_DAYS + 50)
        if r is not None and len(r) > 50:
            returns_dict[tk] = r[-LOOKBACK_DAYS:] if len(r) > LOOKBACK_DAYS else r
            print(f"  ✅ {tk:8} {len(r)} 天")
        else:
            print(f"  ❌ {tk:8} 失败")

    # 对齐
    min_len = min(len(r) for r in returns_dict.values())
    aligned = {tk: r[-min_len:] for tk, r in returns_dict.items()}
    final_tickers = list(aligned.keys())
    matrix = np.array([aligned[t] for t in final_tickers])

    mean_returns = matrix.mean(axis=1)
    cov_matrix = np.cov(matrix)

    # ============================================================
    # 3. Markowitz 优化（带约束）
    # ============================================================
    print(f"\n[2/3] Markowitz 蒙特卡洛优化（20000 次，带 {MAX_WEIGHT*100:.0f}%/{MIN_WEIGHT*100:.0f}% 约束）...")
    weights, sharpe = markowitz_constrained(mean_returns, cov_matrix)
    if weights is None:
        print("❌ 优化失败")
        return
    annual_sharpe = sharpe * np.sqrt(252)
    annual_return = mean_returns @ weights * 252
    annual_vol = np.sqrt(weights @ cov_matrix @ weights) * np.sqrt(252)
    print(f"  组合年化 Sharpe = {annual_sharpe:.2f}")
    print(f"  组合年化收益 = {annual_return*100:+.1f}%")
    print(f"  组合年化波动 = {annual_vol*100:.1f}%")

    # ============================================================
    # 4. 输出对比表
    # ============================================================
    cur_w = {ticker: w for _, ticker, w in CURRENT_PLAN_A}
    print(f"\n[3/3] 方案 A v5 vs 用户当前手编方案 A")
    print(f"\n  {'股票':<8}{'F':>3}{'综合z':>7}{'v5仓位':>9}{'金额':>12}{'当前仓位':>11}")
    print(f"  {'-'*60}")

    plan_v5 = []
    for i, tk in enumerate(final_tickers):
        info = top[top["ticker"] == tk].iloc[0]
        f_score = info.get("f_score")
        z = info.get("composite", 0)
        v5_w = float(weights[i])
        cur = cur_w.get(tk, 0)
        amount = TOTAL_CAPITAL * v5_w
        f_str = str(int(f_score)) if f_score is not None and not np.isnan(f_score) else "-"
        marker = "🆕" if cur == 0 and v5_w > 0.02 else ("⬆" if v5_w > cur + 0.02 else ("⬇" if v5_w < cur - 0.02 else "≈"))
        print(f"  {tk:<8}{f_str:>3}{z:>+7.2f}{v5_w*100:>+8.1f}%{amount:>11,.0f}{cur*100:>10.1f}% {marker}")
        plan_v5.append({
            "ticker": tk,
            "f_score": f_score if f_score is not None else None,
            "composite_z": float(z),
            "v5_weight": v5_w,
            "amount_rmb": round(amount, 2),
            "current_weight": cur,
        })

    print(f"  {'现金':<8}{' ':>10}{CASH_PCT*100:>8.1f}%{TOTAL_CAPITAL*CASH_PCT:>11,.0f}{CASH_PCT*100:>10.1f}%")

    # 用户方案中但 v5 没选的（暴露分歧）
    out_of_v5 = [(name, tk, w) for name, tk, w in CURRENT_PLAN_A if tk not in final_tickers]
    if out_of_v5:
        print(f"\n  ⚠️ 用户当前持有但 v5 未选（建议考虑减仓）：")
        for name, tk, w in out_of_v5:
            print(f"    • {name:<10}{tk:<10}当前 {w*100:.1f}% — v5 未进 Top {TOP_N}")

    # ============================================================
    # 5. 写文件
    # ============================================================
    out = {
        "generated_at": datetime.now().isoformat(),
        "method": "v5 factor selection + Markowitz Max Sharpe with constraints",
        "constraints": {
            "max_weight": MAX_WEIGHT,
            "min_weight": MIN_WEIGHT,
            "cash_pct": CASH_PCT,
            "top_n": TOP_N,
            "lookback_days": LOOKBACK_DAYS,
        },
        "portfolio_metrics": {
            "annual_sharpe": round(annual_sharpe, 2),
            "annual_return_pct": round(annual_return * 100, 2),
            "annual_vol_pct": round(annual_vol * 100, 2),
        },
        "plan_v5": plan_v5,
        "out_of_v5": [{"name": n, "ticker": t, "current_weight": w} for n, t, w in out_of_v5],
    }
    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plan_a_v5.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n✅ {out_file}")
    print(f"\n💡 客观化仓位说明:")
    print(f"  · 每只仓位都是 Markowitz 蒙特卡洛 20000 次找出的最大 Sharpe 解")
    print(f"  · 约束（max 15% / min 2% / 现金 5%）是实操常识，不是我编的")
    print(f"  · 协方差用过去 252 天日度数据，未来 ≠ 过去，仅供参考")


if __name__ == "__main__":
    main()
