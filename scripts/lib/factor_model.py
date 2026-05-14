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


# ────────────────────────────────────────────────────────
# 港股财报 via akshare (2026-05-11 新增,补 yfinance 港股 fundamentals 空白)
# ────────────────────────────────────────────────────────

# Piotroski 9 项需要的字段 → akshare 港股科目名映射
# 港股字段名(中文)从 akshare stock_financial_hk_report_em probe 出来,与 yfinance 英文 schema 对齐
_HK_BS_MAP = {
    "Total Assets": "总资产",
    "Current Assets": "流动资产合计",
    "Total Current Assets": "流动资产合计",
    "Current Liabilities": "流动负债合计",
    "Total Current Liabilities": "流动负债合计",
    "Long Term Debt": "长期贷款",
    "Ordinary Shares Number": "股本",
    "Share Issued": "股本",
}
_HK_FIN_MAP = {
    "Net Income": "股东应占溢利",
    "Net Income Common Stockholders": "股东应占溢利",
    "Total Revenue": "营运收入",
    "Operating Revenue": "营运收入",
    "Gross Profit": "毛利",
}
_HK_CF_MAP = {
    "Operating Cash Flow": "经营业务现金净额",
    "Total Cash From Operating Activities": "经营业务现金净额",
    "Cash Flow From Continuing Operating Activities": "经营业务现金净额",
}


def _hk_long_to_wide(df_long: "pd.DataFrame", date_col: str) -> "pd.DataFrame":
    """akshare 港股财报是长表(每行 = 科目×日期),转成 yfinance 宽表结构(行=科目,列=日期降序)。"""
    df = df_long.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    # 同期同科目多条(港股财报偶有修订)→ 取最新一条
    df = df.drop_duplicates(subset=["STD_ITEM_NAME", date_col], keep="last")
    wide = df.pivot(index="STD_ITEM_NAME", columns=date_col, values="AMOUNT")
    # 列(日期)降序: 最新在前,与 yfinance 一致
    wide = wide.reindex(sorted(wide.columns, reverse=True), axis=1)
    return wide


def _apply_field_map(wide: "pd.DataFrame", field_map: dict) -> "pd.DataFrame":
    """把宽表的中文科目行重命名为 yfinance 英文 schema。

    返回 DataFrame 同时含中文行(向后兼容)+ 英文行(yfinance 兼容)。
    piotroski_f_score 通过 _get_row(candidates=[英文, 中文]) 优先匹配英文。
    """
    out_rows = {}
    for en_name, cn_name in field_map.items():
        if cn_name in wide.index:
            out_rows[en_name] = wide.loc[cn_name]
    if not out_rows:
        return wide
    en_df = pd.DataFrame(out_rows).T
    en_df.columns = wide.columns
    # concat: 英文行在前,piotroski 优先取到
    return pd.concat([en_df, wide], axis=0)


def _fetch_hk_financials_akshare(ticker: str, retries: int = 1):
    """拉港股三大表,返回 (fin, bs, cf) 三个 yfinance-style DataFrame。

    Args:
      ticker: "0700.HK" / "0700" / "00700.HK" 都接受,会规范化成 akshare 需要的 5 位前导零格式

    Returns:
      (fin, bs, cf): 三个 DataFrame; 失败时对应位置为 None
    """
    import akshare as ak
    # 规范化 ticker: akshare stock_financial_hk_report_em 接受 "00700" 5 位前导零
    raw = ticker.upper().replace(".HK", "").lstrip("0")
    ak_code = raw.zfill(5)

    def _safe_fetch(symbol: str):
        for attempt in range(retries + 1):
            try:
                df = ak.stock_financial_hk_report_em(stock=ak_code, symbol=symbol, indicator="年度")
                if df is None or df.empty:
                    return None
                return df
            except Exception:
                if attempt < retries:
                    time.sleep(1.0)
                    continue
                return None

    df_bs = _safe_fetch("资产负债表")
    df_fin = _safe_fetch("利润表")
    df_cf = _safe_fetch("现金流量表")

    # 资产负债表 用 STD_REPORT_DATE; 利润表/现金流量表 用 REPORT_DATE (akshare 表现不一致)
    bs = _apply_field_map(_hk_long_to_wide(df_bs, "STD_REPORT_DATE"), _HK_BS_MAP) if df_bs is not None else None
    fin = _apply_field_map(_hk_long_to_wide(df_fin, "REPORT_DATE"), _HK_FIN_MAP) if df_fin is not None else None
    cf = _apply_field_map(_hk_long_to_wide(df_cf, "REPORT_DATE"), _HK_CF_MAP) if df_cf is not None else None

    return fin, bs, cf


def _is_hk_ticker(ticker: str) -> bool:
    return ticker.upper().endswith(".HK")


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


