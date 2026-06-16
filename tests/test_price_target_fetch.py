"""FMP fetch_price_target 窗口选择逻辑测试(monkeypatch _get,不打网络)。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from stock_research.core import fmp_client  # type: ignore


class FetchPriceTargetTest(unittest.TestCase):
    def setUp(self):
        self._orig_get = fmp_client._get

    def tearDown(self):
        fmp_client._get = self._orig_get

    def _patch(self, payload):
        fmp_client._get = lambda path, params=None, **kw: payload

    def test_prefers_last_month(self):
        self._patch([{
            "lastMonthCount": 4, "lastMonthAvgPriceTarget": 432.5,
            "lastQuarterCount": 22, "lastQuarterAvgPriceTarget": 421.2,
            "lastYearCount": 95, "lastYearAvgPriceTarget": 351.6,
        }])
        r = fmp_client.fetch_price_target("GOOGL")
        self.assertEqual(r["price_target"], 432.5)
        self.assertEqual(r["window"], "近1月")
        self.assertEqual(r["n_analysts"], 4)

    def test_falls_back_to_quarter_when_month_empty(self):
        self._patch([{
            "lastMonthCount": 0, "lastMonthAvgPriceTarget": 0,
            "lastQuarterCount": 12, "lastQuarterAvgPriceTarget": 300.0,
            "lastYearCount": 50, "lastYearAvgPriceTarget": 280.0,
        }])
        r = fmp_client.fetch_price_target("X")
        self.assertEqual(r["price_target"], 300.0)
        self.assertEqual(r["window"], "近1季")

    def test_falls_back_to_year(self):
        self._patch([{
            "lastMonthCount": 0, "lastMonthAvgPriceTarget": None,
            "lastQuarterCount": 0, "lastQuarterAvgPriceTarget": None,
            "lastYearCount": 8, "lastYearAvgPriceTarget": 120.0,
        }])
        r = fmp_client.fetch_price_target("Y")
        self.assertEqual(r["price_target"], 120.0)
        self.assertEqual(r["window"], "近1年")

    def test_none_when_all_empty(self):
        self._patch([{
            "lastMonthCount": 0, "lastQuarterCount": 0, "lastYearCount": 0,
        }])
        self.assertIsNone(fmp_client.fetch_price_target("Z"))

    def test_none_when_no_payload(self):
        self._patch(None)
        self.assertIsNone(fmp_client.fetch_price_target("Z"))
        self._patch([])
        self.assertIsNone(fmp_client.fetch_price_target("Z"))


if __name__ == "__main__":
    unittest.main()
