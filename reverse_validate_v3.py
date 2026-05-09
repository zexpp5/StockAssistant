"""
反向验证 v3：学术因子模型
─────────────────────────────────────────
v1: 我编的 4 维打分（AI/估值/趋势/可信）
v2: v1 + 分析师信号动态门槛
v3: Piotroski F-Score + 12-1 动量 + 分析师上修（横截面 z-score 等权）

每个因子都来自公开学术论文，权重客观：
  - Piotroski (Stanford 2000)
  - Jegadeesh-Titman (JF 1993)
  - Stickel/Womack (JF 1991/1996)

合成方法：横截面 z-score 标准化 + 等权（不预设权重）
推荐规则：composite z-score ≥ 0.5（约前 1/3）
"""
import sys
import os
import json
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reverse_validate import fetch_data_at, VALIDATION_DATE
from factor_model import combine_factors
from early_signals import score_analyst


# v3 只用 yfinance 财报齐全的美股（A 股/港股/韩股 yfinance 没财报）
SAMPLES_V3 = [
    ("AMD",      "AMD"),
    ("Intel",    "INTC"),
    ("Datadog",  "DDOG"),
    ("Vertiv",   "VRT"),
    ("Lam Research", "LRCX"),
    ("Marvell",  "MRVL"),
    ("Broadcom", "AVGO"),
    ("Apple",    "AAPL"),
    ("Microsoft", "MSFT"),
    ("Salesforce", "CRM"),
    ("Snowflake", "SNOW"),
    ("Tesla",    "TSLA"),
]


def main():
    # 1. 加载因子数据
    factor_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factor_scores.json")
    sig_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "early_signals.json")
    if not os.path.exists(factor_file):
        print(f"❌ {factor_file} 不存在，先运行 factor_model.py --as-of 2025-12-31")
        return
    if not os.path.exists(sig_file):
        print(f"❌ {sig_file} 不存在，先运行 early_signals.py --as-of 2025-12-31")
        return

    factor_data = json.load(open(factor_file, encoding="utf-8"))
    sig_data = json.load(open(sig_file, encoding="utf-8"))
    sig_map = {r["ticker"]: r for r in sig_data["results"]}

    # 2. 拉每只股票的实际未来涨幅
    print("=" * 95)
    print(f"  🔍 反向验证 v3：Piotroski + 12-1 动量 + 分析师上修")
    print(f"  as_of = {VALIDATION_DATE} · 因子等权 z-score 合成")
    print("=" * 95)

    print(f"\n[1/3] 拉每只股票之后真实涨跌（{VALIDATION_DATE} → 2026-05-08）...")
    future_pcts = {}
    for name, ticker in SAMPLES_V3:
        d = fetch_data_at(ticker, VALIDATION_DATE)
        if d:
            future_pcts[ticker] = d["future_pct"]
            print(f"  ✅ {name:<14}{ticker:<8}{d['future_pct']:+.1f}%")
        else:
            print(f"  ❌ {name}")

    # 3. 计算分析师信号分（0-15 → 直接用作因子）
    analyst_scores = {}
    for tk, sig in sig_map.items():
        s, _ = score_analyst(sig.get("analyst"))
        analyst_scores[tk] = s

    # 4. 合成因子打分
    composite_df = combine_factors(factor_data["results"], analyst_signals=analyst_scores)
    composite_df["future_pct"] = composite_df["ticker"].map(future_pcts)
    composite_df = composite_df.dropna(subset=["future_pct"])

    # 5. v3 推荐规则：composite z ≥ 0.5（约前 1/3）
    THRESHOLD = 0.5
    composite_df["v3_recommended"] = composite_df["composite"] >= THRESHOLD
    composite_df["winner"] = composite_df["future_pct"] >= 20

    print(f"\n[2/3] 因子合成（z-score 等权）：")
    print(f"\n  {'排名':<5}{'股票':<10}{'F分':>5}{'z-F':>7}{'动量%':>9}{'z-Mom':>7}{'分析师':>7}{'z-Ana':>7}{'综合z':>9}{'推荐':<6}{'实际':>9}{'结果'}")
    print(f"  {'-'*100}")

    correct = wrong = missed = avoided = 0

    for _, r in composite_df.iterrows():
        rec = bool(r["v3_recommended"])
        win = bool(r["winner"])
        if rec and win:
            judge = "✅ 命中"; correct += 1
        elif rec and not win:
            judge = "❌ 误报"; wrong += 1
        elif (not rec) and win:
            judge = "🔴 漏报"; missed += 1
        else:
            judge = "🟢 正确避开"; avoided += 1

        f_str = f"{int(r['f_score'])}" if pd.notna(r['f_score']) else "-"
        m_str = f"{r['momentum']:+.1f}%" if pd.notna(r['momentum']) else "N/A"
        print(f"  {int(r['rank']):<5}{r['ticker']:<10}"
              f"{f_str:>5}"
              f"{r['z_f']:>+7.2f}"
              f"{m_str:>9}"
              f"{r['z_mom']:>+7.2f}"
              f"{int(r['analyst']):>7}"
              f"{r['z_ana']:>+7.2f}"
              f"{r['composite']:>+8.2f}"
              f"  {'是' if rec else '否':<6}"
              f"{r['future_pct']:>+8.1f}%"
              f"  {judge}")

    # 6. 三版本对比
    print("\n" + "=" * 95)
    print("  📊 v1 vs v2 vs v3 召回 / 准确率对比")
    print("=" * 95)
    n = len(composite_df)
    recall = correct / (correct + missed) * 100 if (correct + missed) > 0 else 0
    precision = correct / (correct + wrong) * 100 if (correct + wrong) > 0 else 0

    print(f"  样本: {n} 只（仅美股，因 A 股/港股 yfinance 财报缺失）\n")
    print(f"  v1 我编的 4 维打分:        召回率 40%   准确率 80%   （Intel/MRVL/LRCX/DDOG 漏报）")
    print(f"  v2 v1 + 分析师动态门槛:    召回率 70%   准确率 64%   （救回 3 只但 SNOW/AAPL/TSLA 误报）")
    print(f"  v3 学术因子模型（本次）:   召回率 {recall:.0f}%   准确率 {precision:.0f}%")
    print(f"     命中 {correct}  误报 {wrong}  漏报 {missed}  正确避开 {avoided}")

    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reverse_validation_v3.json")
    composite_df.to_json(out_file, orient="records", force_ascii=False, indent=2)
    print(f"\n✅ {out_file}")


if __name__ == "__main__":
    main()
