"""junior_stock_watcher 打分 / 解禁压力 / PIT 日期过滤单测。

mock akshare 返回 fake DataFrame，避免依赖网络。
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from stock_research.jobs.junior_stock_watcher import (  # type: ignore
    _board_of_cn,
    _cn_junior_summary,
    _enrich_cn_industry,
    fetch_cn_junior_pool,
    fetch_cn_unlock_radar,
)


class BoardClassificationTest(unittest.TestCase):
    def test_star_board(self):
        self.assertEqual(_board_of_cn("688001"), "star")

    def test_chinext(self):
        self.assertEqual(_board_of_cn("300001"), "chinext")

    def test_shanghai_main(self):
        self.assertEqual(_board_of_cn("600000"), "main")
        self.assertEqual(_board_of_cn("601318"), "main")

    def test_shenzhen_main(self):
        self.assertEqual(_board_of_cn("000001"), "main")
        self.assertEqual(_board_of_cn("002001"), "main")

    def test_bse(self):
        self.assertEqual(_board_of_cn("832001"), "bse")

    def test_other_fallback(self):
        self.assertEqual(_board_of_cn("123456"), "other")
        self.assertEqual(_board_of_cn(""), "other")


class UnlockStressScoreTest(unittest.TestCase):
    """解禁压力分 = pct_float*80 + log10(max(mv_yi, 0.1))*5 + 10，封顶 100。"""

    def _fake_ak_with_unlock(self, rows):
        ak = MagicMock()
        ak.stock_restricted_release_detail_em.return_value = pd.DataFrame(rows)
        return ak

    def test_full_score_caps_at_100(self):
        # 100% 占流通 + 100 亿市值 → 80 + log10(100)*5 + 10 = 100，正好封顶
        rows = [{
            "股票代码": "600000",
            "股票简称": "测试股 A",
            "解禁时间": (date.today() + timedelta(days=30)).strftime("%Y-%m-%d"),
            "实际解禁市值": 100e8,
            "占解禁前流通市值比例": 100.0,
            "限售股类型": "首发原股东限售股份",
            "解禁前一交易日收盘价": 12.0,
        }]
        with patch(
            "stock_research.jobs.junior_stock_watcher._import_ak",
            return_value=self._fake_ak_with_unlock(rows),
        ):
            out = fetch_cn_unlock_radar(set(), set(), horizon_days=90)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["stress_score"], 100.0)
        self.assertEqual(out[0]["code"], "600000")
        self.assertEqual(out[0]["board"], "main")

    def test_mid_score(self):
        # 50% 占流通(0.5) + 10 亿市值 → 0.5*80 + log10(10)*5 + 10 = 40 + 5 + 10 = 55
        rows = [{
            "股票代码": "300001",
            "股票简称": "测试股 B",
            "解禁时间": (date.today() + timedelta(days=10)).strftime("%Y-%m-%d"),
            "实际解禁市值": 10e8,
            "占解禁前流通市值比例": 0.5,
            "限售股类型": "首发",
            "解禁前一交易日收盘价": 8.0,
        }]
        with patch(
            "stock_research.jobs.junior_stock_watcher._import_ak",
            return_value=self._fake_ak_with_unlock(rows),
        ):
            out = fetch_cn_unlock_radar(set(), set(), horizon_days=90)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["stress_score"], 55.0)
        self.assertEqual(out[0]["board"], "chinext")

    def test_pool_membership_flag(self):
        rows = [{
            "股票代码": "600000",
            "股票简称": "我的持仓",
            "解禁时间": (date.today() + timedelta(days=20)).strftime("%Y-%m-%d"),
            "实际解禁市值": 5e8,
            "占解禁前流通市值比例": 0.2,
            "限售股类型": "首发",
            "解禁前一交易日收盘价": 10.0,
        }]
        with patch(
            "stock_research.jobs.junior_stock_watcher._import_ak",
            return_value=self._fake_ak_with_unlock(rows),
        ):
            out = fetch_cn_unlock_radar({"600000"}, set(), horizon_days=90)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["in_holdings"])
        self.assertFalse(out[0]["in_watchlist"])


class UnlockPitFilterTest(unittest.TestCase):
    """PIT 日期过滤：解禁日早于今天 或 晚于 horizon 都应被剔除。"""

    def _patch_ak(self, rows):
        ak = MagicMock()
        ak.stock_restricted_release_detail_em.return_value = pd.DataFrame(rows)
        return patch("stock_research.jobs.junior_stock_watcher._import_ak", return_value=ak)

    def test_past_dates_filtered(self):
        rows = [{
            "股票代码": "600001",
            "股票简称": "已解禁股",
            "解禁时间": (date.today() - timedelta(days=5)).strftime("%Y-%m-%d"),
            "实际解禁市值": 10e8,
            "占解禁前流通市值比例": 0.3,
            "限售股类型": "首发",
            "解禁前一交易日收盘价": 10.0,
        }]
        with self._patch_ak(rows):
            out = fetch_cn_unlock_radar(set(), set(), horizon_days=90)
        self.assertEqual(out, [])

    def test_beyond_horizon_filtered(self):
        rows = [{
            "股票代码": "600002",
            "股票简称": "远期解禁",
            "解禁时间": (date.today() + timedelta(days=120)).strftime("%Y-%m-%d"),
            "实际解禁市值": 5e8,
            "占解禁前流通市值比例": 0.2,
            "限售股类型": "首发",
            "解禁前一交易日收盘价": 8.0,
        }]
        with self._patch_ak(rows):
            out = fetch_cn_unlock_radar(set(), set(), horizon_days=90)
        self.assertEqual(out, [])


class JuniorPoolScoreTest(unittest.TestCase):
    """次新股池四维打分：折发行 / 时间衰减 / 首日溢价 / 较首日跌幅。"""

    def _patch_ak(self, rows):
        ak = MagicMock()
        ak.stock_xgsr_ths.return_value = pd.DataFrame(rows)
        return patch("stock_research.jobs.junior_stock_watcher._import_ak", return_value=ak)

    def test_full_score_components(self):
        # 月数 15(12-18 内)→ s_time=25；
        # vs_issue=-20% → s_discount=10；
        # 首日涨 150% → s_first=20；
        # 现价 8 vs 首日收盘 30 → vs_first=-73.3% → s_vs_first=24.4(min(25, 73.3/3))
        list_date = (date.today() - timedelta(days=450)).strftime("%Y-%m-%d")
        rows = [{
            "股票代码": "688001",
            "股票简称": "测试科创",
            "上市日期": list_date,
            "发行价": 10.0,
            "最新价": 8.0,
            "首日收盘价": 30.0,
            "首日涨跌幅": 150.0,
            "是否破发": "是",
        }]
        with self._patch_ak(rows):
            out = fetch_cn_junior_pool(set(), set(), months_min=6, months_max=24)
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertEqual(row["code"], "688001")
        self.assertEqual(row["board"], "star")
        bd = row["score_breakdown"]
        self.assertEqual(bd["discount_to_issue"], 10.0)
        self.assertEqual(bd["time_decay"], 25.0)
        self.assertEqual(bd["first_day_premium"], 20.0)
        self.assertAlmostEqual(bd["vs_first_close"], 24.4, places=1)
        self.assertIn("已破发", row["tags"])
        self.assertIn("首发解禁窗口", row["tags"])
        self.assertIn("首日爆炒", row["tags"])
        self.assertIn("较首日腰斩", row["tags"])

    def test_above_issue_gets_no_discount(self):
        # 现价 > 发行价 → s_discount=0；现价高的不该被打折发行分
        list_date = (date.today() - timedelta(days=400)).strftime("%Y-%m-%d")
        rows = [{
            "股票代码": "688002",
            "股票简称": "强势次新",
            "上市日期": list_date,
            "发行价": 10.0,
            "最新价": 15.0,
            "首日收盘价": 12.0,
            "首日涨跌幅": 20.0,
            "是否破发": "否",
        }]
        with self._patch_ak(rows):
            out = fetch_cn_junior_pool(set(), set(), months_min=6, months_max=24)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["score_breakdown"]["discount_to_issue"], 0.0)
        self.assertNotIn("已破发", out[0]["tags"])


class JuniorPoolPitFilterTest(unittest.TestCase):
    """PIT 过滤：上市月数 <6 或 >24 都应剔除。"""

    def _patch_ak(self, rows):
        ak = MagicMock()
        ak.stock_xgsr_ths.return_value = pd.DataFrame(rows)
        return patch("stock_research.jobs.junior_stock_watcher._import_ak", return_value=ak)

    def test_too_young_filtered(self):
        list_date = (date.today() - timedelta(days=100)).strftime("%Y-%m-%d")  # ~3.3 月
        rows = [{
            "股票代码": "688003",
            "股票简称": "新次新",
            "上市日期": list_date,
            "发行价": 10.0,
            "最新价": 11.0,
            "首日收盘价": 15.0,
            "首日涨跌幅": 50.0,
            "是否破发": "否",
        }]
        with self._patch_ak(rows):
            out = fetch_cn_junior_pool(set(), set(), months_min=6, months_max=24)
        self.assertEqual(out, [])

    def test_too_old_filtered(self):
        list_date = (date.today() - timedelta(days=800)).strftime("%Y-%m-%d")  # ~26.3 月
        rows = [{
            "股票代码": "688004",
            "股票简称": "老次新",
            "上市日期": list_date,
            "发行价": 10.0,
            "最新价": 9.0,
            "首日收盘价": 12.0,
            "首日涨跌幅": 20.0,
            "是否破发": "否",
        }]
        with self._patch_ak(rows):
            out = fetch_cn_junior_pool(set(), set(), months_min=6, months_max=24)
        self.assertEqual(out, [])


class CnJuniorSummaryTest(unittest.TestCase):
    """人话总结的几个关键档位。"""

    def test_solar_window_broken(self):
        # 14 月 + 破发 26% + 较首日跌 59%
        s = _cn_junior_summary(14.2, -25.8, -58.6, None)
        self.assertIn("14 月", s)
        self.assertIn("首发解禁窗口", s)
        self.assertIn("已破发 26%", s)
        self.assertIn("过半", s)

    def test_post_lockup_strong(self):
        # 19 月 + 较发行 +225% + 较首日 -82%
        s = _cn_junior_summary(19.4, 225.4, -82.3, 100)
        self.assertIn("19 月", s)
        self.assertIn("度过解禁压力期", s)
        self.assertIn("主力强势", s)
        self.assertIn("接近底部", s)

    def test_early_stage(self):
        # 7 月 + 接近发行价 + 较首日小幅
        s = _cn_junior_summary(7.0, 5.0, -10.0, 50)
        self.assertIn("7 月", s)
        self.assertIn("刚解禁初期", s)
        # vs_first -10% 在 -20~0 之间应显示"小跌"
        self.assertIn("小跌", s)


class EnrichCnIndustryFailSoftTest(unittest.TestCase):
    """_enrich_cn_industry 必须 fail-soft：网络挂不该抛 + 缓存命中跳过调用。"""

    def test_network_failure_does_not_raise(self):
        items = [
            {"code": "603395", "name": "红四方", "industry": ""},
            {"code": "301501", "name": "恒鑫生活", "industry": ""},
        ]
        ak = MagicMock()
        ak.stock_individual_info_em.side_effect = ConnectionError("Remote disconnected")
        with patch(
            "stock_research.jobs.junior_stock_watcher._import_ak",
            return_value=ak,
        ), patch(
            "stock_research.jobs.junior_stock_watcher._load_cn_industry_cache",
            return_value={},
        ):
            # 不应该抛
            _enrich_cn_industry(items)
        # industry 字段保持空
        self.assertEqual(items[0]["industry"], "")
        self.assertEqual(items[1]["industry"], "")

    def test_cache_hit_skips_api_call(self):
        items = [{"code": "603395", "name": "红四方", "industry": ""}]
        ak = MagicMock()
        with patch(
            "stock_research.jobs.junior_stock_watcher._import_ak",
            return_value=ak,
        ), patch(
            "stock_research.jobs.junior_stock_watcher._load_cn_industry_cache",
            return_value={"603395": "化学制品"},
        ):
            _enrich_cn_industry(items)
        # 缓存命中：industry 已填
        self.assertEqual(items[0]["industry"], "化学制品")
        # ak.stock_individual_info_em 未被调用
        ak.stock_individual_info_em.assert_not_called()


if __name__ == "__main__":
    unittest.main()
