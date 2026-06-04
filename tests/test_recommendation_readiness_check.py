from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.tools import recommendation_readiness_check as readiness  # noqa: E402


def _quality(status: str = "PASS") -> dict:
    return {
        "status": status,
        "summary": {
            "latest_recommendation_run": {
                "run_id": "rec_test",
                "strategy_version": "tech_ai_v2_price_action_gate",
            }
        },
    }


def _acceptance(status: str = "PASS", *, with_failures: bool = False) -> dict:
    summary = {
        "latest_recommendation_run_id": "rec_test",
        "artifacts": {
            "data/event_calendar_us_form4.json": {"age_days": 0},
        },
    }
    issues = []
    if with_failures:
        issues.append({
            "level": "FAIL",
            "code": "pipeline_failed_steps_present",
            "message": "pipeline failed",
            "details": [{"label": "19/25 事件日历", "script": "-m job"}],
        })
    if status == "WARN":
        issues.append({
            "level": "WARN",
            "code": "recommendation_evidence_older_than_current_run",
            "message": "recommendation_evidence.json 早于当前 plan/qgate/pipeline",
        })
    return {"status": status, "summary": summary, "issues": issues}


def _pipeline(status: str = "PASS", *, with_failures: bool = False) -> dict:
    payload = {"status": status}
    if with_failures:
        payload["failed_steps"] = [{"label": "19/25 事件日历", "script": "-m job"}]
    return payload


def _plan(markets: list[str] | None = None) -> dict:
    markets = markets or ["US", "US"]
    return {
        "constraints": {"market_scope": "US"},
        "candidate_universe": {"market_scope": "US"},
        "plan_v5": [
            {"symbol": f"SYM{i}", "market": market, "target_weight": 0.1}
            for i, market in enumerate(markets)
        ],
    }


def _shadow(*, runs: int = 6, us_reviewed: int = 20, us_alpha: float = 1.3, us_hit: float = 60.0) -> dict:
    rows = []
    for market in ("CN", "HK", "US"):
        rows.append({
            "market": market,
            "label": {"CN": "A股", "HK": "港股", "US": "美股"}[market],
            "horizon": "1d",
            "reviewed_shadow_buy_count": us_reviewed if market == "US" else 20,
            "shadow_review_coverage_pct": 100.0 if us_reviewed >= 60 else 20.0,
            "shadow_win_rate": us_hit if market == "US" else 25.0,
            "shadow_avg_alpha_pct": us_alpha if market == "US" else -1.0,
            "production_portfolio_eligible_count": 120 if market == "US" else 0,
        })
    return {
        "status": "BLOCKED" if runs < 10 or us_reviewed < 60 else "READY",
        "shadow_run_count": runs,
        "activation_decision": {
            "criteria": {
                "min_shadow_runs": 10,
                "min_market_reviewed": 60,
                "min_coverage_pct": 80.0,
                "min_hit_rate": 45.0,
                "primary_horizon": "1d",
            }
        },
        "market_horizon_summary": rows,
    }


def _preflight(*, unique_runs: int = 5, raw_runs: int = 6, reviewed: int = 20, coverage: float = 20.0, alpha: float = 1.3, hit: float = 60.0) -> dict:
    return {
        "status": "WARN",
        "criteria": {
            "min_shadow_runs": 10,
            "min_market_reviewed": 60,
            "min_coverage_pct": 80.0,
            "min_hit_rate": 45.0,
            "primary_horizon": "1d",
        },
        "trial_gate": {
            "ready": False,
            "unique_source_run_count": unique_runs,
            "raw_shadow_artifact_count": raw_runs,
            "reviewed_shadow_buy_count": reviewed,
            "shadow_review_coverage_pct": coverage,
            "shadow_avg_alpha_pct": alpha,
            "shadow_win_rate": hit,
            "gaps": [f"唯一 source run {unique_runs}/10"],
        },
        "warnings": ["shadow artifact 存在重复 source run，不能按 raw artifact 数放行。"],
    }


def _validation(us_alpha: float = 1.14, us_hit: float = 54.2, us_n: int = 120) -> dict:
    reports = []
    for market, alpha, hit, n in (
        ("CN", -1.6, 15.0, 120),
        ("HK", -0.6, 31.3, 115),
        ("US", us_alpha, us_hit, us_n),
    ):
        reports.append({
            "strategy_version": "tech_ai_v2_price_action_gate",
            "market": market,
            "by_horizon": {
                "1d": {
                    "sample_size": n,
                    "wins": int(n * hit / 100),
                    "win_rate": hit,
                    "avg_alpha": alpha,
                    "avg_return": alpha / 2,
                    "period_start": "2026-06-01",
                    "period_end": "2026-06-03",
                }
            },
            "conclusion": "策略中性：继续使用并观察下一批成熟样本。" if market == "US" else "策略承压：建议复核因子权重和市场分组。",
            "recommended_action": "continue_observe" if market == "US" else "review_weights",
        })
    return {"status": "WARN", "reports": reports}


