"""触发式 Alert watcher — 每 15 min 跑一次市场层防御信号，severity 升档时即时推送飞书。

设计：
  - 仅查市场层（VIX + SPY/200MA + 宏观 + PCR），不查 picks 止损（picks 日级粒度，
    intraday 不变；省下 feishu API 调用，cron 也跑得起 96 次/天）。
  - 状态文件 data/defense_watcher_state.json 记录上次 severity
  - 严重度排序: NONE(0) < LOW(1) < HIGH(2) < CRITICAL(3)
  - 仅升档时推送（降档静默，避免噪音和"刚升又降"的来回打扰）
  - 推送复用 morning_brief 的 webhook（FEISHU_ALERT_WEBHOOK > FEISHU_BRIEF_WEBHOOK）

CLI:
  python3 -m stock_research.jobs.defense_watcher          # 正常跑
  python3 -m stock_research.jobs.defense_watcher --force  # 强制推送当前 severity 一次（测试用）
  python3 -m stock_research.jobs.defense_watcher --reset  # 把 state 重置为 NONE（下次升档才推）

Cron（每 15 min，盘前 8:00 - 盘后 22:00；夜里不跑省费用）:
  */15 8-22 * * * cd /Users/yanli/我的代码_新/线性视界/StockAssistant && \
    /usr/bin/python3 -m stock_research.jobs.defense_watcher >> data/defense_watcher.log 2>&1
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from stock_research.core import defense_signals  # noqa: E402

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"NONE": 0, "LOW": 1, "HIGH": 2, "CRITICAL": 3}
ICON_MAP = {"NONE": "🟢", "LOW": "🟡", "HIGH": "🟠", "CRITICAL": "🔴"}
TEMPLATE_MAP = {"NONE": "blue", "LOW": "yellow", "HIGH": "orange", "CRITICAL": "red"}
STATE_FILE = _REPO / "data" / "defense_watcher_state.json"


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"读 state 失败: {e}")
            return {}
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _market_severity() -> tuple[str, list[dict]]:
    """只跑市场层，返回 (severity, alerts)。"""
    alerts = defense_signals.check_market_regime()
    if any(a.get("severity") == "CRITICAL" for a in alerts):
        sev = "CRITICAL"
    elif any(a.get("severity") == "HIGH" for a in alerts):
        sev = "HIGH"
    elif alerts:
        sev = "LOW"
    else:
        sev = "NONE"
    return sev, alerts


def _build_alert_card(prev: str, curr: str, alerts: list[dict]) -> dict:
    """飞书 interactive card v1 — 与 morning_brief 视觉一致。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    prev_icon = ICON_MAP.get(prev, "⚪")
    curr_icon = ICON_MAP.get(curr, "⚪")
    advice = {
        "LOW": "👉 **留意但别加仓**，单笔不超 5% 仓位",
        "HIGH": "👉 **减仓 30-50%，停止买入**，可换防御标的（KO / MCD 等）",
        "CRITICAL": "👉 **清仓 sit out** — 崩盘期历史 alpha = -9.77%，等灯转回 LOW 再回来",
    }.get(curr, "")

    elements: list[dict] = [{
        "tag": "div",
        "text": {"tag": "lark_md", "content": (
            f"{prev_icon} **{prev}** → {curr_icon} **{curr}**\n\n"
            f"{advice}"
        )},
    }]

    if alerts:
        alert_lines = []
        for a in alerts[:5]:
            sev = a.get("severity", "")
            typ = a.get("type") or a.get("name", "")
            trig = a.get("trigger") or a.get("suggested_action", "")
            alert_lines.append(f"• [{sev}] **{typ}**: {trig}")
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**触发明细**\n" + "\n".join(alert_lines)},
        })

    elements.append({"tag": "note", "elements": [
        {"tag": "plain_text", "content": (
            "📖 4 档对照：🟢 NONE 正常 ｜ 🟡 LOW 留意 ｜ 🟠 HIGH 减仓 ｜ 🔴 CRITICAL 清仓 · "
            "本卡片仅在 severity 升档时推送 · ⚠️ 不构成投资建议"
        )},
    ]})

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"🚨 防御信号升档 · {now_str}"},
                "subtitle": {"tag": "plain_text", "content": f"{prev} → {curr}"},
                "template": TEMPLATE_MAP.get(curr, "grey"),
            },
            "elements": elements,
        },
    }


def _push(card: dict) -> bool:
    """推送到 FEISHU_ALERT_WEBHOOK（优先）或 FEISHU_BRIEF_WEBHOOK。"""
    webhook = (
        os.environ.get("FEISHU_ALERT_WEBHOOK", "").strip()
        or os.environ.get("FEISHU_BRIEF_WEBHOOK", "").strip()
    )
    if not webhook:
        logger.info("无 webhook 配置，仅打印告警内容；export FEISHU_ALERT_WEBHOOK=... 启用推送")
        return False
    try:
        r = requests.post(webhook, json=card, timeout=15)
        ok = r.status_code == 200 and r.json().get("StatusCode", 0) == 0
        if not ok:
            logger.warning(f"webhook 推送失败 ({r.status_code}): {r.text[:200]}")
        return ok
    except Exception as e:
        logger.warning(f"webhook 推送异常: {e}")
        return False


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="触发式防御 Alert watcher（市场层 only）")
    p.add_argument("--force", action="store_true", help="强制推送当前 severity 一次（测试）")
    p.add_argument("--reset", action="store_true", help="重置 state 为 NONE")
    args = p.parse_args()

    if args.reset:
        _save_state({"last_severity": "NONE", "reset_at": datetime.now().isoformat(timespec="seconds")})
        print("✅ state 已重置为 NONE")
        return 0

    curr, alerts = _market_severity()
    state = _load_state()
    prev = state.get("last_severity", "NONE")

    curr_rank = SEVERITY_ORDER.get(curr, -1)
    prev_rank = SEVERITY_ORDER.get(prev, -1)

    should_push = args.force or (curr_rank > prev_rank)
    if should_push:
        logger.info(f"🚨 推送：{prev} → {curr}（{len(alerts)} 条 alert）")
        card = _build_alert_card(prev, curr, alerts)
        ok = _push(card)
        state["last_alert_at"] = datetime.now().isoformat(timespec="seconds")
        state["last_alert_sent_ok"] = ok
        state["last_alert_severity"] = curr
    elif curr_rank < prev_rank:
        logger.info(f"📉 降档静默：{prev} → {curr}（仅更新 state，不推送）")
    else:
        logger.info(f"= severity 未变：{curr}（{len(alerts)} 条 alert）")

    state["last_severity"] = curr
    state["last_check_at"] = datetime.now().isoformat(timespec="seconds")
    state["last_alert_count"] = len(alerts)
    _save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
