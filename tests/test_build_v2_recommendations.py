from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

MODULE_PATH = REPO / "scripts" / "tools" / "build_v2_recommendations.py"
spec = importlib.util.spec_from_file_location("build_v2_recommendations", MODULE_PATH)
build_v2 = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(build_v2)


class PriceActionReviewGateTest(unittest.TestCase):
    def _base_row(self) -> dict:
        return {
            "close": 100.0,
            "prev_close": 99.0,
            "market_cap": 85_000_000_000,
            "forward_pe": 12.0,
            "trailing_pe": 20.0,
            "peg_ratio": 0.8,
            "ytd_pct": 10.0,
            "one_week_pct": 1.0,
            "one_month_pct": 2.0,
            "one_year_pct": 40.0,
            "trade_date": "2026-06-06",
            "momentum_trade_date": "2026-06-06",
            "fundamentals_trade_date": "2026-06-06",
        }

    def test_deep_reversal_candidate_is_downgraded_to_watch(self):
        row = {
            **self._base_row(),
            "close": 313.0,
            "prev_close": 307.73,
            "forward_pe": 11.47,
            "trailing_pe": 19.09,
            "peg_ratio": 0.81,
            "ytd_pct": -50.27,
            "one_week_pct": 1.93,
            "one_month_pct": -21.82,
            "one_year_pct": -58.66,
        }
        scores = build_v2._factor_scores(row)
        raw_total = scores["total"]

        flags = build_v2._apply_price_action_review_gate(row, scores)

        self.assertGreater(raw_total, 75.0)
        self.assertEqual(scores["review_gate"], "price_action")
        self.assertEqual(scores["raw_total"], round(raw_total, 2))
        self.assertLess(scores["total"], 60.0)
        self.assertEqual(build_v2._rating(scores["total"]), "watch")
        self.assertEqual(build_v2._signal(scores["total"]), "watch")
        self.assertEqual(flags[0]["code"], "STRUCTURAL_DOWNTREND_REVIEW_GATE")
        self.assertIn("结构性下跌", flags[0]["message"])

    def test_normal_pullback_in_uptrend_is_not_gated(self):
        row = {
            **self._base_row(),
            "ytd_pct": 35.0,
            "one_week_pct": -3.0,
            "one_month_pct": -16.0,
            "one_year_pct": 80.0,
        }
        scores = build_v2._factor_scores(row)

        flags = build_v2._apply_price_action_review_gate(row, scores)

        self.assertEqual(flags, [])
        self.assertNotIn("review_gate", scores)

    def test_acute_one_day_drop_in_uptrend_is_warning_not_gate(self):
        row = {
            **self._base_row(),
            "close": 90.0,
            "prev_close": 100.0,
            "ytd_pct": 12.0,
            "one_week_pct": -4.0,
            "one_month_pct": -6.0,
            "one_year_pct": 30.0,
        }
        scores = build_v2._factor_scores(row)

        flags = build_v2._apply_price_action_review_gate(row, scores)
        warnings = build_v2._price_action_warning_flags(row)

        self.assertEqual(flags, [])
        self.assertNotIn("review_gate", scores)
        self.assertEqual(warnings[0]["code"], "ACUTE_PRICE_PULLBACK")
        self.assertIn("单日 -10.0%", warnings[0]["message"])

    def test_acute_one_day_drop_with_medium_weakness_is_gated(self):
        row = {
            **self._base_row(),
            "close": 90.0,
            "prev_close": 100.0,
            "ytd_pct": -30.0,
            "one_week_pct": -4.0,
            "one_month_pct": -9.0,
            "one_year_pct": -20.0,
        }
        scores = build_v2._factor_scores(row)

        flags = build_v2._apply_price_action_review_gate(row, scores)

        self.assertEqual(scores["review_gate"], "price_action")
        self.assertLess(scores["total"], 60.0)
        self.assertIn("单日 -10.0%", flags[0]["message"])

    def test_structural_downtrend_bounce_without_repair_is_gated(self):
        row = {
            **self._base_row(),
            "close": 353.76,
            "prev_close": 331.53,
            "forward_pe": 12.95,
            "trailing_pe": 21.6,
            "peg_ratio": 0.86,
            "ytd_pct": -43.8,
            "one_week_pct": 16.23,
            "one_month_pct": -8.94,
            "one_year_pct": -53.76,
        }
        scores = build_v2._factor_scores(row)

        flags = build_v2._apply_price_action_review_gate(row, scores)

        self.assertEqual(scores["review_gate"], "price_action")
        self.assertLess(scores["total"], 60.0)
        self.assertEqual(build_v2._signal(scores["total"]), "watch")
        self.assertEqual(flags[0]["code"], "STRUCTURAL_DOWNTREND_REVIEW_GATE")
        self.assertIn("结构性下跌未确认修复", flags[0]["message"])

    def test_structural_downtrend_with_confirmed_repair_is_not_gated(self):
        row = {
            **self._base_row(),
            "close": 120.0,
            "prev_close": 119.0,
            "ytd_pct": -30.0,
            "one_week_pct": 8.0,
            "one_month_pct": 12.0,
            "one_year_pct": -35.0,
        }
        scores = build_v2._factor_scores(row)

        flags = build_v2._apply_price_action_review_gate(row, scores)

        self.assertEqual(flags, [])
        self.assertNotIn("review_gate", scores)