def revenue_acceleration_pead(ticker, as_of=None, retries=2):
    """PEAD 因子（Post-Earnings-Announcement Drift）
       公式: 最近 1 季度 YoY 增速 - 上 1 季度 YoY 增速
       论文: Ball-Brown 1968 JAR / Lakonishok 1994 JF
       逻辑: 业绩加速后 60-90 天股票平均跑赢 5%

       PIT (2026-05-12 C-5)：as_of 给定时，过滤 quarterly columns 到
       fiscal_date <= as_of - 45 天（季报披露滞后保守估计）
    """
    # 港股只有年报+半年报,没季度数据 → PEAD 不可算,优雅返回 None
    if _is_hk_ticker(ticker):
        return {"acceleration": None, "error": "hk: no quarterly data"}
    for attempt in range(retries + 1):
        try:
            t = yf.Ticker(ticker)
            qf = t.quarterly_financials
            if qf is None or qf.shape[1] < 5:
                return {"acceleration": None, "error": "not enough quarters"}
            cols = sorted(qf.columns, reverse=True)  # 新 → 旧

            # PIT 过滤：仅保留 as_of - 45 天 之前的财报列
            if as_of is not None:
                cutoff = pd.to_datetime(as_of) - pd.Timedelta(days=45)
                cols = [c for c in cols if pd.to_datetime(c) <= cutoff]
                if len(cols) < 5:
                    return {"acceleration": None,
                            "error": f"PIT filter (as_of={as_of}, lag=45d): <5 quarters"}
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


def piotroski_f_score(ticker, as_of=None, retries=2):
    """计算 Piotroski F-Score（0-9）

    要求：至少 2 期年度财报（当期 + 上期）
    返回: {'f_score': int, 'details': dict, 'data_quality': 'full'/'partial'/'fail'}

    PIT (2026-05-12 C-5)：as_of 给定时，过滤年报 columns 到
    fiscal_date <= as_of - 滞后天数（美股 65 / 港股 120 / 其它 90 天）
    """
    for attempt in range(retries + 1):
        try:
            # 港股走 akshare(yfinance fundamentals 对港股 404),其余走 yfinance
            if _is_hk_ticker(ticker):
                fin, bs, cf = _fetch_hk_financials_akshare(ticker)
            else:
                t = yf.Ticker(ticker)
                fin = t.financials
                bs = t.balance_sheet
                cf = t.cashflow

            if fin is None or bs is None or cf is None:
                return {"f_score": None, "details": {}, "data_quality": "fail",
                        "error": "missing financials"}
            if fin.shape[1] < 2 or bs.shape[1] < 2:
                return {"f_score": None, "details": {}, "data_quality": "fail",
                        "error": "less than 2 periods"}

            # PIT 过滤：仅保留 as_of - 滞后天数 之前的年报列
            if as_of is not None:
                lag_days = 120 if _is_hk_ticker(ticker) else 65
                cutoff = pd.to_datetime(as_of) - pd.Timedelta(days=lag_days)
                def _filter(df):
                    if df is None or df.empty:
                        return df
                    valid = [c for c in df.columns if pd.to_datetime(c) <= cutoff]
                    return df[valid] if valid else df.iloc[:, :0]
                fin = _filter(fin)
                bs = _filter(bs)
                cf = _filter(cf)
                if fin.shape[1] < 2 or bs.shape[1] < 2:
                    return {"f_score": None, "details": {}, "data_quality": "fail",
                            "error": f"PIT filter (as_of={as_of}, lag={lag_days}d): <2 periods"}

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

            # PIT 暴露：让 PIT 测试 / audit 能查"用了哪一年的财报"
            try:
                details["latest_fiscal_date"] = pd.Timestamp(cur_y).strftime("%Y-%m-%d")
            except Exception:
                details["latest_fiscal_date"] = str(cur_y)

            return {"f_score": score, "details": details, "data_quality": "full",
                    "as_of": as_of}
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


