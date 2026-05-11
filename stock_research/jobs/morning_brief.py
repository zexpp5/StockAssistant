"""每日早安简报（主入口）。

把已有 7 个数据源拼成一份 5 section 的 markdown，让用户每天 8:30 看完
一份就知道："今天能不能动手、买什么、AI 说对了么、有什么红旗、要做什么"。

数据源（全部已经在跑，本脚本只做拼装，不产生新数据）：
  - plan_a_v5_constrained.json | plan_a_v5.json     -> 当前建议组合
  - trade_delta.json                                 -> 本周调仓 delta
  - risk_metrics.json                                -> 历史风险指标 + NAV 时序
  - data/snapshots/audit/realtime_defense_*.json     -> regime gate（最新一份）
  - data/event_calendar.json                         -> A 股事件
  - factor_scores_today.json                         -> 美股因子打分（数据新鲜度用）
  - data/a_share_picks.json                          -> A 股选股（盘后才有）

输出：
  - data/reports/morning_brief_YYYY-MM-DD.md         -> 持久存档
  - morning_brief.md（根目录）                       -> 最新一份，方便快速打开

可选飞书推送（任一配置就激活，优先级 lark-cli > webhook）：
  # 方案 A: hermes lark-cli（推荐 — 已经登录的话零配置）
  export FEISHU_BRIEF_USER_ID='ou_xxx'   # 收件人 open_id（自己发给自己最简单）
  export FEISHU_BRIEF_CHAT_ID='oc_xxx'   # 或：发到指定群（与 USER_ID 二选一）
  # 方案 B: 群机器人 webhook（无 lark-cli 时 fallback）
  export FEISHU_BRIEF_WEBHOOK='https://open.feishu.cn/open-apis/bot/v2/hook/XXX'
"""
from __future__ import annotations
import json
import logging
import os
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any

import requests

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────
# 数据读取（每个数据源都允许缺失，缺什么 brief 显示什么）
# ────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"读取 {path} 失败: {e}")
        return None


def _latest_defense_snapshot() -> dict | None:
    """读最新的 realtime_defense_*.json（按文件名时间戳排序）。"""
    snap_dir = REPO / "data" / "snapshots" / "audit"
    if not snap_dir.exists():
        return None
    files = sorted(snap_dir.glob("realtime_defense_*.json"))
    if not files:
        return None
    return _load_json(files[-1])


def _is_a_share(ticker: str) -> bool:
    """A 股 / 港股识别（与 apply_a_share_constraints.py 对齐）。"""
    t = ticker.upper()
    return t.endswith((".SS", ".SZ", ".BJ", ".HK"))


# ────────────────────────────────────────────────────────
# Sparkline helpers — 把 60d 价格画成 Unicode 缩略线
# ────────────────────────────────────────────────────────

_SPARK_BARS = "▁▂▃▄▅▆▇█"


def _load_history() -> dict:
    """读 history_data.json 的 tickers map，缺则返回空 dict。"""
    d = _load_json(REPO / "history_data.json")
    if not isinstance(d, dict):
        return {}
    return d.get("tickers") or {}


def _sparkline(values: list[float], length: int = 10) -> str:
    """价格序列降采样到 length 个点，渲染成 8 级 Unicode 缩略线。"""
    if not values or len(values) < 2:
        return "—"
    n = len(values)
    if n > length:
        step = n / length
        sampled = [values[min(n - 1, int(i * step))] for i in range(length)]
    else:
        sampled = list(values)
    lo, hi = min(sampled), max(sampled)
    if hi == lo:
        return _SPARK_BARS[3] * len(sampled)
    span = hi - lo
    return "".join(_SPARK_BARS[min(7, int((v - lo) / span * 7))] for v in sampled)


def _ticker_sparkline(history: dict, ticker: str, window: int = 60) -> tuple[str, float | None]:
    """返回 (sparkline, window 天涨跌%)。缺数据时返回 ('—', None)。"""
    if not history or ticker not in history:
        return "—", None
    closes = history[ticker].get("close") or []
    if len(closes) < 2:
        return "—", None
    recent = closes[-window:]
    pct = ((recent[-1] - recent[0]) / recent[0] * 100) if recent[0] else None
    return _sparkline(recent, length=10), pct


# ────────────────────────────────────────────────────────
# Section 0: 今天 / 3 天内会发生什么（持仓 earnings + 高相关政策）
# ────────────────────────────────────────────────────────

