from __future__ import annotations

from datetime import date, timedelta
import unittest

import duckdb

from scripts.tools.build_dual_track_ranking import _strict_pick_payload


def _insert_prices(conn, symbol: str, start_close: float, end_close: float) -> None:
    start = date(2026, 1, 1)
    for i in range(20):
        trade_date = start + timedelta(days=i)
        close = start_close + (end_close - start_close) * (i / 19)
        conn.execute(
            "INSERT INTO price_daily VALUES ('US', ?, ?, ?)",
            [symbol, trade_date, close],
        )


class TestDailyStrictPicks(unittest.TestCase):
    def test_filters_expensive_and_falling_knife(self) -> None:
        conn = duckdb.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE price_daily (
                market VARCHAR,
                symbol VARCHAR,
                trade_date DATE,
                close DOUBLE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE analyst_grade_events (
                symbol VARCHAR,
                event_date DATE,
                price_target DOUBLE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE chain_metadata (
                market VARCHAR,
                symbol VARCHAR,
                layman_intro VARCHAR
            )
            """
        )

        _insert_prices(conn, "PRICEY", 90, 100)   # target 100 => high 85, expensive
        _insert_prices(conn, "KNIFE", 110, 80)    # -27%, falling knife
        _insert_prices(conn, "OK1", 90, 80)
        _insert_prices(conn, "OK2", 88, 80)
        _insert_prices(conn, "OK3", 86, 80)
        for sym in ("PRICEY", "KNIFE", "OK1", "OK2", "OK3"):
            conn.execute(
                "INSERT INTO analyst_grade_events VALUES (?, '2026-01-20', 100)",
                [sym],
            )
        conn.execute("INSERT INTO chain_metadata VALUES ('US', 'OK1', '测试公司一句话')")

        rows = [
            {"symbol": "PRICEY", "name": "Too Expensive", "new_rank": 1, "prod_rank": 1},
            {"symbol": "KNIFE", "name": "Falling Knife", "new_rank": 2, "prod_rank": 2},
            {"symbol": "OK1", "name": "Ok One", "new_rank": 3, "prod_rank": 3},
            {"symbol": "OK2", "name": "Ok Two", "new_rank": 4, "prod_rank": 4},
            {"symbol": "OK3", "name": "Ok Three", "new_rank": 5, "prod_rank": 5},
        ]
        payload = _strict_pick_payload(
            {
                "generated_at": "2026-01-21T09:00:00",
                "markets": {"US": {"run_date": "2026-01-20", "candidate_focus_top10": rows}},
            },
            conn,
        )

        self.assertEqual([p["symbol"] for p in payload["picks"]], ["OK1", "OK2", "OK3"])
        self.assertEqual(
            {e["symbol"]: e["reason"] for e in payload["excluded"]},
            {"PRICEY": "剔贵", "KNIFE": "接飞刀"},
        )
        self.assertEqual(payload["picks"][0]["intro"], "测试公司一句话")
        self.assertEqual(payload["empty_slots"], 0)


if __name__ == "__main__":
    unittest.main()
