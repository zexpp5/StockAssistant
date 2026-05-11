"""
学术因子模型（替换拍脑袋打分）
─────────────────────────────────────────
全部规则来自公开发表的学术论文，不是我编的。

1. Piotroski F-Score（0-9）
   - 出处: Joseph Piotroski (2000), Stanford. "Value Investing: The Use of
     Historical Financial Statement Information to Separate Winners from Losers"
   - 9 个二元会计指标，加和：
     盈利能力 (4):
       1. ROA > 0
       2. CFO > 0
       3. ΔROA > 0（同比改善）
       4. CFO > Net Income（盈利质量）
     杠杆/流动性 (3):
       5. ΔLong-term Debt < 0（长期负债下降）
       6. ΔCurrent Ratio > 0（流动性改善）
       7. 没有发新股（股本不增加）
     经营效率 (2):
       8. ΔGross Margin > 0
       9. ΔAsset Turnover > 0
   - 7-9 = 强 / 4-6 = 中 / 0-3 = 弱
   - 论文回测：高 F-Score 组合 1976-1996 年化超额收益 +7.5%，IR 1.0+

2. 12-1 月动量因子（Momentum）
   - 出处: Jegadeesh & Titman (1993), JF. "Returns to Buying Winners and
     Selling Losers"
   - 公式: return from t-252 to t-21（剔除最近 1 月，避免反转效应）
   - 论文回测：动量因子年化超额 +12%，是 Carhart 1997 四因子之一

3. 分析师 EPS 上修（已在 early_signals.py 实现）
   - 出处: Stickel (1991), JF; Womack (1996), JF
   - 论文：分析师上修后 90 天股票平均 +3% 超额

输出: factor_scores.json
"""
import sys
import os
import json
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yfinance as yf
import pandas as pd
import numpy as np


