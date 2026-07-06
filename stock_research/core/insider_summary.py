"""SEC Form 4 内部人买卖摘要。

只做展示，不进打分公式。输入可以是 event_calendar_us_form4.json 的事件对象，
也可以是已展开的 recent_insiders 交易列表。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

DISCLAIMER = "卖出常见于报税/行权，≠看空；买入信号权重大于卖出。"


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


def _num(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        v = float(value)
    except Exception:
        return 0.0
    if v != v or v in (float("inf"), float("-inf")):
        return 0.0
    return v


def _iter_transactions(event: dict[str, Any]) -> Iterable[dict[str, Any]]:
    recent = event.get("recent_insiders")
    if isinstance(recent, list) and recent:
        for tx in recent:
            if isinstance(tx, dict):
                yield tx
        return
    yield {
        "date": event.get("event_date") or event.get("date"),
        "buy_usd": event.get("buys_usd"),
        "sell_usd": event.get("sells_usd"),
        "n_buy": event.get("n_buy_filings"),
        "n_sell": event.get("n_sell_filings"),
    }


def summarize_insider_events(
    events: Iterable[dict[str, Any]],
    *,
    as_of: date | None = None,
    window_days: int = 30,
) -> dict[str, Any]:
    as_of = as_of or date.today()
    start = as_of.toordinal() - window_days
    n_buy = 0
    n_sell = 0
    buy_usd = 0.0
    sell_usd = 0.0
    latest: date | None = None

    for event in events:
        for tx in _iter_transactions(event):
            d = _parse_date(tx.get("date") or tx.get("event_date"))
            if d is None or d.toordinal() < start or d > as_of:
                continue
            latest = d if latest is None or d > latest else latest
            buy = _num(tx.get("buy_usd") or tx.get("buys_usd"))
            sell = _num(tx.get("sell_usd") or tx.get("sells_usd"))
            tx_buy_count = int(_num(tx.get("n_buy"))) if tx.get("n_buy") is not None else (1 if buy > 0 else 0)
            tx_sell_count = int(_num(tx.get("n_sell"))) if tx.get("n_sell") is not None else (1 if sell > 0 else 0)
            n_buy += tx_buy_count
            n_sell += tx_sell_count
            buy_usd += buy
            sell_usd += sell

    if n_buy == 0 and n_sell == 0:
        net_direction = "无申报"
    elif n_buy > n_sell:
        net_direction = "净买入"
    elif n_sell > n_buy:
        net_direction = "净卖出"
    else:
        net_direction = "平静"

    return {
        "n_buy": n_buy,
        "n_sell": n_sell,
        "net_direction": net_direction,
        "latest_date": latest.isoformat() if latest else None,
        "window_days": window_days,
        "buy_usd": round(buy_usd, 2),
        "sell_usd": round(sell_usd, 2),
        "disclaimer": DISCLAIMER,
    }


def format_insider_line(summary: dict[str, Any]) -> str:
    direction = str(summary.get("net_direction") or "无申报")
    if direction == "无申报":
        return "👔 内部人：30 天无申报"
    n_buy = int(summary.get("n_buy") or 0)
    n_sell = int(summary.get("n_sell") or 0)
    return f"👔 内部人：30 天 {n_buy} 笔买入 / {n_sell} 笔卖出（{direction}）"
