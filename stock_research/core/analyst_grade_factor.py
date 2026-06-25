"""Analyst rating-change factor shared by production and shadow replay.

The factor is point-in-time by construction: for a given as-of date, only
upgrade/downgrade events inside the trailing lookback window are counted.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

GRADE_LOOKBACK_DAYS = 30
NEUTRAL_GRADE_SCORE = 50.0


def _as_date(value: Any) -> date | None:
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


def grade_score_from_net(net_upgrades: int) -> float:
    """Map net analyst upgrades to a bounded 0-100 score.

    net=0 stays neutral at 50. Each net upgrade/downgrade moves 12.5 points,
    capped at +/-4 net events.
    """
    return max(0.0, min(100.0, NEUTRAL_GRADE_SCORE + 12.5 * float(net_upgrades)))


def fetch_grade_events(conn: Any, *, market: str = "US") -> dict[str, list[tuple[date, int]]]:
    """Return symbol -> [(event_date, sign)] from analyst_grade_events.

    Missing table/data degrades gracefully to an empty mapping.
    """
    try:
        rows = conn.execute(
            """
            SELECT symbol, event_date,
                   CASE WHEN lower(coalesce(action,''))='upgrade' THEN 1 ELSE -1 END AS sign
            FROM analyst_grade_events
            WHERE market = ?
              AND lower(coalesce(action,'')) IN ('upgrade','downgrade')
            """,
            [market],
        ).fetchall()
    except Exception:
        return {}

    events: dict[str, list[tuple[date, int]]] = defaultdict(list)
    for symbol, event_date, sign in rows:
        d = _as_date(event_date)
        if not symbol or d is None:
            continue
        events[str(symbol).upper()].append((d, int(sign)))
    return dict(events)


def score_symbol_from_events(
    events: dict[str, list[tuple[date, int]]],
    symbol: str,
    as_of: date | str,
    *,
    lookback_days: int = GRADE_LOOKBACK_DAYS,
) -> float:
    """Compute a PIT grade score for one symbol."""
    asof = _as_date(as_of)
    if asof is None:
        return NEUTRAL_GRADE_SCORE
    start = asof - timedelta(days=lookback_days)
    net = sum(
        sign
        for event_date, sign in events.get(str(symbol).upper(), [])
        if start < event_date <= asof
    )
    return grade_score_from_net(net)


def build_grade_score_map(
    conn: Any,
    symbols: list[str] | set[str] | tuple[str, ...],
    as_of: date | str,
    *,
    market: str = "US",
    lookback_days: int = GRADE_LOOKBACK_DAYS,
) -> dict[str, float]:
    """Build a neutral-filled PIT grade score map for a symbol set."""
    events = fetch_grade_events(conn, market=market)
    return {
        str(symbol).upper(): score_symbol_from_events(
            events, str(symbol), as_of, lookback_days=lookback_days
        )
        for symbol in symbols
    }
