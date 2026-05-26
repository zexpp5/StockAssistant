"""美股 SEC Form 4 内部人交易事件日历。

Form 4 = 上市公司董事 / 高管 / 10%+ 股东向 SEC 申报的股票买卖。
学术依据：
  - Lakonishok & Lee (2001): 内部人净买入 → 中性偏多
  - Cohen et al. (2012): "Opportunistic Insider Trading" 含 alpha
  - 集中卖出（含高管 + 董事同时卖）→ 警示信号

数据源：data.sec.gov/submissions/CIK*.json 拿 form="4" 列表
       sec.gov/Archives/edgar/data/{cik}/{acc}/{primaryDoc} 拉 XML

聚合规则（每只 ticker 过去 60 天）：
  - 只数 transactionCode = "P" (买入) / "S" (卖出)；A/D/M/G 排除（期权授予/行权 不算买卖）
  - net_amount_usd = Σ(P × shares × price) - Σ(S × shares × price)
  - 信号触发：|net_amount| ≥ $1M 写入 events
  - 输出 event_type ∈ {insider_net_buy, insider_net_sell}

第二版（TODO）：按 insider 角色加权（CEO/CFO > 其他高管 > 董事），
区分"opportunistic"（无即将披露的财报）vs "routine"。
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


USER_AGENT = "LinearV Research lance7in@gmail.com"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

LOOKBACK_DAYS = 60
NET_THRESHOLD_USD = 1_000_000  # 净额 ≥ $1M 才触发


def _load_ticker_to_cik(session) -> dict[str, int]:
    """复用 C9 的逻辑（同 7 天缓存）。"""
    cache = REPO / "data" / "cache" / "sec_company_tickers.json"
    if cache.exists():
        age = (datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime)).days
        if age < 7:
            data = json.loads(cache.read_text(encoding="utf-8"))
            return {v["ticker"]: int(v["cik_str"]) for v in data.values()}
    try:
        r = session.get(TICKERS_URL, headers={"User-Agent": USER_AGENT}, timeout=15)
        r.raise_for_status()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(r.json(), ensure_ascii=False), encoding="utf-8")
        return {v["ticker"]: int(v["cik_str"]) for v in r.json().values()}
    except Exception as e:
        logger.error("SEC tickers 拉取失败: %s", e)
        return {}


def _fetch_form4_list(session, cik: int, lookback_days: int) -> list[dict]:
    """拿 CIK 最近 lookback_days 的所有 form=4 filings 元数据。"""
    try:
        r = session.get(SUBMISSIONS_URL.format(cik=cik), headers={"User-Agent": USER_AGENT}, timeout=15)
        if r.status_code != 200:
            return []
        recent = (r.json().get("filings") or {}).get("recent") or {}
    except Exception as e:
        logger.warning("submissions fetch CIK=%s err: %s", cik, e)
        return []
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accessions = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    today = date.today()
    cutoff = today - timedelta(days=lookback_days)
    out = []
    for i, form in enumerate(forms):
        if form != "4":
            continue
        try:
            fdate = datetime.strptime(dates[i], "%Y-%m-%d").date()
        except Exception:
            continue
        if fdate < cutoff:
            continue
        acc = accessions[i] if i < len(accessions) else ""
        doc = docs[i] if i < len(docs) else ""
        if not acc or not doc:
            continue
        out.append({"date": fdate, "accession": acc, "doc": doc})
    return out


def _parse_form4_xml(session, cik: int, accession: str, primary_doc: str) -> dict | None:
    """单个 Form 4 XML → {insider, title, txs: [{code, shares, price, ad}]}。"""
    acc_nodash = accession.replace("-", "")
    # primary_doc 通常已经是文件名，例如 wk-form4_xxx.xml；如果含子路径(xslF345X06/...)，
    # 实际 XML 在同目录平铺（xslF345X06/ 是 stylesheet 路径，干掉）
    doc_name = primary_doc.split("/")[-1] if "/" in primary_doc else primary_doc
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc_name}"
    try:
        r = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        if r.status_code != 200 or not r.text.strip().startswith("<"):
            return None
        root = ET.fromstring(r.text)
    except Exception as e:
        logger.debug("form4 parse fail %s: %s", url, e)
        return None

    # 多个 reportingOwner 时取第一个
    insider_name = None
    title = None
    for own in root.findall("reportingOwner"):
        insider_name = own.findtext("reportingOwnerId/rptOwnerName") or insider_name
        rel = own.find("reportingOwnerRelationship")
        if rel is not None:
            is_dir = (rel.findtext("isDirector") or "").strip()
            is_off = (rel.findtext("isOfficer") or "").strip()
            officer_title = (rel.findtext("officerTitle") or "").strip()
            if not title:
                if officer_title:
                    title = officer_title
                elif is_dir == "1":
                    title = "Director"
                elif is_off == "1":
                    title = "Officer"
        if insider_name and title:
            break

    txs = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = tx.findtext("transactionCoding/transactionCode") or ""
        ad = tx.findtext("transactionAmounts/transactionAcquiredDisposedCode/value") or ""
        shares_s = tx.findtext("transactionAmounts/transactionShares/value")
        price_s = tx.findtext("transactionAmounts/transactionPricePerShare/value")
        try:
            shares = float(shares_s) if shares_s else 0
            price = float(price_s) if price_s else 0
        except ValueError:
            continue
        txs.append({"code": code, "ad": ad, "shares": shares, "price": price})
    return {"insider": insider_name or "Unknown", "title": title or "", "txs": txs}


def _gather_universe() -> dict[str, str]:
    """跟 C9 相同 universe。"""
    out: dict[str, str] = {}
    try:
        import duckdb
        db_path = REPO / "stock_history_v2.duckdb"
        if db_path.exists():
            con = duckdb.connect(str(db_path), read_only=True)
            try:
                rows = con.execute("SELECT symbol, name FROM system_universe WHERE market = 'US'").fetchall()
                for sym, name in rows:
                    if sym:
                        out[sym.upper()] = name or ""
            finally:
                con.close()
    except Exception:
        pass
    td = REPO / "data" / "latest" / "trade_delta.json"
    if td.exists():
        try:
            d = json.loads(td.read_text(encoding="utf-8"))
            for bucket in ("buys", "sells", "holds"):
                for item in (d.get(bucket) or []):
                    t = (item.get("ticker") or "").upper()
                    if t and not any(t.endswith(s) for s in (".HK", ".SS", ".SZ", ".BJ")):
                        out.setdefault(t, item.get("name", ""))
        except Exception:
            pass
    return out


def main() -> int:
    try:
        import requests
    except ImportError:
        logger.error("pip install requests")
        return 2

    session = requests.Session()
    ticker_to_cik = _load_ticker_to_cik(session)
    if not ticker_to_cik:
        return 2
    universe = _gather_universe()
    logger.info("覆盖 %d 只美股 ticker", len(universe))

    events: list[dict] = []
    hit, miss, errored = 0, [], []

    for ticker, name in sorted(universe.items()):
        cik = ticker_to_cik.get(ticker.upper()) or ticker_to_cik.get(ticker.replace("-", "").upper())
        if not cik:
            errored.append(ticker)
            continue
        form4s = _fetch_form4_list(session, cik, LOOKBACK_DAYS)
        if not form4s:
            miss.append(ticker)
            time.sleep(0.12)
            continue

        # 聚合：所有 Form 4 → 60 天净金额
        buys_usd = 0.0
        sells_usd = 0.0
        n_buys = 0
        n_sells = 0
        recent_insiders: list[dict] = []
        latest_date: date | None = None

        for f in form4s:
            parsed = _parse_form4_xml(session, cik, f["accession"], f["doc"])
            time.sleep(0.12)
            if not parsed:
                continue
            tx_buys = 0.0
            tx_sells = 0.0
            for tx in parsed["txs"]:
                amt = tx["shares"] * tx["price"]
                if tx["code"] == "P":
                    tx_buys += amt
                elif tx["code"] == "S":
                    tx_sells += amt
            buys_usd += tx_buys
            sells_usd += tx_sells
            if tx_buys > 0:
                n_buys += 1
            if tx_sells > 0:
                n_sells += 1
            if tx_buys + tx_sells > 0:
                recent_insiders.append({
                    "date": f["date"].isoformat(),
                    "insider": parsed["insider"],
                    "title": parsed["title"],
                    "buy_usd": round(tx_buys, 0),
                    "sell_usd": round(tx_sells, 0),
                })
                latest_date = max(latest_date or f["date"], f["date"])

        net_usd = buys_usd - sells_usd
        # 触发条件：|净额| ≥ 阈值
        if abs(net_usd) >= NET_THRESHOLD_USD and latest_date is not None:
            etype = "insider_net_buy" if net_usd > 0 else "insider_net_sell"
            recent_insiders.sort(key=lambda x: x["date"], reverse=True)
            events.append({
                "ticker": ticker,
                "name": name,
                "cik": cik,
                "event_date": latest_date.isoformat(),
                "event_type": etype,
                "net_amount_usd": round(net_usd, 0),
                "buys_usd": round(buys_usd, 0),
                "sells_usd": round(sells_usd, 0),
                "n_buy_filings": n_buys,
                "n_sell_filings": n_sells,
                "n_form4_total": len(form4s),
                "recent_insiders": recent_insiders[:6],
                "source": "sec.gov Form 4",
            })
        hit += 1

    events.sort(key=lambda e: abs(e.get("net_amount_usd") or 0), reverse=True)

    from collections import Counter
    type_counts = Counter(e["event_type"] for e in events)

    payload = {
        "generated_at": datetime.now().isoformat(),
        "n_tickers": len(universe),
        "n_signals": len(events),
        "lookback_days": LOOKBACK_DAYS,
        "net_threshold_usd": NET_THRESHOLD_USD,
        "coverage": {
            "hit": hit, "miss": len(miss), "errored": len(errored),
            "miss_tickers": miss[:20], "errored_tickers": errored[:20],
        },
        "by_event_type": dict(type_counts),
        "events": events,
    }

    out = REPO / "data" / "event_calendar_us_form4.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✅ Form 4 内部人事件已写入 {out}")
    print(f"   tickers: {len(universe)} (hit {hit} / miss {len(miss)} / err {len(errored)})")
    print(f"   signals: {len(events)} (净额 ≥ ${NET_THRESHOLD_USD:,})")
    for t, n in type_counts.most_common():
        print(f"   {t:24s} {n}")
    # 显示 top 5
    if events:
        print(f"\n   净额前 5:")
        for e in events[:5]:
            direction = "净买入" if e["event_type"] == "insider_net_buy" else "净卖出"
            print(f"   {e['ticker']:8s} {direction} ${abs(e['net_amount_usd'])/1e6:.1f}M (60d, {e['n_buy_filings']}买 / {e['n_sell_filings']}卖)")
    return 0 if hit > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
