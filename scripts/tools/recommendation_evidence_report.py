"""Build an evidence report for StockAssistant recommendations.

This report answers a narrow question: "Do the produced recommendations beat
their market benchmarks after they mature?" It only uses local DuckDB evidence.
No network calls are made here.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import get_db  # noqa: E402

# 2026-05-21 V1 cutover: V1 production sources 已废


def _load_json(rel: str) -> dict | None:
    path = REPO / rel
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%"


def _coverage(n: int, d: int) -> float | None:
    if d <= 0:
        return None
    return n / d


def _v2_market_metrics(conn) -> list[dict[str, Any]]:
    """V2 路径：pick_outcomes 按 (market, horizon) 聚合 alpha 评估指标。

    返回与 _source_metrics 一致的字段形状，让 _grade / markdown 渲染兼容。
    market_source 用 'v2_us/v2_hk/v2_cn'，signal 全部当 'buy'（V2 picks 只产 buy/avoid，
    其中只有 alpha_pct 不空的进入 outcomes，全是 buy）。
    """
    tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
    if "pick_outcomes" not in tables:
        return []
    rows = conn.execute(
        """
        SELECT market, horizon,
               COUNT(*) AS n,
               ROUND(AVG(return_pct), 2) AS avg_pct,
               ROUND(AVG(benchmark_pct), 2) AS avg_benchmark_pct,
               ROUND(AVG(alpha_pct), 2) AS avg_alpha_pct,
               ROUND(SUM(CASE WHEN is_success THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS hit_rate
        FROM pick_outcomes
        WHERE alpha_pct IS NOT NULL
        GROUP BY market, horizon
        ORDER BY market, horizon
        """
    ).fetchall()
    out = []
    for market, horizon, n, avg_pct, avg_bench, avg_alpha, hit_rate in rows:
        out.append({
            "model_source": f"v2_{str(market).lower()}",
            "signal": f"buy ({horizon})",
            "n": int(n or 0),
            "avg_pct": avg_pct,
            "avg_benchmark_pct": avg_bench,
            "avg_alpha_pct": avg_alpha,
            "signal_hit_rate": hit_rate,
        })
    return out


def _v2_latest_pick_metrics(conn) -> dict[str, Any]:
    """V2 路径：最新 system_tech_universe run 的 picks 按 market 拆。"""
    tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
    if "recommendation_picks" not in tables or "recommendation_runs" not in tables:
        return {}
    latest = conn.execute(
        """
        SELECT run_id, run_date FROM recommendation_runs
        WHERE universe_scope = 'system_tech_universe' AND status = 'generated'
        ORDER BY generated_at DESC LIMIT 1
        """
    ).fetchone()
    if not latest:
        return {}
    run_id, run_date = latest
    rows = conn.execute(
        """
        SELECT market,
               COUNT(*) AS n,
               SUM(CASE WHEN entry_price IS NOT NULL THEN 1 ELSE 0 END) AS with_entry,
               SUM(CASE WHEN signal = 'buy' THEN 1 ELSE 0 END) AS buy_n,
               SUM(CASE WHEN signal = 'avoid' THEN 1 ELSE 0 END) AS avoid_n
        FROM recommendation_picks
        WHERE run_id = ?
        GROUP BY market
        """,
        [run_id],
    ).fetchall()
    out = {}
    for market, n, with_entry, buy_n, avoid_n in rows:
        key = f"v2_{str(market).lower()}"
        out[key] = {
            "latest_date": str(run_date)[:10],
            "n": int(n or 0),
            "with_entry_price": int(with_entry or 0),
            "buy_n": int(buy_n or 0),
            "avoid_n": int(avoid_n or 0),
        }
    return out


def _review_coverage(conn) -> dict[str, Any]:
    """V2 only：pick_outcomes 按 horizon 计算成熟样本 / 已评估样本。

    2026-05-21 V1 cutover：删除 V1 picks/reviews 查询路径。
    """
    by_horizon: dict[str, dict] = {}
    v2_total_reviewed = 0
    v2_total_mature = 0
    tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
    if {"pick_outcomes", "recommendation_picks", "recommendation_runs"}.issubset(tables):
        v2_mature_rows = conn.execute(
            """
            SELECT horizon, COUNT(*) FROM (
                SELECT rp.run_id, rp.market, rp.symbol, h.horizon
                FROM recommendation_picks rp
                JOIN recommendation_runs rr ON rp.run_id = rr.run_id
                CROSS JOIN (VALUES ('1d', 1), ('5d', 5), ('20d', 20)) AS h(horizon, days)
                WHERE rr.universe_scope = 'system_tech_universe'
                  AND rr.status = 'generated'
                  AND rp.signal = 'buy'
                  AND (rr.run_date + INTERVAL (h.days) DAY) <= CURRENT_DATE
            )
            GROUP BY horizon
            """
        ).fetchall()
        v2_reviewed_rows = conn.execute(
            """
            SELECT horizon, COUNT(*) FROM pick_outcomes
            WHERE alpha_pct IS NOT NULL
            GROUP BY horizon
            """
        ).fetchall()
        mature_by_h = {h: int(n) for h, n in v2_mature_rows}
        reviewed_by_h = {h: int(n) for h, n in v2_reviewed_rows}
        for h in ("1d", "5d", "20d"):
            m = mature_by_h.get(h, 0)
            r = reviewed_by_h.get(h, 0)
            by_horizon[h] = {"mature": m, "reviewed": r, "coverage": _coverage(r, m)}
            v2_total_mature += m
            v2_total_reviewed += r
    return {
        "v2_by_horizon": by_horizon,
        "v2_total_mature": v2_total_mature,
        "v2_total_reviewed": v2_total_reviewed,
        "total_mature": v2_total_mature,
        "total_reviewed": v2_total_reviewed,
        "coverage": _coverage(v2_total_reviewed, v2_total_mature),
    }


def _discovery_metrics(conn) -> dict[str, Any]:
    """V2 path：pick_outcomes 按 horizon 聚合（取代 V1 discovery_tracking）。"""
    out: dict[str, Any] = {}
    tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
    if "pick_outcomes" not in tables:
        return {f"{w}d": {"n": 0, "avg_alpha_pct": None, "hit_rate": None} for w in (1, 5, 20)}
    for horizon in ("1d", "5d", "20d"):
        row = conn.execute(
            """
            SELECT COUNT(alpha_pct) AS n,
                   ROUND(AVG(alpha_pct), 2) AS avg_alpha,
                   ROUND(SUM(CASE WHEN alpha_pct > 0 THEN 1 ELSE 0 END) * 100.0
                         / NULLIF(COUNT(alpha_pct), 0), 1) AS hit_rate
            FROM pick_outcomes WHERE horizon = ?
            """,
            [horizon],
        ).fetchone()
        out[horizon] = {
            "n": int(row[0] or 0),
            "avg_alpha_pct": row[1],
            "hit_rate": row[2],
        }
    return out


def _grade(payload: dict[str, Any]) -> str:
    gate = (payload.get("quality_gate") or {}).get("status")
    if gate == "FAIL":
        return "BLOCKED"
    coverage = (payload.get("review_coverage") or {}).get("coverage") or 0
    total_reviewed = (payload.get("review_coverage") or {}).get("total_reviewed") or 0
    source_rows = payload.get("review_metrics_by_source") or []
    alpha_rows = [r for r in source_rows if r.get("signal") == "buy" and r.get("avg_alpha_pct") is not None]
    avg_alpha = None
    if alpha_rows:
        total_n = sum(r["n"] for r in alpha_rows)
        avg_alpha = sum(r["avg_alpha_pct"] * r["n"] for r in alpha_rows) / total_n if total_n else None
    if total_reviewed < 30:
        return "INSUFFICIENT_EVIDENCE"
    if coverage < 0.8:
        return "LOW_COVERAGE"
    if avg_alpha is not None and avg_alpha > 0:
        return "PROMISING"
    return "NEEDS_IMPROVEMENT"


def _to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Recommendation Evidence Report",
        "",
        f"Generated: {payload['generated_at']}",
        f"Evidence grade: **{payload['evidence_grade']}**",
        f"Quality gate: **{(payload.get('quality_gate') or {}).get('status', 'UNKNOWN')}**",
        "",
        "## Latest Production Picks",
        "",
        "| Source | Latest | Picks | Entry Px | Avoid |",
        "|---|---:|---:|---:|---:|",
    ]
    for src, row in (payload.get("latest_picks") or {}).items():
        lines.append(f"| {src} | {row.get('latest_date', '—')} | {row.get('n', 0)} | {row.get('with_entry_price', 0)} | {row.get('avoid_n', 0)} |")

    cov = payload.get("review_coverage") or {}
    lines.extend([
        "",
        "## Review Coverage",
        "",
        f"Total reviewed/mature: **{cov.get('total_reviewed', 0)} / {cov.get('total_mature', 0)}** "
        f"({(cov.get('coverage') or 0) * 100:.1f}%)",
        "",
        "| Source | Mature | Reviewed | Coverage |",
        "|---|---:|---:|---:|",
    ])
    for src, row in (cov.get("by_source") or {}).items():
        coverage = row.get("coverage")
        lines.append(f"| {src} | {row.get('mature', 0)} | {row.get('reviewed', 0)} | {'—' if coverage is None else f'{coverage * 100:.1f}%'} |")

    lines.extend([
        "",
        "## Alpha By Production Source",
        "",
        "| Source | Signal | N | Avg Pct | Benchmark | Alpha | Signal Hit |",
        "|---|---|---:|---:|---:|---:|---:|",
    ])
    for row in payload.get("review_metrics_by_source") or []:
        lines.append(
            f"| {row['model_source']} | {row['signal']} | {row['n']} | "
            f"{_pct(row.get('avg_pct'))} | {_pct(row.get('avg_benchmark_pct'))} | "
            f"{_pct(row.get('avg_alpha_pct'))} | {row.get('signal_hit_rate', '—')}% |"
        )

    lines.extend([
        "",
        "## Discovery Candidates",
        "",
        "| Window | N Mature | Avg Alpha | Hit Rate |",
        "|---|---:|---:|---:|",
    ])
    for window, row in (payload.get("discovery_metrics") or {}).items():
        lines.append(f"| {window} | {row.get('n', 0)} | {_pct(row.get('avg_alpha_pct'))} | {row.get('hit_rate', '—')}% |")
    lines.append("")
    lines.append("Interpretation: `INSUFFICIENT_EVIDENCE` means the system is collecting clean data but does not yet have enough matured outcomes to prove predictive value.")
    return "\n".join(lines) + "\n"


def build_report() -> dict[str, Any]:
    conn = get_db()
    # V2-only 数据：pick_outcomes 按 market×horizon 聚合 + 最新 run 按 market 拆 picks
    payload = {
        "generated_at": datetime.now().isoformat(),
        "quality_gate": _load_json("data/latest/recommendation_quality_gate.json") or {},
        "latest_picks": _v2_latest_pick_metrics(conn),
        "review_coverage": _review_coverage(conn),
        "review_metrics_by_source": _v2_market_metrics(conn),
        "discovery_metrics": _discovery_metrics(conn),
    }
    conn.close()
    payload["evidence_grade"] = _grade(payload)
    return payload


def main() -> int:
    payload = build_report()
    out_json = REPO / "data" / "latest" / "recommendation_evidence.json"
    out_md = REPO / "data" / "reports" / "recommendation_evidence.md"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    out_md.write_text(_to_markdown(payload), encoding="utf-8")
    print(f"Recommendation evidence: {payload['evidence_grade']}")
    cov = payload["review_coverage"]
    print(f"  reviewed/mature={cov['total_reviewed']}/{cov['total_mature']} coverage={(cov.get('coverage') or 0) * 100:.1f}%")
    print(f"  JSON: {out_json}")
    print(f"  MD:   {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
