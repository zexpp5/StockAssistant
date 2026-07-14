#!/usr/bin/env python3
"""统一回测入口（SHADOW_RESEARCH_ONLY，只读）。

一条命令回答：「这套公式扣完手续费/印花税/滑点，还剩多少肉？」

用法：
  # 三市场生产公式，Top10，每 5 个交易日调一次仓
  /opt/homebrew/bin/python3 scripts/tools/run_backtest.py --hold-days 5

  # 指定市场+变体+调仓频率
  ... run_backtest.py --market US --variant val_down_grade --top-n 10 --hold-days 1

  # 任意自定义权重
  ... run_backtest.py --market CN --weights '{"reversal":0.6,"f_score":0.4}'

变体名单一来源 = replay_weight_variants.VARIANTS（不重复定义权重）。
产物: data/latest/unified_backtest.json （dashboard/早报以后可回灌）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

sys.path.insert(0, str(REPO / "scripts" / "tools"))

from stock_db import get_db  # noqa: E402
from stock_research.core.backtest_engine import (  # noqa: E402
    DEFAULT_COST_MODELS,
    run_market_backtest,
)
from replay_weight_variants import VARIANTS, weights_for_market  # noqa: E402

OUT_JSON = REPO / "data" / "latest" / "unified_backtest.json"

# 各市场"现用生产公式"对应的变体名（2026-07-14 三市场已全切挑战者）
PRODUCTION_VARIANTS = {"US": "val_down_grade", "HK": "quality_heavy", "CN": "cn_reversal_quality"}
MARKET_LABELS = {"US": "美股", "HK": "港股", "CN": "A股"}


def resolve_weights(args, market: str) -> tuple[dict[str, float], str]:
    if args.weights:
        return json.loads(args.weights), "custom"
    name = args.variant or PRODUCTION_VARIANTS[market]
    if name not in VARIANTS:
        raise SystemExit(f"未知变体 {name}；可选: {', '.join(sorted(VARIANTS))}")
    return weights_for_market(VARIANTS[name], market), name


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--market", choices=["US", "HK", "CN"], action="append",
                   help="市场（可多次；默认三市场全跑）")
    p.add_argument("--variant", help="变体名（默认=该市场现用生产公式）")
    p.add_argument("--weights", help='自定义权重 JSON，如 \'{"reversal":0.6,"f_score":0.4}\'')
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--hold-days", type=int, default=1, help="每几个交易日调一次仓")
    p.add_argument("--start", help="起始日 YYYY-MM-DD（默认快照最早）")
    p.add_argument("--end", help="结束日")
    p.add_argument("--no-write", action="store_true", help="只打印，不写 JSON 产物")
    args = p.parse_args()

    markets = args.market or ["US", "HK", "CN"]
    conn = get_db(force_read_only=True)

    report: dict = {
        "schema_version": "unified_backtest_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "safety_boundary": "只读回测；不改公式、不写持仓、不碰真钱。研究参考非投资建议。",
        "caliber": "PIT全宇宙快照(factor_snapshot_universe) + 收盘价 + 扣三市场真实交易成本",
        "params": {"top_n": args.top_n, "hold_days": args.hold_days},
        "markets": {},
    }

    print(f"\n{'='*74}")
    print(f"统一回测 · TopN={args.top_n} · 每{args.hold_days}个交易日调仓 · 扣真实成本")
    print(f"{'='*74}")

    for mkt in markets:
        weights, wname = resolve_weights(args, mkt)
        gross, net = run_market_backtest(
            conn, market=mkt, weights=weights, top_n=args.top_n,
            hold_days=args.hold_days, start=args.start, end=args.end,
        )
        cm = DEFAULT_COST_MODELS[mkt]
        print(f"\n【{MARKET_LABELS[mkt]}】公式={wname}  权重={json.dumps(weights, ensure_ascii=False)}")
        if gross.n_days == 0:
            print(f"  ⚠️ {'; '.join(gross.notes) or '无数据'}")
            continue
        print(f"  窗口: {gross.dates[0]} → {gross.dates[-1]} ({gross.n_days} 个交易日, 调仓 {gross.n_rebalances} 次)")
        print(f"  毛收益: {gross.gross_total_pct:+.2f}%   基准: {gross.benchmark_total_pct:+.2f}%   毛alpha: {gross.gross_alpha_pct:+.2f}%")
        print(f"  净收益: {net.net_total_pct:+.2f}%   净alpha: {net.net_alpha_pct:+.2f}%   ← 扣成本后")
        print(f"  成本吃掉: {net.cost_drag_pct:.2f}个点  (成本模型: {cm.label})")
        print(f"  换手: 平均每次调仓换 {net.avg_turnover_pct:.0f}%  年化换 {net.annualized_turnover_x:.1f} 轮  总买卖 {net.total_trades} 笔")
        for note in net.notes:
            print(f"  · {note}")

        report["markets"][mkt] = {
            "variant": wname,
            "weights": weights,
            "window": [gross.dates[0], gross.dates[-1]],
            "n_days": gross.n_days,
            "n_rebalances": gross.n_rebalances,
            "gross_total_pct": gross.gross_total_pct,
            "net_total_pct": net.net_total_pct,
            "benchmark_total_pct": gross.benchmark_total_pct,
            "gross_alpha_pct": gross.gross_alpha_pct,
            "net_alpha_pct": net.net_alpha_pct,
            "cost_drag_pct": net.cost_drag_pct,
            "cost_model": cm.label,
            "avg_turnover_pct": net.avg_turnover_pct,
            "annualized_turnover_x": net.annualized_turnover_x,
            "total_trades": net.total_trades,
            "notes": net.notes,
        }

    print(f"\n{'='*74}\n口径: 收盘价进出 · TopN等权 · PIT快照宇宙(无幸存者回填) · 保守成本\n")

    if not args.no_write:
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"产物: {OUT_JSON.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
