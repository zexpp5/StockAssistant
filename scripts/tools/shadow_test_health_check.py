#!/usr/bin/env python3
"""Build a single read-only health snapshot for AI recommendation shadow tests.

This answers the operator question: "Are the several shadow/validation jobs
actually complete, or are we waiting on something that will never fix itself?"

Safety boundary:
- reads existing JSON artifacts only;
- writes only data/latest/shadow_test_health_check.json and
  data/reports/shadow_test_health_check.md;
- does not change recommendation formulas, strategy versions, watchlist,
  real holdings, portfolio plans, or database tables.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
OUT_JSON = REPO / "data" / "latest" / "shadow_test_health_check.json"
OUT_MD = REPO / "data" / "reports" / "shadow_test_health_check.md"

ARTIFACTS = {
    "readiness": "data/latest/recommendation_readiness_check.json",
    "shadow_preflight": "data/latest/us_shadow_preflight_check.json",
    "shadow_evidence": "data/latest/shadow_tuning_evidence.json",
    "strict_trial": "data/latest/us_strict_trial.json",
    "strict_caliber": "data/latest/strict_caliber_backtest.json",
    "strategy_validation": "data/latest/strategy_validation_report.json",
    "failure_diagnosis": "data/latest/strategy_failure_diagnosis.json",
    "tuning_proposal": "data/latest/strategy_tuning_proposal.json",
    "formula_proxy": "data/latest/formula_proxy_backtest.json",
    "momentum_ic": "data/latest/momentum_ic_validation.json",
    "value_ic": "data/latest/value_ic_validation.json",
    "revision_ic": "data/latest/revision_ic_validation.json",
    "grade_ic": "data/latest/grade_ic_validation.json",
    "pead_ic": "data/latest/pead_ic_validation.json",
    "pt_ic": "data/latest/pt_ic_validation.json",
}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _artifact_inputs(root: Path = REPO) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, rel in ARTIFACTS.items():
        path = root / rel
        payload = _load_json(path)
        out[key] = {
            "path": str(path),
            "exists": path.exists(),
            "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds") if path.exists() else None,
            "payload": payload,
        }
    return out


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


def _status(value: Any, default: str = "UNKNOWN") -> str:
    text = str(value or default).strip().upper()
    return text or default


def _get(artifacts: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
    return artifacts.get(key, {}).get("payload") or {}


def _exists(artifacts: dict[str, dict[str, Any]], key: str) -> bool:
    return bool(artifacts.get(key, {}).get("exists"))


def _row(
    *,
    check_id: str,
    name: str,
    kind: str,
    status: str,
    source: str,
    key_metrics: dict[str, Any] | None = None,
    blockers: list[str] | None = None,
    warnings: list[str] | None = None,
    next_action: str = "",
    waiting_for_data: bool = False,
    action_required: bool = False,
    can_accelerate_with_history: bool = False,
    usable_for: str = "",
) -> dict[str, Any]:
    return {
        "id": check_id,
        "name": name,
        "kind": kind,
        "status": status,
        "source": source,
        "key_metrics": key_metrics or {},
        "blockers": blockers or [],
        "warnings": warnings or [],
        "next_action": next_action,
        "waiting_for_data": waiting_for_data,
        "action_required": action_required,
        "can_accelerate_with_history": can_accelerate_with_history,
        "usable_for": usable_for,
    }


def _strict_trial_row(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    payload = _get(artifacts, "strict_trial")
    if not _exists(artifacts, "strict_trial"):
        return _row(
            check_id="us_strict_trial",
            name="US 严筛前瞻试运行",
            kind="前瞻验证",
            status="MISSING",
            source=ARTIFACTS["strict_trial"],
            blockers=["缺少 us_strict_trial.json"],
            next_action="先运行 scripts/tools/us_strict_trial.py。",
            action_required=True,
            usable_for="不可判断",
        )

    decision = payload.get("decision") or {}
    gate = decision.get("trial_review_gate") or payload.get("trial_review_gate") or {}
    checks = gate.get("checks") or []
    failed = [str(c.get("label") or c.get("code")) for c in checks if isinstance(c, dict) and not c.get("passed")]
    evidence = decision.get("strict_evidence") or payload.get("strict_evidence") or {}
    status = _status(payload.get("status"))
    ready = _status(gate.get("status")) == "READY" or bool(gate.get("ready"))
    return _row(
        check_id="us_strict_trial",
        name="US 严筛前瞻试运行",
        kind="前瞻验证",
        status="PASS" if ready else status,
        source=ARTIFACTS["strict_trial"],
        key_metrics={
            "reviewed": evidence.get("n"),
            "avg_alpha_pct": evidence.get("avg_alpha_pct"),
            "win_rate_pct": evidence.get("win_rate_pct"),
            "gate_status": gate.get("status") or gate.get("status_label"),
        },
        warnings=failed,
        next_action=(
            "继续等待下一批独立确认轮数；不是缺历史数据，而是要验证上线后新样本是否稳定。"
            if failed else "已满足当前严筛前瞻门槛；仍需买前审查。"
        ),
        waiting_for_data=bool(failed),
        action_required=False,
        can_accelerate_with_history=False,
        usable_for="研究队列；未达标前不作为真钱小仓试探依据",
    )


def _shadow_preflight_row(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    payload = _get(artifacts, "shadow_preflight")
    if not _exists(artifacts, "shadow_preflight"):
        return _row(
            check_id="us_shadow_preflight",
            name="US shadow 预检",
            kind="数据/匹配体检",
            status="MISSING",
            source=ARTIFACTS["shadow_preflight"],
            blockers=["缺少 us_shadow_preflight_check.json"],
            next_action="先运行 scripts/tools/us_shadow_preflight_check.py。",
            action_required=True,
            usable_for="不可判断",
        )
    gate = payload.get("trial_gate") or {}
    gaps = [str(x) for x in gate.get("gaps") or []]
    warnings = [str(x) for x in payload.get("warnings") or []]
    status = _status(payload.get("status"))
    alpha = _num(gate.get("shadow_avg_alpha_pct"))
    coverage = _num(gate.get("shadow_review_coverage_pct"))
    newest_missing = any("最新生产推荐" in item for item in warnings)
    alpha_bad = alpha is not None and alpha <= 0
    coverage_bad = coverage is not None and coverage < (_num(((payload.get("criteria") or {}).get("min_coverage_pct"))) or 80.0)
    return _row(
        check_id="us_shadow_preflight",
        name="US shadow 预检",
        kind="数据/匹配体检",
        status=status,
        source=ARTIFACTS["shadow_preflight"],
        key_metrics={
            "unique_source_run_count": gate.get("unique_source_run_count"),
            "raw_shadow_artifact_count": gate.get("raw_shadow_artifact_count"),
            "reviewed": gate.get("reviewed_shadow_buy_count"),
            "coverage_pct": coverage,
            "avg_alpha_pct": alpha,
            "win_rate_pct": gate.get("shadow_win_rate"),
        },
        warnings=warnings,
        blockers=gaps if status == "FAIL" else [],
        next_action=(
            "先补最新生产 run 的 shadow 预检；同时继续观察覆盖率和 alpha。"
            if newest_missing else
            "不是单纯等数据：alpha 仍为负，先不要放行调权影子版本。"
            if alpha_bad else
            "继续等 1D outcome 覆盖率成熟。"
            if coverage_bad else
            "预检已通过，可交给 readiness 判断。"
        ),
        waiting_for_data=coverage_bad and not alpha_bad,
        action_required=newest_missing or alpha_bad or status == "FAIL",
        can_accelerate_with_history=False,
        usable_for="判断 shadow 调权版本能否进入试探",
    )


def _shadow_evidence_row(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    payload = _get(artifacts, "shadow_evidence")
    if not _exists(artifacts, "shadow_evidence"):
        return _row(
            check_id="shadow_tuning_evidence",
            name="多市场 shadow 调权证据",
            kind="前瞻汇总",
            status="MISSING",
            source=ARTIFACTS["shadow_evidence"],
            blockers=["缺少 shadow_tuning_evidence.json"],
            next_action="先运行 shadow tuning 评估链路。",
            action_required=True,
            usable_for="不可判断",
        )
    decision = payload.get("activation_decision") or {}
    blockers = [str(x) for x in decision.get("blockers") or []]
    status = _status(decision.get("status") or payload.get("status"))
    market_rows = payload.get("market_horizon_summary") or []
    us_1d = next(
        (
            row for row in market_rows
            if isinstance(row, dict)
            and str(row.get("market") or "").upper() == "US"
            and str(row.get("horizon") or "") == "1d"
        ),
        {},
    )
    return _row(
        check_id="shadow_tuning_evidence",
        name="多市场 shadow 调权证据",
        kind="前瞻汇总",
        status=status,
        source=ARTIFACTS["shadow_evidence"],
        key_metrics={
            "us_reviewed": us_1d.get("reviewed_shadow_buy_count"),
            "us_coverage_pct": us_1d.get("shadow_review_coverage_pct"),
            "us_avg_alpha_pct": us_1d.get("shadow_avg_alpha_pct"),
            "us_win_rate": us_1d.get("shadow_win_rate"),
            "market_horizon_rows": len(market_rows),
        },
        blockers=blockers,
        next_action=(
            "当前调权 shadow 证据未达标，不要升级生产公式；先用作诊断。"
            if blockers else "证据达标后也需要用户确认，不能自动切生产版本。"
        ),
        waiting_for_data=any("coverage" in item for item in blockers),
        action_required=any("alpha" in item or "hit rate" in item for item in blockers),
        can_accelerate_with_history=False,
        usable_for="调权版本是否可升级的总闸门",
    )


def _strict_caliber_row(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    payload = _get(artifacts, "strict_caliber")
    if not _exists(artifacts, "strict_caliber"):
        return _row(
            check_id="strict_caliber_backtest",
            name="B1 严筛口径历史回算",
            kind="历史代理/候选筛选",
            status="MISSING",
            source=ARTIFACTS["strict_caliber"],
            blockers=["缺少 strict_caliber_backtest.json"],
            next_action="先运行 scripts/tools/strict_caliber_backtest.py。",
            action_required=True,
            usable_for="不可判断",
        )
    forward = payload.get("forward_progress") or {}
    return _row(
        check_id="strict_caliber_backtest",
        name="B1 严筛口径历史回算",
        kind="历史代理/候选筛选",
        status=_status(payload.get("status")),
        source=ARTIFACTS["strict_caliber"],
        key_metrics={
            "best_candidate": payload.get("best_candidate"),
            "forward_start": forward.get("forward_start"),
            "forward_n": forward.get("n"),
            "upgrade_ready": forward.get("upgrade_ready"),
            "forward_stats": forward.get("stats"),
        },
        warnings=[] if forward.get("upgrade_ready") else ["历史回算可筛候选，但前瞻升级门槛未通过"],
        next_action="用它快速缩小候选规则；不能把历史冠军直接当真钱规则。",
        waiting_for_data=not bool(forward.get("upgrade_ready")),
        action_required=False,
        can_accelerate_with_history=True,
        usable_for="快速筛规则方向，不负责生产放行",
    )


def _strategy_validation_row(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    payload = _get(artifacts, "strategy_validation")
    if not _exists(artifacts, "strategy_validation"):
        return _row(
            check_id="strategy_validation",
            name="生产推荐 PIT 验证",
            kind="PIT 历史验证",
            status="MISSING",
            source=ARTIFACTS["strategy_validation"],
            blockers=["缺少 strategy_validation_report.json"],
            next_action="先运行 build_strategy_validation_v2.py。",
            action_required=True,
            usable_for="不可判断",
        )
    summary = payload.get("summary") or {}
    markets = summary.get("markets") or {}
    us = markets.get("US") or {}
    return _row(
        check_id="strategy_validation",
        name="生产推荐 PIT 验证",
        kind="PIT 历史验证",
        status=_status(payload.get("status")),
        source=ARTIFACTS["strategy_validation"],
        key_metrics={
            "us_sample_size": us.get("sample_size"),
            "us_wins": us.get("wins"),
            "markets": markets,
            "policy_validation_items": summary.get("policy_validation_items"),
        },
        next_action="继续每日滚动；这是最接近真实推荐口径的历史验证。",
        waiting_for_data=False,
        action_required=False,
        can_accelerate_with_history=True,
        usable_for="判断生产推荐规则是否仍可用于研究",
    )


def _readiness_row(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    payload = _get(artifacts, "readiness")
    if not _exists(artifacts, "readiness"):
        return _row(
            check_id="recommendation_readiness",
            name="推荐可用性总闸门",
            kind="总控闸门",
            status="MISSING",
            source=ARTIFACTS["readiness"],
            blockers=["缺少 recommendation_readiness_check.json"],
            next_action="先运行 scripts/tools/recommendation_readiness_check.py。",
            action_required=True,
            usable_for="不可判断",
        )
    decision = payload.get("decision") or {}
    us = payload.get("us") or {}
    gaps = [str(x) for x in us.get("gaps_to_trial") or []]
    return _row(
        check_id="recommendation_readiness",
        name="推荐可用性总闸门",
        kind="总控闸门",
        status=_status(payload.get("status")),
        source=ARTIFACTS["readiness"],
        key_metrics={
            "decision": decision.get("code"),
            "allowed_use": decision.get("allowed_use"),
            "trial_ready": us.get("trial_ready"),
            "shadow_runs": us.get("shadow_runs"),
            "shadow_1d": us.get("shadow_1d"),
            "production_formula_1d": us.get("production_formula_1d"),
        },
        warnings=gaps,
        next_action="按这个总闸门决定使用边界：现在只到研究/买前审查，未到真钱试探。",
        waiting_for_data=any("覆盖" in item or "reviewed" in item for item in gaps),
        action_required=any("alpha" in item and "<=" in item for item in gaps),
        can_accelerate_with_history=False,
        usable_for="最终用户可用边界",
    )


def _diagnosis_row(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    diagnosis = _get(artifacts, "failure_diagnosis")
    proposal = _get(artifacts, "tuning_proposal")
    missing = [name for name in ("failure_diagnosis", "tuning_proposal") if not _exists(artifacts, name)]
    if missing:
        return _row(
            check_id="diagnosis_and_proposal",
            name="失败诊断与调参建议",
            kind="诊断/建议",
            status="MISSING",
            source=f"{ARTIFACTS['failure_diagnosis']} + {ARTIFACTS['tuning_proposal']}",
            blockers=[f"缺少 {name}" for name in missing],
            next_action="先补齐失败诊断和调参建议产物。",
            action_required=True,
            usable_for="不可判断",
        )
    summary = diagnosis.get("summary") or {}
    return _row(
        check_id="diagnosis_and_proposal",
        name="失败诊断与调参建议",
        kind="诊断/建议",
        status=_status(proposal.get("status"), "OK"),
        source=f"{ARTIFACTS['failure_diagnosis']} + {ARTIFACTS['tuning_proposal']}",
        key_metrics={
            "sample_count": summary.get("sample_count"),
            "negative_alpha_count": summary.get("negative_alpha_count"),
            "proposal_actions": len(proposal.get("market_actions") or []),
        },
        next_action="只作为调参候选，不直接改生产公式。",
        waiting_for_data=False,
        action_required=False,
        can_accelerate_with_history=True,
        usable_for="解释为什么输、提出候选修正",
    )


def _formula_proxy_row(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    payload = _get(artifacts, "formula_proxy")
    if not _exists(artifacts, "formula_proxy"):
        return _row(
            check_id="formula_proxy_backtest",
            name="两年公式代理回测",
            kind="历史代理/快速回放",
            status="MISSING",
            source=ARTIFACTS["formula_proxy"],
            blockers=["缺少 formula_proxy_backtest.json"],
            next_action="若要快速看方向，运行 scripts/tools/backtest_formula_proxy.py。",
            action_required=False,
            can_accelerate_with_history=True,
            usable_for="不可判断",
        )
    strategies = payload.get("strategies") or {}
    best_name = None
    best_alpha = None
    for name, row in strategies.items():
        if not isinstance(row, dict):
            continue
        alpha = _num(row.get("avg_alpha_per_period_pct"))
        if alpha is not None and (best_alpha is None or alpha > best_alpha):
            best_name, best_alpha = name, alpha
    return _row(
        check_id="formula_proxy_backtest",
        name="两年公式代理回测",
        kind="历史代理/快速回放",
        status="OK",
        source=ARTIFACTS["formula_proxy"],
        key_metrics={
            "strategy_count": len(strategies),
            "best_proxy_strategy": best_name,
            "best_avg_alpha_per_period_pct": best_alpha,
            "window": payload.get("window"),
            "benchmark": payload.get("benchmark"),
        },
        warnings=["存在代理假设和幸存者偏差，只能筛方向，不能直接放行真钱"],
        next_action="用它快速淘汰明显差的权重；最终仍要回到 PIT/前瞻验证。",
        waiting_for_data=False,
        action_required=False,
        can_accelerate_with_history=True,
        usable_for="快速出结果的研究工具",
    )


def _factor_ic_row(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    keys = ["momentum_ic", "value_ic", "revision_ic", "grade_ic", "pead_ic", "pt_ic"]
    verdicts: dict[str, str] = {}
    missing: list[str] = []
    for key in keys:
        if not _exists(artifacts, key):
            missing.append(key)
            continue
        payload = _get(artifacts, key)
        verdicts[key.replace("_ic", "")] = _status(payload.get("verdict") or payload.get("status"))
    passed = [k for k, v in verdicts.items() if v == "PASS"]
    failed = [k for k, v in verdicts.items() if v == "FAIL"]
    status = "WARN" if failed or missing else "PASS"
    return _row(
        check_id="factor_ic",
        name="单因子 IC 体检",
        kind="因子诊断",
        status=status,
        source="data/latest/*_ic_validation.json",
        key_metrics={"verdicts": verdicts, "passed": passed, "failed": failed},
        warnings=[f"{name} IC 未通过" for name in failed] + [f"{name} 缺失" for name in missing],
        next_action="通过的因子可保留观察；未通过的不要靠拍脑袋加权。",
        waiting_for_data=False,
        action_required=bool(missing),
        can_accelerate_with_history=True,
        usable_for="判断哪些因子值得进入候选公式",
    )


def _data_acquisition_routes() -> list[dict[str, Any]]:
    """Known ways to reduce "missing data" without pretending future data exists."""
    return [
        {
            "id": "outcome_backfill",
            "missing_data": "已成熟推荐的 1D/5D/20D 后续表现",
            "can_fetch_now": "YES_IF_PRICES_EXIST",
            "commands": [
                "/opt/homebrew/bin/python3 scripts/tools/evaluate_v2_picks.py",
                "/opt/homebrew/bin/python3 scripts/tools/evaluate_shadow_tuning_run.py",
                "/opt/homebrew/bin/python3 scripts/tools/us_shadow_preflight_check.py",
            ],
            "solves": "把已成熟样本写入 pick_outcomes，并刷新 shadow 覆盖率/alpha。",
            "does_not_solve": "今天刚推荐、未来还没收盘的 1D/5D/20D 结果。",
            "source": "本地 price_daily + benchmark price_daily；缺口时 evaluate_v2_picks 有 yfinance 兜底。",
        },
        {
            "id": "price_history_backfill",
            "missing_data": "历史价格、动量/反转窗口、历史代理回测原料",
            "can_fetch_now": "YES_NETWORK",
            "commands": [
                "/opt/homebrew/bin/python3 scripts/pipeline/backfill_price_history.py --market US --years 2",
            ],
            "solves": "补 price_daily 历史 K 线，减少动量/回测靠临时外部下载。",
            "does_not_solve": "没有 PIT 的分析师预期、当时估值、当时 AI 证据。",
            "source": "yfinance 历史日线。",
        },
        {
            "id": "benchmark_prices",
            "missing_data": "SPY/QQQ/^HSI/沪深300 等基准收盘",
            "can_fetch_now": "YES_NETWORK",
            "commands": [
                "/opt/homebrew/bin/python3 scripts/pipeline/ingest_benchmark_prices.py",
            ],
            "solves": "减少 alpha_pct 因缺 benchmark 而为空；让 pick_outcomes 更完整。",
            "does_not_solve": "个股自身缺价或未来未成熟。",
            "source": "yfinance benchmark history 写入 price_daily。",
        },
        {
            "id": "analyst_grade_history",
            "missing_data": "分析师评级升降级历史",
            "can_fetch_now": "YES_NETWORK",
            "commands": [
                "/opt/homebrew/bin/python3 scripts/tools/backfill_grade_history.py",
                "/opt/homebrew/bin/python3 scripts/tools/validate_grade_ic.py",
            ],
            "solves": "给 grade 因子提供长历史 PIT 事件原料，并重新验证 IC。",
            "does_not_solve": "真实 forward EPS/PEG 的完整历史一致预期。",
            "source": "yfinance Ticker.upgrades_downgrades。",
        },
        {
            "id": "earnings_surprise_history",
            "missing_data": "历史财报 EPS surprise",
            "can_fetch_now": "YES_NETWORK",
            "commands": [
                "/opt/homebrew/bin/python3 scripts/tools/backfill_earnings_surprises.py",
                "/opt/homebrew/bin/python3 scripts/tools/validate_pead_ic.py",
            ],
            "solves": "给 PEAD/财报公告后漂移因子提供历史原料。",
            "does_not_solve": "未来财报 surprise；公告前不能提前知道。",
            "source": "yfinance Ticker.get_earnings_dates。",
        },
        {
            "id": "ai_theme_evidence_refresh",
            "missing_data": "AI/产业主题证据、ETF 持仓、SEC 证据新鲜度",
            "can_fetch_now": "YES_PARTIAL",
            "commands": [
                "/opt/homebrew/bin/python3 -m stock_research.jobs.ai_theme_evidence_refresh --refresh-etf",
                "/opt/homebrew/bin/python3 -m stock_research.jobs.ai_theme_evidence_refresh --scan-sec",
            ],
            "solves": "刷新主题证据和 stale 标记，减少公司身份/主题标签过期。",
            "does_not_solve": "公司未披露的订单、客户、CapEx 细节；这类只能等公开披露或人工研究。",
            "source": "ETF snapshot / SEC EDGAR / 已配置主题证据源。",
        },
        {
            "id": "institutional_13f",
            "missing_data": "机构持仓/13F 线索",
            "can_fetch_now": "YES_WITH_LAG",
            "commands": [
                "/opt/homebrew/bin/python3 -m stock_research.jobs.refresh_13f",
            ],
            "solves": "补机构持仓线索，可做长期/拥挤度辅助。",
            "does_not_solve": "实时买卖。13F 天然滞后，不能当日内交易信号。",
            "source": "SEC 13F。",
        },
        {
            "id": "future_forward_outcomes",
            "missing_data": "今天新推荐之后的未来收益",
            "can_fetch_now": "NO",
            "commands": [],
            "solves": "无；只能等交易日收盘后再由 outcome_backfill 回填。",
            "does_not_solve": "不能用数据源提前获得未来价格，除非做预测；预测不能当验证结果。",
            "source": "未来市场价格，尚未发生。",
        },
    ]


def build_health(
    artifacts: dict[str, dict[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    artifacts = artifacts or _artifact_inputs()

    rows = [
        _readiness_row(artifacts),
        _shadow_preflight_row(artifacts),
        _shadow_evidence_row(artifacts),
        _strict_trial_row(artifacts),
        _strict_caliber_row(artifacts),
        _strategy_validation_row(artifacts),
        _diagnosis_row(artifacts),
        _formula_proxy_row(artifacts),
        _factor_ic_row(artifacts),
    ]

    missing = [row for row in rows if row["status"] == "MISSING"]
    action_required = [row for row in rows if row["action_required"]]
    waiting = [row for row in rows if row["waiting_for_data"]]
    fast_history = [row for row in rows if row["can_accelerate_with_history"]]

    readiness = _get(artifacts, "readiness")
    decision = readiness.get("decision") or {}
    status = "FAIL" if missing else "WARN" if action_required or waiting else "PASS"
    if status == "FAIL":
        decision_code = "SHADOW_HEALTH_MISSING_ARTIFACTS"
        label = "影子测试体检缺关键产物"
    elif action_required:
        decision_code = "SHADOW_HEALTH_ACTION_REQUIRED"
        label = "影子测试不是单纯等数据"
    elif waiting:
        decision_code = "SHADOW_HEALTH_WAITING_FOR_OUTCOMES"
        label = "影子测试在等未来收益成熟"
    else:
        decision_code = "SHADOW_HEALTH_OK"
        label = "影子测试链路完整"

    return {
        "schema_version": "shadow_test_health_v1",
        "generated_at": now.isoformat(timespec="seconds"),
        "status": status,
        "decision": {
            "code": decision_code,
            "label": label,
            "recommendation_readiness": decision.get("code"),
            "allowed_use": decision.get("allowed_use"),
        },
        "safety_boundary": (
            "Read-only health artifact. It does not create stock pools, write watchlist, "
            "write real holdings, alter recommendation formulas, or activate strategy versions."
        ),
        "summary": {
            "total_checks": len(rows),
            "missing_count": len(missing),
            "action_required_count": len(action_required),
            "waiting_for_data_count": len(waiting),
            "history_acceleration_count": len(fast_history),
        },
        "checks": rows,
        "what_can_be_fast": [
            "用 strict_caliber_backtest / formula_proxy_backtest / factor IC 做历史代理筛选，快速淘汰差口径。",
            "用 strategy_validation 的 PIT 样本判断当前生产公式是否仍可研究使用。",
            "只把历史代理当候选筛选；不要把同一段历史调出来的冠军直接升级成真钱规则。",
        ],
        "data_acquisition_routes": _data_acquisition_routes(),
        "why_wait_data": [
            "前瞻验证要等推荐发生之后的 1D/5D/20D 收益，否则就是提前看答案。",
            "shadow 调权版本必须按唯一 source run 统计，raw artifact 多不代表独立样本多。",
            "覆盖率不足时，少数已成熟股票可能偏向先涨或先跌，直接放行会产生样本偏差。",
        ],
        "why_not_all_history": [
            "历史价格可以重建 momentum/reversal，但 AI 证据、分析师预期、估值、红旗和数据缺口不一定有每日 PIT 快照。",
            "用今天的标签或估值去回测过去，会发生未来函数泄漏，结果通常虚高。",
            "历史代理适合快速筛方向，真钱边界仍要靠 PIT 快照和前瞻样本确认。",
        ],
        "next_actions": [
            row["next_action"]
            for row in rows
            if row.get("action_required") or row.get("waiting_for_data")
        ],
    }


def to_markdown(payload: dict[str, Any]) -> str:
    decision = payload.get("decision") or {}
    summary = payload.get("summary") or {}
    lines = [
        "# Shadow Test Health Check",
        "",
        f"Generated: {payload.get('generated_at')}",
        f"Status: **{payload.get('status')}**",
        f"Decision: **{decision.get('label')}**",
        f"Recommendation readiness: `{decision.get('recommendation_readiness') or '—'}`",
        "",
        payload.get("safety_boundary", ""),
        "",
        "## Summary",
        "",
        f"- Checks: {summary.get('total_checks')}",
        f"- Missing: {summary.get('missing_count')}",
        f"- Action required: {summary.get('action_required_count')}",
        f"- Waiting for data: {summary.get('waiting_for_data_count')}",
        f"- Can accelerate with history: {summary.get('history_acceleration_count')}",
        "",
        "## Checks",
        "",
        "| Test | Kind | Status | Key metrics | Next action |",
        "|---|---|---|---|---|",
    ]
    for row in payload.get("checks") or []:
        metrics = row.get("key_metrics") or {}
        compact = []
        for key, value in metrics.items():
            if isinstance(value, dict):
                continue
            if key.endswith("_pct"):
                compact.append(f"{key}={_fmt_pct(value)}")
            else:
                compact.append(f"{key}={value}")
            if len(compact) >= 4:
                break
        lines.append(
            f"| {row.get('name')} | {row.get('kind')} | {row.get('status')} | "
            f"{'<br>'.join(compact) or '—'} | {row.get('next_action') or '—'} |"
        )
    for title, key in (
        ("What Can Be Fast", "what_can_be_fast"),
        ("Data Acquisition Routes", "data_acquisition_routes"),
        ("Why Wait Data", "why_wait_data"),
        ("Why Not All History", "why_not_all_history"),
        ("Next Actions", "next_actions"),
    ):
        items = payload.get(key) or []
        if not items:
            continue
        lines.extend(["", f"## {title}", ""])
        if key == "data_acquisition_routes":
            lines.extend([
                "| Route | Can fetch now | Solves | Does not solve | Commands |",
                "|---|---|---|---|---|",
            ])
            for item in items:
                commands = "<br>".join(f"`{cmd}`" for cmd in item.get("commands") or []) or "—"
                lines.append(
                    f"| {item.get('missing_data')} | {item.get('can_fetch_now')} | "
                    f"{item.get('solves')} | {item.get('does_not_solve')} | {commands} |"
                )
            continue
        for item in items:
            lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a unified AI recommendation shadow-test health snapshot.")
    parser.add_argument("--json", action="store_true", help="Print full JSON payload.")
    parser.add_argument("--strict", action="store_true", help="Return non-zero when status is FAIL.")
    args = parser.parse_args(argv)

    payload = build_health()
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    OUT_MD.write_text(to_markdown(payload), encoding="utf-8")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        decision = payload.get("decision") or {}
        summary = payload.get("summary") or {}
        print(f"Shadow test health: {payload.get('status')}")
        print(f"  decision={decision.get('code')} · {decision.get('label')}")
        print(
            "  checks={total_checks} · missing={missing_count} · action={action_required_count} · "
            "waiting={waiting_for_data_count} · history_fast={history_acceleration_count}".format(**summary)
        )
        for item in (payload.get("next_actions") or [])[:8]:
            print(f"  [NEXT] {item}")
        print(f"  JSON: {OUT_JSON}")

    if args.strict and payload.get("status") == "FAIL":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
