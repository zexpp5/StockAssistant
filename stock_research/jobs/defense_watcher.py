"""触发式 Alert watcher — 跑市场层防御 + 持仓日内回撤，关键变化及时推飞书。

设计：
  - 市场层：VIX + SPY/200MA + 宏观 + PCR。
  - 持仓层（轻量）：单票日内 ≤-5%（最近收盘 vs 前收）或组合加权日内 ≤-1.5%。
  - picks 止损仍日级，不在此重复。
  - 状态文件 data/defense_watcher_state.json 记录上次 severity
  - 严重度排序: NONE(0) < LOW(1) < HIGH(2) < CRITICAL(3)
  - 固定每 5 分钟扫描；升档立刻推，HIGH/CRITICAL 降档也推一条恢复/降档卡
  - 推送复用 morning_brief 的 webhook（FEISHU_ALERT_WEBHOOK > FEISHU_BRIEF_WEBHOOK）

CLI:
  python3 -m stock_research.jobs.defense_watcher          # 正常跑
  python3 -m stock_research.jobs.defense_watcher --force  # 强制推送当前 severity 一次（测试用）
  python3 -m stock_research.jobs.defense_watcher --reset  # 把 state 重置为 NONE（下次升档才推）
  python3 -m stock_research.jobs.defense_watcher --dry-run --no-holdings

推荐调度：
  bash scripts/setup_defense_watcher_launchd.sh
  # 美股盘中每 5 分钟固定扫描一次。
"""
from __future__ import annotations
import argparse
import fcntl
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
LOCK_FILE = _REPO / "data" / "defense_watcher.lock"
FAST_SCAN_MINUTES = 5


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


