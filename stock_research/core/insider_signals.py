"""内部人交易信号 — 把 Finnhub 原始数据升级成"信号判定"。

学术依据：
  - Lakonishok & Lee (2001) "Are Insider Trades Informative?", RFS
    实证：内部人净买入是中线（3-12 个月）alpha 正信号
  - Seyhun (1986, 1992) — 内部人买入 alpha 显著（年化 +5%）；
    但单笔大额卖出未必是负信号（可能只是分散持仓）

判定规则：
  STRONG_BUY  净买入 ≥ $1M 且 buys ≥ 2 个高管 → 强信号
  WEAK_BUY    净买入 < $1M 但 ≥ $200K
  NEUTRAL     |net_dollars| < $200K 或数据不全
  WEAK_SELL   净卖出 < $5M
  STRONG_SELL 净卖出 ≥ $5M 且 sells ≥ 2 个高管 → 警告
              （注意：单一卖出常因税务/分散，不一定是看空）

输入：
  从 finnhub_client.fetch_insider_transactions 拿到的原始数据
  或 enrich_watchlist 落到 enrich/watchlist_*.json 的快照

输出：
  {ticker, signal, net_dollars, buy_count, sell_count, top_buyers, top_sellers}
"""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ─────────── 阈值（Lakonishok-Lee 2001 启发）───────────

STRONG_BUY_THRESHOLD = 1_000_000   # $1M+ 净买入
STRONG_SELL_THRESHOLD = -5_000_000 # -$5M+ 净卖出
WEAK_BUY_THRESHOLD = 200_000
WEAK_SELL_THRESHOLD = -200_000

MIN_BUYERS_FOR_STRONG = 2  # 至少 2 个高管同向，避免单人偶发交易


# ─────────── 信号判定 ───────────

def evaluate(insider_data: dict[str, Any]) -> dict[str, Any]:
    """单只股票的内部人信号判定。

    输入：fetch_insider_transactions 的原始返回值
    输出：信号 dict
    """
    ticker = insider_data.get("ticker", "")
    transactions = insider_data.get("transactions", [])
    if not transactions:
        return {
            "ticker": ticker,
            "signal": "NO_DATA",
            "net_dollars": 0,
            "buy_count": 0,
            "sell_count": 0,
        }

    # 算每笔的 dollars（change × price）
    net_dollars = 0.0
    buys: list[dict] = []
    sells: list[dict] = []
    for t in transactions:
        change = t.get("change", 0) or 0
        price = t.get("transaction_price", 0) or 0
        if change == 0 or price == 0:
            continue
        dollars = change * price  # change > 0 = 买入；< 0 = 卖出
        net_dollars += dollars
        item = {
            "name": t.get("name", ""),
            "shares": change,
            "price": price,
            "dollars": dollars,
            "date": t.get("transaction_date", ""),
        }
        if change > 0:
            buys.append(item)
        else:
            sells.append(item)

    # 唯一买家 / 卖家数（不同人）
    unique_buyers = len(set(b["name"] for b in buys))
    unique_sellers = len(set(s["name"] for s in sells))

    # 信号判定
    if net_dollars >= STRONG_BUY_THRESHOLD and unique_buyers >= MIN_BUYERS_FOR_STRONG:
        signal = "STRONG_BUY"
    elif net_dollars >= WEAK_BUY_THRESHOLD:
        signal = "WEAK_BUY"
    elif net_dollars <= STRONG_SELL_THRESHOLD and unique_sellers >= MIN_BUYERS_FOR_STRONG:
        signal = "STRONG_SELL"
    elif net_dollars <= WEAK_SELL_THRESHOLD:
        signal = "WEAK_SELL"
    else:
        signal = "NEUTRAL"

    # Top 3 买家 / 卖家（按金额）
    top_buyers = sorted(buys, key=lambda x: -x["dollars"])[:3]
    top_sellers = sorted(sells, key=lambda x: x["dollars"])[:3]

    return {
        "ticker": ticker,
        "signal": signal,
        "net_dollars": round(net_dollars, 2),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "unique_buyers": unique_buyers,
        "unique_sellers": unique_sellers,
        "top_buyers": top_buyers,
        "top_sellers": top_sellers,
        "lookback_days": insider_data.get("lookback_days", 90),
    }


