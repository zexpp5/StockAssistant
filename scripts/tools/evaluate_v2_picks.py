#!/usr/bin/env python3
"""Compute 1d/5d/20d alpha for V2 recommendation_picks, write pick_outcomes.

Per the 2026-05-20 user plan: 做策略验证最小闭环 — 每天更新 pick_outcomes 让产品
能给出"成熟样本 N/M"，证明（或证伪）模型有效性。

逻辑：
  - 扫过去 70 天 recommendation_runs（universe_scope='system_tech_universe'）
  - 对每条 recommendation_picks (run_id, market, symbol)：
      - entry_price = picks.entry_price（写 run 时的价）
      - 当 (run_date + 1d / 5d / 20d) 已成熟时，从 price_daily 取该日 close 算 return_pct
      - benchmark: SPY/HSI/CSI300 同窗口 close 比较；走 yfinance（缓存到内存避免重复拉）
      - alpha_pct = return_pct - benchmark_pct
  - upsert (run_id, market, symbol, horizon) → pick_outcomes

调度位置：daily_refresh.sh --morning（每天跑，幂等；已成熟的样本不会再算）。
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import duckdb  # noqa: E402

HORIZONS = {"1d": 1, "5d": 5, "20d": 20}
BENCHMARK_BY_MARKET = {"US": "SPY", "HK": "^HSI", "CN": "000300.SS"}
LOOKBACK_DAYS = 70  # 扫过去 70 天的 run


def _benchmark_close_cache() -> dict:
    """{(benchmark, date) -> close}, lazy populated via yfinance."""
    return {}


def _fetch_benchmark_close(cache: dict, benchmark: str, target_date: date) -> float | None:
    """target_date 落在周末/假日时找最近的下一个交易日 close。"""
    key = (benchmark, target_date)
    if key in cache:
        return cache[key]
    try:
        import yfinance as yf
        # 拉一个窗口（前后各 7 天）：覆盖周末漏值 + 单 ticker 多窗口缓存
        start = target_date - timedelta(days=10)
        end = target_date + timedelta(days=10)
        df = yf.Ticker(benchmark).history(start=start, end=end, auto_adjust=False)
        if df.empty:
            cache[key] = None
            return None
        # 按日期升序存入缓存
        dated_closes: list[tuple[date, float]] = []
        for ts, row in df.iterrows():
            d = ts.date() if hasattr(ts, "date") else ts
            cache[(benchmark, d)] = float(row["Close"])
            dated_closes.append((d, float(row["Close"])))
        # 找 target_date 当天或之后第一个有数据的交易日
        for d, close in sorted(dated_closes):
            if d >= target_date:
                cache[key] = close  # 周末 target → cache 给它一个近邻值
                return close
        cache[key] = None
        return None
    except Exception:
        cache[key] = None
        return None


def _next_trade_day_close(conn, market: str, symbol: str, target_date: date) -> tuple[date, float] | None:
    """price_daily 取 >= target_date 的第一个 trade_date 的 close。"""
    row = conn.execute(
        """
        SELECT trade_date, close FROM price_daily
        WHERE market = ? AND symbol = ? AND trade_date >= ?
        ORDER BY trade_date ASC LIMIT 1
        """,
        [market, symbol, target_date],
    ).fetchone()
    if not row or row[1] is None:
        return None
    return row[0], float(row[1])


def main() -> int:
    db_path = os.environ.get("STOCK_DB_PATH") or str(REPO / "stock_history_v2.duckdb")
    conn = duckdb.connect(db_path)
    try:
        today = date.today()
        cutoff = today - timedelta(days=LOOKBACK_DAYS)
        runs = conn.execute(
            """
            SELECT run_id, run_date FROM recommendation_runs
            WHERE universe_scope = 'system_tech_universe'
              AND status = 'generated' AND run_date >= ?
            ORDER BY run_date
            """,
            [cutoff],
        ).fetchall()
        if not runs:
            print(f"evaluate_v2_picks: no runs in last {LOOKBACK_DAYS} days")
            return 0

        bench_cache = _benchmark_close_cache()
        wrote = 0
        skipped_immature = 0
        for run_id, run_date in runs:
            picks = conn.execute(
                """
                SELECT market, symbol, entry_price FROM recommendation_picks
                WHERE run_id = ?
                """,
                [run_id],
            ).fetchall()
            for market, symbol, entry_price in picks:
                if entry_price is None or entry_price <= 0:
                    continue
                benchmark = BENCHMARK_BY_MARKET.get(str(market).upper())
                for horizon_name, horizon_days in HORIZONS.items():
                    target_date = run_date + timedelta(days=horizon_days)
                    if target_date > today:
                        skipped_immature += 1
                        continue
                    # 跳过已写过的
                    existing = conn.execute(
                        "SELECT 1 FROM pick_outcomes WHERE run_id=? AND market=? AND symbol=? AND horizon=?",
                        [run_id, market, symbol, horizon_name],
                    ).fetchone()
                    if existing:
                        continue
                    # 标的当期 close
                    stock_row = _next_trade_day_close(conn, market, symbol, target_date)
                    if not stock_row:
                        continue
                    outcome_date, stock_close = stock_row
                    return_pct = (stock_close / float(entry_price) - 1) * 100
                    # 基准
                    benchmark_pct = None
                    if benchmark:
                        bench_entry = _fetch_benchmark_close(bench_cache, benchmark, run_date)
                        bench_exit = _fetch_benchmark_close(bench_cache, benchmark, outcome_date)
                        if bench_entry and bench_exit and bench_entry > 0:
                            benchmark_pct = (bench_exit / bench_entry - 1) * 100
                    alpha_pct = (return_pct - benchmark_pct) if benchmark_pct is not None else None
                    is_success = bool(alpha_pct is not None and alpha_pct > 0)
                    conn.execute(
                        """
                        INSERT INTO pick_outcomes (
                            run_id, market, symbol, horizon, outcome_date,
                            return_pct, benchmark_symbol, benchmark_pct, alpha_pct,
                            is_success, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        [run_id, market, symbol, horizon_name, outcome_date,
                         round(return_pct, 4), benchmark,
                         round(benchmark_pct, 4) if benchmark_pct is not None else None,
                         round(alpha_pct, 4) if alpha_pct is not None else None,
                         is_success],
                    )
                    wrote += 1

        # 汇总成熟样本 / 总样本，供 morning_brief surface
        summary = conn.execute(
            """
            SELECT horizon, COUNT(*) AS n_total,
                   SUM(CASE WHEN is_success THEN 1 ELSE 0 END) AS n_win,
                   AVG(alpha_pct) AS avg_alpha
            FROM pick_outcomes
            GROUP BY horizon ORDER BY horizon
            """
        ).fetchall()
        print(f"evaluate_v2_picks: wrote={wrote} skipped_immature={skipped_immature}")
        for horizon, n_total, n_win, avg_alpha in summary:
            win_rate = (n_win / n_total * 100) if n_total else 0
            avg_str = f"{avg_alpha:+.2f}%" if avg_alpha is not None else "—"
            print(f"  {horizon}: n={n_total} win={n_win} ({win_rate:.0f}%) avg_alpha={avg_str}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
