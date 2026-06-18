"""Monthly actions dashboard guardrails.

These tests intentionally inspect the generated dashboard source template. The
monthly actions page is a read-only front-end consolidation layer, so the most
important regression risks are product-boundary drift and unsafe wording.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DASHBOARD_BUILDER = REPO / "scripts" / "pipeline" / "build_stock_dashboard_html.py"


def _source() -> str:
    return DASHBOARD_BUILDER.read_text(encoding="utf-8")


def _monthly_renderer(src: str) -> str:
    m = re.search(
        r"function renderMonthlyActions\(\) \{(?P<body>.*?)\n\}\n\nfunction _renderAccountRiskLine",
        src,
        flags=re.S,
    )
    if not m:
        raise AssertionError("renderMonthlyActions() not found")
    return m.group("body")


class MonthlyActionsDashboardTest(unittest.TestCase):
    def test_page_is_ai_workbench_read_only_advisory(self):
        src = _source()

        self.assertIn('id="monthly-actions"', src)
        self.assertIn('data-tab="monthly-actions"', src)
        self.assertIn("只读 advisory", src)
        self.assertIn("不自动写真实持仓", src)
        self.assertIn("不自动交易", src)
        self.assertIn("RECOMMENDATION_READINESS", src)

    def test_renderer_does_not_write_watchlist_or_real_holdings(self):
        body = _monthly_renderer(_source())

        self.assertNotIn("fetch(", body)
        self.assertNotIn("/api/watchlist", body)
        self.assertNotIn("/api/real-holdings", body)
        self.assertNotRegex(body, r"\b(POST|PUT|DELETE)\b")

    def test_money_guardrails_are_disclosed_to_user(self):
        # 只查"用户能看到护栏披露"，不绑实现细节/政策数字——后者一改就误报（脆）。
        # 护栏的真判定逻辑（≤3 / 单赛道15% / 同赛道去重 / 超配纠偏）由
        # tests/test_monthly_actions.py 真执行验证，不在这里查字符串。
        src = _source()
        self.assertIn("赛道", src)            # 赛道护栏概念可见
        self.assertIn("不是已证明买点", src)   # 策略未验证的诚实披露
        self.assertIn("US shadow alpha", src)  # 验证状态披露
        # 政策数字放宽成正则，调减仓目标(45%→30%等)不应误报
        self.assertRegex(src, r"主动减至\s*\d+%")

    def test_decisions_come_from_backend_single_source(self):
        # 关键：前端必须消费后端 monthly_actions 判定（单一来源），不得自带判定循环。
        src = _source()
        self.assertIn("MONTHLY_ACTIONS_PLAN", src)
        body = _monthly_renderer(src)
        self.assertIn("mp.buy_rows", body)
        self.assertIn("mp.skip_rows", body)
        self.assertIn("mp.corrections", body)
        # 旧的前端判定循环必须已移除（否则就是双引擎）
        self.assertNotIn("buyRows.length < 3", body)
        self.assertNotIn("const sorted = rows.slice()", body)

    def test_price_column_discloses_close_caliber_and_trade_date(self):
        src = _source()

        self.assertIn("参考价", src)
        self.assertNotIn("<th class=\"py-2 pr-3 text-right\">当前价格</th>", src)
        self.assertIn("最新收盘参考价，不是实时成交价", src)
        self.assertIn("current_trade_date", src)
        self.assertIn("收盘参考", src)
        self.assertIn("参考价对应的交易日", src)

    def test_buy_zone_payload_injects_current_trade_date(self):
        src = _source()
        m = re.search(r"def _buy_zone_payload\(\).*?return out", src, flags=re.S)
        self.assertIsNotNone(m)
        self.assertIn('"current_trade_date": z.get("current_trade_date")', m.group(0))


if __name__ == "__main__":
    unittest.main()