class ValuationInputTest(unittest.TestCase):
    def test_zero_valuation_ratio_is_not_scored_as_cheap(self):
        self.assertEqual(
            build_v2._score_lower_better(0.0, good=1.0, bad=4.0),
            build_v2._NEGATIVE_VALUATION_SCORE,
        )

    def test_zero_peg_lowers_valuation_component(self):
        base = {
            "close": 100.0,
            "prev_close": 99.0,
            "forward_pe": 29.37,
            "trailing_pe": 66.47,
            "peg_ratio": 0.8,
            "ytd_pct": 10.0,
            "one_week_pct": 1.0,
            "one_month_pct": 2.0,
            "one_year_pct": 40.0,
        }
        valid = build_v2._factor_scores(base)
        zero = build_v2._factor_scores({**base, "peg_ratio": 0.0})

        self.assertLess(zero["valuation"], valid["valuation"])

    def test_zero_valuation_ratio_adds_quality_flag(self):
        flags = build_v2._quality_flags({
            "peg_ratio": 0.0,
            "forward_pe": 29.37,
            "trailing_pe": 66.47,
            "one_year_pct": 18.74,
            "trade_date": "2026-05-29",
            "momentum_trade_date": "2026-05-29",
            "fundamentals_trade_date": "2026-05-29",
        })

        self.assertEqual(flags[0]["code"], "INVALID_VALUATION_RATIO")
        self.assertIn("不视为便宜", flags[0]["message"])


