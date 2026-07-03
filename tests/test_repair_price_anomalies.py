from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

MODULE_PATH = REPO / "scripts" / "tools" / "repair_price_anomalies.py"
spec = importlib.util.spec_from_file_location("repair_price_anomalies", MODULE_PATH)
repair = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(repair)


class RepairPriceAnomaliesTest(unittest.TestCase):
    def test_bad_prev_close_row_is_repaired_without_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.duckdb"
            conn = duckdb.connect(str(db))
            try:
                conn.execute(
                    """
                    CREATE TABLE price_daily (
                        market VARCHAR,
                        symbol VARCHAR,
                        trade_date DATE,
                        interval VARCHAR DEFAULT '1d',
                        close DOUBLE,
                        prev_close DOUBLE,
                        one_week_pct DOUBLE,
                        source VARCHAR,
                        source_updated_at TIMESTAMP,
                        PRIMARY KEY (market, symbol, trade_date, interval)
                    )
                    """
                )
                rows = [
                    ("US", "HON", "2026-06-22", "1d", 228.11, 229.01, 0.31, "yfinance"),
                    ("US", "HON", "2026-06-23", "1d", 222.37, 228.11, -3.10, "yfinance"),
                    ("US", "HON", "2026-06-24", "1d", 227.42, 222.37, -0.52, "yfinance"),
                    ("US", "HON", "2026-06-25", "1d", 231.24, 227.42, 0.97, "yfinance"),
                    ("US", "HON", "2026-06-26", "1d", 232.21, 231.24, 1.80, "yfinance"),
                    ("US", "HON", "2026-06-29", "1d", 227.80, 464.42, -48.78, "yfinance"),
                ]
                conn.executemany("INSERT INTO price_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)", rows)

                anomalies = repair.scan(conn, threshold_pct=-40.0, source_prefix="yfinance")
                self.assertEqual(len(anomalies), 1)
                self.assertEqual(anomalies[0]["status"], "repairable_bad_prev_close")
                self.assertEqual(anomalies[0]["db_previous_close"], 232.21)

                applied = repair.apply_repairs(conn, anomalies)
                self.assertEqual(applied, 1)
                row = conn.execute(
                    """
                    SELECT prev_close, one_week_pct
                    FROM price_daily
                    WHERE market='US' AND symbol='HON' AND trade_date='2026-06-29'
                    """
                ).fetchone()
                self.assertAlmostEqual(row[0], 232.21, places=2)
                self.assertAlmostEqual(row[1], -0.14, places=2)
                self.assertEqual(anomalies[0]["status"], "fixed_bad_prev_close")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
