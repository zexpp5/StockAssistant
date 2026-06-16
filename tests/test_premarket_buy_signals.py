"""盘前可买/别追名单单测 — 票池合并、🟢/🔴 分档、日报卡结构。"""
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from stock_research.core import premarket_buy_signals as pbs  # noqa: E402


class FakeConn:
    """按 SQL 文本返回固定行，模拟 manual_watchlist / recommendation_picks / price_daily 查询。"""

    def __init__(self, watchlist, picks, prices=None):
        self._watchlist = watchlist
        self._picks = picks
        self._prices = prices or []  # 近 N 日收盘（新→旧），供 _recent_drawdown

    def execute(self, sql, params=None):
        if "manual_watchlist" in sql:
            rows = self._watchlist
        elif "recommendation_picks" in sql:
            rows = self._picks
        elif "price_daily" in sql:
            rows = [(c,) for c in self._prices]
        else:
            rows = []
        return SimpleNamespace(fetchall=lambda: rows)


def test_gather_universe_dedup_and_source_merge():
    # MU 同时在自选 + 推荐 + 瓶颈宇宙 → 三来源合并，不重复
    conn = FakeConn(
        watchlist=[("US", "MU"), ("US", "AAPL")],
        picks=[("US", "MU"), ("US", "NVDA")],
    )
    uni = pbs._gather_universe(conn)
    assert uni["MU"]["sources"] == [pbs.SRC_WATCHLIST, pbs.SRC_PICK, pbs.SRC_BOTTLENECK]
    assert uni["AAPL"]["sources"] == [pbs.SRC_WATCHLIST]
    assert uni["NVDA"]["sources"] == [pbs.SRC_PICK]
    # 瓶颈宇宙票（如 GEV）即使不在自选/推荐也进池
    assert "GEV" in uni and pbs.SRC_BOTTLENECK in uni["GEV"]["sources"]


def test_compute_buy_avoid_classifies_green_red(monkeypatch):
    conn = FakeConn(watchlist=[("US", "CHEAP"), ("US", "PRICEY"), ("US", "MID")], picks=[])

    def fake_zones(symbols, c, today=None):
        return {
            "CHEAP": {"symbol": "CHEAP", "method": "估值", "current": 70.0,
                      "low": 80.0, "high": 100.0, "target": 100.0, "position": "便宜"},
            "PRICEY": {"symbol": "PRICEY", "method": "技术", "current": 150.0,
                       "low": 90.0, "high": 110.0, "target": None, "position": "偏贵"},
            "MID": {"symbol": "MID", "method": "估值", "current": 95.0,
                    "low": 80.0, "high": 100.0, "target": 110.0, "position": "区间内"},
        }

    monkeypatch.setattr(pbs.buy_zone, "compute_buy_zones", fake_zones)
    out = pbs.compute_buy_avoid(conn=conn)

    green_syms = [g["symbol"] for g in out["green"]]
    red_syms = [r["symbol"] for r in out["red"]]
    assert green_syms == ["CHEAP"]      # 只有"便宜"进 green
    assert red_syms == ["PRICEY"]       # 只有"偏贵"进 red
    assert "MID" not in green_syms and "MID" not in red_syms  # 区间内两边都不进
    # zoned = 算得出区间的数量；universe 含瓶颈宇宙，故 >= 3
    assert out["zoned"] == 3
    assert out["universe_size"] >= 3
    # 结构字段齐全
    g = out["green"][0]
    assert {"symbol", "sources", "position", "current", "low", "high", "line"} <= set(g)


