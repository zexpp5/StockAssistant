"""Stress Test — 把 v6 模型扔到历史上最惨的崩盘里去跑。

学术依据：
  - Basel III / Dodd-Frank：金融监管要求银行/基金做 stress test
  - Allen-Bali (2007), Acharya-Pedersen (2005)：tail risk 是收益的关键决定因素
  - Markowitz Sharpe 是"平均时期"指标，无法刻画尾部风险

测试场景（4 个历史崩盘）：
  1. 2008 雷曼金融危机     2008-09-12 (雷曼倒闭) → 2009-03-09 (最低点)，-46% 半年慢崩
  2. 2020 新冠崩盘         2020-02-19 → 2020-03-23，-35% 一个月最快崩盘
  3. 2022 加息熊市         2022-01-03 → 2022-10-12，-25% 缓跌
  4. 2018 贸易战          2018-09-20 → 2018-12-24，-19% 中型熊

模拟假设：
  在崩盘前一天（regime["start"]）用 v6 因子选股（Top 1/3）
  → 等权满仓持有，不调仓
  → 持有到 regime["trough"]（最低点）
  → 算组合 max drawdown vs SPY max drawdown

判断标准（Acharya-Pedersen 2005）：
  组合 max DD < SPY max DD - 5%  → 🟢 强防御
  组合 max DD ≈ SPY max DD       → 🟡 中性
  组合 max DD > SPY max DD + 5%  → 🔴 放大版 SPY，破产风险

CLI:
  python3 -m stock_research.jobs.stress_test
"""
from __future__ import annotations
import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from .. import config
from ..adapters import store

logger = logging.getLogger("stock_research.jobs.stress_test")


# ─────────── 4 个崩盘 regime（学术经典）───────────

CRASH_REGIMES = [
    {
        "name": "2008 雷曼金融危机",
        "start": "2008-09-12",   # 雷曼倒闭前一天（次日 9-15 申请破产）
        "trough": "2009-03-09",  # SPY 最低点
        "expected_spy_dd": "约 -46%",
        "type": "🐻 慢崩（半年）",
    },
    {
        "name": "2020 新冠崩盘",
        "start": "2020-02-19",   # SPY 历史高点（崩盘前一天）
        "trough": "2020-03-23",  # SPY 最低点
        "expected_spy_dd": "约 -34%",
        "type": "⚡ 闪崩（35 天）",
    },
    {
        "name": "2022 加息熊市",
        "start": "2022-01-03",   # SPY 高点
        "trough": "2022-10-12",  # SPY 最低点
        "expected_spy_dd": "约 -25%",
        "type": "📉 缓跌（10 个月）",
    },
    {
        "name": "2018 贸易战 + 加息双杀",
        "start": "2018-09-20",   # SPY 高点
        "trough": "2018-12-24",  # SPY 最低点
        "expected_spy_dd": "约 -20%",
        "type": "🐾 中型熊（3 个月）",
    },
]


# ─────────── 数据获取 ───────────

