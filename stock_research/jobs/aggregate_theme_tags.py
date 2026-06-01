"""把 ai_theme_company_evidence 聚合成 ai_theme_company_tags（文档 §九）。

严格状态规则（文档 §九）：
  confirmed     至少 2 个独立来源，至少 1 个 A 类，最近 180 天有新证据
  candidate     有线索但不满足 confirmed
  stale         曾 confirmed 但最近 180 天无新证据（已被 SEC fetcher 标了；这里只承接）
  needs_review  ticker 缺失或主题路径不清
  rejected      人工 reviewer 拒绝（这里只承接 evidence 表里的 rejected）

evidence_score 满分 100（§九）：
  source_quality_score      0-25  来源等级
  + theme_directness_score  0-25  此 PoC 用 candidate=10 / confirmed=18 简化
  + business_materiality    0-20  此 PoC 留 0，等 filing 解析后填
  + recency_score           0-15  source_date 距今越近越高
  + cross_validation_score  0-15  来源多样性
  - risk_penalty            0-30  此 PoC 留 0

反误导：
  - 只有单一来源（即使 A 类）→ 不能 confirmed
  - 全部 C 类 → 不能 confirmed
  - 没 source_date → 无法判断 recency → needs_review
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import get_db  # noqa: E402  # type: ignore


CONFIRMED_MIN_SOURCES = 2
CONFIRMED_MAX_AGE_DAYS = 180


def _recency_score(latest: date | None, today: date) -> float:
    if not latest:
        return 0.0
    days = (today - latest).days
    if days <= 30:
        return 15.0
    if days <= 90:
        return 10.0
    if days <= 180:
        return 5.0
    return 0.0


def _source_quality_score(tiers: list[str]) -> float:
    if "A" in tiers:
        return 25.0
    if "B" in tiers:
        return 12.0
    return 3.0


def _cross_validation_score(n_distinct_sources: int) -> float:
    if n_distinct_sources >= 3:
        return 15.0
    if n_distinct_sources == 2:
        return 10.0
    return 0.0


def _theme_directness_score(status: str) -> float:
    # PoC 简化：仅用 status 反推；后续接 filing 解析后改成业务直接性评分
    if status == "confirmed":
        return 18.0
    if status == "candidate":
        return 10.0
    return 3.0


def aggregate_tags(con) -> dict:
    """
    遍历 evidence，按 (theme, symbol) 聚合，落 ai_theme_company_tags 表。
    返回 stat dict。
    """
    today = date.today()

    # 拉所有非 rejected 的 evidence
    rows = con.execute("""
        SELECT theme, market, symbol, company_name,
               source_id, source_tier, source_date, evidence_status
        FROM ai_theme_company_evidence
        WHERE evidence_status != 'rejected'
          AND symbol IS NOT NULL
    """).fetchall()

    # 按 (theme, symbol) 聚合
    buckets: dict[tuple, dict] = {}
    for theme, market, sym, name, sid, tier, sdate, est in rows:
        key = (theme, sym)
        if key not in buckets:
            buckets[key] = {
                "theme": theme,
                "market": market,
                "symbol": sym,
                "company_name": name,
                "source_ids": set(),
                "tiers": [],
                "source_dates": [],
                "evidence_statuses": [],
            }
        b = buckets[key]
        b["source_ids"].add(sid)
        b["tiers"].append(tier)
        if sdate:
            try:
                b["source_dates"].append(
                    date.fromisoformat(sdate) if isinstance(sdate, str) else sdate
                )
            except Exception:
                pass
        b["evidence_statuses"].append(est)

    # 计算每只标的 final tag
    upsert_count = 0
    stats = defaultdict(int)
    con.execute("DELETE FROM ai_theme_company_tags")  # 全量覆盖
    for (theme, sym), b in buckets.items():
        tiers = b["tiers"]
        n_distinct = len(b["source_ids"])
        n_a = sum(1 for t in tiers if t == "A")
        n_b = sum(1 for t in tiers if t == "B")
        n_c = sum(1 for t in tiers if t == "C")
        latest_date = max(b["source_dates"]) if b["source_dates"] else None
        days_old = (today - latest_date).days if latest_date else None

        # 状态判定
        # rejected 已在 query 排除；stale 是 evidence 表自己标的，承接
        all_stale = all(s == "stale" for s in b["evidence_statuses"])
        if all_stale:
            status = "stale"
        elif not b["market"] or not b["company_name"]:
            status = "needs_review"
        elif not latest_date:
            status = "needs_review"
        elif (n_distinct >= CONFIRMED_MIN_SOURCES
              and n_a >= 1
              and days_old is not None
              and days_old <= CONFIRMED_MAX_AGE_DAYS):
            status = "confirmed"
        else:
            status = "candidate"

        # 评分
        score = (
            _source_quality_score(tiers)
            + _theme_directness_score(status)
            + 0  # business_materiality PoC 留空
            + _recency_score(latest_date, today)
            + _cross_validation_score(n_distinct)
        )

        rationale_parts = [f"{n_distinct} 个独立来源 ({n_a}A/{n_b}B/{n_c}C)"]
        if latest_date:
            rationale_parts.append(f"最近证据 {latest_date} ({days_old} 天前)")
        if status == "confirmed":
            rationale_parts.append("满足 §九 confirmed: ≥2 源 + ≥1 A + ≤180 天")
        elif status == "candidate":
            missing = []
            if n_distinct < CONFIRMED_MIN_SOURCES:
                missing.append(f"<{CONFIRMED_MIN_SOURCES} 来源")
            if n_a < 1:
                missing.append("无 A 类")
            if days_old and days_old > CONFIRMED_MAX_AGE_DAYS:
                missing.append(f">{CONFIRMED_MAX_AGE_DAYS} 天")
            rationale_parts.append("未达 confirmed：" + ", ".join(missing))
        elif status == "stale":
            rationale_parts.append("evidence 全部 stale")
        elif status == "needs_review":
            rationale_parts.append("ticker/market 缺失或主题路径不清")
        rationale = " · ".join(rationale_parts)

        con.execute("""
            INSERT INTO ai_theme_company_tags
              (theme, market, symbol, company_name, theme_role, ai_strength,
               evidence_status, evidence_score,
               source_count_a, source_count_b, source_count_c,
               latest_source_date, rationale)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            theme, b["market"] or "?", sym, b["company_name"],
            None, None,  # theme_role / ai_strength: 留待后续
            status, round(score, 1),
            n_a, n_b, n_c,
            latest_date, rationale,
        ])
        upsert_count += 1
        stats[status] += 1

    return {
        "n_tags": upsert_count,
        "by_status": dict(stats),
    }


