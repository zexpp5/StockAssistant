"""V2 Piotroski 计算器 — 简化 5 项版本（P5-Lite），落入 factor_metadata 表。

完整 Piotroski F-Score 是 9 项盈利+杠杆+运营效率指标，YoY 比较；这里先实现 5 项基础版作为
首批数据接入：
  1. 净利润 > 0        (profitable)
  2. 经营现金流 > 0    (cfo_positive)
  3. ROA 同比改善      (roa_improving)
  4. CFO > NI          (cfo_quality)  现金流质量
  5. 杠杆同比下降      (leverage_down) Total Debt / Total Equity 下降

每项 1 分，0-5 总分；写入 factor_metadata.f_score 时映射到 0-9 量级（×1.8）以与完整版兼容。
source 字段标 "yfinance_p5_lite" / "tushare_p5_lite_a_share" / "baostock_p5_lite_a_share"
  / "akshare_p5_lite_a_share"，明示是简化版与数据源。

数据源：
  - 美/港股：yfinance.Ticker.financials/balance_sheet/cashflow（最近 2 年）
  - A 股：Tushare Pro 官方季报（2026-06-05 起主源，付费、fina_indicator 直给 ROA/
          负债率、无 IP 限流）→ baostock（免费官方季报）→ akshare（东财聚合）三级兜底。
          沿革：akshare.stock_financial_abstract 按 IP 限流 220 只成功率仅 ~20%，
          2026-06-02 切 baostock；2026-06-05 升 Tushare Pro 为主源，baostock 退二线。

用法：
  python3 scripts/tools/compute_piotroski_v2.py --markets US,HK            # 算 US+HK
  python3 scripts/tools/compute_piotroski_v2.py --limit 30                 # 限量测试
  python3 scripts/tools/compute_piotroski_v2.py --markets US --symbols NVDA,MSFT  # 单点测试
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import yfinance as yf
from stock_db import get_db  # type: ignore

logger = logging.getLogger("compute_piotroski_v2")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Piotroski 5 项简化版：每项 1 分
SCORE_KEYS = ("profitable", "cfo_positive", "roa_improving", "cfo_quality", "leverage_down")


def _safe_get(df, row_name: str, col_idx: int):
    """容错读 df.loc[row_name].iloc[col_idx]，缺则返回 None。"""
    if df is None or df.empty:
        return None
    try:
        if row_name not in df.index:
            return None
        s = df.loc[row_name]
        if hasattr(s, "iloc") and col_idx < len(s):
            v = s.iloc[col_idx]
            return None if (v is None or (hasattr(v, "__class__") and v != v)) else float(v)
    except Exception:
        return None
    return None


def compute_p5_lite_yfinance(symbol: str) -> dict | None:
    """Returns {f_score: 0..5, details: {...}, source: str} 或 None（数据缺）。"""
    try:
        t = yf.Ticker(symbol)
        fin = t.financials
        bs = t.balance_sheet
        cf = t.cashflow
    except Exception as e:
        logger.debug("yfinance fetch failed %s: %s", symbol, e)
        return None
    if fin is None or fin.empty or bs is None or bs.empty:
        return None

    net_income_y0 = _safe_get(fin, "Net Income", 0)
    net_income_y1 = _safe_get(fin, "Net Income", 1)
    total_assets_y0 = _safe_get(bs, "Total Assets", 0)
    total_assets_y1 = _safe_get(bs, "Total Assets", 1)
    cfo_y0 = _safe_get(cf, "Operating Cash Flow", 0)
    total_debt_y0 = _safe_get(bs, "Total Debt", 0)
    total_debt_y1 = _safe_get(bs, "Total Debt", 1)
    equity_y0 = _safe_get(bs, "Stockholders Equity", 0) or _safe_get(bs, "Common Stock Equity", 0)
    equity_y1 = _safe_get(bs, "Stockholders Equity", 1) or _safe_get(bs, "Common Stock Equity", 1)

    details: dict[str, dict | None] = {}
    score = 0

    # 1. 净利润 > 0
    if net_income_y0 is not None:
        ok = net_income_y0 > 0
        details["profitable"] = {"value": net_income_y0, "pass": ok}
        score += int(ok)
    else:
        details["profitable"] = None

    # 2. CFO > 0
    if cfo_y0 is not None:
        ok = cfo_y0 > 0
        details["cfo_positive"] = {"value": cfo_y0, "pass": ok}
        score += int(ok)
    else:
        details["cfo_positive"] = None

    # 3. ROA improving (NI/TA YoY)
    if all(x is not None for x in (net_income_y0, net_income_y1, total_assets_y0, total_assets_y1)):
        roa_y0 = net_income_y0 / total_assets_y0 if total_assets_y0 else None
        roa_y1 = net_income_y1 / total_assets_y1 if total_assets_y1 else None
        if roa_y0 is not None and roa_y1 is not None:
            ok = roa_y0 > roa_y1
            details["roa_improving"] = {"roa_y0": roa_y0, "roa_y1": roa_y1, "pass": ok}
            score += int(ok)
    if "roa_improving" not in details:
        details["roa_improving"] = None

    # 4. CFO > NI (cash flow quality)
    if cfo_y0 is not None and net_income_y0 is not None:
        ok = cfo_y0 > net_income_y0
        details["cfo_quality"] = {"cfo": cfo_y0, "ni": net_income_y0, "pass": ok}
        score += int(ok)
    else:
        details["cfo_quality"] = None

    # 5. 杠杆同比下降（Debt/Equity）
    if all(x is not None for x in (total_debt_y0, total_debt_y1, equity_y0, equity_y1)) and equity_y0 and equity_y1:
        lev_y0 = total_debt_y0 / equity_y0
        lev_y1 = total_debt_y1 / equity_y1
        ok = lev_y0 < lev_y1
        details["leverage_down"] = {"lev_y0": lev_y0, "lev_y1": lev_y1, "pass": ok}
        score += int(ok)
    else:
        details["leverage_down"] = None

    covered = sum(1 for v in details.values() if v is not None)
    if covered == 0:
        return None
    return {
        "f_score_raw_5": score,
        "f_score_norm_9": round(score / 5.0 * 9.0, 2),  # 映射到 0-9 量级
        "covered_items": covered,
        "details": details,
        "source": "yfinance_p5_lite",
    }


def _safe_pct(v) -> float | None:
    """akshare A 股财报指标值可能是 '12.34' / '12.34%' / '--' / NaN，统一转 float."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in ("--", "nan", "None"):
        return None
    s = s.rstrip("%").replace(",", "")
    try:
        return float(s)
    except Exception:
        return None


