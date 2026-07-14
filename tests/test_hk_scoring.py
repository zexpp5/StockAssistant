"""Shared HK scoring helper tests."""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

from stock_research.core.hk_scoring import (
    HK_LEGACY_FACTOR_WEIGHTS,
    HK_QUALITY_HEAVY_WEIGHTS,
    hk_grade_label,
    hk_production_weights,
    score_hk_entries,
)


def _sample_entry():
    return SimpleNamespace(
        code="9992.HK",
        sector="",
        f_score_norm=6 / 9,
        momentum_12_1=-18.26,
        reversal_1m=4.39,
        south_score=0.5,
        data_quality="partial",
    )


class HKScoringTest(unittest.TestCase):
    def test_single_entry_uses_production_formula_and_thresholds(self):
        # 2026-07-14 生产默认已切 quality_heavy（f_score 重仓、momentum 压低）。
        # 单只票下 momentum/reversal 分位=0.5，f_score=0.6667；
        # 归一权重 f_score 8/15, momentum 2/15, reversal 5/15 → composite 0.5889。
        entries, selected, _cutoff, _skipped = score_hk_entries([_sample_entry()], mode="tertile", top_k=12)

        self.assertEqual(entries[0].code, "9992.HK")
        self.assertAlmostEqual(entries[0].composite, 0.5889, places=4)
        self.assertEqual(hk_grade_label(entries[0]), "⭐ 关注")
        self.assertEqual([x.code for x in selected], ["9992.HK"])

    def test_production_default_is_quality_heavy(self):
        self.assertEqual(hk_production_weights(), dict(HK_QUALITY_HEAVY_WEIGHTS))
        self.assertAlmostEqual(sum(HK_QUALITY_HEAVY_WEIGHTS.values()), 1.0, places=9)

    def test_rollback_flag_restores_legacy_weights(self):
        old = os.environ.get("HK_QUALITY_HEAVY_ACTIVE")
        try:
            os.environ["HK_QUALITY_HEAVY_ACTIVE"] = "0"
            self.assertEqual(hk_production_weights(), dict(HK_LEGACY_FACTOR_WEIGHTS))
            # 显式传老权重仍复现旧 composite（打分机制未变，只是默认权重换了）。
            entries, _s, _c, _k = score_hk_entries(
                [_sample_entry()], mode="tertile", top_k=12,
                factor_weights=HK_LEGACY_FACTOR_WEIGHTS,
            )
            self.assertAlmostEqual(entries[0].composite, 0.5667, places=4)
        finally:
            if old is None:
                os.environ.pop("HK_QUALITY_HEAVY_ACTIVE", None)
            else:
                os.environ["HK_QUALITY_HEAVY_ACTIVE"] = old

    def test_data_quality_fail_is_not_selected(self):
        entry = SimpleNamespace(
            code="0000.HK",
            sector="",
            f_score_norm=1.0,
            momentum_12_1=50.0,
            reversal_1m=10.0,
            south_score=0.5,
            data_quality="fail",
        )
        entries, selected, _cutoff, _skipped = score_hk_entries([entry], mode="tertile", top_k=12)

        self.assertEqual(entries[0].composite, -0.25)
        self.assertEqual(selected, [])


if __name__ == "__main__":
    unittest.main()
