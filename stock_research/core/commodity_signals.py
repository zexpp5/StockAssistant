"""大宗商品信号 — 用商品价格 vs watchlist 标的相关性，验证 5 大稀缺资源逻辑。

为什么需要：
  v6 推荐池里有「下一波稀缺资源」主题（XYL/MP/CCJ/BWXT/RDDT），
  这些标的的真实驱动力是底层商品价格。如果商品价格在涨而股票没动，
  可能是滞后机会；反之如果商品下跌但股票还在涨，要警惕。

5 大商品（与 v6 5 大稀缺资源对应）：
  油价（^CL=F / USO）       → 通胀环境对照
  铜价（^HG=F / CPER）      → AI 数据中心铜需求
  铀价（URA / URNM ETF）    → 核能燃料（CCJ）
  黄金（^GC=F / GLD）       → 避险情绪
  天然气（^NG=F / UNG）     → AI 数据中心电力

学术依据：
  - Bessembinder & Chan (1992) "Time-varying risk premia and forecastable returns
    in futures markets" — 商品期货收益可预测部分股票
  - Hong & Yogo (2012) "What does futures market interest tell us about the
    macroeconomy and asset prices?" — 商品 OI 与 industrial stocks 相关性

核心函数：
  - fetch_commodity_prices(): 拉商品过去 N 天价格
  - correlation_with_stocks(): 算商品 vs 股票每日收益相关性
  - signal_summary(): 综合判定（哪些商品在涨 → 关注哪个稀缺资源）
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


# ─────────── 5 大商品（用 ETF 替代期货，免费 + 数据稳）───────────

COMMODITIES = {
    "USO": "原油（United States Oil Fund）",
    "CPER": "铜（United States Copper Index Fund）",
    "URA": "铀矿（Global X Uranium ETF）",
    "GLD": "黄金（SPDR Gold Trust）",
    "UNG": "天然气（United States Natural Gas Fund）",
}

# 商品 → 受益主题映射（用于"商品涨 → 看哪些股票"）
COMMODITY_TO_BENEFICIARIES = {
    "USO": ["XLE 能源 ETF（间接）"],
    "CPER": ["MP 稀土", "工业链", "电力链 GEV/ETN"],
    "URA": ["CCJ 铀矿", "BWXT SMR", "OKLO", "LEU"],
    "GLD": ["避险资产（无 watchlist 直接对应）"],
    "UNG": ["VST 天然气电力", "电力链 GEV"],
}


# ─────────── 数据获取 ───────────

def fetch_commodity_prices(lookback_days: int = 90,
                           use_openbb: bool = True) -> dict[str, Any]:
    """拉 5 大商品 ETF 过去 N 天价格。"""
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=lookback_days + 10)).strftime("%Y-%m-%d")
    out: dict[str, Any] = {"start": start, "end": end, "commodities": {}}

    if use_openbb:
        try:
            from openbb import obb
            for ticker, name in COMMODITIES.items():
                try:
                    r = obb.equity.price.historical(
                        ticker, start_date=start, end_date=end, provider="yfinance",
                    )
                    df = r.to_df()
                    if len(df) < 2:
                        out["commodities"][ticker] = {"name": name, "error": "no data"}
                        continue
                    closes = df["close"].astype(float)
                    out["commodities"][ticker] = {
                        "name": name,
                        "start_price": round(float(closes.iloc[0]), 2),
                        "end_price": round(float(closes.iloc[-1]), 2),
                        "cum_return_pct": round((float(closes.iloc[-1]) / float(closes.iloc[0]) - 1) * 100, 2),
                        "max_drawdown_pct": round(float(((closes / closes.cummax()) - 1).min()) * 100, 2),
                        "n_days": len(df),
                        "_closes": closes,  # 给 correlation 用
                    }
                except Exception as e:
                    out["commodities"][ticker] = {"name": name, "error": str(e)[:80]}
            return out
        except ImportError:
            pass

    # Fallback: yfinance
    try:
        import yfinance as yf
        for ticker, name in COMMODITIES.items():
            try:
                h = yf.Ticker(ticker).history(start=start, end=end)
                if len(h) < 2:
                    out["commodities"][ticker] = {"name": name, "error": "no data"}
                    continue
                closes = h["Close"].astype(float)
                out["commodities"][ticker] = {
                    "name": name,
                    "start_price": round(float(closes.iloc[0]), 2),
                    "end_price": round(float(closes.iloc[-1]), 2),
                    "cum_return_pct": round((float(closes.iloc[-1]) / float(closes.iloc[0]) - 1) * 100, 2),
                    "max_drawdown_pct": round(float(((closes / closes.cummax()) - 1).min()) * 100, 2),
                    "n_days": len(h),
                    "_closes": closes,
                }
            except Exception as e:
                out["commodities"][ticker] = {"name": name, "error": str(e)[:80]}
    except ImportError:
        out["error"] = "neither openbb nor yfinance available"
    return out


# ─────────── 相关性矩阵 ───────────

def correlation_with_stocks(commodity_data: dict[str, Any],
                            stock_tickers: list[str],
                            lookback_days: int = 90) -> dict[str, Any]:
    """算每个商品 ETF 与一组股票的过去 N 天日收益相关性。"""
    try:
        import yfinance as yf
    except ImportError:
        return {"error": "yfinance required"}

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=lookback_days + 10)).strftime("%Y-%m-%d")

    # 拉股票价格
    stock_returns = {}
    for tk in stock_tickers:
        try:
            yf_ticker = tk
            if tk.isdigit():
                if tk.startswith(("00", "30")):
                    yf_ticker = f"{tk}.SZ"
                elif tk.startswith(("60", "68")):
                    yf_ticker = f"{tk}.SS"
                else:
                    yf_ticker = tk
            h = yf.Ticker(yf_ticker).history(start=start, end=end)
            if len(h) < 30:
                continue
            stock_returns[tk] = h["Close"].pct_change().dropna()
        except Exception as e:
            logger.debug("skip %s: %s", tk, e)

    def _strip_tz(s):
        """统一 index 为 tz-naive，避免 OpenBB(tz-aware) vs yfinance(tz-naive) 混合。"""
        try:
            if hasattr(s.index, "tz") and s.index.tz is not None:
                s = s.copy()
                s.index = s.index.tz_localize(None)
        except Exception:
            pass
        return s

    correlations = {}
    for ctk, info in commodity_data.get("commodities", {}).items():
        closes = info.get("_closes")
        if closes is None or len(closes) < 30:
            continue
        commod_rets = _strip_tz(closes.pct_change().dropna())
        cors = {}
        for stk, srets in stock_returns.items():
            srets_clean = _strip_tz(srets)
            # 对齐 index（normalize 到 date 而非 datetime，避免 OpenBB/yf 时间戳差异）
            try:
                c_by_date = commod_rets.copy()
                s_by_date = srets_clean.copy()
                c_by_date.index = pd.to_datetime(c_by_date.index).normalize()
                s_by_date.index = pd.to_datetime(s_by_date.index).normalize()
                df = pd.DataFrame({"c": c_by_date, "s": s_by_date}).dropna()
            except Exception:
                continue
            if len(df) < 30:
                continue
            cor = df["c"].corr(df["s"])
            if pd.notna(cor):
                cors[stk] = round(float(cor), 3)
        # 排序
        sorted_cors = sorted(cors.items(), key=lambda x: -abs(x[1]))
        correlations[ctk] = {
            "name": info["name"],
            "commodity_return_pct": info.get("cum_return_pct"),
            "top_correlated": sorted_cors[:5],
            "all": cors,
        }
    return correlations


# ─────────── 综合摘要 ───────────

def signal_summary(commodity_data: dict[str, Any]) -> dict[str, Any]:
    """商品价格趋势 → 关注哪些主题/股票。"""
    rankings = []
    for ticker, info in commodity_data.get("commodities", {}).items():
        if "cum_return_pct" in info:
            rankings.append({
                "ticker": ticker,
                "name": info["name"],
                "cum_return_pct": info["cum_return_pct"],
                "beneficiaries": COMMODITY_TO_BENEFICIARIES.get(ticker, []),
            })
    rankings.sort(key=lambda x: -x["cum_return_pct"])

    return {
        "lookback": f"{commodity_data['start']} → {commodity_data['end']}",
        "rankings": rankings,
        "leaders": [r for r in rankings if r["cum_return_pct"] > 5][:3],
        "laggards": [r for r in rankings if r["cum_return_pct"] < -5][:3],
    }
