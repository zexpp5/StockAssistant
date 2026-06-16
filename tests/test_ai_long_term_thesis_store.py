"""AI 长期主线逻辑层测试：候选草稿 / 确认即冻结 / why-changed diff / 持仓 overlay / 日变状态。"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

try:
    from scripts.tools.ai_long_term_thesis_store import (
        ALLOWED_ACTION_STATES, SnapshotExistsError, ThesisValidationError,
        build_candidate_drafts, build_daily_state, compute_portfolio_overlay,
        confirm_and_freeze, diff_snapshots)
except ImportError:
    from ai_long_term_thesis_store import (
        ALLOWED_ACTION_STATES, SnapshotExistsError, ThesisValidationError,
        build_candidate_drafts, build_daily_state, compute_portfolio_overlay,
        confirm_and_freeze, diff_snapshots)


# ── 合法名单 + universe 夹具（members ⊆ universe，过护栏）─────

def _member(**over):
    m = {
        "symbol": "VRT", "market": "US", "routes": ["bottleneck"],
        "chain": "data_center_power_cooling", "thesis_status": "active",
        "thesis_1y_2y": "data center power demand stays tight",
        "discipline_principle": "research only", "evidence_basis": "mixed",
        "platform_evidence_level": None,
        "evidence_vector": {"thesis": "B", "financial_proxy": "B", "counter_signal": "fresh",
                            "valuation_overheat": "normal", "event_risk": "none"},
        "counter_signals": ["book-to-bill drops below 1.2"],
        "counter_signal_review_mode": "structured_quarterly_review",
        "counter_signal_freshness": "fresh", "freshness_reason": "review_confirmed_recently",
        "counter_signal_source": "self_supply", "risk_factor": "ai_capex_cycle",
        "last_reviewed_at": "2026-06-16", "change_reason": "first confirmed MVP list",
    }
    m.update(over)
    return m


def _thesis(**over):
    t = {
        "version": "AI长期主线 v2026-06-16", "as_of_date": "2026-06-16",
        "confirmed_at": "2026-06-16T21:30:00+08:00", "reviewed_by": "human",
        "source_policy": "system_draft_then_human_confirmed",
        "change_summary": "first confirmed MVP list",
        "price_basis": "same_pull_adjusted_close_by_trade_date",
        "entry_trade_date_policy": "next_regular_session_after_confirmation",
        "performance_validation_status": "insufficient_sample",
        "primary_test": {"horizon": "6m", "benchmark": "same_universe_equal_weight",
                         "metric": "median_excess_return_gt_0_and_ex_best_still_gt_0"},
        "risk_exposure_summary": {"single_factor_threshold_pct": 50,
                                  "scope": "long_term_list_only", "concentration_warning": False},
        "members": [_member(), _member(symbol="MU", risk_factor="hbm_cycle")],
    }
    t.update(over)
    return t


def _universe():
    return {"snapshot_id": "univ-2026-06-16", "as_of_date": "2026-06-16",
            "symbols": ["VRT", "MU"]}


# ── P0a 候选草稿 ────────────────────────────────────────────

def test_candidate_drafts_dedup_and_provenance():
    drafts = build_candidate_drafts({
        "bottleneck_signal": ["VRT", "MU"],
        "recent_recommendation": ["VRT", "NVDA"],
        "manual": ["ANET"],
    }, universe_symbols=["VRT", "MU", "NVDA"])
    by = {d["symbol"]: d for d in drafts}

    assert {d["symbol"] for d in drafts} == {"VRT", "MU", "NVDA", "ANET"}
    # VRT 命中两个来源，去重合并
    assert {p["source"] for p in by["VRT"]["provenance"]} == {"bottleneck_signal", "recent_recommendation"}
    # 偏倚标注：动量/共识种子
    assert by["VRT"]["bias_seeded"] and by["NVDA"]["bias_seeded"]
    assert not by["MU"]["bias_seeded"] and not by["ANET"]["bias_seeded"]
    # universe 资格
    assert by["NVDA"]["universe_eligible"] and not by["ANET"]["universe_eligible"]
    # 绝不自动确认
    assert all(d["status"] == "candidate_draft" and d["auto_confirmable"] is False for d in drafts)


# ── P0b→P1 确认即冻结（append-only）────────────────────────

def test_confirm_and_freeze_archives_both(tmp_path):
    res = confirm_and_freeze(_thesis(), _universe(), store_dir=tmp_path)
    assert pathlib.Path(res["thesis_snapshot"]).exists()
    assert pathlib.Path(res["universe_snapshot"]).exists()
    live = json.loads((tmp_path / "ai_long_term_thesis.json").read_text(encoding="utf-8"))
    assert live["version"] == "AI长期主线 v2026-06-16"


def test_confirm_and_freeze_rejects_invalid(tmp_path):
    with pytest.raises(ThesisValidationError):
        confirm_and_freeze(_thesis(price_basis="raw_close"), _universe(), store_dir=tmp_path)


def test_confirm_and_freeze_refuses_member_outside_universe(tmp_path):
    # MU 不在 universe → 护栏 member_in_universe_snapshot → 拒绝冻结
    with pytest.raises(ThesisValidationError):
        confirm_and_freeze(_thesis(), {"snapshot_id": "u1", "symbols": ["VRT"]}, store_dir=tmp_path)


def test_confirm_and_freeze_append_only(tmp_path):
    confirm_and_freeze(_thesis(), _universe(), store_dir=tmp_path)
    with pytest.raises(SnapshotExistsError):
        confirm_and_freeze(_thesis(), _universe(), store_dir=tmp_path)


# ── P4 why-changed diff ─────────────────────────────────────

def test_diff_snapshots():
    old = {"members": [{"symbol": "VRT", "thesis_status": "active", "thesis_1y_2y": "a"},
                       {"symbol": "MU", "thesis_status": "active", "thesis_1y_2y": "b"}]}
    new = {"members": [{"symbol": "VRT", "thesis_status": "watch", "thesis_1y_2y": "a",
                        "change_reason": "过热降级"},
                       {"symbol": "GEV", "thesis_status": "active", "thesis_1y_2y": "c",
                        "change_reason": "新增电力瓶颈"}]}
    d = diff_snapshots(old, new)
    assert [x["symbol"] for x in d["added"]] == ["GEV"]
    assert d["added"][0]["change_reason"] == "新增电力瓶颈"
    assert [x["symbol"] for x in d["removed"]] == ["MU"]
    assert d["status_changed"] == [{"symbol": "VRT", "from": "active", "to": "watch",
                                    "change_reason": "过热降级"}]
    assert d["thesis_changed"] == []  # VRT 的 thesis 文本没变


# ── §九.4 持仓 overlay ──────────────────────────────────────

def test_portfolio_overlay_flags_same_factor_doubling():
    thesis = {"members": [{"symbol": "VRT", "risk_factor": "ai_capex_cycle"},
                          {"symbol": "GEV", "risk_factor": "ai_capex_cycle"}]}
    holdings = [{"symbol": "GOOGL", "weight_pct": 60, "risk_factor": "ai_capex_cycle"},
                {"symbol": "PDD", "weight_pct": 10, "risk_factor": "china_consumer"}]
    ov = compute_portfolio_overlay(thesis, holdings, as_of_date="2026-06-16")

    assert ov["combined_risk_exposure"] == {"ai_capex_cycle": 60.0, "china_consumer": 10.0}
    assert ov["shared_factors"] == ["ai_capex_cycle"]
    assert ov["warnings"] and "加码不是分散" in ov["warnings"][0]
    assert ov["writes_real_holdings"] is False


def test_portfolio_overlay_no_warning_when_below_threshold():
    thesis = {"members": [{"symbol": "VRT", "risk_factor": "ai_capex_cycle"}]}
    holdings = [{"symbol": "GOOGL", "weight_pct": 20, "risk_factor": "ai_capex_cycle"}]
    ov = compute_portfolio_overlay(thesis, holdings)
    assert ov["warnings"] == []


# ── P2 日变状态（只用允许值，无交易词）─────────────────────

def test_daily_state_maps_to_allowed_states_only():
    thesis = {"members": [
        {"symbol": "VRT", "thesis_status": "active", "counter_signal_freshness": "fresh"},
        {"symbol": "MU", "thesis_status": "active", "counter_signal_freshness": "stale"},
        {"symbol": "ANET", "thesis_status": "watch", "counter_signal_freshness": "fresh"},
        {"symbol": "GEV", "thesis_status": "active", "counter_signal_freshness": "fresh"},
    ]}
    state = build_daily_state(thesis, recommendation_hits=["VRT"],
                              price_state_by_symbol={"GEV": "过热"})
    by = {s["symbol"]: s for s in state}

    assert by["VRT"]["current_action_state"] == "research_only"
    assert by["VRT"]["today_recommendation_hit"] is True
    assert by["MU"]["current_action_state"] == "evidence_review_needed"   # stale
    assert by["ANET"]["current_action_state"] == "observe_only"           # watch
    assert by["GEV"]["current_action_state"] == "pause_new_research"      # 过热
    assert all(s["current_action_state"] in ALLOWED_ACTION_STATES for s in state)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
