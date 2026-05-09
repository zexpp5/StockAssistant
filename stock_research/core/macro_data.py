"""宏观经济数据接入（FRED 优先 + yfinance fallback）。

为什么需要：
  v6 选股完全脱离宏观环境 — 加息周期 / 衰退期 vs 牛市 / 流动性宽松，
  适合的因子完全不同。"加息环境感知"是给 v6 加上"看大势"的能力。

数据源优先级（自动 fallback）：
  1. FRED（最权威）— 需 FRED_API_KEY 环境变量
     注册：https://fred.stlouisfed.org/docs/api/api_key.html （免费 5 分钟）
  2. yfinance 等价符号 — 无 key 也能跑，但精度略低

核心指标（学术经典）：
  - Fed Funds Rate（FEDFUNDS / 备用 ^IRX 13周国债）
  - CPI YoY 通胀（CPIAUCSL / 备用 yfinance 隐含通胀 ETF）
  - 失业率（UNRATE / 仅 FRED 有，无备用）
  - 10 年国债收益率（DGS10 / 备用 ^TNX）
  - 收益率曲线（DGS10 - DGS2 / 备用 ^TNX - 短期）— 倒挂 = 衰退预警

用法：
  from stock_research.core.macro_data import macro_regime
  r = macro_regime()
  # → {fed_rate, cpi_yoy, unemp, ten_year_yield, yield_curve, regime, alerts}

边界：
  - 仅做"环境感知"，不做"宏观预测"
  - alerts 是教科书规则，不是机器学习模型
"""
from __future__ import annotations
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _has_fred_key() -> bool:
    return bool(os.environ.get("FRED_API_KEY"))


def _fred_latest(series_id: str, start: str = "2020-01-01") -> float | None:
    """FRED 拉某序列的最新值。无 key / 失败返回 None。"""
    if not _has_fred_key():
        return None
    try:
        from openbb import obb
        r = obb.economy.fred_series(series_id, start_date=start)
        df = r.to_df()
        if len(df) == 0:
            return None
        return float(df.iloc[-1].value)
    except Exception as e:
        logger.warning("FRED %s 失败: %s", series_id, str(e)[:80])
        return None


def _yf_latest_close(ticker: str) -> float | None:
    """yfinance 拉某符号最新收盘价。失败返回 None。"""
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="10d")
        if len(h) == 0:
            return None
        return float(h["Close"].iloc[-1])
    except Exception as e:
        logger.warning("yfinance %s 失败: %s", ticker, str(e)[:80])
        return None


# ─────────── 宏观指标 ───────────

def get_fed_rate() -> tuple[float | None, str]:
    """美联储联邦基金利率（%）。"""
    fed = _fred_latest("FEDFUNDS")
    if fed is not None:
        return fed, "FRED:FEDFUNDS"
    # 备用：13 周国债收益率（与 Fed Funds 高度同步）
    irx = _yf_latest_close("^IRX")
    if irx is not None:
        return irx, "yfinance:^IRX (13W Treasury, 备用)"
    return None, "无数据"


def get_cpi_yoy() -> tuple[float | None, str]:
    """CPI 同比通胀（%）。"""
    if _has_fred_key():
        try:
            from openbb import obb
            df = obb.economy.fred_series("CPIAUCSL", start_date="2020-01-01").to_df()
            if len(df) >= 13:
                latest = float(df.iloc[-1].value)
                year_ago = float(df.iloc[-13].value)
                return ((latest / year_ago - 1) * 100), "FRED:CPIAUCSL"
        except Exception as e:
            logger.warning("FRED CPI 失败: %s", str(e)[:80])
    return None, "需 FRED key"


def get_unemployment() -> tuple[float | None, str]:
    """失业率（%）。"""
    val = _fred_latest("UNRATE")
    return (val, "FRED:UNRATE") if val is not None else (None, "需 FRED key")


