"""AI 主题覆盖审计 job 的产业链覆盖接线测试。

重点守边界：
  - 只统计 system_universe 的 active US 行
  - 不把非 US / inactive 当成已覆盖
  - 输出 count 是 thin + gap 的环节数，不是股票数量
"""
import sys
import unittest
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_research.jobs.ai_theme_coverage_audit import (  # noqa: E402
    _audit_ai_supply_chain_coverage,
)


class TestAISupplyChainCoverageAuditJob(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        self.con.execute("""
            CREATE TABLE system_universe (
                market VARCHAR,
                symbol VARCHAR,
                active BOOLEAN
            )
        """)

    def tearDown(self):
        self.con.close()

    def test_uses_only_active_us_system_universe(self):
        self.con.executemany(
            "INSERT INTO system_universe VALUES (?, ?, ?)",
            [
                ("US", "NVDA", True),
                ("US", "AMD", True),
                ("US", "AVGO", True),
                ("US", "MU", True),
                ("US", "AMKR", True),
                ("HK", "WDC", True),   # 非 US，不能算覆盖
                ("US", "COHR", False), # inactive，不能算覆盖
            ],
        )

        payload = _audit_ai_supply_chain_coverage(self.con)
        rows = {r["key"]: r for r in payload["segments"]}

        self.assertEqual(payload["universe_scope"], "system_universe active US")
        self.assertEqual(payload["universe_size"], 5)
        self.assertEqual(rows["ai_compute_chips"]["status"], "covered")
        self.assertEqual(rows["memory_storage"]["status"], "thin")
        self.assertEqual(rows["memory_storage"]["present"], ["MU"])
        self.assertNotIn("WDC", rows["memory_storage"]["present"])
        self.assertEqual(rows["optical_interconnect"]["status"], "gap")
        self.assertNotIn("COHR", rows["optical_interconnect"]["present"])
        self.assertEqual(rows["advanced_packaging"]["status"], "thin")
        self.assertEqual(
            payload["count"],
            payload["summary"]["thin"] + payload["summary"]["gap"],
        )


if __name__ == "__main__":
    unittest.main()
