from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.tools import shadow_test_health_check as health  # noqa: E402


def _artifact(payload: dict | None = None, *, exists: bool = True) -> dict:
    return {
        "path": "data/latest/test.json",
        "exists": exists,
        "mtime": "2026-06-15T09:00:00" if exists else None,
        "payload": payload or {},
    }


def _base_artifacts() -> dict[str, dict]:
    return {
        "readiness": _artifact({
            "status": "WARN",
            "decision": {
                "code": "US_RESEARCH_READY_TRIAL_PENDING",
                "allowed_use": "研究/买前审查",
            },
            "us": {
                "trial_ready": False,
                "shadow_runs": 24,
                "gaps_to_trial": ["覆盖 60%/80%", "US shadow alpha -0.37% <= 0"],
                "shadow_1d": {
                    "reviewed": 118,
                    "coverage_pct": 59.6,
                    "alpha_pct": -0.3704,
                    "hit_rate_pct": 50.85,
                },
                "production_formula_1d": {
                    "sample_size": 80,
                    "alpha_pct": 0.2741,
                    "hit_rate_pct": 63.75,
                },
            },
        }),
        "shadow_preflight": _artifact({
            "status": "WARN",
            "criteria": {"min_coverage_pct": 80.0},
            "warnings": ["最新生产推荐还没有对应 shadow 预检，不能用旧 shadow 结论放行。"],
            "trial_gate": {
                "ready": False,
                "unique_source_run_count": 24,
                "raw_shadow_artifact_count": 383,
                "reviewed_shadow_buy_count": 118,
                "shadow_review_coverage_pct": 59.6,
                "shadow_avg_alpha_pct": -0.3704,
                "shadow_win_rate": 50.85,
                "gaps": ["覆盖 60%/80%", "US shadow alpha -0.37% <= 0"],
            },
        }),
        "shadow_evidence": _artifact({
            "status": "BLOCKED",
            "activation_decision": {
                "status": "BLOCKED",
                "blockers": ["美股 1d coverage 64.0% < 80.0%", "美股 1d shadow alpha -0.15% < 0"],
            },
            "market_horizon_summary": [{
                "market": "US",
                "horizon": "1d",
                "reviewed_shadow_buy_count": 118,
                "shadow_review_coverage_pct": 64.0,
                "shadow_avg_alpha_pct": -0.15,
                "shadow_win_rate": 50.85,
            }],
        }),
        "strict_trial": _artifact({
            "status": "WARN",
            "decision": {
                "strict_evidence": {"n": 20, "avg_alpha_pct": 0.2572, "win_rate_pct": 60.0},
                "trial_review_gate": {
                    "status": "NOT_READY",
                    "checks": [
                        {"label": "上线后前瞻样本", "passed": True},
                        {"label": "最近确认轮数", "passed": False},
                    ],
                },
            },
        }),
        "strict_caliber": _artifact({
            "status": "OK",
            "best_candidate": "⑤Top3+排过热",
            "forward_progress": {
                "forward_start": "2026-06-08",
                "n": 48,
                "upgrade_ready": False,
                "stats": {"median_alpha_pct": 0.4409},
            },
        }),
        "strategy_validation": _artifact({
            "status": "WARN",
            "summary": {
                "policy_validation_items": 9,
                "markets": {"US": {"sample_size": 80, "wins": 51}},
            },
        }),
        "failure_diagnosis": _artifact({
            "summary": {"sample_count": 1013, "negative_alpha_count": 509},
        }),
        "tuning_proposal": _artifact({
            "status": "SHADOW_ONLY",
            "market_actions": [],
        }),
        "formula_proxy": _artifact({
            "window": "2y",
            "benchmark": "QQQ",
            "strategies": {
                "prod_proxy": {"avg_alpha_per_period_pct": 0.367},
                "no_val_grade": {"avg_alpha_per_period_pct": 1.016},
            },
        }),
        "momentum_ic": _artifact({"verdict": "FAIL"}),
        "value_ic": _artifact({"verdict": "FAIL"}),
        "revision_ic": _artifact({"verdict": "FAIL"}),
        "grade_ic": _artifact({"verdict": "PASS"}),
        "pead_ic": _artifact({"verdict": "FAIL"}),
        "pt_ic": _artifact({"verdict": "FAIL"}),
    }


class TestShadowTestHealthCheck(unittest.TestCase):
    def test_flags_action_required_when_shadow_alpha_is_negative(self):
        payload = health.build_health(_base_artifacts(), now=datetime(2026, 6, 15, 9, 30, 0))

        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["decision"]["code"], "SHADOW_HEALTH_ACTION_REQUIRED")
        self.assertGreater(payload["summary"]["action_required_count"], 0)
        preflight = next(row for row in payload["checks"] if row["id"] == "us_shadow_preflight")
        self.assertTrue(preflight["action_required"])
        self.assertFalse(preflight["can_accelerate_with_history"])
        self.assertIn("alpha", preflight["next_action"])

    def test_history_proxy_is_fast_but_not_production_gate(self):
        payload = health.build_health(_base_artifacts(), now=datetime(2026, 6, 15, 9, 30, 0))

        proxy = next(row for row in payload["checks"] if row["id"] == "formula_proxy_backtest")
        strict = next(row for row in payload["checks"] if row["id"] == "strict_caliber_backtest")

        self.assertTrue(proxy["can_accelerate_with_history"])
        self.assertTrue(strict["can_accelerate_with_history"])
        self.assertIn("代理", proxy["kind"])
        self.assertTrue(proxy["warnings"])
        self.assertIn("历史代理", " ".join(payload["what_can_be_fast"]))

    def test_data_acquisition_routes_include_backfill_and_true_future_boundary(self):
        payload = health.build_health(_base_artifacts(), now=datetime(2026, 6, 15, 9, 30, 0))
        routes = {row["id"]: row for row in payload["data_acquisition_routes"]}

        self.assertIn("outcome_backfill", routes)
        self.assertIn("price_history_backfill", routes)
        self.assertIn("future_forward_outcomes", routes)
        self.assertEqual(routes["future_forward_outcomes"]["can_fetch_now"], "NO")
        self.assertTrue(any("evaluate_v2_picks.py" in cmd for cmd in routes["outcome_backfill"]["commands"]))

    def test_missing_required_artifact_makes_health_fail(self):
        artifacts = _base_artifacts()
        artifacts["readiness"] = _artifact({}, exists=False)

        payload = health.build_health(artifacts, now=datetime(2026, 6, 15, 9, 30, 0))

        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["decision"]["code"], "SHADOW_HEALTH_MISSING_ARTIFACTS")
        row = next(item for item in payload["checks"] if item["id"] == "recommendation_readiness")
        self.assertEqual(row["status"], "MISSING")
        self.assertTrue(row["action_required"])


if __name__ == "__main__":
    unittest.main()
