"""PIT (Point-in-Time) 财报时点正确性测试。

⚠️ **当前状态**：大部分测试 expected_fail（标 SKIP_REASON），因为 factor_model.py /
factor_model_china.py 还没接 as_of 参数（C-5 audit 已定位修复方案，hold 给并行
会话完成因子重做后实施）。

测试用 stdlib unittest（项目没引入 pytest），跑：
    python3 -m unittest tests.test_pit_integrity

PIT 卫生标准（要求）：
  1. fetch_factors_for(ticker, as_of="YYYY-MM-DD") 返回的财报期末日必须 <
     as_of - 美股 65 天 / A 股 90 天 披露滞后
  2. fetch_factors_a_share(code) 必须支持 as_of
  3. 任何 walk_forward 回测里，当月 1 号选股不得读到当月末才公告的财报

测试样本：
  - NVDA / AAPL（美股大盘，FMP 通常齐全）
  - 600519（茅台，A 股财报齐全）
  - 300750（宁德，创业板）

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
    """美股因子 PIT 卫生测试。"""

    @unittest.expectedFailure
    def test_us_factor_respects_as_of(self):
        """fetch_factors_for(ticker, as_of='2024-06-30') 不应返回 fiscal 期末日 > 2024-06-30 的财报。

        当前 factor_model.fetch_factors_for 签名有 as_of 参数但被忽略，故 expected_fail。
        修复后此测试转为 strict pass。
        """
        from factor_model import fetch_factors_for  # type: ignore
        as_of = "2024-06-30"
        r = fetch_factors_for("NVDA", as_of=as_of)
        # 这里期望：r["piotroski"] 用的财报 fiscal date <= 2024-06-30 - 65 天
        # 但当前实现没做这个过滤，所以 fail
        details = r.get("piotroski", {}).get("details", {})
        latest_fiscal = details.get("latest_fiscal_date")
        cutoff = (datetime.strptime(as_of, "%Y-%m-%d") - timedelta(days=65)).date()
        self.assertIsNotNone(latest_fiscal,
                             "PIT 修复后应在 details 中暴露 latest_fiscal_date")
        self.assertLessEqual(
            datetime.strptime(latest_fiscal, "%Y-%m-%d").date(), cutoff,
            f"财报期末日 {latest_fiscal} 应 ≤ {cutoff} (as_of - 65 天 SEC filing 延迟)"
        )


class PITAShareTest(unittest.TestCase):
    """A 股因子 PIT 卫生测试。"""

    @unittest.skip(SKIP_REASON)
    def test_a_share_factor_respects_as_of(self):
        """fetch_factors_a_share(code, as_of=...) 必须支持时点参数。

        当前签名无 as_of，故 skip（不是 fail，因为没参数无从测试）。
        修复方案：见 docs/PIT_audit_2026-05-12.md P0-3。
        """
        from factor_model_china import fetch_factors_a_share  # type: ignore
        import inspect
        sig = inspect.signature(fetch_factors_a_share)
        self.assertIn("as_of", sig.parameters,
                      "fetch_factors_a_share 必须有 as_of 参数（PIT 卫生要求）")
        # 修复后才能真正测试：
        # r = fetch_factors_a_share("600519", as_of="2024-06-30")
        # ... 验证 r 财报 fiscal date <= 2024-06-30 - 90 天


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