def section_calendar(plan: dict | None) -> str:
    """读 event_calendar.json + policy_events.json，挑出今天到 +3d 关键事件。

    - 持仓 earnings：仅列持仓股（A 股）next 3d 内的财报日
    - 政策事件：relevance_score ≥ 4 的近 3d 政策
    - 都空则返回 ""（整 section 不显示，避免占版面）
    """
    events_data = _load_json(REPO / "data" / "event_calendar.json")
    policy_data = _load_json(REPO / "data" / "policy_events.json")
    today = date.today()
    horizon = today + timedelta(days=3)

    held_codes: set[str] = set()
    if plan:
        for e in (plan.get("plan_v5") or []):
            t = e.get("ticker", "")
            if _is_a_share(t):
                held_codes.add(t.split(".")[0])

    earnings: list[tuple[date, dict]] = []
    for ev in (events_data or {}).get("events", []) or []:
        try:
            ed = datetime.strptime(ev.get("event_date", ""), "%Y-%m-%d").date()
        except Exception:
            continue
        if today <= ed <= horizon and ev.get("code", "") in held_codes:
            earnings.append((ed, ev))

    policies: list[tuple[date, dict]] = []
    for ev in (policy_data or {}).get("events", []) or []:
        if (ev.get("relevance_score") or 0) < 4:
            continue
        try:
            ed = datetime.strptime(ev.get("date", ""), "%Y-%m-%d").date()
        except Exception:
            continue
        if today <= ed <= horizon:
            policies.append((ed, ev))

    if not earnings and not policies:
        return ""

    def _day_label(d: date) -> str:
        if d == today:
            return "🔥今天"
        if d == today + timedelta(days=1):
            return "明天"
        return d.strftime("%m-%d")

    lines = ["#### 0. 今天 / 3 天内会发生什么"]
    if earnings:
        lines.append(f"📅 **持仓事件**（{len(earnings)} 个 earnings）")
        for ed, ev in sorted(earnings)[:5]:
            lines.append(f"• {_day_label(ed)} · {ev.get('code')} · {ev.get('description','')[:60]}")
    if policies:
        if earnings:
            lines.append("")
        lines.append(f"📰 **高相关政策**（{len(policies)} 条 · relevance≥4）")
        for ed, ev in sorted(policies, key=lambda x: (-int(x[1].get('relevance_score') or 0), x[0]))[:3]:
            themes = "/".join(ev.get('matched_themes') or [])
            theme_str = f" · {themes}" if themes else ""
            lines.append(f"• {_day_label(ed)}{theme_str} · {ev.get('title','')[:60]}")
    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────
# Section 1: regime gate — 今天能不能动手
# ────────────────────────────────────────────────────────

def section_regime(defense: dict | None) -> str:
    """读 realtime_defense 输出，告诉用户 regime 状态。

    realtime_defense 输出 schema:
      severity: NONE / LOW / MEDIUM / HIGH
      summary:  人类可读摘要（"🟢 无警报" 等）
      alerts:   告警列表
    """
    if not defense:
        return (
            "#### 1. 今天能不能动手？\n"
            "⚠️ 未找到 realtime_defense 输出 — **保守起见今天按已有计划执行，不要加仓**。\n"
        )

    severity = defense.get("severity", "UNKNOWN")
    summary = defense.get("summary", "")
    alerts = defense.get("alerts", []) or []

    # severity 档位与 stock_research/core/defense_signals.py:201 对齐
    # （NONE / LOW / HIGH / CRITICAL 4 档，CRITICAL 最严重）
    icon_map = {"NONE": "🟢", "LOW": "🟡", "HIGH": "🟠", "CRITICAL": "🔴"}
    icon = icon_map.get(severity, "⚪")

    advice = {
        "NONE": "👉 **今天可以正常调仓**（v7 三道闸门都没亮灯）。",
        "LOW": "👉 **留意但别加仓**，单笔不超 5% 仓位。",
        "HIGH": "👉 **减仓 30-50%，停止买入**，可换防御标的（KO / MCD 等）。",
        "CRITICAL": "👉 **清仓 sit out** — 崩盘期历史 alpha = -9.77%，等信号转回 LOW 再回来。",
    }.get(severity, "👉 保守按已有计划执行。")

    lines = [
        "#### 1. 今天能不能动手？（v7 防御层信号）",
        f"{icon} **{severity}** — {summary}",
        advice,
        "",
        "📖 灯色对照：🟢 NONE 正常 ｜ 🟡 LOW 留意别加仓 ｜ 🟠 HIGH 减仓 30-50% ｜ 🔴 CRITICAL 清仓 sit out",
        "🛡️ v7 三道闸门：VIX 飙高 / 跌破 200 日均线 / 单股 -15% 止损 — 任意一道触发就升级灯色。",
    ]
    if alerts:
        lines.append("")
        lines.append("**具体告警：**")
        for a in alerts[:5]:
            kind = a.get("kind") or a.get("type") or "alert"
            # fallback 顺序：人类可读 message/detail > suggested_action > trigger > 最后才 json
            msg = (a.get("message") or a.get("detail") or a.get("suggested_action")
                   or a.get("trigger") or json.dumps(a, ensure_ascii=False))
            lines.append(f"• {kind} · {msg}")
    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────
# Section 2: 今天的候选（A 股 + 美股）
# ────────────────────────────────────────────────────────

def _humanize_picks(plan: list[dict], a_share: bool, history: dict | None = None) -> list[str]:
    """把 plan_v5 entry 排成一句话/只。ticker 加粗、不用反引号；附 60d sparkline。"""
    out = []
    for entry in plan:
        ticker = entry.get("ticker", "?")
        if a_share and not _is_a_share(ticker):
            continue
        if not a_share and _is_a_share(ticker):
            continue
        weight = entry.get("v5_weight") or entry.get("weight") or 0
        f_score = entry.get("f_score", "?")
        z = entry.get("composite_z", entry.get("composite", 0))
        spark, pct60 = _ticker_sparkline(history or {}, ticker)
        spark_str = f" · {spark}" + (f" {pct60:+.1f}% 60d" if pct60 is not None else "")
        out.append(
            f"• **{ticker}** {weight*100:.1f}% · F-Score {f_score} · 综合 {z:+.2f}{spark_str}"
        )
    return out


