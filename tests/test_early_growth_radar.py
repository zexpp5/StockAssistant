from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from stock_research.jobs.early_growth_radar import (  # type: ignore
    build_payload,
    classify_candidate,
    price_early_score,
    recommendation_gap_score,
    theme_score,
)


class TestEarlyGrowthRadarScoring(unittest.TestCase):
    def test_price_overheat_is_not_early(self):
        score, reasons, flags = price_early_score({
            "one_month_pct": 80.0,
            "one_week_pct": 4.0,
            "one_year_pct": 280.0,
        })
        self.assertLess(score, 15)
        self.assertIn("OVERHEATED_1M", flags)
        self.assertTrue(any("不是早期" in r for r in reasons))

    def test_price_not_overheated_scores_high(self):
        score, reasons, flags = price_early_score({
            "one_month_pct": 19.0,
            "one_week_pct": -3.0,
            "one_year_pct": 180.0,
        })
        self.assertGreaterEqual(score, 30)
        self.assertNotIn("OVERHEATED_1M", flags)
        self.assertTrue(any("尚未明显过热" in r for r in reasons))

    def test_theme_scores_emerging_ai_cloud(self):
        score, reason = theme_score({
            "symbol": "CRWV",
            "name": "CoreWeave",
            "theme": "AI cloud",
            "industry": "",
            "source": "us_emerging_ai_cloud",
        })
        self.assertGreaterEqual(score, 20)
        self.assertIn("AI 云", reason)

    def test_front_pick_is_not_early_blind_spot(self):
        score, reason, front = recommendation_gap_score({"market_position": 3})
        self.assertEqual(score, 0)
        self.assertTrue(front)
        self.assertIn("右侧前排", reason)

    def test_classification_guardrails(self):
        label, action = classify_candidate(76, [], False, True)
        self.assertEqual(label, "早发现候选")
        self.assertIn("小仓", action)

        label, action = classify_candidate(76, ["OVERHEATED_1M"], False, True)
        self.assertEqual(label, "已涨出右侧")
        self.assertIn("不追", action)

        label, action = classify_candidate(76, [], False, True, mature_core=True)
        self.assertEqual(label, "成熟/非早期")
        self.assertIn("不当早发现", action)

        label, action = classify_candidate(0, [], False, False)
        self.assertEqual(label, "覆盖缺口")
        self.assertIn("补行情", action)


