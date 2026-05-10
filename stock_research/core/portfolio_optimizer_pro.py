"""专业组合优化（基于 PyPortfolioOpt）— 替代你 v6 的蒙特卡洛 Markowitz。

为什么需要：
  你当前 build_plan_a_v5.py 用 20000 次蒙特卡洛搜索 max Sharpe 解，
  这是粗糙近似（不是精确解）。PyPortfolioOpt 用 cvxpy 凸优化求**精确解**，
  快 100x 且更稳定。

新增能力（之前没有）：
  1. **精确 Markowitz**（max_sharpe / min_volatility / max_quadratic_utility）
  2. **Black-Litterman**：把 v6 因子分数当 view，结合市场均衡（贝叶斯更新）
  3. **Hierarchical Risk Parity (HRP)**：无需协方差，对噪音稳健
  4. **CVaR optimization**：尾部风险敏感（防 2008 / 2020 极端损失）
  5. **Discrete Allocation**：把权重转成整股数量（按真实价格 + 资金量）

2026-05-10 P1 升级：
  6. **Ledoit-Wolf shrinkage 协方差**（默认替代 sample_cov）— 解小样本协方差噪音
  7. **prune_correlated**：选股前按相关性 ≤ 0.7 贪心剪枝
  8. **risk_aware_optimize**：风险指标反馈到优化（不是事后报告），多级降风险

学术依据：
  - Markowitz (1952) Portfolio Selection — 经典
  - Ledoit & Wolf (2003) "Honey, I Shrunk the Sample Covariance Matrix"
  - Black & Litterman (1992) "Global Portfolio Optimization" — 贝叶斯组合优化
  - Lopez de Prado (2016) "Building Diversified Portfolios that Outperform Out-of-Sample"
  - Rockafellar & Uryasev (2000) "Optimization of CVaR" — CVaR 起源

CLI:
  python3 -m stock_research.jobs.optimize_portfolio_pro --method max_sharpe
  python3 -m stock_research.jobs.optimize_portfolio_pro --method hrp
  python3 -m stock_research.jobs.optimize_portfolio_pro --method black_litterman
"""
from __future__ import annotations
import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _feasible_max_weight(returns_df: pd.DataFrame, max_weight: float) -> float:
    """权重和=1 的约束下，单仓上限至少 1/N；否则 cvxpy 报 infeasible。

    给 5% 的 headroom 让 solver 有解空间。
    """
    n = max(1, len(returns_df.columns))
    return max(max_weight, 1.05 / n)


# ─────────────────── 协方差估计（Item 2: Ledoit-Wolf） ───────────────────

def _build_cov(returns_df: pd.DataFrame, method: str = "ledoit_wolf",
               frequency: int = 252) -> pd.DataFrame:
    """统一协方差入口。

    method:
      - "ledoit_wolf"（默认）— Ledoit & Wolf 2003 收缩估计，对 N>>T 鲁棒，
                              收缩目标 = constant_variance
      - "sample"            — 历史样本协方差（小样本噪音大，PSD 不保证）
      - "exp"               — 指数加权协方差（近期权重高）
    """
    from pypfopt import risk_models
    if method == "sample":
        return risk_models.sample_cov(returns_df, returns_data=True, frequency=frequency)
    if method == "exp":
        return risk_models.exp_cov(returns_df, returns_data=True, frequency=frequency)
    if method == "ledoit_wolf":
        cs = risk_models.CovarianceShrinkage(returns_df, returns_data=True, frequency=frequency)
        return cs.ledoit_wolf(shrinkage_target="constant_variance")
    raise ValueError(f"unknown cov method: {method}")


# ─────────────────── 选股层：相关性剪枝（Item 4） ───────────────────

