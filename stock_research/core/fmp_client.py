"""Financial Modeling Prep (FMP) API wrapper：财报 + DCF 估值 + 分析师预期 + 13F 历史。

凭证：FMP_API_KEY 环境变量；缺 key 时所有方法返回 None（graceful degrade）。
免费层：250 calls/day，对个人足够。

为什么用 FMP 而非纯 yfinance：
  - yfinance 财务数据时不时缺失 / NaN / 滞后
  - FMP 提供官方 DCF 估值（这点 yfinance 没有）
  - FMP 财务报表回溯 10 年（yfinance 一般 4 年）
  - 分析师一致预期（EPS/Revenue forecast）质量更高
"""
from __future__ import annotations
import logging
import time
from datetime import datetime
from typing import Any

import requests

from .. import config
from ..adapters import fmp_cache

logger = logging.getLogger(__name__)

FMP_BASE = "https://financialmodelingprep.com/stable"  # 2025-08 后新用户用 /stable，老 /v3 已 deprecated

# 测试时可手动覆盖；实际从 config 读
FMP_API_KEY = config.__dict__.get("FMP_API_KEY") or __import__("os").environ.get("FMP_API_KEY")


def is_available() -> bool:
    return bool(FMP_API_KEY)


def _get(path: str, params: dict | None = None) -> Any:
    """带 24h 文件缓存 + 速率友好的 FMP HTTP 请求。Key 自动附加。

    缓存命中直接返回；未命中走 HTTP，成功响应自动写缓存。429/null 不缓存。
    清缓存：python3 -m stock_research.adapters.fmp_cache clear
    """
    cached = fmp_cache.get(path, params)
    if cached is not None:
        logger.debug("FMP cache HIT: %s %s", path, params)
        return cached

    if not FMP_API_KEY:
        return None
    p = dict(params or {})
    p["apikey"] = FMP_API_KEY
    try:
        r = requests.get(f"{FMP_BASE}{path}", params=p, timeout=20)
        if r.status_code != 200:
            logger.warning("FMP %s -> %d: %s", path, r.status_code, r.text[:200])
            return None
        data = r.json()
        # FMP 错误时返回 {"Error Message": "..."}
        if isinstance(data, dict) and "Error Message" in data:
            logger.debug("FMP error for %s: %s", path, data.get("Error Message"))
            return None
        fmp_cache.save(path, params, data)
        return data
    except Exception as e:
        logger.warning("FMP %s failed: %s", path, e)
        return None


# ────────────────────────────────────────────────────────
# DCF 内在价值（FMP 杀手级功能）
# ────────────────────────────────────────────────────────

def fetch_dcf(ticker: str) -> dict[str, Any] | None:
    """FMP 计算的 DCF 内在价值 vs 当前股价。"""
    raw = _get("/discounted-cash-flow", {"symbol": ticker})
    if not raw or not isinstance(raw, list) or not raw:
        return None
    row = raw[0]
    dcf = row.get("dcf")
    price = row.get("Stock Price") or row.get("stockPrice")
    if dcf is None or price is None:
        return None
    upside = (dcf - price) / price * 100 if price > 0 else None
    return {
        "ticker": ticker,
        "dcf_intrinsic_value": round(float(dcf), 2),
        "current_price": round(float(price), 2),
        "upside_pct": round(upside, 2) if upside is not None else None,
        "verdict": _dcf_verdict(upside),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": "FMP/discounted-cash-flow",
    }


def _dcf_verdict(upside: float | None) -> str:
    if upside is None:
        return "?"
    if upside > 30:
        return "🟢 严重低估（DCF 高于股价 >30%）"
    if upside > 10:
        return "🟢 低估（DCF 高于股价 10-30%）"
    if upside > -10:
        return "🟡 合理估值（DCF ±10% 内）"
    if upside > -30:
        return "🔴 高估（股价高于 DCF 10-30%）"
    return "🔴 严重高估（股价高于 DCF >30%）"