def _us_acceptance(status: str = "PASS") -> dict:
    return {
        "status": status,
        "decision": {
            "code": "US_RESEARCH_READY_TRIAL_PENDING" if status == "PASS" else "US_BLOCKED",
            "label": "US 核心可上线（研究/买前审查）" if status == "PASS" else "US 暂停上线",
            "allowed_use": "可上线为候选发现、研究队列和买前审查；可小仓试探仍等样本门槛",
        },
    }


class TestRecommendationReadiness(unittest.TestCase):
    def _build(self, **overrides):
        args = {
            "quality": _quality(),
            "acceptance": _acceptance(),
            "shadow": _shadow(),
            "plan": _plan(),
            "validation": _validation(),
            "pipeline": _pipeline(),
            "now": datetime(2026, 6, 4, 12, 0, 0),
        }
        args.update(overrides)
        return readiness.build_readiness(**args)

    def test_us_positive_but_shadow_not_mature_is_verifying(self):
        payload = self._build()

        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["decision"]["code"], "US_VERIFYING")
        self.assertFalse(payload["us"]["trial_ready"])
        self.assertIn("US 1D reviewed 20/60", payload["us"]["gaps_to_trial"])
        self.assertEqual(payload["market_policy"]["CN"]["state"], "RESEARCH_ONLY_FROZEN")
        self.assertEqual(payload["market_policy"]["HK"]["state"], "RESEARCH_ONLY_FROZEN")

    def test_us_can_enter_trial_when_shadow_and_system_pass(self):
        payload = self._build(shadow=_shadow(runs=10, us_reviewed=60, us_alpha=1.1, us_hit=52.0))

        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["decision"]["code"], "US_TRIAL_READY")
        self.assertTrue(payload["us"]["trial_ready"])
        self.assertEqual(payload["market_policy"]["US"]["allowed_use"], "可小仓试探")

    def test_preflight_unique_source_count_blocks_trial_even_if_shadow_raw_passes(self):
        payload = self._build(
            shadow=_shadow(runs=10, us_reviewed=60, us_alpha=1.1, us_hit=52.0),
            us_preflight=_preflight(unique_runs=5, raw_runs=10, reviewed=20, coverage=20.0),
        )

        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["decision"]["code"], "US_VERIFYING")
        self.assertFalse(payload["us"]["trial_ready"])
        self.assertEqual(payload["us"]["shadow_runs"], 5)
        self.assertEqual(payload["us"]["raw_shadow_artifact_count"], 10)
        self.assertIn("唯一 source run 5/10", payload["us"]["gaps_to_trial"])

    def test_non_us_or_missing_plan_row_blocks(self):
        payload = self._build(plan=_plan(["US", "HK"]))

        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["decision"]["code"], "US_BLOCKED")
        self.assertEqual(payload["plan"]["non_us_or_missing"], 1)
        self.assertTrue(any("US-only" in item for item in payload["blockers"]))

    def test_pipeline_fail_keeps_us_research_only_even_with_positive_us(self):
        payload = self._build(
            acceptance=_acceptance("FAIL", with_failures=True),
            pipeline=_pipeline("FAIL", with_failures=True),
        )

        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["decision"]["code"], "US_VERIFYING")
        self.assertTrue(any("生产流水线" in item for item in payload["watch_items"]))

    def test_us_acceptance_pass_allows_us_release_despite_global_fail(self):
        payload = self._build(
            acceptance=_acceptance("FAIL", with_failures=True),
            pipeline=_pipeline("FAIL", with_failures=True),
            us_acceptance=_us_acceptance("PASS"),
        )

        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["decision"]["code"], "US_RESEARCH_READY_TRIAL_PENDING")
        self.assertEqual(payload["inputs"]["us_acceptance_status"], "PASS")
        self.assertTrue(any("US-only 验收不阻断" in item for item in payload["watch_items"]))

    def test_acceptance_warning_is_not_swallowed(self):
        payload = self._build(acceptance=_acceptance("WARN"))

        self.assertTrue(any("recommendation_evidence.json" in item for item in payload["watch_items"]))
        self.assertTrue(any(c["code"] == "acceptance_recommendation_evidence_older_than_current_run" for c in payload["checks"]))


class TestRecommendationReadinessDashboard(unittest.TestCase):
    def test_readiness_panel_surfaces_us_state(self):
        from scripts.pipeline import build_stock_dashboard_html as dashboard  # noqa: E402

        payload = readiness.build_readiness(
            quality=_quality(),
            acceptance=_acceptance(),
            shadow=_shadow(),
            plan=_plan(),
            validation=_validation(),
            pipeline=_pipeline(),
            now=datetime(2026, 6, 4, 12, 0, 0),
        )
        original = dashboard._runtime_load_json
        try:
            dashboard._runtime_load_json = lambda rel: payload if rel == "data/latest/recommendation_readiness_check.json" else {}
            html = dashboard.recommendation_readiness_panel_html(compact=False)
        finally:
            dashboard._runtime_load_json = original

        self.assertIn("US 推荐规则快速体检", html)
        self.assertIn("US 验证中", html)
        self.assertIn("可用于发现候选", html)
        self.assertIn("US-only 组合", html)
        self.assertIn("source 6/10", html)


if __name__ == "__main__":
    unittest.main()
