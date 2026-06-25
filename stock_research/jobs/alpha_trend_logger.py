"""每日记一笔生产推荐 alpha → data/latest/alpha_trend.json，攒走势线。

只读 strategy_eval（生产 picks 成熟样本），不改打分、不推送。每天一行，
按日期幂等（同日重跑覆盖当天那条）。配合 launchd 每早跑一次即可。

记录：US/HK × 1d/5d 的 n / 平均 alpha% / 胜率% / 样本档位。
另记 US 新旧公式双轨：从 factor_snapshot_universe 全量池各自独立选 Top20，
再算 1d/5d alpha，避免在任一方已筛过的 picks 里重排造成污染。
口径与 dashboard、手动复算完全同源（strategy_eval.mature_samples + summarize），
起点固定 PRODUCTION_METRICS_START（[[project_metrics_cutoff_2026-05-25]]）。

用法:
  python3 -m stock_research.jobs.alpha_trend_logger            # 记当天
  python3 -m stock_research.jobs.alpha_trend_logger --dry-run  # 只打印不写
  python3 -m stock_research.jobs.alpha_trend_logger --show     # 打印已攒走势
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[2]
_DB = _REPO / "stock_history_v2.duckdb"
OUT = _REPO / "data" / "latest" / "alpha_trend.json"
METRICS_START = "2026-05-25"   # 生产 cutoff，别动
MARKETS = ("US", "HK", "A")
HORIZONS = ("1d", "5d")
DUAL_TRACK_MARKET = "US"
DUAL_TRACK_TOP_N = 20
DUAL_TRACK_FORMULAS = {
    "legacy_baseline": "prod_recheck",
    "val_down_grade": "val_down_grade",
}
US_RECOMMENDABLE_ELIGIBILITY = {"buyable", "research_only"}


def _connect():
    """只读连库，撞 enhancement_refresh 写锁退避重试。"""
    import duckdb
    for _ in range(20):
        try:
            return duckdb.connect(str(_DB), read_only=True)
        except Exception:
            time.sleep(2)
    return None


def _table_columns(conn: Any, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
    except Exception:
        return set()


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _horizon_days(horizon: str) -> int:
    return int(str(horizon).lower().replace("d", ""))


def _safe_return(start: float | None, end: float | None) -> float | None:
    try:
        s = float(start)
        e = float(end)
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    return (e / s - 1.0) * 100.0


def _summarize_alpha(samples: list[float]) -> dict[str, Any]:
    if not samples:
        return {"n": 0}
    return {
        "n": len(samples),
        "avg_alpha_pct": round(sum(samples) / len(samples), 4),
        "win_rate_pct": round(sum(1 for x in samples if x > 0) / len(samples) * 100.0, 2),
        "worst_alpha_pct": round(min(samples), 4),
    }


def _us_trading_dates(conn: Any) -> list[date]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date
        FROM price_daily
        WHERE market='US' AND symbol IN ('SPY','QQQ') AND close IS NOT NULL
        ORDER BY trade_date
        """
    ).fetchall()
    return [d for (d,) in rows if isinstance(d, date)]


def _entry_exit_dates(trading_dates: list[date], run_date: date, horizon: str) -> tuple[date, date] | None:
    start_idx = None
    for i, d in enumerate(trading_dates):
        if d >= run_date:
            start_idx = i
            break
    if start_idx is None:
        return None
    exit_idx = start_idx + _horizon_days(horizon)
    if exit_idx >= len(trading_dates):
        return None
    return trading_dates[start_idx], trading_dates[exit_idx]


