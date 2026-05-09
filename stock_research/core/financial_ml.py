"""De Prado 学术 ML 算法（自实现，替代下架的 mlfinlab）。

学术依据：
  Marcos Lopez de Prado (2018) "Advances in Financial Machine Learning"
  本模块实现该书核心算法（Chapter 3 + 7），替代被商业化下架的 mlfinlab。

实现的算法：
  1. **Triple Barrier Labeling** (Chapter 3.2-3.3)
     给每个交易事件标签：止盈 / 止损 / 时间到期
     替代固定 holding period（避免 forward-looking bias）

  2. **Purged K-Fold Cross Validation** (Chapter 7.4)
     标准 k-fold 在金融时序上有 label leakage；
     Purged 在 train/test 边界移除 overlap 标签；
     Embargo 在 test 后额外删一段防 serial correlation。

  3. **Sample Uniqueness** (Chapter 4.3)
     算每个样本的 "uniqueness"（不与其他样本时间重叠的部分），
     用于 ML 模型 weight 不均衡数据。

为什么自实现：
  - mlfinlab 已被 Hudson&Thames 商业化，PyPI 下架
  - 这些算法是公开论文，可独立实现
  - 不依赖任何商业库

不依赖：仅 numpy + pandas + scipy（标准 ML 栈）。
"""
from __future__ import annotations
import logging
from typing import Iterator

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ─────────── Triple Barrier Labeling (De Prado 2018 Ch 3) ───────────

def triple_barrier_label(prices: pd.Series,
                         events: list[pd.Timestamp] | pd.DatetimeIndex,
                         pt: float = 0.05,
                         sl: float = -0.05,
                         max_hold_days: int = 20) -> pd.DataFrame:
    """三屏标签法（Triple Barrier Labeling）。

    对每个 event（入场日期）打标签：
      +1: 上屏（止盈）先触发 → 价格 ≥ entry × (1 + pt)
      -1: 下屏（止损）先触发 → 价格 ≤ entry × (1 + sl)
       0: 时间屏障（max_hold_days 内都没触发上下屏）

    参数：
      prices: pd.Series，index 是 datetime，value 是价格
      events: 入场日期列表
      pt: profit target，正数（如 0.05 = +5%）
      sl: stop loss，负数（如 -0.05 = -5%）
      max_hold_days: 最大持有天数

    返回 DataFrame：
      [event_date, label, exit_date, return_pct, exit_reason]
    """
    rows = []
    for event_date in events:
        try:
            event_date = pd.Timestamp(event_date)
            if event_date not in prices.index:
                # 找最近的交易日
                future_idx = prices.index[prices.index >= event_date]
                if len(future_idx) == 0:
                    continue
                event_date = future_idx[0]
            entry_price = float(prices.loc[event_date])
            if entry_price <= 0:
                continue

            # 看 max_hold_days 内的未来价格
            future_slice = prices.loc[event_date:].iloc[1:max_hold_days + 1]
            if len(future_slice) == 0:
                continue

            label = 0
            exit_date = future_slice.index[-1]
            exit_ret = float(future_slice.iloc[-1] / entry_price - 1)
            exit_reason = "TIME_BARRIER"

            for date, price in future_slice.items():
                ret = float(price) / entry_price - 1
                if ret >= pt:
                    label = 1
                    exit_date = date
                    exit_ret = ret
                    exit_reason = "PROFIT_TARGET"
                    break
                if ret <= sl:
                    label = -1
                    exit_date = date
                    exit_ret = ret
                    exit_reason = "STOP_LOSS"
                    break

            rows.append({
                "event_date": event_date,
                "label": label,
                "exit_date": exit_date,
                "return_pct": round(exit_ret * 100, 3),
                "exit_reason": exit_reason,
                "hold_days": (exit_date - event_date).days,
            })
        except (KeyError, IndexError, ValueError) as e:
            logger.debug("skip event %s: %s", event_date, e)
            continue

    return pd.DataFrame(rows)


# ─────────── Purged K-Fold CV (De Prado 2018 Ch 7) ───────────

