"""统一重大事件警报聚合 —— 真执行逻辑测试（喂信号验输出）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from stock_research.core import major_event_alert as mea  # noqa: E402


def _sig(source, sev, key=None):
    return {"source": source, "severity": sev, "headline": f"{source} {sev}", "key": key}


class AggregateMajorAlertTest(unittest.TestCase):
    def test_calm_is_not_major_no_push(self):
        out = mea.aggregate_major_alert([_sig("大盘防御", "NONE"), _sig("盘前环境", "LOW")])
        self.assertFalse(out["is_major"])
        self.assertFalse(out["should_push"])
        self.assertEqual(out["severity"], "LOW")

    def test_high_alone_does_not_trip_red(self):
        # 红线=CRITICAL，单个 HIGH 不算重大（平时绝不打扰）
        out = mea.aggregate_major_alert([_sig("持仓异动", "HIGH")])
        self.assertFalse(out["is_major"])
        self.assertFalse(out["should_push"])

    def test_critical_trips_and_pushes(self):
        out = mea.aggregate_major_alert([_sig("大盘防御", "CRITICAL")])
        self.assertTrue(out["is_major"])
        self.assertTrue(out["should_push"])
        self.assertIn("🔴", out["headline"])

    def test_same_fingerprint_not_repushed(self):
        first = mea.aggregate_major_alert([_sig("大盘防御", "CRITICAL", key="defense:crash")])
        again = mea.aggregate_major_alert(
            [_sig("大盘防御", "CRITICAL", key="defense:crash")],
            prev_state=first["state"],
        )
        self.assertTrue(again["is_major"])
        self.assertFalse(again["should_push"], "同一事件不应反复轰炸")

    def test_new_critical_event_escalates_push(self):
        first = mea.aggregate_major_alert([_sig("大盘防御", "CRITICAL", key="defense:crash")])
        second = mea.aggregate_major_alert(
            [_sig("大盘防御", "CRITICAL", key="defense:crash"),
             _sig("盘前环境", "CRITICAL", key="premarket:gap")],
            prev_state=first["state"],
        )
        self.assertTrue(second["should_push"], "出现新的重大事件应再推一次")

    def test_recovery_flagged_once(self):
        major = mea.aggregate_major_alert([_sig("大盘防御", "CRITICAL", key="defense:crash")])
        calm = mea.aggregate_major_alert([_sig("大盘防御", "NONE")], prev_state=major["state"])
        self.assertFalse(calm["is_major"])
        self.assertTrue(calm["recovered"])
        self.assertIn("解除", calm["headline"])
        # 再来一轮平静 → 不再报恢复
        calm2 = mea.aggregate_major_alert([_sig("大盘防御", "NONE")], prev_state=calm["state"])
        self.assertFalse(calm2["recovered"])

    def test_consolidates_multiple_sources(self):
        out = mea.aggregate_major_alert([
            _sig("大盘防御", "CRITICAL", key="a"),
            _sig("盘前环境", "CRITICAL", key="b"),
            _sig("持仓异动", "HIGH", key="c"),  # 不达红线，不进 major
        ])
        self.assertEqual(len(out["major_events"]), 2)
        self.assertEqual(out["severity"], "CRITICAL")

    def test_unknown_severity_treated_none(self):
        out = mea.aggregate_major_alert([_sig("X", "garbage")])
        self.assertEqual(out["severity"], "NONE")
        self.assertFalse(out["is_major"])

    def test_blank_source_dropped(self):
        out = mea.aggregate_major_alert([{"severity": "CRITICAL"}])
        self.assertEqual(out["all_signals"], [])
        self.assertFalse(out["is_major"])

    def test_threshold_high_lets_high_trip(self):
        out = mea.aggregate_major_alert([_sig("持仓异动", "HIGH")], threshold="HIGH")
        self.assertTrue(out["is_major"])


if __name__ == "__main__":
    unittest.main()
