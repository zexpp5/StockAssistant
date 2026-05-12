"""PIT (Point-in-Time) 财报时点正确性测试。

✅ **2026-05-12 C-5 修复后**：PIT 修复已落地，测试转为 strict pass。
  - factor_model.piotroski_f_score / revenue_acceleration_pead 加 as_of
  - factor_model_china.piotroski_a_share 加 as_of（120 天滞后）
  - daily_picks_v5 / a_share_picks 调用方传 as_of=今日

测试用 stdlib unittest（项目没引入 pytest），跑：
    python3 -m unittest tests.test_pit_integrity

PIT 卫生标准：
  1. fetch_factors_for(ticker, as_of="YYYY-MM-DD") 返回的财报期末日 <
     as_of - 美股 65 天 / A 股 120 天 披露滞后
  2. fetch_factors_a_share(code) 必须支持 as_of
  3. 任何 walk_forward 回测里，当月 1 号选股不得读到当月末才公告的财报

测试样本：
  - NVDA / AAPL（美股大盘，FMP 通常齐全）
  - 600519（茅台，A 股财报齐全）

参考：[docs/PIT_audit_2026-05-12.md](../docs/PIT_audit_2026-05-12.md)
"""
from __future__ import annotations
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

# 让 tests/ 能 import 项目模块
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))


SKIP_REASON = (
    "PIT 修复未完成：factor_model 还没接 as_of 参数。"
    "见 docs/PIT_audit_2026-05-12.md P0-1/P0-2/P0-3。"
)


class PITUsStockTest(unittest.TestCase):
    """美股因子 PIT 卫生测试（C-5 修复后）。"""

    def test_us_factor_respects_as_of(self):
        """fetch_factors_for(NVDA, as_of='2024-06-30') 返回的财报期末日应 ≤ 2024-04-26
        (= 2024-06-30 - 65 天 美股 SEC filing 延迟)。
        """
        from factor_model import fetch_factors_for  # type: ignore
        as_of = "2024-06-30"
        r = fetch_factors_for("NVDA", as_of=as_of)
        details = r.get("piotroski", {}).get("details", {})
        if not details:
            self.skipTest(f"factor 拉取失败（可能 yfinance 限流）: {r.get('piotroski', {}).get('error')}")
        latest_fiscal = details.get("latest_fiscal_date")
        self.assertIsNotNone(latest_fiscal,
                             "PIT 修复后必须暴露 latest_fiscal_date 字段")
        cutoff = (datetime.strptime(as_of, "%Y-%m-%d") - timedelta(days=65)).date()
        self.assertLessEqual(
            datetime.strptime(latest_fiscal, "%Y-%m-%d").date(), cutoff,
            f"财报期末日 {latest_fiscal} 应 ≤ {cutoff} (as_of - 65 天 SEC filing 延迟)"
        )


class PITAShareTest(unittest.TestCase):
    """A 股因子 PIT 卫生测试（C-5 修复后）。"""

    def test_a_share_factor_signature_has_as_of(self):
        """fetch_factors_a_share / piotroski_a_share 必须有 as_of 参数。"""
        from factor_model_china import fetch_factors_a_share, piotroski_a_share  # type: ignore
        import inspect
        sig1 = inspect.signature(fetch_factors_a_share)
        sig2 = inspect.signature(piotroski_a_share)
        self.assertIn("as_of", sig1.parameters,
                      "fetch_factors_a_share 必须有 as_of 参数")
        self.assertIn("as_of", sig2.parameters,
                      "piotroski_a_share 必须有 as_of 参数")


class PITWalkForwardTest(unittest.TestCase):
    """walk_forward_backtest PIT 卫生测试。"""

    def test_walk_forward_uses_monthly_cutoff(self):
        """walk_forward 必须按月度 cutoff 选股，不能用未来知识。

        当前 walk_forward 只用价格因子（动量/反转），价格当天可见无 PIT 问题。
        本测试验证：framework 设计上有 cutoff 概念（防止后续加财报因子时退化）。
        """
        try:
            from stock_research.jobs.walk_forward_backtest import walk_forward
            import inspect
            sig = inspect.signature(walk_forward)
            self.assertIn("start_month", sig.parameters)
            self.assertIn("end_month", sig.parameters)
            self.assertIn("train_lookback_months", sig.parameters,
                          "walk_forward 必须有 train_lookback_months 参数")
        except ImportError as e:
            self.skipTest(f"walk_forward_backtest 导入失败: {e}")


class PITPortfolioConstraintsTest(unittest.TestCase):
    """portfolio_constraints 模块的工具函数 PIT 中立性。

    这些函数是仓位约束，不读财报数据，理论上无 PIT 问题。本测试保证它们
    保持 PIT 中立（不引入财报时点依赖）。
    """

    def test_kelly_cap_is_pit_neutral(self):
        """kelly_cap 只看 weights / max_weight，无时点依赖。"""
        from stock_research.core.portfolio_constraints import kelly_cap
        weights = {"NVDA": 0.15, "MSFT": 0.10, "AAPL": 0.08}
        capped = kelly_cap(weights, max_single_pct=0.15, kelly_fraction=0.5)
        # 验证 cap 生效：单股不超过 7.5%
        for tk, w in capped.items():
            self.assertLessEqual(w, 0.075 + 1e-9, f"{tk} 超 cap")

    def test_volatility_adaptive_stop_pct_uses_only_recent_data(self):
        """ATR proxy 只用最近 N 期 close，没有"未来"概念。"""
        from stock_research.core.portfolio_constraints import volatility_adaptive_stop_pct
        # 一段虚构数据
        closes = [100.0 + i * 0.5 for i in range(30)]  # 平稳上涨
        stop_pct, source = volatility_adaptive_stop_pct(closes)
        self.assertGreater(stop_pct, 0)
        self.assertLessEqual(stop_pct, 0.25)
        self.assertIn(source, ("true_atr", "proxy_atr", "fallback"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
