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
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import fetch_picks_view, get_db  # noqa: E402

PRODUCTION_SOURCES = ("v6_us", "v6_hk", "v6_cn")


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


def _run_checks() -> dict:
    now = datetime.now()
    today = now.date()
    conn = get_db()
    issues: list[dict] = []
    ph = ",".join(["?"] * len(PRODUCTION_SOURCES))

    latest_rows = conn.execute(
        f"""
        SELECT model_source, MAX(pick_date) AS latest_date, COUNT(*) AS n_rows
        FROM picks
        WHERE model_source IN ({ph})
        GROUP BY model_source
        ORDER BY model_source
        """,
        list(PRODUCTION_SOURCES),
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

    for src in PRODUCTION_SOURCES:
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
        list(PRODUCTION_SOURCES),
    ).fetchall()
    if missing_price:
        issues.append(_issue(
            "FAIL",
            "production_entry_price_missing",
            "生产 picks 存在缺失入选价/币种，无法做真实收益回顾",
            [{"date": str(r[0]), "source": r[1], "code": r[2], "name": r[3]} for r in missing_price[:30]],
        ))

    mislabeled = conn.execute("""
        SELECT pick_date, model_source, code, name, rating
        FROM picks
        WHERE (
            model_source = 'v6_us'
            AND (rating IS NULL OR (rating NOT LIKE '%z %' AND rating NOT LIKE '%不建议%'))
        ) OR (
            model_source IN ('v6_hk', 'v6_cn')
            AND (rating IS NULL OR (rating NOT LIKE '%综合%' AND rating NOT LIKE '%关注%'))
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
        list(PRODUCTION_SOURCES),
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
        list(PRODUCTION_SOURCES),
    ).fetchone()[0]
    review_coverage = (reviewed / mature_total) if mature_total else None
    if mature_total and review_coverage is not None and review_coverage < 0.5:
        issues.append(_issue(
            "WARN",
            "review_alpha_low_coverage",
            f"成熟 v6 picks 的 alpha 回顾覆盖率偏低：{reviewed}/{mature_total}",
        ))

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
        if (missing_conclusion or 0) > 0 or (missing_risks or 0) > 0:
            issues.append(_issue(
                "WARN",
                "watchlist_research_gaps",
                f"watchlist 仍有研究字段缺口：conclusion {missing_conclusion or 0}/{n}, risks {missing_risks or 0}/{n}",
            ))
        if (missing_chain or 0) > 0:
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
    print(f"  latest={summary['latest_pick_dates']}")
    if payload["issues"]:
        for item in payload["issues"][:12]:
            print(f"  [{item['level']}] {item['code']}: {item['message']}")
        if len(payload["issues"]) > 12:
            print(f"  ... {len(payload['issues']) - 12} more")
    print(f"  JSON: {out}")
    return 1 if payload["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
