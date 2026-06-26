from __future__ import annotations

import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class TestDailyRefreshStatus(unittest.TestCase):
    def test_status_backfill_rebuild_is_recorded_as_pipeline_step(self) -> None:
        script = (REPO / "daily_refresh.sh").read_text(encoding="utf-8")

        self.assertIn(
            'run_step "27b/25 状态回填重建 HTML" "scripts/pipeline/build_stock_dashboard_html.py"',
            script,
        )
        self.assertNotIn(
            "27b 状态回填重建 HTML/scripts/pipeline/build_stock_dashboard_html.py",
            script,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
