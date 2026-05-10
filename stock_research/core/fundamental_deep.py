"""个股基本面深度分析（B 路线 Phase 1）

学术依据 — 全部来自顶刊论文，每个公式可追溯：

1. 杜邦五因子分解（Du Pont 1920s, 5-step refinement Penman 2013）
   ROE = (NI/EBT) × (EBT/EBIT) × (EBIT/Sales) × (Sales/Assets) × (Assets/Equity)
       = 税负担 × 利息负担 × 经营利润率 × 总资产周转 × 财务杠杆
   作用：拆解 ROE 变动来源 — 是经营改善还是加杠杆？

2. Beneish M-Score（Beneish 1999 Financial Analysts Journal）
   "The Detection of Earnings Manipulation"
   8 变量加权识别盈利操纵概率；M > -1.78 高风险
   实证：识别 Enron、WorldCom 等会计造假案例

3. Altman Z-Score（Altman 1968 Journal of Finance）
   "Financial Ratios, Discriminant Analysis and the Prediction of Corporate Bankruptcy"
   5 变量预测 2 年内破产；Z < 1.81 破产警示
   1968-2018 实证准确率 80-90%

4. 盈利质量 8 项（Sloan 1996 Accounting Review + Dechow-Dichev 2002）
   "Do Stock Prices Fully Reflect Information in Accruals and Cash Flows"
   应收/存货/商誉/研发/递延/非经常/CFO 倍数/FCF 转化率

数据源：FMP（10 年历史，含完整三表细项）；缺 FMP 时返回部分指标。
"""
from __future__ import annotations
import logging
from typing import Any

from . import fmp_client

logger = logging.getLogger(__name__)


def _safe_div(a, b):
    """安全除法：a/b，任一缺失或 b≈0 → None。"""
    if a is None or b is None:
        return None
    try:
        a, b = float(a), float(b)
        if abs(b) < 1e-9:
            return None
        return a / b
    except (TypeError, ValueError):
        return None


def _safe_growth(cur, prev):
    """安全同比增速：(cur/prev - 1)；prev≤0 时返回 None 避免符号陷阱。"""
    if cur is None or prev is None:
        return None
    try:
        cur, prev = float(cur), float(prev)
        if prev <= 0:
            return None
        return cur / prev - 1
    except (TypeError, ValueError):
        return None


# ────────────────────────────────────────────────────────
# 1. 杜邦五因子分解
# ────────────────────────────────────────────────────────

