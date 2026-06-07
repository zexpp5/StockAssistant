#!/usr/bin/env python3
"""Build the read-only US strict-trial overlay for AI recommendations.

This tool answers a narrow money-sensitive question:
"Among today's US recommendations, is there a smaller research queue with
better historical evidence than the full Top20?"

Safety boundary:
- reads recommendation_runs / recommendation_picks / pick_outcomes only;
- writes only data/latest/us_strict_trial.json and data/reports/us_strict_trial.md;
- does not update recommendation formulas, recommendation tables, watchlist,
  real holdings, portfolio plans, or strategy_versions.
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from stock_research import config  # noqa: E402

DB_PATH = Path(config.DUCKDB_PATH)
OUT_JSON = REPO / "data" / "latest" / "us_strict_trial.json"
OUT_MD = REPO / "data" / "reports" / "us_strict_trial.md"

MARKET = "US"
HORIZON = "1d"
BUY_SIGNALS = {"buy", "strong_buy"}
METRICS_START_DATE = "2026-05-25"
TRIAL_FORWARD_START_DATE = "2026-06-06"
MIN_REVIEWED_FOR_TRIAL_REVIEW = 20
MIN_WIN_RATE_FOR_TRIAL_REVIEW = 45.0
MIN_CONFIRMING_RUNS_FOR_TRIAL_REVIEW = 2
MIN_REVIEWED_PER_RUN_FOR_FOLD = 3
BLOCKED_RISK_FLAGS = {"OVERHEATED_1Y"}
PULLBACK_RISK_FLAGS = {"ACUTE_PRICE_PULLBACK"}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    text = str(value).strip()
    try:
        parsed = json.loads(text)
        return parsed if parsed is not None else fallback
    except Exception:
        pass
    try:
        parsed = ast.literal_eval(text)
        return parsed if parsed is not None else fallback
    except Exception:
        return fallback


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _round(value: Any, digits: int = 2) -> float | None:
    n = _num(value)
    return round(n, digits) if n is not None else None


def _json_time(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return _json_time(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _tables(conn) -> set[str]:
    try:
        return {str(row[0]) for row in conn.execute("SHOW TABLES").fetchall()}
    except Exception:
        return set()


def _required_tables_ok(conn) -> tuple[bool, list[str]]:
    required = {"recommendation_runs", "recommendation_picks", "pick_outcomes"}
    missing = sorted(required - _tables(conn))
    return not missing, missing


def _latest_strategy_version(conn) -> str | None:
    if "recommendation_runs" not in _tables(conn):
        return None
    row = conn.execute(
        """
        SELECT strategy_version
        FROM recommendation_runs
        WHERE universe_scope = 'system_tech_universe'
          AND status = 'generated'
          AND strategy_version IS NOT NULL
          AND strategy_version <> ''
        ORDER BY generated_at DESC
        LIMIT 1
        """
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _resolve_strategy_version(conn, requested: str | None) -> str | None:
    value = str(requested or "latest").strip()
    if value.lower() in {"", "latest", "current"}:
        return _latest_strategy_version(conn)
    if value.lower() in {"all", "*"}:
        return None
    return value


def _latest_run(conn, strategy_version: str | None) -> dict[str, Any] | None:
    strategy_clause = " AND strategy_version = ?" if strategy_version else ""
    params: list[Any] = []
    if strategy_version:
        params.append(strategy_version)
    row = conn.execute(
        f"""
        SELECT run_id, run_date, generated_at, strategy_version, universe_scope, status
        FROM recommendation_runs
        WHERE universe_scope = 'system_tech_universe'
          AND status = 'generated'
          {strategy_clause}
        ORDER BY generated_at DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if not row:
        return None
    keys = ["run_id", "run_date", "generated_at", "strategy_version", "universe_scope", "status"]
    return {key: _json_time(value) for key, value in zip(keys, row, strict=False)}


def _risk_codes(flags: Any) -> list[str]:
    rows = flags if isinstance(flags, list) else _safe_json(flags, [])
    if not isinstance(rows, list):
        rows = [rows]
    codes: list[str] = []
    for item in rows:
        if isinstance(item, dict):
            code = item.get("code") or item.get("name") or item.get("flag")
            if code:
                codes.append(str(code))
            continue
        text = str(item or "").strip()
        if not text:
            continue
        parsed = _safe_json(text, None)
        if isinstance(parsed, dict):
            code = parsed.get("code") or parsed.get("name") or parsed.get("flag")
            codes.append(str(code or text))
        else:
            codes.append(text)
    return [code for code in codes if code]


