from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DISCOVER_PATH = REPO / "scripts" / "tools" / "discover_candidates.py"
INIT_PATH = REPO / "scripts" / "tools" / "init_stock_db_v2.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


discover = _load_module(DISCOVER_PATH, "discover_candidates_test")
init_v2 = _load_module(INIT_PATH, "init_stock_db_v2_test")


class DiscoverCandidatesTest(unittest.TestCase):
    def _conn(self):
        con = duckdb.connect(":memory:")
        con.execute(init_v2.SCHEMA_SQL)
        con.execute(
            """
            INSERT INTO ai_theme_etf_universe
              (etf_ticker, etf_name, issuer, theme_label, theme_id, holdings_url, active)
            VALUES
              ('SKYY', 'Cloud ETF', 'First Trust', 'Cloud', NULL, 'https://example.com/skyy', TRUE),
              ('NUKZ', 'Nuclear ETF', 'Range', 'Nuclear', 'smr', 'https://example.com/nukz', TRUE),
              ('REMX', 'Rare Earth ETF', 'VanEck', 'Rare Earth', 'rare_earths', 'https://example.com/remx', TRUE)
            """
        )
        con.execute(
            """
            INSERT INTO ai_theme_etf_holdings
              (etf_ticker, rank, raw_ticker, company_name, weight, market_inferred, universe_match)
            VALUES
              ('SKYY', 1, 'CSCO', 'Cisco Systems, Inc.', 6.0, 'US', NULL),
              ('NUKZ', 1, 'TLN', 'Talen Energy Corporation', 3.2, 'US', NULL),
              ('NUKZ', 2, 'CEZ', 'CEZ, a. s.', 3.0, 'US', NULL),
              ('REMX', 1, 'ALB', 'Albemarle Corporation', 8.1, 'US', NULL),
              ('REMX', 2, 'AMG', 'AMG Critical Materials N.V.', 2.9, 'US', NULL)
            """
        )
        return con

    def test_etf_holdings_expand_core_universe_conservatively(self):
        con = self._conn()
        rows = discover.build_universe(conn=con, include_core=True, include_snapshot=False)
        symbols = {r["ticker"] for r in rows}

        self.assertIn("NVDA", symbols)  # core universe still present
        self.assertIn("CSCO", symbols)  # domestic cloud ETF candidate
        self.assertIn("TLN", symbols)   # explicit safe nuclear candidate
        self.assertIn("ALB", symbols)   # explicit safe rare-earth/lithium candidate
        self.assertNotIn("CEZ", symbols)  # foreign local ticker, not US-listed
        self.assertNotIn("AMG", symbols)  # avoid wrong US ticker mapping

        csco = next(r for r in rows if r["ticker"] == "CSCO")
        self.assertEqual(csco["source"], "etf_theme:SKYY")
        self.assertEqual(csco["location"], "United States")

    def test_skip_codes_exclude_core_and_discovered_names(self):
        con = self._conn()
        rows = discover.build_universe(
            skip_codes={"NVDA", "CSCO"},
            conn=con,
            include_core=True,
            include_snapshot=False,
        )
        symbols = {r["ticker"] for r in rows}
        self.assertNotIn("NVDA", symbols)
        self.assertNotIn("CSCO", symbols)
        self.assertIn("TLN", symbols)

    def test_seed_universe_uses_caller_connection_for_dynamic_candidates(self):
        from stock_research.core import a_share_universe, hk_universe

        old_a = a_share_universe.fetch_a_share_tech_universe
        old_hk = hk_universe.fetch_hk_tech_universe
        con = self._conn()
        a_share_universe.fetch_a_share_tech_universe = lambda: []
        hk_universe.fetch_hk_tech_universe = lambda: []
        try:
            n = init_v2._seed_universe(con)
            self.assertGreaterEqual(n, 1)
        finally:
            a_share_universe.fetch_a_share_tech_universe = old_a
            hk_universe.fetch_hk_tech_universe = old_hk

        rows = con.execute(
            """
            SELECT market, symbol, source
            FROM system_universe
            WHERE active = TRUE AND market = 'US'
            """
        ).fetchall()
        by_symbol = {symbol: source for market, symbol, source in rows}
        self.assertIn("CSCO", by_symbol)
        self.assertIn("TLN", by_symbol)
        self.assertNotIn("CEZ", by_symbol)
        self.assertNotIn("AMG", by_symbol)


if __name__ == "__main__":
    unittest.main(verbosity=2)