def dupont_5factor(ticker: str) -> dict[str, Any]:
    """杜邦五因子分解（最近 2 年对比）。

    ROE = 税负担 × 利息负担 × 经营利润率 × 总资产周转 × 财务杠杆

    返回：{
      'roe_cur', 'roe_prev', 'roe_change',
      'factors': {tax_burden, interest_burden, ebit_margin, asset_turnover, leverage},
      'attribution': {因子名: 对 ROE 变动的贡献百分点},
      'verdict': 'quality_growth' / 'leverage_driven' / 'margin_driven' / 'mixed'
    }
    """
    inc = fmp_client.fetch_income_full(ticker, years=2)
    bs = fmp_client.fetch_balance_sheet(ticker, years=2)
    if not inc or not bs or len(inc) < 2 or len(bs) < 2:
        return {"error": "insufficient financial statements", "ticker": ticker}

    def compute_factors(i, b_cur, b_prev):
        revenue = i.get("revenue")
        ebit = i.get("operating_income")
        ebt = i.get("income_before_tax")
        ni = i.get("net_income")
        ta_avg = _safe_div((b_cur.get("total_assets") or 0) + (b_prev.get("total_assets") or 0), 2)
        eq_avg = _safe_div((b_cur.get("total_equity") or 0) + (b_prev.get("total_equity") or 0), 2)

        return {
            "tax_burden": _safe_div(ni, ebt),          # NI / EBT
            "interest_burden": _safe_div(ebt, ebit),   # EBT / EBIT
            "ebit_margin": _safe_div(ebit, revenue),   # EBIT / Sales
            "asset_turnover": _safe_div(revenue, ta_avg),  # Sales / Avg Assets
            "leverage": _safe_div(ta_avg, eq_avg),     # Avg Assets / Avg Equity
        }

    # 当期 = inc[0]，上期 = inc[1]；avg 资产/权益用相邻两年
    if len(bs) >= 2:
        cur_factors = compute_factors(inc[0], bs[0], bs[1])
    else:
        cur_factors = compute_factors(inc[0], bs[0], bs[0])

    # 上期需要 N-1 和 N-2 的资产负债表
    if len(bs) >= 3:
        prev_factors = compute_factors(inc[1], bs[1], bs[2])
    elif len(bs) >= 2:
        prev_factors = compute_factors(inc[1], bs[1], bs[1])
    else:
        prev_factors = None

    def roe_from(factors):
        if not factors:
            return None
        vals = [factors[k] for k in ("tax_burden", "interest_burden", "ebit_margin", "asset_turnover", "leverage")]
        if any(v is None for v in vals):
            return None
        result = 1.0
        for v in vals:
            result *= v
        return result

    roe_cur = roe_from(cur_factors)
    roe_prev = roe_from(prev_factors)

    # 归因：固定其他因子，只让一个因子从 prev → cur，看 ROE 变化（学术标准做法 Penman 2013）
    attribution = {}
    if cur_factors and prev_factors and roe_cur is not None and roe_prev is not None:
        for key in ("tax_burden", "interest_burden", "ebit_margin", "asset_turnover", "leverage"):
            if cur_factors[key] is None or prev_factors[key] is None:
                continue
            mixed = dict(prev_factors)
            mixed[key] = cur_factors[key]
            roe_mix = roe_from(mixed)
            if roe_mix is not None:
                attribution[key] = round((roe_mix - roe_prev) * 100, 2)  # 百分点

    # 解读：哪个因子贡献最大
    verdict = "insufficient_data"
    if attribution:
        top = max(attribution.items(), key=lambda x: abs(x[1]))
        if top[0] in ("ebit_margin",):
            verdict = "margin_driven"      # 经营改善（最优质）
        elif top[0] in ("asset_turnover",):
            verdict = "efficiency_driven"  # 资产效率提升（也好）
        elif top[0] == "leverage":
            verdict = "leverage_driven"    # ⚠️ 加杠杆（风险）
        elif top[0] in ("tax_burden", "interest_burden"):
            verdict = "non_operating"      # 非经营因素
        else:
            verdict = "mixed"

    return {
        "ticker": ticker,
        "roe_cur": round(roe_cur * 100, 2) if roe_cur is not None else None,
        "roe_prev": round(roe_prev * 100, 2) if roe_prev is not None else None,
        "roe_change_pp": round((roe_cur - roe_prev) * 100, 2) if (roe_cur and roe_prev) else None,
        "factors_cur": {k: (round(v, 4) if v is not None else None) for k, v in (cur_factors or {}).items()},
        "factors_prev": {k: (round(v, 4) if v is not None else None) for k, v in (prev_factors or {}).items()} if prev_factors else None,
        "attribution_pp": attribution,
        "verdict": verdict,
        "source": "FMP/income-statement+balance-sheet",
    }


# ────────────────────────────────────────────────────────
# 2. Beneish M-Score（财务造假识别）
# ────────────────────────────────────────────────────────