def prune_correlated(returns_df: pd.DataFrame,
                     ranked_tickers: list[str],
                     max_corr: float = 0.7,
                     min_history: int = 126,
                     industries: dict[str, str] | None = None,
                     industry_cap: int = 1) -> tuple[list[str], list[dict]]:
    """按 ranked 顺序贪心剪枝。

    要求 returns_df 是**复权后**收益率（A 股前复权 qfq、美股 adjusted close）。
    不复权数据会让分红 / 拆股事件污染 corr 矩阵。

    剪枝逻辑（三档退化）：
      1. 样本 ≥ min_history(126) 天：用相关性矩阵，|ρ| > max_corr 则丢
      2. 30 ≤ 样本 < 126：用相关性但 logger.warning 提示噪音；阈值不变
      3. 样本 < 30：相关性不可信。若提供 industries → 退化到行业 cap（每行业最多 industry_cap 只）；否则按原顺序保留全部

    ranked_tickers 已按因子合成分降序排好（高分先入选）。
    返回 (kept, dropped)；dropped 元素含 vs/rho/method 用于审计。
    """
    available = [t for t in ranked_tickers if t in returns_df.columns]
    if len(available) <= 1:
        return available, []

    # 用对齐后的子矩阵算相关性，避免 NaN
    sub = returns_df[available].dropna()
    n_hist = len(sub)

    # 档位 3：样本太短 → 行业 fallback（如果给了 industries）
    if n_hist < 30:
        if industries:
            logger.warning(
                "prune_correlated: 样本仅 %d 天 (<30)，相关性不可信，退化到行业 cap=%d",
                n_hist, industry_cap)
            kept: list[str] = []
            dropped: list[dict] = []
            ind_counts: dict[str, int] = {}
            for tk in available:
                ind = industries.get(tk) or "_unknown_"
                if ind_counts.get(ind, 0) >= industry_cap:
                    dropped.append({"dropped": tk, "industry": ind,
                                    "method": "industry_fallback",
                                    "threshold": industry_cap})
                else:
                    kept.append(tk)
                    ind_counts[ind] = ind_counts.get(ind, 0) + 1
            return kept, dropped
        logger.warning(
            "prune_correlated: 样本仅 %d 天 (<30) 且无 industries fallback；按原顺序保留全部",
            n_hist)
        return available, []

    # 档位 2：警告但仍用 corr
    if n_hist < min_history:
        logger.warning(
            "prune_correlated: 样本 %d 天 < %d (推荐最小)，corr 估计偏噪音；"
            "若 watchlist 有较多新股，建议传 industries 参数启用行业 fallback",
            n_hist, min_history)

    # 档位 1：正常 corr 剪枝
    corr = sub.corr().abs()

    kept: list[str] = []
    dropped: list[dict] = []
    for tk in available:
        if not kept:
            kept.append(tk)
            continue
        worst_kept, worst_rho = max(
            ((k, float(corr.loc[tk, k])) for k in kept),
            key=lambda x: x[1],
        )
        if worst_rho > max_corr:
            dropped.append({"dropped": tk, "vs": worst_kept,
                            "rho": round(worst_rho, 3),
                            "threshold": max_corr,
                            "method": "corr",
                            "n_history": n_hist})
        else:
            kept.append(tk)
    return kept, dropped


# ─────────────────── 优化器（统一加 cov_method 参数） ───────────────────

def optimize_max_sharpe(returns_df: pd.DataFrame,
                        max_weight: float = 0.15,
                        min_weight: float = 0.02,
                        cov_method: str = "ledoit_wolf") -> dict[str, Any]:
    """精确 Max Sharpe 优化（PyPortfolioOpt cvxpy 求解）。

    ⚠️ returns_df 必须基于**复权后**价格（A 股前复权 qfq、美股 adjusted close）。
       不复权数据会让分红 / 拆股事件污染协方差和均值估计。

    比蒙特卡洛 20000 次更快、更精确。
    cov_method 默认 ledoit_wolf 收缩估计（小样本鲁棒）。
    """
    from pypfopt import EfficientFrontier, expected_returns

    mu = expected_returns.mean_historical_return(returns_df, returns_data=True, frequency=252)
    S = _build_cov(returns_df, method=cov_method)
    eff_max = _feasible_max_weight(returns_df, max_weight)

    ef = EfficientFrontier(mu, S, weight_bounds=(min_weight, eff_max))
    ef.max_sharpe(risk_free_rate=0.045)
    cleaned = ef.clean_weights()
    perf = ef.portfolio_performance(verbose=False, risk_free_rate=0.045)

    return {
        "method": f"max_sharpe (PyPortfolioOpt cvxpy, cov={cov_method})",
        "weights": dict(cleaned),
        "annual_return": round(perf[0] * 100, 2),
        "annual_volatility": round(perf[1] * 100, 2),
        "sharpe_ratio": round(perf[2], 3),
        "cov_method": cov_method,
    }


