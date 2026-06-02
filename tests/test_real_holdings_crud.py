"""真实持仓录入 CRUD 回归测试."""
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


class RealHoldingsCrudTest(unittest.TestCase):
    def test_recent_identical_create_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            conn = stock_db.get_db(str(Path(d) / "test.duckdb"))
            try:
                item = {
                    "symbol": "MRVL",
                    "market": "US",
                    "entry_price": 272.54,
                    "shares": 34,
                    "entry_date": "2026-06-02",
                    "currency": "USD",
                }
                first = stock_db.insert_real_holding_result(item, conn=conn)
                second = stock_db.insert_real_holding_result(item, conn=conn)
                count = conn.execute("SELECT COUNT(*) FROM real_holdings WHERE symbol='MRVL'").fetchone()[0]
            finally:
                conn.close()
        self.assertTrue(first["created"])
        self.assertFalse(first["deduped"])
        self.assertFalse(second["created"])
        self.assertTrue(second["deduped"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(count, 1)

    def test_concurrent_duplicate_create_cleans_inserted_lot(self):
        with tempfile.TemporaryDirectory() as d:
            conn = stock_db.get_db(str(Path(d) / "test.duckdb"))
            try:
                item = {
                    "symbol": "MRVL",
                    "market": "US",
                    "entry_price": 272.54,
                    "shares": 34,
                    "entry_date": "2026-06-02",
                    "currency": "USD",
                }
                first = stock_db.insert_real_holding_result(item, conn=conn)
                real_recent = stock_db._recent_duplicate_real_holding_id
                calls = {"n": 0}

                def flaky_recent(vals, *, conn, window_minutes=10):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        return None
                    return real_recent(vals, conn=conn, window_minutes=window_minutes)

                with mock.patch.object(stock_db, "_recent_duplicate_real_holding_id", side_effect=flaky_recent):
                    second = stock_db.insert_real_holding_result(item, conn=conn)
                rows = conn.execute("SELECT id FROM real_holdings WHERE symbol='MRVL' ORDER BY id").fetchall()
            finally:
                conn.close()

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertTrue(second["deduped"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(rows, [(first["id"],)])


if __name__ == "__main__":
    unittest.main()
