#!/usr/bin/env python3
"""盈利预期上修因子的初步 IC 验证（SHADOW_RESEARCH_ONLY，只读）。

revision_score 今天才开始采集,没有自有 PIT 历史 —— 但 yfinance eps_trend
的 7/30/60/90 天前一致预期本身可以重构历史截面:
  截面 A(≈60 天前): 上修幅度 = (eps_60d_ago - eps_90d_ago)/|eps_90d_ago|
  截面 B(≈30 天前): 上修幅度 = (eps_30d_ago - eps_60d_ago)/|eps_60d_ago|
  截面 C(≈ 7 天前): 上修幅度 = (eps_7d_ago  - eps_30d_ago)/|eps_30d_ago|(短窗辅助)
配合 price_daily 已有的美股日线,算 Spearman 截面 IC + 五分位多空价差。

诚实边界(报告里也写):
  1. 只能验证「幅度」分量;「广度」(上/下修家数)无法重构历史,留给前向。
  2. 2-3 个截面 × ~130 只,证据薄;结论只配决定「值不值得设计 shadow 变体」,
     变体本身还要走前向 A/B,不存在跳级。
  3. 宇宙是今天的(幸存者口径);Yahoo 的 Xd-ago 是近似快照。
判定标准(写死,防止事后挪门柱):
  PASS: A/B 两截面 20d IC 同号 且 均值 ≥ +0.05
  WEAK: 同号 且 均值 ∈ [+0.02, +0.05)
  FAIL: 其余
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import get_db  # noqa: E402

OUT_JSON = REPO / "data" / "latest" / "revision_ic_validation.json"
OUT_MD = REPO / "data" / "reports" / "revision_ic_validation.md"

# (截面名, eps 新值列, eps 旧值列, as-of 距今天的日历日, 可用前瞻 horizons)
CROSS_SECTIONS = (
    ("A_60d_ago", "eps_60d_ago", "eps_90d_ago", 60, (5, 10, 20)),
    ("B_30d_ago", "eps_30d_ago", "eps_60d_ago", 30, (5, 10, 20)),
    ("C_7d_ago", "eps_7d_ago", "eps_30d_ago", 7, (1, 3)),
)
PRIMARY_HORIZON = 20
PASS_IC = 0.05
WEAK_IC = 0.02


def _ranks(values: list[float]) -> list[float]:
    """平均秩(处理大量"预期没动"的并列)。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman_ic(factor: list[float], fwd: list[float]) -> float | None:
    n = len(factor)
    if n < 20:
        return None
    rx, ry = _ranks(factor), _ranks(fwd)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx * vy) ** 0.5


def quintile_spread(factor: list[float], fwd: list[float]) -> float | None:
    n = len(factor)
    if n < 25:
        return None
    order = sorted(range(n), key=lambda i: factor[i])
    q = n // 5
    bottom = [fwd[i] for i in order[:q]]
    top = [fwd[i] for i in order[-q:]]
    return sum(top) / len(top) - sum(bottom) / len(bottom)


