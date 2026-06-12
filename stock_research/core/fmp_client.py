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
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from .. import config
from ..adapters import fmp_cache

logger = logging.getLogger(__name__)

FMP_BASE = "https://financialmodelingprep.com/stable"  # 2025-08 后新用户用 /stable，老 /v3 已 deprecated

# 测试时可手动覆盖；实际从 config 读
FMP_API_KEY = config.__dict__.get("FMP_API_KEY") or __import__("os").environ.get("FMP_API_KEY")

# 进程级强制刷新开关：FMP_FORCE_REFRESH=1 时整次运行绕过 24h 缓存（财报日 / debug 用）
_FORCE_REFRESH_ENV = __import__("os").environ.get("FMP_FORCE_REFRESH", "").lower() in ("1", "true", "yes", "on")
_FMP_DISABLED_REASON: str | None = None
_FMP_EVENTS: list[dict[str, Any]] = []

# 请求节流 + 429 退避（2026-06-11 加）：批量抓价连发上百次会撞 FMP per-minute 限流，
#   旧逻辑一次 429 就全局禁用 → 全量只 12/133 走 FMP、其余退 yfinance。节流拉开请求间隔
#   把命中率拉满，429 时指数退避重试而非立刻放弃；可用环境变量调：
#   FMP_MIN_INTERVAL_SEC（请求最小间隔）/ FMP_MAX_ATTEMPTS（重试次数）/ FMP_RETRY_BACKOFF_SEC（退避基数）
_FMP_MIN_INTERVAL = float(os.environ.get("FMP_MIN_INTERVAL_SEC", "0.45"))
_FMP_MAX_ATTEMPTS = int(os.environ.get("FMP_MAX_ATTEMPTS", "3"))
_FMP_RETRY_BACKOFF = float(os.environ.get("FMP_RETRY_BACKOFF_SEC", "2.0"))
_FMP_LAST_REQUEST_AT = 0.0
_FMP_THROTTLE_LOCK = threading.Lock()


def _throttle() -> None:
    """保证两次 FMP HTTP 请求之间至少隔 _FMP_MIN_INTERVAL 秒（避免连发触发限流）。"""
    global _FMP_LAST_REQUEST_AT
    with _FMP_THROTTLE_LOCK:
        wait = _FMP_MIN_INTERVAL - (time.monotonic() - _FMP_LAST_REQUEST_AT)
        if wait > 0:
            time.sleep(wait)
        _FMP_LAST_REQUEST_AT = time.monotonic()


def _redact(text: str) -> str:
    """Keep API keys out of logs, including URLs embedded in exceptions."""
    return re.sub(r"(apikey=)[^&\s)]+", r"\1***", text)


def _record_event(level: str, reason: str, path: str, detail: str | None = None) -> None:
    _FMP_EVENTS.append({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "level": level,
        "reason": reason,
        "path": path,
        "detail": _redact(detail or "")[:300],
    })


def source_health_snapshot(*, pipeline: str = "v2_us") -> dict[str, Any]:
    status = "ok"
    reason = None
    if _FMP_DISABLED_REASON:
        status = "degraded"
        reason = _FMP_DISABLED_REASON
    elif any(e.get("level") in {"ERROR", "WARN"} for e in _FMP_EVENTS):
        status = "degraded"
        reason = "recent_request_errors"
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pipeline": pipeline,
        "sources": {
            "FMP": {
                "status": status,
                "reason": reason,
                "last_event": _FMP_EVENTS[-1] if _FMP_EVENTS else None,
                "events": _FMP_EVENTS[-20:],
                "affected_fields": [
                    "Altman Z-Score",
                    "Beneish M-Score",
                    "财务造假/破产软红旗",
                    "DCF 深度估值",
                ],
                "unaffected_fields": [
                    "价格",
                    "Piotroski F-Score",
                    "12-1 月动量",
                    "1 月反转",
                    "分析师上修",
                    "主推荐排序",
                ],
                "impact": (
                    "FMP 降级时，美股主因子仍可运行；Z/M-Score 与部分深度基本面红旗会显示为空。"
                ),
                "operator_action": (
                    "等待 FMP 免费额度恢复，或升级 FMP 套餐；不需要因此重跑价格数据。"
                ),
            }
        },
    }


def write_source_health(*, pipeline: str = "v2_us", path: str | Path | None = None) -> dict[str, Any]:
    """Persist source health so dashboard/brief can show data degradation."""
    payload = source_health_snapshot(pipeline=pipeline)
    out = Path(path) if path is not None else config.DATA_DIR / "latest" / "source_health.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(__import__("json").dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def is_available() -> bool:
    return bool(FMP_API_KEY)


def reset_throttle_disable() -> None:
    """复位 402/429 软熔断。长回填类调用方冷却(≥60s)后调用,继续慢速拉取。

    软熔断本意是保护"一批内别继续撞墙";一次性回填脚本自己控制节奏时,
    冷却后复位是安全的(实测 402 是短窗限流,会自行恢复)。
    """
    global _FMP_DISABLED_REASON
    _FMP_DISABLED_REASON = None


