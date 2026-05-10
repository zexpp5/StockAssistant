"""
A 股 Piotroski F-Score（akshare 数据源）
─────────────────────────────────────────
解决问题 #1：yfinance 对 A 股没财报，所以北方稀土/中际/海光等无法进 v5 因子模型

数据源：akshare（开源免费）
  - A 股：stock_financial_report_sina（新浪财经）

Piotroski 9 项与美股版完全一致（论文标准 Stanford 2000）
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import akshare as ak
import pandas as pd


def _safe(v):
    try:
        f = float(v)
        if pd.isna(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _normalize_a_share_code(code):
    code = code.replace(".SS", "").replace(".SZ", "")
    if code.startswith(("0", "3")):
        return f"sz{code}"
    if code.startswith(("6", "9")):
        return f"sh{code}"
    return code


def _get_period(df, period_str):
    if df is None or len(df) == 0:
        return None
    rows = df[df["报告日"].astype(str).str.startswith(period_str)]
    if len(rows) == 0:
        return None
    return rows.iloc[0]


def piotroski_a_share(code, retries=2):
    for attempt in range(retries + 1):
        try:
            ak_code = _normalize_a_share_code(code)
            bs = ak.stock_financial_report_sina(stock=ak_code, symbol="资产负债表")
            inc = ak.stock_financial_report_sina(stock=ak_code, symbol="利润表")
            time.sleep(1.0)
            cf = ak.stock_financial_report_sina(stock=ak_code, symbol="现金流量表")

            if bs is None or inc is None or cf is None:
                return {"f_score": None, "data_quality": "fail", "error": "missing reports"}

            year_ends = bs[bs["报告日"].astype(str).str.endswith("1231")]["报告日"].astype(str).unique()
            year_ends = sorted(year_ends, reverse=True)
            if len(year_ends) < 2:
                return {"f_score": None, "data_quality": "fail", "error": "less than 2 annual reports"}

            cur_y = year_ends[0][:4]
            prev_y = year_ends[1][:4]

            bs_cur = _get_period(bs, f"{cur_y}1231")
            bs_prev = _get_period(bs, f"{prev_y}1231")
            inc_cur = _get_period(inc, f"{cur_y}1231")
            inc_prev = _get_period(inc, f"{prev_y}1231")
            cf_cur = _get_period(cf, f"{cur_y}1231")

            if any(x is None for x in [bs_cur, bs_prev, inc_cur, inc_prev, cf_cur]):
                return {"f_score": None, "data_quality": "fail", "error": "missing period"}

            ni_cur = _safe(inc_cur.get("净利润"))
            ni_prev = _safe(inc_prev.get("净利润"))
            rev_cur = _safe(inc_cur.get("营业总收入") or inc_cur.get("营业收入"))
            rev_prev = _safe(inc_prev.get("营业总收入") or inc_prev.get("营业收入"))
            cost_cur = _safe(inc_cur.get("营业成本"))
            cost_prev = _safe(inc_prev.get("营业成本"))

            ta_cur = _safe(bs_cur.get("资产总计"))
            ta_prev = _safe(bs_prev.get("资产总计"))
            ca_cur = _safe(bs_cur.get("流动资产合计"))
            ca_prev = _safe(bs_prev.get("流动资产合计"))
            cl_cur = _safe(bs_cur.get("流动负债合计"))
            cl_prev = _safe(bs_prev.get("流动负债合计"))
            ltd_cur = _safe(bs_cur.get("长期借款") or 0)
            ltd_prev = _safe(bs_prev.get("长期借款") or 0)
            shares_cur = _safe(bs_cur.get("实收资本(或股本)"))
            shares_prev = _safe(bs_prev.get("实收资本(或股本)"))

            cfo_cur = _safe(cf_cur.get("经营活动产生的现金流量净额"))

            score = 0
            details = {}

            roa_cur = (ni_cur / ta_cur) if (ni_cur and ta_cur) else None
            details["1_roa>0"] = (roa_cur is not None and roa_cur > 0)
            if details["1_roa>0"]: score += 1

            details["2_cfo>0"] = (cfo_cur is not None and cfo_cur > 0)
            if details["2_cfo>0"]: score += 1

            roa_prev = (ni_prev / ta_prev) if (ni_prev and ta_prev) else None
            details["3_droa>0"] = (roa_cur is not None and roa_prev is not None and roa_cur > roa_prev)
            if details["3_droa>0"]: score += 1

            details["4_cfo>ni"] = (cfo_cur is not None and ni_cur is not None and cfo_cur > ni_cur)
            if details["4_cfo>ni"]: score += 1

            ltd_r_cur = (ltd_cur / ta_cur) if (ltd_cur is not None and ta_cur) else None
            ltd_r_prev = (ltd_prev / ta_prev) if (ltd_prev is not None and ta_prev) else None
            details["5_dltd<0"] = (ltd_r_cur is not None and ltd_r_prev is not None and ltd_r_cur < ltd_r_prev)
            if details["5_dltd<0"]: score += 1

            cr_cur = (ca_cur / cl_cur) if (ca_cur and cl_cur) else None
            cr_prev = (ca_prev / cl_prev) if (ca_prev and cl_prev) else None
            details["6_dcr>0"] = (cr_cur is not None and cr_prev is not None and cr_cur > cr_prev)
            if details["6_dcr>0"]: score += 1

            details["7_no_new_shares"] = (shares_cur is not None and shares_prev is not None
                                          and shares_cur <= shares_prev * 1.005)
            if details["7_no_new_shares"]: score += 1

            gm_cur = ((rev_cur - cost_cur) / rev_cur) if (rev_cur and cost_cur is not None) else None
            gm_prev = ((rev_prev - cost_prev) / rev_prev) if (rev_prev and cost_prev is not None) else None
            details["8_dgm>0"] = (gm_cur is not None and gm_prev is not None and gm_cur > gm_prev)
            if details["8_dgm>0"]: score += 1

            at_cur = (rev_cur / ta_cur) if (rev_cur and ta_cur) else None
            at_prev = (rev_prev / ta_prev) if (rev_prev and ta_prev) else None
            details["9_dat>0"] = (at_cur is not None and at_prev is not None and at_cur > at_prev)
            if details["9_dat>0"]: score += 1

            return {
                "f_score": score,
                "details": details,
                "data_quality": "full",
                "year_used": cur_y,
                "data_source": "akshare/sina",
            }
        except Exception as e:
            if attempt < retries:
                time.sleep(2 + attempt * 2)
                continue
            return {"f_score": None, "data_quality": "fail", "error": str(e)}


def momentum_a_share(code, as_of=None, retries=2):
    """A 股 12-1 月动量 + 1 月反转因子。

    ⚠️ 关键：使用前复权（qfq）价格序列。
    A 股分红 / 送转股会在除权日"砍一刀"价格，akshare 默认 adjust="" 返回不复权价。
    跨过除权日的动量会被严重低估（银行/公用事业类年分红 5-7%，1 年累计偏差 20-30%）。
    前复权以最新价为基准回算历史价，序列连续 = total return 视角，对动量因子是正确选择。
    """
    for attempt in range(retries + 1):
        try:
            sina_code = _normalize_a_share_code(code)
            target = pd.to_datetime(as_of) if as_of else pd.Timestamp.now()
            start = target - pd.Timedelta(days=400)
            end = target + pd.Timedelta(days=2)
            df = ak.stock_zh_a_daily(symbol=sina_code,
                                    start_date=start.strftime("%Y%m%d"),
                                    end_date=end.strftime("%Y%m%d"),
                                    adjust="qfq")    # 前复权 — 见 docstring
            if df is None or len(df) < 252:
                return {"momentum_12_1": None, "reversal_1m": None, "error": "insufficient history"}

            df["date"] = pd.to_datetime(df["date"])
            df = df[df["date"] <= target].sort_values("date")
            if len(df) < 252:
                return {"momentum_12_1": None, "reversal_1m": None, "error": "insufficient after cutoff"}

            close = df["close"]
            t_now = float(close.iloc[-1])
            t_minus_21 = float(close.iloc[-22])
            t_minus_252 = float(close.iloc[-253])
            mom = (t_minus_21 / t_minus_252 - 1) * 100
            rev = -((t_now / t_minus_21 - 1) * 100)
            return {
                "momentum_12_1": round(mom, 2),
                "reversal_1m": round(rev, 2),
                "adjust": "qfq",
                "error": None,
            }
        except Exception as e:
            if attempt < retries:
                time.sleep(2 + attempt * 2)
                continue
            return {"momentum_12_1": None, "reversal_1m": None, "error": str(e)}


def fetch_factors_a_share(code, as_of=None):
    return {
        "ticker": code,
        "as_of": as_of,
        "piotroski": piotroski_a_share(code),
        "momentum": momentum_a_share(code, as_of=as_of),
    }


def main():
    SAMPLES = [
        ("北方稀土", "600111.SS"),
        ("中际旭创", "300308.SZ"),
        ("海光信息", "688041.SS"),
        ("寒武纪", "688256.SS"),
    ]
    print("=" * 95)
    print(f"  📚 A 股 Piotroski F-Score（akshare 数据源）")
    print("=" * 95)
    print(f"\n  {'股票':<10}{'代码':<14}{'F-Score':>9}{'12-1 动量':>11}{'1月反转':>10}{'数据'}")
    print(f"  {'-'*70}")
    results = []
    for name, code in SAMPLES:
        try:
            r = fetch_factors_a_share(code, as_of=None)
            f = r["piotroski"].get("f_score")
            m = r["momentum"].get("momentum_12_1")
            rev = r["momentum"].get("reversal_1m")
            yr = r["piotroski"].get("year_used", "?")
            f_str = str(f) if f is not None else "N/A"
            m_str = f"{m:+.1f}%" if m is not None else "N/A"
            rev_str = f"{rev:+.1f}%" if rev is not None else "N/A"
            print(f"  {name:<10}{code:<14}{f_str:>9}{m_str:>11}{rev_str:>10}  年报{yr}")
            results.append(r)
        except Exception as e:
            print(f"  {name:<10}{code:<14} 失败: {e}")
        time.sleep(2)

    import json
    from datetime import datetime
    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factor_scores_china.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now().isoformat(), "results": results},
                 f, ensure_ascii=False, indent=2, default=str)
    print(f"\n✅ {out_file}")


if __name__ == "__main__":
    main()
