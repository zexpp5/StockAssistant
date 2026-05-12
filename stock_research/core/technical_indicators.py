"""技术指标库（D 系列 · 2026-05-12）。

⚠️ **不自动接入主路径** — 按二审建议"消融测试通过才上"。本模块只提供
函数实现 + 阈值判断，由 regime_filter / walk_forward 等上游显式调用。

实现清单：
  D-1 ADX / +DI / -DI (Wilder 1978)        — 趋势强度
  D-2 CHOP Choppiness Index                  — 震荡/趋势识别
  D-3 Chandelier Exit                        — ATR-based 移动止损
  D-4 TSMOM (Moskowitz-Ooi-Pedersen 2012)    — 时间序列动量
  D-5 BBWIDTH (Bollinger Band Width)         — 波动率收缩/扩张
  D-6 AVWAP                                  — Hold（需要 volume，history_data.json 暂无）

学术依据：
  - Wilder (1978) "New Concepts in Technical Trading Systems"
  - Moskowitz-Ooi-Pedersen (2012) JFE "Time Series Momentum"
  - Bollinger (1992) "Bollinger on Bollinger Bands"

数据要求：close（最低），high/low（多数指标需要），volume（AVWAP）
"""
from __future__ import annotations
import math
from typing import Sequence


def _ema(values: Sequence[float], period: int) -> list[float]:
    """指数移动平均。返回长度等于 values 的列表（前 period-1 个用 SMA 种子）。"""
    if not values or len(values) < period:
        return []
    alpha = 2.0 / (period + 1)
    out: list[float] = []
    sma_seed = sum(values[:period]) / period
    out.append(sma_seed)
    for v in values[period:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def _sma(values: Sequence[float], period: int) -> list[float]:
    """简单移动平均（rolling）。返回长度 = len(values) - period + 1。"""
    if not values or len(values) < period:
        return []
    out: list[float] = []
    window = sum(values[:period])
    out.append(window / period)
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out.append(window / period)
    return out


def _wilder_smooth(values: Sequence[float], period: int) -> list[float]:
    """Wilder smoothing：种子 = 前 period 个均值，之后用 (prev*(n-1) + new) / n。"""
    if not values or len(values) < period:
        return []
    seed = sum(values[:period]) / period
    out = [seed]
    for v in values[period:]:
        out.append((out[-1] * (period - 1) + v) / period)
    return out


# ──────────────── D-1: ADX (Wilder 1978) ────────────────

def adx(highs: Sequence[float], lows: Sequence[float],
        closes: Sequence[float], period: int = 14) -> dict:
    """ADX (Average Directional Index) + +DI / -DI。

    步骤 (Wilder 1978)：
      1. True Range = max(H-L, |H-prev_C|, |L-prev_C|)
      2. +DM = if H_up > L_down and H_up > 0 else 0；-DM 反之
      3. Wilder smooth TR / +DM / -DM (period=14)
      4. +DI = +DM/TR × 100；-DI = -DM/TR × 100
      5. DX = |+DI - -DI| / (+DI + -DI) × 100
      6. ADX = Wilder smooth of DX

    判读：
      - ADX > 25：强趋势（方向看 +DI vs -DI）
      - ADX < 20：震荡 / 无明显趋势 → 建议关趋势策略
      - +DI > -DI：上升趋势；反之下跌

    返回 {"adx": float, "plus_di": float, "minus_di": float, "regime": str}
    """
    n = min(len(highs), len(lows), len(closes))
    if n < period * 2 + 1:
        return {"adx": None, "plus_di": None, "minus_di": None,
                "regime": None, "error": "insufficient data"}

    trs, plus_dms, minus_dms = [], [], []
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        ph, pl = highs[i - 1], lows[i - 1]
        if any(x is None for x in (h, l, pc, ph, pl)):
            continue
        tr = max(h - l, abs(h - pc), abs(l - pc))
        h_up = h - ph
        l_down = pl - l
        plus_dm = h_up if (h_up > l_down and h_up > 0) else 0.0
        minus_dm = l_down if (l_down > h_up and l_down > 0) else 0.0
        trs.append(tr)
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)

    if len(trs) < period:
        return {"adx": None, "plus_di": None, "minus_di": None,
                "regime": None, "error": "insufficient TR data"}

    tr_smooth = _wilder_smooth(trs, period)
    p_smooth = _wilder_smooth(plus_dms, period)
    m_smooth = _wilder_smooth(minus_dms, period)

    if not tr_smooth:
        return {"adx": None, "plus_di": None, "minus_di": None,
                "regime": None, "error": "smoothing failed"}

    plus_di_series = [(p / t * 100) if t > 0 else 0.0
                      for p, t in zip(p_smooth, tr_smooth)]
    minus_di_series = [(m / t * 100) if t > 0 else 0.0
                       for m, t in zip(m_smooth, tr_smooth)]
    dx_series = [
        (abs(p - m) / (p + m) * 100) if (p + m) > 0 else 0.0
        for p, m in zip(plus_di_series, minus_di_series)
    ]
    if len(dx_series) < period:
        return {"adx": None, "plus_di": round(plus_di_series[-1], 2),
                "minus_di": round(minus_di_series[-1], 2),
                "regime": None,
                "error": f"DX series too short ({len(dx_series)} < {period})"}

    adx_series = _wilder_smooth(dx_series, period)
    if not adx_series:
        return {"adx": None, "plus_di": round(plus_di_series[-1], 2),
                "minus_di": round(minus_di_series[-1], 2),
                "regime": None, "error": "ADX smoothing failed"}

    adx_val = adx_series[-1]
    p_di = plus_di_series[-1]
    m_di = minus_di_series[-1]

    # regime 判定
    if adx_val > 25:
        regime = "trend_up" if p_di > m_di else "trend_down"
    elif adx_val < 20:
        regime = "sideways"
    else:
        regime = "weak_trend"

    return {
        "adx": round(adx_val, 2),
        "plus_di": round(p_di, 2),
        "minus_di": round(m_di, 2),
        "regime": regime,
        "trend_strength": "strong" if adx_val > 25 else ("weak" if adx_val < 20 else "moderate"),
    }


