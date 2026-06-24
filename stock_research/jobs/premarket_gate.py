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
  python3 -m stock_research.jobs.premarket_gate --force      # 演练：无视窗口计算，但不写生产/不推送
  python3 -m stock_research.jobs.premarket_gate --force --push-production  # 盘前窗口内手动写真推送
  python3 -m stock_research.jobs.premarket_gate --force --push-production --allow-outside-window-production
      # 极少数人工应急：允许窗口外写真推送

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

try:  # 盘前可买/别追名单（自选+推荐+瓶颈宇宙 套 buy_zone）；失败不拖垮闸门
    from stock_research.core import premarket_buy_signals as pbs  # noqa: E402
except Exception:  # pragma: no cover - 防御
    pbs = None

logger = logging.getLogger(__name__)

OUT_JSON = _REPO / "data" / "latest" / "premarket_gate.json"
STATE_FILE = _REPO / "data" / "premarket_gate_state.json"
DEFENSE_STATE_FILE = _REPO / "data" / "defense_watcher_state.json"
HISTORY_FILE = _REPO / "data" / "premarket_gate_history.json"
REAL_HOLDING_REVIEW_JSON = _REPO / "data" / "latest" / "real_holding_review.json"

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


def _update_history(res: pg.GateResult, now: datetime) -> None:
    """历史台账：回填过去日期的真实结果 + upsert 今天的预警。

    每天一行,取当天最严重档位。第二天用真实涨跌结算对错(score_outcome)。
    """
    records: list[dict] = []
    if HISTORY_FILE.exists():
        try:
            records = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("读 history 失败: %s", e)
            records = []

    today = now.date().isoformat()

    # 1) 回填过去未结算的日期（用真实 SPY/QQQ 涨跌判对错）
    before = {r.get("date"): r.get("outcome") for r in records}
    records, _ = pg.settle_history_records(records, now=now)
    for r in records:
        if before.get(r.get("date")) or not r.get("outcome"):
            continue
        mv = r.get("actual") or {}
        logger.info("结算 %s: %s → %s（SPY %s%% / NQ %s%%）", r["date"],
                    r.get("color"), r["outcome"], mv.get("spy_pct"), mv.get("nq_pct"))

    # 2) upsert 今天（取当天最严重档）
    rec = next((r for r in records if r.get("date") == today), None)
    if rec is None:
        rec = {"date": today, "color": "NONE", "composite": 0.0, "scans": []}
        records.append(rec)
    if pg.SEVERITY_ORDER.get(res.color, 0) >= pg.SEVERITY_ORDER.get(rec.get("color", "NONE"), 0):
        rec["color"] = res.color
        rec["composite"] = res.composite
        rec["pressure_sources"] = res.pressure_sources
        rec["top_alarm"] = res.top_alarm
        rec["can_buy"] = res.can_buy
        rec["headline_plain"] = res.headline_plain   # 当晚结论(人话)
        rec["reasons_plain"] = res.reasons_plain      # 当晚报的全部理由(可回看验证)
        rec["coverage"] = res.coverage                # 当晚数据覆盖率(低则该降权)
        rec["is_tailwind"] = res.is_tailwind          # 当晚是否判「顺风」(给顺风验证用)
        rec["tailwind_score"] = res.tailwind_score
        # 🔴 当晚每类信号的结构化数值快照 —— 点位数据,过时不可恢复,必须当场存。
        # 用于将来校准:逐信号分析谁真能预测、要不要重新加权/换阈值重打分。
        rec["families"] = res.families
        # 当晚观察到的 VIX —— 给"只看VIX"基准对照用
        _vol = next((f for f in res.families if f.get("key") == "vol"), {})
        _vix = (_vol.get("data") or {}).get("vix")
        if _vix is not None:
            rec["vix"] = _vix
    rec.setdefault("scans", []).append(
        {"at": now.strftime("%H:%M"), "color": res.color, "composite": res.composite})

    records.sort(key=lambda r: r.get("date", ""))
    HISTORY_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("history 已更新（%d 天台账）", len(records))


