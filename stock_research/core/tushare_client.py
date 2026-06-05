"""Tushare Pro A 股付费主源。

2026-06-05 接入。替代 akshare/baostock 的 A 股行情与财报取数：
  - akshare stock_zh_a_spot_em 是全市场快照表，按 IP 限流、盘中才有当日；
  - baostock 财报需派生 ROA/杠杆（接口只给比率）；
  - Tushare Pro 按 ts_code 精确拉、官方季报无限流、fina_indicator 直接给 ROA/负债率。

边界（Tushare Pro 不提供，仍走 akshare）：
  港股行情/财报、次新股/IPO 雷达、A 股事件日历（解禁/减持/业绩预告）、财经新闻。
  北向个股持股 2024-08 起监管取消披露，hk_hold 已无个股数据，本模块不做。

契约对齐：函数名/返回字段镜像 akshare_client / baostock_client，便于调用方平滑切换。
本模块只暴露纯函数，返回 dict（无文件 I/O 副作用）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_pro: Any = None
_pro_init_failed = False
_basic_cache: dict[str, dict[str, Any]] | None = None


# ────────────────────────────────────────────────────────
# 连接（进程内单例）
# ────────────────────────────────────────────────────────

def _get_pro() -> Any:
    """惰性初始化 tushare pro_api 单例；token/库缺失时返回 None（不抛）。"""
    global _pro, _pro_init_failed
    if _pro is not None:
        return _pro
    if _pro_init_failed:
        return None

    token = None
    try:
        from stock_research import config
        token = getattr(config, "TUSHARE_TOKEN", None)
    except Exception:
        token = None
    if not token:
        import os
        token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        logger.warning("TUSHARE_TOKEN 未配置，Tushare A 股源不可用")
        _pro_init_failed = True
        return None

    try:
        import tushare as ts
        ts.set_token(token)
        _pro = ts.pro_api()
        return _pro
    except ImportError:
        logger.warning("tushare 未安装；pip install tushare")
        _pro_init_failed = True
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("tushare init failed: %s", e)
        _pro_init_failed = True
        return None


# ────────────────────────────────────────────────────────
# 代码格式工具
# ────────────────────────────────────────────────────────

def to_ts_code(code: str) -> str | None:
    """6 位代码或 600519.SS → Tushare 的 '600519.SH'。非 A 股返回 None。"""
    if not code:
        return None
    pure = code.strip().upper().split(".")[0]
    if not pure.isdigit() or len(pure) != 6:
        return None
    if pure.startswith(("60", "68", "90", "11")):       # 沪主板 / 科创 / B股 / 沪可转债
        return f"{pure}.SH"
    if pure.startswith(("00", "30", "20", "12")):        # 深主板/中小 / 创业 / B股 / 深可转债
        return f"{pure}.SZ"
    if pure.startswith(("43", "83", "87", "88", "92")):  # 北交所
        return f"{pure}.BJ"
    return None


# ────────────────────────────────────────────────────────
# 基础信息表（名称 + 行业），全市场一次拉后进程内缓存
# ────────────────────────────────────────────────────────

def _basic_table() -> dict[str, dict[str, Any]]:
    global _basic_cache
    if _basic_cache is not None:
        return _basic_cache
    pro = _get_pro()
    if not pro:
        return {}
    try:
        df = pro.stock_basic(
            exchange="", list_status="L",
            fields="ts_code,symbol,name,industry,area",
        )
        out: dict[str, dict[str, Any]] = {}
        for _, r in df.iterrows():
            out[str(r["ts_code"])] = {
                "name": r.get("name"),
                "industry": r.get("industry"),
            }
        _basic_cache = out
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("tushare stock_basic failed: %s", e)
        return {}


# ────────────────────────────────────────────────────────
# A 股行情
# ────────────────────────────────────────────────────────

def fetch_a_stock_quote(code: str) -> dict[str, Any] | None:
    """A 股最新一日行情 + 估值（daily + daily_basic）。

    字段镜像 akshare_client.fetch_a_stock_quote：
      code/name/price/change_pct/turnover/pe_ttm/pb/market_cap_yuan/circulating_cap_yuan
    额外带 ts_code/date/industry。
    注意单位：Tushare daily_basic 的 total_mv/circ_mv 单位是「万元」，
    这里 ×1e4 换算成「元」以对齐 akshare（akshare 总市值口径为元）。
    """
    pro = _get_pro()
    if not pro:
        return None
    ts_code = to_ts_code(code)
    if not ts_code:
        return None
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=14)).strftime("%Y%m%d")
        d = pro.daily(ts_code=ts_code, start_date=start, end_date=end)
        if d is None or d.empty:
            return None
        d = d.sort_values("trade_date")
        last = d.iloc[-1]
        trade_date = str(last["trade_date"])

        pe_ttm = pb = turnover = total_mv = circ_mv = None
        b = pro.daily_basic(ts_code=ts_code, trade_date=trade_date)
        if b is not None and not b.empty:
            br = b.iloc[0]
            pe_ttm = _safe_float(br.get("pe_ttm"))
            pb = _safe_float(br.get("pb"))
            turnover = _safe_float(br.get("turnover_rate"))
            total_mv = _safe_float(br.get("total_mv"))
            circ_mv = _safe_float(br.get("circ_mv"))

        meta = _basic_table().get(ts_code, {})
        return {
            "code": code,
            "ts_code": ts_code,
            "name": meta.get("name"),
            "industry": meta.get("industry"),
            "price": _safe_float(last.get("close")),
            "change_pct": _safe_float(last.get("pct_chg")),
            "turnover": turnover,
            "pe_ttm": pe_ttm,
            "pb": pb,
            "market_cap_yuan": total_mv * 1e4 if total_mv is not None else None,
            "circulating_cap_yuan": circ_mv * 1e4 if circ_mv is not None else None,
            "date": trade_date,
            "source": "tushare/daily+daily_basic",
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("tushare A quote failed for %s: %s", code, e)
        return None


def fetch_a_share_quote(code: str) -> dict[str, Any]:
    """baostock_client.fetch_a_share_quote 的契约镜像（收盘价 + 名字 + 行业）。

    返回 {code, ts_code, price, name, industry, date, source}；失败返回 {}。
    供审计二源/兜底调用方平滑替换 baostock。
    """
    q = fetch_a_stock_quote(code)
    if not q:
        return {}
    return {
        "code": q["code"],
        "ts_code": q["ts_code"],
        "price": q["price"],
        "name": q.get("name"),
        "industry": q.get("industry"),
        "date": q.get("date"),
        "source": "tushare",
    }


# ────────────────────────────────────────────────────────
# 工具
# ────────────────────────────────────────────────────────

def _safe_float(v) -> float | None:
    try:
        if v is None or v == "" or v == "-":
            return None
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None
