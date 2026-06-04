#!/usr/bin/env python3
"""US-only production acceptance for the AI recommendation flow.

Global production acceptance is intentionally strict and can fail because CN/HK
or research-only evidence jobs failed.  This check answers a narrower operator
question: is the US recommendation and model portfolio path usable as a
production advisory surface today?

It is read-only.  It does not change recommendation formulas, write watchlist,
write holdings, or activate strategy versions.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_research import config  # noqa: E402

DB_PATH = Path(config.DUCKDB_PATH)
OUT_JSON = REPO / "data" / "latest" / "us_production_acceptance_check.json"
OUT_MD = REPO / "data" / "reports" / "us_production_acceptance_check.md"

US_CORE_STEP_KEYWORDS = (
    "抓价格",
    "V2 推荐 run",
    "推荐质量闸门",
    "V2 pick alpha",
    "V2 策略验证",
    "全池 AI 推荐",
    "基准指数行情",
    "美股事件日历",
    "美股 SEC EDGAR",
)
US_NON_BLOCKING_STEP_KEYWORDS = (
    "港股",
    "A 股",
    "A股",
    "事件日历（解禁/减持/财报）",
    "AI 主题雷达",
    "生产闭环验收",
)


def _load_json(rel: str) -> dict[str, Any]:
    path = REPO / rel
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:19])
    except Exception:
        return None


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


def _plan_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("plan_v6", "plan_v5", "plan", "portfolio", "current_plan"):
        rows = plan.get(key)
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    return []


def _plan_market_check(plan: dict[str, Any]) -> dict[str, Any]:
    rows = _plan_rows(plan)
    scope = (
        (plan.get("constraints") or {}).get("market_scope")
        or (plan.get("candidate_universe") or {}).get("market_scope")
        or "UNKNOWN"
    )
    markets: dict[str, int] = {}
    bad_rows: list[dict[str, Any]] = []
    for row in rows:
        market = str(row.get("market") or "").upper() or "MISSING"
        symbol = str(row.get("symbol") or row.get("ticker") or row.get("code") or "")
        markets[market] = markets.get(market, 0) + 1
        if str(scope).upper() == "US" and market != "US":
            bad_rows.append({"symbol": symbol, "market": market})
    return {
        "scope": scope,
        "rows": len(rows),
        "markets": markets,
        "bad_rows": bad_rows[:20],
        "status": "PASS" if str(scope).upper() == "US" and rows and not bad_rows else "FAIL",
    }


def _validation_market(validation: dict[str, Any], market: str = "US") -> dict[str, Any]:
    for report in validation.get("reports") or []:
        if not isinstance(report, dict):
            continue
        if str(report.get("market") or "").upper() != market:
            continue
        h = (report.get("by_horizon") or {}).get("1d") or {}
        return {
            "strategy_version": report.get("strategy_version"),
            "conclusion": report.get("conclusion"),
            "recommended_action": report.get("recommended_action"),
            "sample_size": _int(h.get("sample_size")),
            "win_rate": _num(h.get("win_rate")),
            "avg_alpha": _num(h.get("avg_alpha")),
            "period_start": h.get("period_start"),
            "period_end": h.get("period_end"),
        }
    return {}


def _shadow_us(shadow: dict[str, Any]) -> dict[str, Any]:
    for row in shadow.get("market_horizon_summary") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("market") or "").upper() == "US" and str(row.get("horizon") or "") == "1d":
            return row
    return {}


def _preflight_trial(preflight: dict[str, Any]) -> dict[str, Any]:
    trial = preflight.get("trial_gate") or {}
    return trial if isinstance(trial, dict) else {}


def _artifact_payload(rel: str) -> dict[str, Any]:
    path = REPO / rel
    payload = _load_json(rel)
    generated = _parse_dt(payload.get("generated_at") or payload.get("updated_at"))
    if not generated and path.exists():
        generated = datetime.fromtimestamp(path.stat().st_mtime)
    return {
        "rel": rel,
        "exists": path.exists(),
        "generated_at": generated.isoformat(timespec="seconds") if generated else None,
        "age_days": (datetime.now().date() - generated.date()).days if generated else None,
    }


def fetch_db_summary(db_path: Path = DB_PATH) -> dict[str, Any]:
    out: dict[str, Any] = {"db_path": str(db_path)}
    if not db_path.exists():
        out["error"] = "missing_db"
        return out
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
        out["tables_present"] = sorted(tables)
        if {"recommendation_runs", "recommendation_picks"}.issubset(tables):
            run = conn.execute(
                """
                SELECT run_id, generated_at, strategy_version, model_version, universe_scope, status
                FROM recommendation_runs
                WHERE universe_scope = 'system_tech_universe'
                ORDER BY generated_at DESC
                LIMIT 1
                """
            ).fetchone()
            if run:
                run_id = str(run[0])
                out["latest_run"] = {
                    "run_id": run_id,
                    "generated_at": run[1].isoformat(sep=" ") if hasattr(run[1], "isoformat") else str(run[1]),
                    "strategy_version": run[2],
                    "model_version": run[3],
                    "universe_scope": run[4],
                    "status": run[5],
                }
                total, buy, with_entry = conn.execute(
                    """
                    SELECT COUNT(*),
                           SUM(CASE WHEN signal = 'buy' THEN 1 ELSE 0 END),
                           SUM(CASE WHEN entry_price IS NOT NULL THEN 1 ELSE 0 END)
                    FROM recommendation_picks
                    WHERE run_id = ? AND market = 'US'
                    """,
                    [run_id],
                ).fetchone()
                bad_scope = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM recommendation_picks
                    WHERE run_id = ? AND market = 'US'
                      AND (universe_scope <> 'system_tech_universe' OR source_origin <> 'system_pool')
                    """,
                    [run_id],
                ).fetchone()[0]
                out["us_picks"] = {
                    "total": _int(total),
                    "buy": _int(buy),
                    "with_entry_price": _int(with_entry),
                    "bad_scope_or_origin": _int(bad_scope),
                }
        if {"pool_membership", "price_daily"}.issubset(tables):
            active = conn.execute(
                """
                SELECT COUNT(*)
                FROM pool_membership
                WHERE active = TRUE AND pool_type = 'system_tech_universe' AND market = 'US'
                """
            ).fetchone()[0]
            priced = conn.execute(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT DISTINCT p.market, p.symbol
                    FROM price_daily p
                    JOIN pool_membership m
                      ON m.market = p.market
                     AND m.symbol = p.symbol
                     AND m.active = TRUE
                     AND m.pool_type = 'system_tech_universe'
                    WHERE p.market = 'US'
                )
                """
            ).fetchone()[0]
            latest_trade_date = conn.execute(
                "SELECT MAX(trade_date) FROM price_daily WHERE market = 'US'"
            ).fetchone()[0]
            out["us_price_coverage"] = {
                "active_pool": _int(active),
                "priced": _int(priced),
                "coverage_pct": round((_int(priced) / _int(active) * 100), 2) if _int(active) else 0.0,
                "latest_trade_date": str(latest_trade_date) if latest_trade_date else None,
            }
        if "portfolio_plans" in tables and out.get("latest_run"):
            rows = conn.execute(
                """
                SELECT market, COUNT(*)
                FROM portfolio_plans
                WHERE run_id = ?
                GROUP BY market
                """,
                [out["latest_run"]["run_id"]],
            ).fetchall()
            out["portfolio_plan_markets"] = {str(market): _int(n) for market, n in rows}
    finally:
        conn.close()
    return out


def _failed_steps(pipeline: dict[str, Any]) -> list[dict[str, Any]]:
    return [x for x in (pipeline.get("failed_steps") or []) if isinstance(x, dict)]


def _classify_failed_steps(failed_steps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blocking: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for step in failed_steps:
        label = str(step.get("label") or step.get("script") or "")
        is_us_core = any(k in label for k in US_CORE_STEP_KEYWORDS)
        is_non_blocking = any(k in label for k in US_NON_BLOCKING_STEP_KEYWORDS)
        if is_us_core and not is_non_blocking:
            blocking.append(step)
        else:
            ignored.append(step)
    return blocking, ignored


def build_us_acceptance(
    *,
    quality: dict[str, Any],
    pipeline: dict[str, Any],
    plan: dict[str, Any],
    validation: dict[str, Any],
    shadow: dict[str, Any],
    preflight: dict[str, Any] | None = None,
    db_summary: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []

    q_status = str(quality.get("status") or "UNKNOWN").upper()
    if q_status == "PASS":
        checks.append(_check("PASS", "quality_gate", "推荐质量闸门", "PASS：US 推荐基础输入可用。"))
    else:
        checks.append(_check("FAIL", "quality_gate", "推荐质量闸门", f"{q_status}：US 不能单独放行。"))
        blockers.append("推荐质量闸门未通过。")

    failed = _failed_steps(pipeline)
    us_blocking_steps, ignored_steps = _classify_failed_steps(failed)
    if us_blocking_steps:
        labels = [str(x.get("label") or x.get("script") or "未知步骤") for x in us_blocking_steps[:8]]
        checks.append(_check("FAIL", "us_core_pipeline_failed", "US 核心流水线", "存在 US 核心失败步骤。", labels))
        blockers.append("US 核心流水线存在失败步骤。")
    else:
        msg = "US 核心流水线无失败步骤。"
        if ignored_steps:
            msg += f" 已忽略 {len(ignored_steps)} 个非 US/研究失败步骤。"
            warnings.append(f"全局流水线仍有 {len(ignored_steps)} 个非 US/研究失败步骤，US-only 验收不阻断。")
        checks.append(_check("PASS", "us_core_pipeline", "US 核心流水线", msg))

    latest_run = db_summary.get("latest_run") or {}
    us_picks = db_summary.get("us_picks") or {}
    if not latest_run:
        checks.append(_check("FAIL", "latest_run_missing", "US 最新推荐 run", "没有最新 system_tech_universe 推荐 run。"))
        blockers.append("缺少最新系统推荐 run。")
    elif _int(us_picks.get("total")) <= 0:
        checks.append(_check("FAIL", "us_picks_missing", "US 推荐候选", "最新 run 没有 US 推荐候选。", latest_run))
        blockers.append("最新推荐没有 US 候选。")
    elif _int(us_picks.get("buy")) <= 0:
        checks.append(_check("FAIL", "us_buy_missing", "US buy 候选", "最新 run 没有 US buy 候选。", us_picks))
        blockers.append("最新推荐没有 US buy。")
    elif _int(us_picks.get("bad_scope_or_origin")) > 0:
        checks.append(_check("FAIL", "us_pick_scope_invalid", "US 推荐来源", "US 推荐存在非 system pool 来源。", us_picks))
        blockers.append("US 推荐来源身份不干净。")
    elif _int(us_picks.get("with_entry_price")) < _int(us_picks.get("total")):
        checks.append(_check("FAIL", "us_entry_price_missing", "US 入选价", "US 推荐缺 entry_price。", us_picks))
        blockers.append("US 推荐缺入选价。")
    else:
        checks.append(_check(
            "PASS",
            "us_latest_picks",
            "US 最新推荐候选",
            f"{us_picks.get('buy')}/{us_picks.get('total')} 为 buy，entry_price 覆盖 {us_picks.get('with_entry_price')}/{us_picks.get('total')}。",
        ))

    price = db_summary.get("us_price_coverage") or {}
    coverage = _num(price.get("coverage_pct")) or 0.0
    if coverage < 80.0:
        checks.append(_check("FAIL", "us_price_coverage_low", "US 行情覆盖", f"US 行情覆盖 {coverage:.1f}% < 80%。", price))
        blockers.append("US 行情覆盖不足。")
    else:
        checks.append(_check("PASS", "us_price_coverage", "US 行情覆盖", f"{price.get('priced')}/{price.get('active_pool')} ({coverage:.1f}%) · 最新 {price.get('latest_trade_date') or '—'}。"))

    plan_check = _plan_market_check(plan)
    if plan_check["status"] == "PASS":
        checks.append(_check("PASS", "us_only_plan", "US-only 组合方案", f"scope={plan_check['scope']} · rows={plan_check['rows']} · markets={plan_check['markets']}"))
    else:
        checks.append(_check("FAIL", "us_only_plan", "US-only 组合方案", f"组合不是纯 US：scope={plan_check['scope']} · markets={plan_check['markets']}", plan_check["bad_rows"]))
        blockers.append("组合方案没有通过 US-only 硬锁。")

    us_val = _validation_market(validation, "US")
    val_sample = _int(us_val.get("sample_size"))
    val_alpha = _num(us_val.get("avg_alpha"))
    val_hit = _num(us_val.get("win_rate"))
    if val_sample >= 60 and val_alpha is not None and val_alpha > 0 and val_hit is not None and val_hit >= 45.0:
        checks.append(_check("PASS", "us_strategy_evidence", "US 已成熟样本", f"sample={val_sample} · alpha {_fmt_pct(val_alpha)} · 命中 {_fmt_pct(val_hit)}。"))
    elif val_sample >= 20:
        checks.append(_check("WARN", "us_strategy_evidence_weak", "US 已成熟样本", f"sample={val_sample} · alpha {_fmt_pct(val_alpha)} · 命中 {_fmt_pct(val_hit)}，只能弱结论。"))
        warnings.append("US 策略样本尚未达到强放行标准。")
    else:
        checks.append(_check("FAIL", "us_strategy_evidence_missing", "US 已成熟样本", f"sample={val_sample}，不足以放行 US。"))
        blockers.append("US 策略验证样本不足。")

    required_events = (
        ("data/event_calendar_us.json", "US 财报/yfinance 事件", 2, "FAIL"),
        ("data/event_calendar_us_sec.json", "US SEC 事件", 2, "FAIL"),
        ("data/event_calendar_us_form4.json", "US Form 4 内部人事件", 7, "WARN"),
    )
    for rel, label, max_age, stale_level in required_events:
        art = artifacts.get(rel) or _artifact_payload(rel)
        if not art.get("exists"):
            level = "FAIL" if stale_level == "FAIL" else "WARN"
            checks.append(_check(level, f"{rel}_missing", label, f"{rel} 缺失。"))
            (blockers if level == "FAIL" else warnings).append(f"{label} 缺失。")
            continue
        age = _int(art.get("age_days"), -1)
        if age > max_age:
            checks.append(_check(stale_level, f"{rel}_stale", label, f"{rel} 滞后 {age} 天。"))
            (blockers if stale_level == "FAIL" else warnings).append(f"{label} 滞后 {age} 天。")
        else:
            checks.append(_check("PASS", f"{rel}_fresh", label, f"{rel} age={age} 天。"))

    preflight = preflight or {}
    us_shadow = _shadow_us(shadow)
    preflight_trial = _preflight_trial(preflight)
    decision = shadow.get("activation_decision") or {}
    crit = decision.get("criteria") or {}
    preflight_criteria = preflight.get("criteria") or {}
    min_runs = _int(crit.get("min_shadow_runs"), 10)
    min_reviewed = _int(crit.get("min_market_reviewed"), 60)
    min_coverage = _num(crit.get("min_coverage_pct")) or 80.0
    min_hit = _num(crit.get("min_hit_rate")) or 45.0
    if preflight_criteria:
        min_runs = _int(preflight_criteria.get("min_shadow_runs"), min_runs)
        min_reviewed = _int(preflight_criteria.get("min_market_reviewed"), min_reviewed)
        min_coverage = _num(preflight_criteria.get("min_coverage_pct")) or min_coverage
        min_hit = _num(preflight_criteria.get("min_hit_rate")) or min_hit
    has_preflight = bool(preflight_trial)
    runs = _int(preflight_trial.get("unique_source_run_count"), _int(shadow.get("shadow_run_count")))
    raw_runs = _int(preflight_trial.get("raw_shadow_artifact_count"), _int(shadow.get("raw_shadow_artifact_count"), runs))
    reviewed = _int(preflight_trial.get("reviewed_shadow_buy_count"), _int(us_shadow.get("reviewed_shadow_buy_count")))
    if has_preflight:
        shadow_coverage = _num(preflight_trial.get("shadow_review_coverage_pct")) or 0.0
        shadow_alpha = _num(preflight_trial.get("shadow_avg_alpha_pct"))
        shadow_hit = _num(preflight_trial.get("shadow_win_rate"))
    else:
        shadow_coverage = _num(us_shadow.get("shadow_review_coverage_pct")) or 0.0
        shadow_alpha = _num(us_shadow.get("shadow_avg_alpha_pct"))
        shadow_hit = _num(us_shadow.get("shadow_win_rate"))
    trial_gaps: list[str] = []
    preflight_status = str(preflight.get("status") or "").upper()
    preflight_blockers = [str(x) for x in (preflight.get("blockers") or [])]
    preflight_warnings = [str(x) for x in (preflight.get("warnings") or [])]
    if preflight_status == "FAIL":
        trial_gaps.append("US shadow 预检 FAIL")
    if runs < min_runs:
        trial_gaps.append(f"唯一 source run {runs}/{min_runs}")
    if reviewed < min_reviewed:
        trial_gaps.append(f"US 1D reviewed {reviewed}/{min_reviewed}")
    if shadow_coverage < min_coverage:
        trial_gaps.append(f"覆盖 {shadow_coverage:.0f}%/{min_coverage:.0f}%")
    if shadow_alpha is None or shadow_alpha <= 0:
        trial_gaps.append(f"shadow alpha {_fmt_pct(shadow_alpha)}")
    if shadow_hit is not None and shadow_hit < min_hit:
        trial_gaps.append(f"shadow 命中 {shadow_hit:.1f}%/{min_hit:.0f}%")
    for item in preflight_blockers:
        if item not in trial_gaps:
            trial_gaps.append(item)
    trial_ready = not trial_gaps
    if trial_ready:
        checks.append(_check("PASS", "us_trial_gate", "US 可小仓试探门槛", "已达到可小仓试探门槛。"))
    else:
        checks.append(_check(
            "WARN",
            "us_trial_gate",
            "US 可小仓试探门槛",
            (
                "US 核心可上线为研究/买前审查；可小仓试探仍需继续攒证据。"
                f" 口径=唯一 source run {runs}/{min_runs}（raw artifact {raw_runs}）。"
            ),
            trial_gaps,
        ))
        warnings.extend(trial_gaps)
    for item in preflight_warnings:
        if item not in warnings:
            warnings.append(item)

    if blockers:
        status = "FAIL"
        decision_code = "US_BLOCKED"
        decision_label = "US 暂停上线"
        allowed_use = "只读观察，先修 US 阻断项"
    elif trial_ready:
        status = "PASS"
        decision_code = "US_TRIAL_READY"
        decision_label = "US 可小仓试探"
        allowed_use = "可进入可小仓试探，但仍必须逐票买前审查"
    else:
        status = "PASS"
        decision_code = "US_RESEARCH_READY_TRIAL_PENDING"
        decision_label = "US 核心可上线（研究/买前审查）"
        allowed_use = "可上线为候选发现、研究队列和买前审查；可小仓试探仍等样本门槛"

    return {
        "schema_version": "us_production_acceptance_v1",
        "generated_at": now.isoformat(timespec="seconds"),
        "status": status,
        "decision": {
            "code": decision_code,
            "label": decision_label,
            "allowed_use": allowed_use,
            "primary_market": "US",
        },
        "safety_boundary": (
            "US-only read-only acceptance. It ignores CN/HK/research-only failures for US release, "
            "and does not write watchlist, real holdings, recommendation formulas, or strategy versions."
        ),
        "summary": {
            "fail": len(blockers),
            "warn": len(list(dict.fromkeys(warnings))),
            "ignored_global_failed_steps": len(ignored_steps),
            "us_blocking_failed_steps": len(us_blocking_steps),
            "latest_run_id": latest_run.get("run_id"),
            "strategy_version": latest_run.get("strategy_version") or us_val.get("strategy_version"),
            "us_pick_count": us_picks.get("total"),
            "us_buy_count": us_picks.get("buy"),
            "us_price_coverage_pct": coverage,
            "us_validation_alpha_pct": val_alpha,
            "us_validation_hit_rate_pct": val_hit,
            "us_shadow_unique_source_run_count": runs,
            "us_shadow_raw_artifact_count": raw_runs,
            "trial_ready": trial_ready,
        },
        "db_summary": db_summary,
        "checks": checks,
        "blockers": blockers,
        "warnings": list(dict.fromkeys(warnings)),
    }


def to_markdown(payload: dict[str, Any]) -> str:
    decision = payload.get("decision") or {}
    summary = payload.get("summary") or {}
    lines = [
        "# US Production Acceptance",
        "",
        f"Generated: {payload.get('generated_at')}",
        f"Status: **{payload.get('status')}**",
        f"Decision: **{decision.get('label')}**",
        f"Allowed use: {decision.get('allowed_use')}",
        "",
        payload.get("safety_boundary", ""),
        "",
        "## Summary",
        "",
        f"- Latest run: `{summary.get('latest_run_id')}`",
        f"- US picks: {summary.get('us_buy_count')}/{summary.get('us_pick_count')} buy",
        f"- US validation: alpha={_fmt_pct(summary.get('us_validation_alpha_pct'))} hit={_fmt_pct(summary.get('us_validation_hit_rate_pct'))}",
        f"- US shadow source runs: {summary.get('us_shadow_unique_source_run_count')} (raw artifacts={summary.get('us_shadow_raw_artifact_count')})",
        f"- Trial ready: {summary.get('trial_ready')}",
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
    acceptance = _load_json("data/latest/production_acceptance_check.json")
    artifacts = ((acceptance.get("summary") or {}).get("artifacts") or {})
    for rel in ("data/event_calendar_us.json", "data/event_calendar_us_sec.json", "data/event_calendar_us_form4.json"):
        artifacts.setdefault(rel, _artifact_payload(rel))
    return build_us_acceptance(
        quality=_load_json("data/latest/recommendation_quality_gate.json"),
        pipeline=_load_json("data/latest/pipeline_status_production.json"),
        plan=_load_json("data/latest/plan_a_v5.json"),
        validation=_load_json("data/latest/strategy_validation_report.json"),
        shadow=_load_json("data/latest/shadow_tuning_evidence.json"),
        preflight=_load_json("data/latest/us_shadow_preflight_check.json"),
        db_summary=fetch_db_summary(DB_PATH),
        artifacts=artifacts,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run US-only production acceptance.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    parser.add_argument("--strict", action="store_true", help="Return non-zero when US status is FAIL.")
    args = parser.parse_args(argv)

    payload = run()
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    OUT_MD.write_text(to_markdown(payload), encoding="utf-8")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        decision = payload.get("decision") or {}
        summary = payload.get("summary") or {}
        print(f"US production acceptance: {payload.get('status')}")
        print(f"  decision={decision.get('code')} · {decision.get('label')}")
        print(f"  allowed_use={decision.get('allowed_use')}")
        print(
            f"  US picks={summary.get('us_buy_count')}/{summary.get('us_pick_count')} buy · "
            f"alpha={_fmt_pct(summary.get('us_validation_alpha_pct'))} · "
            f"hit={_fmt_pct(summary.get('us_validation_hit_rate_pct'))} · "
            f"shadow_source={summary.get('us_shadow_unique_source_run_count')} "
            f"(raw={summary.get('us_shadow_raw_artifact_count')}) · "
            f"trial_ready={summary.get('trial_ready')}"
        )
        for item in (payload.get("blockers") or [])[:8]:
            print(f"  [BLOCKER] {item}")
        for item in (payload.get("warnings") or [])[:8]:
            print(f"  [WARN] {item}")
        print(f"  JSON: {OUT_JSON}")
    if args.strict and payload.get("status") == "FAIL":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
