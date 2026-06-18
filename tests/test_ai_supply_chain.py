"""AI 产业链覆盖 taxonomy + 缺口扫描测试。

对应需求：docs/V2/2026-06-18_AI产业链覆盖机制与缺口扫描器.md

守住四类规则：
  T1 taxonomy 自洽：每个环节都有代表龙头、min_covered 合法、key 唯一
  T2 anchor 格式：大写、非空、环节内不重复（粗筛"可交易 ticker"格式，真正校验在抓价层）
  T3 覆盖判定纯函数：covered / thin / gap 三态按规则正确
  T4 基线缺口快照：拿真实种子 us_universe 跑，存储=偏薄、光互联=盲区、先进封装=偏薄、
     算力=已覆盖 —— 这条同时是"补桶后是否生效"的 tripwire（补上存储桶后此断言会翻绿，
     提示同步更新基线，这正是我们要的信号）
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from stock_research.core.ai_supply_chain import (  # noqa: E402
    AI_SUPPLY_CHAIN_SEGMENTS,
    COVERED,
    GAP,
    THIN,
    ChainSegment,
    build_coverage_audit_payload,
    compute_chain_coverage,
    coverage_gaps,
    coverage_summary,
)


class TestTaxonomyWellFormed(unittest.TestCase):
    """T1 + T2：taxonomy 本身必须自洽、anchor 格式干净。"""

    def test_every_segment_has_representatives(self):
        for seg in AI_SUPPLY_CHAIN_SEGMENTS:
            self.assertTrue(seg.anchors, f"{seg.key} 没有代表龙头")
            self.assertGreaterEqual(seg.min_covered, 1, f"{seg.key} min_covered 非法")
            self.assertLessEqual(
                seg.min_covered, len(seg.anchors),
                f"{seg.key} min_covered 超过 anchor 数，永远无法 covered",
            )

    def test_segment_keys_unique(self):
        keys = [s.key for s in AI_SUPPLY_CHAIN_SEGMENTS]
        self.assertEqual(len(keys), len(set(keys)), "环节 key 有重复")

    def test_anchor_format_clean(self):
        for seg in AI_SUPPLY_CHAIN_SEGMENTS:
            self.assertEqual(
                len(seg.anchors), len(set(seg.anchors)),
                f"{seg.key} anchor 环节内重复",
            )
            for a in seg.anchors:
                self.assertTrue(a and a.strip(), f"{seg.key} 有空 anchor")
                self.assertEqual(a, a.upper(), f"{seg.key} anchor {a} 非大写")
                self.assertNotIn(" ", a, f"{seg.key} anchor {a} 含空格")


class TestCoveragePure(unittest.TestCase):
    """T3：覆盖判定三态。用合成 segments，与生产 taxonomy 解耦。"""

    SEG = ChainSegment("demo", "演示环节", ("AAA", "BBB", "CCC"), min_covered=2)

    def _status_of(self, universe):
        rows = compute_chain_coverage(universe, segments=[self.SEG])
        return rows[0]

    def test_covered(self):
        row = self._status_of(["AAA", "BBB", "ZZZ"])
        self.assertEqual(row["status"], COVERED)
        self.assertEqual(row["present_count"], 2)
        self.assertEqual(sorted(row["present"]), ["AAA", "BBB"])
        self.assertEqual(row["missing"], ["CCC"])

    def test_thin(self):
        row = self._status_of(["AAA", "ZZZ"])
        self.assertEqual(row["status"], THIN)
        self.assertEqual(row["present_count"], 1)

    def test_gap(self):
        row = self._status_of(["ZZZ", "YYY"])
        self.assertEqual(row["status"], GAP)
        self.assertEqual(row["present_count"], 0)
        self.assertEqual(row["present"], [])

    def test_case_insensitive_and_whitespace(self):
        row = self._status_of([" aaa ", "bbb"])
        self.assertEqual(row["status"], COVERED, "应大小写不敏感、去空格")

    def test_empty_universe_all_gap(self):
        rows = compute_chain_coverage([])
        self.assertTrue(rows, "空宇宙也要返回全部环节，不能崩")
        self.assertTrue(all(r["status"] == GAP for r in rows))


class TestHelpers(unittest.TestCase):

    def test_coverage_gaps_sorted_gap_first(self):
        segs = [
            ChainSegment("g", "全缺", ("X", "Y"), min_covered=2),
            ChainSegment("t", "偏薄", ("A", "Z"), min_covered=2),
            ChainSegment("c", "已覆盖", ("A", "B"), min_covered=2),
        ]
        universe = ["A", "B"]  # g=gap(X,Y都缺), t=thin(只命中A缺Z), c=covered(A,B都中)
        gaps = coverage_gaps(universe, segments=segs)
        self.assertEqual([g["key"] for g in gaps], ["g", "t"], "gap 应排在 thin 前，covered 不出现")

    def test_summary_counts(self):
        s = coverage_summary(["NVDA", "AMD", "AVGO", "MRVL", "ARM", "QCOM", "TSM"])
        self.assertEqual(s["total"], len(AI_SUPPLY_CHAIN_SEGMENTS))
        self.assertEqual(s["covered"] + s["thin"] + s["gap"], s["total"])

    def test_audit_payload_count_is_segment_issues_not_stocks(self):
        segs = [
            ChainSegment("thin_one", "偏薄环节", ("AAA", "BBB"), min_covered=2),
            ChainSegment("gap_one", "盲区环节", ("CCC", "DDD"), min_covered=2),
            ChainSegment("covered_one", "已覆盖环节", ("EEE", "FFF"), min_covered=2),
        ]
        payload = build_coverage_audit_payload(
            ["AAA", "EEE", "FFF"],
            universe_scope="unit-test",
            segments=segs,
        )
        self.assertEqual(payload["count"], 2, "count 应是 thin+gap 环节数，不是 missing 股票数")
        self.assertEqual(payload["summary"]["thin"], 1)
        self.assertEqual(payload["summary"]["gap"], 1)
        self.assertEqual(payload["universe_scope"], "unit-test")
        self.assertIn("不是买入/入池清单", payload["rule"])


class TestSeedCoverageBaseline(unittest.TestCase):
    """T4：用真实 us_universe 种子跑，固化已补齐的关键链覆盖。"""

    def _coverage_by_key(self):
        from stock_research.core.us_universe import US_AI_TECH_UNIVERSE

        syms = [row["ticker"] for row in US_AI_TECH_UNIVERSE]
        return {r["key"]: r for r in compute_chain_coverage(syms)}

    def test_compute_chain_covered(self):
        cov = self._coverage_by_key()
        self.assertEqual(cov["ai_compute_chips"]["status"], COVERED, "算力链应已覆盖")

    def test_storage_optical_packaging_are_now_covered(self):
        cov = self._coverage_by_key()
        self.assertEqual(cov["memory_storage"]["status"], COVERED, "存储链补桶后应已覆盖")
        for sym in ("MU", "WDC", "STX", "SNDK", "SIMO"):
            self.assertIn(sym, cov["memory_storage"]["present"])
        self.assertEqual(
            cov["optical_interconnect"]["status"], COVERED,
            "光互联补桶后不应再是盲区；若失败说明 US universe 光模块桶被删或 ticker 改了",
        )
        self.assertEqual(cov["advanced_packaging"]["status"], COVERED)

    def test_storage_optical_packaging_no_longer_listed_as_gaps(self):
        from stock_research.core.us_universe import US_AI_TECH_UNIVERSE

        syms = [row["ticker"] for row in US_AI_TECH_UNIVERSE]
        gap_keys = {g["key"] for g in coverage_gaps(syms)}
        self.assertNotIn("memory_storage", gap_keys)
        self.assertNotIn("optical_interconnect", gap_keys)
        self.assertNotIn("advanced_packaging", gap_keys)

    def test_new_bucket_symbols_get_ai_identity_and_chain_tags(self):
        from stock_research.core.chain_classifier import classify_one
        from stock_research.core.tech_growth_layers import classify_tech_growth_layer

        samples = [
            ("WDC", "Western Digital", "memory_storage", "NAND/HDD/SSD 控制器"),
            ("COHR", "Coherent", "ai_network", "光模块/CPO"),
            ("ASX", "ASE Technology Holding", "advanced_packaging", "先进封装/测试设备"),
        ]
        for symbol, name, expected_secondary, expected_role in samples:
            with self.subTest(symbol=symbol):
                layer = classify_tech_growth_layer(
                    market="US",
                    symbol=symbol,
                    source="unit_test",
                    theme="AI supply chain",
                    industry="Semiconductors",
                    name=name,
                )
                self.assertEqual(layer.primary_layer, "ai_core")
                self.assertIn(expected_secondary, layer.secondary_layers)

                tag = classify_one(name, "AI supply chain", "Semiconductors")
                self.assertEqual(tag.chain, "AI 算力")
                self.assertEqual(tag.chain_role, expected_role)


if __name__ == "__main__":
    unittest.main()
