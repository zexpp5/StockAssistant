"""灌入 AI 主题雷达证据系统的种子数据源（Phase 0）。

文档：docs/V2/AI主题雷达_产品定位.md §十三

设计原则：
  1. 每条种子必须有 source_url，否则 assert 拒绝
  2. 灌完后逐条 HTTP HEAD 验证 reachable（10s timeout）
  3. 失败的不删除条目，只把 last_check_status / last_check_http 写进去
     —— 用户能在 dashboard 看到"数据源 N 个，活的 M 个"
  4. 幂等：重复跑会 UPSERT，不会重复插入

Phase 0 验收（文档 §十一）：
  - sources 表种子全部入库
  - 每条 source_url 都能 HEAD 拿到 200/3xx，或者明确的失败码
  - 至少 80% 种子源可达
"""
from __future__ import annotations

import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import get_db  # noqa: E402  # type: ignore


# 文档 §十三 首批 16 个种子源
SEEDS: list[dict] = [
    {
        "source_id": "sec_edgar_api",
        "source_name": "SEC EDGAR APIs",
        "source_tier": "A",
        "source_type": "regulator",
        "source_url": "https://www.sec.gov/edgar/sec-api-documentation",
        "update_cadence": "realtime",
        "license_note": "公共域；SEC 要求 User-Agent 标识",
    },
    # 2026-06-01 confirmed 工程：拆 10-K / 8-K 两路径，让 aggregate_tags 算 2 独立 source
    {
        "source_id": "sec_edgar_10k",
        "source_name": "SEC EDGAR 10-K Filings",
        "source_tier": "A",
        "source_type": "regulator",
        "source_url": "https://www.sec.gov/edgar/sec-api-documentation",
        "update_cadence": "annual",
        "license_note": "10-K = 年度审计后申报；公司主营披露",
    },
    {
        "source_id": "sec_edgar_8k",
        "source_name": "SEC EDGAR 8-K Filings",
        "source_tier": "A",
        "source_type": "regulator",
        "source_url": "https://www.sec.gov/edgar/sec-api-documentation",
        "update_cadence": "event_triggered",
        "license_note": "8-K = 重大事件 8 工作日内申报；项目里程碑/合同/产能",
    },
    {
        "source_id": "doe_lbnl_data_center_2024",
        "source_name": "LBNL 2024 U.S. Data Center Energy Usage Report",
        "source_tier": "A",
        "source_type": "government",
        "source_url": "https://buildings.lbl.gov/publications/2024-lbnl-data-center-energy-usage-report",
        "update_cadence": "annual",
        "license_note": "公共域（美国政府报告）",
    },
    {
        "source_id": "doe_data_center_demand_2024",
        "source_name": "DOE Data Center Electricity Demand Release",
        "source_tier": "A",
        "source_type": "government",
        "source_url": "https://www.energy.gov/articles/doe-releases-new-report-evaluating-increase-electricity-demand-data-centers",
        "update_cadence": "annual",
        "license_note": "公共域",
    },
    {
        "source_id": "usgs_mcs_2026",
        "source_name": "USGS Mineral Commodity Summaries 2026",
        "source_tier": "A",
        "source_type": "government",
        "source_url": "https://pubs.usgs.gov/publication/mcs2026",
        "update_cadence": "annual",
        "license_note": "公共域",
    },
    {
        "source_id": "usgs_mcs_2026_data",
        "source_name": "USGS MCS 2026 Data Release",
        "source_tier": "A",
        "source_type": "government",
        # 文档原 URL（data.usgs.gov 路径）已失效 → 2026-05-29 改用 DOI 永久链接
        # 来源：从 https://pubs.usgs.gov/publication/mcs2026 主页正文里"Data Release"链接抓出
        "source_url": "https://doi.org/10.5066/P1WKQ63T",
        "update_cadence": "annual",
        "license_note": "公共域",
    },
    {
        "source_id": "iea_critical_minerals_explorer",
        "source_name": "IEA Critical Minerals Data Explorer",
        "source_tier": "B",  # 文档写 "A/B"，按更严格的 B 入
        "source_type": "industry",
        "source_url": "https://www.iea.org/data-and-statistics/data-tools/critical-minerals-data-explorer",
        "update_cadence": "annual",
        "license_note": "IEA 数据使用条款",
    },
    {
        "source_id": "eia_nuclear_data",
        "source_name": "EIA Nuclear & Uranium Data",
        "source_tier": "A",
        "source_type": "government",
        "source_url": "https://www.eia.gov/nuclear/data/",
        "update_cadence": "monthly",
        "license_note": "公共域",
    },
    {
        "source_id": "eia_uranium_marketing",
        "source_name": "EIA Uranium Marketing Annual Report",
        "source_tier": "A",
        "source_type": "government",
        "source_url": "https://www.eia.gov/uranium/marketing/",
        "update_cadence": "annual",
        "license_note": "公共域",
    },
    {
        "source_id": "wna_uranium_supply",
        "source_name": "World Nuclear Association Supply of Uranium",
        "source_tier": "B",
        "source_type": "industry",
        "source_url": "https://world-nuclear.org/information-library/nuclear-fuel-cycle/uranium-resources/supply-of-uranium",
        "update_cadence": "annual",
        "license_note": "需引用归属",
    },
    {
        "source_id": "wna_uranium_markets",
        "source_name": "World Nuclear Association Uranium Markets",
        "source_tier": "B",
        "source_type": "industry",
        "source_url": "https://world-nuclear.org/information-library/nuclear-fuel-cycle/uranium-resources/uranium-markets",
        "update_cadence": "annual",
        "license_note": "需引用归属",
    },
    {
        "source_id": "doe_advanced_smr",
        "source_name": "DOE Advanced SMRs",
        "source_tier": "A",
        "source_type": "government",
        "source_url": "https://www.energy.gov/ne/nuclear-reactor-technologies/small-modular-nuclear-reactors",
        "update_cadence": "irregular",
        "license_note": "公共域",
    },
    {
        "source_id": "doe_ardp",
        "source_name": "DOE Advanced Reactor Demonstration Projects",
        "source_tier": "A",
        "source_type": "government",
        "source_url": "https://www.energy.gov/ne/advanced-reactor-demonstration-projects",
        "update_cadence": "irregular",
        "license_note": "公共域",
    },
    {
        "source_id": "nrc_advanced_reactors",
        "source_name": "NRC Advanced Reactors",
        "source_tier": "A",
        "source_type": "regulator",
        "source_url": "https://www.nrc.gov/reactors/new-reactors/advanced",
        "update_cadence": "monthly",
        "license_note": "公共域",
    },
    {
        "source_id": "nrc_advanced_reactor_highlights",
        "source_name": "NRC Advanced Reactor Highlights",
        "source_tier": "A",
        "source_type": "regulator",
        "source_url": "https://www.nrc.gov/reactors/new-reactors/advanced/highlights/2026",
        "update_cadence": "quarterly",
        "license_note": "公共域",
    },
    {
        "source_id": "common_crawl",
        "source_name": "Common Crawl",
        "source_tier": "B",
        "source_type": "open_dataset",
        "source_url": "https://commoncrawl.org/",
        "update_cadence": "monthly",
        "license_note": "公开数据集",
    },
    {
        "source_id": "common_crawl_get_started",
        "source_name": "Common Crawl Get Started",
        "source_tier": "B",
        "source_type": "open_dataset",
        "source_url": "https://commoncrawl.org/get-started",
        "update_cadence": "irregular",
        "license_note": "公开数据集",
    },
]


