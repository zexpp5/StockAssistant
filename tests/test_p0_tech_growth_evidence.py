from __future__ import annotations

import unittest
from datetime import datetime

import duckdb

from stock_research.jobs.aggregate_theme_tags import aggregate_tags
from stock_research.jobs.seed_p0_tech_growth_evidence import seed_p0_tech_growth_evidence


class P0TechGrowthEvidenceSeedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = duckdb.connect(":memory:")
        self.con.execute(
            """
            CREATE TABLE ai_theme_evidence_sources (
                source_id VARCHAR PRIMARY KEY,
                source_name VARCHAR NOT NULL,
                source_tier VARCHAR NOT NULL,
                source_type VARCHAR NOT NULL,
                source_url VARCHAR NOT NULL,
                update_cadence VARCHAR,
                license_note VARCHAR,
                last_checked_at TIMESTAMP,
                last_check_status VARCHAR,
                last_check_http INTEGER,
                active BOOLEAN DEFAULT TRUE
            )
            """
        )
        self.con.execute(
            """
            CREATE TABLE ai_theme_company_evidence (
                evidence_id VARCHAR PRIMARY KEY,
                theme VARCHAR NOT NULL,
                market VARCHAR,
                symbol VARCHAR,
                company_name VARCHAR,
                evidence_status VARCHAR NOT NULL,
                source_id VARCHAR NOT NULL,
                source_tier VARCHAR NOT NULL,
                source_url VARCHAR NOT NULL,
                source_title VARCHAR,
                source_date DATE,
                captured_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                evidence_text VARCHAR,
                evidence_kind VARCHAR,
                metric_json VARCHAR,
                confidence_score DOUBLE,
                expires_at DATE,
                reviewer_note VARCHAR
            )
            """
        )
        self.con.execute(
            """
            CREATE TABLE ai_theme_company_tags (
                theme VARCHAR NOT NULL,
                market VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                company_name VARCHAR,
                theme_role VARCHAR,
                ai_strength VARCHAR,
                evidence_status VARCHAR,
                evidence_score DOUBLE,
                source_count_a INTEGER,
                source_count_b INTEGER,
                source_count_c INTEGER,
                latest_source_date DATE,
                rationale VARCHAR,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (theme, market, symbol)
            )
            """
        )

    def tearDown(self) -> None:
        self.con.close()

    def test_seed_creates_confirmed_company_tags_after_aggregation(self) -> None:
        summary = seed_p0_tech_growth_evidence(
            self.con,
            now=datetime(2026, 6, 9, 12, 0, 0),
        )
        stat = aggregate_tags(self.con)

        self.assertGreaterEqual(summary["n_symbols"], 30)
        self.assertEqual(summary["n_evidence_rows"], summary["n_symbols"] * 2)
        self.assertGreaterEqual(stat["by_status"].get("confirmed", 0), 30)

        nvda = self.con.execute(
            """
            SELECT evidence_status, source_count_a, latest_source_date, rationale
            FROM ai_theme_company_tags
            WHERE theme='ai_core' AND market='US' AND symbol='NVDA'
            """
        ).fetchone()
        self.assertIsNotNone(nvda)
        self.assertEqual(nvda[0], "confirmed")
        self.assertEqual(nvda[1], 2)
        self.assertIn("满足 §九 confirmed", nvda[3])

    def test_seed_is_idempotent(self) -> None:
        seed_p0_tech_growth_evidence(self.con, now=datetime(2026, 6, 9, 12, 0, 0))
        first = self.con.execute("SELECT COUNT(*) FROM ai_theme_company_evidence").fetchone()[0]

        seed_p0_tech_growth_evidence(self.con, now=datetime(2026, 6, 9, 13, 0, 0))
        second = self.con.execute("SELECT COUNT(*) FROM ai_theme_company_evidence").fetchone()[0]

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
