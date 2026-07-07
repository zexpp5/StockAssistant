"""alpha_trend_logger 分市场双轨引擎的纯逻辑单测（不打库/网络）。

覆盖 2026-07-07 港A股锦标赛泛化的关键正确性点：
  1. _safe_return / _summarize_alpha 挡住 NaN（HK/CN 价源出现过 → 曾污染 avg）
  2. DUAL_TRACK_CONFIG 完整性：每个变体在 VARIANTS 里、基线/主挑战者在 formulas 里
  3. _switch_criteria_verdict：达标判定 + 连续天数 streak + 分市场历史读取
  4. 向后兼容别名仍指向 US
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_research.jobs import alpha_trend_logger as atl
import scripts.tools.replay_weight_variants as rp


class SafeReturnFiniteTest(unittest.TestCase):
    def test_rejects_nan_and_inf(self):
        self.assertIsNone(atl._safe_return(float("nan"), 100.0))
        self.assertIsNone(atl._safe_return(100.0, float("nan")))
        self.assertIsNone(atl._safe_return(100.0, float("inf")))
        self.assertIsNone(atl._safe_return(0.0, 100.0))     # start<=0
        self.assertIsNone(atl._safe_return(-5.0, 100.0))
        self.assertIsNone(atl._safe_return(None, 100.0))

    def test_normal_return(self):
        self.assertAlmostEqual(atl._safe_return(100.0, 110.0), 10.0)

    def test_summarize_filters_nonfinite(self):
        # 一个 NaN 混进来不能把 avg 污染成 nan（老 bug）
        s = atl._summarize_alpha([1.0, 3.0, float("nan"), float("inf")])
        self.assertEqual(s["n"], 2)
        self.assertAlmostEqual(s["avg_alpha_pct"], 2.0)
        self.assertEqual(atl._summarize_alpha([float("nan")]), {"n": 0})
        self.assertEqual(atl._summarize_alpha([]), {"n": 0})


class ConfigIntegrityTest(unittest.TestCase):
    def test_all_markets_present(self):
        self.assertEqual(set(atl.DUAL_TRACK_CONFIG), {"US", "HK", "CN"})

    def test_variants_exist_and_baseline_in_formulas(self):
        for mkt, cfg in atl.DUAL_TRACK_CONFIG.items():
            self.assertIn(cfg["baseline"], cfg["formulas"], f"{mkt} 基线不在 formulas")
            self.assertIn(cfg["primary_challenger"], cfg["formulas"], f"{mkt} 主挑战者不在 formulas")
            self.assertNotEqual(cfg["baseline"], cfg["primary_challenger"], f"{mkt} 基线=挑战者")
            for public_name, variant in cfg["formulas"].items():
                self.assertIn(variant, rp.VARIANTS, f"{mkt}/{public_name} 变体 {variant} 未注册")
            self.assertTrue(cfg["benchmark"], f"{mkt} 无基准")
            self.assertTrue(cfg["eligibility"], f"{mkt} 无 eligibility")
            self.assertIn(cfg["switch_rule"]["switch_top_n"], cfg["top_ns"])

    def test_backward_compat_aliases(self):
        self.assertEqual(atl.DUAL_TRACK_MARKET, "US")
        self.assertIs(atl.DUAL_TRACK_FORMULAS, atl.DUAL_TRACK_CONFIG["US"]["formulas"])
        self.assertIs(atl.SWITCH_RULE, atl.DUAL_TRACK_CONFIG["US"]["switch_rule"])


def _fake_dual_block(market: str, delta_1d: float, delta_5d: float,
                     challenger_5d_alpha: float, n5: int) -> dict:
    """构造一个最小 dual_track block，喂给 _switch_criteria_verdict。"""
    cfg = atl.DUAL_TRACK_CONFIG[market]
    top_n = cfg["switch_rule"]["switch_top_n"]
    challenger = cfg["primary_challenger"]
    return {
        "market": market,
        "by_top_n": {
            f"top{top_n}": {
                "horizons": {
                    "1d": {challenger: {"delta_vs_baseline_avg_alpha_pct": delta_1d,
                                        "avg_alpha_pct": 0.1, "n": n5}},
                    "5d": {challenger: {"delta_vs_baseline_avg_alpha_pct": delta_5d,
                                        "avg_alpha_pct": challenger_5d_alpha, "n": n5}},
                }
            }
        },
    }


class SwitchCriteriaTest(unittest.TestCase):
    def test_all_conditions_met(self):
        cfg = atl.DUAL_TRACK_CONFIG["HK"]
        n = cfg["switch_rule"]["min_5d_n"]
        block = _fake_dual_block("HK", delta_1d=0.5, delta_5d=0.6,
                                 challenger_5d_alpha=1.2, n5=n)
        v = atl._switch_criteria_verdict(block, [], "HK", cfg)
        self.assertTrue(v["met_today"])
        self.assertEqual(v["consecutive_met_days"], 1)
        self.assertFalse(v["switch_allowed"])  # 才 1 天，不够 10 天

    def test_negative_challenger_alpha_blocks(self):
        # CN 典型：挑战者跑赢基线(delta>0)但自身仍负 → 不达标
        cfg = atl.DUAL_TRACK_CONFIG["CN"]
        n = cfg["switch_rule"]["min_5d_n"]
        block = _fake_dual_block("CN", delta_1d=0.8, delta_5d=0.8,
                                 challenger_5d_alpha=-0.7, n5=n)
        v = atl._switch_criteria_verdict(block, [], "CN", cfg)
        self.assertFalse(v["met_today"])
        self.assertFalse(v["checks"]["top10_5d_new_alpha_positive"])

    def test_insufficient_sample_blocks(self):
        cfg = atl.DUAL_TRACK_CONFIG["HK"]
        block = _fake_dual_block("HK", delta_1d=0.5, delta_5d=0.6,
                                 challenger_5d_alpha=1.2, n5=10)  # n 远不够
        v = atl._switch_criteria_verdict(block, [], "HK", cfg)
        self.assertFalse(v["met_today"])

    def test_streak_counts_from_history_per_market(self):
        cfg = atl.DUAL_TRACK_CONFIG["HK"]
        n = cfg["switch_rule"]["min_5d_n"]
        block = _fake_dual_block("HK", 0.5, 0.6, 1.2, n)
        # 历史两天 HK 都达标 → 今天应连成 3
        hist = [
            {"date": "2026-07-05", "dual_track": {"HK": {"switch_criteria": {"met_today": True}}}},
            {"date": "2026-07-06", "dual_track": {"HK": {"switch_criteria": {"met_today": True}}}},
        ]
        v = atl._switch_criteria_verdict(block, hist, "HK", cfg)
        self.assertEqual(v["consecutive_met_days"], 3)

    def test_streak_breaks_on_gap(self):
        cfg = atl.DUAL_TRACK_CONFIG["HK"]
        n = cfg["switch_rule"]["min_5d_n"]
        block = _fake_dual_block("HK", 0.5, 0.6, 1.2, n)
        hist = [
            {"date": "2026-07-05", "dual_track": {"HK": {"switch_criteria": {"met_today": True}}}},
            {"date": "2026-07-06", "dual_track": {"HK": {"switch_criteria": {"met_today": False}}}},
        ]
        v = atl._switch_criteria_verdict(block, hist, "HK", cfg)
        self.assertEqual(v["consecutive_met_days"], 1)  # 昨天断了，只剩今天


if __name__ == "__main__":
    unittest.main(verbosity=2)