# 主题 ↔ 数据源映射（文档 §七.2 每个主题下列出的"主要数据源"）
# 一个 source 可服务多个 theme（如 sec_edgar_api 同时给水冷+AI 数据用）
THEME_SOURCE_MAPPING: list[tuple[str, str, str]] = [
    # liquid_cooling
    ("liquid_cooling", "doe_lbnl_data_center_2024",     "AI 数据中心能耗/冷却背景"),
    ("liquid_cooling", "doe_data_center_demand_2024",   "数据中心电力需求宏观背景"),
    ("liquid_cooling", "sec_edgar_api",                 "公司 filing 关键词扫描（liquid cooling/cold plate/CDU）"),
    # rare_earths
    ("rare_earths", "usgs_mcs_2026",                    "全球稀土产量/储量/供应结构"),
    ("rare_earths", "usgs_mcs_2026_data",               "MCS 2026 结构化数据"),
    ("rare_earths", "iea_critical_minerals_explorer",   "需求情景与关键矿物供需"),
    # uranium
    ("uranium", "eia_nuclear_data",                     "美国核电/铀采购/铀价"),
    ("uranium", "eia_uranium_marketing",                "反应堆运营商加权均价"),
    ("uranium", "wna_uranium_supply",                   "全球铀供需库存"),
    ("uranium", "wna_uranium_markets",                  "铀市场结构/长合同 vs 现货"),
    # smr
    ("smr", "doe_advanced_smr",                         "SMR 政策与技术背景"),
    ("smr", "doe_ardp",                                 "TerraPower/X-energy 示范项目"),
    ("smr", "nrc_advanced_reactors",                    "监管许可/申请状态"),
    ("smr", "nrc_advanced_reactor_highlights",          "最新监管进展"),
    # ai_data
    ("ai_data", "sec_edgar_api",                        "公司 filing 抽 data licensing/training data"),
    ("ai_data", "common_crawl",                         "开放网络训练数据背景（仅宏观）"),
    ("ai_data", "common_crawl_get_started",             "技术数据源访问方式"),
]


