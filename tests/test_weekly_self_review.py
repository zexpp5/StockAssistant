"""周末复盘 job 单测。"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from stock_research.jobs.weekly_self_review import (  # type: ignore
    _calendar_week_bounds,
    _had_increase_during_week,
    _markdown_report,
    build_weekly_self_review,
)
from stock_research.jobs.real_holding_review import (  # type: ignore
    _default_rules,
    _suggest_size_advisory,
)


class WeeklySelfReviewTest(unittest.TestCase):
    def test_calendar_week_monday_sunday(self):
        # 2026-05-25 is Monday
        start, end = _calendar_week_bounds(date(2026, 5, 27))
        self.assertEqual(start, date(2026, 5, 25))
        self.assertEqual(end, date(2026, 5, 31))

    def test_had_increase_detects_delta(self):
        start = {"AAPL": 10.0}
        end = {"AAPL": 15.0}
        self.assertTrue(_had_increase_during_week("AAPL", start, end, []))

    def test_markdown_report_has_summary(self):
        md = _markdown_report({
            "week_label": "2026-W21",
            "week_start": "2026-05-19",
            "week_end": "2026-05-25",
            "generated_at": "2026-05-25T20:00:00",
            "summary": {"missed": 1, "disobeyed": 2, "aligned": 3},
            "missed": [{"symbol": "NVDA", "note": "x", "return_5d_pct": 8.0}],
            "disobeyed": [],
            "aligned": [],
        })
        self.assertIn("错过", md)
        self.assertIn("NVDA", md)

    @patch("stock_research.jobs.weekly_self_review.stock_db.fetch_pick_outcomes_for_symbols", return_value={})
    @patch("stock_research.jobs.weekly_self_review._backup_snapshots_between", return_value=[])
    @patch("stock_research.jobs.weekly_self_review._current_holdings", return_value={"MCD": 10.0})
    @patch("stock_research.jobs.weekly_self_review._collect_weekly_model_picks")
    @patch("stock_research.jobs.weekly_self_review.stock_db.get_db")
    def test_build_missed_when_not_held(self, mock_db, mock_picks, *_rest):
        mock_picks.return_value = (
            {
                "NVDA": {
                    "symbol": "NVDA",
                    "name": "NVIDIA",
                    "best_rank": 1,
                    "first_run_id": "run_1",
                    "first_run_date": "2026-05-20",
                },
            },
            [{"run_id": "run_1", "run_date": date(2026, 5, 20)}],
        )
        mock_db.return_value.close = lambda: None
        payload = build_weekly_self_review(ref_date=date(2026, 5, 25), top_n=10)
        self.assertEqual(payload["summary"]["missed"], 1)
        self.assertEqual(payload["missed"][0]["symbol"], "NVDA")


class SuggestSizeAdvisoryTest(unittest.TestCase):
    def test_add_advisory_for_underweight(self):
        rules = _default_rules()
        adv = _suggest_size_advisory(
            rules=rules,
            action="关注加仓",
            symbol="MCD",
            shares=10,
            current_price=280.0,
            fx=7.1,
            current_value_rmb=19880.0,
            current_weight=0.04,
            target_weight=0.08,
            total_capital=500000,
            treatment_class="portfolio_model",
        )
        self.assertIsNotNone(adv)
        self.assertEqual(adv.get("direction"), "add")
        self.assertGreater(adv.get("suggested_shares") or 0, 0)

    def test_trim_when_over_hard_cap(self):
        rules = _default_rules()
        adv = _suggest_size_advisory(
            rules=rules,
            action="持有观察",
            symbol="MCD",
            shares=100,
            current_price=280.0,
            fx=7.1,
            current_value_rmb=200000.0,
            current_weight=0.40,
            target_weight=0.10,
            total_capital=500000,
            treatment_class="stock_score",
        )
        self.assertIsNotNone(adv)
        self.assertTrue(adv.get("over_hard_cap"))
        self.assertEqual(adv.get("direction"), "trim")


if __name__ == "__main__":
    unittest.main()
