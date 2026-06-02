import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.tools import evaluate_shadow_tuning_run as evaluator


class TestEvaluateShadowTuningRun(unittest.TestCase):
    def _run(self, *, market_status="unchanged", production_eligible=True):
        return {
            "run_id": "shadow_test_1",
            "generated_at": "2026-06-02T20:00:00",
            "proposed_strategy_version": "shadow_test",
            "source_production_run": {"run_id": "rec_test_1"},
            "market_summary": [
                {
                    "market": "CN",
                    "status": market_status,
                    "recommendation_mode": "keep_current",
                    "production_portfolio_eligible_count": 1 if production_eligible else 0,
                },
            ],
            "picks": [
                {
                    "market": "CN",
                    "symbol": "KEEP",
                    "name": "Keep",
                    "original_signal": "buy",
                    "shadow_signal": "buy",
                    "original_rank": 1,
                    "shadow_rank": 1,
                    "demoted": False,
                    "production_portfolio_eligible": production_eligible,
                    "action": "keep",
                },
                {
                    "market": "CN",
                    "symbol": "DROP",
                    "name": "Drop",
                    "original_signal": "buy",
                    "shadow_signal": "watch",
                    "original_rank": 2,
                    "shadow_rank": 2,
                    "demoted": True,
                    "production_portfolio_eligible": False,
                    "action": "demote_to_watch",
                },
            ],
        }

    def test_market_summary_shows_shadow_improvement_and_avoided_loss(self):
        outcomes = {
            ("rec_test_1", "CN", "KEEP", "1d"): {"alpha_pct": 1.0, "return_pct": 2.0},
            ("rec_test_1", "CN", "DROP", "1d"): {"alpha_pct": -2.0, "return_pct": -1.0},
        }

        rows = evaluator.build_market_horizon_summary([self._run()], outcomes, horizons=("1d",))
        row = rows[0]

        self.assertEqual(row["reviewed_original_buy_count"], 2)
        self.assertEqual(row["original_avg_alpha_pct"], -0.5)
        self.assertEqual(row["reviewed_shadow_buy_count"], 1)
        self.assertEqual(row["shadow_avg_alpha_pct"], 1.0)
        self.assertEqual(row["shadow_vs_original_alpha_delta_pct"], 1.5)
        self.assertEqual(row["demoted_avg_alpha_pct"], -2.0)
        self.assertEqual(row["avoided_loss_alpha_pct"], 2.0)

    def test_activation_ready_when_shadow_has_enough_positive_evidence(self):
        outcomes = {
            ("rec_test_1", "CN", "KEEP", "1d"): {"alpha_pct": 1.0, "return_pct": 2.0},
            ("rec_test_1", "CN", "DROP", "1d"): {"alpha_pct": -2.0, "return_pct": -1.0},
        }

        payload = evaluator.build_evidence_payload(
            runs=[self._run(market_status="unchanged", production_eligible=True)],
            outcomes=outcomes,
            horizons=("1d",),
            min_shadow_runs=1,
            min_market_reviewed=1,
            min_coverage_pct=50.0,
            min_hit_rate=40.0,
            primary_horizon="1d",
        )

        self.assertEqual(payload["status"], "READY")
        self.assertEqual(payload["activation_decision"]["blockers"], [])

    def test_activation_blocks_degraded_or_ineligible_shadow(self):
        outcomes = {
            ("rec_test_1", "CN", "KEEP", "1d"): {"alpha_pct": 1.0, "return_pct": 2.0},
            ("rec_test_1", "CN", "DROP", "1d"): {"alpha_pct": -2.0, "return_pct": -1.0},
        }

        payload = evaluator.build_evidence_payload(
            runs=[self._run(market_status="degraded", production_eligible=False)],
            outcomes=outcomes,
            horizons=("1d",),
            min_shadow_runs=1,
            min_market_reviewed=1,
            min_coverage_pct=50.0,
            min_hit_rate=40.0,
            primary_horizon="1d",
        )

        self.assertEqual(payload["status"], "BLOCKED")
        blockers = " ".join(payload["activation_decision"]["blockers"])
        self.assertIn("degraded", blockers)
        self.assertIn("production_portfolio_eligible_count=0", blockers)

    def test_dedupe_keeps_latest_per_source_run_and_strategy(self):
        old = self._run()
        old["run_id"] = "shadow_old"
        old["generated_at"] = "2026-06-02T10:00:00"
        new = self._run()
        new["run_id"] = "shadow_new"
        new["generated_at"] = "2026-06-02T11:00:00"

        rows = evaluator.dedupe_shadow_runs([old, new])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_id"], "shadow_new")


if __name__ == "__main__":
    unittest.main()
