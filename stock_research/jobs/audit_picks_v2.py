"""V2 版当日 picks 横截面审查（Risk Parity + 估值 + Markowitz）。

替代 2026-05-20 V1 cutover 删除的 stock_research.jobs.audit_picks——
读 V2 recommendation_picks（最新 system_tech_universe run）+ system_universe
+ price_daily，按 V1 picks_audit core 模块期望的 normalized schema 喂入，
落盘到 SNAPSHOT_DIR/audit/picks_audit_*.json + DuckDB snapshots 表（dual-write），
dashboard 的「买前审查」tab 即可读到。

V2 rating → V1 schema 映射（picks_audit.filter_strong_picks 看 "⭐⭐⭐"）：
  strong_buy → "⭐⭐⭐ 推荐"
  buy        → "⭐⭐ 关注"
  其他       → 原样保留

CLI:
  python3 -m stock_research.jobs.audit_picks_v2
  python3 -m stock_research.jobs.audit_picks_v2 --no-correlation
  python3 -m stock_research.jobs.audit_picks_v2 --fast
"""
from __future__ import annotations
import argparse
import logging
import sys
from datetime import datetime
from typing import Any

import duckdb

from .. import config
from ..adapters import store
from ..core import picks_audit

logger = logging.getLogger("stock_research.jobs.audit_picks_v2")


def _rating_to_v1(v2_rating: str | None) -> str:
    if not v2_rating:
        return ""
    r = v2_rating.lower()
    if r == "strong_buy":
        return "⭐⭐⭐ 推荐"
    if r == "buy":
        return "⭐⭐ 关注"
    return v2_rating


def _normalize_picks_from_v2(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """从最新 system_tech_universe run 拼 picks_today，schema 对齐 picks_audit core。"""
    rows = conn.execute(
        """
        WITH latest_run AS (
            SELECT run_id, generated_at
            FROM recommendation_runs
            WHERE universe_scope = 'system_tech_universe' AND status = 'generated'
            ORDER BY generated_at DESC LIMIT 1
        ),
        latest_price AS (
            SELECT * FROM price_daily
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY market, symbol
                ORDER BY trade_date DESC, fetched_at DESC
            ) = 1
        )
        SELECT
            rp.market,
            rp.symbol,
            COALESCE(rp.name, u.name) AS name,
            rp.rating,
            rp.total_score,
            u.theme,
            u.industry,
            lp.peg_ratio,
            lp.forward_pe,
            lp.one_year_pct,
            lr.generated_at
        FROM recommendation_picks rp
        JOIN latest_run lr USING(run_id)
        LEFT JOIN system_universe u ON u.market = rp.market AND u.symbol = rp.symbol
        LEFT JOIN latest_price lp ON lp.market = rp.market AND lp.symbol = rp.symbol
        ORDER BY rp.total_score DESC NULLS LAST
        """
    ).fetchall()

    fallback_ms = int(datetime.now().timestamp() * 1000)
    picks_today: list[dict[str, Any]] = []
    for (market, symbol, name, rating, score, theme, industry,
         peg, fpe, y1, gen_at) in rows:
        picks_today.append({
            "normalized": {
                "code": symbol,
                "name": name or symbol,
                "market": market,
                "rating": _rating_to_v1(rating),
                "raw_rating_v2": rating,
                "theme": theme or industry or "未分类",
                "peg_at_pick": peg,
                "pe_at_pick": fpe,
                "y1_at_pick": y1,
                "score": score,
                "pick_date": int(gen_at.timestamp() * 1000) if gen_at else fallback_ms,
            },
        })
    return picks_today


def run(skip_correlation: bool = False) -> dict[str, Any]:
    logger.info("connect duckdb: %s", config.DUCKDB_PATH)
    conn = duckdb.connect(str(config.DUCKDB_PATH), read_only=True)
    try:
        picks_today = _normalize_picks_from_v2(conn)
    finally:
        conn.close()

    strong_count = len(picks_audit.filter_strong_picks(picks_today))
    logger.info("picks_today=%d strong=%d", len(picks_today), strong_count)

    result: dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "picks_today_count": len(picks_today),
        "strong_picks_count": strong_count,
        "theme_concentration": picks_audit.theme_concentration(picks_today),
        "valuation_sanity": picks_audit.valuation_sanity(picks_today, []),
        "correlation": (
            {"status": "skip", "reason": "用户指定 --no-correlation"}
            if skip_correlation
            else picks_audit.correlation_matrix(picks_today)
        ),
        "source": "v2_recommendation_picks",
    }

    print_report(result)
    store.save_json(result, config.AUDIT_DIR, "picks_audit")
    return result


def print_report(r: dict[str, Any]) -> None:
    print(f"\n{'═' * 72}")
    print(f"  📋 V2 当日 picks 横截面审查 · {r['ts']}")
    print(f"     当日 picks {r['picks_today_count']} 只 · 其中 ⭐⭐⭐ 强推荐 {r.get('strong_picks_count', 0)} 只")
    print(f"{'═' * 72}\n")

    tc = r["theme_concentration"]
    print("【1/3 主题集中度（Risk Parity）】")
    if tc.get("status") == "ok":
        print(f"  {tc['verdict']}")
        for d in tc["distribution"]:
            bar = "█" * int(d["pct"] / 5)
            print(f"    {d['theme']:<24} {d['n']:>2} 只 {d['pct']:>5.1f}% {bar}")
    else:
        print(f"  跳过：{tc.get('reason')}")
    print()

    v = r["valuation_sanity"]
    print("【2/3 估值合理性】")
    if v["warn_count"] == 0:
        print("  🟢 当日 ⭐⭐⭐ 推荐估值均在合理范围")
    else:
        print(f"  ⚠️ {v['warn_count']} 只 ⭐⭐⭐ 推荐有估值警告：")
        for w in v["warnings"]:
            print(f"    · {w['name']} ({w['code']}): {' / '.join(w['flags'])}")
    print()

    c = r["correlation"]
    print("【3/3 相关性矩阵（Markowitz）】")
    if c.get("status") == "ok":
        print(f"  分析 {c['n_tickers']} 只 ⭐⭐⭐ 推荐过去 6 个月日收益相关性")
        print(f"  相关 > {c['threshold']}（伪分散对）：{len(c['high_corr_pairs'])} 对")
        for p in c["high_corr_pairs"][:10]:
            print(f"    · {p['name_a']} ↔ {p['name_b']}: r = {p['r']}")
    else:
        print(f"  跳过：{c.get('reason')}")
    print(f"{'═' * 72}\n")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    p = argparse.ArgumentParser(description="V2 当日 picks 横截面审查（Risk Parity + 估值 + Markowitz）")
    p.add_argument("--no-correlation", action="store_true", help="跳过相关性矩阵（避免 yfinance 慢）")
    p.add_argument("--fast", action="store_true", help="同 --no-correlation")
    args = p.parse_args()
    run(skip_correlation=args.no_correlation or args.fast)
    return 0


if __name__ == "__main__":
    sys.exit(main())