# ─────────── 批量汇总 ───────────

def aggregate_watchlist(watchlist_records: list[dict[str, Any]]) -> dict[str, Any]:
    """对全 watchlist 汇总内部人信号。

    输入：feishu.fetch_watchlist() 的输出，每条含 normalized 字段
    （需要先跑过 jobs/enrich_watchlist 把 insider 数据落到 enrich snapshot）

    返回：
      {
        "strong_buy": [...],     # 强买入（高管增持）
        "strong_sell": [...],    # 强卖出（高管减持）
        "neutral": int,
        "no_data": int,
        "ranked_all": [...],
      }
    """
    from ..adapters import store
    from .. import config

    # 拉最新 enrichment snapshot
    enrich = store.load_latest_json(config.ENRICH_DIR, "watchlist") or []
    if not isinstance(enrich, list):
        return {"error": "no enrichment data; run jobs.enrich_watchlist first"}

    results = []
    for r in enrich:
        finn = (r.get("finnhub") or {})
        insider_raw = finn.get("insider")
        if not insider_raw:
            continue
        sig = evaluate(insider_raw)
        sig["name"] = r.get("name") or sig["ticker"]
        results.append(sig)

    # 按信号分类
    by_signal: dict[str, list] = {
        "STRONG_BUY": [], "WEAK_BUY": [], "NEUTRAL": [],
        "WEAK_SELL": [], "STRONG_SELL": [], "NO_DATA": [],
    }
    for r in results:
        by_signal[r["signal"]].append(r)

    # 排序：净买入降序
    ranked_all = sorted(results, key=lambda x: -x.get("net_dollars", 0))

    return {
        "n_total": len(results),
        "strong_buy": by_signal["STRONG_BUY"],
        "weak_buy": by_signal["WEAK_BUY"],
        "neutral": len(by_signal["NEUTRAL"]),
        "weak_sell": by_signal["WEAK_SELL"],
        "strong_sell": by_signal["STRONG_SELL"],
        "no_data": len(by_signal["NO_DATA"]),
        "ranked_all": ranked_all,
    }


def format_text(agg: dict[str, Any]) -> str:
    """把 aggregate 结果格式化成可读文本。"""
    if "error" in agg:
        return f"❌ {agg['error']}"
    lines = [
        f"📊 内部人交易信号汇总（{agg['n_total']} 只股票，过去 90 天）",
        "",
        f"🟢 STRONG_BUY: {len(agg['strong_buy'])} | "
        f"WEAK_BUY: {len(agg['weak_buy'])} | "
        f"NEUTRAL: {agg['neutral']} | "
        f"WEAK_SELL: {len(agg['weak_sell'])} | "
        f"🔴 STRONG_SELL: {len(agg['strong_sell'])} | "
        f"NO_DATA: {agg['no_data']}",
        "",
    ]
    if agg["strong_buy"]:
        lines.append("🟢 强买入信号（高管同向增持 ≥$1M）:")
        for r in agg["strong_buy"]:
            lines.append(f"  · {r['name']} ({r['ticker']}) "
                         f"+${r['net_dollars']/1e6:.1f}M · "
                         f"{r['unique_buyers']} 高管买入")
        lines.append("")
    if agg["strong_sell"]:
        lines.append("🔴 强卖出警告（高管同向减持 ≥$5M）:")
        for r in agg["strong_sell"]:
            lines.append(f"  · {r['name']} ({r['ticker']}) "
                         f"-${abs(r['net_dollars'])/1e6:.1f}M · "
                         f"{r['unique_sellers']} 高管卖出")
    return "\n".join(lines)