def revision_pct(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return (new - old) / abs(old) * 100.0


def main() -> int:
    conn = get_db(force_read_only=True)
    try:
        snap_date = conn.execute(
            "SELECT max(snapshot_date) FROM analyst_estimate_snapshots WHERE market='US'"
        ).fetchone()[0]
        if snap_date is None:
            print("无快照数据,先跑 collect_estimate_snapshots.py", file=sys.stderr)
            return 1
        snap_rows = conn.execute(
            """
            SELECT symbol, period, eps_current, eps_7d_ago, eps_30d_ago,
                   eps_60d_ago, eps_90d_ago
            FROM analyst_estimate_snapshots
            WHERE market='US' AND snapshot_date = ? AND period IN ('0y', '+1y')
            """,
            [snap_date],
        ).fetchall()
        trade_dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM price_daily WHERE market='US' ORDER BY 1"
        ).fetchall()]
        price_rows = conn.execute(
            """
            SELECT symbol, trade_date, close FROM price_daily
            WHERE market='US' AND close IS NOT NULL
              AND trade_date >= CAST(? AS DATE) - INTERVAL 80 DAY
            """,
            [snap_date],
        ).fetchall()
    finally:
        conn.close()

    closes: dict[str, dict[Any, float]] = {}
    for symbol, td, close in price_rows:
        closes.setdefault(str(symbol), {})[td] = float(close)

    est: dict[str, dict[str, dict[str, float | None]]] = {}
    for symbol, period, cur, d7, d30, d60, d90 in snap_rows:
        est.setdefault(str(symbol), {})[str(period)] = {
            "eps_current": cur, "eps_7d_ago": d7, "eps_30d_ago": d30,
            "eps_60d_ago": d60, "eps_90d_ago": d90,
        }

    def trade_index_at_or_after(target: date) -> int | None:
        for idx, td in enumerate(trade_dates):
            if td >= target:
                return idx
        return None

    results: list[dict[str, Any]] = []
    for cs_name, new_col, old_col, days_ago, horizons in CROSS_SECTIONS:
        asof = snap_date - timedelta(days=days_ago)
        t0 = trade_index_at_or_after(asof)
        if t0 is None:
            continue
        t0_date = trade_dates[t0]
        # 因子值:0y 与 +1y 上修幅度的平均(与 revision_score 的幅度分量同口径)
        factor_by_symbol: dict[str, float] = {}
        for symbol, periods in est.items():
            vals = []
            for period in ("0y", "+1y"):
                p = periods.get(period) or {}
                v = revision_pct(p.get(new_col), p.get(old_col))
                if v is not None:
                    vals.append(v)
            if vals:
                factor_by_symbol[symbol] = sum(vals) / len(vals)

        for horizon in horizons:
            if t0 + horizon >= len(trade_dates):
                continue
            t1_date = trade_dates[t0 + horizon]
            factor: list[float] = []
            fwd: list[float] = []
            for symbol, fv in factor_by_symbol.items():
                series = closes.get(symbol) or {}
                p0, p1 = series.get(t0_date), series.get(t1_date)
                if p0 and p1:
                    factor.append(fv)
                    fwd.append((p1 / p0 - 1.0) * 100.0)
            ic = spearman_ic(factor, fwd)
            spread = quintile_spread(factor, fwd)
            nonzero = sum(1 for v in factor if abs(v) > 1e-9)
            results.append({
                "cross_section": cs_name,
                "asof_trade_date": str(t0_date),
                "horizon_days": horizon,
                "n": len(factor),
                "n_nonzero_factor": nonzero,
                "spearman_ic": round(ic, 4) if ic is not None else None,
                "quintile_spread_pct": round(spread, 3) if spread is not None else None,
            })

    primary = [
        r for r in results
        if r["cross_section"] in ("A_60d_ago", "B_30d_ago")
        and r["horizon_days"] == PRIMARY_HORIZON and r["spearman_ic"] is not None
    ]
    verdict = "FAIL"
    mean_ic = None
    if len(primary) == 2:
        ics = [r["spearman_ic"] for r in primary]
        mean_ic = round(sum(ics) / 2, 4)
        same_sign = ics[0] * ics[1] > 0
        if same_sign and mean_ic >= PASS_IC:
            verdict = "PASS"
        elif same_sign and mean_ic >= WEAK_IC:
            verdict = "WEAK"

    payload = {
        "schema_version": 1,
        "safety_boundary": "SHADOW_RESEARCH_ONLY",
        "generated_at": str(snap_date),
        "snapshot_date": str(snap_date),
        "verdict": verdict,
        "primary_horizon_days": PRIMARY_HORIZON,
        "primary_mean_ic": mean_ic,
        "criteria": {"pass_ic": PASS_IC, "weak_ic": WEAK_IC,
                     "rule": "A/B 截面 20d IC 同号且均值过线"},
        "results": results,
        "notes": [
            "只验证幅度分量(0y/+1y 平均 30 天上修幅度);广度家数无法重构历史。",
            "2-3 截面证据薄:PASS 只代表值得设计 shadow 变体,变体仍要前向 A/B。",
            "宇宙为今日口径(幸存者偏差);Yahoo Xd-ago 为近似快照。",
        ],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 盈利预期上修因子 · 初步 IC 验证",
        "",
        f"- 快照日: {snap_date} · 判定: **{verdict}** (主口径 20d 平均 IC = {mean_ic})",
        f"- 标准: A/B 截面 20d IC 同号且均值 ≥{PASS_IC} PASS / ≥{WEAK_IC} WEAK",
        "",
        "| 截面 | as-of | 前瞻 | n | 非零因子 | Spearman IC | 五分位价差% |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| {r['cross_section']} | {r['asof_trade_date']} | {r['horizon_days']}d | "
            f"{r['n']} | {r['n_nonzero_factor']} | {r['spearman_ic']} | {r['quintile_spread_pct']} |")
    lines += ["", *(f"- {n}" for n in payload["notes"])]
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[OK] JSON → {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
