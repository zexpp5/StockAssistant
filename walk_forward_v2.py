"""
Walk-Forward v2: 加 SPY 200MA 趋势过滤器
─────────────────────────────────────────
对比 v1 walk-forward（无过滤）vs v2（带 Faber 200MA 过滤）
"""
import sys
import os
import json
import pandas as pd
import numpy as np
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trend_filter import get_market_regime
from walk_forward_validate import (
    SAMPLES, REGIMES, calc_factors_at, forward_return,
)


def main():
    print("=" * 110)
    print("  🚀 Walk-Forward v2: 因子选股 + Faber 200MA 趋势过滤")
    print("=" * 110)

    results_no_filter = {}
    results_with_filter = {}

    for start, end, label in REGIMES:
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
            continue

        df = pd.DataFrame(factor_data)
        for col in ["momentum", "reversal"]:
            mean = df[col].mean()
            std = df[col].std(ddof=0)
            df[f"z_{col}"] = (df[col] - mean) / std if std > 0 else 0
        df["composite"] = (df["z_momentum"] + df["z_reversal"]) / 2
        df = df.sort_values("composite", ascending=False).reset_index(drop=True)

        n_pick = max(1, len(df) // 3)
        picks = df.head(n_pick)
        portfolio_return = picks["forward"].mean()

        spy_ret = forward_return("SPY", start, end)
        bond_ret = forward_return("TLT", start, end) or 0

        no_filter_alpha = portfolio_return - spy_ret
        results_no_filter[label] = {"alpha": no_filter_alpha, "portfolio": portfolio_return, "spy": spy_ret}

        regime_info = get_market_regime(as_of=start)
        regime = regime_info.get("regime", "UNKNOWN")
        mult = regime_info.get("position_multiplier", 1.0)

        if regime == "RISK-OFF":
            filtered_return = portfolio_return * mult + bond_ret * (1 - mult)
        else:
            filtered_return = portfolio_return

        filter_alpha = filtered_return - spy_ret
        results_with_filter[label] = {
            "alpha": filter_alpha,
            "portfolio": filtered_return,
            "spy": spy_ret,
            "regime": regime,
            "position_mult": mult,
            "tlt_return": bond_ret,
        }

        print(f"\n  📅 {label} ({start})")
        print(f"     market regime: {regime} (距 200MA {regime_info.get('distance_pct', 0):+.1f}%)")
        print(f"     无过滤: 组合 {portfolio_return:+.1f}%  SPY {spy_ret:+.1f}%  alpha {no_filter_alpha:+.1f}%")
        if regime == "RISK-OFF":
            print(f"     过滤后(50% 因子 + 50% TLT={bond_ret:+.1f}%): {filtered_return:+.1f}%  alpha {filter_alpha:+.1f}%")
        else:
            print(f"     RISK-ON 不触发，结果不变")

    print(f"\n\n{'='*110}")
    print(f"  📊 v1 (无过滤) vs v2 (带 200MA 过滤) 对比")
    print(f"{'='*110}")
    print(f"\n  {'Regime':<35}{'v1 Alpha':>11}{'v2 Alpha':>11}{'差异':>10}{'结论'}")
    print(f"  {'-'*85}")

    v1_alphas, v2_alphas = [], []
    for label in results_no_filter:
        a1 = results_no_filter[label]["alpha"]
        a2 = results_with_filter[label]["alpha"]
        v1_alphas.append(a1)
        v2_alphas.append(a2)
        diff = a2 - a1
        verdict = "✅ 改善" if diff > 0.5 else ("❌ 变差" if diff < -0.5 else "≈ 不变")
        print(f"  {label:<35}{a1:>+10.1f}%{a2:>+10.1f}%{diff:>+9.1f}%  {verdict}")

    v1_mean = np.mean(v1_alphas)
    v2_mean = np.mean(v2_alphas)
    v1_min = min(v1_alphas)
    v2_min = min(v2_alphas)

    print(f"\n  📌 平均 alpha:    v1 {v1_mean:+.1f}%   v2 {v2_mean:+.1f}%")
    print(f"  📌 最差 regime:   v1 {v1_min:+.1f}%   v2 {v2_min:+.1f}%")

    out = {
        "generated_at": datetime.now().isoformat(),
        "v1_no_filter": results_no_filter,
        "v2_with_filter": results_with_filter,
        "summary": {
            "v1_mean_alpha": round(v1_mean, 2),
            "v2_mean_alpha": round(v2_mean, 2),
            "v1_worst": round(v1_min, 2),
            "v2_worst": round(v2_min, 2),
        },
    }
    with open("walk_forward_v2_results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n✅ walk_forward_v2_results.json")


if __name__ == "__main__":
    main()
