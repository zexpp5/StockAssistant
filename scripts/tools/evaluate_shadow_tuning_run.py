#!/usr/bin/env python3
"""Evaluate archived shadow tuning runs against production pick_outcomes.

Shadow runs are deliberately file artifacts, not production recommendation
records. This evaluator joins each archived shadow pick back to its source
production run_id and reuses the point-in-time pick_outcomes for that source
run. It produces a production activation gate without mutating recommendation
or portfolio tables.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import DB_PATH, get_db  # noqa: E402

LATEST_SHADOW_JSON = REPO / "data" / "latest" / "shadow_tuning_run.json"
SHADOW_ARCHIVE_DIR = REPO / "data" / "shadow_tuning_runs"
OUT_JSON = REPO / "data" / "latest" / "shadow_tuning_evidence.json"
OUT_MD = REPO / "data" / "reports" / "shadow_tuning_evidence.md"

BUY_SIGNALS = {"buy", "strong_buy"}
MARKET_LABELS = {"US": "美股", "CN": "A股", "HK": "港股"}
DEFAULT_HORIZONS = ("1d", "5d", "20d")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "")
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return datetime.min


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _source_run_id(run: dict[str, Any]) -> str | None:
    source = run.get("source_production_run") or {}
    value = source.get("run_id")
    return str(value) if value else None


def load_shadow_runs(
    *,
    latest_path: Path = LATEST_SHADOW_JSON,
    archive_dir: Path = SHADOW_ARCHIVE_DIR,
    include_latest: bool = True,
) -> list[dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {}
    if archive_dir.exists():
        for path in sorted(archive_dir.glob("shadow_*.json")):
            payload = _read_json(path)
            if payload and payload.get("run_id"):
                runs[str(payload["run_id"])] = payload
    if include_latest and latest_path.exists():
        payload = _read_json(latest_path)
        if payload and payload.get("run_id"):
            runs[str(payload["run_id"])] = payload
    return sorted(runs.values(), key=lambda row: _as_dt(row.get("generated_at")))


def dedupe_shadow_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the latest shadow artifact per source production run and proposal."""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for run in runs:
        source_run_id = _source_run_id(run)
        if not source_run_id:
            continue
        key = (source_run_id, str(run.get("proposed_strategy_version") or ""))
        current = by_key.get(key)
        if current is None or _as_dt(run.get("generated_at")) >= _as_dt(current.get("generated_at")):
            by_key[key] = run
    return sorted(by_key.values(), key=lambda row: _as_dt(row.get("generated_at")))


