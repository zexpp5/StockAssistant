from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.tools import us_production_acceptance_check as us_acceptance  # noqa: E402


def _quality(status: str = "PASS") -> dict:
    return {"status": status}


def _pipeline(*, failed_labels: list[str] | None = None) -> dict:
    return {
        "status": "FAIL" if failed_labels else "PASS",
        "failed_steps": [
            {"label": label, "script": "test", "status": "FAIL"}
            for label in (failed_labels or [])
        ],
    }


def _plan(markets: list[str] | None = None) -> dict:
    markets = markets or ["US", "US"]
    return {
        "constraints": {"market_scope": "US"},
        "candidate_universe": {"market_scope": "US"},
        "plan_v5": [{"symbol": f"S{i}", "market": market} for i, market in enumerate(markets)],
    }


def _validation(us_alpha: float = 1.14, us_hit: float = 54.17, us_n: int = 120) -> dict:
    return {
        "reports": [{
            "market": "US",
            "strategy_version": "tech_ai_v2_price_action_gate",
            "conclusion": "策略中性：继续使用并观察下一批成熟样本。",
            "recommended_action": "continue_observe",
            "by_horizon": {
                "1d": {
                    "sample_size": us_n,
                    "avg_alpha": us_alpha,
                    "win_rate": us_hit,
                    "period_start": "2026-06-01",
                    "period_end": "2026-06-03",
                }
            },
        }]
    }


def _shadow(*, runs: int = 6, reviewed: int = 20, coverage: float = 16.67, alpha: float = 1.37, hit: float = 60.0) -> dict:
    return {
        "shadow_run_count": runs,
        "activation_decision": {
            "criteria": {
                "min_shadow_runs": 10,
                "min_market_reviewed": 60,
                "min_coverage_pct": 80.0,
                "min_hit_rate": 45.0,
            }
        },
        "market_horizon_summary": [{
            "market": "US",
            "horizon": "1d",
            "reviewed_shadow_buy_count": reviewed,
            "shadow_review_coverage_pct": coverage,
            "shadow_avg_alpha_pct": alpha,
            "shadow_win_rate": hit,
        }],
    }


def _preflight(*, unique_runs: int = 5, raw_runs: int = 6, reviewed: int = 20, coverage: float = 20.0, alpha: float = 1.37, hit: float = 60.0) -> dict:
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


def _db_summary() -> dict:
    return {
        "latest_run": {
            "run_id": "rec_test",
            "strategy_version": "tech_ai_v2_price_action_gate",
            "status": "generated",
        },
        "us_picks": {
            "total": 20,
            "buy": 20,
            "with_entry_price": 20,
            "bad_scope_or_origin": 0,
        },
        "us_price_coverage": {
            "active_pool": 76,
            "priced": 76,
            "coverage_pct": 100.0,
            "latest_trade_date": "2026-06-03",
        },
        "portfolio_plan_markets": {"US": 15},
    }


def _artifacts(*, form4_age: int = 3) -> dict:
    return {
        "data/event_calendar_us.json": {"exists": True, "age_days": 0},
        "data/event_calendar_us_sec.json": {"exists": True, "age_days": 0},
        "data/event_calendar_us_form4.json": {"exists": True, "age_days": form4_age},
    }


class TestUSProductionAcceptance(unittest.TestCase):
    def _build(self, **overrides):
        args = {
            "quality": _quality(),
            "pipeline": _pipeline(failed_labels=[
                "19/25 事件日历（解禁/减持/财报）",
                "19d/25 港股 HKEX 披露易公告（盈警/停牌/股东/回购/并购）",
                "24c/25 AI 主题雷达证据刷新（每日轻量）",
            ]),
            "plan": _plan(),
            "validation": _validation(),
            "shadow": _shadow(),
            "db_summary": _db_summary(),
            "artifacts": _artifacts(),
            "now": datetime(2026, 6, 4, 12, 0, 0),
        }
        args.update(overrides)
        return us_acceptance.build_us_acceptance(**args)

    def test_us_core_can_pass_while_global_non_us_steps_fail(self):
        payload = self._build(artifacts=_artifacts(form4_age=8))

        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["decision"]["code"], "US_RESEARCH_READY_TRIAL_PENDING")
        self.assertEqual(payload["summary"]["ignored_global_failed_steps"], 3)
        self.assertEqual(payload["summary"]["us_blocking_failed_steps"], 0)
        self.assertTrue(any("Form 4" in w for w in payload["warnings"]))

    def test_us_core_pipeline_failure_blocks(self):
        payload = self._build(pipeline=_pipeline(failed_labels=["19c/25 美股事件日历（yfinance 财报+超预期）"]))

        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["decision"]["code"], "US_BLOCKED")
        self.assertTrue(any("US 核心流水线" in b for b in payload["blockers"]))

    def test_non_us_plan_blocks_us_release(self):
        payload = self._build(plan=_plan(["US", "HK"]))

        self.assertEqual(payload["status"], "FAIL")
        self.assertTrue(any("US-only" in b for b in payload["blockers"]))

    def test_trial_ready_state_when_shadow_thresholds_pass(self):
        payload = self._build(shadow=_shadow(runs=10, reviewed=60, coverage=100.0, alpha=1.1, hit=52.0), artifacts=_artifacts(form4_age=0))

        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["decision"]["code"], "US_TRIAL_READY")
        self.assertTrue(payload["summary"]["trial_ready"])

    def test_preflight_unique_source_count_overrides_raw_shadow_count(self):
        payload = self._build(
            shadow=_shadow(runs=10, reviewed=60, coverage=100.0, alpha=1.1, hit=52.0),
            preflight=_preflight(unique_runs=5, raw_runs=10, reviewed=20, coverage=20.0),
            artifacts=_artifacts(form4_age=0),
        )

        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["decision"]["code"], "US_RESEARCH_READY_TRIAL_PENDING")
        self.assertFalse(payload["summary"]["trial_ready"])
        self.assertEqual(payload["summary"]["us_shadow_unique_source_run_count"], 5)
        self.assertTrue(any("唯一 source run 5/10" in item for item in payload["warnings"]))


if __name__ == "__main__":
    unittest.main()
