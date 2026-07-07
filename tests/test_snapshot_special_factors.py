"""特色因子快照 job 的纯逻辑单测（不打网络）。

覆盖：
  1. _cn_bare_to_symbol —— 裸码→后缀符号映射（IC join 对不上会全盘失效）
  2. ensure_table + write_rows —— 建表 + 幂等写入 roundtrip
  3. collect_hk_rows 在 south_flow cache 缺失时优雅跳过
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import duckdb

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_research.jobs import snapshot_special_factors as ssf


class CnSymbolMappingTest(unittest.TestCase):
    def test_shanghai_and_star(self):
        self.assertEqual(ssf._cn_bare_to_symbol("600519"), "600519.SS")
        self.assertEqual(ssf._cn_bare_to_symbol("688347"), "688347.SS")
        self.assertEqual(ssf._cn_bare_to_symbol("601127"), "601127.SS")

    def test_shenzhen_and_chinext(self):
        self.assertEqual(ssf._cn_bare_to_symbol("000063"), "000063.SZ")
        self.assertEqual(ssf._cn_bare_to_symbol("300750"), "300750.SZ")
        self.assertEqual(ssf._cn_bare_to_symbol("002230"), "002230.SZ")

    def test_beijing(self):
        self.assertEqual(ssf._cn_bare_to_symbol("831856"), "831856.BJ")
        self.assertEqual(ssf._cn_bare_to_symbol("430047"), "430047.BJ")

    def test_strips_suffix_and_rejects_bad(self):
        self.assertEqual(ssf._cn_bare_to_symbol("600519.SH"), "600519.SS")
        self.assertIsNone(ssf._cn_bare_to_symbol("ABC"))
        self.assertIsNone(ssf._cn_bare_to_symbol("12345"))   # 5 位非法
        self.assertIsNone(ssf._cn_bare_to_symbol(""))


class TableRoundtripTest(unittest.TestCase):
    def test_ensure_table_and_write_idempotent(self):
        conn = duckdb.connect(":memory:")
        ssf.ensure_table(conn)
        rows = [
            (date(2026, 7, 7), "CN", "600519.SS", "lhb", 0.8, None, "lhb_signals"),
            (date(2026, 7, 7), "CN", "600519.SS", "pead", 0.5, None, "event_calendar"),
        ]
        self.assertEqual(ssf.write_rows(conn, rows), 2)
        # 幂等：同键重写覆盖不新增
        rows2 = [(date(2026, 7, 7), "CN", "600519.SS", "lhb", 0.9, None, "lhb_signals")]
        ssf.write_rows(conn, rows2)
        n = conn.execute("SELECT COUNT(*) FROM factor_signal_snapshot").fetchone()[0]
        self.assertEqual(n, 2)  # 仍是 2 行，lhb 被覆盖不是新增
        val = conn.execute(
            "SELECT factor_value FROM factor_signal_snapshot "
            "WHERE symbol='600519.SS' AND factor_name='lhb'"
        ).fetchone()[0]
        self.assertAlmostEqual(val, 0.9)  # 覆盖成新值
        conn.close()

    def test_write_rows_empty_noop(self):
        conn = duckdb.connect(":memory:")
        ssf.ensure_table(conn)
        self.assertEqual(ssf.write_rows(conn, []), 0)
        conn.close()


class HkGracefulDegradeTest(unittest.TestCase):
    def test_hk_skips_when_south_flow_cache_empty(self):
        with mock.patch(
            "stock_research.core.south_flow_signals.fetch_components_snapshot",
            return_value={},
        ):
            rows, note = ssf.collect_hk_rows(date(2026, 7, 7))
        self.assertEqual(rows, [])
        self.assertIn("跳过 HK", note)

    def test_hk_computes_cross_sectional_rank(self):
        # 4 只港股通标的 + 1 只非港股通（宇宙里有但 components 没有）
        fake_components = {"700": 5.0, "9988": 3.0, "3690": 1.0, "1810": 8.0}
        fake_universe = [
            {"ticker": "0700.HK"}, {"ticker": "9988.HK"},
            {"ticker": "3690.HK"}, {"ticker": "1810.HK"},
            {"ticker": "9999.HK"},  # 非港股通 → 中性 0.5
        ]
        with mock.patch(
            "stock_research.core.south_flow_signals.fetch_components_snapshot",
            return_value=fake_components,
        ), mock.patch(
            "stock_research.core.hk_universe.fetch_hk_tech_universe",
            return_value=fake_universe,
        ):
            rows, note = ssf.collect_hk_rows(date(2026, 7, 7))
        self.assertEqual(len(rows), 5)
        by_sym = {r[2]: r for r in rows}
        # 1810 持股最高(8.0) → rank 最高；3690 最低(1.0) → rank 最低
        self.assertGreater(by_sym["1810.HK"][4], by_sym["3690.HK"][4])
        # 非港股通标的中性 0.5，raw 为 None
        self.assertEqual(by_sym["9999.HK"][4], 0.5)
        self.assertIsNone(by_sym["9999.HK"][5])
        # 港股通标的 raw_value 记原始持股 %
        self.assertEqual(by_sym["0700.HK"][5], 5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
