"""美股盘前风险闸门测试。

核心验收：mock 数据复现 2026-06-05 场景（NFP 强 + 10Y 上 + AVGO 财报回吐
+ 韩国/台湾半导体先跌 + 巨头盘前普跌），闸门必须打到 HIGH/CRITICAL（橙/红）。
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from stock_research.core import premarket_gate as pg  # noqa: E402


def _q(pct=None, last=None, prev=None, ok=True):
    """构造一个 quote dict。"""
    return {"last": last, "prev_close": prev, "pct": pct, "source": "mock", "ok": ok, "premarket": False}


def _scenario_2026_06_05() -> dict:
    """复现 2026-06-05 美股全线大跌的盘前快照。"""
    return {
        # 期货：纳指领跌
        "NQ": _q(pct=-2.6), "ES": _q(pct=-1.6), "RTY": _q(pct=-1.8),
        # 利率/美元：10Y 急上 12bp + 美元走强
        "US10Y": _q(last=4.54, prev=4.42), "US5Y": _q(last=4.16, prev=4.06),
        "DXY": _q(pct=0.5),
        # VIX 跳升
        "VIX": _q(last=21.0, prev=18.0),
        # 巨头盘前：7 只里 6 只跌超 1%
        "AAPL": _q(pct=-0.8), "MSFT": _q(pct=-1.2), "NVDA": _q(pct=-3.0),
        "GOOGL": _q(pct=-1.5), "AMZN": _q(pct=-1.1), "META": _q(pct=-2.6),
        "TSLA": _q(pct=-2.0),
        # 板块：成长深杀 + 防御抗跌 = risk-off；半导体重灾
        "XLK": _q(pct=-2.6), "SMH": _q(pct=-3.5), "SOXX": _q(pct=-3.4),
        "XLP": _q(pct=0.2), "XLU": _q(pct=0.1),
        # 海外领先：韩台半导体先跌
        "KOSPI": _q(pct=-1.8), "TWSE": _q(pct=-2.0), "NIKKEI": _q(pct=-1.0),
        "HSI": _q(pct=-0.8),
    }


def _scenario_calm() -> dict:
    """风平浪静的盘前：各族平稳。"""
    return {
        "NQ": _q(pct=0.2), "ES": _q(pct=0.1), "RTY": _q(pct=0.3),
        "US10Y": _q(last=4.20, prev=4.19), "US5Y": _q(last=4.00, prev=3.99),
        "DXY": _q(pct=-0.1),
        "VIX": _q(last=14.0, prev=14.2),
        "AAPL": _q(pct=0.3), "MSFT": _q(pct=0.2), "NVDA": _q(pct=0.5),
        "GOOGL": _q(pct=0.1), "AMZN": _q(pct=-0.2), "META": _q(pct=0.4),
        "TSLA": _q(pct=-0.3),
        "XLK": _q(pct=0.2), "SMH": _q(pct=0.4), "SOXX": _q(pct=0.3),
        "XLP": _q(pct=0.1), "XLU": _q(pct=0.0),
        "KOSPI": _q(pct=0.3), "TWSE": _q(pct=0.2), "NIKKEI": _q(pct=0.1),
        "HSI": _q(pct=0.2),
    }


# ──────────────────────────────────────────────────
# 核心验收
# ──────────────────────────────────────────────────

def test_2026_06_05_must_be_orange_or_red():
    """硬验收：这种全线逆风必须打到 HIGH/CRITICAL。"""
    now = datetime(2026, 6, 5, 20, 30)  # 北京时间 20:30，美股开盘前
    res = pg.compute_gate(
        quotes=_scenario_2026_06_05(),
        as_of=date(2026, 6, 5),
        now=now,
    )
    assert res.color in ("HIGH", "CRITICAL"), f"应为橙/红，实际 {res.color}（composite={res.composite}）"
    assert res.composite >= 1.1
    assert "不" in res.can_buy  # "不建议开新仓" / "不开新仓"
    assert res.pressure_sources, "应识别出压力源"


def test_calm_day_is_green_or_yellow():
    """风平浪静应为绿/黄，不能误报。"""
    res = pg.compute_gate(
        quotes=_scenario_calm(),
        as_of=date(2026, 6, 9),  # 非首周五、非 CPI 窗口
        now=datetime(2026, 6, 9, 20, 30),
    )
    assert res.color in ("NONE", "LOW"), f"应为绿/黄，实际 {res.color}（composite={res.composite}）"


def _scenario_tailwind() -> dict:
    """明显顺风的盘前：期货涨、恐慌低、巨头涨、板块涨、海外涨、利率回落。"""
    return {
        "NQ": _q(pct=1.0), "ES": _q(pct=0.8), "RTY": _q(pct=0.7),
        "US10Y": _q(last=4.10, prev=4.16), "US5Y": _q(last=3.90, prev=3.95),
        "DXY": _q(pct=-0.2),
        "VIX": _q(last=13.0, prev=13.5),
        "AAPL": _q(pct=0.9), "MSFT": _q(pct=1.1), "NVDA": _q(pct=1.5),
        "GOOGL": _q(pct=0.8), "AMZN": _q(pct=0.7), "META": _q(pct=1.0),
        "TSLA": _q(pct=0.6),
        "XLK": _q(pct=1.0), "SMH": _q(pct=1.4), "SOXX": _q(pct=1.3),
        "XLP": _q(pct=0.1), "XLU": _q(pct=0.0),
        "KOSPI": _q(pct=1.2), "TWSE": _q(pct=1.0), "NIKKEI": _q(pct=0.8),
        "HSI": _q(pct=0.9),
    }


def test_tailwind_strong_day_flagged():
    """明显顺风 → is_tailwind=True，标题说「顺风」，仍是绿灯。"""
    res = pg.compute_gate(quotes=_scenario_tailwind(), as_of=date(2026, 6, 9),
                          now=datetime(2026, 6, 9, 20, 30))
    assert res.color == "NONE"
    assert res.is_tailwind is True and res.tailwind_score >= 3
    assert "顺风" in res.headline_plain
    assert res.tailwind_reasons  # 有"为什么算顺风"
    # 仍只到环境层面，不喊买某只股
    assert "追高" in res.can_buy or "纪律" in res.can_buy


def test_calm_day_not_tailwind():
    """风平浪静但不够有利 → 绿灯但不算顺风（不夸大）。"""
    res = pg.compute_gate(quotes=_scenario_calm(), as_of=date(2026, 6, 9),
                          now=datetime(2026, 6, 9, 20, 30))
    assert res.color in ("NONE", "LOW")
    assert res.is_tailwind is False


def test_no_tailwind_when_warning():
    """逆风大跌日不可能是顺风。"""
    res = pg.compute_gate(quotes=_scenario_2026_06_05(), as_of=date(2026, 6, 5),
                          now=datetime(2026, 6, 5, 20, 30))
    assert res.is_tailwind is False and res.tailwind_score == 0


def test_vix_panic_forces_critical():
    """VIX≥40 硬覆盖到 CRITICAL。"""
    q = _scenario_calm()
    q["VIX"] = _q(last=42.0, prev=30.0)
    res = pg.compute_gate(quotes=q, as_of=date(2026, 6, 9), now=datetime(2026, 6, 9, 20, 30))
    assert res.color == "CRITICAL"
    assert any("VIX" in n for n in res.notes)


def test_nq_deep_drop_forces_at_least_high():
    """纳指期货跌超 2% 硬覆盖到 ≥HIGH，即便其它族平稳。"""
    q = _scenario_calm()
    q["NQ"] = _q(pct=-2.3)
    res = pg.compute_gate(quotes=q, as_of=date(2026, 6, 9), now=datetime(2026, 6, 9, 20, 30))
    assert pg.SEVERITY_ORDER[res.color] >= pg.SEVERITY_ORDER["HIGH"]


# ──────────────────────────────────────────────────
# 信号族
# ──────────────────────────────────────────────────

def test_ai_hardware_tag_from_semis():
    """半导体（板块 + 海外）先跌应打 ai_hardware 标签。"""
    res = pg.compute_gate(quotes=_scenario_2026_06_05(), as_of=date(2026, 6, 5),
                          now=datetime(2026, 6, 5, 20, 30))
    all_tags = {t for f in res.families for t in f["tags"]}
    assert "ai_hardware" in all_tags


def test_rates_spike_detected():
    """10Y 上 12bp 应识别为 rates_spike。"""
    res = pg.compute_gate(quotes=_scenario_2026_06_05(), as_of=date(2026, 6, 5),
                          now=datetime(2026, 6, 5, 20, 30))
    rates = next(f for f in res.families if f["key"] == "rates")
    assert "rates_spike" in rates["tags"]
    assert rates["data"]["ten_year_bps_1d"] >= 8


def test_megacap_breadth_counts():
    """6 只巨头跌超 1% 应被数出来。"""
    res = pg.compute_gate(quotes=_scenario_2026_06_05(), as_of=date(2026, 6, 5),
                          now=datetime(2026, 6, 5, 20, 30))
    mega = next(f for f in res.families if f["key"] == "megacap")
    assert len(mega["data"]["down_over_1pct"]) == 6
    assert mega["stress"] >= 2.0


# ──────────────────────────────────────────────────
# 宏观日历
# ──────────────────────────────────────────────────

def test_nfp_detected_on_first_friday():
    """2026-06-05 是 6 月第一个周五 → NFP 日。"""
    assert date(2026, 6, 5).weekday() == 4 and date(2026, 6, 5).day <= 7
    events = pg._macro_events_for(date(2026, 6, 5))
    assert any(e["type"] == "NFP" for e in events)


def test_macro_pending_before_release():
    """发布前（21 点前）事件应标 event_pending 且贡献压力。"""
    sig = pg._sig_macro(date(2026, 6, 5), now=datetime(2026, 6, 5, 20, 10))
    assert "event_pending" in sig.tags
    assert sig.stress >= 1.0


# ──────────────────────────────────────────────────
# 持仓绑定
# ──────────────────────────────────────────────────

def test_holdings_overlay_flags_ai_hardware():
    """持有 MRVL（AI 硬件）在半导体先跌的夜晚应被点名。"""
    holdings = [
        {"symbol": "MRVL", "market": "US", "shares": 100},
        {"symbol": "KO", "market": "US", "shares": 100},      # 防御消费，不应因 AI 硬件被点名
        {"symbol": "600519", "market": "A", "shares": 100},   # A 股，不受美股盘前直接影响
    ]
    res = pg.compute_gate(quotes=_scenario_2026_06_05(), as_of=date(2026, 6, 5),
                          now=datetime(2026, 6, 5, 20, 30), holdings=holdings)
    flagged = {h["symbol"] for h in res.holdings_impact}
    assert "MRVL" in flagged
    assert "600519" not in flagged  # A 股不应被美股盘前归因
    assert "KO" not in flagged      # 防御低估值票不应被"高估值成长"误伤


def test_holdings_overlay_empty_when_no_holdings():
    res = pg.compute_gate(quotes=_scenario_2026_06_05(), as_of=date(2026, 6, 5),
                          now=datetime(2026, 6, 5, 20, 30), holdings=None)
    assert res.holdings_impact == []


# ──────────────────────────────────────────────────
# 历史回溯 / 战绩评分
# ──────────────────────────────────────────────────

def test_score_outcome_true_positive():
    """警了(红) + 当天真跌 → 真预警。"""
    assert pg.score_outcome("CRITICAL", spy_pct=-1.6, nq_pct=-2.6) == "TRUE_POSITIVE"


def test_score_outcome_false_alarm():
    """警了(橙) + 当天没跌 → 虚惊。"""
    assert pg.score_outcome("HIGH", spy_pct=0.3, nq_pct=0.1) == "FALSE_ALARM"


def test_score_outcome_miss():
    """没警(绿) + 当天真跌 → 漏报(最糟)。"""
    assert pg.score_outcome("NONE", spy_pct=-1.2, nq_pct=-2.0) == "MISS"


def test_score_outcome_true_negative():
    """没警(黄) + 当天没跌 → 正常。"""
    assert pg.score_outcome("LOW", spy_pct=0.2, nq_pct=0.4) == "TRUE_NEGATIVE"


def test_score_outcome_unsettled_when_no_data():
    assert pg.score_outcome("CRITICAL", None, None) is None


def test_score_outcome_nq_threshold_alone_triggers_bad():
    """只有纳指跌穿门槛(标普还好)也算真跌。"""
    assert pg.score_outcome("HIGH", spy_pct=-0.3, nq_pct=-1.5) == "TRUE_POSITIVE"


def test_summarize_history_counts_and_rates():
    records = [
        {"date": "2026-06-01", "color": "CRITICAL", "outcome": "TRUE_POSITIVE"},
        {"date": "2026-06-02", "color": "HIGH", "outcome": "FALSE_ALARM"},
        {"date": "2026-06-03", "color": "NONE", "outcome": "MISS"},
        {"date": "2026-06-04", "color": "LOW", "outcome": "TRUE_NEGATIVE"},
        {"date": "2026-06-05", "color": "CRITICAL"},  # 未结算，不计入
    ]
    s = pg.summarize_history(records)
    assert s["settled_days"] == 4
    assert s["warnings_issued"] == 2          # 2 次橙/红
    assert s["true_positive"] == 1 and s["false_alarm"] == 1
    assert s["miss"] == 1 and s["true_negative"] == 1
    assert s["precision_pct"] == 50           # 警报里 1/2 真跌
    assert s["recall_pct"] == 50              # 真跌 2 天抓到 1 天


def test_summarize_empty():
    s = pg.summarize_history([])
    assert s["settled_days"] == 0
    assert s["precision_pct"] is None
    assert s["enough_sample"] is False
    assert s["tailwind"]["n"] == 0


def test_tailwind_validation_counts():
    """顺风验证：说顺风的天里，几天真涨、几天打脸。"""
    records = [
        {"color": "NONE", "is_tailwind": True, "outcome": "TRUE_NEGATIVE",
         "actual": {"spy_pct": 1.2, "nq_pct": 1.5}},   # 顺风 → 真涨
        {"color": "NONE", "is_tailwind": True, "outcome": "TRUE_NEGATIVE",
         "actual": {"spy_pct": 0.3, "nq_pct": 0.1}},   # 顺风 → 小涨
        {"color": "NONE", "is_tailwind": True, "outcome": "MISS",
         "actual": {"spy_pct": -1.5, "nq_pct": -2.0}},  # 顺风却大跌 → 打脸
        {"color": "NONE", "is_tailwind": False, "outcome": "TRUE_NEGATIVE",
         "actual": {"spy_pct": 0.2, "nq_pct": 0.1}},   # 普通绿灯，不算顺风
    ]
    tw = pg.summarize_history(records)["tailwind"]
    assert tw["n"] == 3            # 3 个顺风日
    assert tw["rose"] == 2         # 2 天真涨
    assert tw["backfired"] == 1    # 1 天打脸(顺风却大跌)


def test_enough_sample_threshold():
    """样本量低于门槛 → enough_sample=False（UI 据此灰显）。"""
    recs = [{"date": f"2026-06-{d:02d}", "color": "NONE", "outcome": "TRUE_NEGATIVE",
             "actual": {"spy_pct": 0.2, "nq_pct": 0.3}} for d in range(1, 6)]
    s = pg.summarize_history(recs, min_sample=20)
    assert s["enough_sample"] is False and s["settled_days"] == 5
    # 凑够门槛
    s2 = pg.summarize_history(recs, min_sample=5)
    assert s2["enough_sample"] is True


def test_color_buckets_separation():
    """按颜色分档：红档事后应明显比绿档惨。"""
    recs = [
        {"color": "CRITICAL", "outcome": "TRUE_POSITIVE", "actual": {"spy_pct": -2.5, "nq_pct": -4.0}},
        {"color": "CRITICAL", "outcome": "TRUE_POSITIVE", "actual": {"spy_pct": -1.5, "nq_pct": -2.0}},
        {"color": "NONE", "outcome": "TRUE_NEGATIVE", "actual": {"spy_pct": 0.4, "nq_pct": 0.6}},
        {"color": "NONE", "outcome": "TRUE_NEGATIVE", "actual": {"spy_pct": 0.2, "nq_pct": 0.1}},
    ]
    s = pg.summarize_history(recs)
    assert s["color_buckets"]["CRITICAL"]["avg_return"] == -2.0
    assert s["color_buckets"]["NONE"]["avg_return"] == 0.3
    assert s["color_buckets"]["CRITICAL"]["bad_rate"] == 100
    assert s["color_buckets"]["NONE"]["bad_rate"] == 0


def test_baseline_never_warn_recall_zero():
    """永远说绿基准：有真跌日时 recall=0（一个都抓不到）。"""
    recs = [
        {"color": "CRITICAL", "outcome": "TRUE_POSITIVE", "actual": {"spy_pct": -2.0, "nq_pct": -3.0}},
        {"color": "NONE", "outcome": "TRUE_NEGATIVE", "actual": {"spy_pct": 0.3, "nq_pct": 0.2}},
    ]
    s = pg.summarize_history(recs)
    assert s["bad_days"] == 1
    assert s["baseline_never_warn"]["recall_pct"] == 0


def test_baseline_vix_only():
    """只看VIX基准：VIX≥20 当预警，与真实涨跌比。"""
    recs = [
        {"color": "NONE", "outcome": "MISS", "vix": 16.0, "actual": {"spy_pct": -1.5, "nq_pct": -2.0}},  # 低VIX没警→VIX基准也漏
        {"color": "CRITICAL", "outcome": "TRUE_POSITIVE", "vix": 28.0, "actual": {"spy_pct": -2.0, "nq_pct": -3.0}},  # 高VIX→VIX基准警中
    ]
    s = pg.summarize_history(recs)
    b = s["baseline_vix_only"]
    assert b is not None and b["n"] == 2
    assert b["precision_pct"] == 100   # VIX 警了1次(28那天)，命中
    assert b["miss"] == 1              # 16那天VIX没警但真跌 → VIX 基准漏报


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
