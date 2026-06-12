#!/usr/bin/env python3
"""权重变体历史回放（SHADOW_RESEARCH_ONLY，只读，不碰生产）。

§19.3 第二步的回测引擎：不等新数据，直接用已落库的历史 picks 回答
「如果当初用不同的因子权重，选出来的组合样本外 alpha 会不会更好」。

口径（与 strategy_eval / evaluate_shadow_tuning_run 对齐）：
  - 样本 = recommendation_picks 每天最后一批（2026-05-25 可信 cutoff 起）
  - 因子分 = factor_scores_json 落库时的 PIT 快照（不重抓数据，无未来函数）
  - outcome = pick_outcomes 收盘价 alpha（1d/5d），指标复用 summarize_distribution

已知边界（报告里也写）：
  1. 池内重排：每天每市场只存了生产 top-20，变体只能在这 20 只里重挑 top-K，
     发现不了池外股票 → 对变体是「保守估计」（变体真实上线后可选全宇宙）。
  2. 窗口短（~13 个交易日），统计功效低，结论当方向参考而非定论。
  3. 某 pick 缺某因子分时记中性 50（reversal 5-27 才上线、f_score A 股覆盖 67%）。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import get_db  # noqa: E402
from stock_research.core.strategy_eval import summarize_distribution  # noqa: E402

OUT_JSON = REPO / "data" / "latest" / "weight_replay_report.json"
OUT_MD = REPO / "data" / "reports" / "weight_replay_report.md"

MARKET_LABELS = {"US": "美股", "CN": "A股", "HK": "港股"}
CUTOFF = "2026-05-25"  # 生产统计可信样本起点（memory: project_metrics_cutoff）
NEUTRAL = 50.0
HORIZONS = ("1d", "5d")

# 因子键 → factor_scores_json 里的取值链（前者缺则取后者）
FACTOR_KEYS: dict[str, tuple[str, ...]] = {
    "momentum": ("momentum",),
    "valuation": ("valuation",),
    "reversal": ("reversal",),
    "f_score": ("f_score",),
    "data_usability": ("data_usability", "data_quality"),
    # grade = 评级净上调(美股 only,analyst_grade_events 注入;CN/HK 中性 50)
    # 2026-06-12 IC 验证 PASS: 20d IC=+0.042 t=2.25,五个半年切片全正
    "grade": ("grade",),
}

GRADE_LOOKBACK_DAYS = 30


def grade_score_from_net(net_upgrades: int) -> float:
    """评级净上调 → 0-100 分(预注册公式,单一来源,shadow 机器复用)。

    net=±4 封顶:50 + 12.5×net。多数票多数日 net=0 → 中性 50。
    """
    return max(0.0, min(100.0, 50.0 + 12.5 * float(net_upgrades)))


# 变体矩阵：生产基线 + 结构性改法 + 单因子消融
VARIANTS: dict[str, dict[str, float]] = {
    # 复算版生产权重（校验回放引擎本身；与 prod_rank 基线应大致一致）
    "prod_recheck": {"momentum": 0.15, "valuation": 0.50, "reversal": 0.15, "data_usability": 0.20},
    # 结构性改法：降估值主导 + 质量(f_score)入场 + 数据完整度移出
    "val_down_quality": {"momentum": 0.15, "valuation": 0.35, "reversal": 0.25, "f_score": 0.25},
    "val_down_mild": {"momentum": 0.15, "valuation": 0.45, "reversal": 0.20, "f_score": 0.20},
    "quality_heavy": {"momentum": 0.10, "valuation": 0.25, "reversal": 0.25, "f_score": 0.40},
    "no_valuation": {"momentum": 0.20, "reversal": 0.40, "f_score": 0.40},
    "equal_4": {"momentum": 0.25, "valuation": 0.25, "reversal": 0.25, "f_score": 0.25},
    # 第三变体(2026-06-12): val_down_mild 把估值再让 0.20 给已 PASS 的评级因子
    "val_down_grade": {"momentum": 0.15, "valuation": 0.25, "reversal": 0.20,
                       "f_score": 0.20, "grade": 0.20},
    # 单因子消融：定位 alpha/毒性来源
    "valuation_pure": {"valuation": 1.0},
    "momentum_pure": {"momentum": 1.0},
    "reversal_pure": {"reversal": 1.0},
    "fscore_pure": {"f_score": 1.0},
    "grade_pure": {"grade": 1.0},
}


def _factor_value(scores: dict[str, Any], factor: str) -> float | None:
    for key in FACTOR_KEYS.get(factor, (factor,)):
        value = scores.get(key)
        if value is None:
            continue
        try:
            v = float(value)
        except Exception:
            continue
        if v == v:  # not NaN
            return v
    return None


def variant_score(scores: dict[str, Any], weights: dict[str, float]) -> tuple[float, int]:
    """返回 (加权分, 缺失因子数)。缺失因子记中性 50。"""
    total = 0.0
    missing = 0
    for factor, weight in weights.items():
        value = _factor_value(scores, factor)
        if value is None:
            value = NEUTRAL
            missing += 1
        total += weight * value
    return total, missing


def inject_grade_scores(conn, picks: list[dict[str, Any]]) -> int:
    """按 run_date 从 analyst_grade_events 注入 PIT 正确的 grade 分(美股)。

    表不存在/为空时静默跳过(grade 因子缺失 → variant_score 记中性 50)。
    """
    try:
        rows = conn.execute(
            """
            SELECT symbol, event_date,
                   CASE WHEN lower(coalesce(action,''))='upgrade' THEN 1 ELSE -1 END AS sign
            FROM analyst_grade_events
            WHERE market='US' AND lower(coalesce(action,'')) IN ('upgrade','downgrade')
            """
        ).fetchall()
    except Exception:
        return 0
    from collections import defaultdict as _dd
    from datetime import date as _date, timedelta as _td
    ev: dict[str, list[tuple[Any, int]]] = _dd(list)
    for symbol, d, sign in rows:
        ev[str(symbol)].append((d, int(sign)))
    injected = 0
    for pick in picks:
        if pick["market"] != "US":
            continue
        asof = _date.fromisoformat(pick["run_date"])
        start = asof - _td(days=GRADE_LOOKBACK_DAYS)
        net = sum(s for d, s in ev.get(pick["symbol"], ()) if start < d <= asof)
        pick["scores"]["grade"] = grade_score_from_net(net)
        injected += 1
    return injected


def load_picks(conn) -> list[dict[str, Any]]:
    """每天最后一批的全部 picks（排除 eligibility=excluded），带 1d/5d outcome。"""
    rows = conn.execute(
        """
        WITH last_runs AS (
            SELECT run_date, max(generated_at) AS g
            FROM recommendation_runs
            WHERE run_date >= CAST(? AS DATE)
            GROUP BY run_date
        ),
        runs AS (
            SELECT rr.run_id, rr.run_date, rr.strategy_version
            FROM recommendation_runs rr
            JOIN last_runs lr ON rr.run_date = lr.run_date AND rr.generated_at = lr.g
        )
        SELECT runs.run_date, runs.run_id, runs.strategy_version,
               rp.market, rp.symbol, rp.name, rp.rank, rp.total_score,
               rp.factor_scores_json, rp.eligibility,
               po1.alpha_pct AS alpha_1d, po5.alpha_pct AS alpha_5d
        FROM recommendation_picks rp
        JOIN runs ON runs.run_id = rp.run_id
        LEFT JOIN pick_outcomes po1
          ON po1.run_id = rp.run_id AND po1.market = rp.market
         AND po1.symbol = rp.symbol AND po1.horizon = '1d'
         AND po1.alpha_pct IS NOT NULL AND isfinite(po1.alpha_pct)
        LEFT JOIN pick_outcomes po5
          ON po5.run_id = rp.run_id AND po5.market = rp.market
         AND po5.symbol = rp.symbol AND po5.horizon = '5d'
         AND po5.alpha_pct IS NOT NULL AND isfinite(po5.alpha_pct)
        WHERE coalesce(rp.eligibility, '') != 'excluded'
        ORDER BY runs.run_date, rp.market, rp.rank
        """,
        [CUTOFF],
    ).fetchall()
    picks = []
    for (run_date, run_id, version, market, symbol, name, rank, total_score,
         fj, eligibility, alpha_1d, alpha_5d) in rows:
        try:
            scores = json.loads(fj) if isinstance(fj, str) else (fj or {})
        except Exception:
            scores = {}
        picks.append({
            "run_date": str(run_date),
            "run_id": run_id,
            "strategy_version": version,
            "market": market,
            "symbol": symbol,
            "name": name,
            "rank": rank,
            "total_score": total_score,
            "scores": scores,
            "alpha_1d": float(alpha_1d) if alpha_1d is not None else None,
            "alpha_5d": float(alpha_5d) if alpha_5d is not None else None,
        })
    return picks


def replay(picks: list[dict[str, Any]], top_k: int) -> dict[str, Any]:
    by_day_market: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for pick in picks:
        by_day_market[(pick["run_date"], pick["market"])].append(pick)

    results: dict[str, Any] = {}
    all_variants = {"prod_rank": None, **VARIANTS}
    for vname, weights in all_variants.items():
        per_market: dict[str, dict[str, Any]] = {}
        selections: dict[str, list[dict[str, Any]]] = defaultdict(list)
        overlap_sum: dict[str, float] = defaultdict(float)
        day_count: dict[str, int] = defaultdict(int)
        missing_total = 0
        for (run_date, market), pool in sorted(by_day_market.items()):
            prod_top = sorted(pool, key=lambda p: (p["rank"] is None, p["rank"]))[:top_k]
            if weights is None:
                chosen = prod_top
            else:
                scored = []
                for p in pool:
                    s, miss = variant_score(p["scores"], weights)
                    missing_total += miss
                    scored.append((s, p))
                scored.sort(key=lambda t: (-t[0], t[1]["symbol"]))
                chosen = [p for _, p in scored[:top_k]]
            prod_set = {p["symbol"] for p in prod_top}
            overlap_sum[market] += len([p for p in chosen if p["symbol"] in prod_set]) / max(len(chosen), 1)
            day_count[market] += 1
            selections[market].extend(chosen)

        for market, chosen in selections.items():
            stats: dict[str, Any] = {}
            for horizon in HORIZONS:
                samples = [{"alpha_pct": p[f"alpha_{horizon}"]} for p in chosen
                           if p.get(f"alpha_{horizon}") is not None]
                summary = summarize_distribution(samples)
                summary["selected"] = len(chosen)
                summary["outcome_coverage_pct"] = round(
                    len(samples) / max(len(chosen), 1) * 100.0, 1)
                stats[horizon] = summary
            stats["overlap_with_prod_pct"] = round(
                overlap_sum[market] / max(day_count[market], 1) * 100.0, 1)
            stats["days"] = day_count[market]
            per_market[market] = stats
        results[vname] = {
            "weights": weights,
            "per_market": per_market,
            "missing_factor_fills": missing_total,
        }
    return results


def render_md(results: dict[str, Any], top_k: int, n_picks: int) -> str:
    lines = [
        "# 权重变体历史回放报告（SHADOW_RESEARCH_ONLY）",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 样本：{CUTOFF} 起每天最后一批生产 picks（共 {n_picks} 条 pick 记录），每市场每日重挑 top-{top_k}",
        "- 口径：pick_outcomes 收盘价样本外 alpha；指标复用 strategy_eval.summarize_distribution",
        "- ⚠️ 边界：池内重排（只能在生产 top-20 里挑）、窗口短、缺因子记中性 50。",
        "  对变体是保守估计；结论用于挑「值得上 shadow 机器长跑」的方向，不直接改生产。",
        "",
    ]
    for horizon in HORIZONS:
        lines.append(f"## {horizon} 样本外 alpha（平均 / 中位 / 命中率 / 去最佳后平均）")
        lines.append("")
        header = "| 变体 | " + " | ".join(
            f"{MARKET_LABELS.get(m, m)}" for m in ("CN", "HK", "US")) + " |"
        lines.append(header)
        lines.append("|---|---|---|---|")
        for vname, payload in results.items():
            cells = []
            for market in ("CN", "HK", "US"):
                stats = payload["per_market"].get(market, {}).get(horizon)
                if not stats or not stats.get("n"):
                    cells.append("—")
                    continue
                cells.append(
                    f"{stats['avg_alpha_pct']:+.2f} / {stats['median_alpha_pct']:+.2f} / "
                    f"{stats['win_rate_pct']:.0f}% / {stats['avg_without_best_alpha_pct']:+.2f} (n={stats['n']})")
            lines.append(f"| {vname} | " + " | ".join(cells) + " |")
        lines.append("")
    lines.append("## 变体与生产组合的重合度")
    lines.append("")
    lines.append("| 变体 | A股 | 港股 | 美股 |")
    lines.append("|---|---|---|---|")
    for vname, payload in results.items():
        cells = [
            f"{payload['per_market'].get(m, {}).get('overlap_with_prod_pct', 0):.0f}%"
            for m in ("CN", "HK", "US")
        ]
        lines.append(f"| {vname} | " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()

    conn = get_db(force_read_only=True)
    try:
        picks = load_picks(conn)
        inject_grade_scores(conn, picks)
    finally:
        conn.close()
    if not picks:
        print("没有可回放的 picks", file=sys.stderr)
        return 1

    results = replay(picks, args.top_k)
    payload = {
        "schema_version": 1,
        "safety_boundary": "SHADOW_RESEARCH_ONLY",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cutoff": CUTOFF,
        "top_k": args.top_k,
        "n_pick_records": len(picks),
        "variants": results,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md = render_md(results, args.top_k, len(picks))
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(md, encoding="utf-8")
    print(md)
    print(f"[OK] JSON → {OUT_JSON}")
    print(f"[OK] MD   → {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
