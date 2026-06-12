#!/usr/bin/env python3
"""trailing 估值因子（E/P）长窗口 IC 验证（SHADOW_RESEARCH_ONLY，只读）。

回答的问题:「便宜」在这个 AI 宇宙里到底有没有预测力 —— 用买得到的数据
口径(trailing),把回放只有 13 天的估值结论拉长到 ~2 年。

口径(与学术价值因子标准一致):
  - E = yfinance 年报 diluted EPS(4 个财年),P = price_daily 收盘价
  - PIT: 财年 EPS 在 财年结束 + 90 天 后才可用(保守滞后,防未来函数)
  - 截面: 每 20 个交易日一个(与 20d 前瞻不重叠,t 统计量不虚高)
  - E/P 而非 PE: 负盈利自然落到因子底部,不用特判

诚实边界:
  1. trailing 口径 ≠ 生产公式的 forward 口径(PEG/forward PE 占 80% 权重,
     其历史只在机构库里) —— 本结论是方向参考,不是 forward 口径的代理证明。
  2. 宇宙 = 今天的 140 只(幸存者偏差,且都是已被选出的 AI 赢家)。
  3. 年报 EPS 对最近一年偏陈旧(最长滞后 ~15 个月) —— 价值因子本就是慢因子,
     学术口径同样如此(Fama-French 用年度数据 + 6 个月滞后)。
判定标准(跑前写死): 主口径 20d,PASS = 平均 IC ≥ +0.03 且 |t| ≥ 2;
  WEAK = ≥ +0.015 且 |t| ≥ 1;FAIL = 其余。负到过线则记 FAIL_NEGATIVE
  (= "贵的反而涨",对生产公式是更强的警报)。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import get_db  # noqa: E402
from scripts.tools.validate_revision_ic import spearman_ic, quintile_spread  # noqa: E402

OUT_JSON = REPO / "data" / "latest" / "value_ic_validation.json"
OUT_MD = REPO / "data" / "reports" / "value_ic_validation.md"

EPS_LAG_DAYS = 90          # 财年 EPS 公布滞后假设
SECTION_STEP = 20          # 截面间隔(交易日)=主前瞻,不重叠
HORIZONS = (5, 20)
PRIMARY_HORIZON = 20
PASS_IC, PASS_T = 0.03, 2.0
WEAK_IC, WEAK_T = 0.015, 1.0


def fetch_annual_eps(symbols: list[str], *, sleep_sec: float = 0.2) -> dict[str, list[tuple[Any, float]]]:
    """yfinance 年报 diluted EPS。返回 {symbol: [(财年结束日, eps), ...] 按日期升序}。"""
    import yfinance as yf

    out: dict[str, list[tuple[Any, float]]] = {}
    for i, symbol in enumerate(symbols):
        try:
            stmt = yf.Ticker(symbol).income_stmt
        except Exception:
            stmt = None
        if stmt is not None and not getattr(stmt, "empty", True) and "Diluted EPS" in stmt.index:
            pairs = []
            for col in stmt.columns:
                try:
                    eps = float(stmt.loc["Diluted EPS", col])
                except Exception:
                    continue
                if eps == eps:  # not NaN
                    pairs.append((col.date() if hasattr(col, "date") else col, eps))
            if pairs:
                out[symbol] = sorted(pairs)
        if (i + 1) % 25 == 0:
            print(f"  年报 EPS 进度 {i+1}/{len(symbols)}", flush=True)
        time.sleep(sleep_sec)
    return out


def main() -> int:
    conn = get_db(force_read_only=True)
    try:
        symbols = [str(r[0]) for r in conn.execute(
            "SELECT DISTINCT symbol FROM system_universe WHERE market='US' AND active ORDER BY symbol"
        ).fetchall()]
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

    print(f"拉取 {len(symbols)} 只年报 EPS(yfinance)…", flush=True)
    eps_hist = fetch_annual_eps(symbols)
    print(f"有年报 EPS 的: {len(eps_hist)} 只", flush=True)

    def eps_available_at(symbol: str, asof) -> float | None:
        """asof 当天「已公布」的最近财年 EPS(财年结束 + 90 天滞后)。"""
        best = None
        for fy_end, eps in eps_hist.get(symbol, []):
            if fy_end + timedelta(days=EPS_LAG_DAYS) <= asof:
                best = eps
        return best

    # 截面:从有 20d 前瞻的最早日期到最新,每 SECTION_STEP 个交易日
    results: list[dict[str, Any]] = []
    max_h = max(HORIZONS)
    for t0 in range(0, len(trade_dates) - max_h, SECTION_STEP):
        t0_date = trade_dates[t0]
        factor_syms: list[str] = []
        factor: list[float] = []
        for symbol in symbols:
            series = closes.get(symbol) or {}
            p0 = series.get(t0_date)
            eps = eps_available_at(symbol, t0_date)
            if p0 and eps is not None and p0 > 0:
                factor_syms.append(symbol)
                factor.append(eps / p0 * 100.0)  # E/P %,越高越"便宜"
        if len(factor) < 30:
            continue
        row: dict[str, Any] = {"asof": str(t0_date), "n": len(factor)}
        for horizon in HORIZONS:
            t1_date = trade_dates[t0 + horizon]
            fwd = []
            keep = []
            for symbol, fv in zip(factor_syms, factor):
                p0 = closes[symbol][t0_date]
                p1 = (closes.get(symbol) or {}).get(t1_date)
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
            verdict = "FAIL_NEGATIVE"  # 显著为负:"贵的反而涨" — 对生产公式是更强警报

    # 半年期稳定性切片(抓 4 月/5 月翻脸那类 regime 依赖)
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
        "factor": "trailing E/P(年报 EPS,90 天滞后)",
        "primary_horizon_days": PRIMARY_HORIZON,
        "primary_summary": primary,
        "summary_5d": _summary(5),
        "stability_by_half_year": stability,
        "criteria": {"pass": f"mean IC≥{PASS_IC} 且 t≥{PASS_T}",
                     "weak": f"mean IC≥{WEAK_IC} 且 t≥{WEAK_T}",
                     "fail_negative": f"mean IC≤-{PASS_IC} 且 t≤-{PASS_T}"},
        "n_symbols_with_eps": len(eps_hist),
        "sections": results,
        "notes": [
            "trailing 口径≠生产 forward 口径(PEG/forward PE 占 80%):方向参考非代理证明。",
            "宇宙=今日 140 只 AI 赢家(幸存者偏差,值越好越要打折看)。",
            "截面间隔=20 交易日与前瞻不重叠,t 统计量为独立近似。",
        ],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# trailing 估值因子(E/P) · 长窗口 IC 验证",
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
    print("\n".join(lines[:12]))
    print(f"…(全表见 {OUT_MD})")
    print(f"[OK] JSON → {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
