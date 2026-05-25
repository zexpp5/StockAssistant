"""触发式 Alert watcher — 每 15 min 跑市场层防御 + 持仓日内回撤，升档/新触发时推飞书。

设计：
  - 市场层：VIX + SPY/200MA + 宏观 + PCR。
  - 持仓层（轻量）：单票日内 ≤-5%（最近收盘 vs 前收）或组合加权日内 ≤-1.5%。
  - picks 止损仍日级，不在此重复。
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
sys.path.insert(0, str(_REPO / "scripts" / "lib"))


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(_REPO / ".env")

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


def _holding_intraday_alerts() -> list[dict]:
    """真实持仓日内波动（用 price_daily 最近两收，非 tick）。"""
    try:
        import fx_rates  # type: ignore
        import stock_db  # type: ignore
    except Exception as exc:
        logger.warning("持仓日内检查跳过: %s", exc)
        return []

    alerts: list[dict] = []
    conn = stock_db.get_db()
    try:
        holdings = stock_db.fetch_all_real_holdings(conn=conn)
        if not holdings:
            return []
        symbols = [str(h.get("symbol") or h.get("code")) for h in holdings if h.get("symbol") or h.get("code")]
        if not symbols:
            return []
        placeholders = ",".join(["?"] * len(symbols))
        rows = conn.execute(
            f"""
            WITH ranked AS (
              SELECT market, symbol, close, trade_date,
                     LAG(close) OVER (PARTITION BY market, symbol ORDER BY trade_date) AS prev_close,
                     ROW_NUMBER() OVER (PARTITION BY market, symbol ORDER BY trade_date DESC) AS rn
              FROM price_daily
              WHERE symbol IN ({placeholders}) AND interval = '1d'
            )
            SELECT symbol, close, prev_close FROM ranked WHERE rn = 1
            """,
            symbols,
        ).fetchall()
        px = {str(r[0]): (r[1], r[2]) for r in rows}

        total_val = 0.0
        weighted_chg = 0.0
        for h in holdings:
            sym = str(h.get("symbol") or h.get("code"))
            shares = float(h.get("shares") or 0)
            if shares <= 0 or sym not in px:
                continue
            close, prev = px[sym]
            if close is None or prev is None or prev <= 0:
                continue
            day_pct = (float(close) / float(prev) - 1.0) * 100.0
            ccy = h.get("currency") or fx_rates.infer_currency_from_ticker(sym)
            fx = fx_rates.get_fx_to_rmb(ccy)
            val = float(close) * shares * fx
            total_val += val
            weighted_chg += val * day_pct
            if day_pct <= -5.0:
                alerts.append({
                    "type": "holding_flash",
                    "severity": "HIGH",
                    "symbol": sym,
                    "trigger": f"日内约 {day_pct:+.1f}%（收盘口径）",
                    "suggested_action": "建议复查是否止损/减仓（advisory）",
                })
        if total_val > 0:
            port_pct = weighted_chg / total_val
            if port_pct <= -1.5:
                alerts.append({
                    "type": "portfolio_day",
                    "severity": "HIGH",
                    "symbol": "PORTFOLIO",
                    "trigger": f"持仓组合加权日内约 {port_pct:+.1f}%",
                    "suggested_action": "整体偏逆风，新开仓宜谨慎（advisory）",
                })
    finally:
        conn.close()
    return alerts


def _holding_alert_fingerprint(alerts: list[dict]) -> str:
    if not alerts:
        return ""
    parts = sorted(
        f"{a.get('symbol')}:{a.get('trigger')}" for a in alerts if a.get("symbol")
    )
    return "|".join(parts)


def _build_holding_alert_card(alerts: list[dict]) -> dict:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = ["**持仓日内告警**（收盘 vs 前收，非 tick）\n"]
    for a in alerts[:8]:
        lines.append(f"• **{a.get('symbol')}**: {a.get('trigger')} — {a.get('suggested_action', '')}")
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"💼 持仓日内 · {now_str}"},
                "subtitle": {"tag": "plain_text", "content": "advisory · 不构成交易指令"},
                "template": "orange",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
                {"tag": "note", "elements": [{
                    "tag": "plain_text",
                    "content": "单票 ≤-5% 或组合加权 ≤-1.5% 触发；与大盘橙卡分开读",
                }]},
            ],
        },
    }


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
        "LOW": (
            "👉 **大盘风控 · 留意档**（不是个股买卖单）\n"
            "市场略偏谨慎：别加仓，单笔新开仓控制在约 5% 以内。"
        ),
        "HIGH": (
            "👉 **大盘风控 · 偏高风险档**（不是个股买卖单）\n\n"
            "**市场在说什么**：触发明细里的指标（常见是 SPY 期权 Put/Call 比 PCR）"
            "显示整体看跌情绪偏强。\n\n"
            "**系统模板建议**：整体减仓约 30–50%、暂停新开仓；"
            "可考虑防御型蓝筹（如 KO、MCD）——仅作风格参考。\n\n"
            "**不要和这些混读**：① AI 推荐买哪只 ② AI 组合调仓 ③「我的持仓」里每只股的体检结论。"
        ),
        "CRITICAL": (
            "👉 **大盘风控 · 极高风险档**（不是个股买卖单）\n"
            "建议大幅降仓或观望；历史压力测试显示崩盘期策略可能明显跑输 SPY。"
            "等档位回落到 LOW 以下再考虑恢复正常仓位。"
        ),
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
            "📖 这是什么：defense_watcher 每 15 分钟扫大盘（VIX/200MA/宏观/PCR），"
            "只在档位变差时推飞书 · 不是 AI 荐股也不是持仓体检 · "
            "🟢正常 🟡留意 🟠减仓 🔴清仓 · ⚠️ 不构成投资建议"
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

    hold_alerts = _holding_intraday_alerts()
    hold_fp = _holding_alert_fingerprint(hold_alerts)
    prev_hold_fp = state.get("holding_alert_fingerprint", "")
    if hold_alerts and (args.force or hold_fp != prev_hold_fp):
        logger.info("💼 持仓日内告警 %d 条（fp 变化）", len(hold_alerts))
        ok_h = _push(_build_holding_alert_card(hold_alerts))
        state["last_holding_alert_at"] = datetime.now().isoformat(timespec="seconds")
        state["last_holding_alert_ok"] = ok_h
        state["holding_alert_fingerprint"] = hold_fp
    state["holding_alert_count"] = len(hold_alerts)

    _save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