def compute_p5_lite_akshare(symbol_with_suffix: str) -> dict | None:
    """A 股 P5-Lite — 用 akshare.stock_financial_abstract（杜邦三表融合宽表）.

    symbol_with_suffix 形如 '600089.SS' / '300919.SZ' — 截掉 suffix 给 akshare.
    指标全在一张表里，免去 join 三表。
    """
    try:
        import akshare as ak  # type: ignore
    except ImportError:
        logger.warning("akshare 未安装，A 股 P5-Lite 跳过")
        return None

    code = symbol_with_suffix.split(".")[0]
    # akshare 批量连打（220 只 active CN）会触发限流/超时，整批可掉 ~80%（2026-06-02
    # CN F-Score 覆盖只剩 6/20 事故）。数据本身可得，故 retry + 退避把瞬时失败救回来。
    df = None
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            df = ak.stock_financial_abstract(symbol=code)
            if df is not None and not df.empty:
                break
        except Exception as e:  # noqa: BLE001 — 网络/限流均退避重试
            last_err = e
        if attempt < 2:
            time.sleep(0.8 * (attempt + 1))  # 0.8s / 1.6s 退避
    if df is None or df.empty:
        if last_err is not None:
            logger.debug("akshare stock_financial_abstract %s 3 次仍失败: %s", code, last_err)
        return None

    # 报告期列（按时间倒序：最新在前）
    report_cols = [c for c in df.columns if c not in ("选项", "指标")]
    if len(report_cols) < 5:
        return None  # 数据不够算 YoY

    # 找 y0 (最新) 和 y4 (4 季度前 = 1 年前同期)
    # report_cols 形如 ['20260331', '20251231', '20250930', '20250630', '20250331', ...]
    # 取前 5 个，y0 = [0], y4 = [4]（同季度前 1 年）
    y0_col, y4_col = report_cols[0], report_cols[4]

    def _get_metric(name_substr: str, col: str) -> float | None:
        """按指标名子串匹配，取该报告期值。"""
        for _, r in df.iterrows():
            metric = str(r.get("指标", ""))
            if name_substr in metric:
                return _safe_pct(r.get(col))
        return None

    ni_y0 = _get_metric("归母净利润", y0_col)
    ni_y4 = _get_metric("归母净利润", y4_col)
    cfo_y0 = _get_metric("经营现金流量净额", y0_col)
    roa_y0 = _get_metric("总资产报酬率(ROA)", y0_col)
    roa_y4 = _get_metric("总资产报酬率(ROA)", y4_col)
    lev_y0 = _get_metric("资产负债率", y0_col)
    lev_y4 = _get_metric("资产负债率", y4_col)

    details: dict[str, dict | None] = {}
    score = 0

    # 1. 净利润 > 0
    if ni_y0 is not None:
        ok = ni_y0 > 0
        details["profitable"] = {"value": ni_y0, "pass": ok}
        score += int(ok)
    else:
        details["profitable"] = None

    # 2. CFO > 0
    if cfo_y0 is not None:
        ok = cfo_y0 > 0
        details["cfo_positive"] = {"value": cfo_y0, "pass": ok}
        score += int(ok)
    else:
        details["cfo_positive"] = None

    # 3. ROA 同比改善
    if roa_y0 is not None and roa_y4 is not None:
        ok = roa_y0 > roa_y4
        details["roa_improving"] = {"roa_y0": roa_y0, "roa_y4": roa_y4, "pass": ok}
        score += int(ok)
    else:
        details["roa_improving"] = None

    # 4. CFO > NI（现金流质量）— 单位都是元，可直接比
    if cfo_y0 is not None and ni_y0 is not None:
        ok = cfo_y0 > ni_y0
        details["cfo_quality"] = {"cfo": cfo_y0, "ni": ni_y0, "pass": ok}
        score += int(ok)
    else:
        details["cfo_quality"] = None

    # 5. 杠杆同比下降（资产负债率下降是好事）
    if lev_y0 is not None and lev_y4 is not None:
        ok = lev_y0 < lev_y4
        details["leverage_down"] = {"lev_y0": lev_y0, "lev_y4": lev_y4, "pass": ok}
        score += int(ok)
    else:
        details["leverage_down"] = None

    covered = sum(1 for v in details.values() if v is not None)
    if covered == 0:
        return None
    return {
        "f_score_raw_5": score,
        "f_score_norm_9": round(score / 5.0 * 9.0, 2),
        "covered_items": covered,
        "details": details,
        "source": "akshare_p5_lite_a_share",
        "report_periods": {"y0": y0_col, "y4": y4_col},
    }


