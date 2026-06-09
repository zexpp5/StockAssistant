import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.tools import build_strategy_validation_v2 as validation


class TestStrategyValidationV2(unittest.TestCase):
    def test_negative_alpha_large_sample_requires_review(self):
        conclusion, action = validation._conclusion(
            sample_size=60,
            avg_alpha=-1.55,
            win_rate=10.0,
        )

        self.assertEqual(action, "review_weights")
        self.assertIn("策略承压", conclusion)

    def test_validation_status_warns_when_reports_are_not_positive(self):
        status, reason = validation._validation_status_from_reports([
            {"recommended_action": "review_weights"},
        ])

        self.assertEqual(status, "WARN")
        self.assertIn("NEGATIVE_ALPHA_OR_LOW_HIT_RATE", reason)

    def test_validation_status_passes_only_positive_reports(self):
        status, reason = validation._validation_status_from_reports([
            {"recommended_action": "continue"},
        ])

        self.assertEqual(status, "PASS")
        self.assertEqual(reason, "strategy_positive_alpha_available")

    def test_policy_gate_blocks_small_sample_from_upgrade(self):
        gate = validation._policy_gate({
            "n": 19,
            "median_alpha_pct": 3.0,
            "win_rate_pct": 80.0,
            "extreme_winner_dependency": False,
        })

        self.assertEqual(gate, "DISPLAY_ONLY")

    def test_policy_gate_detects_extreme_winner_dependency(self):
        gate = validation._policy_gate({
            "n": 30,
            "median_alpha_pct": -0.1,
            "win_rate_pct": 50.0,
            "extreme_winner_dependency": True,
        })

        self.assertEqual(gate, "OBSERVE_EXTREME_WINNER")

    def test_group_policy_samples_keeps_unknown_bucket(self):
        rows = validation._group_policy_samples([
            {"primary_layer": "ai_core", "alpha_pct": 1.0},
            {"primary_layer": "", "alpha_pct": -1.0},
            {"alpha_pct": 2.0},
        ], "primary_layer")

        values = {row["value"] for row in rows}
        self.assertIn("ai_core", values)
        self.assertIn("unknown", values)


if __name__ == "__main__":
    unittest.main()