# ──────────────────────────────────────────────────
# 窗口守卫
# ──────────────────────────────────────────────────

def _us_open_beijing(now: datetime) -> datetime | None:
    """今天美股开盘(09:30 ET)对应的北京时间（naive）。

    用 zoneinfo 自动处理夏令时/冬令时：
      夏令时(EDT) → 北京 21:30 开盘；冬令时(EST) → 北京 22:30 开盘。
    北京傍晚对应同一日历日的美东上午，故直接用 now.date()。
    """
    try:
        from zoneinfo import ZoneInfo
        et = datetime(now.year, now.month, now.day, 9, 30, tzinfo=ZoneInfo("America/New_York"))
        return et.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    except Exception as e:
        logger.warning("时区计算失败，退回固定窗口: %s", e)
        return None


def _market_phase(now: datetime) -> str | None:
    """now 处在哪个阶段：'pre'=盘前窗口 / 'post'=盘后第一小时跟踪窗口 / None=不在任何窗口。

    delta_min = 开盘北京时间 - now（正=开盘前，负=开盘后）。
      盘前窗口  = 开盘前 5 ~ 95 分钟
      盘后窗口  = 开盘后 5 ~ 65 分钟（覆盖 +10/+20/+30/+40/+50/+60 六个跟踪点）
    """
    open_bj = _us_open_beijing(now)
    if open_bj is None:
        return "pre" if 19 <= now.hour <= 23 else None  # 时区库不可用的粗兜底
    delta_min = (open_bj - now).total_seconds() / 60.0
    if 5 <= delta_min <= 95:
        return "pre"
    if -65 <= delta_min <= -5:
        return "post"
    return None


def _is_valid_window(now: datetime) -> tuple[bool, str]:
    """是否在有效扫描窗口（盘前 或 盘后第一小时）。周末/美股假日跳过；夏/冬令时自适配。"""
    d = now.date()
    if now.weekday() >= 5:
        return False, f"{d} 是周末，美股休市"
    if d.isoformat() in US_HOLIDAYS_2026:
        return False, f"{d} 是美股假日，休市"
    open_bj = _us_open_beijing(now)
    if open_bj is None:
        return (19 <= now.hour <= 23), "盘前窗口(固定兜底)"
    delta_min = (open_bj - now).total_seconds() / 60.0
    phase = _market_phase(now)
    if phase == "pre":
        return True, f"盘前窗口（美股 {open_bj:%H:%M} 开盘，距开盘 {delta_min:.0f} 分）"
    if phase == "post":
        return True, f"盘后跟踪窗口（美股 {open_bj:%H:%M} 已开盘 {-delta_min:.0f} 分）"
    return False, f"不在扫描窗口（美股 {open_bj:%H:%M} 开盘，距开盘 {delta_min:.0f} 分）"


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

    phase_txt = "盘后跟踪" if scan_label.startswith("开盘后") else "开盘前"
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"🚦 美股{phase_txt} · 今晚能不能买 · {now_str}"},
                "subtitle": {"tag": "plain_text", "content": f"{scan_label}"},
                "template": pg.TEMPLATE.get(res.color, "grey"),
            },
            "elements": elements,
        },
    }


