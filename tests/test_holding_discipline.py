"""真实持仓纪律计划 / 触发提醒回归测试."""
from __future__ import annotations

import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import stock_db  # type: ignore


def _mrvl_payload() -> dict:
    return {
        "plan_type": "event_trade",
        "source_type": "manual_confirmed",
        "validation_status": "manual_guardrail_unvalidated",
        "thesis": "高波动事件仓，按事前价位纪律提醒。",
        "triggers": [
            {
                "trigger_type": "invalidation",
                "comparator": "lt",
                "threshold_price": 240,
                "severity": "critical",
                "priority": 5,
                "action_label": "交易逻辑失败",
                "suggested_size_text": "别硬扛",
            },
            {
                "trigger_type": "risk_review",
                "comparator": "lt",
                "threshold_price": 250,
                "severity": "warning",
                "priority": 10,
                "action_label": "风险复查",
                "suggested_size_text": "减半或退出",
            },
            {
                "trigger_type": "profit_trim",
                "comparator": "between",
                "price_min": 300,
                "price_max": 320,
                "severity": "warning",
                "priority": 20,
                "action_label": "止盈复查",
                "suggested_size_text": "可卖 10-15 股锁利润",
            },
            {
                "trigger_type": "hold_no_add",
                "comparator": "between_open_high",
                "price_min": 285,
                "price_max": 300,
                "severity": "info",
                "priority": 30,
                "action_label": "持有不加仓",
                "suggested_size_text": "持有，不加仓",
            },
            {
                "trigger_type": "watch_no_add",
                "comparator": "between",
                "price_min": 260,
                "price_max": 265,
                "severity": "info",
                "priority": 40,
                "action_label": "观察不加仓",
            },
        ],
    }


