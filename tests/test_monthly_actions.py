"""真执行逻辑的月度动作判定测试 —— 喂输入断言输出，不是查文案。

针对 renderMonthlyActions 之前"只查字符串在不在"的假安全感，这里直接驱动
stock_research.core.monthly_actions.build_monthly_plan，验证 ≤3 上限 / 单赛道
15% / 同赛道去重 / 超配纠偏 / 闸门 这些判定真生效。逻辑被改坏，这些测试会红。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from stock_research.core import monthly_actions as ma  # noqa: E402


def _readiness(*, trial=False, blocked=False):
    if blocked:
        return {"status": "FAIL", "decision": {"code": "BLOCKED"}, "us": {}}
    if trial:
        return {"status": "PASS", "decision": {"code": "US_TRIAL_READY"}, "us": {"trial_ready": True}}
    return {"status": "PASS", "decision": {"code": "US_RESEARCH_READY_TRIAL_PENDING"}, "us": {"trial_ready": False}}


def _zone(pos, low=10, high=20, current=12):
    return {"position": pos, "low": low, "high": high, "current": current}


class CorrectionPlanTest(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(ma.correction_plan_for(0.53)["target"], 0.45)
        self.assertEqual(ma.correction_plan_for(0.42)["target"], 0.40)
        self.assertEqual(ma.correction_plan_for(0.30)["target"], 0.25)
        self.assertIsNone(ma.correction_plan_for(0.20))

    def test_severe_reduce_amount(self):
        p = ma.correction_plan_for(0.53)
        self.assertAlmostEqual(p["reduce_pct"], 0.08, places=6)


class ThemeClassifyTest(unittest.TestCase):
    def test_override_wins(self):
        self.assertEqual(ma.classify_theme("GOOGL", "random words"), "云与AI平台")

    def test_keyword_fallback(self):
        self.assertEqual(ma.classify_theme("XYZ", "nuclear power utility"), "电力/核能基础设施")
        self.assertEqual(ma.classify_theme("XYZ", "gpu semiconductor"), "半导体/AI硬件")


class CapsTest(unittest.TestCase):
    def test_blocked_zero(self):
        c = ma.resolve_caps(_readiness(blocked=True))
        self.assertTrue(c["blocked"])
        self.assertEqual((c["per_name"], c["total_month"]), (0.0, 0.0))

    def test_trial_ready_higher(self):
        c = ma.resolve_caps(_readiness(trial=True))
        self.assertEqual((c["per_name"], c["total_month"]), (0.02, 0.06))

    def test_research_only_strict(self):
        c = ma.resolve_caps(_readiness())
        self.assertEqual((c["per_name"], c["total_month"]), (0.01, 0.03))


class BuildMonthlyPlanTest(unittest.TestCase):
    def _run(self, plan_rows, **kw):
        defaults = dict(
            readiness=_readiness(trial=True), real_weights={}, buy_zones={},
            candidates={}, review_items=[],
        )
        defaults.update(kw)
        return ma.build_monthly_plan(plan_rows=plan_rows, **defaults)

    def test_blocked_yields_no_buys(self):
        out = self._run(
            [{"ticker": "NVDA", "target_weight": 0.05}],
            readiness=_readiness(blocked=True),
            buy_zones={"NVDA": _zone("便宜")},
            candidates={"NVDA": {"theme": "半导体"}},
        )
        self.assertEqual(out["buy_rows"], [])
        self.assertTrue(any("阻断" in "".join(s["reasons"]) for s in out["skip_rows"]))

    def test_cap_at_three_distinct_themes(self):
        # 5 个不同赛道、都便宜、都有 gap → 只应买 3 个（≤3 上限）
        rows = [{"ticker": t, "target_weight": 0.05} for t in ["AAA", "BBB", "CCC", "DDD", "EEE"]]
        themes = {"AAA": "云与AI平台", "BBB": "半导体/AI硬件", "CCC": "电力/核能基础设施",
                  "DDD": "半导体设备", "EEE": "服务器/数据中心"}
        out = self._run(
            rows,
            buy_zones={t: _zone("便宜") for t in themes},
            candidates={t: {"theme": themes[t]} for t in themes},
        )
        self.assertEqual(len(out["buy_rows"]), 3, "≤3 上限必须生效")

    def test_same_theme_second_pick_allowed_when_actual_tiny(self):
        # 2026-07-16 规则修订（TSM 案例）：赛道实际持仓很小(<5%)时，
        # 同赛道第 2 只不再被一票否决
        out = self._run(
            [{"ticker": "NVDA", "target_weight": 0.05}, {"ticker": "AMD", "target_weight": 0.05}],
            buy_zones={"NVDA": _zone("便宜"), "AMD": _zone("便宜")},
            candidates={"NVDA": {"theme": "半导体/AI硬件"}, "AMD": {"theme": "半导体/AI硬件"}},
        )
        self.assertEqual([b["ticker"] for b in out["buy_rows"]], ["NVDA", "AMD"])

    def test_same_theme_third_pick_blocked(self):
        # 就算实际仓位为 0，同赛道单月最多 2 只
        tks = ("NVDA", "AMD", "AVGO")
        out = self._run(
            [{"ticker": t, "target_weight": 0.05} for t in tks],
            buy_zones={t: _zone("便宜") for t in tks},
            candidates={t: {"theme": "半导体/AI硬件"} for t in tks},
        )
        self.assertEqual([b["ticker"] for b in out["buy_rows"]], ["NVDA", "AMD"])
        skip = next(s for s in out["skip_rows"] if s["ticker"] == "AVGO")
        self.assertTrue(any("最多" in r for r in skip["reasons"]))

    def test_same_theme_dedup_when_actually_invested(self):
        # 该赛道实际持仓已 6%（≥5% 阈值）→ 第 2 只仍被"同赛道不重复"踢出
        out = self._run(
            [{"ticker": "NVDA", "target_weight": 0.08}, {"ticker": "AMD", "target_weight": 0.05}],
            buy_zones={"NVDA": _zone("便宜"), "AMD": _zone("便宜")},
            candidates={"NVDA": {"theme": "半导体/AI硬件"}, "AMD": {"theme": "半导体/AI硬件"}},
            review_items=[{"symbol": "NVDA", "current_weight": 0.06}],
            real_weights={"NVDA": 0.06},
        )
        self.assertEqual([b["ticker"] for b in out["buy_rows"]], ["NVDA"])
        amd_skip = next(s for s in out["skip_rows"] if s["ticker"] == "AMD")
        self.assertTrue(any("同赛道不重复" in r for r in amd_skip["reasons"]))

    def test_theme_over_15pct_blocks_new_buy(self):
        # 某赛道现有暴露已 16% → 该赛道新买被拦
        out = self._run(
            [{"ticker": "AMD", "target_weight": 0.05}],
            buy_zones={"AMD": _zone("便宜")},
            candidates={"AMD": {"theme": "半导体/AI硬件"}},
            real_weights={"NVDA": 0.16},
            review_items=[{"symbol": "NVDA", "current_weight": 0.16}],
        )
        self.assertEqual(out["buy_rows"], [])
        amd = next(s for s in out["skip_rows"] if s["ticker"] == "AMD")
        self.assertTrue(any("15% 赛道上限" in r for r in amd["reasons"]))

    def test_overpriced_zone_skipped(self):
        out = self._run(
            [{"ticker": "TSM", "target_weight": 0.05}],
            buy_zones={"TSM": _zone("偏贵")},
            candidates={"TSM": {"theme": "半导体/AI硬件"}},
        )
        self.assertEqual(out["buy_rows"], [])
        self.assertTrue(any("不追高" in r for s in out["skip_rows"] for r in s["reasons"]))

    def test_over_25pct_holding_skipped(self):
        out = self._run(
            [{"ticker": "GOOGL", "target_weight": 0.10}],
            buy_zones={"GOOGL": _zone("便宜")},
            candidates={"GOOGL": {"theme": "云与AI平台"}},
            real_weights={"GOOGL": 0.53},
        )
        self.assertEqual(out["buy_rows"], [])

    def test_cheap_sizes_full_inzone_half(self):
        # 便宜 zone_scale=1.0 → cap_pct=per_name(0.02); 区间内=0.5 → 0.01
        cheap = self._run(
            [{"ticker": "AAA", "target_weight": 0.50}],
            buy_zones={"AAA": _zone("便宜")}, candidates={"AAA": {"theme": "X"}},
        )
        inzone = self._run(
            [{"ticker": "BBB", "target_weight": 0.50}],
            buy_zones={"BBB": _zone("区间内")}, candidates={"BBB": {"theme": "Y"}},
        )
        self.assertAlmostEqual(cheap["buy_rows"][0]["cap_pct"], 0.02, places=6)
        self.assertAlmostEqual(inzone["buy_rows"][0]["cap_pct"], 0.01, places=6)

    def test_concentration_correction_surfaced(self):
        out = self._run(
            [],
            review_items=[{"symbol": "GOOGL", "current_weight": 0.53, "name": "Alphabet"}],
        )
        corr = next(c for c in out["corrections"] if c["ticker"] == "GOOGL")
        self.assertEqual(corr["plan"]["target"], 0.45)
        self.assertTrue(any("严重集中" in t for t in corr["triggers"]))

    def test_total_month_cap_not_exceeded(self):
        # research-only: total 3%; 3 个不同赛道便宜票各想要 2%→ 合计不超 3%
        rows = [{"ticker": t, "target_weight": 0.50} for t in ["AAA", "BBB", "CCC"]]
        themes = {"AAA": "云与AI平台", "BBB": "半导体/AI硬件", "CCC": "电力/核能基础设施"}
        out = self._run(
            rows, readiness=_readiness(trial=False),
            buy_zones={t: _zone("便宜") for t in themes},
            candidates={t: {"theme": themes[t]} for t in themes},
        )
        self.assertLessEqual(out["new_buy_pct"], 0.03 + 1e-9)


if __name__ == "__main__":
    unittest.main()