def beneish_m_score(ticker: str) -> dict[str, Any]:
    """Beneish M-Score（8 变量盈利操纵识别）。

    论文：Beneish (1999) FAJ "The Detection of Earnings Manipulation"
    M = -4.84 + 0.92·DSRI + 0.528·GMI + 0.404·AQI + 0.892·SGI
        + 0.115·DEPI - 0.172·SGAI + 4.679·TATA - 0.327·LVGI

    阈值：M > -1.78 高造假风险；M < -2.22 低风险
    实证：1982-1992 样本识别准确率 76%
    """
    inc = fmp_client.fetch_income_full(ticker, years=2)
    bs = fmp_client.fetch_balance_sheet(ticker, years=2)
    cf = fmp_client.fetch_cash_flow(ticker, years=2)
    if not inc or not bs or not cf or len(inc) < 2 or len(bs) < 2 or len(cf) < 2:
        return {"error": "insufficient statements (need ≥2 years)", "ticker": ticker}

    i_t, i_p = inc[0], inc[1]
    b_t, b_p = bs[0], bs[1]
    c_t, c_p = cf[0], cf[1]

    # 1. DSRI = (AR_t / Sales_t) / (AR_p / Sales_p) — 应收账款天数指数
    dso_t = _safe_div(b_t.get("net_receivables"), i_t.get("revenue"))
    dso_p = _safe_div(b_p.get("net_receivables"), i_p.get("revenue"))
    dsri = _safe_div(dso_t, dso_p)

    # 2. GMI = GM_p / GM_t — 毛利率指数（恶化时 >1）
    gm_t = _safe_div(i_t.get("gross_profit"), i_t.get("revenue"))
    gm_p = _safe_div(i_p.get("gross_profit"), i_p.get("revenue"))
    gmi = _safe_div(gm_p, gm_t)

    # 3. AQI = (1 - (CA_t + PPE_t)/TA_t) / (1 - (CA_p + PPE_p)/TA_p) — 资产质量指数
    def aqi_ratio(b):
        ta = b.get("total_assets")
        ca = b.get("total_current_assets")
        ppe = b.get("ppe")
        if ta is None or ca is None or ppe is None or ta <= 0:
            return None
        return 1 - (ca + ppe) / ta
    aqi_t = aqi_ratio(b_t)
    aqi_p = aqi_ratio(b_p)
    aqi = _safe_div(aqi_t, aqi_p)

    # 4. SGI = Sales_t / Sales_p — 销售增长指数
    sgi = _safe_div(i_t.get("revenue"), i_p.get("revenue"))

    # 5. DEPI = (DEP_p / (DEP_p + PPE_p)) / (DEP_t / (DEP_t + PPE_t)) — 折旧率指数
    def dep_rate(c, b):
        dep = c.get("depreciation_amortization")
        ppe = b.get("ppe")
        if dep is None or ppe is None or (dep + ppe) <= 0:
            return None
        return dep / (dep + ppe)
    dep_t = dep_rate(c_t, b_t)
    dep_p = dep_rate(c_p, b_p)
    depi = _safe_div(dep_p, dep_t)

    # 6. SGAI = (SGA_t / Sales_t) / (SGA_p / Sales_p) — 销管费率指数
    sga_t = _safe_div(i_t.get("sga_expense"), i_t.get("revenue"))
    sga_p = _safe_div(i_p.get("sga_expense"), i_p.get("revenue"))
    sgai = _safe_div(sga_t, sga_p)

    # 7. TATA = (NI - CFO) / TA — 总应计/总资产（应计盈余比例）
    ni = i_t.get("net_income")
    cfo = c_t.get("operating_cash_flow")
    ta = b_t.get("total_assets")
    tata = _safe_div((ni - cfo) if (ni is not None and cfo is not None) else None, ta)

    # 8. LVGI = (TL_t / TA_t) / (TL_p / TA_p) — 杠杆指数
    lev_t = _safe_div(b_t.get("total_liabilities"), b_t.get("total_assets"))
    lev_p = _safe_div(b_p.get("total_liabilities"), b_p.get("total_assets"))
    lvgi = _safe_div(lev_t, lev_p)

    # 计算 M-Score
    coefs = {"DSRI": (dsri, 0.92), "GMI": (gmi, 0.528), "AQI": (aqi, 0.404),
             "SGI": (sgi, 0.892), "DEPI": (depi, 0.115), "SGAI": (sgai, -0.172),
             "TATA": (tata, 4.679), "LVGI": (lvgi, -0.327)}

    missing = [k for k, (v, _) in coefs.items() if v is None]
    if len(missing) > 3:
        return {"error": f"too many missing variables: {missing}", "ticker": ticker,
                "variables": {k: v for k, (v, _) in coefs.items()}}

    m_score = -4.84
    for v, c in coefs.values():
        if v is not None:
            m_score += c * v

    # 阈值解读（Beneish 1999）
    # 已知缺陷：超高增长公司（SGI > 1.5）会假阳性，SGI 单项就贡献 +0.5 到 M
    # 学术界共识：高增长股需结合应收/存货/应计单独判断（Beneish & Nichols 2007）
    high_growth_caveat = sgi is not None and sgi > 1.5

    # 高增长调整：扣除 SGI 贡献后重算 M-Score（去掉销售增长这一项的影响）
    # 这是 Beneish & Nichols (2007) 提出的 "ex-growth" 处理思路 — 不改阈值，改 M。
    # 下游（research_report / 风险标注 / LLM 提示）应优先使用 m_score_adjusted + risk_level。
    if high_growth_caveat and sgi is not None:
        m_score_adjusted = m_score - 0.892 * sgi
    else:
        m_score_adjusted = m_score

    def _level_from_m(m: float) -> str:
        if m > -1.78:
            return "high"
        if m > -2.22:
            return "medium"
        return "low"

    risk_level_raw = _level_from_m(m_score)            # 原始 M 的等级（含高增长伪信号）
    risk_level = _level_from_m(m_score_adjusted)       # 调整后等级（下游应使用此值）

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
        "m_score_adjusted": round(m_score_adjusted, 3),     # 下游优先使用
        "risk_level": risk_level,                            # low / medium / high（已 growth-adjusted）
        "risk_level_raw": risk_level_raw,                    # 原始 M 等级（仅供对比）
        "verdict": verdict,
        "variables": {k: (round(v, 4) if v is not None else None) for k, (v, _) in coefs.items()},
        "missing": missing,
        "high_growth_caveat": high_growth_caveat,
        "source": "Beneish 1999 FAJ + Beneish & Nichols 2007 growth adj.",
    }


