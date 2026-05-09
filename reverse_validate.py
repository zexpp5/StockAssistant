"""
反向验证：2026/1/1 → 5/8 涨幅最大的股票，
如果在 1/1 用我的当前打分体系，能不能提前看到？

测试样本：
- AMD（YTD +103.7%）
- Intel（YTD +217%）
- SK Hynix（YTD +149%）
- 寒武纪（YTD +26.9% 但 1Y +150%）
- Datadog（YTD +49.6%）
- 中际旭创（A 股 1Y +824%）

对每只股票：
1. 拉 2025-12-31 那天的价格 / PE / PEG / 1Y 涨幅（基于过去 1 年到 2025-12-31）
2. 用我的打分公式套一遍
3. 看会不会进「⭐⭐⭐ 强烈推荐」
4. 然后看 1/1 → 5/8 真实涨幅
5. 计算「漏报 / 误报 / 命中」
"""
import sys, os, json
import numpy as np
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yfinance as yf

# 测试样本：YTD 涨过 50%+ 或 1Y 涨过 100%+ 的标的
SAMPLES = [
    # (name, ticker, AI_relevance, 我们当时的认知)
    ("AMD",          "AMD",        "极强（核心标的）"),
    ("Intel",        "INTC",       "中（间接受益）"),
    ("SK Hynix",     "000660.KS",  "极强（核心标的）"),
    ("Datadog",      "DDOG",       "强（直接受益）"),
    ("Vertiv",       "VRT",        "极强（核心标的）"),
    ("中际旭创",       "300308.SZ",  "极强（核心标的）"),
    ("寒武纪",        "688256.SS",  "极强（核心标的）"),
    ("Lam Research", "LRCX",       "强（直接受益）"),
    ("Marvell",      "MRVL",       "强（直接受益）"),
    ("Broadcom",     "AVGO",       "强（直接受益）"),
    # 控制组（涨幅低或负）
    ("Apple",        "AAPL",       "强（直接受益）"),
    ("Microsoft",    "MSFT",       "极强（核心标的）"),
    ("Salesforce",   "CRM",        "强（直接受益）"),
    ("Snowflake",    "SNOW",       "强（直接受益）"),
    ("Tesla",        "TSLA",       "强（直接受益）"),
]

VALIDATION_DATE = "2025-12-31"  # 假设站在这一天打分


def fetch_data_at(ticker, target_date_str):
    """拉到指定日期为止的历史，模拟「当天的认知」"""
    target = datetime.strptime(target_date_str, "%Y-%m-%d")
    start = target - timedelta(days=400)
    end = target + timedelta(days=2)
    try:
        t = yf.Ticker(ticker)
        hist = t.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
        if len(hist) < 100:
            return None
        # 截取到 target_date 为止
        hist_until = hist[hist.index.date <= target.date()]
        if len(hist_until) < 50:
            return None
        last_price = float(hist_until["Close"].iloc[-1])
        # 1 年涨幅（近似 252 天）
        if len(hist_until) >= 252:
            year_ago = float(hist_until["Close"].iloc[-252])
        else:
            year_ago = float(hist_until["Close"].iloc[0])
        one_year_pct = (last_price / year_ago - 1) * 100
        # 1 周涨幅
        if len(hist_until) >= 5:
            week_ago = float(hist_until["Close"].iloc[-5])
            one_week_pct = (last_price / week_ago - 1) * 100
        else:
            one_week_pct = 0

        # 拉 future 价格（5/8）
        future_end = datetime.strptime("2026-05-09", "%Y-%m-%d")
        hist_future = t.history(start=target.strftime("%Y-%m-%d"), end=future_end.strftime("%Y-%m-%d"))
        if len(hist_future) > 0:
            future_price = float(hist_future["Close"].iloc[-1])
            future_pct = (future_price / last_price - 1) * 100
        else:
            future_price, future_pct = None, None

        return {
            "as_of_price": last_price,
            "one_year_pct_at_time": one_year_pct,
            "one_week_pct_at_time": one_week_pct,
            "future_price": future_price,
            "future_pct": future_pct,
        }
    except Exception as e:
        print(f"  ! {ticker}: {e}")
        return None


# ============================================================
# 模拟 daily_picks.py 的打分逻辑（站在 2025-12-31 的认知）
# ============================================================
def score_ai_relevance(ar):
    if "极强" in ar: return 35
    if "强（直接" in ar or ar == "强": return 28
    if "中" in ar: return 18
    if "弱" in ar: return 8
    return 0


def score_valuation(forward_pe_estimate):
    """2025 末时我们对每只的远期 PE 估计（事后视角，但接近当时分析师共识）
    没有 PEG 时用 PE 近似"""
    if forward_pe_estimate is None: return 5
    if forward_pe_estimate < 25: return 15
    if forward_pe_estimate < 40: return 10
    if forward_pe_estimate < 60: return 6
    return 3


def score_trend(one_year_pct, one_week_pct):
    score = 0
    if one_year_pct > 200: score = 12  # 涨太多扣分
    elif one_year_pct > 50: score = 20
    elif one_year_pct > 0: score = 15
    else: score = 8
    if one_week_pct > 0: score += 5
    return min(score, 25)


def score_credibility():
    return 15  # 假设当时也有完整数据（实际可能不全）


def grade(total):
    if total >= 75: return "⭐⭐⭐ 强烈推荐"
    if total >= 60: return "⭐⭐ 推荐"
    if total >= 50: return "⭐ 关注"
    return "未入选"


