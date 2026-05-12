"""通用消融测试脚本 — 给任何 walk_forward boolean flag 跑 baseline vs +flag 对照。

替代 ablate_kelly.py 单维专版，支持任何 walk_forward 参数的消融：
    kelly_cap / atr_stop / bab_defense / 未来新加的开关。

用法：
    # 单 flag 消融
    python3 scripts/tools/ablate.py --flag enable_kelly_cap
    python3 scripts/tools/ablate.py --flag enable_atr_stop --atr-stop-pct 0.10

    # 多 flag 矩阵（笛卡尔积，每组都跑一次）
    python3 scripts/tools/ablate.py --flag enable_kelly_cap enable_atr_stop --grid

    # 自定义窗口 / universe
    python3 scripts/tools/ablate.py --flag enable_kelly_cap --start 2018-01 --end 2023-12

    # 自定义 atr 阈值灵敏度
    python3 scripts/tools/ablate.py --flag enable_atr_stop --atr-pct-grid 0.10 0.15 0.20 0.25

输出：
    docs/ablation_<flag>_<YYYY-MM-DD>.md（markdown 对照表 + 4 件套判定）
"""
from __future__ import annotations
import argparse
import itertools
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


# 4 件套门 3 判定
SHARPE_DELTA_MIN = 0.30
MDD_IMPROVEMENT_MIN = 2.0   # 改善 ≥ 2pp
TURNOVER_INCREASE_MAX = 0.50  # 增加 ≤ 50%
ERROR_KILL_RATE_MAX = 0.15  # 错杀率 ≤ 15%（待实现）


def run_config(flags: dict, start: str, end: str, top_k: int = 5,
               universe: list[str] | None = None,
               extra_params: dict | None = None) -> dict:
    """跑一次 walk_forward 配置，返回关键指标 + 全量 result."""
    from stock_research.jobs.walk_forward_backtest import walk_forward
    kwargs = {
        "universe": universe or ["NVDA", "TSM", "GOOGL", "MSFT", "AAPL", "AMD",
                                  "AVGO", "MRVL", "META", "AMZN", "VRT", "LRCX"],
        "start_month": start,
        "end_month": end,
        "top_k": top_k,
    }
    kwargs.update(flags)
    if extra_params:
        kwargs.update(extra_params)
    try:
        r = walk_forward(**kwargs)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "config": flags}

    return {
        "config": flags,
        "extra": extra_params or {},
        "total_excess_pct": r.total_excess_return,
        "sharpe": r.sharpe,
        "max_drawdown_pct": r.max_drawdown,
        "n_months": len(r.months),
        # 触发统计（D-2 加的 MonthResult 字段）
        "total_kelly_capped": sum(getattr(m, "n_kelly_capped", 0) for m in r.months),
        "total_atr_stopped": sum(getattr(m, "n_atr_stopped", 0) for m in r.months),
        "total_bab_active_months": sum(1 for m in r.months if getattr(m, "bab_active", False)),
        "total_bab_capped": sum(getattr(m, "n_bab_capped", 0) for m in r.months),
    }


def gate3_verdict(baseline: dict, candidate: dict) -> dict:
    """4 件套门 3 判定：candidate vs baseline 是否通过。"""
    if baseline.get("error") or candidate.get("error"):
        return {"verdict": "ERROR", "details": [
            f"baseline: {baseline.get('error', 'OK')}",
            f"candidate: {candidate.get('error', 'OK')}",
        ]}

    sharpe_delta = candidate["sharpe"] - baseline["sharpe"]
    mdd_improvement = abs(baseline["max_drawdown_pct"]) - abs(candidate["max_drawdown_pct"])

    details = {
        "sharpe_delta": round(sharpe_delta, 3),
        "sharpe_delta_pass": sharpe_delta >= SHARPE_DELTA_MIN,
        "mdd_improvement_pp": round(mdd_improvement, 2),
        "mdd_improvement_pass": mdd_improvement >= MDD_IMPROVEMENT_MIN,
        # turnover / 错杀率 暂未量化，留待后续实现
        "turnover_status": "N/A (待实现)",
        "error_kill_rate_status": "N/A (待实现)",
    }
    pass_count = sum([details["sharpe_delta_pass"], details["mdd_improvement_pass"]])
    if pass_count == 2:
        verdict = "✅ PASS（核心 2/2 通过 — 推荐进 P0）"
    elif pass_count == 1:
        verdict = "⚠️ PARTIAL（1/2，建议进 P1 候选）"
    else:
        verdict = "❌ REJECT（核心 0/2 — 拒绝）"
    details["verdict"] = verdict
    return details