def fetch_source_outcomes(
    source_run_ids: list[str],
    *,
    horizons: tuple[str, ...] = DEFAULT_HORIZONS,
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    if not source_run_ids:
        return {}
    conn = get_db(read_only=True)
    try:
        tables = {str(row[0]) for row in conn.execute("SHOW TABLES").fetchall()}
        if "pick_outcomes" not in tables:
            return {}
        run_placeholders = ",".join(["?"] * len(source_run_ids))
        horizon_placeholders = ",".join(["?"] * len(horizons))
        rows = conn.execute(
            f"""
            SELECT run_id, market, symbol, horizon, outcome_date, return_pct,
                   benchmark_symbol, benchmark_pct, alpha_pct, is_success, updated_at
            FROM pick_outcomes
            WHERE run_id IN ({run_placeholders})
              AND horizon IN ({horizon_placeholders})
            """,
            [*source_run_ids, *horizons],
        ).fetchall()
    finally:
        conn.close()

    out: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        (
            run_id, market, symbol, horizon, outcome_date, return_pct,
            benchmark_symbol, benchmark_pct, alpha_pct, is_success, updated_at,
        ) = row
        out[(str(run_id), str(market), str(symbol), str(horizon))] = {
            "run_id": run_id,
            "market": market,
            "symbol": symbol,
            "horizon": horizon,
            "outcome_date": str(outcome_date) if outcome_date is not None else None,
            "return_pct": return_pct,
            "benchmark_symbol": benchmark_symbol,
            "benchmark_pct": benchmark_pct,
            "alpha_pct": alpha_pct,
            "is_success": bool(is_success) if is_success is not None else None,
            "updated_at": str(updated_at) if updated_at is not None else None,
        }
    return out


def _empty_stats() -> dict[str, Any]:
    return {
        "n": 0,
        "wins": 0,
        "win_rate": None,
        "avg_alpha_pct": None,
        "avg_return_pct": None,
    }


def _stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [
        row for row in records
        if row.get("alpha_pct") is not None
        and math.isfinite(_as_float(row.get("alpha_pct"), float("nan")))
    ]
    if not usable:
        return _empty_stats()
    n = len(usable)
    wins = sum(1 for row in usable if _as_float(row.get("alpha_pct")) > 0)
    return {
        "n": n,
        "wins": wins,
        "win_rate": round(wins / n * 100.0, 2),
        "avg_alpha_pct": round(sum(_as_float(row.get("alpha_pct")) for row in usable) / n, 4),
        "avg_return_pct": round(sum(_as_float(row.get("return_pct")) for row in usable) / n, 4),
    }


def _candidate_counts(
    runs: list[dict[str, Any]],
    horizons: tuple[str, ...],
) -> dict[tuple[str, str], dict[str, int]]:
    counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for run in runs:
        for pick in run.get("picks") or []:
            market = str(pick.get("market") or "")
            if not market:
                continue
            for horizon in horizons:
                key = (market, horizon)
                original_buy = str(pick.get("original_signal") or "") in BUY_SIGNALS
                shadow_buy = str(pick.get("shadow_signal") or "") in BUY_SIGNALS
                if original_buy:
                    counts[key]["original_buy_count"] += 1
                if shadow_buy:
                    counts[key]["shadow_buy_count"] += 1
                if pick.get("demoted"):
                    counts[key]["demoted_count"] += 1
                if pick.get("production_portfolio_eligible"):
                    counts[key]["production_portfolio_eligible_count"] += 1
    return {key: dict(value) for key, value in counts.items()}


def _outcome_records(
    runs: list[dict[str, Any]],
    outcomes: dict[tuple[str, str, str, str], dict[str, Any]],
    horizons: tuple[str, ...],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for run in runs:
        source_run_id = _source_run_id(run)
        if not source_run_id:
            continue
        for pick in run.get("picks") or []:
            market = str(pick.get("market") or "")
            symbol = str(pick.get("symbol") or "")
            if not market or not symbol:
                continue
            for horizon in horizons:
                outcome = outcomes.get((source_run_id, market, symbol, horizon))
                if not outcome or outcome.get("alpha_pct") is None:
                    continue
                original_buy = str(pick.get("original_signal") or "") in BUY_SIGNALS
                shadow_buy = str(pick.get("shadow_signal") or "") in BUY_SIGNALS
                records.append({
                    "shadow_run_id": run.get("run_id"),
                    "source_run_id": source_run_id,
                    "proposed_strategy_version": run.get("proposed_strategy_version"),
                    "market": market,
                    "horizon": horizon,
                    "symbol": symbol,
                    "name": pick.get("name"),
                    "original_rank": pick.get("original_rank"),
                    "shadow_rank": pick.get("shadow_rank"),
                    "original_buy": original_buy,
                    "shadow_buy": shadow_buy,
                    "demoted": bool(pick.get("demoted")),
                    "production_portfolio_eligible": bool(pick.get("production_portfolio_eligible")),
                    "action": pick.get("action"),
                    "alpha_pct": outcome.get("alpha_pct"),
                    "return_pct": outcome.get("return_pct"),
                    "is_success": outcome.get("is_success"),
                    "outcome_date": outcome.get("outcome_date"),
                })
    return records


def _records_for(records: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    return [row for row in records if predicate(row)]


def _coverage(reviewed: int, candidates: int) -> float:
    if candidates <= 0:
        return 0.0
    return round(reviewed / candidates * 100.0, 2)


def _dedup_source_last_batch(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只保留 source_run_id ∈「每天最后一批」（stock_research.core.strategy_eval 统一口径）。

    shadow 归档可能对同一天的多批 production run 各调参一次，评估时同日同票被重复计数
    （reviewed 虚高约 2×，alpha 偏离主链路）。统一到主链路：每天只评估最后一批 production run。
    过滤后为空（口径不匹配）时回退原 runs，避免误清空。
    """
    source_ids = {_source_run_id(r) for r in runs if _source_run_id(r)}
    if not source_ids:
        return runs
    try:
        from stock_research.core import strategy_eval as se
        conn = get_db(read_only=True)
        try:
            last_batch = se.last_batch_run_ids(conn, strategy_version=None)
        finally:
            conn.close()
    except Exception:
        return runs
    if not last_batch:
        return runs
    filtered = [r for r in runs if _source_run_id(r) in last_batch]
    return filtered or runs


def build_market_horizon_summary(
    runs: list[dict[str, Any]],
    outcomes: dict[tuple[str, str, str, str], dict[str, Any]],
    *,
    horizons: tuple[str, ...] = DEFAULT_HORIZONS,
) -> list[dict[str, Any]]:
    runs = _dedup_source_last_batch(runs)
    counts = _candidate_counts(runs, horizons)
    records = _outcome_records(runs, outcomes, horizons)
    markets = sorted({key[0] for key in counts} | {row["market"] for row in records})
    summary: list[dict[str, Any]] = []

    for market in markets:
        for horizon in horizons:
            key = (market, horizon)
            market_records = [row for row in records if row["market"] == market and row["horizon"] == horizon]
            original_records = _records_for(market_records, lambda row: row["original_buy"])
            shadow_records = _records_for(market_records, lambda row: row["shadow_buy"])
            demoted_records = _records_for(market_records, lambda row: row["demoted"])
            eligible_records = _records_for(market_records, lambda row: row["production_portfolio_eligible"])

            original_stats = _stats(original_records)
            shadow_stats = _stats(shadow_records)
            demoted_stats = _stats(demoted_records)
            eligible_stats = _stats(eligible_records)

            candidate = counts.get(key, {})
            original_count = int(candidate.get("original_buy_count", 0))
            shadow_count = int(candidate.get("shadow_buy_count", 0))
            demoted_count = int(candidate.get("demoted_count", 0))
            eligible_count = int(candidate.get("production_portfolio_eligible_count", 0))
            alpha_delta = None
            if original_stats["avg_alpha_pct"] is not None and shadow_stats["avg_alpha_pct"] is not None:
                alpha_delta = round(shadow_stats["avg_alpha_pct"] - original_stats["avg_alpha_pct"], 4)
            avoided_loss = None
            if demoted_stats["avg_alpha_pct"] is not None:
                avoided_loss = round(max(0.0, -_as_float(demoted_stats["avg_alpha_pct"])), 4)

            summary.append({
                "market": market,
                "label": MARKET_LABELS.get(market, market),
                "horizon": horizon,
                "original_buy_count": original_count,
                "reviewed_original_buy_count": original_stats["n"],
                "original_review_coverage_pct": _coverage(original_stats["n"], original_count),
                "original_win_rate": original_stats["win_rate"],
                "original_avg_alpha_pct": original_stats["avg_alpha_pct"],
                "shadow_buy_count": shadow_count,
                "reviewed_shadow_buy_count": shadow_stats["n"],
                "shadow_review_coverage_pct": _coverage(shadow_stats["n"], shadow_count),
                "shadow_win_rate": shadow_stats["win_rate"],
                "shadow_avg_alpha_pct": shadow_stats["avg_alpha_pct"],
                "shadow_vs_original_alpha_delta_pct": alpha_delta,
                "production_portfolio_eligible_count": eligible_count,
                "reviewed_production_eligible_count": eligible_stats["n"],
                "production_eligible_avg_alpha_pct": eligible_stats["avg_alpha_pct"],
                "demoted_count": demoted_count,
                "reviewed_demoted_count": demoted_stats["n"],
                "demoted_avg_alpha_pct": demoted_stats["avg_alpha_pct"],
                "avoided_loss_alpha_pct": avoided_loss,
            })
    return summary


def _latest_market_statuses(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not runs:
        return {}
    latest = max(runs, key=lambda row: _as_dt(row.get("generated_at")))
    return {str(row.get("market")): row for row in latest.get("market_summary") or [] if row.get("market")}


def activation_decision(
    *,
    runs: list[dict[str, Any]],
    market_summary: list[dict[str, Any]],
    min_shadow_runs: int,
    min_market_reviewed: int,
    min_coverage_pct: float,
    min_hit_rate: float,
    primary_horizon: str,
) -> dict[str, Any]:
    blockers: list[str] = []
    watch_items: list[str] = []
    latest_statuses = _latest_market_statuses(runs)

    if len(runs) < min_shadow_runs:
        watch_items.append(f"shadow run 数 {len(runs)} < {min_shadow_runs}")

    if latest_statuses:
        for market, row in latest_statuses.items():
            status = str(row.get("status") or "unchanged")
            if status == "evidence_pending":
                blockers.append(f"{MARKET_LABELS.get(market, market)} 仍是 evidence_pending")
            if status == "degraded":
                blockers.append(f"{MARKET_LABELS.get(market, market)} 仍是 degraded / research-only")

    primary_rows = [row for row in market_summary if row.get("horizon") == primary_horizon]
    if not primary_rows:
        blockers.append(f"没有 {primary_horizon} shadow outcome 样本")

    total_eligible = sum(int(row.get("production_portfolio_eligible_count") or 0) for row in primary_rows)
    if total_eligible <= 0:
        blockers.append("shadow 规则下 production_portfolio_eligible_count=0，不能切生产")

    for row in primary_rows:
        market = str(row.get("market"))
        reviewed = int(row.get("reviewed_shadow_buy_count") or 0)
        coverage = _as_float(row.get("shadow_review_coverage_pct"))
        avg_alpha = row.get("shadow_avg_alpha_pct")
        hit_rate = row.get("shadow_win_rate")
        if reviewed < min_market_reviewed:
            watch_items.append(f"{MARKET_LABELS.get(market, market)} {primary_horizon} reviewed {reviewed} < {min_market_reviewed}")
            continue
        if coverage < min_coverage_pct:
            blockers.append(f"{MARKET_LABELS.get(market, market)} {primary_horizon} coverage {coverage:.1f}% < {min_coverage_pct:.1f}%")
        if avg_alpha is not None and _as_float(avg_alpha) < 0:
            blockers.append(f"{MARKET_LABELS.get(market, market)} {primary_horizon} shadow alpha {avg_alpha:+.2f}% < 0")
        if hit_rate is not None and _as_float(hit_rate) < min_hit_rate:
            blockers.append(f"{MARKET_LABELS.get(market, market)} {primary_horizon} hit rate {hit_rate:.1f}% < {min_hit_rate:.1f}%")

    for row in market_summary:
        horizon = str(row.get("horizon"))
        if horizon == primary_horizon:
            continue
        reviewed = int(row.get("reviewed_shadow_buy_count") or 0)
        avg_alpha = row.get("shadow_avg_alpha_pct")
        if reviewed >= min_market_reviewed and avg_alpha is not None and _as_float(avg_alpha) < 0:
            market = str(row.get("market"))
            blockers.append(f"{MARKET_LABELS.get(market, market)} {horizon} shadow alpha {avg_alpha:+.2f}% < 0")

    if blockers:
        status = "BLOCKED"
    elif watch_items:
        status = "WATCH"
    else:
        status = "READY"
    return {
        "status": status,
        "blockers": blockers,
        "watch_items": watch_items,
        "criteria": {
            "min_shadow_runs": min_shadow_runs,
            "min_market_reviewed": min_market_reviewed,
            "min_coverage_pct": min_coverage_pct,
            "min_hit_rate": min_hit_rate,
            "primary_horizon": primary_horizon,
        },
    }


def build_weight_variant_summary(
    variant_runs: list[dict[str, Any]],
    outcomes: dict[tuple[str, str, str, str], dict[str, Any]],
    *,
    horizons: tuple[str, ...] = DEFAULT_HORIZONS,
) -> list[dict[str, Any]]:
    """权重变体(§19.3 第二步)前向评估：变体 top-10 组合 vs 同日生产 top-10。

    与回放工具同口径(top-10 组合、收盘价 alpha)，但样本是变体上线后逐日
    新攒的前瞻数据 —— 回放给方向，这里给「真金白银前向验证」。
    只产出独立段落，不参与 activation_decision(红绿灯只看主链)。
    """
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in variant_runs:
        name = str(run.get("weight_variant") or "")
        if name:
            by_name[name].append(run)

    summary: list[dict[str, Any]] = []
    for name in sorted(by_name):
        runs = _dedup_source_last_batch(dedupe_shadow_runs(by_name[name]))
        records = _outcome_records(runs, outcomes, horizons)
        # rank 是跨市场全局编号(CN 1-20/HK 21-40/US 41-60),基线 top-10 必须按市场内取
        baseline_keys: set[tuple[str, str, str]] = set()
        for run in runs:
            picks_by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for pick in run.get("picks") or []:
                market = str(pick.get("market") or "")
                if market and pick.get("original_rank") is not None:
                    picks_by_market[market].append(pick)
            for market, market_picks in picks_by_market.items():
                ranked = sorted(market_picks, key=lambda p: int(p["original_rank"]))[:10]
                for pick in ranked:
                    baseline_keys.add((str(run.get("run_id")), market, str(pick.get("symbol"))))
        weights = next(
            (run.get("weight_override") for run in reversed(runs) if run.get("weight_override")),
            None,
        )
        markets = sorted({row["market"] for row in records})
        for market in markets:
            for horizon in horizons:
                rows = [r for r in records if r["market"] == market and r["horizon"] == horizon]
                # 变体 run 里 production_portfolio_eligible = 变体市场内 top-10(见 apply_weight_variant_transform)
                variant_stats = _stats(_records_for(rows, lambda r: r["production_portfolio_eligible"]))
                baseline_stats = _stats(_records_for(
                    rows,
                    lambda r: (str(r["shadow_run_id"]), r["market"], r["symbol"]) in baseline_keys))
                alpha_delta = None
                if variant_stats["avg_alpha_pct"] is not None and baseline_stats["avg_alpha_pct"] is not None:
                    alpha_delta = round(
                        variant_stats["avg_alpha_pct"] - baseline_stats["avg_alpha_pct"], 4)
                summary.append({
                    "variant": name,
                    "weights": weights,
                    "market": market,
                    "label": MARKET_LABELS.get(market, market),
                    "horizon": horizon,
                    "shadow_run_count": len(runs),
                    "variant_top10_n": variant_stats["n"],
                    "variant_top10_avg_alpha_pct": variant_stats["avg_alpha_pct"],
                    "variant_top10_win_rate": variant_stats["win_rate"],
                    "prod_top10_n": baseline_stats["n"],
                    "prod_top10_avg_alpha_pct": baseline_stats["avg_alpha_pct"],
                    "prod_top10_win_rate": baseline_stats["win_rate"],
                    "variant_vs_prod_alpha_delta_pct": alpha_delta,
                })
    return summary


def build_evidence_payload(
    *,
    runs: list[dict[str, Any]],
    outcomes: dict[tuple[str, str, str, str], dict[str, Any]],
    horizons: tuple[str, ...] = DEFAULT_HORIZONS,
    min_shadow_runs: int = 10,
    min_market_reviewed: int = 60,
    min_coverage_pct: float = 80.0,
    min_hit_rate: float = 45.0,
    primary_horizon: str = "1d",
) -> dict[str, Any]:
    # 权重变体 run 走独立评估段,不进 activation_decision/market_horizon_summary
    # (红绿灯口径不变;变体证据只用于决定哪个新公式值得升级提案)
    variant_runs = [run for run in runs if run.get("weight_variant")]
    runs = [run for run in runs if not run.get("weight_variant")]
    weight_variant_summary = build_weight_variant_summary(
        variant_runs, outcomes, horizons=horizons)
    deduped = dedupe_shadow_runs(runs)
    market_summary = build_market_horizon_summary(deduped, outcomes, horizons=horizons)
    decision = activation_decision(
        runs=deduped,
        market_summary=market_summary,
        min_shadow_runs=min_shadow_runs,
        min_market_reviewed=min_market_reviewed,
        min_coverage_pct=min_coverage_pct,
        min_hit_rate=min_hit_rate,
        primary_horizon=primary_horizon,
    )
    latest = max(deduped, key=lambda row: _as_dt(row.get("generated_at"))) if deduped else None
    return {
        "schema_version": "shadow_tuning_evidence_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": decision["status"],
        "safety_boundary": (
            "Read-only evidence artifact. It evaluates archived shadow_tuning_run files "
            "against source production pick_outcomes and does not modify production strategy, "
            "recommendation tables, portfolio plans, watchlist, or holdings."
        ),
        "db_path": str(DB_PATH),
        "shadow_run_count": len(deduped),
        "raw_shadow_artifact_count": len(runs),
        "latest_shadow_run_id": latest.get("run_id") if latest else None,
        "latest_source_run_id": _source_run_id(latest) if latest else None,
        "latest_proposed_strategy_version": latest.get("proposed_strategy_version") if latest else None,
        "activation_decision": decision,
        "market_horizon_summary": market_summary,
        "weight_variant_run_count": len(variant_runs),
        "weight_variant_summary": weight_variant_summary,
    }


def to_markdown(payload: dict[str, Any]) -> str:
    decision = payload.get("activation_decision") or {}
    lines = [
        "# Shadow Tuning Evidence",
        "",
        f"Generated: {payload.get('generated_at')}",
        f"Status: **{payload.get('status')}**",
        f"Shadow runs: **{payload.get('shadow_run_count')}**",
        f"Latest shadow run: `{payload.get('latest_shadow_run_id')}`",
        f"Latest source run: `{payload.get('latest_source_run_id')}`",
        "",
        payload.get("safety_boundary", ""),
        "",
        "## Activation Decision",
        "",
        f"Decision: **{decision.get('status')}**",
        "",
    ]
    blockers = decision.get("blockers") or []
    watch_items = decision.get("watch_items") or []
    if blockers:
        lines.append("### Blockers")
        lines.append("")
        for item in blockers:
            lines.append(f"- {item}")
        lines.append("")
    if watch_items:
        lines.append("### Watch Items")
        lines.append("")
        for item in watch_items:
            lines.append(f"- {item}")
        lines.append("")

    lines.extend([
        "## Market Evidence",
        "",
        "| Market | Horizon | Orig Buy | Orig Reviewed | Orig Alpha | Shadow Buy | Shadow Reviewed | Shadow Alpha | Delta | Demoted | Demoted Alpha | Avoided Loss | Eligible |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for row in payload.get("market_horizon_summary") or []:
        lines.append(
            f"| {row.get('label')} | {row.get('horizon')} | "
            f"{row.get('original_buy_count')} | {row.get('reviewed_original_buy_count')} | "
            f"{row.get('original_avg_alpha_pct')} | {row.get('shadow_buy_count')} | "
            f"{row.get('reviewed_shadow_buy_count')} | {row.get('shadow_avg_alpha_pct')} | "
            f"{row.get('shadow_vs_original_alpha_delta_pct')} | {row.get('demoted_count')} | "
            f"{row.get('demoted_avg_alpha_pct')} | {row.get('avoided_loss_alpha_pct')} | "
            f"{row.get('production_portfolio_eligible_count')} |"
        )

    variant_rows = payload.get("weight_variant_summary") or []
    if variant_rows:
        lines.extend([
            "",
            "## Weight Variant Evidence（§19.3 第二步 · 前向 top-10 组合对比，不参与红绿灯）",
            "",
            "| Variant | Market | Horizon | Runs | Variant n | Variant Alpha | Variant Win | Prod n | Prod Alpha | Prod Win | Delta |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for row in variant_rows:
            lines.append(
                f"| {row.get('variant')} | {row.get('label')} | {row.get('horizon')} | "
                f"{row.get('shadow_run_count')} | {row.get('variant_top10_n')} | "
                f"{row.get('variant_top10_avg_alpha_pct')} | {row.get('variant_top10_win_rate')} | "
                f"{row.get('prod_top10_n')} | {row.get('prod_top10_avg_alpha_pct')} | "
                f"{row.get('prod_top10_win_rate')} | {row.get('variant_vs_prod_alpha_delta_pct')} |"
            )
    return "\n".join(lines) + "\n"


def write_outputs(payload: dict[str, Any], *, output_json: Path = OUT_JSON, output_md: Path = OUT_MD) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    output_md.write_text(to_markdown(payload), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate shadow tuning runs against source pick_outcomes.")
    parser.add_argument("--output-json", default=str(OUT_JSON))
    parser.add_argument("--output-md", default=str(OUT_MD))
    parser.add_argument("--horizons", default=",".join(DEFAULT_HORIZONS))
    parser.add_argument("--min-shadow-runs", type=int, default=10)
    parser.add_argument("--min-market-reviewed", type=int, default=60)
    parser.add_argument("--min-coverage-pct", type=float, default=80.0)
    parser.add_argument("--min-hit-rate", type=float, default=45.0)
    parser.add_argument("--primary-horizon", default="1d")
    args = parser.parse_args(argv)

    horizons = tuple(part.strip() for part in args.horizons.split(",") if part.strip())
    runs = load_shadow_runs()
    deduped = dedupe_shadow_runs(runs)
    source_run_ids = sorted({_source_run_id(run) for run in deduped if _source_run_id(run)})
    outcomes = fetch_source_outcomes(source_run_ids, horizons=horizons)
    payload = build_evidence_payload(
        runs=runs,
        outcomes=outcomes,
        horizons=horizons,
        min_shadow_runs=args.min_shadow_runs,
        min_market_reviewed=args.min_market_reviewed,
        min_coverage_pct=args.min_coverage_pct,
        min_hit_rate=args.min_hit_rate,
        primary_horizon=args.primary_horizon,
    )
    write_outputs(payload, output_json=Path(args.output_json), output_md=Path(args.output_md))
    print(json.dumps({
        "status": payload.get("status"),
        "shadow_run_count": payload.get("shadow_run_count"),
        "latest_shadow_run_id": payload.get("latest_shadow_run_id"),
        "output_json": str(Path(args.output_json)),
        "output_md": str(Path(args.output_md)),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
