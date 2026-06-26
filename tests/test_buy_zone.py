"""可买入区间 buy_zone 测试 —— 纯函数 + 内存 duckdb 三分支(估值/技术/无数据)。"""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from stock_research.core import buy_zone  # type: ignore


class PositionTest(unittest.TestCase):
    def test_position_buckets(self):
        self.assertEqual(buy_zone._position(90, 100, 120), "便宜")   # 低于下沿
        self.assertEqual(buy_zone._position(110, 100, 120), "区间内")
        self.assertEqual(buy_zone._position(130, 100, 120), "偏贵")  # 高于上沿
        self.assertEqual(buy_zone._position(None, 100, 120), "未知")

    def test_position_edges_inclusive(self):
        # 等于下沿/上沿都算"区间内"(不<low、不>high)
        self.assertEqual(buy_zone._position(100, 100, 120), "区间内")
        self.assertEqual(buy_zone._position(120, 100, 120), "区间内")


class FormatLineTest(unittest.TestCase):
    def test_none_zone_returns_none(self):
        self.assertIsNone(buy_zone.format_line(None))
        self.assertIsNone(buy_zone.format_line({"low": None, "high": 1}))

    def test_valuation_line_shows_target_anchor(self):
        z = {"method": "估值", "low": 315, "high": 382, "current": 372,
             "target": 450, "position": "区间内"}
        line = buy_zone.format_line(z)
        self.assertIn("可买区间 $315~$382", line)
        self.assertIn("估值", line)
        self.assertIn("目标价 $450", line)
        self.assertIn("可考虑", line)

    def test_technical_line_shows_ma_anchor(self):
        z = {"method": "技术", "low": 602, "high": 623, "current": 567,
             "target": None, "position": "便宜"}
        line = buy_zone.format_line(z)
        self.assertIn("技术", line)
        self.assertIn("MA20~MA50", line)
        self.assertIn("偏便宜", line)


def _make_conn():
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE price_daily (symbol VARCHAR, trade_date DATE, close DOUBLE)")
    con.execute("CREATE TABLE analyst_grade_events (symbol VARCHAR, event_date DATE, price_target DOUBLE)")
    return con


class ComputeBuyZoneTest(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 6, 15)
        self.con = _make_conn()

    def tearDown(self):
        self.con.close()

    def test_valuation_branch_uses_target(self):
        # 有近期目标价 → 估值锚定 = 目标价 × [0.70, 0.85]
        self.con.execute("INSERT INTO price_daily VALUES ('GOOGL', DATE '2026-06-15', 372)")
        self.con.execute("INSERT INTO analyst_grade_events VALUES ('GOOGL', DATE '2026-06-03', 450)")
        z = buy_zone.compute_buy_zone("GOOGL", self.con, today=self.today)
        self.assertEqual(z["method"], "估值")
        self.assertAlmostEqual(z["low"], 315.0, places=1)   # 450*0.70
        self.assertAlmostEqual(z["high"], 382.5, places=1)  # 450*0.85
        self.assertEqual(z["current"], 372)
        self.assertEqual(z["current_trade_date"], "2026-06-15")
        self.assertEqual(z["position"], "区间内")

    def test_stale_target_falls_back_to_technical(self):
        # 目标价过期(>120天) → 不用估值, 退技术回撤
        old = self.today - timedelta(days=200)
        self.con.execute("INSERT INTO analyst_grade_events VALUES ('XYZ', DATE '2025-11-27', 100)")
        for i in range(50):
            d = self.today - timedelta(days=i)
            self.con.execute("INSERT INTO price_daily VALUES ('XYZ', ?, ?)", [d, 50 + i])
        z = buy_zone.compute_buy_zone("XYZ", self.con, today=self.today)
        self.assertEqual(z["method"], "技术")
        self.assertLessEqual(z["low"], z["high"])

    def test_technical_branch_ma_ordering(self):
        # 无目标价 + 足够收盘价 → 技术回撤, 下沿≤上沿
        for i in range(50):
            d = self.today - timedelta(days=i)
            self.con.execute("INSERT INTO price_daily VALUES ('ABC', ?, ?)", [d, 100 + i])
        z = buy_zone.compute_buy_zone("ABC", self.con, today=self.today)
        self.assertEqual(z["method"], "技术")
        self.assertLessEqual(z["low"], z["high"])
        self.assertEqual(z["current"], 100)  # 最新(i=0)
        self.assertEqual(z["current_trade_date"], "2026-06-15")

    def test_no_data_returns_none(self):
        z = buy_zone.compute_buy_zone("NODATA", self.con, today=self.today)
        self.assertIsNone(z)

    def test_too_few_closes_no_target_returns_none(self):
        # 不足 MA_SHORT(20) 日且无目标价 → None
        for i in range(5):
            d = self.today - timedelta(days=i)
            self.con.execute("INSERT INTO price_daily VALUES ('FEW', ?, ?)", [d, 10 + i])
        z = buy_zone.compute_buy_zone("FEW", self.con, today=self.today)
        self.assertIsNone(z)

    def test_batch_skips_empty(self):
        self.con.execute("INSERT INTO price_daily VALUES ('GOOGL', DATE '2026-06-15', 372)")
        self.con.execute("INSERT INTO analyst_grade_events VALUES ('GOOGL', DATE '2026-06-03', 450)")
        out = buy_zone.compute_buy_zones(["GOOGL", "NODATA", ""], self.con, today=self.today)
        self.assertIn("GOOGL", out)
        self.assertNotIn("NODATA", out)
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