class PurgedKFold:
    """Purged K-Fold Cross Validation with Embargo.

    Standard k-fold 在金融时序上有问题：
      - 样本 i 的 label 来自 t_i → t_i + h（h 个 holding period）
      - 如果 train 含 i, test 含 j，且 [t_i, t_i+h] ∩ [t_j, t_j+h] ≠ ∅
        → label 信息从 train 泄漏到 test

    Purged：移除 train 里 label window 与 test window 重叠的样本
    Embargo：test 之后额外 buffer（防 serial correlation 泄漏）

    用法：
      cv = PurgedKFold(n_splits=5, embargo_pct=0.01)
      for train_idx, test_idx in cv.split(X, t1=label_end_times):
          # train_idx, test_idx 是 numpy 数组
          ...
    """

    def __init__(self, n_splits: int = 5, embargo_pct: float = 0.01):
        if n_splits < 2:
            raise ValueError("n_splits must be >= 2")
        self.n_splits = n_splits
        self.embargo_pct = embargo_pct

    def split(self, X: pd.DataFrame | np.ndarray,
              t1: pd.Series | None = None) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        """生成 (train_idx, test_idx) 对。

        参数：
          X: 特征矩阵（仅用 len 来分 fold）
          t1: pd.Series, index 与 X 对齐, value 是每个样本的 label end time
              （即 label 信息覆盖到哪一天）

        如果 t1 为 None：退化为带 embargo 的 standard k-fold。
        """
        n = len(X)
        indices = np.arange(n)
        embargo = int(n * self.embargo_pct)

        # 把 indices 分成 n_splits 段
        test_chunks = np.array_split(indices, self.n_splits)

        for chunk in test_chunks:
            test_start, test_end = chunk[0], chunk[-1]
            test_idx = indices[test_start:test_end + 1]

            # train = 所有非 test 的 + 不被 purge 的
            if t1 is None:
                # 简化版：仅 embargo
                train_left = indices[:max(0, test_start - embargo)]
                train_right = indices[min(n, test_end + 1 + embargo):]
                train_idx = np.concatenate([train_left, train_right])
            else:
                # Purge: 移除 train 里 label_end > test_start 的样本
                # 这些样本的 label 信息会泄漏到 test
                t1 = pd.Series(t1) if not isinstance(t1, pd.Series) else t1
                test_period_start = pd.Timestamp(t1.iloc[test_start])
                test_period_end = pd.Timestamp(t1.iloc[test_end])

                train_idx_list = []
                for i in indices:
                    if test_start <= i <= test_end:
                        continue
                    label_end = pd.Timestamp(t1.iloc[i])
                    # Purge: 样本 label 跨进 test 区间
                    if i < test_start and label_end > test_period_start:
                        continue
                    # Embargo: 样本紧跟 test 之后
                    if i > test_end and i < test_end + 1 + embargo:
                        continue
                    train_idx_list.append(i)
                train_idx = np.array(train_idx_list)

            yield train_idx, test_idx


# ─────────── Sample Uniqueness (De Prado 2018 Ch 4) ───────────

def sample_uniqueness(t0: pd.Series, t1: pd.Series) -> pd.Series:
    """算每个样本的"唯一性"（不与其他样本时间重叠的占比）。

    用于 ML 模型给重叠样本降权（避免 inflate 过拟合）。

    输入：
      t0: 每个样本的 label start time
      t1: 每个样本的 label end time

    返回：
      pd.Series, value ∈ (0, 1]，越接近 1 = 越独特
    """
    if len(t0) != len(t1):
        raise ValueError("t0 and t1 must have same length")

    # 算每个时间点的"并发样本数"
    all_times = pd.date_range(t0.min(), t1.max(), freq="D")
    concurrent = pd.Series(0, index=all_times)
    for i in range(len(t0)):
        period = pd.date_range(t0.iloc[i], t1.iloc[i], freq="D")
        concurrent.loc[period] += 1
    concurrent = concurrent.replace(0, 1)  # 避免 div by 0

    # 每个样本的 uniqueness = mean(1 / concurrent[t0_i:t1_i])
    uniqueness = []
    for i in range(len(t0)):
        period = pd.date_range(t0.iloc[i], t1.iloc[i], freq="D")
        u = (1 / concurrent.loc[period]).mean()
        uniqueness.append(u)
    return pd.Series(uniqueness, index=t0.index)


# ─────────── 测试 / 演示 ───────────

def demo_purged_cv_vs_standard(n_samples: int = 100, n_splits: int = 5) -> dict:
    """对比标准 k-fold vs purged k-fold 的训练集大小差异。

    Purged 会比 standard 减少几个样本（因为 purge + embargo）。
    """
    from sklearn.model_selection import KFold

    X = pd.DataFrame({"feature": np.random.randn(n_samples)})

    # Standard k-fold
    std_train_sizes = []
    for tr, _ in KFold(n_splits=n_splits).split(X):
        std_train_sizes.append(len(tr))

    # Purged k-fold (无 t1 → 仅 embargo)
    pcv_train_sizes = []
    for tr, _ in PurgedKFold(n_splits=n_splits, embargo_pct=0.02).split(X):
        pcv_train_sizes.append(len(tr))

    return {
        "n_samples": n_samples,
        "n_splits": n_splits,
        "standard_kfold_avg_train": np.mean(std_train_sizes),
        "purged_kfold_avg_train": np.mean(pcv_train_sizes),
        "purged_loses": np.mean(std_train_sizes) - np.mean(pcv_train_sizes),
    }
