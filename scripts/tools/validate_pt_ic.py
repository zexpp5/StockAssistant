#!/usr/bin/env python3
"""分析师目标价修正因子的长窗口 IC 验证（SHADOW_RESEARCH_ONLY，只读）。

评级净上调(已 PASS)的连续版近亲:目标价从 prior → current 的变化幅度,
信息比"上调/下调"二值更细。事件 PIT 正确(带日期)。

因子定义(v0,跑前写死):
  pt_change_30d = 过去 30 个日历日内事件的 (current-prior)/prior×100 平均,
  单事件截尾 ±20%;无事件 → 0(中性)。
判定标准(与 grade/value 同一约定):
  主口径 20d,PASS = mean IC ≥ +0.03 且 t ≥ 2;WEAK = ≥ +0.015 且 t ≥ 1;
  显著为负记 FAIL_NEGATIVE;其余 FAIL。截面每 20 交易日不重叠。
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import get_db  # noqa: E402
from scripts.tools.validate_revision_ic import spearman_ic, quintile_spread  # noqa: E402

OUT_JSON = REPO / "data" / "latest" / "pt_ic_validation.json"
OUT_MD = REPO / "data" / "reports" / "pt_ic_validation.md"

LOOKBACK_DAYS = 30
EVENT_CAP_PCT = 20.0
SECTION_STEP = 20
HORIZONS = (5, 20)
PRIMARY_HORIZON = 20
PASS_IC, PASS_T = 0.03, 2.0
WEAK_IC, WEAK_T = 0.015, 1.0


def main() -> int:
    conn = get_db(force_read_only=True)
    try:
        events = conn.execute(
            """
            SELECT symbol, event_date, price_target, prior_price_target
            FROM analyst_grade_events
            WHERE market='US' AND price_target IS NOT NULL
              AND prior_price_target IS NOT NULL AND prior_price_target > 0
            """
        ).fetchall()
        trade_dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM price_daily WHERE market='US' ORDER BY 1"
        ).fetchall()]
        price_rows = conn.execute(
            "SELECT symbol, trade_date, close FROM price_daily WHERE market='US' AND close IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    if not events:
        print("无目标价事件,先跑 backfill_grade_history.py(新 schema)", file=sys.stderr)
        return 1

    closes: dict[str, dict[Any, float]] = {}
    for symbol, td, close in price_rows:
        closes.setdefault(str(symbol), {})[td] = float(close)

    from collections import defaultdict
    ev: dict[str, list[tuple[Any, float]]] = defaultdict(list)
    for symbol, d, cur, prior in events:
        chg = (float(cur) - float(prior)) / float(prior) * 100.0
        chg = max(-EVENT_CAP_PCT, min(EVENT_CAP_PCT, chg))
        ev[str(symbol)].append((d, chg))

    def pt_factor(symbol: str, asof) -> float:
        start = asof - timedelta(days=LOOKBACK_DAYS)
        vals = [c for d, c in ev.get(symbol, ()) if start < d <= asof]
        return sum(vals) / len(vals) if vals else 0.0

    symbols = sorted(closes.keys())
    results: list[dict[str, Any]] = []
    max_h = max(HORIZONS)
    for t0 in range(0, len(trade_dates) - max_h, SECTION_STEP):
        t0_date = trade_dates[t0]
        factor_syms, factor = [], []
        for symbol in symbols:
            if closes[symbol].get(t0_date):
                factor_syms.append(symbol)
                factor.append(pt_factor(symbol, t0_date))
        if len(factor) < 30:
            continue
        nonzero = sum(1 for v in factor if v != 0.0)
        row: dict[str, Any] = {"asof": str(t0_date), "n": len(factor), "n_nonzero": nonzero}
        for horizon in HORIZONS:
            t1_date = trade_dates[t0 + horizon]
            keep, fwd = [], []
            for symbol, fv in zip(factor_syms, factor):
                p0 = closes[symbol][t0_date]
                p1 = closes[symbol].get(t1_date)
                if p1:
                    keep.append(fv)
                    fwd.append((p1 / p0 - 1.0) * 100.0)
            ic = spearman_ic(keep, fwd)
            spread = quintile_spread(keep, fwd)
            row[f"ic_{horizon}d"] = round(ic, 4) if ic is not None else None
            row[f"spread_{horizon}d"] = round(spread, 3) if spread is not None else None
        results.append(row)

    def _summary(horizon: int) -> dict[str, Any]:
        ics = [r[f"ic_{horizon}d"] for r in results if r.get(f"ic_{horizon}d") is not None]
        if len(ics) < 4:
            return {"n_sections": len(ics), "mean_ic": None, "t_stat": None, "pct_positive": None}
        mean = sum(ics) / len(ics)
        var = sum((x - mean) ** 2 for x in ics) / (len(ics) - 1)
        t = mean / ((var / len(ics)) ** 0.5) if var > 0 else None
        return {"n_sections": len(ics), "mean_ic": round(mean, 4),
                "t_stat": round(t, 2) if t is not None else None,
                "pct_positive": round(sum(1 for x in ics if x > 0) / len(ics) * 100, 1)}

    primary = _summary(PRIMARY_HORIZON)
    verdict = "FAIL"
    mi, ts = primary.get("mean_ic"), primary.get("t_stat")
    if mi is not None and ts is not None:
        if mi >= PASS_IC and ts >= PASS_T:
            verdict = "PASS"
        elif mi >= WEAK_IC and ts >= WEAK_T:
            verdict = "WEAK"
        elif mi <= -PASS_IC and ts <= -PASS_T:
            verdict = "FAIL_NEGATIVE"

    halves: dict[str, list[float]] = {}
    for r in results:
        ic = r.get(f"ic_{PRIMARY_HORIZON}d")
        if ic is None:
            continue
        y, m = r["asof"][:4], int(r["asof"][5:7])
        halves.setdefault(f"{y}H{1 if m <= 6 else 2}", []).append(ic)
    stability = {k: round(sum(v) / len(v), 4) for k, v in sorted(halves.items()) if v}

    payload = {
        "schema_version": 1,
        "safety_boundary": "SHADOW_RESEARCH_ONLY",
        "verdict": verdict,
        "factor": f"pt_change_{LOOKBACK_DAYS}d(目标价修正幅度均值,单事件截尾±{EVENT_CAP_PCT}%)",
        "primary_horizon_days": PRIMARY_HORIZON,
        "primary_summary": primary,
        "summary_5d": _summary(5),
        "stability_by_half_year": stability,
        "criteria": {"pass": f"mean IC≥{PASS_IC} 且 t≥{PASS_T}",
                     "weak": f"mean IC≥{WEAK_IC} 且 t≥{WEAK_T}"},
        "n_events": len(events),
        "sections": results,
        "notes": [
            "目标价事件 PIT 正确;与评级净上调是近亲,若同时用须防双重计数(同一事件两个口径)。",
            "幸存者宇宙;PASS 也只先进 shadow。",
        ],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 分析师目标价修正因子 · 长窗口 IC 验证",
        "",
        f"- 判定: **{verdict}** · 主口径 {PRIMARY_HORIZON}d: mean IC={primary.get('mean_ic')} "
        f"t={primary.get('t_stat')} 截面数={primary.get('n_sections')} 正比例={primary.get('pct_positive')}%",
        f"- 5d: mean IC={payload['summary_5d'].get('mean_ic')} t={payload['summary_5d'].get('t_stat')}",
        f"- 半年稳定性: {stability} · 事件总数 {len(events)}",
        "",
        "| 截面 | n | 非零 | IC 20d | 价差 20d% | IC 5d |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(f"| {r['asof']} | {r['n']} | {r['n_nonzero']} | "
                     f"{r.get('ic_20d')} | {r.get('spread_20d')} | {r.get('ic_5d')} |")
    lines += ["", *(f"- {n}" for n in payload["notes"])]
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:10]))
    print(f"…(全表见 {OUT_MD})")
    print(f"[OK] JSON → {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
