"""V2 Piotroski 计算器 — 简化 5 项版本（P5-Lite），落入 factor_metadata 表。

完整 Piotroski F-Score 是 9 项盈利+杠杆+运营效率指标，YoY 比较；这里先实现 5 项基础版作为
首批数据接入：
  1. 净利润 > 0        (profitable)
  2. 经营现金流 > 0    (cfo_positive)
  3. ROA 同比改善      (roa_improving)
  4. CFO > NI          (cfo_quality)  现金流质量
  5. 杠杆同比下降      (leverage_down) Total Debt / Total Equity 下降

每项 1 分，0-5 总分；写入 factor_metadata.f_score 时映射到 0-9 量级（×1.8）以与完整版兼容。
source 字段标 "yfinance_p5_lite" 或 "akshare_p5_lite_a_share"，明示是简化版。

数据源：
  - 美/港股：yfinance.Ticker.financials/balance_sheet/cashflow（最近 2 年）
  - A 股：暂未实现（akshare 财报接口结构差异较大，留 TODO；接入 baostock_client.py 后激活）

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
    logger.info("待计算 %d 只（A 股暂用 yfinance 不支持，会自动跳过）", len(targets))

    ok = err = skip_a = 0
    for market, symbol in targets:
        if market == "CN":
            skip_a += 1
            continue
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

    logger.info("入库完成：成功 %d · 失败 %d · A股跳过 %d（暂不支持）", ok, err, skip_a)
    total = conn.execute("SELECT COUNT(*) FROM factor_metadata WHERE f_score IS NOT NULL").fetchone()[0]
    logger.info("factor_metadata.f_score 总条目：%d", total)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
