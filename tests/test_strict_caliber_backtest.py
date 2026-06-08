"""strict_caliber_backtest 纯函数单测（不依赖 DB）。

口径核心（_fetch_pool / build_payload）依赖真实 DB，已端到端验证
（best=⑥Top3+排过热+mom<80，momentum<80 边际 +0.44pt 一致）。
此处覆盖评价纪律与口径过滤的边界。
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_spec = importlib.util.spec_from_file_location(
    "strict_caliber_backtest", REPO / "scripts" / "tools" / "strict_caliber_backtest.py")
scb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scb)


class StrictCaliberBacktestTest(unittest.TestCase):
    def test_passes_rank_overheated_momentum(self):
        row = {"market_rank": 1, "momentum": 55, "risk_codes": []}
        self.assertTrue(scb._passes(row, max_rank=5, drop_overheated=True, momentum_lt=80.0))
        # rank 超出
        self.assertFalse(scb._passes({**row, "market_rank": 6}, max_rank=5, drop_overheated=False, momentum_lt=None))
        # Top3 卡掉 rank 4
        self.assertFalse(scb._passes({**row, "market_rank": 4}, max_rank=3, drop_overheated=False, momentum_lt=None))
        # 过热被排除
        self.assertFalse(scb._passes({**row, "risk_codes": ["OVERHEATED_1Y"]}, max_rank=5, drop_overheated=True, momentum_lt=None))
        # momentum>=80 卡掉；momentum 缺失也卡掉
        self.assertFalse(scb._passes({**row, "momentum": 80}, max_rank=5, drop_overheated=False, momentum_lt=80.0))
        self.assertFalse(scb._passes({**row, "momentum": None}, max_rank=5, drop_overheated=False, momentum_lt=80.0))

    def test_evaluate_median_and_leave_one_out(self):
        sub = [
            {"alpha_pct": -1.0, "is_success": False},
            {"alpha_pct": 0.2, "is_success": True},
            {"alpha_pct": 0.3, "is_success": True},
            {"alpha_pct": 10.0, "is_success": True},  # 极端赢家
        ]
        e = scb.evaluate(sub)
        self.assertEqual(e["n"], 4)
        self.assertEqual(e["max_loss_pct"], -1.0)
        # median of sorted [-1,0.2,0.3,10] = (0.2+0.3)/2 = 0.25
        self.assertAlmostEqual(e["median_alpha_pct"], 0.25, places=3)
        # 去掉最佳(10) → [-1,0.2,0.3] median = 0.2
        self.assertAlmostEqual(e["median_drop_best"], 0.2, places=3)

    def test_evaluate_too_few(self):
        self.assertIsNone(scb.evaluate([{"alpha_pct": 1.0, "is_success": True}]))

    def test_is_candidate_requires_leave_one_out(self):
        # median>0 胜率>50 留一>=0 → 候选
        self.assertTrue(scb._is_candidate({"median_alpha_pct": 0.16, "win_rate_pct": 53, "median_drop_best": 0.16}))
        # 留一转负（靠极端赢家撑）→ 不候选，即便 median 正
        self.assertFalse(scb._is_candidate({"median_alpha_pct": 0.16, "win_rate_pct": 51, "median_drop_best": -0.05}))
        # 胜率不过 → 不候选
        self.assertFalse(scb._is_candidate({"median_alpha_pct": 0.5, "win_rate_pct": 50, "median_drop_best": 0.5}))
        # median 负 → 不候选
        self.assertFalse(scb._is_candidate({"median_alpha_pct": -0.1, "win_rate_pct": 60, "median_drop_best": -0.1}))

    def test_upgrade_ready_gates(self):
        base = -8.63  # 原口径①最大亏损
        ok = {"n": 20, "median_alpha_pct": 0.16, "win_rate_pct": 53, "median_drop_best": 0.16, "max_loss_pct": -2.92}
        self.assertTrue(scb._upgrade_ready(ok, base))
        self.assertFalse(scb._upgrade_ready({**ok, "n": 19}, base))            # 样本不足20
        self.assertFalse(scb._upgrade_ready({**ok, "median_alpha_pct": -0.1}, base))  # median 负
        self.assertFalse(scb._upgrade_ready({**ok, "median_drop_best": -0.05}, base))  # 靠极端赢家
        self.assertFalse(scb._upgrade_ready({**ok, "max_loss_pct": -9.0}, base))  # 亏损比原口径更深
        self.assertFalse(scb._upgrade_ready(None, base))


if __name__ == "__main__":
    unittest.main(verbosity=2)