def _risk_messages(flags: Any) -> list[str]:
    rows = flags if isinstance(flags, list) else _safe_json(flags, [])
    if not isinstance(rows, list):
        rows = [rows]
    messages: list[str] = []
    for item in rows:
        if isinstance(item, dict):
            messages.append(str(item.get("message") or item.get("code") or item))
        else:
            messages.append(str(item))
    return [m for m in messages if m and m != "None"]


def _factor_value(row: dict[str, Any], key: str) -> float | None:
    factors = row.get("factor_scores")
    if not isinstance(factors, dict):
        factors = _safe_json(row.get("factor_scores_json"), {})
    direct = _num(row.get(key))
    if direct is not None:
        return direct
    if isinstance(factors, dict):
        return _num(factors.get(key))
    return None


def _is_buy_signal(row: dict[str, Any]) -> bool:
    signal = str(row.get("signal") or row.get("rating") or "").strip().lower()
    return signal in BUY_SIGNALS


def _is_strict_candidate(
    row: dict[str, Any],
    *,
    max_market_rank: int = 5,
    momentum_lt: float = 80.0,
    blocked_flags: set[str] | None = None,
) -> bool:
    blocked_flags = blocked_flags or BLOCKED_RISK_FLAGS
    market_rank = _int(row.get("market_rank"), 9999)
    momentum = _factor_value(row, "momentum")
    codes = set(_risk_codes(row.get("risk_flags") or row.get("risk_flags_json")))
    return (
        str(row.get("market") or "").upper() == MARKET
        and _is_buy_signal(row)
        and market_rank <= max_market_rank
        and momentum is not None
        and momentum < momentum_lt
        and not (codes & blocked_flags)
    )


def _candidate_note(risk_codes: list[str]) -> tuple[str, list[str]]:
    if set(risk_codes) & PULLBACK_RISK_FLAGS:
        return "博反弹候选 · 非稳健票", ["短线大跌后反弹口径", "必须先查事件原因"]
    return "严筛候选 · 仅买前研究", ["不自动买入", "不写持仓"]