def _acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = LOCK_FILE.open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def _holding_intraday_alerts() -> list[dict]:
    """真实持仓日内波动（用 price_daily 最近两收，非 tick）。"""
    try:
        import fx_rates  # type: ignore
        import stock_db  # type: ignore
    except Exception as exc:
        logger.warning("持仓日内检查跳过: %s", exc)
        return []

    alerts: list[dict] = []
    try:
        conn = stock_db.get_db()
    except Exception as exc:
        logger.warning("持仓日内检查跳过: DB 连接失败: %s", str(exc)[:120])
        return []
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
            SELECT symbol, close, prev_close, trade_date FROM ranked WHERE rn = 1
            """,
            symbols,
        ).fetchall()
        px = {str(r[0]): (r[1], r[2], str(r[3])[:10] if r[3] is not None else None) for r in rows}

        total_val = 0.0
        weighted_chg = 0.0
        for h in holdings:
            sym = str(h.get("symbol") or h.get("code"))
            shares = float(h.get("shares") or 0)
            if shares <= 0 or sym not in px:
                continue
            close, prev = px[sym][0], px[sym][1]
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
        alerts.extend(_discipline_line_alerts(stock_db, holdings, px))
    finally:
        conn.close()
    return alerts


def _discipline_line_alerts(stock_db, holdings: list[dict], px: dict) -> list[dict]:
    """活跃纪律计划的价位线触发告警（与 -5% 闪崩告警互补，慢跌触线也能推）。

    fingerprint 走 symbol+trigger，trigger 文案只含线位+交易日，
    同一条线同一交易日只推一次；价格放在 suggested_action 里不参与去重。
    """
    alerts: list[dict] = []
    try:
        plans = stock_db.fetch_real_holding_discipline_plans(status="active")
    except Exception as exc:
        logger.warning("纪律线检查跳过: %s", str(exc)[:120])
        return alerts
    holding_by_id = {int(h["id"]): h for h in holdings if h.get("id") is not None}
    for plan in plans:
        hid = plan.get("holding_id")
        holding = holding_by_id.get(int(hid)) if hid is not None else None
        if not holding:
            continue
        sym = str(holding.get("symbol") or holding.get("code") or "")
        if sym not in px:
            continue
        close, _prev, tdate = px[sym]
        if close is None:
            continue
        try:
            ev = stock_db.evaluate_real_holding_discipline_plan(
                plan, current_price=close, price_trade_date=tdate,
            )
        except Exception as exc:
            logger.warning("纪律线评估失败 %s: %s", sym, str(exc)[:120])
            continue
        if not ev.get("triggered"):
            continue
        sev = str(ev.get("severity") or "info").lower()
        alerts.append({
            "type": "discipline_line",
            "severity": "HIGH" if sev in {"critical", "high"} else "MEDIUM",
            "symbol": sym,
            "trigger": f"纪律线 {ev.get('threshold_text')} 触发 · {tdate or '最近收盘'}",
            "suggested_action": f"{ev.get('action_label') or '按计划执行'} · 现价 {float(close):.2f}（advisory）",
        })
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
                    "content": "单票 ≤-5% / 组合加权 ≤-1.5% / 纪律计划价位线 触发；与大盘橙卡分开读",
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


def _fetch_market_context() -> dict:
    """给告警卡补价格端上下文；失败时不影响主告警。"""
    out: dict = {"generated_at": datetime.now().isoformat(timespec="seconds"), "quotes": []}
    try:
        import yfinance as yf
    except Exception as exc:
        out["error"] = f"yfinance unavailable: {str(exc)[:80]}"
        return out

    for label, symbol in [("SPY", "SPY"), ("QQQ", "QQQ"), ("SMH", "SMH"), ("VIX", "^VIX")]:
        try:
            hist = yf.Ticker(symbol).history(period="5d", interval="1d")
            if hist is None or len(hist) == 0 or "Close" not in hist:
                continue
            closes = [float(x) for x in hist["Close"].dropna().tolist()]
            if not closes:
                continue
            last = closes[-1]
            prev = closes[-2] if len(closes) >= 2 else None
            pct = ((last / prev - 1.0) * 100.0) if prev else None
            out["quotes"].append({
                "label": label,
                "price": round(last, 2),
                "pct": round(pct, 2) if pct is not None else None,
            })
        except Exception as exc:
            out.setdefault("quote_errors", {})[label] = str(exc)[:80]
    return out


def _market_context_lines(context: dict | None, alerts: list[dict]) -> list[str]:
    lines: list[str] = []
    if not context:
        return lines
    quotes = context.get("quotes") or []
    if quotes:
        bits = []
        for q in quotes:
            pct = q.get("pct")
            pct_txt = "—" if pct is None else f"{pct:+.2f}%"
            bits.append(f"{q.get('label')} {q.get('price')} ({pct_txt})")
        lines.append("价格端：" + " · ".join(bits))

    pcr_alert = next((a for a in alerts if a.get("type") == "PUT_CALL_RATIO"), None)
    if pcr_alert:
        pcr = pcr_alert.get("pcr_volume")
        pcr_oi = pcr_alert.get("pcr_oi")
        lines.append(f"期权端：SPY PCR(volume)={pcr if pcr is not None else '—'} · PCR(OI)={pcr_oi if pcr_oi is not None else '—'}")

    quote_map = {q.get("label"): q for q in quotes}
    weak_prices = [
        label for label in ("SPY", "QQQ", "SMH")
        if (quote_map.get(label) or {}).get("pct") is not None and (quote_map.get(label) or {}).get("pct") < 0
    ]
    if pcr_alert and not weak_prices:
        lines.append("读法：期权避险升温，但价格端暂未同步转弱，先当作风险提示而不是单独交易指令。")
    elif weak_prices:
        lines.append(f"读法：价格端也有转弱迹象（{', '.join(weak_prices)}），这类告警权重要提高。")
    return lines


def _build_market_context_block(context: dict | None, alerts: list[dict]) -> dict | None:
    lines = _market_context_lines(context, alerts)
    if not lines:
        return None
    return {
        "tag": "div",
        "text": {"tag": "lark_md", "content": "**盘中上下文**\n" + "\n".join(f"• {line}" for line in lines)},
    }


def _build_alert_card(prev: str, curr: str, alerts: list[dict], context: dict | None = None) -> dict:
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
            "**怎么处理**：新开仓降速、别追高；已有仓位继续按个股价格、止损线和仓位计划处理。\n\n"
            "**不要和这些混读**：① AI 推荐买哪只 ② AI 组合调仓 ③「我的持仓」里每只股的体检结论。"
        ),
        "CRITICAL": (
            "👉 **大盘风控 · 极高风险档**（不是个股买卖单）\n"
            "先暂停追高和新大仓位，检查组合集中度、止损线和现金缓冲。"
            "它是风控提醒，不是自动清仓指令；是否卖出仍看价格端是否共振转弱。"
        ),
    }.get(curr, "")

    elements: list[dict] = [{
        "tag": "div",
        "text": {"tag": "lark_md", "content": (
            f"{prev_icon} **{prev}** → {curr_icon} **{curr}**\n\n"
            f"{advice}"
        )},
    }]

    context_block = _build_market_context_block(context, alerts)
    if context_block:
        elements.append(context_block)

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
            "📖 这是什么：defense_watcher 每 5 分钟扫大盘（VIX/200MA/宏观/PCR），"
            "不是 AI 荐股也不是持仓体检 · "
            "🟢正常 🟡留意 🟠高风险 🔴极高风险 · ⚠️ 不构成投资建议"
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


def _build_downgrade_card(prev: str, curr: str, alerts: list[dict], context: dict | None = None) -> dict:
    """降档/恢复卡：不延迟，下一次扫描看到降档就推。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    curr_icon = ICON_MAP.get(curr, "⚪")
    title = "防御信号回落" if SEVERITY_ORDER.get(curr, 0) <= SEVERITY_ORDER["LOW"] else "防御信号降档"
    lines = [
        f"✅ **{title}**：{prev} → {curr_icon} **{curr}**",
        "",
        "这不是买入信号，只是告诉你上一条高风险状态已经变化；接下来仍按个股价格、成交量和仓位计划处理。",
    ]
    if SEVERITY_ORDER.get(curr, 0) >= SEVERITY_ORDER["HIGH"]:
        lines.append("当前仍在高风险档，只是比上一档缓和。")
    else:
        lines.append("当前已不按高风险状态处理，可以回到正常盘中观察节奏。")

    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
    ]
    context_block = _build_market_context_block(context, alerts)
    if context_block:
        elements.append(context_block)
    if alerts:
        alert_lines = []
        for a in alerts[:5]:
            alert_lines.append(f"• [{a.get('severity', '')}] **{a.get('type') or a.get('name', '')}**: {a.get('trigger') or a.get('suggested_action', '')}")
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**当前仍触发**\n" + "\n".join(alert_lines)}})
    elements.append({"tag": "note", "elements": [{
        "tag": "plain_text",
        "content": "降档/恢复卡由下一次扫描立即推送，用来纠正旧红卡/橙卡造成的滞后印象。",
    }]})
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"✅ {title} · {now_str}"},
                "subtitle": {"tag": "plain_text", "content": f"{prev} → {curr}"},
                "template": TEMPLATE_MAP.get(curr, "green" if curr == "NONE" else "blue"),
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
    p.add_argument("--dry-run", action="store_true", help="打印判定，不推送/不写 state")
    p.add_argument("--no-holdings", action="store_true", help="跳过真实持仓日内检查")
    args = p.parse_args()

    lock_fh = _acquire_lock()
    if lock_fh is None:
        logger.info("已有 defense_watcher 正在运行，本轮跳过")
        return 0

    if args.reset:
        _save_state({"last_severity": "NONE", "reset_at": datetime.now().isoformat(timespec="seconds")})
        print("✅ state 已重置为 NONE")
        return 0

    state = _load_state()
    curr, alerts = _market_severity()
    context = _fetch_market_context()
    prev = state.get("last_severity", "NONE")

    curr_rank = SEVERITY_ORDER.get(curr, -1)
    prev_rank = SEVERITY_ORDER.get(prev, -1)

    push_type = "none"
    should_push = False
    if args.force or (curr_rank > prev_rank):
        should_push = True
        push_type = "escalation"
    elif curr_rank < prev_rank and prev_rank >= SEVERITY_ORDER["HIGH"]:
        should_push = True
        push_type = "downgrade"

    if args.dry_run:
        print(json.dumps({
            "previous_severity": prev,
            "current_severity": curr,
            "push_type": push_type,
            "would_push": should_push,
            "scan_interval_minutes": FAST_SCAN_MINUTES,
            "alerts": alerts,
            "context": context,
        }, ensure_ascii=False, indent=2))
        return 0

    if should_push:
        logger.info(f"🚨 推送 {push_type}：{prev} → {curr}（{len(alerts)} 条 alert）")
        if push_type == "downgrade":
            card = _build_downgrade_card(prev, curr, alerts, context=context)
        else:
            card = _build_alert_card(prev, curr, alerts, context=context)
        ok = _push(card)
        now_iso = datetime.now().isoformat(timespec="seconds")
        if push_type == "downgrade":
            state["last_recovery_at"] = now_iso
            state["last_recovery_sent_ok"] = ok
            state["last_recovery_severity"] = curr
        else:
            state["last_alert_at"] = now_iso
            state["last_alert_sent_ok"] = ok
            state["last_alert_severity"] = curr
    elif curr_rank < prev_rank:
        logger.info(f"📉 降档静默：{prev} → {curr}（仅更新 state，不推送）")
    else:
        logger.info(f"= severity 未变：{curr}（{len(alerts)} 条 alert）")

    state["last_severity"] = curr
    state["last_check_at"] = datetime.now().isoformat(timespec="seconds")
    state["last_full_check_at"] = state["last_check_at"]
    state["last_alert_count"] = len(alerts)
    state["last_push_type"] = push_type
    state["scan_interval_minutes"] = FAST_SCAN_MINUTES
    state.pop("last_schedule_reason", None)
    state.pop("next_interval_minutes", None)

    hold_alerts = [] if args.no_holdings else _holding_intraday_alerts()
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
