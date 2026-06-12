#!/usr/bin/env python3
"""盈利预期上修信号采集（§19.3 第二步,SHADOW_RESEARCH_ONLY）。

成长股最稳健的信号是「盈利预期上修」。yfinance 的 eps_trend 自带
7/30/60/90 天前的一致预期值 + eps_revisions 的上/下修分析师家数,
所以该因子【当天就能算】,不需要先攒一个月 PIT 历史。

同时每天把快照落库 analyst_estimate_snapshots —— 自有 PIT 历史不依赖
Yahoo 的回看窗口,未来可做更长周期/自定义口径的修正因子与回测。

源的选择(实测 2026-06-12):
  - FMP /analyst-estimates 与历史端点同限额(~7-8 次/分钟即 402),
    140 只宇宙扛不住 → 弃用为主源(memory: project_fmp_historical_quota_limit)。
  - yfinance 无此问题,且直接给修正历史 → 主源。
  - 美股 only;A股/港股盈利预期源另案(Tushare report_rc 需更高档位)。

产出 data/latest/earnings_revision_signals.json:
  revision_pct_0y/1y_30d(幅度) + breadth_30d(广度) + revision_score(0-100,
  v0 启发式,只供 shadow 变体试用,未经独立 IC 验证 —— 用前先看清这句)。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import get_db  # noqa: E402

OUT_JSON = REPO / "data" / "latest" / "earnings_revision_signals.json"

# 2026-06-12 重建:旧 FMP 版 schema(fiscal_date 键)只存过一次冒烟测试数据,直接换代
TABLE_DDL = """
CREATE TABLE IF NOT EXISTS analyst_estimate_snapshots (
    snapshot_date DATE NOT NULL,
    market VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    period VARCHAR NOT NULL,
    eps_current DOUBLE,
    eps_7d_ago DOUBLE,
    eps_30d_ago DOUBLE,
    eps_60d_ago DOUBLE,
    eps_90d_ago DOUBLE,
    up_last7 INTEGER,
    up_last30 INTEGER,
    down_last7 INTEGER,
    down_last30 INTEGER,
    source VARCHAR,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (snapshot_date, market, symbol, period)
)
"""

PERIODS = ("0q", "+1q", "0y", "+1y")


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        v = float(value)
        return v if v == v else None
    except Exception:
        return None


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        v = int(value)
        return v
    except Exception:
        return None


def load_us_universe(conn) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT symbol FROM system_universe
        WHERE market = 'US' AND active
        ORDER BY symbol
        """
    ).fetchall()
    return [str(r[0]) for r in rows]


def fetch_symbol(symbol: str) -> list[dict[str, Any]]:
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    try:
        trend = ticker.eps_trend
        revisions = ticker.eps_revisions
    except Exception:
        return []
    if trend is None or getattr(trend, "empty", True):
        return []
    rows: list[dict[str, Any]] = []
    for period in PERIODS:
        if period not in trend.index:
            continue
        t = trend.loc[period]
        r = revisions.loc[period] if (
            revisions is not None and not getattr(revisions, "empty", True)
            and period in revisions.index
        ) else {}
        rows.append({
            "period": period,
            "eps_current": _as_float(t.get("current")),
            "eps_7d_ago": _as_float(t.get("7daysAgo")),
            "eps_30d_ago": _as_float(t.get("30daysAgo")),
            "eps_60d_ago": _as_float(t.get("60daysAgo")),
            "eps_90d_ago": _as_float(t.get("90daysAgo")),
            "up_last7": _as_int(r.get("upLast7days") if hasattr(r, "get") else None),
            "up_last30": _as_int(r.get("upLast30days") if hasattr(r, "get") else None),
            "down_last7": _as_int(r.get("downLast7Days") if hasattr(r, "get") else None),
            "down_last30": _as_int(r.get("downLast30days") if hasattr(r, "get") else None),
        })
    return rows