class TestEarlyGrowthRadarPayload(unittest.TestCase):
    def _make_db(self) -> Path:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        path = Path(tmpdir.name) / "test.duckdb"
        conn = duckdb.connect(str(path))
        conn.execute(
            """
            CREATE TABLE system_universe (
                market VARCHAR,
                symbol VARCHAR,
                name VARCHAR,
                theme VARCHAR,
                industry VARCHAR,
                source VARCHAR,
                active BOOLEAN
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE price_daily (
                market VARCHAR,
                symbol VARCHAR,
                trade_date DATE,
                close DOUBLE,
                currency VARCHAR,
                market_cap DOUBLE,
                forward_pe DOUBLE,
                peg_ratio DOUBLE,
                one_week_pct DOUBLE,
                one_month_pct DOUBLE,
                one_year_pct DOUBLE,
                ytd_pct DOUBLE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE recommendation_runs (
                run_id VARCHAR,
                generated_at TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE recommendation_picks (
                run_id VARCHAR,
                market VARCHAR,
                symbol VARCHAR,
                name VARCHAR,
                rank INTEGER,
                rating VARCHAR,
                signal VARCHAR,
                total_score DOUBLE,
                entry_price DOUBLE
            )
            """
        )
        conn.execute(
            """
            INSERT INTO system_universe VALUES
              ('US', 'ALAB', 'Astera Labs', 'AI connectivity', 'AI connectivity', 'us_ai_connectivity', true),
              ('US', 'CRDO', 'Credo Technology', 'AI connectivity', 'AI connectivity', 'us_ai_connectivity', true),
              ('US', 'TEM', 'Tempus AI', 'AI healthcare', 'AI healthcare', 'us_ai_healthcare', true)
            """
        )
        conn.execute(
            """
            INSERT INTO price_daily VALUES
              ('US', 'ALAB', '2026-06-03', 363.54, 'USD', 62000000000, 86.4, 0.6, 4.1, 80.6, 281.8, 190.0),
              ('US', 'CRDO', '2026-06-03', 214.60, 'USD', 39000000000, 24.9, 0.07, -3.5, 19.2, 180.7, 100.0),
              ('US', 'TEM',  '2026-06-03',  47.51, 'USD',  8500000000, -445.4, NULL, -7.4, -14.9, -24.3, -20.0)
            """
        )
        conn.execute("INSERT INTO recommendation_runs VALUES ('rec_test', '2026-06-04 07:37:17')")
        conn.close()
        return path

    def test_payload_marks_overheat_and_coverage_gap(self):
        db_path = self._make_db()
        try:
            payload = build_payload(db_path, limit=20)
        finally:
            db_path.unlink(missing_ok=True)

        rows = {r["symbol"]: r for r in payload["candidates"]}
        self.assertEqual(rows["ALAB"]["label"], "已涨出右侧")
        self.assertIn(rows["CRDO"]["label"], {"早发现候选", "潜伏观察"})
        self.assertIn(rows["TEM"]["label"], {"早发现候选", "潜伏观察", "仅跟踪"})

        gaps = {r["symbol"] for r in payload["coverage_gaps"]}
        self.assertIn("RKLB", gaps)
        self.assertTrue(payload["guardrails"]["does_not_write_watchlist"])
        self.assertTrue(payload["guardrails"]["does_not_write_real_holdings"])

    def test_dashboard_section_is_static_and_readable(self):
        from scripts.pipeline.build_stock_dashboard_html import early_growth_radar_section_html  # type: ignore

        html = early_growth_radar_section_html({
            "generated_at": "2026-06-04T11:10:30",
            "latest_recommendation_run_id": "rec_test",
            "counts": {"early_or_watch": 1, "overheated": 1},
            "early_or_watch": [{
                "symbol": "CRDO",
                "name": "Credo Technology",
                "label": "早发现候选",
                "score": 77,
                "one_month_pct": 19.2,
                "one_week_pct": -3.5,
                "ytd_pct": 49.8,
                "one_year_pct": 180.7,
                "close": 214.6,
                "currency": "USD",
                "market_cap": 39582957568,
                "forward_pe": 24.9,
                "peg_ratio": 0.07,
                "theme": "AI connectivity",
                "source": "us_ai_connectivity",
                "trade_date": "2026-06-03",
                "reasons": ["1个月 +19.2%，尚未明显过热", "AI 互联/定制芯片链"],
                "score_breakdown": {
                    "price_early": 33,
                    "theme": 22,
                    "recommendation_gap": 12,
                    "catalyst": 0,
                    "ownership_13f": 0,
                    "valuation": 10,
                },
                "latest_pick": None,
                "catalyst_counts": {"bullish": 0, "bearish": 0, "neutral": 0},
                "flags": [],
            }],
            "overheated": [{
                "symbol": "ALAB",
                "name": "Astera Labs",
                "label": "已涨出右侧",
                "score": 42,
                "one_month_pct": 80.6,
                "one_week_pct": 4.1,
                "ytd_pct": 190.0,
                "one_year_pct": 281.8,
                "close": 363.5,
                "currency": "USD",
                "market_cap": 62000000000,
                "forward_pe": 86.4,
                "peg_ratio": 0.6,
                "theme": "AI connectivity",
                "source": "us_ai_connectivity",
                "trade_date": "2026-06-03",
                "reasons": ["1个月 +80.6%，不是早期"],
                "score_breakdown": {
                    "price_early": 0,
                    "theme": 22,
                    "recommendation_gap": 12,
                    "catalyst": 0,
                    "ownership_13f": 0,
                    "valuation": 8,
                },
                "latest_pick": {"market_position": 4, "rating": "buy", "total_score": 88.5},
                "catalyst_counts": {"bullish": 1, "bearish": 0, "neutral": 1},
                "flags": ["OVERHEATED_1M"],
            }],
        })

        self.assertIn("早发现雷达", html)
        self.assertIn("只做研究提醒", html)
        self.assertIn("CRDO", html)
        self.assertIn("可研究", html)
        self.assertIn("走势", html)
        self.assertIn("估值/规模", html)
        self.assertIn("证据", html)
        self.assertIn("理由/拆分", html)
        self.assertIn("AI connectivity", html)
        self.assertIn("FPE", html)
        self.assertIn("PEG", html)
        self.assertIn("13F 暂无覆盖", html)
        self.assertIn("价格", html)
        self.assertIn("赛道", html)
        self.assertIn("已涨太多，先别追", html)
        self.assertNotIn('id="early-growth-radar"', html)


if __name__ == "__main__":
    unittest.main()
