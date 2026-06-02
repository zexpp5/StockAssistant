import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import duckdb

from scripts.tools.init_stock_db_v2 import SCHEMA_SQL
from scripts.tools import recommendation_evidence_report as evidence


class TestRecommendationEvidenceReport(unittest.TestCase):
    def setUp(self):
        self.conn = duckdb.connect(":memory:")
        self.conn.execute(SCHEMA_SQL)
        self.old_start = evidence.PRODUCTION_METRICS_START_DATE
        self.run_date = date.today() - timedelta(days=1)
        evidence.PRODUCTION_METRICS_START_DATE = str(self.run_date - timedelta(days=1))
        self.conn.execute(
            """INSERT INTO recommendation_runs
               (run_id, run_date, strategy_version, model_version, universe_scope,
                data_cutoff_at, generated_at, status)
               VALUES ('run_test', ?, 'strategy_test', 'model_test',
                'system_tech_universe', ?, ?, 'generated')""",
            [self.run_date, datetime.combine(self.run_date, datetime.min.time()), datetime.now()],
        )
        self.conn.execute(
            """INSERT INTO recommendation_picks
               (run_id, market, symbol, name, rank, rating, signal, total_score,
                entry_price, universe_scope, source_origin)
               VALUES
               ('run_test', 'US', 'BUY', 'Buy Co', 1, 'A', 'buy', 80.0,
                100.0, 'system_tech_universe', 'test'),
               ('run_test', 'US', 'PENDING', 'Pending Co', 2, 'A', 'buy', 79.0,
                100.0, 'system_tech_universe', 'test'),
               ('run_test', 'US', 'WATCH', 'Watch Co', 3, 'B', 'watch', 59.0,
                100.0, 'system_tech_universe', 'test')"""
        )
        self.conn.execute(
            """INSERT INTO pick_outcomes
               (run_id, market, symbol, horizon, outcome_date, return_pct,
                benchmark_symbol, benchmark_pct, alpha_pct, is_success)
               VALUES
               ('run_test', 'US', 'BUY', '1d', CURRENT_DATE, 10.0,
                'SPY', 1.0, 9.0, TRUE),
               ('run_test', 'US', 'WATCH', '1d', CURRENT_DATE, -10.0,
                'SPY', 1.0, -11.0, FALSE)"""
        )

    def tearDown(self):
        evidence.PRODUCTION_METRICS_START_DATE = self.old_start
        self.conn.close()

    def test_review_coverage_counts_due_buy_picks_not_local_price_ready_only(self):
        cov = evidence._review_coverage(self.conn, "strategy_test")

        self.assertEqual(cov["total_mature"], 1)
        self.assertEqual(cov["total_reviewed"], 1)
        self.assertEqual(cov["coverage"], 1.0)
        self.assertEqual(cov["v2_by_horizon"]["1d"]["local_price_ready"], 0)
        self.assertEqual(cov["v2_by_horizon"]["1d"]["calendar_due"], 2)
        self.assertEqual(cov["v2_by_horizon"]["1d"]["pending_data_ready"], 1)

    def test_discovery_metrics_ignore_non_buy_outcomes(self):
        metrics = evidence._discovery_metrics(self.conn, "strategy_test")

        self.assertEqual(metrics["1d"]["n"], 1)
        self.assertEqual(metrics["1d"]["avg_alpha_pct"], 9.0)
        self.assertEqual(metrics["1d"]["hit_rate"], 100.0)


if __name__ == "__main__":
    unittest.main()
