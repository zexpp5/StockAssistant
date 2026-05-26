"""美股 SEC EDGAR 事件日历刷新。

补 yfinance 拿不到的非财报催化（重大事件 8-K / 大股东持仓 13G/13D）。
Form 4（内部人交易）由独立 collector 处理 (C10 todo)。

数据源：
  - https://www.sec.gov/files/company_tickers.json    ticker → CIK 映射
  - https://data.sec.gov/submissions/CIK{cik}.json    单个公司最近 1000 filings

EDGAR Rate Limit: 10 req/s，User-Agent 必填（含联系邮箱）。

第一版 MVP：
  · form == "8-K"     → event_type="material_event"（重大事件公告）
  · form 含 "SC 13G"  → event_type="passive_holder_change"（被动 5%+ 持仓）
  · form 含 "SC 13D"  → event_type="active_holder_change"（主动 5%+ 持仓，往往是收购信号）
  · form 含 "DEF 14A" → event_type="proxy"（股东大会）
  · 其他 form 暂时不接

第二版（TODO）：拉 8-K 全文解析 Item 1.01/2.01/5.02/8.01 等具体事件类型，
让 catalyst 句更精准。
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


USER_AGENT = "LinearV Research lance7in@gmail.com"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# Form → event_type
FORM_CLASSIFY = {
    "8-K":      "material_event",
    "8-K/A":    "material_event",
    "SC 13G":   "passive_holder_change",
    "SC 13G/A": "passive_holder_change",
    "SC 13D":   "active_holder_change",
    "SC 13D/A": "active_holder_change",
    "DEF 14A":  "proxy",
    "DEFA14A":  "proxy",
}

# 8-K Item 编号 → 中文标签 + 信号强度（priority 越小越强）
ITEM_8K_LABELS = {
    "1.01": ("📜 重大协议",       1),  # Entry into Material Definitive Agreement
    "1.02": ("📜 终止重大协议",    2),  # Termination of Agreement
    "1.03": ("⚠️ 破产",          1),  # Bankruptcy
    "2.01": ("🤝 收购完成",       1),  # Completion of Acquisition
    "2.02": ("📋 财报",          3),  # Results of Operations (季报常发,信息量低)
    "2.05": ("⚠️ 裁员/退出",      2),  # Exit or Disposal Activities
    "2.06": ("⚠️ 资产减值",       2),  # Material Impairments
    "3.01": ("🛑 退市通知",       1),  # Delisting Notice
    "5.02": ("👤 高管变动",       2),  # Officer Departure / New Hire
    "5.03": ("📜 章程修订",       4),
    "5.07": ("🗳️ 股东表决结果",   4),  # Shareholder Vote Results (proxy 后)
    "7.01": ("📣 重大披露",       2),  # Regulation FD Disclosure
    "8.01": ("📰 其他事件",       3),  # Other Events (含 PR-style 新闻)
    "9.01": ("📎 财报附件",       5),  # Financial Statements (跟 2.02 一起出现,降级)
}

# event_type → label（用于 catalyst 句前缀）
EVENT_LABELS = {
    "material_event":         "📣 重大事件公告",
    "passive_holder_change":  "👥 大股东变动 (被动)",
    "active_holder_change":   "🎯 大股东变动 (主动·常含收购意图)",
    "proxy":                  "🗳️ 股东大会",
}


def _load_ticker_to_cik(session) -> dict[str, int]:
    """从 SEC 拉 ticker→CIK 映射，缓存在 data/cache/。"""
    cache = REPO / "data" / "cache" / "sec_company_tickers.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    # 缓存有效期 7 天
    if cache.exists():
        age = (datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime)).days
        if age < 7:
            data = json.loads(cache.read_text(encoding="utf-8"))
            return {v["ticker"]: int(v["cik_str"]) for v in data.values()}
    # 重新拉
    try:
        r = session.get(TICKERS_URL, headers={"User-Agent": USER_AGENT}, timeout=15)
        r.raise_for_status()
        data = r.json()
        cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return {v["ticker"]: int(v["cik_str"]) for v in data.values()}
    except Exception as e:
        logger.error("SEC ticker file 拉取失败: %s", e)
        if cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            return {v["ticker"]: int(v["cik_str"]) for v in data.values()}
        return {}


_ITEM_RE = __import__("re").compile(r"Item\s+(\d+\.\d+)", __import__("re").IGNORECASE)


def _fetch_8k_items(session, url: str) -> list[str]:
    """拉 8-K HTML 解析含哪些 Items。返回排序后的 Item 编号列表。"""
    if not url:
        return []
    try:
        r = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        if r.status_code != 200:
            return []
        # 8-K HTML 里 "Item 1.01" / "Item 5.02" 等会在 section header 重复出现
        items = set(_ITEM_RE.findall(r.text))
        return sorted(items)
    except Exception:
        return []


def _8k_best_item_label(items: list[str]) -> tuple[str, int]:
    """从 8-K items 列表里挑 priority 最小（最强）的，返回 (label, priority)。"""
    if not items:
        return ("", 9)
    best = (9, "")
    for it in items:
        label, prio = ITEM_8K_LABELS.get(it, (f"Item {it}", 6))
        if prio < best[0]:
            best = (prio, label)
    return (best[1], best[0])


def _fetch_filings(session, cik: int, lookback_days: int = 60) -> list[dict]:
    """拉 CIK 最近 lookback_days 的 filings（过滤到我们关心的 form 类型）。"""
    try:
        r = session.get(
            SUBMISSIONS_URL.format(cik=cik),
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception as e:
        logger.warning("filings fetch CIK=%s err: %s", cik, e)
        return []

    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accessions = recent.get("accessionNumber") or []
    descs = recent.get("primaryDocDescription") or []
    docs = recent.get("primaryDocument") or []

    today = date.today()
    cutoff = today - timedelta(days=lookback_days)
    out: list[dict] = []
    for i, form in enumerate(forms):
        etype = FORM_CLASSIFY.get(form)
        if not etype:
            continue
        try:
            fdate = datetime.strptime(dates[i], "%Y-%m-%d").date()
        except Exception:
            continue
        if fdate < cutoff:
            continue
        accession = accessions[i] if i < len(accessions) else ""
        primary_doc = docs[i] if i < len(docs) else ""
        desc = descs[i] if i < len(descs) else ""
        # Filing 详情页 URL
        if accession:
            acc_no_dash = accession.replace("-", "")
            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no_dash}/{primary_doc}" if primary_doc \
                else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form}"
        else:
            filing_url = ""
        out.append({
            "event_date": fdate.isoformat(),
            "event_type": etype,
            "form": form,
            "title": desc or form,
            "filing_url": filing_url,
            "accession": accession,
        })
    return out


def _gather_universe() -> dict[str, str]:
    """ticker → name；合并 DuckDB system_universe[US] + trade_delta + plan_a_v5。"""
    out: dict[str, str] = {}
    try:
        import duckdb
        db_path = REPO / "stock_history_v2.duckdb"
        if db_path.exists():
            con = duckdb.connect(str(db_path), read_only=True)
            try:
                rows = con.execute(
                    "SELECT symbol, name FROM system_universe WHERE market = 'US'"
                ).fetchall()
                for sym, name in rows:
                    if sym:
                        out[sym.upper()] = name or ""
            finally:
                con.close()
    except Exception as e:
        logger.warning("DuckDB universe 加载失败: %s", e)

    # trade_delta.json 美股 ticker
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
        logger.error("SEC ticker mapping 缺失，无法继续")
        return 2

    universe = _gather_universe()
    logger.info("覆盖 %d 只美股 ticker", len(universe))

    events: list[dict] = []
    hit, miss, errored = 0, [], []

    for ticker, name in sorted(universe.items()):
        cik = ticker_to_cik.get(ticker.upper())
        # BRK-B 这种带破折号的，SEC 用「BRK-B」原样；如果直接查不到尝试去掉破折号
        if not cik:
            cik = ticker_to_cik.get(ticker.replace("-", "").upper())
        if not cik:
            errored.append(ticker)
            continue
        filings = _fetch_filings(session, cik, lookback_days=60)
        if not filings:
            miss.append(ticker)
            continue
        for f in filings:
            f["ticker"] = ticker
            f["name"] = name
            f["cik"] = cik
            f["source"] = "sec.gov/submissions"
            # 8-K 拉详情解析 Item 编号
            if f.get("form") in ("8-K", "8-K/A"):
                items = _fetch_8k_items(session, f.get("filing_url", ""))
                if items:
                    f["items"] = items
                    label, prio = _8k_best_item_label(items)
                    f["item_label"] = label
                    f["item_priority"] = prio
                time.sleep(0.12)
            events.append(f)
        hit += 1
        time.sleep(0.12)  # SEC 限速 10 req/s，留 buffer

    events.sort(key=lambda e: e["event_date"], reverse=True)

    from collections import Counter
    type_counts = Counter(e["event_type"] for e in events)
    form_counts = Counter(e["form"] for e in events)

    payload = {
        "generated_at": datetime.now().isoformat(),
        "n_tickers": len(universe),
        "n_filings": len(events),
        "coverage": {
            "hit": hit, "miss": len(miss), "errored": len(errored),
            "miss_tickers": miss[:20], "errored_tickers": errored[:20],
        },
        "by_event_type": dict(type_counts),
        "by_form": dict(form_counts),
        "events": events,
    }

    out = REPO / "data" / "event_calendar_us_sec.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✅ SEC EDGAR 事件日历已写入 {out}")
    print(f"   tickers: {len(universe)} (hit {hit} / miss {len(miss)} / err {len(errored)})")
    print(f"   filings: {len(events)}")
    for t, n in type_counts.most_common():
        print(f"   {t:24s} {n}")
    return 0 if hit > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
