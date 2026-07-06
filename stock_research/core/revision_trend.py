"""分析师预期修正趋势展示。

只做展示，不进打分公式。输入是 90 天内的评级/目标价事件流，输出给
daily_strict_picks.json 和严选卡渲染使用。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        v = float(value)
    except Exception:
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _event_direction(event: dict[str, Any]) -> tuple[str | None, float | None]:
    action = str(event.get("price_target_action") or event.get("action") or "").lower()
    target = _num(event.get("price_target"))
    prior = _num(event.get("prior_price_target"))
    pct = None
    if target is not None and prior is not None and prior > 0 and target != prior:
        pct = (target / prior - 1.0) * 100.0
    if "raise" in action or "up" in action or (pct is not None and pct > 0):
        return "up", pct
    if "lower" in action or "cut" in action or "down" in action or (pct is not None and pct < 0):
        return "down", pct
    return None, pct


def summarize_revision_trend(
    events: Iterable[dict[str, Any]],
    *,
    as_of: date | None = None,
    window_days: int = 90,
) -> dict[str, Any]:
    """把 90 天分析师目标价/评级事件汇总成人话方向。"""
    as_of = as_of or date.today()
    start = as_of.toordinal() - window_days
    n_up = 0
    n_down = 0
    pct_changes: list[float] = []
    latest: date | None = None

    for event in events:
        d = _parse_date(event.get("event_date") or event.get("date"))
        if d is None or d.toordinal() < start or d > as_of:
            continue
        latest = d if latest is None or d > latest else latest
        direction, pct = _event_direction(event)
        if direction == "up":
            n_up += 1
        elif direction == "down":
            n_down += 1
        if pct is not None:
            pct_changes.append(pct)

    net = round(sum(pct_changes) / len(pct_changes), 1) if pct_changes else None
    if n_up == 0 and n_down == 0:
        direction = "平静"
    elif n_up >= n_down + 2 and (net is None or net > 0):
        direction = "上调中"
    elif n_down >= n_up + 2 and (net is None or net < 0):
        direction = "下调中"
    elif n_up > n_down and (net is None or net >= 0):
        direction = "上调中"
    elif n_down > n_up and (net is None or net <= 0):
        direction = "下调中"
    else:
        direction = "分歧"

    return {
        "direction": direction,
        "n_up": n_up,
        "n_down": n_down,
        "net_target_change_pct": net,
        "latest_date": latest.isoformat() if latest else None,
        "window_days": window_days,
    }


def format_revision_line(summary: dict[str, Any]) -> str:
    direction = str(summary.get("direction") or "平静")
    n_up = int(summary.get("n_up") or 0)
    n_down = int(summary.get("n_down") or 0)
    net = summary.get("net_target_change_pct")
    if n_up == 0 and n_down == 0:
        return "📈 分析师风向：90 天无修正记录"
    suffix = ""
    if isinstance(net, (int, float)):
        suffix = f" · 目标价净 {net:+.1f}%"
    return f"📈 分析师风向：90 天 {n_up} 家上调 / {n_down} 家下调 · {direction}{suffix}"
