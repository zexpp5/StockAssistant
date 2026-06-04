from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.tools import us_shadow_preflight_check as preflight  # noqa: E402


def _shadow_evidence() -> dict:
    return {
        "activation_decision": {
            "criteria": {
                "min_shadow_runs": 10,
                "min_market_reviewed": 60,
                "min_coverage_pct": 80.0,
                "min_hit_rate": 45.0,
                "primary_horizon": "1d",
            }
        }
    }


def _run(source: str, generated_at: str, *, version: str = "shadow_v1", picks: int = 20) -> dict:
    return {
        "run_id": f"shadow_{source}_{generated_at[-8:].replace(':', '')}",
        "generated_at": generated_at,
        "proposed_strategy_version": version,
        "source_production_run": {
            "run_id": source,
            "run_date": generated_at[:10],
        },
        "picks": [
            {
                "market": "US",
                "symbol": f"US{i:02d}",
                "shadow_signal": "buy",
                "original_signal": "buy",
                "production_portfolio_eligible": True,
            }
            for i in range(picks)
        ],
    }


def _outcomes(source_ids: list[str], picks: int = 20, *, alpha: float = 1.0) -> dict:
    rows = {}
    for source in source_ids:
        for i in range(picks):
            rows[(source, "US", f"US{i:02d}", "1d")] = {
                "run_id": source,
                "market": "US",
                "symbol": f"US{i:02d}",
                "horizon": "1d",
                "alpha_pct": alpha,
                "return_pct": alpha,
                "is_success": alpha > 0,
            }
    return rows


class TestUSShadowPreflight(unittest.TestCase):
    def test_duplicate_source_runs_are_warned_and_deduped(self):
        runs = [
            _run("rec_1", "2026-06-02T21:00:00", version="shadow_a"),
            _run("rec_1", "2026-06-03T08:00:00", version="shadow_b"),
            _run("rec_2", "2026-06-03T21:00:00", version="shadow_b"),
        ]
        payload = preflight.build_preflight(
            runs=runs,
            shadow_evidence=_shadow_evidence(),
            latest_production_run={"run_id": "rec_2"},
            outcomes=_outcomes(["rec_1"]),
            now=datetime(2026, 6, 4, 12, 0, 0),
        )

        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["summary"]["raw_shadow_artifact_count"], 3)
        self.assertEqual(payload["summary"]["unique_source_run_count"], 2)
        self.assertEqual(payload["summary"]["duplicate_source_run_count"], 1)
        self.assertFalse(payload["trial_gate"]["ready"])
        self.assertIn("唯一 source run 2/10", payload["trial_gate"]["gaps"])
        self.assertTrue(any("重复 source run" in item for item in payload["warnings"]))

    def test_trial_ready_requires_unique_sources_and_reviewed_outcomes(self):
        runs = [_run(f"rec_{i}", f"2026-06-{i + 1:02d}T21:00:00", picks=6) for i in range(10)]
        source_ids = [f"rec_{i}" for i in range(10)]
        payload = preflight.build_preflight(
            runs=runs,
            shadow_evidence=_shadow_evidence(),
            latest_production_run={"run_id": "rec_9"},
            outcomes=_outcomes(source_ids, picks=6, alpha=1.2),
            now=datetime(2026, 6, 12, 12, 0, 0),
        )

        self.assertEqual(payload["status"], "PASS")
        self.assertTrue(payload["trial_gate"]["ready"])
        self.assertEqual(payload["trial_gate"]["unique_source_run_count"], 10)
        self.assertEqual(payload["trial_gate"]["reviewed_shadow_buy_count"], 60)


if __name__ == "__main__":
    unittest.main()