def optimize_min_volatility(returns_df: pd.DataFrame,
                            max_weight: float = 0.15,
                            min_weight: float = 0.0,
                            cov_method: str = "ledoit_wolf") -> dict[str, Any]:
    """最小波动率组合（防御型）。"""
    from pypfopt import EfficientFrontier, expected_returns

    mu = expected_returns.mean_historical_return(returns_df, returns_data=True, frequency=252)
    S = _build_cov(returns_df, method=cov_method)
    eff_max = _feasible_max_weight(returns_df, max_weight)

    ef = EfficientFrontier(mu, S, weight_bounds=(min_weight, eff_max))
    ef.min_volatility()
    cleaned = ef.clean_weights()
    perf = ef.portfolio_performance(verbose=False, risk_free_rate=0.045)

    return {
        "method": f"min_volatility (PyPortfolioOpt, cov={cov_method})",
        "weights": dict(cleaned),
        "annual_return": round(perf[0] * 100, 2),
        "annual_volatility": round(perf[1] * 100, 2),
        "sharpe_ratio": round(perf[2], 3),
        "cov_method": cov_method,
    }


def optimize_hrp(returns_df: pd.DataFrame,
                 cov_method: str = "ledoit_wolf") -> dict[str, Any]:
    """Hierarchical Risk Parity（HRP, Lopez de Prado 2016）。

    优势：
      - 无需协方差矩阵稳定性（对小样本鲁棒）
      - 在 out-of-sample 测试中常 > Markowitz
      - 自动按层级聚类决定权重

    cov_method: 与其他优化器一致（默认 ledoit_wolf）。HRP 内部用 corr 做聚类，
                仍受协方差噪音影响，传 shrinkage cov 让聚类更稳。
    """
    from pypfopt import HRPOpt

    # HRP 内部用相关矩阵做层次聚类。即使 HRP 对协方差不敏感，
    # 距离矩阵的稳定性仍受协方差噪音影响 — 一致性上传 ledoit_wolf 的 cov 更稳。
    cov = _build_cov(returns_df, method=cov_method)
    hrp = HRPOpt(returns=returns_df, cov_matrix=cov)
    hrp.optimize()
    weights = hrp.clean_weights()
    perf = hrp.portfolio_performance(verbose=False, risk_free_rate=0.045)

    return {
        "method": f"Hierarchical Risk Parity (Lopez de Prado 2016, cov={cov_method})",
        "weights": dict(weights),
        "annual_return": round(perf[0] * 100, 2),
        "annual_volatility": round(perf[1] * 100, 2),
        "sharpe_ratio": round(perf[2], 3),
        "cov_method": cov_method,
    }


