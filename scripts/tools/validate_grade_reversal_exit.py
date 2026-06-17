#!/usr/bin/env python3
"""验证"评级+反转 + 冲高退出"规则 —— 用已有前向样本,不发明新机制。

背景（2026-06-17，用户问"alpha 怎么转正"后）：
- 因子端：评级(grade)与反转(reversal)是仅有的两个 IC 验证通过的因子，组合
  已落地为夜班权重变体 `rev_grade_5050`（reversal .5 / grade .5）。无需新变体。
- 退出端："冲高退出"不是打分权重，而是"持有多久"。本系统对每个 source run 的
  每只票都已算 1d/5d/20d outcome → "冲高就走" 在数据上等价于"只看 1 日"。

本脚本把这两半拎成一个可追踪指标：取 rev_grade_5050 每个 source run 的分市场
top-N 买入票，比较【1 日持有(冲高退出)】vs【5 日持有】的 alpha 与胜率。回答：
"用验证过的因子 + 冲高就走，alpha 能不能转正？"

安全边界：只读 pick_outcomes + shadow 归档，绝不改生产策略/推荐/持仓。
非投资建议，研究诊断用。门槛(alpha>0)沿用 activation_decision，不在此处挪门柱。
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import stock_db  # noqa: E402
from scripts.tools.evaluate_shadow_tuning_run import (  # noqa: E402
    BUY_SIGNALS,
    SHADOW_ARCHIVE_DIR,
    _is_weekend_source_run,
    _read_json,
    _source_run_id,
)

VARIANT = "rev_grade_5050"
TOP_N = 10                 # 每市场每批取前 N 名(组合规模)
HORIZONS = ("1d", "5d")    # 冲高退出=1d / 拿到第五天=5d
OUT_PATH = REPO / "data" / "latest" / "grade_reversal_exit_validation.json"
MARKET_LABELS = {"US": "美股", "HK": "港股", "CN": "A股"}


def _load_variant_runs() -> list[dict[str, Any]]:
    """只取 rev_grade_5050 的归档 run，剔除周末 source run（永远评不出 outcome）。"""
    runs: dict[str, dict[str, Any]] = {}
    if SHADOW_ARCHIVE_DIR.exists():
        for path in sorted(SHADOW_ARCHIVE_DIR.glob("shadow_*.json")):
            payload = _read_json(path)
            if not payload or not payload.get("run_id"):
                continue
            tags = payload.get("shadow_tags") or []
            if payload.get("weight_variant") == VARIANT or f"weight_variant:{VARIANT}" in tags:
                runs[str(payload["run_id"])] = payload
    return [r for r in runs.values() if not _is_weekend_source_run(r)]


def _top_picks(run: dict[str, Any]) -> list[tuple[str, str]]:
    """该 run 每个市场按 shadow_market_rank 取前 TOP_N 的买入票 → [(market, symbol)]。"""
    by_market: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for pick in run.get("picks") or []:
        market = str(pick.get("market") or "").upper()
        symbol = str(pick.get("symbol") or "")
        if not market or not symbol:
            continue
        if str(pick.get("shadow_signal") or "") not in BUY_SIGNALS:
            continue
        rank = pick.get("shadow_market_rank")
        if rank is None:
            continue
        by_market[market].append((float(rank), symbol))
    out: list[tuple[str, str]] = []
    for market, rows in by_market.items():
        for _rank, symbol in sorted(rows)[:TOP_N]:
            out.append((market, symbol))
    return out


def _fetch_outcomes(source_run_ids: list[str]) -> dict[tuple[str, str, str, str], float]:
    """(run_id, market, symbol, horizon) -> alpha_pct，force_read_only 防写锁段落静默丢。"""
    if not source_run_ids:
        return {}
    conn = stock_db.get_db(force_read_only=True)
    try:
        ph = ",".join(["?"] * len(source_run_ids))
        hz = ",".join(["?"] * len(HORIZONS))
        rows = conn.execute(
            f"""
            SELECT run_id, market, symbol, horizon, alpha_pct
            FROM pick_outcomes
            WHERE run_id IN ({ph}) AND horizon IN ({hz})
              AND alpha_pct IS NOT NULL AND isfinite(alpha_pct)
            """,
            [*source_run_ids, *HORIZONS],
        ).fetchall()
    finally:
        conn.close()
    return {(str(r[0]), str(r[1]), str(r[2]), str(r[3])): float(r[4]) for r in rows}


def _summarize(alphas: list[float]) -> dict[str, Any]:
    n = len(alphas)
    if not n:
        return {"n": 0, "avg_alpha_pct": None, "win_rate_pct": None}
    wins = sum(1 for a in alphas if a > 0)
    return {
        "n": n,
        "avg_alpha_pct": round(sum(alphas) / n, 4),
        "win_rate_pct": round(wins / n * 100, 2),
    }


def build_report() -> dict[str, Any]:
    runs = _load_variant_runs()
    source_ids = sorted({sid for r in runs if (sid := _source_run_id(r))})
    outcomes = _fetch_outcomes(source_ids)

    # market -> horizon -> [alpha]
    buckets: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for run in runs:
        sid = _source_run_id(run)
        if not sid:
            continue
        for market, symbol in _top_picks(run):
            for horizon in HORIZONS:
                alpha = outcomes.get((sid, market, symbol, horizon))
                if alpha is not None:
                    buckets[market][horizon].append(alpha)

    markets = []
    for market in ("US", "HK", "CN"):
        h1 = _summarize(buckets[market]["1d"])
        h5 = _summarize(buckets[market]["5d"])
        verdict = "样本不足"
        if h1["n"] and h1["avg_alpha_pct"] is not None:
            if h1["avg_alpha_pct"] > 0 and (h5["avg_alpha_pct"] is None or h1["avg_alpha_pct"] > h5["avg_alpha_pct"]):
                verdict = "冲高退出(1日)正且优于持有5日 → 规则有效"
            elif h1["avg_alpha_pct"] > 0:
                verdict = "1日为正但未明显优于5日"
            else:
                verdict = "1日仍为负 → 该规则未转正"
        markets.append({
            "market": market,
            "label": MARKET_LABELS.get(market, market),
            "hold_1d_exit_on_pop": h1,
            "hold_5d": h5,
            "verdict": verdict,
        })

    return {
        "schema_version": "grade_reversal_exit_v1",
        "rule": "评级+反转(rev_grade_5050) + 冲高退出(持有1日)",
        "safety_boundary": "只读 shadow 归档 + pick_outcomes;不改生产策略/推荐/持仓。研究诊断,非投资建议。",
        "variant": VARIANT,
        "top_n_per_market": TOP_N,
        "source_run_count": len(source_ids),
        "shadow_run_count": len(runs),
        "markets": markets,
        "note": "门槛 alpha>0 沿用 activation_decision;样本仍在累积,1日窗口需下一交易日收盘成熟。",
    }


def main() -> int:
    report = build_report()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"规则: {report['rule']}")
    print(f"样本: {report['shadow_run_count']} 个 shadow run / {report['source_run_count']} 个 source run")
    print(f"{'市场':6}{'1日持有(冲高退出)':>22}{'5日持有':>18}  判定")
    for m in report["markets"]:
        h1, h5 = m["hold_1d_exit_on_pop"], m["hold_5d"]
        a1 = f"{h1['avg_alpha_pct']:+.2f}%×{h1['n']}({h1['win_rate_pct']:.0f}%)" if h1["n"] else "—"
        a5 = f"{h5['avg_alpha_pct']:+.2f}%×{h5['n']}" if h5["n"] else "—"
        print(f"{m['label']:6}{a1:>22}{a5:>18}  {m['verdict']}")
    print(f"JSON: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
