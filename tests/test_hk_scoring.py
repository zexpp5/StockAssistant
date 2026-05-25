"""Shared HK scoring helper tests."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from stock_research.core.hk_scoring import hk_grade_label, score_hk_entries


class HKScoringTest(unittest.TestCase):
    def test_single_entry_uses_production_formula_and_thresholds(self):
        entry = SimpleNamespace(
            code="9992.HK",
            sector="",
            f_score_norm=6 / 9,
            momentum_12_1=-18.26,
            reversal_1m=4.39,
            south_score=0.5,
            data_quality="partial",
        )
        entries, selected, _cutoff, _skipped = score_hk_entries([entry], mode="tertile", top_k=12)

        self.assertEqual(entries[0].code, "9992.HK")
        self.assertAlmostEqual(entries[0].composite, 0.5667, places=4)
        self.assertEqual(hk_grade_label(entries[0]), "⭐ 关注")
        self.assertEqual([x.code for x in selected], ["9992.HK"])

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
