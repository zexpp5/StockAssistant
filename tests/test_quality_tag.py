"""quality_tag 公共模块单测 — 锁定追涨/早期/中性三档逻辑 + 市场分阈值。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from stock_research.core.quality_tag import (  # type: ignore
    QualityTag,
    SPIKE_THRESHOLD_BY_MARKET,
    classify,
    classify_from_history,
    compute_pct60_and_max_daily,
    detect_market,
)


class TestDetectMarket(unittest.TestCase):
    def test_a_share_suffixes(self):
        self.assertEqual(detect_market("000725.SZ"), "A")
        self.assertEqual(detect_market("605117.SS"), "A")
        self.assertEqual(detect_market("430047.BJ"), "A")

    def test_hk(self):
        self.assertEqual(detect_market("9988.HK"), "HK")
        self.assertEqual(detect_market("0700.HK"), "HK")

    def test_us_default(self):
        self.assertEqual(detect_market("NVDA"), "US")
        self.assertEqual(detect_market("BRK-B"), "US")
        self.assertEqual(detect_market(""), "US")  # 空字符串兜底

    def test_case_insensitive(self):
        self.assertEqual(detect_market("000725.sz"), "A")


class TestComputePct60(unittest.TestCase):
    def test_simple_growth(self):
        closes = [100.0] + [110.0] * 58 + [120.0]  # 60 个点，首 100 尾 120
        pct60, _ = compute_pct60_and_max_daily(closes)
        self.assertAlmostEqual(pct60, 20.0, places=2)

    def test_handles_none_values(self):
        # None 会被跳过；100 → 110 = +10%
        closes = [None, 100.0, None, 110.0]
        pct60, _ = compute_pct60_and_max_daily(closes)
        self.assertAlmostEqual(pct60, 10.0, places=2)

    def test_empty_returns_none(self):
        pct60, max_daily = compute_pct60_and_max_daily([])
        self.assertIsNone(pct60)
        self.assertIsNone(max_daily)

    def test_max_daily_spike(self):
        # 制造一根 +12% 单日（在最近 30d 窗口内）
        closes = [100.0] * 50 + [100.0, 112.0] + [112.0] * 8
        _, max_daily = compute_pct60_and_max_daily(closes)
        self.assertAlmostEqual(max_daily, 12.0, places=1)


class TestClassifyScoreThreshold(unittest.TestCase):
    def test_normalized_below_threshold_returns_none(self):
        # 综合分 0.70 < 0.80 → None
        self.assertIsNone(
            classify("NVDA", score=0.70, pct60=20.0, max_daily=10.0, mode="normalized")
        )

    def test_score_0_100_below_threshold_returns_none(self):
        # 综合分 70 < 80 → None
        self.assertIsNone(
            classify("NVDA", score=70.0, pct60=20.0, max_daily=10.0, mode="score_0_100")
        )

    def test_z_score_no_threshold(self):
        # 美股 z-score 模式不卡分数，picks 进表即判
        tag = classify("MU", score=-1.44, pct60=82.0, max_daily=8.0, mode="z_score")
        self.assertIsNotNone(tag)
        self.assertEqual(tag.kind, "chase")


class TestClassifyChase(unittest.TestCase):
    def test_chase_60d_only(self):
        # 京东方 case
        tag = classify("000725.SZ", score=0.93, pct60=20.6, max_daily=10.1,
                       mode="normalized")
        self.assertIsNotNone(tag)
        self.assertEqual(tag.kind, "chase")
        self.assertIn("+20.6%", tag.detail)
        self.assertIn("+10.1%", tag.detail)

    def test_chase_spike_only(self):
        # 60d 平台但 30d 内有涨停（扬杰科技 case）
        tag = classify("300373.SZ", score=0.89, pct60=-0.6, max_daily=11.3,
                       mode="normalized")
        self.assertIsNotNone(tag)
        self.assertEqual(tag.kind, "chase")
        self.assertIn("+11.3%", tag.detail)
        # 60d -0.6% 不触发追涨，detail 不应该有 "60d 已涨"
        self.assertNotIn("60d 已涨", tag.detail)


class TestClassifyEarly(unittest.TestCase):
    def test_early_signal(self):
        # 中国船舶 case
        tag = classify("600150.SS", score=0.90, pct60=2.9, max_daily=5.0,
                       mode="normalized")
        self.assertIsNotNone(tag)
        self.assertEqual(tag.kind, "early")

    def test_early_with_no_max_daily(self):
        tag = classify("600150.SS", score=0.90, pct60=-3.0, max_daily=None,
                       mode="normalized")
        self.assertIsNotNone(tag)
        self.assertEqual(tag.kind, "early")


class TestClassifyNeutral(unittest.TestCase):
    def test_60d_between_5_and_15_is_neutral(self):
        # 5% < 60d < 15%, 既不追涨也不算早期 → None
        self.assertIsNone(
            classify("000725.SZ", score=0.90, pct60=10.0, max_daily=5.0,
                     mode="normalized")
        )

    def test_no_60d_data_is_neutral(self):
        self.assertIsNone(
            classify("NVDA", score=0.90, pct60=None, max_daily=None,
                     mode="normalized")
        )


class TestMarketThresholds(unittest.TestCase):
    """涨停阈值按市场区分 — 这是 2026-05-26 抽公共模块时新加的。"""

    def test_us_spike_threshold_7pct(self):
        # 美股 7% 单日 = spike 档
        tag = classify("NVDA", score=80.0, pct60=2.0, max_daily=7.5,
                       mode="score_0_100")
        self.assertIsNotNone(tag)
        self.assertEqual(tag.kind, "chase")

    def test_a_share_spike_threshold_9_5pct(self):
        # 同样 7.5% 单日，A 股不算 spike（A 股阈值 9.5）→ 仍是早期信号
        tag = classify("000725.SZ", score=0.90, pct60=2.0, max_daily=7.5,
                       mode="normalized")
        self.assertIsNotNone(tag)
        self.assertEqual(tag.kind, "early")

    def test_hk_spike_threshold_8pct(self):
        # 港股 8% 单日 = spike 档
        tag = classify("9988.HK", score=0.90, pct60=2.0, max_daily=8.5,
                       mode="normalized")
        self.assertIsNotNone(tag)
        self.assertEqual(tag.kind, "chase")

    def test_thresholds_constant_sanity(self):
        # A 严格于 HK 严格于 US（A 股涨停 9.8，港美无涨停）
        self.assertGreater(SPIKE_THRESHOLD_BY_MARKET["A"], SPIKE_THRESHOLD_BY_MARKET["HK"])
        self.assertGreater(SPIKE_THRESHOLD_BY_MARKET["HK"], SPIKE_THRESHOLD_BY_MARKET["US"])


class TestQualityTagRender(unittest.TestCase):
    def test_chase_markdown(self):
        tag = QualityTag(kind="chase", label="⚠️ 追涨进表", detail="60d 已涨 +20.6%")
        self.assertEqual(
            tag.as_markdown_line(),
            "  ⚠️ 追涨进表（60d 已涨 +20.6%，不是早期信号）"
        )

    def test_early_markdown(self):
        tag = QualityTag(kind="early", label="✅ 早期信号", detail="60d 在 ±5% 平台")
        self.assertEqual(
            tag.as_markdown_line(),
            "  ✅ 早期信号（60d 在 ±5% 平台 + 高分）"
        )

    def test_as_dict_keys(self):
        tag = QualityTag(kind="early", label="✅ 早期信号", detail="60d 在 ±5% 平台")
        d = tag.as_dict()
        self.assertEqual(set(d.keys()), {"kind", "label", "detail"})


class TestClassifyFromHistory(unittest.TestCase):
    def test_lookup_missing_ticker_returns_none(self):
        self.assertIsNone(
            classify_from_history("UNKNOWN", 0.90, {}, mode="normalized")
        )

    def test_lookup_with_real_close_list(self):
        # 模拟历史：60 个点，首 100 尾 120 → +20%（应触发追涨）
        history = {"FAKE.SS": {"close": [100.0] + [110.0] * 58 + [120.0]}}
        tag = classify_from_history("FAKE.SS", 0.90, history, mode="normalized")
        self.assertIsNotNone(tag)
        self.assertEqual(tag.kind, "chase")


class TestAsOfDate(unittest.TestCase):
    """PIT 回溯：as_of_date 让历史 row 用"当时"的 60d 窗口，避免误标。"""

    def _build_history(self):
        # 120 个交易日：前 60 个 100 → 100（平台期），后 60 个 100 → 200（翻倍涨）
        # 日期从 2026-01-01 起，每天 +1
        from datetime import date, timedelta
        base = date(2026, 1, 1)
        ts = [(base + timedelta(days=i)).isoformat() for i in range(120)]
        closes = [100.0] * 60 + [100.0 + i * (100.0 / 60) for i in range(60)]
        return {"FAKE.SS": {"close": closes, "ts": ts}}

    def test_as_of_old_date_uses_old_window(self):
        # 60d 平台期窗口（2026-01-01 ~ 2026-03-01）→ 早期信号
        history = self._build_history()
        tag = classify_from_history("FAKE.SS", 0.90, history,
                                    mode="normalized",
                                    as_of_date="2026-03-01")  # 第 60 天
        self.assertIsNotNone(tag)
        self.assertEqual(tag.kind, "early")

    def test_as_of_recent_date_uses_recent_window(self):
        # 不给 as_of → 用末尾窗口（2026-03-02 ~ 2026-04-30 翻倍涨）→ 追涨
        history = self._build_history()
        tag = classify_from_history("FAKE.SS", 0.90, history, mode="normalized")
        self.assertIsNotNone(tag)
        self.assertEqual(tag.kind, "chase")

    def test_as_of_missing_ts_falls_back_to_tail(self):
        # info 没有 ts 字段，as_of_date 被忽略，走默认尾部窗口
        history = {"FAKE.SS": {"close": [100.0] + [110.0] * 58 + [120.0]}}
        tag = classify_from_history("FAKE.SS", 0.90, history,
                                    mode="normalized",
                                    as_of_date="2024-01-01")
        self.assertIsNotNone(tag)
        self.assertEqual(tag.kind, "chase")  # 仍按末尾算


if __name__ == "__main__":
    unittest.main()
