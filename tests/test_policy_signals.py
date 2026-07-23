"""政策大事雷达打分函数单测 — 纯函数,不联网。

验收锚点（方案 2026-07-21 §4）：上周真实新闻回放,AI大会/规划印发/国常会必须命中,
统计报数/宣传专栏/外事礼仪必须被挡。
"""
from __future__ import annotations

import unittest

from stock_research.core.policy_signals import is_excluded, score_news, signal_line


class ScoreNewsTest(unittest.TestCase):
    def test_ai_conference_xi_speech_hits(self):
        """7-17 习AI大会主旨讲话 = 用户最关心那条,必须命中。"""
        v = score_news("习近平出席2026世界人工智能大会暨人工智能全球治理高级别会议开幕式并发表主旨讲话")
        self.assertTrue(v["is_signal"])
        self.assertIn("最高层", v["actors"])
        self.assertTrue(any("AI" in t for t in v["themes"]))

    def test_plan_issued_hits(self):
        """规划印发 = 真政策动作,命中。"""
        v = score_news("国务院印发《国民健康“十五五”规划》")
        self.assertTrue(v["is_signal"])
        self.assertIn("国务院", v["actors"])
        self.assertIn("规划", v["actions"])

    def test_state_team_buyback_hits(self):
        """国家队增持 = 真金白银,命中(即便来自市场新闻源)。"""
        v = score_news("中国国新已使用回购增持专项再贷款超500亿元增持央企股票")
        self.assertTrue(v["is_signal"])
        self.assertIn("国家队", v["actors"])
        self.assertIn("真金白银买入", v["actions"])

    def test_consumption_plan_approval_hits(self):
        v = score_news("国务院批复同意《扩大消费“十五五”规划》 促进服务消费")
        self.assertTrue(v["is_signal"])
        self.assertTrue(any("消费" in t for t in v["themes"]))

    def test_rate_cut_hits(self):
        v = score_news("中国人民银行宣布下调存款准备金率0.5个百分点")
        self.assertTrue(v["is_signal"])
        self.assertIn("央行", v["actors"])
        self.assertIn("货币宽松", v["actions"])

    # ── 噪音必须被挡 ──────────────────────────────────────────────
    def test_gdp_stat_excluded(self):
        """GDP报数不是政策动作。"""
        self.assertTrue(is_excluded("上半年GDP同比增长4.7% 中国经济持续向新向优"))
        self.assertFalse(score_news("上半年GDP同比增长4.7% 中国经济持续向新向优")["is_signal"])

    def test_propaganda_column_excluded(self):
        self.assertTrue(is_excluded("【新思想引领新征程】人工智能蓬勃兴起 锻造高质量发展新引擎"))

    def test_diplomacy_excluded(self):
        self.assertTrue(is_excluded("习近平会见联合国秘书长"))
        self.assertTrue(is_excluded("王沪宁将访问朝鲜"))

    def test_media_reaction_excluded(self):
        """媒体评价/央视快评 = 反应,不是动作。"""
        self.assertTrue(is_excluded("国际媒体积极评价中国推动人工智能发展与全球人工智能治理"))
        self.assertTrue(is_excluded("央视快评：携手构建公正合理的全球人工智能治理体系"))

    def test_routine_reverse_repo_excluded(self):
        """央行逆回购是每天例行操作,不是政策事件(否则天天误报)。"""
        self.assertTrue(is_excluded("央行今日开展2040亿元7天期逆回购操作"))
        self.assertFalse(score_news("央行今日开展2040亿元7天期逆回购操作")["is_signal"])
        # 但降准是真政策,不能被误挡
        self.assertFalse(is_excluded("中国人民银行决定下调存款准备金率0.5个百分点"))

    def test_no_actor_no_signal(self):
        """没有国家级主体 → 不是国家大动作。"""
        self.assertFalse(score_news("某公司发布新产品 计划扩大生产")["is_signal"])

    def test_actor_without_action_no_signal(self):
        """有主体但只是空泛表态,没动作/方向 → 不推。"""
        v = score_news("国务院有关负责人就当前形势答记者问")
        self.assertFalse(v["is_signal"])

    def test_signal_line_readable(self):
        v = score_news("国务院印发《国民健康“十五五”规划》")
        line = signal_line("国务院印发《国民健康“十五五”规划》", v)
        self.assertIn("国务院", line)
        self.assertIn("关注方向", line)


if __name__ == "__main__":
    unittest.main()