def _build_downgrade_card(res: pg.GateResult, scan_label: str, previous_color: str) -> dict:
    """风险降级/解除卡。真钱场景里，解除警报也要主动告诉用户。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    prev_icon = pg.ICON.get(previous_color, "⚪")
    curr_icon = pg.ICON.get(res.color, "⚪")
    content = (
        f"### {curr_icon} 盘前风险已从 {prev_icon}{previous_color} 降到 {curr_icon}{res.color}\n\n"
        f"**现在怎么做**：{res.can_buy}\n\n"
        "这不是追涨信号，只表示前一轮警报的压力有所缓解；仍按你的仓位纪律和买前研究执行。"
    )
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"🚦 盘前风险降级 · {now_str}"},
                "subtitle": {"tag": "plain_text", "content": scan_label},
                "template": pg.TEMPLATE.get(res.color, "blue"),
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": "仅供参考，不是投资建议。"}]},
            ],
        },
    }


# 大盘中期趋势（defense_watcher）—— 并进盘前卡，省得用户对照三张卡
_SEV_ORDER = {"NONE": 0, "LOW": 1, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


def _defense_summary() -> dict:
    """大盘中期趋势：档位取 defense_watcher 已落库的权威值(用户看到的那个)，
    原因用轻量版 check_market_regime(不跑慢的宏观/期权)现算一句人话。失败给 NONE。
    """
    severity = "NONE"
    try:
        st = json.loads(DEFENSE_STATE_FILE.read_text(encoding="utf-8"))
        severity = str(st.get("last_severity") or "NONE").upper()
    except Exception:
        pass
    reason = None
    try:
        from stock_research.core import defense_signals
        alerts = defense_signals.check_market_regime(include_macro=False, include_options=False)
        alerts = sorted(alerts, key=lambda a: _SEV_ORDER.get(str(a.get("severity", "")).upper(), 0),
                        reverse=True)
        if alerts:
            top = alerts[0]
            t = str(top.get("type", ""))
            if "TREND_BREAK" in t:
                reason = "SPY 跌破 200 日均线（中期趋势转弱）"
            elif "PANIC" in t:
                reason = f"VIX 偏高（{top.get('vix_close', '恐慌')}）"
            elif "MACRO" in t:
                reason = "宏观面转弱"
            else:
                reason = str(top.get("trigger") or "")[:40]
    except Exception as e:
        logger.warning("defense 摘要失败(不影响盘前卡): %s", str(e)[:80])
    return {"severity": severity, "reason": reason}


def _synthesize(env_color: str, def_sev: str) -> str:
    """把「今晚开盘环境」和「大盘中期趋势」合成一句人话结论。"""
    e = _SEV_ORDER.get(str(env_color).upper(), 0)
    d = _SEV_ORDER.get(str(def_sev).upper(), 0)
    if e <= 0 and d <= 0:
        return "短期、中期都平稳，可按计划研究/操作。"
    if e <= 1 and d >= 2:
        return "今晚开盘环境平稳，但大盘中期偏防守（已破位）→ 可研究/小仓，别激进加仓追高。"
    if e >= 2 and d <= 1:
        return "今晚开盘有扰动，但大盘中期还健康 → 想动等开盘 30-60 分钟企稳再看。"
    if e >= 2 and d >= 2:
        return "短期、中期都偏防守 → 以守为主，原则上别开新仓。"
    return "短期或中期有轻微留意 → 控制仓位、别追高。"


def _build_daily_briefing_card(res: pg.GateResult, scan_label: str,
                               buy_signals: dict | None,
                               defense: dict | None = None) -> dict:
    """每交易日盘前主动推的「盘前一句话」：环境灯 + 🟢可买 / 🔴别追名单。

    与 _build_card（出事才报警的风险卡）分开：这张每天都推一次，正常日也推。
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    bs = buy_signals or {}
    green = bs.get("green") or []
    red = bs.get("red") or []

    # 1) 今晚开盘环境（大字）
    head = f"### 今晚开盘环境：{res.headline_plain}\n\n**怎么做**：{res.can_buy}"
    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content": head}}]

    # 1.5) 大盘中期趋势（defense_watcher）+ 综合一句话 —— 一张卡看懂短期+中期
    ds = defense or {}
    dsev = str(ds.get("severity") or "NONE").upper()
    dicon = pg.ICON.get(dsev, "⚪")
    dline = f"{dicon} 大盘中期趋势：**{dsev}**"
    if ds.get("reason"):
        dline += f" — {ds['reason']}"
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": dline}})
    syn = _synthesize(res.color, dsev)
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"🧭 **综合**：{syn}"}})

    # 2) 🟢 现在偏便宜（可研究）
    elements.append({"tag": "hr"})
    if green:
        body = "\n".join(f"• {g['line']}" for g in green[:8])
        more = f"\n…另有 {len(green) - 8} 只" if len(green) > 8 else ""
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": "**🟢 现在偏便宜（可研究，不是叫你买）**\n" + body + more}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": "**🟢 现在偏便宜**：今日名单为空（票池里没有现价跌破可买区间下沿的）"}})

    # 3) 🔴 偏贵别追
    if red:
        body = "\n".join(f"• {r['line']}" for r in red[:6])
        more = f"\n…另有 {len(red) - 6} 只" if len(red) > 6 else ""
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": "**🔴 偏贵别追**\n" + body + more}})

    # 4) 脚注：覆盖率诚实交代 + 免责
    uni = bs.get("universe_size", 0)
    zoned = bs.get("zoned", 0)
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": (
        f"📖 可买区间 = 分析师目标价打 7~8.5 折（留安全边际）；现价低于下沿=偏便宜、高于上沿=偏贵。"
        f"环境灯看「今晚适不适合开新仓」。名单池 自选+推荐+瓶颈 共 {uni} 只，其中 {zoned} 只有区间数据"
        f"（目标价覆盖有限，名单可能偏短）。⚠️ 研究参考，不是投资建议；真钱决策你来定。"
    )}]})

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"🌅 今日盘前一句话 · {now_str}"},
                "subtitle": {"tag": "plain_text", "content": f"{scan_label} · 开盘前最终一扫"},
                "template": pg.TEMPLATE.get(res.color, "blue"),
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

