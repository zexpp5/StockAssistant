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
from stock_db import fetch_picks_view, get_db  # noqa: E402

A_SHARE_ENABLED = config.A_SHARE_PRODUCTION_ENABLED


def _watchlist_required_sources(conn) -> tuple[str, ...]:
    """Self AI picks are required only for markets present in manual watchlist."""
    rows = conn.execute("SELECT code, COALESCE(market, '') FROM watchlist").fetchall()
    has_us = False
    has_hk = False
    for code, market in rows:
        c = str(code or "").upper()
        m = str(market or "")
        if c.endswith(".HK") or "港股" in m:
            has_hk = True
        elif c.replace("-", "").replace(".", "").isalpha() or "美股" in m:
            has_us = True
    sources: list[str] = []
    if has_us:
        sources.append("v6_us")
    if has_hk:
        sources.append("v6_hk")
    if A_SHARE_ENABLED:
        sources.append("v6_cn")
    return tuple(sources)


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
        outcomes = count("pick_outcomes")
        reports = count("strategy_review_reports")
        summary["pick_outcomes_count"] = outcomes
        summary["strategy_review_reports_count"] = reports
        if outcomes <= 0 and reports <= 0:
            issues.append(_issue(
                "INFO",
                "v2_strategy_evidence_not_mature",
                "策略验证样本仍在积累；这不阻断今日推荐生成，但不能证明长期有效",
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
    now = datetime.now()
    today = now.date()
    conn = get_db()
    issues: list[dict] = []
    tables = _table_names(conn)
    if {"system_universe", "recommendation_runs", "recommendation_picks"}.issubset(tables):
        try:
            payload = _run_v2_checks(conn, now)
        finally:
            conn.close()
        return payload

    production_sources = _watchlist_required_sources(conn)
    if not production_sources:
        production_sources = ("v6_cn",) if A_SHARE_ENABLED else tuple()
    ph = ",".join(["?"] * len(production_sources))

    latest_rows = conn.execute(
        f"""
        SELECT model_source, MAX(pick_date) AS latest_date, COUNT(*) AS n_rows
        FROM picks
        WHERE model_source IN ({ph})
        GROUP BY model_source
        ORDER BY model_source
        """,
        list(production_sources),
    ).fetchall()
    latest = {src: _as_date(d) for src, d, _n in latest_rows}
    latest_counts = {}
    # signal='buy' 才算"真生产推荐"。avoid/watch 是同一张 picks 表里的负向/中性档，
    # 用 rating 文本前缀分类不可靠（"⭐ 关注" 也是 buy，"⭐ 观察" 是 watch），改用结构化 signal。
    for src, d in latest.items():
        if d is None:
            latest_counts[src] = 0
            continue
        latest_counts[src] = conn.execute(
            "SELECT COUNT(*) FROM picks "
            "WHERE model_source = ? AND pick_date = ? AND COALESCE(signal, 'buy') = 'buy'",
            [src, d],
        ).fetchone()[0]

    for src in production_sources:
        d = latest.get(src)
        if d is None:
            level = "FAIL" if src in ("v6_us", "v6_hk") or _a_share_ready(now) else "WARN"
            issues.append(_issue(level, f"{src}_missing", f"{src} 没有生产 picks"))
            continue
        age = (today - d).days
        if src in ("v6_us", "v6_hk") and age > 3:
            issues.append(_issue("FAIL", f"{src}_stale", f"{src} 最新 pick_date={d}，已滞后 {age} 天"))
        elif src == "v6_cn":
            if _a_share_ready(now) and age > 3:
                issues.append(_issue("FAIL", "v6_cn_stale_after_close", f"A 股最新 pick_date={d}，收盘后仍滞后 {age} 天"))
            elif age > 1:
                issues.append(_issue("WARN", "v6_cn_stale", f"A 股最新 pick_date={d}，滞后 {age} 天；若今天未到 16:00 可能正常"))

    missing_price = conn.execute(
        f"""
        WITH latest AS (
            SELECT model_source, MAX(pick_date) AS pick_date
            FROM picks
            WHERE model_source IN ({ph})
            GROUP BY model_source
        )
        SELECT p.pick_date, p.model_source, p.code, p.name, p.entry_price, p.entry_currency
        FROM picks p
        INNER JOIN latest l
          ON l.model_source = p.model_source AND l.pick_date = p.pick_date
        WHERE COALESCE(p.signal, 'buy') = 'buy'
          AND (p.entry_price IS NULL OR p.entry_price <= 0 OR p.entry_currency IS NULL)
        ORDER BY p.model_source, p.code
        """,
        list(production_sources),
    ).fetchall()
    if missing_price:
        issues.append(_issue(
            "FAIL",
            "production_entry_price_missing",
            "生产 picks 存在缺失入选价/币种，无法做真实收益回顾",
            [{"date": str(r[0]), "source": r[1], "code": r[2], "name": r[3]} for r in missing_price[:30]],
        ))

    cn_rating_clause = """
            model_source IN ('v6_hk', 'v6_cn')
            AND (rating IS NULL OR (rating NOT LIKE '%综合%' AND rating NOT LIKE '%关注%'))
    """ if A_SHARE_ENABLED else """
            model_source = 'v6_hk'
            AND (rating IS NULL OR (rating NOT LIKE '%综合%' AND rating NOT LIKE '%关注%'))
    """
    mislabeled = conn.execute(f"""
        SELECT pick_date, model_source, code, name, rating
        FROM picks
        WHERE (
            model_source = 'v6_us'
            AND (rating IS NULL OR (rating NOT LIKE '%z %' AND rating NOT LIKE '%不建议%'))
        ) OR (
            {cn_rating_clause}
        )
        ORDER BY pick_date DESC, code
        LIMIT 30
    """).fetchall()
    if mislabeled:
        issues.append(_issue(
            "FAIL",
            "legacy_rating_mislabeled_as_v6",
            "有旧模型评级格式仍标成 v6，可能污染生产推荐",
            [{"date": str(r[0]), "source": r[1], "code": r[2], "rating": r[4]} for r in mislabeled],
        ))

    view_rows = fetch_picks_view(conn=conn)
    legacy_view = [r for r in view_rows if str(r.get("model_source") or "").startswith("legacy")]
    if legacy_view:
        issues.append(_issue(
            "FAIL",
            "legacy_in_dashboard_view",
            "dashboard 生产视图混入 legacy picks",
            [{"date": str(r.get("pick_date")), "code": r.get("code"), "source": r.get("model_source")} for r in legacy_view[:20]],
        ))

    mature_total = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM picks
        WHERE model_source IN ({ph})
          AND COALESCE(signal, 'buy') = 'buy'
          AND pick_date < CURRENT_DATE
          AND entry_price IS NOT NULL
        """,
        list(production_sources),
    ).fetchone()[0]
    reviewed = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM picks p
        WHERE p.model_source IN ({ph})
          AND COALESCE(p.signal, 'buy') = 'buy'
          AND p.pick_date < CURRENT_DATE
          AND p.entry_price IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM reviews r
              WHERE r.pick_date = p.pick_date
                AND r.code = p.code
                AND COALESCE(r.model_source, p.model_source) = p.model_source
                AND r.alpha_pct IS NOT NULL
          )
        """,
        list(production_sources),
    ).fetchone()[0]
    review_coverage = (reviewed / mature_total) if mature_total else None
    if mature_total and review_coverage is not None and review_coverage < 0.5:
        issues.append(_issue(
            "WARN",
            "review_alpha_low_coverage",
            f"成熟 v6 picks 的 alpha 回顾覆盖率偏低：{reviewed}/{mature_total}",
        ))

    if A_SHARE_ENABLED:
        cn_json_date = _json_generated_date(REPO / "data" / "a_share_picks.json")
        cn_db_date = latest.get("v6_cn")
        if cn_json_date and cn_db_date and cn_json_date < cn_db_date:
            issues.append(_issue(
                "WARN",
                "a_share_json_lags_db",
                f"data/a_share_picks.json={cn_json_date} 落后 DuckDB v6_cn={cn_db_date}",
            ))
        elif cn_json_date and cn_db_date and cn_json_date > cn_db_date:
            issues.append(_issue(
                "WARN",
                "a_share_db_lags_json",
                f"DuckDB v6_cn={cn_db_date} 落后 data/a_share_picks.json={cn_json_date}",
            ))

    watchlist_row = conn.execute("""
        SELECT
          COUNT(*) AS n,
          SUM(CASE WHEN conclusion IS NULL OR TRIM(conclusion) = '' THEN 1 ELSE 0 END) AS missing_conclusion,
          SUM(CASE WHEN risks IS NULL OR TRIM(risks) = '' THEN 1 ELSE 0 END) AS missing_risks,
          SUM(CASE WHEN chain IS NULL OR TRIM(chain) = '' THEN 1 ELSE 0 END) AS missing_chain
        FROM watchlist
    """).fetchone()
    if watchlist_row:
        n, missing_conclusion, missing_risks, missing_chain = watchlist_row
        if n == 0:
            issues.append(_issue(
                "INFO",
                "watchlist_empty",
                "watchlist 为空：用户尚未选择自选股，这是合法状态",
            ))
        elif (missing_conclusion or 0) > 0 or (missing_risks or 0) > 0:
            issues.append(_issue(
                "INFO",
                "watchlist_research_gaps",
                f"watchlist 研究字段缺口：conclusion {missing_conclusion or 0}/{n}, risks {missing_risks or 0}/{n}；自选股资料不作为生产阻断",
            ))
        if n > 0 and (missing_chain or 0) > 0:
            issues.append(_issue(
                "INFO",
                "watchlist_chain_gaps",
                f"watchlist 产业链字段缺口：chain {missing_chain or 0}/{n}",
            ))

    conn.close()
    n_fail = sum(1 for x in issues if x["level"] == "FAIL")
    n_warn = sum(1 for x in issues if x["level"] == "WARN")
    payload = {
        "generated_at": now.isoformat(),
        "status": "FAIL" if n_fail else ("WARN" if n_warn else "PASS"),
        "summary": {
            "fail": n_fail,
            "warn": n_warn,
            "info": sum(1 for x in issues if x["level"] == "INFO"),
            "production_view_rows": len(view_rows),
            "a_share_production_enabled": A_SHARE_ENABLED,
            "required_production_sources": list(production_sources),
            "latest_pick_dates": {k: str(v) if v else None for k, v in latest.items()},
            "latest_pick_counts": latest_counts,
            "mature_review_coverage": round(review_coverage, 4) if review_coverage is not None else None,
        },
        "issues": issues,
    }
    return payload


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
