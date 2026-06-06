"""美股盘前风险闸门 — 晚间 job。

值班定位（与 defense_watcher 区分）：
  defense_watcher = "大盘风险变差才推"，8:00-22:00 收盘价，给 A 股盘前用。
  本 job        = "美股开盘前主动回答今晚能不能买"，北京 20-21 点跑。

三个扫描时点（launchd 每天跑，脚本内部判断是否有效窗口）：
  20:10  开盘前初扫（海外已收、期货、盘前巨头、事件日风险）
  20:45  数据发布后复扫（NFP/CPI 夏令时约 20:30 出，卡边界）
  21:15  开盘前最终版，给"今晚能不能买"的结论

产出：
  data/latest/premarket_gate.json   —— 唯一事实源（今日决策台 / 持仓页读它）
  data/premarket_gate_state.json    —— 防重复打扰（同日同档不重复推）

通知分级（用户定）：
  🟢 NONE     不弹不推，只写 JSON
  🟡 LOW      今日决策台顶部小横幅（页面读 JSON，不推飞书）
  🟠 HIGH     飞书 + 页面轻提醒（推一次）
  🔴 CRITICAL 强提醒，页面需点"我知道了"（推一次，升档再推）

CLI：
  python3 -m stock_research.jobs.premarket_gate              # 正常跑（按窗口+分级推送）
  python3 -m stock_research.jobs.premarket_gate --dry-run    # 只算只打印，不写 state 不推送
  python3 -m stock_research.jobs.premarket_gate --push       # 算完按分级推送（同 normal）
  python3 -m stock_research.jobs.premarket_gate --force      # 无视窗口+state，强制推一次（测试）

launchd（北京 20:10 / 20:45 / 21:15）见 docs / LaunchAgents。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, date
from pathlib import Path

import requests

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts" / "lib"))


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(_REPO / ".env")

from stock_research.core import premarket_gate as pg  # noqa: E402

logger = logging.getLogger(__name__)

OUT_JSON = _REPO / "data" / "latest" / "premarket_gate.json"
STATE_FILE = _REPO / "data" / "premarket_gate_state.json"

# 美股 2026 假日（NYSE 全天休市；待人工校准/逐年更新）
US_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


# ──────────────────────────────────────────────────
# state
# ──────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("读 state 失败: %s", e)
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ──────────────────────────────────────────────────
# 窗口守卫
# ──────────────────────────────────────────────────

def _is_valid_window(now: datetime) -> tuple[bool, str]:
    """是否美股开盘前有效窗口。周末/美股假日跳过。

    北京傍晚 19-23 点对应同一美股交易日的美东上午（开盘前）。
    """
    d = now.date()
    if now.weekday() >= 5:
        return False, f"{d} 是周末，美股休市"
    if d.isoformat() in US_HOLIDAYS_2026:
        return False, f"{d} 是美股假日，休市"
    if not (19 <= now.hour <= 23):
        return False, f"当前 {now:%H:%M} 不在盘前窗口（北京 19-23 点）"
    return True, "盘前有效窗口"


# ──────────────────────────────────────────────────
# 飞书卡片
# ──────────────────────────────────────────────────

def _build_card(res: pg.GateResult, scan_label: str) -> dict:
    """白话版卡片：给完全新手看，每条先说是什么、再说所以呢。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 1) 一句话结论（大字）
    head_line = f"### {res.headline_plain}\n\n**该怎么做**：{res.can_buy}"
    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content": head_line}}]

    # 1.5) 🚨 最该注意（最严重信号置顶突出）
    if res.top_alarm:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**{res.top_alarm}**"}})

    # 2) 为什么这么判断（人话 bullets，已按🔴严重/🟠留意标好）
    if res.reasons_plain:
        lines = "\n".join(f"{r}" for r in res.reasons_plain[:7])
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**为什么这么判断**\n" + lines}})

    # 3) 对你持仓的影响
    if res.holdings_impact:
        hl = "\n".join(f"• **{h['symbol']}**：{h['reason']}" for h in res.holdings_impact[:8])
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": "**对你持仓的影响**（只是提醒，不是叫你一定买卖）\n" + hl}})

    if res.notes:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": "ℹ️ " + "；".join(res.notes)}})

    # 4) 脚注：用大白话解释这张卡是什么 + 风险打分
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": (
        f"📖 这是「美股开盘前的看天气」：在美股开盘前帮你看一眼今晚适不适合买。"
        f"🟢正常买 🟡小仓试 🟠先别开新仓 🔴别买只看好已有 · "
        f"风险打分 {res.composite:.1f}/3（越高越危险）· ⚠️ 仅供参考，不是投资建议"
    )}]})

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"🚦 美股开盘前 · 今晚能不能买 · {now_str}"},
                "subtitle": {"tag": "plain_text", "content": f"{scan_label}"},
                "template": pg.TEMPLATE.get(res.color, "grey"),
            },
            "elements": elements,
        },
    }