# ============================================================
# 历史 PE 估计（基于 2025-12 时的真实分析师预期）
# ============================================================
PE_AT_2025_12 = {
    "AMD":        45,  # 当时分析师预期高（已经 +200%）
    "INTC":       30,  # 当时还没爆 14A，PE 估计低
    "000660.KS":  10,  # HBM 周期股 PE 历来低
    "DDOG":       60,  # 高估值 SaaS
    "VRT":        35,  # 已涨过一波
    "300308.SZ":  30,  # A 股光模块
    "688256.SS":  150, # 寒武纪 2025 末已经 PE 100+
    "LRCX":       25,  # 半导体设备合理
    "MRVL":       40,  # 已涨过
    "AVGO":       30,  # 收购消化中
    "AAPL":       30,
    "MSFT":       30,
    "CRM":        25,  # 当时低估
    "SNOW":       100, # 高估值
    "TSLA":       150, # Robotaxi 故事股
}


def main():
    print("=" * 80)
    print("  🔍 反向验证：站在 2025-12-31，我的打分会推荐谁？")
    print("=" * 80)

    print(f"\n[1/2] 拉每只股票到 {VALIDATION_DATE} 的历史 + 之后真实涨跌...")
    results = []
    for name, ticker, ai_rel in SAMPLES:
        d = fetch_data_at(ticker, VALIDATION_DATE)
        if not d:
            print(f"  ❌ {name}")
            continue
        print(f"  ✅ {name:<14}{ticker:<14}1Y@当时={d['one_year_pct_at_time']:+.1f}% · 之后涨跌={d['future_pct']:+.1f}%")
        results.append({"name": name, "ticker": ticker, "ai_rel": ai_rel, **d})

    # 打分
    print(f"\n[2/2] 站在 {VALIDATION_DATE} 用我的打分体系...")
    print(f"\n{'股票':<14}{'AI':>4}{'估值':>5}{'趋势':>5}{'可信':>5}{'总分':>6}{'评级':<14}{'之后涨跌':>10}{'判断'}")
    print("-" * 95)

    total_picks_correct = 0  # 体系预测推荐 + 实际涨过 +20%
    total_picks_wrong = 0    # 体系推荐但实际跌
    total_missed = 0         # 体系没推荐但实际涨过 +20%
    total_avoided = 0        # 体系没推荐且实际跌

    for r in results:
        s_ai = score_ai_relevance(r["ai_rel"])
        s_val = score_valuation(PE_AT_2025_12.get(r["ticker"]))
        s_trend = score_trend(r["one_year_pct_at_time"], r["one_week_pct_at_time"])
        s_cred = score_credibility()
        total = s_ai + s_val + s_trend + s_cred
        gr = grade(total)
        recommended = total >= 75
        future = r["future_pct"]
        actual_winner = future >= 20

        if recommended and actual_winner:
            judgment = "✅ 命中"
            total_picks_correct += 1
        elif recommended and not actual_winner:
            judgment = "❌ 误报"
            total_picks_wrong += 1
        elif not recommended and actual_winner:
            judgment = "🔴 漏报"
            total_missed += 1
        else:
            judgment = "🟢 正确避开"
            total_avoided += 1

        print(f"{r['name']:<14}{s_ai:>4}{s_val:>5}{s_trend:>5}{s_cred:>5}{total:>6.0f}  {gr:<14}{future:>+9.1f}%   {judgment}")

    print("\n" + "=" * 80)
    print(f"  📊 反向验证结果")
    print("=" * 80)
    n = len(results)
    print(f"  样本：{n} 只")
    print(f"  ✅ 命中（推荐+实际涨>20%）：     {total_picks_correct}")
    print(f"  ❌ 误报（推荐但实际跌/横盘）：  {total_picks_wrong}")
    print(f"  🔴 漏报（没推荐但实际涨>20%）： {total_missed}  ⬅️ **这是关键问题**")
    print(f"  🟢 正确避开：                  {total_avoided}")

    if total_picks_correct + total_missed > 0:
        recall = total_picks_correct / (total_picks_correct + total_missed) * 100
        print(f"\n  📌 召回率（涨股中推荐了多少）：{recall:.1f}%")
    if total_picks_correct + total_picks_wrong > 0:
        precision = total_picks_correct / (total_picks_correct + total_picks_wrong) * 100
        print(f"  📌 准确率（推荐中真涨了多少）：{precision:.1f}%")

    print(f"\n💡 关键洞察")
    print(f"  漏报的股票：")
    for r in results:
        s_ai = score_ai_relevance(r["ai_rel"])
        s_val = score_valuation(PE_AT_2025_12.get(r["ticker"]))
        s_trend = score_trend(r["one_year_pct_at_time"], r["one_week_pct_at_time"])
        s_cred = score_credibility()
        total = s_ai + s_val + s_trend + s_cred
        if total < 75 and r["future_pct"] >= 20:
            print(f"    • {r['name']:<14}总分 {total:.0f} 分 / 之后涨 {r['future_pct']:+.1f}% — 我的体系没推荐")

    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reverse_validation.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({
            "validation_date": VALIDATION_DATE,
            "results": results,
            "summary": {
                "total": n,
                "correct": total_picks_correct,
                "wrong": total_picks_wrong,
                "missed": total_missed,
                "avoided": total_avoided,
            },
        }, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 完整数据：{out_file}")


if __name__ == "__main__":
    main()