def _load_real_holdings_from_review_snapshot(path: Path = REAL_HOLDING_REVIEW_JSON) -> list[dict]:
    """Fallback: read latest advisory holding review snapshot when DuckDB is locked.

    It is read-only and never writes holdings/watchlist/model portfolios.
    """
    if not path.exists():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("持仓快照读取失败: %s", exc)
        return []
    out: list[dict] = []
    for item in doc.get("items") or []:
        sym = str(item.get("symbol") or item.get("code") or "").strip().upper()
        if not sym:
            continue
        out.append({
            "symbol": sym,
            "code": sym,
            "market": item.get("market") or "US",
            "shares": item.get("shares") or item.get("remaining_shares"),
            "source": "real_holding_review_snapshot",
        })
    if out:
        logger.warning("使用 real_holding_review 快照兜底读取真实持仓（%d 只）", len(out))
    return out


def _load_real_holdings() -> list[dict]:
    try:
        import stock_db  # type: ignore
    except Exception as e:
        logger.warning("持仓读取跳过: %s", e)
        return _load_real_holdings_from_review_snapshot()
    try:
        rows = stock_db.fetch_all_real_holdings()
        if rows:
            return rows
        logger.warning("real_holdings 为空，尝试使用最新持仓体检快照兜底")
        return _load_real_holdings_from_review_snapshot()
    except Exception as e:
        logger.warning("fetch_all_real_holdings 失败: %s", e)
        return _load_real_holdings_from_review_snapshot()


# ──────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────

