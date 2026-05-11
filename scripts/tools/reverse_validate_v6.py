"""反向验证 v6：5 因子（+ PEAD 业绩加速度 Ball-Brown 1968）

修改: combine_factors 已加入 z_pead，5 因子等权
"""
import sys, os, json
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root
from reverse_validate import fetch_data_at, VALIDATION_DATE
from factor_model import combine_factors
from early_signals import score_analyst
import yfinance as yf

SAMPLES = [
    ("AMD", "AMD"), ("Intel", "INTC"), ("Datadog", "DDOG"),
    ("Vertiv", "VRT"), ("Lam Research", "LRCX"), ("Marvell", "MRVL"),
    ("Broadcom", "AVGO"), ("Apple", "AAPL"), ("Microsoft", "MSFT"),
    ("Salesforce", "CRM"), ("Snowflake", "SNOW"), ("Tesla", "TSLA"),
]
ALPHA_THR = 5.0


def main():
    from reverse_validate import load_factors_and_signals
    fd, sd = load_factors_and_signals()
    sig_map = {r["ticker"]: r for r in sd["results"]}

    print("=" * 110)
    print("  🔍 反向验证 v6：5 因子（+ PEAD 业绩加速度）")
    print("=" * 110)

    spy = yf.Ticker("SPY").history(start="2025-12-29", end="2026-05-10")
    spy_ret = (spy["Close"].iloc[-1] / spy["Close"].iloc[0] - 1) * 100
    print(f"\n  SPY: {spy_ret:+.2f}% · winner = α ≥ {ALPHA_THR}%")

    fp, ap = {}, {}
    for name, tk in SAMPLES:
        d = fetch_data_at(tk, VALIDATION_DATE)
        if d:
            fp[tk] = d["future_pct"]
            ap[tk] = d["future_pct"] - spy_ret

    ana = {tk: score_analyst(s.get("analyst"))[0] for tk, s in sig_map.items()}
    df = combine_factors(fd["results"], analyst_signals=ana, include_reversal=True)
    df["future_pct"] = df["ticker"].map(fp)
    df["alpha"] = df["ticker"].map(ap)
    df = df.dropna(subset=["future_pct"])

    cutoff = df["composite"].quantile(2/3)
    df["rec"] = df["composite"] >= cutoff
    df["win"] = df["alpha"] >= ALPHA_THR

    print(f"\n[2/3] 5 因子合成（top tertile cutoff z={cutoff:.2f}）：")
    print(f"\n  {'排':<3}{'股票':<8}{'F':>3}{'动量':>7}{'反转':>8}{'PEAD':>8}{'分析师':>7}{'综合z':>8}{'推荐':>5}{'α%':>9}  结果")
    print(f"  {'-'*92}")
    c = w = m = a = 0
    for _, r in df.iterrows():
        rec, win = bool(r["rec"]), bool(r["win"])
        if rec and win: j = "✅ 命中"; c += 1
        elif rec and not win: j = "❌ 误报"; w += 1
        elif (not rec) and win: j = "🔴 漏报"; m += 1
        else: j = "🟢 正确避开"; a += 1

        f_s = str(int(r['f_score'])) if pd.notna(r['f_score']) else "-"
        m_s = f"{r['momentum']:+.0f}%" if pd.notna(r['momentum']) else "N/A"
        rv_s = f"{r['reversal']:+.1f}%" if pd.notna(r['reversal']) else "N/A"
        pead_s = f"{r['pead']:+.1f}" if pd.notna(r['pead']) else "N/A"
        print(f"  {int(r['rank']):<3}{r['ticker']:<8}{f_s:>3}{m_s:>7}{rv_s:>8}{pead_s:>8}"
              f"{int(r['analyst']):>7}{r['composite']:>+7.2f}{'是' if rec else '否':>5}"
              f"{r['alpha']:>+8.1f}%   {j}")

    print("\n" + "=" * 110)
    rec_rate = c / (c + m) * 100 if (c + m) > 0 else 0
    prec = c / (c + w) * 100 if (c + w) > 0 else 0
    print(f"\n  v1 我编的 4 维:               召回 40%   准确 80%")
    print(f"  v2 v1 + 分析师动态门槛:       召回 70%   准确 64%")
    print(f"  v3 学术 3 因子 z≥0.5:         召回 43%   准确 100%")
    print(f"  v4 学术 3 因子 + tertile + α: 召回 57%   准确 100%")
    print(f"  v5 学术 4 因子（+ 反转）:      召回 57%   准确 100%")
    print(f"  v6 学术 5 因子（+ PEAD）:      召回 {rec_rate:.0f}%   准确 {prec:.0f}%")
    print(f"\n     v6: 命中 {c}  误报 {w}  漏报 {m}  正确避开 {a}")
    df.to_json("reverse_validation_v6.json", orient="records", force_ascii=False, indent=2)


if __name__ == "__main__":
    main()