# ──────────────── D-2: CHOP (Choppiness Index) ────────────────

def chop(highs: Sequence[float], lows: Sequence[float],
         closes: Sequence[float], period: int = 14) -> dict:
    """Choppiness Index — 量化"市场是震荡还是趋势"。

    公式：CHOP = 100 × log10(sum(ATR_n) / (max(H_n) - min(L_n))) / log10(n)
    阈值：
      > 61.8  ↔  震荡市（关趋势策略）
      < 38.2  ↔  趋势市（开趋势策略）
    """
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return {"chop": None, "regime": None, "error": "insufficient data"}

    trs = []
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        if any(x is None for x in (h, l, pc)):
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return {"chop": None, "regime": None, "error": "insufficient TR"}

    sum_tr = sum(trs[-period:])
    period_highs = [h for h in highs[-period:] if h is not None]
    period_lows = [l for l in lows[-period:] if l is not None]
    if not period_highs or not period_lows:
        return {"chop": None, "regime": None, "error": "high/low missing"}

    h_max = max(period_highs)
    l_min = min(period_lows)
    if h_max - l_min <= 0 or sum_tr <= 0:
        return {"chop": None, "regime": None, "error": "zero range"}

    chop_val = 100 * math.log10(sum_tr / (h_max - l_min)) / math.log10(period)

    if chop_val > 61.8:
        regime = "sideways"
    elif chop_val < 38.2:
        regime = "trending"
    else:
        regime = "neutral"

    return {"chop": round(chop_val, 2), "regime": regime}


# ──────────────── D-3: Chandelier Exit ────────────────

