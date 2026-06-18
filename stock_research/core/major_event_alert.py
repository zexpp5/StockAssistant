"""统一「重大事件」红色警报 —— 收敛多路信号，只在真·重大(🔴)时响。

背景（2026-06-18）：大盘防御🔴 / 盘前🔴 / 持仓日内暴跌 / 财报雷 现在散在各自的
飞书卡里，没有一条"最高级别、平时绝不打扰、一响就是真有事"的统一红色警报。本模块
是聚合判定的单一来源（纯函数，可单测）：把各源已算好的 severity 收敛，只有达到
🔴 阈值才算重大事件；并用指纹去重，避免同一事件反复轰炸。

severity 沿用全系统口径 NONE/LOW/HIGH/CRITICAL（不发明新体系）。默认红线 = CRITICAL。
安全边界：纯计算，不读 DB / 不推送 / 不写文件（job 层做这些）。研究/风控提示，非交易指令。
"""
from __future__ import annotations

from typing import Any

SEVERITY_ORDER = {"NONE": 0, "LOW": 1, "HIGH": 2, "CRITICAL": 3}
ICON = {"NONE": "🟢", "LOW": "🟡", "HIGH": "🟠", "CRITICAL": "🔴"}
DEFAULT_THRESHOLD = "CRITICAL"


def _order(sev: Any) -> int:
    return SEVERITY_ORDER.get(str(sev or "").upper(), 0)


def _norm_sev(sev: Any) -> str:
    s = str(sev or "").upper()
    return s if s in SEVERITY_ORDER else "NONE"


def aggregate_major_alert(
    signals: list[dict[str, Any]],
    prev_state: dict[str, Any] | None = None,
    *,
    threshold: str = DEFAULT_THRESHOLD,
    opportunities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """收敛多路信号 → 双向提醒：🔴重大风险 + 🟢重大机会。只在"新"出现时推。

    signals: 风险类 [{source, severity, headline, detail?, key?}, ...]（≥threshold 才算重大）。
    opportunities: 机会类 [{source, headline, key, detail?}, ...]（如 跌进可买区的票）。
    prev_state: {risk_fp:[...], opp_fp:[...], is_active:bool}（指纹去重 + 判恢复）。
    返回：is_major / has_opportunities / is_active / major_events / opportunities /
    severity / headline / should_push / recovered / state。
    """
    prev_state = prev_state or {}
    thr = _order(threshold)

    norm: list[dict[str, Any]] = []
    for s in signals or []:
        if not str(s.get("source") or "").strip():
            continue
        sev = _norm_sev(s.get("severity"))
        norm.append({
            "source": str(s.get("source")), "severity": sev,
            "headline": str(s.get("headline") or ""), "detail": str(s.get("detail") or ""),
            "key": str(s.get("key") or f"{s.get('source')}:{sev}"),
        })

    opps: list[dict[str, Any]] = []
    for o in opportunities or []:
        if not str(o.get("headline") or o.get("key") or "").strip():
            continue
        opps.append({
            "source": str(o.get("source") or "机会"),
            "headline": str(o.get("headline") or ""), "detail": str(o.get("detail") or ""),
            "key": str(o.get("key") or o.get("headline")),
        })

    max_sev = "NONE"
    for s in norm:
        if _order(s["severity"]) > _order(max_sev):
            max_sev = s["severity"]

    major_events = [s for s in norm if _order(s["severity"]) >= thr]
    is_major = bool(major_events)
    has_opp = bool(opps)
    is_active = is_major or has_opp

    risk_fp = sorted({s["key"] for s in major_events})
    opp_fp = sorted({o["key"] for o in opps})
    prev_risk = sorted(prev_state.get("risk_fp") or prev_state.get("fingerprint") or [])
    prev_opp = sorted(prev_state.get("opp_fp") or [])
    prev_active = bool(prev_state.get("is_active") if "is_active" in prev_state
                       else prev_state.get("is_major"))

    # 出现"新"风险或"新"机会才推；同一批指纹不重复轰炸（平时绝不打扰）。
    should_push = (is_major and risk_fp != prev_risk) or (has_opp and opp_fp != prev_opp)
    recovered = (not is_active) and prev_active

    if is_major and has_opp:
        headline = f"🔴 风险 {len(major_events)} 项 ＋ 💡 机会 {len(opps)} 项"
    elif is_major:
        srcs = "、".join(dict.fromkeys(s["source"] for s in major_events))
        headline = f"🔴 重大风险 · {len(major_events)} 项 · {srcs}"
    elif has_opp:
        headline = f"💡 机会提醒 · {len(opps)} 项（跌进可买区等）"
    elif recovered:
        headline = "🟢 已恢复常态 · 风险/机会均已解除"
    else:
        headline = f"{ICON.get(max_sev, '🟢')} 无重大事件（当前最高 {max_sev}）"

    return {
        "is_major": is_major,
        "has_opportunities": has_opp,
        "is_active": is_active,
        "severity": max_sev,
        "major_events": major_events,
        "opportunities": opps,
        "all_signals": norm,
        "headline": headline,
        "should_push": should_push,
        "recovered": recovered,
        "threshold": _norm_sev(threshold),
        "state": {"risk_fp": risk_fp, "opp_fp": opp_fp, "is_active": is_active,
                  # 兼容旧字段
                  "fingerprint": risk_fp, "is_major": is_major},
    }