def _push(card: dict) -> bool:
    webhook = (
        os.environ.get("FEISHU_ALERT_WEBHOOK", "").strip()
        or os.environ.get("FEISHU_BRIEF_WEBHOOK", "").strip()
    )
    if not webhook:
        logger.info("无 webhook 配置，仅本地打印；export FEISHU_ALERT_WEBHOOK=... 启用推送")
        return False
    try:
        r = requests.post(webhook, json=card, timeout=15)
        ok = r.status_code == 200 and r.json().get("StatusCode", 0) == 0
        if not ok:
            logger.warning("webhook 推送失败 (%s): %s", r.status_code, r.text[:200])
        return ok
    except Exception as e:
        logger.warning("webhook 推送异常: %s", e)
        return False


# ──────────────────────────────────────────────────
# 数据
# ──────────────────────────────────────────────────

def _load_real_holdings() -> list[dict]:
    try:
        import stock_db  # type: ignore
    except Exception as e:
        logger.warning("持仓读取跳过: %s", e)
        return []
    try:
        return stock_db.fetch_all_real_holdings()
    except Exception as e:
        logger.warning("fetch_all_real_holdings 失败: %s", e)
        return []


# ──────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────

def _scan_label(now: datetime) -> str:
    h, m = now.hour, now.minute
    if h == 20 and m < 30:
        return "初扫"
    if h == 20:
        return "数据后复扫"
    if h >= 21:
        return "开盘前最终"
    return "盘前"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="美股盘前风险闸门")
    p.add_argument("--dry-run", action="store_true", help="只算只打印，不写 state 不推送")
    p.add_argument("--push", action="store_true", help="算完按分级推送（同 normal）")
    p.add_argument("--force", action="store_true", help="无视窗口+state，强制推一次（测试）")
    args = p.parse_args()

    now = datetime.now()
    ok_window, why = _is_valid_window(now)
    if not ok_window and not (args.force or args.dry_run):
        logger.info("跳过：%s（--force 可强制跑）", why)
        return 0

    holdings = _load_real_holdings()
    logger.info("拉行情 + %d 只真实持仓...", len(holdings))
    res = pg.compute_gate(now=now, holdings=holdings)
    scan = _scan_label(now)

    # 控制台摘要
    print(f"\n{res.icon} {res.color}  综合压力 {res.composite:.2f}/3  覆盖率 {res.coverage:.0%}  [{scan}]")
    print(f"今晚能不能买：{res.can_buy}")
    if res.pressure_sources:
        print("压力源：" + "、".join(res.pressure_sources))
    for r in res.reasons:
        print(f"  • {r}")
    for h in res.holdings_impact:
        print(f"  💼 {h['symbol']}: {h['reason']}")
    if res.notes:
        print("备注：" + "；".join(res.notes))

    if args.dry_run:
        print("\n[--dry-run] 不写 JSON / state / 不推送")
        return 0

    # 写唯一事实源
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = res.to_dict()
    payload["scan_label"] = scan
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("已写 %s", OUT_JSON.relative_to(_REPO))

    # 分级推送 + 防重复
    state = _load_state()
    today = now.date().isoformat()
    if state.get("date") != today:
        state = {"date": today, "last_pushed_color": "NONE", "last_color": "NONE", "scans": []}

    curr_rank = pg.SEVERITY_ORDER.get(res.color, 0)
    pushed_rank = pg.SEVERITY_ORDER.get(state.get("last_pushed_color", "NONE"), 0)

    # 只有橙/红才推飞书；同日同档（或更低）不重复，升档才再推
    should_push = args.force or (curr_rank >= pg.SEVERITY_ORDER["HIGH"] and curr_rank > pushed_rank)

    if should_push:
        logger.info("🚦 推送飞书：%s（%s）", res.color, scan)
        ok = _push(_build_card(res, scan))
        state["last_pushed_color"] = res.color
        state["last_push_at"] = now.isoformat(timespec="seconds")
        state["last_push_ok"] = ok
    else:
        if curr_rank >= pg.SEVERITY_ORDER["HIGH"]:
            logger.info("橙/红但同档已推过，不重复轰炸（%s，今日已推 %s）",
                        res.color, state.get("last_pushed_color"))
        elif res.color == "LOW":
            logger.info("🟡 LOW：只写 JSON，今日决策台横幅读取，不推飞书")
        else:
            logger.info("🟢 NONE：不弹不推，仅写 JSON")

    state["last_color"] = res.color
    state["scans"].append({"at": now.strftime("%H:%M"), "color": res.color,
                           "composite": res.composite, "scan": scan})
    _save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
