"""Google Trends 搜索热度抓取（情绪面客观指标）。

pytrends 不需要 API key，但有反爬限流；建议每只股票之间 sleep 1s。
"""
from __future__ import annotations
import logging
import time
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def _import_pytrends():
    try:
        from pytrends.request import TrendReq
        return TrendReq
    except ImportError:
        logger.error("pytrends not installed; pip install pytrends")
        return None


def fetch_trend(keyword: str, timeframe: str = "today 3-m",
                geo: str = "") -> dict[str, Any] | None:
    """单关键词搜索热度。

    timeframe: 'today 3-m' / 'today 12-m' / 'now 7-d' 等
    geo: '' = 全球, 'US' / 'CN' / 'HK' 等
    返回 {'keyword','avg','last','peak','trend','source'}.
    """
    TrendReq = _import_pytrends()
    if not TrendReq:
        return None
    try:
        py = TrendReq(hl="en-US", tz=480, retries=2, timeout=(10, 25))
        py.build_payload([keyword], timeframe=timeframe, geo=geo)
        df = py.interest_over_time()
        if df is None or df.empty:
            return None
        col = df[keyword]
        avg = float(col.mean())
        last = float(col.iloc[-1])
        peak = float(col.max())
        # 简单趋势：最近 4 个点 vs 之前的均值
        if len(col) >= 8:
            recent = col.iloc[-4:].mean()
            earlier = col.iloc[:-4].mean()
            if earlier > 0:
                trend_pct = (recent - earlier) / earlier * 100
            else:
                trend_pct = 0.0
        else:
            trend_pct = 0.0
        return {
            "keyword": keyword,
            "geo": geo or "global",
            "timeframe": timeframe,
            "avg": round(avg, 2),
            "last": round(last, 2),
            "peak": round(peak, 2),
            "trend_pct": round(trend_pct, 2),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "source": "pytrends/Google Trends",
        }
    except Exception as e:
        logger.warning("pytrends failed for %s: %s", keyword, e)
        return None


def fetch_trends_batch(keywords: list[str], timeframe: str = "today 3-m",
                       geo: str = "", sleep_sec: float = 1.5) -> list[dict[str, Any]]:
    """批量抓取，注意 Google Trends 限流。"""
    out = []
    for kw in keywords:
        d = fetch_trend(kw, timeframe=timeframe, geo=geo)
        if d:
            out.append(d)
        time.sleep(sleep_sec)
    return out
