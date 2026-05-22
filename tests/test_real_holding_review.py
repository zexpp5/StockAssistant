"""真实持仓每日体检 smoke tests."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import stock_db  # type: ignore
from stock_research.jobs.real_holding_review import _build_item, _default_rules  # type: ignore


class RealHoldingReviewTest(unittest.TestCase):
    def test_stock_without_model_score_is_data_action(self):
        item = _build_item(
            {"symbol": "MCD", "market": "US", "entry_price": 280, "shares": 10,
             "currency": "USD", "entry_fx_rate": 7.1},
            price={"close": 285, "currency": "USD", "trade_date": "2026-05-22"},
            pick=None,
            verdict={"coverage_class": "picks_only", "treatment_class": "picks_only", "asset_class": "equity"},
            total_capital=500000,
            target_weights={},
        )
        self.assertEqual(item["action_label"], "补数据")
        self.assertIn("no_model_score", item["data_flags"])

    def test_tracking_asset_is_not_forced_into_stock_score(self):
        item = _build_item(
            {"symbol": "IAUM", "market": "US", "entry_price": 45, "shares": 100,
             "currency": "USD", "entry_fx_rate": 7.1},
            price={"close": 46, "currency": "USD", "trade_date": "2026-05-22"},
            pick=None,
            verdict={"coverage_class": "tracking_only", "treatment_class": "tracking_only", "asset_class": "commodity"},
            total_capital=500000,
            target_weights={},
        )
        self.assertEqual(item["action_label"], "仅风控跟踪")
        self.assertIsNone(item["score"])

    def test_large_loss_takes_priority_over_buy_signal(self):
        item = _build_item(
            {"symbol": "NVDA", "market": "US", "entry_price": 100, "shares": 10,
             "currency": "USD", "entry_fx_rate": 7.1},
            price={"close": 70, "currency": "USD", "trade_date": "2026-05-22"},
            pick={"total_score": 95, "rating": "strong_buy"},
            verdict={"coverage_class": "ai_portfolio", "treatment_class": "ai_portfolio", "asset_class": "equity"},
            total_capital=500000,
            target_weights={"NVDA": 0.05},
        )
        self.assertEqual(item["action_label"], "风险复查")
        self.assertLessEqual(item["score"], 45)

    def test_rule_override_changes_loss_threshold(self):
        default_item = _build_item(
            {"symbol": "NVDA", "market": "US", "entry_price": 100, "shares": 10,
             "currency": "USD", "entry_fx_rate": 7.1},
            price={"close": 85, "currency": "USD", "trade_date": "2026-05-22"},
            pick={"total_score": 90, "rating": "strong_buy"},
            verdict={"coverage_class": "picks_only", "asset_class": "equity"},
            total_capital=500000,
            target_weights={},
        )
        self.assertEqual(default_item["action_label"], "风险复查")

        rules = _default_rules()
        rules["loss_review_pct"] = -20.0
        custom_item = _build_item(
            {"symbol": "NVDA", "market": "US", "entry_price": 100, "shares": 10,
             "currency": "USD", "entry_fx_rate": 7.1},
            rules=rules,
            price={"close": 85, "currency": "USD", "trade_date": "2026-05-22"},
            pick={"total_score": 90, "rating": "strong_buy"},
            verdict={"coverage_class": "picks_only", "asset_class": "equity"},
            total_capital=500000,
            target_weights={},
        )
        self.assertEqual(custom_item["action_label"], "持有观察")

    def test_underweight_high_score_can_be_add_watch(self):
        item = _build_item(
            {"symbol": "NVDA", "market": "US", "entry_price": 100, "shares": 10,
             "currency": "USD", "entry_fx_rate": 7.1},
            price={"close": 110, "currency": "USD", "trade_date": "2026-05-22"},
            pick={"total_score": 88, "rating": "strong_buy"},
            verdict={"coverage_class": "ai_portfolio", "treatment_class": "ai_portfolio", "asset_class": "equity"},
            total_capital=500000,
            target_weights={"NVDA": 0.08},
        )
        self.assertEqual(item["action_label"], "关注加仓")

    def test_save_fetch_review_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            conn = stock_db.get_db(str(Path(d) / "test.duckdb"))
            try:
                run = {
                    "review_run_id": "test_run",
                    "as_of_date": "2026-05-22",
                    "status": "generated",
                    "holding_count": 1,
                    "data_quality": "OK",
                    "notes": "unit test",
                }
                item = {
                    "symbol": "MCD",
                    "market": "US",
                    "asset_class": "equity",
                    "treatment_class": "stock_score",
                    "score": 72.5,
                    "coverage_score": 0.8,
                    "rating": "buy",
                    "action_label": "持有观察",
                    "action_priority": 6,
                    "reasons": ["测试原因"],
                    "risk_flags": ["测试风险"],
                    "data_flags": [],
                }
                self.assertEqual(stock_db.save_real_holding_review(run, [item], conn=conn), 1)
                fetched = stock_db.fetch_latest_real_holding_review(conn=conn)
            finally:
                conn.close()
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["run"]["review_run_id"], "test_run")
        self.assertEqual(fetched["items"][0]["symbol"], "MCD")
        self.assertEqual(fetched["items"][0]["reasons"], ["测试原因"])

    def test_latest_endpoint_missing_does_not_recompute(self):
        try:
            from fastapi.testclient import TestClient
            from stock_research.api.main import create_app
        except (ImportError, RuntimeError) as exc:
            if "httpx" not in str(exc).lower() and not isinstance(exc, ImportError):
                raise
            self.skipTest("fastapi/httpx test client deps not installed")

        app = create_app()
        with patch("stock_db.fetch_latest_real_holding_review", return_value=None):
            r = TestClient(app).get("/api/real-holdings/daily-review/latest")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["status"], "missing")
        self.assertEqual(data["items"], [])
        self.assertFalse(data["transient"])


if __name__ == "__main__":
    unittest.main()
