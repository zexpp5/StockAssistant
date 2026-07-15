"""卖出三条线引擎单测 — 参数必须锁死预注册值（方案 §2）。"""
from __future__ import annotations

import unittest

from stock_research.core.exit_rules import (
    EXIT_PARAMS,
    build_exit_plan,
    exit_plan_compact,
)


class PreregisteredParamsTest(unittest.TestCase):
    """预注册参数回归锁：谁改参数谁必须先过用户拍板（改这里的期望值=挪门槛）。"""

    def test_params_locked(self):
        self.assertEqual(EXIT_PARAMS["US"], {"stop_pct": -8.0, "review_days": 5, "fallback_target_pct": 15.0})
        self.assertEqual(EXIT_PARAMS["HK"], {"stop_pct": -10.0, "review_days": 20, "fallback_target_pct": 15.0})
        self.assertEqual(EXIT_PARAMS["CN"], {"stop_pct": -6.0, "review_days": 5, "fallback_target_pct": 15.0})


class BuildExitPlanTest(unittest.TestCase):
    def test_us_with_buy_zone(self):
        plan = build_exit_plan("US", 100.0, {"low": 90.0, "high": 120.0})
        self.assertEqual(plan["stop_price"], 92.0)          # -8%
        self.assertEqual(plan["target_price"], 120.0)       # 区间上沿
        self.assertEqual(plan["target_source"], "buy_zone_high")
        self.assertEqual(plan["review_days"], 5)
        self.assertEqual(len(plan["lines"]), 3)

    def test_hk_wider_stop_longer_review(self):
        plan = build_exit_plan("HK", 200.0, None)
        self.assertEqual(plan["stop_price"], 180.0)         # -10%
        self.assertEqual(plan["target_price"], 230.0)       # +15% 兜底
        self.assertEqual(plan["target_source"], "entry_plus_15pct")
        self.assertEqual(plan["review_days"], 20)

    def test_cn_tight_stop_and_alias(self):
        for alias in ("CN", "A", "A股"):
            plan = build_exit_plan(alias, 50.0, None)
            self.assertEqual(plan["market"], "CN")
            self.assertEqual(plan["stop_price"], 47.0)      # -6%
            self.assertEqual(plan["review_days"], 5)

    def test_zone_high_below_entry_falls_back(self):
        # 已涨破区间上沿(追高买入):上沿不能当目标线(比入场还低),退化 +15%
        plan = build_exit_plan("US", 130.0, {"high": 120.0})
        self.assertEqual(plan["target_source"], "entry_plus_15pct")
        self.assertAlmostEqual(plan["target_price"], 149.5)

    def test_invalid_entry_returns_none(self):
        self.assertIsNone(build_exit_plan("US", None))
        self.assertIsNone(build_exit_plan("US", float("nan")))
        self.assertIsNone(build_exit_plan("US", -5))
        self.assertIsNone(build_exit_plan("XX", 100.0))     # 未知市场

    def test_advisory_wording_no_directives(self):
        plan = build_exit_plan("US", 100.0, {"high": 120.0})
        joined = "".join(plan["lines"])
        for banned in ("必须买", "必须卖"):
            self.assertNotIn(banned, joined)
        self.assertIn("建议", joined)

    def test_compact_line(self):
        plan = build_exit_plan("US", 100.0, {"high": 120.0})
        s = exit_plan_compact(plan)
        self.assertIn("🛑止损92.0", s)
        self.assertIn("🎯目标120.0", s)
        self.assertIn("⏰5日复评", s)
        self.assertEqual(exit_plan_compact(None), "")


if __name__ == "__main__":
    unittest.main()
