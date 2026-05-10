"""每日产业政策扫描：扫最近 7 天新闻流 → data/policy_events.json。

被 daily_refresh.sh 调用。下游消费：
  - daily_picks_v5.py 在打分时给"政策受益主题"加权
  - dashboard 顶部 banner 提示当日政策亮点
"""
from __future__ import annotations
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from stock_research.core.policy_events import (
    scan_recent_policies,
    themes_under_policy_tailwind,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    logger.info("扫描最近 7 天政策事件...")
    events = scan_recent_policies(days=7, min_score=2)
    tailwind = themes_under_policy_tailwind(days=14, min_count=2)

    out = REPO / "data" / "policy_events.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(),
        "n_events": len(events),
        "events": [e.to_dict() for e in events],
        "themes_tailwind_14d": tailwind,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")

    print(f"✅ 政策事件已写入 {out}")
    print(f"   事件数: {len(events)}")
    if tailwind:
        print(f"   受益主题（最近 14 天命中 ≥ 2 次）：")
        for t, c in sorted(tailwind.items(), key=lambda x: -x[1])[:10]:
            print(f"     {t:<14} {c} 次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
