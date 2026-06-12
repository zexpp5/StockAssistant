"""AI 瓶颈领先信号 · 财报复查提醒。

来源：docs/2026-06-10_AI供给瓶颈行业与标的研究.md 提出的三个"叙事退潮"领先信号
（GEV 燃机槽位、Vertiv book-to-bill、美光 HBM 合约价）。这些是季度财报级订单数据，
没有实时数据源可自动监控——本 job 做能做到的那一半：到财报窗口时推一张飞书卡片，
提醒用户"该去复查了"，并附上每家具体要看什么。

触发：由 jobs/premarket_gate.py 在生产模式末尾顺带调用（每个美股交易日晚都会过一遍），
     北京日期落在 [财报日, 财报日+2] 窗口内推送，按 (ticker, 年-月) 去重 = 每季一次。
数据：只读 data/event_calendar_us.json（每日 08:00 刷新，含 earnings_upcoming）。
产出：data/bottleneck_earnings_reminder_state.json（防重复）。

CLI（独立测试）：
  python3 -m stock_research.jobs.bottleneck_earnings_reminder --dry-run
  python3 -m stock_research.jobs.bottleneck_earnings_reminder --dry-run --as-of 2026-06-24
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

logger = logging.getLogger(__name__)

CALENDAR_JSON = _REPO / "data" / "event_calendar_us.json"
STATE_FILE = _REPO / "data" / "bottleneck_earnings_reminder_state.json"

# 财报日后再提醒几天仍有效（覆盖盘后发布 + 周末错位）
WINDOW_AFTER_DAYS = 2

# 三个领先信号 → 各自的"财报里看什么"复查清单（白话，新手能照着看）
BELLWETHERS: dict[str, dict] = {
    "GEV": {
        "name": "GE Vernova（燃机/电力设备）",
        "signal": "燃机订单还抢手吗",
        "checks": [
            "燃机槽位/新订单：预订增速比上季度回落了吗？",
            "有没有客户「转售槽位 / 折价」的字眼？出现 = 抢产能的人开始撤了",
            "订单积压（backlog）还在创新高吗？",
        ],
    },
    "VRT": {
        "name": "Vertiv（数据中心电力/散热）",
        "signal": "book-to-bill 还 ≥1.2 吗",
        "checks": [
            "book-to-bill（新签订单 ÷ 当期出货）：≥1.2 = 订单仍供不应求",
            "跌破 1.2 = 出货追上了订单，是数据中心建设热度见顶的领先信号",
            "管理层对明年订单管线（pipeline）的措辞有没有变保守？",
        ],
    },
    "MU": {
        "name": "美光（HBM 存储）",
        "signal": "HBM 还在涨价、还售罄吗",
        "checks": [
            "HBM 合约价：环比还在涨吗？环比转负 = 存储瓶颈退潮",
            "HBM 产能是否仍「提前售罄」（sold out）？措辞从售罄变「供需平衡」要警惕",
            "注意它是周期股：利润最好的时候往往就是周期顶",
        ],
    },
}


def _load_due_events(as_of: date) -> list[dict]:
    """从本地事件日历找出落在复查窗口内的瓶颈龙头财报。"""
    if not CALENDAR_JSON.exists():
        logger.warning("事件日历不存在：%s（跳过提醒）", CALENDAR_JSON.name)
        return []
    try:
        doc = json.loads(CALENDAR_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("事件日历读取失败：%s", exc)
        return []
    due: list[dict] = []
    for ev in doc.get("events") or []:
        sym = str(ev.get("ticker") or ev.get("symbol") or "").upper()
        if sym not in BELLWETHERS:
            continue
        if ev.get("event_type") not in ("earnings", "earnings_upcoming"):
            continue
        try:
            ed = date.fromisoformat(str(ev.get("event_date") or "")[:10])
        except Exception:
            continue
        if ed <= as_of <= ed + timedelta(days=WINDOW_AFTER_DAYS):
            due.append({"ticker": sym, "event_date": ed.isoformat()})
    # 同一 ticker 取最近一条
    best: dict[str, dict] = {}
    for ev in sorted(due, key=lambda x: x["event_date"]):
        best[ev["ticker"]] = ev
    return list(best.values())


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("读 state 失败: %s", exc)
    return {}


def _dedup_key(ev: dict) -> str:
    # 按 (ticker, 年-月) 去重：财报日在日历里小幅改期也不会重复推
    return f"{ev['ticker']}:{ev['event_date'][:7]}"


def build_card(events: list[dict], as_of: date) -> dict:
    blocks: list[str] = []
    for ev in events:
        meta = BELLWETHERS[ev["ticker"]]
        checks = "\n".join(f"  {i}. {c}" for i, c in enumerate(meta["checks"], 1))
        blocks.append(
            f"**{meta['name']}** · 财报日 {ev['event_date']}\n"
            f"核心问题：**{meta['signal']}**\n{checks}"
        )
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": (
            "### 🔬 AI 瓶颈龙头财报窗口到了\n\n"
            "财报发布后花十分钟，对着下面的清单核对一遍——"
            "这是「AI 基建还缺不缺货」最早的体温计。"
        )}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(blocks)}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": (
            "**信号亮了怎么办**：任一指标转弱 ≠ 清仓，含义是**停止给瓶颈类个股加仓**，"
            "等下一季财报确认方向。三个信号同时转弱才说明整条「缺货叙事」在退潮。"
        )}},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": (
            "📖 出处：2026-06-10《AI供给瓶颈行业与标的研究》三大领先信号；"
            "订单类数据无法自动抓取，本卡只负责到点提醒。每家每季最多提醒一次 · "
            "仅供研究参考，不构成买卖指令"
        )}]},
    ]
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"🔬 瓶颈信号复查提醒 · {as_of.isoformat()}"},
                "subtitle": {"tag": "plain_text",
                             "content": " / ".join(e["ticker"] for e in events) + " 财报窗口"},
                "template": "blue",
            },
            "elements": elements,
        },
    }


def run(now: datetime | None = None, dry_run: bool = False) -> int:
    """检查并推送。返回本次推送的事件数。premarket_gate 生产链路里调用。"""
    as_of = (now or datetime.now()).date()
    due = _load_due_events(as_of)
    if not due:
        logger.info("瓶颈财报提醒：%s 无在窗事件", as_of)
        return 0

    state = _load_state()
    pushed: dict = state.get("pushed") or {}
    fresh = [ev for ev in due if _dedup_key(ev) not in pushed]
    if not fresh:
        logger.info("瓶颈财报提醒：在窗事件本季均已推过（%s）",
                    "、".join(e["ticker"] for e in due))
        return 0

    card = build_card(fresh, as_of)
    if dry_run:
        print(f"[dry-run] 将推送 {len(fresh)} 家：" + "、".join(e["ticker"] for e in fresh))
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return len(fresh)

    from stock_research.jobs.premarket_gate import _push  # 复用同一 webhook/推送逻辑
    ok = _push(card)
    logger.info("瓶颈财报提醒：推送 %s → %s",
                "、".join(e["ticker"] for e in fresh), "成功" if ok else "失败")
    if ok:
        ts = datetime.now().isoformat(timespec="seconds")
        for ev in fresh:
            pushed[_dedup_key(ev)] = {"event_date": ev["event_date"], "pushed_at": ts}
        state["pushed"] = pushed
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    return len(fresh) if ok else 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="AI 瓶颈龙头财报复查提醒")
    p.add_argument("--dry-run", action="store_true", help="只打印卡片，不推送不写 state")
    p.add_argument("--as-of", help="模拟日期 YYYY-MM-DD（测试用）")
    args = p.parse_args()
    now = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now()
    n = run(now=now, dry_run=args.dry_run)
    print(f"在窗且需推送的事件数：{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
