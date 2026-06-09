"""strategy_eval 纯函数单测（不依赖 DB）。

口径核心（last_batch_run_ids / mature_samples）依赖真实 DB，已在接入处的
build_strategy_validation_v2 / us_strict_trial 端到端验证（n 260→80、严筛 49→14）。
此处覆盖解析与子集谓词的边界。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from stock_research.core import strategy_eval as se


class StrategyEvalTest(unittest.TestCase):
    def test_momentum_parses_dict_and_json_and_bad(self):
        self.assertEqual(se._momentum({"momentum": 55}), 55.0)
        self.assertEqual(se._momentum('{"momentum": 79.99}'), 79.99)
        self.assertIsNone(se._momentum(None))
        self.assertIsNone(se._momentum('{"valuation": 10}'))  # 无 momentum 键
        self.assertIsNone(se._momentum('not json'))

    def test_risk_codes_parses_dicts_and_strings(self):
        self.assertEqual(
            se._risk_codes('[{"code":"OVERHEATED_1Y","message":"hot"}]'),
            ["OVERHEATED_1Y"],
        )
        self.assertEqual(
            se._risk_codes([{"code": "ACUTE_PRICE_PULLBACK"}, "RAW_FLAG"]),
            ["ACUTE_PRICE_PULLBACK", "RAW_FLAG"],
        )
        self.assertEqual(se._risk_codes(None), [])
        self.assertEqual(se._risk_codes("[]"), [])

    def test_is_top_and_is_strict(self):
        ok = {"market_rank": 1, "momentum": 55, "risk_codes": []}
        self.assertTrue(se.is_top(ok))
        self.assertTrue(se.is_strict(ok))

        # market_rank 超出
        self.assertFalse(se.is_strict({**ok, "market_rank": 6}))
        # momentum 到阈值（边界 80 不算严筛）
        self.assertFalse(se.is_strict({**ok, "momentum": 80}))
        self.assertTrue(se.is_strict({**ok, "momentum": 79.99}))
        # 命中过热红旗
        self.assertFalse(se.is_strict({**ok, "risk_codes": ["OVERHEATED_1Y"]}))
        # momentum 缺失不入严筛
        self.assertFalse(se.is_strict({**ok, "momentum": None}))

    def test_summarize(self):
        samples = [
            {"alpha_pct": 2.0},
            {"alpha_pct": -1.0},
            {"alpha_pct": 3.0},
            {"alpha_pct": None},  # 应被忽略
        ]
        out = se.summarize(samples)
        self.assertEqual(out["n"], 3)
        self.assertAlmostEqual(out["avg_alpha_pct"], 1.3333, places=3)
        self.assertAlmostEqual(out["win_rate_pct"], 66.67, places=1)
        self.assertEqual(out["median_alpha_pct"], 2.0)
        self.assertEqual(out["max_loss_pct"], -1.0)

    def test_summarize_empty(self):
        out = se.summarize([{"alpha_pct": None}])
        self.assertEqual(out["n"], 0)
        self.assertIsNone(out["avg_alpha_pct"])
        self.assertIsNone(out["win_rate_pct"])
        self.assertEqual(out["sample_power"], "display_only")

    def test_summarize_distribution_marks_extreme_winner_dependency(self):
        samples = [
            {"alpha_pct": -1.0},
            {"alpha_pct": -0.5},
            {"alpha_pct": -0.2},
            {"alpha_pct": 0.1},
            {"alpha_pct": 8.0},
        ]
        out = se.summarize_distribution(samples)
        self.assertEqual(out["n"], 5)
        self.assertGreater(out["avg_alpha_pct"], 0)
        self.assertLessEqual(out["median_alpha_pct"], 0)
        self.assertTrue(out["extreme_winner_dependency"])
        self.assertEqual(out["sample_power"], "display_only")

    def test_sample_power_thresholds(self):
        self.assertEqual(se.sample_power(19)["sample_power"], "display_only")
        self.assertEqual(se.sample_power(20)["sample_power"], "weak_reference")
        self.assertEqual(se.sample_power(50)["sample_power"], "initial_reference")
        self.assertEqual(se.sample_power(100)["sample_power"], "upgrade_evidence")


if __name__ == "__main__":
    unittest.main(verbosity=2)
