#!/usr/bin/env python3
"""Build v2 system-pool recommendations from the clean database.

Inputs:
  system_universe + pool_membership + price_daily

Outputs:
  recommendation_runs + recommendation_picks + portfolio_plans

This script intentionally does not read legacy watchlist/prices/picks tables or
data/latest artifacts. It is the clean v2 path for AI Assistant recommendations.
When the newest same-day price row is a minimal market snapshot, factor fields
are backfilled from the most recent non-null price_daily row for the same symbol.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from stock_research import config  # noqa: E402


def _clip(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


_NEGATIVE_VALUATION_SCORE = 27.5  # loss / earnings decline: not "cheap"
_PRICE_ACTION_REVIEW_SCORE_CAP = 59.99
STRATEGY_VERSION = "tech_ai_v2_price_action_gate"
MODEL_VERSION = "v2_rule_factor_2026_06_price_action_gate"


def _score_lower_better(value: Any, good: float, bad: float, missing: float = 30.0) -> float:
    try:
        x = float(value)
    except Exception:
        return missing
    if x <= 0:
        # PE / PEG 的 0 通常是缺失、哨兵值或口径不可用，不能当成"极便宜"。
        return _NEGATIVE_VALUATION_SCORE
    if x <= good:
        return 95.0
    if x >= bad:
        return 20.0
    return _clip(95.0 - (x - good) / (bad - good) * 75.0)


def _score_one_year_momentum(value: Any) -> float:
    # 驼峰评分：50%-150% 区间最优，>200% 反向扣分（追高惩罚）。
    # 参考 stock_research.jobs.calibrate_pick_weights._score_trend_inline 的 IC 实证分档。
    try:
        x = float(value)
    except Exception:
        return 45.0
    if x >= 400:
        return 20.0
    if x >= 200:
        return 50.0
    if x >= 50:
        return 100.0
    if x >= 0:
        return 75.0
    if x >= -35:
        return 50.0
    return 25.0


def _score_reversal(row: dict[str, Any]) -> float:
    """1 月反转分（mean reversion）— 最近 1 月跌得多 → 高分；涨得多 → 低分。

    定义跟 scripts/lib/factor_model.reversal_1m 一致：reversal_raw = -one_month_pct。
    映射：跌 15% → 100；持平 → 50；涨 15% → 0；线性 clip。
    缺数据 → 45（中性偏低，避免无数据撑高分）。

    Why: calibrated_factor_weights.json 的 IC audit 判定 reversal 🟢 strong
    (IC=0.062, hit_rate=72.2%)，是 V2 当前唯一被独立验证的 alpha 来源。
    """
    try:
        x = float(row.get("one_month_pct"))
    except (TypeError, ValueError):
        return 45.0
    return _clip(50.0 - x * (50.0 / 15.0))


def _load_reversal_weight() -> float:
    """从 calibrated_factor_weights.json 读 reversal 启用状态。

    selected=True → V2 给 reversal 0.15 权重（从 valuation 0.65 切下来）。
    selected=False（IC 失效） → 不用，权重 0（valuation 回到 0.65）。
    文件缺失 / 解析失败 → 保守按 0（保持现状）。

    返回值是 V2 公式里 reversal 的子权重，**不是** calibrated 的原始 weight。
    """
    path = REPO / "data" / "calibrated_factor_weights.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        diag = (data.get("diagnostics") or {}).get("reversal") or {}
        if diag.get("selected"):
            return 0.15
    except Exception:
        pass
    return 0.0


def _score_momentum(row: dict[str, Any]) -> float:
    parts = []
    for key, weight, lo, hi in (
        ("one_week_pct", 0.20, -8.0, 8.0),
        ("one_month_pct", 0.25, -15.0, 20.0),
        ("ytd_pct", 0.25, -25.0, 60.0),
    ):
        value = row.get(key)
        try:
            x = float(value)
        except Exception:
            parts.append((45.0, weight))
            continue
        parts.append((_clip((x - lo) / (hi - lo) * 100.0), weight))
    parts.append((_score_one_year_momentum(row.get("one_year_pct")), 0.30))
    momentum = sum(score * weight for score, weight in parts)
    # 追高一票封顶：1Y > 300% 时，短期动量也多半是情绪驱动，整体动量分压到 40。
    try:
        y1 = float(row.get("one_year_pct"))
        if y1 > 300:
            momentum = min(momentum, 40.0)
    except Exception:
        pass
    return momentum


def _factor_scores(row: dict[str, Any]) -> dict[str, Any]:
    peg_score = _score_lower_better(row.get("peg_ratio"), good=1.0, bad=4.0)
    fpe_score = _score_lower_better(row.get("forward_pe"), good=20.0, bad=80.0)
    tpe_score = _score_lower_better(row.get("trailing_pe"), good=25.0, bad=100.0)
    valuation = 0.45 * peg_score + 0.35 * fpe_score + 0.20 * tpe_score
    momentum = _score_momentum(row)
    reversal = _score_reversal(row)
    coverage_fields = (
        "close", "market_cap", "forward_pe", "trailing_pe", "peg_ratio",
        "ytd_pct", "one_week_pct", "one_month_pct", "one_year_pct",
    )
    coverage = sum(1 for key in coverage_fields if row.get(key) is not None) / len(coverage_fields)
    data_quality = coverage * 100.0
    # 2026-05-26: momentum 0.42→0.15、valuation 0.38→0.65（IC 审计判 momentum 失效）。
    # 2026-05-27: 引入 reversal 因子 — calibrated_factor_weights.json 判 reversal
    # 🟢 strong (IC=0.062, hit=72.2%)，是 V2 唯一被独立验证的 alpha。reversal 子权重
    # 0.15 由 _load_reversal_weight() 从 calibrated 读，IC 失效时自动归零、回到
    # 旧公式 (0.15·mom + 0.65·val + 0.20·dq)，跟 IC audit 单一来源对齐。
    rev_w = _load_reversal_weight()
    val_w = 0.65 - rev_w  # reversal 从 valuation 切，保持总权重 1.0
    total = (
        0.15 * momentum
        + val_w * valuation
        + rev_w * reversal
        + 0.20 * data_quality
    )
    scores: dict[str, Any] = {
        "valuation": round(valuation, 2),
        "momentum": round(momentum, 2),
        "reversal": round(reversal, 2),
        "data_quality": round(data_quality, 2),
        "coverage": round(coverage, 4),
        "total": round(total, 2),
    }
    # F-Score / Piotroski 已计算入 factor_metadata 时透传（compute_piotroski_v2.py 写入）
    # 当前数据接入：A 股 akshare 财报、美/港股 待 FMP/yfinance 财报源激活
    f_score = row.get("_factor_meta_f_score")
    if f_score is not None:
        scores["f_score"] = round(float(f_score) / 9.0 * 100.0, 2)  # 标准化到 0-100
    quality_score = row.get("_factor_meta_quality_score")
    if quality_score is not None:
        scores["quality"] = round(float(quality_score), 2)
    return scores


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _daily_pct(row: dict[str, Any]) -> float | None:
    close = _as_float(row.get("close"))
    prev_close = _as_float(row.get("prev_close"))
    if close is None or prev_close is None or prev_close <= 0:
        return None
    return (close / prev_close - 1.0) * 100.0


def _fmt_pct(label: str, value: float | None) -> str | None:
    if value is None:
        return None
    return f"{label} {value:+.1f}%"


def _structural_repair_confirmed(
    *,
    one_week: float | None,
    one_month: float | None,
    day: float | None,
) -> bool:
    """Conservative repair check for names already in a structural downtrend.

    A sharp one-week bounce alone is not enough. The one-month window must have
    turned clearly positive, and the latest day cannot be another hard selloff.
    """
    if one_month is None or one_week is None:
        return False
    if one_month < 5.0 or one_week < 0.0:
        return False
    if day is not None and day <= -3.0:
        return False
    return True


def _price_action_review_reasons(row: dict[str, Any]) -> list[str]:
    """Return price-action reasons that require buy-before review.

    Reversal is useful as a research signal, but a name that is down sharply on
    short and medium windows should not graduate directly into a buy signal. A
    single sharp down day inside an otherwise strong trend is warning material,
    not enough by itself to turn a candidate into a "fallen knife" review gate.
    """
    one_week = _as_float(row.get("one_week_pct"))
    one_month = _as_float(row.get("one_month_pct"))
    ytd = _as_float(row.get("ytd_pct"))
    one_year = _as_float(row.get("one_year_pct"))
    day = _daily_pct(row)

    deep_month_drop = one_month is not None and one_month <= -15.0
    severe_month_drop = one_month is not None and one_month <= -20.0
    structural_downtrend = (
        (ytd is not None and ytd <= -25.0)
        or (one_year is not None and one_year <= -25.0)
    )
    medium_weakness = structural_downtrend or (one_month is not None and one_month <= -8.0)

    reasons: list[str] = []
    if day is not None and day <= -8.0 and medium_weakness:
        reasons.append(f"单日 {day:+.1f}%")
    if one_week is not None and one_week <= -12.0 and (
        structural_downtrend or (one_month is not None and one_month <= -5.0)
    ):
        reasons.append(f"近1周 {one_week:+.1f}%")
    if severe_month_drop or (deep_month_drop and structural_downtrend):
        details = [
            _fmt_pct("近1月", one_month),
            _fmt_pct("YTD", ytd),
            _fmt_pct("1Y", one_year),
        ]
        reasons.append(" / ".join(x for x in details if x))
    if structural_downtrend and not _structural_repair_confirmed(
        one_week=one_week, one_month=one_month, day=day,
    ):
        details = [
            _fmt_pct("YTD", ytd),
            _fmt_pct("1Y", one_year),
            _fmt_pct("近1月", one_month),
            _fmt_pct("近1周", one_week),
        ]
        reasons.append("结构性下跌未确认修复：" + " / ".join(x for x in details if x))

    return reasons


def _price_action_warning_flags(row: dict[str, Any]) -> list[dict[str, Any]]:
    if _price_action_review_reasons(row):
        return []

    reasons: list[str] = []
    day = _daily_pct(row)
    one_week = _as_float(row.get("one_week_pct"))
    if day is not None and day <= -8.0:
        reasons.append(f"单日 {day:+.1f}%")
    if one_week is not None and one_week <= -12.0:
        reasons.append(f"近1周 {one_week:+.1f}%")
    if not reasons:
        return []
    return [{
        "code": "ACUTE_PRICE_PULLBACK",
        "severity": "medium",
        "message": (
            "短线价格异动："
            + "；".join(reasons)
            + "。未达到跌深反转降级条件，但需确认是否为事件性下跌。"
        ),
    }]


def _apply_price_action_review_gate(row: dict[str, Any], scores: dict[str, Any]) -> list[dict[str, Any]]:
    reasons = _price_action_review_reasons(row)
    if not reasons:
        return []

    raw_total = float(scores["total"])
    scores["raw_total"] = round(raw_total, 2)
    scores["review_gate"] = "price_action"
    scores["total"] = round(min(raw_total, _PRICE_ACTION_REVIEW_SCORE_CAP), 2)
    structural = any("结构性下跌" in r for r in reasons)

    return [{
        "code": "STRUCTURAL_DOWNTREND_REVIEW_GATE" if structural else "PRICE_ACTION_REVIEW_GATE",
        "severity": "high",
        "message": (
            "价格行为红旗："
            + "；".join(reasons)
            + ("。属于结构性下跌后的反弹候选，已从 buy 降为 watch；需先确认修复。"
               if structural else
               "。属于跌深反转候选，已从 buy 降为 watch；需先进入买前审查。")
        ),
    }]


def _rating(total: float) -> str:
    if total >= 75:
        return "strong_buy"
    if total >= 60:
        return "buy"
    if total >= 50:
        return "watch"
    return "avoid"


def _signal(total: float) -> str:
    if total >= 60:
        return "buy"
    if total >= 50:
        return "watch"
    return "avoid"


def _same_day(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    return str(a)[:10] == str(b)[:10]


def _quality_flags(row: dict[str, Any]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    invalid_valuation_fields = []
    for key, label in (
        ("peg_ratio", "PEG"),
        ("forward_pe", "Forward PE"),
        ("trailing_pe", "Trailing PE"),
    ):
        value = _as_float(row.get(key))
        if value is not None and value <= 0:
            invalid_valuation_fields.append(f"{label}={value:g}")
    if invalid_valuation_fields:
        flags.append({
            "code": "INVALID_VALUATION_RATIO",
            "severity": "medium",
            "message": (
                "估值字段异常："
                + " / ".join(invalid_valuation_fields)
                + "；已按亏损/缺失口径扣分，不视为便宜。"
            ),
        })
    try:
        y1 = float(row.get("one_year_pct"))
    except (TypeError, ValueError):
        y1 = None
    if y1 is not None and y1 > 200:
        flags.append({
            "code": "OVERHEATED_1Y",
            "severity": "high" if y1 > 300 else "medium",
            "message": f"1Y 涨幅 {y1:.0f}%（>200% 历史前向收益走弱，已对动量分追高扣分）",
        })
    if not row.get("momentum_trade_date"):
        flags.append({
            "code": "MOMENTUM_MISSING",
            "severity": "medium",
            "message": "该标的 price_daily 找不到任何非空动量字段，可能用了默认动量分。",
        })
    elif not _same_day(row.get("momentum_trade_date"), row.get("trade_date")):
        flags.append({
            "code": "MOMENTUM_REUSED_RECENT_V2_SNAPSHOT",
            "severity": "low",
            "message": (
                "最新行情动量字段为空，已回退到 "
                f"{str(row.get('momentum_trade_date'))[:10]} 最近一次有效快照。"
            ),
        })
    if not row.get("fundamentals_trade_date"):
        flags.append({
            "code": "FUNDAMENTALS_MISSING",
            "severity": "medium",
            "message": "该标的 price_daily 找不到任何非空估值字段。",
        })
    elif not _same_day(row.get("fundamentals_trade_date"), row.get("trade_date")):
        flags.append({
            "code": "FUNDAMENTALS_REUSED_RECENT_V2_SNAPSHOT",
            "severity": "low",
            "message": (
                "最新行情估值字段为空，已回退到 "
                f"{str(row.get('fundamentals_trade_date'))[:10]} 最近一次有效快照。"
            ),
        })
    return flags


def _reason(row: dict[str, Any]) -> str:
    scores = row["factor_scores"]
    momentum_date = str(row.get("momentum_trade_date") or "")[:10] or "missing"
    fundamentals_date = str(row.get("fundamentals_trade_date") or "")[:10] or "missing"
    return (
        f"momentum={scores['momentum']}, valuation={scores['valuation']}, "
        f"coverage={scores['coverage']}; "
        f"momentum_source=price_daily:{momentum_date}, "
        f"valuation_source=price_daily:{fundamentals_date}"
    )


def _load_candidates(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT market, symbol, MAX(trade_date) AS trade_date
            FROM price_daily
            GROUP BY market, symbol
        ),
        latest_price AS (
            SELECT p.*
            FROM price_daily p
            JOIN latest l
              ON l.market = p.market AND l.symbol = p.symbol AND l.trade_date = p.trade_date
        ),
        latest_momentum AS (
            SELECT *
            FROM (
                SELECT
                    market, symbol, trade_date, source, fetched_at,
                    ytd_pct, one_week_pct, one_month_pct, one_year_pct,
                    ROW_NUMBER() OVER (
                        PARTITION BY market, symbol
                        ORDER BY trade_date DESC, fetched_at DESC
                    ) AS rn
                FROM price_daily
                WHERE ytd_pct IS NOT NULL
                   OR one_week_pct IS NOT NULL
                   OR one_month_pct IS NOT NULL
                   OR one_year_pct IS NOT NULL
            )
            WHERE rn = 1
        ),
        latest_fundamentals AS (
            SELECT *
            FROM (
                SELECT
                    market, symbol, trade_date, source, fetched_at,
                    market_cap, forward_pe, trailing_pe, peg_ratio,
                    ROW_NUMBER() OVER (
                        PARTITION BY market, symbol
                        ORDER BY trade_date DESC, fetched_at DESC
                    ) AS rn
                FROM price_daily
                WHERE market_cap IS NOT NULL
                   OR forward_pe IS NOT NULL
                   OR trailing_pe IS NOT NULL
                   OR peg_ratio IS NOT NULL
            )
            WHERE rn = 1
        )
        SELECT
            m.pool_id, m.market, m.symbol, u.name, u.theme, u.industry,
            p.trade_date, p.close, p.prev_close, p.currency,
            COALESCE(p.market_cap, f.market_cap) AS market_cap,
            COALESCE(p.forward_pe, f.forward_pe) AS forward_pe,
            COALESCE(p.trailing_pe, f.trailing_pe) AS trailing_pe,
            COALESCE(p.peg_ratio, f.peg_ratio) AS peg_ratio,
            COALESCE(p.ytd_pct, mo.ytd_pct) AS ytd_pct,
            COALESCE(p.one_week_pct, mo.one_week_pct) AS one_week_pct,
            COALESCE(p.one_month_pct, mo.one_month_pct) AS one_month_pct,
            COALESCE(p.one_year_pct, mo.one_year_pct) AS one_year_pct,
            p.source, p.fetched_at,
            mo.trade_date AS momentum_trade_date,
            mo.source AS momentum_source,
            f.trade_date AS fundamentals_trade_date,
            f.source AS fundamentals_source,
            fm.f_score AS _factor_meta_f_score,
            fm.quality_score AS _factor_meta_quality_score
        FROM pool_membership m
        JOIN latest_price p
          ON p.market = m.market AND p.symbol = m.symbol
        LEFT JOIN latest_momentum mo
          ON mo.market = m.market AND mo.symbol = m.symbol
        LEFT JOIN latest_fundamentals f
          ON f.market = m.market AND f.symbol = m.symbol
        LEFT JOIN system_universe u
          ON u.pool_id = m.pool_id AND u.market = m.market AND u.symbol = m.symbol
        LEFT JOIN factor_metadata fm
          ON fm.market = m.market AND fm.symbol = m.symbol
        WHERE m.active = TRUE
          AND m.pool_type = 'system_tech_universe'
        """
    ).fetchall()
    cols = [
        "pool_id", "market", "symbol", "name", "theme", "industry",
        "trade_date", "close", "prev_close", "currency", "market_cap",
        "forward_pe", "trailing_pe", "peg_ratio", "ytd_pct",
        "one_week_pct", "one_month_pct", "one_year_pct", "source",
        "fetched_at", "momentum_trade_date", "momentum_source",
        "fundamentals_trade_date", "fundamentals_source",
        "_factor_meta_f_score", "_factor_meta_quality_score",
    ]
    return [dict(zip(cols, row)) for row in rows]


