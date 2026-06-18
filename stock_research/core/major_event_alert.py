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
) -> dict[str, Any]:
    """收敛多路信号 → 是否重大事件 + 是否该推送（升档/新事件才推）。

    signals: [{source, severity, headline, detail?, key?}, ...]
    prev_state: 上次的 {fingerprint:[...], is_major:bool}（去重 + 判恢复）。
    返回 dict：is_major / severity / major_events / all_signals / headline /
    should_push / recovered / state（新的待持久化状态）。
    """
    prev_state = prev_state or {}
    thr = _order(threshold)

    norm: list[dict[str, Any]] = []
    for s in signals or []:
        sev = _norm_sev(s.get("severity"))
        if not str(s.get("source") or "").strip():
            continue
        norm.append({
            "source": str(s.get("source")),
            "severity": sev,
            "headline": str(s.get("headline") or ""),
            "detail": str(s.get("detail") or ""),
            "key": str(s.get("key") or f"{s.get('source')}:{sev}"),
        })

    max_sev = "NONE"
    for s in norm:
        if _order(s["severity"]) > _order(max_sev):
            max_sev = s["severity"]

    major_events = [s for s in norm if _order(s["severity"]) >= thr]
    is_major = bool(major_events)

    fingerprint = sorted({s["key"] for s in major_events})
    prev_fp = sorted(prev_state.get("fingerprint") or [])
    prev_major = bool(prev_state.get("is_major"))

    # 升档/出现新重大事件才推；同一批指纹不重复轰炸（平时绝不打扰）。
    should_push = is_major and (fingerprint != prev_fp)
    # 从重大恢复到平静：推一条"已解除"（只在真的从 major→非 major 时）。
    recovered = (not is_major) and prev_major

    if is_major:
        srcs = "、".join(dict.fromkeys(s["source"] for s in major_events))
        headline = f"🔴 重大事件 · {len(major_events)} 项 · {srcs}"
    elif recovered:
        headline = "🟢 重大警报已解除 · 恢复常态"
    else:
        headline = f"{ICON.get(max_sev, '🟢')} 无重大事件（当前最高 {max_sev}）"

    return {
        "is_major": is_major,
        "severity": max_sev,
        "major_events": major_events,
        "all_signals": norm,
        "headline": headline,
        "should_push": should_push,
        "recovered": recovered,
        "threshold": _norm_sev(threshold),
        "state": {"fingerprint": fingerprint, "is_major": is_major},
    }
