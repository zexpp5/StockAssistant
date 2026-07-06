"""预期消耗度纯函数测试。用例编号对应 docs/V2/预期消耗度_测试用例.md。"""
from __future__ import annotations

import unittest

from stock_research.core.expectation_meter import (
    LIGHT_HIGH,
    LIGHT_LOW,
    LIGHT_MID,
    LIGHT_UNKNOWN,
    expectation_meter,
    format_meter_line,
    is_cyclical_industry,
)


class TestExpectationMeter(unittest.TestCase):
    def test_t1_mu_like_cyclical_top_forces_red(self):
        """T1 MU 型：一年+698%、fPE 6.5、内存行业 → 周期顶旗 + 强制红灯。"""
        m = expectation_meter(
            price=976, target_price=2000, peg_ratio=0.14, forward_pe=6.5,
            one_year_pct=697.7, industry_text="半导体 内存/存储 HBM",
        )
        self.assertTrue(m["cyclical_top_risk"])
        self.assertEqual(m["light"], LIGHT_HIGH)
        self.assertTrue(any("低 PE ≠ 便宜" in r for r in m["reasons"]))

    def test_t2_vrt_like_mid(self):
        """T2 VRT 型：吃掉目标价 72%、PEG 1.5、一年+135% → 黄灯。"""
        m = expectation_meter(
            price=301, target_price=416, peg_ratio=1.51, forward_pe=33.9,
            one_year_pct=135.1, industry_text="数据中心电力/散热",
        )
        self.assertFalse(m["cyclical_top_risk"])
        self.assertEqual(m["light"], LIGHT_MID)

    def test_t3_crm_like_green(self):
        """T3 CRM 型：一年-39%、PEG 0.79、目标价消耗 73% → 绿灯（预期不满）。"""
        m = expectation_meter(
            price=166, target_price=228, peg_ratio=0.79, forward_pe=10.7,
            one_year_pct=-39.0, industry_text="企业软件 SaaS",
        )
        self.assertEqual(m["light"], LIGHT_LOW)

    def test_t4_overshoot_target_red(self):
        """T4 现价超过目标价 + 高 PEG + 大涨幅 → 红灯（非周期路径）。"""
        m = expectation_meter(
            price=110, target_price=100, peg_ratio=3.0, forward_pe=60,
            one_year_pct=250, industry_text="软件平台",
        )
        self.assertFalse(m["cyclical_top_risk"])
        self.assertEqual(m["light"], LIGHT_HIGH)

    def test_t5_missing_components_skipped_not_faked(self):
        """T5 缺目标价/PEG 时只按剩余部件算，不硬凑中性值。"""
        m = expectation_meter(price=100, one_year_pct=30)
        self.assertNotIn("target_consumption", m["components"])
        self.assertNotIn("peg_pressure", m["components"])
        self.assertEqual(m["max_score"], 2)  # 只有 runup 一项
        self.assertEqual(m["light"], LIGHT_LOW)

    def test_t6_all_missing_unknown(self):
        """T6 全缺 → ⚪ 数据不足，不给假灯。"""
        m = expectation_meter()
        self.assertEqual(m["light"], LIGHT_UNKNOWN)
        self.assertIsNone(m["ratio"])

    def test_t7_cyclical_needs_all_three_conditions(self):
        """T7 周期旗须同时满足行业+涨幅+低PE：只中两条不触发。"""
        # 内存行业 + 大涨幅，但 fPE 高 → 不触发
        m1 = expectation_meter(price=1, one_year_pct=300, forward_pe=40,
                               industry_text="memory storage")
        self.assertFalse(m1["cyclical_top_risk"])
        # 内存行业 + 低PE，但涨幅不足 → 不触发
        m2 = expectation_meter(price=1, one_year_pct=80, forward_pe=6,
                               industry_text="内存")
        self.assertFalse(m2["cyclical_top_risk"])
        # 非周期行业 → 不触发
        m3 = expectation_meter(price=1, one_year_pct=300, forward_pe=6,
                               industry_text="企业软件")
        self.assertFalse(m3["cyclical_top_risk"])

    def test_t8_negative_or_zero_target_ignored(self):
        """T8 目标价<=0 视为缺失，不产生消耗度部件。"""
        m = expectation_meter(price=100, target_price=0, one_year_pct=10)
        self.assertNotIn("target_consumption", m["components"])

    def test_t9_negative_peg_ignored(self):
        """T9 PEG<=0（亏损/无意义）不计入，不当"便宜"加分。"""
        m = expectation_meter(price=100, target_price=200, peg_ratio=-0.5, one_year_pct=10)
        self.assertNotIn("peg_pressure", m["components"])

    def test_t10_format_line(self):
        """T10 一行摘要含灯/标签/目标价消耗/一年涨幅；数据不足显示⚪。"""
        m = expectation_meter(price=301, target_price=416, peg_ratio=1.51,
                              one_year_pct=135, industry_text="x")
        line = format_meter_line(m)
        self.assertIn("🟡", line)
        self.assertIn("已吃目标价 72%", line)
        self.assertIn("+135%", line)
        self.assertEqual(format_meter_line(None), "⚪ 预期消耗：数据不足")

    def test_t11_cyclical_keyword_matching(self):
        self.assertTrue(is_cyclical_industry("HBM 内存"))
        self.assertTrue(is_cyclical_industry(None, "NAND flash storage"))
        self.assertFalse(is_cyclical_industry("SaaS 软件", "云计算"))


if __name__ == "__main__":
    unittest.main()
