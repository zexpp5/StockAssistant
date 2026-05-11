"""期权信号 — 用 SPY put/call ratio 补强 v7 实盘防御。

学术依据：
  - Pan & Poteshman (2006) "The Information in Option Volume for Future Stock Prices",
    RFS — put/call volume 是 informed 交易者的领先指标
  - CBOE 官方 PCR 阈值标准（行业共识）：
    < 0.7    极度看涨（contrarian 谨慎信号）
    0.7-1.0  正常
    > 1.0    看跌偏多
    > 1.2    恐慌
    > 1.5    极端恐慌（contrarian 反向信号）

为什么 PCR 比 VIX 更早：
  - VIX 是隐含波动率（期权价格反推），还原经过几次平滑
  - PCR 是 raw 期权交易量 = 资金真实下注方向
  - 历史回看：2020-02 PCR 在 SPY 顶点前 1 周开始抬升

核心函数：
  - spy_put_call_ratio(): 当前 SPY 整体 PCR（volume + OI 两版）
  - put_call_signal(): 综合判定 → BULLISH / NEUTRAL / BEARISH / PANIC

集成进 v7：
  defense_signals.check_market_regime() 加入 PCR 信号 → 当 PCR > 1.2 触发 MARKET_PANIC 警报
"""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ─────────── CBOE 阈值（行业标准）───────────

PCR_EXTREME_BEARISH = 1.5    # 极端恐慌
PCR_BEARISH = 1.2            # 恐慌
PCR_BALANCED_HIGH = 1.0
PCR_BALANCED_LOW = 0.7
PCR_EXTREME_BULLISH = 0.7    # 过度乐观（contrarian 谨慎）


# ─────────── 数据获取 ───────────

def spy_put_call_ratio(symbol: str = "SPY", n_expirations: int = 8) -> dict[str, Any]:
    """拉某个 ticker 的当前期权链，算 PCR。直接用 yfinance（OpenBB 在此原本也是
    `provider="yfinance"` 的包装，去掉一层间接降低依赖体积）。

    汇总最近 `n_expirations` 个到期日的 puts/calls volume + open interest。

    返回 {
      "pcr_volume": float,    # 成交量 PCR
      "pcr_oi": float,        # 持仓量 PCR
      "put_volume": int,
      "call_volume": int,
      "put_oi": int,
      "call_oi": int,
      "underlying_price": float,
      "n_contracts": int,
    }
    """
    try:
        import yfinance as yf
        tkr = yf.Ticker(symbol)
        expirations = list(tkr.options or ())
    except Exception as e:
        logger.warning("yfinance options chain failed for %s: %s", symbol, str(e)[:80])
        return {"error": str(e)[:80]}

    if not expirations:
        return {"error": "no options expirations"}

    put_vol = call_vol = put_oi = call_oi = 0.0
    n_contracts = 0
    for exp in expirations[:n_expirations]:
        try:
            chain = tkr.option_chain(exp)
        except Exception as e:
            logger.debug("chain fetch failed for %s exp=%s: %s", symbol, exp, str(e)[:80])
            continue
        puts_df, calls_df = chain.puts, chain.calls
        if puts_df is not None and len(puts_df):
            put_vol += float(puts_df["volume"].fillna(0).sum())
            put_oi += float(puts_df["openInterest"].fillna(0).sum())
            n_contracts += len(puts_df)
        if calls_df is not None and len(calls_df):
            call_vol += float(calls_df["volume"].fillna(0).sum())
            call_oi += float(calls_df["openInterest"].fillna(0).sum())
            n_contracts += len(calls_df)

    if n_contracts == 0:
        return {"error": "no options contracts returned"}

    pcr_vol = (put_vol / call_vol) if call_vol > 0 else None
    pcr_oi = (put_oi / call_oi) if call_oi > 0 else None

    underlying = None
    try:
        hist = tkr.history(period="1d")
        if hist is not None and len(hist):
            underlying = float(hist["Close"].iloc[-1])
    except Exception:
        pass

    return {
        "symbol": symbol,
        "pcr_volume": round(pcr_vol, 3) if pcr_vol else None,
        "pcr_oi": round(pcr_oi, 3) if pcr_oi else None,
        "put_volume": int(put_vol),
        "call_volume": int(call_vol),
        "put_oi": int(put_oi),
        "call_oi": int(call_oi),
        "underlying_price": underlying,
        "n_contracts": n_contracts,
        "n_expirations_used": min(len(expirations), n_expirations),
    }


# ─────────── 信号判定 ───────────

def put_call_signal(pcr: float | None) -> dict[str, Any]:
    """根据 PCR 数值判定信号。

    返回 {signal, severity, label, action_suggestion}
    """
    if pcr is None:
        return {
            "signal": "NO_DATA", "severity": "NONE",
            "label": "无数据", "action": "—",
        }
    if pcr >= PCR_EXTREME_BEARISH:
        return {
            "signal": "PANIC",
            "severity": "CRITICAL",
            "label": f"🚨🚨 极端恐慌（PCR={pcr:.2f} ≥ {PCR_EXTREME_BEARISH}）",
            "action": "考虑抄底（contrarian 信号）+ 同时减仓防御",
            "pcr": pcr,
        }
    if pcr >= PCR_BEARISH:
        return {
            "signal": "BEARISH",
            "severity": "HIGH",
            "label": f"⚠️ 看跌情绪强（PCR={pcr:.2f} ≥ {PCR_BEARISH}）",
            "action": "减仓 50% / 买 put 对冲",
            "pcr": pcr,
        }
    if pcr >= PCR_BALANCED_HIGH:
        return {
            "signal": "MILD_BEARISH",
            "severity": "MEDIUM",
            "label": f"看跌偏多（PCR={pcr:.2f}）",
            "action": "保持警惕，关注是否升至 1.2",
            "pcr": pcr,
        }
    if pcr >= PCR_BALANCED_LOW:
        return {
            "signal": "NEUTRAL",
            "severity": "NONE",
            "label": f"🟢 正常（PCR={pcr:.2f}）",
            "action": "—",
            "pcr": pcr,
        }
    if pcr >= PCR_EXTREME_BULLISH:
        return {
            "signal": "BULLISH",
            "severity": "LOW",
            "label": f"看涨（PCR={pcr:.2f}）",
            "action": "—",
            "pcr": pcr,
        }
    return {
        "signal": "EXTREME_BULLISH",
        "severity": "MEDIUM",
        "label": f"⚠️ 过度乐观（PCR={pcr:.2f} < {PCR_EXTREME_BULLISH}）",
        "action": "市场可能见顶（contrarian 谨慎信号）",
        "pcr": pcr,
    }


def diagnose() -> dict[str, Any]:
    """一站式诊断 SPY 期权情绪。"""
    pcr_data = spy_put_call_ratio("SPY")
    if "error" in pcr_data:
        return {"error": pcr_data["error"]}

    sig_vol = put_call_signal(pcr_data["pcr_volume"])
    sig_oi = put_call_signal(pcr_data["pcr_oi"])

    return {
        "symbol": "SPY",
        "underlying_price": pcr_data["underlying_price"],
        "pcr_volume": pcr_data["pcr_volume"],
        "pcr_oi": pcr_data["pcr_oi"],
        "signal_volume": sig_vol,
        "signal_oi": sig_oi,
        "raw": pcr_data,
    }
