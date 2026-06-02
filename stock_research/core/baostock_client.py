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
  fetch_a_share_p5_inputs(code) → {ni_y0, cfo_y0, roa_y0, roa_y4, lev_y0, lev_y4, ...}
      P5-Lite F-Score 的 5 项原始输入（净利润/CFO/ROA YoY/杠杆 YoY）。
      2026-06-02 替换 akshare.stock_financial_abstract —— 后者按 IP 限流，220 只批量
      连打成功率仅 ~20%。baostock 拉交易所官方季报，无此封禁。
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


def _to_float(v: Any) -> float | None:
    try:
        f = float(v)
        return f
    except (ValueError, TypeError):
        return None


def _stmt_row(fn: Any, bsc: str, year: int, quarter: int) -> dict[str, str] | None:
    """拉单季单表，返回 {field: value} dict；无数据返回 None。"""
    try:
        rs = fn(code=bsc, year=year, quarter=quarter)
    except Exception as e:  # noqa: BLE001
        logger.debug("baostock %s %s %sQ%s 失败: %s", fn.__name__, bsc, year, quarter, e)
        return None
    while rs.error_code == "0" and rs.next():
        return dict(zip(rs.fields, rs.get_row_data()))
    return None


def fetch_a_share_p5_inputs(code: str) -> dict[str, Any] | None:
    """A 股 P5-Lite F-Score 的 5 项原始输入（最新季 y0 + 去年同季 y4）。

    返回 ni_y0/cfo_y0/roa_y0/roa_y4/lev_y0/lev_y4，组装与打分留在
    compute_piotroski_v2.compute_p5_lite_baostock，本函数只负责取数。

    derive 说明（baostock 无直接 ROA / liabilityToAsset 个别季返脏值）：
      ROA = roeAvg / assetToEquity（= ROE × 权益/资产）
      杠杆 = 1 - 1/assetToEquity（内部一致，比直接读 liabilityToAsset 稳）
      CFO  = CFOToNP × netProfit（baostock 现金流表只给比率）
    """
    bsc = _code_to_bs(code)
    if not bsc or not _ensure_login():
        return None
    try:
        import baostock as bs
    except ImportError:
        return None

    now = datetime.now()
    # 候选报告期：近 3 年全部季度，新→旧
    cands = sorted(
        [(y, q) for y in range(now.year, now.year - 3, -1) for q in (4, 3, 2, 1)],
        reverse=True,
    )
    # 扫到最新一个有净利润数据的季作为 y0
    y0 = None
    p0_row = None
    for y, q in cands:
        if (y, q) > (now.year, (now.month - 1) // 3 + 1):
            continue  # 跳过未来季度
        row = _stmt_row(bs.query_profit_data, bsc, y, q)
        if row and _to_float(row.get("netProfit")) is not None:
            y0, p0_row = (y, q), row
            break
    if y0 is None or p0_row is None:
        return None
    y4 = (y0[0] - 1, y0[1])  # 去年同季

    p4_row = _stmt_row(bs.query_profit_data, bsc, *y4)
    b0_row = _stmt_row(bs.query_balance_data, bsc, *y0)
    b4_row = _stmt_row(bs.query_balance_data, bsc, *y4)
    c0_row = _stmt_row(bs.query_cash_flow_data, bsc, *y0)

    ni_y0 = _to_float(p0_row.get("netProfit"))
    cfo_to_np = _to_float(c0_row.get("CFOToNP")) if c0_row else None
    cfo_y0 = cfo_to_np * ni_y0 if (cfo_to_np is not None and ni_y0 is not None) else None

    def _roa(profit_row: dict | None, bal_row: dict | None) -> float | None:
        if not profit_row or not bal_row:
            return None
        roe = _to_float(profit_row.get("roeAvg"))
        a2e = _to_float(bal_row.get("assetToEquity"))
        if roe is None or not a2e:  # a2e 为 0/None 都跳过
            return None
        return roe / a2e

    def _lev(bal_row: dict | None) -> float | None:
        if not bal_row:
            return None
        a2e = _to_float(bal_row.get("assetToEquity"))
        if not a2e:
            return None
        return 1.0 - 1.0 / a2e

    return {
        "ni_y0": ni_y0,
        "cfo_y0": cfo_y0,
        "roa_y0": _roa(p0_row, b0_row),
        "roa_y4": _roa(p4_row, b4_row),
        "lev_y0": _lev(b0_row),
        "lev_y4": _lev(b4_row),
        "report_periods": {"y0": p0_row.get("statDate"),
                           "y4": p4_row.get("statDate") if p4_row else None},
        "cfo_to_np": cfo_to_np,
    }
