#!/usr/bin/env python3
"""动量因子(12-1)的长窗口 IC 验证（SHADOW_RESEARCH_ONLY，只读）。

动量在生产公式里占 0.15、在多个变体里占 0.2,却从没过预注册 IC 验证 ——
和 valuation/pead/pt/revision 不同,它一路绿灯进了变体。本工具补上,
口径与 validate_value_ic / validate_grade_ic 完全一致。

因子定义(跑前写死): 12-1 动量 = 过去 252 交易日涨幅,跳过最近 21 日
  (学术标准,跳过近月以避开与 reversal 因子的重叠/短期反转污染)。
判定标准: 主口径 20d,PASS = mean IC ≥ +0.03 且 t ≥ 2;
  WEAK = ≥ +0.015 且 t ≥ 1;显著为负 FAIL_NEGATIVE;其余 FAIL。

🚨 幸存者警告(本因子尤其严重): 宇宙 = 今天的 133 只 AI 赢家,而动量 =
  "已经涨上去" —— 与"能留在池子里"高度同义,IC 会被系统性抬高。因此本测
  即便 PASS 也要打折看;两年代理回测里 momentum_pure 净 405% 几乎确定是
  这个偏差的产物,真实样本外回放里 momentum_pure 仅 +0.53(弱)。
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

OUT_JSON = REPO / "data" / "latest" / "momentum_ic_validation.json"
OUT_MD = REPO / "data" / "reports" / "momentum_ic_validation.md"

LOOKBACK = 252      # 12 月
SKIP = 21           # 跳过最近 1 月
SECTION_STEP = 20
HORIZONS = (5, 20)
PRIMARY_HORIZON = 20
PASS_IC, PASS_T = 0.03, 2.0
WEAK_IC, WEAK_T = 0.015, 1.0


def main() -> int:
    conn = get_db(force_read_only=True)
    try:
        trade_dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM price_daily WHERE market='US' ORDER BY 1"
        ).fetchall()]
        price_rows = conn.execute(
            "SELECT symbol, trade_date, close FROM price_daily WHERE market='US' AND close IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    closes: dict[str, dict[Any, float]] = {}
    for symbol, td, close in price_rows:
        closes.setdefault(str(symbol), {})[td] = float(close)
    symbols = sorted(closes.keys())

    results: list[dict[str, Any]] = []
    max_h = max(HORIZONS)
    for t0 in range(LOOKBACK, len(trade_dates) - max_h, SECTION_STEP):
        t0_date = trade_dates[t0]
        skip_date = trade_dates[t0 - SKIP]
        base_date = trade_dates[t0 - LOOKBACK]
        factor_syms, factor = [], []
        for symbol in symbols:
            s = closes[symbol]
            p_base, p_skip, p0 = s.get(base_date), s.get(skip_date), s.get(t0_date)
            if p_base and p_skip and p0 and p_base > 0:
                factor_syms.append(symbol)
                factor.append((p_skip / p_base - 1.0) * 100.0)  # 12-1 动量
        if len(factor) < 30:
            continue
        row: dict[str, Any] = {"asof": str(t0_date), "n": len(factor)}
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
        "factor": "12-1 动量(252日涨幅,跳过近21日)",
        "primary_horizon_days": PRIMARY_HORIZON,
        "primary_summary": primary,
        "summary_5d": _summary(5),
        "stability_by_half_year": stability,
        "criteria": {"pass": f"mean IC≥{PASS_IC} 且 t≥{PASS_T}",
                     "weak": f"mean IC≥{WEAK_IC} 且 t≥{WEAK_T}"},
        "notes": [
            "🚨 动量与'幸存者'高度同义,本测 IC 系统性偏高,PASS 也要打折看。",
            "真实样本外回放里 momentum_pure 仅 +0.53(弱);回测 405% 几乎确定是幸存者产物。",
            "12-1 口径跳过近月,避开与 reversal 因子重叠。",
        ],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 动量因子(12-1) · 长窗口 IC 验证",
        "",
        f"- 判定: **{verdict}** · 主口径 {PRIMARY_HORIZON}d: mean IC={primary.get('mean_ic')} "
        f"t={primary.get('t_stat')} 截面数={primary.get('n_sections')} 正比例={primary.get('pct_positive')}%",
        f"- 5d: mean IC={payload['summary_5d'].get('mean_ic')} t={payload['summary_5d'].get('t_stat')}",
        f"- 半年稳定性: {stability}",
        "",
        "| 截面 | n | IC 20d | 价差 20d% | IC 5d |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(f"| {r['asof']} | {r['n']} | {r.get('ic_20d')} | {r.get('spread_20d')} | {r.get('ic_5d')} |")
    lines += ["", *(f"- {n}" for n in payload["notes"])]
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:10]))
    print(f"…(全表见 {OUT_MD})")
    print(f"[OK] JSON → {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