# ────────────────────────────────────────────────────────
# 财务报表（10 年历史，比 yfinance 全）
# ────────────────────────────────────────────────────────

def fetch_income_statement(ticker: str, years: int = 5) -> list[dict[str, Any]] | None:
    raw = _get("/income-statement", {"symbol": ticker, "limit": years})
    if not raw or not isinstance(raw, list):
        return None
    out = []
    for r in raw:
        out.append({
            "date": r.get("date"),
            "fiscal_year": r.get("fiscalYear"),
            "revenue": r.get("revenue"),
            "gross_profit": r.get("grossProfit"),
            "operating_income": r.get("operatingIncome"),
            "net_income": r.get("netIncome"),
            "eps": r.get("eps"),
            "eps_diluted": r.get("epsdiluted") or r.get("epsDiluted"),
            "gross_margin": (r.get("grossProfit") / r.get("revenue")) if r.get("revenue") else None,
            "net_margin": (r.get("netIncome") / r.get("revenue")) if r.get("revenue") else None,
        })
    return out


def fetch_balance_sheet(ticker: str, years: int = 5, period: str = "annual") -> list[dict[str, Any]] | None:
    """资产负债表（最近 N 年，新→旧）。period='quarter' 切季度。"""
    params = {"symbol": ticker, "limit": years}
    if period == "quarter":
        params["period"] = "quarter"
    raw = _get("/balance-sheet-statement", params)
    if not raw or not isinstance(raw, list):
        return None
    out = []
    for r in raw:
        out.append({
            "date": r.get("date"),
            "fiscal_year": r.get("fiscalYear"),
            "total_assets": r.get("totalAssets"),
            "total_current_assets": r.get("totalCurrentAssets"),
            "total_liabilities": r.get("totalLiabilities"),
            "total_current_liabilities": r.get("totalCurrentLiabilities"),
            "total_equity": r.get("totalStockholdersEquity") or r.get("totalEquity"),
            "long_term_debt": r.get("longTermDebt"),
            "short_term_debt": r.get("shortTermDebt"),
            "cash_and_equivalents": r.get("cashAndCashEquivalents"),
            "net_receivables": r.get("netReceivables"),
            "inventory": r.get("inventory"),
            "goodwill": r.get("goodwill"),
            "intangible_assets": r.get("intangibleAssets"),
            "ppe": r.get("propertyPlantEquipmentNet"),
            "retained_earnings": r.get("retainedEarnings"),
            "deferred_revenue": r.get("deferredRevenue") or r.get("deferredRevenueNonCurrent"),
            "shares_outstanding": r.get("commonStock"),
        })
    return out


def fetch_cash_flow(ticker: str, years: int = 5, period: str = "annual") -> list[dict[str, Any]] | None:
    """现金流量表（最近 N 年，新→旧）。period='quarter' 切季度。"""
    params = {"symbol": ticker, "limit": years}
    if period == "quarter":
        params["period"] = "quarter"
    raw = _get("/cash-flow-statement", params)
    if not raw or not isinstance(raw, list):
        return None
    out = []
    for r in raw:
        out.append({
            "date": r.get("date"),
            "fiscal_year": r.get("fiscalYear"),
            "operating_cash_flow": r.get("operatingCashFlow") or r.get("netCashProvidedByOperatingActivities"),
            "capex": r.get("capitalExpenditure"),
            "free_cash_flow": r.get("freeCashFlow"),
            "depreciation_amortization": r.get("depreciationAndAmortization"),
            "stock_based_compensation": r.get("stockBasedCompensation"),
            "change_in_working_capital": r.get("changeInWorkingCapital"),
            "dividends_paid": r.get("dividendsPaid"),
            "stock_repurchased": r.get("commonStockRepurchased"),
            "stock_issued": r.get("commonStockIssued"),
        })
    return out


