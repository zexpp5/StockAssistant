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
import math
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
MARKETS = ("US", "HK", "CN")
HORIZONS = ("1d", "5d", "20d")  # 2026-07-06 用户问"多久操作一次"→ 补 20d(月度操作真实持有期)验证
DUAL_TRACK_TOP_NS = (5, 10, 20)          # 向后兼容：US 顶层默认 top_ns
DUAL_TRACK_DEFAULT_TOP_N = 20

# ── 分市场双轨锦标赛配置（2026-07-07 泛化，docs/V2/2026-07-06_港A股影子组合锦标赛_方案）──
# 每市场：基线(现行生产) vs 挑战者(锦标赛变体)，从 factor_snapshot_universe 全池
# 各自独立选 TopN，算前向 alpha 对照。variant 名指向 replay_weight_variants.VARIANTS。
# switch_rule 预注册、不许事后挪门槛；达标只亮绿灯，切生产仍须用户拍板。
DUAL_TRACK_CONFIG: dict[str, dict[str, Any]] = {
    "US": {
        "benchmark": ("SPY", "QQQ"),
        "eligibility": {"buyable", "research_only"},
        "top_ns": (5, 10, 20),
        "default_top_n": 20,
        "baseline": "legacy_baseline",
        "primary_challenger": "val_down_grade",
        "formulas": {
            "legacy_baseline": "prod_recheck",
            "val_down_grade": "val_down_grade",
            # 2026-07-02 锦标赛唯一挑战者（海选每轮只提拔 1 个，防多重比较）。
            "challenger_quality_heavy": "quality_heavy",
        },
        "switch_rule": {
            "target": "US 主榜 Top20 切换 val_down_grade",
            "min_5d_n": 300, "consecutive_days_required": 10, "switch_top_n": 20,
            "rollback": "切换后 top20 5d delta 连续 5 日 < 0 → 回滚",
        },
    },
    "HK": {
        "benchmark": ("^HSI",),
        "eligibility": {"research_only"},
        "top_ns": (5, 10),                 # 港股池小（33→100），不做 Top20
        "default_top_n": 10,
        "baseline": "hk_production",       # hk_scoring.HK_FACTOR_WEIGHTS
        "primary_challenger": "hk_quality_heavy",  # 回放冠军（f_score 0.40）
        "formulas": {
            "hk_production": "hk_production",
            "hk_quality_heavy": "quality_heavy",
            "hk_equal_4": "equal_4",
            "hk_val_down_quality": "val_down_quality",
        },
        "switch_rule": {
            "target": "HK 主榜切换回放冠军 quality_heavy",
            "min_5d_n": 150, "consecutive_days_required": 10, "switch_top_n": 10,
            "rollback": "切换后 top10 5d delta 连续 5 日 < 0 → 回滚",
        },
    },
    "CN": {
        "benchmark": ("000300.SS",),
        "eligibility": {"research_only"},
        "top_ns": (5, 10),
        "default_top_n": 10,
        "baseline": "cn_production",       # reversal_pure = 生产 reversal 1.0
        "primary_challenger": "cn_reversal_quality",
        "formulas": {
            "cn_production": "reversal_pure",
            "cn_reversal_quality": "cn_reversal_quality",
            # 全池等权：诊断「池子本身是不是负 alpha 源」。方案 §3B。
            "cn_full_pool_equal": "equal_4",
        },
        "switch_rule": {
            "target": "CN 换池：挑战者跑赢生产 reversal",
            "min_5d_n": 300, "consecutive_days_required": 10, "switch_top_n": 10,
            "rollback": "切换后 top10 5d delta 连续 5 日 < 0 → 回滚",
        },
    },
}

# ── 向后兼容别名（旧代码/测试仍引用这些 US 常量）────────────────────────
DUAL_TRACK_MARKET = "US"
DUAL_TRACK_FORMULAS = DUAL_TRACK_CONFIG["US"]["formulas"]
US_RECOMMENDABLE_ELIGIBILITY = DUAL_TRACK_CONFIG["US"]["eligibility"]
SWITCH_RULE = DUAL_TRACK_CONFIG["US"]["switch_rule"]


