"""baostock A 股二源：作为 akshare 的免费 cross-check。

为什么用 baostock：
  - 完全免费、无注册、无 token（pip 装就能跑）
  - 数据直拉交易所官方接口，权威性 ≥ akshare（akshare 是聚合）
  - 当 akshare 静默失败 / 接口改字段 / 缓存毒化时，多一票看出来

时效说明：
  baostock 日线在收盘后 T+0 入库，盘中只能拿到 T-1。
  审计在 daily_refresh 08:30 跑，此时 baostock 已有 T-1 数据，akshare quote
  也是收盘价或 T-1（看市场是否开盘），两者可比。盘中跑会有 timing 错位。

API：
  fetch_a_share_quote(code) → {code, bs_code, price, name, industry, date, source}
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


_industry_cache: dict[str, dict[str, str]] | None = None
_logged_in = False


def _code_to_bs(code: str) -> str | None:
    """6 位代码或 600519.SS → 'sh.600519'。"""
    if not code:
        return None
    pure = code.strip().upper().split(".")[0]
    if not pure.isdigit() or len(pure) != 6:
        return None
    if pure.startswith(("60", "68", "9")):
        return f"sh.{pure}"
    if pure.startswith(("00", "30", "15", "16")):
        return f"sz.{pure}"
    if pure.startswith(("43", "83", "87", "88", "92")):
        return f"bj.{pure}"
    return None


def _ensure_login() -> bool:
    global _logged_in
    if _logged_in:
        return True
    try:
        import baostock as bs
        rs = bs.login()
        if rs.error_code != "0":
            logger.warning("baostock login failed: %s", rs.error_msg)
            return False
        _logged_in = True
        return True
    except ImportError:
        logger.warning("baostock 未安装，A 股二源跳过")
        return False
    except Exception as e:
        logger.warning("baostock login error: %s", e)
        return False


def _industry_table() -> dict[str, dict[str, str]]:
    """全市场行业表（约 5500 行），首次拉 1s 后缓存到进程内存。"""
    global _industry_cache
    if _industry_cache is not None:
        return _industry_cache
    if not _ensure_login():
        return {}
    try:
        import baostock as bs
        rs = bs.query_stock_industry()
        out: dict[str, dict[str, str]] = {}
        while rs.error_code == "0" and rs.next():
            r = rs.get_row_data()
            # [updateDate, code, code_name, industry, industryClassification]
            out[r[1]] = {"name": r[2], "industry": r[3]}
        _industry_cache = out
        return out
    except Exception as e:
        logger.warning("baostock industry fetch failed: %s", e)
        return {}


def fetch_a_share_quote(code: str, lookback_days: int = 10) -> dict[str, Any]:
    """A 股最新收盘价 + 名字 + 行业。非 A 股代码或拉失败返回 {}。"""
    bsc = _code_to_bs(code)
    if not bsc:
        return {}
    if not _ensure_login():
        return {}

    try:
        import baostock as bs
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            bsc, "date,code,close,pctChg",
            start_date=start, end_date=end,
            frequency="d", adjustflag="3",
        )
        rows: list[list[str]] = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return {}
        latest = rows[-1]
        try:
            price = float(latest[2])
        except (ValueError, TypeError):
            price = None
        date = latest[0]
        ind = _industry_table().get(bsc, {})
        return {
            "code": code,
            "bs_code": bsc,
            "price": price,
            "name": ind.get("name"),
            "industry": ind.get("industry"),
            "date": date,
            "source": "baostock",
        }
    except Exception as e:
        logger.warning("baostock fetch failed for %s: %s", code, e)
        return {}
