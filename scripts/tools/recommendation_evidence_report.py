"""Build an evidence report for StockAssistant recommendations.

This report answers a narrow question: "Do the produced recommendations beat
their market benchmarks after they mature?" It only uses local DuckDB evidence.
No network calls are made here.
"""
from __future__ import annotations

import json
import os
import sys
import argparse
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import get_db  # noqa: E402

# 2026-05-21 V1 cutover: V1 production sources 已废
PRODUCTION_METRICS_START_DATE = os.environ.get("STOCK_ASSISTANT_METRICS_START_DATE", "2026-05-25")
DEFAULT_STRATEGY_VERSION = os.environ.get("STOCK_ASSISTANT_STRATEGY_VERSION", "latest")


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


def _tables(conn) -> set[str]:
    try:
        return {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
    except Exception:
        return set()


def _table_columns(conn, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
    except Exception:
        return set()


def _latest_strategy_version(conn) -> str | None:
    if "recommendation_runs" not in _tables(conn):
        return None
    if "strategy_version" not in _table_columns(conn, "recommendation_runs"):
        return None
    row = conn.execute(
        """
        SELECT strategy_version
        FROM recommendation_runs
        WHERE universe_scope = 'system_tech_universe'
          AND status = 'generated'
          AND strategy_version IS NOT NULL
          AND strategy_version <> ''
        ORDER BY generated_at DESC
        LIMIT 1
        """
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _resolve_strategy_version(conn, requested: str | None) -> str | None:
    value = str(requested or "latest").strip()
    if value.lower() in {"", "latest", "current"}:
        return _latest_strategy_version(conn)
    if value.lower() in {"all", "*"}:
        return None
    return value


def _strategy_filter(conn, strategy_version: str | None, alias: str = "rr") -> tuple[str, list[Any]]:
    if not strategy_version:
        return "", []
    if "strategy_version" not in _table_columns(conn, "recommendation_runs"):
        return "", []
    prefix = f"{alias}." if alias else ""
    return f" AND {prefix}strategy_version = ?", [strategy_version]


def _v2_market_metrics(conn, strategy_version: str | None = None) -> list[dict[str, Any]]:
    """V2 路径：pick_outcomes 按 (market, horizon) 聚合 alpha 评估指标。

    返回与 _source_metrics 一致的字段形状，让 _grade / markdown 渲染兼容。
    market_source 用 'v2_us/v2_hk/v2_cn'，仅统计 picks.signal='buy'。

    2026-06-02 加 JOIN picks WHERE signal='buy':
      历史 picks 已经出现 watch 信号（1810.HK 等），evaluate_v2_picks
      之前没过滤把 watch 也算进 outcomes；这里 alpha 聚合再加一层防御性
      过滤，确保即使 outcomes 含历史污染也不影响策略证据指标。
    """
    tables = _tables(conn)
    if not {"pick_outcomes", "recommendation_runs", "recommendation_picks"}.issubset(tables):
        return []
    strategy_clause, strategy_params = _strategy_filter(conn, strategy_version)
    rows = conn.execute(
        f"""
        SELECT po.market, po.horizon,
               COUNT(*) AS n,
               ROUND(AVG(po.return_pct), 2) AS avg_pct,
               ROUND(AVG(po.benchmark_pct), 2) AS avg_benchmark_pct,
               ROUND(AVG(po.alpha_pct), 2) AS avg_alpha_pct,
               ROUND(SUM(CASE WHEN po.is_success THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS hit_rate
        FROM pick_outcomes po
        JOIN recommendation_runs rr ON rr.run_id = po.run_id
        JOIN recommendation_picks p
          ON p.run_id = po.run_id AND p.market = po.market AND p.symbol = po.symbol
        WHERE po.alpha_pct IS NOT NULL
          AND rr.universe_scope = 'system_tech_universe'
          AND rr.run_date >= ?
          AND COALESCE(p.signal, 'buy') = 'buy'
          {strategy_clause}
        GROUP BY po.market, po.horizon
        ORDER BY po.market, po.horizon
        """,
        [PRODUCTION_METRICS_START_DATE, *strategy_params],
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


def _v2_latest_pick_metrics(conn, strategy_version: str | None = None) -> dict[str, Any]:
    """V2 路径：最新 system_tech_universe run 的 picks 按 market 拆。"""
    tables = _tables(conn)
    if "recommendation_picks" not in tables or "recommendation_runs" not in tables:
        return {}
    strategy_clause, strategy_params = _strategy_filter(conn, strategy_version, alias="")
    latest = conn.execute(
        f"""
        SELECT run_id, run_date, strategy_version, model_version FROM recommendation_runs
        WHERE universe_scope = 'system_tech_universe' AND status = 'generated'
          {strategy_clause}
        ORDER BY generated_at DESC LIMIT 1
        """,
        strategy_params,
    ).fetchone()
    if not latest:
        return {}
    run_id, run_date, latest_strategy, latest_model = latest
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
            "strategy_version": latest_strategy,
            "model_version": latest_model,
        }
    return out


def _review_coverage(conn, strategy_version: str | None = None) -> dict[str, Any]:
    """V2 only：pick_outcomes 按 horizon 计算成熟样本 / 已评估样本。

    2026-05-21 V1 cutover：删除 V1 picks/reviews 查询路径。
    """
    by_horizon: dict[str, dict] = {}
    v2_total_reviewed = 0
    v2_total_mature = 0
    tables = _tables(conn)
    if {"pick_outcomes", "recommendation_picks", "recommendation_runs"}.issubset(tables):
        strategy_clause, strategy_params = _strategy_filter(conn, strategy_version)
        v2_mature_rows = conn.execute(
            f"""
            SELECT h.horizon, COUNT(*)
            FROM recommendation_picks rp
            JOIN recommendation_runs rr ON rp.run_id = rr.run_id
            CROSS JOIN (VALUES ('1d', 1), ('5d', 5), ('20d', 20)) AS h(horizon, days)
            WHERE rr.universe_scope = 'system_tech_universe'
              AND rr.status = 'generated'
              AND COALESCE(rp.signal, 'buy') = 'buy'
              AND rp.entry_price IS NOT NULL
              AND rp.entry_price > 0
              AND rr.run_date >= ?
              {strategy_clause}
              AND (rr.run_date + INTERVAL (h.days) DAY) <= CURRENT_DATE
            GROUP BY h.horizon
            """,
            [PRODUCTION_METRICS_START_DATE, *strategy_params],
        ).fetchall()
        v2_reviewed_rows = conn.execute(
            f"""
            SELECT po.horizon, COUNT(*)
            FROM pick_outcomes po
            JOIN recommendation_runs rr ON rr.run_id = po.run_id
            JOIN recommendation_picks rp
              ON rp.run_id = po.run_id AND rp.market = po.market AND rp.symbol = po.symbol
            WHERE po.alpha_pct IS NOT NULL
              AND rr.universe_scope = 'system_tech_universe'
              AND rr.run_date >= ?
              AND COALESCE(rp.signal, 'buy') = 'buy'
              {strategy_clause}
            GROUP BY po.horizon
            """,
            [PRODUCTION_METRICS_START_DATE, *strategy_params],
        ).fetchall()
        local_price_ready_rows = []
        if "price_daily" in tables:
            local_price_ready_rows = conn.execute(
                f"""
                SELECT horizon, COUNT(*) FROM (
                SELECT rp.run_id, rp.market, rp.symbol, h.horizon
                FROM recommendation_picks rp
                JOIN recommendation_runs rr ON rp.run_id = rr.run_id
                CROSS JOIN (VALUES ('1d', 1), ('5d', 5), ('20d', 20)) AS h(horizon, days)
                WHERE rr.universe_scope = 'system_tech_universe'
                  AND rr.status = 'generated'
                  AND COALESCE(rp.signal, 'buy') = 'buy'
                  AND rp.entry_price IS NOT NULL
                  AND rp.entry_price > 0
                  AND rr.run_date >= ?
                  {strategy_clause}
                  AND (rr.run_date + INTERVAL (h.days) DAY) <= CURRENT_DATE
                  AND EXISTS (
                    SELECT 1
                    FROM price_daily ps
                    WHERE ps.market = rp.market
                      AND ps.symbol = rp.symbol
                      AND ps.close IS NOT NULL
                      AND ps.trade_date >= (rr.run_date + INTERVAL (h.days) DAY)
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM price_daily be
                    WHERE be.market = rp.market
                      AND be.symbol = CASE rp.market
                        WHEN 'US' THEN 'SPY'
                        WHEN 'HK' THEN '^HSI'
                        WHEN 'CN' THEN '000300.SS'
                        ELSE NULL
                      END
                      AND be.close IS NOT NULL
                      AND be.trade_date >= rr.run_date
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM price_daily bx
                    WHERE bx.market = rp.market
                      AND bx.symbol = CASE rp.market
                        WHEN 'US' THEN 'SPY'
                        WHEN 'HK' THEN '^HSI'
                        WHEN 'CN' THEN '000300.SS'
                        ELSE NULL
                      END
                      AND bx.close IS NOT NULL
                      AND bx.trade_date >= (rr.run_date + INTERVAL (h.days) DAY)
                  )
            )
            GROUP BY horizon
            """,
                [PRODUCTION_METRICS_START_DATE, *strategy_params],
            ).fetchall()
        mature_by_h = {h: int(n) for h, n in v2_mature_rows}
        reviewed_by_h = {h: int(n) for h, n in v2_reviewed_rows}
        local_ready_by_h = {h: int(n) for h, n in local_price_ready_rows}
        for h in ("1d", "5d", "20d"):
            m = mature_by_h.get(h, 0)
            r = reviewed_by_h.get(h, 0)
            by_horizon[h] = {
                "mature": m,
                "reviewed": r,
                "coverage": _coverage(r, m),
                "local_price_ready": local_ready_by_h.get(h, 0),
            }
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


def _discovery_metrics(conn, strategy_version: str | None = None) -> dict[str, Any]:
    """V2 path：pick_outcomes 按 horizon 聚合（取代 V1 discovery_tracking）。"""
    out: dict[str, Any] = {}
    tables = _tables(conn)
    if not {"pick_outcomes", "recommendation_runs", "recommendation_picks"}.issubset(tables):
        return {f"{w}d": {"n": 0, "avg_alpha_pct": None, "hit_rate": None} for w in (1, 5, 20)}
    strategy_clause, strategy_params = _strategy_filter(conn, strategy_version)
    for horizon in ("1d", "5d", "20d"):
        row = conn.execute(
            f"""
            SELECT COUNT(po.alpha_pct) AS n,
                   ROUND(AVG(po.alpha_pct), 2) AS avg_alpha,
                   ROUND(SUM(CASE WHEN po.alpha_pct > 0 THEN 1 ELSE 0 END) * 100.0
                         / NULLIF(COUNT(po.alpha_pct), 0), 1) AS hit_rate
            FROM pick_outcomes po
            JOIN recommendation_runs rr ON rr.run_id = po.run_id
            JOIN recommendation_picks rp
              ON rp.run_id = po.run_id AND rp.market = po.market AND rp.symbol = po.symbol
            WHERE po.horizon = ?
              AND rr.universe_scope = 'system_tech_universe'
              AND rr.run_date >= ?
              AND COALESCE(rp.signal, 'buy') = 'buy'
              {strategy_clause}
            """,
            [horizon, PRODUCTION_METRICS_START_DATE, *strategy_params],
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
    alpha_rows = [
        r for r in source_rows
        if str(r.get("signal") or "").startswith("buy") and r.get("avg_alpha_pct") is not None
    ]
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
        f"Metrics start date: **{payload.get('metrics_start_date', '—')}**",
        f"Strategy version: **{payload.get('strategy_version_filter', 'all')}**",
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
        "| Horizon | Mature | Reviewed | Coverage | Local Price Ready |",
        "|---|---:|---:|---:|---:|",
    ])
    for src, row in (cov.get("v2_by_horizon") or cov.get("by_source") or {}).items():
        coverage = row.get("coverage")
        lines.append(
            f"| {src} | {row.get('mature', 0)} | {row.get('reviewed', 0)} | "
            f"{'—' if coverage is None else f'{coverage * 100:.1f}%'} | "
            f"{row.get('local_price_ready', '—')} |"
        )

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


def build_report(strategy_version: str | None = DEFAULT_STRATEGY_VERSION) -> dict[str, Any]:
    conn = get_db(force_read_only=True)
    resolved_strategy_version = _resolve_strategy_version(conn, strategy_version)
    # V2-only 数据：pick_outcomes 按 market×horizon 聚合 + 最新 run 按 market 拆 picks
    payload = {
        "generated_at": datetime.now().isoformat(),
        "metrics_start_date": PRODUCTION_METRICS_START_DATE,
        "strategy_version_filter": resolved_strategy_version or "all",
        "requested_strategy_version": strategy_version or "latest",
        "sample_policy": (
            f"Only V2 recommendation runs on/after {PRODUCTION_METRICS_START_DATE} "
            f"and matching strategy_version={resolved_strategy_version or 'all'} count toward user-facing evidence; "
            "earlier/cross-version rows are retained for audit only."
        ),
        "quality_gate": _load_json("data/latest/recommendation_quality_gate.json") or {},
        "latest_picks": _v2_latest_pick_metrics(conn, resolved_strategy_version),
        "review_coverage": _review_coverage(conn, resolved_strategy_version),
        "review_metrics_by_source": _v2_market_metrics(conn, resolved_strategy_version),
        "discovery_metrics": _discovery_metrics(conn, resolved_strategy_version),
    }
    conn.close()
    payload["evidence_grade"] = _grade(payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strategy-version",
        default=DEFAULT_STRATEGY_VERSION,
        help="Strategy version to evaluate; use 'latest' (default) or 'all'.",
    )
    args = parser.parse_args()
    payload = build_report(strategy_version=args.strategy_version)
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
