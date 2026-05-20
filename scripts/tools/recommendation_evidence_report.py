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

PRODUCTION_SOURCES = ("v6_us", "v6_hk", "v6_cn")


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


def _source_metrics(conn) -> list[dict[str, Any]]:
    # picks 表自 2026-05-14 起有结构化 signal 字段；reviews 表自 b3f8239 起有 signal。
    # rating LIKE '%不建议%' 仅作为历史 reviews（signal IS NULL 且无 picks JOIN）的兜底。
    rows = conn.execute("""
        WITH base AS (
          SELECT
            COALESCE(r.model_source, 'unknown') AS model_source,
            COALESCE(r.signal, p.signal,
              CASE WHEN r.rating LIKE '%⛔%' OR r.rating LIKE '%不建议%' THEN 'avoid'
                   WHEN r.rating LIKE '%观察%' THEN 'watch'
                   ELSE 'buy' END
            ) AS signal,
            r.pct,
            r.benchmark_pct,
            r.alpha_pct,
            COALESCE(
              r.is_success,
              CASE
                WHEN COALESCE(r.signal, p.signal,
                       CASE WHEN r.rating LIKE '%⛔%' OR r.rating LIKE '%不建议%' THEN 'avoid'
                            WHEN r.rating LIKE '%观察%' THEN 'watch'
                            ELSE 'buy' END
                     ) = 'avoid'
                THEN r.alpha_pct < 0
                ELSE r.alpha_pct > 0
              END
            ) AS success
          FROM reviews r
          LEFT JOIN picks p ON p.code = r.code AND p.pick_date = r.pick_date
          WHERE COALESCE(r.model_source, '') IN ('v6_us', 'v6_hk', 'v6_cn')
            AND r.alpha_pct IS NOT NULL
        )
        SELECT
          model_source,
          signal,
          COUNT(*) AS n,
          ROUND(AVG(pct), 2) AS avg_pct,
          ROUND(AVG(benchmark_pct), 2) AS avg_benchmark_pct,
          ROUND(AVG(alpha_pct), 2) AS avg_alpha_pct,
          ROUND(SUM(CASE WHEN success THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS signal_hit_rate
        FROM base
        GROUP BY model_source, signal
        ORDER BY model_source, signal
    """).fetchall()
    cols = ["model_source", "signal", "n", "avg_pct", "avg_benchmark_pct", "avg_alpha_pct", "signal_hit_rate"]
    return [dict(zip(cols, r)) for r in rows]


def _latest_pick_metrics(conn) -> dict[str, Any]:
    ph = ",".join(["?"] * len(PRODUCTION_SOURCES))
    latest_rows = conn.execute(
        f"""
        SELECT model_source, MAX(pick_date) AS latest_date
        FROM picks
        WHERE model_source IN ({ph})
        GROUP BY model_source
        """,
        list(PRODUCTION_SOURCES),
    ).fetchall()
    out: dict[str, Any] = {}
    for src, latest_date in latest_rows:
        stats = conn.execute("""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN entry_price IS NOT NULL THEN 1 ELSE 0 END) AS with_entry,
                   SUM(CASE WHEN COALESCE(signal, 'buy') = 'buy' THEN 1 ELSE 0 END) AS buy_n,
                   SUM(CASE WHEN COALESCE(signal,
                                          CASE WHEN rating LIKE '%⛔%' OR rating LIKE '%不建议%' THEN 'avoid'
                                               ELSE 'buy' END) = 'avoid' THEN 1 ELSE 0 END) AS avoid_n
            FROM picks
            WHERE model_source = ? AND pick_date = ?
        """, [src, latest_date]).fetchone()
        out[src] = {
            "latest_date": str(latest_date)[:10],
            "n": int(stats[0] or 0),
            "with_entry_price": int(stats[1] or 0),
            "buy_n": int(stats[2] or 0),
            "avoid_n": int(stats[3] or 0),
        }
    return out


def _review_coverage(conn) -> dict[str, Any]:
    ph = ",".join(["?"] * len(PRODUCTION_SOURCES))
    mature = conn.execute(
        f"""
        SELECT model_source, COUNT(*)
        FROM picks
        WHERE model_source IN ({ph})
          AND COALESCE(signal, 'buy') = 'buy'
          AND pick_date < CURRENT_DATE
          AND entry_price IS NOT NULL
        GROUP BY model_source
        """,
        list(PRODUCTION_SOURCES),
    ).fetchall()
    reviewed = conn.execute(
        f"""
        SELECT p.model_source, COUNT(*)
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
        GROUP BY p.model_source
        """,
        list(PRODUCTION_SOURCES),
    ).fetchall()
    mature_map = {src: int(n) for src, n in mature}
    reviewed_map = {src: int(n) for src, n in reviewed}
    by_source = {}
    for src in PRODUCTION_SOURCES:
        m = mature_map.get(src, 0)
        r = reviewed_map.get(src, 0)
        by_source[src] = {"mature": m, "reviewed": r, "coverage": _coverage(r, m)}

    # V2 path: pick_outcomes 里每个 (run_id, market, symbol, horizon) 都是一个 reviewed 样本
    # mature 计数 = 每个 V2 pick 在每个 horizon 上是否已成熟（today >= run_date + horizon_days）
    v2_total_reviewed = 0
    v2_total_mature = 0
    by_horizon: dict[str, dict] = {}
    try:
        tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
        if "pick_outcomes" in tables and "recommendation_picks" in tables and "recommendation_runs" in tables:
            # mature = (run_date + horizon_days) <= today，针对 system_tech_universe 系列
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
    except Exception as e:
        by_horizon["error"] = str(e)

    total_m = sum(x["mature"] for x in by_source.values()) + v2_total_mature
    total_r = sum(x["reviewed"] for x in by_source.values()) + v2_total_reviewed
    return {
        "by_source": by_source,
        "v2_by_horizon": by_horizon,
        "v2_total_mature": v2_total_mature,
        "v2_total_reviewed": v2_total_reviewed,
        "total_mature": total_m,
        "total_reviewed": total_r,
        "coverage": _coverage(total_r, total_m),
    }


def _discovery_metrics(conn) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for window in (1, 5, 20, 60):
        row = conn.execute(f"""
            SELECT COUNT(alpha_{window}d) AS n,
                   ROUND(AVG(alpha_{window}d), 2) AS avg_alpha,
                   ROUND(SUM(CASE WHEN alpha_{window}d > 0 THEN 1 ELSE 0 END) * 100.0
                         / NULLIF(COUNT(alpha_{window}d), 0), 1) AS hit_rate
            FROM discovery_tracking
        """).fetchone()
        out[f"{window}d"] = {
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
    for src in PRODUCTION_SOURCES:
        row = (payload.get("latest_picks") or {}).get(src) or {}
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
    payload = {
        "generated_at": datetime.now().isoformat(),
        "quality_gate": _load_json("data/latest/recommendation_quality_gate.json") or {},
        "latest_picks": _latest_pick_metrics(conn),
        "review_coverage": _review_coverage(conn),
        "review_metrics_by_source": _source_metrics(conn),
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
