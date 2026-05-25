"""周末复盘 job 单测。"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import duckdb

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from stock_research.jobs.weekly_self_review import (  # type: ignore
    DISOBEDIENT_ACTIONS,
    _calendar_week_bounds,
    _had_increase_during_week,
    _lookup_symbol_info,
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

    def test_a_share_add_rounds_to_lot_100(self):
        """A 股建议加仓必须按 100 股向下取整，且缺口不足 1 手时不出建议。"""
        rules = _default_rules()
        # 加 ¥10000 / ¥15.0 ≈ 666 股 → 应取整到 600 股
        adv = _suggest_size_advisory(
            rules=rules,
            action="关注加仓",
            symbol="002463.SZ",
            shares=0,
            current_price=15.0,
            fx=1.0,
            current_value_rmb=0.0,
            current_weight=0.0,
            target_weight=0.04,
            total_capital=500000,
            treatment_class="stock_score",
        )
        self.assertIsNotNone(adv)
        self.assertEqual(adv.get("lot_size"), 100)
        sug = adv.get("suggested_shares") or 0
        self.assertGreater(sug, 0)
        self.assertEqual(sug % 100, 0, "A 股建议股数必须是 100 的倍数")

    def test_a_share_skips_when_add_below_one_lot(self):
        """缺口 < 1 手金额时不出加仓建议（avoid '加 30 股 A 股' footgun）。"""
        rules = _default_rules()
        # 缺 ¥500 / ¥15 ≈ 33 股 < 100 股一手 → 不应出建议
        adv = _suggest_size_advisory(
            rules=rules,
            action="关注加仓",
            symbol="002463.SZ",
            shares=100,
            current_price=15.0,
            fx=1.0,
            current_value_rmb=1500.0,
            current_weight=0.003,
            target_weight=0.004,
            total_capital=500000,
            treatment_class="stock_score",
        )
        self.assertIsNone(adv)

    def test_us_add_keeps_single_share(self):
        """美股 lot=1，单股建议仍然有效（向后兼容）。"""
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
        self.assertEqual(adv.get("lot_size"), 1)
        self.assertGreater(adv.get("suggested_shares") or 0, 0)


class LookupSymbolInfoSQLTest(unittest.TestCase):
    """_lookup_symbol_info 是 _ 私有 SQL，必须用真 DuckDB 喂一遍。

    这条 test 防回归：之前 SQL 缺了一对括号导致 parser 失败，
    上层全 mock 的 build_weekly_self_review test 抓不到。
    """

    def _setup_db(self) -> "duckdb.DuckDBPyConnection":
        conn = duckdb.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE system_universe (
                symbol VARCHAR, name VARCHAR, market VARCHAR, active BOOLEAN
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE manual_watchlist (
                symbol VARCHAR, name VARCHAR, market VARCHAR
            )
            """
        )
        conn.execute("INSERT INTO system_universe VALUES ('002463.SZ', '沪电股份', 'CN', TRUE)")
        conn.execute("INSERT INTO manual_watchlist VALUES ('NVDA', 'NVIDIA', 'US')")
        return conn

    def test_sql_parses_and_aggregates_two_sources(self):
        conn = self._setup_db()
        try:
            info = _lookup_symbol_info(conn, ["002463.SZ", "NVDA", "MISSING.SS"])
        finally:
            conn.close()
        self.assertEqual(info.get("002463.SZ", {}).get("name"), "沪电股份")
        self.assertEqual(info.get("NVDA", {}).get("name"), "NVIDIA")
        self.assertEqual(info.get("002463.SZ", {}).get("market"), "CN")

    def test_empty_symbols_returns_empty(self):
        conn = self._setup_db()
        try:
            self.assertEqual(_lookup_symbol_info(conn, []), {})
        finally:
            conn.close()