def section_picks(plan: dict | None, a_share_picks: dict | None, history: dict | None = None) -> str:
    """美股从 plan_v5 取，A 股优先从 a_share_picks 取（盘后），否则也从 plan_v5。"""
    if not plan:
        return (
            "#### 2. 建议组合\n"
            "⚠️ 未找到 plan_a_v5 — 先跑 `python3 build_plan_a_v5.py`。\n"
        )

    plan_v5 = plan.get("plan_v5") or []
    pm = plan.get("portfolio_metrics") or {}

    head = "#### 2. 建议组合"
    if pm:
        head += (
            f"  ·  Sharpe {pm.get('annual_sharpe', '?')} · "
            f"年化 {pm.get('annual_return_pct', '?')}% · "
            f"波动 {pm.get('annual_vol_pct', '?')}%"
        )
    lines = [head]

    # 美股
    us_lines = _humanize_picks(plan_v5, a_share=False, history=history)
    if us_lines:
        lines.append(f"**🇺🇸 美股 ({len(us_lines)} 只)**")
        lines.extend(us_lines)

    # A 股：盘后有 a_share_picks 就用，否则用 plan_v5 里的 A 股
    if a_share_picks and a_share_picks.get("selected"):
        sel = a_share_picks["selected"][:10]
        lines.append(f"**🇨🇳 A 股 ({len(sel)} 只 · 6 因子 + 盘后龙虎榜+北向)**")
        for entry in sel:
            ticker = entry.get("ticker", entry.get("code", "?"))
            name = entry.get("name", "")
            score = entry.get("composite", 0)
            spark, pct60 = _ticker_sparkline(history or {}, ticker)
            spark_str = f" · {spark}" + (f" {pct60:+.1f}% 60d" if pct60 is not None else "")
            lines.append(f"• **{ticker}** {name} · 综合 {score:.3f}{spark_str}")
    else:
        a_lines = _humanize_picks(plan_v5, a_share=True, history=history)
        if a_lines:
            lines.append(f"**🇨🇳 A 股 ({len(a_lines)} 只 · 盘前数据，16:30 后更准)**")
            lines.extend(a_lines)
    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────
# Section 3: AI alpha 跟踪
# ────────────────────────────────────────────────────────

def section_ai_alpha(risk_metrics: dict | None) -> str:
    """系统同时跑两个方案（A 静态 vs C 动态），让用户一眼看懂"AI 到底有没有用"。

    TODO（4 周后）：切到 build_stock_dashboard_html.compute_plan_forward_track
    + compute_dynamic_rebalance_track 的真实 forward 数据。当前 tracked=0 / rebalance=0。
    """
    today = date.today()
    inception_date = date(2026, 5, 10)
    days_tracked = max(0, (today - inception_date).days - 1)

    lines = [
        "#### 3. 系统在跑两个方案 · 看 AI 到底有没有用",
        "_系统每周一同时跑两套策略，让数据自然分胜负_",
        "",
        "**📦 方案 A · 静态死守**：5-10 锁定 12 只股，从此不动（佛系基准）",
        "**🔄 方案 C · 动态调仓**：每周一按 AI 重新优化（扣 10bps/换股 手续费）",
        "",
    ]
    if days_tracked < 7:
        lines.append(f"📅 **Forward tracking 累积中**：已 {days_tracked} / 7 天（第一周还没结束）")
        lines.append("🆚 等下周起每周一较量 → **C − A spread** 就是 AI 加的 alpha")
    else:
        lines.append(f"📅 已 forward tracked {days_tracked} 天 — 真实数据见 dashboard")

    if risk_metrics:
        rm = risk_metrics
        lines.append("")
        lines.append("_历史回测参考（不代表未来；崩盘期实测 alpha = **-9.77%**）_")
        lines.append(
            f"Sharpe **{rm.get('sharpe', '?')}** · "
            f"MaxDD **{rm.get('max_drawdown_pct', '?')}%** · "
            f"95% VaR {rm.get('var_95_pct', '?')}%"
        )

    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────
# Section 4: 红旗
# ────────────────────────────────────────────────────────

