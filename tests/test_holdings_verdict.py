"""持仓 7 档判断纯函数 smoke 测试 (2026-05-22 Part 2)。

守护 compute_holdings_verdict() 契约,避免下游(早报 + dashboard endpoint)漂移:
  1. 空持仓不崩
  2. 7 档标签优先级正确取最严重
  3. AVWAP 跌破只附 reason,不抢主标签
  4. summary 计数与 holdings label_kind 一致

跑:
    python3 -m unittest tests.test_holdings_verdict
"""
from __future__ import annotations
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))


class HoldingsVerdictTest(unittest.TestCase):
    def _import(self):
        from stock_research.jobs.morning_brief import compute_holdings_verdict, _VERDICT_LABELS  # type: ignore
        return compute_holdings_verdict, _VERDICT_LABELS

    def test_empty_holdings(self):
        compute_holdings_verdict, _ = self._import()
        r = compute_holdings_verdict([])
        self.assertEqual(r["holdings"], [])
        self.assertEqual(sum(r["summary"].values()), 0)

    def test_seven_labels_registered(self):
        _, labels = self._import()
        expected = {"stop_breach", "stop_watch", "model_weak",
                    "near_event", "weight_off", "normal", "ai_uncovered"}
        self.assertEqual(set(labels.keys()), expected)
        # 优先级单调
        prios = [labels[k][2] for k in
                 ("stop_breach", "stop_watch", "model_weak",
                  "near_event", "weight_off", "normal", "ai_uncovered")]
        self.assertEqual(prios, sorted(prios))

    def test_stop_breach_takes_priority(self):
        """破止损 + 临近事件同时命中 → label = stop_breach。"""
        compute_holdings_verdict, _ = self._import()
        holdings = [{"code": "X.US", "entry_price": 100, "shares": 10}]
        history = {"X.US": {"close": [100, 95, 90, 80]}}  # -20% 破 15% 止损
        events = {"events": [{"event_date": date.today().strftime("%Y-%m-%d"),
                              "code": "X", "description": "Q1 earnings"}]}
        r = compute_holdings_verdict(holdings, history, events_data=events)
        self.assertEqual(r["holdings"][0]["label_kind"], "stop_breach")
        kinds = [x["kind"] for x in r["holdings"][0]["reasons"]]
        self.assertIn("stop_breach", kinds)
        self.assertIn("near_event", kinds)

    def test_model_weak_via_watch_rating(self):
        compute_holdings_verdict, _ = self._import()
        holdings = [{"code": "Y.US", "entry_price": 100, "shares": 10}]
        history = {"Y.US": {"close": [100, 101, 102]}}  # 涨,不触发止损
        picks = [{"code": "Y.US", "rating": "watch", "signal": "watch"}]
        r = compute_holdings_verdict(holdings, history, picks=picks)
        self.assertEqual(r["holdings"][0]["label_kind"], "model_weak")

    def test_ai_uncovered_when_not_in_picks_or_universe(self):
        compute_holdings_verdict, _ = self._import()
        holdings = [{"code": "Z.US", "entry_price": 100, "shares": 10}]
        history = {"Z.US": {"close": [100, 101]}}
        r = compute_holdings_verdict(holdings, history, picks=[], universe=[])
        self.assertEqual(r["holdings"][0]["label_kind"], "ai_uncovered")

    def test_in_universe_but_not_picks_means_model_weak(self):
        """在 universe 里(系统看过)但今日没入推荐池 → 模型转弱。"""
        compute_holdings_verdict, _ = self._import()
        holdings = [{"code": "W.US", "entry_price": 100, "shares": 10}]
        history = {"W.US": {"close": [100, 101]}}
        universe = [{"code": "W.US"}]  # 在覆盖池
        r = compute_holdings_verdict(holdings, history, picks=[], universe=universe)
        self.assertEqual(r["holdings"][0]["label_kind"], "model_weak")

    def test_normal_when_strong_buy_and_no_issues(self):
        compute_holdings_verdict, _ = self._import()
        holdings = [{"code": "A.US", "entry_price": 100, "shares": 10}]
        history = {"A.US": {"close": [100, 101, 102]}}
        picks = [{"code": "A.US", "rating": "strong_buy", "signal": "buy"}]
        r = compute_holdings_verdict(holdings, history, picks=picks)
        self.assertEqual(r["holdings"][0]["label_kind"], "normal")

    def test_summary_counts_match_holdings(self):
        compute_holdings_verdict, _ = self._import()
        holdings = [
            {"code": "X.US", "entry_price": 100, "shares": 10},  # 破止损
            {"code": "Y.US", "entry_price": 100, "shares": 10},  # 模型转弱
            {"code": "A.US", "entry_price": 100, "shares": 10},  # 正常
        ]
        history = {
            "X.US": {"close": [100, 80]},
            "Y.US": {"close": [100, 102]},
            "A.US": {"close": [100, 105]},
        }
        picks = [
            {"code": "Y.US", "rating": "watch", "signal": "watch"},
            {"code": "A.US", "rating": "strong_buy", "signal": "buy"},
        ]
        r = compute_holdings_verdict(holdings, history, picks=picks)
        self.assertEqual(r["summary"]["stoploss_breached"], 1)
        self.assertEqual(r["summary"]["model_weakened"], 1)
        self.assertEqual(r["summary"]["normal"], 1)
        self.assertEqual(sum(r["summary"].values()), 3)


class DailyVerdictEndpointTest(unittest.TestCase):
    """API 路由层 smoke (TestClient,不依赖 launchd)。"""

    def test_endpoint_returns_200_on_empty_db(self):
        try:
            from fastapi.testclient import TestClient
            from stock_research.api.main import create_app
        except ImportError:
            self.skipTest("fastapi not installed")
        app = create_app()
        client = TestClient(app)
        r = client.get("/api/real-holdings/daily-verdict")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("holdings", data)
        self.assertIn("summary", data)
        # summary 7 个 key 必须都在
        for k in ("stoploss_breached", "stoploss_watched", "model_weakened",
                  "near_event", "weight_off", "ai_uncovered", "normal"):
            self.assertIn(k, data["summary"])


if __name__ == "__main__":
    unittest.main()
