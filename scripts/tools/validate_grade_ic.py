#!/usr/bin/env python3
"""分析师评级净上调因子的长窗口 IC 验证（SHADOW_RESEARCH_ONLY，只读）。

「盈利预期上修」的长历史近亲:评级上下调是带日期的离散事件(PIT 正确,
不会被改写),analyst_grade_events 回填了十余年历史。本工具用近两年
(price_daily 美股价格覆盖范围)做截面 IC,把上修类因子的验证从
revision IC 的 2 个截面扩到几十个。

因子定义(v0,跑前写死):
  net_upgrade_30d = 过去 30 个日历日内 (上调家数 - 下调家数)
  只数 action ∈ {upgrade, downgrade};initiate/maintain 不计(中性处理)。
判定标准(与 validate_value_ic 同一约定):
  主口径 20d,PASS = mean IC ≥ +0.03 且 t ≥ 2;WEAK = ≥ +0.015 且 t ≥ 1;
  显著为负记 FAIL_NEGATIVE;其余 FAIL。
诚实边界:幸存者宇宙;事件覆盖依赖 FMP 收录;净上调多数日子为 0(平局多,
  Spearman 平均秩可处理,但有效区分度集中在少数有事件的票上)。
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

OUT_JSON = REPO / "data" / "latest" / "grade_ic_validation.json"
OUT_MD = REPO / "data" / "reports" / "grade_ic_validation.md"

LOOKBACK_DAYS = 30
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
            SELECT symbol, event_date, lower(coalesce(action, '')) AS action
            FROM analyst_grade_events
            WHERE market='US' AND lower(coalesce(action, '')) IN ('upgrade', 'downgrade')
            ORDER BY symbol, event_date
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
        print("analyst_grade_events 无 upgrade/downgrade 事件,先跑 backfill_grade_history.py",
              file=sys.stderr)
        return 1

    closes: dict[str, dict[Any, float]] = {}
    for symbol, td, close in price_rows:
        closes.setdefault(str(symbol), {})[td] = float(close)

    ev_by_symbol: dict[str, list[tuple[Any, int]]] = {}
    for symbol, ev_date, action in events:
        ev_by_symbol.setdefault(str(symbol), []).append(
            (ev_date, 1 if action == "upgrade" else -1))

    def net_upgrades(symbol: str, asof) -> int:
        start = asof - timedelta(days=LOOKBACK_DAYS)
        return sum(sign for d, sign in ev_by_symbol.get(symbol, ())
                   if start < d <= asof)

    symbols = sorted(closes.keys())
    results: list[dict[str, Any]] = []
    max_h = max(HORIZONS)
    for t0 in range(0, len(trade_dates) - max_h, SECTION_STEP):
        t0_date = trade_dates[t0]
        factor_syms: list[str] = []
        factor: list[float] = []
        for symbol in symbols:
            if closes[symbol].get(t0_date):
                factor_syms.append(symbol)
                factor.append(float(net_upgrades(symbol, t0_date)))
        if len(factor) < 30:
            continue
        nonzero = sum(1 for v in factor if v != 0)
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
        return {
            "n_sections": len(ics),
            "mean_ic": round(mean, 4),
            "t_stat": round(t, 2) if t is not None else None,
            "pct_positive": round(sum(1 for x in ics if x > 0) / len(ics) * 100, 1),
        }

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
        "factor": f"net_upgrade_{LOOKBACK_DAYS}d(评级净上调家数)",
        "primary_horizon_days": PRIMARY_HORIZON,
        "primary_summary": primary,
        "summary_5d": _summary(5),
        "stability_by_half_year": stability,
        "criteria": {"pass": f"mean IC≥{PASS_IC} 且 t≥{PASS_T}",
                     "weak": f"mean IC≥{WEAK_IC} 且 t≥{WEAK_T}"},
        "sections": results,
        "notes": [
            "事件 PIT 正确(带日期不可改写);覆盖依赖 FMP 收录。",
            "净上调多数票多数日为 0,区分度集中在有事件的票上(平均秩处理平局)。",
            "幸存者宇宙;与盈利上修是近亲但非同一因子,PASS 也只先进 shadow。",
        ],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 分析师评级净上调因子 · 长窗口 IC 验证",
        "",
        f"- 判定: **{verdict}** · 主口径 {PRIMARY_HORIZON}d: mean IC={primary.get('mean_ic')} "
        f"t={primary.get('t_stat')} 截面数={primary.get('n_sections')} 正比例={primary.get('pct_positive')}%",
        f"- 5d: mean IC={payload['summary_5d'].get('mean_ic')} t={payload['summary_5d'].get('t_stat')}",
        f"- 半年稳定性: {stability}",
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
    print("\n".join(lines[:12]))
    print(f"…(全表见 {OUT_MD})")
    print(f"[OK] JSON → {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