def optimize_black_litterman(returns_df: pd.DataFrame,
                             v6_factor_scores: dict[str, float],
                             market_caps: dict[str, float] | None = None,
                             max_weight: float = 0.15,
                             cov_method: str = "ledoit_wolf") -> dict[str, Any]:
    """Black-Litterman 优化：把 v6 因子分数当作"主观 view"，与市场均衡先验贝叶斯组合。

    输入：
      v6_factor_scores: {ticker: composite_z_score}（v6 因子合成分）
                        z > 0 = 看多；z < 0 = 看空
      market_caps: {ticker: market_cap}（用于市场均衡先验）

    输出：贝叶斯后验最优权重。
    """
    from pypfopt import BlackLittermanModel, EfficientFrontier

    S = _build_cov(returns_df, method=cov_method)
    eff_max = _feasible_max_weight(returns_df, max_weight)

    # 1. 市场均衡先验（implied returns from market cap）
    if market_caps and len(market_caps) >= len(returns_df.columns):
        mcaps = pd.Series({t: market_caps.get(t, 1e9) for t in returns_df.columns})
        risk_aversion = 2.5  # 标准值
        prior = risk_aversion * S @ (mcaps / mcaps.sum())
    else:
        prior = pd.Series(0.08, index=returns_df.columns)  # 8% 等权先验

    # 2. v6 因子 view（z=1 → +5% 超额；z=2 → +10%；z=-1 → -5%）
    views = {t: (v6_factor_scores.get(t, 0) * 0.05) for t in returns_df.columns}
    Q = pd.Series([views[t] for t in returns_df.columns], index=returns_df.columns)

    # 3. Black-Litterman 后验
    bl = BlackLittermanModel(
        cov_matrix=S,
        pi=prior,
        absolute_views=Q,
        omega="default",
    )
    posterior_returns = bl.bl_returns()
    posterior_cov = bl.bl_cov()

    # 4. 在后验上做 max Sharpe
    ef = EfficientFrontier(posterior_returns, posterior_cov,
                           weight_bounds=(0.0, eff_max))
    ef.max_sharpe(risk_free_rate=0.045)
    cleaned = ef.clean_weights()
    perf = ef.portfolio_performance(verbose=False, risk_free_rate=0.045)

    return {
        "method": f"Black-Litterman (v6 factor views + market cap prior, cov={cov_method})",
        "weights": dict(cleaned),
        "annual_return": round(perf[0] * 100, 2),
        "annual_volatility": round(perf[1] * 100, 2),
        "sharpe_ratio": round(perf[2], 3),
        "prior_returns": prior.to_dict(),
        "view_returns": {t: round(v * 100, 2) for t, v in views.items()},
        "posterior_returns": posterior_returns.to_dict(),
        "cov_method": cov_method,
    }


def optimize_min_cvar(returns_df: pd.DataFrame,
                      beta: float = 0.95,
                      max_weight: float = 0.15) -> dict[str, Any]:
    """最小 CVaR（Conditional Value at Risk）组合 — 尾部风险敏感。

    在 2008 / 2020 极端崩盘里比 Markowitz 表现好（因为 Markowitz 假设正态，CVaR 不假设）。
    """
    from pypfopt import EfficientCVaR, expected_returns

    mu = expected_returns.mean_historical_return(returns_df, returns_data=True, frequency=252)
    eff_max = _feasible_max_weight(returns_df, max_weight)
    ec = EfficientCVaR(mu, returns_df, weight_bounds=(0.0, eff_max), beta=beta)
    ec.min_cvar()
    cleaned = ec.clean_weights()
    perf = ec.portfolio_performance(verbose=False)

    return {
        "method": f"Min CVaR (β={beta}, Rockafellar-Uryasev 2000)",
        "weights": dict(cleaned),
        "annual_return": round(perf[0] * 100, 2),
        "cvar": round(perf[1] * 100, 2),
        "beta": beta,
    }


def discrete_allocation(weights: dict[str, float], latest_prices: dict[str, float],
                        portfolio_value: float = 100_000) -> dict[str, Any]:
    """把权重转成"具体买几股 + 余多少现金"（解决整数股问题）。"""
    from pypfopt.discrete_allocation import DiscreteAllocation

    da = DiscreteAllocation(weights, pd.Series(latest_prices), total_portfolio_value=portfolio_value)
    allocation, leftover = da.lp_portfolio()
    return {
        "shares": allocation,
        "leftover_cash": round(leftover, 2),
        "total_value": portfolio_value,
    }


# ─────────────────── 风险反馈（Item 3） ───────────────────

