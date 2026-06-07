from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

STRICT_PATH = REPO / "scripts" / "tools" / "us_strict_trial.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


strict = _load_module(STRICT_PATH, "us_strict_trial_test")


class UsStrictTrialTest(unittest.TestCase):
    def test_uses_market_rank_not_global_rank(self):
        row = {
            "market": "US",
            "signal": "buy",
            "global_rank": 41,
            "market_rank": 1,
            "factor_scores": {"momentum": 55},
            "risk_flags": [],
        }
        self.assertTrue(strict._is_strict_candidate(row))

        row["market_rank"] = 6
        self.assertFalse(strict._is_strict_candidate(row))

    def test_blocks_overheated_and_momentum_cutoff(self):
        base = {
            "market": "US",
            "signal": "buy",
            "market_rank": 1,
            "factor_scores": {"momentum": 79.99},
            "risk_flags": [],
        }
        self.assertTrue(strict._is_strict_candidate(base))

        overheated = dict(base)
        overheated["risk_flags"] = [{"code": "OVERHEATED_1Y", "message": "1Y涨幅过高"}]
        self.assertFalse(strict._is_strict_candidate(overheated))

        at_cutoff = dict(base)
        at_cutoff["factor_scores"] = {"momentum": 80}
        self.assertFalse(strict._is_strict_candidate(at_cutoff))

    def test_risk_codes_parse_json_and_dicts(self):
        self.assertEqual(
            strict._risk_codes('[{"code":"OVERHEATED_1Y","message":"too hot"}]'),
            ["OVERHEATED_1Y"],
        )
        self.assertEqual(
            strict._risk_codes([{"code": "ACUTE_PRICE_PULLBACK"}, "MOMENTUM_REUSED_RECENT_V2_SNAPSHOT"]),
            ["ACUTE_PRICE_PULLBACK", "MOMENTUM_REUSED_RECENT_V2_SNAPSHOT"],
        )

    def test_display_mode_folds_or_withdraws_on_negative_evidence(self):
        folded, folded_notes = strict._display_mode({
            "strict_overlay": {"n": 10, "avg_alpha_pct": 1.2},
            "recent_strict_runs": [
                {"n": 3, "avg_alpha_pct": -0.3},
                {"n": 3, "avg_alpha_pct": -0.8},
            ],
        })
        self.assertEqual(folded, "folded_research")
        self.assertTrue(folded_notes)

        too_small, small_notes = strict._display_mode({
            "strict_overlay": {"n": 10, "avg_alpha_pct": 1.2},
            "recent_strict_runs": [
                {"n": 1, "avg_alpha_pct": -0.3},
                {"n": 2, "avg_alpha_pct": -0.8},
            ],
        })
        self.assertEqual(too_small, "active_research")
        self.assertTrue(any("不触发折叠" in note for note in small_notes))

        withdrawn, withdrawn_notes = strict._display_mode({
            "strict_overlay": {"n": 20, "avg_alpha_pct": -0.1},
            "recent_strict_runs": [],
        })
        self.assertEqual(withdrawn, "withdraw")
        self.assertTrue(withdrawn_notes)

    def test_evidence_summary_separates_strict_from_all_us(self):
        samples = [
            {"market_rank": 1, "factor_scores": {"momentum": 50}, "risk_flags": [], "alpha_pct": 2.0},
            {"market_rank": 2, "factor_scores": {"momentum": 85}, "risk_flags": [], "alpha_pct": -3.0},
            {"market_rank": 3, "factor_scores": {"momentum": 40}, "risk_flags": [{"code": "OVERHEATED_1Y"}], "alpha_pct": -5.0},
            {"market_rank": 6, "factor_scores": {"momentum": 40}, "risk_flags": [], "alpha_pct": -1.0},
        ]
        evidence = strict._evidence_summary(samples, max_market_rank=5, momentum_lt=80)
        self.assertEqual(evidence["all_us_buy"]["n"], 4)
        self.assertEqual(evidence["top_1_5"]["n"], 3)
        self.assertEqual(evidence["strict_overlay"]["n"], 1)
        self.assertEqual(evidence["strict_overlay"]["avg_alpha_pct"], 2.0)

    def test_trial_review_gate_uses_forward_samples_not_backtest_only(self):
        not_ready = strict._trial_review_gate({
            "strict_overlay": {"n": 49, "avg_alpha_pct": 1.59, "win_rate_pct": 59.18},
            "strict_overlay_forward": {"n": 10, "avg_alpha_pct": 3.0, "win_rate_pct": 60.0},
            "recent_forward_strict_runs": [
                {"avg_alpha_pct": 1.0, "win_rate_pct": 60.0},
                {"avg_alpha_pct": 2.0, "win_rate_pct": 60.0},
            ],
        })
        self.assertEqual(not_ready["status"], "NOT_READY")
        self.assertFalse(not_ready["checks"][0]["passed"])

        eligible = strict._trial_review_gate({
            "strict_overlay_forward": {"n": 20, "avg_alpha_pct": 1.1, "win_rate_pct": 55.0},
            "recent_forward_strict_runs": [
                {"avg_alpha_pct": 1.0, "win_rate_pct": 50.0},
                {"avg_alpha_pct": 0.5, "win_rate_pct": 60.0},
            ],
        })
        self.assertEqual(eligible["status"], "ELIGIBLE_FOR_MANUAL_REVIEW")
        self.assertTrue(all(item["passed"] for item in eligible["checks"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
