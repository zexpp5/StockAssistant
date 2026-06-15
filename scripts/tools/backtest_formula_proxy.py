#!/usr/bin/env python3
"""整套打分公式的两年代理回测（SHADOW_RESEARCH_ONLY，只读）。

回答用户的问题:「拿同样一套公式,用以前每天的数据跑会怎样?」

忠实复刻 vs 代理(逐项标注):
  - 动量分: 复刻 build_v2_recommendations._score_momentum
    (1w .20[-8,8] / 1m .25[-15,20] / ytd .25[-25,60] / 1y .30 驼峰分档,>300% 封顶 40)
  - 反转分: 复刻 _score_reversal(-1m,±15%→100/0,缺=45)
  - 估值分: ⚠️ 代理 — 生产 = 0.45×PEG+0.35×fwdPE+0.20×trailPE,其中 PEG/fwdPE
    依赖当时的分析师预测(历史只在机构库)。代理用 trailing PE 走生产同款
    _score_lower_better(25,100) 映射,EPS=年报 diluted(90 天滞后)。
  - 数据完整度(0.20): 回测里对全员为常数 → 不影响排名,略去(排名等价)。
  - f_score: 无历史 → 含它的变体里该项略去(常数中性,排名等价)。
  - grade: 评级净上调,analyst_grade_events PIT 计算(2012 起,真历史)。

组合口径: 每 5 个交易日重选 top-10 等权,持有至下次换仓;不计交易成本。
基准: QQQ(同窗口,自动复权);另给等权全宇宙基线(剥离"选股技能"看)。
🚨 幸存者偏差: 宇宙=今天的 133 只 AI 赢家 → 所有策略的【绝对】收益都被抬高,
  只有策略【之间】的相对比较有意义。
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
from scripts.tools.replay_weight_variants import (  # noqa: E402
    GRADE_LOOKBACK_DAYS,
    grade_score_from_net,
)
from scripts.tools.validate_value_ic import fetch_annual_eps  # noqa: E402

OUT_JSON = REPO / "data" / "latest" / "formula_proxy_backtest.json"
OUT_MD = REPO / "data" / "reports" / "formula_proxy_backtest.md"

BACKTEST_START = date(2024, 6, 12)   # 两年窗口
REBALANCE_STEP = 5                   # 交易日
TOP_K = 10
EPS_LAG_DAYS = 90
BENCHMARK = "QQQ"
COST_PER_SIDE_PCT = 0.10             # 单边成本 10bp(含点差,美股大盘保守值);换手按买+卖双边计

# 排名等价权重(略去常数因子 data_usability/f_score,见模块 docstring)
STRATEGIES: dict[str, dict[str, float]] = {
    # 基线 + 既有变体
    "prod_proxy": {"momentum": 0.15, "valuation": 0.50, "reversal": 0.15},
    "val_down_grade": {"momentum": 0.15, "valuation": 0.25, "reversal": 0.20, "grade": 0.20},
    "no_val_grade": {"momentum": 0.20, "reversal": 0.40, "grade": 0.40},  # 当前冠军
    "valuation_pure": {"valuation": 1.0},
    "grade_pure": {"grade": 1.0},
    # 2026-06-13 单纯形海选: 已验证双因子(reversal✅+grade✅)±动量的最优配比
    "rev_grade_5050": {"reversal": 0.50, "grade": 0.50},        # 完全去动量 — 测动量是否冗余
    "grade_heavy": {"momentum": 0.10, "reversal": 0.30, "grade": 0.60},
    "rev_heavy": {"momentum": 0.10, "reversal": 0.60, "grade": 0.30},
    "mrg_even": {"momentum": 1 / 3, "reversal": 1 / 3, "grade": 1 / 3},
    "reversal_pure": {"reversal": 1.0},                          # 反转单因子对照
    "momentum_pure": {"momentum": 1.0},                          # 动量单因子对照
}


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def score_lower_better(value: float | None, good: float, bad: float, missing: float = 30.0) -> float:
    if value is None or value != value or value <= 0:
        return missing
    if value <= good:
        return 100.0
    if value >= bad:
        return 0.0
    return (bad - value) / (bad - good) * 100.0


def score_one_year(value: float | None) -> float:
    if value is None or value != value:
        return 45.0
    if value >= 400:
        return 20.0
    if value >= 200:
        return 50.0
    if value >= 50:
        return 100.0
    if value >= 0:
        return 75.0
    if value >= -35:
        return 50.0
    return 25.0


def score_momentum(r1w, r1m, rytd, r1y) -> float:
    parts = []
    for value, weight, lo, hi in ((r1w, 0.20, -8.0, 8.0), (r1m, 0.25, -15.0, 20.0),
                                  (rytd, 0.25, -25.0, 60.0)):
        if value is None or value != value:
            parts.append((45.0, weight))
        else:
            parts.append((_clip((value - lo) / (hi - lo) * 100.0), weight))
    parts.append((score_one_year(r1y), 0.30))
    momentum = sum(s * w for s, w in parts)
    if r1y is not None and r1y == r1y and r1y > 300:
        momentum = min(momentum, 40.0)
    return momentum


def score_reversal(r1m) -> float:
    if r1m is None or r1m != r1m:
        return 45.0
    return _clip(50.0 - r1m / 15.0 * 50.0)


def main() -> int:
    import pandas as pd
    import yfinance as yf

    conn = get_db(force_read_only=True)
    try:
        symbols = [str(r[0]) for r in conn.execute(
            "SELECT DISTINCT symbol FROM system_universe WHERE market='US' AND active ORDER BY symbol"
        ).fetchall()]
        grade_rows = conn.execute(
            """
            SELECT symbol, event_date,
                   CASE WHEN lower(coalesce(action,''))='upgrade' THEN 1 ELSE -1 END
            FROM analyst_grade_events
            WHERE market='US' AND lower(coalesce(action,'')) IN ('upgrade','downgrade')
            """
        ).fetchall()
    finally:
        conn.close()

    from collections import defaultdict
    grade_events: dict[str, list[tuple[Any, int]]] = defaultdict(list)
    for symbol, d, sign in grade_rows:
        grade_events[str(symbol)].append((d, int(sign)))

    print(f"批量下载 {len(symbols)}+1 只 ~40 个月日线(yfinance)…", flush=True)
    px = yf.download(symbols + [BENCHMARK], period="40mo", interval="1d",
                     auto_adjust=True, progress=False)["Close"]
    px = px.dropna(axis=1, how="all")
    have = [s for s in symbols if s in px.columns]
    print(f"有价格的: {len(have)} 只;基准 {BENCHMARK}: {'OK' if BENCHMARK in px.columns else '缺!'}", flush=True)

    print("拉取年报 EPS(估值代理原料)…", flush=True)
    eps_hist = fetch_annual_eps(have)

    def eps_at(symbol: str, asof: date) -> float | None:
        best = None
        for fy_end, eps in eps_hist.get(symbol, []):
            if fy_end + timedelta(days=EPS_LAG_DAYS) <= asof:
                best = eps
        return best

    trade_dates = [d.date() for d in px.index]
    start_idx = next(i for i, d in enumerate(trade_dates) if d >= BACKTEST_START)
    # 动量需要 252 个交易日历史;数据从 ~2023-02 起,2024-06 起步正好够
    rebalance_idx = list(range(max(start_idx, 252), len(trade_dates) - 1, REBALANCE_STEP))

    def ret_pct(col, i0: int, i1: int) -> float | None:
        try:
            p0, p1 = float(px[col].iloc[i0]), float(px[col].iloc[i1])
        except Exception:
            return None
        if p0 != p0 or p1 != p1 or p0 <= 0:
            return None
        return (p1 / p0 - 1.0) * 100.0

    # 逐换仓日: 算因子分 → 各策略选 top-10 → 持有期收益(毛/净=扣换手成本)
    curves: dict[str, list[float]] = {name: [] for name in STRATEGIES}
    curves["universe_eqw"] = []
    curves[BENCHMARK] = []
    net_curves: dict[str, list[float]] = {name: [] for name in STRATEGIES}
    turnovers: dict[str, list[float]] = {name: [] for name in STRATEGIES}
    prev_holdings: dict[str, set[str]] = {name: set() for name in STRATEGIES}
    periods: list[dict[str, Any]] = []
    for n, t0 in enumerate(rebalance_idx):
        t1 = min(t0 + REBALANCE_STEP, len(trade_dates) - 1)
        asof = trade_dates[t0]
        ytd_anchor = next((i for i, d in enumerate(trade_dates) if d.year == asof.year), t0)
        scores_by_symbol: dict[str, dict[str, float]] = {}
        fwd: dict[str, float] = {}
        for symbol in have:
            series = px[symbol]
            if series.iloc[t0] != series.iloc[t0]:  # 当日无价
                continue
            r_fwd = ret_pct(symbol, t0, t1)
            if r_fwd is None:
                continue
            r1w = ret_pct(symbol, t0 - 5, t0)
            r1m = ret_pct(symbol, t0 - 21, t0)
            r1y = ret_pct(symbol, t0 - 252, t0)
            rytd = ret_pct(symbol, ytd_anchor, t0) if ytd_anchor < t0 else None
            eps = eps_at(symbol, asof)
            price = float(series.iloc[t0])
            tpe = price / eps if (eps and eps > 0) else None
            start = asof - timedelta(days=GRADE_LOOKBACK_DAYS)
            net = sum(s for d, s in grade_events.get(symbol, ()) if start < d <= asof)
            scores_by_symbol[symbol] = {
                "momentum": score_momentum(r1w, r1m, rytd, r1y),
                "valuation": score_lower_better(tpe, 25.0, 100.0),
                "reversal": score_reversal(r1m),
                "grade": grade_score_from_net(net),
            }
            fwd[symbol] = r_fwd
        if len(scores_by_symbol) < 30:
            continue
        bench_ret = ret_pct(BENCHMARK, t0, t1) or 0.0
        row: dict[str, Any] = {"asof": str(asof), "n": len(scores_by_symbol),
                               "bench_ret": round(bench_ret, 3)}
        curves[BENCHMARK].append(bench_ret)
        curves["universe_eqw"].append(sum(fwd.values()) / len(fwd))
        for name, weights in STRATEGIES.items():
            ranked = sorted(
                scores_by_symbol,
                key=lambda s: (-sum(w * scores_by_symbol[s][f] for f, w in weights.items()), s),
            )[:TOP_K]
            port_ret = sum(fwd[s] for s in ranked) / len(ranked)
            holdings = set(ranked)
            # 换手率 = 本期换掉的比例(首期建仓不计成本,与各策略公平)
            if prev_holdings[name]:
                turnover = 1.0 - len(holdings & prev_holdings[name]) / TOP_K
            else:
                turnover = 0.0
            prev_holdings[name] = holdings
            cost = turnover * 2.0 * COST_PER_SIDE_PCT  # 卖旧+买新双边
            turnovers[name].append(turnover)
            curves[name].append(port_ret)
            net_curves[name].append(port_ret - cost)
            row[name] = round(port_ret - bench_ret, 3)
        periods.append(row)

    def stats(name: str) -> dict[str, Any]:
        rets = curves[name]
        bench = curves[BENCHMARK]
        extra: dict[str, Any] = {}
        if name in net_curves and net_curves[name]:
            net_cum = 1.0
            for r in net_curves[name]:
                net_cum *= 1 + r / 100.0
            tos = turnovers[name]
            extra = {
                "net_cum_return_pct": round((net_cum - 1) * 100, 1),
                "avg_turnover_pct": round(sum(tos) / len(tos) * 100, 1) if tos else None,
            }
        cum = 1.0
        for r in rets:
            cum *= 1 + r / 100.0
        cum_bench = 1.0
        for r in bench:
            cum_bench *= 1 + r / 100.0
        alphas = [r - b for r, b in zip(rets, bench)]
        wins = sum(1 for a in alphas if a > 0)
        # 半年切片平均周 alpha
        halves: dict[str, list[float]] = {}
        for p, a in zip(periods, alphas):
            y, m = p["asof"][:4], int(p["asof"][5:7])
            halves.setdefault(f"{y}H{1 if m <= 6 else 2}", []).append(a)
        return {
            "n_periods": len(rets),
            "cum_return_pct": round((cum - 1) * 100, 1),
            **extra,
            "cum_vs_bench_pp": round((cum - cum_bench) * 100, 1),
            "avg_alpha_per_period_pct": round(sum(alphas) / len(alphas), 3) if alphas else None,
            "win_rate_vs_bench_pct": round(wins / len(alphas) * 100, 1) if alphas else None,
            "alpha_by_half_year": {k: round(sum(v) / len(v), 3) for k, v in sorted(halves.items())},
        }

    summary = {name: stats(name) for name in [*STRATEGIES, "universe_eqw"]}
    bench_cum = 1.0
    for r in curves[BENCHMARK]:
        bench_cum *= 1 + r / 100.0

    payload = {
        "schema_version": 1,
        "safety_boundary": "SHADOW_RESEARCH_ONLY",
        "window": f"{periods[0]['asof']} → {periods[-1]['asof']}",
        "rebalance_step_days": REBALANCE_STEP,
        "top_k": TOP_K,
        "benchmark": BENCHMARK,
        "benchmark_cum_return_pct": round((bench_cum - 1) * 100, 1),
        "strategies": summary,
        "periods": periods,
        "notes": [
            "估值分是 trailing PE 代理(生产 80% 权重在 forward 口径,历史买不起);方向参考。",
            "🚨 幸存者宇宙:所有绝对收益都被抬高,只有策略之间的相对比较有意义。",
            "动量/反转忠实复刻生产打分;data_usability/f_score 为常数项已略去(排名等价)。",
            "不计交易成本;每 5 交易日换仓 top-10 等权。",
        ],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 整套打分公式 · 两年代理回测",
        "",
        f"- 窗口: {payload['window']} · 每 {REBALANCE_STEP} 交易日换仓 top-{TOP_K} 等权 · 基准 {BENCHMARK}"
        f"(累计 {payload['benchmark_cum_return_pct']}%)",
        "",
        f"| 策略 | 毛收益% | 净收益%(扣{COST_PER_SIDE_PCT}%/边×双边换手) | 周换手% | vs QQQ(pp) | 周胜率% | 半年切片 alpha |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for name, s in summary.items():
        lines.append(
            f"| {name} | {s['cum_return_pct']} | {s.get('net_cum_return_pct', '—')} | "
            f"{s.get('avg_turnover_pct', '—')} | {s['cum_vs_bench_pp']} | "
            f"{s['win_rate_vs_bench_pct']} | {s['alpha_by_half_year']} |")
    lines += ["", *(f"- {n}" for n in payload["notes"])]
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"[OK] JSON → {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
