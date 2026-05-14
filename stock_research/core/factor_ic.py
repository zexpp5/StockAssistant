"""因子 IC（Information Coefficient）持续监测。

学术依据：
  - Grinold (1994) "Alpha is Volatility Times IC Times Score"
    定义：IC = corr(factor_score_t, forward_return_{t+k})
  - Grinold-Kahn (2000) Active Portfolio Management — 行业标准
    IC > 0.05 = 有效因子；IC < 0.02 = 边际；IC ≈ 0 / 翻负 = 失效或反向

为什么需要：
  v6 五因子等权（Piotroski + 12-1 动量 + 1 月反转 + PEAD + 分析师）
  但**没人监督每个因子还有没有用** —— 一旦某因子在新 regime 下衰减，
  等权组合会被它拖累。每月跑一次 IC，自动告警衰减因子。

输入（来自历史数据）：
  对每个时间窗口 t，准备：
    factors:  {ticker: {factor_name: score}}    ← t 时刻
    forward_returns:  {ticker: future_return}    ← t+k（k 通常 1 个月或 3 个月）

核心函数：
  - compute_ic(factors, forward_returns, factor_name) → IC（单因子单期）
  - rolling_ic(history, factor_name, window=12) → IC 时间序列
  - decay_alert(ic_series, threshold=0.02) → 衰减告警

纯函数，输入 dict/DataFrame，输出 dict。无 I/O。
"""
from __future__ import annotations
from typing import Sequence

import numpy as np
import pandas as pd

try:  # scipy is optional in the daily runtime; pandas ranking is enough here.
    from scipy.stats import spearmanr as _scipy_spearmanr
    from scipy.stats import t as _scipy_t_dist
except Exception:  # pragma: no cover - exercised on lean production envs
    _scipy_spearmanr = None
    _scipy_t_dist = None


def _spearmanr(scores: np.ndarray, rets: np.ndarray) -> tuple[float, float]:
    if _scipy_spearmanr is not None:
        ic, pval = _scipy_spearmanr(scores, rets)
        return float(ic), float(pval)
    score_rank = pd.Series(scores).rank(method="average")
    ret_rank = pd.Series(rets).rank(method="average")
    ic = score_rank.corr(ret_rank)
    return float(ic) if ic == ic else float("nan"), float("nan")


def _pearson_pvalue(ic: float, n: int) -> float:
    if _scipy_t_dist is None:
        return float("nan")
    t_stat = ic * np.sqrt((n - 2) / (1 - ic ** 2))
    return float(2 * (1 - _scipy_t_dist.cdf(abs(t_stat), df=n - 2)))


# ─────────── 单期 IC ───────────

def compute_ic(factors: dict[str, dict[str, float]],
               forward_returns: dict[str, float],
               factor_name: str,
               method: str = "spearman") -> dict:
    """算单因子单期 IC（Spearman 排序相关）。

    method:
      - "spearman"（默认，行业标准）：因子分数排名 vs 收益排名的相关系数
      - "pearson"：原值相关系数（对极端值敏感，少用）

    返回 {
      "ic": float,                       # 相关系数
      "ic_pvalue": float,                # 显著性 p 值
      "n_samples": int,                  # 有效样本数
      "factor_name": str,
      "method": str,
    }
    """
    paired = []
    for ticker, score_dict in factors.items():
        score = score_dict.get(factor_name)
        ret = forward_returns.get(ticker)
        if score is None or ret is None:
            continue
        if not np.isfinite(score) or not np.isfinite(ret):
            continue
        paired.append((score, ret))

    if len(paired) < 5:
        return {"factor_name": factor_name, "ic": float("nan"),
                "ic_pvalue": float("nan"), "n_samples": len(paired), "method": method}

    scores = np.array([s for s, _ in paired])
    rets = np.array([r for _, r in paired])

    if method == "spearman":
        ic, pval = _spearmanr(scores, rets)
    elif method == "pearson":
        ic = float(np.corrcoef(scores, rets)[0, 1])
        # 用 t 分布近似 p 值
        n = len(paired)
        if abs(ic) < 0.999:
            pval = _pearson_pvalue(ic, n)
        else:
            pval = 0.0
    else:
        raise ValueError(f"unknown method: {method}")

    return {
        "factor_name": factor_name,
        "ic": float(ic) if not np.isnan(ic) else float("nan"),
        "ic_pvalue": float(pval) if not np.isnan(pval) else float("nan"),
        "n_samples": len(paired),
        "method": method,
    }


