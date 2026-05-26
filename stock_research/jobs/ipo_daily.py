"""新股打新日报：抓 IPO 日历 + 写入 data/ipo_calendar.json。

每日 daily_refresh.sh 调一次（早班/夜班/full 都跑，受 needs_ipo_data 守卫）。

输出：
  data/ipo_calendar.json    被 junior_stock_watcher.py 读取，
                            再合并到 data/latest/junior_stock_radar.json，
                            最终在 dashboard IPO & 次新股 tab 展示
  data/reports/ipo_daily_YYYYMMDD.md  人类可读 markdown
  stdout: 当日是否有可申购 + AI 相关新股摘要

注意：飞书侧目前不直接消费此 JSON（morning_brief 暂未拼装 IPO section）。
如需飞书推送，需在 morning_brief.py 增加 IPO 区块或单独写 webhook 推送 job。

退出码：
  0 = 成功（含"接口正常但当日无新股"）
  2 = 数据源完全失败（akshare 两个端点都报错）
"""
from __future__ import annotations
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from stock_research.core.ipo_pipeline import build_ipo_calendar, IpoCalendar

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _write_json(cal: IpoCalendar, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(cal.to_dict(), f, ensure_ascii=False, indent=2, default=str)


def _write_markdown(cal: IpoCalendar, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    today = date.today()
    lines = []
    lines.append(f"# 新股打新日报 — {today.isoformat()}\n")
    lines.append(f"_生成时间: {cal.fetched_at:%Y-%m-%d %H:%M}_\n")

    today_subs = [e for e in cal.upcoming_subscription
                  if e.subscribe_date == today]
    if today_subs:
        lines.append("## ⚠️ 今日可申购\n")
        for e in today_subs:
            ai_flag = "🟢AI" if e.ai_relevance >= 2 else ""
            lines.append(
                f"- **{e.name}** ({e.code} / 申购 {e.subscribe_code}) "
                f"`{e.board}` {ai_flag} {e.theme} "
                f"发行价 ¥{e.issue_price or '?'}"
            )
        lines.append("")

    sections = [
        ("## 🚀 即将申购（未来 7 天）", [e for e in cal.upcoming_subscription
                                         if e.subscribe_date and (e.subscribe_date - today).days <= 7]),
        ("## ⏳ 已申购未上市", cal.awaiting_listing),
        ("## 📊 近 30 日上市", cal.recently_listed[:15]),
    ]

    for title, entries in sections:
        if not entries:
            continue
        lines.append(title + "\n")
        lines.append("| 代码 | 名称 | 板块 | 申购日 | 发行价 | AI | 主题 | 业务 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for e in entries[:25]:
            ai_flag = "🟢" if e.ai_relevance >= 2 else ("🟡" if e.ai_relevance == 1 else "⚪")
            sub_d = e.subscribe_date.isoformat() if e.subscribe_date else "?"
            price = f"¥{e.issue_price:.2f}" if e.issue_price else "?"
            biz = (e.business_desc or e.industry)[:40].replace("|", "/")
            lines.append(
                f"| {e.code} | {e.name} | {e.board} | {sub_d} | {price} | "
                f"{ai_flag} {e.ai_relevance} | {e.theme} | {biz} |"
            )
        lines.append("")

    # AI 相关性专区
    ai_entries = cal.ai_relevant(min_score=2)
    if ai_entries:
        lines.append("## 🤖 AI 相关新股精选（相关性 ≥ 2）\n")
        for e in ai_entries:
            sub_d = e.subscribe_date.isoformat() if e.subscribe_date else "?"
            lines.append(
                f"- **{e.name}** ({e.code}) `{e.theme}` 申购 {sub_d} "
                f"发行价 ¥{e.issue_price or '?'} — {(e.business_desc or e.industry)[:80]}"
            )
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    repo = REPO
    out_json = repo / "data" / "ipo_calendar.json"
    out_md = repo / "data" / "reports" / f"ipo_daily_{date.today().strftime('%Y%m%d')}.md"

    cal = build_ipo_calendar()

    n_total = (len(cal.upcoming_subscription) + len(cal.awaiting_listing)
               + len(cal.recently_listed))

    # 写文件（即使为空），避免下游 reader 报错
    _write_json(cal, out_json)
    _write_markdown(cal, out_md)

    # 区分"接口失败" vs "当日无新股"：
    #   fetch_ok=False → akshare 两个端点都报错 → exit 2（真失败，daily_refresh 标红）
    #   fetch_ok=True 但 n_total=0 → 接口正常但当日确实无新股 → exit 0（正常情况）
    if not cal.fetch_ok():
        logger.error("akshare IPO 接口全部失败 (fetch_status=%s)", cal.fetch_status)
        return 2
    if n_total == 0:
        logger.info("IPO 接口正常但当日无新股 (fetch_status=%s)", cal.fetch_status)
        print(f"✅ IPO 日历空 — 接口正常但当日无新股 — {datetime.now():%Y-%m-%d %H:%M}")
        return 0

    today = date.today()
    today_subs = [e for e in cal.upcoming_subscription if e.subscribe_date == today]
    ai_entries = cal.ai_relevant(min_score=2)

    print(f"✅ IPO 日历已生成 — {datetime.now():%Y-%m-%d %H:%M}")
    print(f"   即将申购: {len(cal.upcoming_subscription)} | 已申购未上市: {len(cal.awaiting_listing)}"
          f" | 近 30 日上市: {len(cal.recently_listed)}")
    if today_subs:
        print(f"\n   ⚠️ 今日可申购 {len(today_subs)} 只:")
        for e in today_subs:
            print(f"     - {e.name} ({e.subscribe_code}) ¥{e.issue_price or '?'} {e.theme}")
    if ai_entries:
        print(f"\n   🤖 AI 相关 {len(ai_entries)} 只:")
        for e in ai_entries[:10]:
            sub_d = e.subscribe_date.isoformat() if e.subscribe_date else "?"
            print(f"     - {e.name} ({e.code}) {e.theme} 申购 {sub_d}")
    print(f"\n   JSON: {out_json}")
    print(f"   MD:   {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