def _scan_label(now: datetime) -> str:
    """按"距开盘还有多久"命名，夏/冬令时通用。盘后按"开盘后多少分"命名。"""
    open_bj = _us_open_beijing(now)
    if open_bj is None:
        return "盘前"
    delta = (open_bj - now).total_seconds() / 60.0
    if delta >= 0:  # 盘前
        if delta > 60:
            return "初扫"
        if delta > 25:
            return "数据后复扫"
        return "开盘前最终"
    # 盘后：四舍五入到最近的 10 分钟刻度
    mins = int(round(-delta / 10.0) * 10) or 10
    return f"开盘后{mins}分"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="美股盘前风险闸门")
    p.add_argument("--dry-run", action="store_true", help="只算只打印，绝不写生产/不推送")
    p.add_argument("--force", action="store_true",
                   help="无视窗口/state 强制计算（测试用，默认不写生产 JSON、不推飞书）")
    p.add_argument("--push-production", action="store_true",
                   help="配合 --force：明确允许写生产 JSON/state/history 并按档位推送（慎用）")
    p.add_argument("--allow-outside-window-production", action="store_true",
                   help="极少数人工应急：允许 --force --push-production 在非盘前窗口写生产")
    args = p.parse_args()

    now = datetime.now()
    # 保险丝：--force 默认是"演练"，不碰生产、不推送；要真写真推必须显式 --push-production。
    test_mode = args.force and not args.push_production
    do_production = (not args.dry_run) and (not test_mode)

    ok_window, why = _is_valid_window(now)
    if not ok_window and not (args.force or args.dry_run):
        logger.info("跳过：%s（--force 可强制跑）", why)
        return 0
    if args.force and args.push_production and not ok_window and not args.allow_outside_window_production:
        logger.error("拒绝窗口外写真推送：%s。若确认为人工应急，需显式加 --allow-outside-window-production", why)
        return 2

    # 防近邻重复：launchd 夏/冬令时各排了点，季内可能有两个点挨得很近(如 21:10/21:15)。
    # 同一天若上一次扫描在 18 分钟内，跳过这一次（--force 例外）。
    if not (args.force or args.dry_run):
        _st = _load_state()
        if _st.get("date") == now.date().isoformat():
            _scans = _st.get("scans", [])
            if _scans:
                try:
                    last_hm = _scans[-1].get("at", "")
                    lh, lm = (int(x) for x in last_hm.split(":"))
                    gap = (now.hour * 60 + now.minute) - (lh * 60 + lm)
                    # 盘前同季可能两个 launchd 点挨 5 分钟（需去重）；盘后是 10 分钟一扫（必须保留）。
                    # 阈值取 8 分钟：5 分钟近邻仍去重，10 分钟盘后节奏不误杀。
                    if 0 <= gap < 8:
                        logger.info("跳过：距上次扫描仅 %d 分钟（避免近邻重复）", gap)
                        return 0
                except Exception:
                    pass

    holdings = _load_real_holdings()
    logger.info("拉行情 + %d 只真实持仓...", len(holdings))
    res = pg.compute_gate(now=now, holdings=holdings)
    scan = _scan_label(now)

    # 盘前可买/别追名单（自选+推荐+瓶颈宇宙 套 buy_zone）；失败给空名单，绝不拖垮闸门
    buy_signals: dict = {"green": [], "red": [], "universe_size": 0, "zoned": 0}
    if pbs is not None:
        try:
            buy_signals = pbs.compute_buy_avoid(today=now.date())
        except Exception as e:
            logger.warning("可买名单计算失败（不影响闸门）: %s", e)

    # 大盘中期趋势（defense_watcher）—— 并进盘前卡 + 首页抽屉，省得对照三张卡
    defense = _defense_summary()

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
    print(f"🟢 可研究 {len(buy_signals['green'])} 只 / 🔴 别追 {len(buy_signals['red'])} 只"
          f"（票池 {buy_signals.get('universe_size', 0)} 只，有区间 {buy_signals.get('zoned', 0)} 只）")
    for g in buy_signals["green"][:8]:
        print(f"  🟢 {g['symbol']}({'/'.join(g['sources'])}) {g.get('current')} ∈ "
              f"[{g.get('low')},{g.get('high')}]")

    if not do_production:
        tag = "--dry-run" if args.dry_run else "--force 测试模式（未带 --push-production）"
        print(f"\n[{tag}] 演练：不写生产 JSON / state / history，不推送飞书")
        return 0

    # 写唯一事实源
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = res.to_dict()
    payload["scan_label"] = scan
    payload["phase"] = _market_phase(now) or "pre"  # pre=盘前 / post=盘后跟踪
    payload["buy_signals"] = buy_signals  # 首页盘前预警横幅 + 日报卡共用
    payload["defense"] = defense          # 大盘中期趋势（首页抽屉显示）
    payload["synthesis"] = _synthesize(res.color, defense.get("severity", "NONE"))
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("已写 %s", OUT_JSON.relative_to(_REPO))

    # 历史台账（回填昨天之前的真实结果 + 记今天）
    try:
        _update_history(res, now)
    except Exception as e:
        logger.warning("history 更新失败: %s", e)

    # 分级推送 + 防重复
    state = _load_state()
    today = now.date().isoformat()
    if state.get("date") != today:
        state = {"date": today, "last_pushed_color": "NONE", "last_color": "NONE", "scans": []}

    curr_rank = pg.SEVERITY_ORDER.get(res.color, 0)
    pushed_rank = pg.SEVERITY_ORDER.get(state.get("last_pushed_color", "NONE"), 0)

    # 只有橙/红才报警；同日升档才再报警。
    # 若已推过橙/红，后续明显降级也推一次"解除/降级"，否则用户只收到坏消息，收不到风险缓解。
    should_push = curr_rank >= pg.SEVERITY_ORDER["HIGH"] and curr_rank > pushed_rank
    should_push_downgrade = (
        pushed_rank >= pg.SEVERITY_ORDER["HIGH"]
        and curr_rank < pushed_rank
    )

    if should_push:
        logger.info("🚦 推送飞书：%s（%s）", res.color, scan)
        ok = _push(_build_card(res, scan))
        state["last_pushed_color"] = res.color
        state["last_push_at"] = now.isoformat(timespec="seconds")
        state["last_push_ok"] = ok
        state["last_push_kind"] = "warning"
    elif should_push_downgrade:
        prev = state.get("last_pushed_color", "NONE")
        logger.info("🚦 推送风险降级：%s → %s（%s）", prev, res.color, scan)
        ok = _push(_build_downgrade_card(res, scan, prev))
        state["last_pushed_color"] = res.color
        state["last_push_at"] = now.isoformat(timespec="seconds")
        state["last_push_ok"] = ok
        state["last_push_kind"] = "downgrade"
    else:
        if curr_rank >= pg.SEVERITY_ORDER["HIGH"]:
            logger.info("橙/红但同档已推过，不重复轰炸（%s，今日已推 %s）",
                        res.color, state.get("last_pushed_color"))
        elif res.color == "LOW":
            logger.info("🟡 LOW：只写 JSON，今日决策台横幅读取，不推飞书")
        else:
            logger.info("🟢 NONE：不弹不推，仅写 JSON")

    # 每交易日「盘前一句话」日报卡：只在开盘前最终一扫推一次（正常日也推），
    # 与上面的橙/红报警分开 —— 用户要的是每天一个明确的积极/消极信号 + 名单。
    if scan == "开盘前最终" and state.get("briefing_date") != today:
        logger.info("🌅 推送盘前日报卡（%s，🟢%d/🔴%d）",
                    res.color, len(buy_signals["green"]), len(buy_signals["red"]))
        ok_brief = _push(_build_daily_briefing_card(res, scan, buy_signals, defense))
        state["briefing_date"] = today
        state["briefing_push_ok"] = ok_brief

    state["last_color"] = res.color
    state["scans"].append({"at": now.strftime("%H:%M"), "color": res.color,
                           "composite": res.composite, "scan": scan})
    _save_state(state)

    # 财报领先信号提醒：瓶颈订单组(GEV/VRT/MU) + 云大厂capex组(MSFT/GOOGL/AMZN/META)
    # 独立于闸门颜色：到财报窗口就提醒，每家每季最多一次；失败不影响闸门。
    try:
        from stock_research.jobs.bottleneck_earnings_reminder import run as _bottleneck_reminder
        _bottleneck_reminder(now=now)
    except Exception as e:
        logger.warning("瓶颈财报提醒失败（不影响闸门）: %s", e)

    # 财报前 1 周预告：自选股 earnings_upcoming 提前一周飞书提醒，每家每季最多一次。
    try:
        from stock_research.jobs.earnings_preview_reminder import run as _earnings_preview
        _earnings_preview(now=now)
    except Exception as e:
        logger.warning("财报前预告失败（不影响闸门）: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
