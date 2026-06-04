#!/usr/bin/env python3
"""Preflight the US shadow evidence before it can unlock trial usage.

This is a read-only guard for the operator question: "Will the US shadow gate
actually be valid when the counters reach the threshold?"  It checks the parts
that can quietly go wrong before enough time has passed:

- duplicated shadow artifacts for the same source production run;
- whether the latest production run has a matching shadow artifact;
- whether pick_outcomes can be read and joined back to source runs;
- whether US trial metrics are counted by unique source production runs.

It does not change recommendation formulas, write recommendation tables, write
watchlist, write real holdings, or activate strategy versions.
"""
from __future__ import annotations

import argparse
import json
import sys
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from scripts.tools import evaluate_shadow_tuning_run as shadow_eval  # noqa: E402
from stock_research import config  # noqa: E402

DB_PATH = Path(config.DUCKDB_PATH)
OUT_JSON = REPO / "data" / "latest" / "us_shadow_preflight_check.json"
OUT_MD = REPO / "data" / "reports" / "us_shadow_preflight_check.md"
SHADOW_EVIDENCE_JSON = REPO / "data" / "latest" / "shadow_tuning_evidence.json"

BUY_SIGNALS = {"buy", "strong_buy"}
MARKET = "US"
HORIZON = "1d"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return datetime.min
    try:
        return datetime.fromisoformat(text[:19])
    except Exception:
        return datetime.min