def chandelier_exit(highs: Sequence[float], lows: Sequence[float],
                    closes: Sequence[float],
                    period: int = 22,
                    multiplier: float = 3.0) -> dict:
    """Chandelier Exit — ATR 移动止损（Chuck LeBeau 提出）。

    Long Exit  = Highest_High_period - multiplier × ATR_period
    Short Exit = Lowest_Low_period + multiplier × ATR_period

    比固定 -X% 止损更聪明：高波动期止损位自动放宽，低波动期收紧。
    """
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return {"long_exit": None, "short_exit": None, "error": "insufficient data"}

    # ATR (Wilder)
    trs = []
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        if any(x is None for x in (h, l, pc)):
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return {"long_exit": None, "short_exit": None, "error": "insufficient TR"}
    atr_series = _wilder_smooth(trs, period)
    if not atr_series:
        return {"long_exit": None, "short_exit": None, "error": "ATR failed"}
    atr_val = atr_series[-1]

    recent_highs = [h for h in highs[-period:] if h is not None]
    recent_lows = [l for l in lows[-period:] if l is not None]
    if not recent_highs or not recent_lows:
        return {"long_exit": None, "short_exit": None, "error": "high/low missing"}
    hh = max(recent_highs)
    ll = min(recent_lows)

    long_exit = hh - multiplier * atr_val
    short_exit = ll + multiplier * atr_val
    current = closes[-1] if closes[-1] is not None else None

    breach = None
    if current is not None:
        if current < long_exit:
            breach = "long_stopped"  # 多头止损触发
        elif current > short_exit:
            breach = "short_stopped"

    return {
        "long_exit": round(long_exit, 4),
        "short_exit": round(short_exit, 4),
        "atr": round(atr_val, 4),
        "highest_high": round(hh, 4),
        "lowest_low": round(ll, 4),
        "current": round(current, 4) if current is not None else None,
        "breach": breach,
    }


# ──────────────── D-4: TSMOM (Moskowitz 2012) ────────────────

def tsmom(closes: Sequence[float], lookback_days: int = 252) -> dict:
    """Time-Series Momentum — Moskowitz-Ooi-Pedersen (2012) JFE。

    论文实证：过去 12 个月（约 252 交易日）累计正收益 → 未来 1 个月持续正收益
    （在 58 个不同资产类别上跨 25 年都显著）。

    简化版：sign(过去 N 天累计收益) = ±1
    返回 {"signal": +1/-1/0, "return_pct": ..., "lookback_days": ...}
    """
    if not closes or len(closes) < lookback_days + 1:
        return {"signal": 0, "error": f"insufficient data (need {lookback_days + 1})"}

    start = closes[-(lookback_days + 1)]
    end = closes[-1]
    if start is None or end is None or start <= 0:
        return {"signal": 0, "error": "invalid endpoints"}

    ret = (end / start - 1) * 100
    signal = 1 if ret > 0 else (-1 if ret < 0 else 0)
    return {
        "signal": signal,
        "return_pct": round(ret, 2),
        "lookback_days": lookback_days,
        "start_close": round(start, 4),
        "end_close": round(end, 4),
        "source": "Moskowitz-Ooi-Pedersen 2012 JFE",
    }


# ──────────────── D-5: BBWIDTH (Bollinger Band Width) ────────────────

