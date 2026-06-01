"""Recommendation quality gate.

This is a local, no-network guardrail for the daily stock-advice system. It
does not try to prove the picks will be profitable. It verifies that the system
is using the right production pipelines, fresh-enough data, entry prices, and
review fields before the dashboard/brief presents recommendations confidently.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_research import config  # noqa: E402
from stock_db import get_db  # noqa: E402

A_SHARE_ENABLED = config.A_SHARE_PRODUCTION_ENABLED


def _as_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _json_generated_date(path: Path) -> date | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return _as_date(payload.get("generated_at"))
    except Exception:
        return None
    return None


def _a_share_ready(now: datetime) -> bool:
    # A-share event data is reliable after 16:00 China time, or on weekends.
    return now.isoweekday() >= 6 or now.hour >= 16


def _issue(level: str, code: str, message: str, details: Any = None) -> dict:
    item = {"level": level, "code": code, "message": message}
    if details is not None:
        item["details"] = details
    return item


def _table_names(conn) -> set[str]:
    try:
        return {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
    except Exception:
        return set()


def _run_v2_checks(conn, now: datetime) -> dict:
    """Validate the clean v2 production line.

    v2 recommendations live in recommendation_runs/recommendation_picks and are
    independent of legacy v6_us/v6_hk/v6_cn picks.  The quality gate should
    answer whether today's AI Assistant production path is usable, not whether
    legacy self-watchlist artifacts exist.
    """
    issues: list[dict] = []
    tables = _table_names(conn)

    required = {
        "manual_watchlist", "holdings", "system_universe", "pool_membership",
        "price_daily", "recommendation_runs", "recommendation_picks",
        "portfolio_plans",
    }
    missing = sorted(required - tables)
    if missing:
        issues.append(_issue("FAIL", "v2_schema_missing_tables", "v2 推荐链路缺少核心表", missing))

    def count(table: str) -> int:
        if table not in tables:
            return 0
        if table == "system_universe":
            return int(conn.execute("SELECT COUNT(*) FROM system_universe WHERE active = TRUE").fetchone()[0])
        if table == "pool_membership":
            return int(conn.execute("SELECT COUNT(*) FROM pool_membership WHERE active = TRUE").fetchone()[0])
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    summary: dict[str, Any] = {
        "schema_mode": "v2",
        "manual_watchlist_count": count("manual_watchlist"),
        "holdings_count": count("holdings"),
        "system_universe_count": count("system_universe"),
        "pool_membership_count": count("pool_membership"),
        "price_daily_count": count("price_daily"),
        "recommendation_runs_count": count("recommendation_runs"),
        "recommendation_picks_count": count("recommendation_picks"),
        "portfolio_plans_count": count("portfolio_plans"),
    }

    if summary["manual_watchlist_count"] == 0:
        issues.append(_issue(
            "INFO",
            "manual_watchlist_empty",
            "manual_watchlist 为空：用户尚未重新添加自选股，这是合法状态",
        ))
    if summary["holdings_count"] == 0:
        issues.append(_issue(
            "INFO",
            "holdings_empty",
            "holdings 为空：用户尚未确认真实持仓，这是合法状态",
        ))

    if "system_universe" in tables and summary["system_universe_count"] <= 0:
        issues.append(_issue("FAIL", "v2_system_universe_empty", "v2 system_universe 为空"))
    if "pool_membership" in tables and summary["pool_membership_count"] <= 0:
        issues.append(_issue("FAIL", "v2_pool_membership_empty", "v2 pool_membership 为空"))
    if "price_daily" in tables and summary["price_daily_count"] <= 0:
        issues.append(_issue("FAIL", "v2_price_daily_empty", "v2 price_daily 为空，AI 推荐没有行情输入"))

    if {"price_daily", "pool_membership"}.issubset(tables):
        active_pool_count = int(conn.execute(
            """
            SELECT COUNT(*)
            FROM pool_membership
            WHERE active = TRUE AND pool_type = 'system_tech_universe'
            """
        ).fetchone()[0])
        priced_count = int(conn.execute(
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
            )
            """
        ).fetchone()[0])
        coverage = (priced_count / active_pool_count) if active_pool_count else 0.0
        summary["v2_price_coverage"] = {
            "priced_count": priced_count,
            "active_pool_count": active_pool_count,
            "coverage_pct": round(coverage * 100, 2),
        }
        if active_pool_count <= 0:
            issues.append(_issue("FAIL", "v2_no_active_system_pool", "v2 没有 active system_tech_universe 池成员"))
        elif coverage < 0.50:
            issues.append(_issue("FAIL", "v2_price_coverage_too_low", f"v2 行情覆盖率过低：{priced_count}/{active_pool_count}"))
        elif coverage < 0.80:
            issues.append(_issue("WARN", "v2_price_coverage_low", f"v2 行情覆盖率偏低：{priced_count}/{active_pool_count}"))

    latest: dict[str, Any] = {"run_id": None, "generated_at": None}
    signal_counts: dict[str, int] = {}
    if {"recommendation_runs", "recommendation_picks"}.issubset(tables):
        latest_row = conn.execute(
            """
            SELECT run_id, generated_at, strategy_version, model_version, universe_scope
            FROM recommendation_runs
            ORDER BY generated_at DESC
            LIMIT 1
            """
        ).fetchone()
        if not latest_row:
            issues.append(_issue("FAIL", "v2_no_recommendation_runs", "v2 没有 recommendation_runs"))
        else:
            latest = {
                "run_id": latest_row[0],
                "generated_at": str(latest_row[1]) if latest_row[1] is not None else None,
                "strategy_version": latest_row[2],
                "model_version": latest_row[3],
                "universe_scope": latest_row[4],
            }
            if latest_row[4] != "system_tech_universe":
                issues.append(_issue("FAIL", "v2_latest_run_scope_invalid", "v2 最新推荐 run 不是 system_tech_universe", latest))
            rows = conn.execute(
                """
                SELECT signal, COUNT(*)
                FROM recommendation_picks
                WHERE run_id = ?
                GROUP BY signal
                """,
                [latest_row[0]],
            ).fetchall()
            signal_counts = {str(signal): int(n) for signal, n in rows}
            if signal_counts.get("buy", 0) <= 0:
                issues.append(_issue("FAIL", "v2_latest_run_no_buy", "v2 最新推荐批次没有 buy 推荐"))

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
                [latest_row[0]],
            ).fetchall()
            if bad_scope:
                issues.append(_issue(
                    "FAIL",
                    "v2_pick_scope_origin_invalid",
                    "v2 最新系统池推荐必须标记 system_tech_universe/system_pool",
                    [{"market": r[0], "symbol": r[1], "universe_scope": r[2], "source_origin": r[3]} for r in bad_scope],
                ))

        if "pool_membership" in tables and latest.get("run_id"):
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
                [latest["run_id"]],
            ).fetchall()
            if orphan:
                issues.append(_issue(
                    "FAIL",
                    "v2_pick_not_in_system_pool",
                    "v2 系统推荐出现未属于 pool_membership/system_tech_universe 的股票",
                    [{"market": r[0], "symbol": r[1]} for r in orphan],
                ))

    if "portfolio_plans" in tables and summary["portfolio_plans_count"] <= 0:
        issues.append(_issue("FAIL", "v2_no_portfolio_plans", "v2 还没有生成 AI 组合方案"))

    if {"pick_outcomes", "strategy_review_reports"}.intersection(tables):
        # 按当前(最新)策略口径数,不数全历史 —— 否则旧公式攒下的上千行 outcomes 会让
        # 当前新策略 0 成熟样本时看起来"已有验证证据",status 假性 PASS。
        # 与 evidence gate / strategy_validation 的 latest-strategy 口径保持一致。
        cur_strategy = latest.get("strategy_version")
        if cur_strategy and "recommendation_runs" in tables:
            outcomes = int(conn.execute(
                """
                SELECT COUNT(*)
                FROM pick_outcomes o
                JOIN recommendation_runs r ON r.run_id = o.run_id
                WHERE r.strategy_version = ?
                """,
                [cur_strategy],
            ).fetchone()[0])
            reports = int(conn.execute(
                "SELECT COUNT(*) FROM strategy_review_reports WHERE strategy_version = ?",
                [cur_strategy],
            ).fetchone()[0]) if "strategy_review_reports" in tables else 0
        else:
            # 拿不到当前策略版本时退回全历史(至少不漏 INFO)
            outcomes = count("pick_outcomes")
            reports = count("strategy_review_reports")
        summary["pick_outcomes_count"] = outcomes
        summary["strategy_review_reports_count"] = reports
        summary["strategy_evidence_scope"] = cur_strategy or "all_history"
        if outcomes <= 0 and reports <= 0:
            issues.append(_issue(
                "INFO",
                "v2_strategy_evidence_not_mature",
                f"当前策略 {cur_strategy or 'all'} 的验证样本仍在积累"
                f"(成熟 outcomes={outcomes} / reports={reports})；"
                "这不阻断今日推荐生成，但不能证明长期有效",
            ))

    n_fail = sum(1 for x in issues if x["level"] == "FAIL")
    n_warn = sum(1 for x in issues if x["level"] == "WARN")
    return {
        "generated_at": now.isoformat(),
        "status": "FAIL" if n_fail else ("WARN" if n_warn else "PASS"),
        "summary": {
            "fail": n_fail,
            "warn": n_warn,
            "info": sum(1 for x in issues if x["level"] == "INFO"),
            **summary,
            "latest_recommendation_run": latest,
            "latest_signal_counts": {"system_tech_universe": signal_counts},
            "required_production_sources": ["system_tech_universe"],
        },
        "issues": issues,
    }


