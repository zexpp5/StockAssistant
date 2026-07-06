from __future__ import annotations

from datetime import date

from stock_research.core.revision_trend import (
    format_revision_line,
    summarize_revision_trend,
)


AS_OF = date(2026, 7, 6)


def test_revision_trend_upgrading():
    s = summarize_revision_trend(
        [
            {"event_date": "2026-07-01", "price_target_action": "raises", "price_target": 120, "prior_price_target": 100},
            {"event_date": "2026-06-01", "price_target_action": "raises", "price_target": 110, "prior_price_target": 100},
            {"event_date": "2026-05-15", "price_target_action": "maintains", "price_target": 100, "prior_price_target": 100},
        ],
        as_of=AS_OF,
    )
    assert s["direction"] == "上调中"
    assert s["n_up"] == 2
    assert s["n_down"] == 0
    assert s["net_target_change_pct"] == 15.0
    assert "2 家上调 / 0 家下调" in format_revision_line(s)


def test_revision_trend_downgrading():
    s = summarize_revision_trend(
        [
            {"event_date": "2026-07-01", "price_target_action": "lowers", "price_target": 80, "prior_price_target": 100},
            {"event_date": "2026-06-20", "price_target_action": "lowers", "price_target": 90, "prior_price_target": 100},
        ],
        as_of=AS_OF,
    )
    assert s["direction"] == "下调中"
    assert s["n_up"] == 0
    assert s["n_down"] == 2
    assert s["net_target_change_pct"] == -15.0


def test_revision_trend_diverging():
    s = summarize_revision_trend(
        [
            {"event_date": "2026-07-01", "price_target_action": "raises", "price_target": 120, "prior_price_target": 100},
            {"event_date": "2026-06-20", "price_target_action": "lowers", "price_target": 80, "prior_price_target": 100},
        ],
        as_of=AS_OF,
    )
    assert s["direction"] == "分歧"
    assert s["n_up"] == 1
    assert s["n_down"] == 1


def test_revision_trend_quiet_for_maintains():
    s = summarize_revision_trend(
        [
            {"event_date": "2026-07-01", "price_target_action": "maintains", "price_target": 100, "prior_price_target": 100},
        ],
        as_of=AS_OF,
    )
    assert s["direction"] == "平静"
    assert format_revision_line(s) == "📈 分析师风向：90 天无修正记录"


def test_revision_trend_empty_data():
    s = summarize_revision_trend([], as_of=AS_OF)
    assert s["direction"] == "平静"
    assert s["latest_date"] is None
