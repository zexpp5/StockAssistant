"""A 股版 Altman Z''-Score / Beneish M-Score — P0-3b (2026-05-12)。

为什么需要 A 股专版（vs fundamental_deep.py 美股版）：
  fundamental_deep.py 走 FMP（美股财报源），FMP 对 A 股代码（600519 等）不支持。
  fmp_client.is_available() 在 FMP_API_KEY 缺失时返回 None，graceful skip。

A 股 Z''-Score（新兴市场变体，Altman 2000）：
  Z'' = 6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4    （比 Z 少 X5，避免行业偏差）
  阈值：Z'' > 2.6 安全 / 1.1-2.6 灰色 / Z'' < 1.1 破产警示

数据源：yfinance（A 股 .SS / .SZ / .BJ 后缀），字段全英文（与美股版一致）。

⚠️ 已知约束：
  - 金融 / 地产 / 公用事业 Z'' 模型不适用（资产负债结构特殊）；上游通过
    a_share_industry.Z_PRIME_INAPPLICABLE_SECTORS 排除（A-6 接入）。本模块
    照算 Z 值并提供 verdict，由调用方决定是否标红旗。
  - Beneish M 对超高增长公司（SGI>1.5）伪阳性，已用 m_score_adjusted
    （Beneish & Nichols 2007）规避。

实测（茅台 600519 / 宁德 300750 / 寒武纪 688256）：
  茅台 Z''=14.13 SAFE,  M_adj=-2.89 LOW → 无红旗
  宁德 Z''=3.41 SAFE,   M_adj=-2.61 LOW → 无红旗
  寒武纪 Z''=14.07 SAFE, M_adj=-2.85 LOW（SGI=5.53 触发 caveat 但 adj 后健康）
"""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _normalize_a_share_ticker(ticker: str) -> str:
    """A 股代码自动加交易所后缀供 yfinance 用。

    600519 / 600519.SS / 000001 / 300750 / 688256 / 833533 都能识别。
    """
    t = str(ticker).upper().strip()
    if t.endswith((".SS", ".SZ", ".BJ")):
        return t
    code = t.replace(".SS", "").replace(".SZ", "").replace(".BJ", "")
    if not (code.isdigit() and len(code) == 6):
        return t
    if code.startswith("6"):
        return f"{code}.SS"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return t


def _safe_div(a, b):
    if a is None or b is None or b == 0:
        return None
    try:
        return float(a) / float(b)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _get_row(df, candidates: list[str], col_idx: int = 0):
    """从 yfinance 财报 DataFrame 取一行（candidates 第一个命中），按 col_idx 取期次。

    col_idx=0 是最新期；col_idx=1 是上一期。
    """
    if df is None or df.empty:
        return None
    cols = list(df.columns)
    if col_idx >= len(cols):
        return None
    for name in candidates:
        if name in df.index:
            try:
                v = df.loc[name].iloc[col_idx]
                if v is None:
                    continue
                return float(v)
            except (TypeError, ValueError, IndexError, KeyError):
                continue
    return None


def _fetch_a_share_statements(ticker: str) -> tuple:
    """拉 A 股 yfinance 三表（financials, balance_sheet, cashflow）。

    需要至少 2 期数据（M-Score 需上下期对比）。失败时返回 (None, None, None)。
    """
    yf_ticker = _normalize_a_share_ticker(ticker)
    try:
        import yfinance as yf
        t = yf.Ticker(yf_ticker)
        fin = t.financials
        bs = t.balance_sheet
        cf = t.cashflow
        if fin is None or bs is None or cf is None:
            return None, None, None
        if fin.empty or bs.empty:
            return None, None, None
        return fin, bs, cf
    except Exception as e:
        logger.debug("yfinance A 股财报拉取失败 %s: %s", yf_ticker, e)
        return None, None, None


# ─────────── Altman Z''-Score (Altman 2000 新兴市场版) ───────────

