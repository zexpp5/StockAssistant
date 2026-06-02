import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import duckdb

from scripts.tools.init_stock_db_v2 import SCHEMA_SQL
from scripts.tools import evaluate_v2_picks


class TestEvaluateV2Picks(unittest.TestCase):
    def test_non_buy_picks_do_not_write_or_keep_outcomes(self):
        today = date.today()
        run_date = today - timedelta(days=1)
        outcome_date = today

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.duckdb")
            conn = duckdb.connect(db_path)
            try:
                conn.execute(SCHEMA_SQL)
                conn.execute(
                    """INSERT INTO recommendation_runs
                       (run_id, run_date, strategy_version, model_version,
                        universe_scope, data_cutoff_at, generated_at, status)
                       VALUES ('run_test', ?, 'strategy_test', 'model_test',
                        'system_tech_universe', ?, ?, 'generated')""",
                    [run_date, datetime.combine(run_date, datetime.min.time()), datetime.now()],
                )
                conn.execute(
                    """INSERT INTO recommendation_picks
                       (run_id, market, symbol, name, rank, rating, signal,
                        total_score, entry_price, universe_scope, source_origin)
                       VALUES
                       ('run_test', 'US', 'BUY', 'Buy Co', 1, 'A', 'buy',
                        80.0, 100.0, 'system_tech_universe', 'test'),
                       ('run_test', 'US', 'WATCH', 'Watch Co', 2, 'B', 'watch',
                        59.0, 100.0, 'system_tech_universe', 'test')"""
                )
                conn.execute(
                    """INSERT INTO price_daily (market, symbol, trade_date, close, source)
                       VALUES
                       ('US', 'BUY', ?, 110.0, 'test'),
                       ('US', 'WATCH', ?, 90.0, 'test'),
                       ('US', 'SPY', ?, 100.0, 'test'),
                       ('US', 'SPY', ?, 101.0, 'test')""",
                    [outcome_date, outcome_date, run_date, outcome_date],
                )
                conn.execute(
                    """INSERT INTO pick_outcomes
                       (run_id, market, symbol, horizon, outcome_date, return_pct,
                        benchmark_symbol, benchmark_pct, alpha_pct, is_success)
                       VALUES
                       ('run_test', 'US', 'WATCH', '1d', ?, -10.0,
                        'SPY', 1.0, -11.0, FALSE)""",
                    [outcome_date],
                )
            finally:
                conn.close()

            old_db_path = os.environ.get("STOCK_DB_PATH")
            old_start = evaluate_v2_picks.PRODUCTION_METRICS_START_DATE
            os.environ["STOCK_DB_PATH"] = db_path
            evaluate_v2_picks.PRODUCTION_METRICS_START_DATE = run_date - timedelta(days=1)
            try:
                self.assertEqual(evaluate_v2_picks.main(), 0)
            finally:
                evaluate_v2_picks.PRODUCTION_METRICS_START_DATE = old_start
                if old_db_path is None:
                    os.environ.pop("STOCK_DB_PATH", None)
                else:
                    os.environ["STOCK_DB_PATH"] = old_db_path

            conn = duckdb.connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT symbol, horizon, return_pct, alpha_pct FROM pick_outcomes ORDER BY symbol"
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "BUY")
        self.assertEqual(rows[0][1], "1d")
        self.assertAlmostEqual(rows[0][2], 10.0)
        self.assertAlmostEqual(rows[0][3], 9.0)


if __name__ == "__main__":
    unittest.main()