class HoldingDisciplineTest(unittest.TestCase):
    def _db(self):
        tmp = tempfile.TemporaryDirectory()
        conn = stock_db.get_db(str(Path(tmp.name) / "test.duckdb"))
        return tmp, conn

    def _insert_mrvl(self, conn) -> int:
        return int(stock_db.insert_real_holding_result({
            "symbol": "MRVL",
            "market": "US",
            "entry_price": 272.54,
            "shares": 34,
            "entry_date": "2026-06-02",
            "currency": "USD",
        }, conn=conn)["id"])

    def test_create_plan_is_bound_to_real_holding_and_advisory_only(self):
        tmp, conn = self._db()
        try:
            holding_id = self._insert_mrvl(conn)
            before = stock_db.fetch_real_holding_by_id(holding_id, conn=conn)
            plan = stock_db.create_real_holding_discipline_plan(holding_id, _mrvl_payload(), conn=conn)
            fetched = stock_db.fetch_real_holding_discipline_plans(holding_id=holding_id, conn=conn)
            after = stock_db.fetch_real_holding_by_id(holding_id, conn=conn)
        finally:
            conn.close()
            tmp.cleanup()

        self.assertEqual(plan["holding_id"], holding_id)
        self.assertEqual(plan["symbol"], "MRVL")
        self.assertEqual(plan["status"], "active")
        self.assertEqual(len(plan["triggers"]), 5)
        self.assertTrue(all(t["auto_trade_allowed"] is False for t in plan["triggers"]))
        self.assertEqual(len(fetched), 1)
        self.assertEqual(before["entry_price"], after["entry_price"])
        self.assertEqual(before["shares"], after["shares"])

    def test_one_active_plan_per_holding(self):
        tmp, conn = self._db()
        try:
            holding_id = self._insert_mrvl(conn)
            stock_db.create_real_holding_discipline_plan(holding_id, _mrvl_payload(), conn=conn)
            with self.assertRaises(stock_db.DisciplinePlanConflict):
                stock_db.create_real_holding_discipline_plan(holding_id, _mrvl_payload(), conn=conn)
        finally:
            conn.close()
            tmp.cleanup()

    def test_evaluate_price_triggers_and_stale_price_blocks_actions(self):
        tmp, conn = self._db()
        try:
            holding_id = self._insert_mrvl(conn)
            plan = stock_db.create_real_holding_discipline_plan(holding_id, _mrvl_payload(), conn=conn)
        finally:
            conn.close()
            tmp.cleanup()

        stale = stock_db.evaluate_real_holding_discipline_plan(
            plan,
            current_price=305,
            price_trade_date="2026-06-01",
            price_is_stale=True,
        )
        self.assertEqual(stale["status"], "stale_price")
        self.assertFalse(stale["triggered"])
        self.assertTrue(stale["data_blocked"])

        profit = stock_db.evaluate_real_holding_discipline_plan(plan, current_price=305, price_trade_date="2026-06-03")
        self.assertEqual(profit["status"], "triggered")
        self.assertEqual(profit["action_label"], "止盈复查")
        self.assertTrue(any(t["action_label"] == "风险复查" for t in profit["all_triggers"]))
        self.assertTrue(any(
            t["action_label"] == "持有不加仓" and t["suggested_size_text"] == "持有，不加仓"
            for t in profit["all_triggers"]
        ))

        risk = stock_db.evaluate_real_holding_discipline_plan(plan, current_price=249, price_trade_date="2026-06-03")
        self.assertEqual(risk["action_label"], "风险复查")

        invalid = stock_db.evaluate_real_holding_discipline_plan(plan, current_price=239, price_trade_date="2026-06-03")
        self.assertEqual(invalid["action_label"], "交易逻辑失败")

        watching = stock_db.evaluate_real_holding_discipline_plan(plan, current_price=272.54, price_trade_date="2026-06-03")
        self.assertEqual(watching["status"], "watching")

    def test_review_snapshot_persists_discipline_and_dedupes_events(self):
        tmp, conn = self._db()
        try:
            holding_id = self._insert_mrvl(conn)
            plan = stock_db.create_real_holding_discipline_plan(holding_id, _mrvl_payload(), conn=conn)
            discipline = stock_db.evaluate_real_holding_discipline_plan(
                plan,
                current_price=305,
                price_trade_date="2026-06-03",
            )
            base_item = {
                "holding_id": holding_id,
                "account": "default",
                "symbol": "MRVL",
                "market": "US",
                "asset_class": "equity",
                "treatment_class": "stock_score",
                "score": 70.0,
                "coverage_score": 0.8,
                "rating": "buy",
                "action_label": "持有观察",
                "action_priority": 6,
                "current_price": 305,
                "current_currency": "USD",
                "price_trade_date": "2026-06-03",
                "reasons": [],
                "risk_flags": [],
                "data_flags": [],
                "discipline": discipline,
            }
            stock_db.save_real_holding_review(
                {"review_run_id": "r_disc_1", "as_of_date": "2026-06-03",
                 "status": "generated", "holding_count": 1, "data_quality": "OK", "notes": ""},
                [base_item],
                conn=conn,
            )
            stock_db.save_real_holding_review(
                {"review_run_id": "r_disc_2", "as_of_date": "2026-06-03",
                 "status": "generated", "holding_count": 1, "data_quality": "OK", "notes": ""},
                [base_item],
                conn=conn,
            )
            latest = stock_db.fetch_latest_real_holding_review(conn=conn)
            events = stock_db.fetch_real_holding_discipline_events(holding_id=holding_id, conn=conn)
        finally:
            conn.close()
            tmp.cleanup()

        self.assertIsNotNone(latest)
        item = latest["items"][0]
        self.assertEqual(item["discipline"]["status"], "triggered")
        self.assertEqual(item["discipline"]["action_label"], "止盈复查")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action_label"], "止盈复查")

    def test_api_create_discipline_endpoint(self):
        try:
            from fastapi.testclient import TestClient
            from stock_research.api.main import create_app
        except (ImportError, RuntimeError) as exc:
            if "httpx" not in str(exc).lower() and not isinstance(exc, ImportError):
                raise
            self.skipTest("fastapi/httpx test client deps not installed")

        fake_plan = {
            "plan_id": "disc_test",
            "holding_id": 7,
            "symbol": "MRVL",
            "status": "active",
            "triggers": [{"trigger_id": "t1", "auto_trade_allowed": False}],
        }
        app = create_app()
        with mock.patch("stock_db.create_real_holding_discipline_plan", return_value=fake_plan) as create_mock:
            r = TestClient(app).post("/api/real-holdings/7/discipline", json=_mrvl_payload())
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["plan"]["plan_id"], "disc_test")
        create_mock.assert_called_once()


