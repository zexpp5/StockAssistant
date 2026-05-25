"""NASDAQ 公开 IPO API 封装 — 三市场新股雷达的美股数据底座。

API: https://api.nasdaq.com/api/ipo/calendar?date=YYYY-MM
返回结构: {data: {priced: {rows: []}, upcoming: {rows: []}, filed: {rows: []}, withdrawn: {rows: []}}}

接口虽是 NASDAQ 旗下,但**覆盖全美交易所**(NYSE/NASDAQ/AMEX),不止 NASDAQ。
免费、稳定、不需要 API key。是 finnhub 免费层之外最实用的美股 IPO 数据源。

字段约定 (priced):
  proposedTickerSymbol → ticker
  pricedDate           → 上市日 (M/D/YYYY)
  proposedSharePrice   → 发行价 (常缺失,需 fallback yfinance 首日)
  sharesOffered        → 发行股数 (带逗号字符串)
  dollarValueOfSharesOffered → 总融资额 (字符串带 $)
  dealStatus           → 通常 "Priced"

字段约定 (filed):
  proposedTickerSymbol → ticker (有时为空)
  filedDate            → 申报日 (M/D/YYYY)
  dollarValueOfSharesOffered → 预计融资额

字段约定 (upcoming):
  通常和 priced 字段一致,但实测多数月份为空 — 用 filed 替代「即将申购」更可靠

缓存:
  data/cache/nasdaq_ipo_monthly/{YYYY-MM}.json
  - 过去月份永久缓存(IPO 事件不会回改)
  - 当前月份 TTL 24 小时,过期重拉
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO / "data" / "cache" / "nasdaq_ipo_monthly"
CURRENT_MONTH_TTL_HOURS = 24

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _parse_us_date(s: str | None) -> str | None:
    """NASDAQ 用 M/D/YYYY 格式,统一成 ISO YYYY-MM-DD。"""
    if not s:
        return None
    try:
        # 兼容 "5/21/2026" 和 "05/21/2026"
        m, d, y = s.split("/")
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except Exception:
        return None


def _parse_shares(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(str(s).replace(",", ""))
    except Exception:
        return None


def _parse_dollars(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except Exception:
        return None


def _parse_float(s: Any) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(s)
    except Exception:
        return None


def _fetch_month_remote(year: int, month: int) -> dict:
    """直接请求 NASDAQ API,不走缓存。返回 raw payload。"""
    ym = f"{year:04d}-{month:02d}"
    url = f"https://api.nasdaq.com/api/ipo/calendar?date={ym}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json() or {}
    except Exception as e:
        logger.warning("nasdaq ipo %s fetch failed: %s", ym, e)
        return {}


def fetch_month(year: int, month: int, force: bool = False) -> dict:
    """带缓存的月度 IPO 数据。过去月份永久缓存,当前月份 24h TTL。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ym = f"{year:04d}-{month:02d}"
    p = CACHE_DIR / f"{ym}.json"

    today = date.today()
    is_current_month = (year == today.year and month == today.month)
    is_future_month = (year > today.year) or (year == today.year and month > today.month)

    if not force and p.exists():
        try:
            cached = json.loads(p.read_text(encoding="utf-8"))
            if is_current_month or is_future_month:
                fetched_at = cached.get("_fetched_at")
                if fetched_at:
                    try:
                        age_h = (datetime.now() - datetime.fromisoformat(fetched_at)).total_seconds() / 3600
                        if age_h < CURRENT_MONTH_TTL_HOURS:
                            return cached
                    except Exception:
                        pass
            else:
                # 过去月份永久缓存
                return cached
        except Exception:
            pass

    payload = _fetch_month_remote(year, month)
    payload["_fetched_at"] = datetime.now().isoformat(timespec="seconds")
    payload["_year"] = year
    payload["_month"] = month
    try:
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("cache write %s failed: %s", ym, e)
    return payload


def _normalize_priced(row: dict) -> dict:
    return {
        "symbol": (row.get("proposedTickerSymbol") or "").upper(),
        "name": row.get("companyName") or "",
        "exchange": row.get("proposedExchange") or "",
        "priced_date": _parse_us_date(row.get("pricedDate")),
        "issue_price": _parse_float(row.get("proposedSharePrice")),
        "shares_offered": _parse_shares(row.get("sharesOffered")),
        "deal_value_usd": _parse_dollars(row.get("dollarValueOfSharesOffered")),
        "deal_status": row.get("dealStatus") or "Priced",
        "deal_id": row.get("dealID"),
    }


def _normalize_filed(row: dict) -> dict:
    return {
        "symbol": (row.get("proposedTickerSymbol") or "").upper(),
        "name": row.get("companyName") or "",
        "exchange": row.get("proposedExchange") or "",
        "filed_date": _parse_us_date(row.get("filedDate")),
        "expected_value_usd": _parse_dollars(row.get("dollarValueOfSharesOffered")),
        "deal_id": row.get("dealID"),
    }


def fetch_window(months_back: int = 24, months_forward: int = 2,
                 anchor: date | None = None) -> dict:
    """拉过去 months_back 月 + 未来 months_forward 月的合并结果。

    Returns:
      {
        "priced":  [{symbol, name, priced_date, issue_price, shares_offered, deal_value_usd, ...}],
        "filed":   [{symbol, name, filed_date, expected_value_usd, ...}],
        "months_pulled": [YYYY-MM list],
        "errors":  [],
      }
    """
    today = anchor or date.today()
    months: list[tuple[int, int]] = []
    # 历史
    y, m = today.year, today.month
    for _ in range(months_back):
        m -= 1
        if m < 1:
            m = 12; y -= 1
        months.append((y, m))
    # 未来
    y, m = today.year, today.month
    months.append((y, m))  # 当前月
    for _ in range(months_forward):
        m += 1
        if m > 12:
            m = 1; y += 1
        months.append((y, m))

    priced_all: list[dict] = []
    filed_all: list[dict] = []
    pulled = []
    errors = []
    seen_ids = set()  # 跨月去重 (dealID)

    for (yr, mo) in months:
        payload = fetch_month(yr, mo)
        if not payload:
            errors.append(f"{yr}-{mo:02d}: empty")
            continue
        pulled.append(f"{yr:04d}-{mo:02d}")
        data = payload.get("data") or {}
        for r in (data.get("priced") or {}).get("rows", []) or []:
            did = r.get("dealID")
            if did and did in seen_ids:
                continue
            seen_ids.add(did)
            norm = _normalize_priced(r)
            if norm["symbol"]:
                priced_all.append(norm)
        for r in (data.get("filed") or {}).get("rows", []) or []:
            did = r.get("dealID")
            if did and did in seen_ids:
                continue
            seen_ids.add(did)
            norm = _normalize_filed(r)
            filed_all.append(norm)

    # 按日期排序
    priced_all.sort(key=lambda x: x.get("priced_date") or "", reverse=True)
    filed_all.sort(key=lambda x: x.get("filed_date") or "", reverse=True)

    return {
        "priced": priced_all,
        "filed": filed_all,
        "months_pulled": pulled,
        "errors": errors,
    }


__all__ = ["fetch_month", "fetch_window"]