def compute_quality_factors(ticker, as_of=None, retries=2):
    """ROIC / FCFY / Accruals 三个学术质量因子（C-2 2026-05-12）。

    - ROIC = NOPAT / Invested Capital
      Koller-Goedhart-Wessels (2020) "Valuation"：ROIC 是衡量经营效率黄金标准
    - FCFY = FCF / Market Cap
      自由现金流收益率（低估值 + 真现金，避免 PE 受会计调整影响）
    - Accruals = (Net Income - CFO) / Total Assets
      Sloan (1996) AR：应计高的公司未来收益显著低（被广泛复现的会计异象）

    PIT (C-5)：as_of 给定时，过滤 columns 到 fiscal_date <= as_of - 65 天。
    港股 / A 股走各自专版（_is_hk_ticker → 返回 not_supported；A 股见 factor_model_china）
    """
    if _is_hk_ticker(ticker):
        return {"roic": None, "fcfy": None, "accruals": None,
                "error": "hk: use factor_model_china equivalent"}
    for attempt in range(retries + 1):
        try:
            t = yf.Ticker(ticker)
            fin = t.financials
            bs = t.balance_sheet
            cf = t.cashflow
            info = t.info or {}
            market_cap = info.get("marketCap")

            if fin is None or bs is None or cf is None or fin.empty or bs.empty:
                return {"roic": None, "fcfy": None, "accruals": None,
                        "error": "missing financials"}

            # PIT 过滤
            if as_of is not None:
                cutoff = pd.to_datetime(as_of) - pd.Timedelta(days=65)
                def _filter(df):
                    if df is None or df.empty:
                        return df
                    valid = [c for c in df.columns if pd.to_datetime(c) <= cutoff]
                    return df[valid] if valid else df.iloc[:, :0]
                fin = _filter(fin); bs = _filter(bs); cf = _filter(cf)
                if fin.shape[1] < 1 or bs.shape[1] < 1 or cf.shape[1] < 1:
                    return {"roic": None, "fcfy": None, "accruals": None,
                            "error": f"PIT filter (as_of={as_of}): insufficient periods"}

            cur_y = sorted(fin.columns, reverse=True)[0]
            bs_cur = sorted(bs.columns, reverse=True)[0]
            cf_cur = sorted(cf.columns, reverse=True)[0]

            ebit_row = _get_row(fin, ["EBIT", "Operating Income"])
            ebit = _safe(ebit_row[cur_y]) if ebit_row is not None else None

            ta_row = _get_row(bs, ["Total Assets"])
            cl_row = _get_row(bs, ["Current Liabilities", "Total Current Liabilities"])
            ta = _safe(ta_row[bs_cur]) if ta_row is not None else None
            cl = _safe(cl_row[bs_cur]) if cl_row is not None else None
            invested_cap = (ta - cl) if (ta is not None and cl is not None) else None

            ni_row = _get_row(fin, ["Net Income", "Net Income Common Stockholders"])
            ni = _safe(ni_row[cur_y]) if ni_row is not None else None

            ocf_row = _get_row(cf, ["Operating Cash Flow",
                                    "Cash Flow From Continuing Operating Activities"])
            ocf = _safe(ocf_row[cf_cur]) if ocf_row is not None else None
            capex_row = _get_row(cf, ["Capital Expenditure"])
            capex = _safe(capex_row[cf_cur]) if capex_row is not None else None
            # yfinance capex 一般是负数（投入），fcf = ocf + capex（负数）
            fcf = (ocf + capex) if (ocf is not None and capex is not None) else None

            # ROIC: NOPAT / Invested Capital；NOPAT = EBIT × (1 - 美国企业税率 21%)
            nopat = ebit * (1 - 0.21) if ebit is not None else None
            roic = (nopat / invested_cap * 100) if (nopat is not None and invested_cap and invested_cap > 0) else None

            # FCFY: FCF / Market Cap
            fcfy = (fcf / market_cap * 100) if (fcf is not None and market_cap and market_cap > 0) else None

            # Accruals: (NI - CFO) / Total Assets（Sloan 1996）
            accruals = ((ni - ocf) / ta) if (ni is not None and ocf is not None and ta and ta > 0) else None

            return {
                "roic": round(roic, 2) if roic is not None else None,
                "fcfy": round(fcfy, 2) if fcfy is not None else None,
                "accruals": round(accruals, 4) if accruals is not None else None,
                "fiscal_date": pd.Timestamp(cur_y).strftime("%Y-%m-%d"),
                "as_of": as_of,
                "source": "Koller 2020 (ROIC) + Sloan 1996 (Accruals)",
            }
        except Exception as e:
            if attempt < retries:
                time.sleep(2 + attempt * 2)
                continue
            return {"roic": None, "fcfy": None, "accruals": None, "error": str(e)[:120]}


