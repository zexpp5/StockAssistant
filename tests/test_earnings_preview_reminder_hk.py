"""财报前预告：港股（如泡泡玛特 9992.HK）覆盖 + 自选∪持仓 universe 归市场。

回归背景：原 job 只读 event_calendar_us.json + manual_watchlist[US]，
港股（走 event_calendar_hk.json）永远不会触发临近提醒。2026-07-07 扩为
美股 + 港股、自选 ∪ 真实持仓。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from stock_research.jobs import earnings_preview_reminder as epr


def _write_cal(tmp_path: Path, name: str, events: list[dict]) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps({"events": events}, ensure_ascii=False), encoding="utf-8")
    return p


def test_hk_ticker_in_window_triggers(tmp_path):
    cal = _write_cal(tmp_path, "hk.json", [
        {"ticker": "9992.HK", "name": "泡泡玛特",
         "event_date": "2026-08-19", "event_type": "earnings_upcoming"},
    ])
    hk = {"9992.HK": "泡泡玛特"}
    # 财报前 6 天：命中
    due = epr._due_from_calendar(date(2026, 8, 13), cal, hk, {})
    assert "9992.HK" in due
    assert due["9992.HK"]["days_until"] == 6
    assert due["9992.HK"]["name"] == "泡泡玛特"


def test_out_of_window_and_strict_before(tmp_path):
    cal = _write_cal(tmp_path, "hk.json", [
        {"ticker": "9992.HK", "event_date": "2026-08-19",
         "event_type": "earnings_upcoming"},
    ])
    hk = {"9992.HK": ""}
    # 太远（43 天后）：不命中
    assert epr._due_from_calendar(date(2026, 7, 7), cal, hk, {}) == {}
    # 严格「前」：财报当天不算（交给到点提醒）
    assert epr._due_from_calendar(date(2026, 8, 19), cal, hk, {}) == {}
    # 边界：前 1 天算
    assert "9992.HK" in epr._due_from_calendar(date(2026, 8, 18), cal, hk, {})


def test_universe_filter_excludes_non_holdings(tmp_path):
    cal = _write_cal(tmp_path, "hk.json", [
        {"ticker": "9992.HK", "event_date": "2026-08-19",
         "event_type": "earnings_upcoming"},
        {"ticker": "0700.HK", "event_date": "2026-08-15",
         "event_type": "earnings_upcoming"},  # 不在 universe
    ])
    hk = {"9992.HK": "泡泡玛特"}
    due = epr._due_from_calendar(date(2026, 8, 13), cal, hk, {})
    assert set(due) == {"9992.HK"}


def test_load_universe_splits_market_by_suffix(monkeypatch):
    """自选∪持仓：按 .HK 后缀归市场；持仓表的 NULL 名不覆盖自选名。"""
    class _FakeCon:
        def __init__(self):
            self._q = None

        def execute(self, sql):
            self._q = sql
            return self

        def fetchall(self):
            if "manual_watchlist" in self._q:
                return [("9992.HK", "泡泡玛特"), ("NVDA", "英伟达"), ("AAPL", "苹果")]
            if "real_holdings" in self._q:
                # 持仓里 9992 名字为 NULL（不该覆盖自选名），并新增一只未在自选的港股
                return [("9992.HK", None), ("3690.HK", "美团"), ("TSM", None)]
            return []

        def close(self):
            pass

    monkeypatch.setattr(epr, "_connect_ro", lambda: _FakeCon())
    us, hk = epr._load_universe()
    assert set(hk) == {"9992.HK", "3690.HK"}
    assert set(us) == {"NVDA", "AAPL", "TSM"}
    # 自选的非空名不被持仓 NULL 覆盖
    assert hk["9992.HK"] == "泡泡玛特"
    # 只在持仓、不在自选的也纳入
    assert hk["3690.HK"] == "美团"


def test_load_universe_db_unavailable_returns_empty(monkeypatch):
    monkeypatch.setattr(epr, "_connect_ro", lambda: None)
    assert epr._load_universe() == ({}, {})
