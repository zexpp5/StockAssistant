"""Kelly Fraction 消融测试 — 跑 optimize_portfolio.run() 多个 kelly_fraction，
对比关键指标，输出 markdown 报告。

用法：
    python3 scripts/tools/ablate_kelly.py
    python3 scripts/tools/ablate_kelly.py --fractions 0 0.25 0.5 0.75 1.0
    python3 scripts/tools/ablate_kelly.py --out docs/ablation_kelly_2026-05-12.md

输出指标：
  - kelly 单股 cap = max_weight × kelly_fraction
  - kelly_clipped 触发数（被压的股票数）
  - 现金占比（仓位减少 → cash 增加）
  - 组合 Sharpe / 年化收益 / 年化波动（线性缩放后 Sharpe 不变，但收益/波动变）
  - 单股最大权重（实际 cap 后）
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def run_one(kelly_fraction: float) -> dict | None:
    """跑一次 optimize_portfolio.run(kelly_fraction=...) 收集关键指标。"""
    from stock_research.jobs.optimize_portfolio import run
    try:
        r = run(kelly_fraction=kelly_fraction)
    except Exception as e:
        return {"error": f"exception: {e}", "kelly_fraction": kelly_fraction}
    if "error" in r:
        return {"error": r["error"], "kelly_fraction": kelly_fraction}
    return r


def summarize(r: dict, kf: float) -> dict:
    """从 run() 结果提取对比用关键字段。"""
    plan = r.get("plan", [])
    metrics = r.get("portfolio_metrics", {})
    constraints = r.get("constraints", {})
    max_weight = constraints.get("max_weight", 0.15)
    deployed = sum(p.get("capped_weight", 0) for p in plan)
    max_single = max((p.get("capped_weight", 0) for p in plan), default=0.0)
    return {
        "kelly_fraction": kf,
        "kelly_cap": max_weight * kf if kf > 0 else None,
        "n_kelly_clipped": len(r.get("kelly_clipped", [])),
        "deployed_pct": round(deployed * 100, 2),
        "cash_pct": round((1 - deployed) * 100, 2),
        "max_single_pct": round(max_single * 100, 2),
        "annual_sharpe": metrics.get("annual_sharpe"),
        "annual_return_pct": metrics.get("annual_return_pct"),
        "annual_vol_pct": metrics.get("annual_vol_pct"),
        "net_alpha_pct": metrics.get("net_alpha_pct"),
        "n_holdings": len(plan),
    }


def main():
    p = argparse.ArgumentParser(description="Kelly fraction 消融测试")
    p.add_argument("--fractions", type=float, nargs="*",
                   default=[0.0, 0.25, 0.5, 0.75, 1.0],
                   help="测试的 kelly_fraction 列表")
    p.add_argument("--out", default=None,
                   help="输出 markdown 路径（默认 docs/ablation_kelly_YYYY-MM-DD.md）")
    args = p.parse_args()

    print(f"=== Kelly Fraction 消融测试 ({len(args.fractions)} 档) ===\n")
    summaries: list[dict] = []
    for kf in args.fractions:
        print(f"\n{'='*60}\n  跑 kelly_fraction = {kf}\n{'='*60}")
        r = run_one(kf)
        if r is None or "error" in (r or {}):
            print(f"  ❌ 失败: {(r or {}).get('error', 'unknown')}")
            continue
        s = summarize(r, kf)
        summaries.append(s)
        print(f"  ✅ Sharpe={s['annual_sharpe']} 收益={s['annual_return_pct']:+.1f}% "
              f"波动={s['annual_vol_pct']:.1f}% / kelly 触发={s['n_kelly_clipped']} "
              f"/ 现金={s['cash_pct']:.1f}%")

    if not summaries:
        print("\n❌ 所有档位失败，未生成报告")
        return 1

    # 写 markdown
    out_path = Path(args.out) if args.out else (
        REPO / "docs" / f"ablation_kelly_{datetime.now():%Y-%m-%d}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    lines.append("# Kelly Fraction 消融测试")
    lines.append("")
    lines.append(f"**时间**：{datetime.now():%Y-%m-%d %H:%M}")
    lines.append(f"**档位**：{', '.join(str(f) for f in args.fractions)}")
    lines.append("")
    lines.append("## 实测对比")
    lines.append("")
    lines.append("| kelly_fraction | 单股 cap | 触发数 | 投入仓位 | 现金 | 单股最大 | Sharpe | 年化收益 | 年化波动 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for s in summaries:
        cap = f"{s['kelly_cap']*100:.1f}%" if s['kelly_cap'] is not None else "—"
        lines.append(
            f"| **{s['kelly_fraction']}** | {cap} | {s['n_kelly_clipped']} | "
            f"{s['deployed_pct']}% | {s['cash_pct']}% | {s['max_single_pct']}% | "
            f"{s['annual_sharpe']} | {s['annual_return_pct']:+.1f}% | {s['annual_vol_pct']:.1f}% |"
        )
    lines.append("")
    lines.append("## 解读")
    lines.append("")
    lines.append("- **kelly_fraction=0 或 1.0 等价于关闭**：单股 cap = max_weight 时永远不触发")
    lines.append("- **kelly_fraction=0.5 是有效的半 Kelly**：单股 cap 收紧到 max_weight × 0.5")
    lines.append("- **Sharpe 不变是预期行为**：Kelly cap 是线性缩放，比值类指标不变")
    lines.append("- **真实价值在单股暴雷场景**：单股 -50% → cap 0.5 时组合 -3.75% vs 满 cap -7.5%")
    lines.append("- **机会成本**：减仓部分按 rf=4.5% 按现金收益，部分抵消")
    lines.append("")
    lines.append("## 待办")
    lines.append("")
    lines.append("- 12-24 月 walk-forward 含/不含对比（待 walk_forward 接入 kelly_cap）")
    lines.append("- 黑天鹅事件压力测试（人为构造单股 -30% 一周）")
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n✅ 报告输出：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