def test_hk_and_a_share_hidden_by_default(monkeypatch):
    # 港股 + A 股默认都不进名单（美股盘前卡 US-only）
    conn = FakeConn(
        watchlist=[("US", "MU"), ("CN", "300073.SZ"), ("HK", "9618.HK")], picks=[]
    )

    def fake_zones(symbols, c, today=None):
        return {s: {"symbol": s, "method": "估值", "current": 70.0, "low": 80.0,
                    "high": 100.0, "target": 100.0, "position": "便宜"} for s in symbols}

    monkeypatch.setattr(pbs.buy_zone, "compute_buy_zones", fake_zones)
    out = pbs.compute_buy_avoid(conn=conn)
    syms = [g["symbol"] for g in out["green"]]
    assert "MU" in syms                                   # 美股保留
    assert "300073.SZ" not in syms and "9618.HK" not in syms  # 港/A 默认过滤
    # 显式放开各自参数时才出现
    out_a = pbs.compute_buy_avoid(conn=conn, include_a_share=True)
    assert "300073.SZ" in [g["symbol"] for g in out_a["green"]]
    assert "9618.HK" not in [g["symbol"] for g in out_a["green"]]  # 只开 A 股，港股仍隐
    out_hk = pbs.compute_buy_avoid(conn=conn, include_hk=True)
    assert "9618.HK" in [g["symbol"] for g in out_hk["green"]]


def test_enrichment_discount_stale_and_falling_knife(monkeypatch):
    from datetime import date, timedelta
    old_target = (date.today() - timedelta(days=90)).isoformat()
    # 收盘(新→旧) 75/80/90/100/95 → 现价75、期间高点100 → -25% 回撤 → 接飞刀
    conn = FakeConn(watchlist=[("US", "FALL")], picks=[], prices=[75, 80, 90, 100, 95])

    def fake_zones(symbols, c, today=None):
        return {"FALL": {"symbol": "FALL", "method": "估值", "current": 75.0,
                         "low": 80.0, "high": 100.0, "target": 100.0,
                         "target_date": old_target, "position": "便宜"}}

    monkeypatch.setattr(pbs.buy_zone, "compute_buy_zones", fake_zones)
    g = pbs.compute_buy_avoid(conn=conn)["green"][0]
    assert g["discount_pct"] == -25       # A：现价75 vs 目标价100
    assert g["target_stale"] is True       # B：目标价90天>60天
    assert g["falling_knife"] is True      # C：近20日 -25% <= -20%
    assert any("接飞刀" in f for f in g["flags"])
    assert any("偏旧" in f for f in g["flags"])
    assert "比目标价低 25%" in g["line"] and "接飞刀" in g["line"]


def test_no_flags_when_fresh_and_stable(monkeypatch):
    from datetime import date
    # 目标价今天、价格平稳(无急跌) → 不该有任何 ⚠️
    conn = FakeConn(watchlist=[("US", "CALM")], picks=[], prices=[99, 100, 99, 100, 98])

    def fake_zones(symbols, c, today=None):
        return {"CALM": {"symbol": "CALM", "method": "估值", "current": 80.0,
                         "low": 85.0, "high": 100.0, "target": 100.0,
                         "target_date": date.today().isoformat(), "position": "便宜"}}

    monkeypatch.setattr(pbs.buy_zone, "compute_buy_zones", fake_zones)
    g = pbs.compute_buy_avoid(conn=conn)["green"][0]
    assert g["flags"] == [] and g["falling_knife"] is False and g["target_stale"] is False


def test_compute_buy_avoid_survives_db_failure(monkeypatch):
    # _gather_universe 查询抛错时，仍只剩瓶颈宇宙，不崩
    class BrokenConn:
        def execute(self, sql):
            raise RuntimeError("locked")

    monkeypatch.setattr(pbs.buy_zone, "compute_buy_zones", lambda s, c, today=None: {})
    out = pbs.compute_buy_avoid(conn=BrokenConn())
    assert out["green"] == [] and out["red"] == []
    assert out["universe_size"] >= 1  # 瓶颈宇宙兜底