def write_markdown_report(flag: str, baseline: dict, results: list[dict],
                          verdicts: list[dict], out_path: Path,
                          start: str, end: str, universe: list[str]):
    lines = []
    lines.append(f"# 消融测试：{flag}")
    lines.append("")
    lines.append(f"**生成时间**：{datetime.now():%Y-%m-%d %H:%M}")
    lines.append(f"**窗口**：{start} ~ {end}")
    lines.append(f"**Universe** ({len(universe)})：{', '.join(universe[:8])}{'...' if len(universe)>8 else ''}")
    lines.append("")

    lines.append("## 实测对比")
    lines.append("")
    lines.append("| 配置 | 总超额 % | Sharpe | MDD | n_months | 触发统计 |")
    lines.append("|---|---|---|---|---|---|")
    bl_label = "baseline"
    if "error" in baseline:
        lines.append(f"| {bl_label} | — | — | — | — | ❌ {baseline['error']} |")
    else:
        bl_trig = f"k={baseline.get('total_kelly_capped',0)} atr={baseline.get('total_atr_stopped',0)} bab={baseline.get('total_bab_active_months',0)}"
        lines.append(f"| {bl_label} | {baseline['total_excess_pct']:+.1f}% | {baseline['sharpe']:.2f} | {baseline['max_drawdown_pct']:.1f}% | {baseline['n_months']} | {bl_trig} |")

    for r in results:
        cfg_str = ",".join(f"{k}={v}" for k, v in r["config"].items())
        extra = ",".join(f"{k}={v}" for k, v in r.get("extra", {}).items())
        if extra:
            cfg_str += " · " + extra
        if "error" in r:
            lines.append(f"| {cfg_str} | — | — | — | — | ❌ {r['error']} |")
        else:
            trig = f"k={r.get('total_kelly_capped',0)} atr={r.get('total_atr_stopped',0)} bab={r.get('total_bab_active_months',0)}"
            lines.append(f"| {cfg_str} | {r['total_excess_pct']:+.1f}% | {r['sharpe']:.2f} | {r['max_drawdown_pct']:.1f}% | {r['n_months']} | {trig} |")

    lines.append("")
    lines.append("## 4 件套门 3 判定（vs baseline）")
    lines.append("")
    lines.append("| 配置 | Sharpe Δ | MDD 改善 (pp) | turnover | 错杀率 | 判定 |")
    lines.append("|---|---|---|---|---|---|")
    for r, v in zip(results, verdicts):
        cfg_str = ",".join(f"{k}={v_}" for k, v_ in r["config"].items())
        if v.get("verdict") == "ERROR":
            lines.append(f"| {cfg_str} | — | — | — | — | ❌ ERROR |")
            continue
        sharpe_d = v["sharpe_delta"]
        mdd_imp = v["mdd_improvement_pp"]
        sharpe_str = f"{sharpe_d:+.2f} " + ("✅" if v["sharpe_delta_pass"] else "❌")
        mdd_str = f"{mdd_imp:+.2f} " + ("✅" if v["mdd_improvement_pass"] else "❌")
        lines.append(f"| {cfg_str} | {sharpe_str} | {mdd_str} | N/A | N/A | {v['verdict']} |")

    lines.append("")
    lines.append("## 门 3 阈值参考")
    lines.append(f"- Sharpe Δ ≥ {SHARPE_DELTA_MIN}")
    lines.append(f"- MDD 改善 ≥ {MDD_IMPROVEMENT_MIN}pp")
    lines.append(f"- Turnover 增加 ≤ {TURNOVER_INCREASE_MAX*100:.0f}% （待量化）")
    lines.append(f"- 错杀率 ≤ {ERROR_KILL_RATE_MAX*100:.0f}% （待量化）")
    lines.append("")
    lines.append("通过标准：核心 2 件套（Sharpe + MDD）全过 → P0；缺 1 件 → P1 候选；两件都失 → 拒绝。")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description="通用 walk_forward 消融测试")
    p.add_argument("--flag", nargs="+", required=True,
                   help="要消融的 walk_forward boolean 参数名，如 enable_kelly_cap")
    p.add_argument("--start", default="2015-01")
    p.add_argument("--end", default="2020-12")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--universe", nargs="*", default=None)
    p.add_argument("--grid", action="store_true",
                   help="多 flag 跑笛卡尔积（默认每个 flag 单独跑）")
    p.add_argument("--atr-pct-grid", type=float, nargs="*", default=None,
                   help="对 enable_atr_stop 跑多个 atr_stop_pct 阈值灵敏度")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    print(f"=== 消融测试 ===")
    print(f"  flags: {args.flag}")
    print(f"  window: {args.start} ~ {args.end} (top-k {args.top_k})\n")

    # 1. baseline (全部 False)
    print(f"[baseline]")
    baseline = run_config({}, args.start, args.end, args.top_k, args.universe)
    print(f"  → Sharpe {baseline.get('sharpe','?')} / MDD {baseline.get('max_drawdown_pct','?')}%\n")

    # 2. 候选配置组合
    configs: list[dict] = []
    extras: list[dict] = []
    if args.atr_pct_grid and "enable_atr_stop" in args.flag:
        for pct in args.atr_pct_grid:
            configs.append({"enable_atr_stop": True})
            extras.append({"atr_stop_pct": pct})
    elif args.grid and len(args.flag) > 1:
        # 笛卡尔积所有 True 组合
        for r in range(1, len(args.flag) + 1):
            for combo in itertools.combinations(args.flag, r):
                configs.append({f: True for f in combo})
                extras.append({})
    else:
        # 默认：每个 flag 单独跑 True
        for f in args.flag:
            configs.append({f: True})
            extras.append({})

    # 3. 跑候选
    results: list[dict] = []
    verdicts: list[dict] = []
    for cfg, ex in zip(configs, extras):
        label = ",".join(f"{k}={v}" for k, v in cfg.items())
        if ex:
            label += " · " + ",".join(f"{k}={v}" for k, v in ex.items())
        print(f"[{label}]")
        r = run_config(cfg, args.start, args.end, args.top_k, args.universe, ex)
        if "error" not in r:
            print(f"  → Sharpe {r['sharpe']:.2f} / MDD {r['max_drawdown_pct']:.1f}%\n")
        else:
            print(f"  → ❌ {r['error']}\n")
        results.append(r)
        verdicts.append(gate3_verdict(baseline, r))

    # 4. 写 markdown 报告
    flag_label = "_".join(f.replace("enable_", "") for f in args.flag)
    out_path = (Path(args.out) if args.out else
                REPO / "docs" / f"ablation_{flag_label}_{datetime.now():%Y-%m-%d}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    universe = args.universe or ["NVDA", "TSM", "GOOGL", "MSFT", "AAPL", "AMD",
                                  "AVGO", "MRVL", "META", "AMZN", "VRT", "LRCX"]
    write_markdown_report(",".join(args.flag), baseline, results, verdicts,
                          out_path, args.start, args.end, universe)
    print(f"✅ 报告：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
