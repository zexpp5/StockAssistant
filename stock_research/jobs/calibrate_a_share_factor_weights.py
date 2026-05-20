"""Calibrate A-share production factor weights from market-internal IC.

This job intentionally starts with factors that can be reconstructed
point-in-time from adjusted daily prices.  Event/flow factors such as LHB,
northbound flow, PEAD, and policy themes need historical signal snapshots; until
those snapshots exist, they are zero-weighted rather than treated as validated.

Output:
  data/calibrated_factor_weights.json

The file is marked validated=true only when the A-share universe has enough
cross-sectional observations and at least one factor has positive IC.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_research import config
from stock_research.adapters import store
from stock_research.core import factor_ic
from stock_research.jobs.a_share_picks import DEFAULT_FACTOR_WEIGHTS

logger = logging.getLogger(__name__)

OUT_PATH = REPO / "data" / "calibrated_factor_weights.json"
PRICE_CACHE = REPO / "data" / "latest" / "a_share_price_history_cache.json"
CALIBRATABLE_FACTORS = ("momentum", "reversal")


def _normalize_ak_code(code: str) -> str:
    raw = str(code or "").upper().replace(".SS", "").replace(".SZ", "").replace(".BJ", "")
    if raw.startswith(("60", "68", "9")):
        return "sh" + raw
    if raw.startswith(("8", "43")):
        return "bj" + raw
    return "sz" + raw


def _load_universe(kind: str, limit: int | None) -> list[dict[str, Any]]:
    from stock_research.core.a_share_universe import fetch_a_share_tech_universe
    items = fetch_a_share_tech_universe()
    if limit and limit > 0:
        items = items[:limit]
    out = []
    for item in items:
        raw = str(item.get("raw_ticker") or item.get("ticker") or "").split(".")[0]
        if raw.isdigit() and len(raw) == 6:
            out.append({"code": raw, "name": item.get("name") or raw, "sector": item.get("sector") or ""})
    return out


def _load_price_cache() -> dict[str, Any]:
    try:
        data = json.loads(PRICE_CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"items": {}}
    except Exception:
        return {"items": {}}


def _save_price_cache(cache: dict[str, Any]) -> None:
    PRICE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": datetime.now().isoformat(timespec="seconds"), "items": cache.get("items", {})}
    tmp = PRICE_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(PRICE_CACHE)


def _fetch_history(code: str, start: pd.Timestamp, end: pd.Timestamp, cache: dict[str, Any]) -> pd.DataFrame | None:
    items = cache.setdefault("items", {})
    cached = items.get(code)
    if isinstance(cached, dict):
        try:
            df = pd.DataFrame(cached.get("rows") or [])
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])
                if df["date"].min() <= start and df["date"].max() >= end - pd.Timedelta(days=10):
                    return df.sort_values("date").reset_index(drop=True)
        except Exception:
            pass

    try:
        import akshare as ak
        df = ak.stock_zh_a_daily(
            symbol=_normalize_ak_code(code),
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="qfq",
        )
    except Exception as e:
        logger.warning("price history failed for %s: %s", code, e)
        return None
    if df is None or df.empty:
        return None
    df = df[["date", "close"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna().sort_values("date").reset_index(drop=True)
    if len(df) < 280:
        return None
    items[code] = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "rows": [{"date": r.date.strftime("%Y-%m-%d"), "close": float(r.close)} for r in df.itertuples()],
    }
    time.sleep(0.15)
    return df


def _calibration_dates(months: int, horizon_days: int) -> list[pd.Timestamp]:
    end = pd.Timestamp.today().normalize() - pd.Timedelta(days=max(35, horizon_days + 10))
    start = end - pd.DateOffset(months=months)
    return [pd.Timestamp(d).normalize() for d in pd.date_range(start=start, end=end, freq="ME")]


def _factor_at(df: pd.DataFrame, as_of: pd.Timestamp, horizon_days: int) -> tuple[dict[str, float], float] | None:
    dates = df["date"]
    idx = int(dates.searchsorted(as_of, side="right") - 1)
    if idx < 253 or idx + horizon_days >= len(df):
        return None
    close = df["close"].astype(float)
    now = float(close.iloc[idx])
    minus_21 = float(close.iloc[idx - 21])
    minus_252 = float(close.iloc[idx - 252])
    future = float(close.iloc[idx + horizon_days])
    if min(now, minus_21, minus_252, future) <= 0:
        return None
    factors = {
        "momentum": (minus_21 / minus_252 - 1.0) * 100.0,
        "reversal": -((now / minus_21 - 1.0) * 100.0),
    }
    forward = (future / now - 1.0) * 100.0
    return factors, forward


def _build_history(
    universe: list[dict[str, Any]],
    *,
    months: int,
    horizon_days: int,
) -> tuple[list[tuple[dict, dict]], dict[str, Any]]:
    dates = _calibration_dates(months, horizon_days)
    start = min(dates) - pd.Timedelta(days=430)
    end = pd.Timestamp.today().normalize()
    cache = _load_price_cache()
    histories: dict[str, pd.DataFrame] = {}
    for item in universe:
        code = item["code"]
        df = _fetch_history(code, start, end, cache)
        if df is not None:
            histories[code] = df
    _save_price_cache(cache)

    history: list[tuple[dict, dict]] = []
    period_samples: list[dict[str, Any]] = []
    for as_of in dates:
        factors_t: dict[str, dict[str, float]] = {}
        returns_t: dict[str, float] = {}
        for code, df in histories.items():
            row = _factor_at(df, as_of, horizon_days)
            if row is None:
                continue
            factors, forward = row
            factors_t[code] = factors
            returns_t[code] = forward
        if factors_t:
            history.append((factors_t, returns_t))
        period_samples.append({"as_of": as_of.strftime("%Y-%m-%d"), "n": len(factors_t)})
    meta = {
        "dates": [d.strftime("%Y-%m-%d") for d in dates],
        "period_samples": period_samples,
        "n_price_histories": len(histories),
    }
    return history, meta


def _derive_weights(
    audit: dict[str, Any],
    *,
    min_periods: int,
    min_mean_ic: float,
    min_hit_rate: float,
) -> tuple[dict[str, float], dict[str, Any]]:
    raw: dict[str, float] = {}
    diagnostics: dict[str, Any] = {}
    for factor in CALIBRATABLE_FACTORS:
        summary = (audit.get("factors") or {}).get(factor, {}).get("summary") or {}
        mean_ic = summary.get("mean_ic")
        hit_rate = summary.get("hit_rate")
        n_periods = int(summary.get("n_periods") or 0)
        ok = (
            mean_ic is not None
            and mean_ic >= min_mean_ic
            and n_periods >= min_periods
            and (hit_rate is None or hit_rate >= min_hit_rate)
        )
        diagnostics[factor] = {"selected": ok, **summary}
        raw[factor] = float(mean_ic) ** 2 if ok else 0.0
    total = sum(raw.values())
    weights = {k: 0.0 for k in DEFAULT_FACTOR_WEIGHTS}
    if total > 0:
        for factor, score in raw.items():
            weights[factor] = score / total
    return weights, diagnostics


def run(
    *,
    universe_kind: str = "static",
    limit: int = 42,
    months: int = 18,
    horizon_days: int = 20,
    min_periods: int = 6,
    min_period_sample: int = 10,
    min_mean_ic: float = 0.02,
    min_hit_rate: float = 0.50,
    dry_run: bool = False,
) -> dict[str, Any]:
    print("=" * 80)
    print("  A 股因子权重校准（市场内 price-only IC）")
    print("=" * 80)
    universe = _load_universe(universe_kind, limit)
    print(f"  universe={universe_kind} n={len(universe)} horizon={horizon_days}d months={months}")
    history, meta = _build_history(universe, months=months, horizon_days=horizon_days)
    usable_periods = [p for p in meta["period_samples"] if p["n"] >= min_period_sample]
    history = [row for row, p in zip(history, meta["period_samples"]) if p["n"] >= min_period_sample]
    print(f"  usable periods={len(history)} / {len(meta['period_samples'])}; price histories={meta['n_price_histories']}")

    if history:
        audit = factor_ic.audit_factors(history, list(CALIBRATABLE_FACTORS))
        weights, diagnostics = _derive_weights(
            audit,
            min_periods=min_periods,
            min_mean_ic=min_mean_ic,
            min_hit_rate=min_hit_rate,
        )
    else:
        audit = {"factors": {}, "ranking": []}
        weights = {k: 0.0 for k in DEFAULT_FACTOR_WEIGHTS}
        diagnostics = {}

    selected = [k for k, v in weights.items() if v > 0]
    validated = bool(selected) and len(history) >= min_periods
    if validated:
        total = sum(weights.values()) or 1.0
        weights = {k: round(v / total, 6) for k, v in weights.items()}
    else:
        # Invalid files should never be mistaken for production-ready weights.
        weights = {k: 0.0 for k in DEFAULT_FACTOR_WEIGHTS}

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "market": "a_share",
        "validated": validated,
        "validation_status": "validated" if validated else "insufficient_ic_evidence",
        "method": "Spearman IC on A-share adjusted daily prices; non-reconstructable event/flow factors zero-weighted",
        "weights": weights,
        "calibratable_factors": list(CALIBRATABLE_FACTORS),
        "zero_weight_reason": {
            "f_score": "financial PIT calibration not enabled in this price-only job",
            "lhb": "requires historical LHB signal snapshots",
            "north_flow": "requires historical northbound signal snapshots",
            "pead": "requires point-in-time earnings announcement snapshots",
            "policy_theme": "requires historical policy-theme signal snapshots",
        },
        "thresholds": {
            "min_periods": min_periods,
            "min_period_sample": min_period_sample,
            "min_mean_ic": min_mean_ic,
            "min_hit_rate": min_hit_rate,
            "horizon_days": horizon_days,
        },
        "sample": {
            "universe": universe_kind,
            "n_universe": len(universe),
            "n_periods": len(history),
            "period_samples": usable_periods,
            "n_price_histories": meta["n_price_histories"],
        },
        "diagnostics": diagnostics,
        "ic_audit": audit,
    }

    print("\n  因子诊断:")
    for factor in CALIBRATABLE_FACTORS:
        d = diagnostics.get(factor) or {}
        print(
            f"    {factor:<10} selected={d.get('selected')} "
            f"mean_ic={d.get('mean_ic')} hit={d.get('hit_rate')} n={d.get('n_periods')}"
        )
    print(f"\n  validated={validated} weights={weights}")

    if not dry_run:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        store.save_json(payload, config.AUDIT_DIR, "calibrate_a_share_factor_weights")
        print(f"  ✅ {OUT_PATH}")
    else:
        print(f"  [dry-run] would write {OUT_PATH}")
    return payload


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", choices=["static", "dynamic"], default="static")
    parser.add_argument("--limit", type=int, default=42)
    parser.add_argument("--months", type=int, default=18)
    parser.add_argument("--horizon-days", type=int, default=20)
    parser.add_argument("--min-periods", type=int, default=6)
    parser.add_argument("--min-period-sample", type=int, default=10)
    parser.add_argument("--min-mean-ic", type=float, default=0.02)
    parser.add_argument("--min-hit-rate", type=float, default=0.50)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    payload = run(
        universe_kind=args.universe,
        limit=args.limit,
        months=args.months,
        horizon_days=args.horizon_days,
        min_periods=args.min_periods,
        min_period_sample=args.min_period_sample,
        min_mean_ic=args.min_mean_ic,
        min_hit_rate=args.min_hit_rate,
        dry_run=args.dry_run,
    )
    return 0 if payload.get("validated") else 1


if __name__ == "__main__":
    raise SystemExit(main())