def get_ten_year_yield() -> tuple[float | None, str]:
    """10 年国债收益率（%）。"""
    val = _fred_latest("DGS10")
    if val is not None:
        return val, "FRED:DGS10"
    tnx = _yf_latest_close("^TNX")
    if tnx is not None:
        return tnx, "yfinance:^TNX"
    return None, "无数据"


def get_yield_curve() -> tuple[float | None, str]:
    """收益率曲线斜率（10Y - 2Y），%。倒挂 = 衰退前兆。"""
    if _has_fred_key():
        ten = _fred_latest("DGS10")
        two = _fred_latest("DGS2")
        if ten is not None and two is not None:
            return (ten - two), "FRED:DGS10-DGS2"
    # 备用：^TNX - ^IRX（10年 - 13周）
    tnx = _yf_latest_close("^TNX")
    irx = _yf_latest_close("^IRX")
    if tnx is not None and irx is not None:
        return (tnx - irx), "yfinance:^TNX-^IRX (备用)"
    return None, "无数据"


# ─────────── 综合 Regime 判定 ───────────

def macro_regime() -> dict[str, Any]:
    """综合宏观 regime 判定。

    返回 {
      fed_rate, cpi_yoy, unemp, ten_year_yield, yield_curve,
      regime: "tightening" / "neutral" / "easing" / "recession_warning",
      alerts: [...]   # 教科书警告规则
    }
    """
    fed, fed_src = get_fed_rate()
    cpi, cpi_src = get_cpi_yoy()
    unemp, unemp_src = get_unemployment()
    ten, ten_src = get_ten_year_yield()
    curve, curve_src = get_yield_curve()

    alerts = []

    # 加息周期警告（Fed Funds > 4.5%）
    if fed is not None and fed > 4.5:
        alerts.append({
            "type": "TIGHTENING",
            "severity": "MEDIUM",
            "msg": f"⚠️ Fed Funds = {fed:.2f}% > 4.5%，处于加息周期。"
                   f"高 PE 成长股（v6 推荐池）历史上在加息周期跑输（参考 2022 -52% DD）。"
                   f"建议给 ⭐⭐⭐ 推荐加'加息环境警告'标签。",
        })

    # 通胀警告（CPI > 5%）
    if cpi is not None and cpi > 5:
        alerts.append({
            "type": "INFLATION",
            "severity": "MEDIUM",
            "msg": f"⚠️ CPI YoY = {cpi:.2f}% > 5%，通胀偏高。"
                   f"利好稀缺资源（铀/稀土/水）和能源；不利消费品。",
        })

    # 收益率曲线倒挂（衰退预警）
    if curve is not None and curve < 0:
        alerts.append({
            "type": "RECESSION_WARNING",
            "severity": "HIGH",
            "msg": f"🚨 收益率曲线倒挂 ({curve:.2f}%)！历史上 100% 准确预测衰退（提前 6-24 个月）。"
                   f"建议组合加防御票（KO/MCD）或减仓。",
        })

    # 失业率上升（仅有 FRED 数据时）
    # （简化版：只看绝对值；真实应该看 3 个月趋势）
    if unemp is not None and unemp > 4.5:
        alerts.append({
            "type": "UNEMPLOYMENT",
            "severity": "LOW",
            "msg": f"失业率 = {unemp:.1f}%，需关注是否上升趋势（>4.5% + 3 月连涨 = 衰退确认）。",
        })

    # 综合 regime
    if curve is not None and curve < 0:
        regime = "recession_warning"
    elif fed is not None and fed > 4.5:
        regime = "tightening"
    elif fed is not None and fed < 2:
        regime = "easing"
    else:
        regime = "neutral"

    return {
        "fed_rate": fed,
        "fed_rate_source": fed_src,
        "cpi_yoy": cpi,
        "cpi_source": cpi_src,
        "unemp": unemp,
        "unemp_source": unemp_src,
        "ten_year_yield": ten,
        "ten_year_source": ten_src,
        "yield_curve": curve,
        "yield_curve_source": curve_src,
        "regime": regime,
        "alerts": alerts,
        "fred_key_present": _has_fred_key(),
    }