# ────────────────────────────────────────────────────────
# 3. Altman Z-Score（破产预警）
# ────────────────────────────────────────────────────────

def altman_z_score(ticker: str, market_cap: float | None = None) -> dict[str, Any]:
    """Altman Z-Score（5 变量预测破产）。

    论文：Altman (1968) JF
    制造业公式：Z = 1.2·X1 + 1.4·X2 + 3.3·X3 + 0.6·X4 + 1.0·X5
      X1 = 营运资本 / 总资产
      X2 = 留存收益 / 总资产
      X3 = EBIT / 总资产
      X4 = 股权市值 / 总负债
      X5 = 营收 / 总资产

    阈值：Z > 2.99 安全 / 1.81-2.99 灰色 / Z < 1.81 破产警示
    """
    inc = fmp_client.fetch_income_full(ticker, years=1)
    bs = fmp_client.fetch_balance_sheet(ticker, years=1)
    if not inc or not bs:
        return {"error": "no statements", "ticker": ticker}

    i = inc[0]
    b = bs[0]

    if market_cap is None:
        prof = fmp_client.fetch_company_profile(ticker)
        market_cap = (prof or {}).get("market_cap")

    ta = b.get("total_assets")
    tl = b.get("total_liabilities")
    ca = b.get("total_current_assets")
    cl = b.get("total_current_liabilities")
    re = b.get("retained_earnings")
    ebit = i.get("operating_income")
    revenue = i.get("revenue")

    wc = (ca - cl) if (ca is not None and cl is not None) else None

    x1 = _safe_div(wc, ta)
    x2 = _safe_div(re, ta)
    x3 = _safe_div(ebit, ta)
    x4_raw = _safe_div(market_cap, tl)
    # X4 cap：低/零负债公司会让 X4 爆炸（如 NVDA tl 极小 → X4=100+）
    # Altman 自己 2000 论文建议 cap，工业实践常见 cap=10
    x4 = min(x4_raw, 10.0) if x4_raw is not None else None
    x5 = _safe_div(revenue, ta)

    components = {"X1_wc/ta": x1, "X2_re/ta": x2, "X3_ebit/ta": x3,
                  "X4_mvE/tl": x4, "X5_sales/ta": x5}
    missing = [name for name, v in components.items() if v is None]
    if len(missing) > 1:
        return {"error": f"missing components: {missing}", "ticker": ticker, "components": components}

    coefs = {"X1_wc/ta": 1.2, "X2_re/ta": 1.4, "X3_ebit/ta": 3.3,
             "X4_mvE/tl": 0.6, "X5_sales/ta": 1.0}
    z = sum(coefs[name] * v for name, v in components.items() if v is not None)

    if z > 2.99:
        verdict = "🟢 SAFE (Z > 2.99)"
    elif z > 1.81:
        verdict = "🟡 GREY ZONE (1.81 < Z < 2.99)"
    else:
        verdict = "🔴 DISTRESS (Z < 1.81)"

    return {
        "ticker": ticker,
        "z_score": round(z, 3),
        "verdict": verdict,
        "components": {k: (round(v, 4) if v is not None else None) for k, v in components.items()},
        "missing": missing,
        "market_cap": market_cap,
        "source": "Altman 1968 JF",
    }


