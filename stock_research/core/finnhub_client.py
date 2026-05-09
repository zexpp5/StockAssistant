"""Finnhub API wrapper：新闻 + 内部人交易 + 分析师评级。

凭证：FINNHUB_API_KEY 环境变量；缺 key 时所有方法返回 None（不抛错，便于 graceful degrade）。
免费层：60 calls/min，足够 watchlist 全量刷新。
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from .. import config

logger = logging.getLogger(__name__)


def _client():
    if not config.FINNHUB_API_KEY:
        logger.debug("FINNHUB_API_KEY not set; finnhub disabled")
        return None
    try:
        import finnhub
        return finnhub.Client(api_key=config.FINNHUB_API_KEY)
    except ImportError:
        logger.error("finnhub-python not installed; pip install finnhub-python")
        return None


def is_available() -> bool:
    return bool(config.FINNHUB_API_KEY)


# ────────────────────────────────────────────────────────
# 新闻
# ────────────────────────────────────────────────────────

def fetch_company_news(ticker: str, days: int = 7) -> list[dict[str, Any]] | None:
    """近 N 天的公司新闻。返回精简结构。"""
    c = _client()
    if not c:
        return None
    try:
        end = datetime.now().date()
        start = end - timedelta(days=days)
        raw = c.company_news(ticker, _from=start.isoformat(), to=end.isoformat())
        if not raw:
            return []
        out = []
        for item in raw[:30]:  # 最多 30 条
            out.append({
                "datetime": datetime.fromtimestamp(item.get("datetime", 0)).isoformat(timespec="minutes")
                            if item.get("datetime") else None,
                "headline": item.get("headline", ""),
                "summary": (item.get("summary") or "")[:200],
                "source": item.get("source", ""),
                "url": item.get("url", ""),
                "category": item.get("category", ""),
            })
        return out
    except Exception as e:
        logger.warning("finnhub news failed for %s: %s", ticker, e)
        return None


# ────────────────────────────────────────────────────────
# 内部人交易（C-suite 买卖股票）
# ────────────────────────────────────────────────────────

def fetch_insider_transactions(ticker: str, days: int = 90) -> dict[str, Any] | None:
    """近 N 天的内部人交易。"""
    c = _client()
    if not c:
        return None
    try:
        end = datetime.now().date()
        start = end - timedelta(days=days)
        raw = c.stock_insider_transactions(ticker, start.isoformat(), end.isoformat())
        data = raw.get("data", []) if isinstance(raw, dict) else []
        if not data:
            return {"ticker": ticker, "count": 0, "buy_count": 0, "sell_count": 0,
                    "net_shares": 0, "transactions": []}
        buy_count = sum(1 for t in data if t.get("change", 0) > 0)
        sell_count = sum(1 for t in data if t.get("change", 0) < 0)
        net_shares = sum(t.get("change", 0) for t in data)
        latest = sorted(data, key=lambda x: x.get("filingDate") or "", reverse=True)[:10]
        return {
            "ticker": ticker,
            "lookback_days": days,
            "count": len(data),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "net_shares": net_shares,
            "transactions": [
                {
                    "name": t.get("name", ""),
                    "filing_date": t.get("filingDate", ""),
                    "transaction_date": t.get("transactionDate", ""),
                    "change": t.get("change", 0),
                    "share": t.get("share", 0),
                    "transaction_price": t.get("transactionPrice", 0),
                }
                for t in latest
            ],
            "source": "finnhub/stock_insider_transactions",
        }
    except Exception as e:
        logger.warning("finnhub insider failed for %s: %s", ticker, e)
        return None


# ────────────────────────────────────────────────────────
# 分析师评级
# ────────────────────────────────────────────────────────

def fetch_analyst_recommendations(ticker: str) -> dict[str, Any] | None:
    """最近一期的分析师评级分布（buy/hold/sell 数量）。"""
    c = _client()
    if not c:
        return None
    try:
        raw = c.recommendation_trends(ticker)
        if not raw:
            return None
        latest = raw[0]
        total = (latest.get("strongBuy", 0) + latest.get("buy", 0) +
                 latest.get("hold", 0) + latest.get("sell", 0) + latest.get("strongSell", 0))
        return {
            "ticker": ticker,
            "period": latest.get("period", ""),
            "strong_buy": latest.get("strongBuy", 0),
            "buy": latest.get("buy", 0),
            "hold": latest.get("hold", 0),
            "sell": latest.get("sell", 0),
            "strong_sell": latest.get("strongSell", 0),
            "total": total,
            "buy_ratio": round((latest.get("strongBuy", 0) + latest.get("buy", 0)) / total, 2)
                         if total else None,
            "source": "finnhub/recommendation_trends",
        }
    except Exception as e:
        logger.warning("finnhub recs failed for %s: %s", ticker, e)
        return None


def fetch_analyst_price_target(ticker: str) -> dict[str, Any] | None:
    """分析师目标价共识。"""
    c = _client()
    if not c:
        return None
    try:
        raw = c.price_target(ticker)
        if not raw:
            return None
        # 免费层访问受限会返回空 dict 或 403
        if not raw.get("targetMean") and not raw.get("targetMedian"):
            return None
        return {
            "ticker": ticker,
            "target_high": raw.get("targetHigh"),
            "target_low": raw.get("targetLow"),
            "target_mean": raw.get("targetMean"),
            "target_median": raw.get("targetMedian"),
            "last_updated": raw.get("lastUpdated", ""),
            "source": "finnhub/price_target",
        }
    except Exception as e:
        # 免费层限制时返回 403，静默跳过（debug 级别）
        if "403" in str(e) or "access" in str(e).lower():
            logger.debug("finnhub price target requires premium for %s", ticker)
        else:
            logger.warning("finnhub price target failed for %s: %s", ticker, e)
        return None


# ────────────────────────────────────────────────────────
# 一站式
# ────────────────────────────────────────────────────────

def fetch_enriched(ticker: str, sleep_sec: float = 0.5) -> dict[str, Any]:
    """对一只美股做一站式 enrichment。"""
    out: dict[str, Any] = {"ticker": ticker, "finnhub": {}}
    if not is_available():
        out["finnhub"]["disabled"] = "FINNHUB_API_KEY not set"
        return out
    news = fetch_company_news(ticker)
    if news is not None:
        out["finnhub"]["news"] = news
    time.sleep(sleep_sec)
    insider = fetch_insider_transactions(ticker)
    if insider:
        out["finnhub"]["insider"] = insider
    time.sleep(sleep_sec)
    recs = fetch_analyst_recommendations(ticker)
    if recs:
        out["finnhub"]["analyst_recommendations"] = recs
    time.sleep(sleep_sec)
    pt = fetch_analyst_price_target(ticker)
    if pt:
        out["finnhub"]["price_target"] = pt
    return out
