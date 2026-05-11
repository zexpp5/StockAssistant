"""完整组合优化流水线（v6）— 中性化 + Markowitz + ADV 约束 + 成本扣减。

与 build_plan_a_v5.py 互补：v5 是"裸 Markowitz"（无中性化、无成本约束）。
v6 在 v5 基础上加了三个关键质量门：

  1. 行业+市值中性化（core.neutralization）
     → 因子分数剔除行业 beta 和小盘溢价后再排名
     → Top N 选股不会被某个行业（如半导体动量）一边倒污染

  2. ADV 限流（core.portfolio_constraints.cap_by_adv）
     → 单日交易 ≤ 5% × ADV，避免冲击成本
     → 小盘 / A 股低流动票自动减仓

  3. 交易成本扣减（core.portfolio_constraints.apply_transaction_cost）
     → 净 alpha = 总 alpha - 佣金 - 冲击
     → 高换手策略会被成本吃掉的部分明确暴露

2026-05-10 P1 升级（默认开启）：
  4. **风险感知优化**：核心 Markowitz 步从蒙特卡洛改用 portfolio_optimizer_pro
     的 risk_aware_optimize（Ledoit-Wolf 协方差 + 相关性 < 0.7 剪枝 + 多级降风险）
     - 加 --legacy-mc 开关回退到 20000 次蒙特卡洛（兼容老 review）

CLI:
  python3 -m stock_research.jobs.optimize_portfolio
  python3 -m stock_research.jobs.optimize_portfolio --capital 1000000
  python3 -m stock_research.jobs.optimize_portfolio --no-neutralize  # 关闭中性化对比
  python3 -m stock_research.jobs.optimize_portfolio --legacy-mc      # 用旧蒙特卡洛
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

# 让本 job 能 import 根目录的 factor_model + early_signals + build_plan_a_v5 helpers
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))  # 2026-05-11 lib 迁移
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "pipeline"))  # build_plan_a_v5 等 sibling

from .. import config
from ..core import neutralization as nz
from ..core import portfolio_constraints as pc
from ..core import portfolio_optimizer_pro as opt_pro
from ..adapters import store, feishu  # noqa: F401

import pandas as pd

logger = logging.getLogger("stock_research.jobs.optimize_portfolio")


# ─────────── 数据获取 ───────────

def _load_factor_scores() -> dict | None:
    """读 daily_picks_v5 / build_plan_a_v5 共享的 factor_scores_today.json 缓存。"""
    cache = _REPO_ROOT / "data" / "latest" / "factor_scores_today.json"
    if not cache.exists():
        logger.error("缓存 %s 不存在，先跑 daily_picks_v5.py", cache)
        return None
    with open(cache, encoding="utf-8") as f:
        return json.load(f)


def _fetch_returns_and_adv(ticker: str, lookback_days: int = 252):
    """yfinance 拉历史收益 + 当前 ADV（美元）。"""
    try:
        import yfinance as yf
    except ImportError:
        return None, None
    try:
        end = datetime.now()
        start = end - timedelta(days=lookback_days + 60)
        t = yf.Ticker(ticker)
        hist = t.history(start=start, end=end)
        if len(hist) < 60:
            return None, None
        rets = hist["Close"].pct_change().dropna().values
        if len(rets) < 60:
            return None, None
        # ADV = 过去 30 天平均成交量 × 当前价
        last_30 = hist.tail(30)
        avg_vol = float(last_30["Volume"].mean())
        last_close = float(hist["Close"].iloc[-1])
        adv = avg_vol * last_close
        return rets[-lookback_days:], adv
    except Exception as e:
        logger.warning("yfinance %s 失败: %s", ticker, e)
        return None, None


def _industry_for(ticker: str, watchlist_lookup: dict) -> str:
    """行业归类（用作中性化 + 行业敞口约束分组）。

    策略：优先用 daily_picks.THEME_MAPPING（用户维护的 76 只主题映射，粒度合适），
    回退到 watchlist「行业归类」字段的第二段（剔除"美股·"前缀）。
    """
    # 1. 优先 THEME_MAPPING（粗粒度主题，每个主题通常 5-15 只）
    try:
        from daily_picks import THEME_MAPPING  # type: ignore
        theme = THEME_MAPPING.get(ticker.upper())
        if theme:
            return theme
    except ImportError:
        pass

    # 2. 回退：watchlist 行业字段第二段
    wf = watchlist_lookup.get(ticker.upper(), {})
    industry = wf.get("行业归类", "") or ""
    if isinstance(industry, list):
        industry = (industry[0].get("text", "") if industry else "") or ""
    if isinstance(industry, dict):
        industry = industry.get("text", "") or industry.get("name", "")
    # "美股·半导体/AI 算力霸主" → "半导体/AI 算力霸主"；取第一个 / 之前
    parts = industry.split("·") if industry else []
    second = parts[1] if len(parts) > 1 else (parts[0] if parts else "")
    return second.split("/")[0].strip() if second else "Unknown"


def _market_cap_for(ticker: str, watchlist_lookup: dict) -> float | None:
    """从 watchlist「yf市值」字段取值，单位转成美元。"""
    wf = watchlist_lookup.get(ticker.upper(), {})
    mcap_str = wf.get("yf市值", "")
    if isinstance(mcap_str, list):
        mcap_str = (mcap_str[0].get("text", "") if mcap_str else "") or ""
    if not mcap_str or not isinstance(mcap_str, str):
        return None
    s = str(mcap_str).strip().upper()
    try:
        if s.endswith("T"):
            return float(s[:-1]) * 1e12
        if s.endswith("B"):
            return float(s[:-1]) * 1e9
        if s.endswith("M"):
            return float(s[:-1]) * 1e6
        return float(s)
    except ValueError:
        return None


# ─────────── Markowitz（蒙特卡洛带约束）───────────

def markowitz_constrained(mean_rets: np.ndarray, cov: np.ndarray,
                          risk_free: float = 0.045 / 252,
                          n_iter: int = 20000, max_w: float = 0.15,
                          min_w: float = 0.02, cash_pct: float = 0.05,
                          seed: int = 42) -> tuple[np.ndarray | None, float]:
    """带 max/min 仓位约束的 Markowitz Max Sharpe。"""
    n = len(mean_rets)
    invest = 1 - cash_pct
    best_sharpe = -np.inf
    best_w = None
    np.random.seed(seed)
    for _ in range(n_iter):
        w = np.random.dirichlet(np.ones(n) * 1.5)
        w = np.clip(w, min_w / invest, max_w / invest)
        w = w / w.sum()
        if w.max() > max_w / invest + 1e-6:
            continue
        port_var = w @ cov @ w
        if port_var <= 0:
            continue
        port_ret = w @ mean_rets
        sharpe = (port_ret - risk_free) / np.sqrt(port_var)
        if sharpe > best_sharpe:
            best_sharpe = sharpe
            best_w = w
    return (best_w * invest, best_sharpe) if best_w is not None else (None, best_sharpe)


# ─────────── 主流水线 ───────────

def run(capital: float = 500_000,
        top_n: int = 12,
        max_weight: float = 0.15,
        min_weight: float = 0.02,
        cash_pct: float = 0.05,
        max_adv_pct: float = 0.05,
        cost_bps: float = 5.0,
        impact_bps_per_pct_adv: float = 2.0,
        skip_neutralize: bool = False,
        use_legacy_mc: bool = False,
        max_corr: float = 0.7) -> dict:
    """完整 v6 流水线。返回结果 dict。"""
    print("=" * 92)
    print(f"  📊 方案 A v6（中性化 + Markowitz + ADV 约束 + 成本扣减）")
    print("=" * 92)

    # ────── 1. 读因子缓存 ──────
    cached = _load_factor_scores()
    if not cached:
        return {"error": "factor_scores_today.json 不存在；先跑 daily_picks_v5.py"}

    factors = cached["factors"]
    sig_map = {s["ticker"]: s for s in cached.get("signals", [])}

    # ────── 2. 读 watchlist 拿行业 + 市值（用于中性化）──────
    print("\n[1/5] 读 watchlist 拿行业 + 市值...")
    try:
        watchlist = feishu.fetch_watchlist()
        wl_lookup = {(r["normalized"]["code"] or "").upper(): r["fields"] for r in watchlist}
        print(f"  watchlist: {len(wl_lookup)} 条")
    except Exception as e:
        print(f"  ⚠️ 飞书拉取失败: {e}；跳过中性化")
        wl_lookup = {}
        skip_neutralize = True

    # ────── 3. 用因子 + 中性化重新排序 ──────
    print(f"\n[2/5] 因子合成{'（含中性化）' if not skip_neutralize else '（不中性化，对照模式）'}...")
    from factor_model import combine_factors
    from early_signals import score_analyst
    analyst = {tk: score_analyst(s.get("analyst"))[0] for tk, s in sig_map.items()}
    df = combine_factors(factors, analyst_signals=analyst, include_reversal=True)

    if not skip_neutralize and not df.empty and len(wl_lookup) > 0:
        df["industry"] = df["ticker"].apply(lambda t: _industry_for(t, wl_lookup))
        df["market_cap"] = df["ticker"].apply(lambda t: _market_cap_for(t, wl_lookup))
        # 对各因子做行业 + 市值中性化
        factor_cols = [c for c in ["momentum", "reversal", "pead", "z_momentum",
                                    "z_reversal", "z_pead"] if c in df.columns]
        if factor_cols:
            df = nz.neutralize_all(df, factor_cols, industry_col="industry",
                                   market_cap_col="market_cap")
            # 用中性化后的 z 列重新合成 composite
            n_z_cols = [f"n_{c}" for c in factor_cols if c.startswith("z_")
                        and f"n_{c}" in df.columns]
            if n_z_cols:
                df["composite_neutral"] = df[n_z_cols].mean(axis=1)
                df = df.sort_values("composite_neutral", ascending=False).reset_index(drop=True)
                print(f"  ✅ 中性化使用列: {n_z_cols}")
                print(f"  ↳ Top 5（中性化）: {df.head(5)['ticker'].tolist()}")
                print(f"  ↳ Top 5（原始）  : {combine_factors(factors, analyst_signals=analyst).head(5)['ticker'].tolist()}")

    top = df.head(top_n)
    tickers = top["ticker"].tolist()
    print(f"\n  Top {top_n}: {tickers}")

    # ────── 4. 拉历史收益 + ADV ──────
    print(f"\n[3/5] 拉 {len(tickers)} 只标的历史收益 + ADV...")
    returns_dict = {}
    adv_dict = {}
    for tk in tickers:
        rets, adv = _fetch_returns_and_adv(tk, lookback_days=252)
        if rets is not None and adv is not None:
            returns_dict[tk] = rets
            adv_dict[tk] = adv
            print(f"  ✅ {tk:<8} ADV=${adv/1e6:8.1f}M  样本 {len(rets)} 天")
        else:
            print(f"  ❌ {tk:<8} 数据获取失败")

    if len(returns_dict) < 3:
        return {"error": "可用样本不足"}

    # 对齐
    min_len = min(len(r) for r in returns_dict.values())
    aligned = {tk: r[-min_len:] for tk, r in returns_dict.items()}
    final_tickers = list(aligned.keys())
    matrix = np.array([aligned[t] for t in final_tickers])
    mean_rets = matrix.mean(axis=1)
    cov = np.cov(matrix)

    # ────── 5. 核心组合优化 ──────
    if use_legacy_mc:
        print(f"\n[4/5] Markowitz 蒙特卡洛 20000 次（legacy）...")
        weights, sharpe = markowitz_constrained(mean_rets, cov,
                                                n_iter=20000, max_w=max_weight,
                                                min_w=min_weight, cash_pct=cash_pct)
        if weights is None:
            return {"error": "Markowitz 优化失败"}
        annual_sharpe = sharpe * np.sqrt(252)
        annual_ret = (mean_rets @ weights) * 252
        annual_vol = np.sqrt(weights @ cov @ weights) * np.sqrt(252)
        target_w = {final_tickers[i]: float(weights[i]) for i in range(len(final_tickers))}
        risk_aware_meta: dict = {"engine": "legacy_monte_carlo"}
    else:
        # 新路径：Ledoit-Wolf cov + 相关性剪枝 + 风险闸门多级降级
        print(f"\n[4/5] risk_aware_optimize（Ledoit-Wolf + 相关性<{max_corr} + 风险闸门）...")
        # final_tickers 已按 watchlist 顺序排，但传入 ranked_tickers 给剪枝
        # 用因子 composite 排序（df 在第 [2/5] 步已 sort 过，按 df 顺序取交集）
        ranked = [t for t in df["ticker"].tolist() if t in final_tickers]
        rdf = pd.DataFrame({tk: aligned[tk] for tk in final_tickers})
        out = opt_pro.risk_aware_optimize(
            rdf, ranked_tickers=ranked,
            max_weight=max_weight, min_weight=min_weight,
            cash_pct=cash_pct, max_corr=max_corr,
        )
        if "error" in out and "weights" not in out:
            print(f"  ⚠️ risk_aware 全级失败：{out['error']}；回退到 legacy MC")
            weights, sharpe = markowitz_constrained(mean_rets, cov,
                                                    n_iter=20000, max_w=max_weight,
                                                    min_w=min_weight, cash_pct=cash_pct)
            if weights is None:
                return {"error": "Markowitz 优化失败（含 fallback）"}
            annual_sharpe = sharpe * np.sqrt(252)
            annual_ret = (mean_rets @ weights) * 252
            annual_vol = np.sqrt(weights @ cov @ weights) * np.sqrt(252)
            target_w = {final_tickers[i]: float(weights[i]) for i in range(len(final_tickers))}
            risk_aware_meta = {"engine": "legacy_monte_carlo (fallback)",
                               "stages": out.get("stages", [])}
        else:
            # risk_aware_optimize 现在返回 weights sum=1（100% 投资视角）+ cash_pct 元数据。
            # 调用方按 stage 实际建议的 cash 缩股票权重，结果 sum = 1 - effective_cash，
            # 与 legacy MC 路径（markowitz_constrained 内部已缩）保持一致。
            effective_cash = float(out.get("cash_pct", cash_pct))
            deployed = max(0.0, 1.0 - effective_cash)
            target_w = {t: float(w) * deployed for t, w in out["weights"].items()}
            # stage_metrics 是 100% 投资基线；下游显示用，乘 deployed 才是实盘组合的年化数字
            # （cash 部分按 rf=4.5% 计入收益；vol 假设 cash 零方差）
            stage_metrics = out["stages"][out["risk_aware_stage"]].get("metrics") or {}
            stock_ret = float(stage_metrics.get("annual_return", 0.0))
            stock_vol = float(stage_metrics.get("annual_vol", 0.0))
            annual_ret = stock_ret * deployed + 0.045 * effective_cash
            annual_vol = stock_vol * deployed
            annual_sharpe = (annual_ret - 0.045) / annual_vol if annual_vol > 0 else 0.0
            risk_aware_meta = {
                "engine": "risk_aware_optimize",
                "stage": out["risk_aware_stage"],
                "stage_label": out["stages"][out["risk_aware_stage"]].get("label"),
                "effective_cash_pct": effective_cash,
                "pruned_dropped": out.get("pruned_dropped", []),
                "selected_tickers": out.get("selected_tickers", []),
                "warning": out.get("warning"),
            }
            if out.get("pruned_dropped"):
                print(f"  相关性剪枝丢弃 {len(out['pruned_dropped'])} 只：")
                for d in out["pruned_dropped"][:5]:
                    print(f"    · {d['dropped']} vs {d['vs']} ρ={d['rho']}")
            print(f"  最终采用 stage={out['risk_aware_stage']} "
                  f"（{out['stages'][out['risk_aware_stage']].get('label')}）")
            for s in out["stages"]:
                m = s.get("metrics") or {}
                br = s.get("breached") or s.get("error") or "—"
                print(f"    [{s['stage']}] {s['label']}: vol={m.get('annual_vol', 0):.2%} "
                      f"DD={m.get('max_drawdown', 0):.2%} → {br}")

    print(f"  组合年化 Sharpe = {annual_sharpe:.2f}")
    print(f"  组合年化收益   = {annual_ret*100:+.1f}%")
    print(f"  组合年化波动   = {annual_vol*100:.1f}%")

    # ────── 6a. 行业敞口约束（≤ 25% / 行业）──────
    industries_map = {tk: _industry_for(tk, wl_lookup) for tk in final_tickers}
    industry_capped, industry_summary = pc.cap_by_industry(
        target_w, industries_map, max_industry_pct=0.25,
    )
    capped_industries = [ind for ind, s in industry_summary.items()
                         if s["original"] > s["capped"] + 1e-6]
    if capped_industries:
        print(f"\n[5a/6] ⚠️ 行业敞口约束触发（≤ 25%）：")
        for ind in capped_industries:
            s = industry_summary[ind]
            print(f"    · {ind}: {s['original']:.1%} → {s['capped']:.1%}（溢出 {s['overflow']:.1%}）")
    else:
        print(f"\n[5a/6] 🟢 行业敞口检查通过（最高 {max(s['original'] for s in industry_summary.values()):.1%}）")
    target_w = industry_capped  # 用约束后的权重继续下一步

    # ────── 6b. ADV 限流 + 交易成本 ──────
    print(f"\n[5b/6] 应用 ADV 限流（≤ {max_adv_pct:.0%} ADV/单日）+ 成本扣减...")
    prev_w: dict[str, float] = {}  # 假设当前空仓
    capped, warns = pc.cap_by_adv(target_w, prev_w, adv_dict, capital,
                                  max_adv_pct=max_adv_pct)
    if warns:
        print(f"  ⚠️ ADV 限流触发 {len(warns)} 项：")
        for w in warns[:5]:
            print(f"    · {w}")

    cost = pc.apply_transaction_cost(capped, prev_w, capital,
                                     adv_dollars=adv_dict,
                                     cost_bps=cost_bps,
                                     impact_bps_per_pct_adv=impact_bps_per_pct_adv)
    print(f"\n  组合层成本: ${cost['total_cost_dollars']:,.2f} "
          f"({cost['total_cost_bps_of_portfolio']:.1f} bps)")
    print(f"  单边换手率: {cost['turnover']:.1%}")
    net_alpha_pct = pc.alpha_after_cost(annual_ret * 100, cost['total_cost_bps_of_portfolio'])
    print(f"  Gross alpha = {annual_ret*100:+.1f}% → "
          f"Net alpha = {net_alpha_pct:+.1f}%（扣 {cost['total_cost_bps_of_portfolio']:.1f} bps 成本）")

    # ────── 7. 输出 ──────
    print(f"\n  {'股票':<10}{'目标w':>9}{'限流后':>9}{'金额':>12}{'ADV(M)':>10}")
    print("  " + "-" * 55)
    plan = []
    for tk in final_tickers:
        v_target = target_w.get(tk, 0)
        v_capped = capped.get(tk, 0)
        amount = v_capped * capital
        adv_m = adv_dict.get(tk, 0) / 1e6
        flag = " ⚠️" if abs(v_target - v_capped) > 1e-6 else ""
        print(f"  {tk:<10}{v_target*100:>+8.1f}%{v_capped*100:>+8.1f}%{amount:>11,.0f}{adv_m:>9.1f}{flag}")
        plan.append({"ticker": tk, "target_weight": v_target, "capped_weight": v_capped,
                     "amount": amount, "adv_dollars": adv_dict.get(tk, 0)})

    used_total = capital * sum(capped.values())
    actual_cash = capital - used_total
    print(f"  {'现金':<10}{'':>9}{actual_cash/capital*100:>+8.1f}%{actual_cash:>11,.0f}")

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "method": ("v6+: factor neutralization + risk_aware_optimize "
                   "(Ledoit-Wolf + corr<0.7 + risk gate) + ADV cap + cost"
                   if not use_legacy_mc else
                   "v6: factor neutralization + Markowitz MC + ADV cap + cost"),
        "capital": capital,
        "constraints": {
            "max_weight": max_weight, "min_weight": min_weight,
            "cash_pct": cash_pct, "max_adv_pct": max_adv_pct,
            "cost_bps": cost_bps, "impact_bps_per_pct_adv": impact_bps_per_pct_adv,
            "neutralize": not skip_neutralize,
            "max_corr": max_corr,
            "use_legacy_mc": use_legacy_mc,
        },
        "portfolio_metrics": {
            "annual_sharpe": round(annual_sharpe, 2),
            "annual_return_pct": round(annual_ret * 100, 2),
            "annual_vol_pct": round(annual_vol * 100, 2),
            "gross_alpha_pct": round(annual_ret * 100, 2),
            "total_cost_bps": round(cost['total_cost_bps_of_portfolio'], 2),
            "net_alpha_pct": round(net_alpha_pct, 2),
            "turnover": round(cost["turnover"], 4),
        },
        "risk_aware": risk_aware_meta,
        "plan": plan,
        "adv_warnings": warns,
    }
    store.save_json(result, config.AUDIT_DIR.parent / "optimize", "plan_v6")
    print(f"\n✅ 快照已保存")
    return result


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    p = argparse.ArgumentParser(description="组合优化 v6: 中性化 + Markowitz + ADV + 成本")
    p.add_argument("--capital", type=float, default=500_000, help="组合规模（美元）")
    p.add_argument("--top-n", type=int, default=12, help="选股数")
    p.add_argument("--max-weight", type=float, default=0.15)
    p.add_argument("--min-weight", type=float, default=0.02)
    p.add_argument("--max-adv-pct", type=float, default=0.05, help="单日交易上限占 ADV 的比例")
    p.add_argument("--cost-bps", type=float, default=5.0, help="双边佣金（bps）")
    p.add_argument("--no-neutralize", action="store_true", help="跳过中性化（对照模式）")
    p.add_argument("--legacy-mc", action="store_true",
                   help="用旧蒙特卡洛 Markowitz（默认走 risk_aware_optimize）")
    p.add_argument("--max-corr", type=float, default=0.7,
                   help="选股层 pairwise 相关性上限（贪心剪枝阈值）")
    args = p.parse_args()
    r = run(capital=args.capital, top_n=args.top_n,
            max_weight=args.max_weight, min_weight=args.min_weight,
            max_adv_pct=args.max_adv_pct, cost_bps=args.cost_bps,
            skip_neutralize=args.no_neutralize,
            use_legacy_mc=args.legacy_mc,
            max_corr=args.max_corr)
    return 0 if "error" not in r else 1


if __name__ == "__main__":
    sys.exit(main())
