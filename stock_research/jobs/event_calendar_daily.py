"""每日事件日历刷新：解禁 + 减持 + 财报公告 → data/event_calendar.json。

被 daily_refresh.sh 调用。下游消费：
  - daily_picks_v5.py 在打分时给"7 日内大额解禁"标的降权
  - dashboard 在个股 tab 展示"未来 30 日事件"
"""
from __future__ import annotations
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from stock_research.core.event_calendar import build_calendar

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    logger.info("构建事件日历（解禁 90 天 + 增减持 ±60 天 + 最近 4 季财报）...")
    cal = build_calendar(
        horizon_unlock_days=90,
        horizon_insider_days=60,
        include_earnings=True,
    )
    out = REPO / "data" / "event_calendar.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at": cal.fetched_at.isoformat(),
        "status": "ok" if cal.events else "empty",
        "n_events": len(cal.events),
        "source_health": cal.source_health,
        "events": [e.to_dict() for e in cal.events],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")

    by_type = {}
    for e in cal.events:
        by_type[e.event_type] = by_type.get(e.event_type, 0) + 1
    print(f"✅ 事件日历已写入 {out}")
    print(f"   总事件数: {len(cal.events)}")
    for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"   {t:<20} {c}")
    if not cal.events:
        print("   今日窗口内没有可用事件；这是合法空结果，不阻断生产验收。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