def main():
    print("=" * 70)
    print("ai_theme_company_tags 聚合（文档 §九）")
    print("=" * 70)

    con = get_db()
    try:
        stat = aggregate_tags(con)
        print(f"\n✅ 聚合完成: {stat['n_tags']} 行入 ai_theme_company_tags")
        print(f"  状态分布: {stat['by_status']}")

        # 反误导守门：confirmed 必须 ≥1 A 类 + ≥2 来源
        bad_confirmed = con.execute("""
            SELECT theme, symbol, source_count_a, source_count_b, source_count_c, rationale
            FROM ai_theme_company_tags
            WHERE evidence_status = 'confirmed'
              AND (source_count_a < 1 OR (source_count_a + source_count_b + source_count_c) < 2)
        """).fetchall()
        if bad_confirmed:
            print(f"\n❌ 检测到不合规 confirmed (违反 §九):")
            for r in bad_confirmed:
                print(f"  {r}")
            return 1

        # 列样本
        sample = con.execute("""
            SELECT theme, symbol, evidence_status, evidence_score,
                   source_count_a, source_count_b, latest_source_date,
                   SUBSTR(rationale, 1, 80)
            FROM ai_theme_company_tags ORDER BY evidence_score DESC LIMIT 10
        """).fetchall()
        if sample:
            print(f"\n样本（按 evidence_score 降序前 10）:")
            for r in sample:
                print(f"  {r[0]:<14} {r[1]:<10} {r[2]:<13} score={r[3]:<5} A={r[4]} B={r[5]} latest={r[6]}")
                print(f"    {r[7]}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
