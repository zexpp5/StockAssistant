"""板块 60d 热度 → 持仓行 badge（读 openbb_intel / 实时 sector_etf）。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from stock_research import config
from stock_research.adapters import store
from stock_research.core.sector_etf import GICS_SECTOR_ETFS, THEME_TO_ETF, get_sector_rotation_signal

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[2]


def _load_sector_rotation() -> dict[str, Any]:
    snap = store.load_latest_json(config.AUDIT_DIR, "openbb_intel")
    rot = (snap or {}).get("sector_rotation") if isinstance(snap, dict) else None
    if rot and rot.get("all_rankings"):
        return rot
    try:
        return get_sector_rotation_signal(lookback_days=60)
    except Exception as exc:
        logger.warning("sector rotation fallback failed: %s", exc)
        return {}


def _etf_returns(rotation: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rotation.get("all_rankings") or []:
        t = row.get("ticker")
        if t and row.get("return_pct") is not None:
            out[str(t)] = float(row["return_pct"])
    if not out:
        for row in (rotation.get("leaders") or []) + (rotation.get("laggards") or []):
            t = row.get("ticker")
            if t and row.get("return_pct") is not None:
                out[str(t)] = float(row["return_pct"])
    return out


def _theme_for_symbol(conn, symbol: str, market: str) -> str | None:
    sym = str(symbol).strip()
    mkt = str(market or "").upper()
    try:
        row = conn.execute(
            """
            SELECT COALESCE(mw.industry, u.industry), COALESCE(u.theme, '')
            FROM (SELECT ? AS symbol, ? AS market) q
            LEFT JOIN manual_watchlist mw ON mw.symbol = q.symbol AND mw.market = q.market
            LEFT JOIN system_universe u ON u.symbol = q.symbol AND u.market = q.market AND u.active = TRUE
            LIMIT 1
            """,
            [sym, mkt],
        ).fetchone()
    except Exception:
        row = None
    if not row:
        return None
    industry, theme = row[0], row[1]
    if theme and str(theme).strip():
        return str(theme).strip()
    if industry and str(industry).strip():
        return str(industry).strip()
    return None


# 非 GICS 11 类、但持仓页需要单独展示的基准（如黄金 ETF）
_EXTRA_BENCHMARK_ETFS = {
    "GLD": "黄金 (Gold)",
}

# 持仓页的最后兜底: 有些真实持仓不在系统科技池,也没有手工 industry。
# 这里按市面常用行业基准 ETF 做保守映射,只用于展示"板块热度"。
_SYMBOL_BENCHMARK_ETFS = {
    "MRVL": "XLK",
    "MCD": "XLY",
    "BRK-B": "XLF",
    "BRK.B": "XLF",
    "9992.HK": "XLY",
    "IAUM": "GLD",
    "GLD": "GLD",
}


def _symbol_benchmark_etf(symbol: str, *, asset_class: str | None = None) -> str | None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    if sym in _SYMBOL_BENCHMARK_ETFS:
        return _SYMBOL_BENCHMARK_ETFS[sym]
    if str(asset_class or "").lower() == "commodity":
        if any(k in sym for k in ("GOLD", "GLD", "IAU", "IAUM")):
            return "GLD"
    return None


def _fetch_extra_etf_return(ticker: str, lookback_days: int = 60) -> float | None:
    """GLD 等不在 GICS 轮动榜里的基准，按需拉 60d 收益。"""
    try:
        import yfinance as yf
        from datetime import datetime, timedelta

        end = datetime.now()
        start = (end - timedelta(days=lookback_days + 10)).strftime("%Y-%m-%d")
        end_s = end.strftime("%Y-%m-%d")
        hist = yf.Ticker(ticker).history(start=start, end=end_s)
        if hist is None or len(hist) < 2:
            return None
        first = float(hist["Close"].iloc[0])
        last = float(hist["Close"].iloc[-1])
        if first <= 0:
            return None
        return round((last / first - 1) * 100, 2)
    except Exception as exc:
        logger.debug("extra etf return %s failed: %s", ticker, exc)
        return None


def _local_symbol_return(conn, symbol: str, lookback_days: int = 60) -> tuple[float, int] | None:
    """外部基准拉不到时,用本地 price_daily 估算持仓自身可用区间收益。"""
    try:
        rows = conn.execute(
            """
            SELECT trade_date, close
            FROM price_daily
            WHERE symbol = ?
              AND interval = '1d'
              AND close IS NOT NULL
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            [str(symbol).strip().upper(), max(2, int(lookback_days) + 10)],
        ).fetchall()
    except Exception:
        return None
    vals = [(r[0], float(r[1])) for r in rows if r and r[1] is not None and float(r[1]) > 0]
    if len(vals) < 2:
        return None
    latest_date, latest = vals[0]
    oldest_date, oldest = vals[-1]
    if oldest <= 0:
        return None
    try:
        days = max(1, (latest_date - oldest_date).days)
    except Exception:
        days = min(lookback_days, len(vals))
    return round((latest / oldest - 1) * 100, 2), int(days)