def fetch_factors_for(ticker, as_of=None):
    return {
        "ticker": ticker,
        "as_of": as_of,
        "piotroski": piotroski_f_score(ticker, as_of=as_of),
        "momentum": momentum_12_1(ticker, as_of=as_of),
        "pead": revenue_acceleration_pead(ticker, as_of=as_of),
        "quality": compute_quality_factors(ticker, as_of=as_of),
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
DEFAULT_FACTOR_WEIGHTS = {
    "f_score": 1.0,
    "momentum": 1.0,
    "reversal": 1.0,
    "pead": 1.0,
    "analyst": 1.0,
    "quality": 1.0,
}


def combine_factors(records, analyst_signals=None, include_reversal=True,
                    include_quality=True, factor_weights=None,
                    min_coverage_score=0.50):
    """6 因子合成（z-score 标准化后按有效权重合成）。

    因子（全部来自顶刊学术论文）:
      1. Piotroski F-Score (Stanford 2000)
      2. 12-1 月动量 (Jegadeesh-Titman JF 1993)
      3. 1 月反转 (Jegadeesh JF 1990)
      4. 分析师上修 (Stickel JF 1991, Womack JF 1996)
      5. PEAD 收入加速 (Ball-Brown JAR 1968)
      6. Quality 合成因子（2026-05-12 二审驱动接入）
         - ROIC (Koller 2020)
         - -Accruals (Sloan 1996 AR)：高应计 → 未来收益低，故取负
         z_quality = (z_roic + z_(-accruals)) / 2

    include_quality=False 时退回 5 因子（旧行为，用于消融对照）。
    factor_weights 可由 IC gate 传入；被降权到 0 的因子不会参与 composite。
    缺失因子不再 fill 成中性 0，而是降低 coverage_score，并对 composite 施加保守惩罚。
    """
    weights = dict(DEFAULT_FACTOR_WEIGHTS)
    if factor_weights:
        weights.update({k: float(v) for k, v in factor_weights.items() if k in weights})
    if not include_reversal:
        weights["reversal"] = 0.0
    if not include_quality:
        weights["quality"] = 0.0

    df = []
    for r in records:
        f = r["piotroski"]["f_score"]
        m = r["momentum"]["momentum_12_1"]
        rev = r["momentum"].get("reversal_1m")
        pead = (r.get("pead") or {}).get("acceleration")
        ana = (analyst_signals or {}).get(r["ticker"])
        q = r.get("quality") or {}
        roic = q.get("roic") if not q.get("error") else None
        accruals = q.get("accruals") if not q.get("error") else None
        df.append({
            "ticker": r["ticker"],
            "f_score": f,
            "momentum": m,
            "reversal": rev,
            "pead": pead,
            "analyst": ana,
            "roic": roic,
            "accruals_neg": (-accruals) if accruals is not None else None,  # Sloan 取负
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

    df["z_f"] = zscore(df["f_score"])
    df["z_mom"] = zscore(df["momentum"])
    df["z_rev"] = zscore(df["reversal"])
    df["z_pead"] = zscore(df["pead"])
    df["z_ana"] = zscore(df["analyst"])

    if include_quality:
        df["z_roic"] = zscore(df["roic"])
        df["z_acc_neg"] = zscore(df["accruals_neg"])
        df["z_quality"] = (df["z_roic"] + df["z_acc_neg"]) / 2
    else:
        df["z_quality"] = np.nan

    factor_cols = {
        "f_score": ("z_f", "f_score"),
        "momentum": ("z_mom", "momentum"),
        "reversal": ("z_rev", "reversal"),
        "pead": ("z_pead", "pead"),
        "analyst": ("z_ana", "analyst"),
        "quality": ("z_quality", "roic"),
    }
    active = [(name, col, raw_col, max(0.0, weights.get(name, 0.0)))
              for name, (col, raw_col) in factor_cols.items()
              if max(0.0, weights.get(name, 0.0)) > 0]
    total_active_w = sum(w for _, _, _, w in active)
    if total_active_w <= 0:
        df["coverage_score"] = 0.0
        df["composite_raw"] = np.nan
        df["composite"] = -1.0
        df["missing_factors"] = ",".join(factor_cols)
        df["factor_weights_used"] = "{}"
    else:
        weighted_sum = pd.Series(0.0, index=df.index)
        covered_weight = pd.Series(0.0, index=df.index)
        missing_by_row: list[list[str]] = [[] for _ in range(len(df))]
        weights_used = {name: round(w / total_active_w, 6) for name, _, _, w in active}
        for name, z_col, raw_col, w in active:
            available = df[raw_col].notna() & df[z_col].notna()
            weighted_sum = weighted_sum.add(df[z_col].where(available, 0.0) * w, fill_value=0.0)
            covered_weight = covered_weight.add(available.astype(float) * w, fill_value=0.0)
            for idx, ok in enumerate(available.tolist()):
                if not ok:
                    missing_by_row[idx].append(name)
        df["coverage_score"] = (covered_weight / total_active_w).round(4)
        df["composite_raw"] = (weighted_sum / covered_weight.replace(0, np.nan))
        penalty = ((min_coverage_score - df["coverage_score"]).clip(lower=0) / min_coverage_score)
        df["composite"] = (df["composite_raw"].fillna(0.0) * df["coverage_score"] - penalty).round(6)
        df["missing_factors"] = [",".join(x) for x in missing_by_row]
        df["factor_weights_used"] = json.dumps(weights_used, sort_keys=True)

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
