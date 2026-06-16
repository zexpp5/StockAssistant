"""AI 长期主线 MVP 护栏校验器测试。

对齐 docs/V2/2026-06-16_AI长期主线与推荐准确性数据评估.md §九.5 的 22 条测试规格：
干净名单必须零违规；每条规则各有一个触发用例点亮对应测试名；§九.3 的 freshness
worst 规则单独验证“事件规则只降级不升级”。
"""
from __future__ import annotations

import pathlib
import sys
from datetime import date

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

try:
    from scripts.tools.validate_ai_long_term_thesis import (compute_freshness,
                                                            validate)
except ImportError:  # 兜底：直接按文件名导入
    from validate_ai_long_term_thesis import compute_freshness, validate


# ── 测试夹具 ────────────────────────────────────────────────

def _member(**over):
    """一个合法的 bottleneck active 成员（VRT），可用关键字覆盖任意字段。"""
    m = {
        "symbol": "VRT", "market": "US", "routes": ["bottleneck"],
        "chain": "data_center_power_cooling", "thesis_status": "active",
        "thesis_1y_2y": "data center power and cooling demand stays tight",
        "discipline_principle": "research only; no daily buy instruction",
        "evidence_basis": "mixed", "platform_evidence_level": None,
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


def _mu(**over):
    """第二个合法成员（MU，不同风险因子），让默认名单的单因子占比 = 50%，不触发集中度。"""
    return _member(symbol="MU", risk_factor="hbm_cycle", **over)


def _thesis(members=None, **over):
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
        "members": members if members is not None else [_member(), _mu()],
    }
    t.update(over)
    return t


def _names(viols):
    return {x.test for x in viols}


# ── 基线：干净名单必须零违规 ──────────────────────────────────

def test_clean_baseline_passes():
    assert validate(_thesis()) == []


# ── §九.5 逐条触发 ──────────────────────────────────────────

def test_schema_required_fields_top():
    t = _thesis()
    del t["version"]
    assert "schema_required_fields" in _names(validate(t))


def test_schema_required_fields_member():
    m = _member()
    m.pop("counter_signal_source")
    assert "schema_required_fields" in _names(validate(_thesis(members=[m, _mu()])))


def test_no_trade_words_page_text():
    payload = {"text": "现在可以买入", "portfolio_overlay": {"read_only": True}}
    assert "no_trade_words" in _names(validate(_thesis(), page_payload=payload))


def test_no_trade_words_free_text():
    bad = _member(thesis_1y_2y="估值到位可低吸")
    assert "no_trade_words_free_text" in _names(validate(_thesis(members=[bad, _mu()])))


def test_active_requires_counter_signals():
    bad = _member(counter_signals=[])
    assert "active_requires_counter_signals" in _names(validate(_thesis(members=[bad, _mu()])))


def test_active_requires_risk_factor():
    bad = _member(risk_factor="")
    assert "active_requires_risk_factor" in _names(validate(_thesis(members=[bad, _mu()])))


def test_active_requires_change_reason():
    bad = _member()
    bad.pop("change_reason")
    assert "active_requires_change_reason" in _names(validate(_thesis(members=[bad, _mu()])))


def test_platform_level_required():
    plat = _member(symbol="ORCL", routes=["platform"], platform_evidence_level=None,
                   counter_signal_source="manual_thesis")
    assert "platform_level_required" in _names(validate(_thesis(members=[plat, _mu()])))


def test_no_fake_ai_attribution():
    plat = _member(symbol="ORCL", routes=["platform"],
                   platform_evidence_level="ai_attribution_verified",
                   counter_signal_source="manual_thesis")  # 无 ai_attribution_disclosure
    assert "no_fake_ai_attribution" in _names(validate(_thesis(members=[plat, _mu()])))


def test_bottleneck_review_required():
    bad = _member()
    bad.pop("counter_signal_review_mode")
    assert "bottleneck_review_required" in _names(validate(_thesis(members=[bad, _mu()])))


def test_capex_not_self_supply():
    googl = _member(symbol="GOOGL", routes=["platform"], platform_evidence_level="manual_thesis",
                    counter_signal_source="self_supply", risk_factor="ai_capex_cycle")
    assert "capex_not_self_supply" in _names(validate(_thesis(members=[googl, _mu()])))


def test_meta_demand_valve_direction():
    meta = _member(symbol="META", routes=["platform"], platform_evidence_level="manual_thesis",
                   counter_signal_source="self_supply", risk_factor="ai_capex_cycle")
    assert "meta_demand_valve_direction" in _names(validate(_thesis(members=[meta, _mu()])))


def test_factor_concentration_warning():
    both = [_member(), _member(symbol="GEV")]  # 都是 ai_capex_cycle → 100% > 50%，warning=False
    assert "factor_concentration_warning" in _names(validate(_thesis(members=both)))