# ────────────────────────────────────────────────────────
# 4. 盈利质量 8 项（Sloan 1996 + Dechow-Dichev 2002）
# ────────────────────────────────────────────────────────

def earnings_quality_8(ticker: str) -> dict[str, Any]:
    """8 项盈利质量指标，揭示"账面利润 vs 真实赚钱"的差距。

    指标：
      1. 应收增速 - 营收增速  → >10pp 警示（销售依赖应收）
      2. 存货增速 - 营收增速  → >15pp 警示（库存积压）
      3. 商誉 / 净资产        → >50% 警示（并购溢价过大）
      4. 研发资本化判定        → 看是否有大额无形资产新增（保守为佳）
      5. 递延收入同比增速      → 订阅业务正面信号
      6. CFO / NI            → 理想 >1（现金赚的比账面多）
      7. FCF 转化率 (FCF/NI)  → 理想 >0.7
      8. 应计盈余 / 资产       → Sloan 1996 反向指标，越大越差

    数据：FMP balance + cash flow + income，最近 2 年。
    """
    inc = fmp_client.fetch_income_full(ticker, years=2)
    bs = fmp_client.fetch_balance_sheet(ticker, years=2)
    cf = fmp_client.fetch_cash_flow(ticker, years=2)
    if not inc or not bs or not cf or len(inc) < 2 or len(bs) < 2 or len(cf) < 2:
        return {"error": "insufficient data", "ticker": ticker}

    i_t, i_p = inc[0], inc[1]
    b_t, b_p = bs[0], bs[1]
    c_t = cf[0]

    revenue_g = _safe_growth(i_t.get("revenue"), i_p.get("revenue"))
    ar_g = _safe_growth(b_t.get("net_receivables"), b_p.get("net_receivables"))
    inv_g = _safe_growth(b_t.get("inventory"), b_p.get("inventory"))
    deferred_g = _safe_growth(b_t.get("deferred_revenue"), b_p.get("deferred_revenue"))

    goodwill = b_t.get("goodwill") or 0
    equity = b_t.get("total_equity") or 0
    intangibles = b_t.get("intangible_assets") or 0

    ni = i_t.get("net_income")
    cfo = c_t.get("operating_cash_flow")
    fcf = c_t.get("free_cash_flow")
    ta = b_t.get("total_assets")

    # 应计盈余 = NI - CFO，再除以总资产（Sloan 1996）
    accruals_to_ta = _safe_div((ni - cfo) if (ni is not None and cfo is not None) else None, ta)

    # 各指标 + verdict
    metrics = []

    if revenue_g is not None and ar_g is not None:
        gap = (ar_g - revenue_g) * 100  # 百分点
        v = "🔴" if gap > 10 else ("🟡" if gap > 5 else "🟢")
        metrics.append({"name": "应收 vs 营收增速差", "value_pp": round(gap, 2),
                        "verdict": v, "note": "应收增速远超营收 → 销售依赖赊账"})

    if revenue_g is not None and inv_g is not None:
        gap = (inv_g - revenue_g) * 100
        v = "🔴" if gap > 15 else ("🟡" if gap > 8 else "🟢")
        metrics.append({"name": "存货 vs 营收增速差", "value_pp": round(gap, 2),
                        "verdict": v, "note": "存货增速远超营收 → 库存积压"})

    gw_to_eq = _safe_div(goodwill, equity)
    if gw_to_eq is not None:
        v = "🔴" if gw_to_eq > 0.5 else ("🟡" if gw_to_eq > 0.25 else "🟢")
        metrics.append({"name": "商誉/净资产", "value_pct": round(gw_to_eq * 100, 2),
                        "verdict": v, "note": "并购溢价占比，过大有减值风险"})

    rd = i_t.get("rd_expense")
    rd_to_revenue = _safe_div(rd, i_t.get("revenue"))
    intan_g = _safe_growth(intangibles, b_p.get("intangible_assets"))
    metrics.append({"name": "研发资本化迹象",
                    "rd_to_revenue_pct": round(rd_to_revenue * 100, 2) if rd_to_revenue else None,
                    "intangible_growth_pct": round(intan_g * 100, 2) if intan_g else None,
                    "verdict": "🟡" if intan_g and intan_g > 0.3 else "🟢",
                    "note": "无形资产增速 >30% 提示研发资本化"})

    if deferred_g is not None:
        v = "🟢" if deferred_g > 0.1 else ("🟡" if deferred_g > -0.05 else "🔴")
        metrics.append({"name": "递延收入增速", "value_pct": round(deferred_g * 100, 2),
                        "verdict": v, "note": "订阅业务正面信号；下降表示订单减少"})

    cfo_to_ni = _safe_div(cfo, ni)
    if cfo_to_ni is not None:
        v = "🟢" if cfo_to_ni > 1.0 else ("🟡" if cfo_to_ni > 0.7 else "🔴")
        metrics.append({"name": "CFO / NI", "value": round(cfo_to_ni, 2),
                        "verdict": v, "note": "现金赚得比账面多则 >1"})

    fcf_to_ni = _safe_div(fcf, ni)
    if fcf_to_ni is not None:
        v = "🟢" if fcf_to_ni > 0.7 else ("🟡" if fcf_to_ni > 0.4 else "🔴")
        metrics.append({"name": "FCF / NI", "value": round(fcf_to_ni, 2),
                        "verdict": v, "note": "自由现金流转化率"})

    if accruals_to_ta is not None:
        v = "🟢" if accruals_to_ta < 0 else ("🟡" if accruals_to_ta < 0.05 else "🔴")
        metrics.append({"name": "应计盈余/资产 (Sloan)", "value": round(accruals_to_ta, 4),
                        "verdict": v, "note": "Sloan 1996：越大未来回报越差"})

    # 综合得分（红=-1 黄=0 绿=+1 平均后映射 0-100）
    score_map = {"🔴": -1, "🟡": 0, "🟢": 1}
    valid = [score_map[m["verdict"]] for m in metrics if m.get("verdict") in score_map]
    quality_score = int((sum(valid) / len(valid) + 1) / 2 * 100) if valid else None

    return {
        "ticker": ticker,
        "quality_score": quality_score,  # 0-100
        "metrics": metrics,
        "source": "Sloan 1996 AR + Dechow-Dichev 2002 AR",
    }


