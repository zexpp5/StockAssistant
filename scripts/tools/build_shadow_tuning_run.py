#!/usr/bin/env python3
"""Build a read-only shadow recommendation run from the tuning proposal.

This script compares the latest production AI recommendation run with the
current SHADOW_ONLY tuning proposal. It intentionally writes only JSON/Markdown
artifacts and does not insert into recommendation_runs, recommendation_picks,
portfolio_plans, watchlist, or holdings.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import DB_PATH, get_db  # noqa: E402
from scripts.tools import strategy_tuning_proposal  # noqa: E402
from scripts.tools.replay_weight_variants import (  # noqa: E402
    VARIANTS as WEIGHT_VARIANTS,
    variant_score,
)

OUT_JSON = REPO / "data" / "latest" / "shadow_tuning_run.json"
OUT_MD = REPO / "data" / "reports" / "shadow_tuning_run.md"
ARCHIVE_DIR = REPO / "data" / "shadow_tuning_runs"
REPORT_ARCHIVE_DIR = REPO / "data" / "reports" / "shadow_tuning_runs"
PROPOSAL_JSON = REPO / "data" / "latest" / "strategy_tuning_proposal.json"

MARKET_LABELS = {"US": "美股", "CN": "A股", "HK": "港股"}
BUY_SIGNALS = {"buy", "strong_buy"}


def _safe_json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        try:
            return ast.literal_eval(str(value))
        except Exception:
            return fallback


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _round(value: Any, digits: int = 2) -> float:
    return round(_as_float(value), digits)


def _signal(total_score: float) -> str:
    if total_score >= 60:
        return "buy"
    if total_score >= 50:
        return "watch"
    return "avoid"


def _rating(total_score: float) -> str:
    if total_score >= 75:
        return "strong_buy"
    if total_score >= 60:
        return "buy"
    if total_score >= 50:
        return "watch"
    return "avoid"


def _normalize_risk_flags(value: Any) -> list[str]:
    raw = _safe_json(value, [])
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raw = [raw]

    flags: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            code = item.get("code") or item.get("risk_flag") or item.get("flag")
            if code:
                flags.append(str(code))
        elif item not in (None, ""):
            flags.append(str(item))
    return flags


def load_proposal(path: Path = PROPOSAL_JSON, *, strategy_version: str | None = None, horizon: str | None = None) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return strategy_tuning_proposal.build_proposal(strategy_version=strategy_version, horizon=horizon)


def _tables(conn: Any) -> set[str]:
    try:
        return {str(row[0]) for row in conn.execute("SHOW TABLES").fetchall()}
    except Exception:
        return set()


def fetch_latest_production_run() -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    conn = get_db(read_only=True)
    try:
        tables = _tables(conn)
        if "recommendation_runs" not in tables or "recommendation_picks" not in tables:
            return None, []
        run = conn.execute(
            """
            SELECT run_id, run_date, strategy_version, model_version, universe_scope,
                   data_cutoff_at, generated_at, status, notes
            FROM recommendation_runs
            WHERE universe_scope = 'system_tech_universe'
              AND status = 'generated'
            ORDER BY generated_at DESC
            LIMIT 1
            """
        ).fetchone()
        if not run:
            return None, []

        keys = [
            "run_id", "run_date", "strategy_version", "model_version",
            "universe_scope", "data_cutoff_at", "generated_at", "status", "notes",
        ]
        run_meta = {key: value for key, value in zip(keys, run, strict=False)}
        rows = conn.execute(
            """
            SELECT market, symbol, name, rank, rating, signal, total_score,
                   factor_scores_json, recommendation_reason, risk_flags_json,
                   entry_price, entry_currency, universe_scope, source_origin, created_at
            FROM recommendation_picks
            WHERE run_id = ?
            ORDER BY rank, market, symbol
            """,
            [run_meta["run_id"]],
        ).fetchall()
    finally:
        conn.close()

    picks: list[dict[str, Any]] = []
    pick_keys = [
        "market", "symbol", "name", "rank", "rating", "signal", "total_score",
        "factor_scores_json", "recommendation_reason", "risk_flags_json",
        "entry_price", "entry_currency", "universe_scope", "source_origin", "created_at",
    ]
    for row in rows:
        pick = {key: value for key, value in zip(pick_keys, row, strict=False)}
        pick["factor_scores"] = _safe_json(pick.get("factor_scores_json"), {})
        pick["risk_flags"] = _normalize_risk_flags(pick.get("risk_flags_json"))
        picks.append(pick)
    return run_meta, picks


def _market_action_map(proposal: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("market")): row
        for row in proposal.get("market_actions") or []
        if row.get("market")
    }


def _factor_actions_by_market(proposal: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    actions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in proposal.get("factor_actions") or []:
        market = str(row.get("market") or "")
        if market:
            actions[market].append(row)
    return actions


def _gate_actions_by_market_flag(proposal: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    actions: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in proposal.get("gate_actions") or []:
        market = str(row.get("market") or "*")
        flag = str(row.get("risk_flag") or "")
        if flag:
            actions[(market, flag)].append(row)
    return actions


def _factor_value(pick: dict[str, Any], factor: str) -> float:
    factor_scores = pick.get("factor_scores") or {}
    if not isinstance(factor_scores, dict):
        return 0.0
    return _as_float(factor_scores.get(factor), 0.0)


def _append_adjustment(
    adjustments: list[dict[str, Any]],
    *,
    source: str,
    amount: float,
    reason: str,
    detail: str | None = None,
) -> None:
    if abs(amount) < 0.01:
        return
    row = {"source": source, "score_delta": round(amount, 2), "reason": reason}
    if detail:
        row["detail"] = detail
    adjustments.append(row)


def _apply_factor_actions(pick: dict[str, Any], actions: list[dict[str, Any]]) -> tuple[float, list[dict[str, Any]], list[str]]:
    score_delta = 0.0
    adjustments: list[dict[str, Any]] = []
    tags: list[str] = []

    for action in actions:
        factor = str(action.get("factor") or "")
        action_type = str(action.get("action") or "")
        value = _factor_value(pick, factor)
        delta = 0.0
        reason = str(action.get("proposed_change") or action_type)

        if action_type == "convert_to_gate_only":
            delta = -min(max(value - 50.0, 0.0) * 0.08, 6.0)
        elif action_type == "reduce_weight_and_require_confirmation":
            delta = -min(max(value - 50.0, 0.0) * 0.08, 6.0)
            momentum = _factor_value(pick, "momentum")
            if value >= 80 and momentum < 60:
                delta -= 4.0
                tags.append("valuation_requires_price_action_confirmation")
        elif action_type == "reduce_or_zero_weight":
            delta = -min(max(value - 50.0, 0.0) * 0.12, 8.0)
        elif action_type == "reduce_weight_until_multihorizon_confirms":
            delta = -min(max(value - 50.0, 0.0) * 0.12, 8.0)
            if value >= 80:
                tags.append("reversal_requires_5d_20d_confirmation")
        elif action_type == "no_standalone_boost":
            delta = -min(max(value - 70.0, 0.0) * 0.08, 3.0)

        _append_adjustment(
            adjustments,
            source=f"factor:{factor}",
            amount=delta,
            reason=reason,
            detail=f"{factor}={value:.2f}",
        )
        score_delta += delta

    return score_delta, adjustments, tags


def _apply_gate_actions(
    pick: dict[str, Any],
    actions_by_flag: dict[tuple[str, str], list[dict[str, Any]]],
) -> tuple[float, list[dict[str, Any]], bool, list[str]]:
    market = str(pick.get("market") or "")
    score_delta = 0.0
    force_watch = False
    adjustments: list[dict[str, Any]] = []
    tags: list[str] = []

    for flag in pick.get("risk_flags") or []:
        gate_actions = list(actions_by_flag.get((market, flag), []))
        gate_actions.extend(actions_by_flag.get(("*", flag), []))
        for action in gate_actions:
            action_type = str(action.get("action") or "")
            proposed = str(action.get("proposed_change") or action_type)
            if action_type == "demote_buy_to_watch_or_score_haircut":
                delta = -10.0
                tags.append("momentum_reuse_haircut")
            elif action_type == "strengthen_overheat_gate":
                delta = -15.0
                force_watch = True
                tags.append("overheated_requires_buy_before_review")
            else:
                delta = -5.0
            _append_adjustment(
                adjustments,
                source=f"gate:{flag}",
                amount=delta,
                reason=proposed,
                detail=str(action.get("evidence") or ""),
            )
            score_delta += delta

    return score_delta, adjustments, force_watch, tags


def apply_shadow_transform(picks: list[dict[str, Any]], proposal: dict[str, Any]) -> list[dict[str, Any]]:
    market_actions = _market_action_map(proposal)
    factor_actions = _factor_actions_by_market(proposal)
    gate_actions = _gate_actions_by_market_flag(proposal)

    transformed: list[dict[str, Any]] = []
    for pick in picks:
        market = str(pick.get("market") or "")
        original_score = _round(pick.get("total_score"))
        market_action = market_actions.get(market, {})
        market_status = str(market_action.get("status") or "unchanged")
        recommendation_mode = str(market_action.get("recommendation_mode") or "keep_current")
        market_multiplier = _as_float(market_action.get("portfolio_multiplier"), 1.0)

        factor_delta, factor_adjustments, factor_tags = _apply_factor_actions(pick, factor_actions.get(market, []))
        gate_delta, gate_adjustments, force_watch, gate_tags = _apply_gate_actions(pick, gate_actions)
        shadow_score = max(0.0, min(100.0, original_score + factor_delta + gate_delta))
        shadow_signal = _signal(shadow_score)
        if force_watch and shadow_signal == "buy":
            shadow_signal = "watch"
        shadow_rating = _rating(shadow_score)
        if shadow_signal == "watch" and shadow_rating in BUY_SIGNALS:
            shadow_rating = "watch"

        original_signal = str(pick.get("signal") or _signal(original_score))
        demoted = original_signal in BUY_SIGNALS and shadow_signal not in BUY_SIGNALS
        production_portfolio_eligible = (
            shadow_signal in BUY_SIGNALS
            and recommendation_mode not in {"research_only_until_shadow_passes", "keep_current_until_alpha_available"}
            and market_status not in {"degraded", "evidence_pending"}
        )

        action = "keep"
        if demoted:
            action = "demote_to_watch" if shadow_signal == "watch" else "demote_to_avoid"
        elif abs(factor_delta + gate_delta) >= 0.01:
            action = "score_haircut"
        if market_status == "degraded" and shadow_signal in BUY_SIGNALS:
            action = "research_only"
        elif market_status == "evidence_pending" and shadow_signal in BUY_SIGNALS:
            action = "evidence_pending"

        transformed.append({
            "market": market,
            "market_label": MARKET_LABELS.get(market, market),
            "symbol": pick.get("symbol"),
            "name": pick.get("name"),
            "original_rank": pick.get("rank"),
            "original_rating": pick.get("rating"),
            "original_signal": original_signal,
            "original_score": original_score,
            "shadow_rating": shadow_rating,
            "shadow_signal": shadow_signal,
            "shadow_score": round(shadow_score, 2),
            "score_delta": round(shadow_score - original_score, 2),
            "market_status": market_status,
            "shadow_recommendation_mode": recommendation_mode,
            "shadow_portfolio_multiplier": market_multiplier,
            "shadow_portfolio_eligible": shadow_signal in BUY_SIGNALS,
            "production_portfolio_eligible": production_portfolio_eligible,
            "action": action,
            "demoted": demoted,
            "risk_flags": pick.get("risk_flags") or [],
            "shadow_tags": sorted(set(factor_tags + gate_tags)),
            "adjustments": factor_adjustments + gate_adjustments,
            "entry_price": pick.get("entry_price"),
            "entry_currency": pick.get("entry_currency"),
            "source_origin": pick.get("source_origin"),
            "universe_scope": pick.get("universe_scope"),
        })

    transformed.sort(key=lambda row: (-_as_float(row.get("shadow_score")), str(row.get("market")), str(row.get("symbol"))))
    for idx, row in enumerate(transformed, start=1):
        row["shadow_rank"] = idx

    eligible = [row for row in transformed if row.get("shadow_portfolio_eligible")]
    total_raw_weight = sum(
        _as_float(row.get("shadow_score")) * _as_float(row.get("shadow_portfolio_multiplier"), 1.0)
        for row in eligible
    )
    for row in transformed:
        if row.get("shadow_portfolio_eligible") and total_raw_weight > 0:
            raw_weight = _as_float(row.get("shadow_score")) * _as_float(row.get("shadow_portfolio_multiplier"), 1.0)
            row["shadow_portfolio_weight_hint_pct"] = round(raw_weight / total_raw_weight * 100.0, 2)
        else:
            row["shadow_portfolio_weight_hint_pct"] = 0.0

    return transformed


def apply_weight_variant_transform(
    picks: list[dict[str, Any]],
    *,
    variant_name: str,
    weights: dict[str, float],
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """纯权重重打分变体（§19.3 第二步）：用 pick 落库时的因子分快照按新权重重算总分。

    与 apply_shadow_transform 的扣分/降级互斥 —— 变体只测「换公式」这一件事，
    不叠加 factor/gate haircut，保证归因干净。组合口径与回放工具一致：每市场
    按变体分取 top_k 标 shadow_portfolio_top（评估时用它而非 signal 阈值）。
    """
    transformed: list[dict[str, Any]] = []
    for pick in picks:
        factor_scores = pick.get("factor_scores") or {}
        if not isinstance(factor_scores, dict):
            factor_scores = {}
        shadow_score, missing = variant_score(factor_scores, weights)
        shadow_score = max(0.0, min(100.0, shadow_score))
        original_score = _round(pick.get("total_score"))
        original_signal = str(pick.get("signal") or _signal(original_score))
        transformed.append({
            "market": str(pick.get("market") or ""),
            "market_label": MARKET_LABELS.get(str(pick.get("market") or ""), str(pick.get("market") or "")),
            "symbol": pick.get("symbol"),
            "name": pick.get("name"),
            "original_rank": pick.get("rank"),
            "original_rating": pick.get("rating"),
            "original_signal": original_signal,
            "original_score": original_score,
            "shadow_rating": _rating(shadow_score),
            "shadow_signal": _signal(shadow_score),
            "shadow_score": round(shadow_score, 2),
            "score_delta": round(shadow_score - original_score, 2),
            "market_status": "unchanged",
            "shadow_recommendation_mode": "keep_current",
            "shadow_portfolio_multiplier": 1.0,
            "action": "reweight",
            "demoted": False,
            "risk_flags": pick.get("risk_flags") or [],
            "shadow_tags": [f"weight_variant:{variant_name}"],
            "adjustments": [{
                "source": f"weight_variant:{variant_name}",
                "score_delta": round(shadow_score - original_score, 2),
                "reason": "按变体权重从因子分快照重算总分",
                "detail": f"missing_factor_fills={missing}",
            }] if abs(shadow_score - original_score) >= 0.01 else [],
            "entry_price": pick.get("entry_price"),
            "entry_currency": pick.get("entry_currency"),
            "source_origin": pick.get("source_origin"),
            "universe_scope": pick.get("universe_scope"),
        })

    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in transformed:
        by_market[str(row.get("market") or "")].append(row)
    for market_rows in by_market.values():
        market_rows.sort(key=lambda row: (-_as_float(row.get("shadow_score")), str(row.get("symbol"))))
        for idx, row in enumerate(market_rows, start=1):
            row["shadow_market_rank"] = idx
            row["shadow_portfolio_top"] = idx <= top_k
            # 评估/组合口径 = top_k 成员；eligible 沿用同一含义,便于复用现有汇总
            row["shadow_portfolio_eligible"] = idx <= top_k
            row["production_portfolio_eligible"] = idx <= top_k

    transformed.sort(key=lambda row: (-_as_float(row.get("shadow_score")), str(row.get("market")), str(row.get("symbol"))))
    for idx, row in enumerate(transformed, start=1):
        row["shadow_rank"] = idx
        row["shadow_portfolio_weight_hint_pct"] = 0.0
    return transformed


def _market_summary(rows: list[dict[str, Any]], proposal: dict[str, Any]) -> list[dict[str, Any]]:
    market_actions = _market_action_map(proposal)
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_market[str(row.get("market") or "")].append(row)

    summary = []
    for market in sorted(by_market):
        items = by_market[market]
        original_buy = sum(1 for item in items if str(item.get("original_signal")) in BUY_SIGNALS)
        shadow_buy = sum(1 for item in items if str(item.get("shadow_signal")) in BUY_SIGNALS)
        production_eligible = sum(1 for item in items if item.get("production_portfolio_eligible"))
        demoted = sum(1 for item in items if item.get("demoted"))
        avg_delta = sum(_as_float(item.get("score_delta")) for item in items) / max(len(items), 1)
        market_action = market_actions.get(market, {})
        summary.append({
            "market": market,
            "label": MARKET_LABELS.get(market, market),
            "status": market_action.get("status", "unchanged"),
            "recommendation_mode": market_action.get("recommendation_mode", "keep_current"),
            "portfolio_multiplier": _as_float(market_action.get("portfolio_multiplier"), 1.0),
            "source_count": len(items),
            "original_buy_count": original_buy,
            "shadow_buy_count": shadow_buy,
            "production_portfolio_eligible_count": production_eligible,
            "demoted_count": demoted,
            "avg_score_delta": round(avg_delta, 2),
            "reason": market_action.get("reason", ""),
        })
    return summary


def build_shadow_run(
    *,
    proposal: dict[str, Any],
    source_run: dict[str, Any] | None,
    source_picks: list[dict[str, Any]],
    weight_variant: str | None = None,
) -> dict[str, Any]:
    generated_at = datetime.now().isoformat(timespec="seconds")
    if weight_variant:
        run_id = f"shadow_{datetime.now().strftime('%Y%m%d_%H%M%S')}_wv_{weight_variant}"
    else:
        run_id = f"shadow_{datetime.now().strftime('%Y%m%d_%H%M%S')}_tuning"

    if not source_run:
        return {
            "schema_version": "shadow_tuning_run_v1",
            "run_id": run_id,
            "generated_at": generated_at,
            "status": "NO_PRODUCTION_RUN",
            "safety_boundary": (
                "Read-only shadow artifact. No production recommendation or portfolio tables were modified."
            ),
            "db_path": str(DB_PATH),
            "proposed_strategy_version": proposal.get("proposed_strategy_version"),
            "source_production_run": None,
            "market_summary": [],
            "picks": [],
            "notes": ["No latest generated system_tech_universe production run was found."],
        }

    if weight_variant:
        weights = WEIGHT_VARIANTS[weight_variant]
        shadow_picks = apply_weight_variant_transform(
            source_picks, variant_name=weight_variant, weights=weights)
    else:
        weights = None
        shadow_picks = apply_shadow_transform(source_picks, proposal)
    market_summary = _market_summary(shadow_picks, proposal if not weight_variant else {})
    demoted_count = sum(1 for row in shadow_picks if row.get("demoted"))
    production_eligible_count = sum(1 for row in shadow_picks if row.get("production_portfolio_eligible"))

    return {
        "schema_version": "shadow_tuning_run_v1",
        "run_id": run_id,
        "generated_at": generated_at,
        "status": "SHADOW_ONLY",
        "weight_variant": weight_variant,
        "weight_override": weights,
        "safety_boundary": (
            "Read-only shadow artifact. It reads the latest production system_tech_universe run, "
            "but does not write recommendation_runs, recommendation_picks, portfolio_plans, "
            "watchlist, or real holdings."
        ),
        "db_path": str(DB_PATH),
        "source_production_run": {
            key: (value.isoformat() if hasattr(value, "isoformat") else value)
            for key, value in source_run.items()
        },
        # 变体 run 用独立版本号,避免 evaluate 的 dedupe 与主链 haircut run 互相顶掉
        "proposed_strategy_version": (
            f"weight_variant:{weight_variant}" if weight_variant
            else proposal.get("proposed_strategy_version")
        ),
        "proposal_generated_at": proposal.get("generated_at"),
        "source_proposal_status": proposal.get("status"),
        "summary": {
            "source_pick_count": len(source_picks),
            "shadow_pick_count": len(shadow_picks),
            "demoted_count": demoted_count,
            "production_portfolio_eligible_count": production_eligible_count,
            "us_status": next((row for row in market_summary if row.get("market") == "US"), None),
        },
        "market_summary": market_summary,
        "top_shadow_picks": shadow_picks[:20],
        "picks": shadow_picks,
        "activation_criteria": proposal.get("activation_criteria") or [],
    }


def _fmt_score(value: Any) -> str:
    return f"{_as_float(value):.2f}"


def to_markdown(payload: dict[str, Any], *, top_n: int = 20) -> str:
    lines = [
        "# Shadow Tuning Run",
        "",
        f"Generated: {payload.get('generated_at')}",
        f"Status: **{payload.get('status')}**",
        f"Shadow run: **{payload.get('run_id')}**",
        f"Proposed version: **{payload.get('proposed_strategy_version')}**",
        "",
        payload.get("safety_boundary", ""),
        "",
    ]
    source_run = payload.get("source_production_run")
    if source_run:
        lines.extend([
            "## Source Production Run",
            "",
            f"- run_id: `{source_run.get('run_id')}`",
            f"- strategy_version: `{source_run.get('strategy_version')}`",
            f"- generated_at: `{source_run.get('generated_at')}`",
            "",
        ])
    else:
        for note in payload.get("notes") or []:
            lines.append(f"- {note}")
        return "\n".join(lines) + "\n"

    summary = payload.get("summary") or {}
    lines.extend([
        "## Summary",
        "",
        f"- Source picks: {summary.get('source_pick_count', 0)}",
        f"- Demoted from buy: {summary.get('demoted_count', 0)}",
        f"- Production-eligible after shadow gates: {summary.get('production_portfolio_eligible_count', 0)}",
        "",
        "## Market Summary",
        "",
        "| Market | Status | Mode | Multiplier | Source | Orig Buy | Shadow Buy | Prod Eligible | Demoted | Avg Delta | Reason |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ])
    for row in payload.get("market_summary") or []:
        lines.append(
            f"| {row.get('label')} | {row.get('status')} | {row.get('recommendation_mode')} | "
            f"{row.get('portfolio_multiplier')} | {row.get('source_count')} | "
            f"{row.get('original_buy_count')} | {row.get('shadow_buy_count')} | "
            f"{row.get('production_portfolio_eligible_count')} | {row.get('demoted_count')} | "
            f"{row.get('avg_score_delta')} | {row.get('reason')} |"
        )

    lines.extend([
        "",
        f"## Top {top_n} Shadow Picks",
        "",
        "| Shadow Rank | Market | Symbol | Name | Orig Rank | Orig | Shadow | Action | Prod Eligible | Key Adjustments |",
        "|---:|---|---|---|---:|---|---|---|---|---|",
    ])
    for row in (payload.get("picks") or [])[:top_n]:
        adjustments = row.get("adjustments") or []
        if adjustments:
            key_adjustments = "; ".join(
                f"{adj.get('source')} {adj.get('score_delta')}" for adj in adjustments[:3]
            )
        else:
            key_adjustments = "-"
        lines.append(
            f"| {row.get('shadow_rank')} | {row.get('market_label')} | {row.get('symbol')} | "
            f"{row.get('name') or ''} | {row.get('original_rank')} | "
            f"{row.get('original_signal')} {_fmt_score(row.get('original_score'))} | "
            f"{row.get('shadow_signal')} {_fmt_score(row.get('shadow_score'))} | "
            f"{row.get('action')} | {row.get('production_portfolio_eligible')} | {key_adjustments} |"
        )

    lines.extend([
        "",
        "## Activation Criteria",
        "",
    ])
    for item in payload.get("activation_criteria") or []:
        lines.append(f"- {item}")

    return "\n".join(lines) + "\n"


def write_outputs(
    payload: dict[str, Any],
    *,
    output_json: Path = OUT_JSON,
    output_md: Path = OUT_MD,
    top_n: int = 20,
    archive: bool = True,
) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    artifact_paths = {
        "latest_json": str(output_json),
        "latest_md": str(output_md),
    }
    archive_json = None
    archive_md = None
    if archive and payload.get("run_id"):
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        REPORT_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        archive_json = ARCHIVE_DIR / f"{payload['run_id']}.json"
        archive_md = REPORT_ARCHIVE_DIR / f"{payload['run_id']}.md"
        artifact_paths["archive_json"] = str(archive_json)
        artifact_paths["archive_md"] = str(archive_md)
    payload["artifact_paths"] = artifact_paths
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    output_md.write_text(to_markdown(payload, top_n=top_n), encoding="utf-8")
    if archive_json and archive_md:
        archive_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        archive_md.write_text(to_markdown(payload, top_n=top_n), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build read-only shadow tuning run artifacts.")
    parser.add_argument("--proposal-json", default=str(PROPOSAL_JSON))
    parser.add_argument("--output-json", default=str(OUT_JSON))
    parser.add_argument("--output-md", default=str(OUT_MD))
    parser.add_argument("--strategy-version", default="latest")
    parser.add_argument("--horizon", default="1d")
    parser.add_argument("--top-n-report", type=int, default=20)
    parser.add_argument("--no-archive", action="store_true", help="Only write latest artifacts; do not archive PIT shadow run.")
    parser.add_argument(
        "--weight-variant",
        choices=sorted(WEIGHT_VARIANTS),
        help="纯权重重打分变体(§19.3 第二步);与 haircut proposal 互斥,归因干净。",
    )
    args = parser.parse_args(argv)

    output_json = Path(args.output_json)
    output_md = Path(args.output_md)
    if args.weight_variant:
        # 变体有独立 latest 文件,不覆盖主链 shadow_tuning_run.json
        if output_json == OUT_JSON:
            output_json = OUT_JSON.with_name(f"shadow_tuning_run_wv_{args.weight_variant}.json")
        if output_md == OUT_MD:
            output_md = OUT_MD.with_name(f"shadow_tuning_run_wv_{args.weight_variant}.md")

    proposal = load_proposal(Path(args.proposal_json), strategy_version=args.strategy_version, horizon=args.horizon)
    source_run, source_picks = fetch_latest_production_run()
    payload = build_shadow_run(
        proposal=proposal,
        source_run=source_run,
        source_picks=source_picks,
        weight_variant=args.weight_variant,
    )
    write_outputs(
        payload,
        output_json=output_json,
        output_md=output_md,
        top_n=args.top_n_report,
        archive=not args.no_archive,
    )
    print(
        json.dumps(
            {
                "status": payload.get("status"),
                "run_id": payload.get("run_id"),
                "source_run_id": (payload.get("source_production_run") or {}).get("run_id"),
                "weight_variant": args.weight_variant,
                "output_json": str(output_json),
                "output_md": str(output_md),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
