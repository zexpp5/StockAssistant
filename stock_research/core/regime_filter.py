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


def _breadth_advance_decline(history: dict | None = None,
                              lookback: int = 21,
                              pct_advance_threshold: float = 0.45,
                              ) -> tuple[bool, dict]:
    """真 A/D Line：从 history_data.json universe 算每日涨/跌家数。

    学术依据：
      - Zweig (1986) "Winning on Wall Street" — 涨跌家数广度领先于市值指数
      - Fosback (1976) "Stock Market Logic" — A/D Line 是大盘健康度首要指标
      - Whaley & Cooper (1991) JFR — 实证 NYSE A/D 在大盘转折点领先 5-15 个交易日

    阈值：21 日均"涨家数比 < 45%" 即广度差（保守，因 50% 已意味着多空均衡）。
    缺数据时由调用方 fallback 到 RSP-SPY spread 代理（_breadth_weak）。
    """
    if history is None:
        try:
            from pathlib import Path
            import json as _json
            repo = Path(__file__).resolve().parents[2]
            hist_path = repo / "data" / "latest" / "history_data.json"
            if hist_path.exists():
                d = _json.loads(hist_path.read_text(encoding="utf-8"))
                history = d.get("tickers") or {}
            else:
                return False, {"error": "history_data.json missing"}
        except Exception as e:
            return False, {"error": f"history load failed: {str(e)[:80]}"}

    if not history:
        return False, {"error": "empty history"}

    daily_advances = [0] * lookback
    daily_total = [0] * lookback

    for ticker, data in history.items():
        closes = (data or {}).get("close") or []
        if len(closes) < lookback + 1:
            continue
        recent = closes[-(lookback + 1):]
        for i in range(lookback):
            prev_c = recent[i]
            cur_c = recent[i + 1]
            if prev_c is None or cur_c is None or prev_c <= 0:
                continue
            daily_total[i] += 1
            if cur_c > prev_c:
                daily_advances[i] += 1

    universe_size = max(daily_total) if daily_total else 0
    if universe_size < 20:
        return False, {"error": f"universe too small ({universe_size})"}

    pct_advancing = [
        (a / t) if t > 0 else 0.5
        for a, t in zip(daily_advances, daily_total)
    ]
    avg_pct = sum(pct_advancing) / lookback
    # A/D Line 21 日累计变化 = sum(advancing - declining)
    ad_line_chg = sum((a - (t - a)) for a, t in zip(daily_advances, daily_total))

    breadth_bad = avg_pct < pct_advance_threshold
    return breadth_bad, {
        "avg_pct_advancing_21d": round(avg_pct * 100, 1),
        "ad_line_change_21d": ad_line_chg,
        "universe_size": universe_size,
        "threshold_pct": round(pct_advance_threshold * 100, 1),
        "method": "advance_decline_real",
        "interpretation": ("广度差 - 跌家数偏多" if breadth_bad
                           else "广度健康 - 涨家数充足"),
    }