def collect(conn, symbols: list[str], snapshot_date: date, *,
            sleep_sec: float = 0.25) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]]]:
    fetched = 0
    failed = 0
    inserted = 0
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        rows = fetch_symbol(symbol)
        if not rows:
            failed += 1
            continue
        fetched += 1
        by_symbol[symbol] = rows
        for row in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO analyst_estimate_snapshots
                (snapshot_date, market, symbol, period, eps_current, eps_7d_ago,
                 eps_30d_ago, eps_60d_ago, eps_90d_ago, up_last7, up_last30,
                 down_last7, down_last30, source, fetched_at)
                VALUES (?, 'US', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'yfinance/eps_trend', ?)
                """,
                [
                    snapshot_date, symbol, row["period"], row["eps_current"],
                    row["eps_7d_ago"], row["eps_30d_ago"], row["eps_60d_ago"],
                    row["eps_90d_ago"], row["up_last7"], row["up_last30"],
                    row["down_last7"], row["down_last30"], datetime.now(),
                ],
            )
            inserted += 1
        time.sleep(sleep_sec)
    return {"symbols": len(symbols), "fetched": fetched, "failed": failed, "rows": inserted}, by_symbol


def _revision_pct(current: float | None, ago: float | None) -> float | None:
    if current is None or ago is None or ago == 0:
        return None
    return (current - ago) / abs(ago) * 100.0


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def build_signals(by_symbol: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for symbol, rows in sorted(by_symbol.items()):
        per = {row["period"]: row for row in rows}
        r0y = per.get("0y") or {}
        r1y = per.get("+1y") or {}
        rev_0y = _revision_pct(r0y.get("eps_current"), r0y.get("eps_30d_ago"))
        rev_1y = _revision_pct(r1y.get("eps_current"), r1y.get("eps_30d_ago"))
        up30 = (r0y.get("up_last30") or 0) + (r1y.get("up_last30") or 0)
        down30 = (r0y.get("down_last30") or 0) + (r1y.get("down_last30") or 0)
        breadth = (up30 - down30) / max(up30 + down30, 1)
        rev_values = [v for v in (rev_0y, rev_1y) if v is not None]
        if not rev_values and not (up30 or down30):
            continue
        rev_avg = sum(rev_values) / len(rev_values) if rev_values else 0.0
        # v0 启发式打分:幅度 ±10% 封顶贡献 ±40 分,广度贡献 ±10 分,中性 50。
        # 未经独立 IC 验证,只供 shadow 变体试用。
        score = round(_clip(50.0 + 4.0 * _clip(rev_avg, -10.0, 10.0) + 10.0 * breadth, 0.0, 100.0), 2)
        signals.append({
            "symbol": symbol,
            "revision_pct_0y_30d": round(rev_0y, 2) if rev_0y is not None else None,
            "revision_pct_1y_30d": round(rev_1y, 2) if rev_1y is not None else None,
            "up_last30": up30,
            "down_last30": down30,
            "breadth_30d": round(breadth, 3),
            "revision_score": score,
        })
    signals.sort(key=lambda s: -s["revision_score"])
    return signals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-date", default=None, help="默认今天;只用于补采测试")
    parser.add_argument("--limit", type=int, default=0, help=">0 时只采前 N 只(冒烟测试)")
    args = parser.parse_args()

    snapshot_date = (
        date.fromisoformat(args.snapshot_date) if args.snapshot_date else date.today()
    )
    conn = get_db()
    try:
        # 旧 FMP 版 schema 只存过 2026-06-12 冒烟测试的 4 行,检测到就换代重建
        legacy = conn.execute(
            """
            SELECT count(*) FROM information_schema.columns
            WHERE table_name = 'analyst_estimate_snapshots' AND column_name = 'fiscal_date'
            """
        ).fetchone()[0]
        if legacy:
            conn.execute("DROP TABLE analyst_estimate_snapshots")
        conn.execute(TABLE_DDL)
        symbols = load_us_universe(conn)
        if args.limit > 0:
            symbols = symbols[: args.limit]
        stats, by_symbol = collect(conn, symbols, snapshot_date)
    finally:
        conn.close()

    signals = build_signals(by_symbol)
    payload = {
        "schema_version": 2,
        "safety_boundary": "SHADOW_RESEARCH_ONLY",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot_date": str(snapshot_date),
        "market": "US",
        "status": "READY" if signals else "NO_DATA",
        "source": "yfinance/eps_trend+eps_revisions",
        "n_signals": len(signals),
        "signals": signals,
        "notes": [
            "盈利预期上修 = 当前/来年 fiscal year 的 EPS 一致预期 30 天变化幅度 + 上/下修分析师广度。",
            "revision_score 是 v0 启发式(中性50),未经独立 IC 验证,只供 shadow 变体试用。",
            "快照同时落库 analyst_estimate_snapshots 攒自有 PIT 历史;美股 only。",
        ],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": payload["status"],
        "n_signals": payload["n_signals"],
        **stats,
        "output_json": str(OUT_JSON),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