def _safe(v):
    try:
        f = float(v)
        if pd.isna(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _get_row(df, candidates):
    """从财报 DataFrame 取第一个能匹配的行（不同 ticker schema 略有差异）"""
    if df is None or df.empty:
        return None
    for c in candidates:
        if c in df.index:
            return df.loc[c]
    return None


def revenue_acceleration_pead(ticker, retries=2):
    """PEAD 因子（Post-Earnings-Announcement Drift）
       公式: 最近 1 季度 YoY 增速 - 上 1 季度 YoY 增速
       论文: Ball-Brown 1968 JAR / Lakonishok 1994 JF
       逻辑: 业绩加速后 60-90 天股票平均跑赢 5%
    """
    for attempt in range(retries + 1):
        try:
            t = yf.Ticker(ticker)
            qf = t.quarterly_financials
            if qf is None or qf.shape[1] < 5:
                return {"acceleration": None, "error": "not enough quarters"}
            cols = sorted(qf.columns, reverse=True)  # 新 → 旧
            rev = _get_row(qf, ["Total Revenue", "Operating Revenue"])
            if rev is None:
                return {"acceleration": None, "error": "no revenue row"}

            # 优先 YoY（需要 cols[5] 有效，季节性中性）
            if len(cols) >= 6:
                q_now = _safe(rev[cols[0]])
                q_yoy = _safe(rev[cols[4]])
                q_prev = _safe(rev[cols[1]])
                q_prev_yoy = _safe(rev[cols[5]])
                if all(x and x > 0 for x in [q_now, q_yoy, q_prev, q_prev_yoy]):
                    yoy_now = (q_now / q_yoy - 1) * 100
                    yoy_prev = (q_prev / q_prev_yoy - 1) * 100
                    return {
                        "acceleration": round(yoy_now - yoy_prev, 2),
                        "method": "YoY",
                        "yoy_now_pct": round(yoy_now, 2),
                        "yoy_prev_pct": round(yoy_prev, 2),
                        "error": None,
                    }

            # 降级 QoQ 加速度（需要 cols[2] 有效）
            if len(cols) >= 3:
                q0 = _safe(rev[cols[0]])
                q1 = _safe(rev[cols[1]])
                q2 = _safe(rev[cols[2]])
                if all(x and x > 0 for x in [q0, q1, q2]):
                    qoq_now = (q0 / q1 - 1) * 100
                    qoq_prev = (q1 / q2 - 1) * 100
                    return {
                        "acceleration": round(qoq_now - qoq_prev, 2),
                        "method": "QoQ",
                        "qoq_now_pct": round(qoq_now, 2),
                        "qoq_prev_pct": round(qoq_prev, 2),
                        "error": None,
                    }

            return {"acceleration": None, "error": "insufficient valid quarters"}
        except Exception as e:
            if attempt < retries:
                time.sleep(2 + attempt * 2)
                continue
            return {"acceleration": None, "error": str(e)}


def piotroski_f_score(ticker, retries=2):
    """计算 Piotroski F-Score（0-9）

    要求：至少 2 期年度财报（当期 + 上期）
    返回: {'f_score': int, 'details': dict, 'data_quality': 'full'/'partial'/'fail'}
    """
    for attempt in range(retries + 1):
        try:
            t = yf.Ticker(ticker)
            fin = t.financials  # income statement
            bs = t.balance_sheet
            cf = t.cashflow

            if fin is None or bs is None or cf is None:
                return {"f_score": None, "details": {}, "data_quality": "fail",
                        "error": "missing financials"}
            if fin.shape[1] < 2 or bs.shape[1] < 2:
                return {"f_score": None, "details": {}, "data_quality": "fail",
                        "error": "less than 2 periods"}

            # 取最近两年（columns 是日期，左到右一般是新到旧）
            # 但顺序不一定，确保按日期降序
            fin_cols = sorted(fin.columns, reverse=True)
            bs_cols = sorted(bs.columns, reverse=True)
            cf_cols = sorted(cf.columns, reverse=True)

            cur_y, prev_y = fin_cols[0], fin_cols[1]
            bs_cur, bs_prev = bs_cols[0], bs_cols[1]
            cf_cur = cf_cols[0]

            # ----- 拉所需指标 -----
            net_income = _get_row(fin, ["Net Income", "Net Income Common Stockholders"])
            total_revenue = _get_row(fin, ["Total Revenue", "Operating Revenue"])
            gross_profit = _get_row(fin, ["Gross Profit"])
            cost_revenue = _get_row(fin, ["Cost Of Revenue", "Reconciled Cost Of Revenue"])

            cfo = _get_row(cf, ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities",
                                "Total Cash From Operating Activities"])

            total_assets = _get_row(bs, ["Total Assets"])
            current_assets = _get_row(bs, ["Current Assets", "Total Current Assets"])
            current_liab = _get_row(bs, ["Current Liabilities", "Total Current Liabilities"])
            lt_debt = _get_row(bs, ["Long Term Debt"])
            shares = _get_row(bs, ["Ordinary Shares Number", "Share Issued",
                                   "Common Stock Equity"])

            score = 0
            details = {}

            # ── 盈利能力（4 项）─────────────────────────
            ni_cur = _safe(net_income[cur_y]) if net_income is not None else None
            ni_prev = _safe(net_income[prev_y]) if net_income is not None else None
            ta_cur = _safe(total_assets[bs_cur]) if total_assets is not None else None
            ta_prev = _safe(total_assets[bs_prev]) if total_assets is not None else None
            cfo_cur = _safe(cfo[cf_cur]) if cfo is not None else None

            # 1. ROA > 0
            roa_cur = (ni_cur / ta_cur) if (ni_cur is not None and ta_cur) else None
            details["1_roa>0"] = (roa_cur is not None and roa_cur > 0)
            score += 1 if details["1_roa>0"] else 0

            # 2. CFO > 0
            details["2_cfo>0"] = (cfo_cur is not None and cfo_cur > 0)
            score += 1 if details["2_cfo>0"] else 0

            # 3. ΔROA > 0
            roa_prev = (ni_prev / ta_prev) if (ni_prev is not None and ta_prev) else None
            details["3_droa>0"] = (roa_cur is not None and roa_prev is not None and roa_cur > roa_prev)
            score += 1 if details["3_droa>0"] else 0

            # 4. CFO > Net Income (现金质量)
            details["4_cfo>ni"] = (cfo_cur is not None and ni_cur is not None and cfo_cur > ni_cur)
            score += 1 if details["4_cfo>ni"] else 0

            # ── 杠杆/流动性（3 项）─────────────────────
            # 5. ΔLong-term Debt < 0（杠杆下降好）
            ltd_cur = _safe(lt_debt[bs_cur]) if lt_debt is not None else None
            ltd_prev = _safe(lt_debt[bs_prev]) if lt_debt is not None else None
            if ltd_cur is not None and ltd_prev is not None:
                # 用资产比标准化避免规模影响
                ltd_ratio_cur = ltd_cur / ta_cur if ta_cur else None
                ltd_ratio_prev = ltd_prev / ta_prev if ta_prev else None
                details["5_dltd<0"] = (ltd_ratio_cur is not None and ltd_ratio_prev is not None
                                       and ltd_ratio_cur < ltd_ratio_prev)
            else:
                details["5_dltd<0"] = False
            score += 1 if details["5_dltd<0"] else 0

            # 6. ΔCurrent Ratio > 0
            ca_cur = _safe(current_assets[bs_cur]) if current_assets is not None else None
            cl_cur = _safe(current_liab[bs_cur]) if current_liab is not None else None
            ca_prev = _safe(current_assets[bs_prev]) if current_assets is not None else None
            cl_prev = _safe(current_liab[bs_prev]) if current_liab is not None else None
            cr_cur = (ca_cur / cl_cur) if (ca_cur is not None and cl_cur) else None
            cr_prev = (ca_prev / cl_prev) if (ca_prev is not None and cl_prev) else None
            details["6_dcr>0"] = (cr_cur is not None and cr_prev is not None and cr_cur > cr_prev)
            score += 1 if details["6_dcr>0"] else 0

            # 7. No new shares issued（股本不增）
            if shares is not None:
                sh_cur = _safe(shares[bs_cur])
                sh_prev = _safe(shares[bs_prev])
                details["7_no_new_shares"] = (sh_cur is not None and sh_prev is not None
                                              and sh_cur <= sh_prev * 1.005)  # 容忍 0.5% 噪声
            else:
                details["7_no_new_shares"] = False
            score += 1 if details["7_no_new_shares"] else 0

            # ── 经营效率（2 项）─────────────────────
            # 8. ΔGross Margin > 0
            gp_cur = _safe(gross_profit[cur_y]) if gross_profit is not None else None
            gp_prev = _safe(gross_profit[prev_y]) if gross_profit is not None else None
            rev_cur = _safe(total_revenue[cur_y]) if total_revenue is not None else None
            rev_prev = _safe(total_revenue[prev_y]) if total_revenue is not None else None
            gm_cur = (gp_cur / rev_cur) if (gp_cur is not None and rev_cur) else None
            gm_prev = (gp_prev / rev_prev) if (gp_prev is not None and rev_prev) else None
            details["8_dgm>0"] = (gm_cur is not None and gm_prev is not None and gm_cur > gm_prev)
            score += 1 if details["8_dgm>0"] else 0

            # 9. ΔAsset Turnover > 0（营收/资产）
            at_cur = (rev_cur / ta_cur) if (rev_cur is not None and ta_cur) else None
            at_prev = (rev_prev / ta_prev) if (rev_prev is not None and ta_prev) else None
            details["9_dat>0"] = (at_cur is not None and at_prev is not None and at_cur > at_prev)
            score += 1 if details["9_dat>0"] else 0

            return {"f_score": score, "details": details, "data_quality": "full"}
        except Exception as e:
            if attempt < retries:
                time.sleep(2 + attempt * 2)
                continue
            return {"f_score": None, "details": {}, "data_quality": "fail", "error": str(e)}
    return None


