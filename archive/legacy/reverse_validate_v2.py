"""
反向验证 v2：原 4 维打分 + 早期预警信号
─────────────────────────────────────────
对比 v1（仅 AI/估值/趋势/可信）和 v2（+ 内部人 + 分析师），
看能不能把 INTC/MRVL/DDOG/LRCX 这些漏报抓回来。

⚠️ 数据真实性声明：
  - 分析师事件：按 as_of=2025-12-31 严格历史切片（真实可用）
  - 内部人 6m 净买入：yfinance 只返回当前快照（含 2026），有未来泄漏
    → v2 主信号用分析师；内部人仅作参考列展示
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 复用 v1 的逻辑
from reverse_validate import (
    SAMPLES, VALIDATION_DATE, fetch_data_at,
    score_ai_relevance, score_valuation, score_trend, score_credibility,
    PE_AT_2025_12,
)
from early_signals import score_insider, score_analyst


def v2_recommend(v1_total, analyst_score, analyst_data):
    """v2 决策逻辑（不是简单加分）：
       分析师强信号（≥ 10 分）→ 把 v1 门槛从 75 降到 60
       分析师中信号（5-9 分） → 把 v1 门槛降到 70
       分析师无信号或负向    → 维持原 75 门槛
       分析师明确看空（下调多于上调）→ 提高门槛到 85
    """
    threshold = 75
    if analyst_data and "error" not in (analyst_data or {}):
        raises = analyst_data.get("raises", 0)
        lowers = analyst_data.get("lowers", 0)
        if raises < lowers and lowers >= 3:
            threshold = 85  # 看空共识：抬高门槛
        elif analyst_score >= 10:
            threshold = 60  # 强看多：放宽门槛
        elif analyst_score >= 5:
            threshold = 70
    return v1_total >= threshold, threshold


def main():
    # 1. 拉早期信号（用 reverse 已生成的 early_signals.json）
    sig_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "early_signals.json")
    if not os.path.exists(sig_file):
        print(f"❌ 找不到 {sig_file}")
        print(f"   先运行：python3 early_signals.py --as-of 2025-12-31 --lookback 90")
        return
    sig_data = json.load(open(sig_file, encoding="utf-8"))
    sig_map = {r["ticker"]: r for r in sig_data["results"]}

    print("=" * 95)
    print(f"  🔍 反向验证 v2：原 4 维 + 早期信号 vs v1 仅 4 维")
    print(f"  as_of = {VALIDATION_DATE} · 信号源 = early_signals.json")
    print("=" * 95)

    print(f"\n[1/2] 拉每只股票到 {VALIDATION_DATE} 的历史 + 之后真实涨跌...")
    results = []
    for name, ticker, ai_rel in SAMPLES:
        d = fetch_data_at(ticker, VALIDATION_DATE)
        if not d:
            print(f"  ❌ {name}")
            continue
        results.append({"name": name, "ticker": ticker, "ai_rel": ai_rel, **d})
        print(f"  ✅ {name:<14}之后涨跌={d['future_pct']:+.1f}%")

    # 2. 双版本打分对比
    print(f"\n[2/2] v1 vs v2 决策对比（分析师信号 → 动态门槛）")
    print(f"\n{'股票':<14}{'v1分':>5}{'分析师':>7}{'v2门':>5}{'v1判':<12}{'v2判':<12}{'实际':>9}")
    print("-" * 90)

    v1_correct = v1_wrong = v1_missed = v1_avoided = 0
    v2_correct = v2_wrong = v2_missed = v2_avoided = 0
    rescued = []
    new_misses = []

    for r in results:
        s_ai = score_ai_relevance(r["ai_rel"])
        s_val = score_valuation(PE_AT_2025_12.get(r["ticker"]))
        s_trend = score_trend(r["one_year_pct_at_time"], r["one_week_pct_at_time"])
        s_cred = score_credibility()
        v1_total = s_ai + s_val + s_trend + s_cred
        v1_recommended = v1_total >= 75

        # 早期信号
        sig = sig_map.get(r["ticker"], {})
        ana_score, _ = score_analyst(sig.get("analyst"))
        v2_recommended, v2_threshold = v2_recommend(v1_total, ana_score, sig.get("analyst"))

        future = r["future_pct"]
        winner = future >= 20

        v1_judge = ("✅ 命中" if winner else "❌ 误报") if v1_recommended else ("🔴 漏报" if winner else "🟢 正确避开")
        v2_judge = ("✅ 命中" if winner else "❌ 误报") if v2_recommended else ("🔴 漏报" if winner else "🟢 正确避开")

        if v1_recommended and winner: v1_correct += 1
        elif v1_recommended and not winner: v1_wrong += 1
        elif not v1_recommended and winner: v1_missed += 1
        else: v1_avoided += 1

        if v2_recommended and winner: v2_correct += 1
        elif v2_recommended and not winner: v2_wrong += 1
        elif not v2_recommended and winner: v2_missed += 1
        else: v2_avoided += 1

        if not v1_recommended and v2_recommended and winner:
            ana_reason = f"分析师 {ana_score} 分，门槛降到 {v2_threshold}"
            rescued.append((r["name"], future, ana_reason))
        if v1_recommended and not v2_recommended and not winner:
            new_misses.append((r["name"], future))

        print(f"{r['name']:<14}"
              f"{v1_total:>5.0f}"
              f"{ana_score:>7}"
              f"{v2_threshold:>5}"
              f"  {v1_judge:<12}{v2_judge:<12}{future:>+8.1f}%")

    print("\n" + "=" * 95)
    print("  📊 v1 vs v2 对比")
    print("=" * 95)
    n = len(results)

    def stats(label, c, w, m, a):
        recall = c / (c + m) * 100 if (c + m) > 0 else 0
        precision = c / (c + w) * 100 if (c + w) > 0 else 0
        print(f"  {label:<8}命中 {c}  误报 {w}  漏报 {m}  正确避开 {a}  "
              f"召回率 {recall:.0f}%  准确率 {precision:.0f}%")

    print(f"  样本：{n} 只\n")
    stats("v1 原版", v1_correct, v1_wrong, v1_missed, v1_avoided)
    stats("v2 +信号", v2_correct, v2_wrong, v2_missed, v2_avoided)

    print(f"\n  🔼 v2 救回的漏报（v1 没推 / v2 推了 / 实际涨）：")
    if rescued:
        for name, fp, reason in rescued:
            print(f"    • {name:<14}涨 {fp:+.1f}%   分析师：{reason}")
    else:
        print(f"    无")

    print(f"\n  🔽 v2 引入的新误报（v1 推 / v2 没推 / 实际跌）：")
    if new_misses:
        for name, fp in new_misses:
            print(f"    • {name:<14}{fp:+.1f}%")
    else:
        print(f"    无")

    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reverse_validation_v2.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({
            "validation_date": VALIDATION_DATE,
            "v1": {"correct": v1_correct, "wrong": v1_wrong, "missed": v1_missed, "avoided": v1_avoided},
            "v2": {"correct": v2_correct, "wrong": v2_wrong, "missed": v2_missed, "avoided": v2_avoided},
            "rescued": rescued,
            "new_misses": new_misses,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n✅ 完整数据：{out_file}")


if __name__ == "__main__":
    main()