def fetch_income_full(ticker: str, years: int = 5, period: str = "annual") -> list[dict[str, Any]] | None:
    """完整版利润表（含 EBIT/EBITDA/SG&A/R&D/利息/税）。比 fetch_income_statement 更细。

    period='annual' → 年报；period='quarter' → 季报（FY trend 用）。
    """
    params = {"symbol": ticker, "limit": years}
    if period == "quarter":
        params["period"] = "quarter"
    raw = _get("/income-statement", params)
    if not raw or not isinstance(raw, list):
        return None
    out = []
    for r in raw:
        out.append({
            "date": r.get("date"),
            "fiscal_year": r.get("fiscalYear"),
            "revenue": r.get("revenue"),
            "cost_of_revenue": r.get("costOfRevenue"),
            "gross_profit": r.get("grossProfit"),
            "rd_expense": r.get("researchAndDevelopmentExpenses"),
            "sga_expense": r.get("sellingGeneralAndAdministrativeExpenses") or r.get("generalAndAdministrativeExpenses"),
            "operating_expenses": r.get("operatingExpenses"),
            "operating_income": r.get("operatingIncome"),
            "ebitda": r.get("ebitda"),
            "ebit": r.get("operatingIncome"),  # FMP 用 operating income 作 EBIT
            "interest_expense": r.get("interestExpense"),
            "income_before_tax": r.get("incomeBeforeTax"),
            "income_tax_expense": r.get("incomeTaxExpense"),
            "net_income": r.get("netIncome"),
            "eps": r.get("eps"),
            "eps_diluted": r.get("epsdiluted") or r.get("epsDiluted"),
            "weighted_average_shares": r.get("weightedAverageShsOut"),
            "weighted_average_shares_diluted": r.get("weightedAverageShsOutDil"),
        })
    return out


def fetch_company_profile(ticker: str) -> dict[str, Any] | None:
    """公司基本信息（行业 / 国家 / 描述 / CEO / 员工 / 上市日 / SIC / Sector / Industry）。"""
    raw = _get("/profile", {"symbol": ticker})
    if not raw or not isinstance(raw, list) or not raw:
        return None
    r = raw[0]
    return {
        "ticker": ticker,
        "company_name": r.get("companyName"),
        "sector": r.get("sector"),
        "industry": r.get("industry"),
        "country": r.get("country"),
        "exchange": r.get("exchangeFullName") or r.get("exchange"),
        "ceo": r.get("ceo"),
        "employees": r.get("fullTimeEmployees"),
        "ipo_date": r.get("ipoDate"),
        "description": r.get("description"),
        "website": r.get("website"),
        "market_cap": r.get("marketCap"),
        "is_etf": r.get("isEtf"),
        "is_actively_trading": r.get("isActivelyTrading"),
    }


def fetch_peers(ticker: str) -> list[str] | None:
    """同业列表（FMP 自动按 sector/industry/marketcap 推荐）。"""
    raw = _get("/stock-peers", {"symbol": ticker})
    if not raw or not isinstance(raw, list):
        return None
    # FMP 返回格式: [{"symbol":"AAPL","peersList":["MSFT","GOOG",...]}] 或直接 list of {"symbol":"..."}
    if raw and isinstance(raw[0], dict) and "peersList" in raw[0]:
        return raw[0].get("peersList") or []
    return [r.get("symbol") for r in raw if isinstance(r, dict) and r.get("symbol")]


def fetch_key_metrics(ticker: str) -> dict[str, Any] | None:
    """关键估值指标（最新一期）。"""
    raw = _get("/key-metrics-ttm", {"symbol": ticker})
    if not raw or not isinstance(raw, list) or not raw:
        return None
    r = raw[0]
    return {
        "ticker": ticker,
        "market_cap": r.get("marketCap"),
        "enterprise_value": r.get("enterpriseValueTTM"),
        "ev_to_sales_ttm": r.get("evToSalesTTM"),
        "ev_to_ebitda_ttm": r.get("evToEBITDATTM"),
        "ev_to_fcf_ttm": r.get("evToFreeCashFlowTTM"),
        "current_ratio_ttm": r.get("currentRatioTTM"),
        "net_debt_to_ebitda_ttm": r.get("netDebtToEBITDATTM"),
        "income_quality_ttm": r.get("incomeQualityTTM"),
        "graham_number": r.get("grahamNumberTTM"),
        "source": "FMP/key-metrics-ttm",
    }