# DEFAULT_RISK_LIMITS — 中等风险偏好（成长 + 价值混合）的实务基线。
# 风险偏好不同请按需覆盖（risk_limits=... 传入 risk_aware_optimize）：
#   保守（防御 / 退休账户）：max_drawdown=-0.15, annual_vol=0.20, cvar_95_daily=-0.025
#   中等（默认）           ：max_drawdown=-0.25, annual_vol=0.30, cvar_95_daily=-0.040
#   激进（成长 / 高 Beta）  ：max_drawdown=-0.40, annual_vol=0.40, cvar_95_daily=-0.060
# 依据：S&P 500 历史滚动 5Y max DD ≈ -56%（2008）、典型成长股组合 vol 30-35%、
#       SPY 日 95% CVaR 约 -2.5%（平静期）至 -5%（危机期）。
DEFAULT_RISK_LIMITS: dict[str, float] = {
    # 历史样本内的最大回撤底线（更低 = 更严格）
    "max_drawdown": -0.25,
    # 年化波动率上限
    "annual_vol": 0.30,
    # 每日 95% CVaR（Expected Shortfall）下限，单位是日收益率
    "cvar_95_daily": -0.04,
}


def _portfolio_realized_metrics(weights: dict[str, float],
                                returns_df: pd.DataFrame) -> dict[str, float] | None:
    """用历史样本算候选组合的「样本内」年化波动 / 最大回撤 / 日 CVaR_95。

    样本内 ≠ 未来，但能用作优化器内部的风险闸门，比事后报告早一步发现问题。
    """
    cols = [t for t, w in weights.items() if w > 1e-6 and t in returns_df.columns]
    if not cols:
        return None
    w = np.array([weights[t] for t in cols], dtype=float)
    s = w.sum()
    if s <= 1e-9:
        return None
    w = w / s  # 归一到 1（cash 部分已被剥离）
    R = returns_df[cols].dropna().values
    if len(R) < 30:
        return None
    port = R @ w
    cum = np.cumprod(1.0 + port)
    peak = np.maximum.accumulate(cum)
    dd = (cum - peak) / peak
    var_95 = float(np.percentile(port, 5))
    tail = port[port <= var_95]
    cvar_95 = float(tail.mean()) if len(tail) > 0 else var_95
    return {
        "annual_vol": float(port.std(ddof=1) * np.sqrt(252)),
        "max_drawdown": float(dd.min()),
        "cvar_95_daily": cvar_95,
        "annual_return": float(port.mean() * 252),
        "sample_days": int(len(R)),
    }


def _which_breached(metrics: dict[str, float] | None,
                    limits: dict[str, float]) -> list[str]:
    """返回违规字段列表。空 = 全过。"""
    if not metrics:
        return ["no_metrics"]
    bad: list[str] = []
    if metrics["max_drawdown"] < limits["max_drawdown"]:
        bad.append(f"max_drawdown {metrics['max_drawdown']:.2%} < {limits['max_drawdown']:.2%}")
    if metrics["annual_vol"] > limits["annual_vol"]:
        bad.append(f"annual_vol {metrics['annual_vol']:.2%} > {limits['annual_vol']:.2%}")
    if metrics["cvar_95_daily"] < limits["cvar_95_daily"]:
        bad.append(f"cvar_95_daily {metrics['cvar_95_daily']:.2%} < {limits['cvar_95_daily']:.2%}")
    return bad


def _attach_cash_meta(result: dict[str, Any], cash_pct: float) -> dict[str, Any]:
    """把建议 cash_pct 作元数据附到结果上（不缩放 weights）。

    ⚠️ 重要约定（与 PyPortfolioOpt 一致）：
      - weights 始终 sum=1，代表"如果 100% 资金投向股票"的配置
      - cash_pct 是建议留出的现金缓冲（元数据）
      - 调用方决定是否真的把权重缩到 (1 - cash_pct)：
            real_weights = {t: w * (1 - cash_pct) for t, w in weights.items()}
            real_weights["$CASH"] = cash_pct  # 想显式 cash 行就这样
      - stage_metrics 里的 annual_ret/vol 也是 100% 投资视角，
        实盘乘以 (1 - cash_pct) 才是真实组合数字

    旧版本 _scale_for_cash 隐式把 weights 缩到 sum=0.95，导致：
      1. weights.sum() ≠ 1 — sum-to-1 检查失败 / discrete_allocation 资金分配异常
      2. annual_ret/vol 计算在 unscaled 上，与 weights 不一致
      3. cash 缓冲不可见 — 字典里没有 $CASH 键
    """
    out = dict(result)
    out["cash_pct"] = float(cash_pct)
    return out