def _run_checks() -> dict:
    """V2 路径：只检查 system_universe / recommendation_runs / recommendation_picks。
    2026-05-21 V1 cutover：legacy v6_us/hk/cn picks 检查全删（约 200 行 V1 SQL）。
    """
    now = datetime.now()
    # 只读验收:本工具纯读库,用 force_read_only 真正以 DuckDB 只读模式打开,
    # 避免与 nightly 写连接抢锁(2026-06-01 复现过并发锁失败)。这是独立 CLI 非 API 进程,可安全只读。
    conn = get_db(force_read_only=True)
    try:
        return _run_v2_checks(conn, now)
    finally:
        conn.close()


def main() -> int:
    payload = _run_checks()
    out = REPO / "data" / "latest" / "recommendation_quality_gate.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(f"Recommendation quality gate: {payload['status']}")
    summary = payload["summary"]
    print(f"  fail={summary['fail']} warn={summary['warn']} info={summary['info']}")
    if summary.get("schema_mode") == "v2":
        print(f"  latest_run={summary.get('latest_recommendation_run')}")
        print(f"  signals={summary.get('latest_signal_counts')}")
    else:
        print(f"  latest={summary.get('latest_pick_dates')}")
    if payload["issues"]:
        for item in payload["issues"][:12]:
            print(f"  [{item['level']}] {item['code']}: {item['message']}")
        if len(payload["issues"]) > 12:
            print(f"  ... {len(payload['issues']) - 12} more")
    print(f"  JSON: {out}")
    return 1 if payload["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