def altman_z_double_prime_a(ticker: str) -> dict[str, Any]:
    """A 股 Altman Z''-Score（新兴市场 / 非制造业版）。

    公式：Z'' = 6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4
      X1 = 营运资本 / 总资产
      X2 = 留存收益 / 总资产
      X3 = EBIT / 总资产
      X4 = 股权账面价值 / 总负债（注：账面 BV，不是市值）

    阈值（Altman 2000）：
      Z'' > 2.6      🟢 SAFE
      1.1 < Z'' < 2.6  🟡 GREY ZONE
      Z'' < 1.1      🔴 DISTRESS

    与美股 Z 相比少 X5（销售/总资产），因该项对新兴市场金融/服务/科技不公平。
    """
    fin, bs, _ = _fetch_a_share_statements(ticker)
    if fin is None or bs is None:
        return {"error": "no statements", "ticker": ticker}

    ta = _get_row(bs, ["Total Assets"])
    tl = _get_row(bs, ["Total Liabilities Net Minority Interest", "Total Liabilities"])
    # 营运资本：优先用 yfinance 直接字段，缺则 CA - CL 算
    wc = _get_row(bs, ["Working Capital"])
    if wc is None:
        ca = _get_row(bs, ["Current Assets", "Total Current Assets"])
        cl = _get_row(bs, ["Current Liabilities", "Total Current Liabilities"])
        wc = (ca - cl) if (ca is not None and cl is not None) else None
    re = _get_row(bs, ["Retained Earnings"])
    equity = _get_row(bs, ["Stockholders Equity", "Common Stock Equity",
                           "Total Equity Gross Minority Interest"])
    ebit = _get_row(fin, ["EBIT", "Operating Income", "Pretax Income"])

    x1 = _safe_div(wc, ta)
    x2 = _safe_div(re, ta)
    x3 = _safe_div(ebit, ta)
    x4 = _safe_div(equity, tl)

    components = {"X1_wc/ta": x1, "X2_re/ta": x2, "X3_ebit/ta": x3, "X4_eq/tl": x4}
    missing = [k for k, v in components.items() if v is None]
    if len(missing) > 1:
        return {"error": f"missing components: {missing}", "ticker": ticker,
                "components": components}

    coefs = {"X1_wc/ta": 6.56, "X2_re/ta": 3.26, "X3_ebit/ta": 6.72, "X4_eq/tl": 1.05}
    z = sum(coefs[k] * v for k, v in components.items() if v is not None)

    if z > 2.6:
        verdict = "🟢 SAFE (Z'' > 2.6)"
    elif z > 1.1:
        verdict = "🟡 GREY ZONE (1.1 < Z'' < 2.6)"
    else:
        verdict = "🔴 DISTRESS (Z'' < 1.1)"

    return {
        "ticker": ticker,
        "z_score": round(z, 3),
        "verdict": verdict,
        "components": {k: (round(v, 4) if v is not None else None)
                       for k, v in components.items()},
        "missing": missing,
        "source": "Altman 2000 - 新兴市场 Z''-Score",
    }


# ─────────── Beneish M-Score (A 股 yfinance 适配) ───────────