def _switch_criteria_verdict(dual_track: dict[str, Any], trend: list[dict],
                             market: str, config: dict[str, Any]) -> dict[str, Any]:
    """按该市场 switch_rule 判定当天是否达标 + 连续达标天数。纯读，不改生产。

    达标条件（预注册）：默认 TopN 的 1d 与 5d，挑战者-基线 Δ 同时 > 0，
    且挑战者自身 5d alpha > 0（不能只是「输得比基线少」），5d 样本 n ≥ min_5d_n。
    连续满足 ≥ consecutive_days_required 个交易日 → switch_allowed 亮绿灯。
    """
    rule = config["switch_rule"]
    challenger = config["primary_challenger"]
    top_key = f"top{rule['switch_top_n']}"
    top = ((dual_track.get("by_top_n") or {}).get(top_key) or {}).get("horizons") or {}
    h1, h5 = top.get("1d") or {}, top.get("5d") or {}
    d1 = (h1.get(challenger) or {}).get("delta_vs_baseline_avg_alpha_pct")
    d5 = (h5.get(challenger) or {}).get("delta_vs_baseline_avg_alpha_pct")
    new5 = (h5.get(challenger) or {}).get("avg_alpha_pct")
    n5 = (h5.get(challenger) or {}).get("n") or 0
    checks = {
        f"{top_key}_1d_delta_positive": d1 is not None and d1 > 0,
        f"{top_key}_5d_delta_positive": d5 is not None and d5 > 0,
        f"{top_key}_5d_new_alpha_positive": new5 is not None and new5 > 0,
        f"{top_key}_5d_n_enough": n5 >= rule["min_5d_n"],
    }
    met_today = all(checks.values())
    streak = 1 if met_today else 0
    if met_today:
        for entry in reversed(trend):
            if market == "US":
                v = (entry.get("dual_track_us") or {}).get("switch_criteria") or {}
            else:
                v = ((entry.get("dual_track") or {}).get(market) or {}).get("switch_criteria") or {}
            if v.get("met_today"):
                streak += 1
            else:
                break
    return {
        "rule": rule,
        "market": market,
        "challenger": challenger,
        "checks": checks,
        "met_today": met_today,
        "consecutive_met_days": streak,
        "switch_allowed": met_today and streak >= rule["consecutive_days_required"],
        "values": {"delta_1d": d1, "delta_5d": d5, "challenger_5d_alpha": new5, "n_5d": n5},
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
    # DuckDB 里 NaN close 不是 NULL，过不掉 `close IS NOT NULL`；这里必须挡掉
    # 非有限值，否则一个 NaN 会把整组 avg 污染成 nan（HK/CN 价源出现过）。
    if not (math.isfinite(s) and math.isfinite(e)) or s <= 0:
        return None
    return (e / s - 1.0) * 100.0


def _summarize_alpha(samples: list[float]) -> dict[str, Any]:
    finite = [x for x in samples if math.isfinite(x)]
    if not finite:
        return {"n": 0}
    return {
        "n": len(finite),
        "avg_alpha_pct": round(sum(finite) / len(finite), 4),
        "win_rate_pct": round(sum(1 for x in finite if x > 0) / len(finite) * 100.0, 2),
        "worst_alpha_pct": round(min(finite), 4),
    }


def _benchmark_trading_dates(conn: Any, market: str, benchmark: tuple[str, ...]) -> list[date]:
    """以基准 ETF/指数的有价日作为该市场交易日历（US=SPY/QQQ, HK=^HSI, CN=000300.SS）。"""
    placeholders = ",".join("?" for _ in benchmark)
    rows = conn.execute(
        f"""
        SELECT DISTINCT trade_date
        FROM price_daily
        WHERE market=? AND symbol IN ({placeholders}) AND close IS NOT NULL
        ORDER BY trade_date
        """,
        [market, *benchmark],
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


def _rank_full_pool(conn: Any, market: str, run_date_value: date, formula_key: str,
                    eligibility: set[str], limit: int | None = None) -> list[dict[str, Any]]:
    """从 factor_snapshot_universe 全池按指定公式独立排序某市场候选。

    grade 因子仅美股有分析师覆盖：US 缺分时用 build_grade_score_map 补，
    HK/CN 无此数据 → grade 恒中性（且港A股变体权重不含 grade，取值不影响排序）。
    """
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
        [market, run_date_value],
    ).fetchall()
    if not rows:
        return []

    grade_map: dict[str, float] = {}
    if market == "US":
        missing_grade_symbols = {
            str(sym).upper()
            for sym, *_rest, grade, eligibility_val in rows
            if grade is None and str(eligibility_val or "") in eligibility
        }
        if missing_grade_symbols:
            grade_map = build_grade_score_map(conn, missing_grade_symbols, run_date_value, market=market)
    weights = rp.weights_for_market(rp.VARIANTS[formula_key], market)
    ranked = []
    for sym, momentum, valuation, reversal, data_usability, f_score, grade, eligibility_val in rows:
        if str(eligibility_val or "") not in eligibility:
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


def _dual_track_market(conn: Any, market: str, config: dict[str, Any]) -> dict[str, Any]:
    """某市场：基线 vs 挑战者全池独立选 TopN 的前向 alpha 对照。

    每个 challenger 记 delta_vs_baseline；主挑战者的 delta 另存
    delta_new_minus_old_avg_alpha_pct（US 向后兼容旧 dashboard/日志字段）。
    """
    benchmark = tuple(config["benchmark"])
    eligibility = set(config["eligibility"])
    top_ns = tuple(config["top_ns"])
    default_top_n = config["default_top_n"]
    formulas = config["formulas"]
    baseline = config["baseline"]
    primary = config["primary_challenger"]

    trading_dates = _benchmark_trading_dates(conn, market, benchmark)
    if not trading_dates:
        return {"status": "no_benchmark_dates", "market": market}
    run_dates = [
        d for (d,) in conn.execute(
            """
            SELECT DISTINCT run_date
            FROM factor_snapshot_universe
            WHERE market=? AND run_date>=?
            ORDER BY run_date
            """,
            [market, METRICS_START],
        ).fetchall()
        if isinstance(d, date)
    ]
    if not run_dates:
        return {"status": "no_factor_snapshots", "market": market}

    result: dict[str, Any] = {
        "market": market,
        "pool_source": "factor_snapshot_universe",
        "benchmark": list(benchmark),
        "top_ns": list(top_ns),
        "default_top_n": default_top_n,
        "baseline": baseline,
        "primary_challenger": primary,
        "formulas": {
            public_name: {"variant": variant_name}
            for public_name, variant_name in formulas.items()
        },
        "by_top_n": {},
        "run_date_count": len(run_dates),
        "note": (
            "基线=现行生产，挑战者=锦标赛变体，同池各自独立选 TopN 前向对照；"
            "达标只亮绿灯，切生产须用户拍板，不等于买入指令。"
        ),
    }

    close_cache: dict[date, dict[str, float]] = {}
    rank_cache: dict[tuple[date, str], list[dict[str, Any]]] = {}
    max_top_n = max(top_ns)

    def _bench_ret(entry_close: dict[str, float], exit_close: dict[str, float]) -> float | None:
        start = next((entry_close.get(b) for b in benchmark if entry_close.get(b)), None)
        end = next((exit_close.get(b) for b in benchmark if exit_close.get(b)), None)
        return _safe_return(start, end)

    for top_n in top_ns:
        top_key = f"top{top_n}"
        top_block: dict[str, Any] = {
            "market": market,
            "pool_source": "factor_snapshot_universe",
            "top_n": top_n,
            "formulas": result["formulas"],
            "horizons": {},
            "run_date_count": len(run_dates),
        }
        for horizon in HORIZONS:
            horizon_block: dict[str, Any] = {}
            for public_name, variant_name in formulas.items():
                alphas: list[float] = []
                used_run_dates: set[str] = set()
                for rd in run_dates:
                    pair = _entry_exit_dates(trading_dates, rd, horizon)
                    if pair is None:
                        continue
                    entry_date, exit_date = pair
                    close_cache.setdefault(entry_date, _close_map(conn, market, entry_date))
                    close_cache.setdefault(exit_date, _close_map(conn, market, exit_date))
                    entry_close = close_cache[entry_date]
                    exit_close = close_cache[exit_date]
                    bench_ret = _bench_ret(entry_close, exit_close)
                    if bench_ret is None:
                        continue
                    cache_key = (rd, variant_name)
                    if cache_key not in rank_cache:
                        rank_cache[cache_key] = _rank_full_pool(conn, market, rd, variant_name,
                                                                eligibility, limit=max_top_n)
                    for pick in rank_cache[cache_key][:top_n]:
                        stock_ret = _safe_return(entry_close.get(pick["symbol"]), exit_close.get(pick["symbol"]))
                        if stock_ret is None:
                            continue
                        alphas.append(stock_ret - bench_ret)
                        used_run_dates.add(rd.isoformat())
                summary = _summarize_alpha(alphas)
                summary["mature_run_dates"] = len(used_run_dates)
                horizon_block[public_name] = summary
            # 每个挑战者记 delta_vs_baseline（基线自身 delta=0 不记）
            base_avg = (horizon_block.get(baseline) or {}).get("avg_alpha_pct")
            if base_avg is not None:
                for public_name in formulas:
                    if public_name == baseline:
                        continue
                    ch_avg = (horizon_block.get(public_name) or {}).get("avg_alpha_pct")
                    if ch_avg is not None:
                        horizon_block[public_name]["delta_vs_baseline_avg_alpha_pct"] = round(ch_avg - base_avg, 4)
                # US 向后兼容：顶层 delta 字段=主挑战者 delta
                primary_delta = (horizon_block.get(primary) or {}).get("delta_vs_baseline_avg_alpha_pct")
                if primary_delta is not None:
                    horizon_block["delta_new_minus_old_avg_alpha_pct"] = primary_delta
            top_block["horizons"][horizon] = horizon_block
        result["by_top_n"][top_key] = top_block

    default_block = result["by_top_n"].get(f"top{default_top_n}") or {}
    result["top_n"] = default_top_n
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
        prior = [r for r in _load_history() if str(r.get("date")) < rec["date"]]  # 同日重跑不重复计连胜
        dual_by_market: dict[str, Any] = {}
        for mkt, cfg in DUAL_TRACK_CONFIG.items():
            block = _dual_track_market(conn, mkt, cfg)
            if not block.get("status"):  # 有数据才判 switch
                block["switch_criteria"] = _switch_criteria_verdict(block, prior, mkt, cfg)
            dual_by_market[mkt] = block
        rec["dual_track"] = dual_by_market
        rec["dual_track_us"] = dual_by_market.get("US", {})  # 向后兼容 dashboard
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
    # 港A股锦标赛：主挑战者 vs 基线的默认 TopN 5d Δ
    extra = []
    for mkt in ("HK", "CN"):
        blk = (r.get("dual_track") or {}).get(mkt) or {}
        if blk.get("status"):
            continue
        dtn = blk.get("default_top_n")
        h5 = ((blk.get("by_top_n") or {}).get(f"top{dtn}") or {}).get("horizons", {}).get("5d") or {}
        ch = blk.get("primary_challenger")
        d = (h5.get(ch) or {}).get("delta_vs_baseline_avg_alpha_pct")
        n = (h5.get(ch) or {}).get("n") or 0
        if isinstance(d, (int, float)):
            extra.append(f"{mkt} Top{dtn} {d:+.2f}pp(n{n})")
    extra_hint = f"  锦标赛5d {' / '.join(extra)}" if extra else ""
    return f"{r['date']}  US 1d {cell(u1)}  5d {cell(u5)}{dual_hint}{extra_hint}"


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
