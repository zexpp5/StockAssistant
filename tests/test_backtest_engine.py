"""统一回测引擎单测 — 纯合成数据，不碰 DB。"""
from __future__ import annotations

import unittest

from stock_research.core.backtest_engine import (
    DEFAULT_COST_MODELS,
    ZERO_COST,
    CostModel,
    score_row,
    simulate,
    us_eligibility_filter,
)


def make_frames_closes():
    """3 个快照日、4 只票的合成宇宙。

    A: 因子最高、天天涨 10%；B: 次高、天天跌 5%；
    C: 第 3 天因子分反超 B（触发调仓换手）；D: 分最低不入选。
    """
    dates = ["2026-07-01", "2026-07-02", "2026-07-03"]
    def row(sym, score):
        return {"symbol": sym, "reversal": score, "f_score": score, "eligibility": "buyable"}
    frames = {
        dates[0]: [row("A", 90), row("B", 80), row("C", 40), row("D", 10)],
        dates[1]: [row("A", 90), row("B", 80), row("C", 40), row("D", 10)],
        dates[2]: [row("A", 90), row("B", 40), row("C", 80), row("D", 10)],
    }
    closes = {
        "A": {dates[0]: 100.0, dates[1]: 110.0, dates[2]: 121.0},
        "B": {dates[0]: 100.0, dates[1]: 95.0, dates[2]: 90.25},
        "C": {dates[0]: 50.0, dates[1]: 50.0, dates[2]: 50.0},
        "D": {dates[0]: 10.0, dates[1]: 10.0, dates[2]: 10.0},
    }
    bench = {"IDX": {dates[0]: 1000.0, dates[1]: 1010.0, dates[2]: 1020.1}}  # 每天 +1%
    return frames, closes, bench


WEIGHTS = {"reversal": 0.6, "f_score": 0.4}


class ScoreRowTest(unittest.TestCase):
    def test_weighted_score(self):
        self.assertAlmostEqual(score_row({"reversal": 100, "f_score": 50}, WEIGHTS), 80.0)

    def test_missing_and_nan_factor_neutral_50(self):
        self.assertAlmostEqual(score_row({}, WEIGHTS), 50.0)
        self.assertAlmostEqual(score_row({"reversal": float("nan"), "f_score": None}, WEIGHTS), 50.0)


class SimulateTest(unittest.TestCase):
    def test_gross_return_top2_daily(self):
        frames, closes, bench = make_frames_closes()
        r = simulate(market="US", frames=frames, closes=closes, benchmark_closes=bench,
                     weights=WEIGHTS, top_n=2, hold_days=1, cost_model=ZERO_COST)
        # 日1-2: (A +10% + B -5%)/2 = +2.5%; 日2-3 调仓后持 A,B(B 仍在 top2 因为第2天快照):
        # 第3天快照 C 反超 B,但收益吃的是 2→3 段,持仓在第2天快照定 = A,B → (+10% -5%)/2=+2.5%
        self.assertEqual(r.n_days, 2)
        self.assertAlmostEqual(r.gross_total_pct, ((1.025 * 1.025) - 1) * 100, places=3)
        # 基准每天+1% → 累计 2.01%
        self.assertAlmostEqual(r.benchmark_total_pct, 2.01, places=2)
        self.assertAlmostEqual(r.gross_alpha_pct, r.gross_total_pct - r.benchmark_total_pct, places=4)

    def test_net_below_gross_with_costs(self):
        frames, closes, bench = make_frames_closes()
        cm = CostModel(buy_pct=0.5, sell_pct=0.5, label="test")
        common = dict(market="US", frames=frames, closes=closes, benchmark_closes=bench,
                      weights=WEIGHTS, top_n=2, hold_days=1)
        gross = simulate(cost_model=ZERO_COST, **common)
        net = simulate(cost_model=cm, **common)
        self.assertLess(net.net_total_pct, gross.gross_total_pct)
        # 建仓买入 0.5% + 期末清仓 0.5%（本例中途无换手）→ 成本拖累 ≈1% 左右
        self.assertGreater(net.cost_drag_pct, 0.9)
        self.assertEqual(net.total_trades, 4)  # 建仓2买 + 清仓2卖

    def test_turnover_counted_on_rebalance(self):
        frames, closes, bench = make_frames_closes()
        # 加第4天,让第3天快照(C反超B)生效一次调仓
        frames["2026-07-04"] = frames["2026-07-03"]
        for s, series in closes.items():
            series["2026-07-04"] = series["2026-07-03"]
        bench["IDX"]["2026-07-04"] = bench["IDX"]["2026-07-03"]
        r = simulate(market="US", frames=frames, closes=closes, benchmark_closes=bench,
                     weights=WEIGHTS, top_n=2, hold_days=1, cost_model=ZERO_COST)
        # 第3天调仓: B 换 C → 换手 50%
        self.assertAlmostEqual(r.avg_turnover_pct, 50.0 / 2, places=1)  # 3次调仓平均(0+0+50)/2? 见下
        # 实际: 调仓在 i=0(建仓,不计换手),i=1(无变化,0%),i=2(B→C,50%) → 平均 25%
        self.assertEqual(r.total_trades, 2 + 0 + 2 + 2)  # 建仓2 + i1换0 + i2换1卖1买 + 清仓2

    def test_hold_days_reduces_rebalances(self):
        frames, closes, bench = make_frames_closes()
        r1 = simulate(market="US", frames=frames, closes=closes, benchmark_closes=bench,
                      weights=WEIGHTS, top_n=2, hold_days=1, cost_model=ZERO_COST)
        r5 = simulate(market="US", frames=frames, closes=closes, benchmark_closes=bench,
                      weights=WEIGHTS, top_n=2, hold_days=5, cost_model=ZERO_COST)
        self.assertGreater(r1.n_rebalances, r5.n_rebalances)
        self.assertEqual(r5.n_rebalances, 1)  # 只建仓一次

    def test_nan_close_skipped_not_crash(self):
        frames, closes, bench = make_frames_closes()
        closes["A"]["2026-07-02"] = float("nan")
        r = simulate(market="US", frames=frames, closes=closes, benchmark_closes=bench,
                     weights=WEIGHTS, top_n=2, hold_days=1, cost_model=ZERO_COST)
        self.assertTrue(any("缺价" in n for n in r.notes))

    def test_eligibility_filter_excludes_watch_only(self):
        frames, closes, bench = make_frames_closes()
        for rows in frames.values():
            for r in rows:
                if r["symbol"] == "A":
                    r["eligibility"] = "watch_only"  # 最高分票被资格闸拦掉
        r = simulate(market="US", frames=frames, closes=closes, benchmark_closes=bench,
                     weights=WEIGHTS, top_n=2, hold_days=1, cost_model=ZERO_COST,
                     eligibility_filter=us_eligibility_filter)
        # A 被排除 → 持 B,C → 收益应为负(B 天天跌,C 平)
        self.assertLess(r.gross_total_pct, 0)

    def test_insufficient_data(self):
        r = simulate(market="US", frames={"2026-07-01": []}, closes={}, benchmark_closes={},
                     weights=WEIGHTS, top_n=2, hold_days=1, cost_model=ZERO_COST)
        self.assertEqual(r.n_days, 0)
        self.assertTrue(any("不足" in n for n in r.notes))


