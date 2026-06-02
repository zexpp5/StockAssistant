#!/usr/bin/env python3
"""Build a read-only shadow tuning proposal from strategy failure diagnosis.

The proposal translates evidence into conservative next-step parameters. It
does not update recommendation formulas, strategy_versions, portfolio_plans, or
holdings. Production changes should be implemented as a separate shadow
strategy and compared point-in-time before activation.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from scripts.tools import strategy_failure_diagnosis as diagnosis  # noqa: E402

OUT_JSON = REPO / "data" / "latest" / "strategy_tuning_proposal.json"
OUT_MD = REPO / "data" / "reports" / "strategy_tuning_proposal.md"

DEFAULT_STRATEGY_VERSION = os.environ.get("STOCK_ASSISTANT_STRATEGY_VERSION", "latest")
DEFAULT_HORIZON = os.environ.get("STOCK_ASSISTANT_TUNING_HORIZON", "1d")

MARKET_LABELS = {"US": "美股", "CN": "A股", "HK": "港股"}


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _fmt_pct(value: Any) -> str:
    v = _as_float(value)
    if v is None:
        return "-"
    return f"{v:+.2f}%"


def _market_actions(diag: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    covered_markets = {str(row.get("market")) for row in diag.get("market_summary") or []}
    top5_alpha_by_market = {
        str(row.get("market")): row.get("avg_alpha_pct")
        for row in diag.get("rank_bucket_summary") or []
        if row.get("rank_bucket") == "top_1_5"
    }

    for row in diag.get("market_summary") or []:
        market = str(row.get("market"))
        label = MARKET_LABELS.get(market, market)
        n = int(row.get("n") or 0)
        win_rate = _as_float(row.get("win_rate")) or 0.0
        avg_alpha = _as_float(row.get("avg_alpha_pct"))
        top5_alpha = _as_float(top5_alpha_by_market.get(market))
        if n >= 30 and avg_alpha is not None and avg_alpha <= -1 and win_rate < 45:
            action = {
                "market": market,
                "label": label,
                "status": "degraded",
                "portfolio_multiplier": 0.35,
                "recommendation_mode": "research_only_until_shadow_passes",
                "reason": f"{label} {n} 个样本平均 alpha {avg_alpha:+.2f}%，胜率 {win_rate:.1f}%。",
            }
            if top5_alpha is not None and top5_alpha <= -1:
                action["formula_note"] = (
                    f"Top1-5 alpha {top5_alpha:+.2f}%，不是单纯缩小 Top20 可以解决。"
                )
                action["candidate_count_change"] = "do_not_cut_only; pair with factor/gate changes"
            actions.append(action)

    for row in diag.get("coverage_summary") or []:
        market = str(row.get("market"))
        if market in covered_markets:
            continue
        due = int(row.get("calendar_due") or 0)
        reviewed = int(row.get("reviewed") or 0)
        pending = int(row.get("pending_data_ready") or 0)
        if due > 0 and reviewed == 0 and pending > 0:
            label = MARKET_LABELS.get(market, market)
            actions.append({
                "market": market,
                "label": label,
                "status": "evidence_pending",
                "portfolio_multiplier": 1.0,
                "recommendation_mode": "keep_current_until_alpha_available",
                "reason": f"{label}有 {due} 个日历到期样本，但 {pending} 个仍缺收盘/基准数据，不能评价输赢。",
            })
    return actions


def _factor_actions(diag: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in diag.get("factor_diagnostics") or []:
        market = str(row.get("market"))
        factor = str(row.get("factor"))
        key = (market, factor)
        if key in seen:
            continue
        seen.add(key)
        reason = str(row.get("reason") or "")
        if factor == "data_quality":
            action = "convert_to_gate_only"
            proposed = "不再把 data_quality 当 alpha 加分；只用于缺数据降级。"
        elif factor == "valuation":
            action = "reduce_weight_and_require_confirmation"
            proposed = "降低估值主导权重，估值高分必须叠加量价/催化确认。"
        elif factor == "momentum":
            action = "reduce_or_zero_weight"
            proposed = "该市场先降低或归零 momentum 权重，等待 5D/20D 复核。"
        elif factor == "reversal":
            action = "reduce_weight_until_multihorizon_confirms"
            proposed = "短期反转因子先降权，避免把继续下跌误当触底。"
        elif factor == "f_score":
            action = "no_standalone_boost"
            proposed = "F-score 不单独加分，只作为质量过滤/辅助。"
        else:
            action = "review_weight"
            proposed = "进入因子权重复核。"
        actions.append({
            "market": market,
            "label": MARKET_LABELS.get(market, market),
            "factor": factor,
            "action": action,
            "proposed_change": proposed,
            "evidence": reason,
        })
    return actions


def _gate_actions(diag: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for row in diag.get("risk_flag_summary") or []:
        flag = str(row.get("risk_flag") or "")
        n = int(row.get("n") or 0)
        avg_alpha = _as_float(row.get("avg_alpha_pct"))
        if flag == "MOMENTUM_REUSED_RECENT_V2_SNAPSHOT" and n >= 10 and avg_alpha is not None and avg_alpha <= -1:
            actions.append({
                "risk_flag": flag,
                "market": row.get("market"),
                "action": "demote_buy_to_watch_or_score_haircut",
                "proposed_change": "动量字段回退样本不直接进 buy；shadow 版先扣 8-12 分或降为 watch。",
                "evidence": f"n={n}, avg_alpha={avg_alpha:+.2f}%。",
            })
        if flag == "OVERHEATED_1Y" and avg_alpha is not None and avg_alpha <= -2:
            actions.append({
                "risk_flag": flag,
                "market": row.get("market"),
                "action": "strengthen_overheat_gate",
                "proposed_change": "1Y 过热样本先进入买前审查，不作为自动 buy。",
                "evidence": f"n={n}, avg_alpha={avg_alpha:+.2f}%。",
            })
    return actions


def _activation_criteria() -> list[str]:
    return [
        "新策略只做 shadow run，不覆盖当前 production strategy_version。",
        "每个参与调权的市场至少 reviewed alpha 样本 >= 60，且 evidence coverage >= 80%。",
        "1D 不再是唯一依据；5D/20D 任一窗口不能继续显著负 alpha。",
        "AI 组合方案只能读取 signal='buy' 且无强 gate 的 shadow picks。",
        "切生产前必须生成新 strategy_version，保留旧版本审计。",
    ]


def build_proposal(
    *,
    strategy_version: str | None = DEFAULT_STRATEGY_VERSION,
    horizon: str | None = DEFAULT_HORIZON,
) -> dict[str, Any]:
    diag = diagnosis.build_report(strategy_version=strategy_version, horizon=horizon, markets=None)
    generated_at = datetime.now().isoformat(timespec="seconds")
    suffix = datetime.now().strftime("%Y%m%d")
    market_actions = _market_actions(diag)
    factor_actions = _factor_actions(diag)
    gate_actions = _gate_actions(diag)
    return {
        "schema_version": "strategy_tuning_proposal_v1",
        "generated_at": generated_at,
        "source_diagnosis_strategy": diag.get("strategy_version_filter"),
        "source_horizon": diag.get("horizon_filter"),
        "proposed_strategy_version": f"tech_ai_v2_guarded_shadow_{suffix}",
        "status": "SHADOW_ONLY",
        "safety_boundary": (
            "Read-only proposal. Does not update production formula, recommendation_runs, "
            "portfolio_plans, watchlist, or real holdings."
        ),
        "market_actions": market_actions,
        "factor_actions": factor_actions,
        "gate_actions": gate_actions,
        "activation_criteria": _activation_criteria(),
        "source_summary": {
            "sample_count": (diag.get("summary") or {}).get("sample_count", 0),
            "negative_alpha_count": (diag.get("summary") or {}).get("negative_alpha_count", 0),
            "coverage_summary": diag.get("coverage_summary") or [],
            "market_summary": diag.get("market_summary") or [],
        },
    }


def _to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Strategy Tuning Proposal",
        "",
        f"Generated: {payload['generated_at']}",
        f"Source strategy: **{payload.get('source_diagnosis_strategy')}**",
        f"Proposed shadow version: **{payload.get('proposed_strategy_version')}**",
        f"Status: **{payload.get('status')}**",
        "",
        payload.get("safety_boundary", ""),
        "",
        "## Market Actions",
        "",
        "| Market | Status | Portfolio Multiplier | Mode | Reason |",
        "|---|---|---:|---|---|",
    ]
    for row in payload.get("market_actions") or []:
        lines.append(
            f"| {row.get('label') or row.get('market')} | {row.get('status')} | "
            f"{row.get('portfolio_multiplier')} | {row.get('recommendation_mode')} | "
            f"{row.get('reason')} {row.get('formula_note', '')} |"
        )

    lines.extend([
        "",
        "## Factor Actions",
        "",
        "| Market | Factor | Action | Proposed Change | Evidence |",
        "|---|---|---|---|---|",
    ])
    for row in payload.get("factor_actions") or []:
        lines.append(
            f"| {row.get('label') or row.get('market')} | {row.get('factor')} | "
            f"{row.get('action')} | {row.get('proposed_change')} | {row.get('evidence')} |"
        )

    lines.extend([
        "",
        "## Gate Actions",
        "",
        "| Market | Risk Flag | Action | Proposed Change | Evidence |",
        "|---|---|---|---|---|",
    ])
    gates = payload.get("gate_actions") or []
    if gates:
        for row in gates:
            market = MARKET_LABELS.get(str(row.get("market")), str(row.get("market")))
            lines.append(
                f"| {market} | {row.get('risk_flag')} | {row.get('action')} | "
                f"{row.get('proposed_change')} | {row.get('evidence')} |"
            )
    else:
        lines.append("| - | - | - | No gate action triggered. | - |")

    lines.extend([
        "",
        "## Activation Criteria",
        "",
    ])
    for item in payload.get("activation_criteria") or []:
        lines.append(f"- {item}")

    lines.extend([
        "",
        "## Source Coverage",
        "",
        "| Market | Horizon | Calendar Due | Reviewed | Pending Data |",
        "|---|---|---:|---:|---:|",
    ])
    for row in (payload.get("source_summary") or {}).get("coverage_summary") or []:
        market = MARKET_LABELS.get(str(row.get("market")), str(row.get("market")))
        lines.append(
            f"| {market} | {row.get('horizon')} | {row.get('calendar_due', 0)} | "
            f"{row.get('reviewed', 0)} | {row.get('pending_data_ready', 0)} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build read-only strategy tuning proposal.")
    parser.add_argument("--strategy-version", default=DEFAULT_STRATEGY_VERSION)
    parser.add_argument("--horizon", default=DEFAULT_HORIZON)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    horizon = None if str(args.horizon).lower() in {"all", "*", ""} else str(args.horizon)
    payload = build_proposal(strategy_version=args.strategy_version, horizon=horizon)
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    OUT_MD.write_text(_to_markdown(payload), encoding="utf-8")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Strategy tuning proposal: {payload['status']}")
        print(f"  proposed={payload['proposed_strategy_version']}")
        print(f"  markets={len(payload.get('market_actions') or [])} factors={len(payload.get('factor_actions') or [])} gates={len(payload.get('gate_actions') or [])}")
        print(f"  JSON: {OUT_JSON}")
        print(f"  MD:   {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
