"""A 股生产权重切换开关测试。

2026-07-14 用户拍板：A 股生产切锦标赛挑战者 cn_reversal_quality
(reversal 0.60 + f_score 0.40)，回退开关 CN_REVERSAL_QUALITY_ACTIVE=0 回 IC 校准纯反转。
"""
from __future__ import annotations

import os
import unittest

from stock_research.jobs.a_share_picks import (
    CN_REVERSAL_QUALITY_WEIGHTS,
    load_weights,
)


class CNWeightSwitchTest(unittest.TestCase):
    def _run_with_flag(self, value):
        old = os.environ.get("CN_REVERSAL_QUALITY_ACTIVE")
        try:
            if value is None:
                os.environ.pop("CN_REVERSAL_QUALITY_ACTIVE", None)
            else:
                os.environ["CN_REVERSAL_QUALITY_ACTIVE"] = value
            return load_weights()
        finally:
            if old is None:
                os.environ.pop("CN_REVERSAL_QUALITY_ACTIVE", None)
            else:
                os.environ["CN_REVERSAL_QUALITY_ACTIVE"] = old

    def test_default_uses_reversal_quality_challenger(self):
        weights, source = self._run_with_flag(None)
        self.assertEqual(weights, dict(CN_REVERSAL_QUALITY_WEIGHTS))
        self.assertIn("cn_reversal_quality", source)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=9)

    def test_rollback_flag_restores_calibrated_reversal(self):
        weights, source = self._run_with_flag("0")
        # 回退后不再是挑战者公式；生产环境应回到 IC 校准（纯反转）或启发式兜底。
        self.assertNotEqual(source, "cn_reversal_quality@tournament_challenger")
        self.assertNotIn("f_score", {k for k, w in weights.items() if w > 0})


if __name__ == "__main__":
    unittest.main()