# 主题 ↔ chain 映射（chain 来自 chain_metadata.chain）
# direct  = 主题与 chain 主营高度重合
# partial = chain 内部分子类符合主题
# indirect = chain 整体只间接受益
THEME_CHAIN_MAPPING: list[tuple[str, str, str, str]] = [
    # 水冷 / 液冷 → chain_classifier 把 VRT/VST/GEV 都归到"数据中心电力"（粗分类）
    ("liquid_cooling", "数据中心电力", "partial",
     "数据中心电力 chain 同时包含液冷/散热（Vertiv）和电力供给（VST/GEV），主题层只取液冷部分"),
    # 铀 → 核能 / 铀
    ("uranium", "核能 / 铀", "direct",
     "核能 / 铀 chain 主要是铀矿/核燃料/SMR 标的"),
    # SMR → 核能 / 铀（BWXT/CCJ 等同时跨 SMR 和铀）
    ("smr", "核能 / 铀", "partial",
     "核能 / 铀 chain 包含 SMR 反应堆开发商（OKLO/NuScale）和铀矿，主题层只取 SMR 部分"),
    # 稀土：当前 chain 体系还没有"稀缺资源/稀土"独立链；MP 在 chain_metadata 现在被归到稀缺资源
    ("rare_earths", "稀缺资源", "direct",
     "稀缺资源 chain 当前只有 MP Materials 1 只；扩 universe 后会变多"),
    # AI 数据：当前 chain 体系没有专门类目；常和"互联网/云"重叠（Reddit/数据公司）
    # 暂不映射 — 这是诚实的"data gap"，主题卡会显示"chain 未覆盖"
]


def _assert_seeds_well_formed(seeds: list[dict]) -> None:
    """文档 §十二 反误导规则：没有 source_url 不能入库。"""
    required = ("source_id", "source_name", "source_tier", "source_type", "source_url")
    valid_tiers = {"A", "B", "C"}
    for s in seeds:
        for k in required:
            assert s.get(k), f"种子 {s.get('source_id')!r} 缺字段 {k}"
        assert s["source_tier"] in valid_tiers, f"非法 tier {s['source_tier']}"
        assert s["source_url"].startswith(("http://", "https://")), \
            f"source_url 必须是 http(s)：{s['source_url']}"
    # source_id 不能重复
    ids = [s["source_id"] for s in seeds]
    assert len(set(ids)) == len(ids), "source_id 有重复"