def bbwidth(closes: Sequence[float], period: int = 20,
            num_std: float = 2.0) -> dict:
    """Bollinger Band Width — 波动率收缩 / 扩张识别。

    width = (upper - lower) / middle × 100
    middle = SMA(close, 20); upper = middle + 2σ; lower = middle - 2σ

    判读：
      - width 显著收缩（< 历史 20% 分位）→ "Squeeze"，常预示突破
      - width 显著扩张 → 波动率上升，止损位应放宽

    返回 {"bbwidth": ..., "percent_b": ..., "squeeze": bool, ...}
    """
    if not closes or len(closes) < period:
        return {"bbwidth": None, "percent_b": None, "error": "insufficient data"}

    recent = [c for c in closes[-period:] if c is not None]
    if len(recent) < period:
        return {"bbwidth": None, "percent_b": None, "error": "too many nulls"}

    mean = sum(recent) / period
    var = sum((c - mean) ** 2 for c in recent) / period
    sd = var ** 0.5
    upper = mean + num_std * sd
    lower = mean - num_std * sd
    if mean <= 0:
        return {"bbwidth": None, "percent_b": None, "error": "non-positive mean"}

    width = (upper - lower) / mean * 100
    current = closes[-1]
    pct_b = ((current - lower) / (upper - lower)) if upper > lower else None

    # squeeze 判定：近 N 期 width 历史分位
    squeeze = False
    if len(closes) >= period * 4:
        width_history: list[float] = []
        for end_idx in range(period, len(closes) + 1):
            window = [c for c in closes[end_idx - period:end_idx] if c is not None]
            if len(window) < period:
                continue
            m = sum(window) / period
            v = sum((c - m) ** 2 for c in window) / period
            s = v ** 0.5
            if m > 0:
                width_history.append((2 * num_std * s) / m * 100)
        if width_history:
            sorted_widths = sorted(width_history)
            p20 = sorted_widths[int(len(sorted_widths) * 0.2)]
            squeeze = width < p20

    return {
        "bbwidth": round(width, 2),
        "percent_b": round(pct_b, 3) if pct_b is not None else None,
        "middle": round(mean, 4),
        "upper": round(upper, 4),
        "lower": round(lower, 4),
        "current": round(current, 4) if current is not None else None,
        "squeeze": squeeze,
    }


# ──────────────── D-6: AVWAP（Hold — 需要 volume）────────────────
#
# Anchored VWAP from anchor_date:
#   VWAP = cumsum(price × volume) / cumsum(volume)
# 当前 history_data.json 仅含 close/high/low（C-4 加），无 volume。
# 接入步骤：
#   1. _fetch_history_for_dashboard.py 增加 "volume": _fmt(h["Volume"])
#   2. 这里实现 anchored_vwap(closes, volumes, anchor_idx)
# 暂 hold 等数据层就绪。


def anchored_vwap(closes: Sequence[float], volumes: Sequence[float] | None,
                  anchor_idx: int = 0) -> dict:
    """Anchored VWAP — 从 anchor_idx 起算的成交量加权均价。

    用法（事件 anchor）：
      anchor_idx = 财报日 / IPO 日 / 政策事件日的索引
      → AVWAP = 该事件之后所有日的 (price × volume) / volume 累计

    数据要求：volume 不能为 None。当前 history_data.json 暂无 volume，
    返回 error 提示需先升级数据层。
    """
    if volumes is None:
        return {"avwap": None,
                "error": "needs volume data (history_data.json 暂无；待数据层升级)"}
    n = min(len(closes), len(volumes))
    if anchor_idx >= n or anchor_idx < 0:
        return {"avwap": None, "error": f"anchor_idx {anchor_idx} out of range"}
    pv_sum = 0.0
    v_sum = 0.0
    for i in range(anchor_idx, n):
        c, v = closes[i], volumes[i]
        if c is None or v is None or v <= 0:
            continue
        pv_sum += c * v
        v_sum += v
    if v_sum <= 0:
        return {"avwap": None, "error": "no valid volume"}
    avwap = pv_sum / v_sum
    current = closes[-1]
    return {
        "avwap": round(avwap, 4),
        "current": round(current, 4) if current is not None else None,
        "deviation_pct": round((current - avwap) / avwap * 100, 2)
                          if (current is not None and avwap > 0) else None,
        "anchor_idx": anchor_idx,
        "days_since_anchor": n - 1 - anchor_idx,
    }
