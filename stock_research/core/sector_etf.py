"""GICS 11 行业 ETF 数据 — 用于 Brinson 业绩归因和精准基准对比。

为什么需要：
  - SPY 是"宽基"基准，不能告诉你"v6 跑赢 SPY 是因为押对了科技板块还是真的选股有 alpha"
  - 11 个 SPDR Sector ETF 对应 GICS 一级行业，是 Brinson-Hood-Beebower (1986)
    业绩归因的标准基准

11 个 SPDR ETF（State Street, 1998 推出）：
  XLK  科技             ← AI 算力链对应
  XLC  通信             ← 大科技 GOOGL/META 对应
  XLY  可选消费         ← TSLA/AMZN 对应
  XLP  必需消费品       ← KO/MCD 对应
  XLV  医疗保健         ← TEM/RXRX/VEEV 对应
  XLF  金融             ← 防御对照（v6 不持有）
  XLI  工业             ← VRT/ETN/PWR 电力链对应
  XLU  公用事业         ← VST/CEG 电力链对应
  XLRE 房地产           ← EQIX 数据中心 REIT 对应
  XLE  能源             ← 通胀环境对照
  XLB  原材料           ← MP 稀土对应

应用：
  1. Brinson 归因：组合 vs 11 个 ETF 加权基准 → 拆出 allocation/selection effect
  2. 精准基准：v6 推荐主要是 XLK + XLC，比 vs SPY 更公平
  3. 行业轮动信号：哪个 ETF 在涨可指示当前主线
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# ─────────── 11 个 GICS 一级行业 ETF（SPDR）───────────

GICS_SECTOR_ETFS = {
    "XLK": "科技 (Technology)",
    "XLC": "通信 (Communication)",
    "XLY": "可选消费 (Consumer Discretionary)",
    "XLP": "必需消费 (Consumer Staples)",
    "XLV": "医疗 (Health Care)",
    "XLF": "金融 (Financials)",
    "XLI": "工业 (Industrials)",
    "XLU": "公用事业 (Utilities)",
    "XLRE": "房地产 (Real Estate)",
    "XLE": "能源 (Energy)",
    "XLB": "原材料 (Materials)",
}

# 你 watchlist 主题 → GICS ETF 映射
THEME_TO_ETF = {
    "🔥 AI 算力核心": "XLK",
    "💡 AI 连接（光通信+ASIC）": "XLK",
    "⚡ AI 电力链": "XLI",  # 电力设备多归工业，VST/CEG 实际是 XLU
    "🤖 AI 应用层": "XLK",
    "🏢 数据中心承载层": "XLRE",
    "🦾 物理 AI": "XLY",
    "🧬 AI 医疗": "XLV",
    "💎 下一波稀缺资源": "XLB",  # MP 稀土；CCJ 铀 可能 XLE
    "🇨🇳 中国 AI": "XLK",  # ADR 大都科技
    "🌏 海外 AI 生态": "XLK",
    "📱 平台/转型": "XLK",
    "🛡️ 防御对照": "XLP",
}


# ─────────── 数据获取 ───────────

def fetch_sector_returns(start: str, end: str | None = None,
                         use_openbb: bool = True) -> dict[str, Any]:
    """拉 11 个 GICS ETF 在 [start, end] 的价格 + 收益。

    优先用 OpenBB（统一接口），缺包/失败时退回 yfinance。
    """
    end = end or datetime.now().strftime("%Y-%m-%d")
    out: dict[str, Any] = {"start": start, "end": end, "etfs": {}}

    if use_openbb:
        try:
            from openbb import obb
            for ticker, name in GICS_SECTOR_ETFS.items():
                try:
                    r = obb.equity.price.historical(
                        ticker, start_date=start, end_date=end,
                        provider="yfinance",
                    )
                    df = r.to_df()
                    if len(df) < 2:
                        out["etfs"][ticker] = {"name": name, "error": "no data"}
                        continue
                    first = float(df["close"].iloc[0])
                    last = float(df["close"].iloc[-1])
                    cum_ret = (last / first - 1) * 100
                    # 算最大回撤
                    closes = df["close"].astype(float)
                    drawdown = ((closes / closes.cummax()) - 1).min() * 100
                    out["etfs"][ticker] = {
                        "name": name,
                        "start_price": round(first, 2),
                        "end_price": round(last, 2),
                        "cum_return_pct": round(cum_ret, 2),
                        "max_drawdown_pct": round(float(drawdown), 2),
                        "n_days": len(df),
                    }
                except Exception as e:
                    out["etfs"][ticker] = {"name": name, "error": str(e)[:80]}
            return out
        except ImportError:
            logger.info("OpenBB 未安装，回退 yfinance")

    # Fallback: yfinance
    try:
        import yfinance as yf
        for ticker, name in GICS_SECTOR_ETFS.items():
            try:
                h = yf.Ticker(ticker).history(start=start, end=end)
                if len(h) < 2:
                    out["etfs"][ticker] = {"name": name, "error": "no data"}
                    continue
                first = float(h["Close"].iloc[0])
                last = float(h["Close"].iloc[-1])
                cum_ret = (last / first - 1) * 100
                drawdown = ((h["Close"] / h["Close"].cummax()) - 1).min() * 100
                out["etfs"][ticker] = {
                    "name": name,
                    "start_price": round(first, 2),
                    "end_price": round(last, 2),
                    "cum_return_pct": round(cum_ret, 2),
                    "max_drawdown_pct": round(float(drawdown), 2),
                    "n_days": len(h),
                }
            except Exception as e:
                out["etfs"][ticker] = {"name": name, "error": str(e)[:80]}
    except ImportError:
        out["error"] = "neither openbb nor yfinance available"

    return out


def benchmark_for_themes(theme_weights: dict[str, float]) -> tuple[str, dict]:
    """根据组合的主题权重，计算 GICS ETF 加权基准。

    输入：theme_weights = {主题名: 权重}（来自 picks 表的"主题分类"）
    返回：
      - 主基准 ticker（占比最高的 ETF）
      - 加权 ETF 权重 dict {ETF: 权重}
    """
    etf_weights: dict[str, float] = {}
    for theme, w in theme_weights.items():
        etf = THEME_TO_ETF.get(theme, "XLK")
        etf_weights[etf] = etf_weights.get(etf, 0) + w
    if not etf_weights:
        return "SPY", {"SPY": 1.0}
    # 归一化
    total = sum(etf_weights.values())
    etf_weights = {k: v / total for k, v in etf_weights.items()}
    primary = max(etf_weights, key=etf_weights.get)
    return primary, etf_weights


def get_sector_rotation_signal(lookback_days: int = 60) -> dict[str, Any]:
    """简单行业轮动信号：过去 N 天哪个 ETF 涨幅最大 = 当前主线。

    用于补充"当前主题应该聚焦哪个行业"的判断。
    """
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=lookback_days + 10)).strftime("%Y-%m-%d")
    data = fetch_sector_returns(start, end, use_openbb=True)
    rankings = []
    for ticker, info in data["etfs"].items():
        if "cum_return_pct" in info:
            rankings.append({
                "ticker": ticker,
                "name": info["name"],
                "return_pct": info["cum_return_pct"],
                "drawdown_pct": info.get("max_drawdown_pct", 0),
            })
    rankings.sort(key=lambda x: -x["return_pct"])

    return {
        "lookback_days": lookback_days,
        "start": start,
        "end": end,
        "leaders": rankings[:3],
        "laggards": rankings[-3:],
        "all_rankings": rankings,
    }
