#!/usr/bin/env python3
"""财报惊喜(PEAD)因子 IC 验证（SHADOW_RESEARCH_ONLY，只读）。

PEAD = 业绩公告漂移:财报超预期的股票在公告后数日-数周仍倾向跑赢。
A 股打分已有 pead 因子(权重 0.10),美股一直缺;本工具用
earnings_surprise_events(每季实际 vs 当时预期,PIT 定格)验证美股版。

事件研究口径:
  - 因子 = surprise_pct = (actual - estimated)/|estimated|,截尾 ±50%
  - 入场 = 公告日后第一个交易日收盘(不假设盘前/盘后,保守)
  - 前瞻 = 入场后 5/20 个交易日收益
  - 按公告季度分组做截面 IC(同季事件互为对照),再对季度 IC 求均值/t
判定标准(跑前写死,与 value/grade 同一约定):
  主口径 20d,PASS = mean IC ≥ +0.03 且 t ≥ 2;WEAK = ≥ +0.015 且 t ≥ 1;
  显著为负记 FAIL_NEGATIVE;其余 FAIL。季度数少(~5)时 t 仅供参考。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import get_db  # noqa: E402
from scripts.tools.validate_revision_ic import spearman_ic, quintile_spread  # noqa: E402

OUT_JSON = REPO / "data" / "latest" / "pead_ic_validation.json"
OUT_MD = REPO / "data" / "reports" / "pead_ic_validation.md"

HORIZONS = (5, 20)
PRIMARY_HORIZON = 20
SURPRISE_CAP = 50.0
PASS_IC, PASS_T = 0.03, 2.0
WEAK_IC, WEAK_T = 0.015, 1.0
MIN_EVENTS_PER_COHORT = 25


def main() -> int:
    conn = get_db(force_read_only=True)
    try:
        events = conn.execute(
            """
            SELECT symbol, announce_date, eps_actual, eps_estimated
            FROM earnings_surprise_events
            WHERE market='US' AND eps_actual IS NOT NULL
              AND eps_estimated IS NOT NULL AND eps_estimated != 0
            ORDER BY announce_date
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
        print("earnings_surprise_events 为空,先跑 backfill_earnings_surprises.py", file=sys.stderr)
        return 1

    closes: dict[str, dict[Any, float]] = {}
    for symbol, td, close in price_rows:
        closes.setdefault(str(symbol), {})[td] = float(close)
    date_index = {d: i for i, d in enumerate(trade_dates)}

    def entry_index(announce) -> int | None:
        """公告日后第一个交易日的下标。

        必须落在公告后 7 个日历日内 —— 价格库(price_daily)只覆盖 2024-06 起,
        更早的事件若不设上限会被错误映射到价格库首日,形成垃圾截面。
        """
        for d in trade_dates:
            if d > announce:
                return date_index[d] if (d - announce).days <= 7 else None
        return None

    # 事件 → (季度 cohort, surprise, 前瞻收益)
    cohorts: dict[str, list[dict[str, Any]]] = {}
    used = skipped_no_price = 0
    max_h = max(HORIZONS)
    for symbol, announce, actual, estimated in events:
        t0 = entry_index(announce)
        if t0 is None or t0 + max_h >= len(trade_dates):
            continue
        series = closes.get(str(symbol)) or {}
        p0 = series.get(trade_dates[t0])
        if not p0:
            skipped_no_price += 1
            continue
        surprise = (float(actual) - float(estimated)) / abs(float(estimated)) * 100.0
        surprise = max(-SURPRISE_CAP, min(SURPRISE_CAP, surprise))
        row: dict[str, Any] = {"symbol": symbol, "surprise": surprise}
        ok = True
        for horizon in HORIZONS:
            p1 = series.get(trade_dates[t0 + horizon])
            if not p1:
                ok = False
                break
            row[f"fwd_{horizon}d"] = (p1 / p0 - 1.0) * 100.0
        if not ok:
            skipped_no_price += 1
            continue
        quarter = f"{announce.year}Q{(announce.month - 1) // 3 + 1}"
        cohorts.setdefault(quarter, []).append(row)
        used += 1

    results: list[dict[str, Any]] = []
    for quarter in sorted(cohorts):
        rows = cohorts[quarter]
        if len(rows) < MIN_EVENTS_PER_COHORT:
            continue
        out: dict[str, Any] = {"cohort": quarter, "n_events": len(rows)}
        for horizon in HORIZONS:
            factor = [r["surprise"] for r in rows]
            fwd = [r[f"fwd_{horizon}d"] for r in rows]
            ic = spearman_ic(factor, fwd)
            spread = quintile_spread(factor, fwd)
            out[f"ic_{horizon}d"] = round(ic, 4) if ic is not None else None
            out[f"spread_{horizon}d"] = round(spread, 3) if spread is not None else None
        results.append(out)

    def _summary(horizon: int) -> dict[str, Any]:
        ics = [r[f"ic_{horizon}d"] for r in results if r.get(f"ic_{horizon}d") is not None]
        if len(ics) < 3:
            return {"n_cohorts": len(ics), "mean_ic": None, "t_stat": None, "pct_positive": None}
        mean = sum(ics) / len(ics)
        var = sum((x - mean) ** 2 for x in ics) / (len(ics) - 1)
        t = mean / ((var / len(ics)) ** 0.5) if var > 0 else None
        return {
            "n_cohorts": len(ics),
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

    payload = {
        "schema_version": 1,
        "safety_boundary": "SHADOW_RESEARCH_ONLY",
        "verdict": verdict,
        "factor": "eps_surprise_pct(截尾±50%),公告后次日收盘入场",
        "primary_horizon_days": PRIMARY_HORIZON,
        "primary_summary": primary,
        "summary_5d": _summary(5),
        "criteria": {"pass": f"mean IC≥{PASS_IC} 且 t≥{PASS_T}",
                     "weak": f"mean IC≥{WEAK_IC} 且 t≥{WEAK_T}"},
        "events_used": used,
        "events_skipped_no_price": skipped_no_price,
        "cohorts": results,
        "notes": [
            "surprise 是公告日定格的 PIT 数据(yfinance earnings_dates,~24 季度史)。",
            "price_daily 仅覆盖 2024-06 起 → 有效 cohort 限近 8 个季度;入场限公告后 7 日内,防垃圾映射。",
            "季度 cohort 数少,t 统计量仅供参考;PASS 也只先进 shadow。",
            "幸存者宇宙;缺价事件已计入 skipped。",
        ],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 财报惊喜(PEAD)因子 · IC 验证",
        "",
        f"- 判定: **{verdict}** · 主口径 {PRIMARY_HORIZON}d: mean IC={primary.get('mean_ic')} "
        f"t={primary.get('t_stat')} 季度数={primary.get('n_cohorts')} 正比例={primary.get('pct_positive')}%",
        f"- 5d: mean IC={payload['summary_5d'].get('mean_ic')} t={payload['summary_5d'].get('t_stat')}",
        f"- 事件: 用 {used} / 缺价跳过 {skipped_no_price}",
        "",
        "| 季度 | 事件数 | IC 20d | 价差 20d% | IC 5d | 价差 5d% |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(f"| {r['cohort']} | {r['n_events']} | {r.get('ic_20d')} | "
                     f"{r.get('spread_20d')} | {r.get('ic_5d')} | {r.get('spread_5d')} |")
    lines += ["", *(f"- {n}" for n in payload["notes"])]
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"[OK] JSON → {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
