"""板块热度 badge 单测。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from stock_research.core.industry_heat import (  # type: ignore
    _map_theme_to_etf,
    _symbol_benchmark_etf,
    classify_etf_return,
)


class IndustryHeatTest(unittest.TestCase):
    def test_classify_hot_cold(self):
        self.assertEqual(classify_etf_return(30.0), "hot")
        self.assertEqual(classify_etf_return(-2.0), "cold")
        self.assertEqual(classify_etf_return(5.0), "neutral")

    def test_theme_maps_energy(self):
        self.assertEqual(_map_theme_to_etf("能源链", "US"), "XLE")

    def test_consumer_staples_not_xlk(self):
        self.assertEqual(_map_theme_to_etf("消费 / 餐饮连锁", "US"), "XLP")

    def test_consumer_discretionary_toys(self):
        self.assertEqual(_map_theme_to_etf("消费 / 潮流玩具", "HK"), "XLY")

    def test_finance_maps_xlf(self):
        self.assertEqual(_map_theme_to_etf("金融 / 综合投资控股", "US"), "XLF")

    def test_gold_commodity_gld(self):
        self.assertEqual(
            _map_theme_to_etf("商品 / 黄金", "US", asset_class="commodity"),
            "GLD",
        )

    def test_english_ai_infrastructure_maps_technology(self):
        self.assertEqual(_map_theme_to_etf("ASIC / networking", "US"), "XLK")
        self.assertEqual(_map_theme_to_etf("semiconductor data center chips", "US"), "XLK")

    def test_real_holding_symbol_fallbacks(self):
        self.assertEqual(_symbol_benchmark_etf("MRVL"), "XLK")
        self.assertEqual(_symbol_benchmark_etf("MCD"), "XLY")
        self.assertEqual(_symbol_benchmark_etf("BRK-B"), "XLF")
        self.assertEqual(_symbol_benchmark_etf("9992.HK"), "XLY")
        self.assertEqual(_symbol_benchmark_etf("IAUM", asset_class="commodity"), "GLD")

    def test_no_default_xlk_for_unknown(self):
        self.assertIsNone(_map_theme_to_etf(None, "US"))
        self.assertIsNone(_map_theme_to_etf("未知行业", "HK"))


if __name__ == "__main__":
    unittest.main()