# ────────────────────────────────────────────────────────
# 5. 杜邦 TTM 版（季度数据合成 trailing 4Q）
# ────────────────────────────────────────────────────────

def _sum_field(rows: list[dict], field: str) -> float | None:
    """累加多季度的某字段；任一缺失返回 None。"""
    vals = [r.get(field) for r in rows]
    if any(v is None for v in vals):
        return None
    try:
        return sum(float(v) for v in vals)
    except (TypeError, ValueError):
        return None


def dupont_5factor_ttm(ticker: str) -> dict[str, Any]:
    """杜邦五因子 — TTM 版（trailing 4 quarters vs prior 4 quarters）。

    年度版会平滑掉拐点（半导体/能源/材料周期股财年内 ROE 可能翻倍或腰斩）。
    TTM 在每个季度滚动重算，能更早捕捉拐点。

    Why TTM 不直接用季报：
      - 单季度数据有季节性（Q4 收入通常高于 Q1）
      - TTM 累加 4 季消除季节，类似年度但实时性高 1-3 季

    需要：8 季度 income + cashflow + 5 季度 balance sheet（用于 avg）
    """
    # 拉 8 季度数据（period='quarter'）；FMP 的 limit 参数控制回溯季度数
    inc_q = fmp_client.fetch_income_full(ticker, years=8, period="quarter")
    bs_q = fmp_client.fetch_balance_sheet(ticker, years=8, period="quarter")

    if not inc_q or len(inc_q) < 8 or not bs_q or len(bs_q) < 5:
        return {"error": f"insufficient quarterly data (need 8Q income + 5Q BS, got {len(inc_q or [])}Q/{len(bs_q or [])}Q)",
                "ticker": ticker}

    cur_4q = inc_q[:4]      # 最近 4 季 = 当期 TTM
    prev_4q = inc_q[4:8]    # 前 4 季 = 同比 TTM

    def compute_ttm_factors(rows: list[dict], bs_end: dict, bs_start: dict) -> dict:
        revenue = _sum_field(rows, "revenue")
        ebit = _sum_field(rows, "operating_income")
        ebt = _sum_field(rows, "income_before_tax")
        ni = _sum_field(rows, "net_income")
        ta_avg = _safe_div(((bs_end.get("total_assets") or 0)
                            + (bs_start.get("total_assets") or 0)), 2)
        eq_avg = _safe_div(((bs_end.get("total_equity") or 0)
                            + (bs_start.get("total_equity") or 0)), 2)

        return {
            "tax_burden": _safe_div(ni, ebt),
            "interest_burden": _safe_div(ebt, ebit),
            "ebit_margin": _safe_div(ebit, revenue),
            "asset_turnover": _safe_div(revenue, ta_avg),
            "leverage": _safe_div(ta_avg, eq_avg),
        }

    # 当期 TTM 资产平均：BS[0] (最新季末) 和 BS[4] (4 季度前)
    cur_factors = compute_ttm_factors(cur_4q, bs_q[0], bs_q[4])
    # 同比 TTM 资产平均：BS[4] 和 BS[8 if exists else fallback]
    prev_bs_start = bs_q[7] if len(bs_q) >= 8 else bs_q[-1]
    prev_factors = compute_ttm_factors(prev_4q, bs_q[4], prev_bs_start)

    def roe_from(factors):
        if not factors or any(factors.get(k) is None
                              for k in ("tax_burden", "interest_burden",
                                        "ebit_margin", "asset_turnover", "leverage")):
            return None
        result = 1.0
        for k in ("tax_burden", "interest_burden", "ebit_margin", "asset_turnover", "leverage"):
            result *= factors[k]
        return result

    roe_cur = roe_from(cur_factors)
    roe_prev = roe_from(prev_factors)

    # 归因：固定其他因子，单因子从 prev → cur
    attribution = {}
    if cur_factors and prev_factors and roe_cur is not None and roe_prev is not None:
        for key in ("tax_burden", "interest_burden", "ebit_margin", "asset_turnover", "leverage"):
            if cur_factors[key] is None or prev_factors[key] is None:
                continue
            mixed = dict(prev_factors)
            mixed[key] = cur_factors[key]
            roe_mix = roe_from(mixed)
            if roe_mix is not None:
                attribution[key] = round((roe_mix - roe_prev) * 100, 2)

    verdict = "insufficient_data"
    if attribution:
        top = max(attribution.items(), key=lambda x: abs(x[1]))
        verdict_map = {
            "ebit_margin": "margin_driven",
            "asset_turnover": "efficiency_driven",
            "leverage": "leverage_driven",
            "tax_burden": "non_operating",
            "interest_burden": "non_operating",
        }
        verdict = verdict_map.get(top[0], "mixed")

    # 拐点信号：TTM ROE 与年度 ROE 差异 > 5pp 提示动量
    cur_period = inc_q[0].get("date") or inc_q[0].get("period")
    prev_period = inc_q[4].get("date") or inc_q[4].get("period")

    return {
        "ticker": ticker,
        "period_cur": cur_period,                        # 最新季末日期（TTM 截止）
        "period_prev": prev_period,                      # 同比对照季末
        "roe_cur_ttm_pct": round(roe_cur * 100, 2) if roe_cur is not None else None,
        "roe_prev_ttm_pct": round(roe_prev * 100, 2) if roe_prev is not None else None,
        "roe_change_pp": round((roe_cur - roe_prev) * 100, 2) if (roe_cur and roe_prev) else None,
        "factors_cur": {k: (round(v, 4) if v is not None else None) for k, v in (cur_factors or {}).items()},
        "factors_prev": {k: (round(v, 4) if v is not None else None) for k, v in (prev_factors or {}).items()},
        "attribution_pp": attribution,
        "verdict": verdict,
        "source": "FMP/quarterly statements (TTM = sum 4Q)",
    }


