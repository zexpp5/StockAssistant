import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.tools import strategy_tuning_proposal as proposal


class TestStrategyTuningProposal(unittest.TestCase):
    def _diagnosis_payload(self):
        return {
            "strategy_version_filter": "strategy_test",
            "horizon_filter": "1d",
            "summary": {"sample_count": 60, "negative_alpha_count": 54},
            "coverage_summary": [
                {"market": "CN", "horizon": "1d", "calendar_due": 60, "reviewed": 60, "pending_data_ready": 0},
                {"market": "US", "horizon": "1d", "calendar_due": 60, "reviewed": 0, "pending_data_ready": 60},
            ],
            "market_summary": [
                {"market": "CN", "horizon": "1d", "n": 60, "win_rate": 10.0, "avg_alpha_pct": -1.5},
            ],
            "rank_bucket_summary": [
                {"market": "CN", "horizon": "1d", "rank_bucket": "top_1_5", "avg_alpha_pct": -1.2},
            ],
            "factor_diagnostics": [
                {"market": "CN", "horizon": "1d", "factor": "reversal", "reason": "reversal 高分组偏弱"},
                {"market": "CN", "horizon": "1d", "factor": "valuation", "reason": "valuation 高分组偏弱"},
            ],
            "risk_flag_summary": [
                {
                    "market": "CN",
                    "horizon": "1d",
                    "risk_flag": "MOMENTUM_REUSED_RECENT_V2_SNAPSHOT",
                    "n": 12,
                    "avg_alpha_pct": -1.7,
                },
            ],
        }

    def test_build_proposal_keeps_us_pending_and_degrades_negative_market(self):
        with patch.object(proposal.diagnosis, "build_report", return_value=self._diagnosis_payload()):
            payload = proposal.build_proposal(strategy_version="strategy_test", horizon="1d")

        self.assertEqual(payload["status"], "SHADOW_ONLY")
        market_actions = {row["market"]: row for row in payload["market_actions"]}
        self.assertEqual(market_actions["CN"]["status"], "degraded")
        self.assertEqual(market_actions["CN"]["portfolio_multiplier"], 0.35)
        self.assertEqual(market_actions["US"]["status"], "evidence_pending")
        self.assertEqual(market_actions["US"]["portfolio_multiplier"], 1.0)
        factors = {(row["market"], row["factor"]): row for row in payload["factor_actions"]}
        self.assertEqual(factors[("CN", "reversal")]["action"], "reduce_weight_until_multihorizon_confirms")
        self.assertEqual(factors[("CN", "valuation")]["action"], "reduce_weight_and_require_confirmation")
        self.assertEqual(payload["gate_actions"][0]["action"], "demote_buy_to_watch_or_score_haircut")


if __name__ == "__main__":
    unittest.main()
