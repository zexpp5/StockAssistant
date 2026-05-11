"""
SPY 200-Day MA 趋势过滤器（解决问题 #3 暴露的熊市盲区）
─────────────────────────────────────────
论文出处:
  · Faber, M. T. (2007). "A Quantitative Approach to Tactical Asset Allocation"
    SSRN. 简单 200-day MA 规则在 1973-2006 把最大回撤从 -55% 降到 -23%
  · Antonacci (2014) "Dual Momentum Investing" 进一步验证

规则（学术经典，不是我编的）:
  · SPY 收盘价 > 200-day MA  →  RISK-ON  : 全仓推荐 (1.0x)
  · SPY 收盘价 < 200-day MA  →  RISK-OFF : 减仓至 50% (0.5x), 现金加倍

Walk-forward 测试时:
  · 在每个 regime 起点检查 SPY/200MA
  · RISK-OFF 时自动减仓 + 切现金对冲
"""
import sys
import os
import pandas as pd
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yfinance as yf


def get_market_regime(as_of=None, lookback_days=400):
    """返回当前市场 regime: RISK-ON / RISK-OFF + 详细信息"""
    target = pd.to_datetime(as_of) if as_of else pd.Timestamp.now()
    start = target - pd.Timedelta(days=lookback_days)
    end = target + pd.Timedelta(days=2)
    spy = yf.Ticker("SPY").history(start=start, end=end)
    if len(spy) < 200:
        return {"regime": "UNKNOWN", "reason": "insufficient SPY history"}

    if spy.index.tz:
        spy = spy[spy.index.tz_localize(None) <= target]
    else:
        spy = spy[spy.index <= target]

    if len(spy) < 200:
        return {"regime": "UNKNOWN", "reason": "insufficient after cutoff"}

    close = spy["Close"]
    ma_200 = close.rolling(200).mean()
    spy_now = float(close.iloc[-1])
    ma_now = float(ma_200.iloc[-1])

    # Faber 规则
    if spy_now > ma_now:
        regime = "RISK-ON"
        position_multiplier = 1.0
    else:
        regime = "RISK-OFF"
        position_multiplier = 0.5  # 减仓到 50%

    distance_pct = (spy_now / ma_now - 1) * 100

    return {
        "as_of": as_of or "now",
        "regime": regime,
        "spy_close": round(spy_now, 2),
        "spy_200ma": round(ma_now, 2),
        "distance_pct": round(distance_pct, 2),
        "position_multiplier": position_multiplier,
        "rule": "SPY > 200MA → 全仓 / SPY < 200MA → 减仓至 50%",
        "source": "Faber 2007 SSRN",
    }


def main():
    """对历史关键时点跑一遍 regime 判断"""
    test_dates = [
        ("2018-10-01", "贸易战 + 加息双杀（熊）"),
        ("2020-04-01", "疫情后反弹"),
        ("2022-01-01", "加息熊市"),
        ("2023-04-01", "AI 主题崛起"),
        ("2024-07-01", "震荡"),
        ("2025-12-31", "2025 末"),
        (None, "今天"),
    ]
    print("=" * 100)
    print(f"  📡 SPY 200-MA 趋势过滤器（Faber 2007 SSRN）")
    print("=" * 100)
    print(f"\n  {'日期':<14}{'Regime':<12}{'SPY':>10}{'200MA':>10}{'距离':>10}{'仓位倍数':>10}  说明")
    print(f"  {'-'*100}")
    for date, label in test_dates:
        info = get_market_regime(as_of=date)
        date_str = date or "今天"
        regime = info.get("regime", "?")
        spy_now = info.get("spy_close", "-")
        ma_now = info.get("spy_200ma", "-")
        dist = info.get("distance_pct", 0)
        mult = info.get("position_multiplier", 1.0)
        marker = "🟢" if regime == "RISK-ON" else ("🔴" if regime == "RISK-OFF" else "⚪")
        print(f"  {date_str:<14}{marker} {regime:<10}{spy_now:>10}{ma_now:>10}{dist:>+9.2f}%{mult:>9.0%}  {label}")


if __name__ == "__main__":
    main()