def section_red_flags(
    plan: dict | None,
    events: dict | None,
    factor_scores: dict | None,
    defense: dict | None,
) -> str:
    """汇总 4 类红旗：数据陈旧 / 事件迫近 / 持仓异常 / regime 告警。

    无红旗时返回空字符串，整 section 不显示（避免占空间）。
    """
    flags: list[str] = []

    # 1. 数据陈旧
    if factor_scores:
        try:
            scored_date = factor_scores.get("date")
            if scored_date:
                d = datetime.strptime(scored_date, "%Y-%m-%d").date()
                age = (date.today() - d).days
                if age >= 2:
                    flags.append(
                        f"⚠️ **因子分数过期** — factor_scores_today 是 {scored_date}（{age} 天前）"
                    )
        except Exception:
            pass

    # 2. 持仓里 3 天内的事件
    if plan and events:
        held_tickers = {e.get("ticker", "") for e in (plan.get("plan_v5") or [])}
        held_codes = {t.split(".")[0] for t in held_tickers if _is_a_share(t)}
        today_d = date.today()
        soon = today_d + timedelta(days=3)
        urgent_events = []
        for ev in (events.get("events") or []):
            code = ev.get("code", "")
            if code not in held_codes:
                continue
            try:
                ev_d = datetime.strptime(ev.get("event_date", ""), "%Y-%m-%d").date()
            except Exception:
                continue
            if today_d <= ev_d <= soon:
                urgent_events.append(ev)
        if urgent_events:
            flags.append(f"⚠️ **持仓 3 天内有 {len(urgent_events)} 个事件：**")
            for ev in urgent_events[:5]:
                flags.append(
                    f"• {ev.get('code')} {ev.get('event_date')} · "
                    f"{ev.get('event_type')} · {ev.get('description', '')[:60]}"
                )

    # 3. regime 告警重复一遍（HIGH/CRITICAL 时才上红旗，对齐 defense_signals 4 档）
    if defense and defense.get("severity") in ("HIGH", "CRITICAL"):
        flags.append(
            f"⚠️ **regime = {defense.get('severity')}** — 见 section 1 详情。"
        )

    if not flags:
        return ""  # 无红旗时整 section 不显示

    return "#### 4. 红旗\n" + "\n".join(flags) + "\n"


# ────────────────────────────────────────────────────────
# Section 5: 今天必须做的动作
# ────────────────────────────────────────────────────────

def section_actions(trade_delta: dict | None, defense: dict | None) -> str:
    """0-3 条具体动作。原则：越少越好；多了说明系统在乱报。"""
    lines = ["#### 5. 今天必须做的动作"]
    actions: list[str] = []

    weekday = date.today().weekday()  # 0 = 周一
    is_monday = (weekday == 0)

    # regime CRITICAL = 任何动作都让位（HIGH 时仅减仓不加仓，不阻断 rebalance 卖出）
    sev = defense.get("severity") if defense else None
    if sev == "CRITICAL":
        actions.append("🔴 **regime = CRITICAL，今天清仓 sit out**，等信号转回 LOW 再回来")
        lines.extend(actions)
        return "\n".join(lines) + "\n"
    if sev == "HIGH":
        actions.append('🟠 **regime = HIGH：今天只减仓不加仓**（rebalance 的「卖」照做，「买」暂停）')

    # 周一 rebalance
    if is_monday and trade_delta:
        sells = trade_delta.get("sells") or []
        buys = trade_delta.get("buys") or []
        if sells or buys:
            actions.append(f"**周一 rebalance**：卖 {len(sells)} 只 / 买 {len(buys)} 只")
            for s in sells[:3]:
                actions.append(
                    f"• 卖 **{s.get('ticker')}** {s.get('name','')} "
                    f"(当前 {s.get('current_weight', 0)*100:.0f}%)"
                )
            for b in buys[:3]:
                actions.append(
                    f"• 买 **{b.get('ticker')}** "
                    f"目标 {b.get('v6_weight', 0)*100:.0f}% (≈¥{b.get('amount_rmb', 0):.0f})"
                )
            if len(sells) + len(buys) > 6:
                actions.append("• … 完整清单见 trade_delta.json")

    if not actions:
        actions.append("无 — 系统建议你今天不动手")

    lines.extend(actions)
    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────
# 头部 + 拼装
# ────────────────────────────────────────────────────────

def build_brief(share_mode: bool = False) -> str:
    """读所有数据源 + 拼装 markdown，返回 brief 文本。

    Args:
      share_mode: True 时为"共享版" — section 5 替换为脱敏提示。
                  当前默认 webhook 走完整版，share_mode 留作以后切换余地。

    格式约定（适配飞书 interactive card 渲染）：
      - 不再写"# 早安 · 日期"标题（卡片 header 已包含日期）
      - section 标题用 #### (H4)，飞书不会降级渲染
      - ticker 用 **加粗** 而非反引号（反引号在飞书会留下字符）
      - section 间用空行分隔，不用 `---`（飞书会渲染成粗水平线）
      - 红旗 section 为空时整段省略
      - 免责声明放最末一行（每天看一遍即可）
    """
    plan_constrained = _load_json(REPO / "plan_a_v5_constrained.json")
    plan = plan_constrained or _load_json(REPO / "plan_a_v5.json")
    trade_delta = _load_json(REPO / "trade_delta.json")
    risk_metrics = _load_json(REPO / "risk_metrics.json")
    factor_scores = _load_json(REPO / "factor_scores_today.json")
    events = _load_json(REPO / "data" / "event_calendar.json")
    a_share_picks = _load_json(REPO / "data" / "a_share_picks.json")
    defense = _latest_defense_snapshot()
    history = _load_history()

    parts: list[str] = []
    cal = section_calendar(plan)
    if cal:
        parts.extend([cal, "\n"])
    parts.extend([
        section_regime(defense),
        "\n",
        section_picks(plan, a_share_picks, history=history),
        "\n",
        section_ai_alpha(risk_metrics),
        "\n",
    ])
    red_flags = section_red_flags(plan, events, factor_scores, defense)
    if red_flags:
        parts.append(red_flags)
        parts.append("\n")

    if not share_mode:
        parts.append(section_actions(trade_delta, defense))
    else:
        parts.append(
            "#### 5. 今天必须做的动作\n"
            "_共享版隐藏个人调仓明细。请参考自己持仓 + section 2 建议组合自行决策。_\n"
        )

    parts.append(
        "\n———\n"
        "⚠️ 不构成投资建议。崩盘期历史 alpha = -9.77%（4/4 regime 3 跑输 SPY）。"
    )
    return "".join(parts)


