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
# 指数成分
# ────────────────────────────────────────────────────────

def fetch_index_cons(index_code: str) -> list[str]:
    """指数最新成分股 ts_code 列表（取返回数据里最新 trade_date）。

    index_code 例：沪深300 000300.SH / 科创50 000688.SH / 创业板指 399006.SZ。
    index_weight 月度更新，取最近 ~45 天窗口里最新一期成分；失败返回 []。
    """
    pro = _get_pro()
    if not pro:
        return []
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=45)).strftime("%Y%m%d")
        df = pro.index_weight(index_code=index_code, start_date=start, end_date=end)
        if df is None or df.empty:
            return []
        latest = df["trade_date"].max()
        return df[df["trade_date"] == latest]["con_code"].astype(str).tolist()
    except Exception as e:  # noqa: BLE001
        logger.warning("tushare index_weight %s failed: %s", index_code, e)
        return []


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
# A 股财报 — P5-Lite F-Score 输入
# ────────────────────────────────────────────────────────

def fetch_a_share_p5_inputs(code: str) -> dict[str, Any] | None:
    """A 股 P5-Lite F-Score 的 5 项原始输入（最新季 y0 + 去年同季 y4）。

    契约镜像 baostock_client.fetch_a_share_p5_inputs，作为其升级主源：
      - Tushare fina_indicator 直接给 roa / debt_to_assets，免去 baostock 的派生
        （baostock 只给比率，需 ROA=roe/a2e、杠杆=1-1/a2e 自行推导）；
      - 官方季报、无 IP 限流。
    口径：income/cashflow/fina_indicator 均为季度累计（YTD），y0 与 y4 取相同季度同比。
    返回 ni_y0/cfo_y0/roa_y0/roa_y4/lev_y0/lev_y4/report_periods/cfo_to_np。
    下游 _score_p5_lite 全为同比/符号比较，roa/lev 单位（%）不影响打分。
    """
    pro = _get_pro()
    if not pro:
        return None
    ts_code = to_ts_code(code)
    if not ts_code:
        return None
    try:
        start = (datetime.now() - timedelta(days=365 * 3)).strftime("%Y%m%d")
        end = datetime.now().strftime("%Y%m%d")
        fi = pro.fina_indicator(
            ts_code=ts_code, start_date=start, end_date=end,
            fields="end_date,roa,debt_to_assets",
        )
        inc = pro.income(
            ts_code=ts_code, start_date=start, end_date=end,
            fields="end_date,report_type,n_income",
        )
        cf = pro.cashflow(
            ts_code=ts_code, start_date=start, end_date=end,
            fields="end_date,report_type,n_cashflow_act",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("tushare p5 fetch failed for %s: %s", code, e)
        return None

    def _index(df, val_col: str, only_consolidated: bool = False) -> dict[str, float | None]:
        """end_date → 数值；去重（Tushare 同报告期返多行）保留首个=最新公告版。"""
        out: dict[str, float | None] = {}
        if df is None or getattr(df, "empty", True):
            return out
        if only_consolidated and "report_type" in df.columns:
            df = df[df["report_type"].astype(str) == "1"]
        for _, r in df.iterrows():
            ed = str(r.get("end_date") or "")
            if not ed or ed in out:
                continue
            out[ed] = _safe_float(r.get(val_col))
        return out

    roa_map = _index(fi, "roa")
    lev_map = _index(fi, "debt_to_assets")
    ni_map = _index(inc, "n_income", only_consolidated=True)
    cfo_map = _index(cf, "n_cashflow_act", only_consolidated=True)

    # y0 = 有净利润的最近报告期；y4 = 去年同季（同月日，年-1）
    periods = sorted([d for d in ni_map if ni_map[d] is not None], reverse=True)
    if not periods:
        return None
    y0 = periods[0]
    y4 = f"{int(y0[:4]) - 1}{y0[4:]}"

    ni_y0 = ni_map.get(y0)
    cfo_y0 = cfo_map.get(y0)
    cfo_to_np = (cfo_y0 / ni_y0) if (cfo_y0 is not None and ni_y0) else None
    y4_present = any(y4 in m for m in (roa_map, lev_map, ni_map))
    return {
        "ni_y0": ni_y0,
        "cfo_y0": cfo_y0,
        "roa_y0": roa_map.get(y0),
        "roa_y4": roa_map.get(y4),
        "lev_y0": lev_map.get(y0),
        "lev_y4": lev_map.get(y4),
        "report_periods": {"y0": y0, "y4": y4 if y4_present else None},
        "cfo_to_np": cfo_to_np,
        "source": "tushare",
    }


# ────────────────────────────────────────────────────────
# A 股三表 + 日线 — akshare 兼容适配层
# （factor_model_china 的 piotroski/quality/momentum 计算逻辑不变，仅换源）
# ────────────────────────────────────────────────────────

# Tushare 英文字段 → akshare 新浪三表中文科目（供 factor_model_china 直接消费）
_INCOME_MAP = {
    "end_date": "报告日",
    "n_income": "净利润",
    "total_revenue": "营业总收入",
    "revenue": "营业收入",
    "oper_cost": "营业成本",
    "operate_profit": "营业利润",
}
_BALANCE_MAP = {
    "end_date": "报告日",
    "total_assets": "资产总计",
    "total_cur_assets": "流动资产合计",
    "total_cur_liab": "流动负债合计",
    "lt_borr": "长期借款",
    "total_share": "实收资本(或股本)",
}
_CASHFLOW_MAP = {
    "end_date": "报告日",
    "n_cashflow_act": "经营活动产生的现金流量净额",
    "c_pay_acq_const_fiolta": "购建固定资产、无形资产和其他长期资产支付的现金",
}
_STATEMENT_DISPATCH = {
    "资产负债表": "balancesheet",
    "利润表": "income",
    "现金流量表": "cashflow",
}


def fetch_a_share_report(code: str, statement: str):
    """三表年报取数，返回 akshare stock_financial_report_sina 兼容的宽表 DataFrame。

    statement ∈ {"资产负债表", "利润表", "现金流量表"}。
    列含「报告日」(YYYYMMDD) + 各中文科目，行=报告期；失败返回 None。
    report_type==1 合并报表、按 end_date 去重保留最新公告版。
    """
    pro = _get_pro()
    if not pro:
        return None
    ts_code = to_ts_code(code)
    if not ts_code:
        return None
    api = _STATEMENT_DISPATCH.get(statement)
    if not api:
        return None
    mapping = {
        "balancesheet": _BALANCE_MAP,
        "income": _INCOME_MAP,
        "cashflow": _CASHFLOW_MAP,
    }[api]
    try:
        start = (datetime.now() - timedelta(days=365 * 6)).strftime("%Y%m%d")
        end = datetime.now().strftime("%Y%m%d")
        fn = getattr(pro, api)
        df = fn(
            ts_code=ts_code, start_date=start, end_date=end,
            fields="report_type," + ",".join(mapping.keys()),
        )
        if df is None or df.empty:
            return None
        if "report_type" in df.columns:
            df = df[df["report_type"].astype(str) == "1"]
        df = df.drop_duplicates(subset=["end_date"], keep="first")
        df = df.rename(columns=mapping)
        df["报告日"] = df["报告日"].astype(str)
        keep = [c for c in mapping.values() if c in df.columns]
        out = df[keep].reset_index(drop=True)
        out.attrs["source"] = "tushare"
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("tushare report %s failed for %s: %s", statement, code, e)
        return None


def fetch_a_share_daily_qfq(code: str, start_date: str, end_date: str):
    """前复权日线，返回 akshare stock_zh_a_daily(adjust='qfq') 兼容 DataFrame。

    start_date/end_date 为 YYYYMMDD。列含 date(YYYYMMDD 字符串)+ open/high/low/close/volume。
    失败返回 None。供 factor_model_china.momentum_a_share 直接消费（其只读 date/close）。
    """
    if not _get_pro():  # 确保 token 已 set（pro_bar 依赖全局 token）
        return None
    ts_code = to_ts_code(code)
    if not ts_code:
        return None
    try:
        import tushare as ts
        df = ts.pro_bar(
            ts_code=ts_code, adj="qfq",
            start_date=start_date, end_date=end_date,
        )
        if df is None or df.empty:
            return None
        df = df.rename(columns={
            "trade_date": "date", "vol": "volume", "amount": "amount",
        })
        df["date"] = df["date"].astype(str)
        return df.reset_index(drop=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("tushare daily qfq failed for %s: %s", code, e)
        return None


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
