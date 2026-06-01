#!/usr/bin/env python3
"""End-to-end acceptance check for the production recommendation pipeline.

This is stricter than recommendation_quality_gate.py.  The quality gate checks
whether today's recommendations are safe enough to trade.  This script checks
whether the whole production chain is closed: schema, latest DuckDB rows,
generated JSON files, optimizer method, trade deltas, dashboard, and brief.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from stock_research import config  # noqa: E402

DB_PATH = Path(config.DUCKDB_PATH)
OUT_PATH = REPO / "data" / "latest" / "production_acceptance_check.json"

# 2026-05-21 V1 cutover：旧 PRODUCTION_SOURCES = ("v6_us","v6_hk","v6_cn") 已删；
# V2 通过 universe_scope='system_tech_universe' 标识生产推荐
V2_UNIVERSE_SCOPE = "system_tech_universe"
REQUIRED_PICK_COLUMNS = {
    "signal",
    "coverage_score",
    "missing_factors",
    "factor_weights_used",
}

CORE_LATEST_JSON = (
    "data/latest/factor_scores_today.json",
    "data/latest/plan_a_v5.json",
    "data/latest/plan_v6.json",
    "data/latest/recommendation_quality_gate.json",
    "data/latest/recommendation_evidence.json",
    "data/latest/pipeline_status.json",
    "data/latest/source_health.json",
    "data/latest/trade_delta.json",
    "data/latest/risk_metrics.json",
    "data/discovery_candidates.json",
)

PRODUCTION_PIPELINE_STATUS_JSON = (
    "data/latest/pipeline_status_production.json",
    # Backward-compatible production alias. Research runs must not write this
    # file anymore, but keep it here while old workspaces roll forward.
    "data/latest/pipeline_status.json",
    "data/latest/pipeline_status_morning.json",
    "data/latest/pipeline_status_full.json",
    "data/latest/pipeline_status_a_share_only.json",
    "data/latest/pipeline_status_skip_a_share.json",
)

HK_LATEST_JSON = (
    "data/latest/hk_picks.json",
    "data/latest/trade_delta_hk.json",
)

CN_LATEST_JSON = (
    "data/a_share_picks.json",
    "data/latest/trade_delta_cn.json",
)

# IPO & 次新股 tab 数据源（dashboard 用）
# WARN 级而非 FAIL：IPO 数据滞后不影响 v2 主推荐的生产正确性，
# 但页面会空/旧，应该提示同事而不是阻断 pipeline
IPO_LATEST_JSON = (
    "data/latest/junior_stock_radar.json",
)

# 事件日历 / catalyst 数据源（dashboard 的 📰 推荐依据 + morning_brief 的 📰 一句话）
# WARN 级：事件源滞后或缺失会让 📰 解释消失，但主推荐还能跑。
EVENT_CALENDAR_JSON = (
    "data/event_calendar.json",            # A 股 akshare 财报 + 解禁 + 减增持
    "data/event_calendar_hk.json",         # 港股 yfinance 财报 + EPS 超预期
    "data/event_calendar_us.json",         # 美股 yfinance 财报 + EPS 超预期
    "data/event_calendar_hk_hkex.json",    # 港股 HKEX 披露易公告（盈警/停牌/股东/回购/并购）
    "data/event_calendar_us_sec.json",     # 美股 SEC EDGAR（8-K/13G/13D/DEF 14A）
    "data/event_calendar_us_form4.json",   # 美股 SEC Form 4 内部人交易（净买/卖聚合）
)


def _issue(level: str, code: str, message: str, details: Any = None) -> dict[str, Any]:
    item = {"level": level, "code": code, "message": message}
    if details is not None:
        item["details"] = details
    return item


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except Exception:
            continue
    return None


def _json_load(rel: str) -> tuple[dict[str, Any] | None, Path]:
    path = REPO / rel
    if not path.exists():
        return None, path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return (payload if isinstance(payload, dict) else {"_payload": payload}), path
    except Exception as e:
        return {"_error": str(e)}, path


def _payload_dt(payload: dict[str, Any] | None, path: Path) -> datetime | None:
    if payload:
        for key in ("generated_at", "updated_at", "completed_at", "as_of", "date", "timestamp"):
            dt = _parse_dt(payload.get(key))
            if dt:
                return dt
    if path.exists():
        return datetime.fromtimestamp(path.stat().st_mtime)
    return None


def _load_production_pipeline_status() -> tuple[dict[str, Any] | None, Path | None, str | None]:
    """Return the newest production-line status, explicitly ignoring research."""
    candidates: list[tuple[datetime, int, dict[str, Any], Path, str]] = []
    for index, rel in enumerate(PRODUCTION_PIPELINE_STATUS_JSON):
        payload, path = _json_load(rel)
        if not payload or "_error" in payload:
            continue
        mode = str(payload.get("mode") or "").lower()
        role = str(payload.get("status_role") or "").lower()
        if mode == "research" or role == "research":
            continue
        dt = _payload_dt(payload, path) or datetime.min
        # Prefer fresher files; for identical timestamps prefer explicit production file.
        candidates.append((dt, -index, payload, path, rel))
    if not candidates:
        return None, None, None
    _dt, _rank, payload, path, rel = max(candidates, key=lambda x: (x[0], x[1]))
    return payload, path, rel


def _check_pipeline_status(
    *,
    issues: list[dict[str, Any]],
    summary: dict[str, Any],
    now: datetime,
) -> dict[str, Any] | None:
    pipeline_status, _path, source_rel = _load_production_pipeline_status()
    if not pipeline_status:
        issues.append(_issue(
            "FAIL",
            "production_pipeline_status_missing",
            "未找到 production/morning/a_share_only 流水线状态；research 状态不能作为今日生产验收依据",
        ))
        summary["pipeline_status_source"] = None
        return None

    summary["pipeline_status_source"] = source_rel
    pstatus = str(pipeline_status.get("status") or "UNKNOWN").upper()
    summary["pipeline_status"] = {
        "run_id": pipeline_status.get("run_id"),
        "mode": pipeline_status.get("mode"),
        "status_role": pipeline_status.get("status_role"),
        "source": source_rel,
        "status": pstatus,
        "started_at": pipeline_status.get("started_at"),
        "updated_at": pipeline_status.get("updated_at"),
        "completed_at": pipeline_status.get("completed_at"),
        "failed_steps": len(pipeline_status.get("failed_steps") or []),
    }
    if pipeline_status.get("failed_steps"):
        issues.append(_issue(
            "FAIL",
            "pipeline_failed_steps_present",
            f"{source_rel} 存在失败步骤，不能只看最终 JSON 产物",
            (pipeline_status.get("failed_steps") or [])[:10],
        ))
    updated = _parse_dt(pipeline_status.get("updated_at"))
    if pstatus in {"FAIL", "FAILED", "ERROR"}:
        age = (now - updated) if updated else None
        issues.append(_issue(
            "FAIL",
            "pipeline_not_completed",
            f"{source_rel} 当前为 {pstatus}，报告可能混用半成品/跨轮次产物",
            {"updated_at": pipeline_status.get("updated_at"), "age_minutes": int(age.total_seconds() / 60) if age else None},
        ))
    elif pstatus == "RUNNING":
        # daily_refresh 内部 step 跑 acceptance 时 pipeline 必然 RUNNING；只在 updated_at 长时间未推进时报 WARN（疑似卡死）。
        age = (now - updated) if updated else None
        stale_minutes = 30
        if age is not None and age.total_seconds() > stale_minutes * 60:
            issues.append(_issue(
                "WARN",
                "pipeline_running_stale",
                f"{source_rel} 已 {int(age.total_seconds()/60)} 分钟未推进（>{stale_minutes} 分钟），可能卡死",
                {"updated_at": pipeline_status.get("updated_at"), "age_minutes": int(age.total_seconds() / 60)},
            ))
    return pipeline_status


def _a_share_ready(now: datetime) -> bool:
    return now.isoweekday() >= 6 or now.hour >= 16


def _calibrated_a_share_weights_status() -> tuple[bool, str]:
    path = REPO / "data" / "calibrated_factor_weights.json"
    if not path.exists():
        return False, "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False, "unreadable"
    if not isinstance(data, dict):
        return False, "invalid_payload"
    market = str(data.get("market") or data.get("universe") or "").lower()
    if market and market not in {"a_share", "ashare", "cn", "china", "v6_cn", "a股"}:
        return False, f"wrong_market:{market}"
    validated = data.get("validated") is True or str(data.get("validation_status") or "").lower() in {
        "pass", "passed", "valid", "validated",
    }
    if not validated:
        return False, "not_validated"
    weights = data.get("weights")
    if not isinstance(weights, dict):
        return False, "missing_weights"
    try:
        total = sum(float(v) for v in weights.values())
    except Exception:
        return False, "non_numeric_weight"
    if abs(total - 1.0) > 1e-4:
        return False, f"weights_sum:{total:.6f}"
    return True, "validated"


def _watchlist_market_flags(conn: duckdb.DuckDBPyConnection | None) -> dict[str, bool]:
    """V2: manual_watchlist 含哪些市场（用户手动加的自选股按市场分桶）。"""
    flags = {"us": False, "hk": False, "cn": False}
    if conn is None:
        return flags
    try:
        rows = conn.execute(
            "SELECT UPPER(market) FROM manual_watchlist"
        ).fetchall()
    except Exception:
        return flags
    for (market,) in rows:
        if market == "US":
            flags["us"] = True
        elif market == "HK":
            flags["hk"] = True
        elif market == "CN":
            flags["cn"] = True
    return flags


def _connect_with_retry(path: str, *, read_only: bool = True,
                        retries: int = 10, delay: float = 3.0) -> duckdb.DuckDBPyConnection:
    """重试连库 — 与 scripts/tools/drop_v1_tables_v2.py:_connect_with_retry 对齐。

    DuckDB 同进程间互斥，API server (launchd 常驻) + daily_refresh 并行跑时偶尔
    撞锁；验收脚本是 daily_refresh step 27，撞锁直接 raise 会让整条 pipeline FAIL，
    所以这里做 retry，最坏情况退到调用端做 read-only fallback。
    """
    last_err: Exception | None = None
    for i in range(retries):
        try:
            return duckdb.connect(path, read_only=read_only)
        except duckdb.IOException as e:
            last_err = e
            if i < retries - 1:
                time.sleep(delay)
    assert last_err is not None
    raise last_err


def _check_v1_tables_gone(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """V1 表存在 → 报 FAIL（V1 cutover 后这些表必须不在）。

    清单与 scripts/tools/drop_v1_tables_v2.py:V1_TABLES_TO_DROP 同步——
    一个负责检测并 FAIL，一个负责每日 pipeline 开头 DROP；保持两边一致。
    """
    v1_tables = {"prices", "picks", "reviews", "watchlist",
                 "discovery_history", "discovery_tracking", "earnings_history"}
    try:
        existing = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
    except Exception:
        return []
    return sorted(v1_tables & existing)


def _latest_json_checks(
    issues: list[dict[str, Any]],
    rel_paths: tuple[str, ...],
    *,
    max_age_days: int,
    level: str = "FAIL",
) -> dict[str, Any]:
    now = datetime.now()
    summary: dict[str, Any] = {}
    for rel in rel_paths:
        payload, path = _json_load(rel)
        if payload is None:
            issues.append(_issue(level, "missing_latest_artifact", f"{rel} 不存在"))
            summary[rel] = {"exists": False}
            continue
        if "_error" in payload:
            issues.append(_issue("FAIL", "invalid_json_artifact", f"{rel} 不是合法 JSON", payload["_error"]))
            summary[rel] = {"exists": True, "valid_json": False}
            continue
        dt = _payload_dt(payload, path)
        age_days = (now.date() - dt.date()).days if dt else None
        summary[rel] = {
            "exists": True,
            "generated_at": dt.isoformat() if dt else None,
            "age_days": age_days,
        }
        if dt is None:
            issues.append(_issue("WARN", "artifact_missing_timestamp", f"{rel} 无 generated_at/as_of/date"))
        elif age_days is not None and age_days > max_age_days:
            issues.append(_issue(level, "stale_latest_artifact", f"{rel} 已滞后 {age_days} 天"))
    return summary


def _extract_tickers(plan_payload: dict[str, Any]) -> set[str]:
    rows = plan_payload.get("plan_v5") or plan_payload.get("plan_v6") or plan_payload.get("plan") or []
    out: set[str] = set()
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                ticker = row.get("ticker") or row.get("code")
                weight = row.get("capped_weight", row.get("target_weight", row.get("v5_weight", 0)))
                try:
                    active = float(weight or 0) > 1e-9
                except Exception:
                    active = True
                if ticker and active:
                    out.add(str(ticker).upper())
    return out


def _extract_plan_weights(plan_payload: dict[str, Any]) -> dict[str, float]:
    rows = plan_payload.get("plan_v5") or plan_payload.get("plan_v6") or plan_payload.get("plan") or []
    out: dict[str, float] = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = row.get("ticker") or row.get("code") or row.get("symbol")
            if not ticker:
                continue
            weight = row.get("capped_weight", row.get("target_weight", row.get("v5_weight", 0)))
            try:
                w = float(weight or 0)
            except Exception:
                continue
            if w > 1e-9:
                out[str(ticker).upper()] = w
    return out


def _is_fallback_plan(plan_payload: dict[str, Any]) -> bool:
    method = str(plan_payload.get("method") or "").lower()
    risk_aware = plan_payload.get("risk_aware") if isinstance(plan_payload.get("risk_aware"), dict) else {}
    constraints = plan_payload.get("constraints") if isinstance(plan_payload.get("constraints"), dict) else {}
    engine = str(risk_aware.get("engine") or "").lower()
    return bool(
        constraints.get("use_legacy_mc")
        or "fallback" in engine
        or "legacy_monte_carlo" in engine
        or ("markowitz mc" in method and "risk_aware_optimize" not in method)
    )


def _has_f_score(factor_scores_json: Any) -> bool:
    if isinstance(factor_scores_json, str):
        try:
            payload = json.loads(factor_scores_json)
        except Exception:
            return False
    elif isinstance(factor_scores_json, dict):
        payload = factor_scores_json
    else:
        return False
    candidates = (
        payload.get("f_score"),
        payload.get("piotroski_f_score"),
        (payload.get("piotroski") or {}).get("f_score") if isinstance(payload.get("piotroski"), dict) else None,
    )
    return any(v is not None for v in candidates)


def _check_portfolio_plan_alignment(
    *,
    issues: list[dict[str, Any]],
    summary: dict[str, Any],
    plan_payload: dict[str, Any] | None,
) -> None:
    """Verify the production DB plan is the same risk-aware plan as latest JSON."""
    if not plan_payload or "_error" in plan_payload:
        return
    try:
        conn = duckdb.connect(str(DB_PATH), read_only=True)
    except Exception as e:
        issues.append(_issue(
            "WARN",
            "portfolio_plan_alignment_unchecked",
            "无法只读打开 DuckDB 检查组合方案口径一致性",
            str(e),
        ))
        return
    try:
        tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
        required = {"recommendation_runs", "portfolio_plans"}
        missing = sorted(required - tables)
        if missing:
            issues.append(_issue("FAIL", "portfolio_plan_tables_missing",
                                 "无法验收 AI 组合方案口径，DuckDB 缺少表", missing))
            return
        latest = conn.execute(
            """
            SELECT run_id
            FROM recommendation_runs
            WHERE universe_scope = 'system_tech_universe' AND status = 'generated'
            ORDER BY generated_at DESC
            LIMIT 1
            """
        ).fetchone()
        if not latest:
            return
        latest_run_id = str(latest[0])
        db_rows = conn.execute(
            """
            SELECT plan_version, symbol, target_weight
            FROM portfolio_plans
            WHERE run_id = ? AND strategy_scope = 'system_tech_universe'
            ORDER BY symbol
            """,
            [latest_run_id],
        ).fetchall()
    finally:
        conn.close()

    plan_versions = sorted({str(r[0]) for r in db_rows})
    db_weights = {
        str(symbol).upper(): float(weight)
        for _version, symbol, weight in db_rows
        if weight is not None and float(weight) > 1e-9
    }
    json_weights = _extract_plan_weights(plan_payload)
    source_summary = {
        "run_id": latest_run_id,
        "plan_versions": plan_versions,
        "db_rows": len(db_weights),
        "json_rows": len(json_weights),
        "db_gross_exposure": round(sum(db_weights.values()), 6),
        "json_gross_exposure": round(sum(json_weights.values()), 6),
    }
    summary["portfolio_plan_source"] = source_summary
    if not db_weights:
        issues.append(_issue("FAIL", "portfolio_plan_missing_for_latest_run",
                             "最新 V2 run 没有可执行 portfolio_plans 权重"))
    elif plan_versions != ["v6_risk_aware"]:
        issues.append(_issue(
            "FAIL",
            "portfolio_plan_not_risk_aware_source",
            "最新 V2 run 的 portfolio_plans 不是唯一 v6_risk_aware 来源，存在等权/旧口径混用风险",
            source_summary,
        ))
    elif set(db_weights) != set(json_weights):
        issues.append(_issue(
            "FAIL",
            "portfolio_plan_symbols_diverge",
            "DuckDB portfolio_plans 与 data/latest/plan_a_v5.json 标的集合不一致",
            {
                "db_only": sorted(set(db_weights) - set(json_weights)),
                "json_only": sorted(set(json_weights) - set(db_weights)),
            },
        ))
    else:
        mismatched = [
            {"symbol": s, "db": round(db_weights[s], 6), "json": round(json_weights[s], 6)}
            for s in sorted(db_weights)
            if abs(db_weights[s] - json_weights[s]) > 1e-5
        ]
        if mismatched:
            issues.append(_issue(
                "FAIL",
                "portfolio_plan_weights_diverge",
                "DuckDB portfolio_plans 与 data/latest/plan_a_v5.json 权重不一致",
                mismatched[:10],
            ))


def _surface_artifact_checks(
    *,
    issues: list[dict[str, Any]],
    summary: dict[str, Any],
    now: datetime,
    max_age_days: int,
) -> None:
    """Checks that final user-visible artifacts are fresh and internally labeled."""
    artifact_summary: dict[str, Any] = {}
    artifact_summary.update(_latest_json_checks(issues, CORE_LATEST_JSON, max_age_days=max_age_days))
    # IPO tab 数据源放宽到 2 天 + WARN：早班加跑后通常 <24h，给周末/失败一点 buffer
    artifact_summary.update(_latest_json_checks(
        issues, IPO_LATEST_JSON, max_age_days=max(max_age_days, 2), level="WARN"
    ))
    # 事件日历同样 WARN + 2 天 buffer（事件源滞后只影响 📰 解释,不阻断主推荐）
    artifact_summary.update(_latest_json_checks(
        issues, EVENT_CALENDAR_JSON, max_age_days=max(max_age_days, 2), level="WARN"
    ))
    summary["artifacts"] = artifact_summary

    qgate, _ = _json_load("data/latest/recommendation_quality_gate.json")
    if qgate and "_error" not in qgate:
        qstatus = str(qgate.get("status") or "UNKNOWN")
        summary["quality_gate_status"] = qstatus
        if qstatus == "FAIL":
            issues.append(_issue("FAIL", "quality_gate_fail", "recommendation_quality_gate 当前为 FAIL"))
        elif qstatus == "WARN":
            issues.append(_issue("WARN", "quality_gate_warn", "recommendation_quality_gate 当前为 WARN"))

    plan_a, plan_path = _json_load("data/latest/plan_a_v5.json")
    plan_v6, _ = _json_load("data/latest/plan_v6.json")
    pipeline_status = _check_pipeline_status(issues=issues, summary=summary, now=now)

    brief = REPO / "morning_brief.md"
    if plan_a and "_error" not in plan_a:
        method = str(plan_a.get("method") or "")
        engine = str((plan_a.get("risk_aware") or {}).get("engine") or "")
        summary["plan_method"] = method
        summary["plan_engine"] = engine
        if "risk_aware_optimize" not in method and engine != "risk_aware_optimize":
            issues.append(_issue("FAIL", "us_plan_not_risk_aware", "plan_a_v5.json 不是 risk-aware optimizer 产物"))
        if _is_fallback_plan(plan_a):
            brief_text = brief.read_text(encoding="utf-8") if brief.exists() else ""
            if "仓位来源=legacy_monte_carlo fallback" in brief_text or "legacy_monte_carlo fallback" in brief_text:
                issues.append(_issue(
                    "INFO",
                    "us_plan_risk_aware_fallback_labeled",
                    "plan_a_v5.json 是 fallback/legacy 输出，morning_brief 已显式标注仓位来源",
                    {"engine": engine},
                ))
            else:
                issues.append(_issue(
                    "FAIL",
                    "us_plan_risk_aware_fallback_unlabeled",
                    "plan_a_v5.json 是 fallback/legacy 输出，但 morning_brief 未标注仓位来源",
                    {"engine": engine, "method": method},
                ))
    if plan_a and plan_v6 and "_error" not in plan_a and "_error" not in plan_v6:
        if _extract_tickers(plan_a) != _extract_tickers(plan_v6):
            issues.append(_issue("FAIL", "plan_a_v5_plan_v6_diverge", "plan_a_v5.json 与 plan_v6.json 标的集合不一致"))

    _check_portfolio_plan_alignment(issues=issues, summary=summary, plan_payload=plan_a)

    if plan_a and "_error" not in plan_a:
        cons = plan_a.get("constraints") if isinstance(plan_a.get("constraints"), dict) else {}
        cash_eff = cons.get("cash_pct_effective")
        cash_target = cons.get("cash_pct")
        if isinstance(cash_eff, (int, float)):
            summary["cash_pct_effective"] = float(cash_eff)
            summary["cash_pct_target"] = float(cash_target) if isinstance(cash_target, (int, float)) else None
            brief_text = brief.read_text(encoding="utf-8") if brief.exists() else ""
            cash_explained = "组合现金" in brief_text and "约束器" in brief_text
            details = {
                "cash_pct_effective": round(float(cash_eff), 4),
                "cash_pct_target": float(cash_target) if isinstance(cash_target, (int, float)) else None,
                "explained_in_brief": cash_explained,
            }
            if float(cash_eff) > 0.70:
                issues.append(_issue("FAIL", "plan_cash_extreme",
                    f"组合现金 {float(cash_eff)*100:.1f}% > 70%（接近全空），检查约束/优化器", details))
            elif float(cash_eff) > 0.50:
                issues.append(_issue("WARN", "plan_cash_high",
                    f"组合现金 {float(cash_eff)*100:.1f}% > 50%（已配≤一半），约束器抑制过强", details))
            elif float(cash_eff) > 0.30 and not cash_explained:
                issues.append(_issue("WARN", "plan_cash_unexplained",
                    f"组合现金 {float(cash_eff)*100:.1f}% > 30% 但 morning_brief 未给约束器解释", details))
            elif float(cash_eff) > 0.30:
                issues.append(_issue("WARN", "plan_cash_high_disclosed",
                    f"组合现金 {float(cash_eff)*100:.1f}% > 30%；虽已披露原因，但仍不应视为满额可执行组合", details))

    rec_evidence, _ = _json_load("data/latest/recommendation_evidence.json")
    evidence_dt = _payload_dt(rec_evidence, REPO / "data/latest/recommendation_evidence.json") if rec_evidence else None
    ref_payloads = [p for p in (plan_a, qgate, pipeline_status) if p and "_error" not in p]
    ref_dates = [_payload_dt(p, REPO) for p in ref_payloads]
    ref_dates = [d for d in ref_dates if d is not None]
    latest_ref = max(ref_dates) if ref_dates else None
    if rec_evidence and "_error" not in rec_evidence:
        summary["recommendation_evidence_generated_at"] = evidence_dt.isoformat(timespec="seconds") if evidence_dt else None
        if latest_ref and evidence_dt and evidence_dt < latest_ref - timedelta(minutes=5):
            issues.append(_issue(
                "WARN",
                "recommendation_evidence_older_than_current_run",
                "recommendation_evidence.json 早于当前 plan/qgate/pipeline，morning_brief 不能把它当成本轮推荐证据",
                {
                    "evidence_at": evidence_dt.isoformat(timespec="seconds"),
                    "latest_reference_at": latest_ref.isoformat(timespec="seconds"),
                },
            ))

    discovery, _ = _json_load("data/discovery_candidates.json")
    if discovery and "_error" not in discovery:
        n_cands = len(discovery.get("candidates") or [])
        universe_size = int(discovery.get("universe_size") or 0)
        summary["discovery_candidates"] = {
            "generated_at": discovery.get("generated_at"),
            "universe_size": universe_size,
            "candidate_count": n_cands,
        }
        if universe_size <= 0 or n_cands <= 0:
            issues.append(_issue(
                "FAIL",
                "discovery_candidates_empty",
                "全池 AI 推荐产物为空，AI 助手核心链路不可用",
                summary["discovery_candidates"],
            ))

    dashboard = REPO / "stock_dashboard.html"
    for path, code, label in (
        (dashboard, "dashboard_missing", "stock_dashboard.html"),
        (brief, "morning_brief_missing", "morning_brief.md"),
    ):
        if not path.exists():
            issues.append(_issue("FAIL", code, f"{label} 不存在；完整链路未生成最终阅读产物"))
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        summary[f"{label}_mtime"] = mtime.isoformat(timespec="seconds")
        if (now.date() - mtime.date()).days > max_age_days:
            issues.append(_issue("FAIL", f"{code}_stale", f"{label} 已滞后超过 {max_age_days} 天"))
    if dashboard.exists() and plan_path.exists():
        if dashboard.stat().st_mtime + 1 < plan_path.stat().st_mtime:
            issues.append(_issue("WARN", "dashboard_older_than_plan", "dashboard 比最新 plan 更旧，需要重建 HTML"))


def _check_api_freshness(now: datetime, summary: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    """API 进程启动时间必须晚于 stock_research/api/main.py 修改时间。

    否则即使代码已修，跑批后端服务仍跑陈旧版本，前端看到的字段会与 DuckDB 不一致。
    判定方式：launchctl list 拿 PID，ps -o lstart 拿启动时间，与 main.py mtime 比较。
    无法判定时（launchctl 不存在 / 服务未运行 / 进程读不到）不报错，只记 INFO。
    """
    import subprocess
    main_py = REPO / "stock_research" / "api" / "main.py"
    if not main_py.exists():
        return
    main_mtime = datetime.fromtimestamp(main_py.stat().st_mtime)
    summary["api_main_mtime"] = main_mtime.isoformat(timespec="seconds")
    try:
        out = subprocess.run(
            ["launchctl", "list", "com.linearview.stockassistant.api"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        summary["api_process"] = {"status": "launchctl_unavailable"}
        return
    if out.returncode != 0:
        summary["api_process"] = {"status": "service_not_loaded"}
        return
    pid = None
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith('"PID"'):
            try:
                pid = int(line.split("=", 1)[1].strip().rstrip(";").strip())
            except Exception:
                pid = None
            break
    if not pid:
        summary["api_process"] = {"status": "service_not_running"}
        return
    try:
        ps_out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        summary["api_process"] = {"status": "ps_failed", "pid": pid}
        return
    lstart_text = (ps_out.stdout or "").strip()
    if not lstart_text:
        summary["api_process"] = {"status": "pid_dead", "pid": pid}
        return
    # ps lstart 格式: "Wed May 21 10:53:05 2026"
    try:
        started_at = datetime.strptime(lstart_text, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        summary["api_process"] = {"status": "lstart_parse_failed", "pid": pid, "raw": lstart_text}
        return
    summary["api_process"] = {
        "pid": pid,
        "started_at": started_at.isoformat(timespec="seconds"),
        "main_mtime": main_mtime.isoformat(timespec="seconds"),
    }
    if main_mtime > started_at:
        age_h = round((main_mtime - started_at).total_seconds() / 3600, 1)
        issues.append(_issue(
            "WARN",
            "api_stale_deployment",
            f"API 进程启动于 {started_at.isoformat(timespec='minutes')}，但 main.py 已在 {age_h} 小时后被修改；"
            f"服务仍跑旧版本代码。修：launchctl kickstart -k gui/$(id -u)/com.linearview.stockassistant.api",
            {"pid": pid, "started_at": started_at.isoformat(), "main_mtime": main_mtime.isoformat(),
             "delta_hours": age_h},
        ))


def _build_payload(now: datetime, summary: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any]:
    n_fail = sum(1 for x in issues if x["level"] == "FAIL")
    n_warn = sum(1 for x in issues if x["level"] == "WARN")
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "status": "FAIL" if n_fail else ("WARN" if n_warn else "PASS"),
        "summary": {
            **summary,
            "fail": n_fail,
            "warn": n_warn,
            "info": sum(1 for x in issues if x["level"] == "INFO"),
        },
        "issues": issues,
    }


def run_check(max_age_days: int = 1, allow_a_share_disabled: bool = False) -> dict[str, Any]:
    now = datetime.now()
    allow_a_share_disabled = allow_a_share_disabled or not config.A_SHARE_PRODUCTION_ENABLED
    issues: list[dict[str, Any]] = []
    signal_counts: dict[str, dict[str, int]] = {}
    summary: dict[str, Any] = {
        "generated_at": now.isoformat(timespec="seconds"),
        "db_path": str(DB_PATH),
        "a_share_ready": _a_share_ready(now),
        "a_share_production_enabled": config.A_SHARE_PRODUCTION_ENABLED,
    }

    if not DB_PATH.exists():
        issues.append(_issue("FAIL", "missing_duckdb", f"{DB_PATH} 不存在"))
        conn = None
    else:
        try:
            conn = _connect_with_retry(str(DB_PATH))
        except duckdb.IOException as e:
            # 锁拿不到 → 不让验收脚本 crash 整条 pipeline；写 WARN 由调用者决定后续动作
            conn = None
            issues.append(_issue(
                "WARN", "duckdb_lock_unavailable",
                f"无法获取 DuckDB 连接（retry 后仍冲突）；本轮 DB 维度的检查跳过，"
                f"只能基于 data/latest/*.json 给结论",
                str(e),
            ))

    db_tables: set[str] = set()
    if conn is not None:
        try:
            db_tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
        except Exception as e:
            issues.append(_issue("FAIL", "duckdb_table_list_failed", "无法读取 DuckDB 表清单", str(e)))
        # 2026-05-21 V1 cutover：V1 表存在 → FAIL（保护已切到 V2 的库不再被回写 V1 数据）
        v1_present = _check_v1_tables_gone(conn)
        if v1_present:
            issues.append(_issue(
                "FAIL", "v1_tables_present",
                "V1 表不该再存在于库里；某处代码可能在重建（运行 init_stock_db_v2.py 重建库或单独 DROP）",
                v1_present,
            ))
    v2_required_tables = {
        "manual_watchlist", "real_holdings", "model_sim_holdings",
        "system_universe", "pool_membership",
        "price_daily", "recommendation_runs", "recommendation_picks",
        "portfolio_plans", "strategy_versions", "strategy_review_reports",
        "pipeline_runs", "pipeline_steps", "source_fetch_log",
        "data_quality_checks", "source_raw_snapshots",
    }
    is_v2_schema = "recommendation_picks" in db_tables or "system_universe" in db_tables
    summary["schema_mode"] = "v2" if is_v2_schema else "legacy"
    summary["db_tables"] = sorted(db_tables)
    if is_v2_schema:
        missing_v2 = sorted(v2_required_tables - db_tables)
        if missing_v2:
            issues.append(_issue("FAIL", "v2_schema_missing_tables", "v2 新库缺少 P0 表", missing_v2))
        if conn is not None:
            for table in ("manual_watchlist", "real_holdings", "model_sim_holdings",
                          "holdings", "system_universe", "pool_membership",
                          "price_daily", "recommendation_runs", "recommendation_picks", "portfolio_plans",
                          "pick_outcomes", "portfolio_performance", "factor_attribution",
                          "strategy_review_reports"):
                if table in db_tables:
                    try:
                        if table == "system_universe":
                            summary[f"{table}_count"] = int(conn.execute(
                                "SELECT COUNT(*) FROM system_universe WHERE active = TRUE"
                            ).fetchone()[0])
                        elif table == "pool_membership":
                            summary[f"{table}_count"] = int(conn.execute(
                                "SELECT COUNT(*) FROM pool_membership WHERE active = TRUE"
                            ).fetchone()[0])
                        else:
                            summary[f"{table}_count"] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    except Exception as e:
                        issues.append(_issue("FAIL", f"{table}_count_failed", f"无法读取 {table} 行数", str(e)))
            if "system_universe" in db_tables and summary.get("system_universe_count", 0) <= 0:
                issues.append(_issue("FAIL", "v2_system_universe_empty", "v2 system_universe 为空"))
            if "pool_membership" in db_tables and summary.get("pool_membership_count", 0) <= 0:
                issues.append(_issue("FAIL", "v2_pool_membership_empty", "v2 pool_membership 为空"))
            if "price_daily" in db_tables and summary.get("price_daily_count", 0) <= 0:
                issues.append(_issue("FAIL", "v2_price_daily_empty", "v2 还没有从新数据源拉取行情"))
            if summary.get("holdings_count", 0) > 0:
                issues.append(_issue(
                    "WARN",
                    "legacy_holdings_nonempty",
                    f"legacy holdings 表仍有 {summary.get('holdings_count')} 行；真实持仓应写 real_holdings，推荐模拟应写 model_sim_holdings",
                ))
            if "price_daily" in db_tables and "pool_membership" in db_tables and summary.get("price_daily_count", 0) > 0:
                active_pool_count = int(conn.execute(
                    "SELECT COUNT(*) FROM pool_membership WHERE active = TRUE AND pool_type = 'system_tech_universe'"
                ).fetchone()[0])
                priced_count = int(conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM (
                        SELECT DISTINCT p.market, p.symbol
                        FROM price_daily p
                        JOIN pool_membership m
                          ON m.market = p.market AND m.symbol = p.symbol
                         AND m.active = TRUE
                         AND m.pool_type = 'system_tech_universe'
                    )
                    """
                ).fetchone()[0])
                coverage = (priced_count / active_pool_count) if active_pool_count else 0.0
                summary["v2_price_coverage"] = {
                    "priced_count": priced_count,
                    "active_pool_count": active_pool_count,
                    "coverage_pct": round(coverage * 100, 2),
                }
                if coverage < 0.50:
                    issues.append(_issue(
                        "FAIL",
                        "v2_price_coverage_too_low",
                        f"v2 行情覆盖率过低：{priced_count}/{active_pool_count}",
                    ))
                elif coverage < 0.80:
                    issues.append(_issue(
                        "WARN",
                        "v2_price_coverage_low",
                        f"v2 行情覆盖率偏低：{priced_count}/{active_pool_count}",
                    ))
            if "recommendation_runs" in db_tables and summary.get("recommendation_runs_count", 0) <= 0:
                issues.append(_issue("FAIL", "v2_no_recommendation_runs", "v2 还没有生成 recommendation_runs"))
            if "recommendation_picks" in db_tables and summary.get("recommendation_picks_count", 0) <= 0:
                issues.append(_issue("FAIL", "v2_no_recommendation_picks", "v2 还没有生成 recommendation_picks"))
            if "strategy_review_reports" in db_tables and summary.get("strategy_review_reports_count", 0) <= 0:
                issues.append(_issue(
                    "INFO",
                    "v2_strategy_review_not_built",
                    "v2 策略验证报告尚未生成；不阻断今日推荐，但不能证明策略有效性",
                ))
            if "recommendation_picks" in db_tables and summary.get("recommendation_picks_count", 0) > 0:
                latest_run = conn.execute(
                    "SELECT run_id FROM recommendation_runs ORDER BY generated_at DESC LIMIT 1"
                ).fetchone()
                latest_run_id = latest_run[0] if latest_run else None
                summary["latest_recommendation_run_id"] = latest_run_id
                if latest_run_id:
                    rows = conn.execute(
                        """
                        SELECT signal, COUNT(*)
                        FROM recommendation_picks
                        WHERE run_id=?
                        GROUP BY signal
                        """,
                        [latest_run_id],
                    ).fetchall()
                    signal_counts["system_tech_universe"] = {str(sig): int(n) for sig, n in rows}
                    buy_count = signal_counts["system_tech_universe"].get("buy", 0)
                    if buy_count <= 0:
                        issues.append(_issue("FAIL", "v2_latest_run_no_buy", "v2 最新推荐批次没有 buy 推荐"))
                    factor_rows = conn.execute(
                        """
                        SELECT market, symbol, factor_scores_json
                        FROM recommendation_picks
                        WHERE run_id=?
                        """,
                        [latest_run_id],
                    ).fetchall()
                    f_score_rows = [
                        {"market": str(market), "symbol": str(symbol)}
                        for market, symbol, factor_json in factor_rows
                        if _has_f_score(factor_json)
                    ]
                    f_score_coverage = (len(f_score_rows) / len(factor_rows)) if factor_rows else 0.0
                    # 分市场覆盖率 —— 防止某个市场(如 CN 0/20)被全局总数(如 40/60=67%)掩盖成"达标"。
                    # factor_rows 每行已带 market,直接按市场分组。
                    f_score_by_market: dict[str, dict] = {}
                    for market, symbol, factor_json in factor_rows:
                        mk = str(market)
                        slot = f_score_by_market.setdefault(mk, {"with_f_score": 0, "pick_count": 0})
                        slot["pick_count"] += 1
                        if _has_f_score(factor_json):
                            slot["with_f_score"] += 1
                    for slot in f_score_by_market.values():
                        slot["coverage_pct"] = (
                            round(slot["with_f_score"] / slot["pick_count"] * 100, 2)
                            if slot["pick_count"] else 0.0
                        )
                    summary["v2_f_score_coverage"] = {
                        "with_f_score": len(f_score_rows),
                        "latest_pick_count": len(factor_rows),
                        "coverage_pct": round(f_score_coverage * 100, 2),
                        "by_market": f_score_by_market,
                    }
                    if f_score_coverage < 0.50:
                        statement_types: dict[str, int] = {}
                        if "financial_statements" in db_tables:
                            statement_types = {
                                str(k): int(v)
                                for k, v in conn.execute(
                                    """
                                    SELECT statement_type, COUNT(*)
                                    FROM financial_statements
                                    GROUP BY statement_type
                                    """
                                ).fetchall()
                            }
                        issues.append(_issue(
                            "WARN",
                            "v2_f_score_coverage_low",
                            (
                                "v2 最新推荐批次 F-Score 覆盖不足；Piotroski 9 项需要 income/balance/cashflow "
                                "三表，当前不能把基本面维度当作已覆盖"
                            ),
                            {
                                **summary["v2_f_score_coverage"],
                                "financial_statement_types": statement_types,
                            },
                        ))
                    # 分市场闸门:即使全局覆盖达标,单个市场覆盖 <50% 也要 WARN —— 否则
                    # CN 0/20 会被 US/HK 的高覆盖在全局总数里掩盖,基本面维度对 A 股根本没接上。
                    low_f_markets = sorted(
                        mk for mk, slot in f_score_by_market.items()
                        if slot["pick_count"] > 0 and slot["with_f_score"] / slot["pick_count"] < 0.50
                    )
                    if low_f_markets:
                        issues.append(_issue(
                            "WARN",
                            "v2_f_score_coverage_low_by_market",
                            "部分市场 F-Score 覆盖过低，基本面维度对这些市场不能算已覆盖：" + "，".join(
                                f"{mk} {f_score_by_market[mk]['with_f_score']}/{f_score_by_market[mk]['pick_count']}"
                                for mk in low_f_markets
                            ),
                            {"low_markets": low_f_markets, "by_market": f_score_by_market},
                        ))
                if latest_run_id:
                    bad_scope = conn.execute(
                        """
                        SELECT market, symbol, universe_scope, source_origin
                        FROM recommendation_picks
                        WHERE run_id = ?
                          AND (
                            COALESCE(universe_scope, '') <> 'system_tech_universe'
                            OR COALESCE(source_origin, '') <> 'system_pool'
                          )
                        LIMIT 30
                        """,
                        [latest_run_id],
                    ).fetchall()
                    if bad_scope:
                        issues.append(_issue(
                            "FAIL",
                            "v2_pick_scope_origin_invalid",
                            "v2 最新系统池推荐必须标记 system_tech_universe/system_pool",
                            [{"market": r[0], "symbol": r[1], "universe_scope": r[2], "source_origin": r[3]} for r in bad_scope],
                        ))
                    orphan = conn.execute(
                        """
                        SELECT p.market, p.symbol
                        FROM recommendation_picks p
                        LEFT JOIN pool_membership m
                          ON m.market = p.market
                         AND m.symbol = p.symbol
                         AND m.active = TRUE
                         AND m.pool_type = 'system_tech_universe'
                        WHERE p.run_id = ?
                          AND m.symbol IS NULL
                        LIMIT 30
                        """,
                        [latest_run_id],
                    ).fetchall()
                    if orphan:
                        issues.append(_issue(
                            "FAIL",
                            "v2_pick_not_in_system_pool",
                            "v2 最新系统推荐出现未属于 pool_membership 的股票",
                            [{"market": r[0], "symbol": r[1]} for r in orphan],
                        ))

            if {"system_universe", "source_raw_snapshots"}.issubset(db_tables):
                active_rows = conn.execute(
                    """
                    SELECT market, symbol
                    FROM system_universe
                    WHERE active = TRUE
                    """
                ).fetchall()
                active_keys = {
                    (str(market or "").upper(), str(symbol or "").upper())
                    for market, symbol in active_rows
                    if symbol
                }
                enrichment_rows = conn.execute(
                    """
                    SELECT business_date, market, payload_json
                    FROM source_raw_snapshots
                    WHERE source = 'v2_system_enrichment'
                      AND json_extract_string(payload_json, '$.symbol') IS NOT NULL
                    QUALIFY ROW_NUMBER() OVER (
                        PARTITION BY market, json_extract_string(payload_json, '$.symbol')
                        ORDER BY business_date DESC, fetched_at DESC
                    ) = 1
                    """
                ).fetchall()

                seen_keys: set[tuple[str, str]] = set()
                seen_by_day: dict[str, set[tuple[str, str]]] = {}
                missing_fields: list[dict[str, Any]] = []
                degraded_rows: list[dict[str, Any]] = []
                bad_payloads = 0
                required_detail_fields = ("earnings", "info_breakdown", "conclusion", "risks", "source_text")
                business_days: list[Any] = []
                for business_date, market, payload_json in enrichment_rows:
                    business_days.append(business_date)
                    try:
                        payload = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
                    except Exception:
                        bad_payloads += 1
                        continue
                    if not isinstance(payload, dict):
                        bad_payloads += 1
                        continue
                    payload_market = str(payload.get("market") or market or "").upper()
                    symbol = str(payload.get("symbol") or "").upper()
                    if not symbol:
                        bad_payloads += 1
                        continue
                    key = (payload_market, symbol)
                    if key in active_keys:
                        seen_keys.add(key)
                        seen_by_day.setdefault(str(business_date), set()).add(key)
                    blank = [field for field in required_detail_fields if not str(payload.get(field) or "").strip()]
                    if blank:
                        missing_fields.append({"market": payload_market, "symbol": symbol, "fields": blank})
                    if str(payload.get("external_source_error") or "").strip():
                        degraded_rows.append({
                            "market": payload_market,
                            "symbol": symbol,
                            "error": str(payload.get("external_source_error"))[:160],
                        })

                missing_keys = sorted(active_keys - seen_keys)
                latest_enrichment_day = max(business_days) if business_days else None
                oldest_seen_day = min(business_days) if business_days else None
                latest_enrichment_dt = _parse_dt(latest_enrichment_day)
                latest_day_key = str(latest_enrichment_day) if latest_enrichment_day else None
                latest_day_seen = len(seen_by_day.get(latest_day_key, set())) if latest_day_key else 0
                summary["v2_enrichment_coverage"] = {
                    "latest_business_date": str(latest_enrichment_day) if latest_enrichment_day else None,
                    "oldest_latest_symbol_business_date": str(oldest_seen_day) if oldest_seen_day else None,
                    "active_system_symbols": len(active_keys),
                    "enriched_system_symbols": len(seen_keys),
                    "latest_day_enriched_system_symbols": latest_day_seen,
                    "missing_system_symbols": len(missing_keys),
                    "missing_detail_rows": len(missing_fields),
                    "degraded_rows": len(degraded_rows),
                    "bad_payloads": bad_payloads,
                }
                if latest_enrichment_dt is None:
                    issues.append(_issue("FAIL", "v2_enrichment_missing", "v2 系统池详情 enrichment 尚未生成"))
                elif (now.date() - latest_enrichment_dt.date()).days > max_age_days:
                    issues.append(_issue(
                        "FAIL",
                        "v2_enrichment_stale",
                        f"v2 系统池详情 enrichment 已滞后：{latest_enrichment_day}",
                    ))
                if latest_day_seen and latest_day_seen < len(active_keys):
                    issues.append(_issue(
                        "INFO",
                        "v2_enrichment_latest_day_partial",
                        "最新 enrichment 日期只覆盖部分系统池；验收按每只股票最新快照计算，完整覆盖仍可用",
                        {
                            "latest_business_date": str(latest_enrichment_day),
                            "latest_day_enriched_system_symbols": latest_day_seen,
                            "active_system_symbols": len(active_keys),
                        },
                    ))
                if missing_keys:
                    issues.append(_issue(
                        "FAIL",
                        "v2_enrichment_incomplete",
                        "v2 系统池详情 enrichment 未覆盖全部 active system_universe 标的",
                        [{"market": m, "symbol": s} for m, s in missing_keys[:30]],
                    ))
                if bad_payloads:
                    issues.append(_issue(
                        "FAIL",
                        "v2_enrichment_bad_payload",
                        f"v2 系统池详情存在 {bad_payloads} 条无法解析的 payload",
                    ))
                if missing_fields:
                    issues.append(_issue(
                        "FAIL",
                        "v2_enrichment_blank_detail_fields",
                        "v2 系统池详情存在空白关键字段",
                        missing_fields[:30],
                    ))
                if degraded_rows:
                    issues.append(_issue(
                        "WARN",
                        "v2_enrichment_source_degraded",
                        "v2 系统池详情存在外部源降级；页面已展示 V2 fallback，但需要排查源稳定性",
                        degraded_rows[:30],
                    ))

        source_health, _ = _json_load("data/latest/source_health.json")
        if isinstance(source_health, dict) and source_health.get("_error") is None:
            yfinance_health = ((source_health.get("sources") or {}).get("yfinance") or {})
            yfinance_status = str(yfinance_health.get("status") or "").lower()
            summary["source_health_status"] = yfinance_status or "unknown"
            summary["source_health_reason"] = yfinance_health.get("reason")
            summary["source_health_markets"] = source_health.get("markets") or {}
            if yfinance_status in {"source_down", "source_degraded"} and summary.get("price_daily_count", 0) <= 0:
                issues.append(_issue(
                    "FAIL",
                    "v2_price_source_degraded",
                    f"v2 行情源当前降级({yfinance_status})，导致 price_daily 仍为空",
                    {
                        "reason": yfinance_health.get("reason"),
                        "operator_action": yfinance_health.get("operator_action"),
                        "summary": source_health.get("summary"),
                        "markets": source_health.get("markets") or {},
                    },
                ))

        summary["latest_signal_counts"] = signal_counts
        if conn is not None:
            conn.close()
            conn = None
        _surface_artifact_checks(
            issues=issues,
            summary=summary,
            now=now,
            max_age_days=max_age_days,
        )
        _check_api_freshness(now, summary, issues)
        return _build_payload(now, summary, issues)

    watchlist_markets = _watchlist_market_flags(conn)
    summary["manual_watchlist_market_flags"] = watchlist_markets
    # 2026-05-21 V1 cutover：required_sources (v6_us/v6_hk/v6_cn) 已废；
    # 自选股·AI 优选是 manual_watchlist 空就该空的设计，不再作为生产硬要求。
    required_sources: list[str] = []
    summary["required_production_sources"] = required_sources

    # V2 acceptance：检查 recommendation_runs / recommendation_picks / portfolio_plans
    # 在 V2 spec 下 watchlist 空是合法状态，但 V2 system_tech_universe 推荐必须每天有产出
    v2_summary: dict[str, Any] = {}
    if conn is not None and "recommendation_runs" in db_tables and "recommendation_picks" in db_tables:
        v2_run = conn.execute(
            """
            SELECT run_id, run_date, generated_at FROM recommendation_runs
            WHERE universe_scope = 'system_tech_universe' AND status = 'generated'
            ORDER BY generated_at DESC LIMIT 1
            """
        ).fetchone()
        if not v2_run:
            issues.append(_issue("FAIL", "v2_recommendation_run_missing",
                                 "V2 system_tech_universe 无 generated run；跑 build_v2_recommendations"))
            v2_summary["latest_run"] = None
        else:
            run_id, run_date, gen_at = v2_run
            today = now.date().isoformat()
            run_date_str = str(run_date)[:10]
            v2_summary["latest_run"] = {
                "run_id": run_id, "run_date": run_date_str, "generated_at": str(gen_at)[:19],
            }
            if run_date_str < today:
                issues.append(_issue("WARN", "v2_recommendation_run_stale",
                                     f"V2 推荐 run 日期 {run_date_str} 旧于今日 {today}"))
            # 按 market 统计 buy/total
            market_rows = conn.execute(
                """
                SELECT market, signal, COUNT(*) FROM recommendation_picks
                WHERE run_id = ? GROUP BY market, signal
                """,
                [run_id],
            ).fetchall()
            per_market: dict[str, dict[str, int]] = {}
            for market, signal, n in market_rows:
                per_market.setdefault(str(market), {})[str(signal)] = int(n)
            v2_summary["picks_by_market"] = per_market
            for m in ("US", "CN", "HK"):
                if not per_market.get(m, {}).get("buy"):
                    issues.append(_issue("WARN", f"v2_no_buy_{m.lower()}",
                                         f"V2 {m} 市场最新 run 没有 buy 信号；检查 build_v2_recommendations 评分阈值"))
        if "portfolio_plans" in db_tables:
            plan_n = int(conn.execute("SELECT COUNT(*) FROM portfolio_plans").fetchone()[0])
            v2_summary["portfolio_plans_total"] = plan_n
            if plan_n == 0:
                issues.append(_issue("FAIL", "v2_portfolio_plans_empty", "portfolio_plans 表为空；build_v2_recommendations 没写"))
    summary["v2"] = v2_summary

    # 2026-05-21 V1 cutover：原 V1 picks/watchlist 检查段(118 行) 已删除
    summary["latest_signal_counts"] = {}


    a_weights_ok, a_weights_status = _calibrated_a_share_weights_status()
    summary["a_share_calibration"] = {"ok": a_weights_ok, "status": a_weights_status}
    if not a_weights_ok and _a_share_ready(now):
        level = "WARN" if allow_a_share_disabled else "FAIL"
        issues.append(_issue(
            level,
            "a_share_calibration_not_production_ready",
            f"A 股无有效校准权重({a_weights_status})；收盘后不能作为生产推荐写库",
        ))

    artifact_summary: dict[str, Any] = {}
    artifact_summary.update(_latest_json_checks(issues, CORE_LATEST_JSON, max_age_days=max_age_days))
    artifact_summary.update(_latest_json_checks(
        issues, IPO_LATEST_JSON, max_age_days=max(max_age_days, 2), level="WARN"
    ))
    artifact_summary.update(_latest_json_checks(
        issues, EVENT_CALENDAR_JSON, max_age_days=max(max_age_days, 2), level="WARN"
    ))
    if watchlist_markets["hk"]:
        artifact_summary.update(_latest_json_checks(issues, HK_LATEST_JSON, max_age_days=max_age_days))
    if _a_share_ready(now) and (a_weights_ok or not allow_a_share_disabled):
        artifact_summary.update(_latest_json_checks(issues, CN_LATEST_JSON, max_age_days=max_age_days))
    summary["artifacts"] = artifact_summary

    qgate, _ = _json_load("data/latest/recommendation_quality_gate.json")
    if qgate and "_error" not in qgate:
        qstatus = str(qgate.get("status") or "UNKNOWN")
        summary["quality_gate_status"] = qstatus
        if qstatus == "FAIL":
            issues.append(_issue("FAIL", "quality_gate_fail", "recommendation_quality_gate 当前为 FAIL"))
        elif qstatus == "WARN":
            issues.append(_issue("WARN", "quality_gate_warn", "recommendation_quality_gate 当前为 WARN"))

    plan_a, plan_path = _json_load("data/latest/plan_a_v5.json")
    plan_v6, _ = _json_load("data/latest/plan_v6.json")
    pipeline_status = _check_pipeline_status(issues=issues, summary=summary, now=now)
    if plan_a and "_error" not in plan_a:
        method = str(plan_a.get("method") or "")
        engine = str((plan_a.get("risk_aware") or {}).get("engine") or "")
        summary["plan_method"] = method
        summary["plan_engine"] = engine
        if "risk_aware_optimize" not in method and engine != "risk_aware_optimize":
            issues.append(_issue("FAIL", "us_plan_not_risk_aware", "plan_a_v5.json 不是 risk-aware optimizer 产物"))
        if _is_fallback_plan(plan_a):
            issues.append(_issue(
                "WARN",
                "us_plan_risk_aware_fallback",
                "plan_a_v5.json 标记为 fallback/legacy 优化输出；客户可见报告必须标注仓位来源",
                {"engine": engine, "method": method},
            ))
        # 2026-05-21 V1 cutover：原 V1 picks v6_us buy_set 校验已删；V2 buy 在 v2_summary 已覆盖
    if plan_a and plan_v6 and "_error" not in plan_a and "_error" not in plan_v6:
        if _extract_tickers(plan_a) != _extract_tickers(plan_v6):
            issues.append(_issue("FAIL", "plan_a_v5_plan_v6_diverge", "plan_a_v5.json 与 plan_v6.json 标的集合不一致"))

    # 现金比例闸门（对称于上方 surface_artifact_checks 第一份）
    if plan_a and "_error" not in plan_a:
        cons2 = plan_a.get("constraints") if isinstance(plan_a.get("constraints"), dict) else {}
        cash_eff2 = cons2.get("cash_pct_effective")
        cash_target2 = cons2.get("cash_pct")
        if isinstance(cash_eff2, (int, float)):
            summary["cash_pct_effective"] = float(cash_eff2)
            summary["cash_pct_target"] = float(cash_target2) if isinstance(cash_target2, (int, float)) else None
            brief2 = REPO / "morning_brief.md"
            brief_text2 = brief2.read_text(encoding="utf-8") if brief2.exists() else ""
            cash_explained2 = "组合现金" in brief_text2 and "约束器" in brief_text2
            details2 = {
                "cash_pct_effective": round(float(cash_eff2), 4),
                "cash_pct_target": float(cash_target2) if isinstance(cash_target2, (int, float)) else None,
                "explained_in_brief": cash_explained2,
            }
            if float(cash_eff2) > 0.70:
                issues.append(_issue("FAIL", "plan_cash_extreme",
                    f"组合现金 {float(cash_eff2)*100:.1f}% > 70%（接近全空），检查约束/优化器", details2))
            elif float(cash_eff2) > 0.50:
                issues.append(_issue("WARN", "plan_cash_high",
                    f"组合现金 {float(cash_eff2)*100:.1f}% > 50%（已配≤一半），约束器抑制过强", details2))
            elif float(cash_eff2) > 0.30 and not cash_explained2:
                issues.append(_issue("WARN", "plan_cash_unexplained",
                    f"组合现金 {float(cash_eff2)*100:.1f}% > 30% 但 morning_brief 未给约束器解释", details2))
            elif float(cash_eff2) > 0.30:
                issues.append(_issue("WARN", "plan_cash_high_disclosed",
                    f"组合现金 {float(cash_eff2)*100:.1f}% > 30%；虽已披露原因，但仍不应视为满额可执行组合", details2))

    for rel in ("data/latest/trade_delta.json", "data/latest/trade_delta_hk.json", "data/latest/trade_delta_cn.json"):
        if rel == "data/latest/trade_delta_hk.json" and not watchlist_markets["hk"]:
            continue
        payload, _ = _json_load(rel)
        if payload and "_error" not in payload and payload.get("trade_blocked") is True:
            if payload.get("disabled") is True and payload.get("market") == "cn" and allow_a_share_disabled:
                issues.append(_issue("INFO", "a_share_trade_delta_disabled", f"{rel} 已按 A 股 disabled 输出只读状态"))
            else:
                issues.append(_issue("FAIL", "trade_delta_blocked", f"{rel} 当前 trade_blocked=true", payload.get("block_reason")))

    rec_evidence, _ = _json_load("data/latest/recommendation_evidence.json")
    evidence_dt = _payload_dt(rec_evidence, REPO / "data/latest/recommendation_evidence.json") if rec_evidence else None
    ref_payloads = [p for p in (plan_a, qgate, pipeline_status) if p and "_error" not in p]
    ref_dates = [_payload_dt(p, REPO) for p in ref_payloads]
    ref_dates = [d for d in ref_dates if d is not None]
    latest_ref = max(ref_dates) if ref_dates else None
    if rec_evidence and "_error" not in rec_evidence:
        summary["recommendation_evidence_generated_at"] = evidence_dt.isoformat(timespec="seconds") if evidence_dt else None
        if latest_ref and evidence_dt and evidence_dt < latest_ref - timedelta(minutes=5):
            issues.append(_issue(
                "WARN",
                "recommendation_evidence_older_than_current_run",
                "recommendation_evidence.json 早于当前 plan/qgate/pipeline，morning_brief 不能把它当成本轮推荐证据",
                {
                    "evidence_at": evidence_dt.isoformat(timespec="seconds"),
                    "latest_reference_at": latest_ref.isoformat(timespec="seconds"),
                },
            ))
    discovery, _ = _json_load("data/discovery_candidates.json")
    if discovery and "_error" not in discovery:
        n_cands = len(discovery.get("candidates") or [])
        universe_size = int(discovery.get("universe_size") or 0)
        summary["discovery_candidates"] = {
            "generated_at": discovery.get("generated_at"),
            "universe_size": universe_size,
            "candidate_count": n_cands,
        }
        if universe_size <= 0 or n_cands <= 0:
            issues.append(_issue(
                "FAIL",
                "discovery_candidates_empty",
                "全池 AI 推荐产物为空，AI 助手核心链路不可用",
                summary["discovery_candidates"],
            ))

    dashboard = REPO / "stock_dashboard.html"
    brief = REPO / "morning_brief.md"
    for path, code, label in (
        (dashboard, "dashboard_missing", "stock_dashboard.html"),
        (brief, "morning_brief_missing", "morning_brief.md"),
    ):
        if not path.exists():
            issues.append(_issue("FAIL", code, f"{label} 不存在；完整链路未生成最终阅读产物"))
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        summary[f"{label}_mtime"] = mtime.isoformat(timespec="seconds")
        if (now.date() - mtime.date()).days > max_age_days:
            issues.append(_issue("FAIL", f"{code}_stale", f"{label} 已滞后超过 {max_age_days} 天"))
    if dashboard.exists() and plan_path.exists():
        if dashboard.stat().st_mtime + 1 < plan_path.stat().st_mtime:
            issues.append(_issue("WARN", "dashboard_older_than_plan", "dashboard 比最新 plan 更旧，需要重建 HTML"))

    _check_api_freshness(now, summary, issues)
    return _build_payload(now, summary, issues)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-age-days", type=int, default=1)
    parser.add_argument(
        "--allow-a-share-disabled",
        action="store_true",
        help="A 股校准缺失时降级为 WARN；适合只验收美股/港股早盘链路。",
    )
    args = parser.parse_args()

    payload = run_check(
        max_age_days=args.max_age_days,
        allow_a_share_disabled=args.allow_a_share_disabled,
    )
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(f"Production acceptance: {payload['status']}")
    s = payload["summary"]
    print(f"  fail={s['fail']} warn={s['warn']} info={s['info']}")
    print(f"  latest={s.get('latest_pick_dates') or s.get('latest_recommendation_run_id')}")
    print(f"  signals={s.get('latest_signal_counts')}")
    if payload["issues"]:
        for item in payload["issues"][:16]:
            print(f"  [{item['level']}] {item['code']}: {item['message']}")
        if len(payload["issues"]) > 16:
            print(f"  ... {len(payload['issues']) - 16} more")
    print(f"  JSON: {OUT_PATH}")
    return 1 if payload["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
