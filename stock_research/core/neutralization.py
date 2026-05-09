"""因子中性化：Industry z-score + Size residual（Barra-like 风格剔除）。

学术依据：
  - Fama & French (1992) "The Cross-Section of Expected Stock Returns" — size 因子显著
  - Rosenberg & Marathe (1976) "Common Factors in Security Returns: Microeconomic
    Determinants and Macroeconomic Correlates" — Barra 风险模型奠基论文
  - Asness, Moskowitz & Pedersen (2013) — 因子中性化是行业标准做法

为什么要做：
  没有中性化时，因子分数会被「行业溢价」和「小盘溢价」污染：
    - 例：所有半导体股 12-1 动量都 +80%，全部进 Top 10 → 实际是行业 beta，不是 alpha
    - 例：小盘股波动大、动量数字大，但流动性差，无法真买
  中性化后比较的是「同一行业、同一市值档内的相对优势」，更接近真 alpha。

用法：
  from stock_research.core.neutralization import neutralize_all
  df = neutralize_all(df, ["momentum", "reversal", "pead"],
                      industry_col="industry", market_cap_col="market_cap")
  # df 新增 n_momentum / n_reversal / n_pead 三列（中性化后的因子）

纯函数；输入 pandas.DataFrame；输出 pandas.DataFrame；无 I/O。
"""
from __future__ import annotations
import logging
from typing import Iterable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ─────────── 行业 z-score 中性化 ───────────

def industry_zscore(df: pd.DataFrame, factor_col: str,
                    industry_col: str = "industry",
                    min_group_size: int = 3) -> pd.Series:
    """对因子按行业分组做 z-score。

    分组样本 < min_group_size 的行业，回退到全市场 z-score（避免 1-2 个样本的极端值）。
    NaN 输入 → NaN 输出（保持原 dtype 一致）。
    """
    out = pd.Series(index=df.index, dtype=float)
    if industry_col not in df.columns:
        logger.warning("industry column %r missing, skip industry neutralization", industry_col)
        return df[factor_col].copy()

    market_mean = df[factor_col].mean()
    market_std = df[factor_col].std(ddof=0)

    for ind, group in df.groupby(industry_col, dropna=False):
        x = group[factor_col]
        valid = x.dropna()
        if len(valid) < min_group_size or pd.isna(ind) or ind == "":
            # 退回到全市场 z-score
            if market_std and market_std > 0:
                out.loc[group.index] = (x - market_mean) / market_std
            else:
                out.loc[group.index] = 0.0
        else:
            mean = valid.mean()
            std = valid.std(ddof=0)
            if std and std > 0:
                out.loc[group.index] = (x - mean) / std
            else:
                out.loc[group.index] = 0.0
    return out


# ─────────── 市值（Size）中性化 ───────────

def size_neutralize(df: pd.DataFrame, factor_col: str,
                    market_cap_col: str = "market_cap") -> pd.Series:
    """剔除市值因子的影响：factor ~ log(market_cap) 的 OLS 残差。

    经典做法（Barra 风险模型）：因子值 = α + β·log(MCap) + ε，取 ε 作为 size-neutral 因子。
    样本不足（<10）或 market_cap 缺失则原样返回。
    """
    if market_cap_col not in df.columns:
        logger.warning("market_cap column %r missing, skip size neutralization", market_cap_col)
        return df[factor_col].copy()

    work = df[[factor_col, market_cap_col]].copy()
    work[market_cap_col] = pd.to_numeric(work[market_cap_col], errors="coerce")
    work = work[(work[market_cap_col] > 0)].dropna(subset=[factor_col, market_cap_col])

    if len(work) < 10:
        return df[factor_col].copy()

    x = np.log(work[market_cap_col].astype(float).values)
    y = work[factor_col].astype(float).values
    # OLS: y = b*x + a，coef = [b, a]
    coef = np.polyfit(x, y, 1)
    fitted = coef[0] * x + coef[1]
    residual = pd.Series(y - fitted, index=work.index)

    # NaN 处保留 NaN，其余替换为残差
    out = df[factor_col].copy().astype(float)
    out.loc[residual.index] = residual
    return out


# ─────────── 组合中性化 ───────────

def neutralize(df: pd.DataFrame, factor_col: str,
               industry_col: str = "industry",
               market_cap_col: str = "market_cap",
               steps: Iterable[str] = ("industry", "size")) -> pd.Series:
    """组合中性化：按 steps 顺序依次做（industry → size 通常是默认）。"""
    df_work = df.copy()
    work_col = factor_col
    for step in steps:
        tmp_col = f"_{factor_col}_after_{step}"
        if step == "industry":
            df_work[tmp_col] = industry_zscore(df_work, work_col, industry_col)
        elif step == "size":
            df_work[tmp_col] = size_neutralize(df_work, work_col, market_cap_col)
        else:
            logger.warning("unknown neutralization step: %s (skipped)", step)
            continue
        work_col = tmp_col
    return df_work[work_col].rename(factor_col)


def neutralize_all(df: pd.DataFrame, factor_cols: list[str],
                   industry_col: str = "industry",
                   market_cap_col: str = "market_cap",
                   steps: Iterable[str] = ("industry", "size"),
                   prefix: str = "n_") -> pd.DataFrame:
    """批量中性化多个因子，返回原 df 加 prefix_<factor> 列。"""
    out = df.copy()
    for f in factor_cols:
        if f not in out.columns:
            logger.warning("factor column %r not in df, skip", f)
            continue
        out[f"{prefix}{f}"] = neutralize(df, f, industry_col, market_cap_col, steps)
    return out


# ─────────── 诊断：中性化前后对比 ───────────

def diagnose(df: pd.DataFrame, factor_col: str,
             neutralized_col: str | None = None,
             industry_col: str = "industry",
             ticker_col: str = "ticker", top_n: int = 10) -> dict:
    """对比中性化前/后的 Top N 标的，看效果。

    返回：
      {
        'before_top': [...],
        'after_top': [...],
        'changed_in': [...],  # 中性化后新进入 top
        'changed_out': [...], # 中性化后掉出 top
        'industry_distribution_before': {ind: n},
        'industry_distribution_after': {ind: n},
      }
    """
    if neutralized_col is None:
        neutralized_col = f"n_{factor_col}"
    if neutralized_col not in df.columns:
        df = neutralize_all(df, [factor_col], industry_col=industry_col)

    before = df.sort_values(factor_col, ascending=False).head(top_n)
    after = df.sort_values(neutralized_col, ascending=False).head(top_n)

    before_set = set(before[ticker_col]) if ticker_col in df.columns else set()
    after_set = set(after[ticker_col]) if ticker_col in df.columns else set()

    return {
        "before_top": before[ticker_col].tolist() if ticker_col in df.columns else [],
        "after_top": after[ticker_col].tolist() if ticker_col in df.columns else [],
        "changed_in": list(after_set - before_set),
        "changed_out": list(before_set - after_set),
        "industry_distribution_before": (
            before[industry_col].value_counts().to_dict() if industry_col in df.columns else {}
        ),
        "industry_distribution_after": (
            after[industry_col].value_counts().to_dict() if industry_col in df.columns else {}
        ),
    }
