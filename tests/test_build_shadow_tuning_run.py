import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.tools import build_shadow_tuning_run as shadow


class TestBuildShadowTuningRun(unittest.TestCase):
    def _proposal(self):
        return {
            "proposed_strategy_version": "tech_ai_v2_guarded_shadow_test",
            "status": "SHADOW_ONLY",
            "market_actions": [
                {
                    "market": "CN",
                    "status": "degraded",
                    "portfolio_multiplier": 0.35,
                    "recommendation_mode": "research_only_until_shadow_passes",
                    "reason": "A股 60 个样本平均 alpha -1.5%。",
                },
                {
                    "market": "US",
                    "status": "evidence_pending",
                    "portfolio_multiplier": 1.0,
                    "recommendation_mode": "keep_current_until_alpha_available",
                    "reason": "美股样本缺收盘/基准数据。",
                },
            ],
            "factor_actions": [
                {
                    "market": "CN",
                    "factor": "reversal",
                    "action": "reduce_weight_until_multihorizon_confirms",
                    "proposed_change": "短期反转因子先降权。",
                },
                {
                    "market": "CN",
                    "factor": "valuation",
                    "action": "reduce_weight_and_require_confirmation",
                    "proposed_change": "估值高分必须叠加量价确认。",
                },
            ],
            "gate_actions": [
                {
                    "market": "CN",
                    "risk_flag": "MOMENTUM_REUSED_RECENT_V2_SNAPSHOT",
                    "action": "demote_buy_to_watch_or_score_haircut",
                    "proposed_change": "动量字段回退样本扣分。",
                },
            ],
            "activation_criteria": ["只做 shadow run。"],
        }

    def test_shadow_transform_demotes_flagged_cn_buy_without_changing_us_score(self):
        picks = [
            {
                "market": "CN",
                "symbol": "300001.SZ",
                "name": "CN Test",
                "rank": 1,
                "rating": "buy",
                "signal": "buy",
                "total_score": 66.0,
                "factor_scores": {"reversal": 95.0, "valuation": 92.0, "momentum": 40.0},
                "risk_flags": ["MOMENTUM_REUSED_RECENT_V2_SNAPSHOT"],
                "universe_scope": "system_tech_universe",
                "source_origin": "system_pool",
            },
            {
                "market": "US",
                "symbol": "NVDA",
                "name": "Nvidia",
                "rank": 2,
                "rating": "buy",
                "signal": "buy",
                "total_score": 70.0,
                "factor_scores": {"reversal": 60.0, "valuation": 50.0, "momentum": 70.0},
                "risk_flags": [],
                "universe_scope": "system_tech_universe",
                "source_origin": "system_pool",
            },
        ]

        rows = shadow.apply_shadow_transform(picks, self._proposal())
        by_symbol = {row["symbol"]: row for row in rows}

        cn = by_symbol["300001.SZ"]
        self.assertNotIn(cn["shadow_signal"], shadow.BUY_SIGNALS)
        self.assertIn(cn["action"], {"demote_to_watch", "demote_to_avoid"})
        self.assertLess(cn["shadow_score"], cn["original_score"])
        self.assertEqual(cn["shadow_recommendation_mode"], "research_only_until_shadow_passes")
        self.assertFalse(cn["production_portfolio_eligible"])

        us = by_symbol["NVDA"]
        self.assertEqual(us["shadow_signal"], "buy")
        self.assertEqual(us["shadow_score"], us["original_score"])
        self.assertEqual(us["shadow_recommendation_mode"], "keep_current_until_alpha_available")
        self.assertEqual(us["action"], "evidence_pending")
        self.assertFalse(us["production_portfolio_eligible"])

    def test_build_shadow_run_keeps_source_run_separate_from_shadow_run_id(self):
        source_run = {
            "run_id": "rec_test",
            "run_date": "2026-06-02",
            "strategy_version": "tech_ai_v2",
            "model_version": "test",
            "universe_scope": "system_tech_universe",
            "data_cutoff_at": "2026-06-02T18:00:00",
            "generated_at": "2026-06-02T18:01:00",
            "status": "generated",
        }
        payload = shadow.build_shadow_run(
            proposal=self._proposal(),
            source_run=source_run,
            source_picks=[],
        )

        self.assertEqual(payload["status"], "SHADOW_ONLY")
        self.assertNotEqual(payload["run_id"], source_run["run_id"])
        self.assertEqual(payload["source_production_run"]["run_id"], "rec_test")
        self.assertIn("does not write recommendation_runs", payload["safety_boundary"])

    def test_build_shadow_run_handles_missing_production_run_as_nonfatal_artifact(self):
        payload = shadow.build_shadow_run(
            proposal=self._proposal(),
            source_run=None,
            source_picks=[],
        )

        self.assertEqual(payload["status"], "NO_PRODUCTION_RUN")
        self.assertEqual(payload["picks"], [])


if __name__ == "__main__":
    unittest.main()
