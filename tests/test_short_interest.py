"""空头拥挤度提示灯 classify_short_crowding 纯函数测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import short_interest  # type: ignore

C = short_interest.classify_short_crowding


class ShortCrowdingClassifyTest(unittest.TestCase):
    def test_mrvl_real_values_are_low(self):
        # 2026-06-12 实测：MRVL short%float 4.7%、days_to_cover 1.3、空头环比 28.3M→35.2M
        # 关键纠偏：MRVL 下跌不是空头围猎，提示灯必须给"低"（但标环比在增加）
        r = C(0.0471, 1.3, 35_215_842, 28_296_571)
        self.assertEqual(r["level"], "低")
        self.assertAlmostEqual(r["short_pct_float"], 4.71, places=1)
        self.assertGreater(r["mom_change_pct"], 15)  # +24%
        self.assertIn("增加", r["note"])

    def test_aapl_real_values_are_low(self):
        # 实测 AAPL：short%float 1.06%、days_to_cover 3.12 → 低（days<6 不升级）
        r = C(0.0106, 3.12, 155_886_024, 134_675_274)
        self.assertEqual(r["level"], "低")

    def test_medium_by_float(self):
        self.assertEqual(C(0.07, 2.0)["level"], "中")

    def test_high_by_float(self):
        self.assertEqual(C(0.12, 2.0)["level"], "高")

    def test_very_high_float_notes_extreme(self):
        r = C(0.22, 2.0)
        self.assertEqual(r["level"], "高")
        self.assertIn("极高", r["note"])

    def test_days_to_cover_escalates_low_to_medium(self):
        # short%float 仅 3%（低），但回补天数 6.5 → 抬到中
        self.assertEqual(C(0.03, 6.5)["level"], "中")

    def test_days_to_cover_8_forces_high(self):
        self.assertEqual(C(0.03, 9.0)["level"], "高")

    def test_accepts_already_percent_form(self):
        # 兼容 0.047 与 4.7 两种存法，结果一致
        self.assertEqual(C(4.7, 1.3)["level"], C(0.047, 1.3)["level"])
        self.assertAlmostEqual(C(4.7, 1.3)["short_pct_float"], 4.7, places=1)

    def test_missing_float_is_unknown(self):
        r = C(None, 3.0)
        self.assertEqual(r["level"], "未知")
        self.assertIsNone(r["short_pct_float"])

    def test_mom_decrease_noted(self):
        r = C(0.12, 5.0, 20_000_000, 30_000_000)  # -33%
        self.assertLess(r["mom_change_pct"], -15)
        self.assertIn("减少", r["note"])

    def test_non_us_ticker_is_not_applicable(self):
        out = short_interest.resolve_short_crowding(["9992.HK", "600519.SS"], use_cache=False)
        self.assertEqual(out["9992.HK"]["level"], "不适用")
        self.assertEqual(out["600519.SS"]["level"], "不适用")


if __name__ == "__main__":
    unittest.main()
