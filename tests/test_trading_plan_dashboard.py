import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "pipeline" / "build_stock_dashboard_html.py"


def _load_dashboard_module():
    spec = importlib.util.spec_from_file_location("build_stock_dashboard_html", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class TradingPlanDashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_dashboard_module()
        payload = json.loads((ROOT / "stock_research" / "data" / "trading_plans.json").read_text())
        cls.plan = payload["plans"][0]

    def test_waits_for_pullback_above_first_buy_zone(self):
        out = self.module._evaluate_trading_plan(self.plan, {"current": 289.68, "currency": "USD"})
        self.assertEqual(out["status_key"], "above_zone_wait")
        self.assertEqual(out["nearest_range"], "275-282")
        self.assertIn("再跌", out["distance_text"])

    def test_marks_first_buy_zone_when_price_enters_range(self):
        out = self.module._evaluate_trading_plan(self.plan, {"current": 280.0, "currency": "USD"})
        self.assertEqual(out["status_key"], "in_buy_zone")
        self.assertEqual(out["nearest_level"], "第一买点")

    def test_marks_breakout_zone_when_price_reclaims_breakout_line(self):
        out = self.module._evaluate_trading_plan(self.plan, {"current": 310.0, "currency": "USD"})
        self.assertEqual(out["status_key"], "breakout_watch")
        self.assertEqual(out["nearest_level"], "突破确认")

    def test_marks_invalidated_below_hard_stop(self):
        out = self.module._evaluate_trading_plan(self.plan, {"current": 251.0, "currency": "USD"})
        self.assertEqual(out["status_key"], "invalidated")
        self.assertEqual(out["nearest_level"], "失效线")

    def test_missing_price_stays_display_only(self):
        out = self.module._evaluate_trading_plan(self.plan, None)
        self.assertEqual(out["status_key"], "missing_price")
        self.assertEqual(out["status_label"], "缺行情")

    def test_today_panel_renders_breakout_price(self):
        evaluated = self.module._evaluate_trading_plan(self.plan, {"current": 289.68, "currency": "USD"})
        html = self.module.trading_plan_today_panel_html({
            "source": "stock_research/data/trading_plans.json",
            "plans": [evaluated],
        })
        self.assertIn("突破确认", html)
        self.assertIn("306+", html)


class BuyZonePayloadTest(unittest.TestCase):
    """「买点计划」列无人工计划时兜底的自动可买区间。"""

    @classmethod
    def setUpClass(cls):
        cls.module = _load_dashboard_module()

    def _patch(self, *, ok=True, universe=None, zones=None):
        from unittest import mock
        from stock_research.core import buy_zone, premarket_buy_signals as pbs
        universe = universe if universe is not None else {"GOOGL": {"market": "US"}}
        zones = zones if zones is not None else {}
        return (
            mock.patch.object(buy_zone, "_open_conn", return_value=(mock.MagicMock() if ok else None, ok)),
            mock.patch.object(buy_zone, "compute_buy_zones", return_value=zones),
            mock.patch.object(pbs, "_gather_universe", return_value=universe),
        )

    def test_lock_failure_degrades_to_empty(self):
        p1, p2, p3 = self._patch(ok=False)
        with p1, p2, p3:
            self.assertEqual(self.module._buy_zone_payload(), {})

    def test_transforms_zone_and_computes_discount(self):
        zones = {"GOOGL": {"low": 315.0, "high": 382.5, "current": 373.25,
                           "target": 450.0, "position": "区间内", "method": "估值"}}
        p1, p2, p3 = self._patch(zones=zones)
        with p1, p2, p3:
            out = self.module._buy_zone_payload()
        self.assertIn("GOOGL", out)
        self.assertEqual(out["GOOGL"]["position"], "区间内")
        self.assertEqual(out["GOOGL"]["discount_pct"], round((373.25 / 450.0 - 1) * 100, 1))

    def test_drops_zone_without_band(self):
        zones = {"AAA": {"low": None, "high": None, "current": 10.0}}
        p1, p2, p3 = self._patch(universe={"AAA": {"market": "US"}}, zones=zones)
        with p1, p2, p3:
            self.assertEqual(self.module._buy_zone_payload(), {})

    def test_excludes_hk_and_a_share_from_universe(self):
        from unittest import mock
        from stock_research.core import buy_zone, premarket_buy_signals as pbs
        captured = {}

        def _capture(syms, conn):
            captured["syms"] = list(syms)
            return {}

        with mock.patch.object(buy_zone, "_open_conn", return_value=(mock.MagicMock(), True)), \
             mock.patch.object(buy_zone, "compute_buy_zones", side_effect=_capture), \
             mock.patch.object(pbs, "_gather_universe", return_value={
                 "GOOGL": {"market": "US"}, "9992.HK": {"market": "HK"}, "600519": {"market": "CN"}}):
            self.module._buy_zone_payload()
        self.assertIn("GOOGL", captured["syms"])
        self.assertNotIn("9992.HK", captured["syms"])
        self.assertNotIn("600519", captured["syms"])


if __name__ == "__main__":
    unittest.main()