class DataUsabilityGateTest(unittest.TestCase):
    def _strong_row(self) -> dict:
        return {
            "close": 100.0,
            "prev_close": 99.0,
            "market_cap": 85_000_000_000,
            "forward_pe": 12.0,
            "trailing_pe": 20.0,
            "peg_ratio": 0.8,
            "ytd_pct": 25.0,
            "one_week_pct": 2.0,
            "one_month_pct": 6.0,
            "one_year_pct": 80.0,
            "trade_date": "2026-06-06",
            "momentum_trade_date": "2026-06-06",
            "fundamentals_trade_date": "2026-06-06",
        }

    def test_complete_usable_data_does_not_gate_buy_candidate(self):
        row = self._strong_row()
        scores = build_v2._factor_scores(row)

        flags = build_v2._apply_data_usability_gate(row, scores)

        self.assertEqual(flags, [])
        self.assertNotIn("data_usability_gate", scores)
        self.assertGreaterEqual(scores["data_usability"], 90.0)

    def test_missing_fundamentals_cannot_be_buy(self):
        row = {
            **self._strong_row(),
            "forward_pe": None,
            "trailing_pe": None,
            "peg_ratio": None,
            "fundamentals_trade_date": None,
        }
        scores = build_v2._factor_scores(row)

        flags = build_v2._apply_data_usability_gate(row, scores)

        self.assertEqual(scores["data_usability_gate"], "core_data")
        self.assertLess(scores["total"], 60.0)
        self.assertNotEqual(build_v2._signal(scores["total"]), "buy")
        self.assertEqual(flags[0]["code"], "DATA_USABILITY_REVIEW_GATE")
        self.assertIn("缺估值数据源", flags[0]["message"])
        self.assertIn("没有可用正向估值字段", flags[0]["message"])

    def test_stale_factor_snapshot_cannot_be_buy(self):
        row = {
            **self._strong_row(),
            "trade_date": "2026-06-06",
            "momentum_trade_date": "2026-05-15",
            "fundamentals_trade_date": "2026-05-15",
        }
        scores = build_v2._factor_scores(row)

        flags = build_v2._apply_data_usability_gate(row, scores)

        self.assertEqual(scores["data_usability_gate"], "core_data")
        self.assertLess(scores["total"], 60.0)
        self.assertIn("动量数据已过期", flags[0]["message"])
        self.assertIn("估值数据已过期", flags[0]["message"])

    def test_audit_lists_blocked_and_attention_candidates(self):
        good = {
            **self._strong_row(),
            "market": "US",
            "symbol": "GOOD",
            "name": "Good Co",
            "theme": "AI",
            "industry": "Software",
        }
        blocked = {
            **good,
            "symbol": "MISS",
            "name": "Missing Fundamentals",
            "forward_pe": None,
            "trailing_pe": None,
            "peg_ratio": None,
            "fundamentals_trade_date": None,
        }
        attention = {
            **good,
            "symbol": "REUSE",
            "name": "Reused Snapshot",
            "trade_date": "2026-06-06",
            "momentum_trade_date": "2026-06-01",
            "fundamentals_trade_date": "2026-06-01",
        }
        scored = []
        for row in (good, blocked, attention):
            scores = build_v2._factor_scores(row)
            data_flags = build_v2._apply_data_usability_gate(row, scores)
            scored.append({
                **row,
                "factor_scores": scores,
                "risk_flags": data_flags + build_v2._quality_flags(row),
                "total_score": scores["total"],
                "signal": build_v2._signal(scores["total"]),
                "rating": build_v2._rating(scores["total"]),
            })

        audit = build_v2._build_data_usability_audit(
            scored,
            [scored[0], scored[2]],
            run_id="r1",
            generated_at=build_v2.datetime(2026, 6, 6, 9, 0, 0),
        )

        self.assertEqual(audit["blocked_count"], 1)
        self.assertEqual(audit["blocked"][0]["symbol"], "MISS")
        self.assertIn("缺估值数据源", audit["blocked"][0]["reasons"])
        self.assertEqual(audit["attention_count"], 1)
        self.assertEqual(audit["attention"][0]["symbol"], "REUSE")
        self.assertTrue(audit["attention"][0]["in_recommendation_list"])


class PortfolioCandidateFilterTest(unittest.TestCase):
    def test_optimizer_filters_non_buy_recommendation_picks(self):
        from stock_research.jobs import optimize_portfolio

        fake_stock_db = types.ModuleType("stock_db")
        fake_stock_db.fetch_latest_recommendation_picks = lambda: [
            {
                "market": "US", "symbol": "BUY", "name": "Buy", "rank": 1,
                "signal": "buy", "total_score": 81.0, "run_id": "r1",
                "run_date": "2026-06-01",
            },
            {
                "market": "US", "symbol": "WATCH", "name": "Watch", "rank": 2,
                "signal": "watch", "total_score": 59.99, "run_id": "r1",
                "run_date": "2026-06-01",
            },
            {
                "market": "US", "symbol": "AVOID", "name": "Avoid", "rank": 3,
                "signal": "avoid", "total_score": 49.0, "run_id": "r1",
                "run_date": "2026-06-01",
            },
        ]

        old_stock_db = sys.modules.get("stock_db")
        sys.modules["stock_db"] = fake_stock_db
        try:
            result = optimize_portfolio._load_factor_scores("US")
        finally:
            if old_stock_db is None:
                sys.modules.pop("stock_db", None)
            else:
                sys.modules["stock_db"] = old_stock_db

        self.assertIsNotNone(result)
        self.assertEqual([x["ticker"] for x in result["factors"]], ["BUY"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