def _json_time(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _fmt_pct(value: Any) -> str:
    n = _num(value)
    return "—" if n is None else f"{n:.2f}%"


def _check(status: str, code: str, title: str, message: str, details: Any = None) -> dict[str, Any]:
    item = {"status": status, "code": code, "title": title, "message": message}
    if details is not None:
        item["details"] = details
    return item


def _source_run_id(run: dict[str, Any]) -> str | None:
    source = run.get("source_production_run") or {}
    value = source.get("run_id")
    return str(value) if value else None


def _source_run_date(run: dict[str, Any]) -> str | None:
    source = run.get("source_production_run") or {}
    value = source.get("run_date")
    return str(value) if value else None


def _us_shadow_buy_count(run: dict[str, Any]) -> int:
    return sum(
        1
        for pick in run.get("picks") or []
        if str(pick.get("market") or "").upper() == MARKET
        and str(pick.get("shadow_signal") or "") in BUY_SIGNALS
    )


def _latest_by_source(runs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        source_run_id = _source_run_id(run)
        if source_run_id:
            grouped[source_run_id].append(run)

    unique_runs: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for source_run_id, items in sorted(grouped.items()):
        ordered = sorted(items, key=lambda row: _as_dt(row.get("generated_at")))
        latest = ordered[-1]
        unique_runs.append(latest)
        if len(items) > 1:
            duplicates.append({
                "source_run_id": source_run_id,
                "artifact_count": len(items),
                "kept_shadow_run_id": latest.get("run_id"),
                "shadow_run_ids": [row.get("run_id") for row in ordered],
                "proposed_strategy_versions": sorted({
                    str(row.get("proposed_strategy_version") or "")
                    for row in ordered
                    if row.get("proposed_strategy_version")
                }),
                "generated_at": [row.get("generated_at") for row in ordered],
            })
    return sorted(unique_runs, key=lambda row: _as_dt(row.get("generated_at"))), duplicates


def _criteria_from_shadow(shadow: dict[str, Any]) -> dict[str, Any]:
    decision = shadow.get("activation_decision") or {}
    crit = decision.get("criteria") or {}
    return {
        "min_shadow_runs": _int(crit.get("min_shadow_runs"), 10),
        "min_market_reviewed": _int(crit.get("min_market_reviewed"), 60),
        "min_coverage_pct": _num(crit.get("min_coverage_pct")) or 80.0,
        "min_hit_rate": _num(crit.get("min_hit_rate")) or 45.0,
        "primary_horizon": str(crit.get("primary_horizon") or HORIZON),
    }


def _us_summary(market_summary: list[dict[str, Any]], horizon: str = HORIZON) -> dict[str, Any]:
    for row in market_summary:
        if str(row.get("market") or "").upper() == MARKET and str(row.get("horizon") or "") == horizon:
            return row
    return {}


def fetch_latest_production_run(db_path: Path = DB_PATH) -> tuple[dict[str, Any], str | None]:
    if not db_path.exists():
        return {}, "missing_db"
    try:
        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            tables = {str(row[0]) for row in conn.execute("SHOW TABLES").fetchall()}
            if "recommendation_runs" not in tables:
                return {}, "missing_recommendation_runs"
            row = conn.execute(
                """
                SELECT run_id, run_date, generated_at, strategy_version, universe_scope, status
                FROM recommendation_runs
                WHERE universe_scope = 'system_tech_universe'
                  AND status = 'generated'
                ORDER BY generated_at DESC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return {}, "missing_latest_system_run"
            keys = ["run_id", "run_date", "generated_at", "strategy_version", "universe_scope", "status"]
            out = {key: value for key, value in zip(keys, row, strict=False)}
            return {key: _json_time(value) for key, value in out.items()}, None
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - platform-specific lock text
        return {}, f"{type(exc).__name__}: {exc}"


def fetch_outcomes(
    source_run_ids: list[str],
    *,
    horizons: tuple[str, ...] = shadow_eval.DEFAULT_HORIZONS,
) -> tuple[dict[tuple[str, str, str, str], dict[str, Any]], str | None]:
    try:
        return shadow_eval.fetch_source_outcomes(source_run_ids, horizons=horizons), None
    except Exception as exc:  # pragma: no cover - platform-specific lock text
        return {}, f"{type(exc).__name__}: {exc}"


def _outcome_sources(
    outcomes: dict[tuple[str, str, str, str], dict[str, Any]],
    *,
    market: str = MARKET,
    horizon: str = HORIZON,
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for (run_id, row_market, _symbol, row_horizon), outcome in outcomes.items():
        if str(row_market).upper() != market or str(row_horizon) != horizon:
            continue
        if outcome.get("alpha_pct") is None:
            continue
        counts[str(run_id)] += 1
    return dict(counts)


def _estimate_gap_days(
    *,
    min_runs: int,
    unique_sources: int,
    min_reviewed: int,
    reviewed: int,
    avg_us_buys_per_source: float,
) -> dict[str, Any]:
    source_gap = max(0, min_runs - unique_sources)
    sample_gap = max(0, min_reviewed - reviewed)
    mature_source_gap = math.ceil(sample_gap / avg_us_buys_per_source) if avg_us_buys_per_source > 0 else None
    needed = source_gap if mature_source_gap is None else max(source_gap, mature_source_gap)
    return {
        "unique_source_runs_needed": source_gap,
        "reviewed_samples_needed": sample_gap,
        "estimated_more_mature_source_runs_needed": mature_source_gap,
        "rough_pipeline_days_needed_at_one_source_per_day": needed,
        "note": "粗估：按每天 1 个唯一生产 run 估算；1D outcome 通常还要等下一次 US 收盘后才成熟。",
    }


def build_preflight(
    *,
    runs: list[dict[str, Any]],
    shadow_evidence: dict[str, Any],
    latest_production_run: dict[str, Any] | None = None,
    latest_production_error: str | None = None,
    outcomes: dict[tuple[str, str, str, str], dict[str, Any]] | None = None,
    outcome_error: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    latest_production_run = latest_production_run or {}
    outcomes = outcomes or {}
    criteria = _criteria_from_shadow(shadow_evidence)
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []

    source_unique_runs, duplicate_sources = _latest_by_source(runs)
    raw_artifact_count = len(runs)
    source_ids = [_source_run_id(run) for run in source_unique_runs if _source_run_id(run)]
    unique_source_count = len(source_ids)
    run_dates = sorted({d for d in (_source_run_date(run) for run in source_unique_runs) if d})
    latest_shadow = max(source_unique_runs, key=lambda row: _as_dt(row.get("generated_at"))) if source_unique_runs else None
    latest_shadow_source_id = _source_run_id(latest_shadow or {})

    if raw_artifact_count <= 0:
        checks.append(_check("FAIL", "shadow_artifacts_missing", "shadow 归档", "没有 shadow_tuning_run 归档，不能验证 US 试探门槛。"))
        blockers.append("缺少 shadow 归档。")
    elif duplicate_sources:
        checks.append(_check(
            "WARN",
            "duplicate_source_runs",
            "唯一 source run 计数",
            f"发现 {len(duplicate_sources)} 个 source run 被重复 shadow；试探门槛已按唯一 source run={unique_source_count} 计数。",
            duplicate_sources,
        ))
        warnings.append("shadow artifact 存在重复 source run，不能按 raw artifact 数放行。")
    else:
        checks.append(_check(
            "PASS",
            "unique_source_runs",
            "唯一 source run 计数",
            f"{unique_source_count} 个唯一 source run，未发现重复 source run。",
        ))

    latest_prod_id = str(latest_production_run.get("run_id") or "")
    if latest_production_error:
        checks.append(_check("WARN", "latest_production_run_unreadable", "最新生产 run", f"无法读取最新生产 run：{latest_production_error}"))
        warnings.append("无法确认 latest shadow 是否覆盖最新生产 run。")
    elif latest_prod_id and latest_shadow_source_id == latest_prod_id:
        checks.append(_check("PASS", "latest_shadow_matches_production", "最新 shadow 覆盖", f"latest source={latest_shadow_source_id}。"))
    elif latest_prod_id:
        checks.append(_check(
            "WARN",
            "latest_shadow_stale",
            "最新 shadow 覆盖",
            f"latest production={latest_prod_id}，latest shadow source={latest_shadow_source_id or '—'}。",
        ))
        warnings.append("最新生产推荐还没有对应 shadow 预检，不能用旧 shadow 结论放行。")

    if outcome_error:
        checks.append(_check("FAIL", "outcomes_unreadable", "pick_outcomes 可读性", f"无法读取 outcomes：{outcome_error}"))
        blockers.append("无法读取 pick_outcomes，不能验证 US 试探门槛。")

    market_summary = (
        shadow_eval.build_market_horizon_summary(
            source_unique_runs,
            outcomes,
            horizons=shadow_eval.DEFAULT_HORIZONS,
        )
        if not outcome_error
        else []
    )
    us = _us_summary(market_summary, str(criteria.get("primary_horizon") or HORIZON))
    reviewed = _int(us.get("reviewed_shadow_buy_count"))
    coverage = _num(us.get("shadow_review_coverage_pct")) or 0.0
    alpha = _num(us.get("shadow_avg_alpha_pct"))
    hit = _num(us.get("shadow_win_rate"))
    total_us_shadow_buys = sum(_us_shadow_buy_count(run) for run in source_unique_runs)
    avg_us_buys = total_us_shadow_buys / unique_source_count if unique_source_count else 0.0
    outcome_source_counts = _outcome_sources(outcomes)
    mature_source_count = len(outcome_source_counts)

    if not outcome_error:
        checks.append(_check(
            "PASS" if mature_source_count else "WARN",
            "us_outcome_join",
            "US outcome 回连",
            f"{mature_source_count}/{unique_source_count} 个唯一 source run 已有 US 1D outcome；reviewed={reviewed}。",
            outcome_source_counts or None,
        ))
        if mature_source_count <= 0:
            warnings.append("US 1D outcome 尚未成熟，试探门槛不能通过。")

    min_runs = _int(criteria.get("min_shadow_runs"), 10)
    min_reviewed = _int(criteria.get("min_market_reviewed"), 60)
    min_coverage = _num(criteria.get("min_coverage_pct")) or 80.0
    min_hit = _num(criteria.get("min_hit_rate")) or 45.0

    gaps: list[str] = []
    if unique_source_count < min_runs:
        gaps.append(f"唯一 source run {unique_source_count}/{min_runs}")
    if reviewed < min_reviewed:
        gaps.append(f"US 1D reviewed {reviewed}/{min_reviewed}")
    if coverage < min_coverage:
        gaps.append(f"覆盖 {coverage:.0f}%/{min_coverage:.0f}%")
    if alpha is None:
        gaps.append("US shadow alpha 待样本成熟")
    elif alpha <= 0:
        gaps.append(f"US shadow alpha {alpha:.2f}% <= 0")
    if hit is not None and hit < min_hit:
        gaps.append(f"US shadow 命中 {hit:.1f}%/{min_hit:.0f}%")

    trial_ready = not blockers and not gaps
    checks.append(_check(
        "PASS" if trial_ready else "WARN",
        "us_trial_gate_unique_source",
        "US 可小仓试探预检",
        (
            f"source {unique_source_count}/{min_runs} · reviewed {reviewed}/{min_reviewed} · "
            f"alpha {_fmt_pct(alpha)} · 命中 {_fmt_pct(hit)}"
        ),
        gaps or None,
    ))

    estimate = _estimate_gap_days(
        min_runs=min_runs,
        unique_sources=unique_source_count,
        min_reviewed=min_reviewed,
        reviewed=reviewed,
        avg_us_buys_per_source=avg_us_buys,
    )
    if gaps:
        warnings.extend(gaps)

    status = "FAIL" if blockers else ("WARN" if warnings else "PASS")
    return {
        "schema_version": "us_shadow_preflight_v1",
        "generated_at": now.isoformat(timespec="seconds"),
        "status": status,
        "safety_boundary": (
            "Read-only US shadow preflight. It only validates archived shadow artifacts "
            "and source pick_outcomes; it does not modify formulas, strategy versions, "
            "recommendation tables, watchlist, or real holdings."
        ),
        "criteria": criteria,
        "summary": {
            "raw_shadow_artifact_count": raw_artifact_count,
            "unique_source_run_count": unique_source_count,
            "source_run_date_count": len(run_dates),
            "duplicate_source_run_count": len(duplicate_sources),
            "latest_shadow_source_run_id": latest_shadow_source_id,
            "latest_production_run_id": latest_prod_id or None,
            "us_shadow_buy_count_unique_sources": total_us_shadow_buys,
            "avg_us_shadow_buys_per_source": round(avg_us_buys, 2),
            "us_mature_source_run_count": mature_source_count,
            "trial_ready": trial_ready,
        },
        "trial_gate": {
            "ready": trial_ready,
            "market": MARKET,
            "horizon": str(criteria.get("primary_horizon") or HORIZON),
            "unique_source_run_count": unique_source_count,
            "raw_shadow_artifact_count": raw_artifact_count,
            "reviewed_shadow_buy_count": reviewed,
            "shadow_review_coverage_pct": coverage,
            "shadow_avg_alpha_pct": alpha,
            "shadow_win_rate": hit,
            "gaps": gaps,
            "estimate": estimate,
        },
        "duplicate_sources": duplicate_sources,
        "source_runs": [
            {
                "source_run_id": _source_run_id(run),
                "source_run_date": _source_run_date(run),
                "shadow_run_id": run.get("run_id"),
                "generated_at": run.get("generated_at"),
                "proposed_strategy_version": run.get("proposed_strategy_version"),
                "us_shadow_buy_count": _us_shadow_buy_count(run),
                "us_1d_outcome_count": outcome_source_counts.get(str(_source_run_id(run)), 0),
            }
            for run in source_unique_runs
        ],
        "checks": checks,
        "blockers": blockers,
        "warnings": list(dict.fromkeys(warnings)),
    }


def to_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    gate = payload.get("trial_gate") or {}
    estimate = gate.get("estimate") or {}
    lines = [
        "# US Shadow Preflight Check",
        "",
        f"Generated: {payload.get('generated_at')}",
        f"Status: **{payload.get('status')}**",
        f"Trial ready: **{gate.get('ready')}**",
        "",
        payload.get("safety_boundary", ""),
        "",
        "## Summary",
        "",
        f"- Raw shadow artifacts: {summary.get('raw_shadow_artifact_count')}",
        f"- Unique source runs: {summary.get('unique_source_run_count')}",
        f"- Duplicate source runs: {summary.get('duplicate_source_run_count')}",
        f"- Mature US source runs: {summary.get('us_mature_source_run_count')}",
        f"- US reviewed: {gate.get('reviewed_shadow_buy_count')} ({_fmt_pct(gate.get('shadow_review_coverage_pct'))} coverage)",
        f"- US alpha/hit: {_fmt_pct(gate.get('shadow_avg_alpha_pct'))} / {_fmt_pct(gate.get('shadow_win_rate'))}",
        f"- Rough wait: {estimate.get('rough_pipeline_days_needed_at_one_source_per_day')} pipeline days",
        "",
        "## Checks",
        "",
    ]
    for item in payload.get("checks") or []:
        lines.append(f"- [{item.get('status')}] {item.get('title')}: {item.get('message')}")
        details = item.get("details")
        if isinstance(details, list):
            for detail in details[:8]:
                lines.append(f"  - {detail}")
        elif isinstance(details, dict):
            lines.append(f"  - {details}")
    if payload.get("blockers"):
        lines.extend(["", "## Blockers", ""])
        for item in payload["blockers"]:
            lines.append(f"- {item}")
    if payload.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        for item in payload["warnings"]:
            lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def run() -> dict[str, Any]:
    runs = shadow_eval.load_shadow_runs()
    shadow_evidence = _load_json(SHADOW_EVIDENCE_JSON)
    source_unique_runs, _duplicates = _latest_by_source(runs)
    source_run_ids = sorted({
        str(_source_run_id(run))
        for run in source_unique_runs
        if _source_run_id(run)
    })
    latest_run, latest_error = fetch_latest_production_run(DB_PATH)
    outcomes, outcome_error = fetch_outcomes(source_run_ids)
    return build_preflight(
        runs=runs,
        shadow_evidence=shadow_evidence,
        latest_production_run=latest_run,
        latest_production_error=latest_error,
        outcomes=outcomes,
        outcome_error=outcome_error,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run US shadow preflight checks.")
    parser.add_argument("--json", action="store_true", help="Print full JSON payload.")
    parser.add_argument("--strict", action="store_true", help="Return non-zero when preflight status is FAIL.")
    args = parser.parse_args(argv)

    payload = run()
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    OUT_MD.write_text(to_markdown(payload), encoding="utf-8")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        summary = payload.get("summary") or {}
        gate = payload.get("trial_gate") or {}
        print(f"US shadow preflight: {payload.get('status')}")
        print(
            f"  unique_source_runs={summary.get('unique_source_run_count')}/"
            f"{(payload.get('criteria') or {}).get('min_shadow_runs')} · "
            f"raw_artifacts={summary.get('raw_shadow_artifact_count')} · "
            f"duplicates={summary.get('duplicate_source_run_count')}"
        )
        print(
            f"  reviewed={gate.get('reviewed_shadow_buy_count')}/"
            f"{(payload.get('criteria') or {}).get('min_market_reviewed')} · "
            f"alpha={_fmt_pct(gate.get('shadow_avg_alpha_pct'))} · "
            f"hit={_fmt_pct(gate.get('shadow_win_rate'))} · "
            f"trial_ready={gate.get('ready')}"
        )
        for item in (payload.get("blockers") or [])[:6]:
            print(f"  [BLOCKER] {item}")
        for item in (payload.get("warnings") or [])[:8]:
            print(f"  [WARN] {item}")
        print(f"  JSON: {OUT_JSON}")
    if args.strict and payload.get("status") == "FAIL":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