# ────────────────────────────────────────────────────────
# 飞书推送 — 优先 lark-cli (hermes)，回退 webhook
# ────────────────────────────────────────────────────────

def _lark_cli_send(brief: str, target_kind: str, target_id: str) -> bool:
    """单次推送：通过 lark-cli 把 brief 发到 user_id 或 chat_id。

    必须用 bot identity：user → 自己 user_id 会进 user_count=0 的孤儿 P2P，
    飞书客户端不会显示。bot identity 走"机器人 → 用户/群"路径才会正常出现。
    """
    import shutil
    import subprocess

    if shutil.which("lark-cli") is None:
        return False
    if not target_id:
        return False

    today = date.today().strftime("%Y-%m-%d")
    payload = f"📊 早安简报 · {today}\n\n{brief}"

    cmd = ["lark-cli", "--as", "bot", "im", "+messages-send", "--markdown", payload]
    if target_kind == "user":
        cmd.extend(["--user-id", target_id])
    elif target_kind == "chat":
        cmd.extend(["--chat-id", target_id])
    else:
        return False

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True
        logger.warning(
            f"lark-cli 推送失败 ({target_kind}={target_id}, rc={r.returncode}): "
            f"{r.stderr[:300]}"
        )
        return False
    except Exception as e:
        logger.warning(f"lark-cli 推送异常: {e}")
        return False


def _push_via_lark_cli(brief_personal: str, brief_shared: str) -> list[str]:
    """双通道推送：自己 P2P 完整版 + 群共享版（已脱敏）。

    Args:
      brief_personal: 完整版 brief（含 section 5 个人调仓）
      brief_shared:   共享版 brief（section 5 已脱敏，去掉 ¥ 仓位）

    Returns:
      已成功推送的通道列表，例如 ["user", "chat"]。
    """
    sent: list[str] = []
    user_id = os.environ.get("FEISHU_BRIEF_USER_ID", "").strip()
    chat_id = os.environ.get("FEISHU_BRIEF_CHAT_ID", "").strip()

    if user_id and _lark_cli_send(brief_personal, "user", user_id):
        sent.append("user")
    if chat_id and _lark_cli_send(brief_shared, "chat", chat_id):
        sent.append("chat")
    return sent


def _color_block(bg: str, elements: list[dict]) -> dict:
    """v1 schema 把任意 elements 包进一个 column_set 色块。

    bg 取 "default"（无色）/ "grey"（浅灰）/ saturated 颜色名（wathet/violet/turquoise/...）。
    v1 没有真正的 pastel 浅色，所以默认用 default 干净版；section 5 调仓建议
    可以单独用 "grey" 轻分块。
    """
    block: dict = {
        "tag": "column_set",
        "flex_mode": "none",
        "horizontal_spacing": "default",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": elements}
        ],
    }
    if bg and bg != "default":
        block["background_style"] = bg
    return block


def _kpi_row(items: list[tuple[str, str]]) -> dict:
    """3 列（或 N 列）均分横排 KPI: items 是 [(label, value), ...]。"""
    return {
        "tag": "column_set",
        "flex_mode": "stretch",
        "horizontal_spacing": "default",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": f"**{label}**\n{value}"}}],
            }
            for label, value in items
        ],
    }


def _two_col_lines(lines_left: list[str], lines_right: list[str]) -> dict:
    """美股 12 只之类长列表拆 2 列横排。"""
    return {
        "tag": "column_set",
        "flex_mode": "stretch",
        "horizontal_spacing": "default",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1,
             "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines_left)}}]},
            {"tag": "column", "width": "weighted", "weight": 1,
             "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines_right)}}]},
        ],
    }


