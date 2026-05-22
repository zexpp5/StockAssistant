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
        # 只 sum 7 档 label 部分(不含 coverage_* 后加的字段)
        label_keys = ("stoploss_breached", "stoploss_watched", "model_weakened",
                      "near_event", "weight_off", "ai_uncovered", "normal")
        self.assertEqual(sum(r["summary"].get(k, 0) for k in label_keys), 3)


class HoldingsCoverageClassTest(unittest.TestCase):
    """Part 3 · 2026-05-22: 持仓 4 档 coverage_class 分类逻辑。"""

    def _import(self):
        from stock_research.jobs.morning_brief import (  # type: ignore
            compute_holdings_verdict, _classify_coverage, _is_needs_fix,
            _TRACKING_ONLY_TICKERS, _COVERAGE_CLASSES,
            _classify_asset_class, _ASSET_CLASSES,
        )
        return (compute_holdings_verdict, _classify_coverage, _is_needs_fix,
                _TRACKING_ONLY_TICKERS, _COVERAGE_CLASSES,
                _classify_asset_class, _ASSET_CLASSES)

    def test_four_classes_registered(self):
        _, _, _, _, classes, _, _ = self._import()
        self.assertEqual(set(classes.keys()),
                         {"ai_portfolio", "picks_only", "tracking_only", "needs_fix"})

    def test_etf_whitelist_contains_common_etfs(self):
        _, _, _, whitelist, _, _, _ = self._import()
        # 账户里的 IAUM 必须命中,其他主流也要
        for etf in ("IAUM", "GLD", "SPY", "QQQ", "TLT"):
            self.assertIn(etf, whitelist)

    def test_asset_classes_registered(self):
        _, _, _, _, _, _, classes = self._import()
        self.assertEqual(set(classes.keys()),
                         {"equity", "fund_etf", "commodity", "fixed_income",
                          "crypto", "cash", "unknown"})

    def test_asset_class_market_standard_mapping(self):
        _, _, _, _, _, classify_asset, _ = self._import()
        self.assertEqual(classify_asset("MCD", "picks_only")[0], "equity")
        self.assertEqual(classify_asset("9992.HK", "picks_only")[0], "equity")
        self.assertEqual(classify_asset("BRK-B", "picks_only")[0], "equity")
        self.assertEqual(classify_asset("IAUM", "tracking_only")[0], "commodity")
        self.assertEqual(classify_asset("SPY", "tracking_only")[0], "fund_etf")
        self.assertEqual(classify_asset("TLT", "tracking_only")[0], "fixed_income")
        self.assertEqual(classify_asset("泡泡玛特", "needs_fix")[0], "unknown")

    def test_ai_portfolio_when_has_target(self):
        _, classify, _, _, _, _, _ = self._import()
        self.assertEqual(classify("NVDA", 132.0, in_picks=True, in_target=True), "ai_portfolio")

    def test_picks_only_when_in_picks_no_target(self):
        _, classify, _, _, _, _, _ = self._import()
        # MCD: 普通股,行情有但不在 AI 组合 → picks_only
        self.assertEqual(classify("MCD", 280.0, in_picks=True, in_target=False), "picks_only")
        # 即使不在 picks 但行情有 + 不是 ETF + 不是错误 → picks_only(默认兜底)
        self.assertEqual(classify("UNKNOWN", 100.0, in_picks=False, in_target=False), "picks_only")

    def test_tracking_only_for_etfs(self):
        _, classify, _, _, _, _, _ = self._import()
        # IAUM 是 ETF,即使行情有也归 tracking_only
        self.assertEqual(classify("IAUM", 45.0, in_picks=False, in_target=False), "tracking_only")
        self.assertEqual(classify("GLD", 200.0, in_picks=False, in_target=False), "tracking_only")

    def test_needs_fix_chinese_ticker(self):
        _, classify, _, _, _, _, _ = self._import()
        self.assertEqual(classify("泡泡玛特", None, in_picks=False, in_target=False), "needs_fix")
        self.assertEqual(classify("泡泡玛特", 148.0, in_picks=False, in_target=False), "needs_fix")  # 即使有 current

    def test_needs_fix_b_class_when_no_price(self):
        _, classify, _, _, _, _, _ = self._import()
        # BRK.B 拉不到 → needs_fix
        self.assertEqual(classify("BRK.B", None, in_picks=False, in_target=False), "needs_fix")
        # 但 BRK-B(连字符)拉得到 → 不应触发 needs_fix
        self.assertEqual(classify("BRK-B", 484.0, in_picks=False, in_target=False), "picks_only")

    def test_etf_priority_higher_than_no_price(self):
        """IAUM 即使行情没拉到,仍优先 tracking_only(避免 ETF 临时数据缺失被误判为 needs_fix)。"""
        _, classify, _, _, _, _, _ = self._import()
        self.assertEqual(classify("IAUM", None, in_picks=False, in_target=False), "tracking_only")

    def test_compute_returns_coverage_class_field(self):
        compute_holdings_verdict, _, _, _, _, _, _ = self._import()
        holdings = [
            {"code": "NVDA", "entry_price": 100, "shares": 10},
            {"code": "IAUM", "entry_price": 45, "shares": 100},
            {"code": "泡泡玛特", "entry_price": 148, "shares": 1000},
        ]
        history = {"NVDA": {"close": [100, 105, 110]}}
        target_weights = {"NVDA": 0.10}
        r = compute_holdings_verdict(holdings, history, target_weights=target_weights)
        by_code = {h["code"]: h for h in r["holdings"]}
        self.assertEqual(by_code["NVDA"]["coverage_class"], "ai_portfolio")
        self.assertEqual(by_code["IAUM"]["coverage_class"], "tracking_only")
        self.assertEqual(by_code["泡泡玛特"]["coverage_class"], "needs_fix")
        self.assertEqual(by_code["NVDA"]["asset_class"], "equity")
        self.assertEqual(by_code["IAUM"]["asset_class"], "commodity")
        self.assertEqual(by_code["泡泡玛特"]["asset_class"], "unknown")
        # summary 里有 coverage 计数
        self.assertEqual(r["summary"].get("coverage_ai_portfolio"), 1)
        self.assertEqual(r["summary"].get("coverage_tracking_only"), 1)
        self.assertEqual(r["summary"].get("coverage_needs_fix"), 1)
        self.assertEqual(r["summary"].get("asset_equity"), 1)
        self.assertEqual(r["summary"].get("asset_commodity"), 1)
        self.assertEqual(r["summary"].get("asset_unknown"), 1)


class DailyVerdictEndpointTest(unittest.TestCase):
    """API 路由层 smoke (TestClient,不依赖 launchd)。"""

    def test_endpoint_returns_200_on_empty_db(self):
        try:
            from fastapi.testclient import TestClient
            from stock_research.api.main import create_app
        except (ImportError, RuntimeError) as exc:
            if "httpx" not in str(exc).lower() and not isinstance(exc, ImportError):
                raise
            self.skipTest("fastapi/httpx test client deps not installed")
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