def beneish_m_score_a(ticker: str) -> dict[str, Any]:
    """A 股 Beneish M-Score（8 变量盈利操纵识别，1999 FAJ）。

    M = -4.84 + 0.92·DSRI + 0.528·GMI + 0.404·AQI + 0.892·SGI
        + 0.115·DEPI - 0.172·SGAI + 4.679·TATA - 0.327·LVGI

    阈值：M > -1.78 高造假风险；-2.22 < M < -1.78 中等；M < -2.22 低风险
    使用 m_score_adjusted（去掉高增长 SGI 贡献）规避 SGI>1.5 假阳性。
    """
    fin, bs, cf = _fetch_a_share_statements(ticker)
    if fin is None or bs is None or cf is None:
        return {"error": "no statements", "ticker": ticker}

    if fin.shape[1] < 2 or bs.shape[1] < 2 or cf.shape[1] < 2:
        return {"error": "insufficient statements (need ≥2 years)", "ticker": ticker}

    def f(df, names, ci): return _get_row(df, names, col_idx=ci)

    rev_t = f(fin, ["Total Revenue", "Operating Revenue"], 0)
    rev_p = f(fin, ["Total Revenue", "Operating Revenue"], 1)
    gp_t = f(fin, ["Gross Profit"], 0)
    gp_p = f(fin, ["Gross Profit"], 1)
    ni_t = f(fin, ["Net Income", "Net Income Common Stockholders"], 0)
    sga_t = f(fin, ["Selling General And Administration", "SGA Expense"], 0)
    sga_p = f(fin, ["Selling General And Administration", "SGA Expense"], 1)

    ta_t = f(bs, ["Total Assets"], 0)
    ta_p = f(bs, ["Total Assets"], 1)
    ar_t = f(bs, ["Accounts Receivable", "Receivables", "Net Receivables"], 0)
    ar_p = f(bs, ["Accounts Receivable", "Receivables", "Net Receivables"], 1)
    ca_t = f(bs, ["Current Assets", "Total Current Assets"], 0)
    ca_p = f(bs, ["Current Assets", "Total Current Assets"], 1)
    ppe_t = f(bs, ["Net PPE", "Gross PPE", "Property Plant Equipment Net"], 0)
    ppe_p = f(bs, ["Net PPE", "Gross PPE", "Property Plant Equipment Net"], 1)
    tl_t = f(bs, ["Total Liabilities Net Minority Interest", "Total Liabilities"], 0)
    tl_p = f(bs, ["Total Liabilities Net Minority Interest", "Total Liabilities"], 1)

    dep_t = f(cf, ["Reconciled Depreciation", "Depreciation Amortization Depletion",
                   "Depreciation And Amortization"], 0)
    dep_p = f(cf, ["Reconciled Depreciation", "Depreciation Amortization Depletion",
                   "Depreciation And Amortization"], 1)
    cfo_t = f(cf, ["Operating Cash Flow",
                   "Cash Flow From Continuing Operating Activities"], 0)

    dso_t = _safe_div(ar_t, rev_t)
    dso_p = _safe_div(ar_p, rev_p)
    dsri = _safe_div(dso_t, dso_p)

    gm_t = _safe_div(gp_t, rev_t)
    gm_p = _safe_div(gp_p, rev_p)
    gmi = _safe_div(gm_p, gm_t)

    def aqi_ratio(ta_, ca_, ppe_):
        if ta_ is None or ca_ is None or ppe_ is None or ta_ <= 0:
            return None
        return 1 - (ca_ + ppe_) / ta_
    aqi_t = aqi_ratio(ta_t, ca_t, ppe_t)
    aqi_p = aqi_ratio(ta_p, ca_p, ppe_p)
    aqi = _safe_div(aqi_t, aqi_p)

    sgi = _safe_div(rev_t, rev_p)

    def dep_rate(dep_, ppe_):
        if dep_ is None or ppe_ is None or (dep_ + ppe_) <= 0:
            return None
        return dep_ / (dep_ + ppe_)
    drt = dep_rate(dep_t, ppe_t)
    drp = dep_rate(dep_p, ppe_p)
    depi = _safe_div(drp, drt)

    sga_rate_t = _safe_div(sga_t, rev_t)
    sga_rate_p = _safe_div(sga_p, rev_p)
    sgai = _safe_div(sga_rate_t, sga_rate_p)

    tata = _safe_div(
        (ni_t - cfo_t) if (ni_t is not None and cfo_t is not None) else None,
        ta_t,
    )

    lev_t = _safe_div(tl_t, ta_t)
    lev_p = _safe_div(tl_p, ta_p)
    lvgi = _safe_div(lev_t, lev_p)

    coefs = {"DSRI": (dsri, 0.92), "GMI": (gmi, 0.528), "AQI": (aqi, 0.404),
             "SGI": (sgi, 0.892), "DEPI": (depi, 0.115), "SGAI": (sgai, -0.172),
             "TATA": (tata, 4.679), "LVGI": (lvgi, -0.327)}

    missing = [k for k, (v, _) in coefs.items() if v is None]
    if len(missing) > 3:
        return {"error": f"too many missing variables: {missing}",
                "ticker": ticker,
                "variables": {k: v for k, (v, _) in coefs.items()}}

    m_score = -4.84
    for v, c in coefs.values():
        if v is not None:
            m_score += c * v

    high_growth_caveat = sgi is not None and sgi > 1.5
    m_score_adjusted = (m_score - 0.892 * sgi
                        if (high_growth_caveat and sgi is not None) else m_score)

    def _level(m):
        if m > -1.78:
            return "high"
        if m > -2.22:
            return "medium"
        return "low"

    risk_level = _level(m_score_adjusted)
    risk_level_raw = _level(m_score)

    if m_score > -1.78:
        if high_growth_caveat:
            verdict = (f"🟡 ELEVATED on raw M ({m_score:.2f}) but growth-adjusted "
                       f"M={m_score_adjusted:.2f} → {risk_level.upper()} (SGI={sgi:.2f}>1.5)")
        else:
            verdict = "🔴 HIGH manipulation risk (M > -1.78)"
    elif m_score > -2.22:
        verdict = "🟡 MEDIUM (gray zone)"
    else:
        verdict = "🟢 LOW manipulation risk"

    return {
        "ticker": ticker,
        "m_score": round(m_score, 3),
        "m_score_adjusted": round(m_score_adjusted, 3),
        "risk_level": risk_level,
        "risk_level_raw": risk_level_raw,
        "verdict": verdict,
        "variables": {k: (round(v, 4) if v is not None else None)
                      for k, (v, _) in coefs.items()},
        "missing": missing,
        "high_growth_caveat": high_growth_caveat,
        "source": "Beneish 1999 FAJ + Beneish & Nichols 2007 growth adj.",
    }


# ─────────── 软红旗 helper ───────────

def build_a_share_risk_flags(altman: dict | None, beneish: dict | None,
                              z_prime_inapplicable: bool = False) -> list[str]:
    """A 股版红旗清单（不淘汰，仅标注）。

    阈值用 A 股 Z''（1.1，比美股 Z 的 1.81 更紧）。
    z_prime_inapplicable=True 时（金融/地产/公用事业），跳过 Z 校验只看 M。
    """
    flags: list[str] = []
    if altman and not altman.get("error") and not z_prime_inapplicable:
        z = altman.get("z_score")
        if z is not None and z < 1.1:
            flags.append(f"🚨 Altman Z''={z:.2f}<1.1 破产警示")
    if beneish and not beneish.get("error"):
        if beneish.get("risk_level") == "high":
            m_adj = beneish.get("m_score_adjusted")
            flags.append(f"🚨 Beneish M={m_adj:.2f}>-1.78 造假风险")
    return flags
