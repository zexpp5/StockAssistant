"""US equity Altman/Beneish risk flag helpers."""
from __future__ import annotations

import unittest

from stock_research.core.us_risk_flags import (
    build_us_equity_risk_flags,
    build_us_equity_risk_flags_from_fundamental,
)


class UsRiskFlagsTest(unittest.TestCase):
    def test_empty_when_no_data(self):
        self.assertEqual(build_us_equity_risk_flags(None, None), [])

    def test_altman_distress(self):
        flags = build_us_equity_risk_flags({"z_score": 1.5}, None)
        self.assertEqual(len(flags), 1)
        self.assertIn("Altman", flags[0])

    def test_beneish_high_uses_adjusted_score(self):
        flags = build_us_equity_risk_flags(
            None,
            {"risk_level": "high", "m_score_adjusted": -1.5},
        )
        self.assertEqual(len(flags), 1)
        self.assertIn("Beneish", flags[0])

    def test_skips_error_payloads(self):
        self.assertEqual(
            build_us_equity_risk_flags({"error": "no data"}, {"error": "no data"}),
            [],
        )

    def test_from_fundamental_row_shape(self):
        flags = build_us_equity_risk_flags_from_fundamental({
            "altman": {"z_score": 1.0},
            "beneish": {"risk_level": "high", "m_score_adjusted": -1.0},
        })
        self.assertEqual(len(flags), 2)


if __name__ == "__main__":
    unittest.main()