def test_daily_briefing_card_structure():
    from stock_research.jobs import premarket_gate as job

    res = SimpleNamespace(color="NONE", headline_plain="🟢 今晚环境正常",
                          can_buy="可正常研究/按计划操作。")
    buy_signals = {
        "green": [{"symbol": "MU", "sources": ["自选", "瓶颈"], "current": 180.0,
                   "low": 150.0, "high": 180.0, "method": "估值",
                   "line": "**MU**（自选/瓶颈） 现价 $180 · 区间 $150~$180 · 锚:目标价"}],
        "red": [],
        "universe_size": 12, "zoned": 4,
    }
    card = job._build_daily_briefing_card(res, "开盘前最终", buy_signals)
    assert card["msg_type"] == "interactive"
    assert "今日盘前一句话" in card["card"]["header"]["title"]["content"]
    blob = str(card["card"]["elements"])
    assert "MU" in blob and "现在偏便宜" in blob


def test_market_phase_pre_post_none():
    from datetime import datetime
    from stock_research.jobs import premarket_gate as job
    # 2026-06-16 周二，夏令时，美股 21:30(北京) 开盘
    assert job._market_phase(datetime(2026, 6, 16, 21, 15)) == "pre"   # 开盘前15分
    assert job._market_phase(datetime(2026, 6, 16, 20, 45)) == "pre"   # 开盘前45分
    assert job._market_phase(datetime(2026, 6, 16, 21, 50)) == "post"  # 开盘后20分
    assert job._market_phase(datetime(2026, 6, 16, 22, 30)) == "post"  # 开盘后60分
    assert job._market_phase(datetime(2026, 6, 16, 18, 0)) is None     # 太早
    assert job._market_phase(datetime(2026, 6, 16, 23, 0)) is None     # 超过+60


def test_post_open_window_valid_and_labeled():
    from datetime import datetime
    from stock_research.jobs import premarket_gate as job
    ok, why = job._is_valid_window(datetime(2026, 6, 16, 22, 0))  # 开盘后30分
    assert ok and "盘后" in why
    assert job._scan_label(datetime(2026, 6, 16, 22, 0)) == "开盘后30分"
    assert job._scan_label(datetime(2026, 6, 16, 21, 50)) == "开盘后20分"
    assert job._scan_label(datetime(2026, 6, 16, 21, 15)) == "开盘前最终"


def test_weekend_never_valid():
    from datetime import datetime
    from stock_research.jobs import premarket_gate as job
    ok, why = job._is_valid_window(datetime(2026, 6, 20, 22, 0))  # 周六
    assert not ok and "周末" in why


def test_synthesize_combines_short_and_medium():
    from stock_research.jobs import premarket_gate as job
    assert "都平稳" in job._synthesize("NONE", "NONE")
    # 短期稳 + 中期 HIGH（当前实况：开盘平稳但 SPY 破位）
    s = job._synthesize("NONE", "HIGH")
    assert "中期偏防守" in s and "别激进加仓" in s
    # 短期差 + 中期稳
    assert "等开盘" in job._synthesize("HIGH", "NONE")
    # 双防守
    assert "以守为主" in job._synthesize("CRITICAL", "HIGH")


def test_daily_briefing_card_includes_defense():
    from stock_research.jobs import premarket_gate as job
    res = SimpleNamespace(color="NONE", headline_plain="🟢 今晚环境正常", can_buy="可正常研究。")
    defense = {"severity": "HIGH", "reason": "SPY 跌破 200 日均线（中期趋势转弱）"}
    card = job._build_daily_briefing_card(
        res, "开盘前最终",
        {"green": [], "red": [], "universe_size": 5, "zoned": 0}, defense)
    blob = str(card["card"]["elements"])
    assert "大盘中期趋势" in blob and "HIGH" in blob
    assert "综合" in blob and "200 日均线" in blob


def test_daily_briefing_card_empty_green():
    from stock_research.jobs import premarket_gate as job

    res = SimpleNamespace(color="LOW", headline_plain="🟡 今晚略偏谨慎",
                          can_buy="小仓试探，别追高。")
    card = job._build_daily_briefing_card(res, "开盘前最终",
                                          {"green": [], "red": [], "universe_size": 5, "zoned": 0})
    blob = str(card["card"]["elements"])
    assert "今日名单为空" in blob
