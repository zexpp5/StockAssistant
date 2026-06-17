"""AI 长期主线适配器 · 转换逻辑回归测试（注入假 con，不依赖真实 DB）。

锁住真实数据验证里抓到的两个 bug：
  - risk_flags_json 是 [{"code","severity","message"}]，过热判定取 code（不是把 dict 当 set 元素）
  - real_holding_review_items.current_weight 是小数，×100 转 pct；因子映射含名单成员优先 / 显式映射 / unclassified
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

try:
    from scripts.tools.ai_long_term_thesis_adapters import (
        fetch_candidate_sources, fetch_daily_state_inputs, fetch_holdings_for_overlay)
except ImportError:
    from ai_long_term_thesis_adapters import (
        fetch_candidate_sources, fetch_daily_state_inputs, fetch_holdings_for_overlay)


class FakeCon:
    """按 SQL 子串匹配返回预置行，模拟 DuckDB 只读连接。"""

    def __init__(self, plan):
        self.plan = plan
        self._rows = []

    def execute(self, sql, params=None):
        for sub, rows in self.plan:
            if sub in sql:
                self._rows = rows
                return self
        raise AssertionError(f"未预期 SQL: {sql[:70]}")

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def test_daily_state_extracts_code_and_only_overheat_flags():
    over = json.dumps([{"code": "OVERHEATED_1Y", "severity": "high", "message": "x"}])
    low = json.dumps([{"code": "MOMENTUM_REUSED_RECENT_V2_SNAPSHOT", "severity": "low", "message": "y"}])
    con = FakeCon([
        ("LIMIT 1", [("rec_x",)]),
        ("WHERE run_id = ?", [("VRT", over), ("MU", low), ("GEV", None)]),
    ])
    out = fetch_daily_state_inputs(con)
    assert out["latest_run_id"] == "rec_x"
    assert set(out["recommendation_hits"]) == {"VRT", "MU", "GEV"}
    # 只有带 OVERHEATED/RUNUP code 的才算过热；low 档和空 flags 不算
    assert out["price_state_by_symbol"] == {"VRT": "过热"}


def test_holdings_weight_pct_and_factor_mapping():
    con = FakeCon([
        ("created_at DESC", [("realhold_x",)]),
        ("WHERE review_run_id = ?", [("GOOGL", 0.532), ("9992.HK", 0.294), ("VRT", 0.05)]),
    ])
    thesis = {"members": [{"symbol": "VRT", "risk_factor": "power_buildout"}]}
    out = fetch_holdings_for_overlay(con, thesis=thesis)
    by = {h["symbol"]: h for h in out}

    assert by["GOOGL"]["weight_pct"] == 53.2          # 0.532 × 100
    assert by["GOOGL"]["risk_factor"] == "ai_capex_cycle"   # 显式映射
    assert by["VRT"]["risk_factor"] == "power_buildout"     # 名单成员口径优先于显式映射
    assert by["9992.HK"]["risk_factor"] == "unclassified"   # 未知 → 不乱给因子
    assert [h["symbol"] for h in out] == ["GOOGL", "9992.HK", "VRT"]  # 权重降序


def test_holdings_empty_when_no_run():
    con = FakeCon([("created_at DESC", [])])
    assert fetch_holdings_for_overlay(con) == []


def test_candidate_sources_always_includes_bottleneck_seven():
    con = FakeCon([
        ("ai_theme_company_tags", [("NVDA",), ("AMD",)]),
        ("ai_theme_etf_holdings", [("NVDA",)]),
        ("recommendation_picks", [("AVGO",)]),
    ])
    out = fetch_candidate_sources(con)
    assert len(out["bottleneck_signal"]) == 7
    assert out["theme_evidence"] == ["NVDA", "AMD"]
    assert out["etf_consensus"] == ["NVDA"]
    assert out["recent_recommendation"] == ["AVGO"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
