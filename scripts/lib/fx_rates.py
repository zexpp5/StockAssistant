"""Single source for FX rates used by the whole system.

All RMB conversion must flow through this module or through the `/api/fx-rates`
endpoint that wraps it. Daily refresh writes `data/latest/fx_rates.json`; if the
external source is unavailable, callers fall back to one static, repo-wide table.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
CACHE_PATH = REPO / "data" / "latest" / "fx_rates.json"

# Repo-wide fallback. These are not "live"; they are the last-resort values used
# when Yahoo/remote refresh and local cache are both unavailable.
FALLBACK_FX_TO_RMB: dict[str, float] = {
    "CNY": 1.0,
    "USD": 6.7645,
    "HKD": 0.8627,
    "JPY": 0.0423,
    "KRW": 0.0045,
    "TWD": 0.2151,
    "EUR": 7.8698,
    "AUD": 4.8450,
    "GBP": 9.1041,
}

FALLBACK_AS_OF = "2026-06-02"

_YAHOO_SYMBOLS: dict[str, str] = {
    "USD": "CNY=X",       # CNY per USD
    "HKD": "HKDCNY=X",    # CNY per HKD
    "JPY": "JPYCNY=X",    # CNY per JPY
    "KRW": "KRWCNY=X",    # CNY per KRW
    "TWD": "TWDCNY=X",    # CNY per TWD
    "EUR": "EURCNY=X",    # CNY per EUR
    "AUD": "AUDCNY=X",    # CNY per AUD
    "GBP": "GBPCNY=X",    # CNY per GBP
}

_VALID_RANGES: dict[str, tuple[float, float]] = {
    "CNY": (1.0, 1.0),
    "USD": (6.0, 7.8),
    "HKD": (0.80, 1.05),
    "JPY": (0.035, 0.060),
    "KRW": (0.0040, 0.0065),
    "TWD": (0.18, 0.26),
    "EUR": (7.0, 9.0),
    "AUD": (4.0, 5.5),
    "GBP": (7.5, 10.5),
}


def _today() -> str:
    return date.today().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _clean_ccy(ccy: str | None) -> str:
    return (ccy or "").strip().upper()


def _is_valid_rate(ccy: str, value: Any) -> bool:
    try:
        v = float(value)
    except Exception:
        return False
    if not math.isfinite(v) or v <= 0:
        return False
    lo, hi = _VALID_RANGES.get(ccy, (0.000001, 1_000_000.0))
    return lo <= v <= hi


def _normalize_rates(rates: dict[str, Any] | None) -> dict[str, float]:
    out = dict(FALLBACK_FX_TO_RMB)
    if not isinstance(rates, dict):
        return out
    for raw_ccy, raw_value in rates.items():
        ccy = _clean_ccy(raw_ccy)
        if ccy in FALLBACK_FX_TO_RMB and _is_valid_rate(ccy, raw_value):
            out[ccy] = float(raw_value)
    out["CNY"] = 1.0
    return out


def _fallback_payload(*, errors: list[str] | None = None) -> dict[str, Any]:
    return {
        "status": "FALLBACK",
        "source": "static",
        "as_of": FALLBACK_AS_OF,
        "refreshed_at": None,
        "rates": dict(FALLBACK_FX_TO_RMB),
        "errors": errors or [],
    }


def _read_cache() -> dict[str, Any] | None:
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    rates = _normalize_rates(payload.get("rates"))
    payload["rates"] = rates
    payload.setdefault("status", "OK")
    payload.setdefault("source", "cache")
    payload.setdefault("as_of", payload.get("date") or FALLBACK_AS_OF)
    payload.setdefault("errors", [])
    return payload


def _write_cache(payload: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".fx_rates_", suffix=".json", dir=str(CACHE_PATH.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    Path(tmp).replace(CACHE_PATH)


def _fetch_yahoo_rate(symbol: str, *, timeout_sec: float = 6.0) -> float | None:
    encoded = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=5d&interval=1d"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "StockAssistant/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    result = ((data.get("chart") or {}).get("result") or [None])[0] or {}
    meta = result.get("meta") or {}
    price = meta.get("regularMarketPrice") or meta.get("previousClose")
    if price is not None and math.isfinite(float(price)):
        return float(price)
    closes = (((result.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    for value in reversed(closes):
        if value is not None and math.isfinite(float(value)):
            return float(value)
    return None


def _fetch_yahoo_rate_yf(symbol: str) -> float | None:
    """Fallback via the yfinance library when the raw v8 chart endpoint is blocked.

    2026-06-02 修复（HKD 0.917 vs 0.863 漂移事故）：某些环境下
    query1.finance.yahoo.com 的 v8 chart 端点会 timeout/拒连，但 yfinance 库走自己的
    session 仍可用（价格抓取就靠它）。FX 单一来源不能因这一路不通就退化到 real-world
    静态表 —— 那会和入场时锁定的 yahoo_historical 汇率口径打架，把持仓盈亏算虚高。
    """
    try:
        import yfinance as yf  # 延迟导入，保持本模块默认零额外依赖
    except Exception:
        return None
    try:
        fi = yf.Ticker(symbol).fast_info
        value = (
            fi.get("lastPrice") or fi.get("last_price")
            or fi.get("previousClose") or fi.get("previous_close")
        )
        if value is not None and math.isfinite(float(value)):
            return float(value)
    except Exception:
        return None
    return None


def _coerce_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except Exception:
            return None
    return None


def _fetch_yahoo_historical_rate(
    symbol: str,
    target_date: date,
    *,
    timeout_sec: float = 6.0,
) -> tuple[float, str] | None:
    """Fetch the closest daily close on or before target_date from Yahoo."""
    start = target_date - timedelta(days=8)
    end = target_date + timedelta(days=2)
    period1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    period2 = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp())
    encoded = urllib.parse.quote(symbol, safe="")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}"
        f"?period1={period1}&period2={period2}&interval=1d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "StockAssistant/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    result = ((data.get("chart") or {}).get("result") or [None])[0] or {}
    timestamps = result.get("timestamp") or []
    closes = (((result.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    candidates: list[tuple[date, float]] = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        try:
            d = datetime.fromtimestamp(float(ts), tz=timezone.utc).date()
            v = float(close)
        except Exception:
            continue
        if math.isfinite(v) and v > 0:
            candidates.append((d, v))
    if not candidates:
        return None
    before = [x for x in candidates if x[0] <= target_date]
    picked = max(before, key=lambda x: x[0]) if before else min(candidates, key=lambda x: x[0])
    return picked[1], picked[0].isoformat()


def refresh_fx_rates(*, timeout_sec: float = 6.0, write_cache: bool = True) -> dict[str, Any]:
    """Refresh live FX rates.

    The function never raises for source/network failures. It returns a payload
    with `status=OK` when all configured live rates were fetched, `PARTIAL` when
    some currencies fell back, and `FALLBACK` when all remote calls failed.
    """
    rates = dict(FALLBACK_FX_TO_RMB)
    errors: list[str] = []
    live_count = 0

    for ccy, symbol in _YAHOO_SYMBOLS.items():
        try:
            value = _fetch_yahoo_rate(symbol, timeout_sec=timeout_sec)
            if value is None or not _is_valid_rate(ccy, value):
                # 原始 HTTP 端点不通/超范围 → 退到 yfinance 库再试一次，仍不行才记 error。
                value = _fetch_yahoo_rate_yf(symbol)
            if value is None or not _is_valid_rate(ccy, value):
                raise ValueError(f"invalid {symbol}={value}")
            rates[ccy] = float(value)
            live_count += 1
        except Exception as exc:
            errors.append(f"{ccy}:{symbol}:{str(exc)[:120]}")

    rates["CNY"] = 1.0
    if live_count == len(_YAHOO_SYMBOLS):
        status = "OK"
        source = "yahoo"
    elif live_count > 0:
        status = "PARTIAL"
        source = "yahoo+fallback"
    else:
        status = "FALLBACK"
        source = "static"

    payload = {
        "status": status,
        "source": source,
        "as_of": _today() if live_count else FALLBACK_AS_OF,
        "refreshed_at": _now_iso(),
        "rates": rates,
        "errors": errors,
    }
    if write_cache:
        _write_cache(payload)
    return payload


def get_fx_payload(*, prefer_cache: bool = True) -> dict[str, Any]:
    """Return the latest cached payload, or the static fallback payload."""
    if prefer_cache:
        cached = _read_cache()
        if cached:
            return cached
    return _fallback_payload()


def get_all_fx_to_rmb(*, prefer_cache: bool = True) -> dict[str, float]:
    return dict(get_fx_payload(prefer_cache=prefer_cache)["rates"])


def get_fx_to_rmb(ccy: str | None) -> float:
    """Safely query currency-to-RMB. Unknown currencies are treated as CNY."""
    ccy = _clean_ccy(ccy)
    if not ccy:
        return 1.0
    return get_all_fx_to_rmb().get(ccy, 1.0)


def get_historical_fx_payload(
    ccy: str | None,
    target_date: Any,
    *,
    timeout_sec: float = 6.0,
) -> dict[str, Any]:
    """Return an RMB FX rate intended to be locked on a holding's entry date.

    This function is deliberately fail-soft: if a historical quote cannot be
    fetched, it falls back to the current single-source FX cache, then to the
    static table. Callers can safely persist the returned `rate` with its source.
    """
    clean = _clean_ccy(ccy) or "CNY"
    d = _coerce_date(target_date)
    errors: list[str] = []

    if clean == "CNY":
        return {"currency": "CNY", "rate": 1.0, "as_of": (d or date.today()).isoformat(), "source": "identity", "errors": []}

    symbol = _YAHOO_SYMBOLS.get(clean)
    if symbol and d:
        try:
            fetched = _fetch_yahoo_historical_rate(symbol, d, timeout_sec=timeout_sec)
            if fetched is not None:
                rate, as_of = fetched
                if _is_valid_rate(clean, rate):
                    return {
                        "currency": clean,
                        "rate": float(rate),
                        "as_of": as_of,
                        "source": "yahoo_historical",
                        "errors": [],
                    }
                errors.append(f"invalid historical {symbol}={rate}")
            else:
                errors.append(f"no historical quote for {symbol}")
        except Exception as exc:
            errors.append(f"{symbol}:{str(exc)[:120]}")

    payload = get_fx_payload()
    rates = _normalize_rates(payload.get("rates"))
    rate = rates.get(clean, FALLBACK_FX_TO_RMB.get(clean, 1.0))
    if not _is_valid_rate(clean, rate):
        rate = FALLBACK_FX_TO_RMB.get(clean, 1.0)
        source = "static_fallback"
        as_of = FALLBACK_AS_OF
    else:
        source = f"{payload.get('source') or 'cache'}_entry_fallback"
        as_of = str(payload.get("as_of") or FALLBACK_AS_OF)
    return {
        "currency": clean,
        "rate": float(rate),
        "as_of": as_of,
        "source": source,
        "errors": errors,
    }


def infer_currency_from_ticker(ticker: str | None) -> str:
    """Infer quote currency from ticker suffix."""
    if not ticker:
        return "USD"
    s = ticker.upper().strip()
    if s.endswith((".SS", ".SZ", ".BJ", ".SH")):
        return "CNY"
    if s.endswith(".HK"):
        return "HKD"
    if s.endswith(".T"):
        return "JPY"
    if s.endswith(".KS"):
        return "KRW"
    if s.endswith(".AX"):
        return "AUD"
    if s.endswith((".L", ".IL")):
        return "GBP"
    return "USD"


_INITIAL_PAYLOAD = get_fx_payload()
FX_TO_RMB: dict[str, float] = dict(_INITIAL_PAYLOAD["rates"])
AS_OF: str = str(_INITIAL_PAYLOAD.get("as_of") or FALLBACK_AS_OF)
SOURCE: str = str(_INITIAL_PAYLOAD.get("source") or "static")
STATUS: str = str(_INITIAL_PAYLOAD.get("status") or "FALLBACK")
