"""持仓票 catalyst 每日扫描（MVP v2 — FINNHUB + FMP）。

为什么这个 job 存在：
  factor_model / piotroski / momentum 等系统现有的因子打分**永远看不到**：
    - 大股东 / 名人投资人持仓变动
    - 公司新闻 / catalyst（合作、新品、海外扩张）
    - 评级变动（buy/sell 转向）
    - Insider 交易
    - 监管事件
  → 系统给出"减仓观察"动作时，可能完全没考虑真实利好/利空 catalyst。

数据源（用现有 .env 里的 key，不烧 Anthropic API）：
  - FINNHUB：news（含中港新闻）、recommendation_trends 评级变动、insider_transactions
  - FMP：press releases、grades 评级、insider trading、institutional ownership

⚠️ 已知 gap（v2 没覆盖，需 v3 补）：
  - 港股 9992 中文新闻 / 雪球 / 新浪财经 / AASTOCKS 拉不到
  - 段永平/巴菲特/李录 中文圈"明星投资人持仓"拉不到
  - 南向资金 / 港股通持股变动 拉不到（需 akshare 东财）
  - HKEX 中文披露 拉不到
  → v3 加 akshare 港股专门源 + 中文新闻爬虫。

  本 v2 适合 cover 美股持仓（MCD/BRK-B/IAUM）的 90% catalyst；
  港股 9992 只能拉到英文新闻 + FINNHUB 评级，遗漏中文事件。

输出：data/latest/holding_catalyst_scan.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

OUT_PATH = REPO / "data" / "latest" / "holding_catalyst_scan.json"


# ───────── helpers ─────────

def _http_get_json(url: str, timeout: int = 15) -> Any:
    req = Request(url, headers={"User-Agent": "StockAssistant/catalyst-scan"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        return {"_error": str(e)}


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _finnhub_symbol(ticker: str) -> str:
    """FINNHUB symbol 转换：
    9992.HK → 9992.HK（保留）
    BRK-B → BRK.B（FINNHUB 用 .）
    其它美股 → 不变
    """
    t = ticker.upper()
    if "-" in t and not t.endswith(".HK"):
        return t.replace("-", ".")
    return t


# ───────── FINNHUB API ─────────

def fetch_finnhub_news(ticker: str, *, days: int = 7) -> list[dict]:
    """FINNHUB company-news"""
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return [{"_error": "no FINNHUB_API_KEY"}]
    today = datetime.now().date()
    sym = _finnhub_symbol(ticker)
    params = {
        "symbol": sym,
        "from": (today - timedelta(days=days)).isoformat(),
        "to": today.isoformat(),
        "token": key,
    }
    data = _http_get_json(f"https://finnhub.io/api/v1/company-news?{urlencode(params)}")
    if isinstance(data, dict) and "_error" in data:
        return [data]
    if not isinstance(data, list):
        return []
    out = []
    for n in data[:25]:
        out.append({
            "date": datetime.fromtimestamp(n.get("datetime", 0)).date().isoformat() if n.get("datetime") else "?",
            "headline": n.get("headline"),
            "source": n.get("source"),
            "url": n.get("url"),
            "summary": (n.get("summary") or "")[:300],
            "category": n.get("category"),
        })
    return out


def fetch_finnhub_recommendation_trends(ticker: str) -> list[dict]:
    """FINNHUB recommendation-trends（analyst buy/sell/hold 月度演变）"""
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return []
    sym = _finnhub_symbol(ticker)
    data = _http_get_json(
        f"https://finnhub.io/api/v1/stock/recommendation?symbol={sym}&token={key}"
    )
    if not isinstance(data, list):
        return []
    return data[:4]  # 最近 4 个月


def fetch_finnhub_insider_transactions(ticker: str, *, days: int = 30) -> list[dict]:
    """FINNHUB insider-transactions（高管/董事买卖）"""
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return []
    today = datetime.now().date()
    sym = _finnhub_symbol(ticker)
    params = {
        "symbol": sym,
        "from": (today - timedelta(days=days)).isoformat(),
        "to": today.isoformat(),
        "token": key,
    }
    data = _http_get_json(f"https://finnhub.io/api/v1/stock/insider-transactions?{urlencode(params)}")
    if not isinstance(data, dict):
        return []
    raw = data.get("data") or []
    return raw[:20]


# ───────── FMP API ─────────

def fetch_fmp_grades(ticker: str, *, limit: int = 10) -> list[dict]:
    """FMP grades（analyst rating 升降级历史）"""
    key = os.environ.get("FMP_API_KEY")
    if not key:
        return []
    sym = _finnhub_symbol(ticker)
    url = f"https://financialmodelingprep.com/api/v3/grade/{sym}?limit={limit}&apikey={key}"
    data = _http_get_json(url)
    if not isinstance(data, list):
        return []
    return data[:limit]


def fetch_fmp_press_releases(ticker: str, *, limit: int = 10) -> list[dict]:
    """FMP press releases（公司新闻稿）"""
    key = os.environ.get("FMP_API_KEY")
    if not key:
        return []
    sym = _finnhub_symbol(ticker)
    url = f"https://financialmodelingprep.com/api/v3/press-releases/{sym}?limit={limit}&apikey={key}"
    data = _http_get_json(url)
    if not isinstance(data, list):
        return []
    return data[:limit]


# ───────── catalyst 分类 + 简报 ─────────

_BULLISH_HINTS = ["raise", "upgrade", "outperform", "buy", "overweight", "beat", "soar", "surge",
                  "increase", "boost", "growth", "expand", "partnership", "deal", "acquisition"]
_BEARISH_HINTS = ["cut", "downgrade", "underperform", "sell", "underweight", "miss", "plunge", "tumble",
                  "drop", "fall", "decline", "concern", "lawsuit", "investigation", "probe", "ban",
                  "tariff", "fraud"]


def _sentiment_of(text: str) -> str:
    t = (text or "").lower()
    bull = sum(1 for w in _BULLISH_HINTS if w in t)
    bear = sum(1 for w in _BEARISH_HINTS if w in t)
    if bear > bull:
        return "bearish"
    if bull > bear:
        return "bullish"
    return "neutral"


def scan_one_ticker(ticker: str, name: str) -> dict:
    """对单只 ticker 跑完整 catalyst 扫描。"""
    print(f"  · 拉 news / rating / insider / grades / press release...")
    news = fetch_finnhub_news(ticker, days=7)
    rec_trends = fetch_finnhub_recommendation_trends(ticker)
    insiders = fetch_finnhub_insider_transactions(ticker, days=30)
    grades = fetch_fmp_grades(ticker, limit=10)
    pr = fetch_fmp_press_releases(ticker, limit=10)
    time.sleep(0.5)  # 限流

    # ── 分类成 4 个 catalyst 桶
    ownership_action = []
    fund_flow = []
    ip_marketing = []
    regulatory_risk = []

    # insider transactions → ownership_action
    for it in insiders[:8]:
        change = it.get("change") or 0
        name_field = it.get("name") or it.get("insiderName") or "—"
        date = it.get("transactionDate") or "?"
        direction = "增持" if change > 0 else "减持" if change < 0 else "调整"
        event = f"{name_field} {direction} {abs(change):,} 股 @ {it.get('transactionPrice', '?')} ({it.get('transactionCode', '?')})"
        ownership_action.append({
            "date": date,
            "event": event,
            "source_url": "finnhub:insider",
            "sentiment": "bullish" if change > 0 else "bearish" if change < 0 else "neutral",
        })

    # FINNHUB recommendation trends → ownership_action（评级共识演变）
    if rec_trends:
        latest = rec_trends[0]
        prev = rec_trends[1] if len(rec_trends) > 1 else None
        if prev:
            buy_delta = (latest.get("buy") or 0) + (latest.get("strongBuy") or 0) \
                - ((prev.get("buy") or 0) + (prev.get("strongBuy") or 0))
            sell_delta = (latest.get("sell") or 0) + (latest.get("strongSell") or 0) \
                - ((prev.get("sell") or 0) + (prev.get("strongSell") or 0))
            if abs(buy_delta) >= 1 or abs(sell_delta) >= 1:
                ownership_action.append({
                    "date": latest.get("period"),
                    "event": f"Analyst 评级共识：Buy {buy_delta:+d} / Sell {sell_delta:+d}（{latest.get('period')} vs {prev.get('period')}）",
                    "source_url": "finnhub:recommendation_trends",
                    "sentiment": "bullish" if buy_delta > 0 else "bearish" if sell_delta > 0 else "neutral",
                })

    # FMP grades → ownership_action（rating change）
    for g in grades[:6]:
        date = g.get("date") or "?"
        firm = g.get("gradingCompany") or "—"
        prev_g = g.get("previousGrade") or "—"
        new_g = g.get("newGrade") or "—"
        action_g = g.get("action") or "—"
        # 只保留最近 30 天
        try:
            d = datetime.strptime(date[:10], "%Y-%m-%d").date()
            if (datetime.now().date() - d).days > 30:
                continue
        except Exception:
            continue
        sent = "bullish" if any(w in (new_g + " " + action_g).lower() for w in ["buy", "outperform", "overweight"]) \
            else "bearish" if any(w in (new_g + " " + action_g).lower() for w in ["sell", "underperform", "underweight"]) \
            else "neutral"
        ownership_action.append({
            "date": date,
            "event": f"{firm}：{prev_g} → {new_g}（{action_g}）",
            "source_url": "fmp:grades",
            "sentiment": sent,
        })

    # FINNHUB news 分桶
    for n in news[:15]:
        if "_error" in n:
            continue
        text = (n.get("headline") or "") + " " + (n.get("summary") or "")
        sent = _sentiment_of(text)
        entry = {
            "date": n.get("date"),
            "event": n.get("headline"),
            "source_url": n.get("url"),
            "source": n.get("source"),
            "sentiment": sent,
        }
        # 简单关键词路由
        t_lower = text.lower()
        if any(w in t_lower for w in ["tariff", "lawsuit", "investigation", "probe", "regulator",
                                      "sec", "dhs", "esg", "forced labor", "sanction"]):
            regulatory_risk.append(entry)
        elif any(w in t_lower for w in ["partnership", "launch", "release", "collab", "deal",
                                        "expand", "store", "open", "product", "world cup", "fifa",
                                        "movie", "ip"]):
            ip_marketing.append(entry)
        elif any(w in t_lower for w in ["fund", "etf", "flow", "stake", "13d", "13g", "13f",
                                        "ownership", "shareholder"]):
            fund_flow.append(entry)
        else:
            ip_marketing.append(entry)

    # FMP press releases → ip_marketing（公司官方稿）
    for p in pr[:6]:
        date = p.get("date") or "?"
        try:
            d = datetime.strptime(date[:10], "%Y-%m-%d").date()
            if (datetime.now().date() - d).days > 14:
                continue
        except Exception:
            continue
        title = p.get("title") or "—"
        ip_marketing.append({
            "date": date,
            "event": title,
            "source_url": "fmp:press_release",
            "source": "公司新闻稿",
            "sentiment": _sentiment_of(title + " " + (p.get("text") or "")),
        })

    # ── 总结
    counts = {
        "ownership_action": len(ownership_action),
        "fund_flow": len(fund_flow),
        "ip_marketing": len(ip_marketing),
        "regulatory_risk": len(regulatory_risk),
    }
    bull = sum(1 for c in (ownership_action + fund_flow + ip_marketing + regulatory_risk)
               if c.get("sentiment") == "bullish")
    bear = sum(1 for c in (ownership_action + fund_flow + ip_marketing + regulatory_risk)
               if c.get("sentiment") == "bearish")

    if bull > bear and bull >= 2:
        direction_hint = "lean_bullish"
    elif bear > bull and bear >= 2:
        direction_hint = "lean_bearish"
    else:
        direction_hint = "neutral"

    summary_3_lines = (
        f"利好 {bull} 条 / 利空 {bear} 条 / 总 {sum(counts.values())} 条 catalyst（7-30d 窗口）；"
        f"维度分布 持仓 {counts['ownership_action']} 资金 {counts['fund_flow']} "
        f"IP {counts['ip_marketing']} 监管 {counts['regulatory_risk']}；"
        f"⚠️ MVP v2 限制：港股中文 catalyst / 南向资金 / 段永平类 中文圈持仓 拉不到。"
    )

    return {
        "ticker": ticker,
        "name": name,
        "scanned_at": datetime.now().isoformat(),
        "scan_window_days": 7,
        "data_sources": ["FINNHUB:company-news", "FINNHUB:recommendation-trends",
                         "FINNHUB:insider-transactions", "FMP:grades", "FMP:press-releases"],
        "limitations": [
            "港股中文新闻（雪球/新浪/AASTOCKS）未接入",
            "南向资金 / 港股通持股 未接入",
            "中文圈明星投资人持仓（段永平/巴菲特中概仓）未接入",
            "HKEX 中文披露 未接入",
        ],
        "counts": counts,
        "categories": {
            "ownership_action": ownership_action,
            "fund_flow": fund_flow,
            "ip_marketing": ip_marketing,
            "regulatory_risk": regulatory_risk,
        },
        "summary_3_lines": summary_3_lines,
        "advisory_override": {
            "should_overlay_factor_score": direction_hint != "neutral",
            "direction_hint": direction_hint,
            "rationale": f"{bull} 条利好 vs {bear} 条利空，倾向 {direction_hint}",
        },
    }


def _print_result(result: dict) -> None:
    print()
    print("=" * 60)
    print(f"📊 {result.get('name')}（{result.get('ticker')}）catalyst 扫描结果")
    print("=" * 60)
    cats = result.get("categories") or {}
    for label, key in [
        ("👥 大股东 / 评级 / Insider", "ownership_action"),
        ("💰 资金面", "fund_flow"),
        ("🎨 IP / Marketing", "ip_marketing"),
        ("⚠️  监管 / Tail Risk", "regulatory_risk"),
    ]:
        events = cats.get(key) or []
        print(f"\n{label}（{len(events)} 条）")
        for ev in events[:6]:
            sent = ev.get("sentiment", "?")
            emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "🟡"}.get(sent, "⚪")
            print(f"  {emoji} {ev.get('date')} · {ev.get('event')}")
    print()
    print(f"📝 三行总结：")
    print(f"   {result.get('summary_3_lines', '—')}")
    print()
    adv = result.get("advisory_override") or {}
    print(f"🎯 对 factor 评分的覆盖建议：")
    print(f"   方向 = {adv.get('direction_hint')} / 应覆盖 = {adv.get('should_overlay_factor_score')}")
    print(f"   理由：{adv.get('rationale')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="持仓票 catalyst 每日扫描 MVP v2（FINNHUB+FMP）")
    parser.add_argument("--ticker", required=True, help="股票代码，如 9992.HK")
    parser.add_argument("--name", required=True, help="股票名称，如 泡泡玛特")
    parser.add_argument("--out", default=str(OUT_PATH))
    args = parser.parse_args()

    _load_dotenv(REPO / ".env")
    if not os.environ.get("FINNHUB_API_KEY") and not os.environ.get("FMP_API_KEY"):
        print("✗ FINNHUB_API_KEY / FMP_API_KEY 都未设置", file=sys.stderr)
        return 1

    print(f"扫描 {args.name}（{args.ticker}）...")
    result = scan_one_ticker(args.ticker, args.name)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ {out_path}")

    _print_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
