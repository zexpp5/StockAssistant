"""Watchlist 数据增强 job：akshare + Google Trends + Finnhub 多源补全。

按市场路由：
  - 美股 → finnhub (新闻/内部人/分析师) + Google Trends
  - A 股 / 港股 → akshare (实时报价/财务/资金流) + Google Trends

输出：
  - JSON 快照存 SNAPSHOT_DIR/enrich/
  - 写回飞书 watchlist 的「数据来源」「信息构成」字段（追加多源标注）

CLI：
  python3 -m stock_research.jobs.enrich_watchlist                # 全量
  python3 -m stock_research.jobs.enrich_watchlist --code NVDA    # 单只
  python3 -m stock_research.jobs.enrich_watchlist --skip-trends  # 跳过慢的 trends

Web 部署时直接 import：
  from stock_research.jobs.enrich_watchlist import enrich_one
"""
from __future__ import annotations
import argparse
import logging
import sys
import time
from datetime import datetime
from typing import Any

from .. import config
from ..core import akshare_client, finnhub_client, trends, baostock_client
from ..adapters import feishu, store

logger = logging.getLogger("stock_research.jobs.enrich_watchlist")


def _is_us_stock(market: str, code: str) -> bool:
    if "美股" in market:
        return True
    return bool(code) and code.replace("-", "").replace(".", "").isalpha()


def enrich_one(name: str, code: str, market: str,
               do_trends: bool = True, do_finnhub: bool = True,
               do_akshare: bool = True, do_baostock: bool = True) -> dict[str, Any]:
    """对一只股票做一站式 enrichment。返回完整 dict（已 JSON 可序列化）。"""
    out: dict[str, Any] = {
        "name": name,
        "code": code,
        "market": market,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "sources_used": [],
    }

    if _is_us_stock(market, code):
        if do_finnhub and finnhub_client.is_available():
            fh = finnhub_client.fetch_enriched(code)
            if fh and fh.get("finnhub"):
                out["finnhub"] = fh["finnhub"]
                out["sources_used"].append("finnhub")
        if do_trends:
            tr = trends.fetch_trend(name if any('一' <= c <= '鿿' for c in name) else code, geo="")
            if tr:
                out["trends"] = tr
                out["sources_used"].append("trends")
    else:
        if do_akshare:
            ak = akshare_client.fetch_enriched(code, market)
            if ak.get("akshare"):
                out["akshare"] = ak["akshare"]
                out["sources_used"].append("akshare")
        # A 股二源：baostock（免费、官方接口，给 akshare cross-check）
        if do_baostock and "A股" in market:
            bs_quote = baostock_client.fetch_a_share_quote(code)
            if bs_quote:
                out["baostock"] = bs_quote
                out["sources_used"].append("baostock")
        if do_trends:
            tr = trends.fetch_trend(name, geo="CN" if "A股" in market else "")
            if tr:
                out["trends"] = tr
                out["sources_used"].append("trends")

    return out


