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


class TestSeedBaselineGaps(unittest.TestCase):
    """T4：用真实 us_universe 种子跑，固化当前已知缺口（补桶后会翻绿 = tripwire）。"""

    def _coverage_by_key(self):
        from stock_research.core.us_universe import US_AI_TECH_UNIVERSE

        syms = [row["ticker"] for row in US_AI_TECH_UNIVERSE]
        return {r["key"]: r for r in compute_chain_coverage(syms)}

    def test_compute_chain_covered(self):
        cov = self._coverage_by_key()
        self.assertEqual(cov["ai_compute_chips"]["status"], COVERED, "算力链应已覆盖")

    def test_storage_optical_packaging_are_known_gaps(self):
        cov = self._coverage_by_key()
        # 存储：种子里仅 MU → 未覆盖
        self.assertNotEqual(
            cov["memory_storage"]["status"], COVERED,
            "存储链当前应未覆盖；若已补桶请更新此基线断言",
        )
        self.assertIn("MU", cov["memory_storage"]["present"])
        # 光互联：种子里 0 只纯光 → 盲区
        self.assertEqual(
            cov["optical_interconnect"]["status"], GAP,
            "光互联当前应为盲区；若已补桶请更新此基线断言",
        )

    def test_gaps_listed_for_morning_report(self):
        from stock_research.core.us_universe import US_AI_TECH_UNIVERSE

        syms = [row["ticker"] for row in US_AI_TECH_UNIVERSE]
        gap_keys = {g["key"] for g in coverage_gaps(syms)}
        self.assertIn("memory_storage", gap_keys)
        self.assertIn("optical_interconnect", gap_keys)


if __name__ == "__main__":
    unittest.main()
