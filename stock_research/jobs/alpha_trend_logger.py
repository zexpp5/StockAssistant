"""每日记一笔生产推荐 alpha → data/latest/alpha_trend.json，攒走势线。

只读 strategy_eval（生产 picks 成熟样本），不改打分、不推送。每天一行，
按日期幂等（同日重跑覆盖当天那条）。配合 launchd 每早跑一次即可。

记录：US/HK × 1d/5d 的 n / 平均 alpha% / 胜率% / 样本档位。
另记 US 新旧公式双轨：从 factor_snapshot_universe 全量池各自独立选 Top5/Top10/Top20，
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
DUAL_TRACK_TOP_NS = (5, 10, 20)
DUAL_TRACK_DEFAULT_TOP_N = 20
DUAL_TRACK_FORMULAS = {
    "legacy_baseline": "prod_recheck",
    "val_down_grade": "val_down_grade",
    # 2026-07-02 全变体锦标赛后注册的唯一挑战者（海选每轮只提拔 1 个，防多重比较）：
    # quality_heavy 在 Top5/Top10 × 1d/5d 四个格全为正、跌市最稳(Top10 5d 跌市 +0.80%)。
    # 只做前向追踪对照，不参与任何生产/精选层输出。
    "challenger_quality_heavy": "quality_heavy",
}
US_RECOMMENDABLE_ELIGIBILITY = {"buyable", "research_only"}

# ── 预注册主榜切换标准（2026-07-02 拍板，不许事后挪门槛）────────────────
# 全部满足才允许把美股主榜从 legacy 切到 val_down_grade：
#   1. Top20 1d 与 5d 的 Δ(new-old) 同时 > 0
#   2. Top20 新公式自身 5d alpha > 0（不能只是"输得比老公式少"）
#   3. 5d 样本 n ≥ 300
#   4. 以上条件连续满足 ≥ 10 个交易日（由 alpha_trend.json 历史判定）
# 切换后回滚线：Top20 5d Δ 连续 5 日 < 0 → 立即回滚老公式。
SWITCH_RULE = {
    "target": "US 主榜 Top20 切换 val_down_grade",
    "min_5d_n": 300,
    "consecutive_days_required": 10,
    "conditions": [
        "top20.1d.delta>0",
        "top20.5d.delta>0",
        "top20.5d.val_down_grade.avg_alpha_pct>0",
        "top20.5d.n>=300",
    ],
    "rollback": "切换后 top20 5d delta 连续 5 日 < 0 → 回滚",
}


def _switch_criteria_verdict(dual_track: dict[str, Any], trend: list[dict]) -> dict[str, Any]:
    """按 SWITCH_RULE 判定当天是否达标 + 已连续达标天数。纯读，不改任何生产行为。"""
    top20 = ((dual_track.get("by_top_n") or {}).get("top20") or {}).get("horizons") or {}
    h1, h5 = top20.get("1d") or {}, top20.get("5d") or {}
    d1 = h1.get("delta_new_minus_old_avg_alpha_pct")
    d5 = h5.get("delta_new_minus_old_avg_alpha_pct")
    new5 = (h5.get("val_down_grade") or {}).get("avg_alpha_pct")
    n5 = (h5.get("val_down_grade") or {}).get("n") or 0
    checks = {
        "top20_1d_delta_positive": d1 is not None and d1 > 0,
        "top20_5d_delta_positive": d5 is not None and d5 > 0,
        "top20_5d_new_alpha_positive": new5 is not None and new5 > 0,
        "top20_5d_n_enough": n5 >= SWITCH_RULE["min_5d_n"],
    }
    met_today = all(checks.values())
    # 连续达标天数：从历史 trend 里往回数（含今天）
    streak = 1 if met_today else 0
    if met_today:
        for entry in reversed(trend):
            v = (entry.get("dual_track_us") or {}).get("switch_criteria") or {}
            if v.get("met_today"):
                streak += 1
            else:
                break
    return {
        "rule": SWITCH_RULE,
        "checks": checks,
        "met_today": met_today,
        "consecutive_met_days": streak,
        "switch_allowed": met_today and streak >= SWITCH_RULE["consecutive_days_required"],
        "values": {"top20_1d_delta": d1, "top20_5d_delta": d5,
                   "top20_5d_new_alpha": new5, "top20_5d_n": n5},
    }


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


def _rank_full_pool_us(conn: Any, run_date_value: date, formula_key: str,
                       limit: int | None = None) -> list[dict[str, Any]]:
    """从 factor_snapshot_universe 全池按指定公式独立排序 US 候选。"""
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
    return ranked[:limit] if limit else ranked


def _dual_track_us(conn: Any) -> dict[str, Any]:
    """US 新旧公式全池独立选 Top5/Top10/Top20 的前向 alpha 对照。"""
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
        "top_ns": list(DUAL_TRACK_TOP_NS),
        "default_top_n": DUAL_TRACK_DEFAULT_TOP_N,
        "formulas": {
            public_name: {"variant": variant_name}
            for public_name, variant_name in DUAL_TRACK_FORMULAS.items()
        },
        "by_top_n": {},
        "run_date_count": len(run_dates),
        "note": (
            "Top5/Top10 用来验证精选层；Top20 用来验证全榜替换。"
            "精选层可优先研究，但不等于真实买入指令。"
        ),
    }

    close_cache: dict[date, dict[str, float]] = {}
    rank_cache: dict[tuple[date, str], list[dict[str, Any]]] = {}
    max_top_n = max(DUAL_TRACK_TOP_NS)
    for top_n in DUAL_TRACK_TOP_NS:
        top_key = f"top{top_n}"
        top_block: dict[str, Any] = {
            "market": DUAL_TRACK_MARKET,
            "pool_source": "factor_snapshot_universe",
            "top_n": top_n,
            "formulas": result["formulas"],
            "horizons": {},
            "run_date_count": len(run_dates),
        }
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
                    cache_key = (rd, variant_name)
                    if cache_key not in rank_cache:
                        rank_cache[cache_key] = _rank_full_pool_us(conn, rd, variant_name, limit=max_top_n)
                    for pick in rank_cache[cache_key][:top_n]:
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
            top_block["horizons"][horizon] = horizon_block
        result["by_top_n"][top_key] = top_block

    default_key = f"top{DUAL_TRACK_DEFAULT_TOP_N}"
    default_block = result["by_top_n"].get(default_key) or {}
    # 兼容旧面板/旧日志：顶层 horizons 仍代表默认 Top20。
    result["top_n"] = DUAL_TRACK_DEFAULT_TOP_N
    result["horizons"] = default_block.get("horizons", {})
    return result


# ── 推荐持有期止损闸（#4）────────────────────────────────────────────
# 动机: 5d 最差单票 -32%, 一只破位票吃掉 Top10 一个月 alpha。
# 口径: 扫最近 STOP_GATE_LOOKBACK_RUNS 个交易日内的生产 picks(三市场),
#       自入榜日收盘价到最新收盘价回撤 ≤ STOP_GATE_DRAWDOWN_PCT → 记破位名单。
# 只提示复查(advisory), 不自动卖出、不改榜单。
STOP_GATE_DRAWDOWN_PCT = -20.0
STOP_GATE_LOOKBACK_RUNS = 5


def _pick_stop_gate(conn: Any) -> dict[str, Any]:
    """持有期破位扫描。纯读; 失败返回 error 块, 不拦 alpha 记录主流程。"""
    breaches: list[dict[str, Any]] = []
    scanned = 0
    try:
        rows = conn.execute(
            """
            WITH recent_runs AS (
                SELECT DISTINCT rp.market, DATE(rr.generated_at) AS run_day
                FROM recommendation_runs rr
                JOIN recommendation_picks rp ON rp.run_id = rr.run_id
                WHERE rr.universe_scope = 'system_tech_universe'
                  AND DATE(rr.generated_at) >= CURRENT_DATE - INTERVAL 14 DAY
            ), ranked_runs AS (
                SELECT market, run_day,
                       ROW_NUMBER() OVER (PARTITION BY market ORDER BY run_day DESC) AS rn
                FROM recent_runs
            ), scope AS (
                SELECT market, MIN(run_day) AS since FROM ranked_runs WHERE rn <= ? GROUP BY market
            ), latest_batch AS (
                SELECT rp.market, rp.symbol, rp.name, DATE(rr.generated_at) AS pick_day,
                       ROW_NUMBER() OVER (PARTITION BY rp.market, rp.symbol
                                          ORDER BY rr.generated_at ASC) AS first_seen
                FROM recommendation_runs rr
                JOIN recommendation_picks rp ON rp.run_id = rr.run_id
                JOIN scope s ON s.market = rp.market AND DATE(rr.generated_at) >= s.since
                WHERE rr.universe_scope = 'system_tech_universe'
            )
            SELECT market, symbol, name, pick_day FROM latest_batch WHERE first_seen = 1
            """,
            [STOP_GATE_LOOKBACK_RUNS],
        ).fetchall()
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}

    for market, symbol, name, pick_day in rows:
        pd = _as_date(pick_day)
        if pd is None:
            continue
        try:
            px = conn.execute(
                """
                SELECT
                    (SELECT close FROM price_daily
                     WHERE market=? AND symbol=? AND trade_date>=? AND close IS NOT NULL
                     ORDER BY trade_date ASC LIMIT 1) AS entry_close,
                    (SELECT close FROM price_daily
                     WHERE market=? AND symbol=? AND close IS NOT NULL
                     ORDER BY trade_date DESC LIMIT 1) AS last_close
                """,
                [market, symbol, pd, market, symbol],
            ).fetchone()
        except Exception:
            continue
        if not px or px[0] is None or px[1] is None or float(px[0]) <= 0:
            continue
        scanned += 1
        dd = (float(px[1]) / float(px[0]) - 1.0) * 100.0
        if dd <= STOP_GATE_DRAWDOWN_PCT:
            breaches.append({
                "market": market, "symbol": symbol, "name": name,
                "pick_day": pd.isoformat(),
                "drawdown_pct": round(dd, 2),
                "entry_close": round(float(px[0]), 4),
                "last_close": round(float(px[1]), 4),
            })
    breaches.sort(key=lambda b: b["drawdown_pct"])
    return {
        "status": "ok",
        "threshold_pct": STOP_GATE_DRAWDOWN_PCT,
        "lookback_runs": STOP_GATE_LOOKBACK_RUNS,
        "scanned": scanned,
        "n_breach": len(breaches),
        "breaches": breaches,
        "note": "advisory: 破位票建议复查基本面/催化, 不构成自动卖出指令",
    }


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
        dual = _dual_track_us(conn)
        prior = [r for r in _load_history() if str(r.get("date")) < rec["date"]]  # 同日重跑不重复计连胜
        dual["switch_criteria"] = _switch_criteria_verdict(dual, prior)
        rec["dual_track_us"] = dual
        rec["pick_stop_gate"] = _pick_stop_gate(conn)
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
    by_top_n = dual.get("by_top_n") or {}
    parts = []
    for top_n in DUAL_TRACK_TOP_NS:
        h5 = ((by_top_n.get(f"top{top_n}") or {}).get("horizons") or {}).get("5d") or {}
        delta = h5.get("delta_new_minus_old_avg_alpha_pct")
        if isinstance(delta, (int, float)):
            parts.append(f"Top{top_n} {delta:+.2f}pp")
    if not parts:
        h5 = (dual.get("horizons") or {}).get("5d") or {}
        delta = h5.get("delta_new_minus_old_avg_alpha_pct")
        if isinstance(delta, (int, float)):
            parts.append(f"Top{dual.get('top_n', DUAL_TRACK_DEFAULT_TOP_N)} {delta:+.2f}pp")
    dual_hint = f"  双轨5d {' / '.join(parts)}" if parts else ""
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
