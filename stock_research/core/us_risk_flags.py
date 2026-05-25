"""US equity soft risk flags from Altman Z / Beneish M (shared by picks + holdings review)."""
from __future__ import annotations

from typing import Any

ALTMAN_Z_DISTRESS = 1.81
BENEISH_M_HIGH = -1.78


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_us_equity_risk_flags(
    altman: dict | None,
    beneish: dict | None,
) -> list[str]:
    """Altman Z / Beneish M soft red flags (annotate only, do not filter out).

    Beneish uses m_score_adjusted when present (growth-adjusted). When altman/beneish
    contain ``error`` (e.g. HK / missing FMP), returns an empty list.
    """
    flags: list[str] = []
    if altman and not altman.get("error"):
        z = _as_float(altman.get("z_score"))
        if z is not None and z < ALTMAN_Z_DISTRESS:
            flags.append(f"🚨 Altman Z={z:.2f}<{ALTMAN_Z_DISTRESS} 破产警示")
    if beneish and not beneish.get("error") and beneish.get("risk_level") == "high":
        m_adj = _as_float(beneish.get("m_score_adjusted"))
        if m_adj is not None:
            flags.append(f"🚨 Beneish M={m_adj:.2f}>{BENEISH_M_HIGH} 造假风险")
    return flags


def build_us_equity_risk_flags_from_fundamental(row: dict | None) -> list[str]:
    """Same as :func:`build_us_equity_risk_flags` for ``factor_scores_today`` row shape."""
    row = row or {}
    altman = row.get("altman") if isinstance(row.get("altman"), dict) else None
    beneish = row.get("beneish") if isinstance(row.get("beneish"), dict) else None
    return build_us_equity_risk_flags(altman, beneish)