def _build_card_payload() -> dict:
    """构造飞书 card v1 schema dict — 每个 section 上色块 + 横排 KPI + 长列表分 2 列。

    每个 section 用 column_set + background_style 包装：
      regime  → 动态色（NONE blue / LOW yellow / HIGH orange / CRITICAL red；与 defense_signals 4 档对齐）
      建议组合 → wathet 浅蓝（专业凉爽）
      AI alpha → violet 紫（数据回顾）
      红旗    → carmine 红粉（警示，仅非空时）
      调仓    → turquoise 青绿（最醒目 · 用户最关心的"今天做什么"）
    """
    plan_constrained = _load_json(REPO / "plan_a_v5_constrained.json")
    plan = plan_constrained or _load_json(REPO / "plan_a_v5.json")
    trade_delta = _load_json(REPO / "trade_delta.json")
    risk_metrics = _load_json(REPO / "risk_metrics.json")
    factor_scores = _load_json(REPO / "factor_scores_today.json")
    events = _load_json(REPO / "data" / "event_calendar.json")
    policy_events = _load_json(REPO / "data" / "policy_events.json")
    a_share_picks = _load_json(REPO / "data" / "a_share_picks.json")
    defense = _latest_defense_snapshot()
    history = _load_history()

    today = date.today()
    weekday_cn = "一二三四五六日"[today.weekday()]
    severity = (defense or {}).get("severity", "UNKNOWN")
    # 4 档与 stock_research/core/defense_signals.py:201 对齐 (NONE/LOW/HIGH/CRITICAL)
    severity_icon = {"NONE": "🟢", "LOW": "🟡", "HIGH": "🟠", "CRITICAL": "🔴"}.get(severity, "⚪")
    header_template = {"NONE": "blue", "LOW": "yellow", "HIGH": "orange", "CRITICAL": "red"}.get(severity, "grey")

    blocks: list[dict] = []

    # ─── Section 0: 今天 / 3 天内会发生什么（持仓 earnings + 高相关政策）───
    horizon = today + timedelta(days=3)
    held_codes: set[str] = set()
    if plan:
        for e in (plan.get("plan_v5") or []):
            t = e.get("ticker", "")
            if _is_a_share(t):
                held_codes.add(t.split(".")[0])
    earnings_soon: list[tuple[date, dict]] = []
    for ev in (events or {}).get("events", []) or []:
        ed_p = _safe_parse_date(ev.get("event_date", ""))
        if ed_p and today <= ed_p <= horizon and ev.get("code", "") in held_codes:
            earnings_soon.append((ed_p, ev))
    policy_soon: list[tuple[date, dict]] = []
    for ev in (policy_events or {}).get("events", []) or []:
        if (ev.get("relevance_score") or 0) < 4:
            continue
        ed_p = _safe_parse_date(ev.get("date", ""))
        if ed_p and today <= ed_p <= horizon:
            policy_soon.append((ed_p, ev))
    if earnings_soon or policy_soon:
        def _day_label(d: date) -> str:
            if d == today:
                return "🔥今天"
            if d == today + timedelta(days=1):
                return "明天"
            return d.strftime("%m-%d")
        sec0_lines: list[str] = ["**📅 今天 / 3 天内会发生什么**"]
        if earnings_soon:
            sec0_lines.append(f"**持仓事件**（{len(earnings_soon)} 个 earnings）")
            for ed, ev in sorted(earnings_soon)[:5]:
                sec0_lines.append(f"• {_day_label(ed)} · {ev.get('code')} · {ev.get('description','')[:60]}")
        if policy_soon:
            if earnings_soon:
                sec0_lines.append("")
            sec0_lines.append(f"**高相关政策**（{len(policy_soon)} 条 · relevance≥4）")
            for ed, ev in sorted(policy_soon, key=lambda x: (-int(x[1].get('relevance_score') or 0), x[0]))[:3]:
                themes = "/".join(ev.get('matched_themes') or [])
                theme_str = f" · {themes}" if themes else ""
                sec0_lines.append(f"• {_day_label(ed)}{theme_str} · {ev.get('title','')[:60]}")
        blocks.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(sec0_lines)}})
        blocks.append({"tag": "hr"})

    # ─── Section 1: regime（白底；自带新人能看懂的灯色对照 + 三道闸门解释）───
    advice = {
        "NONE": "👉 **今天可以正常调仓**（v7 三道闸门都没亮灯）",
        "LOW": "👉 **留意但别加仓**，单笔不超 5% 仓位",
        "HIGH": "👉 **减仓 30-50%，停止买入**，可换防御标的（KO / MCD 等）",
        "CRITICAL": "👉 **清仓 sit out** — 崩盘期 alpha = -9.77%，等灯转回 LOW 再回来",
    }.get(severity, "👉 保守按已有计划执行")
    blocks.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": (
            f"{severity_icon} **regime = {severity}** — {advice}\n\n"
            "📖 **灯色对照**：🟢 NONE 正常 ｜ 🟡 LOW 留意别加仓 ｜ 🟠 HIGH 减仓 30-50% ｜ 🔴 CRITICAL 清仓 sit out\n"
            "🛡️ **v7 三道闸门**（什么时候升级灯色）：VIX 恐慌指数飙高 / 大盘跌破 200 日均线 / 单股亏 -15% 自动止损"
        )}
    })
    blocks.append({"tag": "hr"})

    # ─── Section 2: 建议组合（白底）───
    if plan:
        section2: list[dict] = [
            {"tag": "div", "text": {"tag": "lark_md", "content": "**📦 今天的建议组合**"}}
        ]
        pm = plan.get("portfolio_metrics") or {}
        if pm:
            section2.append(_kpi_row([
                ("Sharpe", str(pm.get("annual_sharpe", "?"))),
                ("年化", f"{pm.get('annual_return_pct', '?')}%"),
                ("波动", f"{pm.get('annual_vol_pct', '?')}%"),
            ]))
        plan_v5 = plan.get("plan_v5") or []
        us_lines = _humanize_picks(plan_v5, a_share=False, history=history)
        if us_lines:
            section2.append({"tag": "div", "text": {"tag": "lark_md",
                "content": f"**🇺🇸 美股 ({len(us_lines)} 只)**"}})
            half = (len(us_lines) + 1) // 2
            section2.append(_two_col_lines(us_lines[:half], us_lines[half:]))
        if a_share_picks and a_share_picks.get("selected"):
            sel = a_share_picks["selected"][:10]
            a_lines = []
            for e in sel:
                t = e.get("ticker", e.get("code", "?"))
                spark, pct60 = _ticker_sparkline(history or {}, t)
                spark_str = f" · {spark}" + (f" {pct60:+.1f}% 60d" if pct60 is not None else "")
                a_lines.append(
                    f"• **{t}** {e.get('name','')} · {e.get('composite', 0):.2f}{spark_str}"
                )
            section2.append({"tag": "div", "text": {"tag": "lark_md",
                "content": f"**🇨🇳 A 股 ({len(sel)} 只 · 6 因子 + 龙虎榜+北向)**"}})
            if len(a_lines) >= 4:
                half_a = (len(a_lines) + 1) // 2
                section2.append(_two_col_lines(a_lines[:half_a], a_lines[half_a:]))
            else:
                section2.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(a_lines)}})
        else:
            a_lines_pre = _humanize_picks(plan_v5, a_share=True, history=history)
            if a_lines_pre:
                section2.append({"tag": "div", "text": {"tag": "lark_md",
                    "content": f"**🇨🇳 A 股 ({len(a_lines_pre)} 只 · 盘前 · 16:30 后更准)**"}})
                if len(a_lines_pre) >= 4:
                    half_a = (len(a_lines_pre) + 1) // 2
                    section2.append(_two_col_lines(a_lines_pre[:half_a], a_lines_pre[half_a:]))
                else:
                    section2.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(a_lines_pre)}})
        blocks.extend(section2)
        blocks.append({"tag": "hr"})

    # ─── Section 3: 两个方案对比（核心 — 让新人一眼看懂"AI 有没有用"）───
    inception_date = date(2026, 5, 10)
    days_tracked = max(0, (today - inception_date).days - 1)

    section3: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content":
            "**🆚 系统在跑两个方案**\n_每周一同时跑两套策略，让数据自然分胜负_"}}
    ]
    # 2 列横向对比卡：A 静态 vs C 动态
    section3.append({
        "tag": "column_set",
        "flex_mode": "stretch",
        "horizontal_spacing": "default",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1,
             "elements": [{"tag": "div", "text": {"tag": "lark_md", "content":
                "**📦 方案 A · 静态死守**\n5-10 锁定 12 只股\n从此不调仓\n_模拟「佛系投资者」_"}}]},
            {"tag": "column", "width": "weighted", "weight": 1,
             "elements": [{"tag": "div", "text": {"tag": "lark_md", "content":
                "**🔄 方案 C · 动态调仓**\n每周一按 AI rebalance\n扣 10bps/换股 手续费\n_模拟「听 AI 调仓」_"}}]},
        ],
    })
    if days_tracked < 7:
        section3.append({"tag": "div", "text": {"tag": "lark_md", "content":
            f"📅 **Forward tracking 累积中**：已 {days_tracked} / 7 天 — 等下周起每周一较量\n"
            f"🆚 **C − A spread = AI 加的 alpha**（等数据累积）"}})
    else:
        section3.append({"tag": "div", "text": {"tag": "lark_md", "content":
            f"📅 已 forward tracked {days_tracked} 天 — 真实曲线见 dashboard"}})

    if risk_metrics:
        rm = risk_metrics
        section3.append({"tag": "div", "text": {"tag": "lark_md", "content":
            "_历史回测参考（仅参考，不代表未来）_"}})
        section3.append(_kpi_row([
            ("回测 Sharpe", str(rm.get("sharpe", "?"))),
            ("Max DD", f"{rm.get('max_drawdown_pct', '?')}%"),
            ("95% VaR", f"{rm.get('var_95_pct', '?')}%"),
        ]))
        section3.append({"tag": "note", "elements": [
            {"tag": "plain_text", "content": "回测含 survivorship bias 不代表未来；崩盘期实测 alpha = -9.77%。"}
        ]})
    blocks.extend(section3)

    # ─── Section 4: 红旗（仅非空时，唯一例外用色块强警示）───
    flags: list[str] = []
    if factor_scores:
        try:
            scored_date = factor_scores.get("date")
            if scored_date:
                d = datetime.strptime(scored_date, "%Y-%m-%d").date()
                age = (today - d).days
                if age >= 2:
                    flags.append(f"⚠️ 因子分数过期 {age} 天（{scored_date}）")
        except Exception:
            pass
    if plan and events:
        held_tickers = {e.get("ticker", "") for e in (plan.get("plan_v5") or [])}
        held_codes = {t.split(".")[0] for t in held_tickers if _is_a_share(t)}
        soon = today + timedelta(days=3)
        urgent = [
            ev for ev in (events.get("events") or [])
            if ev.get("code", "") in held_codes
            and (_safe_parse_date(ev.get("event_date", "")) is not None
                 and today <= _safe_parse_date(ev.get("event_date", "")) <= soon)
        ]
        if urgent:
            flags.append(f"⚠️ 持仓 3 天内有 {len(urgent)} 个事件")
    if defense and severity in ("HIGH", "CRITICAL"):
        flags.append(f"⚠️ regime = {severity}（见上方）")
    if flags:
        blocks.append({"tag": "hr"})
        blocks.append(_color_block("carmine", [
            {"tag": "div", "text": {"tag": "lark_md",
                "content": "**🚨 红旗**\n" + "\n".join(f"• {f}" for f in flags)}}
        ]))

    blocks.append({"tag": "hr"})

    # ─── Section 5: 今天必须做的动作（卖/买左右两列）───
    is_monday = (today.weekday() == 0)
    if severity == "HIGH":
        blocks.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                "content": "**✅ 今天必须做的动作**\n🔴 **regime HIGH，今天不交易**，等系统恢复 NORMAL"}
        })
    elif is_monday and trade_delta and ((trade_delta.get("sells") or []) or (trade_delta.get("buys") or [])):
        sells = trade_delta.get("sells") or []
        buys = trade_delta.get("buys") or []
        blocks.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                "content": f"**✅ 今天必须做的动作**\n**周一 rebalance**：卖 {len(sells)} 只 / 买 {len(buys)} 只"}
        })
        sell_lines: list[str] = []
        for s in sells:
            name = s.get("name", "")
            name_str = f" {name}" if name and name != s.get("ticker", "") else ""
            sell_lines.append(
                f"• 卖 **{s.get('ticker')}**{name_str} ({s.get('current_weight', 0)*100:.0f}%, ¥{s.get('current_amount', 0):.0f})"
            )
        buy_lines: list[str] = []
        for b in buys:
            buy_lines.append(
                f"• 买 **{b.get('ticker')}** {b.get('v6_weight', 0)*100:.1f}% (≈¥{b.get('amount_rmb', 0):.0f})"
            )
        # 卖在左、买在右；行数不同时短的一边自动留白
        blocks.append(_two_col_lines(sell_lines or ["• 无卖出"], buy_lines or ["• 无买入"]))
    else:
        blocks.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**✅ 今天必须做的动作**\n无 — 系统建议你今天不动手"}
        })

    # ─── 底部注脚 ───
    blocks.append({"tag": "note", "elements": [
        {"tag": "plain_text", "content": "⚠️ 不构成投资建议 · 崩盘期历史 alpha = -9.77%（4/4 regime 3 跑输 SPY）"}
    ]})

    n_picks = len(plan.get("plan_v5") or []) if plan else 0
    n_actions = (len(trade_delta.get("sells", [])) + len(trade_delta.get("buys", []))) if trade_delta else 0
    subtitle = f"regime {severity} · {n_picks} 只候选 · 调仓 {n_actions} 笔"

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"📊 早安简报 · {today} (周{weekday_cn})"},
                "subtitle": {"tag": "plain_text", "content": subtitle},
                "template": header_template,
            },
            "elements": blocks,
        },
    }


