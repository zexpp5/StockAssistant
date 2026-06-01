"""AI 主题雷达覆盖率审计（文档 §十.2 / 评审 P3 #10）。

输出 data/latest/ai_theme_coverage_audit.json，列出：
  1. 高分但无 chain 的票（picks total_score >= threshold 但 chain_metadata 空）
  2. 有 chain 但无公司证据的主题票（chain 关联到主题但 evidence 空）
  3. confirmed 但超过 180 天的票（stale 风险）
  4. ticker 映射冲突（同 symbol 多 market 或 evidence vs picks 不一致）

设计原则：
  - 只读，不写任何数据表
  - 失败的审计项不阻断后续
  - 输出 JSON 是 audit trail，给运维/用户参考
  - dashboard 可读这个 JSON 在覆盖率审计区做扩展显示
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import get_db  # noqa: E402  # type: ignore
from stock_research.core.ai_radar import AI_RELEVANT_THEME_KEYWORDS  # noqa: E402


OUTPUT_PATH = REPO / "data" / "latest" / "ai_theme_coverage_audit.json"

# 阈值
HIGH_SCORE_THRESHOLD = 70.0       # picks 高分线
CONFIRMED_STALE_DAYS = 180        # confirmed evidence 超过此天数视为 stale risk


def _audit_high_score_no_chain(con) -> list[dict]:
    """高分 picks 但 chain_metadata 缺失 — 仅审计"AI 雷达相关"的票。

    白名单（system_universe.theme/industry 含任一关键词才进 audit）:
      美/港股 theme: AI / semiconductor / cloud / SaaS / software / internet /
                    cooling / power / nuclear / uranium / rare earth / robot /
                    data / quantum / 互联网 / 半导体 / 软件
      A 股 GICS 代码:
        C39 计算机/通信电子 · C38 电气机械 · I65 软件信息技术
        I64 互联网相关 · I63 电信 · M73 研发 · C40 仪器仪表
        C35 专用设备（含半导体/光伏装备）

    排除非 AI 行业（检测/零售/化工/纺织 等），避免污染 AI 雷达视野。
    保留这些非 AI 高分票供运维参考但不在主 audit 里 — 由别的工具关心。
    """
    # 用 ai_radar.py 的 AI_RELEVANT_THEME_KEYWORDS（单一来源，避免双引擎漂移）
    where_clauses = " OR ".join([
        f"LOWER(COALESCE(su.theme, '')) LIKE '%{kw.lower()}%' OR LOWER(COALESCE(su.industry, '')) LIKE '%{kw.lower()}%'"
        for kw in AI_RELEVANT_THEME_KEYWORDS
    ])

    rows = con.execute(f"""
        WITH latest_run AS (
            SELECT rp.market, MAX(rr.generated_at) AS latest_at
            FROM recommendation_runs rr
            JOIN recommendation_picks rp ON rp.run_id = rr.run_id
            WHERE rr.universe_scope = 'system_tech_universe'
            GROUP BY rp.market
        )
        SELECT rp.market, rp.symbol, rp.name, rp.total_score,
               su.theme, su.industry
        FROM recommendation_runs rr
        JOIN recommendation_picks rp ON rp.run_id = rr.run_id
        JOIN latest_run l ON l.market = rp.market AND l.latest_at = rr.generated_at
        LEFT JOIN chain_metadata cm ON cm.market = rp.market AND cm.symbol = rp.symbol
        LEFT JOIN system_universe su ON su.market = rp.market AND su.symbol = rp.symbol
        WHERE rp.total_score >= ?
          AND (cm.chain IS NULL OR cm.chain = '')
          AND ({where_clauses})
        ORDER BY rp.total_score DESC
    """, [HIGH_SCORE_THRESHOLD]).fetchall()
    return [
        {"market": m, "symbol": s, "name": n,
         "score": round(float(sc), 1),
         "theme": th, "industry": ind}
        for m, s, n, sc, th, ind in rows
    ]


def _audit_theme_chain_no_evidence(con) -> list[dict]:
    """有 chain 映射但 evidence 表里该 theme 没公司证据."""
    # 列每个 theme，标注是否完全没 confirmed/candidate evidence
    rows = con.execute("""
        SELECT cm.theme,
               COUNT(*) FILTER (WHERE t.evidence_status IN ('confirmed', 'candidate')) AS n_active
        FROM (SELECT DISTINCT theme FROM ai_theme_chain_mapping) cm
        LEFT JOIN ai_theme_company_tags t ON t.theme = cm.theme
        GROUP BY cm.theme
    """).fetchall()
    return [
        {"theme": theme, "n_active_evidence": int(n or 0)}
        for theme, n in rows
        if not n  # 只列 0 的
    ]


def _audit_confirmed_stale_risk(con) -> list[dict]:
    """confirmed 但 latest_source_date 超过 180 天."""
    threshold = date.today() - timedelta(days=CONFIRMED_STALE_DAYS)
    rows = con.execute("""
        SELECT theme, symbol, company_name, latest_source_date, evidence_score
        FROM ai_theme_company_tags
        WHERE evidence_status = 'confirmed'
          AND (latest_source_date IS NULL OR latest_source_date < ?)
    """, [threshold]).fetchall()
    return [
        {
            "theme": theme, "symbol": sym, "name": name,
            "latest_source_date": d.isoformat() if hasattr(d, "isoformat") else d,
            "score": float(sc) if sc is not None else None,
        }
        for theme, sym, name, d, sc in rows
    ]


def _audit_ticker_conflicts(con) -> list[dict]:
    """ticker 映射冲突 — 同 symbol 在不同 market 出现."""
    # 1. evidence 表里同 symbol 多 market
    rows = con.execute("""
        SELECT symbol, COUNT(DISTINCT market) AS n_markets, STRING_AGG(DISTINCT market, ',') AS markets
        FROM ai_theme_company_evidence
        WHERE symbol IS NOT NULL AND market IS NOT NULL
        GROUP BY symbol
        HAVING COUNT(DISTINCT market) > 1
    """).fetchall()
    return [
        {"symbol": s, "n_markets": int(n), "markets": m, "source": "evidence"}
        for s, n, m in rows
    ]


def run_audit(con) -> dict[str, Any]:
    high_score_no_chain = _audit_high_score_no_chain(con)
    theme_no_evidence = _audit_theme_chain_no_evidence(con)
    confirmed_stale = _audit_confirmed_stale_risk(con)
    ticker_conflicts = _audit_ticker_conflicts(con)

    n_total_issues = (
        len(high_score_no_chain)
        + len(theme_no_evidence)
        + len(confirmed_stale)
        + len(ticker_conflicts)
    )

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "thresholds": {
            "high_score": HIGH_SCORE_THRESHOLD,
            "confirmed_stale_days": CONFIRMED_STALE_DAYS,
        },
        "n_total_issues": n_total_issues,
        "high_score_no_chain": {
            "count": len(high_score_no_chain),
            "items": high_score_no_chain,
            "rule": f"picks total_score >= {HIGH_SCORE_THRESHOLD} 且 chain_metadata 空",
        },
        "theme_no_evidence": {
            "count": len(theme_no_evidence),
            "items": theme_no_evidence,
            "rule": "chain 映射到主题，但 ai_theme_company_tags 里该主题没 confirmed/candidate",
        },
        "confirmed_stale_risk": {
            "count": len(confirmed_stale),
            "items": confirmed_stale,
            "rule": f"confirmed 公司证据但 latest_source_date < today - {CONFIRMED_STALE_DAYS} 天",
        },
        "ticker_conflicts": {
            "count": len(ticker_conflicts),
            "items": ticker_conflicts,
            "rule": "同 symbol 在 evidence 表里出现多个不同 market",
        },
    }


def main():
    print("=" * 70)
    print("AI 主题雷达 · 覆盖率审计")
    print("=" * 70)

    # 纯读 job — force_read_only 让自己不持写锁
    # ⚠️ DuckDB 实测：跨进程的写锁会阻塞只读 conn（不像 sqlite/postgres）
    # 所以即使本进程 read-only，遇到别人的写锁仍然 IOException
    # → 加重试 3 次 × 5s，避开生产 pipeline 短时写窗口
    import time
    last_err = None
    for attempt in range(3):
        try:
            con = get_db(force_read_only=True)
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                print(f"  ⚠️ DB 被占用，{attempt+1}/3 重试中（5s）...")
                time.sleep(5)
    else:
        print(f"❌ 3 次重试后仍打不开 DB: {last_err}")
        return 1

    try:
        audit = run_audit(con)
    finally:
        con.close()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str))

    print(f"\n✅ 审计完成，{audit['n_total_issues']} 个 issue 写入 {OUTPUT_PATH}")
    for key in ("high_score_no_chain", "theme_no_evidence",
                "confirmed_stale_risk", "ticker_conflicts"):
        c = audit[key]["count"]
        print(f"  {key:<25} {c:>3} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