def _score_p5_lite(ni_y0, cfo_y0, roa_y0, roa_y4, lev_y0, lev_y4, source) -> dict | None:
    """P5-Lite 5 项打分 —— akshare/baostock 共用，逻辑与原 akshare 版逐项一致。"""
    details: dict[str, dict | None] = {}
    score = 0
    # 1. 净利润 > 0
    if ni_y0 is not None:
        ok = ni_y0 > 0
        details["profitable"] = {"value": ni_y0, "pass": ok}
        score += int(ok)
    else:
        details["profitable"] = None
    # 2. CFO > 0
    if cfo_y0 is not None:
        ok = cfo_y0 > 0
        details["cfo_positive"] = {"value": cfo_y0, "pass": ok}
        score += int(ok)
    else:
        details["cfo_positive"] = None
    # 3. ROA 同比改善
    if roa_y0 is not None and roa_y4 is not None:
        ok = roa_y0 > roa_y4
        details["roa_improving"] = {"roa_y0": roa_y0, "roa_y4": roa_y4, "pass": ok}
        score += int(ok)
    else:
        details["roa_improving"] = None
    # 4. CFO > NI（现金流质量）
    if cfo_y0 is not None and ni_y0 is not None:
        ok = cfo_y0 > ni_y0
        details["cfo_quality"] = {"cfo": cfo_y0, "ni": ni_y0, "pass": ok}
        score += int(ok)
    else:
        details["cfo_quality"] = None
    # 5. 杠杆同比下降
    if lev_y0 is not None and lev_y4 is not None:
        ok = lev_y0 < lev_y4
        details["leverage_down"] = {"lev_y0": lev_y0, "lev_y4": lev_y4, "pass": ok}
        score += int(ok)
    else:
        details["leverage_down"] = None

    covered = sum(1 for v in details.values() if v is not None)
    if covered == 0:
        return None
    return {
        "f_score_raw_5": score,
        "f_score_norm_9": round(score / 5.0 * 9.0, 2),
        "covered_items": covered,
        "details": details,
        "source": source,
    }