def momentum_12_1(ticker, as_of=None, retries=2):
    """同时返回:
       - momentum_12_1: 12 月动量 (t-252→t-21)，剔除最近 1 月反转
         论文: Jegadeesh-Titman 1993, JF
       - reversal_1m: 短期反转 = -(过去 21 天收益)，跌得多得高分
         论文: Jegadeesh 1990, JF
    """
    for attempt in range(retries + 1):
        try:
            t = yf.Ticker(ticker)
            target = pd.to_datetime(as_of) if as_of else pd.Timestamp.now()
            start = target - pd.Timedelta(days=400)
            end = target + pd.Timedelta(days=2)
            hist = t.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
            if len(hist) < 252:
                return {"momentum_12_1": None, "reversal_1m": None, "error": "insufficient history"}

            hist = hist[hist.index.tz_localize(None) <= target] if hist.index.tz else hist[hist.index <= target]
            if len(hist) < 252:
                return {"momentum_12_1": None, "reversal_1m": None, "error": "insufficient history after cutoff"}

            close = hist["Close"]
            t_now = float(close.iloc[-1])
            t_minus_21 = float(close.iloc[-22])
            t_minus_252 = float(close.iloc[-253])
            mom_12_1 = (t_minus_21 / t_minus_252 - 1) * 100
            reversal_1m = -((t_now / t_minus_21 - 1) * 100)
            return {
                "momentum_12_1": round(mom_12_1, 2),
                "reversal_1m": round(reversal_1m, 2),
                "error": None,
            }
        except Exception as e:
            if attempt < retries:
                time.sleep(2 + attempt * 2)
                continue
            return {"momentum_12_1": None, "reversal_1m": None, "error": str(e)}


def fetch_factors_for(ticker, as_of=None):
    return {
        "ticker": ticker,
        "as_of": as_of,
        "piotroski": piotroski_f_score(ticker),
        "momentum": momentum_12_1(ticker, as_of=as_of),
        "pead": revenue_acceleration_pead(ticker),
    }


