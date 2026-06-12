"""AI 叙事领先信号 · 财报复查提醒（订单级 + capex 级）。

两组信号，来源 docs/2026-06-10_AI供给瓶颈行业与标的研究.md 及牛熊机制讨论：
  bottleneck —— 供给侧订单信号：GEV 燃机槽位 / Vertiv book-to-bill / 美光 HBM 合约价。
  capex      —— 需求侧总阀门：MSFT/GOOGL/AMZN/META 的资本开支指引。
                整条 AI 供应链(英伟达/台积电/电力链)的收入 = 这四家的钱包。

这些都是季度财报级数据，没有实时数据源可自动抓取——本 job 做能做到的那一半：
到财报窗口时按组推飞书卡片，提醒"该去复查了"，并附上每家具体要看什么。

触发：由 jobs/premarket_gate.py 在生产模式末尾顺带调用（每个美股交易日晚都会过一遍），
     北京日期落在 [财报日, 财报日+2] 窗口内推送，按 (ticker, 年-月) 去重 = 每家每季一次。
数据：只读 data/event_calendar_us.json（每日 08:00 刷新，含 earnings_upcoming）。
产出：data/bottleneck_earnings_reminder_state.json（防重复）。

CLI（独立测试）：
  python3 -m stock_research.jobs.bottleneck_earnings_reminder --dry-run
  python3 -m stock_research.jobs.bottleneck_earnings_reminder --dry-run --as-of 2026-07-23
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

# 两组领先信号 → 各自的"财报里看什么"复查清单（白话，新手能照着看）
GROUPS: dict[str, dict] = {
    "bottleneck": {
        "title": "🔬 瓶颈信号复查提醒",
        "headline": "AI 瓶颈龙头财报窗口到了",
        "intro": ("财报发布后花十分钟，对着下面的清单核对一遍——"
                  "这是「AI 基建还缺不缺货」最早的体温计。"),
        "meaning": ("**信号亮了怎么办**：任一指标转弱 ≠ 清仓，含义是**停止给瓶颈类个股加仓**，"
                    "等下一季财报确认方向。三个信号同时转弱才说明整条「缺货叙事」在退潮。"),
        "tickers": {
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
        },
    },
    "capex": {
        "title": "☁️ 云大厂 capex 指引复查",
        "headline": "AI 供应链「总阀门」财报窗口到了",
        "intro": ("整条 AI 供应链（英伟达/台积电/电力链/光模块）的收入，本质上就是这四家的资本开支。"
                  "财报后只盯一个问题：**capex 指引是上调、维持，还是下调？**"),
        "meaning": ("**信号怎么读**：一家下调 = 记一笔，先不动作；**两家以上同季下调 = "
                    "「capex 消化期」开始的强信号**——停止 AI 基建/算力类个股加仓，底仓定投照旧。"
                    "这是本轮牛熊机制里最重要的领先指标，比股价早一到两个季度。"),
        "tickers": {
            "MSFT": {
                "name": "微软（Azure）",
                "signal": "capex 指引方向 + 产能措辞",
                "checks": [
                    "下季度/全财年 capex 指引：上调、维持还是下调？",
                    "「产能受限/供不应求」（capacity constrained）的措辞还在吗？消失 = 需求降温早期信号",
                    "Azure 增速有没有掉档？",
                ],
            },
            "GOOGL": {
                "name": "谷歌（GCP/TPU）",
                "signal": "全年 capex 数字变没变",
                "checks": [
                    "全年 capex 指引金额：比上次说的数字高了还是低了？",
                    "Cloud 增速与利润率方向",
                    "管理层对「算力供不应求」的表述是否退坡？",
                ],
            },
            "AMZN": {
                "name": "亚马逊（AWS）",
                "signal": "capex（大头给 AWS）还在加吗",
                "checks": [
                    "capex 同比增速方向（绝大部分投给 AWS/AI）",
                    "AWS 增速有没有掉档？",
                    "管理层对 AI 需求的措辞：仍然「需求远超供给」吗？",
                ],
            },
            "META": {
                "name": "Meta（纯自用烧钱方）",
                "signal": "capex 指引区间上调还是下调",
                "checks": [
                    "全年 capex 指引区间：上调还是下调？",
                    "⚠️ 解读相反：META 下调对**它自己**股价常是利好，但对**整条 AI 供应链**是需求转弱的坏信号",
                    "对「AI 投入回报」的措辞有没有从进攻转防守？",
                ],
            },
        },
    },
}


def _load_due_events(as_of: date) -> list[dict]:
    """从本地事件日历找出落在复查窗口内的信号股财报，标注所属组。"""
    if not CALENDAR_JSON.exists():
        logger.warning("事件日历不存在：%s（跳过提醒）", CALENDAR_JSON.name)
        return []
    try:
        doc = json.loads(CALENDAR_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("事件日历读取失败：%s", exc)
        return []
    ticker_group = {t: g for g, spec in GROUPS.items() for t in spec["tickers"]}
    due: list[dict] = []
    for ev in doc.get("events") or []:
        sym = str(ev.get("ticker") or ev.get("symbol") or "").upper()
        if sym not in ticker_group:
            continue
        if ev.get("event_type") not in ("earnings", "earnings_upcoming"):
            continue
        try:
            ed = date.fromisoformat(str(ev.get("event_date") or "")[:10])
        except Exception:
            continue
        if ed <= as_of <= ed + timedelta(days=WINDOW_AFTER_DAYS):
            due.append({"ticker": sym, "event_date": ed.isoformat(),
                        "group": ticker_group[sym]})
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


def build_card(group_key: str, events: list[dict], as_of: date) -> dict:
    spec = GROUPS[group_key]
    blocks: list[str] = []
    for ev in events:
        meta = spec["tickers"][ev["ticker"]]
        checks = "\n".join(f"  {i}. {c}" for i, c in enumerate(meta["checks"], 1))
        blocks.append(
            f"**{meta['name']}** · 财报日 {ev['event_date']}\n"
            f"核心问题：**{meta['signal']}**\n{checks}"
        )
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": (
            f"### {spec['headline']}\n\n{spec['intro']}"
        )}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(blocks)}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": spec["meaning"]}},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": (
            "📖 出处：2026-06-10《AI供给瓶颈行业与标的研究》领先信号体系；"
            "订单/指引类数据无法自动抓取，本卡只负责到点提醒。每家每季最多提醒一次 · "
            "仅供研究参考，不构成买卖指令"
        )}]},
    ]
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"{spec['title']} · {as_of.isoformat()}"},
                "subtitle": {"tag": "plain_text",
                             "content": " / ".join(e["ticker"] for e in events) + " 财报窗口"},
                "template": "blue",
            },
            "elements": elements,
        },
    }


def run(now: datetime | None = None, dry_run: bool = False) -> int:
    """检查并按组推送。返回本次推送的事件数。premarket_gate 生产链路里调用。"""
    as_of = (now or datetime.now()).date()
    due = _load_due_events(as_of)
    if not due:
        logger.info("财报信号提醒：%s 无在窗事件", as_of)
        return 0

    state = _load_state()
    pushed: dict = state.get("pushed") or {}
    fresh = [ev for ev in due if _dedup_key(ev) not in pushed]
    if not fresh:
        logger.info("财报信号提醒：在窗事件本季均已推过（%s）",
                    "、".join(e["ticker"] for e in due))
        return 0

    n_sent = 0
    ts = datetime.now().isoformat(timespec="seconds")
    for group_key in GROUPS:
        group_events = [ev for ev in fresh if ev["group"] == group_key]
        if not group_events:
            continue
        card = build_card(group_key, group_events, as_of)
        if dry_run:
            print(f"[dry-run] {group_key} 组将推送 {len(group_events)} 家："
                  + "、".join(e["ticker"] for e in group_events))
            print(json.dumps(card, ensure_ascii=False, indent=2))
            n_sent += len(group_events)
            continue
        from stock_research.jobs.premarket_gate import _push  # 复用同一 webhook/推送逻辑
        ok = _push(card)
        logger.info("财报信号提醒[%s]：推送 %s → %s", group_key,
                    "、".join(e["ticker"] for e in group_events), "成功" if ok else "失败")
        if ok:
            for ev in group_events:
                pushed[_dedup_key(ev)] = {"event_date": ev["event_date"],
                                          "group": group_key, "pushed_at": ts}
            n_sent += len(group_events)

    if not dry_run and n_sent:
        state["pushed"] = pushed
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    return n_sent


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="AI 叙事领先信号 · 财报复查提醒")
    p.add_argument("--dry-run", action="store_true", help="只打印卡片，不推送不写 state")
    p.add_argument("--as-of", help="模拟日期 YYYY-MM-DD（测试用）")
    args = p.parse_args()
    now = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now()
    n = run(now=now, dry_run=args.dry_run)
    print(f"在窗且需推送的事件数：{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