def _safe_parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def _push_via_webhook() -> bool:
    """群机器人 webhook 推送 — 走结构化 card v1 schema。

    适用于跨租户 external 群 — lark-cli bot 无法进 external 群，但自定义
    机器人 webhook 不受限制。payload 由 _build_card_payload() 独立构造，
    所以本函数不再接受 brief markdown 参数。
    """
    webhook = os.environ.get("FEISHU_BRIEF_WEBHOOK", "").strip()
    if not webhook:
        return False
    payload = _build_card_payload()
    try:
        r = requests.post(webhook, json=payload, timeout=15)
        ok = r.status_code == 200 and r.json().get("StatusCode", 0) == 0
        if not ok:
            logger.warning(f"webhook 推送返回非成功: {r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:
        logger.warning(f"webhook 推送异常: {e}")
        return False


def push_to_feishu(brief_personal: str, brief_shared: str) -> list[str]:
    """统一推送入口 — 双通道（个人 P2P + 群 webhook）。

    Returns:
      已成功推送的通道名字列表，例如 ["user", "chat", "webhook"]。
    """
    sent = _push_via_lark_cli(brief_personal, brief_shared)
    # webhook 走结构化 card payload（与 brief markdown 解耦，独立构造）
    if _push_via_webhook():
        sent.append("webhook")
    return sent


# ────────────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────────────

def main() -> int:
    brief_personal = build_brief(share_mode=False)
    brief_shared = build_brief(share_mode=True)

    today = date.today().strftime("%Y-%m-%d")
    reports_dir = REPO / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    archive_path = reports_dir / f"morning_brief_{today}.md"
    archive_path.write_text(brief_personal, encoding="utf-8")

    latest_path = REPO / "morning_brief.md"
    latest_path.write_text(brief_personal, encoding="utf-8")

    logger.info(f"✅ 早安简报已生成: {archive_path}")
    logger.info(f"   最新版镜像:    {latest_path}")
    logger.info(f"   完整版字数: {len(brief_personal)} chars · 共享版字数: {len(brief_shared)} chars")

    sent = push_to_feishu(brief_personal, brief_shared)
    if sent:
        logger.info(f"📨 已推送到飞书（{', '.join(sent)}）")
    else:
        logger.info("📨 未配置 FEISHU_BRIEF_USER_ID/CHAT_ID/WEBHOOK，跳过推送")

    return 0


if __name__ == "__main__":
    sys.exit(main())