def _fetch_price_window(ticker: str, start: str, end: str):
    """yfinance 拉某区间历史价（buffer 60 天用于因子计算）。"""
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        # start 前 400 天买余量给因子用
        buffer_start = (pd.to_datetime(start) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        buffer_end = (pd.to_datetime(end) + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
        h = yf.Ticker(ticker).history(start=buffer_start, end=buffer_end)
        if len(h) < 50:
            return None
        return h
    except Exception:
        return None


def _calc_factors_at(hist: pd.DataFrame, target_date: str):
    """从历史价计算 12-1 月动量 + 1 月反转。"""
    target = pd.to_datetime(target_date)
    if hist.index.tz:
        h = hist[hist.index.tz_localize(None) <= target]
    else:
        h = hist[hist.index <= target]
    if len(h) < 252:
        return None, None
    close = h["Close"]
    t_now = float(close.iloc[-1])
    t_minus_21 = float(close.iloc[-22])
    t_minus_252 = float(close.iloc[-253])
    mom = (t_minus_21 / t_minus_252 - 1) * 100
    rev = -((t_now / t_minus_21 - 1) * 100)
    return mom, rev


def _slice_window(hist: pd.DataFrame, start: str, end: str) -> pd.Series | None:
    """截取 [start, end] 区间的 Close 价；标准化为起点 1.0。"""
    s = pd.to_datetime(start)
    e = pd.to_datetime(end)
    if hist.index.tz:
        idx = hist.index.tz_localize(None)
    else:
        idx = hist.index
    mask = (idx >= s) & (idx <= e)
    h = hist[mask]
    if len(h) < 5:
        return None
    return h["Close"] / h["Close"].iloc[0]


# ─────────── Regime filter（v1: 200MA / v2: 200MA + VIX）───────────

def _get_position_multiplier(start: str, mode: str = "faber_200ma") -> tuple[float, str]:
    """获取仓位倍数 + 触发原因。

    mode = "faber_200ma": Faber 2007 单信号
    mode = "combined":    Whaley + Faber 双信号（OR 触发）
    """
    try:
        from ..core.regime_filter import get_position_multiplier as gpm
        info = gpm(as_of=start, mode=mode)
        return float(info["position_multiplier"]), info.get("trigger", "")
    except Exception as e:
        return 1.0, f"error: {e}"


# ─────────── 单 regime 测试 ───────────

def crash_test(regime: dict, samples: list[str],
               defense_mode: str = "none") -> dict | None:
    """对一个崩盘 regime 跑 stress test。

    defense_mode:
      "none"               : v6 原版，无防御
      "faber_200ma"        : Faber 2007 单信号（SPY < 200MA 减仓 50%）
      "combined"           : Faber + Whaley（200MA OR VIX>30 减仓 50%）
      "combined_with_stop" : combined + 单股 -15% 止损（O'Neil 2002）
    """
    """对一个崩盘 regime 跑 stress test。"""
    name = regime["name"]
    start = regime["start"]
    trough = regime["trough"]

    print(f"\n{'='*90}")
    print(f"  📉 {name}  {start} → {trough}  [{regime.get('type', '')}]")
    print(f"     SPY 实际跌幅: {regime.get('expected_spy_dd', '?')}")
    print(f"{'='*90}")

    # 1. 拉每只样本的历史价（含 400 天 buffer 给因子）
    print(f"\n  [1/4] 拉 {len(samples)} 只样本历史价...")
    histories = {}
    for tk in samples:
        h = _fetch_price_window(tk, start, trough)
        if h is None:
            continue
        histories[tk] = h
    print(f"        {len(histories)} 只数据可用（其余在该 regime 未上市或缺数据）")

    if len(histories) < 3:
        print(f"  ⚠️ 可用样本太少，跳过")
        return None

    # 2. 在 start 点用因子选股（Top 1/3）
    factor_data = []
    for tk, h in histories.items():
        mom, rev = _calc_factors_at(h, start)
        if mom is None:
            continue
        factor_data.append({"ticker": tk, "mom": mom, "rev": rev})

    if len(factor_data) < 3:
        print(f"  ⚠️ 因子可算样本太少（{len(factor_data)}），跳过")
        return None

    df = pd.DataFrame(factor_data)
    for col in ["mom", "rev"]:
        std = df[col].std(ddof=0)
        df[f"z_{col}"] = (df[col] - df[col].mean()) / std if std > 0 else 0
    df["composite"] = (df[["z_mom", "z_rev"]]).mean(axis=1)
    df = df.sort_values("composite", ascending=False).reset_index(drop=True)

    # Top 1/3
    n_pick = max(1, len(df) // 3)
    picks = df.head(n_pick)["ticker"].tolist()
    print(f"  [2/4] 因子选股（Top 1/3）：{picks}")

    # 3. 拉 picks 在 [start, trough] 的每日价格 → 标准化路径
    paths = {}
    for tk in picks:
        p = _slice_window(histories[tk], start, trough)
        if p is not None:
            paths[tk] = p

    if len(paths) < 2:
        print(f"  ⚠️ 区间内可用 picks 太少，跳过")
        return None

    # 个股层 -15% 止损（如果模式包含 stop）
    apply_stop = defense_mode == "combined_with_stop"
    if apply_stop:
        from ..core.portfolio_constraints import apply_stop_loss
        n_stopped = 0
        for tk, p in list(paths.items()):
            capped, triggered = apply_stop_loss(p, stop_pct=0.15)
            paths[tk] = capped
            if triggered is not None:
                n_stopped += 1
        print(f"  [2.5/4] 个股 -15% 止损触发: {n_stopped}/{len(paths)} 只")

    # 等权组合每日价值（picks 部分，可能已经过止损处理）
    df_paths = pd.DataFrame(paths)
    df_paths = df_paths.dropna(how="all")
    picks_value = df_paths.mean(axis=1)

    # 市场层 regime filter（如果模式包含 200MA 或 combined）
    position_mult = 1.0
    trigger = ""
    if defense_mode == "faber_200ma":
        position_mult, trigger = _get_position_multiplier(start, mode="faber_200ma")
    elif defense_mode in ("combined", "combined_with_stop"):
        position_mult, trigger = _get_position_multiplier(start, mode="combined")

    if position_mult < 1.0:
        cash_part = 1.0 - position_mult
        portfolio_value = position_mult * picks_value + cash_part * 1.0
        print(f"  [2.6/4] Regime filter ({defense_mode}): position_mult={position_mult:.0%} "
              f"RISK_OFF · 触发: {trigger}")
    else:
        portfolio_value = picks_value
        if defense_mode != "none":
            print(f"  [2.6/4] Regime filter ({defense_mode}): 满仓 RISK_ON · {trigger}")

    # SPY 路径
    spy_hist = _fetch_price_window("SPY", start, trough)
    spy_value = _slice_window(spy_hist, start, trough) if spy_hist is not None else None

    # 4. 算 max drawdown + 终点收益
    portfolio_dd = (portfolio_value / portfolio_value.cummax() - 1).min()
    portfolio_end = float(portfolio_value.iloc[-1] - 1)

    spy_dd = float((spy_value / spy_value.cummax() - 1).min()) if spy_value is not None else None
    spy_end = float(spy_value.iloc[-1] - 1) if spy_value is not None else None

    # 终点 picks 排名
    end_returns = {tk: float(p.iloc[-1] - 1) for tk, p in paths.items() if len(p) > 0}
    worst = sorted(end_returns.items(), key=lambda x: x[1])[:5]
    best = sorted(end_returns.items(), key=lambda x: -x[1])[:5]

    print(f"\n  [3/4] 期间表现：")
    print(f"        组合最大回撤: {portfolio_dd*100:+.2f}%")
    print(f"        SPY 最大回撤: {spy_dd*100:+.2f}%" if spy_dd is not None else "        SPY 数据缺失")
    print(f"        组合区间收益: {portfolio_end*100:+.2f}%")
    print(f"        SPY 区间收益: {spy_end*100:+.2f}%" if spy_end is not None else "")

    # 5. 防御能力判定
    # 注意：DD 都是负数。-50% 比 -25% 跌得"更深"。
    # diff = portfolio_dd - spy_dd
    #   diff > 0  → portfolio_dd 比 spy_dd 更接近 0（跌得少）→ 强防御
    #   diff < 0  → portfolio_dd 比 spy_dd 更负（跌得惨）→ 放大版 SPY
    if spy_dd is None:
        verdict, icon = "未知（SPY 数据缺失）", "?"
    else:
        diff = (portfolio_dd - spy_dd) * 100  # 正值 = 比 SPY 抗跌
        if diff > 5:
            verdict, icon = "🟢 强防御（组合回撤显著小于 SPY）", "🟢"
        elif diff > -5:
            verdict, icon = "🟡 中性（组合回撤 ≈ SPY）", "🟡"
        else:
            verdict, icon = "🔴 放大版 SPY（组合回撤显著大于 SPY，破产风险高）", "🔴"

    print(f"  [4/4] 防御判定: {verdict}")
    if best:
        print(f"        Top 3 抗跌: " + ", ".join(f"{tk} ({r*100:+.1f}%)" for tk, r in best[:3]))
    if worst:
        print(f"        Bottom 3 :  " + ", ".join(f"{tk} ({r*100:+.1f}%)" for tk, r in worst[:3]))

    return {
        "regime": name,
        "start": start,
        "trough": trough,
        "n_picks_used": len(paths),
        "n_universe": len(factor_data),
        "picks": picks,
        "defense_mode": defense_mode,
        "position_multiplier": position_mult,
        "regime_trigger": trigger,
        "portfolio_max_drawdown_pct": round(portfolio_dd * 100, 2),
        "spy_max_drawdown_pct": round(spy_dd * 100, 2) if spy_dd is not None else None,
        "portfolio_total_return_pct": round(portfolio_end * 100, 2),
        "spy_total_return_pct": round(spy_end * 100, 2) if spy_end is not None else None,
        "alpha_dd_pct": round((portfolio_dd - spy_dd) * 100, 2) if spy_dd is not None else None,
        "alpha_total_pct": round((portfolio_end - spy_end) * 100, 2) if spy_end is not None else None,
        "verdict": verdict,
        "icon": icon,
        "worst_stocks": [{"ticker": tk, "return_pct": round(r * 100, 2)} for tk, r in worst],
        "best_stocks": [{"ticker": tk, "return_pct": round(r * 100, 2)} for tk, r in best],
    }


# ─────────── 主流程 ───────────

def run(compare_filter: bool = True) -> dict:
    print("=" * 90)
    print("  💀 Stress Test — v6 模型在历史崩盘期表现")
    print("=" * 90)

    from walk_forward_validate import SAMPLES
    print(f"\n  样本: {len(SAMPLES)} 只 (来自 walk_forward_validate.SAMPLES)")
    print(f"  Crash regimes: {len(CRASH_REGIMES)} 个")
    print(f"  对照模式: {'A/B (无过滤 vs 200MA 过滤)' if compare_filter else '仅无过滤'}")

    # A. Baseline（v6 原版无防御）
    print(f"\n\n{'#'*90}\n#  A. Baseline (v6 原版，无防御)\n{'#'*90}")
    results = []
    for regime in CRASH_REGIMES:
        r = crash_test(regime, SAMPLES, defense_mode="none")
        if r is not None:
            results.append(r)

    # B. + Faber 200MA 单信号
    results_filtered = []
    results_combined = []
    if compare_filter:
        print(f"\n\n{'#'*90}\n#  B. v6 + Faber 200MA 单信号\n{'#'*90}")
        for regime in CRASH_REGIMES:
            r = crash_test(regime, SAMPLES, defense_mode="faber_200ma")
            if r is not None:
                results_filtered.append(r)

        # C. + 200MA + VIX + 单股止损（终极版）
        print(f"\n\n{'#'*90}\n#  C. v6 + 200MA + VIX>30 + 单股 -15% 止损（终极版）\n{'#'*90}")
        for regime in CRASH_REGIMES:
            r = crash_test(regime, SAMPLES, defense_mode="combined_with_stop")
            if r is not None:
                results_combined.append(r)

    # 汇总报告
    print(f"\n\n{'='*90}")
    print(f"  📊 跨崩盘期汇总（{len(results)} 个 regime）")
    print(f"{'='*90}")
    print(f"\n  {'Regime':<28}{'组合 DD':>10}{'SPY DD':>10}{'DD α':>10}{'组合收益':>11}{'SPY收益':>10}{'判定'}")
    print(f"  {'-'*88}")
    for r in results:
        dd = f"{r['portfolio_max_drawdown_pct']:.1f}%"
        spy_dd = f"{r['spy_max_drawdown_pct']:.1f}%" if r['spy_max_drawdown_pct'] is not None else "N/A"
        alpha_dd = f"{r['alpha_dd_pct']:+.1f}%" if r['alpha_dd_pct'] is not None else "N/A"
        ret = f"{r['portfolio_total_return_pct']:+.1f}%"
        spy_ret = f"{r['spy_total_return_pct']:+.1f}%" if r['spy_total_return_pct'] is not None else "N/A"
        print(f"  {r['regime']:<28}{dd:>10}{spy_dd:>10}{alpha_dd:>10}{ret:>11}{spy_ret:>10}  {r['icon']}")

    # 平均防御能力（DD α > 0 = 比 SPY 抗跌；< 0 = 跌得更惨）
    valid_alpha_dd = [r["alpha_dd_pct"] for r in results if r["alpha_dd_pct"] is not None]
    if valid_alpha_dd:
        avg_alpha_dd = sum(valid_alpha_dd) / len(valid_alpha_dd)
        print(f"\n  📌 [Baseline] 平均 DD α: {avg_alpha_dd:+.2f}%（> 0 = 平均比 SPY 抗跌）")
        defended = sum(1 for x in valid_alpha_dd if x > 0)
        print(f"     抗跌 regime: {defended}/{len(valid_alpha_dd)}")

    # 3 路对比表
    if compare_filter and (results_filtered or results_combined):
        print(f"\n{'='*100}")
        print(f"  🆚 3 路对比：A 原版 / B 200MA / C 200MA+VIX+止损")
        print(f"{'='*100}")
        print(f"\n  {'Regime':<24}{'A DD':>10}{'B DD':>10}{'C DD':>10}{'C-A 改善':>11}{'C 触发':>22}")
        print(f"  {'-'*87}")
        idx_b = {r["regime"]: r for r in results_filtered}
        idx_c = {r["regime"]: r for r in results_combined}
        improvements_b = []
        improvements_c = []
        for r in results:
            b = idx_b.get(r["regime"])
            c = idx_c.get(r["regime"])
            a_dd = r["portfolio_max_drawdown_pct"]
            b_dd = b["portfolio_max_drawdown_pct"] if b else None
            c_dd = c["portfolio_max_drawdown_pct"] if c else None
            imp_b = (b_dd - a_dd) if b_dd is not None else 0
            imp_c = (c_dd - a_dd) if c_dd is not None else 0
            improvements_b.append(imp_b)
            improvements_c.append(imp_c)
            c_trigger = c.get("regime_trigger", "")[:18] if c else ""
            arrow = "🟢" if imp_c > 5 else ("🟡" if imp_c > 0 else "🔴")
            b_str = f"{b_dd:+.1f}%" if b_dd is not None else "N/A"
            c_str = f"{c_dd:+.1f}%" if c_dd is not None else "N/A"
            print(f"  {r['regime']:<24}{a_dd:>+9.1f}%{b_str:>10}{c_str:>10}{imp_c:>+9.1f}% {arrow} {c_trigger:<20}")

        if improvements_b:
            avg_b = sum(improvements_b) / len(improvements_b)
            avg_c = sum(improvements_c) / len(improvements_c)
            print(f"\n  📌 平均改善：B (200MA) {avg_b:+.2f}% · C (终极版) {avg_c:+.2f}%")
            n_b = sum(1 for x in improvements_b if x > 0)
            n_c = sum(1 for x in improvements_c if x > 0)
            print(f"     改善 regime：B {n_b}/{len(improvements_b)} · C {n_c}/{len(improvements_c)}")

    # 写文件
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model_version": "v6 因子选股（动量+反转）",
        "n_regimes": len(results),
        "results": results,
        "results_filtered": results_filtered,
        "results_combined": results_combined,
        "avg_alpha_dd_pct": round(sum(valid_alpha_dd) / len(valid_alpha_dd), 2) if valid_alpha_dd else None,
        "n_defended": sum(1 for x in valid_alpha_dd if x > 0) if valid_alpha_dd else 0,
    }
    # 改善量统计
    if compare_filter and results_filtered:
        idx_b = {r["regime"]: r for r in results_filtered}
        idx_c = {r["regime"]: r for r in results_combined}
        imp_b = [idx_b[r["regime"]]["portfolio_max_drawdown_pct"] - r["portfolio_max_drawdown_pct"]
                 for r in results if r["regime"] in idx_b]
        imp_c = [idx_c[r["regime"]]["portfolio_max_drawdown_pct"] - r["portfolio_max_drawdown_pct"]
                 for r in results if r["regime"] in idx_c]
        if imp_b:
            summary["filter_b_avg_improvement_pct"] = round(sum(imp_b) / len(imp_b), 2)
            summary["filter_b_n_improved"] = sum(1 for x in imp_b if x > 0)
        if imp_c:
            summary["filter_c_avg_improvement_pct"] = round(sum(imp_c) / len(imp_c), 2)
            summary["filter_c_n_improved"] = sum(1 for x in imp_c if x > 0)
    snap = store.save_json(summary, config.AUDIT_DIR, "stress_test")
    print(f"\n  📁 JSON 快照: {snap}")

    # 写 markdown 报告
    md = _to_markdown(summary)
    md_path = _REPO_ROOT / "docs" / "STRESS_TEST_REPORT.md"
    md_path.write_text(md, encoding="utf-8")
    print(f"  📁 Markdown 报告: {md_path}")
    print(f"\n{'='*90}\n")

    return summary


def _to_markdown(summary: dict) -> str:
    """渲染 stress test 报告。"""
    lines = [
        "# Stress Test Report — v6 模型崩盘期表现",
        "",
        f"_生成时间：{summary['generated_at']}_",
        f"_模型版本：{summary['model_version']}_",
        "",
        "## 摘要",
        "",
        f"- 测试 **{summary['n_regimes']}** 个历史崩盘 regime",
        f"- 平均 **drawdown alpha** = {summary.get('avg_alpha_dd_pct', 'N/A')}%（> 0 = 比 SPY 抗跌；< 0 = 跌得更惨）",
        f"- 抗跌 regime: **{summary.get('n_defended', 0)}/{summary['n_regimes']}**",
        "",
        "## 各 Regime 详情",
        "",
    ]
    for r in summary["results"]:
        lines += [
            f"### {r['regime']} — {r['icon']}",
            "",
            f"- 期间：`{r['start']}` → `{r['trough']}`",
            f"- 持仓数: {r['n_picks_used']} / 全宇宙 {r['n_universe']}",
            f"- **组合最大回撤**: `{r['portfolio_max_drawdown_pct']:+.2f}%`",
            f"- **SPY 最大回撤**: `{r['spy_max_drawdown_pct']:+.2f}%`",
            f"- **DD alpha**: `{r['alpha_dd_pct']:+.2f}%`（正值 = 比 SPY 抗跌；负值 = 跌得更惨）",
            f"- 组合区间收益: {r['portfolio_total_return_pct']:+.2f}% / SPY: {r['spy_total_return_pct']:+.2f}%",
            f"- 判定：**{r['verdict']}**",
            "",
            f"**抗跌 Top 3**：" + " · ".join(f"`{x['ticker']}` ({x['return_pct']:+.1f}%)" for x in r["best_stocks"][:3]),
            "",
            f"**最差 Bottom 3**：" + " · ".join(f"`{x['ticker']}` ({x['return_pct']:+.1f}%)" for x in r["worst_stocks"][:3]),
            "",
            f"**入选股票全集**：`{', '.join(r['picks'])}`",
            "",
            "---",
            "",
        ]

    # 3 路对比（A 原版 / B 200MA / C 200MA+VIX+止损）
    if summary.get("results_filtered") or summary.get("results_combined"):
        lines += [
            "## 🆚 3 路防御机制对比",
            "",
            "| 模式 | 学术依据 | 触发规则 |",
            "|---|---|---|",
            "| **A** v6 原版 | - | 无防御，纯因子满仓 |",
            "| **B** + 200MA 单信号 | Faber (2007) SSRN | SPY < 200MA → 减仓 50% |",
            "| **C** + 200MA + VIX + 止损 | Faber + Whaley (2009) + O'Neil (2002) | 200MA 或 VIX>30 触发，再加单股 -15% 止损 |",
            "",
            "| Regime | A 原版 DD | B 200MA DD | C 终极版 DD | C-A 改善 | C 触发原因 |",
            "|---|---|---|---|---|---|",
        ]
        idx_b = {r["regime"]: r for r in summary.get("results_filtered", [])}
        idx_c = {r["regime"]: r for r in summary.get("results_combined", [])}
        for r in summary["results"]:
            b = idx_b.get(r["regime"])
            c = idx_c.get(r["regime"])
            a_dd = r["portfolio_max_drawdown_pct"]
            b_str = f"{b['portfolio_max_drawdown_pct']:+.1f}%" if b else "N/A"
            c_str = f"{c['portfolio_max_drawdown_pct']:+.1f}%" if c else "N/A"
            imp_c = (c["portfolio_max_drawdown_pct"] - a_dd) if c else 0
            arrow = "🟢" if imp_c > 5 else ("🟡" if imp_c > 0 else "🔴")
            c_trigger = (c.get("regime_trigger") or "—") if c else "—"
            lines.append(f"| {r['regime']} | {a_dd:+.1f}% | {b_str} | {c_str} | "
                         f"{imp_c:+.1f}% {arrow} | {c_trigger} |")

        avg_b = summary.get("filter_b_avg_improvement_pct")
        n_b = summary.get("filter_b_n_improved", 0)
        avg_c = summary.get("filter_c_avg_improvement_pct")
        n_c = summary.get("filter_c_n_improved", 0)
        n_total = len(summary["results"])
        lines += [
            "",
            f"**平均改善**: B 200MA `{avg_b}%`（{n_b}/{n_total} regime 改善）· "
            f"C 终极版 `{avg_c}%`（{n_c}/{n_total} regime 改善）",
            "",
            "### 关键洞察",
            "",
            "- **B 200MA**：仅在慢崩里有效（2008-09-12 已跌破 200MA），对闪崩/缓跌起点无效（200MA 滞后）",
            "- **C 终极版**：加 VIX>30 + 单股 -15% 止损，理论上能覆盖闪崩",
            "- 没有一个滞后指标能完美防御所有崩盘 → 多信号 + 个股止损是行业标准",
            "",
        ]

    lines += [
        "## 限制声明",
        "",
        "- 本测试**假设站在 regime 起点用 v6 因子选股，等权满仓持有，不调仓**",
        "- 真实交易里你会做仓位管理、止损、再平衡，所以本测试是「最坏情况」模拟",
        "- 样本：`walk_forward_validate.SAMPLES` 16 只大盘 AI 股（小盘股不在测试范围）",
        "- 因子：仅用 12-1 月动量 + 1 月反转（其他因子在远期历史数据可获取性差）",
        "- 不构成投资建议；模型已知缺陷见 [MODEL_CARD.md](MODEL_CARD.md)",
        "",
        f"_StockAssistant v6.2 · {summary['generated_at']}_",
    ]
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    p = argparse.ArgumentParser(description="Stress Test — 历史崩盘期回测")
    p.add_argument("--no-compare", action="store_true",
                   help="不做 A/B 对比，仅跑 v6 原版")
    args = p.parse_args()
    run(compare_filter=not args.no_compare)
    return 0


if __name__ == "__main__":
    sys.exit(main())
