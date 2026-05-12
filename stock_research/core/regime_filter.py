"""市场 regime 过滤器（多模式）— 解决 200MA 在闪崩里失效的问题。

学术依据：
  - Faber (2007) "A Quantitative Approach to Tactical Asset Allocation"
    SPY < 200MA → 减仓（防御慢崩，对闪崩无效）
  - Whaley (2009) "Understanding the VIX"
    VIX > 30 = 市场恐慌阈值；VIX > 40 = 极端恐慌
    历史回看：2020-02-28 VIX 突破 30，**比 SPY 跌破 200MA 早 3 周**
  - Whaley (1993, 2000) 原始 VIX 论文 — CBOE 标准计算法
  - Estrella-Mishkin (1998) "Predicting U.S. Recessions: Financial Variables..."
    10Y-2Y 利差倒挂 → 未来 12-18 个月经济衰退概率 ↑
  - Wright (2006) Fed Working Paper：yield curve 是 NBER 衰退最稳的领先指标

Modes:
  - "none"        : 满仓
  - "faber_200ma" : Faber 2007，SPY < 200MA → 减仓 50%
  - "panic_vix"   : Whaley 2009，VIX > 30 → 减仓 50%
  - "combined"    : OR 触发（200MA 或 VIX，任一触发减仓）

输出：
  (position_multiplier, regime_label, signals_dict)

────────────────────────────────────────────────────────
v2 升级（get_dynamic_gross_exposure）：连续档位 gross exposure
────────────────────────────────────────────────────────
  原 get_position_multiplier 是 binary（1.0 / 0.5），过于粗糙：
    - VIX 22 和 VIX 60 给同一个 0.5 倍数，吃满下行
    - 不能利用 yield curve 这种"领先 6-12 月"的慢信号
  新 get_dynamic_gross_exposure 用 3 信号合成 5 档位 gross：
    1.00 (RISK_ON)     全无信号
    0.85 (CAUTIOUS_1)  1 个慢信号告警（如 yield curve 倒挂）
    0.65 (CAUTIOUS_2)  2 个信号 或 VIX 20-30
    0.40 (RISK_OFF)    3 个信号 或 VIX 30-40
    0.20 (PANIC)       VIX ≥ 40（Whaley 极端恐慌阈值）

  对应 2026-05-10 review 提的"动态 gross exposure 决定器"。
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


# ═══════════════════════════════════════════════════════════════════
#  动态 gross exposure（v2）— 连续档位替代 binary
# ═══════════════════════════════════════════════════════════════════

def _yield_curve_inverted(as_of: str | None = None) -> tuple[bool, dict]:
    """10Y-2Y 利差是否倒挂（Estrella-Mishkin 1998）。

    用 FRED-style yfinance proxy：
      ^TNX = 10Y Treasury yield (报价 = 实际 yield × 10，需 / 10)
      ^IRX = 13W T-Bill（短端代理）
      ^FVX = 5Y Treasury yield
    准确做 10Y-2Y 需 FRED API；yfinance 没有 2Y 直接行情。
    退而求其次：用 10Y - 13W（更陡的曲线测度），同样能反映"短端高于长端"的衰退信号。
    返回 inverted=True 表示倒挂 / 衰退预警。
    """
    try:
        import yfinance as yf
    except ImportError:
        return False, {"error": "yfinance not installed"}

    target = pd.to_datetime(as_of) if as_of else pd.Timestamp.now()
    start = target - pd.Timedelta(days=30)
    end = target + pd.Timedelta(days=2)
    try:
        tnx = yf.Ticker("^TNX").history(start=start, end=end)
        irx = yf.Ticker("^IRX").history(start=start, end=end)
    except Exception as e:
        return False, {"error": str(e)[:80]}
    if len(tnx) < 1 or len(irx) < 1:
        return False, {"error": "no yield data"}

    def _last_close(df):
        if df.index.tz:
            df = df[df.index.tz_localize(None) <= target]
        else:
            df = df[df.index <= target]
        if len(df) < 1:
            return None
        return float(df["Close"].iloc[-1])

    tnx_yield = _last_close(tnx)
    irx_yield = _last_close(irx)
    if tnx_yield is None or irx_yield is None:
        return False, {"error": "no cutoff data"}

    # yfinance 报价已是 yield 百分比（^TNX 报 45 = 4.5%）
    spread_pct = (tnx_yield - irx_yield) / 10.0
    inverted = spread_pct < 0
    return inverted, {
        "tnx_10y": round(tnx_yield / 10.0, 3),
        "irx_13w": round(irx_yield / 10.0, 3),
        "spread_pct": round(spread_pct, 3),
        "is_inverted": inverted,
    }


def _breadth_weak(as_of: str | None = None,
                  spread_threshold_pct: float = -3.0) -> tuple[bool, dict]:
    """市场广度 proxy：RSP（等权 S&P 500）vs SPY（市值加权）近 21 个交易日相对收益。

    学术依据：Zweig (1986)、Fosback (1976) 指出 advance-decline 广度领先于市值指数。
    用 RSP-SPY spread 作为简化代理：spread 显著为负 → 头部少数大盘股扛指数，
    其余成分股已转弱 → 广度恶化，下行风险升高。

    阈值 -3% 是经验值（2022/01 / 2024/07 头部分化时期均触发）。
    """
    try:
        import yfinance as yf
    except ImportError:
        return False, {"error": "yfinance not installed"}
    target = pd.to_datetime(as_of) if as_of else pd.Timestamp.now()
    start = target - pd.Timedelta(days=60)
    end = target + pd.Timedelta(days=2)
    try:
        rsp = yf.Ticker("RSP").history(start=start, end=end)
        spy = yf.Ticker("SPY").history(start=start, end=end)
    except Exception as e:
        return False, {"error": str(e)[:80]}
    if len(rsp) < 21 or len(spy) < 21:
        return False, {"error": "insufficient history (need 21 trading days)"}
    rsp_close = rsp["Close"].iloc[-21:]
    spy_close = spy["Close"].iloc[-21:]
    try:
        rsp_ret = float(rsp_close.iloc[-1] / rsp_close.iloc[0] - 1) * 100
        spy_ret = float(spy_close.iloc[-1] / spy_close.iloc[0] - 1) * 100
    except Exception as e:
        return False, {"error": f"compute_failed: {e}"}
    spread = rsp_ret - spy_ret
    return spread < spread_threshold_pct, {
        "rsp_1m_pct": round(rsp_ret, 2),
        "spy_1m_pct": round(spy_ret, 2),
        "spread_pct": round(spread, 2),
        "threshold_pct": spread_threshold_pct,
        "interpretation": ("广度差 - 头部集中" if spread < spread_threshold_pct
                           else "广度健康 - 涨势分布均匀"),
    }


def get_dynamic_gross_exposure(as_of: str | None = None,
                               *,
                               vix_threshold_cautious: float = 20.0,
                               vix_threshold_risk_off: float = 30.0,
                               vix_threshold_panic: float = 40.0,
                               breadth_spread_threshold_pct: float = -3.0
                               ) -> dict:
    """根据 3 信号合成 5 档位 gross exposure 上限。

    信号：
      1. SPY < 200MA  (Faber 2007 慢信号)
      2. VIX 分档       (Whaley 2009 快信号)
      3. 10Y - 13W < 0  (Estrella-Mishkin 1998 慢信号，领先 6-12 月)

    档位（优先级：VIX 极端 > 信号计数）：
      VIX ≥ 40                       → 0.20 PANIC
      VIX ≥ 30 或 3 信号触发          → 0.40 RISK_OFF
      VIX ≥ 20 或 2 信号触发          → 0.65 CAUTIOUS_2
      1 信号触发                      → 0.85 CAUTIOUS_1
      全无                            → 1.00 RISK_ON

    返回 {
      "gross_exposure_cap": float (0.20 / 0.40 / 0.65 / 0.85 / 1.00),
      "regime": str (PANIC / RISK_OFF / CAUTIOUS_2 / CAUTIOUS_1 / RISK_ON),
      "signals_triggered": int (0-3),
      "signals": {ma, vix, yield_curve},  # 各信号细节
      "triggers": [str, ...],             # 触发的信号名
      "advice": str,                       # 给上层的操作建议
    }
    """
    spy_ok, ma_info = _spy_above_200ma(as_of)
    _, vix_info = _vix_below_panic(as_of, panic_threshold=vix_threshold_panic)
    yc_inverted, yc_info = _yield_curve_inverted(as_of)
    breadth_bad, breadth_info = _breadth_weak(
        as_of, spread_threshold_pct=breadth_spread_threshold_pct,
    )
    vix_value = vix_info.get("vix_close")

    triggers = []
    if not spy_ok:
        triggers.append("SPY < 200MA (Faber)")
    if vix_value is not None and vix_value >= vix_threshold_cautious:
        triggers.append(f"VIX={vix_value} ≥ {vix_threshold_cautious}")
    if yc_inverted:
        triggers.append("10Y-13W 倒挂 (Estrella-Mishkin)")
    if breadth_bad:
        triggers.append(f"广度差 (RSP-SPY spread {breadth_info.get('spread_pct','?')}%)")

    # 档位判定（VIX 极端绕过信号计数；信号数门槛对应 4 信号体系微调）
    if vix_value is not None and vix_value >= vix_threshold_panic:
        gross, regime = 0.20, "PANIC"
        advice = "VIX 极端恐慌 — 仓位强制降至 20%，新仓位暂停"
    elif vix_value is not None and vix_value >= vix_threshold_risk_off:
        gross, regime = 0.40, "RISK_OFF"
        advice = "VIX 恐慌阈值上 — 仓位降至 40%，新仓位仅限防御板块"
    elif len(triggers) >= 3:
        gross, regime = 0.40, "RISK_OFF"
        advice = f"{len(triggers)} 信号同时告警 — 仓位降至 40%（结构性熊市风险）"
    elif vix_value is not None and vix_value >= vix_threshold_cautious:
        gross, regime = 0.65, "CAUTIOUS_2"
        advice = "VIX 抬升 — 仓位降至 65%，控制新仓位 beta"
    elif len(triggers) >= 2:
        gross, regime = 0.65, "CAUTIOUS_2"
        advice = "2 信号告警 — 仓位降至 65%，新仓位防御优先"
    elif len(triggers) == 1:
        gross, regime = 0.85, "CAUTIOUS_1"
        advice = "1 信号告警 — 仓位降至 85%，留 15% 缓冲"
    else:
        gross, regime = 1.00, "RISK_ON"
        advice = "全无信号 — 满仓"

    return {
        "gross_exposure_cap": gross,
        "regime": regime,
        "signals_triggered": len(triggers),
        "signals": {"ma": ma_info, "vix": vix_info, "yield_curve": yc_info,
                    "breadth": breadth_info},
        "triggers": triggers,
        "advice": advice,
        "thresholds": {
            "vix_cautious": vix_threshold_cautious,
            "vix_risk_off": vix_threshold_risk_off,
            "vix_panic": vix_threshold_panic,
            "breadth_spread": breadth_spread_threshold_pct,
        },
    }


def format_gross_exposure_report(result: dict) -> str:
    """渲染 get_dynamic_gross_exposure 结果为可读报告。"""
    regime_icons = {
        "RISK_ON": "🟢", "CAUTIOUS_1": "🟡", "CAUTIOUS_2": "🟠",
        "RISK_OFF": "🔴", "PANIC": "⛔",
    }
    icon = regime_icons.get(result["regime"], "?")
    lines = [
        "=" * 72,
        f"  动态 Gross Exposure — {icon} {result['regime']}",
        "=" * 72,
        f"  Gross exposure 上限：{result['gross_exposure_cap']:.0%}",
        f"  触发信号数：{result['signals_triggered']}/3",
        f"  操作建议：{result['advice']}",
        "",
        "  信号细节：",
    ]
    ma = result["signals"].get("ma", {})
    if "spy_close" in ma:
        lines.append(f"    SPY = {ma['spy_close']}  200MA = {ma['spy_200ma']}  "
                     f"距离 = {ma['distance_pct']:+.2f}%")
    vix = result["signals"].get("vix", {})
    if "vix_close" in vix:
        lines.append(f"    VIX = {vix['vix_close']}  阈值 = {vix['panic_threshold']}  "
                     f"恐慌 = {vix['is_panic']}")
    yc = result["signals"].get("yield_curve", {})
    if "spread_pct" in yc:
        lines.append(f"    10Y = {yc['tnx_10y']}%  13W = {yc['irx_13w']}%  "
                     f"利差 = {yc['spread_pct']:+.3f}%  "
                     f"倒挂 = {yc['is_inverted']}")
    if result["triggers"]:
        lines.append("")
        lines.append(f"  触发：{' | '.join(result['triggers'])}")
    lines.append("=" * 72)
    return "\n".join(lines)