def _map_theme_to_etf(
    theme: str | None,
    market: str,
    *,
    asset_class: str | None = None,
) -> str | None:
    """主题/行业 → GICS 板块 ETF。匹配不到时返回 None，禁止默认 XLK（会误标消费/黄金）。"""
    ac = str(asset_class or "").lower()
    raw = str(theme or "").strip()
    raw_l = raw.lower()

    if ac in {"commodity", "crypto"}:
        if any(k in raw for k in ("黄金", "贵金属", "金矿")) or any(k in raw_l for k in ("gold", "precious metal")):
            return "GLD"
        return None

    if raw:
        for key, etf in THEME_TO_ETF.items():
            if key in raw or raw in key:
                return etf
        if any(k in raw for k in ("餐饮", "连锁", "快餐", "食品", "饮料", "必需消费", "日常消费")):
            return "XLP"
        if any(k in raw for k in ("玩具", "潮玩", "潮流", "可选消费", "零售", "电商", "汽车", "新能源车", "奢侈品")):
            return "XLY"
        if "消费" in raw:
            return "XLY"
        if any(k in raw for k in ("科技", "半导体", "软件", "算力", "芯片", "AI", "互联网", "云")):
            return "XLK"
        if any(k in raw_l for k in (
            "asic", "networking", "semiconductor", "chip", "gpu", "datacenter",
            "data center", "ai infrastructure", "software", "cloud", "server",
        )):
            return "XLK"
        if any(k in raw for k in ("通信", "传媒", "媒体", "社交")):
            return "XLC"
        if any(k in raw for k in ("医疗", "药", "生物", "器械", "医院")):
            return "XLV"
        if any(k in raw for k in ("金融", "银行", "保险", "券商", "投资控股")):
            return "XLF"
        if any(k in raw for k in ("能源", "油", "气", "铀", "煤炭")):
            return "XLE"
        if any(k in raw for k in ("工业", "制造", "机械", "电力设备", "电网")):
            return "XLI"
        if any(k in raw for k in ("公用", "电力", "核电")):
            return "XLU"
        if any(k in raw for k in ("地产", "房地产", "REIT", "数据中心")):
            return "XLRE"
        if any(k in raw for k in ("材料", "稀土", "金属", "矿业", "化工")):
            return "XLB"
        if any(k in raw for k in ("黄金", "贵金属")):
            return "GLD"

    return None


def classify_etf_return(return_pct: float | None) -> str:
    if return_pct is None:
        return "unknown"
    if return_pct >= 15.0:
        return "hot"
    if return_pct <= 0.0:
        return "cold"
    return "neutral"


def resolve_industry_heat(
    conn,
    symbol: str,
    market: str,
    *,
    rotation: dict[str, Any] | None = None,
    asset_class: str | None = None,
) -> dict[str, Any] | None:
    rot = rotation or _load_sector_rotation()
    etf_returns = _etf_returns(rot)
    if not etf_returns:
        return None
    theme = _theme_for_symbol(conn, symbol, market)
    etf = _map_theme_to_etf(theme, market, asset_class=asset_class)
    mapping_source = "theme"
    if not etf:
        etf = _symbol_benchmark_etf(symbol, asset_class=asset_class)
        mapping_source = "symbol_fallback"
    if not etf:
        return None
    lookback = int(rot.get("lookback_days") or 60)
    ret = etf_returns.get(etf)
    if ret is None and etf in _EXTRA_BENCHMARK_ETFS:
        ret = _fetch_extra_etf_return(etf, lookback_days=lookback)
        if ret is None:
            local = _local_symbol_return(conn, symbol, lookback_days=lookback)
            if local is not None:
                ret, lookback = local
                mapping_source = "local_symbol_fallback"
    if ret is None:
        return None
    badge = classify_etf_return(ret)
    etf_name = GICS_SECTOR_ETFS.get(etf) or _EXTRA_BENCHMARK_ETFS.get(etf, etf)
    leaders = {x.get("ticker") for x in rot.get("leaders") or []}
    laggards = {x.get("ticker") for x in rot.get("laggards") or []}
    if etf in leaders:
        badge = "hot"
    elif etf in laggards:
        badge = "cold"
    hint = {
        "hot": "板块偏强，注意趋势是否过热",
        "cold": "板块偏弱，持仓是否该止盈/减仓可结合体检",
        "neutral": "板块中性",
        "unknown": "",
    }.get(badge, "")
    if ret is not None and ret >= 25.0:
        hint = "板块 60d 涨幅较高，警惕 trend exhaustion"
    return {
        "etf_ticker": etf,
        "etf_name": etf_name,
        "sector_return_60d_pct": ret,
        "industry_heat_badge": badge,
        "theme_used": theme or f"{symbol} fallback",
        "mapping_source": mapping_source,
        "hint": hint,
        "lookback_days": lookback,
    }
