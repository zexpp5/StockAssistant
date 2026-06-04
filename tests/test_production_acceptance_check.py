from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.tools import production_acceptance_check as acceptance  # noqa: E402


class TestProductionAcceptancePipelineResolution(unittest.TestCase):
    def test_hkex_degraded_artifact_resolves_auxiliary_pipeline_failure(self):
        original = acceptance._json_load

        def fake_json_load(rel: str):
            if rel == "data/event_calendar_hk_hkex.json":
                return {
                    "status": "degraded",
                    "generated_at": "2026-06-04T13:30:34",
                    "n_announcements": 0,
                    "source_health": {"status": "degraded", "reason": "hkex_source_unavailable_or_blocked"},
                    "coverage": {"hit": 0, "miss": 0, "errored": 34},
                }, REPO / rel
            return None, REPO / rel

        acceptance._json_load = fake_json_load
        try:
            result = acceptance._pipeline_failed_step_resolution({
                "label": "19d/25 港股 HKEX 披露易公告（盈警/停牌/股东/回购/并购）",
                "script": "-m stock_research.jobs.event_calendar_hk_hkex_daily",
                "status": "FAIL",
            })
        finally:
            acceptance._json_load = original

        self.assertIsNotNone(result)
        self.assertEqual(result["level"], "WARN")
        self.assertEqual(result["artifact"]["status"], "degraded")

    def test_previous_acceptance_self_failure_is_not_self_blocking(self):
        result = acceptance._pipeline_failed_step_resolution({
            "label": "27 生产闭环验收",
            "script": "scripts/tools/production_acceptance_check.py",
            "status": "FAIL",
        })

        self.assertIsNotNone(result)
        self.assertEqual(result["level"], "INFO")
        self.assertEqual(result["reason"], "previous_acceptance_self_reference")


class TestProductionAcceptanceArtifactCadence(unittest.TestCase):
    def test_weekly_form4_does_not_warn_inside_weekly_cadence(self):
        original = acceptance._json_load
        generated_at = (acceptance.datetime.now() - acceptance.timedelta(days=3)).isoformat()

        def fake_json_load(rel: str):
            if rel == "data/event_calendar_us_form4.json":
                return {"generated_at": generated_at}, REPO / rel
            return None, REPO / rel

        acceptance._json_load = fake_json_load
        try:
            issues: list[dict] = []
            summary = acceptance._latest_json_checks(
                issues,
                acceptance.WEEKLY_EVENT_CALENDAR_JSON,
                max_age_days=acceptance.WEEKLY_EVENT_CALENDAR_MAX_AGE_DAYS,
                level="WARN",
            )
        finally:
            acceptance._json_load = original

        self.assertEqual(summary["data/event_calendar_us_form4.json"]["age_days"], 3)
        self.assertFalse(any(i["code"] == "stale_latest_artifact" for i in issues))

    def test_weekly_form4_warns_after_weekly_cadence_expires(self):
        original = acceptance._json_load
        generated_at = (acceptance.datetime.now() - acceptance.timedelta(days=8)).isoformat()

        def fake_json_load(rel: str):
            if rel == "data/event_calendar_us_form4.json":
                return {"generated_at": generated_at}, REPO / rel
            return None, REPO / rel

        acceptance._json_load = fake_json_load
        try:
            issues: list[dict] = []
            acceptance._latest_json_checks(
                issues,
                acceptance.WEEKLY_EVENT_CALENDAR_JSON,
                max_age_days=acceptance.WEEKLY_EVENT_CALENDAR_MAX_AGE_DAYS,
                level="WARN",
            )
        finally:
            acceptance._json_load = original

        self.assertTrue(any(i["code"] == "stale_latest_artifact" for i in issues))


if __name__ == "__main__":
    unittest.main()