def build(db_path: Path, *, top_per_market: int, portfolio_size: int, dry_run: bool) -> dict[str, Any]:
    conn = duckdb.connect(str(db_path), read_only=dry_run)
    tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
    required = {"pool_membership", "system_universe", "price_daily", "recommendation_runs", "recommendation_picks", "portfolio_plans"}
    missing = sorted(required - tables)
    if missing:
        conn.close()
        raise RuntimeError(f"当前 DB 缺少 v2 推荐表: {missing}")

    pool_membership_count = int(conn.execute(
        "SELECT COUNT(*) FROM pool_membership WHERE active = TRUE AND pool_type = 'system_tech_universe'"
    ).fetchone()[0])
    price_daily_count = int(conn.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0])
    candidates = _load_candidates(conn)
    now = datetime.now()
    run_id = f"rec_{now.strftime('%Y%m%d_%H%M%S')}_system_tech"
    strategy_version = STRATEGY_VERSION
    model_version = MODEL_VERSION
    scored: list[dict[str, Any]] = []
    for row in candidates:
        scores = _factor_scores(row)
        price_action_flags = _apply_price_action_review_gate(row, scores)
        if not price_action_flags:
            price_action_flags = _price_action_warning_flags(row)
        total = scores["total"]
        scored.append({
            **row,
            "total_score": total,
            "factor_scores": scores,
            "risk_flags": price_action_flags + _quality_flags(row),
            "rating": _rating(total),
            "signal": _signal(total),
        })

    selected: list[dict[str, Any]] = []
    for market in ("US", "HK", "CN"):
        market_rows = [r for r in scored if r["market"] == market]
        market_rows.sort(key=lambda x: (-x["total_score"], x["symbol"]))
        selected.extend(market_rows[:top_per_market])
    selected.sort(key=lambda x: (x["market"], -x["total_score"], x["symbol"]))

    if dry_run:
        conn.close()
        return {
            "db_path": str(db_path),
            "run_id": run_id,
            "dry_run": True,
            "pool_membership_count": pool_membership_count,
            "price_daily_count": price_daily_count,
            "candidate_count": len(candidates),
            "selected_count": len(selected),
            "selected": [
                {"market": r["market"], "symbol": r["symbol"], "name": r["name"], "score": r["total_score"], "signal": r["signal"]}
                for r in selected
            ],
        }

    conn.execute(
        """
        INSERT INTO strategy_versions (
            strategy_version, status, description, params_json, created_at, activated_at
        ) VALUES (?, 'active', ?, ?, ?, ?)
        ON CONFLICT (strategy_version) DO UPDATE SET
            status='active',
            description=excluded.description,
            params_json=excluded.params_json
        """,
        [
            strategy_version,
            (
                "V2 system tech universe rule-factor strategy with price-action review gate, "
                "invalid valuation-ratio guard, and buy-only portfolio eligibility."
            ),
            json.dumps({
                "top_per_market": top_per_market,
                "portfolio_size": portfolio_size,
                "score_cap_price_action_review": _PRICE_ACTION_REVIEW_SCORE_CAP,
                "invalid_valuation_ratio_score": _NEGATIVE_VALUATION_SCORE,
                "structural_repair_requires": "one_month>=+5%, one_week>=0%, latest_day>-3%",
            }, ensure_ascii=False),
            now,
            now,
        ],
    )
    conn.execute(
        """
        INSERT INTO recommendation_runs (
            run_id, run_date, strategy_version, model_version, universe_scope,
            data_cutoff_at, generated_at, status, notes
        ) VALUES (?, ?, ?, ?, 'system_tech_universe', ?, ?, 'generated', ?)
        """,
        [
            run_id,
            now.date(),
            strategy_version,
            model_version,
            now,
            now,
            (
                f"candidate_count={len(candidates)}; selected_count={len(selected)}; "
                f"pool_membership={pool_membership_count}; price_daily={price_daily_count}; "
                "scoring_change=price_action_review_gate+structural_downtrend_gate+invalid_zero_valuation_guard"
            ),
        ],
    )
    for rank, row in enumerate(selected, start=1):
        conn.execute(
            """
            INSERT INTO recommendation_picks (
                run_id, market, symbol, name, rank, rating, signal,
                total_score, factor_scores_json, recommendation_reason,
                risk_flags_json, entry_price, entry_currency, universe_scope,
                source_origin, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'system_tech_universe', 'system_pool', ?)
            """,
            [
                run_id,
                row["market"],
                row["symbol"],
                row["name"],
                rank,
                row["rating"],
                row["signal"],
                row["total_score"],
                json.dumps(row["factor_scores"], ensure_ascii=False),
                _reason(row),
                json.dumps(row["risk_flags"], ensure_ascii=False),
                row["close"],
                row["currency"],
                now,
            ],
        )

    buy_rows = [r for r in selected if r["signal"] == "buy"]
    buy_rows.sort(key=lambda x: x["total_score"], reverse=True)
    portfolio_rows = buy_rows[:portfolio_size]
    target_weight = round(1.0 / len(portfolio_rows), 6) if portfolio_rows else 0.0
    for row in portfolio_rows:
        conn.execute(
            """
            INSERT INTO portfolio_plans (
                run_id, plan_version, strategy_scope, market, symbol,
                target_weight, action, risk_limit_json, transaction_cost_bps,
                benchmark_symbol, created_at
            ) VALUES (?, 'v2_pre_optimizer_equal_weight', 'system_tech_universe', ?, ?, ?, 'pre_optimizer_placeholder', ?, 10, ?, ?)
            """,
            [
                run_id,
                row["market"],
                row["symbol"],
                target_weight,
                json.dumps({
                    "max_single_weight": 0.12,
                    "min_sample_note": "pre_optimizer_placeholder",
                    "replaced_by": "stock_research.jobs.optimize_portfolio -> v6_risk_aware",
                }, ensure_ascii=False),
                "SPY" if row["market"] == "US" else ("2800.HK" if row["market"] == "HK" else "000300.SH"),
                now,
            ],
        )

    if "pipeline_runs" in tables:
        conn.execute(
            """
            INSERT INTO pipeline_runs (
                run_id, mode, status, planned_at, started_at, completed_at, trigger_source
            ) VALUES (?, 'v2_recommendation', 'success', ?, ?, ?, 'manual')
            ON CONFLICT (run_id) DO UPDATE SET status='success', completed_at=excluded.completed_at
            """,
            [run_id, now, now, now],
        )
    if "pipeline_steps" in tables:
        conn.execute(
            """
            INSERT INTO pipeline_steps (
                run_id, step_name, status, started_at, ended_at, duration_seconds, sink, error_summary
            ) VALUES (?, 'build_v2_recommendations', 'success', ?, ?, 0, 'recommendation_runs,recommendation_picks,portfolio_plans', NULL)
            """,
            [run_id, now, now],
        )
    conn.close()
    return {
        "db_path": str(db_path),
        "run_id": run_id,
        "dry_run": False,
        "pool_membership_count": pool_membership_count,
        "price_daily_count": price_daily_count,
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "portfolio_count": len(portfolio_rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build v2 recommendations from system tech universe.")
    parser.add_argument("--db", default=os.environ.get("STOCK_DB_PATH") or str(config.DUCKDB_PATH))
    parser.add_argument("--top-per-market", type=int, default=20,
                        help="每个市场写入 recommendation_picks 的候选数；需足够大，供风险优化器选满目标持仓")
    parser.add_argument("--portfolio-size", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = build(
        Path(args.db).expanduser().resolve(),
        top_per_market=args.top_per_market,
        portfolio_size=args.portfolio_size,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"v2 recommendations: run_id={payload['run_id']}")
        print(
            f"  pool_membership={payload.get('pool_membership_count', 0)} "
            f"price_daily={payload.get('price_daily_count', 0)} "
            f"candidates={payload['candidate_count']} selected={payload['selected_count']}"
        )
        if not args.dry_run:
            print(f"  portfolio={payload['portfolio_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
