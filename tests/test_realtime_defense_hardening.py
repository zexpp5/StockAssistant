from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_research.core import defense_signals
from stock_research.jobs import realtime_defense


class TestRealtimeDefenseHardening(unittest.TestCase):
    def test_diagnose_all_forwards_include_options_flag(self) -> None:
        with patch.object(defense_signals, "check_market_regime", return_value=[]) as market:
            with patch.object(defense_signals, "check_stop_loss", return_value=[]):
                result = defense_signals.diagnose_all(
                    [],
                    include_macro=True,
                    include_options=False,
                )

        self.assertEqual(result["severity"], "NONE")
        market.assert_called_once_with(
            as_of=None,
            include_macro=True,
            include_options=False,
        )

    def test_market_data_errors_become_low_alerts(self) -> None:
        with patch("stock_research.core.regime_filter._spy_above_200ma", return_value=(True, {"error": "SPY timeout"})):
            with patch("stock_research.core.regime_filter._vix_below_panic", return_value=(True, {"error": "VIX timeout"})):
                alerts = defense_signals.check_market_regime(
                    include_macro=False,
                    include_options=False,
                )

        self.assertEqual(len(alerts), 2)
        self.assertEqual({a["severity"] for a in alerts}, {"LOW"})
        self.assertTrue(all(a["type"] == "MARKET_DATA_UNAVAILABLE" for a in alerts))

    def test_degraded_result_is_non_crashing_low_alert(self) -> None:
        result = realtime_defense._degraded_result("external source timeout")

        self.assertEqual(result["severity"], "LOW")
        self.assertTrue(result["degraded"])
        self.assertEqual(result["alerts"][0]["type"], "REALTIME_DEFENSE_DEGRADED")
        self.assertIn("不要据此加仓", result["alerts"][0]["suggested_action"])

    def test_run_skips_options_by_default_and_saves_snapshot(self) -> None:
        fake_stock_db = types.SimpleNamespace(fetch_picks_normalized=lambda: [])
        sys.modules["stock_db"] = fake_stock_db
        seen: dict[str, object] = {}

        def fake_diagnose(picks, **kwargs):
            seen["picks"] = picks
            seen["kwargs"] = kwargs
            return {
                "alerts": [],
                "severity": "NONE",
                "market_alerts": [],
                "stop_loss_alerts": [],
                "summary": "ok",
            }

        with patch.object(realtime_defense.defense_signals, "diagnose_all", side_effect=fake_diagnose):
            with patch.object(realtime_defense.store, "save_json", return_value=Path("/tmp/realtime_defense.json")) as save:
                snap = realtime_defense.run(notify=False, diagnose_timeout_seconds=0)

        self.assertEqual(seen["kwargs"]["include_options"], False)
        self.assertEqual(snap["options_pcr"], "skipped_in_production_batch")
        save.assert_called_once()

    def test_run_degrades_when_picks_db_is_locked(self) -> None:
        fake_stock_db = types.SimpleNamespace(
            fetch_picks_normalized=lambda: (_ for _ in ()).throw(RuntimeError("duckdb lock"))
        )
        sys.modules["stock_db"] = fake_stock_db

        def fake_diagnose(picks, **_kwargs):
            self.assertEqual(picks, [])
            return {
                "alerts": [],
                "severity": "NONE",
                "market_alerts": [],
                "stop_loss_alerts": [],
                "summary": "ok",
            }

        with patch.object(realtime_defense.defense_signals, "diagnose_all", side_effect=fake_diagnose):
            with patch.object(realtime_defense.store, "save_json", return_value=Path("/tmp/realtime_defense.json")):
                snap = realtime_defense.run(notify=False, diagnose_timeout_seconds=0)

        self.assertEqual(snap["severity"], "LOW")
        self.assertTrue(snap["degraded"])
        self.assertEqual(snap["alerts"][0]["type"], "PICKS_DATA_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