def _get(path: str, params: dict | None = None, force_refresh: bool = False) -> Any:
    """带 24h 文件缓存 + 速率友好的 FMP HTTP 请求。Key 自动附加。

    缓存命中直接返回；未命中或 force_refresh=True 走 HTTP，成功响应仍写缓存。
    429/null 不缓存。

    强制刷新两种方式（财报日想绕开 24h 缓存时）：
      - 单次调用：fmp_client.fetch_xxx(..., force_refresh=True)（如果该 fetch_ 函数支持透传）
      - 全局：FMP_FORCE_REFRESH=1 python3 ... 命令行 export
    清缓存：python3 -m stock_research.adapters.fmp_cache clear
    """
    effective_force = force_refresh or _FORCE_REFRESH_ENV
    cached = fmp_cache.get(path, params, force_refresh=effective_force)
    if cached is not None:
        logger.debug("FMP cache HIT: %s %s", path, params)
        return cached

    global _FMP_DISABLED_REASON
    if _FMP_DISABLED_REASON:
        logger.debug("FMP skipped for %s: %s", path, _FMP_DISABLED_REASON)
        return None
    if not FMP_API_KEY:
        return None
    p = dict(params or {})
    p["apikey"] = FMP_API_KEY
    for attempt in range(1, _FMP_MAX_ATTEMPTS + 1):
        _throttle()  # 全局最小请求间隔，避免连发触发 per-minute 限流
        try:
            r = requests.get(f"{FMP_BASE}{path}", params=p, timeout=20)
        except Exception as e:
            _record_event("WARN", "request_exception", path, str(e))
            logger.warning("FMP %s failed: %s", path, _redact(str(e)))
            return None
        # 429 = per-minute 限流；402 = FMP 对历史端点的突发超限（措辞像"需升级订阅"，
        # 但实测会自行恢复，本质是短窗口限流）。两者都退避重试，给限流窗口恢复机会，不一次放弃。
        if r.status_code in (429, 402):
            _record_event("WARN", f"throttled_{r.status_code}_attempt_{attempt}", path, r.text)
            if attempt < _FMP_MAX_ATTEMPTS:
                time.sleep(_FMP_RETRY_BACKOFF * attempt)  # 线性退避，给限流窗口恢复
                continue
            # 重试耗尽仍被限：软禁用本批剩余（避免继续撞墙），调用方退 yfinance 兜底
            _FMP_DISABLED_REASON = f"throttled_{r.status_code}"
            logger.warning("FMP %s 限流(%d)重试 %d 次仍失败，本批剩余跳过退兜底", path, r.status_code, _FMP_MAX_ATTEMPTS)
            return None
        if r.status_code != 200:
            _record_event("WARN", f"http_{r.status_code}", path, r.text)
            logger.warning("FMP %s -> %d: %s", path, r.status_code, _redact(r.text[:200]))
            return None
        try:
            data = r.json()
        except Exception as e:
            _record_event("WARN", "json_decode", path, str(e))
            logger.warning("FMP %s json 解析失败: %s", path, _redact(str(e)))
            return None
        # FMP 错误时返回 {"Error Message": "..."}
        if isinstance(data, dict) and "Error Message" in data:
            logger.debug("FMP error for %s: %s", path, data.get("Error Message"))
            return None
        fmp_cache.save(path, params, data)
        return data
    return None


# ────────────────────────────────────────────────────────
# 行情：实时报价 + 日线历史（2026-06-10 加 · 美股抓价主源，替代不稳定的 yfinance）
#   背景：yfinance 抓 Yahoo 公开数据、无 SLA，会整批限流返回空历史 → 大票动量断档。
#   FMP /stable 行情接口稳定，付费源，已是系统美股主源（FMP=美股付费主源的分工延续）。
# ────────────────────────────────────────────────────────

def fetch_quote(ticker: str, *, force_refresh: bool = True) -> dict[str, Any] | None:
    """实时报价：价格 / 涨跌幅 / 52 周高低 / 市值 / 均线。价格是快变量，默认不吃缓存。"""
    raw = _get("/quote", {"symbol": ticker}, force_refresh=force_refresh)
    if not raw or not isinstance(raw, list) or not raw:
        return None
    r = raw[0]
    return {
        "ticker": ticker,
        "price": r.get("price"),
        "previous_close": r.get("previousClose"),
        "change_pct": r.get("changePercentage"),
        "day_low": r.get("dayLow"),
        "day_high": r.get("dayHigh"),
        "year_high": r.get("yearHigh"),
        "year_low": r.get("yearLow"),
        "market_cap": r.get("marketCap"),
        "price_avg_50": r.get("priceAvg50"),
        "price_avg_200": r.get("priceAvg200"),
        "volume": r.get("volume"),
        "exchange": r.get("exchange"),
        "source": "FMP/stable/quote",
    }


def fetch_historical_eod(ticker: str, *, days: int = 420, force_refresh: bool = True) -> list[dict[str, Any]] | None:
    """日线收盘历史（新→旧）。默认拉最近 ~14 个月，够算 YTD / 1M / 1W / 1Y 动量。

    返回 [{date, open, high, low, close, volume, change, changePercent, vwap}]。
    价格是快变量，默认 force_refresh=True 不吃 24h 缓存（每天要最新收盘）。
    """
    frm = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    raw = _get("/historical-price-eod/full", {"symbol": ticker, "from": frm}, force_refresh=force_refresh)
    if not raw or not isinstance(raw, list):
        return None
    return raw


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


def fetch_grade_events(ticker: str) -> list[dict[str, Any]] | None:
    """逐笔分析师评级变动事件流(PIT 正确:每条带发生日期,不会被改写)。

    实测 2026-06-12: 一次调用返回全量历史(NVDA ~1130 条,跨数年),
    是"盈利预期上修"的长历史近亲 — 上下调事件即预期方向的离散信号。
    """
    raw = _get("/grades", {"symbol": ticker, "limit": 2000})
    if not raw or not isinstance(raw, list):
        return None
    out = []
    for r in raw:
        out.append({
            "date": r.get("date"),
            "grading_company": r.get("gradingCompany"),
            "previous_grade": r.get("previousGrade"),
            "new_grade": r.get("newGrade"),
            "action": r.get("action"),
        })
    return out


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