class DisciplineTemplateDraftTest(unittest.TestCase):
    """建仓自动模板草稿（2026-06-12 固定规则）：成本±15% 三线 + 保本锁公式。"""

    def _db(self):
        tmp = tempfile.TemporaryDirectory()
        conn = stock_db.get_db(str(Path(tmp.name) / "test.duckdb"))
        return tmp, conn

    def test_formula_us_basic(self):
        payload = stock_db.build_discipline_template_draft(
            {"avg_cost_local_per_share": 100.0, "remaining_shares": 1000, "market": "US"}
        )
        self.assertEqual(payload["source_type"], "rule_template")
        self.assertEqual(payload["validation_status"], "template_draft_unconfirmed")
        by_type = {t["trigger_type"]: t for t in payload["triggers"]}
        self.assertEqual(by_type["invalidation_review"]["price_max"], 85.0)
        self.assertEqual(by_type["cost_line_review"]["price_max"], 100.0)
        self.assertEqual(by_type["strength_trim"]["price_min"], 115.0)
        # N = 1000×15÷30 = 500，立于不败：锁 500×15 = 剩 500×15
        self.assertIn("卖500股,保留500股", by_type["strength_trim"]["suggested_size_text"])

    def test_low6m_snap_and_hk_lot_rounding(self):
        # 泡泡玛特口径复算：C=167 S=3000，半年低点 160 落在 (141.95, 161.99) 吸附
        payload = stock_db.build_discipline_template_draft(
            {"avg_cost_local_per_share": 167.0, "remaining_shares": 3000, "market": "HK"},
            low6m_price=160.0,
        )
        by_type = {t["trigger_type"]: t for t in payload["triggers"]}
        self.assertEqual(by_type["invalidation_review"]["price_max"], 160.0)
        self.assertIn("吸附半年低点", by_type["invalidation_review"]["action_label"] + by_type["invalidation_review"]["rationale"])
        # N = 3000×7÷32.05 = 655.2 → 港股向上取整到 700
        self.assertIn("卖700股,保留2300股", by_type["strength_trim"]["suggested_size_text"])

    def test_low6m_below_band_ignored(self):
        payload = stock_db.build_discipline_template_draft(
            {"avg_cost_local_per_share": 167.0, "remaining_shares": 3000, "market": "HK"},
            low6m_price=140.0,  # < 0.85×167，太远不吸附
        )
        by_type = {t["trigger_type"]: t for t in payload["triggers"]}
        self.assertAlmostEqual(by_type["invalidation_review"]["price_max"], 141.95, places=2)

    def test_tiny_position_trim_becomes_full_exit_wording(self):
        payload = stock_db.build_discipline_template_draft(
            {"avg_cost_local_per_share": 417.0, "remaining_shares": 1, "market": "US"}
        )
        by_type = {t["trigger_type"]: t for t in payload["triggers"]}
        self.assertIn("清仓复查", by_type["strength_trim"]["suggested_size_text"])

    def test_concentration_warning_in_notes(self):
        h = {"avg_cost_local_per_share": 167.0, "remaining_shares": 3000, "market": "HK",
             "remaining_cost_rmb": 433078.0}
        flagged = stock_db.build_discipline_template_draft(h, total_capital=500000.0)
        self.assertIn("仓位闸", flagged["notes"])
        ok = stock_db.build_discipline_template_draft(h, total_capital=5000000.0)
        self.assertNotIn("仓位闸", ok["notes"])

    def test_missing_cost_returns_none(self):
        self.assertIsNone(stock_db.build_discipline_template_draft({"market": "US", "shares": 10}))

    def test_ensure_creates_once_and_skips_existing(self):
        tmp, conn = self._db()
        try:
            holding_id = int(stock_db.insert_real_holding_result({
                "symbol": "AAPL", "market": "US", "entry_price": 302.0,
                "shares": 5, "entry_date": "2026-06-09", "currency": "USD",
            }, conn=conn)["id"])
            plan = stock_db.ensure_discipline_template_draft(holding_id, conn=conn)
            self.assertIsNotNone(plan)
            self.assertEqual(plan["source_type"], "rule_template")
            self.assertIsNone(plan.get("confirmed_at"))  # 草稿没有"已确认"语义
            self.assertEqual(len(plan["triggers"]), 3)
            self.assertTrue(all(t["auto_trade_allowed"] is False for t in plan["triggers"]))
            # 幂等：已有 active 计划（无论草稿还是手工）不再生成
            self.assertIsNone(stock_db.ensure_discipline_template_draft(holding_id, conn=conn))
        finally:
            conn.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
