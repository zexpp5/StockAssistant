from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.tools import validate_grade_reversal_exit as vex  # noqa: E402


def _run(source: str, run_date: str, picks: list[dict]) -> dict:
    return {
        "run_id": f"shadow_{source}",
        "weight_variant": "rev_grade_5050",
        "source_production_run": {"run_id": source, "run_date": run_date},
        "picks": picks,
    }


def _pick(market: str, symbol: str, rank: int, signal: str = "buy") -> dict:
    return {"market": market, "symbol": symbol, "shadow_market_rank": rank, "shadow_signal": signal}


class TestExitRuleValidation(unittest.TestCase):
    def test_top_picks_respects_per_market_rank_and_buy_only(self):
        run = _run("rec_1", "2026-06-15", [
            _pick("US", "AAA", 1),
            _pick("US", "BBB", 2),
            _pick("US", "CCC", 3, signal="hold"),   # 非买入 → 剔除
            _pick("HK", "0001", 1),
        ])
        with mock.patch.object(vex, "TOP_N", 2):
            picks = sorted(vex._top_picks(run))
        self.assertEqual(picks, [("HK", "0001"), ("US", "AAA"), ("US", "BBB")])

    def test_weekend_source_runs_excluded(self):
        runs = [
            _run("wd", "2026-06-15", [_pick("US", "AAA", 1)]),   # 周一
            _run("sat", "2026-06-13", [_pick("US", "BBB", 1)]),  # 周六
        ]
        kept = [r for r in runs if not vex._is_weekend_source_run(r)]
        self.assertEqual([r["source_production_run"]["run_id"] for r in kept], ["wd"])

    def test_report_flags_positive_exit_rule(self):
        runs = [_run("rec_1", "2026-06-15", [_pick("US", "AAA", 1), _pick("US", "BBB", 2)])]
        # 1日两只都正且优于5日 → 规则有效
        outcomes = {
            ("rec_1", "US", "AAA", "1d"): 1.5,
            ("rec_1", "US", "BBB", "1d"): 0.5,
            ("rec_1", "US", "AAA", "5d"): -2.0,
            ("rec_1", "US", "BBB", "5d"): -1.0,
        }
        with mock.patch.object(vex, "_load_variant_runs", return_value=runs), \
             mock.patch.object(vex, "_fetch_outcomes", return_value=outcomes):
            report = vex.build_report()
        us = next(m for m in report["markets"] if m["market"] == "US")
        self.assertEqual(us["hold_1d_exit_on_pop"]["n"], 2)
        self.assertEqual(us["hold_1d_exit_on_pop"]["avg_alpha_pct"], 1.0)
        self.assertEqual(us["hold_1d_exit_on_pop"]["win_rate_pct"], 100.0)
        self.assertIn("规则有效", us["verdict"])

    def test_report_flags_still_negative(self):
        runs = [_run("rec_1", "2026-06-15", [_pick("US", "AAA", 1)])]
        outcomes = {("rec_1", "US", "AAA", "1d"): -0.5, ("rec_1", "US", "AAA", "5d"): -0.2}
        with mock.patch.object(vex, "_load_variant_runs", return_value=runs), \
             mock.patch.object(vex, "_fetch_outcomes", return_value=outcomes):
            report = vex.build_report()
        us = next(m for m in report["markets"] if m["market"] == "US")
        self.assertIn("未转正", us["verdict"])


if __name__ == "__main__":
    unittest.main()
