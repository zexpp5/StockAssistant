"""灌入 5 主题宏观指标种子（Phase 1B）。

数据获取方式：通过 WebFetch 从官方 source URL 实时抓取 → 人工记录数值 → 灌入。
每条指标都带 source_url，符合文档 §十二 反误导规则。

⚠️ 重要原则：
  1. 不灌任何"凭印象"的数字
  2. 数值来源必须可追溯到具体 source_url
  3. 同一主题下的多个指标可关联同一 source_id
  4. metric_date 是数据本身的时间锚，不是抓取时间
  5. 抓取时间记在 captured_at（自动）

首版指标（2026-05-29 通过 WebFetch 抓取）：
  uranium    : WNA 年度反应堆铀需求量
  liquid_cooling : DOE/LBNL 美国数据中心 2028 用电量预测（lower + upper）
  ai_data    : Common Crawl 最新月度抓取规模
  smr        : DOE ARDP 项目累计拨款 + 项目数
  rare_earths: ⚠️ 此次 WebFetch 未拿到 PDF 内具体数字，故留空（诚实承认）
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import get_db  # noqa: E402  # type: ignore


# (theme, metric_name, metric_value, metric_unit, source_id, source_url, metric_date)
# metric_date 是数据本身代表的时间（非抓取时间）
METRICS: list[tuple] = [
    # ───── uranium ─────
    ("uranium",
     "年度反应堆铀需求", 67000, "tU/year",
     "wna_uranium_supply",
     "https://world-nuclear.org/information-library/nuclear-fuel-cycle/uranium-resources/supply-of-uranium",
     date(2026, 5, 29)),

    # ───── liquid_cooling（数据中心电力，AI 服务器散热间接需求）─────
    ("liquid_cooling",
     "美国数据中心 2028 用电量预测 (下限)", 325, "TWh",
     "doe_data_center_demand_2024",
     "https://www.energy.gov/articles/doe-releases-new-report-evaluating-increase-electricity-demand-data-centers",
     date(2028, 12, 31)),
    ("liquid_cooling",
     "美国数据中心 2028 用电量预测 (上限)", 580, "TWh",
     "doe_data_center_demand_2024",
     "https://www.energy.gov/articles/doe-releases-new-report-evaluating-increase-electricity-demand-data-centers",
     date(2028, 12, 31)),
    ("liquid_cooling",
     "美国数据中心 2023 用电量基线", 176, "TWh",
     "doe_data_center_demand_2024",
     "https://www.energy.gov/articles/doe-releases-new-report-evaluating-increase-electricity-demand-data-centers",
     date(2023, 12, 31)),
    ("liquid_cooling",
     "数据中心 2028 占美国电网比例 (上限)", 12, "%",
     "doe_data_center_demand_2024",
     "https://www.energy.gov/articles/doe-releases-new-report-evaluating-increase-electricity-demand-data-centers",
     date(2028, 12, 31)),

    # ───── ai_data ─────
    ("ai_data",
     "Common Crawl 2026-05 月度抓取页数", 2.16, "billion pages",
     "common_crawl",
     "https://commoncrawl.org/blog",
     date(2026, 5, 1)),
    ("ai_data",
     "Common Crawl 2026-05 月度抓取未压缩大小", 365.56, "TiB",
     "common_crawl",
     "https://commoncrawl.org/blog",
     date(2026, 5, 1)),

    # ───── smr ─────
    # 注：这两条数据来自 DOE ARDP 主页对 DE-FOA-0002271 的描述。
    # DE-FOA-0002271 是 ARDP 项目的初始 funding announcement，金额 $160M，
    # 对应资助 2 个示范项目（TerraPower Natrium + X-energy Xe-100）。
    # 不要写成"累计拨款" — 后续 TerraPower/X-energy 还有数十亿增补，未计入此项。
    ("smr",
     "DOE ARDP 初始示范项目数 (DE-FOA-0002271)", 2, "个",
     "doe_ardp",
     "https://www.energy.gov/ne/advanced-reactor-demonstration-projects",
     date(2026, 5, 29)),
    ("smr",
     "DOE ARDP DE-FOA-0002271 初始拨款", 160, "USD M",
     "doe_ardp",
     "https://www.energy.gov/ne/advanced-reactor-demonstration-projects",
     date(2026, 5, 29)),

    # ───── rare_earths ─────
    # WebFetch 拉 USGS MCS 2026 主页只拿到 metadata，未能取到 PDF 内具体数字
    # → 严格按 §十二 反误导：宁可留空也不凭印象灌
    # 后续若有 PDF 解析器或人工录入，可补 NdPr 价格、中国份额等
]


def _assert_metric_well_formed(m: tuple) -> None:
    theme, name, val, unit, sid, url, dt = m
    assert theme in {"liquid_cooling", "rare_earths", "uranium", "smr", "ai_data"}, \
        f"非法 theme {theme}"
    assert name and isinstance(name, str), f"metric_name 不能为空: {m}"
    assert val is not None, f"metric_value 不能 None（违反反误导规则）: {m}"
    assert unit and isinstance(unit, str), f"metric_unit 必须填: {m}"
    assert sid and isinstance(sid, str), f"source_id 必须填: {m}"
    assert url and url.startswith(("http://", "https://")), f"source_url 必须是 http(s): {m}"
    assert isinstance(dt, date), f"metric_date 必须是 date: {m}"


def main() -> int:
    print("=" * 70)
    print("AI 主题雷达 · 主题宏观指标灌入（Phase 1B）")
    print("=" * 70)

    for m in METRICS:
        _assert_metric_well_formed(m)
    print(f"✅ {len(METRICS)} 条指标格式校验通过")

    con = get_db()
    try:
        # 校验 source_id 在 sources 表
        known_sources = {r[0] for r in con.execute(
            "SELECT source_id FROM ai_theme_evidence_sources"
        ).fetchall()}
        for m in METRICS:
            assert m[4] in known_sources, f"metric source_id={m[4]} 不在 sources 表"

        # 幂等 upsert
        n = 0
        for theme, name, val, unit, sid, url, dt in METRICS:
            # captured_at 不更新 — 保留首次抓取时间作为审计锚
            con.execute("""
                INSERT INTO ai_theme_topic_metrics
                  (theme, metric_date, metric_name, metric_value, metric_unit, source_id, source_url)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (theme, metric_date, metric_name, source_id) DO UPDATE SET
                  metric_value = excluded.metric_value,
                  metric_unit = excluded.metric_unit,
                  source_url = excluded.source_url
            """, [theme, dt, name, val, unit, sid, url])
            n += 1
        print(f"✅ {n} 条指标已 upsert 入 ai_theme_topic_metrics")

        # 分布统计
        by_theme = dict(con.execute(
            "SELECT theme, COUNT(*) FROM ai_theme_topic_metrics GROUP BY theme"
        ).fetchall())
        print(f"  按主题分布: {by_theme}")
        missing = {"liquid_cooling", "rare_earths", "uranium", "smr", "ai_data"} - set(by_theme)
        if missing:
            print(f"  ⚠️ 主题 {sorted(missing)} 暂无宏观指标（首版诚实留空，避免凭印象灌）")

        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
