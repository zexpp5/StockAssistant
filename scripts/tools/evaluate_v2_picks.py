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

import bisect
import math
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
PRODUCTION_METRICS_START_DATE = date.fromisoformat(
    os.environ.get("STOCK_ASSISTANT_METRICS_START_DATE", "2026-05-25")
)


def _benchmark_close_cache() -> dict:
    """{(benchmark, date) -> close}, lazy populated via yfinance."""
    return {}


def _fetch_benchmark_close(
    cache: dict,
    benchmark: str,
    target_date: date,
    conn=None,
    market: str | None = None,
) -> float | None:
    """target_date 落在周末/假日时找最近的下一个交易日 close。

    优先级：price_daily（本地，由 ingest_benchmark_prices 灌入）→ yfinance 在线兜底。
    跑批环境拿不到 yfinance 时本地仍可工作。
    """
    key = (benchmark, target_date)
    if key in cache:
        return cache[key]

    # 1) 本地 price_daily 优先
    if conn is not None and market is not None:
        row = conn.execute(
            """
            SELECT close FROM price_daily
            WHERE market = ? AND symbol = ? AND trade_date >= ?
            ORDER BY trade_date ASC LIMIT 1
            """,
            [market, benchmark, target_date],
        ).fetchone()
        if row and row[0] is not None:
            cache[key] = float(row[0])
            return cache[key]

    # 2) yfinance 兜底
    try:
        import yfinance as yf
        start = target_date - timedelta(days=10)
        end = target_date + timedelta(days=10)
        df = yf.Ticker(benchmark).history(start=start, end=end, auto_adjust=False)
        if df.empty:
            cache[key] = None
            return None
        dated_closes: list[tuple[date, float]] = []
        for ts, row in df.iterrows():
            d = ts.date() if hasattr(ts, "date") else ts
            cache[(benchmark, d)] = float(row["Close"])
            dated_closes.append((d, float(row["Close"])))
        for d, close in sorted(dated_closes):
            if d >= target_date:
                cache[key] = close
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


def _trading_days(conn, market: str) -> list[date]:
    """该市场交易日历 —— 用 benchmark 在 price_daily 的 trade_date（benchmark 每日灌，最全）。"""
    benchmark = BENCHMARK_BY_MARKET.get(str(market).upper())
    if not benchmark:
        return []
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM price_daily WHERE market = ? AND symbol = ? ORDER BY trade_date",
        [market, benchmark],
    ).fetchall()
    return [r[0] for r in rows]


def _nth_trading_day_after(days: list[date], start: date, n: int) -> date | None:
    """start 之后第 n 个交易日（第 N 个交易日口径，非 +N 自然日，避免 5d/20d 跨周末错位）。"""
    i = bisect.bisect_right(days, start)
    idx = i + n - 1
    return days[idx] if 0 <= idx < len(days) else None


def _close_on(conn, market: str, symbol: str, d: date) -> float | None:
    """price_daily 中 symbol 在 d 当天的收盘（精确日；缺失或非有限值返回 None，根治 NaN 入库）。"""
    row = conn.execute(
        "SELECT close FROM price_daily WHERE market = ? AND symbol = ? AND trade_date = ?",
        [market, symbol, d],
    ).fetchone()
    if not row or row[0] is None:
        return None
    v = float(row[0])
    return v if math.isfinite(v) else None


