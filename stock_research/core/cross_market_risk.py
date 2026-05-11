"""跨市场风险：SPY × CSI300 已实现相关性 + USD/CNY 汇率敞口。

为什么需要：
  系统同时持有美股 + A 股两条线，但目前的风险预算 (Markowitz / risk parity) 是
  **分别在各自市场内做的**，跨市场相关性、汇率波动一个都没看到。
  在 2022 全球加息周期，SPY 跌 25%、CSI300 跌 22%，两者相关性 ≈ 0.6 —
  分散效果远低于直觉。

输出（两块）：
  1. correlation_block:
     - SPY × CSI300 实现相关性（rolling 60d / 252d / 全样本）
     - 相关性 trend（recent_60d vs full）→ 趋升/趋降
  2. fx_exposure_block:
     - USD/CNY 当前价 + 252d 波动率 + 1Y 累计变化
     - 组合中 USD/CNY exposure 拆分（输入 plan 的 ticker × weight）
     - 估算 1σ 汇率冲击对组合 P&L 的影响

依赖：
  - yfinance: SPY, CNY=X, ^IXIC, ^HSI
  - akshare:   csi300 (sh000300) — 数据更全
  缺失任一源不抛异常，相关字段标 None。

设计原则：
  - 纯计算，输入 plan dict + 历史窗口；不直接 I/O
  - 失败降级（数据源不可用时返回 None + reason）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────── 数据结构 ───────────────

@dataclass
class CorrelationBlock:
    spy_csi300_corr_60d: float | None
    spy_csi300_corr_252d: float | None
    spy_csi300_corr_full: float | None
    n_obs_full: int
    trend: str                          # 'rising' / 'falling' / 'stable' / 'no_data'
    note: str
    errors: list[str] = field(default_factory=list)


@dataclass
class FxExposureBlock:
    usdcny_close: float | None
    usdcny_vol_252d_pct: float | None     # 年化波动率 %
    usdcny_change_1y_pct: float | None    # 1Y 变化 %
    usd_exposure_pct: float | None        # 组合中 USD 计价资产权重
    cny_exposure_pct: float | None        # 组合中 CNY 计价资产权重
    cash_pct: float | None                # 现金权重（按默认币种）
    fx_shock_1sigma_pct: float | None     # 1σ 汇率冲击对组合的 P&L 影响 %
    advice: str
    errors: list[str] = field(default_factory=list)


# ─────────────── 数据拉取 ───────────────

def _fetch_yf_close(ticker: str, days: int = 400):
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        return None, "yfinance/pandas not installed"
    end = datetime.now()
    start = end - timedelta(days=days)
    try:
        df = yf.Ticker(ticker).history(start=start, end=end)
    except Exception as e:
        return None, f"yf {ticker} error: {str(e)[:60]}"
    if len(df) < 30:
        return None, f"yf {ticker} insufficient history"
    return df["Close"].copy(), None


def _fetch_csi300_close(days: int = 400):
    try:
        import akshare as ak
        import pandas as pd
    except ImportError:
        return None, "akshare/pandas not installed"
    try:
        df = ak.index_zh_a_hist(symbol="000300", period="daily")
        if df is None or len(df) == 0:
            return None, "csi300 empty"
        df = df.tail(days).copy()
        df["日期"] = pd.to_datetime(df["日期"])
        df = df.set_index("日期")
        return df["收盘"].astype(float), None
    except Exception as e:
        return None, f"akshare csi300 error: {str(e)[:60]}"


# ─────────────── 相关性计算 ───────────────

def compute_correlation(window_days: int = 400) -> CorrelationBlock:
    """SPY × CSI300 已实现相关性，3 个时间窗口。"""
    errors: list[str] = []
    spy_close, e1 = _fetch_yf_close("SPY", days=window_days)
    if e1:
        errors.append(e1)
    csi_close, e2 = _fetch_csi300_close(days=window_days)
    if e2:
        errors.append(e2)

    if spy_close is None or csi_close is None:
        return CorrelationBlock(
            spy_csi300_corr_60d=None,
            spy_csi300_corr_252d=None,
            spy_csi300_corr_full=None,
            n_obs_full=0,
            trend="no_data",
            note="数据源缺失，相关性无法计算",
            errors=errors,
        )

    try:
        import pandas as pd
    except ImportError:
        errors.append("pandas not installed")
        return CorrelationBlock(None, None, None, 0, "no_data", "pandas missing", errors)

    spy_ret = spy_close.pct_change().dropna()
    csi_ret = csi_close.pct_change().dropna()
    # 处理时区
    if spy_ret.index.tz is not None:
        spy_ret.index = spy_ret.index.tz_localize(None)
    if hasattr(csi_ret.index, "tz") and csi_ret.index.tz is not None:
        csi_ret.index = csi_ret.index.tz_localize(None)
    # normalize to date
    spy_ret.index = spy_ret.index.normalize()
    csi_ret.index = csi_ret.index.normalize()
    df = pd.DataFrame({"spy": spy_ret, "csi300": csi_ret}).dropna()
    n_full = len(df)
    if n_full < 30:
        return CorrelationBlock(
            None, None, None, n_full, "no_data",
            f"对齐后样本仅 {n_full} 天，不足 30 天",
            errors,
        )

    def _corr(d):
        if len(d) < 5:
            return None
        v = d["spy"].corr(d["csi300"])
        return round(float(v), 3) if v == v else None

    corr_60 = _corr(df.tail(60))
    corr_252 = _corr(df.tail(252))
    corr_full = _corr(df)

    trend = "no_data"
    if corr_60 is not None and corr_full is not None:
        delta = corr_60 - corr_full
        if delta > 0.10:
            trend = "rising"
        elif delta < -0.10:
            trend = "falling"
        else:
            trend = "stable"

    note_lines = []
    if corr_60 is not None and corr_60 > 0.5:
        note_lines.append(f"近 60d 相关性 {corr_60} 偏高，跨市场分散效果有限")
    elif corr_60 is not None and corr_60 < 0.2:
        note_lines.append(f"近 60d 相关性 {corr_60} 较低，跨市场分散有效")
    if trend == "rising":
        note_lines.append("相关性近期 ↑，加息/系统性事件期请收紧")
    note = "；".join(note_lines) if note_lines else f"相关性 {trend}"

    return CorrelationBlock(
        spy_csi300_corr_60d=corr_60,
        spy_csi300_corr_252d=corr_252,
        spy_csi300_corr_full=corr_full,
        n_obs_full=n_full,
        trend=trend,
        note=note,
        errors=errors,
    )


# ─────────────── 汇率敞口 ───────────────

def _classify_currency(ticker: str) -> str:
    """根据 ticker 推断计价货币：USD / CNY / HKD / UNKNOWN。"""
    s = (ticker or "").upper().strip()
    if not s:
        return "UNKNOWN"
    # A 股：6 位数字
    if s.replace(".SS", "").replace(".SZ", "").replace(".BJ", "").isdigit():
        return "CNY"
    if s.endswith(".HK"):
        return "HKD"
    if s.endswith(".SS") or s.endswith(".SZ") or s.endswith(".BJ"):
        return "CNY"
    # 默认美股
    if s.isalpha() and 1 <= len(s) <= 5:
        return "USD"
    return "UNKNOWN"


def compute_fx_exposure(plan: dict | None,
                         base_currency: str = "CNY") -> FxExposureBlock:
    """根据 plan dict 计算 USD/CNY 敞口与汇率冲击。

    plan 格式：与 plan_a_v5*.json 兼容
      {"plan_v5": [{"ticker": str, "v5_weight": float, ...}, ...]}
    """
    errors: list[str] = []

    # 拉 USDCNY 历史
    import math
    cny_close, e = _fetch_yf_close("CNY=X", days=400)
    if e:
        errors.append(e)

    usdcny_close = None
    vol_252 = None
    change_1y = None
    if cny_close is not None and len(cny_close) >= 30:
        try:
            ret = cny_close.pct_change().dropna()
            usdcny_close = float(cny_close.iloc[-1])
            if len(ret) >= 60:
                vol_252 = float(ret.tail(252).std() * math.sqrt(252) * 100)
            if len(cny_close) >= 200:
                change_1y = float((cny_close.iloc[-1] / cny_close.iloc[-252] - 1) * 100) \
                    if len(cny_close) >= 252 else None
        except Exception as ex:
            errors.append(f"fx calc error: {str(ex)[:60]}")

    # 拆分组合敞口
    if not plan or "plan_v5" not in plan:
        return FxExposureBlock(
            usdcny_close=usdcny_close, usdcny_vol_252d_pct=vol_252,
            usdcny_change_1y_pct=change_1y, usd_exposure_pct=None,
            cny_exposure_pct=None, cash_pct=None,
            fx_shock_1sigma_pct=None,
            advice="无 plan 输入，无法拆分敞口",
            errors=errors,
        )

    usd_w = 0.0
    cny_w = 0.0
    other_w = 0.0
    for entry in plan["plan_v5"]:
        w = float(entry.get("v5_weight", 0))
        if w <= 0:
            continue
        cur = _classify_currency(entry.get("ticker", ""))
        if cur == "USD":
            usd_w += w
        elif cur == "CNY":
            cny_w += w
        else:
            other_w += w

    total_invested = usd_w + cny_w + other_w
    cash_w = max(0.0, 1.0 - total_invested)

    # 1σ 汇率冲击对组合 P&L 的影响（以 base_currency 视角）
    # 若 base=CNY：USD 资产升值 1σ → 组合 P&L = usd_w × (1σ%)
    fx_shock = None
    if vol_252 is not None:
        # 日波动转 1σ 月度波动作为现实冲击估计（更直观）
        monthly_sigma = vol_252 / math.sqrt(12)
        if base_currency == "CNY":
            fx_shock = round(usd_w * monthly_sigma, 2)
        else:
            fx_shock = round(cny_w * monthly_sigma, 2)

    # 建议
    advice_parts = []
    if usd_w > 0.30 and cny_w > 0.30:
        advice_parts.append(
            f"USD ({usd_w*100:.0f}%) + CNY ({cny_w*100:.0f}%) 双重敞口 — "
            f"考虑用 CNY 计价 ETF 或 USDCNH 远期对冲"
        )
    if vol_252 is not None and vol_252 > 6.0:
        advice_parts.append(f"USDCNY 年化波动 {vol_252:.1f}% 偏高，建议关注汇率")
    if not advice_parts:
        advice_parts.append("敞口集中度可控")

    return FxExposureBlock(
        usdcny_close=round(usdcny_close, 4) if usdcny_close else None,
        usdcny_vol_252d_pct=round(vol_252, 2) if vol_252 is not None else None,
        usdcny_change_1y_pct=round(change_1y, 2) if change_1y is not None else None,
        usd_exposure_pct=round(usd_w * 100, 2),
        cny_exposure_pct=round(cny_w * 100, 2),
        cash_pct=round(cash_w * 100, 2),
        fx_shock_1sigma_pct=fx_shock,
        advice="；".join(advice_parts),
        errors=errors,
    )


# ─────────────── 主入口 ───────────────

def compute_cross_market_risk(plan: dict | None = None,
                              base_currency: str = "CNY") -> dict:
    """两块拼成完整报告。"""
    corr = compute_correlation()
    fx = compute_fx_exposure(plan, base_currency=base_currency)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_currency": base_currency,
        "correlation": {
            "spy_csi300_60d": corr.spy_csi300_corr_60d,
            "spy_csi300_252d": corr.spy_csi300_corr_252d,
            "spy_csi300_full": corr.spy_csi300_corr_full,
            "n_obs_full": corr.n_obs_full,
            "trend": corr.trend,
            "note": corr.note,
            "errors": corr.errors,
        },
        "fx_exposure": {
            "usdcny_close": fx.usdcny_close,
            "usdcny_vol_252d_pct": fx.usdcny_vol_252d_pct,
            "usdcny_change_1y_pct": fx.usdcny_change_1y_pct,
            "usd_exposure_pct": fx.usd_exposure_pct,
            "cny_exposure_pct": fx.cny_exposure_pct,
            "cash_pct": fx.cash_pct,
            "fx_shock_1sigma_monthly_pct": fx.fx_shock_1sigma_pct,
            "advice": fx.advice,
            "errors": fx.errors,
        },
    }


def format_report(payload: dict) -> str:
    """渲染跨市场风险报告。"""
    corr = payload["correlation"]
    fx = payload["fx_exposure"]
    lines = [
        "=" * 72,
        f"  跨市场风险监控 — {payload['generated_at']}",
        "=" * 72,
        "",
        "  【SPY × CSI300 已实现相关性】",
        f"    60d   = {corr['spy_csi300_60d']}",
        f"    252d  = {corr['spy_csi300_252d']}",
        f"    full  = {corr['spy_csi300_full']}  (n={corr['n_obs_full']})",
        f"    trend = {corr['trend']}",
        f"    备注  : {corr['note']}",
    ]
    if corr["errors"]:
        lines.append(f"    ⚠️ 错误：{'; '.join(corr['errors'])}")

    lines.extend([
        "",
        f"  【USDCNY 汇率 + 组合敞口（基准 = {payload['base_currency']}）】",
        f"    USDCNY 收盘 = {fx['usdcny_close']}",
        f"    年化波动    = {fx['usdcny_vol_252d_pct']}%",
        f"    1Y 变化     = {fx['usdcny_change_1y_pct']}%",
        f"    USD 敞口    = {fx['usd_exposure_pct']}%",
        f"    CNY 敞口    = {fx['cny_exposure_pct']}%",
        f"    现金        = {fx['cash_pct']}%",
        f"    1σ 月度汇率冲击对 P&L = {fx['fx_shock_1sigma_monthly_pct']}%",
        f"    建议：{fx['advice']}",
    ])
    if fx["errors"]:
        lines.append(f"    ⚠️ 错误：{'; '.join(fx['errors'])}")
    lines.append("=" * 72)
    return "\n".join(lines)