def compute_p5_lite_baostock(symbol_with_suffix: str) -> dict | None:
    """A 股 P5-Lite —— baostock 季报取数（替换 akshare，规避 IP 限流）。

    打分逻辑与 compute_p5_lite_akshare 完全一致；只是数据源换成交易所官方季报。
    """
    try:
        from stock_research.core.baostock_client import fetch_a_share_p5_inputs  # type: ignore
    except ImportError:
        return None
    inp = fetch_a_share_p5_inputs(symbol_with_suffix)
    if not inp:
        return None
    result = _score_p5_lite(
        inp.get("ni_y0"), inp.get("cfo_y0"),
        inp.get("roa_y0"), inp.get("roa_y4"),
        inp.get("lev_y0"), inp.get("lev_y4"),
        source="baostock_p5_lite_a_share",
    )
    if result is not None:
        result["report_periods"] = inp.get("report_periods")
    return result


def compute_p5_lite_tushare(symbol_with_suffix: str) -> dict | None:
    """A 股 P5-Lite —— Tushare Pro 季报取数（2026-06-05 起主源）。

    打分逻辑与 baostock/akshare 版完全一致；Tushare fina_indicator 直接给
    ROA / 资产负债率，官方季报无 IP 限流。返 None 时调用方回退 baostock → akshare。
    """
    try:
        from stock_research.core.tushare_client import fetch_a_share_p5_inputs  # type: ignore
    except ImportError:
        return None
    inp = fetch_a_share_p5_inputs(symbol_with_suffix)
    if not inp:
        return None
    result = _score_p5_lite(
        inp.get("ni_y0"), inp.get("cfo_y0"),
        inp.get("roa_y0"), inp.get("roa_y4"),
        inp.get("lev_y0"), inp.get("lev_y4"),
        source="tushare_p5_lite_a_share",
    )
    if result is not None:
        result["report_periods"] = inp.get("report_periods")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", default="US,HK", help="逗号分隔市场代码")
    parser.add_argument("--symbols", default="", help="逗号分隔 symbol 限定（覆盖 markets）")
    parser.add_argument("--limit", type=int, default=0, help="限量测试，>0 才生效")
    args = parser.parse_args()

    conn = get_db()
    # 确认 factor_metadata 表存在（IF NOT EXISTS 兜底）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS factor_metadata (
            market            VARCHAR NOT NULL,
            symbol            VARCHAR NOT NULL,
            f_score           DOUBLE,
            value_score       DOUBLE,
            quality_score     DOUBLE,
            composite_details JSON,
            source            VARCHAR,
            computed_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (market, symbol)
        )
    """)

    if args.symbols:
        targets = []
        for s in args.symbols.split(","):
            s = s.strip().upper()
            if not s:
                continue
            row = conn.execute(
                "SELECT market, symbol FROM system_universe WHERE active=true AND symbol=?",
                [s],
            ).fetchone()
            if row:
                targets.append(row)
            else:
                logger.warning("symbol %s 未在 system_universe 中（active=true）", s)
    else:
        markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]
        placeholders = ",".join(["?"] * len(markets))
        targets = conn.execute(
            f"SELECT market, symbol FROM system_universe WHERE active=true AND market IN ({placeholders})",
            markets,
        ).fetchall()
    if args.limit:
        targets = targets[:args.limit]
    logger.info("待计算 %d 只", len(targets))

    ok = err = 0
    by_market = {}
    for market, symbol in targets:
        if market == "CN":
            # Tushare Pro 主源（付费、官方季报、fina_indicator 直给 ROA/负债率）
            # → baostock（免费官方季报）→ akshare（东财聚合）三级兜底
            result = compute_p5_lite_tushare(symbol)
            if result is None:
                result = compute_p5_lite_baostock(symbol)
            if result is None:
                result = compute_p5_lite_akshare(symbol)
        else:
            result = compute_p5_lite_yfinance(symbol)
        if result is None:
            err += 1
            continue
        conn.execute("""
            INSERT INTO factor_metadata (market, symbol, f_score, value_score, quality_score, composite_details, source, computed_at)
            VALUES (?, ?, ?, NULL, NULL, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (market, symbol) DO UPDATE SET
                f_score=excluded.f_score,
                composite_details=excluded.composite_details,
                source=excluded.source,
                computed_at=excluded.computed_at
        """, [market, symbol, result["f_score_norm_9"], json.dumps(result, ensure_ascii=False), result["source"]])
        ok += 1
        by_market[market] = by_market.get(market, 0) + 1

    logger.info("入库完成：成功 %d · 失败 %d · 按市场 %s", ok, err, by_market)
    total = conn.execute("SELECT COUNT(*) FROM factor_metadata WHERE f_score IS NOT NULL").fetchone()[0]
    logger.info("factor_metadata.f_score 总条目：%d", total)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