def risk_aware_optimize(returns_df: pd.DataFrame,
                        ranked_tickers: list[str] | None = None,
                        max_weight: float = 0.15,
                        min_weight: float = 0.02,
                        cash_pct: float = 0.05,
                        cov_method: str = "ledoit_wolf",
                        max_corr: float = 0.7,
                        prune_corr: bool = True,
                        risk_limits: dict[str, float] | None = None) -> dict[str, Any]:
    """风险闸门反馈到优化的多级流水。

    ⚠️ returns_df 必须基于**复权后**价格（A 股前复权 qfq、美股 adjusted close）。
       不复权数据会让分红 / 拆股事件污染 corr / cov 矩阵 → 银行股相关性虚高、剪枝错误。

    ⚠️ 返回的 weights 始终 sum=1（"100% 投资"基线视角）。cash_pct 是元数据 / 建议值，
       由调用方自行决定缩放：real_target = {t: w * (1 - cash_pct) for ...}

    逻辑（凡命中 risk_limits 任一项视为「破线」）：
      Stage 0: max_sharpe（Ledoit-Wolf）, cash 建议 cash_pct (默认 5%)
      Stage 1: 收紧 max_weight (×0.6), cash 建议 +10pp (15%)
      Stage 2: min_cvar（尾部风险敏感）, cash 建议 +20pp (25%)
      Stage 3: min_volatility 兜底, cash 建议 +25pp (30%, 上限)

    返回 dict 里 risk_aware_stage 标记最终用了哪一级，stages 里有每级 trace。

    与「事后报告」的区别：
      原 risk_metrics.py 是组合**已经成形**后跑指标 → 出问题只能调仓
      这里是优化器**输出权重前**先 in-sample 评估 → 直接换更稳的方案
    """
    limits = {**DEFAULT_RISK_LIMITS, **(risk_limits or {})}

    # 1) 先做相关性剪枝（如果给了 ranked_tickers）
    pruned_log: list[dict] = []
    if prune_corr and ranked_tickers:
        kept, pruned_log = prune_correlated(returns_df, ranked_tickers, max_corr=max_corr)
        if kept:
            returns_df = returns_df[kept]

    stages: list[dict[str, Any]] = []

    def _try(stage_idx: int, label: str, fn) -> tuple[dict | None, list[str]]:
        try:
            res = fn()
        except Exception as e:
            stages.append({"stage": stage_idx, "label": label, "error": str(e)[:160]})
            return None, ["error"]
        m = _portfolio_realized_metrics(res["weights"], returns_df)
        breached = _which_breached(m, limits)
        stages.append({"stage": stage_idx, "label": label,
                       "metrics": m, "breached": breached})
        return res, breached

    # 每级 stage 的 cash 建议（破线越多 → cash 越高）。上限 30%。
    # weights 始终 sum=1，以下 cash_pct 仅作元数据，调用方自己决定是否缩放。
    cash_by_stage = {
        0: cash_pct,                         # 默认 5%
        1: min(0.30, cash_pct + 0.10),       # 15%
        2: min(0.30, cash_pct + 0.20),       # 25%
        3: min(0.30, cash_pct + 0.25),       # 30%
    }

    # Stage 0: max_sharpe
    res, bad = _try(0, "max_sharpe(ledoit_wolf)",
                    lambda: optimize_max_sharpe(returns_df, max_weight=max_weight,
                                                min_weight=min_weight,
                                                cov_method=cov_method))
    if res is not None and not bad:
        out = _attach_cash_meta(res, cash_by_stage[0])
        out.update({"risk_aware_stage": 0, "stages": stages,
                    "pruned_dropped": pruned_log,
                    "selected_tickers": list(returns_df.columns),
                    "risk_limits": limits})
        return out

    # Stage 1: 收紧 max_weight + 提高建议 cash
    # 注意 tighter_max 必须满足 N*tighter_max ≥ 1（权重和=1 的可行性下界），
    # 否则 cvxpy 直接报 infeasible；这里加 1.05/N 缓冲。
    n_assets = max(1, len(returns_df.columns))
    tighter_max = max(0.05, max_weight * 0.6, 1.05 / n_assets)
    tighter_min = min(min_weight, tighter_max * 0.5)
    res, bad = _try(1, f"max_sharpe(max_w={tighter_max:.2f}, cash={cash_by_stage[1]:.2f})",
                    lambda: optimize_max_sharpe(returns_df, max_weight=tighter_max,
                                                min_weight=tighter_min,
                                                cov_method=cov_method))
    if res is not None and not bad:
        out = _attach_cash_meta(res, cash_by_stage[1])
        out.update({"risk_aware_stage": 1, "stages": stages,
                    "pruned_dropped": pruned_log,
                    "selected_tickers": list(returns_df.columns),
                    "risk_limits": limits})
        return out

    # Stage 2: min_cvar — 尾部风险敏感
    res, bad = _try(2, f"min_cvar(max_w={tighter_max:.2f}, cash={cash_by_stage[2]:.2f})",
                    lambda: optimize_min_cvar(returns_df, max_weight=tighter_max, beta=0.95))
    if res is not None and not bad:
        out = _attach_cash_meta(res, cash_by_stage[2])
        out.update({"risk_aware_stage": 2, "stages": stages,
                    "pruned_dropped": pruned_log,
                    "selected_tickers": list(returns_df.columns),
                    "risk_limits": limits})
        return out

    # Stage 3: min_volatility 兜底
    res, bad = _try(3, f"min_volatility(max_w={tighter_max:.2f}, cash={cash_by_stage[3]:.2f})",
                    lambda: optimize_min_volatility(returns_df, max_weight=tighter_max,
                                                   cov_method=cov_method))
    if res is None:
        return {"error": "all stages failed", "stages": stages,
                "pruned_dropped": pruned_log}
    out = _attach_cash_meta(res, cash_by_stage[3])
    out.update({"risk_aware_stage": 3, "stages": stages,
                "pruned_dropped": pruned_log,
                "selected_tickers": list(returns_df.columns),
                "risk_limits": limits,
                "warning": "fallback to min_volatility; risk limits still breached"
                           if bad else None})
    return out