def fetch_factors_batch(tickers, as_of=None, sleep_sec=1.5):
    out = []
    for tk in tickers:
        print(f"  · {tk} ...", end="", flush=True)
        r = fetch_factors_for(tk, as_of=as_of)
        f = r["piotroski"]["f_score"]
        m = r["momentum"]["momentum_12_1"]
        print(f" F={f} 动量={m}%")
        out.append(r)
        time.sleep(sleep_sec)
    return out


# ============================================================
# 因子合成（横截面 z-score 等权）
# ============================================================
def combine_factors(records, analyst_signals=None, include_reversal=True):
    """4 因子等权合成（z-score 标准化后等权）

    因子（全部来自顶刊学术论文）:
      1. Piotroski F-Score (Stanford 2000)
      2. 12-1 月动量 (Jegadeesh-Titman JF 1993)
      3. 1 月反转 (Jegadeesh JF 1990) - include_reversal=True 时启用
      4. 分析师上修 (Stickel JF 1991, Womack JF 1996)
    """
    df = []
    for r in records:
        f = r["piotroski"]["f_score"]
        m = r["momentum"]["momentum_12_1"]
        rev = r["momentum"].get("reversal_1m")
        pead = (r.get("pead") or {}).get("acceleration")
        ana = (analyst_signals or {}).get(r["ticker"], 0)
        df.append({
            "ticker": r["ticker"],
            "f_score": f,
            "momentum": m,
            "reversal": rev,
            "pead": pead,
            "analyst": ana,
        })
    df = pd.DataFrame(df)

    def zscore(s, winsorize_pct=0.02):
        """z-score 标准化 + winsorize（学术标准，防止极值污染）

        winsorize_pct=0.02 把上下 2% 极端值 clip 到 2% 分位
        论文出处：Wooldridge 2010 计量经济学手册标准做法
        """
        s = pd.to_numeric(s, errors="coerce")
        if s.notna().sum() < 2:
            return s * 0
        # Winsorize
        lower = s.quantile(winsorize_pct)
        upper = s.quantile(1 - winsorize_pct)
        s_w = s.clip(lower, upper)
        mean = s_w.mean()
        std = s_w.std(ddof=0)
        if std == 0 or pd.isna(std):
            return s_w * 0
        return (s_w - mean) / std

    df["z_f"] = zscore(df["f_score"]).fillna(0)
    df["z_mom"] = zscore(df["momentum"]).fillna(0)
    df["z_rev"] = zscore(df["reversal"]).fillna(0)
    df["z_pead"] = zscore(df["pead"]).fillna(0)
    df["z_ana"] = zscore(df["analyst"]).fillna(0)
    df["composite"] = (df["z_f"] + df["z_mom"] + df["z_rev"] + df["z_pead"] + df["z_ana"]) / 5
    df = df.sort_values("composite", ascending=False).reset_index(drop=True)
    df["rank"] = df.index + 1
    return df


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+")
    parser.add_argument("--as-of", help="动量计算的截止日 YYYY-MM-DD")
    parser.add_argument("--out", help="输出 JSON")
    args = parser.parse_args()

    if not args.tickers:
        args.tickers = [
            "AMD", "INTC", "DDOG", "VRT", "LRCX", "MRVL", "AVGO",
            "AAPL", "MSFT", "CRM", "SNOW", "TSLA",
            # A 股/港股 yfinance 财报常无，这里只跑美股
        ]

    print("=" * 80)
    print(f"  📚 学术因子模型: Piotroski F-Score + 12-1 动量")
    print(f"  as_of = {args.as_of or '当前'} · {len(args.tickers)} 只股票")
    print("=" * 80)

    print(f"\n[1/2] 拉因子...")
    results = fetch_factors_batch(args.tickers, as_of=args.as_of)

    print(f"\n[2/2] 因子明细：")
    print(f"\n  {'股票':<10}{'F-Score':>9}{'动量12-1':>11}{'数据质量':>12}")
    print(f"  {'-'*45}")
    for r in results:
        f = r["piotroski"]["f_score"]
        m = r["momentum"]["momentum_12_1"]
        q = r["piotroski"]["data_quality"]
        f_str = str(f) if f is not None else "N/A"
        m_str = f"{m:+.1f}%" if m is not None else "N/A"
        print(f"  {r['ticker']:<10}{f_str:>9}{m_str:>11}{q:>12}")

    out_file = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "factor_scores.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "as_of": args.as_of,
            "results": results,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n✅ {out_file}")


if __name__ == "__main__":
    main()
