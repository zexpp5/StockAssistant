import unittest

from stock_research.jobs import defense_watcher


class DefenseWatcherFixedFrequencyTest(unittest.TestCase):
    def test_fixed_scan_interval_is_five_minutes(self) -> None:
        self.assertEqual(defense_watcher.FAST_SCAN_MINUTES, 5)


class DefenseWatcherContextTest(unittest.TestCase):
    def test_pcr_warning_says_price_has_not_turned_weak(self) -> None:
        context = {
            "quotes": [
                {"label": "SPY", "price": 742.18, "pct": 0.63},
                {"label": "QQQ", "price": 719.3, "pct": 2.02},
                {"label": "SMH", "price": 600.9, "pct": 5.48},
                {"label": "VIX", "price": 18.24, "pct": -15.2},
            ]
        }
        alerts = [{
            "type": "PUT_CALL_RATIO",
            "severity": "CRITICAL",
            "pcr_volume": 2.5,
            "pcr_oi": 1.4,
        }]

        lines = defense_watcher._market_context_lines(context, alerts)

        self.assertTrue(any("价格端" in line for line in lines))
        self.assertTrue(any("期权端" in line for line in lines))
        self.assertTrue(any("暂未同步转弱" in line for line in lines))

    def test_weak_prices_raise_context_weight(self) -> None:
        context = {
            "quotes": [
                {"label": "SPY", "price": 730.0, "pct": -0.8},
                {"label": "QQQ", "price": 700.0, "pct": -1.2},
                {"label": "SMH", "price": 560.0, "pct": -2.5},
            ]
        }

        lines = defense_watcher._market_context_lines(context, [])

        self.assertTrue(any("价格端也有转弱迹象" in line for line in lines))

    def test_alert_card_note_mentions_five_minutes(self) -> None:
        card = defense_watcher._build_alert_card("HIGH", "CRITICAL", [], context=None)
        note = card["card"]["elements"][-1]["elements"][0]["content"]

        self.assertIn("每 5 分钟", note)


if __name__ == "__main__":
    unittest.main()
