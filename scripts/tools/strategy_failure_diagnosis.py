#!/usr/bin/env python3
"""Diagnose weak V2 recommendation outcomes before changing strategy weights.

This is a read-only strategy validation companion. It turns negative alpha
samples into grouped evidence: market, rank bucket, factor bucket, risk flags,
chain/theme, and worst examples. It does not create today's buy list and does
not update strategy_versions.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import DB_PATH, get_db  # noqa: E402

OUT_JSON = REPO / "data" / "latest" / "strategy_failure_diagnosis.json"
OUT_MD = REPO / "data" / "reports" / "strategy_failure_diagnosis.md"

PRODUCTION_METRICS_START_DATE = os.environ.get("STOCK_ASSISTANT_METRICS_START_DATE", "2026-05-25")
DEFAULT_STRATEGY_VERSION = os.environ.get("STOCK_ASSISTANT_STRATEGY_VERSION", "latest")
DEFAULT_HORIZON = os.environ.get("STOCK_ASSISTANT_DIAGNOSIS_HORIZON", "1d")

FACTOR_KEYS = ("valuation", "momentum", "reversal", "data_quality", "coverage", "f_score")
MARKET_LABELS = {"US": "美股", "CN": "A股", "HK": "港股"}


def _tables(conn) -> set[str]:
    try:
        return {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
    except Exception:
        return set()


def _table_columns(conn, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
    except Exception:
        return set()


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


def _safe_json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
        return parsed if parsed is not None else fallback
    except Exception:
        return fallback


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _rank_bucket(rank: Any) -> str:
    try:
        r = int(rank)
    except Exception:
        return "unknown"
    if r <= 5:
        return "top_1_5"
    if r <= 10:
        return "top_6_10"
    if r <= 20:
        return "top_11_20"
    return "rank_21_plus"


def _factor_bucket(value: Any) -> str:
    v = _as_float(value)
    if v is None:
        return "missing"
    if v >= 80:
        return "high_80_plus"
    if v >= 60:
        return "mid_60_80"
    if v >= 40:
        return "low_mid_40_60"
    return "low_under_40"


def _loss_bucket(alpha: Any) -> str:
    a = _as_float(alpha)
    if a is None:
        return "unknown"
    if a <= -3:
        return "large_loss_le_-3"
    if a <= -1:
        return "loss_-3_to_-1"
    if a < 0:
        return "small_loss_0_to_-1"
    return "win_alpha_pos"


def _normalize_risk_flag(flag: Any) -> str:
    if isinstance(flag, dict):
        code = flag.get("code") or flag.get("name") or flag.get("flag")
        return str(code or flag)
    text = str(flag or "").strip()
    if not text:
        return ""
    if text.startswith("{") and "code" in text:
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, dict):
                code = parsed.get("code") or parsed.get("name") or parsed.get("flag")
                return str(code or text)
        except Exception:
            pass
    return text


def _mean(values: list[float]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def _round(value: float | None, ndigits: int = 4) -> float | None:
    return round(float(value), ndigits) if value is not None else None


def _parse_markets(value: str | None) -> list[str] | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.lower() in {"all", "*"}:
        return None
    return [x.strip().upper() for x in raw.split(",") if x.strip()]


def _required_tables_ok(conn) -> tuple[bool, list[str]]:
    required = {"recommendation_runs", "recommendation_picks", "pick_outcomes"}
    missing = sorted(required - _tables(conn))
    return not missing, missing


def _load_samples(
    conn,
    *,
    strategy_version: str | None,
    horizon: str | None,
    markets: list[str] | None,
) -> list[dict[str, Any]]:
    ok, missing = _required_tables_ok(conn)
    if not ok:
        raise RuntimeError(f"当前 DB 缺少策略诊断必需表: {missing}")

    has_universe = "system_universe" in _tables(conn)
    has_chain = "chain_metadata" in _tables(conn)
    has_strategy_col = "strategy_version" in _table_columns(conn, "recommendation_runs")
    strategy_clause = " AND rr.strategy_version = ?" if strategy_version and has_strategy_col else ""
    horizon_clause = " AND po.horizon = ?" if horizon else ""
    market_clause = ""
    params: list[Any] = [PRODUCTION_METRICS_START_DATE]
    if strategy_clause:
        params.append(strategy_version)
    if horizon_clause:
        params.append(horizon)
    if markets:
        market_clause = " AND rp.market IN (" + ",".join(["?"] * len(markets)) + ")"
        params.extend(markets)

    universe_select = (
        "su.theme AS universe_theme, su.industry AS universe_industry"
        if has_universe else
        "NULL AS universe_theme, NULL AS universe_industry"
    )
    universe_join = (
        """
        LEFT JOIN system_universe su
          ON su.pool_id = 'system_tech_universe'
         AND su.market = rp.market
         AND su.symbol = rp.symbol
        """
        if has_universe else ""
    )
    chain_select = (
        "cm.chain, cm.chain_tier, cm.chain_role"
        if has_chain else
        "NULL AS chain, NULL AS chain_tier, NULL AS chain_role"
    )
    chain_join = (
        """
        LEFT JOIN chain_metadata cm
          ON cm.market = rp.market
         AND cm.symbol = rp.symbol
        """
        if has_chain else ""
    )

    rows = conn.execute(
        f"""
        WITH samples AS (
        SELECT
          rr.run_id, rr.run_date, rr.strategy_version, rr.model_version,
          rp.market, rp.symbol, rp.name, rp.rank, rp.rating, rp.signal,
          rp.total_score, rp.factor_scores_json, rp.risk_flags_json,
          rp.entry_price, rp.recommendation_reason,
          po.horizon, po.outcome_date, po.return_pct, po.benchmark_symbol,
          po.benchmark_pct, po.alpha_pct, po.is_success,
          ROW_NUMBER() OVER (
            PARTITION BY rr.run_id, rp.market
            ORDER BY rp.total_score DESC NULLS LAST, rp.symbol
          ) AS market_rank,
          {universe_select},
          {chain_select}
        FROM pick_outcomes po
        JOIN recommendation_runs rr ON rr.run_id = po.run_id
        JOIN recommendation_picks rp
          ON rp.run_id = po.run_id AND rp.market = po.market AND rp.symbol = po.symbol
        {universe_join}
        {chain_join}
        WHERE rr.universe_scope = 'system_tech_universe'
          AND rr.status = 'generated'
          AND rr.run_date >= ?
          AND po.alpha_pct IS NOT NULL
          AND COALESCE(rp.signal, 'buy') = 'buy'
          {strategy_clause}
          {horizon_clause}
          {market_clause}
        )
        SELECT * FROM samples
        ORDER BY market, horizon, market_rank, alpha_pct ASC
        """,
        params,
    ).fetchall()

    samples: list[dict[str, Any]] = []
    for row in rows:
        (
            run_id, run_date, strategy, model, market, symbol, name, rank, rating, signal,
            score, factor_json, risk_json, entry_price, reason, outcome_horizon,
            outcome_date, return_pct, benchmark_symbol, benchmark_pct, alpha_pct,
            is_success, market_rank, theme, industry, chain, chain_tier, chain_role,
        ) = row
        factors = _safe_json(factor_json, {})
        if not isinstance(factors, dict):
            factors = {}
        risk_flags = _safe_json(risk_json, [])
        if isinstance(risk_flags, str):
            risk_flags = [risk_flags]
        if not isinstance(risk_flags, list):
            risk_flags = []
        samples.append({
            "run_id": str(run_id),
            "run_date": str(run_date)[:10],
            "strategy_version": str(strategy) if strategy is not None else None,
            "model_version": str(model) if model is not None else None,
            "market": str(market),
            "symbol": str(symbol),
            "name": str(name or ""),
            "raw_rank": int(rank) if rank is not None else None,
            "rank": int(market_rank) if market_rank is not None else None,
            "rank_bucket": _rank_bucket(market_rank),
            "rating": str(rating or ""),
            "signal": str(signal or ""),
            "total_score": _round(_as_float(score), 2),
            "factor_scores": {k: _round(_as_float(v), 4) for k, v in factors.items()},
            "risk_flags": [x for x in (_normalize_risk_flag(v) for v in risk_flags) if x],
            "entry_price": _round(_as_float(entry_price), 4),
            "recommendation_reason": str(reason or ""),
            "horizon": str(outcome_horizon),
            "outcome_date": str(outcome_date)[:10],
            "return_pct": _round(_as_float(return_pct), 4),
            "benchmark_symbol": str(benchmark_symbol or ""),
            "benchmark_pct": _round(_as_float(benchmark_pct), 4),
            "alpha_pct": _round(_as_float(alpha_pct), 4),
            "is_success": bool(is_success),
            "loss_bucket": _loss_bucket(alpha_pct),
            "universe_theme": str(theme or ""),
            "universe_industry": str(industry or ""),
            "chain": str(chain or "未分类"),
            "chain_tier": str(chain_tier or ""),
            "chain_role": str(chain_role or ""),
        })
    return samples


def _aggregate(samples: list[dict[str, Any]], key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for item in samples:
        key = tuple(item.get(k) or "unknown" for k in key_fields)
        grouped[key].append(item)

    rows: list[dict[str, Any]] = []
    for key, items in grouped.items():
        alphas = [x["alpha_pct"] for x in items if x.get("alpha_pct") is not None]
        returns = [x["return_pct"] for x in items if x.get("return_pct") is not None]
        benchmarks = [x["benchmark_pct"] for x in items if x.get("benchmark_pct") is not None]
        scores = [x["total_score"] for x in items if x.get("total_score") is not None]
        wins = sum(1 for x in items if (x.get("alpha_pct") or 0) > 0)
        n = len(items)
        row = {field: key[i] for i, field in enumerate(key_fields)}
        row.update({
            "n": n,
            "wins": wins,
            "losses": n - wins,
            "win_rate": _round(wins * 100.0 / n if n else None, 2),
            "avg_alpha_pct": _round(_mean(alphas), 4),
            "avg_return_pct": _round(_mean(returns), 4),
            "avg_benchmark_pct": _round(_mean(benchmarks), 4),
            "avg_score": _round(_mean(scores), 2),
            "worst_alpha_pct": _round(min(alphas) if alphas else None, 4),
            "best_alpha_pct": _round(max(alphas) if alphas else None, 4),
        })
        rows.append(row)

    def sort_key(row: dict[str, Any]) -> tuple:
        return (str(row.get("market", "")), str(row.get("horizon", "")), -(row.get("n") or 0), row.get("avg_alpha_pct") or 0)

    return sorted(rows, key=sort_key)


def _factor_bucket_summary(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for item in samples:
        for factor in FACTOR_KEYS:
            value = item.get("factor_scores", {}).get(factor)
            enriched = dict(item)
            enriched["factor"] = factor
            enriched["factor_bucket"] = _factor_bucket(value)
            expanded.append(enriched)
    return _aggregate(expanded, ("market", "horizon", "factor", "factor_bucket"))


def _risk_flag_summary(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for item in samples:
        flags = item.get("risk_flags") or ["NO_RISK_FLAG"]
        for flag in flags:
            enriched = dict(item)
            enriched["risk_flag"] = str(flag)
            expanded.append(enriched)
    return _aggregate(expanded, ("market", "horizon", "risk_flag"))


def _factor_diagnostics(factor_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in factor_rows:
        key = (str(row["market"]), str(row["horizon"]), str(row["factor"]))
        by_key[key][str(row["factor_bucket"])] = row

    diagnostics: list[dict[str, Any]] = []
    for (market, horizon, factor), buckets in by_key.items():
        high = buckets.get("high_80_plus")
        low = buckets.get("low_under_40")
        mid_low = buckets.get("low_mid_40_60")
        high_alpha = high.get("avg_alpha_pct") if high else None
        low_alpha = low.get("avg_alpha_pct") if low else None
        comparison_alpha = low_alpha
        comparison_bucket = "low_under_40"
        if comparison_alpha is None and mid_low:
            comparison_alpha = mid_low.get("avg_alpha_pct")
            comparison_bucket = "low_mid_40_60"
        signal = "neutral"
        reason = ""
        if high and (high.get("n") or 0) >= 5 and high_alpha is not None and high_alpha <= -1:
            signal = "high_bucket_underperforms"
            reason = f"{factor} 高分组平均 alpha {high_alpha:+.2f}%"
        if (
            high and comparison_alpha is not None and high_alpha is not None
            and (high.get("n") or 0) >= 5
            and high_alpha + 1.0 < comparison_alpha
        ):
            signal = "possible_inverted_factor"
            reason = f"{factor} 高分组比 {comparison_bucket} 低 {comparison_alpha - high_alpha:.2f} 个百分点"
        if signal != "neutral":
            diagnostics.append({
                "market": market,
                "horizon": horizon,
                "factor": factor,
                "signal": signal,
                "reason": reason,
                "high_bucket": high,
                "comparison_bucket": buckets.get(comparison_bucket),
            })
    return sorted(diagnostics, key=lambda x: (x["market"], x["factor"]))


def _recommended_actions(
    market_summary: list[dict[str, Any]],
    rank_summary: list[dict[str, Any]],
    factor_diagnostics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    by_market = {str(row["market"]): row for row in market_summary}
    rank_by_market: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rank_summary:
        rank_by_market[str(row["market"])][str(row["rank_bucket"])] = row

    for market, row in by_market.items():
        n = int(row.get("n") or 0)
        avg_alpha = row.get("avg_alpha_pct")
        win_rate = row.get("win_rate")
        label = MARKET_LABELS.get(market, market)
        if n < 30:
            actions.append({
                "market": market,
                "severity": "medium",
                "action": "continue_collecting",
                "reason": f"{label} 样本 {n} 个，先补样本，不宜调权。",
            })
            continue
        if avg_alpha is not None and avg_alpha <= -1 and (win_rate or 0) < 45:
            actions.append({
                "market": market,
                "severity": "high",
                "action": "market_weight_review",
                "reason": f"{label} 平均 alpha {avg_alpha:+.2f}%，胜率 {win_rate:.1f}%，不能视为已验证可依赖。",
            })
        top5 = rank_by_market.get(market, {}).get("top_1_5")
        top10b = rank_by_market.get(market, {}).get("top_6_10")
        tail = rank_by_market.get(market, {}).get("top_11_20")
        top10_alpha_values = [
            x.get("avg_alpha_pct") for x in (top5, top10b)
            if x and x.get("avg_alpha_pct") is not None
        ]
        top10_alpha = _mean(top10_alpha_values)
        tail_alpha = tail.get("avg_alpha_pct") if tail else None
        if top10_alpha is not None and tail_alpha is not None and tail_alpha + 1.0 < top10_alpha:
            actions.append({
                "market": market,
                "severity": "medium",
                "action": "test_top10_cut",
                "reason": f"{label} 11-20 名 alpha {tail_alpha:+.2f}%，Top10 均值 {top10_alpha:+.2f}%，建议灰度测试缩池。",
            })
        if top5 and top5.get("avg_alpha_pct") is not None and top5["avg_alpha_pct"] <= -1:
            actions.append({
                "market": market,
                "severity": "high",
                "action": "formula_review_not_only_cut_count",
                "reason": f"{label} Top1-5 alpha {top5['avg_alpha_pct']:+.2f}%，问题不只是推荐数量过宽。",
            })

    for diag in factor_diagnostics:
        factor = diag["factor"]
        if diag["signal"] in {"high_bucket_underperforms", "possible_inverted_factor"}:
            actions.append({
                "market": diag["market"],
                "severity": "medium",
                "action": "factor_weight_review",
                "factor": factor,
                "reason": diag["reason"],
            })
    return actions


def _worst_examples(samples: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    sorted_items = sorted(samples, key=lambda x: (x.get("alpha_pct") if x.get("alpha_pct") is not None else 999))
    keep_fields = (
        "market", "symbol", "name", "rank", "total_score", "chain", "universe_industry",
        "risk_flags", "return_pct", "benchmark_pct", "alpha_pct", "run_date", "outcome_date",
    )
    return [{k: item.get(k) for k in keep_fields} for item in sorted_items[:limit]]


def _format_pct(value: Any) -> str:
    v = _as_float(value)
    if v is None:
        return "-"
    return f"{v:+.2f}%"


def _to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Strategy Failure Diagnosis",
        "",
        f"Generated: {payload['generated_at']}",
        f"Metrics start date: **{payload['metrics_start_date']}**",
        f"Strategy version: **{payload['strategy_version_filter']}**",
        f"Horizon: **{payload['horizon_filter']}**",
        "",
        "This report is diagnostic only. It evaluates historical point-in-time AI recommendations and does not create today's buy list or change strategy versions.",
        "",
        "## Executive Summary",
        "",
        "| Market | N | Win Rate | Avg Alpha | Avg Return | Worst Alpha |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in payload.get("market_summary") or []:
        market = MARKET_LABELS.get(str(row.get("market")), str(row.get("market")))
        lines.append(
            f"| {market} | {row.get('n', 0)} | {_format_pct(row.get('win_rate'))} | "
            f"{_format_pct(row.get('avg_alpha_pct'))} | {_format_pct(row.get('avg_return_pct'))} | "
            f"{_format_pct(row.get('worst_alpha_pct'))} |"
        )

    lines.extend(["", "## Recommended Next Actions", ""])
    actions = payload.get("recommended_actions") or []
    if actions:
        for i, action in enumerate(actions, start=1):
            market = MARKET_LABELS.get(str(action.get("market")), str(action.get("market")))
            factor = f" · factor={action.get('factor')}" if action.get("factor") else ""
            lines.append(f"{i}. [{action.get('severity')}] {market} · {action.get('action')}{factor}: {action.get('reason')}")
    else:
        lines.append("- No immediate negative-alpha action was triggered.")

    lines.extend([
        "",
        "## Rank Bucket",
        "",
        "| Market | Bucket | N | Win Rate | Avg Alpha | Avg Score |",
        "|---|---|---:|---:|---:|---:|",
    ])
    for row in payload.get("rank_bucket_summary") or []:
        lines.append(
            f"| {row.get('market')} | {row.get('rank_bucket')} | {row.get('n', 0)} | "
            f"{_format_pct(row.get('win_rate'))} | {_format_pct(row.get('avg_alpha_pct'))} | "
            f"{row.get('avg_score', '-')} |"
        )

    lines.extend([
        "",
        "## Factor Diagnostics",
        "",
        "| Market | Factor | Signal | Reason |",
        "|---|---|---|---|",
    ])
    diags = payload.get("factor_diagnostics") or []
    if diags:
        for row in diags:
            lines.append(f"| {row.get('market')} | {row.get('factor')} | {row.get('signal')} | {row.get('reason')} |")
    else:
        lines.append("| - | - | - | No factor bucket issue triggered. |")

    lines.extend([
        "",
        "## Risk Flags",
        "",
        "| Market | Risk Flag | N | Win Rate | Avg Alpha |",
        "|---|---|---:|---:|---:|",
    ])
    for row in (payload.get("risk_flag_summary") or [])[:30]:
        lines.append(
            f"| {row.get('market')} | {row.get('risk_flag')} | {row.get('n', 0)} | "
            f"{_format_pct(row.get('win_rate'))} | {_format_pct(row.get('avg_alpha_pct'))} |"
        )

    lines.extend([
        "",
        "## Worst Examples",
        "",
        "| Market | Symbol | Name | Rank | Score | Chain | Alpha | Return | Benchmark | Flags |",
        "|---|---|---|---:|---:|---|---:|---:|---:|---|",
    ])
    for item in payload.get("worst_examples") or []:
        flags = ",".join(item.get("risk_flags") or [])
        lines.append(
            f"| {item.get('market')} | {item.get('symbol')} | {item.get('name')} | "
            f"{item.get('rank', '')} | {item.get('total_score', '')} | {item.get('chain', '')} | "
            f"{_format_pct(item.get('alpha_pct'))} | {_format_pct(item.get('return_pct'))} | "
            f"{_format_pct(item.get('benchmark_pct'))} | {flags or '-'} |"
        )
    return "\n".join(lines) + "\n"


def build_report(
    conn=None,
    *,
    strategy_version: str | None = DEFAULT_STRATEGY_VERSION,
    horizon: str | None = DEFAULT_HORIZON,
    markets: list[str] | None = None,
) -> dict[str, Any]:
    owns_conn = conn is None
    if owns_conn:
        conn = get_db(force_read_only=True)
    try:
        resolved_strategy = _resolve_strategy_version(conn, strategy_version)
        samples = _load_samples(
            conn,
            strategy_version=resolved_strategy,
            horizon=horizon,
            markets=markets,
        )
    finally:
        if owns_conn and conn is not None:
            conn.close()

    market_summary = _aggregate(samples, ("market", "horizon"))
    rank_summary = _aggregate(samples, ("market", "horizon", "rank_bucket"))
    loss_summary = _aggregate(samples, ("market", "horizon", "loss_bucket"))
    chain_summary = _aggregate(samples, ("market", "horizon", "chain"))
    factor_rows = _factor_bucket_summary(samples)
    risk_rows = _risk_flag_summary(samples)
    factor_diag = _factor_diagnostics(factor_rows)
    actions = _recommended_actions(market_summary, rank_summary, factor_diag)

    return {
        "schema_version": "strategy_failure_diagnosis_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db_path": str(DB_PATH),
        "metrics_start_date": PRODUCTION_METRICS_START_DATE,
        "strategy_version_filter": resolved_strategy or "all",
        "requested_strategy_version": strategy_version or "latest",
        "horizon_filter": horizon or "all",
        "markets_filter": markets or "all",
        "sample_policy": (
            f"Only system_tech_universe V2 recommendation picks on/after {PRODUCTION_METRICS_START_DATE}, "
            "with signal='buy' and alpha_pct available, are diagnosed. This report is read-only and "
            "does not change production strategy versions."
        ),
        "summary": {
            "sample_count": len(samples),
            "action_count": len(actions),
            "negative_alpha_count": sum(1 for x in samples if (x.get("alpha_pct") or 0) < 0),
        },
        "market_summary": market_summary,
        "rank_bucket_summary": rank_summary,
        "loss_bucket_summary": loss_summary,
        "chain_summary": chain_summary,
        "factor_bucket_summary": factor_rows,
        "factor_diagnostics": factor_diag,
        "risk_flag_summary": risk_rows,
        "recommended_actions": actions,
        "worst_examples": _worst_examples(samples),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build V2 strategy failure diagnosis report.")
    parser.add_argument("--strategy-version", default=DEFAULT_STRATEGY_VERSION,
                        help="latest/current by default; use all to aggregate across strategy versions.")
    parser.add_argument("--horizon", default=DEFAULT_HORIZON,
                        help="Outcome horizon to diagnose, e.g. 1d, 5d, 20d. Use all for all horizons.")
    parser.add_argument("--markets", default="all",
                        help="Comma-separated market list, e.g. CN,HK. Default all.")
    parser.add_argument("--json", action="store_true", help="Print JSON payload to stdout.")
    args = parser.parse_args()

    horizon = None if str(args.horizon).lower() in {"all", "*", ""} else str(args.horizon)
    markets = _parse_markets(args.markets)
    payload = build_report(strategy_version=args.strategy_version, horizon=horizon, markets=markets)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    OUT_MD.write_text(_to_markdown(payload), encoding="utf-8")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Strategy failure diagnosis: samples={payload['summary']['sample_count']} actions={payload['summary']['action_count']}")
        print(f"  strategy={payload['strategy_version_filter']} horizon={payload['horizon_filter']}")
        for row in payload.get("market_summary") or []:
            print(
                f"  {row['market']} {row['horizon']}: n={row['n']} "
                f"win={row['win_rate']:.1f}% avg_alpha={row['avg_alpha_pct']:+.2f}%"
            )
        print(f"  JSON: {OUT_JSON}")
        print(f"  MD:   {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
