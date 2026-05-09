"""
反向验证 v4：完整客观化版本
─────────────────────────────────────────
相对 v3 的改进：
  ✅ AI 关联度：用 gics_classifier（industry + override 带 URL）替换主观分级
  ✅ Winner 定义：从「涨 ≥20%」改为「alpha vs SPY ≥ 5%」（学术标准）
  ✅ 推荐规则：从「z ≥ 0.5」改为「top tertile」（学术分位法）
  ✅ 移除 PE_AT_2025_12 硬编码字典（v3 已不用 PE）
"""
import sys
import os
import json
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reverse_validate import fetch_data_at, VALIDATION_DATE
from factor_model import combine_factors
from early_signals import score_analyst
import yfinance as yf


SAMPLES_V4 = [
    ("AMD", "AMD"), ("Intel", "INTC"), ("Datadog", "DDOG"),
    ("Vertiv", "VRT"), ("Lam Research", "LRCX"), ("Marvell", "MRVL"),
    ("Broadcom", "AVGO"), ("Apple", "AAPL"), ("Microsoft", "MSFT"),
    ("Salesforce", "CRM"), ("Snowflake", "SNOW"), ("Tesla", "TSLA"),
]

ALPHA_WINNER_THRESHOLD = 5.0


def fetch_spy_return(start_date, end_date):
    spy = yf.Ticker("SPY").history(start=start_date, end=end_date)
    return (spy["Close"].iloc[-1] / spy["Close"].iloc[0] - 1) * 100


def main():
    factor_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factor_scores.json")
    sig_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "early_signals.json")
    factor_data = json.load(open(factor_file, encoding="utf-8"))
    sig_data = json.load(open(sig_file, encoding="utf-8"))
    sig_map = {r["ticker"]: r for r in sig_data["results"]}

    print("=" * 110)
    print(f"  🔍 反向验证 v4：完整客观化（GICS + α-winner + tertile）")
    print(f"  as_of = {VALIDATION_DATE} → 2026-05-08")
    print("=" * 110)

    spy_return = fetch_spy_return("2025-12-29", "2026-05-10")
    print(f"\n  📊 SPY 同期收益: {spy_return:+.2f}%")
    print(f"  Winner 定义: 个股收益 - SPY 收益 ≥ {ALPHA_WINNER_THRESHOLD}%（α-winner 标准）")

    print(f"\n[1/3] 拉每只股票实际收益 + alpha...")
    future_pcts = {}
    alphas = {}
    for name, ticker in SAMPLES_V4:
        d = fetch_data_at(ticker, VALIDATION_DATE)
        if d:
            future_pcts[ticker] = d["future_pct"]
            alphas[ticker] = d["future_pct"] - spy_return
            tag = "✅ WIN" if alphas[ticker] >= ALPHA_WINNER_THRESHOLD else "  --"
            print(f"  {tag}  {name:<14}{ticker:<8}收益 {d['future_pct']:+7.1f}%  α {alphas[ticker]:+7.1f}%")

    analyst_scores = {}
    for tk, sig in sig_map.items():
        s, _ = score_analyst(sig.get("analyst"))
        analyst_scores[tk] = s

    composite_df = combine_factors(factor_data["results"], analyst_signals=analyst_scores)
    composite_df["future_pct"] = composite_df["ticker"].map(future_pcts)
    composite_df["alpha"] = composite_df["ticker"].map(alphas)
    composite_df = composite_df.dropna(subset=["future_pct"])

    tertile_cutoff = composite_df["composite"].quantile(2/3)
    composite_df["v4_recommended"] = composite_df["composite"] >= tertile_cutoff
    composite_df["winner"] = composite_df["alpha"] >= ALPHA_WINNER_THRESHOLD

    print(f"\n[2/3] 因子合成 + 决策（top tertile，cutoff z = {tertile_cutoff:.2f}）：")
    print(f"\n  {'排名':<5}{'股票':<10}{'F':>4}{'动量%':>8}{'分析师':>7}{'综合z':>8}  {'推荐':<5}{'α%':>8}  {'结果'}")
    print(f"  {'-'*85}")

    correct = wrong = missed = avoided = 0
    for _, r in composite_df.iterrows():
        rec = bool(r["v4_recommended"])
        win = bool(r["winner"])
        if rec and win:
            judge = "✅ 命中"; correct += 1
        elif rec and not win:
            judge = "❌ 误报"; wrong += 1
        elif (not rec) and win:
            judge = "🔴 漏报"; missed += 1
        else:
            judge = "🟢 正确避开"; avoided += 1

        f_str = str(int(r['f_score'])) if pd.notna(r['f_score']) else "-"
        m_str = f"{r['momentum']:+.1f}" if pd.notna(r['momentum']) else "N/A"
        print(f"  {int(r['rank']):<5}{r['ticker']:<10}{f_str:>4}{m_str:>8}{int(r['analyst']):>7}"
              f"{r['composite']:>+7.2f}    {'是' if rec else '否':<5}{r['alpha']:>+7.1f}%   {judge}")

    print("\n" + "=" * 110)
    print("  📊 全版本对比（同一 12 只样本 / 2025-12-31 → 2026-05-08）")
    print("=" * 110)
    recall = correct / (correct + missed) * 100 if (correct + missed) > 0 else 0
    precision = correct / (correct + wrong) * 100 if (correct + wrong) > 0 else 0
    print(f"\n  v1 我编的 4 维:                召回 40%   准确 80%   （winner = +20%）")
    print(f"  v2 v1 + 分析师动态门槛:        召回 70%   准确 64%   （winner = +20%）")
    print(f"  v3 学术因子 z≥0.5:             召回 43%   准确 100%  （winner = +20%）")
    print(f"  v4 学术因子 + tertile + α:      召回 {recall:.0f}%   准确 {precision:.0f}%  （winner = α ≥ 5% vs SPY）")
    print(f"\n     v4 命中 {correct}  误报 {wrong}  漏报 {missed}  正确避开 {avoided}")

    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reverse_validation_v4.json")
    composite_df.to_json(out_file, orient="records", force_ascii=False, indent=2)
    print(f"\n✅ {out_file}")


if __name__ == "__main__":
    main()
