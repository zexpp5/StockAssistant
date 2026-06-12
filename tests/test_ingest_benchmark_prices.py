"""基准灌入不得覆盖完整行情行 — 2026-06-12 QQQ 自选动量被每日抹空回归测试."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "scripts" / "pipeline"))

import duckdb  # type: ignore

from ingest_benchmark_prices import write_benchmark_rows  # type: ignore

PRICE_DAILY_DDL = """
CREATE TABLE price_daily (
    market VARCHAR, symbol VARCHAR, trade_date DATE, interval VARCHAR,
    close DOUBLE, prev_close DOUBLE, currency VARCHAR, market_cap DOUBLE,
    forward_pe DOUBLE, trailing_pe DOUBLE, peg_ratio DOUBLE, ytd_pct DOUBLE,
    one_week_pct DOUBLE, one_month_pct DOUBLE, one_year_pct DOUBLE,
    source VARCHAR, source_updated_at TIMESTAMP, fetched_at TIMESTAMP
)
"""


class WriteBenchmarkRowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = duckdb.connect(str(Path(self.tmp.name) / "t.duckdb"))
        self.con.execute(PRICE_DAILY_DDL)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _insert(self, trade_date, close, source, one_month_pct=None):
        self.con.execute(
            "INSERT INTO price_daily (market, symbol, trade_date, interval, close, currency, "
            "one_month_pct, source, source_updated_at, fetched_at) "
            "VALUES ('US','QQQ',?, '1d', ?, 'USD', ?, ?, now(), now())",
            [trade_date, close, one_month_pct, source],
        )

    def _rows(self):
        return self.con.execute(
            "SELECT trade_date, close, one_month_pct, source FROM price_daily "
            "WHERE symbol='QQQ' ORDER BY trade_date"
        ).fetchall()

    def test_fuller_row_survives_and_gaps_filled(self):
        # 完整批写的带动量行（昨晚 21:02 的那种）
        self._insert(date(2026, 6, 10), 693.69, "yfinance", one_month_pct=-1.5)
        # 基准自己的旧行（允许被刷新）
        self._insert(date(2026, 6, 9), 700.0, "yfinance_benchmark")

        bars = [
            (date(2026, 6, 9), 707.83),   # 已有基准行 → 刷新
            (date(2026, 6, 10), 693.69),  # 已有完整行 → 必须保留不动
            (date(2026, 6, 11), 717.12),  # 缺失 → 补
        ]
        written, kept = write_benchmark_rows(
            self.con, "US", "QQQ", "USD", bars, datetime(2026, 6, 12, 8, 0)
        )

        self.assertEqual(written, 2)
        self.assertEqual(kept, 1)
        rows = {str(r[0]): r for r in self._rows()}
        self.assertEqual(len(rows), 3)
        # 旧 bug 的断言核心：完整行的动量没有被抹成 NULL
        self.assertEqual(rows["2026-06-10"][2], -1.5)
        self.assertEqual(rows["2026-06-10"][3], "yfinance")
        self.assertEqual(rows["2026-06-09"][1], 707.83)
        self.assertEqual(rows["2026-06-09"][3], "yfinance_benchmark")
        self.assertEqual(rows["2026-06-11"][3], "yfinance_benchmark")

    def test_pure_benchmark_symbol_still_refreshes(self):
        # SPY 这类纯基准（不在自选）行为不变：自己的行整窗刷新
        self._insert(date(2026, 6, 10), 600.0, "yfinance_benchmark")
        bars = [(date(2026, 6, 10), 605.5), (date(2026, 6, 11), 610.0)]
        written, kept = write_benchmark_rows(
            self.con, "US", "QQQ", "USD", bars, datetime(2026, 6, 12, 8, 0)
        )
        self.assertEqual((written, kept), (2, 0))
        rows = {str(r[0]): r for r in self._rows()}
        self.assertEqual(rows["2026-06-10"][1], 605.5)


if __name__ == "__main__":
    unittest.main()