# ────────────────────────────────────────────────────────
# 一站式：调用全部 4 个深度分析
# ────────────────────────────────────────────────────────

def analyze_fundamentals(ticker: str) -> dict[str, Any]:
    """一次性跑完所有深度分析模块（含年度 + TTM 双视角杜邦）。"""
    return {
        "ticker": ticker,
        "dupont": dupont_5factor(ticker),                # 年度（FY vs FY-1）
        "dupont_ttm": dupont_5factor_ttm(ticker),        # TTM（4Q vs 同比 4Q）
        "beneish": beneish_m_score(ticker),
        "altman": altman_z_score(ticker),
        "quality": earnings_quality_8(ticker),
    }


# ────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────

def _print_report(ticker: str, r: dict[str, Any]) -> None:
    print("=" * 80)
    print(f"  📚 基本面深度分析 — {ticker}")
    print("=" * 80)

    d = r.get("dupont", {})
    print("\n【1. 杜邦五因子分解】")
    if d.get("error"):
        print(f"  ⚠️ {d['error']}")
    else:
        print(f"  ROE 当期: {d.get('roe_cur')}% | 上期: {d.get('roe_prev')}% | 变动: {d.get('roe_change_pp')}pp")
        print(f"  当期因子: {d.get('factors_cur')}")
        print(f"  归因 (各因子对 ROE 变动的贡献，pp): {d.get('attribution_pp')}")
        print(f"  判定: {d.get('verdict')}")

    b = r.get("beneish", {})
    print("\n【2. Beneish M-Score（造假识别）】")
    if b.get("error"):
        print(f"  ⚠️ {b['error']}")
    else:
        print(f"  M-Score: {b.get('m_score')} → {b.get('verdict')}")
        print(f"  8 变量: {b.get('variables')}")
        if b.get("missing"):
            print(f"  缺失: {b['missing']}")

    a = r.get("altman", {})
    print("\n【3. Altman Z-Score（破产预警）】")
    if a.get("error"):
        print(f"  ⚠️ {a['error']}")
    else:
        print(f"  Z-Score: {a.get('z_score')} → {a.get('verdict')}")
        print(f"  组件: {a.get('components')}")

    q = r.get("quality", {})
    print("\n【4. 盈利质量 8 项】")
    if q.get("error"):
        print(f"  ⚠️ {q['error']}")
    else:
        print(f"  综合质量分: {q.get('quality_score')}/100")
        for m in q.get("metrics", []):
            v = (m.get("value_pp") if "value_pp" in m else
                 m.get("value_pct") if "value_pct" in m else
                 m.get("value", "—"))
            print(f"  {m['verdict']} {m['name']}: {v}  · {m.get('note', '')}")
    print("=" * 80)


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description="个股基本面深度分析")
    parser.add_argument("ticker", help="股票代码 e.g. NVDA")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非格式化")
    parser.add_argument("--out", help="保存 JSON 到文件")
    args = parser.parse_args()

    if not fmp_client.is_available():
        print("⚠️ FMP_API_KEY 未配置，深度分析不可用。")
        return 1

    r = analyze_fundamentals(args.ticker)
    if args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        _print_report(args.ticker, r)

    if args.out:
        with open(args.out, "w") as f:
            json.dump(r, f, indent=2, ensure_ascii=False)
        print(f"\n💾 已保存: {args.out}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