class PerMarketTopNSQLTest(unittest.TestCase):
    """fetch_recommendation_picks_for_run(per_market_top_n=N) 必须每市场各取前 N。

    防回归：build_v2_recommendations 写 rank 时按 market 分段（CN=1..20、HK=21..40、
    US=41..60），全局 LIMIT 10 会只剩 CN。weekly_self_review 必须走 QUALIFY 分组裁切。
    """

    def _setup_db(self) -> "duckdb.DuckDBPyConnection":
        conn = duckdb.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE recommendation_picks (
                run_id VARCHAR, market VARCHAR, symbol VARCHAR, name VARCHAR,
                rank INTEGER, rating VARCHAR, signal VARCHAR, total_score DOUBLE,
                factor_scores_json VARCHAR, entry_price DOUBLE,
                entry_currency VARCHAR, universe_scope VARCHAR,
                source_origin VARCHAR, recommendation_reason VARCHAR,
                risk_flags_json VARCHAR, created_at TIMESTAMP
            )
            """
        )
        # 模拟 build_v2_recommendations 写入：CN rank 1..3、HK rank 4..6、US rank 7..9
        rows = [
            ("r1", "CN", "002463.SZ", "沪电股份", 1, "buy", "buy", 92.0),
            ("r1", "CN", "002709.SZ", "天赐材料", 2, "buy", "buy", 90.0),
            ("r1", "CN", "300037.SZ", "新宙邦", 3, "buy", "buy", 88.0),
            ("r1", "HK", "0992.HK", "联想集团", 4, "buy", "buy", 85.0),
            ("r1", "HK", "1347.HK", "华虹半导体", 5, "buy", "buy", 83.0),
            ("r1", "HK", "2382.HK", "舜宇光学", 6, "buy", "buy", 81.0),
            ("r1", "US", "DELL", "Dell", 7, "buy", "buy", 78.0),
            ("r1", "US", "MU", "Micron", 8, "buy", "buy", 76.0),
            ("r1", "US", "NXPI", "NXP", 9, "buy", "buy", 74.0),
        ]
        for r in rows:
            conn.execute(
                "INSERT INTO recommendation_picks (run_id, market, symbol, name, rank, rating, signal, total_score) VALUES (?,?,?,?,?,?,?,?)",
                r,
            )
        return conn

    def test_global_top_n_returns_only_cn(self):
        """top_n=3 全局裁切 → 只剩 CN（这是 bug 现象的回归测试）。"""
        import sys
        sys.path.insert(0, str(REPO / "scripts" / "lib"))
        import stock_db  # type: ignore
        conn = self._setup_db()
        try:
            picks = stock_db.fetch_recommendation_picks_for_run("r1", top_n=3, conn=conn)
        finally:
            conn.close()
        self.assertEqual(len(picks), 3)
        self.assertTrue(all(p["market"] == "CN" for p in picks))

    def test_per_market_top_n_balances_three_markets(self):
        """per_market_top_n=2 → CN 2 + HK 2 + US 2 = 6 行（防 bug 再发）。"""
        import sys
        sys.path.insert(0, str(REPO / "scripts" / "lib"))
        import stock_db  # type: ignore
        conn = self._setup_db()
        try:
            picks = stock_db.fetch_recommendation_picks_for_run("r1", per_market_top_n=2, conn=conn)
        finally:
            conn.close()
        markets = [p["market"] for p in picks]
        self.assertEqual(markets.count("CN"), 2)
        self.assertEqual(markets.count("HK"), 2)
        self.assertEqual(markets.count("US"), 2)


class DisobedientActionsConsistencyTest(unittest.TestCase):
    """DISOBEDIENT_ACTIONS 必须与 real_holding_review._review_action 实际产出的 label 对齐。"""

    def test_constant_is_subset_of_known_labels(self):
        from stock_research.jobs.real_holding_review import DISOBEDIENT_ACTIONS as src
        self.assertIs(DISOBEDIENT_ACTIONS, src, "weekly 和 real_holding_review 必须复用同一常量")
        # 这两个 label 实际由 _review_action 在多个分支返回
        self.assertIn("风险复查", DISOBEDIENT_ACTIONS)
        self.assertIn("减仓观察", DISOBEDIENT_ACTIONS)


if __name__ == "__main__":
    unittest.main()