class RegimeGateTest(unittest.TestCase):
    """防御闸(2026-07-27): 基准跌破N日均线→空仓休息。"""

    def test_gate_off_goes_to_cash(self):
        frames, closes, bench = make_frames_closes()
        # 基准序列: 3日均线之下(连跌) → 调仓日闸关 → 空仓
        regime = {"2026-06-28": 110.0, "2026-06-29": 105.0, "2026-06-30": 100.0,
                  "2026-07-01": 90.0, "2026-07-02": 85.0, "2026-07-03": 80.0}
        r = simulate(market="US", frames=frames, closes=closes, benchmark_closes=bench,
                     weights=WEIGHTS, top_n=2, hold_days=1, cost_model=ZERO_COST,
                     regime_ma=3, regime_series=regime)
        # 全程闸关 → никогда建仓 → 毛收益 0
        self.assertEqual(r.gross_total_pct, 0.0)
        self.assertEqual(r.total_trades, 0)

    def test_gate_on_when_above_ma(self):
        frames, closes, bench = make_frames_closes()
        # 基准在均线上方(上涨) → 闸开,正常持仓
        regime = {"2026-06-28": 80.0, "2026-06-29": 85.0, "2026-06-30": 90.0,
                  "2026-07-01": 100.0, "2026-07-02": 105.0, "2026-07-03": 110.0}
        r = simulate(market="US", frames=frames, closes=closes, benchmark_closes=bench,
                     weights=WEIGHTS, top_n=2, hold_days=1, cost_model=ZERO_COST,
                     regime_ma=3, regime_series=regime)
        self.assertGreater(r.gross_total_pct, 0)   # 正常吃到 A 的上涨

    def test_insufficient_history_does_not_block(self):
        frames, closes, bench = make_frames_closes()
        regime = {"2026-07-01": 50.0}  # 只有1天,不足N=3 → 不拦(宁可漏防不误伤)
        r = simulate(market="US", frames=frames, closes=closes, benchmark_closes=bench,
                     weights=WEIGHTS, top_n=2, hold_days=1, cost_model=ZERO_COST,
                     regime_ma=3, regime_series=regime)
        self.assertGreater(r.total_trades, 0)

    def test_exit_pays_sell_side_only(self):
        frames, closes, bench = make_frames_closes()
        # 第1天闸开建仓,第2/3天闸关清仓 → 成本=建仓买入+清仓卖出各一次
        regime = {"2026-06-28": 100.0, "2026-06-29": 100.0, "2026-06-30": 100.0,
                  "2026-07-01": 101.0, "2026-07-02": 80.0, "2026-07-03": 70.0}
        cm = CostModel(buy_pct=0.5, sell_pct=0.5, label="t")
        r = simulate(market="US", frames=frames, closes=closes, benchmark_closes=bench,
                     weights=WEIGHTS, top_n=2, hold_days=1, cost_model=cm,
                     regime_ma=3, regime_series=regime)
        # 建仓2买 + 第2天清仓2卖 = 4笔;期末无持仓无清仓成本
        self.assertEqual(r.total_trades, 4)


class CostModelTest(unittest.TestCase):
    def test_default_models_sane(self):
        # 成本排序符合常识: 港股 > A股 > 美股
        self.assertGreater(DEFAULT_COST_MODELS["HK"].round_trip_pct,
                           DEFAULT_COST_MODELS["CN"].round_trip_pct)
        self.assertGreater(DEFAULT_COST_MODELS["CN"].round_trip_pct,
                           DEFAULT_COST_MODELS["US"].round_trip_pct)
        # A股印花税只在卖出侧
        cn = DEFAULT_COST_MODELS["CN"]
        self.assertGreater(cn.sell_pct, cn.buy_pct)


if __name__ == "__main__":
    unittest.main()