def main() -> int:
    db_path = os.environ.get("STOCK_DB_PATH") or str(REPO / "stock_history_v2.duckdb")
    conn = duckdb.connect(db_path)
    try:
        today = date.today()
        cutoff = max(today - timedelta(days=LOOKBACK_DAYS), PRODUCTION_METRICS_START_DATE)
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
            print(f"evaluate_v2_picks: no production runs on/after {cutoff}")
            return 0

        bench_cache = _benchmark_close_cache()
        wrote = 0
        backfilled = 0
        skipped_immature = 0
        # 2026-06-02 幂等清除 non-buy outcomes:
        # 历史 evaluate_v2_picks 无 signal 过滤，把 watch / avoid 也写进 outcomes,
        # alpha 统计因此被污染。每次跑都重新对齐 picks.signal:
        # 凡当前 picks.signal != 'buy' 的 (run_id, market, symbol) 一律删 outcome。
        conn.execute("""
            DELETE FROM pick_outcomes
            WHERE (run_id, market, symbol) IN (
              SELECT run_id, market, symbol FROM recommendation_picks
              WHERE COALESCE(signal, 'buy') != 'buy'
            )
        """)

        trading_days_cache: dict[str, list[date]] = {}
        for run_id, run_date in runs:
            picks = conn.execute(
                """
                SELECT market, symbol FROM recommendation_picks
                WHERE run_id = ?
                  AND LOWER(COALESCE(signal, rating, '')) IN ('buy', 'strong_buy')
                """,
                [run_id],
            ).fetchall()
            for market, symbol in picks:
                mkey = str(market).upper()
                # 入场价 = 推荐日（run_date）收盘价 —— 统一可复现基准点。
                # entry_price 是写 run 时的盘中价，系统性偏离收盘（例 META 6-01 entry 632.51 vs 收盘 600.47），
                # 会让 alpha 虚高；双源验证真实严筛 1D 为 +0.54%，而 entry_price 口径虚高到 +1.65%。
                entry_close = _close_on(conn, market, symbol, run_date)
                if entry_close is None or entry_close <= 0:
                    continue
                benchmark = BENCHMARK_BY_MARKET.get(mkey)
                if mkey not in trading_days_cache:
                    trading_days_cache[mkey] = _trading_days(conn, market)
                days = trading_days_cache[mkey]
                bench_entry = _close_on(conn, market, benchmark, run_date) if benchmark else None
                for horizon_name, horizon_days in HORIZONS.items():
                    # 第 N 个交易日（非 +N 自然日）
                    outcome_date = _nth_trading_day_after(days, run_date, horizon_days)
                    if outcome_date is None or outcome_date > today:
                        skipped_immature += 1
                        continue
                    # 幂等：已算且 alpha 有限就跳过。换口径重跑时先 DELETE 历史 outcome，使其全部重算。
                    existing = conn.execute(
                        "SELECT alpha_pct FROM pick_outcomes WHERE run_id=? AND market=? AND symbol=? AND horizon=?",
                        [run_id, market, symbol, horizon_name],
                    ).fetchone()
                    if existing and existing[0] is not None and math.isfinite(float(existing[0])):
                        continue
                    stock_exit = _close_on(conn, market, symbol, outcome_date)
                    if stock_exit is None:
                        continue
                    return_pct = (stock_exit / entry_close - 1) * 100
                    benchmark_pct = None
                    if benchmark and bench_entry and bench_entry > 0:
                        bench_exit = _close_on(conn, market, benchmark, outcome_date)
                        if bench_exit is not None and bench_exit > 0:
                            bp = (bench_exit / bench_entry - 1) * 100
                            benchmark_pct = bp if math.isfinite(bp) else None
                    alpha_pct = None
                    if benchmark_pct is not None and math.isfinite(return_pct):
                        a = return_pct - benchmark_pct
                        alpha_pct = a if math.isfinite(a) else None
                    is_success = bool(alpha_pct is not None and alpha_pct > 0)
                    # DELETE+INSERT：换口径时旧行被新口径覆盖（有效行已在上面幂等跳过）。
                    conn.execute(
                        "DELETE FROM pick_outcomes WHERE run_id=? AND market=? AND symbol=? AND horizon=?",
                        [run_id, market, symbol, horizon_name],
                    )
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
        latest_strategy = conn.execute(
            """
            SELECT strategy_version
            FROM recommendation_runs
            WHERE universe_scope = 'system_tech_universe' AND status = 'generated'
            ORDER BY generated_at DESC
            LIMIT 1
            """
        ).fetchone()
        latest_strategy_version = str(latest_strategy[0]) if latest_strategy and latest_strategy[0] else None
        summary = conn.execute(
            """
            SELECT po.horizon, COUNT(*) AS n_total,
                   SUM(CASE WHEN po.is_success THEN 1 ELSE 0 END) AS n_win,
                   AVG(po.alpha_pct) AS avg_alpha
            FROM pick_outcomes po
            JOIN recommendation_runs rr ON rr.run_id = po.run_id
            WHERE rr.universe_scope = 'system_tech_universe'
              AND rr.run_date >= ?
              AND (? IS NULL OR rr.strategy_version = ?)
            GROUP BY po.horizon ORDER BY po.horizon
            """,
            [PRODUCTION_METRICS_START_DATE, latest_strategy_version, latest_strategy_version],
        ).fetchall()
        print(
            f"evaluate_v2_picks: wrote={wrote} backfilled={backfilled} skipped_immature={skipped_immature} "
            f"metrics_start={PRODUCTION_METRICS_START_DATE} strategy={latest_strategy_version or 'all'}"
        )
        for horizon, n_total, n_win, avg_alpha in summary:
            win_rate = (n_win / n_total * 100) if n_total else 0
            avg_str = f"{avg_alpha:+.2f}%" if avg_alpha is not None else "—"
            print(f"  {horizon}: n={n_total} win={n_win} ({win_rate:.0f}%) avg_alpha={avg_str}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
