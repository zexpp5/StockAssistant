"""akshare wrapper：A 股 / 港股专业数据补全。

akshare 是 Python 财经数据社区库，覆盖 A 股、港股的财务、龙虎榜、北向资金等。
本模块只暴露纯函数，返回 dict（无 I/O 副作用）。
"""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _import_ak():
    try:
        import akshare as ak
        return ak
    except ImportError:
        logger.error("akshare not installed; pip install akshare")
        return None


# ────────────────────────────────────────────────────────
# 代码格式工具
# ────────────────────────────────────────────────────────

def cn_a_market_prefix(code: str) -> str:
    """A 股 6 位代码 → akshare 的 sh/sz/bj 前缀。"""
    if not code or not code.isdigit() or len(code) != 6:
        return ""
    if code.startswith(("60", "68", "78", "73", "603")):
        return "sh"
    if code.startswith(("00", "30", "20")):
        return "sz"
    if code.startswith(("8", "9")):
        return "bj"
    return ""


def hk_pad(code: str) -> str:
    """港股代码补齐 5 位。"""
    s = "".join(c for c in code if c.isdigit())
    return s.zfill(5) if s else ""


# ────────────────────────────────────────────────────────
# A 股
# ────────────────────────────────────────────────────────

def fetch_a_stock_quote(code: str) -> dict[str, Any] | None:
    """A 股实时快照（akshare 东财源）。"""
    ak = _import_ak()
    if not ak:
        return None
    try:
        df = ak.stock_zh_a_spot_em()
        row = df[df["代码"] == code]
        if row.empty:
            return None
        r = row.iloc[0]
        return {
            "code": code,
            "name": str(r.get("名称", "")),
            "price": _safe_float(r.get("最新价")),
            "change_pct": _safe_float(r.get("涨跌幅")),
            "turnover": _safe_float(r.get("换手率")),
            "pe_ttm": _safe_float(r.get("市盈率-动态")),
            "pb": _safe_float(r.get("市净率")),
            "market_cap_yuan": _safe_float(r.get("总市值")),
            "circulating_cap_yuan": _safe_float(r.get("流通市值")),
            "source": "akshare/stock_zh_a_spot_em",
        }
    except Exception as e:
        logger.warning("akshare A spot failed for %s: %s", code, e)
        return None


def fetch_a_stock_financial(code: str) -> dict[str, Any] | None:
    """A 股核心财务指标（最新一期）。"""
    ak = _import_ak()
    if not ak:
        return None
    try:
        df = ak.stock_financial_abstract(symbol=code)
        if df is None or df.empty:
            return None
        # 新版 akshare 返回的是宽表（指标 × 报告期），取最新一列
        report_cols = [c for c in df.columns if c not in ("选项", "指标")]
        if not report_cols:
            return None
        latest = report_cols[0]
        out = {"code": code, "report_date": latest, "source": "akshare/stock_financial_abstract"}
        for _, r in df.iterrows():
            metric = str(r.get("指标", "")).strip()
            if metric:
                out[metric] = str(r.get(latest, ""))
        return out
    except Exception as e:
        logger.warning("akshare A financial failed for %s: %s", code, e)
        return None


def fetch_a_north_flow(code: str) -> dict[str, Any] | None:
    """A 股北向资金持股变动（个股）。"""
    ak = _import_ak()
    if not ak:
        return None
    try:
        prefix = cn_a_market_prefix(code)
        if not prefix:
            return None
        df = ak.stock_hsgt_individual_em(stock=f"{prefix}{code}")
        if df is None or df.empty:
            return None
        latest = df.iloc[-1]
        return {
            "code": code,
            "date": str(latest.get("持股日期", "")),
            "shares_held": _safe_float(latest.get("持股数量")),
            "shares_held_pct": _safe_float(latest.get("持股数量占发行股百分比")),
            "value_yuan": _safe_float(latest.get("持股市值")),
            "source": "akshare/stock_hsgt_individual_em",
        }
    except Exception as e:
        logger.debug("akshare north flow not available for %s: %s", code, e)
        return None


# ────────────────────────────────────────────────────────
# 港股
# ────────────────────────────────────────────────────────

def fetch_hk_stock_quote(code: str) -> dict[str, Any] | None:
    """港股实时快照。"""
    ak = _import_ak()
    if not ak:
        return None
    try:
        sym = hk_pad(code)
        df = ak.stock_hk_spot_em()
        row = df[df["代码"] == sym]
        if row.empty:
            return None
        r = row.iloc[0]
        return {
            "code": sym,
            "name": str(r.get("名称", "")),
            "price": _safe_float(r.get("最新价")),
            "change_pct": _safe_float(r.get("涨跌幅")),
            "market_cap_hkd": _safe_float(r.get("总市值")),
            "source": "akshare/stock_hk_spot_em",
        }
    except Exception as e:
        logger.warning("akshare HK spot failed for %s: %s", code, e)
        return None


def fetch_hk_southbound_flow(code: str) -> dict[str, Any] | None:
    """港股南向资金持股（来自港交所披露易，akshare 抓的）。"""
    ak = _import_ak()
    if not ak:
        return None
    try:
        df = ak.stock_hk_ggt_components_em()
        if df is None or df.empty:
            return None
        sym = hk_pad(code)
        row = df[df["代码"] == sym]
        if row.empty:
            return None
        r = row.iloc[0]
        return {
            "code": sym,
            "name": str(r.get("名称", "")),
            "shares_pct": _safe_float(r.get("持股占已发行股本百分比")),
            "source": "akshare/stock_hk_ggt_components_em",
        }
    except Exception as e:
        logger.debug("akshare HK southbound not available for %s: %s", code, e)
        return None


# ────────────────────────────────────────────────────────
# 工具
# ────────────────────────────────────────────────────────

def _safe_float(v) -> float | None:
    try:
        if v is None or v == "" or v == "-":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_enriched(code: str, market: str) -> dict[str, Any]:
    """一站式：根据市场类型聚合 akshare 各项数据。"""
    market_lc = (market or "").lower()
    out: dict[str, Any] = {"code": code, "market": market, "akshare": {}}

    if "港股" in market or "hk" in market_lc:
        q = fetch_hk_stock_quote(code)
        if q:
            out["akshare"]["quote"] = q
        sb = fetch_hk_southbound_flow(code)
        if sb:
            out["akshare"]["southbound_flow"] = sb
    elif any(t in market for t in ("A股", "深交所", "上交所", "科创", "北交")) or code.isdigit():
        q = fetch_a_stock_quote(code)
        if q:
            out["akshare"]["quote"] = q
        fin = fetch_a_stock_financial(code)
        if fin:
            out["akshare"]["financial"] = fin
        nb = fetch_a_north_flow(code)
        if nb:
            out["akshare"]["north_flow"] = nb

    return out