# ────────────────────────────────────────────────────────
# 分析师一致预期（FMP 比 Finnhub 全）
# ────────────────────────────────────────────────────────

def fetch_analyst_estimates(ticker: str) -> dict[str, Any] | None:
    """未来 4 年的 Revenue / EPS 一致预期。"""
    raw = _get("/analyst-estimates", {"symbol": ticker, "period": "annual", "limit": 4})
    if not raw or not isinstance(raw, list):
        return None
    out = []
    for r in raw:
        out.append({
            "date": r.get("date"),
            "revenue_avg": r.get("revenueAvg") or r.get("estimatedRevenueAvg"),
            "revenue_high": r.get("revenueHigh") or r.get("estimatedRevenueHigh"),
            "revenue_low": r.get("revenueLow") or r.get("estimatedRevenueLow"),
            "eps_avg": r.get("epsAvg") or r.get("estimatedEpsAvg"),
            "eps_high": r.get("epsHigh") or r.get("estimatedEpsHigh"),
            "eps_low": r.get("epsLow") or r.get("estimatedEpsLow"),
            "analysts_revenue": r.get("numAnalystsRevenue") or r.get("numberAnalystEstimatedRevenue"),
            "analysts_eps": r.get("numAnalystsEps") or r.get("numberAnalystsEstimatedEps"),
        })
    return {"ticker": ticker, "estimates": out, "source": "FMP/analyst-estimates"}


# ────────────────────────────────────────────────────────
# 财报日历
# ────────────────────────────────────────────────────────

def fetch_earnings_calendar(ticker: str) -> list[dict[str, Any]] | None:
    """已发布 + 未来财报日历。"""
    # 免费层 limit 上限是 5；超过会 402
    raw = _get("/earnings", {"symbol": ticker, "limit": 5})
    if not raw or not isinstance(raw, list):
        return None
    out = []
    for r in raw:
        eps_act = r.get("epsActual") or r.get("eps")
        eps_est = r.get("epsEstimated")
        out.append({
            "date": r.get("date"),
            "eps_actual": eps_act,
            "eps_estimated": eps_est,
            "revenue_actual": r.get("revenueActual") or r.get("revenue"),
            "revenue_estimated": r.get("revenueEstimated"),
            "surprise": (eps_act - eps_est) if (eps_act and eps_est) else None,
        })
    return out


# ────────────────────────────────────────────────────────
# 一站式：对一只股票拉所有 FMP 能给的
# ────────────────────────────────────────────────────────

def fetch_enriched(ticker: str, sleep_sec: float = 0.3) -> dict[str, Any]:
    out: dict[str, Any] = {"ticker": ticker, "fmp": {}}
    if not is_available():
        out["fmp"]["disabled"] = "FMP_API_KEY not set"
        return out

    dcf = fetch_dcf(ticker)
    if dcf:
        out["fmp"]["dcf"] = dcf
    time.sleep(sleep_sec)

    metrics = fetch_key_metrics(ticker)
    if metrics:
        out["fmp"]["key_metrics"] = metrics
    time.sleep(sleep_sec)

    income = fetch_income_statement(ticker, years=5)
    if income:
        out["fmp"]["income_5y"] = income
    time.sleep(sleep_sec)

    estimates = fetch_analyst_estimates(ticker)
    if estimates:
        out["fmp"]["analyst_estimates"] = estimates
    time.sleep(sleep_sec)

    earnings = fetch_earnings_calendar(ticker)
    if earnings:
        out["fmp"]["earnings_history"] = earnings

    return out
