"""瓶颈信号复查记录 + 聚合判定（core/bottleneck_signals.py）。

聚合规则对齐提醒卡文案：
  bottleneck 组：任一转弱=caution(停止加仓)；三个同季转弱=alert(叙事退潮)。
  capex 组：一家下调=caution(记一笔)；两家以上=alert(消化期)。
"""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from stock_research.core import bottleneck_signals as bs


class _TmpReviewsMixin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_file = bs.REVIEWS_FILE
        bs.REVIEWS_FILE = Path(self._tmp.name) / "reviews.json"

    def tearDown(self) -> None:
        bs.REVIEWS_FILE = self._orig_file
        self._tmp.cleanup()


class SaveLoadTest(_TmpReviewsMixin):
    def test_save_and_reload(self) -> None:
        rec = bs.save_review("VRT", "2026Q1", "转弱", "A",
                             "https://x.test", "book-to-bill 1.1")
        self.assertEqual(rec["group"], "bottleneck")
        rows = bs.load_reviews()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["conclusion"], "转弱")

    def test_upsert_dedups_by_ticker_quarter(self) -> None:
        bs.save_review("MU", "2026Q1", "转弱")
        bs.save_review("MU", "2026Q1", "转强")  # 同季改口 → 覆盖
        rows = bs.load_reviews()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["conclusion"], "转强")

    def test_latest_review_picks_max_quarter(self) -> None:
        bs.save_review("GEV", "2025Q4", "转强")
        bs.save_review("GEV", "2026Q1", "持平")
        latest = bs.latest_review("GEV")
        self.assertEqual(latest["quarter"], "2026Q1")

    def test_validation_errors(self) -> None:
        with self.assertRaises(ValueError):
            bs.save_review("NVDA", "2026Q1", "转弱")  # 不在 7 信号名单
        with self.assertRaises(ValueError):
            bs.save_review("MU", "2026-Q1", "转弱")  # 季度格式
        with self.assertRaises(ValueError):
            bs.save_review("MU", "2026Q1", "暴涨")  # 结论枚举
        with self.assertRaises(ValueError):
            bs.save_review("MU", "2026Q1", "转弱", evidence_tier="S")  # 档位枚举


class AggregateTest(_TmpReviewsMixin):
    def test_pending_when_nothing_reviewed(self) -> None:
        v = bs.aggregate_group("bottleneck")
        self.assertEqual(v["level"], "pending")
        self.assertEqual(v["n_reviewed"], 0)

    def test_bottleneck_one_weak_is_caution(self) -> None:
        bs.save_review("VRT", "2026Q1", "转弱")
        bs.save_review("GEV", "2026Q1", "转强")
        v = bs.aggregate_group("bottleneck")
        self.assertEqual(v["level"], "caution")
        self.assertIn("VRT", v["weak_tickers"])

    def test_bottleneck_three_weak_is_alert(self) -> None:
        for t in ("GEV", "VRT", "MU"):
            bs.save_review(t, "2026Q1", "转弱")
        v = bs.aggregate_group("bottleneck")
        self.assertEqual(v["level"], "alert")
        self.assertIn("退潮", v["text"])

    def test_bottleneck_all_strong_is_ok(self) -> None:
        for t in ("GEV", "VRT", "MU"):
            bs.save_review(t, "2026Q1", "转强")
        self.assertEqual(bs.aggregate_group("bottleneck")["level"], "ok")

    def test_capex_one_weak_records_only(self) -> None:
        bs.save_review("META", "2026Q1", "转弱")
        v = bs.aggregate_group("capex")
        self.assertEqual(v["level"], "caution")
        self.assertIn("记一笔", v["text"])

    def test_capex_two_weak_is_alert(self) -> None:
        bs.save_review("META", "2026Q1", "转弱")
        bs.save_review("MSFT", "2026Q1", "转弱")
        v = bs.aggregate_group("capex")
        self.assertEqual(v["level"], "alert")
        self.assertIn("消化期", v["text"])

    def test_stale_review_not_counted(self) -> None:
        bs.save_review("VRT", "2025Q1", "转弱")
        # 手动把 reviewed_at 改老（超 STALE_AFTER_DAYS）
        doc = json.loads(bs.REVIEWS_FILE.read_text(encoding="utf-8"))
        old = datetime.now() - timedelta(days=bs.STALE_AFTER_DAYS + 10)
        doc["reviews"][0]["reviewed_at"] = old.isoformat(timespec="seconds")
        bs.REVIEWS_FILE.write_text(json.dumps(doc, ensure_ascii=False),
                                   encoding="utf-8")
        v = bs.aggregate_group("bottleneck")
        self.assertEqual(v["level"], "pending")  # 过期记录不采信


class PayloadTest(_TmpReviewsMixin):
    def test_payload_shape(self) -> None:
        bs.save_review("MU", "2026Q1", "持平", "B")
        p = bs.build_payload(as_of=date(2026, 6, 12))
        self.assertTrue(p["available"])
        self.assertEqual(p["current_quarter"], "2026Q2")
        self.assertEqual({g["key"] for g in p["groups"]}, {"bottleneck", "capex"})
        mu = next(s for g in p["groups"] for s in g["signals"]
                  if s["ticker"] == "MU")
        self.assertEqual(mu["latest"]["conclusion"], "持平")
        self.assertEqual(len(mu["checks"]), 3)

    def test_history_capped_at_four(self) -> None:
        for i, q in enumerate(["2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1"]):
            bs.save_review("GEV", q, "转强")
        p = bs.build_payload()
        gev = next(s for g in p["groups"] for s in g["signals"]
                   if s["ticker"] == "GEV")
        self.assertEqual(len(gev["history"]), 4)
        self.assertEqual(gev["history"][0]["quarter"], "2026Q1")  # 最新在前


class RegistryCompatTest(unittest.TestCase):
    def test_job_reexports_groups(self) -> None:
        # earnings_signal_analyzer 仍从提醒 job import GROUPS — 迁移后不能破
        from stock_research.jobs.bottleneck_earnings_reminder import GROUPS
        self.assertIs(GROUPS, bs.GROUPS)
        self.assertEqual(set(bs.TICKER_GROUP),
                         {"GEV", "VRT", "MU", "MSFT", "GOOGL", "AMZN", "META"})


if __name__ == "__main__":
    unittest.main()