def compare_methods(returns_df: pd.DataFrame,
                    v6_factor_scores: dict[str, float] | None = None,
                    max_weight: float = 0.15,
                    cov_method: str = "ledoit_wolf") -> dict[str, dict]:
    """跑全部 4 种优化方法，给对比表。"""
    results = {}
    try:
        results["max_sharpe"] = optimize_max_sharpe(returns_df, max_weight=max_weight,
                                                   cov_method=cov_method)
    except Exception as e:
        results["max_sharpe"] = {"error": str(e)[:100]}
    try:
        results["min_volatility"] = optimize_min_volatility(returns_df, max_weight=max_weight,
                                                            cov_method=cov_method)
    except Exception as e:
        results["min_volatility"] = {"error": str(e)[:100]}
    try:
        results["hrp"] = optimize_hrp(returns_df)
    except Exception as e:
        results["hrp"] = {"error": str(e)[:100]}
    if v6_factor_scores:
        try:
            results["black_litterman"] = optimize_black_litterman(
                returns_df, v6_factor_scores, max_weight=max_weight, cov_method=cov_method
            )
        except Exception as e:
            results["black_litterman"] = {"error": str(e)[:100]}
    try:
        results["min_cvar"] = optimize_min_cvar(returns_df, max_weight=max_weight)
    except Exception as e:
        results["min_cvar"] = {"error": str(e)[:100]}
    return results
