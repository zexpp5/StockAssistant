"""港股 HKEX 披露易公告日历刷新。

补 yfinance 拿不到的非财报类催化（业绩预告 / 停牌 / 股东减增持 / 回购 / 并购）。

流程：
  1. partial.do 查每只 ticker 的 HKEX 内部 stockId（需要，不是 00992 这种代码）
  2. titleSearchServlet.do 拉最近 N 天该 stockId 的所有公告 JSON
  3. 按 LONG_TEXT 关键词分类成 5 类 event_type
  4. 写 data/event_calendar_hk_hkex.json

下游：catalyst.py 合并这里的事件 + yfinance 财报，给 ticker 选最强催化句。

输出 schema:
  {
    "generated_at": "...",
    "n_tickers": 34,
    "n_announcements": 312,
    "events": [
      {
        "ticker": "0992.HK",
        "stock_code": "00992",
        "stock_id": 2325,
        "event_date": "2026-05-22",
        "event_type": "earnings_announcement",  # 见 EVENT_TYPES
        "title": "二零二五/二六年财政年度全年业绩公布",
        "long_text": "公告及通告 - [末期业绩 / 股息或分派 ...]",
        "file_link": "/listedco/listconews/sehk/2026/0522/2026052200034_c.pdf",
        "news_id": "12167750"
      }
    ]
  }
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from stock_research.core.hk_universe import HK_TECH_UNIVERSE

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


PARTIAL_URL = "https://www1.hkexnews.hk/search/partial.do"
SEARCH_URL = "https://www1.hkexnews.hk/search/titleSearchServlet.do"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh",
    "Accept": "text/javascript, application/javascript, application/ecmascript, application/x-ecmascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

# 公告 LONG_TEXT 关键词分类（按 HKEX 官方公告分类的中文表述）
# 顺序重要：更具体的类先匹配（停牌/盈警优先于股东/回购）
CATEGORY_RULES = [
    # A1 业绩预告 / 盈警 / 盈喜
    ("earnings_preview",  ["盈利警告", "盈利預告", "業績預告", "盈喜", "盈警", "正面盈利", "負面盈利"]),
    # A2 停牌 / 复牌
    ("trading_halt",      ["暫停買賣", "恢復買賣", "短暫停牌", "復牌"]),
    # A3 股东减/增持（披露权益）
    ("insider_change",    ["披露權益", "權益披露", "董事權益", "股東權益變動", "主要股東"]),
    # A4 回购
    ("buyback",           ["股份回購", "股份購回", "回購股份", "購回股份"]),
    # A6 并购 / 私有化 / 借壳
    ("ma_takeover",       ["收購", "要約", "合併", "私有化", "聯合公佈", "建議收購"]),
    # A 财报正式公布（已被 yfinance 覆盖，但有时间戳更早 PDF）
    ("earnings_announcement", ["末期業績", "中期業績", "季度業績", "年度業績", "全年業績"]),
]

# 字符实体反转义（HKEX JSON 里 / 转成 &#x2f;）
_HTML_ENT = re.compile(r"\\?&#x([0-9a-fA-F]+);")


def _unescape(s: str) -> str:
    if not s:
        return s
    def _r(m): return chr(int(m.group(1), 16))
    return _HTML_ENT.sub(_r, s)


def _classify(long_text: str) -> str | None:
    """LONG_TEXT → event_type；不匹配返回 None。"""
    if not long_text:
        return None
    txt = _unescape(long_text)
    for etype, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw in txt:
                return etype
    return None


def _stock_id(session, raw_code: str) -> int | None:
    """raw_code 形如 '0992' / '00992' → HKEX 内部 stockId (int)。"""
    import requests
    # 补到 5 位（HKEX 习惯）
    padded = raw_code.zfill(5)
    try:
        r = session.get(
            PARTIAL_URL,
            params={"callback": "callback", "lang": "ZH", "type": "A", "name": padded, "market": "SEHK"},
            headers=HEADERS,
            timeout=10,
        )
        # JSONP: callback({...});
        m = re.search(r"callback\((\{.*\})\);?", r.text, re.S)
        if not m:
            return None
        data = json.loads(m.group(1))
        for item in (data.get("stockInfo") or []):
            if str(item.get("code", "")).lstrip("0") == raw_code.lstrip("0"):
                return int(item.get("stockId"))
        # 兜底：取第一条
        info = (data.get("stockInfo") or [])
        return int(info[0].get("stockId")) if info else None
    except Exception as e:
        logger.warning("partial.do fail %s: %s", raw_code, e)
        return None


def _fetch_announcements(session, stock_id: int, from_dt: date, to_dt: date) -> list[dict]:
    """拉单只 ticker 的公告 JSON 列表。"""
    import requests
    params = {
        "sortDir": "0", "sortByOptions": "DateTime",
        "category": "0", "market": "SEHK",
        "stockId": str(stock_id), "documentType": "-1",
        "fromDate": from_dt.strftime("%Y%m%d"),
        "toDate": to_dt.strftime("%Y%m%d"),
        "title": "", "searchType": "1",
        "t1code": "-2", "t2Gcode": "-2", "t2code": "-2",
        "rowRange": "200", "lang": "ZH",
    }
    try:
        r = session.get(SEARCH_URL, params=params, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
        result_str = data.get("result", "[]")
        return json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        logger.warning("search fail stock_id=%s: %s", stock_id, e)
        return []


def _parse_event_date(date_str: str) -> str | None:
    """HKEX 'DATE_TIME': '22/05/2026 07:32' → '2026-05-22'。"""
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str.split()[0], "%d/%m/%Y").date()
        return d.isoformat()
    except Exception:
        return None


def _gather_universe() -> dict[str, str]:
    """ticker → name；合并 hk_universe 白名单 + trade_delta_hk 推荐池。"""
    out: dict[str, str] = {}
    for x in HK_TECH_UNIVERSE:
        out[x["ticker"]] = x.get("name", "")
    td = REPO / "data" / "latest" / "trade_delta_hk.json"
    if td.exists():
        try:
            d = json.loads(td.read_text(encoding="utf-8"))
            for bucket in ("buys", "sells", "holds"):
                for item in (d.get(bucket) or []):
                    t = item.get("ticker", "")
                    if t and t.endswith(".HK"):
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

    universe = _gather_universe()
    logger.info("覆盖 %d 只港股 ticker", len(universe))

    # 时间窗：过去 60 天
    today = date.today()
    from_dt = today - timedelta(days=60)

    session = requests.Session()
    events: list[dict] = []
    hit, miss, errored = 0, [], []

    for ticker, name in sorted(universe.items()):
        raw_code = ticker.replace(".HK", "")
        sid = _stock_id(session, raw_code)
        if not sid:
            errored.append(ticker)
            continue
        anns = _fetch_announcements(session, sid, from_dt, today)
        if not anns:
            miss.append(ticker)
            continue
        for a in anns:
            etype = _classify(a.get("LONG_TEXT", ""))
            if not etype:
                continue
            event_date = _parse_event_date(a.get("DATE_TIME", ""))
            if not event_date:
                continue
            events.append({
                "ticker": ticker,
                "stock_code": _unescape(a.get("STOCK_CODE", "")).split("<")[0].strip(),
                "stock_id": sid,
                "event_date": event_date,
                "event_type": etype,
                "title": _unescape(a.get("TITLE", "")),
                "long_text": _unescape(a.get("LONG_TEXT", "")),
                "file_link": a.get("FILE_LINK", ""),
                "news_id": a.get("NEWS_ID", ""),
                "source": "hkexnews.hk/titleSearchServlet",
            })
        hit += 1
        time.sleep(0.3)  # rate limit 礼貌

    events.sort(key=lambda e: e["event_date"], reverse=True)

    # 按类型统计
    from collections import Counter
    type_counts = Counter(e["event_type"] for e in events)

    payload = {
        "generated_at": datetime.now().isoformat(),
        "n_tickers": len(universe),
        "n_announcements": len(events),
        "coverage": {
            "hit": hit, "miss": len(miss), "errored": len(errored),
            "miss_tickers": miss[:20], "errored_tickers": errored[:20],
        },
        "by_event_type": dict(type_counts),
        "events": events,
    }

    out = REPO / "data" / "event_calendar_hk_hkex.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✅ HKEX 公告日历已写入 {out}")
    print(f"   tickers: {len(universe)} (hit {hit} / miss {len(miss)} / err {len(errored)})")
    print(f"   announcements: {len(events)}")
    for t, n in type_counts.most_common():
        print(f"   {t:24s} {n}")
    return 0 if hit > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