def _market_new_high_new_low(history: dict | None = None,
                              lookback: int = 252,
                              window: int = 21,
                              net_ratio_threshold: float = -0.10) -> tuple[bool, dict]:
    """NHNL (New High - New Low) — 比 A/D Line 更能识别结构性熊市。

    学术依据：
      - Zweig (1986) "Winning on Wall Street" — 新低家数超过新高家数 = 大盘结构恶化先兆
      - Fosback (1976) "Stock Market Logic" — NHNL 比 A/D 更看结构（趋势内部健康度）
      - Whaley-Cooper (1991) JFR — NHNL 在主要顶部 / 底部领先 10-30 个交易日

    定义：
      - 当日新高 = 当日 close == 过去 lookback (252) 日最大
      - 当日新低 = 当日 close == 过去 lookback (252) 日最小
      - net_ratio = (新高家数 - 新低家数) / universe_size
      - 取近 window (21) 日均值，< net_ratio_threshold (-10%) → 广度恶化

    判读：
      - 与 A/D Line 互补：A/D 看每日涨跌（短期），NHNL 看结构突破（长期）
      - 大盘指数还在涨但 NHNL 净比例为负 → "narrow rally" 头部领涨、底部破位
    """
    if history is None:
        try:
            from pathlib import Path
            import json as _json
            repo = Path(__file__).resolve().parents[2]
            hist_path = repo / "data" / "latest" / "history_data.json"
            if hist_path.exists():
                d = _json.loads(hist_path.read_text(encoding="utf-8"))
                history = d.get("tickers") or {}
            else:
                return False, {"error": "history_data.json missing"}
        except Exception as e:
            return False, {"error": f"history load failed: {str(e)[:80]}"}

    if not history:
        return False, {"error": "empty history"}

    # 每日新高 / 新低家数（最近 window 日）
    daily_new_highs = [0] * window
    daily_new_lows = [0] * window
    daily_universe = [0] * window

    for ticker, data in history.items():
        closes = (data or {}).get("close") or []
        # 需要 lookback + window 长度（每个 window 内日点都要往前看 lookback 日）
        if len(closes) < lookback + window:
            continue
        # 对最近 window 日，分别判断该日 close 是否为过去 lookback 日的 max/min
        for i in range(window):
            idx = len(closes) - window + i  # 该 window 日在 closes 里的位置
            cur = closes[idx]
            if cur is None:
                continue
            past = closes[idx - lookback:idx + 1]  # 包含当日，往前 lookback 日
            past_valid = [c for c in past if c is not None]
            if len(past_valid) < lookback // 2:  # 数据太稀疏跳过
                continue
            daily_universe[i] += 1
            if cur >= max(past_valid):
                daily_new_highs[i] += 1
            elif cur <= min(past_valid):
                daily_new_lows[i] += 1

    universe_size = max(daily_universe) if daily_universe else 0
    if universe_size < 20:
        return False, {"error": f"universe too small ({universe_size})"}

    net_ratios = [
        ((nh - nl) / u) if u > 0 else 0.0
        for nh, nl, u in zip(daily_new_highs, daily_new_lows, daily_universe)
    ]
    avg_net = sum(net_ratios) / window

    # 累计 NHNL 趋势变化
    cum_nh = sum(daily_new_highs)
    cum_nl = sum(daily_new_lows)

    breadth_bad = avg_net < net_ratio_threshold
    return breadth_bad, {
        "avg_net_ratio_21d": round(avg_net * 100, 1),  # 单位 %
        "cum_new_highs": cum_nh,
        "cum_new_lows": cum_nl,
        "universe_size": universe_size,
        "lookback_days": lookback,
        "window_days": window,
        "threshold_pct": round(net_ratio_threshold * 100, 1),
        "interpretation": ("结构恶化 - 新低家数超新高" if breadth_bad
                           else "结构健康 - 新高家数占优"),
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
    # 广度信号：3 个独立维度，OR 触发但合并占 1 个 trigger 槽位
    # - 真 A/D Line (Zweig 1986)：universe 每日涨跌家数
    # - RSP-SPY spread (头部集中)：S&P 内部分布健康度
    # - NHNL (Zweig/Fosback)：252 日新高/新低家数 — 结构性恶化先兆
    # 三者测不同维度，互相补充
    ad_bad, ad_info = _breadth_advance_decline()
    spread_bad, spread_info = _breadth_weak(
        as_of, spread_threshold_pct=breadth_spread_threshold_pct,
    )
    nhnl_bad, nhnl_info = _market_new_high_new_low()
    breadth_bad = ad_bad or spread_bad or nhnl_bad
    breadth_msgs = []
    if ad_bad and not ad_info.get("error"):
        breadth_msgs.append(f"A/D {ad_info.get('avg_pct_advancing_21d','?')}% 涨")
    if spread_bad and not spread_info.get("error"):
        breadth_msgs.append(f"RSP-SPY {spread_info.get('spread_pct','?')}%")
    if nhnl_bad and not nhnl_info.get("error"):
        breadth_msgs.append(f"NHNL {nhnl_info.get('avg_net_ratio_21d','?')}%")
    breadth_trigger_msg = "广度差 (" + " / ".join(breadth_msgs) + ")" if breadth_msgs else ""
    breadth_info = {
        "advance_decline": ad_info,
        "rsp_spy_spread": spread_info,
        "new_high_new_low": nhnl_info,
    }
    vix_value = vix_info.get("vix_close")

    triggers = []
    if not spy_ok:
        triggers.append("SPY < 200MA (Faber)")
    if vix_value is not None and vix_value >= vix_threshold_cautious:
        triggers.append(f"VIX={vix_value} ≥ {vix_threshold_cautious}")
    if yc_inverted:
        triggers.append("10Y-13W 倒挂 (Estrella-Mishkin)")
    if breadth_bad:
        triggers.append(breadth_trigger_msg)

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
