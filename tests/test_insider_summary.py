from __future__ import annotations

from datetime import date

from stock_research.core.insider_summary import (
    DISCLAIMER,
    format_insider_line,
    summarize_insider_events,
)


AS_OF = date(2026, 7, 6)

import unittest


class TestInsiderSummary(unittest.TestCase):
    def test_insider_summary_net_buy(self):
        s = summarize_insider_events(
            [
                {"recent_insiders": [
                    {"date": "2026-07-01", "buy_usd": 1000, "sell_usd": 0},
                    {"date": "2026-06-20", "buy_usd": 2000, "sell_usd": 0},
                ]}
            ],
            as_of=AS_OF,
        )
        assert s["net_direction"] == "净买入"
        assert s["n_buy"] == 2
        assert s["n_sell"] == 0
        assert s["disclaimer"] == DISCLAIMER
        assert "2 笔买入 / 0 笔卖出" in format_insider_line(s)


    def test_insider_summary_net_sell(self):
        s = summarize_insider_events(
            [
                {"recent_insiders": [
                    {"date": "2026-07-01", "buy_usd": 0, "sell_usd": 1000},
                    {"date": "2026-06-20", "buy_usd": 0, "sell_usd": 2000},
                ]}
            ],
            as_of=AS_OF,
        )
        assert s["net_direction"] == "净卖出"
        assert s["n_buy"] == 0
        assert s["n_sell"] == 2


    def test_insider_summary_flat(self):
        s = summarize_insider_events(
            [
                {"recent_insiders": [
                    {"date": "2026-07-01", "buy_usd": 1000, "sell_usd": 0},
                    {"date": "2026-06-20", "buy_usd": 0, "sell_usd": 2000},
                ]}
            ],
            as_of=AS_OF,
        )
        assert s["net_direction"] == "平静"
        assert s["n_buy"] == 1
        assert s["n_sell"] == 1


    def test_insider_summary_no_recent_filing(self):
        s = summarize_insider_events(
            [
                {"recent_insiders": [
                    {"date": "2026-04-01", "buy_usd": 1000, "sell_usd": 0},
                ]}
            ],
            as_of=AS_OF,
        )
        assert s["net_direction"] == "无申报"
        assert format_insider_line(s) == "👔 内部人：30 天无申报"


if __name__ == "__main__":
    unittest.main()
