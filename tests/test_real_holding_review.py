"""真实持仓每日体检 smoke tests."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import stock_db  # type: ignore
from stock_research.jobs.real_holding_review import (  # type: ignore
    _build_item,
    _default_rules,
    _hk_watchlist_score_fallbacks,
    _manual_watchlist_score_fallbacks,
    _normalize_treatment_class,
    _suggest_size_advisory,
)


class RealHoldingReviewTest(unittest.TestCase):
    def _insert_holding(
        self,
        conn,
        *,
        symbol: str,
        market: str = "US",
        currency: str = "USD",
    ) -> int:
        return stock_db.insert_real_holding(
            {
                "symbol": symbol,
                "market": market,
                "entry_price": 100,
                "shares": 1,
                "entry_date": "2026-05-20",
                "currency": currency,
                "entry_fx_rate": 1.0,
            },
            conn=conn,
        )

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

    def test_day_change_filled_when_prev_close_available(self):
        """有 prev_close 时,snapshot 必须输出今日盈亏 (RMB + %)。"""
        with patch("stock_research.jobs.real_holding_review._market_local_date", return_value=date(2026, 5, 22)):
            item = _build_item(
                {"symbol": "MCD", "market": "US", "entry_price": 280, "shares": 10,
                 "currency": "USD", "entry_fx_rate": 7.1},
                price={"close": 285, "prev_close": 282, "currency": "USD",
                       "trade_date": "2026-05-22", "prev_trade_date": "2026-05-21"},
                pick=None,
                verdict={"coverage_class": "picks_only", "treatment_class": "picks_only", "asset_class": "equity"},
                total_capital=500000,
                target_weights={},
            )
        self.assertEqual(item["prev_close"], 282)
        self.assertEqual(item["prev_trade_date"], "2026-05-21")
        # day_change_pct = (285/282 - 1) * 100 ≈ 1.0638
        self.assertAlmostEqual(item["day_change_pct"], (285 / 282 - 1) * 100, places=3)
        # day_change_rmb = (285-282) * 10 * fx(USD)
        self.assertIsNotNone(item["day_change_rmb"])
        self.assertGreater(item["day_change_rmb"], 0)

    def test_us_large_move_source_keeps_day_change_with_flag(self):
        """美股大幅跳动不能被压回昨收,但必须在体检里标注来源风险。"""
        with patch("stock_research.jobs.real_holding_review._market_local_date", return_value=date(2026, 6, 2)):
            item = _build_item(
                {"symbol": "MRVL", "market": "US", "entry_price": 272.54, "shares": 34,
                 "currency": "USD", "entry_fx_rate": 6.7611},
                price={"close": 280.78, "prev_close": 219.493, "currency": "USD",
                       "trade_date": "2026-06-02", "prev_trade_date": "2026-06-01",
                       "source": "yfinance_intraday_large_move"},
                pick={"total_score": 45, "rating": "观察"},
                verdict={"coverage_class": "picks_only", "treatment_class": "stock_score", "asset_class": "equity"},
                total_capital=500000,
                target_weights={},
            )
        self.assertFalse(item["price_is_prior_session"])
        self.assertEqual(item["day_change_basis"], "prev_close")
        self.assertIsNotNone(item["day_change_rmb"])
        self.assertGreater(item["day_change_rmb"], 0)
        self.assertIn("large_move_unconfirmed", item["data_flags"])

    def test_day_change_suppressed_for_prior_session_price(self):
        """盘前/未刷新时,上一交易日涨跌不能冒充成今日盈亏。"""
        item = _build_item(
            {"symbol": "9992.HK", "market": "HK", "entry_price": 176.5, "shares": 2000,
             "currency": "HKD", "entry_fx_rate": 0.8627},
            price={"close": 179.6, "prev_close": 173.4, "currency": "HKD",
                   "trade_date": "2000-01-03", "prev_trade_date": "2000-01-02"},
            pick=None,
            verdict={"coverage_class": "picks_only", "treatment_class": "picks_only", "asset_class": "equity"},
            total_capital=500000,
            target_weights={},
        )
        self.assertTrue(item["price_is_prior_session"])
        self.assertEqual(item["day_change_basis"], "prior_session")
        self.assertIsNone(item["day_change_rmb"])
        self.assertIsNone(item["day_change_pct"])
        self.assertIn("prior_session_price", item["data_flags"])

    def test_hk_yfinance_day_change_requires_native_confirmation(self):
        """港股只有 yfinance 单源时,不展示当日盈亏,避免和本地行情冲突误导。"""
        with patch("stock_research.jobs.real_holding_review._market_local_date", return_value=date(2026, 6, 2)):
            item = _build_item(
                {"symbol": "9992.HK", "market": "HK", "entry_price": 176.5, "shares": 2000,
                 "currency": "HKD", "entry_fx_rate": 0.8627},
                price={"close": 179.2, "prev_close": 179.6, "currency": "HKD",
                       "trade_date": "2026-06-02", "prev_trade_date": "2026-06-01",
                       "source": "yfinance"},
                pick={"total_score": 60, "rating": "⭐ 关注"},
                verdict={"coverage_class": "picks_only", "treatment_class": "stock_score", "asset_class": "equity"},
                total_capital=500000,
                target_weights={},
            )
        self.assertFalse(item["price_is_prior_session"])
        self.assertEqual(item["day_change_basis"], "unconfirmed_hk_yfinance")
        self.assertIsNone(item["day_change_rmb"])
        self.assertIsNone(item["day_change_pct"])
        self.assertIn("hk_yfinance_unconfirmed", item["data_flags"])

    def test_day_change_missing_when_prev_close_absent(self):
        """没有 prev_close 时,day_change 字段保持 None,不应崩。"""
        item = _build_item(
            {"symbol": "MCD", "market": "US", "entry_price": 280, "shares": 10,
             "currency": "USD", "entry_fx_rate": 7.1},
            price={"close": 285, "currency": "USD", "trade_date": "2026-05-22"},
            pick=None,
            verdict={"coverage_class": "picks_only", "treatment_class": "picks_only", "asset_class": "equity"},
            total_capital=500000,
            target_weights={},
        )
        self.assertIsNone(item["prev_close"])
        self.assertIsNone(item["day_change_rmb"])
        self.assertIsNone(item["day_change_pct"])

    def test_underweight_add_has_size_advisory(self):
        rules = _default_rules()
        item = _build_item(
            {"symbol": "MCD", "market": "US", "entry_price": 280, "shares": 10,
             "currency": "USD", "entry_fx_rate": 7.1},
            rules=rules,
            price={"close": 280, "currency": "USD", "trade_date": "2026-05-22"},
            pick={"total_score": 75, "rating": "⭐⭐"},
            verdict={"coverage_class": "ai_portfolio", "treatment_class": "portfolio_model",
                     "asset_class": "equity"},
            total_capital=500000,
            target_weights={"MCD": 0.10},
        )
        self.assertEqual(item["action_label"], "关注加仓")
        adv = item.get("size_advisory")
        self.assertIsNotNone(adv)
        self.assertEqual(adv.get("direction"), "add")
        self.assertTrue(adv.get("advisory_only"))

    def test_over_25pct_has_trim_advisory(self):
        rules = _default_rules()
        item = _build_item(
            {"symbol": "MCD", "market": "US", "entry_price": 280, "shares": 500,
             "currency": "USD", "entry_fx_rate": 7.1},
            rules=rules,
            price={"close": 280, "currency": "USD", "trade_date": "2026-05-22"},
            pick={"total_score": 80, "rating": "⭐⭐⭐"},
            verdict={"coverage_class": "ai_portfolio", "treatment_class": "portfolio_model",
                     "asset_class": "equity"},
            total_capital=500000,
            target_weights={"MCD": 0.08},
        )
        adv = item.get("size_advisory")
        self.assertIsNotNone(adv)
        self.assertTrue(adv.get("over_hard_cap") or item["current_weight"] >= 0.25)

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
        self.assertEqual(item["rating"], "tracking")
        # Phase 0: 应能看到仓位% + 距风控线的缓冲,而不是只有「不看因子分」一句空话
        self.assertTrue(any("当前仓位" in r and "%" in r for r in item["reasons"]),
                        f"reasons 缺少仓位% 行: {item['reasons']}")
        self.assertTrue(any("风控线" in r and "缓冲" in r for r in item["reasons"]),
                        f"reasons 缺少风控线缓冲行: {item['reasons']}")

    def test_tracking_asset_stop_breach_escalates_to_risk_review(self):
        """ETF 破止损 (verdict.label_kind=stop_breach) 必须升级为风险复查,
        否则页面会一直停在「仅风控跟踪」误导用户。"""
        item = _build_item(
            {"symbol": "IAUM", "market": "US", "entry_price": 45, "shares": 100,
             "currency": "USD", "entry_fx_rate": 7.1},
            price={"close": 44, "currency": "USD", "trade_date": "2026-05-22"},
            pick=None,
            verdict={"coverage_class": "tracking_only", "treatment_class": "tracking_only",
                     "asset_class": "commodity", "label_kind": "stop_breach"},
            total_capital=500000,
            target_weights={},
        )
        self.assertEqual(item["action_label"], "风险复查")

    def test_tracking_asset_stop_watch_escalates_to_trim_watch(self):
        """ETF 接近止损 → 减仓观察,优先级高于「仅风控跟踪」。"""
        item = _build_item(
            {"symbol": "IAUM", "market": "US", "entry_price": 45, "shares": 100,
             "currency": "USD", "entry_fx_rate": 7.1},
            price={"close": 44.5, "currency": "USD", "trade_date": "2026-05-22"},
            pick=None,
            verdict={"coverage_class": "tracking_only", "treatment_class": "tracking_only",
                     "asset_class": "commodity", "label_kind": "stop_watch"},
            total_capital=500000,
            target_weights={},
        )
        self.assertEqual(item["action_label"], "减仓观察")

    def test_tracking_asset_below_threshold_shows_breach_text(self):
        """跌破 -8% 阈值时 reasons 应明示「已跌破」而不是「还有缓冲」。"""
        item = _build_item(
            {"symbol": "IAUM", "market": "US", "entry_price": 100, "shares": 10,
             "currency": "USD", "entry_fx_rate": 7.1},
            price={"close": 90, "currency": "USD", "trade_date": "2026-05-22"},
            pick=None,
            verdict={"coverage_class": "tracking_only", "treatment_class": "tracking_only",
                     "asset_class": "commodity"},
            total_capital=500000,
            target_weights={},
        )
        self.assertEqual(item["action_label"], "风险复查")
        self.assertTrue(any("已跌破" in r and "风控线" in r for r in item["reasons"]),
                        f"reasons 缺少已跌破说明: {item['reasons']}")

    def test_treatment_class_normalization_accepts_old_and_new_names(self):
        self.assertEqual(_normalize_treatment_class("tracking_only"), "risk_only")
        self.assertEqual(_normalize_treatment_class("risk_only"), "risk_only")
        self.assertEqual(_normalize_treatment_class(None, "ai_portfolio"), "portfolio_model")
        self.assertEqual(_normalize_treatment_class(None, "picks_only"), "stock_score")

    def test_reason_uses_capped_score_when_raw_exceeds_display_cap(self):
        item = _build_item(
            {"symbol": "MCD", "market": "US", "entry_price": 280, "shares": 10,
             "currency": "USD", "entry_fx_rate": 7.1},
            price={"close": 285, "currency": "USD", "trade_date": "2026-05-22"},
            pick={"total_score": 119.0, "rating": "⭐⭐⭐ 强烈推荐"},
            verdict={"coverage_class": "ai_portfolio", "treatment_class": "ai_portfolio", "asset_class": "equity"},
            total_capital=500000,
            target_weights={"MCD": 0.05},
        )
        self.assertEqual(item["score"], 100.0)
        score_reason = next(r for r in item["reasons"] if r.startswith("最新股票评分"))
        self.assertIn("100.0", score_reason)
        self.assertIn("原始 119.0", score_reason)
        self.assertNotRegex(score_reason, r"最新股票评分 119\.0 ·")

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
                holding_id = self._insert_holding(conn, symbol="MCD")
                run = {
                    "review_run_id": "test_run",
                    "as_of_date": "2026-05-22",
                    "status": "generated",
                    "holding_count": 1,
                    "data_quality": "OK",
                    "notes": "unit test",
                }
                item = {
                    "holding_id": holding_id,
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

    def test_history_returns_chronological_per_symbol(self):
        """同一只持仓在多个日期落库,history 应按 as_of_date 升序返回。"""
        with tempfile.TemporaryDirectory() as d:
            conn = stock_db.get_db(str(Path(d) / "test.duckdb"))
            try:
                holding_id = self._insert_holding(conn, symbol="IAUM")
                for as_of, action, pnl in [("2026-05-20", "持有观察", -1.0),
                                            ("2026-05-21", "减仓观察", -5.0),
                                            ("2026-05-22", "风险复查", -9.0)]:
                    stock_db.save_real_holding_review(
                        {"review_run_id": f"run_{as_of}", "as_of_date": as_of,
                         "status": "generated", "holding_count": 1,
                         "data_quality": "OK", "notes": ""},
                        [{"holding_id": holding_id, "symbol": "IAUM", "market": "US", "asset_class": "commodity",
                          "treatment_class": "risk_only", "score": None, "coverage_score": 0.7,
                          "rating": "tracking", "action_label": action,
                          "action_priority": stock_db.USER_CONFIG_DEFAULTS.get("noop", 0) or 7,
                          "pnl_pct": pnl, "current_weight": 0.12,
                          "reasons": [], "risk_flags": [], "data_flags": []}],
                        conn=conn,
                    )
                hist = stock_db.fetch_real_holding_review_history(symbols=["IAUM"], days=30, conn=conn)
            finally:
                conn.close()
        self.assertIn("IAUM", hist)
        dates = [row["as_of_date"] for row in hist["IAUM"]]
        self.assertEqual(dates, ["2026-05-20", "2026-05-21", "2026-05-22"])
        self.assertEqual([row["action_label"] for row in hist["IAUM"]],
                         ["持有观察", "减仓观察", "风险复查"])

    def test_history_dedups_same_day_multiple_runs_to_latest(self):
        """同一天有两次 run (e.g. 早晨自动 + 手动重算),取 generated_at 更晚的那次。"""
        import time
        with tempfile.TemporaryDirectory() as d:
            conn = stock_db.get_db(str(Path(d) / "test.duckdb"))
            try:
                holding_id = self._insert_holding(conn, symbol="IAUM")
                stock_db.save_real_holding_review(
                    {"review_run_id": "morning", "as_of_date": "2026-05-22",
                     "status": "generated", "holding_count": 1, "data_quality": "OK", "notes": ""},
                    [{"holding_id": holding_id, "symbol": "IAUM", "market": "US", "asset_class": "commodity",
                      "treatment_class": "risk_only", "score": None, "coverage_score": 0.7,
                      "rating": "tracking", "action_label": "持有观察", "action_priority": 6,
                      "pnl_pct": -1.0, "reasons": [], "risk_flags": [], "data_flags": []}],
                    conn=conn,
                )
                time.sleep(0.05)  # 保证 generated_at 不同
                stock_db.save_real_holding_review(
                    {"review_run_id": "rerun", "as_of_date": "2026-05-22",
                     "status": "generated", "holding_count": 1, "data_quality": "OK", "notes": ""},
                    [{"holding_id": holding_id, "symbol": "IAUM", "market": "US", "asset_class": "commodity",
                      "treatment_class": "risk_only", "score": None, "coverage_score": 0.7,
                      "rating": "tracking", "action_label": "风险复查", "action_priority": 1,
                      "pnl_pct": -9.0, "reasons": [], "risk_flags": [], "data_flags": []}],
                    conn=conn,
                )
                hist = stock_db.fetch_real_holding_review_history(symbols=["IAUM"], days=30, conn=conn)
            finally:
                conn.close()
        self.assertEqual(len(hist["IAUM"]), 1)
        self.assertEqual(hist["IAUM"][0]["action_label"], "风险复查")  # 后写的赢

    def test_history_empty_when_no_data(self):
        with tempfile.TemporaryDirectory() as d:
            conn = stock_db.get_db(str(Path(d) / "test.duckdb"))
            try:
                hist = stock_db.fetch_real_holding_review_history(symbols=["IAUM"], days=14, conn=conn)
            finally:
                conn.close()
        self.assertEqual(hist, {})

    def test_history_no_symbol_filter_returns_all(self):
        with tempfile.TemporaryDirectory() as d:
            conn = stock_db.get_db(str(Path(d) / "test.duckdb"))
            try:
                iaum_id = self._insert_holding(conn, symbol="IAUM")
                mcd_id = self._insert_holding(conn, symbol="MCD")
                stock_db.save_real_holding_review(
                    {"review_run_id": "r1", "as_of_date": "2026-05-22",
                     "status": "generated", "holding_count": 2, "data_quality": "OK", "notes": ""},
                    [
                        {"holding_id": iaum_id, "symbol": "IAUM", "market": "US", "asset_class": "commodity",
                         "treatment_class": "risk_only", "score": None, "coverage_score": 0.7,
                         "rating": "tracking", "action_label": "仅风控跟踪", "action_priority": 7,
                         "pnl_pct": -1.0, "reasons": [], "risk_flags": [], "data_flags": []},
                        {"holding_id": mcd_id, "symbol": "MCD", "market": "US", "asset_class": "equity",
                         "treatment_class": "stock_score", "score": 72, "coverage_score": 0.8,
                         "rating": "buy", "action_label": "持有观察", "action_priority": 6,
                         "pnl_pct": 2.0, "reasons": [], "risk_flags": [], "data_flags": []},
                    ],
                    conn=conn,
                )
                hist = stock_db.fetch_real_holding_review_history(days=30, conn=conn)
            finally:
                conn.close()
        self.assertEqual(set(hist.keys()), {"IAUM", "MCD"})

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

    def test_manual_watchlist_score_uses_final_scored_rows_and_prefers_exact_symbol(self):
        cache = {
            "date": "2026-05-22",
            "generated_at": "2026-05-22T20:30:00",
            "cutoff": 0.33,
            "factor_weights_used": {"reversal": 1.0},
            "watchlist_scores": [
                {"code": "BRK-B", "market": "US", "name": "Berkshire Hathaway",
                 "total_score": 79.67, "coverage_score": 0.8, "rating": "⭐⭐ 推荐", "signal": "buy"},
                {"code": "BRK.B", "market": "US", "name": "stale alias",
                 "total_score": -100.0, "coverage_score": 0.0, "rating": "旧别名", "signal": "watch"},
            ],
        }
        with patch("stock_research.jobs.real_holding_review._load_json", return_value=cache):
            out = _manual_watchlist_score_fallbacks(["BRK-B"])
        self.assertEqual(out["BRK-B"]["total_score"], 79.67)
        self.assertEqual(out["BRK-B"]["name"], "Berkshire Hathaway")

    def test_hk_watchlist_score_fallback_reads_factor_cache(self):
        cache = {
            "updated_at": "2026-05-22T20:40:00",
            "items": {
                "9992.HK": {
                    "date": date.today().isoformat(),
                    "factor": {
                        "piotroski": {"f_score": 6},
                        "momentum": {"momentum_12_1": -18.26, "reversal_1m": 4.39},
                    },
                },
            },
        }
        with patch("stock_research.jobs.real_holding_review._load_json", return_value=cache):
            out = _hk_watchlist_score_fallbacks(["9992.HK"])
        self.assertIn("9992.HK", out)
        self.assertAlmostEqual(out["9992.HK"]["total_score"], 56.67, places=2)
        self.assertEqual(out["9992.HK"]["rating"], "⭐ 关注")
        self.assertEqual(out["9992.HK"]["signal"], "watch")

    def test_hk_fallback_signal_uses_absolute_threshold_not_holdings_rank(self):
        """单只持仓 composite<0.60 不得因 tertile 在持仓内排第一而变成 buy。"""
        cache = {
            "updated_at": "2026-05-22T20:40:00",
            "items": {
                "9992.HK": {
                    "date": date.today().isoformat(),
                    "factor": {
                        "piotroski": {"f_score": 6},
                        "momentum": {"momentum_12_1": -18.26, "reversal_1m": 4.39},
                    },
                },
                "0700.HK": {
                    "date": date.today().isoformat(),
                    "factor": {
                        "piotroski": {"f_score": 8},
                        "momentum": {"momentum_12_1": 25.0, "reversal_1m": 8.0},
                    },
                },
            },
        }
        with patch("stock_research.jobs.real_holding_review._load_json", return_value=cache):
            out = _hk_watchlist_score_fallbacks(["9992.HK", "0700.HK"])
        self.assertLess(out["9992.HK"]["total_score"] / 100.0, 0.60)
        self.assertEqual(out["9992.HK"]["signal"], "watch")
        self.assertGreaterEqual(out["0700.HK"]["total_score"] / 100.0, 0.60)
        self.assertEqual(out["0700.HK"]["signal"], "buy")

    def test_hk_watchlist_score_accepts_recent_snapshot(self):
        cache = {
            "updated_at": "2026-05-22T20:40:00",
            "items": {
                "9992.HK": {
                    "date": (date.today() - timedelta(days=2)).isoformat(),
                    "factor": {
                        "piotroski": {"f_score": 6},
                        "momentum": {"momentum_12_1": -18.26, "reversal_1m": 4.39},
                    },
                },
            },
        }
        with patch("stock_research.jobs.real_holding_review._load_json", return_value=cache):
            out = _hk_watchlist_score_fallbacks(["9992.HK"], max_age_days=3)
        self.assertIn("9992.HK", out)

    def test_hk_watchlist_score_rejects_stale_snapshot(self):
        cache = {
            "updated_at": "2026-05-22T20:40:00",
            "items": {
                "9992.HK": {
                    "date": (date.today() - timedelta(days=4)).isoformat(),
                    "factor": {
                        "piotroski": {"f_score": 6},
                        "momentum": {"momentum_12_1": -18.26, "reversal_1m": 4.39},
                    },
                },
            },
        }
        with patch("stock_research.jobs.real_holding_review._load_json", return_value=cache):
            out = _hk_watchlist_score_fallbacks(["9992.HK"], max_age_days=3)
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()
