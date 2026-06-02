"""真实持仓轻量行情刷新日期判定回归测试."""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.pipeline import intraday_refresh_holdings as refresh  # type: ignore


class IntradayRefreshHoldingsDateTest(unittest.TestCase):
    def _at(self, symbol: str, year: int, month: int, day: int, hour: int, minute: int) -> datetime:
        tz_name = {
            "HK": "Asia/Hong_Kong",
            "CN": "Asia/Shanghai",
            "US": "America/New_York",
        }[refresh._infer_market(symbol)]
        return datetime(year, month, day, hour, minute, tzinfo=refresh.zoneinfo.ZoneInfo(tz_name))

    def test_hk_premarket_does_not_write_today_without_today_bar(self):
        with patch.object(refresh, "_market_now", return_value=self._at("9992.HK", 2026, 6, 2, 9, 28)):
            self.assertIsNone(refresh._market_trade_date("9992.HK", date(2026, 6, 1)))
            self.assertIsNone(refresh._market_trade_date("9992.HK", None))

    def test_hk_regular_session_allows_fast_info_as_today(self):
        with patch.object(refresh, "_market_now", return_value=self._at("9992.HK", 2026, 6, 2, 10, 0)):
            self.assertEqual(refresh._market_trade_date("9992.HK", date(2026, 6, 1)), date(2026, 6, 2))

    def test_hk_after_close_writes_today_when_daily_bar_is_today(self):
        with patch.object(refresh, "_market_now", return_value=self._at("9992.HK", 2026, 6, 2, 16, 2)):
            self.assertEqual(refresh._market_trade_date("9992.HK", date(2026, 6, 2)), date(2026, 6, 2))

    def test_hk_after_close_skips_when_data_source_is_still_yesterday(self):
        with patch.object(refresh, "_market_now", return_value=self._at("9992.HK", 2026, 6, 2, 16, 2)):
            self.assertIsNone(refresh._market_trade_date("9992.HK", date(2026, 6, 1)))

    def test_us_premarket_does_not_relabel_yesterday_close_as_today(self):
        with patch.object(refresh, "_market_now", return_value=self._at("MCD", 2026, 6, 2, 4, 28)):
            self.assertIsNone(refresh._market_trade_date("MCD", date(2026, 6, 1)))

    def test_string_trade_date_is_accepted(self):
        with patch.object(refresh, "_market_now", return_value=self._at("9992.HK", 2026, 6, 2, 16, 2)):
            self.assertEqual(refresh._market_trade_date("9992.HK", "2026-06-02 00:00:00+08:00"), date(2026, 6, 2))

    def test_daily_bar_parser_accepts_yfinance_multiindex_close(self):
        idx = pd.to_datetime(["2026-06-01", "2026-06-02"])
        cols = pd.MultiIndex.from_tuples([("Close", "9992.HK")])
        df = pd.DataFrame([[179.6], [179.2]], index=idx, columns=cols)
        with patch.object(refresh.yf, "download", return_value=df):
            daily = refresh._latest_daily_bar("9992.HK")
        self.assertEqual(daily["trade_date"], date(2026, 6, 2))
        self.assertAlmostEqual(daily["price"], 179.2)
        self.assertAlmostEqual(daily["prev_close"], 179.6)

    def test_fetch_one_uses_daily_bar_when_fast_info_raises(self):
        class BrokenTicker:
            @property
            def fast_info(self):
                raise RuntimeError("fast_info broken")

        daily = {"price": 179.2, "prev_close": 179.6, "trade_date": date(2026, 6, 2)}
        with patch.object(refresh, "_hk_native_quote", return_value=None), \
             patch.object(refresh, "_latest_daily_bar", return_value=daily), \
             patch.object(refresh.yf, "Ticker", return_value=BrokenTicker()):
            row = refresh._fetch_one("9992.HK")
        self.assertEqual(row["symbol"], "9992.HK")
        self.assertEqual(row["currency"], "HKD")
        self.assertAlmostEqual(row["price"], 179.2)
        self.assertEqual(row["trade_date"], date(2026, 6, 2))

    def test_hk_fetch_prefers_native_quote(self):
        quote = {
            "price": 184.4,
            "change_pct": 2.0,
            "source": "akshare/stock_hk_spot_em",
        }
        with patch.object(refresh, "_market_now", return_value=self._at("9992.HK", 2026, 6, 2, 16, 10)), \
             patch.object(refresh.akshare_client, "fetch_hk_stock_quote", return_value=quote):
            row = refresh._fetch_one("9992.HK")
        self.assertEqual(row["symbol"], "9992.HK")
        self.assertEqual(row["trade_date"], date(2026, 6, 2))
        self.assertEqual(row["source"], "akshare/stock_hk_spot_em")
        self.assertAlmostEqual(row["price"], 184.4)
        self.assertAlmostEqual(row["prev_close"], 184.4 / 1.02)

    def test_hk_native_quote_premarket_tags_previous_session(self):
        quote = {
            "price": 184.4,
            "change_pct": 2.0,
            "source": "akshare/stock_hk_spot_em",
        }
        with patch.object(refresh, "_market_now", return_value=self._at("9992.HK", 2026, 6, 3, 0, 5)), \
             patch.object(refresh.akshare_client, "fetch_hk_stock_quote", return_value=quote):
            row = refresh._fetch_one("9992.HK")
        self.assertEqual(row["trade_date"], date(2026, 6, 2))

    def test_us_large_yfinance_jump_is_written_with_caution_source(self):
        class WeirdTicker:
            fast_info = {
                "lastPrice": 274.49,
                "previousClose": 219.493,
                "currency": "USD",
            }

        daily = {"price": 274.49, "prev_close": 219.493, "trade_date": date(2026, 6, 2)}
        with patch.object(refresh, "_market_now", return_value=self._at("MRVL", 2026, 6, 2, 11, 5)), \
             patch.object(refresh, "_latest_daily_bar", return_value=daily), \
             patch.object(refresh.yf, "Ticker", return_value=WeirdTicker()):
            row = refresh._fetch_one("MRVL")
        self.assertFalse(row.get("skip_write", False))
        self.assertEqual(row["source"], "yfinance_intraday_large_move")
        self.assertAlmostEqual(row["price"], 274.49)
        self.assertEqual(row["trade_date"], date(2026, 6, 2))


if __name__ == "__main__":
    unittest.main()