# ─────────── 多期 IC（滚动）───────────

def rolling_ic(history: list[tuple[dict, dict]],
               factor_name: str,
               method: str = "spearman") -> list[dict]:
    """对一系列历史 (factors, forward_returns) 数据，每个时间点算 IC。

    history: [(factors_t1, forward_t1), (factors_t2, forward_t2), ...]
    返回每个时间点的 IC 列表。
    """
    return [compute_ic(f, r, factor_name, method=method) for f, r in history]


# ─────────── IC 摘要统计 ───────────

def ic_summary(ic_list: Sequence[dict]) -> dict:
    """汇总一段时间的 IC 表现：均值、标准差、IR、命中率。

    Information Ratio (IR) = mean(IC) / std(IC) — Grinold 1994 经典定义。
    IR > 0.5 = 优秀；0.3-0.5 = 良好；< 0.2 = 边际。
    """
    valid = [r["ic"] for r in ic_list if not np.isnan(r["ic"])]
    if not valid:
        return {"n_periods": 0}

    arr = np.array(valid)
    return {
        "n_periods": len(valid),
        "mean_ic": round(float(arr.mean()), 4),
        "std_ic": round(float(arr.std(ddof=1)) if len(arr) > 1 else 0.0, 4),
        "ic_ir": round(float(arr.mean() / arr.std(ddof=1)) if len(arr) > 1 and arr.std(ddof=1) > 0 else 0.0, 4),
        "hit_rate": round(float((arr > 0).mean()), 3),  # IC > 0 的比例
        "max_ic": round(float(arr.max()), 4),
        "min_ic": round(float(arr.min()), 4),
    }


# ─────────── 衰减告警 ───────────

def decay_alert(ic_summary_dict: dict,
                threshold_strong: float = 0.05,
                threshold_marginal: float = 0.02) -> dict:
    """根据 IC 摘要判定因子状态。

    分级（Grinold-Kahn 2000 行业标准）：
      🟢 strong       mean_ic ≥ threshold_strong（默认 0.05）
      🟡 marginal     threshold_marginal ≤ mean_ic < threshold_strong
      🔴 decayed      |mean_ic| < threshold_marginal
      ⛔ inverted     mean_ic < -threshold_marginal（反向 alpha）
    """
    mean_ic = ic_summary_dict.get("mean_ic")
    if mean_ic is None:
        return {"status": "no_data", "verdict": "无数据"}

    if mean_ic >= threshold_strong:
        status, icon, verdict = "strong", "🟢", f"有效因子（mean IC = {mean_ic:.3f} ≥ {threshold_strong}）"
    elif mean_ic >= threshold_marginal:
        status, icon, verdict = "marginal", "🟡", f"边际有效（mean IC = {mean_ic:.3f}，建议观察）"
    elif mean_ic > -threshold_marginal:
        status, icon, verdict = "decayed", "🔴", f"已失效（mean IC = {mean_ic:.3f} ≈ 0）"
    else:
        status, icon, verdict = "inverted", "⛔", f"反向 alpha（mean IC = {mean_ic:.3f} < 0，建议剔除或反向使用）"

    return {
        "status": status,
        "icon": icon,
        "verdict": verdict,
        "mean_ic": mean_ic,
        "ic_ir": ic_summary_dict.get("ic_ir", 0.0),
        "hit_rate": ic_summary_dict.get("hit_rate", 0.0),
    }


# ─────────── 一站式：批量算多个因子 ───────────

def audit_factors(history: list[tuple[dict, dict]],
                  factor_names: list[str],
                  method: str = "spearman") -> dict:
    """对多个因子批量做 IC 监测，返回每个因子的诊断。

    返回 {
      "factors": {factor_name: {"summary": {...}, "alert": {...}}},
      "ranking": [(factor_name, mean_ic), ...]  # 按 mean_ic 降序
    }
    """
    out = {"factors": {}}
    rankings = []
    for f in factor_names:
        ic_list = rolling_ic(history, f, method=method)
        summary = ic_summary(ic_list)
        alert = decay_alert(summary)
        out["factors"][f] = {
            "summary": summary,
            "alert": alert,
            "ic_history": [{"ic": r["ic"], "n": r["n_samples"]} for r in ic_list],
        }
        if "mean_ic" in summary:
            rankings.append((f, summary["mean_ic"]))
    out["ranking"] = sorted(rankings, key=lambda x: -x[1])
    return out