def test_portfolio_overlay_required():
    payload = {"text": "未来 1-2 年 AI 主线研究"}  # 缺 portfolio_overlay
    assert "portfolio_overlay_required" in _names(validate(_thesis(), page_payload=payload))


def test_frozen_snapshot_required():
    ctx = {"frozen_snapshot_archived": False, "universe_snapshot_archived": True}
    assert "frozen_snapshot_required" in _names(validate(_thesis(), build_context=ctx))


def test_universe_snapshot_required():
    ctx = {"frozen_snapshot_archived": True, "universe_snapshot_archived": False}
    assert "universe_snapshot_required" in _names(validate(_thesis(), build_context=ctx))


def test_member_in_universe_snapshot():
    snap = {"symbols": ["VRT"]}  # MU 不在内
    assert "member_in_universe_snapshot" in _names(validate(_thesis(), universe_snapshot=snap))


def test_adjusted_close_same_basis():
    assert "adjusted_close_same_basis" in _names(validate(_thesis(price_basis="raw_close")))


def test_adjusted_close_frozen_value_rejected():
    bad = _member(entry_adj_close=123.45)
    assert "adjusted_close_same_basis" in _names(validate(_thesis(members=[bad, _mu()])))


def test_freshness_worst_rule_member_level():
    # 最终 aging 优于 worst(stale, aging)=stale → 违规
    bad = _member(calendar_freshness="stale", event_freshness="aging",
                  counter_signal_freshness="aging")
    assert "freshness_worst_rule" in _names(validate(_thesis(members=[bad, _mu()])))


def test_validation_status_source_gated():
    t = _thesis(performance_validation_status="passed_primary_test")
    assert "validation_status_source" in _names(validate(t))


def test_validation_status_allowed_when_pipeline_live():
    t = _thesis(performance_validation_status="passed_primary_test")
    assert "validation_status_source" not in _names(validate(t, outcome_pipeline_live=True))


def test_performance_not_overclaimed():
    payload = {"text": "AI 主线研究", "portfolio_overlay": {"read_only": True},
               "performance_text": "本名单已跑赢 QQQ"}
    assert "performance_not_overclaimed" in _names(validate(_thesis(), page_payload=payload))


def test_no_watchlist_write():
    ctx = {"frozen_snapshot_archived": True, "universe_snapshot_archived": True,
           "wrote_watchlist": True}
    assert "no_watchlist_write" in _names(validate(_thesis(), build_context=ctx))


def test_no_real_holding_write():
    ctx = {"frozen_snapshot_archived": True, "universe_snapshot_archived": True,
           "wrote_real_holdings": True}
    assert "no_real_holding_write" in _names(validate(_thesis(), build_context=ctx))


# ── §九.3 反证新鲜度计算 ────────────────────────────────────

AS_OF = date(2026, 6, 16)


def test_freshness_missing():
    assert compute_freshness(None, as_of=AS_OF) == ("missing", "missing_review")


def test_freshness_calendar_fresh():
    assert compute_freshness("2026-06-01", as_of=AS_OF)[0] == "fresh"


def test_freshness_calendar_aging():
    assert compute_freshness("2026-03-01", as_of=AS_OF)[0] == "aging"  # ~107 天


def test_freshness_calendar_stale():
    assert compute_freshness("2025-12-01", as_of=AS_OF)[0] == "stale"  # ~197 天


def test_freshness_event_does_not_upgrade_stale():
    # 日历 stale（200 天前）+ 财报 2 天前未复查（事件本会判 aging）→ 最终必须仍是 stale
    fresh, _reason = compute_freshness("2025-11-28", as_of=AS_OF,
                                       latest_earnings_date="2026-06-14")
    assert fresh == "stale"


def test_freshness_post_earnings_downgrades_fresh_to_aging():
    # 日历 fresh（10 天前复查）+ 财报 3 天前才出、晚于复查 → 降到 aging
    fresh, reason = compute_freshness("2026-06-06", as_of=AS_OF,
                                      latest_earnings_date="2026-06-13")
    assert fresh == "aging"
    assert reason == "post_earnings_unreviewed"


def test_freshness_post_earnings_overdue_to_stale():
    # 财报晚于复查且已过 >7 天未复查 → stale
    fresh, reason = compute_freshness("2026-05-01", as_of=AS_OF,
                                      latest_earnings_date="2026-05-20")
    assert fresh == "stale"
    assert reason == "post_earnings_unreviewed_overdue"


def test_freshness_calendar_only_reason_when_no_earnings():
    assert compute_freshness("2026-06-01", as_of=AS_OF)[1] == "calendar_only"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
