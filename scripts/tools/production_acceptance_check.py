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
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from stock_research import config  # noqa: E402

DB_PATH = Path(config.DUCKDB_PATH)
OUT_PATH = REPO / "data" / "latest" / "production_acceptance_check.json"

PRODUCTION_SOURCES = ("v6_us", "v6_hk", "v6_cn")
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
    "data/latest/source_health.json",
    "data/latest/trade_delta.json",
    "data/latest/risk_metrics.json",
)

HK_LATEST_JSON = (
    "data/latest/hk_picks.json",
    "data/latest/trade_delta_hk.json",
)

CN_LATEST_JSON = (
    "data/a_share_picks.json",
    "data/latest/trade_delta_cn.json",
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
        for key in ("generated_at", "as_of", "date", "timestamp"):
            dt = _parse_dt(payload.get(key))
            if dt:
                return dt
    if path.exists():
        return datetime.fromtimestamp(path.stat().st_mtime)
    return None


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
    flags = {"us": False, "hk": False}
    if conn is None:
        return flags
    try:
        rows = conn.execute("SELECT code, COALESCE(market, '') FROM watchlist").fetchall()
    except Exception:
        return flags
    for code, market in rows:
        c = str(code or "").upper()
        m = str(market or "")
        if c.endswith(".HK") or "港股" in m:
            flags["hk"] = True
        elif c.replace("-", "").replace(".", "").isalpha() or "美股" in m:
            flags["us"] = True
    return flags


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
                if ticker:
                    out.add(str(ticker).upper())
    return out


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
        conn = duckdb.connect(str(DB_PATH), read_only=True)

    db_tables: set[str] = set()
    if conn is not None:
        try:
            db_tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
        except Exception as e:
            issues.append(_issue("FAIL", "duckdb_table_list_failed", "无法读取 DuckDB 表清单", str(e)))
    v2_required_tables = {
        "manual_watchlist", "holdings", "system_universe", "pool_membership",
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
            for table in ("manual_watchlist", "holdings", "system_universe", "pool_membership",
                          "price_daily", "recommendation_runs", "recommendation_picks", "portfolio_plans"):
                if table in db_tables:
                    try:
                        summary[f"{table}_count"] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    except Exception as e:
                        issues.append(_issue("FAIL", f"{table}_count_failed", f"无法读取 {table} 行数", str(e)))
            if "system_universe" in db_tables and summary.get("system_universe_count", 0) <= 0:
                issues.append(_issue("FAIL", "v2_system_universe_empty", "v2 system_universe 为空"))
            if "pool_membership" in db_tables and summary.get("pool_membership_count", 0) <= 0:
                issues.append(_issue("FAIL", "v2_pool_membership_empty", "v2 pool_membership 为空"))
            if "price_daily" in db_tables and summary.get("price_daily_count", 0) <= 0:
                issues.append(_issue("FAIL", "v2_price_daily_empty", "v2 还没有从新数据源拉取行情"))
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
                bad_scope = conn.execute(
                    """
                    SELECT market, symbol, universe_scope, source_origin
                    FROM recommendation_picks
                    WHERE universe_scope <> 'system_tech_universe'
                       OR source_origin <> 'system_pool'
                    LIMIT 30
                    """
                ).fetchall()
                if bad_scope:
                    issues.append(_issue(
                        "FAIL",
                        "v2_pick_scope_origin_invalid",
                        "v2 系统池推荐必须标记 system_tech_universe/system_pool",
                        [{"market": r[0], "symbol": r[1], "universe_scope": r[2], "source_origin": r[3]} for r in bad_scope],
                    ))
                orphan = conn.execute(
                    """
                    SELECT p.market, p.symbol
                    FROM recommendation_picks p
                    LEFT JOIN pool_membership m
                      ON m.market = p.market AND m.symbol = p.symbol AND m.active = TRUE
                    WHERE m.symbol IS NULL
                    LIMIT 30
                    """
                ).fetchall()
                if orphan:
                    issues.append(_issue(
                        "FAIL",
                        "v2_pick_not_in_system_pool",
                        "v2 系统推荐出现未属于 pool_membership 的股票",
                        [{"market": r[0], "symbol": r[1]} for r in orphan],
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
        return _build_payload(now, summary, issues)

    watchlist_markets = _watchlist_market_flags(conn)
    required_sources: list[str] = []
    if watchlist_markets["us"]:
        required_sources.append("v6_us")
    if watchlist_markets["hk"]:
        required_sources.append("v6_hk")
    if config.A_SHARE_PRODUCTION_ENABLED:
        required_sources.append("v6_cn")
    summary["watchlist_market_flags"] = watchlist_markets
    summary["required_production_sources"] = required_sources

    latest_by_source: dict[str, str | None] = {}
    latest_buy_sets: dict[str, set[str]] = {}
    if conn is not None and "picks" in db_tables:
        try:
            try:
                summary["watchlist_count"] = int(conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0])
            except Exception:
                summary["watchlist_count"] = None
            cols = {r[1] for r in conn.execute("PRAGMA table_info('picks')").fetchall()}
            missing_cols = sorted(REQUIRED_PICK_COLUMNS - cols)
            summary["picks_schema_columns"] = sorted(cols)
            if missing_cols:
                issues.append(_issue("FAIL", "picks_schema_missing_columns", "picks 表缺少生产分流字段", missing_cols))
            signal_expr = (
                "COALESCE(signal, "
                "CASE "
                "WHEN rating LIKE '%⛔%' OR rating LIKE '%不建议%' THEN 'avoid' "
                "WHEN rating LIKE '%观察%' THEN 'watch' "
                "WHEN rating IS NULL OR TRIM(rating) = '' THEN 'watch' "
                "ELSE 'buy' END)"
                if "signal" in cols
                else
                "CASE "
                "WHEN rating LIKE '%⛔%' OR rating LIKE '%不建议%' THEN 'avoid' "
                "WHEN rating LIKE '%观察%' THEN 'watch' "
                "WHEN rating IS NULL OR TRIM(rating) = '' THEN 'watch' "
                "ELSE 'buy' END"
            )

            if "signal" in cols:
                bad_signals = conn.execute(
                    """
                    SELECT signal, COUNT(*)
                    FROM picks
                    WHERE signal IS NOT NULL AND lower(signal) NOT IN ('buy', 'avoid', 'watch')
                    GROUP BY signal
                    """
                ).fetchall()
                if bad_signals:
                    issues.append(_issue("FAIL", "invalid_pick_signal", "picks.signal 存在非法值", dict(bad_signals)))

            latest_rows = conn.execute(
                """
                SELECT model_source, MAX(pick_date) AS latest_date
                FROM picks
                WHERE model_source IN ('v6_us', 'v6_hk', 'v6_cn')
                GROUP BY model_source
                """
            ).fetchall()
            latest_by_source = {src: str(d)[:10] if d else None for src, d in latest_rows}
            summary["latest_pick_dates"] = latest_by_source

            for src in required_sources:
                latest = latest_by_source.get(src)
                if not latest:
                    if src == "v6_cn" and (not _a_share_ready(now) or allow_a_share_disabled):
                        issues.append(_issue("WARN", "v6_cn_missing_not_blocking", "A 股暂无 v6_cn picks；当前验收允许 A 股未启用"))
                    else:
                        issues.append(_issue("FAIL", f"{src}_missing", f"{src} 没有最新生产 picks"))
                    signal_counts[src] = {}
                    latest_buy_sets[src] = set()
                    continue

                rows = conn.execute(
                    f"""
                    SELECT
                      {signal_expr} AS sig,
                      COUNT(*) AS n
                    FROM picks
                    WHERE model_source = ? AND pick_date = ?
                    GROUP BY sig
                    """,
                    [src, latest],
                ).fetchall()
                signal_counts[src] = {str(sig): int(n) for sig, n in rows}
                buy_rows = conn.execute(
                    f"""
                    SELECT code
                    FROM picks
                    WHERE model_source = ? AND pick_date = ?
                      AND {signal_expr} = 'buy'
                    """,
                    [src, latest],
                ).fetchall()
                latest_buy_sets[src] = {str(r[0]).upper() for r in buy_rows}
                if signal_counts[src].get("buy", 0) <= 0 and (src != "v6_cn" or _a_share_ready(now)):
                    level = "WARN" if src == "v6_cn" and allow_a_share_disabled else "FAIL"
                    issues.append(_issue(level, f"{src}_no_buy_picks", f"{src} 最新批次没有 signal='buy' 推荐"))

                if "coverage_score" in cols:
                    low_coverage = conn.execute(
                        f"""
                        SELECT code, coverage_score, missing_factors
                        FROM picks
                        WHERE model_source = ? AND pick_date = ?
                          AND {signal_expr} = 'buy'
                          AND (coverage_score IS NULL OR coverage_score < 0.50)
                        ORDER BY code
                        LIMIT 30
                        """,
                        [src, latest],
                    ).fetchall()
                    if low_coverage:
                        issues.append(_issue(
                            "FAIL",
                            f"{src}_buy_low_coverage",
                            f"{src} 最新 buy picks 存在 coverage_score 缺失或低于 50%",
                            [{"code": r[0], "coverage_score": r[1], "missing_factors": r[2]} for r in low_coverage],
                        ))
        finally:
            conn.close()
            conn = None
    elif conn is not None and not is_v2_schema:
        issues.append(_issue("FAIL", "missing_picks_table", "DuckDB 缺少 legacy picks 表"))
        conn.close()
        conn = None
    elif conn is not None:
        conn.close()
        conn = None
    summary["latest_signal_counts"] = signal_counts

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
    if plan_a and "_error" not in plan_a:
        method = str(plan_a.get("method") or "")
        engine = str((plan_a.get("risk_aware") or {}).get("engine") or "")
        summary["plan_method"] = method
        summary["plan_engine"] = engine
        if "risk_aware_optimize" not in method and engine != "risk_aware_optimize":
            issues.append(_issue("FAIL", "us_plan_not_risk_aware", "plan_a_v5.json 不是 risk-aware optimizer 产物"))
        plan_tickers = _extract_tickers(plan_a)
        non_buy = sorted(plan_tickers - latest_buy_sets.get("v6_us", set()))
        if non_buy and latest_buy_sets.get("v6_us"):
            issues.append(_issue("FAIL", "us_plan_contains_non_buy", "美股 plan 含非最新 buy picks 标的", non_buy[:30]))
    if plan_a and plan_v6 and "_error" not in plan_a and "_error" not in plan_v6:
        if _extract_tickers(plan_a) != _extract_tickers(plan_v6):
            issues.append(_issue("FAIL", "plan_a_v5_plan_v6_diverge", "plan_a_v5.json 与 plan_v6.json 标的集合不一致"))

    for rel in ("data/latest/trade_delta.json", "data/latest/trade_delta_hk.json", "data/latest/trade_delta_cn.json"):
        if rel == "data/latest/trade_delta_hk.json" and not watchlist_markets["hk"]:
            continue
        payload, _ = _json_load(rel)
        if payload and "_error" not in payload and payload.get("trade_blocked") is True:
            if payload.get("disabled") is True and payload.get("market") == "cn" and allow_a_share_disabled:
                issues.append(_issue("INFO", "a_share_trade_delta_disabled", f"{rel} 已按 A 股 disabled 输出只读状态"))
            else:
                issues.append(_issue("FAIL", "trade_delta_blocked", f"{rel} 当前 trade_blocked=true", payload.get("block_reason")))

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
    print(f"  latest={s.get('latest_pick_dates')}")
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
