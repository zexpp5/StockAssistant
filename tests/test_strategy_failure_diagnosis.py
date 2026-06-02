import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import duckdb

from scripts.tools.init_stock_db_v2 import SCHEMA_SQL
from scripts.tools import strategy_failure_diagnosis as diagnosis


class TestStrategyFailureDiagnosis(unittest.TestCase):
    def setUp(self):
        self.conn = duckdb.connect(":memory:")
        self.conn.execute(SCHEMA_SQL)
        self.old_start = diagnosis.PRODUCTION_METRICS_START_DATE
        self.run_date = date.today() - timedelta(days=1)
        diagnosis.PRODUCTION_METRICS_START_DATE = str(self.run_date - timedelta(days=1))
        self.conn.execute(
            """INSERT INTO recommendation_runs
               (run_id, run_date, strategy_version, model_version, universe_scope,
                data_cutoff_at, generated_at, status)
               VALUES ('run_test', ?, 'strategy_test', 'model_test',
                'system_tech_universe', ?, ?, 'generated')""",
            [self.run_date, datetime.combine(self.run_date, datetime.min.time()), datetime.now()],
        )
        self.conn.execute(
            """INSERT INTO system_universe
               (pool_id, market, symbol, name, theme, industry, source)
               VALUES
               ('system_tech_universe', 'CN', 'TOPBAD', 'Top Bad', 'AI', 'software', 'test'),
               ('system_tech_universe', 'CN', 'TAILBAD', 'Tail Bad', 'AI', 'software', 'test'),
               ('system_tech_universe', 'CN', 'WATCH', 'Watch Bad', 'AI', 'software', 'test')"""
        )
        self.conn.execute(
            """INSERT INTO chain_metadata (market, symbol, chain, chain_role, source)
               VALUES
               ('CN', 'TOPBAD', 'AI 软件', '应用软件', 'test'),
               ('CN', 'TAILBAD', 'AI 软件', '应用软件', 'test')"""
        )
        self.conn.execute(
            """INSERT INTO recommendation_picks
               (run_id, market, symbol, name, rank, rating, signal, total_score,
                factor_scores_json, risk_flags_json, entry_price, universe_scope, source_origin)
               VALUES
               ('run_test', 'CN', 'TOPBAD', 'Top Bad', 1, 'A', 'buy', 88,
                '{"valuation":90,"momentum":90,"reversal":90,"data_quality":100,"coverage":1,"f_score":80}',
                '["STRUCTURAL_DOWNTREND_REVIEW_GATE"]', 100, 'system_tech_universe', 'test'),
               ('run_test', 'CN', 'TAILBAD', 'Tail Bad', 12, 'A', 'buy', 80,
                '{"valuation":30,"momentum":20,"reversal":30,"data_quality":100,"coverage":1,"f_score":20}',
                '[]', 100, 'system_tech_universe', 'test'),
               ('run_test', 'CN', 'WATCH', 'Watch Bad', 13, 'B', 'watch', 59,
                '{"valuation":90,"momentum":90,"reversal":90}', '[]',
                100, 'system_tech_universe', 'test')"""
        )
        self.conn.execute(
            """INSERT INTO pick_outcomes
               (run_id, market, symbol, horizon, outcome_date, return_pct,
                benchmark_symbol, benchmark_pct, alpha_pct, is_success)
               VALUES
               ('run_test', 'CN', 'TOPBAD', '1d', CURRENT_DATE, -4, '000300.SS', 1, -5, FALSE),
               ('run_test', 'CN', 'TAILBAD', '1d', CURRENT_DATE, -1, '000300.SS', 1, -2, FALSE),
               ('run_test', 'CN', 'WATCH', '1d', CURRENT_DATE, -20, '000300.SS', 1, -21, FALSE)"""
        )
        extra_universe = [
            ("system_tech_universe", "CN", f"BAD{i:02d}", f"Bad {i}", "AI", "software", "test")
            for i in range(2, 30)
        ]
        self.conn.executemany(
            """INSERT INTO system_universe
               (pool_id, market, symbol, name, theme, industry, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            extra_universe,
        )
        extra_picks = [
            (
                "run_test", "CN", f"BAD{i:02d}", f"Bad {i}", i, "A", "buy", 85.0,
                '{"valuation":90,"momentum":90,"reversal":90,"data_quality":100,"coverage":1,"f_score":80}',
                "[]", 100.0, "system_tech_universe", "test",
            )
            for i in range(2, 30)
        ]
        self.conn.executemany(
            """INSERT INTO recommendation_picks
               (run_id, market, symbol, name, rank, rating, signal, total_score,
                factor_scores_json, risk_flags_json, entry_price, universe_scope, source_origin)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            extra_picks,
        )
        extra_outcomes = [
            ("run_test", "CN", f"BAD{i:02d}", "1d", -1.0, "000300.SS", 1.0, -2.0, False)
            for i in range(2, 30)
        ]
        self.conn.executemany(
            """INSERT INTO pick_outcomes
               (run_id, market, symbol, horizon, outcome_date, return_pct,
                benchmark_symbol, benchmark_pct, alpha_pct, is_success)
               VALUES (?, ?, ?, ?, CURRENT_DATE, ?, ?, ?, ?, ?)""",
            extra_outcomes,
        )

    def tearDown(self):
        diagnosis.PRODUCTION_METRICS_START_DATE = self.old_start
        self.conn.close()

    def test_report_ignores_non_buy_outcomes(self):
        report = diagnosis.build_report(
            self.conn,
            strategy_version="strategy_test",
            horizon="1d",
            markets=["CN"],
        )

        self.assertEqual(report["summary"]["sample_count"], 30)
        self.assertEqual(report["summary"]["negative_alpha_count"], 30)
        self.assertLess(report["market_summary"][0]["avg_alpha_pct"], -2.0)
        symbols = {item["symbol"] for item in report["worst_examples"]}
        self.assertNotIn("WATCH", symbols)

    def test_negative_market_and_top_bucket_trigger_actions(self):
        report = diagnosis.build_report(
            self.conn,
            strategy_version="strategy_test",
            horizon="1d",
            markets=["CN"],
        )

        actions = {item["action"] for item in report["recommended_actions"]}
        self.assertIn("formula_review_not_only_cut_count", actions)
        self.assertIn("factor_weight_review", actions)
        risk_rows = {
            row["risk_flag"]: row
            for row in report["risk_flag_summary"]
        }
        self.assertEqual(risk_rows["STRUCTURAL_DOWNTREND_REVIEW_GATE"]["n"], 1)


if __name__ == "__main__":
    unittest.main()