def _head_check(url: str, timeout: float = 10.0) -> tuple[str, int | None]:
    """HEAD 验证 URL 可达；失败回退到 GET 头几个字节（有些站不接 HEAD）。

    Returns:
        (status, http_code)  status ∈ {ok, http_404, http_5xx, timeout, network_err}
    """
    # SEC 要求 User-Agent 必须含可联系 email；其它站接受通用 UA。
    # https://www.sec.gov/os/accessing-edgar-data
    headers = {
        "User-Agent": "StockAssistant Research lance7in@gmail.com",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = resp.status
                if 200 <= code < 400:
                    return "ok", code
                return ("http_404" if code == 404 else "http_5xx" if code >= 500 else f"http_{code}", code)
        except urllib.error.HTTPError as e:
            code = e.code
            # 405 = method not allowed → 重试 GET
            if code == 405 and method == "HEAD":
                continue
            return ("http_404" if code == 404 else "http_5xx" if code >= 500 else f"http_{code}", code)
        except (urllib.error.URLError, TimeoutError) as e:
            if "timed out" in str(e).lower():
                return "timeout", None
            # HEAD 失败再试 GET
            if method == "HEAD":
                continue
            return "network_err", None
        except Exception:
            if method == "HEAD":
                continue
            return "network_err", None
    return "network_err", None


def upsert_seeds(con) -> int:
    """灌入种子源，幂等。返回插入/更新的行数。"""
    n = 0
    for s in SEEDS:
        con.execute("""
            INSERT INTO ai_theme_evidence_sources
              (source_id, source_name, source_tier, source_type, source_url,
               update_cadence, license_note, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, TRUE)
            ON CONFLICT (source_id) DO UPDATE SET
              source_name = excluded.source_name,
              source_tier = excluded.source_tier,
              source_type = excluded.source_type,
              source_url  = excluded.source_url,
              update_cadence = excluded.update_cadence,
              license_note = excluded.license_note
        """, [
            s["source_id"], s["source_name"], s["source_tier"], s["source_type"],
            s["source_url"], s.get("update_cadence"), s.get("license_note"),
        ])
        n += 1
    return n


def upsert_theme_mapping(con) -> int:
    """灌入主题 ↔ 数据源映射，幂等。每条映射的 source_id 必须先存在于 sources 表。"""
    # 校验：每个映射的 source_id 都存在
    known = {r[0] for r in con.execute("SELECT source_id FROM ai_theme_evidence_sources").fetchall()}
    for theme, sid, _ in THEME_SOURCE_MAPPING:
        assert sid in known, f"映射 ({theme}, {sid}) 引用了未注册的 source_id"
    n = 0
    for theme, sid, note in THEME_SOURCE_MAPPING:
        con.execute("""
            INSERT INTO ai_theme_source_mapping (theme, source_id, note)
            VALUES (?, ?, ?)
            ON CONFLICT (theme, source_id) DO UPDATE SET note = excluded.note
        """, [theme, sid, note])
        n += 1
    return n


def upsert_theme_chain_mapping(con) -> int:
    """灌入主题 ↔ chain 映射，幂等。"""
    # 校验：chain 必须在 chain_metadata 出现过（避免拼写错误）
    known_chains = {r[0] for r in con.execute(
        "SELECT DISTINCT chain FROM chain_metadata WHERE chain IS NOT NULL"
    ).fetchall()}
    for theme, chain, rel, _ in THEME_CHAIN_MAPPING:
        if chain not in known_chains:
            print(f"  ⚠️ 主题映射 ({theme}, {chain}) 引用的 chain 在 chain_metadata 中不存在；映射仍入库但不会有 picks 关联")
    n = 0
    for theme, chain, rel, note in THEME_CHAIN_MAPPING:
        con.execute("""
            INSERT INTO ai_theme_chain_mapping (theme, chain, relevance, note)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (theme, chain) DO UPDATE SET
              relevance = excluded.relevance,
              note      = excluded.note
        """, [theme, chain, rel, note])
        n += 1
    return n


def check_and_record(con) -> dict:
    """逐条 HEAD 检查并写回 last_check_*。"""
    sources = con.execute(
        "SELECT source_id, source_url FROM ai_theme_evidence_sources WHERE active = TRUE"
    ).fetchall()
    stats = {"total": len(sources), "ok": 0, "fail": 0, "by_status": {}}
    now = datetime.now()
    for sid, url in sources:
        status, code = _head_check(url)
        if status == "ok":
            stats["ok"] += 1
        else:
            stats["fail"] += 1
        stats["by_status"][status] = stats["by_status"].get(status, 0) + 1
        con.execute("""
            UPDATE ai_theme_evidence_sources
            SET last_checked_at = ?, last_check_status = ?, last_check_http = ?
            WHERE source_id = ?
        """, [now, status, code, sid])
        print(f"  {status:<12} {code or '-':<5} {sid:<35} {url[:80]}")
    return stats


def main() -> int:
    print("=" * 70)
    print("AI 主题雷达 · 数据源种子灌入（Phase 0）")
    print("=" * 70)

    _assert_seeds_well_formed(SEEDS)
    print(f"✅ {len(SEEDS)} 条种子格式校验通过")

    con = get_db()
    try:
        n = upsert_seeds(con)
        print(f"✅ {n} 条种子已 upsert 入 ai_theme_evidence_sources")

        # 分布统计
        tier = dict(con.execute(
            "SELECT source_tier, COUNT(*) FROM ai_theme_evidence_sources GROUP BY source_tier"
        ).fetchall())
        print(f"  Tier 分布: {tier}")

        nm = upsert_theme_mapping(con)
        print(f"✅ {nm} 条主题×数据源映射已 upsert（5 主题 × {nm} 关联）")
        themes = dict(con.execute(
            "SELECT theme, COUNT(*) FROM ai_theme_source_mapping GROUP BY theme"
        ).fetchall())
        print(f"  主题分布: {themes}")

        ncm = upsert_theme_chain_mapping(con)
        print(f"✅ {ncm} 条主题×chain 映射已 upsert")
        chain_map = dict(con.execute(
            "SELECT theme, COUNT(*) FROM ai_theme_chain_mapping GROUP BY theme"
        ).fetchall())
        print(f"  主题-chain 分布: {chain_map}")

        print("\n=== URL 可达性检查 ===")
        stats = check_and_record(con)

        ok_pct = stats["ok"] / stats["total"] * 100 if stats["total"] else 0
        print(f"\n汇总: {stats['ok']}/{stats['total']} 可达 ({ok_pct:.1f}%)")
        print(f"按状态: {stats['by_status']}")

        # 文档 §十一 Phase 0 验收：至少 80% 可达
        if ok_pct < 80:
            print(f"\n⚠️ 可达率 {ok_pct:.1f}% < 80% 阈值；不阻断（可能是临时网络问题），但请人工复核。")
            return 1
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
