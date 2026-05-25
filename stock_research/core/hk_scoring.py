"""Shared Hong Kong watchlist scoring helpers.

This module is intentionally pure: both the production HK picker and the real
holdings review call the same functions, so dashboard fallback paths cannot
invent a second rating system.
"""
from __future__ import annotations

from typing import Any, Iterable


HK_FACTOR_WEIGHTS = {
    "f_score": 0.40,
    "momentum": 0.35,
    "reversal": 0.25,
    "south_flow": 0.00,
}

HK_STRONG_THRESHOLD = 0.75
HK_RECOMMEND_THRESHOLD = 0.60


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and value == value


def winsorize_rank(values: list[float | None]) -> list[float | None]:
    """[1%, 99%] winsorize + percent-rank to [0, 1]. Missing stays None."""
    valid = sorted([v for v in values if _is_number(v)])
    n = len(valid)
    if n < 4:
        return [0.5 if _is_number(v) else None for v in values]
    lo_idx = max(0, int(0.01 * (n - 1)))
    hi_idx = min(n - 1, int(0.99 * (n - 1)))
    lo, hi = valid[lo_idx], valid[hi_idx]
    pool = sorted(max(lo, min(hi, v)) for v in valid)
    pn = len(pool)
    out: list[float | None] = []
    for v in values:
        if not _is_number(v):
            out.append(None)
            continue
        clipped = max(lo, min(hi, v))
        below = sum(1 for x in pool if x < clipped)
        eq = sum(1 for x in pool if x == clipped)
        out.append((below + 0.5 * eq) / pn)
    return out


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(q * (len(s) - 1))
    return s[idx]


def hk_grade_label(entry: Any) -> str:
    composite = float(getattr(entry, "composite", 0.0) or 0.0)
    if composite >= HK_STRONG_THRESHOLD:
        return "⭐⭐⭐ 强烈推荐（综合 ≥0.75）"
    if composite >= HK_RECOMMEND_THRESHOLD:
        return "⭐⭐ 推荐（综合 ≥0.60）"
    return "⭐ 关注"


def apply_sector_cap(entries: list[Any], cutoff: float, top_k: int,
                     max_sector_frac: float = 0.35) -> tuple[list[Any], list[str]]:
    max_per_sector = max(1, int(top_k * max_sector_frac))
    selected: list[Any] = []
    counts: dict[str, int] = {}
    skipped: list[str] = []
    eligible = [
        e for e in entries
        if getattr(e, "data_quality", "") != "fail"
        and float(getattr(e, "composite", 0.0) or 0.0) >= cutoff
    ]
    for e in eligible:
        sector = getattr(e, "sector", "") or "未分类"
        if counts.get(sector, 0) >= max_per_sector:
            skipped.append(f"{getattr(e, 'code', '?')}({sector})")
            continue
        selected.append(e)
        counts[sector] = counts.get(sector, 0) + 1
        if len(selected) >= top_k:
            break
    return selected, skipped


def score_hk_entries(
    entries: Iterable[Any],
    *,
    mode: str = "tertile",
    top_k: int = 12,
    factor_weights: dict[str, float] | None = None,
) -> tuple[list[Any], list[Any], float, list[str]]:
    """Mutate and rank HK entries using the production HK picker formula.

    Returns (entries, selected, cutoff, sector_skipped).
    """
    entries = list(entries)
    weights = dict(factor_weights or HK_FACTOR_WEIGHTS)

    mom_ranks = winsorize_rank([getattr(e, "momentum_12_1", None) for e in entries])
    rev_ranks = winsorize_rank([getattr(e, "reversal_1m", None) for e in entries])
    for i, e in enumerate(entries):
        setattr(e, "momentum_norm", mom_ranks[i])
        setattr(e, "reversal_norm", rev_ranks[i])

    active = {k: w for k, w in weights.items() if w > 0}
    total_w = sum(active.values()) or 1.0
    for e in entries:
        factors = {
            "f_score": getattr(e, "f_score_norm", None),
            "momentum": getattr(e, "momentum_norm", None),
            "reversal": getattr(e, "reversal_norm", None),
            "south_flow": getattr(e, "south_score", None) if weights.get("south_flow", 0) > 0 else None,
        }
        covered_w = sum(w for k, w in active.items() if factors.get(k) is not None)
        missing = [k for k in active if factors.get(k) is None]
        setattr(e, "coverage_score", round(covered_w / total_w, 4))
        setattr(e, "missing_factors", ",".join(missing))
        if getattr(e, "data_quality", "") == "fail" or covered_w <= 0:
            setattr(e, "composite", -0.25)
            continue
        raw = sum(active[k] * float(factors[k]) for k in active if factors.get(k) is not None) / covered_w
        penalty = max(0.0, 0.50 - float(getattr(e, "coverage_score", 0.0))) / 0.50 * 0.25
        setattr(e, "composite", round(raw * float(getattr(e, "coverage_score", 0.0)) - penalty, 4))

    entries.sort(key=lambda e: -float(getattr(e, "composite", 0.0) or 0.0))
    valid_composites = [
        float(getattr(e, "composite", 0.0) or 0.0)
        for e in entries
        if getattr(e, "data_quality", "") != "fail"
    ]
    cutoff_map = {
        "quartile": quantile(valid_composites, 0.75),
        "tertile": quantile(valid_composites, 2 / 3),
        "median": quantile(valid_composites, 0.50),
    }
    cutoff = cutoff_map.get(mode, cutoff_map["tertile"]) if valid_composites else 0.0
    for i, e in enumerate(entries, 1):
        setattr(e, "rank", i)
        setattr(e, "recommended", False)

    selected, sector_skipped = apply_sector_cap(entries, cutoff, top_k)
    for e in selected:
        setattr(e, "recommended", True)
    return entries, selected, cutoff, sector_skipped
