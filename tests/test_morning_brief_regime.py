from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from stock_research.jobs import morning_brief  # noqa: E402


class TestMorningBriefRegimeAdvice(unittest.TestCase):
    def test_high_defense_with_passing_gates_is_not_framed_as_pipeline_failure(self):
        advice = morning_brief._regime_advice_text("HIGH", "HIGH", "PASS", "PASS")

        self.assertIn("防御 HIGH", advice)
        self.assertIn("生产验收已通过", advice)
        self.assertNotIn("修验收", advice)
        self.assertNotIn("FAIL 项", advice)

    def test_gate_failures_are_called_out_as_failures(self):
        advice = morning_brief._regime_advice_text("HIGH", "LOW", "FAIL", "PASS")

        self.assertIn("质量闸门", advice)
        self.assertIn("FAIL 项", advice)


if __name__ == "__main__":
    unittest.main()
