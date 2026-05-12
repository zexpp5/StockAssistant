"""BAB (Betting Against Beta) 防御模式 — 二审 P0-4 (2026-05-12)。

学术依据：
  - Frazzini-Pedersen (2014) JFE "Betting Against Beta"
    实证：高 Beta 股票风险调整后收益低于低 Beta（与 CAPM 预测相反）。
    AQR 论文显示 BAB 策略在 1928-2012 年美股 / 1989-2012 全球股市均显著盈利。
  - Black (1972) / Asness-Frazzini (2013)
    解释：杠杆约束 → 投资者追逐高 Beta 股票 → 高 Beta 估值偏贵 → 风险调整后收益低

防御模式应用（**不做空** — 我们是 long-only）：
  当 regime_filter 进入 CAUTIOUS_2 / RISK_OFF / PANIC 时：
    1. 高 Beta（> beta_cap）股票权重打折
    2. 等价于"组合 Beta 收敛到 < 1.0"
    3. 释放出的权重转入现金 / 防御 ETF

⚠️ 二审准入门槛：本模块仅提供函数，**不自动集成 optimize_portfolio**，
   等 walk-forward 消融通过（验证 risk_off 期 BAB 真降低 MDD）才接入主路径。

接入计划（待 ablation_bab.md 通过后）：
  - optimize_portfolio.run() 加 enable_bab_defense 参数
  - 在 [5pre/6] kelly cap 之前应用 bab_weight_adjustment
"""
from __future__ import annotations
import logging
import time
from typing import Sequence

logger = logging.getLogger(__name__)


def compute_betas(tickers: Sequence[str], period_days: int = 252,
                  benchmark: str = "SPY", throttle_seconds: float = 0.3) -> dict[str, float | None]:
    """批量算 ticker vs benchmark 的 Beta（最近 period_days 日）。

    Beta = cov(stock_returns, benchmark_returns) / var(benchmark_returns)
    """
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        return {tk: None for tk in tickers}

    # 一次性拉 benchmark
    try:
        bm = yf.Ticker(benchmark).history(period=f"{period_days + 30}d")
        if bm is None or bm.empty:
            return {tk: None for tk in tickers}
        bm_ret = bm["Close"].pct_change().dropna()[-period_days:]
        bm_var = float(bm_ret.var())
    except Exception as e:
        logger.warning("benchmark %s 拉取失败: %s", benchmark, e)
        return {tk: None for tk in tickers}

    if bm_var <= 0:
        return {tk: None for tk in tickers}

    out: dict[str, float | None] = {}
    for tk in tickers:
        try:
            h = yf.Ticker(tk).history(period=f"{period_days + 30}d")
            if h is None or h.empty:
                out[tk] = None
                continue
            stock_ret = h["Close"].pct_change().dropna()
            # 对齐日期（取交集）
            aligned = pd.concat([stock_ret, bm_ret], axis=1, join="inner").dropna()
            if len(aligned) < period_days // 2:
                out[tk] = None
                continue
            aligned = aligned.iloc[-period_days:]
            cov = float(aligned.iloc[:, 0].cov(aligned.iloc[:, 1]))
            beta = cov / bm_var
            out[tk] = round(beta, 3)
            if throttle_seconds > 0:
                time.sleep(throttle_seconds)
        except Exception as e:
            logger.debug("beta 计算失败 %s: %s", tk, e)
            out[tk] = None
    return out


def bab_weight_adjustment(weights: dict[str, float],
                           betas: dict[str, float | None],
                           regime: str = "RISK_ON",
                           beta_cap: float = 1.0,
                           high_beta_downweight: float = 0.5) -> tuple[dict[str, float], dict]:
    """按 regime 给高 Beta 股票打折，溢出权重转现金（防御模式）。

    参数：
      weights              {ticker: weight} 原始组合权重
      betas                {ticker: beta} 已算好的 Beta（缺失视作 1.0 = 市场均值）
      regime               from get_dynamic_gross_exposure(): RISK_ON / CAUTIOUS_1 /
                           CAUTIOUS_2 / RISK_OFF / PANIC
      beta_cap             高 Beta 阈值（默认 1.0 = 跟随大盘）
      high_beta_downweight CAUTIOUS_2 时高 Beta 仓位打折系数（默认 0.5 = 减半）

    档位映射（基于 Frazzini-Pedersen 实证：风险升级时低 Beta 风险调整后表现更好）：
      RISK_ON       → 不调整（原权重）
      CAUTIOUS_1    → 不调整
      CAUTIOUS_2    → 高 Beta 仓位 × 0.5
      RISK_OFF      → 高 Beta 仓位 × 0.3
      PANIC         → 高 Beta 仓位 × 0.1（接近清仓）

    返回 (adjusted_weights, info)：
      adjusted_weights  调整后权重（高 Beta 减仓后总和 < 1，差额是现金）
      info              {high_beta_tickers, weights_clipped, cash_increment, ...}
    """
    # 档位 downweight 系数
    downweight_map = {
        "RISK_ON":    1.0,
        "CAUTIOUS_1": 1.0,
        "CAUTIOUS_2": high_beta_downweight,  # 默认 0.5
        "RISK_OFF":   0.3,
        "PANIC":      0.1,
    }
    factor = downweight_map.get(regime, 1.0)

    high_beta_tickers: list[str] = []
    adjusted: dict[str, float] = {}
    cash_increment = 0.0

    for tk, w in weights.items():
        beta = betas.get(tk)
        # 缺失 Beta 视作 1.0（市场均值，不打折也不放过）
        beta_use = beta if beta is not None else 1.0
        if beta_use > beta_cap:
            new_w = w * factor
            cash_increment += w - new_w
            adjusted[tk] = new_w
            if factor < 1.0:
                high_beta_tickers.append(tk)
        else:
            adjusted[tk] = w

    info = {
        "regime": regime,
        "factor_applied": factor,
        "beta_cap": beta_cap,
        "high_beta_tickers": high_beta_tickers,
        "n_high_beta": len(high_beta_tickers),
        "cash_increment": round(cash_increment, 4),
        "downweight_map": downweight_map,
        "interpretation": (
            f"regime={regime} factor={factor} → "
            f"{len(high_beta_tickers)} 只 Beta>{beta_cap} 仓位 ×{factor} "
            f"→ {cash_increment * 100:.1f}pp 转入现金"
        ),
    }
    return adjusted, info


def cli_smoketest():
    """快速 smoke test：对几只样本算 Beta + 模拟 3 个 regime 调整。"""
    samples = ["NVDA", "AAPL", "MSFT", "KO", "JNJ", "TLT"]
    print(f"=== BAB Smoke Test ({len(samples)} 只样本，throttle 0s) ===\n")
    print("Beta vs SPY (252d)...")
    betas = compute_betas(samples, throttle_seconds=0.0)
    for tk, b in betas.items():
        flag = "HIGH" if (b or 0) > 1.0 else ("LOW" if (b or 0) < 0.7 else "MID")
        print(f"  {tk:6} β={b}  [{flag}]")
    print()
    # 等权初始权重
    init_w = {tk: 1.0 / len(samples) for tk in samples}
    for regime in ["RISK_ON", "CAUTIOUS_2", "RISK_OFF", "PANIC"]:
        adj, info = bab_weight_adjustment(init_w, betas, regime=regime)
        deployed = sum(adj.values())
        print(f"regime={regime:11} factor={info['factor_applied']:.2f}  "
              f"deployed={deployed:.2%}  cash={(1-deployed)*100:.1f}pp  "
              f"high_β cnt={info['n_high_beta']}")


if __name__ == "__main__":
    cli_smoketest()
