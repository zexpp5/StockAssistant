"""AI 主题雷达 · 证据系统统一刷新入口（文档 §十.1）。

按顺序跑：
  1. 数据源健康检查（重测 16 个 source URL HEAD）
  2. ETF 持仓刷新（PoC：用预存 SNAPSHOTS；生产可接 fetcher）
  3. SEC / 公司证据扫描（uranium PoC）+ 自动 stale 降级
  4. 宏观指标刷新提示（manual 路径：列出过期 metric）
  5. 公司标签聚合（aggregate_theme_tags）
  6. 输出 data/latest/ai_theme_evidence_summary.json

设计原则:
  - 每个 step 独立 try/except — 一个失败不阻断后续
  - summary.json 是 audit trail，记录 step 状态 + 计数 + 错误
  - 默认不抓 SEC（除非传 --scan-sec），避免每次 refresh 都打 SEC API
  - 默认不重抓 ETF（用 ingest 已有快照），传 --refresh-etf 才重抓
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "scripts" / "tools"))

from stock_db import get_db  # noqa: E402  # type: ignore


SUMMARY_PATH = REPO / "data" / "latest" / "ai_theme_evidence_summary.json"


def _query_one(sql: str, params: list = None):
    """short-lived read connection — 避免持锁阻塞 subprocess。"""
    con = get_db()
    try:
        return con.execute(sql, params or []).fetchone()
    finally:
        con.close()


def _query_all(sql: str, params: list = None):
    con = get_db()
    try:
        return con.execute(sql, params or []).fetchall()
    finally:
        con.close()


def _step_seed_sources() -> dict:
    """Step 1: 跑 seed_ai_theme_sources（含 URL HEAD check + 写 last_check_status）"""
    try:
        r = subprocess.run(
            ["/opt/homebrew/bin/python3", str(REPO / "scripts" / "tools" / "seed_ai_theme_sources.py")],
            capture_output=True, text=True, timeout=120
        )
        rows = _query_all("SELECT last_check_status, COUNT(*) FROM ai_theme_evidence_sources GROUP BY last_check_status")
        stats = dict(rows)
        n_total = sum(stats.values())
        n_ok = stats.get("ok", 0)
        ok_pct = round(n_ok / max(n_total, 1) * 100, 1)
        # seed 脚本 exit 1 = 可达率 < 80%；这里看实际可达率判 status
        return {
            "step": "sources_health_check",
            "status": "ok" if ok_pct >= 80 else "degraded",
            "n_total": n_total,
            "n_ok": n_ok,
            "ok_pct": ok_pct,
            "by_status": stats,
            "subprocess_returncode": r.returncode,
        }
    except Exception as e:
        return {"step": "sources_health_check", "status": "error", "error": str(e)}


def _step_refresh_etf(refresh: bool) -> dict:
    """Step 2: ETF 持仓刷新（默认用已存快照；--refresh-etf 才重抓）"""
    try:
        if refresh:
            r = subprocess.run(
                ["/opt/homebrew/bin/python3", str(REPO / "scripts" / "tools" / "ingest_etf_holdings_snapshot.py")],
                capture_output=True, text=True, timeout=120
            )
            action = "re-ingested from snapshot"
            ok = r.returncode == 0
        else:
            action = "skip (use --refresh-etf to reingest)"
            ok = True
        n_etfs = _query_one("SELECT COUNT(*) FROM ai_theme_etf_universe WHERE active=TRUE")[0]
        n_holdings = _query_one("SELECT COUNT(*) FROM ai_theme_etf_holdings")[0]
        latest = _query_one("SELECT MAX(last_fetched_at) FROM ai_theme_etf_universe")[0]
        return {
            "step": "etf_holdings_refresh",
            "status": "ok" if ok else "error",
            "action": action,
            "n_etfs": n_etfs,
            "n_holdings": n_holdings,
            "latest_fetched_at": latest.isoformat() if hasattr(latest, "isoformat") else latest,
        }
    except Exception as e:
        return {"step": "etf_holdings_refresh", "status": "error", "error": str(e)}


def _step_sec_scan(scan: bool) -> dict:
    """Step 3: SEC 公司证据扫描（默认不跑，传 --scan-sec 才跑；包含 stale 标记）"""
    try:
        if scan:
            r = subprocess.run(
                ["/opt/homebrew/bin/python3", str(REPO / "scripts" / "tools" / "sec_edgar_evidence_scan.py")],
                capture_output=True, text=True, timeout=180
            )
            action = "ran scan + stale rule"
            ok = r.returncode == 0
        else:
            # 不扫但跑 stale 规则
            from sec_edgar_evidence_scan import mark_stale_evidence
            con = get_db()
            try:
                n_stale_now = mark_stale_evidence(con)
            finally:
                con.close()
            action = f"skip scan; stale rule run → {n_stale_now} stale"
            ok = True
        status_dist = dict(_query_all(
            "SELECT evidence_status, COUNT(*) FROM ai_theme_company_evidence GROUP BY evidence_status"
        ))
        return {
            "step": "sec_evidence_scan",
            "status": "ok" if ok else "error",
            "action": action,
            "n_evidence_total": sum(status_dist.values()),
            "by_status": status_dist,
        }
    except Exception as e:
        return {"step": "sec_evidence_scan", "status": "error", "error": str(e), "trace": traceback.format_exc()}


def _step_topic_metrics() -> dict:
    """Step 4: 宏观指标新鲜度审计（不自动重抓 — 数据来自 WebFetch 实测）"""
    try:
        rows = _query_all("""
            SELECT theme, COUNT(*) AS n_metrics,
                   MAX(metric_date) AS latest, MAX(captured_at) AS captured
            FROM ai_theme_topic_metrics GROUP BY theme
        """)
        themes_info = [{
            "theme": theme, "n_metrics": n,
            "latest_metric_date": latest.isoformat() if hasattr(latest, "isoformat") else latest,
            "last_captured_at": captured.isoformat() if hasattr(captured, "isoformat") else captured,
        } for theme, n, latest, captured in rows]
        themes_with_none = ["rare_earths"] if not any(t["theme"] == "rare_earths" for t in themes_info) else []
        return {
            "step": "topic_metrics_audit",
            "status": "ok",
            "themes_with_metrics": themes_info,
            "themes_without_metrics": themes_with_none,
            "note": "宏观指标手动录入（WebFetch 实测）；后续可加 cron 提醒",
        }
    except Exception as e:
        return {"step": "topic_metrics_audit", "status": "error", "error": str(e)}


def _step_coverage_audit() -> dict:
    """Step 6: 跑覆盖率审计 → data/latest/ai_theme_coverage_audit.json"""
    try:
        from stock_research.jobs.ai_theme_coverage_audit import run_audit, OUTPUT_PATH
        con = get_db()
        try:
            audit = run_audit(con)
        finally:
            con.close()
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str))
        return {
            "step": "coverage_audit",
            "status": "ok",
            "n_total_issues": audit["n_total_issues"],
            "out": str(OUTPUT_PATH),
            "counts": {
                k: audit[k]["count"] for k in
                ("high_score_no_chain", "theme_no_evidence",
                 "confirmed_stale_risk", "ticker_conflicts")
            },
        }
    except Exception as e:
        return {"step": "coverage_audit", "status": "error", "error": str(e), "trace": traceback.format_exc()}


def _step_aggregate_tags() -> dict:
    """Step 5: 跑 aggregate_theme_tags 把 evidence 聚合成 tags"""
    try:
        from stock_research.jobs.aggregate_theme_tags import aggregate_tags
        con = get_db()
        try:
            stat = aggregate_tags(con)
            bad_confirmed = con.execute("""
                SELECT COUNT(*) FROM ai_theme_company_tags
                WHERE evidence_status='confirmed'
                  AND (source_count_a < 1 OR (source_count_a + source_count_b + source_count_c) < 2)
            """).fetchone()[0]
        finally:
            con.close()
        return {
            "step": "tags_aggregate",
            "status": "ok" if bad_confirmed == 0 else "error",
            "n_tags": stat["n_tags"],
            "by_status": stat["by_status"],
            "bad_confirmed": bad_confirmed,
        }
    except Exception as e:
        return {"step": "tags_aggregate", "status": "error", "error": str(e), "trace": traceback.format_exc()}


def _watchlist_count() -> int:
    return _query_one("SELECT COUNT(*) FROM manual_watchlist")[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="AI 主题雷达统一刷新（文档 §十.1）")
    parser.add_argument("--scan-sec", action="store_true",
                        help="跑 SEC EDGAR 扫描（默认 skip，避免每次 refresh 都打 API）")
    parser.add_argument("--refresh-etf", action="store_true",
                        help="重 ingest ETF 持仓快照（默认 skip）")
    parser.add_argument("--summary-out", default=str(SUMMARY_PATH),
                        help="刷新摘要 JSON 输出路径")
    args = parser.parse_args()

    print("=" * 70)
    print("AI 主题雷达 · 证据系统统一刷新")
    print("=" * 70)

    sys.path.insert(0, str(REPO / "scripts" / "tools"))
    wl_before = _watchlist_count()

    started_at = datetime.now()
    results = []

    print("\n[Step 1/6] 数据源健康检查")
    r = _step_seed_sources(); results.append(r); print(f"  → {r['status']} · ok={r.get('n_ok')}/{r.get('n_total')}")

    print("\n[Step 2/6] ETF 持仓刷新")
    r = _step_refresh_etf(args.refresh_etf); results.append(r); print(f"  → {r['status']} · {r.get('action')}")

    print("\n[Step 3/6] SEC 公司证据扫描 + stale 规则")
    r = _step_sec_scan(args.scan_sec); results.append(r); print(f"  → {r['status']} · {r.get('action')}")

    print("\n[Step 4/6] 宏观指标新鲜度审计")
    r = _step_topic_metrics(); results.append(r); print(f"  → {r['status']}")

    print("\n[Step 5/6] 公司标签聚合")
    r = _step_aggregate_tags(); results.append(r); print(f"  → {r['status']} · tags={r.get('n_tags')} · {r.get('by_status')}")

    print("\n[Step 6/6] 覆盖率审计")
    r = _step_coverage_audit(); results.append(r); print(f"  → {r['status']} · issues={r.get('n_total_issues')} · counts={r.get('counts')}")

    wl_after = _watchlist_count()

    summary = {
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now().isoformat(),
        "args": {"scan_sec": args.scan_sec, "refresh_etf": args.refresh_etf},
        "steps": results,
        "n_errors": sum(1 for r in results if r["status"] not in ("ok",)),
        "watchlist_invariant": {
            "before": wl_before,
            "after": wl_after,
            "ok": wl_before == wl_after,
        },
    }

    out = Path(args.summary_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(f"\n✅ 刷新摘要写入: {out}")
    print(f"  steps ok={sum(1 for r in results if r['status']=='ok')}/{len(results)}")
    print(f"  watchlist 不变: {summary['watchlist_invariant']['ok']}")

    return 0 if summary["n_errors"] == 0 and summary["watchlist_invariant"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
