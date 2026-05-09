"""市场 regime 过滤器（多模式）— 解决 200MA 在闪崩里失效的问题。

学术依据：
  - Faber (2007) "A Quantitative Approach to Tactical Asset Allocation"
    SPY < 200MA → 减仓（防御慢崩，对闪崩无效）
  - Whaley (2009) "Understanding the VIX"
    VIX > 30 = 市场恐慌阈值；VIX > 40 = 极端恐慌
    历史回看：2020-02-28 VIX 突破 30，**比 SPY 跌破 200MA 早 3 周**
  - Whaley (1993, 2000) 原始 VIX 论文 — CBOE 标准计算法

为什么 VIX 比 200MA 灵敏：
  - 200MA 是"价格滞后指标"：要等价格跌一段时间才跌破
  - VIX 是"期权隐含波动率"：恐慌情绪即时反映在期权价格里
  - 闪崩时 VIX 通常**先于价格** spike

Modes:
  - "none"        : 满仓
  - "faber_200ma" : Faber 2007，SPY < 200MA → 减仓 50%
  - "panic_vix"   : Whaley 2009，VIX > 30 → 减仓 50%
  - "combined"    : OR 触发（200MA 或 VIX，任一触发减仓）

输出：
  (position_multiplier, regime_label, signals_dict)
"""
from __future__ import annotations
import logging

import pandas as pd

logger = logging.getLogger(__name__)


def _spy_above_200ma(as_of: str | None = None) -> tuple[bool, dict]:
    """SPY 是否在 200MA 之上（Faber 2007 规则）。"""
    try:
        import yfinance as yf
    except ImportError:
        return True, {"error": "yfinance not installed"}

    target = pd.to_datetime(as_of) if as_of else pd.Timestamp.now()
    start = target - pd.Timedelta(days=400)
    end = target + pd.Timedelta(days=2)
    try:
        spy = yf.Ticker("SPY").history(start=start, end=end)
    except Exception as e:
        return True, {"error": str(e)[:80]}
    if len(spy) < 200:
        return True, {"error": "insufficient SPY history"}

    if spy.index.tz:
        spy = spy[spy.index.tz_localize(None) <= target]
    else:
        spy = spy[spy.index <= target]
    if len(spy) < 200:
        return True, {"error": "insufficient after cutoff"}

    close = spy["Close"]
    ma_200 = close.rolling(200).mean()
    spy_now = float(close.iloc[-1])
    ma_now = float(ma_200.iloc[-1])
    above = spy_now > ma_now
    return above, {
        "spy_close": round(spy_now, 2),
        "spy_200ma": round(ma_now, 2),
        "distance_pct": round((spy_now / ma_now - 1) * 100, 2),
    }


def _vix_below_panic(as_of: str | None = None,
                     panic_threshold: float = 30.0) -> tuple[bool, dict]:
    """VIX 是否在恐慌阈值之下（Whaley 2009）。

    VIX < threshold（默认 30）→ True（市场平静）
    VIX > threshold → False（恐慌）
    """
    try:
        import yfinance as yf
    except ImportError:
        return True, {"error": "yfinance not installed"}

    target = pd.to_datetime(as_of) if as_of else pd.Timestamp.now()
    start = target - pd.Timedelta(days=30)
    end = target + pd.Timedelta(days=2)
    try:
        vix = yf.Ticker("^VIX").history(start=start, end=end)
    except Exception as e:
        return True, {"error": str(e)[:80]}
    if len(vix) < 1:
        return True, {"error": "no VIX data"}

    if vix.index.tz:
        vix = vix[vix.index.tz_localize(None) <= target]
    else:
        vix = vix[vix.index <= target]
    if len(vix) < 1:
        return True, {"error": "no VIX after cutoff"}

    vix_now = float(vix["Close"].iloc[-1])
    below = vix_now < panic_threshold
    return below, {
        "vix_close": round(vix_now, 2),
        "panic_threshold": panic_threshold,
        "is_panic": not below,
    }


def get_position_multiplier(as_of: str | None = None,
                            mode: str = "combined",
                            vix_threshold: float = 30.0,
                            risk_off_mult: float = 0.5) -> dict:
    """根据 regime 模式返回仓位倍数。

    返回 {
      "mode": str,
      "position_multiplier": float (0.5 / 1.0),
      "regime": "RISK_ON" / "RISK_OFF",
      "signals": {ma_status, vix_status},
      "trigger": str (说明哪个信号触发了减仓),
    }
    """
    if mode == "none":
        return {
            "mode": mode, "position_multiplier": 1.0, "regime": "RISK_ON",
            "signals": {}, "trigger": "no filter applied",
        }

    spy_ok, ma_info = _spy_above_200ma(as_of)
    vix_ok, vix_info = _vix_below_panic(as_of, panic_threshold=vix_threshold)

    if mode == "faber_200ma":
        risk_on = spy_ok
        trigger = "SPY 在 200MA 之下" if not risk_on else "SPY > 200MA"
    elif mode == "panic_vix":
        risk_on = vix_ok
        trigger = f"VIX > {vix_threshold} 恐慌触发" if not risk_on else f"VIX < {vix_threshold}"
    elif mode == "combined":
        risk_on = spy_ok and vix_ok
        if not risk_on:
            triggers = []
            if not spy_ok:
                triggers.append("SPY < 200MA")
            if not vix_ok:
                triggers.append(f"VIX > {vix_threshold}")
            trigger = " 或 ".join(triggers)
        else:
            trigger = "无信号触发"
    else:
        raise ValueError(f"unknown mode: {mode}")

    return {
        "mode": mode,
        "position_multiplier": 1.0 if risk_on else risk_off_mult,
        "regime": "RISK_ON" if risk_on else "RISK_OFF",
        "signals": {"ma": ma_info, "vix": vix_info},
        "trigger": trigger,
    }
