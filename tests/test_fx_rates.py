"""单一汇率源 smoke 测试。

2026-05-22 Part 1 收敛后：5 处硬编码 (risk_metrics / trade_delta /
backtest_plan_a / dashboard JS x2) 全部走 scripts/lib/fx_rates.py。
本测试守护：
  1. 关键币种存在且数值在合理区间
  2. HKD 不会再被偷偷写成 0.91 / 0.92 这种漂移值
  3. get_fx_to_rmb / infer_currency_from_ticker 行为契约

跑：
    python3 -m unittest tests.test_fx_rates
"""
from __future__ import annotations
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "lib"))


class FxRatesTest(unittest.TestCase):
    def test_known_currencies_present(self):
        """USD/HKD/CNY/JPY/KRW/AUD/GBP 必须都在表里。"""
        from fx_rates import FX_TO_RMB  # type: ignore
        for ccy in ("USD", "HKD", "CNY", "JPY", "KRW", "AUD", "GBP"):
            self.assertIn(ccy, FX_TO_RMB, f"{ccy} 缺失")

    def test_usd_cny_range(self):
        """USD/CNY 必须在合理区间 (历史 6.0 - 7.8)。"""
        from fx_rates import FX_TO_RMB  # type: ignore
        self.assertGreaterEqual(FX_TO_RMB["USD"], 6.0)
        self.assertLessEqual(FX_TO_RMB["USD"], 7.8)

    def test_hkd_cny_range(self):
        """HKD/CNY 必须在 [0.85, 0.95]。守护 0.91/0.92 漂移不复发。"""
        from fx_rates import FX_TO_RMB  # type: ignore
        self.assertGreaterEqual(FX_TO_RMB["HKD"], 0.85)
        self.assertLessEqual(FX_TO_RMB["HKD"], 0.95)

    def test_cny_is_one(self):
        from fx_rates import FX_TO_RMB  # type: ignore
        self.assertEqual(FX_TO_RMB["CNY"], 1.0)

    def test_get_fx_unknown_returns_one(self):
        """未知 / 空 ccy 必须 fallback 到 1.0（不抛异常）。"""
        from fx_rates import get_fx_to_rmb  # type: ignore
        self.assertEqual(get_fx_to_rmb("XXX"), 1.0)
        self.assertEqual(get_fx_to_rmb(""), 1.0)
        self.assertEqual(get_fx_to_rmb(None), 1.0)

    def test_get_fx_case_insensitive(self):
        from fx_rates import get_fx_to_rmb, FX_TO_RMB  # type: ignore
        self.assertEqual(get_fx_to_rmb("usd"), FX_TO_RMB["USD"])
        self.assertEqual(get_fx_to_rmb("hkd"), FX_TO_RMB["HKD"])

    def test_infer_currency_from_ticker(self):
        from fx_rates import infer_currency_from_ticker  # type: ignore
        self.assertEqual(infer_currency_from_ticker("600519.SS"), "CNY")
        self.assertEqual(infer_currency_from_ticker("300308.SZ"), "CNY")
        self.assertEqual(infer_currency_from_ticker("0700.HK"), "HKD")
        self.assertEqual(infer_currency_from_ticker("NVDA"), "USD")
        self.assertEqual(infer_currency_from_ticker("7203.T"), "JPY")
        self.assertEqual(infer_currency_from_ticker(""), "USD")
        self.assertEqual(infer_currency_from_ticker(None), "USD")


if __name__ == "__main__":
    unittest.main()