def _close_map(conn: Any, market: str, trade_date: date) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT symbol, close
        FROM price_daily
        WHERE market=? AND trade_date=? AND close IS NOT NULL
        """,
        [market, trade_date],
    ).fetchall()
    out: dict[str, float] = {}
    for symbol, close in rows:
        try:
            out[str(symbol).upper()] = float(close)
        except (TypeError, ValueError):
            continue
    return out


def _rank_full_pool_us(conn: Any, run_date_value: date, formula_key: str) -> list[dict[str, Any]]:
    """从 factor_snapshot_universe 全池按指定公式独立选 US TopN。"""
    import scripts.tools.replay_weight_variants as rp
    from stock_research.core.analyst_grade_factor import (
        NEUTRAL_GRADE_SCORE,
        build_grade_score_map,
    )

    cols = _table_columns(conn, "factor_snapshot_universe")
    grade_expr = "grade" if "grade" in cols else "NULL AS grade"
    rows = conn.execute(
        f"""
        SELECT symbol, momentum, valuation, reversal, data_usability, f_score,
               {grade_expr}, eligibility
        FROM factor_snapshot_universe
        WHERE market=? AND run_date=?
        """,
        [DUAL_TRACK_MARKET, run_date_value],
    ).fetchall()
    if not rows:
        return []

    missing_grade_symbols = {
        str(sym).upper()
        for sym, *_rest, grade, eligibility in rows
        if grade is None and str(eligibility or "") in US_RECOMMENDABLE_ELIGIBILITY
    }
    grade_map = (
        build_grade_score_map(conn, missing_grade_symbols, run_date_value, market=DUAL_TRACK_MARKET)
        if missing_grade_symbols else {}
    )
    weights = rp.weights_for_market(rp.VARIANTS[formula_key], DUAL_TRACK_MARKET)
    ranked = []
    for sym, momentum, valuation, reversal, data_usability, f_score, grade, eligibility in rows:
        if str(eligibility or "") not in US_RECOMMENDABLE_ELIGIBILITY:
            continue
        symbol = str(sym).upper()
        scores = {
            "momentum": momentum,
            "valuation": valuation,
            "reversal": reversal,
            "data_usability": data_usability,
            "f_score": f_score,
            "grade": grade if grade is not None else grade_map.get(symbol, NEUTRAL_GRADE_SCORE),
        }
        score, missing = rp.variant_score(scores, weights)
        ranked.append({
            "symbol": symbol,
            "score": score,
            "missing_factors": missing,
        })
    ranked.sort(key=lambda r: (-r["score"], r["symbol"]))
    return ranked[:DUAL_TRACK_TOP_N]


def _dual_track_us(conn: Any) -> dict[str, Any]:
    """US 新旧公式全池独立选 TopN 的前向 alpha 对照。"""
    trading_dates = _us_trading_dates(conn)
    if not trading_dates:
        return {"status": "no_benchmark_dates"}
    run_dates = [
        d for (d,) in conn.execute(
            """
            SELECT DISTINCT run_date
            FROM factor_snapshot_universe
            WHERE market=? AND run_date>=?
            ORDER BY run_date
            """,
            [DUAL_TRACK_MARKET, METRICS_START],
        ).fetchall()
        if isinstance(d, date)
    ]
    if not run_dates:
        return {"status": "no_factor_snapshots"}

    result: dict[str, Any] = {
        "market": DUAL_TRACK_MARKET,
        "pool_source": "factor_snapshot_universe",
        "top_n": DUAL_TRACK_TOP_N,
        "formulas": {
            public_name: {"variant": variant_name}
            for public_name, variant_name in DUAL_TRACK_FORMULAS.items()
        },
        "horizons": {},
        "run_date_count": len(run_dates),
    }

    close_cache: dict[date, dict[str, float]] = {}
    for horizon in HORIZONS:
        horizon_block: dict[str, Any] = {}
        for public_name, variant_name in DUAL_TRACK_FORMULAS.items():
            alphas: list[float] = []
            used_run_dates: set[str] = set()
            for rd in run_dates:
                pair = _entry_exit_dates(trading_dates, rd, horizon)
                if pair is None:
                    continue
                entry_date, exit_date = pair
                close_cache.setdefault(entry_date, _close_map(conn, DUAL_TRACK_MARKET, entry_date))
                close_cache.setdefault(exit_date, _close_map(conn, DUAL_TRACK_MARKET, exit_date))
                entry_close = close_cache[entry_date]
                exit_close = close_cache[exit_date]
                bench_start = entry_close.get("SPY") or entry_close.get("QQQ")
                bench_end = exit_close.get("SPY") or exit_close.get("QQQ")
                bench_ret = _safe_return(bench_start, bench_end)
                if bench_ret is None:
                    continue
                for pick in _rank_full_pool_us(conn, rd, variant_name):
                    stock_ret = _safe_return(entry_close.get(pick["symbol"]), exit_close.get(pick["symbol"]))
                    if stock_ret is None:
                        continue
                    alphas.append(stock_ret - bench_ret)
                    used_run_dates.add(rd.isoformat())
            summary = _summarize_alpha(alphas)
            summary["mature_run_dates"] = len(used_run_dates)
            horizon_block[public_name] = summary
        if all(horizon_block.get(k, {}).get("n", 0) for k in DUAL_TRACK_FORMULAS):
            new_avg = horizon_block["val_down_grade"]["avg_alpha_pct"]
            old_avg = horizon_block["legacy_baseline"]["avg_alpha_pct"]
            horizon_block["delta_new_minus_old_avg_alpha_pct"] = round(new_avg - old_avg, 4)
        result["horizons"][horizon] = horizon_block
    return result


def compute(as_of: date | None = None) -> dict:
    sys.path.insert(0, str(_REPO / "scripts" / "lib"))
    import stock_research.core.strategy_eval as se
    conn = _connect()
    if conn is None:
        raise RuntimeError("DB 持续被写锁占用，跳过本轮 alpha 记录")
    try:
        ver = se.latest_strategy_version(conn)
        rec: dict = {
            "date": (as_of or date.today()).isoformat(),
            "strategy_version": ver,
            "metrics_start": METRICS_START,
            "markets": {},
        }
        for mkt in MARKETS:
            rec["markets"][mkt] = {}
            for hz in HORIZONS:
                s = se.mature_samples(conn, market=mkt, horizon=hz, metrics_start=METRICS_START)
                if not s:
                    rec["markets"][mkt][hz] = {"n": 0}
                    continue
                m = se.summarize(s)
                rec["markets"][mkt][hz] = {
                    "n": m["n"],
                    "avg_alpha_pct": round(m["avg_alpha_pct"], 4),
                    "win_rate_pct": round(m["win_rate_pct"], 2),
                    "worst_alpha_pct": round(m["worst_alpha_pct"], 4),
                    "sample_power": m["sample_power"],
                }
        rec["dual_track_us"] = _dual_track_us(conn)
        return rec
    finally:
        conn.close()


def _load_history() -> list[dict]:
    if OUT.exists():
        try:
            d = json.loads(OUT.read_text(encoding="utf-8"))
            return d.get("trend") or []
        except Exception as exc:
            logger.warning("读历史失败: %s", exc)
    return []


def save(rec: dict) -> None:
    trend = [r for r in _load_history() if r.get("date") != rec["date"]]  # 当天幂等覆盖
    trend.append(rec)
    trend.sort(key=lambda r: r["date"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"trend": trend}, ensure_ascii=False, indent=2), encoding="utf-8")


def _fmt_row(r: dict) -> str:
    us = r["markets"].get("US", {})
    u1 = us.get("1d", {}); u5 = us.get("5d", {})
    def cell(c):
        return f"{c.get('avg_alpha_pct', float('nan')):+.2f}%/{c.get('win_rate_pct', 0):.0f}%(n{c.get('n', 0)})" if c.get("n") else "—"
    dual = r.get("dual_track_us") or {}
    h5 = (dual.get("horizons") or {}).get("5d") or {}
    delta = h5.get("delta_new_minus_old_avg_alpha_pct")
    dual_hint = f"  双轨5d 新-旧 {delta:+.2f}pp" if isinstance(delta, (int, float)) else ""
    return f"{r['date']}  US 1d {cell(u1)}  5d {cell(u5)}{dual_hint}"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="每日记 alpha 走势")
    p.add_argument("--dry-run", action="store_true", help="只算不写")
    p.add_argument("--show", action="store_true", help="打印已攒走势")
    args = p.parse_args()

    if args.show:
        hist = _load_history()
        if not hist:
            print("暂无走势数据。")
        for r in hist:
            print(_fmt_row(r))
        return 0

    rec = compute()
    print(_fmt_row(rec))
    if args.dry_run:
        print("[dry-run] 未写入")
        return 0
    save(rec)
    print(f"已记入 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
