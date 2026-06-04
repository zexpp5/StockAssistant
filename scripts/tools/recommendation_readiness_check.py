#!/usr/bin/env python3
"""Build a read-only recommendation readiness snapshot.

This tool answers the operator question: "Can I use today's recommendation
rule, especially the US track?"  It does not score stocks, change formulas, or
write production tables.  It only reads existing gate/evidence artifacts and
writes JSON/Markdown status files for the dashboard and quick CLI checks.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
OUT_JSON = REPO / "data" / "latest" / "recommendation_readiness_check.json"
OUT_MD = REPO / "data" / "reports" / "recommendation_readiness_check.md"

MARKET_LABELS = {"US": "美股", "CN": "A股", "HK": "港股"}


def _load_json(rel: str) -> dict[str, Any]:
    path = REPO / rel
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _upper(value: Any, default: str = "UNKNOWN") -> str:
    text = str(value or default).strip().upper()
    return text or default


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
        "non_us_or_missing": len(bad_rows),
        "bad_rows": bad_rows[:20],
        "status": "PASS" if str(scope).upper() == "US" and rows and not bad_rows else "FAIL",
    }


def _shadow_market(shadow: dict[str, Any], market: str, horizon: str = "1d") -> dict[str, Any]:
    for row in shadow.get("market_horizon_summary") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("market") or "").upper() == market and str(row.get("horizon") or "") == horizon:
            return row
    return {}


def _validation_market(validation: dict[str, Any], market: str, horizon: str = "1d") -> dict[str, Any]:
    for report in validation.get("reports") or []:
        if not isinstance(report, dict):
            continue
        if str(report.get("market") or "").upper() != market:
            continue
        h = (report.get("by_horizon") or {}).get(horizon) or {}
        if isinstance(h, dict):
            return {
                "status": validation.get("status"),
                "status_reason": validation.get("status_reason"),
                "strategy_version": report.get("strategy_version"),
                "conclusion": report.get("conclusion"),
                "recommended_action": report.get("recommended_action"),
                "sample_size": _int(h.get("sample_size")),
                "wins": _int(h.get("wins")),
                "win_rate": _num(h.get("win_rate")),
                "avg_alpha": _num(h.get("avg_alpha")),
                "avg_return": _num(h.get("avg_return")),
                "period_start": h.get("period_start"),
                "period_end": h.get("period_end"),
            }
    return {}


def _preflight_trial(preflight: dict[str, Any]) -> dict[str, Any]:
    trial = preflight.get("trial_gate") or {}
    return trial if isinstance(trial, dict) else {}


def _pipeline_failed_steps(acceptance: dict[str, Any], pipeline: dict[str, Any]) -> list[dict[str, Any]]:
    failed = pipeline.get("failed_steps")
    if isinstance(failed, list) and failed:
        return [x for x in failed if isinstance(x, dict)]
    for issue in acceptance.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        if issue.get("code") == "pipeline_failed_steps_present" and isinstance(issue.get("details"), list):
            return [x for x in issue["details"] if isinstance(x, dict)]
    return []


def _artifact_age(acceptance: dict[str, Any], rel: str) -> int | None:
    artifact = ((acceptance.get("summary") or {}).get("artifacts") or {}).get(rel) or {}
    return _int(artifact.get("age_days"), -1) if artifact else None


def _check(status: str, code: str, title: str, message: str, details: Any = None) -> dict[str, Any]:
    item = {"status": status, "code": code, "title": title, "message": message}
    if details is not None:
        item["details"] = details
    return item


def build_readiness(
    *,
    quality: dict[str, Any],
    acceptance: dict[str, Any],
    shadow: dict[str, Any],
    plan: dict[str, Any],
    validation: dict[str, Any],
    pipeline: dict[str, Any],
    us_acceptance: dict[str, Any] | None = None,
    us_preflight: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    watch_items: list[str] = []

    q_status = _upper(quality.get("status"))
    a_status = _upper(acceptance.get("status"))
    p_status = _upper(pipeline.get("status") or ((acceptance.get("summary") or {}).get("pipeline_status") or {}).get("status"))
    us_acceptance = us_acceptance or {}
    us_preflight = us_preflight or {}
    us_acceptance_status = _upper(us_acceptance.get("status"), "")
    us_acceptance_decision = us_acceptance.get("decision") or {}
    us_core_pass = us_acceptance_status == "PASS"

    if q_status == "PASS":
        checks.append(_check("PASS", "quality_gate", "推荐质量闸门", "PASS：今日推荐基础输入可用。"))
    elif q_status == "WARN":
        checks.append(_check("WARN", "quality_gate", "推荐质量闸门", "WARN：今日推荐输入有降级项。"))
        watch_items.append("推荐质量闸门为 WARN，使用前先看降级原因。")
    else:
        checks.append(_check("FAIL", "quality_gate", "推荐质量闸门", f"{q_status}：推荐输入未通过。"))
        blockers.append("推荐质量闸门未通过。")

    if us_acceptance:
        msg = (
            f"{us_acceptance_decision.get('label') or us_acceptance_status} · "
            f"{us_acceptance_decision.get('allowed_use') or ''}"
        ).strip(" ·")
        checks.append(_check(
            "PASS" if us_core_pass else "FAIL",
            "us_production_acceptance",
            "US-only 生产验收",
            msg,
        ))
        if not us_core_pass:
            blockers.append("US-only 生产验收未通过。")

    failed_steps = _pipeline_failed_steps(acceptance, pipeline)
    if a_status == "PASS" and p_status not in {"FAIL", "FAILED", "ERROR"}:
        checks.append(_check("PASS", "production_acceptance", "生产验收", "PASS：生产链路闭环。"))
    elif failed_steps:
        labels = [str(x.get("label") or x.get("script") or "未知步骤") for x in failed_steps[:4]]
        checks.append(_check(
            "WARN" if us_core_pass else "FAIL",
            "production_acceptance",
            "生产验收/跑批",
            "全局生产仍有失败步骤；US-only 验收已单独判定。" if us_core_pass
            else "生产跑批仍有失败步骤；推荐可看，但不能伪装成完整生产通过。",
            labels,
        ))
        if us_core_pass:
            watch_items.append("全局流水线仍有非 US/研究失败步骤；US-only 验收不阻断。")
        else:
            watch_items.append("生产流水线仍有失败步骤，今天只能以研究队列方式使用。")
    elif a_status in {"WARN", "FAIL"}:
        checks.append(_check("WARN", "production_acceptance", "生产验收", f"{a_status}：有生产验收问题。"))
        watch_items.append("生产验收未完全通过，先看运行状态。")
    else:
        checks.append(_check("WARN", "production_acceptance", "生产验收", "尚未生成可用的生产验收结论。"))
        watch_items.append("生产验收缺失，先跑 production_acceptance_check。")

    handled_acceptance_codes = {
        "pipeline_failed_steps_present",
        "pipeline_not_completed",
        "stale_latest_artifact",
    }
    for issue in acceptance.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        level = _upper(issue.get("level"), "INFO")
        code = str(issue.get("code") or "acceptance_issue")
        if level not in {"FAIL", "WARN"} or code in handled_acceptance_codes:
            continue
        message = str(issue.get("message") or code)
        checks.append(_check(
            "FAIL" if level == "FAIL" else "WARN",
            f"acceptance_{code}",
            "生产验收附加问题",
            message,
        ))
        watch_items.append(message)

    form4_age = _artifact_age(acceptance, "data/event_calendar_us_form4.json")
    if form4_age is not None and form4_age > 7:
        checks.append(_check(
            "WARN",
            "us_form4_stale",
            "美股 Form 4 新鲜度",
            f"Form 4 事件源滞后 {form4_age} 天；影响内部人交易风险提示，不直接改分。",
        ))
        watch_items.append(f"美股 Form 4 滞后 {form4_age} 天，买前审查要人工补看。")

    plan_check = _plan_market_check(plan)
    checks.append(_check(
        plan_check["status"],
        "us_only_plan_lock",
        "US-only 组合硬锁",
        f"scope={plan_check['scope']} · rows={plan_check['rows']} · markets={plan_check['markets']}",
        plan_check["bad_rows"] if plan_check["bad_rows"] else None,
    ))
    if plan_check["status"] != "PASS":
        blockers.append("AI 组合方案没有通过 US-only 硬锁。")

    decision = shadow.get("activation_decision") or {}
    crit = decision.get("criteria") or {}
    preflight_trial = _preflight_trial(us_preflight)
    preflight_criteria = us_preflight.get("criteria") or {}
    min_runs = _int(crit.get("min_shadow_runs"), 10)
    min_reviewed = _int(crit.get("min_market_reviewed"), 60)
    min_coverage = _num(crit.get("min_coverage_pct")) or 80.0
    min_hit = _num(crit.get("min_hit_rate")) or 45.0
    if preflight_criteria:
        min_runs = _int(preflight_criteria.get("min_shadow_runs"), min_runs)
        min_reviewed = _int(preflight_criteria.get("min_market_reviewed"), min_reviewed)
        min_coverage = _num(preflight_criteria.get("min_coverage_pct")) or min_coverage
        min_hit = _num(preflight_criteria.get("min_hit_rate")) or min_hit
    shadow_runs = _int(preflight_trial.get("unique_source_run_count"), _int(shadow.get("shadow_run_count")))
    raw_shadow_runs = _int(preflight_trial.get("raw_shadow_artifact_count"), _int(shadow.get("raw_shadow_artifact_count"), shadow_runs))

    us_shadow = _shadow_market(shadow, "US")
    us_validation = _validation_market(validation, "US")
    if preflight_trial:
        reviewed = _int(preflight_trial.get("reviewed_shadow_buy_count"))
        coverage = _num(preflight_trial.get("shadow_review_coverage_pct"))
        shadow_alpha = _num(preflight_trial.get("shadow_avg_alpha_pct"))
        shadow_hit = _num(preflight_trial.get("shadow_win_rate"))
    else:
        reviewed = _int(us_shadow.get("reviewed_shadow_buy_count"))
        coverage = _num(us_shadow.get("shadow_review_coverage_pct"))
        shadow_alpha = _num(us_shadow.get("shadow_avg_alpha_pct"))
        shadow_hit = _num(us_shadow.get("shadow_win_rate"))
    production_eligible = _int(us_shadow.get("production_portfolio_eligible_count"))
    validation_sample = _int(us_validation.get("sample_size"))
    validation_alpha = _num(us_validation.get("avg_alpha"))
    validation_hit = _num(us_validation.get("win_rate"))

    us_shadow_gaps: list[str] = []
    preflight_status = _upper(us_preflight.get("status"), "")
    preflight_blockers = [str(x) for x in (us_preflight.get("blockers") or [])]
    preflight_warnings = [str(x) for x in (us_preflight.get("warnings") or [])]
    if preflight_status == "FAIL":
        us_shadow_gaps.append("US shadow 预检 FAIL")
    if shadow_runs < min_runs:
        us_shadow_gaps.append(f"唯一 source run {shadow_runs}/{min_runs}")
    if reviewed < min_reviewed:
        us_shadow_gaps.append(f"US 1D reviewed {reviewed}/{min_reviewed}")
    if (coverage or 0.0) < min_coverage:
        us_shadow_gaps.append(f"覆盖 {(coverage or 0.0):.0f}%/{min_coverage:.0f}%")
    if shadow_alpha is None:
        us_shadow_gaps.append("US shadow alpha 待样本成熟")
    elif shadow_alpha <= 0:
        us_shadow_gaps.append(f"US shadow alpha {shadow_alpha:.2f}% <= 0")
    if shadow_hit is not None and shadow_hit < min_hit:
        us_shadow_gaps.append(f"US shadow 命中 {shadow_hit:.1f}%/{min_hit:.0f}%")
    for item in preflight_blockers:
        if item not in us_shadow_gaps:
            us_shadow_gaps.append(item)

    us_trial_ready = not us_shadow_gaps and production_eligible > 0
    checks.append(_check(
        "PASS" if us_trial_ready else "WARN",
        "us_shadow_gate",
        "US 可小仓试探门槛",
        (
            f"唯一 source {shadow_runs}/{min_runs}（raw {raw_shadow_runs}） · reviewed {reviewed}/{min_reviewed} · "
            f"alpha {_fmt_pct(shadow_alpha)} · 命中 {_fmt_pct(shadow_hit)}"
        ),
        us_shadow_gaps or None,
    ))
    if us_shadow_gaps:
        watch_items.extend(us_shadow_gaps)
    for item in preflight_warnings:
        if item not in watch_items:
            watch_items.append(item)

    us_validation_positive = (
        validation_sample >= 60
        and validation_alpha is not None and validation_alpha > 0
        and validation_hit is not None and validation_hit >= min_hit
    )
    checks.append(_check(
        "PASS" if us_validation_positive else "WARN",
        "us_strategy_validation",
        "US 已成熟样本表现",
        (
            f"sample={validation_sample} · alpha {_fmt_pct(validation_alpha)} · "
            f"命中 {_fmt_pct(validation_hit)} · {us_validation.get('conclusion') or '暂无结论'}"
        ),
    ))
    if not us_validation_positive:
        watch_items.append("US 已成熟样本还不足以给强结论。")

    market_policy: dict[str, dict[str, Any]] = {}
    for mk in ("US", "CN", "HK"):
        s = _shadow_market(shadow, mk)
        v = _validation_market(validation, mk)
        if mk == "US":
            state = "TRIAL_READY" if us_trial_ready else "VERIFYING"
            allowed_use = "可小仓试探" if us_trial_ready else "研究队列/买前审查"
            shadow_snapshot = {
                "reviewed": reviewed,
                "coverage_pct": coverage,
                "alpha_pct": shadow_alpha,
                "hit_rate_pct": shadow_hit,
            }
        else:
            state = "RESEARCH_ONLY_FROZEN"
            allowed_use = "只观察，不进真钱组合"
            shadow_snapshot = {
                "reviewed": _int(s.get("reviewed_shadow_buy_count")),
                "coverage_pct": _num(s.get("shadow_review_coverage_pct")),
                "alpha_pct": _num(s.get("shadow_avg_alpha_pct")),
                "hit_rate_pct": _num(s.get("shadow_win_rate")),
            }
        market_policy[mk] = {
            "label": MARKET_LABELS[mk],
            "state": state,
            "allowed_use": allowed_use,
            "shadow_1d": shadow_snapshot,
            "production_formula_1d": {
                "sample_size": _int(v.get("sample_size")),
                "alpha_pct": _num(v.get("avg_alpha")),
                "hit_rate_pct": _num(v.get("win_rate")),
                "conclusion": v.get("conclusion"),
                "recommended_action": v.get("recommended_action"),
            },
        }

    hard_blocked = bool(blockers)
    if hard_blocked:
        status = "FAIL"
        decision_code = "US_BLOCKED"
        decision_label = "US 暂停使用"
        allowed_use = "暂停真钱相关使用，先修阻断项"
    elif us_trial_ready and a_status == "PASS" and p_status not in {"FAIL", "FAILED", "ERROR"}:
        status = "PASS"
        decision_code = "US_TRIAL_READY"
        decision_label = "US 可小仓试探"
        allowed_use = "可进入可小仓试探，但仍必须走买前审查"
    elif us_trial_ready:
        status = "WARN"
        decision_code = "US_TRIAL_READY_SYSTEM_WARN"
        decision_label = "US 达标但系统未完全通过"
        allowed_use = "先修生产健康，再考虑可小仓试探"
    elif us_core_pass and us_validation_positive and plan_check["status"] == "PASS" and q_status == "PASS":
        status = "WARN"
        decision_code = "US_RESEARCH_READY_TRIAL_PENDING"
        decision_label = "US 核心可上线（研究/买前审查）"
        allowed_use = "可上线为候选发现、研究队列和买前审查；可小仓试探仍等样本门槛"
    elif us_validation_positive and plan_check["status"] == "PASS" and q_status == "PASS":
        status = "WARN"
        decision_code = "US_VERIFYING"
        decision_label = "US 验证中"
        allowed_use = "可用于发现候选、进入研究和买前审查；还不是自动买入依据"
    else:
        status = "WARN"
        decision_code = "US_RESEARCH_ONLY"
        decision_label = "US 研究队列"
        allowed_use = "只用于观察和研究，不进入真钱执行"

    return {
        "schema_version": "recommendation_readiness_v1",
        "generated_at": now.isoformat(timespec="seconds"),
        "status": status,
        "decision": {
            "code": decision_code,
            "label": decision_label,
            "allowed_use": allowed_use,
            "primary_market": "US",
        },
        "safety_boundary": (
            "Read-only advisory artifact. It does not create stock pools, write watchlist, "
            "write real holdings, alter recommendation formulas, or activate strategy versions."
        ),
        "inputs": {
            "quality_gate_status": q_status,
            "production_acceptance_status": a_status,
            "us_acceptance_status": us_acceptance_status or None,
            "us_acceptance_decision": us_acceptance_decision.get("code"),
            "us_shadow_preflight_status": preflight_status or None,
            "pipeline_status": p_status,
            "latest_recommendation_run_id": (quality.get("summary") or {}).get("latest_recommendation_run", {}).get("run_id")
            or (acceptance.get("summary") or {}).get("latest_recommendation_run_id"),
            "strategy_version": us_validation.get("strategy_version"),
        },
        "plan": plan_check,
        "us": {
            "trial_ready": us_trial_ready,
            "shadow_runs": shadow_runs,
            "raw_shadow_artifact_count": raw_shadow_runs,
            "criteria": {
                "min_shadow_runs": min_runs,
                "min_market_reviewed": min_reviewed,
                "min_coverage_pct": min_coverage,
                "min_hit_rate": min_hit,
            },
            "shadow_1d": market_policy["US"]["shadow_1d"],
            "production_formula_1d": market_policy["US"]["production_formula_1d"],
            "gaps_to_trial": us_shadow_gaps,
        },
        "market_policy": market_policy,
        "checks": checks,
        "blockers": blockers,
        "watch_items": list(dict.fromkeys(watch_items)),
    }


def to_markdown(payload: dict[str, Any]) -> str:
    decision = payload.get("decision") or {}
    us = payload.get("us") or {}
    formula = us.get("production_formula_1d") or {}
    shadow = us.get("shadow_1d") or {}
    lines = [
        "# Recommendation Readiness Check",
        "",
        f"Generated: {payload.get('generated_at')}",
        f"Status: **{payload.get('status')}**",
        f"Decision: **{decision.get('label')}**",
        f"Allowed use: {decision.get('allowed_use')}",
        "",
        payload.get("safety_boundary", ""),
        "",
        "## US Snapshot",
        "",
        f"- Production formula 1D: sample={formula.get('sample_size')} alpha={_fmt_pct(formula.get('alpha_pct'))} hit={_fmt_pct(formula.get('hit_rate_pct'))}",
        f"- Shadow gate 1D: reviewed={shadow.get('reviewed')} coverage={_fmt_pct(shadow.get('coverage_pct'))} alpha={_fmt_pct(shadow.get('alpha_pct'))} hit={_fmt_pct(shadow.get('hit_rate_pct'))}",
        f"- Shadow source runs: {us.get('shadow_runs')}/{(us.get('criteria') or {}).get('min_shadow_runs')} (raw artifacts={us.get('raw_shadow_artifact_count')})",
        "",
        "## Checks",
        "",
    ]
    for item in payload.get("checks") or []:
        lines.append(f"- [{item.get('status')}] {item.get('title')}: {item.get('message')}")
        details = item.get("details")
        if isinstance(details, list):
            for detail in details[:6]:
                lines.append(f"  - {detail}")
    blockers = payload.get("blockers") or []
    if blockers:
        lines.extend(["", "## Blockers", ""])
        for item in blockers:
            lines.append(f"- {item}")
    watch_items = payload.get("watch_items") or []
    if watch_items:
        lines.extend(["", "## Watch Items", ""])
        for item in watch_items:
            lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def run() -> dict[str, Any]:
    return build_readiness(
        quality=_load_json("data/latest/recommendation_quality_gate.json"),
        acceptance=_load_json("data/latest/production_acceptance_check.json"),
        us_acceptance=_load_json("data/latest/us_production_acceptance_check.json"),
        shadow=_load_json("data/latest/shadow_tuning_evidence.json"),
        us_preflight=_load_json("data/latest/us_shadow_preflight_check.json"),
        plan=_load_json("data/latest/plan_a_v5.json"),
        validation=_load_json("data/latest/strategy_validation_report.json"),
        pipeline=_load_json("data/latest/pipeline_status_production.json"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a US-first recommendation readiness snapshot.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    parser.add_argument("--strict", action="store_true", help="Return non-zero when status is FAIL.")
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
        us = payload.get("us") or {}
        print(f"Recommendation readiness: {payload.get('status')}")
        print(f"  decision={decision.get('code')} · {decision.get('label')}")
        print(f"  allowed_use={decision.get('allowed_use')}")
        print(
            f"  US shadow source={us.get('shadow_runs')}/{(us.get('criteria') or {}).get('min_shadow_runs')} "
            f"(raw={us.get('raw_shadow_artifact_count')}) · gaps={us.get('gaps_to_trial') or []}"
        )
        for item in (payload.get("blockers") or [])[:6]:
            print(f"  [BLOCKER] {item}")
        for item in (payload.get("watch_items") or [])[:8]:
            print(f"  [WATCH] {item}")
        print(f"  JSON: {OUT_JSON}")
    if args.strict and payload.get("status") == "FAIL":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