def _pick_rows_for_run(conn, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          market, symbol, name, rank AS global_rank, rating, signal, total_score,
          factor_scores_json, recommendation_reason, risk_flags_json,
          entry_price, entry_currency, universe_scope, source_origin, created_at
        FROM recommendation_picks
        WHERE run_id = ?
          AND market = 'US'
          AND LOWER(COALESCE(signal, rating, '')) IN ('buy', 'strong_buy')
        ORDER BY total_score DESC NULLS LAST, symbol ASC
        """,
        [run_id],
    ).fetchall()
    keys = [
        "market", "symbol", "name", "global_rank", "rating", "signal", "total_score",
        "factor_scores_json", "recommendation_reason", "risk_flags_json",
        "entry_price", "entry_currency", "universe_scope", "source_origin", "created_at",
    ]
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        record = {key: _json_time(value) for key, value in zip(keys, row, strict=False)}
        record["market_rank"] = idx
        record["factor_scores"] = _safe_json(record.get("factor_scores_json"), {})
        record["risk_flags"] = _safe_json(record.get("risk_flags_json"), [])
        out.append(record)
    return out


def _current_candidates(
    conn,
    *,
    latest_run_id: str,
    max_market_rank: int,
    max_candidates: int,
    momentum_lt: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _pick_rows_for_run(conn, latest_run_id)
    candidates: list[dict[str, Any]] = []
    rejected_top: list[dict[str, Any]] = []
    for row in rows:
        risk_codes = _risk_codes(row.get("risk_flags"))
        momentum = _factor_value(row, "momentum")
        strict = _is_strict_candidate(
            row,
            max_market_rank=max_market_rank,
            momentum_lt=momentum_lt,
            blocked_flags=BLOCKED_RISK_FLAGS,
        )
        base = {
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "market": row.get("market"),
            "global_rank": _int(row.get("global_rank"), 0),
            "market_rank": _int(row.get("market_rank"), 0),
            "rating": row.get("rating"),
            "signal": row.get("signal"),
            "total_score": _round(row.get("total_score"), 2),
            "entry_price": _round(row.get("entry_price"), 4),
            "entry_currency": row.get("entry_currency") or "USD",
            "momentum": _round(momentum, 2),
            "factor_scores": {
                key: _round(_factor_value(row, key), 2)
                for key in ("valuation", "momentum", "reversal", "data_quality", "coverage", "f_score")
                if _factor_value(row, key) is not None
            },
            "risk_codes": risk_codes,
            "risk_messages": _risk_messages(row.get("risk_flags")),
            "reason": row.get("recommendation_reason"),
            "strict_pass": strict,
        }
        if strict:
            note, labels = _candidate_note(risk_codes)
            base["trial_note"] = note
            base["labels"] = labels
            base["allowed_use"] = "仅买前研究，不自动买入"
            candidates.append(base)
        elif _int(row.get("market_rank"), 9999) <= max_market_rank:
            reasons: list[str] = []
            if momentum is None:
                reasons.append("momentum 缺失")
            elif momentum >= momentum_lt:
                reasons.append(f"momentum {momentum:.1f} >= {momentum_lt:.0f}")
            if set(risk_codes) & BLOCKED_RISK_FLAGS:
                reasons.append("命中过热红旗 OVERHEATED_1Y")
            base["reject_reasons"] = reasons or ["未通过严筛"]
            rejected_top.append(base)
    return candidates[:max_candidates], rejected_top


def _evidence_samples(
    conn,
    *,
    strategy_version: str | None,
    horizon: str,
    metrics_start_date: str,
) -> list[dict[str, Any]]:
    strategy_clause = " AND rr.strategy_version = ?" if strategy_version else ""
    params: list[Any] = [horizon, metrics_start_date]
    if strategy_version:
        params.append(strategy_version)
    rows = conn.execute(
        f"""
        SELECT *
        FROM (
          SELECT
            rr.run_id,
            rr.run_date,
            rr.generated_at,
            rr.strategy_version,
            rp.market,
            rp.symbol,
            rp.name,
            rp.rank AS global_rank,
            ROW_NUMBER() OVER (
              PARTITION BY rp.run_id, rp.market
              ORDER BY rp.total_score DESC NULLS LAST, rp.symbol ASC
            ) AS market_rank,
            rp.rating,
            rp.signal,
            rp.total_score,
            rp.factor_scores_json,
            rp.risk_flags_json,
            po.outcome_date,
            po.return_pct,
            po.benchmark_symbol,
            po.benchmark_pct,
            po.alpha_pct,
            po.is_success
          FROM recommendation_picks rp
          JOIN recommendation_runs rr ON rr.run_id = rp.run_id
          JOIN pick_outcomes po
            ON po.run_id = rp.run_id
           AND po.market = rp.market
           AND po.symbol = rp.symbol
           AND po.horizon = ?
          WHERE rr.universe_scope = 'system_tech_universe'
            AND rr.status = 'generated'
            AND rr.run_date >= ?
            {strategy_clause}
            AND rp.market = 'US'
            AND LOWER(COALESCE(rp.signal, rp.rating, '')) IN ('buy', 'strong_buy')
            AND po.alpha_pct IS NOT NULL
        )
        ORDER BY generated_at ASC, market_rank ASC
        """,
        params,
    ).fetchall()
    keys = [
        "run_id", "run_date", "generated_at", "strategy_version", "market", "symbol", "name",
        "global_rank", "market_rank", "rating", "signal", "total_score", "factor_scores_json",
        "risk_flags_json", "outcome_date", "return_pct", "benchmark_symbol", "benchmark_pct",
        "alpha_pct", "is_success",
    ]
    samples: list[dict[str, Any]] = []
    for row in rows:
        sample = {key: _json_time(value) for key, value in zip(keys, row, strict=False)}
        sample["factor_scores"] = _safe_json(sample.get("factor_scores_json"), {})
        sample["risk_flags"] = _safe_json(sample.get("risk_flags_json"), [])
        if _num(sample.get("alpha_pct")) is not None:
            samples.append(sample)
    return samples


def _summarize_samples(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    alphas = [_num(row.get("alpha_pct")) for row in rows]
    alphas = [value for value in alphas if value is not None]
    wins = sum(1 for value in alphas if value > 0)
    losses = sum(1 for value in alphas if value < 0)
    return {
        "name": name,
        "n": len(alphas),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": _round((wins / len(alphas) * 100) if alphas else None, 2),
        "avg_alpha_pct": _round((sum(alphas) / len(alphas)) if alphas else None, 4),
        "median_alpha_pct": _round(_median(alphas), 4),
    }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _recent_run_summaries(rows: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    meta: dict[str, dict[str, Any]] = {}
    for row in rows:
        run_id = str(row.get("run_id") or "")
        if not run_id:
            continue
        grouped[run_id].append(row)
        meta[run_id] = {
            "run_id": run_id,
            "run_date": row.get("run_date"),
            "generated_at": row.get("generated_at"),
            "strategy_version": row.get("strategy_version"),
        }
    out = []
    for run_id, items in grouped.items():
        summary = _summarize_samples("strict_overlay_by_run", items)
        summary.update(meta.get(run_id, {}))
        out.append(summary)
    out.sort(key=lambda r: str(r.get("generated_at") or ""))
    return out[-limit:]


def _evidence_summary(
    samples: list[dict[str, Any]],
    *,
    max_market_rank: int,
    momentum_lt: float,
) -> dict[str, Any]:
    top_1_5 = [row for row in samples if _int(row.get("market_rank"), 9999) <= max_market_rank]
    top_6_20 = [row for row in samples if max_market_rank < _int(row.get("market_rank"), 9999) <= 20]
    top_1_5_momentum = [
        row for row in top_1_5
        if (_factor_value(row, "momentum") is not None and _factor_value(row, "momentum") < momentum_lt)
    ]
    strict = [
        row for row in top_1_5_momentum
        if not (set(_risk_codes(row.get("risk_flags"))) & BLOCKED_RISK_FLAGS)
    ]
    forward_strict = [
        row for row in strict
        if str(row.get("run_date") or "") >= TRIAL_FORWARD_START_DATE
    ]
    overheated = [
        row for row in samples
        if set(_risk_codes(row.get("risk_flags"))) & BLOCKED_RISK_FLAGS
    ]
    high_momentum = [
        row for row in samples
        if (_factor_value(row, "momentum") is not None and _factor_value(row, "momentum") >= momentum_lt)
    ]
    low_momentum = [
        row for row in samples
        if (_factor_value(row, "momentum") is not None and _factor_value(row, "momentum") < 40)
    ]
    recent = _recent_run_summaries(strict)
    return {
        "all_us_buy": _summarize_samples("全部 US buy", samples),
        "top_1_5": _summarize_samples("US Top 1-5", top_1_5),
        "top_6_20": _summarize_samples("US Top 6-20", top_6_20),
        "top_1_5_momentum_lt_80": _summarize_samples("US Top 1-5 且 momentum<80", top_1_5_momentum),
        "strict_overlay": _summarize_samples("US 严筛：Top1-5 + momentum<80 + 无过热红旗", strict),
        "strict_overlay_forward": _summarize_samples("US 严筛上线后前瞻样本", forward_strict),
        "overheated_1y": _summarize_samples("命中 OVERHEATED_1Y", overheated),
        "momentum_gte_80": _summarize_samples("momentum>=80", high_momentum),
        "momentum_lt_40": _summarize_samples("momentum<40", low_momentum),
        "recent_strict_runs": recent,
        "recent_forward_strict_runs": _recent_run_summaries(forward_strict),
    }


def _trial_review_gate(evidence: dict[str, Any]) -> dict[str, Any]:
    """Gate for submitting the strict overlay to manual small-trial review.

    Use forward samples since the overlay launch date.  Historical backtest
    evidence may justify a research queue, but it must not by itself unlock
    money-facing review.
    """
    forward = evidence.get("strict_overlay_forward") or {}
    recent = evidence.get("recent_forward_strict_runs") or []
    reviewed = _int(forward.get("n"))
    alpha = _num(forward.get("avg_alpha_pct"))
    win_rate = _num(forward.get("win_rate_pct"))
    last_runs = recent[-MIN_CONFIRMING_RUNS_FOR_TRIAL_REVIEW:]
    confirming_runs = sum(
        1
        for row in last_runs
        if (_num(row.get("avg_alpha_pct")) or 0) > 0
        and (_num(row.get("win_rate_pct")) or 0) >= MIN_WIN_RATE_FOR_TRIAL_REVIEW
    )
    checks = [
        {
            "code": "forward_reviewed_min",
            "label": "上线后前瞻样本",
            "passed": reviewed >= MIN_REVIEWED_FOR_TRIAL_REVIEW,
            "current": reviewed,
            "required": MIN_REVIEWED_FOR_TRIAL_REVIEW,
        },
        {
            "code": "forward_alpha_positive",
            "label": "上线后 alpha",
            "passed": alpha is not None and alpha > 0,
            "current": _round(alpha, 4),
            "required": ">0",
        },
        {
            "code": "forward_win_rate_min",
            "label": "上线后命中率",
            "passed": win_rate is not None and win_rate >= MIN_WIN_RATE_FOR_TRIAL_REVIEW,
            "current": _round(win_rate, 2),
            "required": MIN_WIN_RATE_FOR_TRIAL_REVIEW,
        },
        {
            "code": "confirming_runs",
            "label": "最近确认轮数",
            "passed": confirming_runs >= MIN_CONFIRMING_RUNS_FOR_TRIAL_REVIEW,
            "current": confirming_runs,
            "required": MIN_CONFIRMING_RUNS_FOR_TRIAL_REVIEW,
        },
    ]
    passed = all(item["passed"] for item in checks)
    return {
        "status": "ELIGIBLE_FOR_MANUAL_REVIEW" if passed else "NOT_READY",
        "status_label": "可提交人工可小仓试探评审" if passed else "未达到可小仓试探评审门槛",
        "sample_scope": "forward_after_overlay_start",
        "trial_forward_start_date": TRIAL_FORWARD_START_DATE,
        "minimums": {
            "reviewed": MIN_REVIEWED_FOR_TRIAL_REVIEW,
            "alpha_pct": ">0",
            "win_rate_pct": MIN_WIN_RATE_FOR_TRIAL_REVIEW,
            "confirming_runs": MIN_CONFIRMING_RUNS_FOR_TRIAL_REVIEW,
        },
        "checks": checks,
        "latest_forward_summary": forward,
    }


def _display_mode(evidence: dict[str, Any]) -> tuple[str, list[str]]:
    strict = evidence.get("strict_overlay") or {}
    recent = evidence.get("recent_strict_runs") or []
    notes: list[str] = []
    recent_negative = False
    if len(recent) >= 2:
        last_two = recent[-2:]
        last_two_negative = all((_num(row.get("avg_alpha_pct")) or 0) < 0 for row in last_two)
        last_two_enough_samples = all(_int(row.get("n")) >= MIN_REVIEWED_PER_RUN_FOR_FOLD for row in last_two)
        recent_negative = last_two_negative and last_two_enough_samples
        if recent_negative:
            notes.append("最近两轮严筛 alpha 均为负，需自动降级为折叠观察。")
        elif last_two_negative:
            notes.append(
                f"最近两轮严筛 alpha 为负，但单轮成熟样本未均达到 {MIN_REVIEWED_PER_RUN_FOR_FOLD} 个，不触发折叠。"
            )

    strict_n = _int(strict.get("n"))
    strict_alpha = _num(strict.get("avg_alpha_pct"))
    if strict_n >= 20 and strict_alpha is not None and strict_alpha < 0:
        notes.append("严筛累计样本已够但 alpha 仍为负，应撤下严筛区。")
        return "withdraw", notes
    if recent_negative:
        return "folded_research", notes
    return "active_research", notes


def _decision(
    *,
    candidates: list[dict[str, Any]],
    evidence: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    display_mode, mode_notes = _display_mode(evidence)
    strict = evidence.get("strict_overlay") or {}
    strict_alpha = _num(strict.get("avg_alpha_pct"))
    strict_win = _num(strict.get("win_rate_pct"))
    strict_n = _int(strict.get("n"))
    trial_gate = _trial_review_gate(evidence)
    if display_mode == "withdraw":
        status = "FAIL"
        label = "严筛证据转弱，自动撤下"
    elif not candidates:
        status = "INFO"
        label = "今日无 US 严筛候选"
    else:
        status = "WARN"
        label = "US 严筛试运行"
    return {
        "status": status,
        "code": "US_STRICT_RESEARCH_ONLY",
        "label": label,
        "display_mode": display_mode,
        "allowed_use": "仅供买前研究；不自动买入、不写真实持仓、不改变组合方案",
        "not_allowed": ["自动买入", "自动写 watchlist", "自动写真实持仓", "修改生产打分公式"],
        "strict_evidence": {
            "n": strict_n,
            "avg_alpha_pct": _round(strict_alpha, 4),
            "win_rate_pct": _round(strict_win, 2),
        },
        "trial_review_gate": trial_gate,
        "notes": mode_notes + warnings[:4],
    }


def build_payload(
    *,
    db_path: Path = DB_PATH,
    strategy_version: str | None = "latest",
    horizon: str = HORIZON,
    max_market_rank: int = 5,
    max_candidates: int = 5,
    momentum_lt: float = 80.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    warnings: list[str] = []
    blockers: list[str] = []
    if not db_path.exists():
        return {
            "schema_version": "us_strict_trial_v1",
            "generated_at": now.isoformat(timespec="seconds"),
            "status": "FAIL",
            "blockers": [f"DB 不存在：{db_path}"],
            "safety_boundary": "只读 overlay；未写生产表、watchlist 或真实持仓。",
        }

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        ok, missing = _required_tables_ok(conn)
        if not ok:
            return {
                "schema_version": "us_strict_trial_v1",
                "generated_at": now.isoformat(timespec="seconds"),
                "status": "FAIL",
                "blockers": [f"缺少必需表：{', '.join(missing)}"],
                "safety_boundary": "只读 overlay；未写生产表、watchlist 或真实持仓。",
            }

        resolved_strategy = _resolve_strategy_version(conn, strategy_version)
        latest = _latest_run(conn, resolved_strategy)
        if not latest:
            return {
                "schema_version": "us_strict_trial_v1",
                "generated_at": now.isoformat(timespec="seconds"),
                "status": "FAIL",
                "strategy_version": resolved_strategy,
                "blockers": ["找不到最新 system_tech_universe 推荐批次。"],
                "safety_boundary": "只读 overlay；未写生产表、watchlist 或真实持仓。",
            }

        candidates, rejected_top = _current_candidates(
            conn,
            latest_run_id=str(latest["run_id"]),
            max_market_rank=max_market_rank,
            max_candidates=max_candidates,
            momentum_lt=momentum_lt,
        )
        samples = _evidence_samples(
            conn,
            strategy_version=resolved_strategy,
            horizon=horizon,
            metrics_start_date=METRICS_START_DATE,
        )
        evidence = _evidence_summary(samples, max_market_rank=max_market_rank, momentum_lt=momentum_lt)
    finally:
        conn.close()

    strict = evidence.get("strict_overlay") or {}
    if _int(strict.get("n")) < 20:
        warnings.append(f"严筛历史成熟样本只有 {_int(strict.get('n'))} 个，小样本过拟合风险高。")
    if (_num(strict.get("avg_alpha_pct")) or 0) <= 0:
        warnings.append("严筛历史 alpha 未转正，只能折叠观察。")
    if any(set(row.get("risk_codes") or []) & PULLBACK_RISK_FLAGS for row in candidates):
        warnings.append("今日名单含短线大跌后的博反弹候选，必须先查事件原因。")

    decision = _decision(candidates=candidates, evidence=evidence, warnings=warnings)
    payload = {
        "schema_version": "us_strict_trial_v1",
        "generated_at": now.isoformat(timespec="seconds"),
        "status": decision["status"],
        "safety_boundary": "只读 overlay；不改公式、不写 watchlist、不写真实持仓、不自动切策略版本。",
        "strategy_version": resolved_strategy,
        "horizon": horizon,
        "metrics_start_date": METRICS_START_DATE,
        "trial_forward_start_date": TRIAL_FORWARD_START_DATE,
        "latest_run": latest,
        "criteria": {
            "market": MARKET,
            "signal": sorted(BUY_SIGNALS),
            "max_market_rank": max_market_rank,
            "max_candidates": max_candidates,
            "momentum_lt": momentum_lt,
            "blocked_risk_flags": sorted(BLOCKED_RISK_FLAGS),
            "market_rank_definition": "US buy 内按 total_score DESC NULLS LAST, symbol ASC 重排；不是全局 rank。",
        },
        "decision": decision,
        "current_candidates": candidates,
        "rejected_top": rejected_top,
        "evidence_summary": evidence,
        "warnings": warnings,
        "blockers": blockers,
    }
    return _jsonable(payload)


def _fmt_pct(value: Any, digits: int = 2) -> str:
    n = _num(value)
    return "—" if n is None else f"{n:+.{digits}f}%"


def _md(payload: dict[str, Any]) -> str:
    decision = payload.get("decision") or {}
    evidence = payload.get("evidence_summary") or {}
    strict = evidence.get("strict_overlay") or {}
    all_us = evidence.get("all_us_buy") or {}
    rows = []
    for item in payload.get("current_candidates") or []:
        rows.append(
            "| {symbol} | {name} | #{market_rank} | {global_rank} | {score} | {momentum} | {note} |".format(
                symbol=item.get("symbol") or "",
                name=item.get("name") or "",
                market_rank=item.get("market_rank") or "",
                global_rank=item.get("global_rank") or "",
                score=item.get("total_score") or "",
                momentum=item.get("momentum") or "",
                note=item.get("trial_note") or "",
            )
        )
    if not rows:
        rows.append("| — | 今日无严筛候选 | — | — | — | — | — |")
    warnings = "\n".join(f"- {x}" for x in (payload.get("warnings") or [])) or "- 无新增警告"
    return f"""# US 严筛试运行

- 生成：{payload.get('generated_at')}
- 状态：{payload.get('status')} / {decision.get('label')}
- 用途：{decision.get('allowed_use')}
- 最新批次：{(payload.get('latest_run') or {}).get('run_id')}

## 当前候选

| 股票 | 名称 | US market_rank | global_rank | 综合分 | momentum | 标注 |
|---|---|---:|---:|---:|---:|---|
{chr(10).join(rows)}

## 历史证据

- 全部 US buy：n={all_us.get('n')}，alpha {_fmt_pct(all_us.get('avg_alpha_pct'))}，胜率 {_fmt_pct(all_us.get('win_rate_pct'))}
- 严筛口径：n={strict.get('n')}，alpha {_fmt_pct(strict.get('avg_alpha_pct'))}，胜率 {_fmt_pct(strict.get('win_rate_pct'))}

## 警告

{warnings}

## 安全边界

{payload.get('safety_boundary')}
"""


def write_outputs(payload: dict[str, Any], *, out_json: Path = OUT_JSON, out_md: Path = OUT_MD) -> None:
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_md(payload), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build read-only US strict-trial overlay")
    parser.add_argument("--db", default=str(DB_PATH), help="DuckDB path")
    parser.add_argument("--strategy-version", default="latest", help="strategy version, or latest")
    parser.add_argument("--horizon", default=HORIZON, help="outcome horizon for evidence")
    parser.add_argument("--max-market-rank", type=int, default=5)
    parser.add_argument("--max-candidates", type=int, default=5)
    parser.add_argument("--momentum-lt", type=float, default=80.0)
    parser.add_argument("--json", default=str(OUT_JSON), help="output JSON path")
    parser.add_argument("--md", default=str(OUT_MD), help="output Markdown path")
    args = parser.parse_args(argv)

    payload = build_payload(
        db_path=Path(args.db),
        strategy_version=args.strategy_version,
        horizon=args.horizon,
        max_market_rank=args.max_market_rank,
        max_candidates=args.max_candidates,
        momentum_lt=args.momentum_lt,
    )
    write_outputs(payload, out_json=Path(args.json), out_md=Path(args.md))
    decision = payload.get("decision") or {}
    latest = payload.get("latest_run") or {}
    print(
        "US strict trial: "
        f"{payload.get('status')} · {decision.get('label')} · "
        f"{len(payload.get('current_candidates') or [])} candidates · "
        f"run={latest.get('run_id')}"
    )
    if payload.get("blockers"):
        print("blockers:", "; ".join(str(x) for x in payload["blockers"]))
    # Business status FAIL means "do not show/use the strict queue", not a
    # pipeline crash.  Keep the job non-blocking for daily_refresh; the JSON
    # carries the operator-facing status.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
