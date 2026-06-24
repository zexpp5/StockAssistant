"""财报前 1 周预告 —— 自选股 earnings_upcoming 提前一周飞书提醒。

和 bottleneck_earnings_reminder 的分工：
  - 本 job：财报「前」7 天预告 —— 让你提前知道下周哪些自选股要出财报，好提前减仓/留意；
            范围 = manual_watchlist[US] 全部，每家每季最多预告一次。
  - bottleneck_earnings_reminder：财报「当天/盘后」到点复查提醒（仅瓶颈/capex 信号组）。

数据源：data/event_calendar_us.json（event_calendar_us_daily.py 拉 yfinance 财报日）。
出口：飞书一张「下周财报预告」汇总卡（复用 premarket_gate._push webhook）。
状态：data/earnings_preview_reminder_state.json（按 ticker:年-月 去重，改期不重复推）。

用法:
  python3 -m stock_research.jobs.earnings_preview_reminder --dry-run
  python3 -m stock_research.jobs.earnings_preview_reminder --dry-run --as-of 2026-06-18
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[2]
CALENDAR_JSON = _REPO / "data" / "event_calendar_us.json"
STATE_FILE = _REPO / "data" / "earnings_preview_reminder_state.json"

# 提前几天预告（含当天端点）：1 <= days_until <= LEAD_DAYS 才算「下周内」
LEAD_DAYS = 7


def _watchlist_us() -> dict[str, str]:
    """ticker → name；manual_watchlist 美股（只读连库，缺失则空）。"""
    out: dict[str, str] = {}
    try:
        import time

        import duckdb
        db_path = _REPO / "stock_history_v2.duckdb"
        if not db_path.exists():
            return out
        # read_only 连接遇 enhancement_refresh 写锁会失败 → 带退避重试
        con = None
        for _ in range(15):
            try:
                con = duckdb.connect(str(db_path), read_only=True)
                break
            except Exception:
                time.sleep(2)
        if con is None:
            logger.warning("读自选股：DB 持续被写锁占用，跳过本轮预告")
            return out
        try:
            for sym, name in con.execute(
                # market 列取值不统一：'US' 与 '美股' 都是美股（HK 排除）
                "SELECT symbol, name FROM manual_watchlist WHERE market IN ('US', '美股') OR market IS NULL"
            ).fetchall():
                if sym:
                    out[str(sym).upper()] = name or ""
        finally:
            con.close()
    except Exception as exc:
        logger.warning("读自选股失败: %s", exc)
    return out


def _signal_groups() -> dict[str, str]:
    """ticker → 组标签（瓶颈组/capex组），用于卡片里高亮。"""
    out: dict[str, str] = {}
    try:
        from stock_research.jobs.bottleneck_earnings_reminder import GROUPS
        for gk, spec in GROUPS.items():
            label = "瓶颈组" if "bottleneck" in gk else ("capex组" if "capex" in gk else gk)
            for t in spec.get("tickers", {}):
                out[str(t).upper()] = label
    except Exception:
        pass
    return out


def _load_due(as_of: date) -> list[dict]:
    """自选股中，财报日落在 (as_of, as_of+LEAD_DAYS] 的事件，每只取最近一条。"""
    if not CALENDAR_JSON.exists():
        logger.warning("事件日历不存在：%s（跳过预告）", CALENDAR_JSON.name)
        return []
    try:
        doc = json.loads(CALENDAR_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("事件日历读取失败：%s", exc)
        return []
    wl = _watchlist_us()
    grp = _signal_groups()
    if not wl:
        return []
    horizon = as_of + timedelta(days=LEAD_DAYS)
    best: dict[str, dict] = {}
    for ev in doc.get("events") or []:
        if ev.get("event_type") != "earnings_upcoming":
            continue
        sym = str(ev.get("ticker") or ev.get("symbol") or "").upper()
        if sym not in wl:
            continue
        try:
            ed = date.fromisoformat(str(ev.get("event_date") or "")[:10])
        except Exception:
            continue
        # 严格「前」：当天交给到点提醒，这里只管 1..LEAD_DAYS 天后
        if not (as_of < ed <= horizon):
            continue
        cur = best.get(sym)
        if cur is None or ed < date.fromisoformat(cur["event_date"]):
            best[sym] = {
                "ticker": sym,
                "name": wl.get(sym) or "",
                "event_date": ed.isoformat(),
                "days_until": (ed - as_of).days,
                "group": grp.get(sym, ""),
            }
    return sorted(best.values(), key=lambda x: x["event_date"])


def _dedup_key(ev: dict) -> str:
    return f"{ev['ticker']}:{ev['event_date'][:7]}"


def build_card(events: list[dict], as_of: date) -> dict:
    lines = []
    for ev in events:
        tag = f" `{ev['group']}`" if ev.get("group") else ""
        nm = f" {ev['name']}" if ev.get("name") else ""
        lines.append(
            f"**{ev['ticker']}**{nm} · {ev['event_date']}（{ev['days_until']} 天后）{tag}"
        )
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": (
            "### 📅 下周财报预告\n\n以下自选股将在 **未来 1 周内** 公布财报，提前留意 / 决定是否在财报前调整仓位。"
        )}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": (
            "标 `瓶颈组`/`capex组` 的票，财报当天/盘后还会再推一张复查提醒卡 + 次日 08:30 AI 体检。"
            "日期来自 yfinance，可能因公司改期变动，以官方为准。每家每季最多预告一次 · 仅供研究参考，不构成买卖指令"
        )}]},
    ]
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"下周财报预告 · {as_of.isoformat()}"},
                "subtitle": {"tag": "plain_text",
                             "content": " / ".join(e["ticker"] for e in events) + " 临近财报"},
                "template": "turquoise",
            },
            "elements": elements,
        },
    }


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("读 state 失败: %s", exc)
    return {}


def run(now: datetime | None = None, dry_run: bool = False) -> int:
    """检查并推送下周财报预告。返回本次预告的股票数。premarket_gate 链路里调用。"""
    as_of = (now or datetime.now()).date()
    due = _load_due(as_of)
    if not due:
        logger.info("财报预告：%s 未来 %d 天内无自选股财报", as_of, LEAD_DAYS)
        return 0

    state = _load_state()
    pushed: dict = state.get("pushed") or {}
    fresh = [ev for ev in due if _dedup_key(ev) not in pushed]
    if not fresh:
        logger.info("财报预告：在窗股票本季均已预告（%s）",
                    "、".join(e["ticker"] for e in due))
        return 0

    card = build_card(fresh, as_of)
    if dry_run:
        print(f"[dry-run] 将预告 {len(fresh)} 只：" + "、".join(e["ticker"] for e in fresh))
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return len(fresh)

    from stock_research.jobs.premarket_gate import _push  # 复用同一 webhook
    ok = _push(card)
    logger.info("财报预告：推送 %s → %s",
                "、".join(e["ticker"] for e in fresh), "成功" if ok else "失败")
    if ok:
        ts = datetime.now().isoformat(timespec="seconds")
        for ev in fresh:
            pushed[_dedup_key(ev)] = {"event_date": ev["event_date"], "pushed_at": ts}
        state["pushed"] = pushed
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(fresh)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="自选股财报前 1 周预告")
    p.add_argument("--dry-run", action="store_true", help="只打印卡片，不推送不写 state")
    p.add_argument("--as-of", help="模拟日期 YYYY-MM-DD（测试用）")
    args = p.parse_args()
    now = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now()
    n = run(now=now, dry_run=args.dry_run)
    print(f"需预告的股票数：{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