def _format_for_feishu(enriched: dict[str, Any]) -> dict[str, str]:
    """把 enrichment 结果压成飞书可写的字段字典。

    主要写两个字段：
      - 信息构成：多源摘要（人类可读）
      - 数据来源：URL 列表
    """
    lines = []
    sources = []

    finn = enriched.get("finnhub") or {}
    if finn.get("insider"):
        ins = finn["insider"]
        lines.append(f"🧑‍💼 内部人交易（90天）: 共 {ins['count']} 笔，买 {ins['buy_count']} / 卖 {ins['sell_count']}，净 {ins['net_shares']:,} 股 [Finnhub]")
        sources.append("Finnhub stock_insider_transactions")
    if finn.get("analyst_recommendations"):
        rec = finn["analyst_recommendations"]
        lines.append(f"📊 分析师评级（{rec.get('period')}）: 强买 {rec['strong_buy']} / 买 {rec['buy']} / 持有 {rec['hold']} / 卖 {rec['sell']} / 强卖 {rec['strong_sell']} [Finnhub]")
        sources.append("Finnhub recommendation_trends")
    if finn.get("price_target"):
        pt = finn["price_target"]
        lines.append(f"🎯 分析师目标价: 中位 ${pt.get('target_median')} / 均值 ${pt.get('target_mean')} / 区间 ${pt.get('target_low')} - ${pt.get('target_high')} [Finnhub]")
        sources.append("Finnhub price_target")
    if finn.get("news"):
        n = finn["news"]
        if n:
            lines.append(f"📰 近 7 天新闻 {len(n)} 条，最新：{n[0].get('headline', '')[:80]} [Finnhub]")
            sources.append("Finnhub company_news")

    ak = enriched.get("akshare") or {}
    if ak.get("quote"):
        q = ak["quote"]
        lines.append(f"💹 akshare 实时: {q.get('name','')} 价 {q.get('price')} 元 / 涨幅 {q.get('change_pct')}% / PE {q.get('pe_ttm')} / PB {q.get('pb')} [akshare 东财]")
        sources.append(q.get("source", "akshare"))
    if ak.get("north_flow"):
        nf = ak["north_flow"]
        if nf.get("shares_held_pct") is not None:
            lines.append(f"🇨🇳 北向持股: {nf.get('shares_held_pct'):.2f}% （{nf.get('date')}）[akshare/沪深港通]")
            sources.append(nf.get("source", "akshare/north"))
    if ak.get("southbound_flow"):
        sb = ak["southbound_flow"]
        if sb.get("shares_pct") is not None:
            lines.append(f"🇭🇰 南向持股占比: {sb.get('shares_pct'):.2f}% [akshare/港股通]")
            sources.append(sb.get("source", "akshare/southbound"))

    tr = enriched.get("trends")
    if tr:
        emoji = "🔥" if tr.get("trend_pct", 0) > 30 else ("📈" if tr.get("trend_pct", 0) > 0 else "📉")
        lines.append(f"{emoji} Google Trends（{tr.get('timeframe')} · {tr.get('geo')}）: 平均 {tr.get('avg')} / 最近 {tr.get('last')} / 趋势 {tr.get('trend_pct'):+.1f}% [Google Trends]")
        sources.append("Google Trends (pytrends)")

    if not lines:
        return {}

    info_text = "\n".join(lines)
    info_text += f"\n\n⏰ 多源同步：{enriched.get('fetched_at')}"

    source_text = "\n".join(f"· {s}" for s in dict.fromkeys(sources))

    return {
        config.Fields.INFO_COMPOSITION: info_text,
        config.Fields.DATA_SOURCE: source_text,
        config.Fields.SNAPSHOT_DATE: feishu.ts_today_ms(),
    }


def run_all(only_code: str | None = None, do_trends: bool = True,
            do_finnhub: bool = True, do_akshare: bool = True,
            do_baostock: bool = True,
            sleep_sec: float = 1.0) -> dict[str, Any]:
    watchlist = feishu.fetch_watchlist()
    print(f"[enrich] watchlist {len(watchlist)} 只")

    results = []
    feishu_updates = []
    for w in watchlist:
        code = w["normalized"]["code"]
        name = w["normalized"]["name"]
        market = w["normalized"]["market"]
        if only_code and only_code != code:
            continue
        if not code or not name:
            continue
        print(f"  → {name} ({code}, {market or '?'})")
        try:
            enriched = enrich_one(name, code, market,
                                  do_trends=do_trends, do_finnhub=do_finnhub,
                                  do_akshare=do_akshare, do_baostock=do_baostock)
        except Exception as e:
            logger.warning("enrich_one failed for %s: %s", code, e)
            continue
        results.append(enriched)
        fields = _format_for_feishu(enriched)
        if fields:
            feishu_updates.append({"record_id": w["record_id"], "fields": fields})
            print(f"     ✓ 多源 [{', '.join(enriched['sources_used']) or '无'}]")
        time.sleep(sleep_sec)

    if results:
        store.save_json(results, config.ENRICH_DIR, "watchlist")

    # 写飞书
    write_summary = feishu.batch_update(feishu_updates) if feishu_updates else {"success": 0, "failed": 0}

    print(f"\n[enrich] 抓取 {len(results)} / 写飞书 {write_summary['success']} 成功 / {write_summary['failed']} 失败")
    return {
        "fetched": len(results),
        **write_summary,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    p = argparse.ArgumentParser(description="Watchlist multi-source enrichment")
    p.add_argument("--code", help="只跑某只股票")
    p.add_argument("--skip-trends", action="store_true", help="跳过 Google Trends（慢）")
    p.add_argument("--skip-finnhub", action="store_true", help="跳过 Finnhub")
    p.add_argument("--skip-akshare", action="store_true", help="跳过 akshare")
    p.add_argument("--skip-baostock", action="store_true", help="跳过 A 股 baostock 二源")
    args = p.parse_args()
    run_all(
        only_code=args.code,
        do_trends=not args.skip_trends,
        do_finnhub=not args.skip_finnhub,
        do_akshare=not args.skip_akshare,
        do_baostock=not args.skip_baostock,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
