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
    # A6 并购 / 私有化 / 借壳（必须先于 A5，避免「重大合约」被 ma_takeover 抢走）
    ("ma_takeover",       ["要約", "私有化", "聯合公佈", "建議收購", "全面收購"]),
    # A5 重大订单 / 合同 / 战略合作（订单类公告，常发在「自愿公告」/「内幕消息」类）
    # 注意：避免单"收購"（已被 ma_takeover 占用）；避免单"合作"（太宽）
    ("major_order",       ["重大合同", "重大合約", "重大合约", "重大订单", "重大訂單",
                           "中標", "中标",
                           "採購協議", "采购协议", "供應協議", "供应协议",
                           "框架協議", "框架协议", "戰略合作協議", "战略合作协议",
                           "重大關連交易", "重大关连交易"]),
    # A3 股东减/增持（披露权益）
    ("insider_change",    ["披露權益", "權益披露", "董事權益", "股東權益變動", "主要股東"]),
    # A4 回购
    ("buyback",           ["股份回購", "股份購回", "回購股份", "購回股份"]),
    # A 财报正式公布（已被 yfinance 覆盖，但有时间戳更早 PDF）
    ("earnings_announcement", ["末期業績", "中期業績", "季度業績", "年度業績", "全年業績"]),
]

# 重大订单金额识别正则（中港股常用表达）：用于从 title 提取金额
import re as _re
_AMOUNT_PATTERNS = [
    # 亿级（繁简两套）— 优先级高，先匹配
    _re.compile(r"(\d+(?:\.\d+)?)\s*億美元"),
    _re.compile(r"(\d+(?:\.\d+)?)\s*億港元"),
    _re.compile(r"(\d+(?:\.\d+)?)\s*億元?(?!美|港)"),
    _re.compile(r"(\d+(?:\.\d+)?)\s*亿美元"),
    _re.compile(r"(\d+(?:\.\d+)?)\s*亿港元"),
    _re.compile(r"(\d+(?:\.\d+)?)\s*亿元?(?!美|港)"),
    # 万级
    _re.compile(r"(\d+(?:\.\d+)?)\s*萬美元"),
    _re.compile(r"(\d+(?:\.\d+)?)\s*萬港元"),
    _re.compile(r"(\d+(?:\.\d+)?)\s*萬元?(?!美|港)"),
    _re.compile(r"(\d+(?:\.\d+)?)\s*万美元"),
    _re.compile(r"(\d+(?:\.\d+)?)\s*万港元"),
    _re.compile(r"(\d+(?:\.\d+)?)\s*万元?(?!美|港)"),
    # 英文
    _re.compile(r"USD\s*(\d+(?:\.\d+)?)\s*(?:billion|b)\b", _re.IGNORECASE),
    _re.compile(r"USD\s*(\d+(?:\.\d+)?)\s*(?:million|m)\b", _re.IGNORECASE),
]

def _extract_amount(text: str) -> str:
    """从 title 抽取订单金额（含单位），找不到返回空。"""
    if not text:
        return ""
    for p in _AMOUNT_PATTERNS:
        m = p.search(text)
        if m:
            # 取整个匹配文本，保留单位
            return m.group(0)
    return ""


# 简单单位 → CNY 等值（粗略，用来过滤"重大"阈值）
_UNIT_TO_CNY = {
    "億美元": 7.2e8, "亿美元": 7.2e8,
    "億港元": 0.9e8, "亿港元": 0.9e8,
    "億元":   1e8,    "亿元":   1e8,
    "億":     1e8,    "亿":     1e8,
    "萬美元": 7.2e4, "万美元": 7.2e4,
    "萬港元": 0.9e4, "万港元": 0.9e4,
    "萬元":   1e4,    "万元":   1e4,
    "萬":     1e4,    "万":     1e4,
    "billion": 7.2e9, "Billion": 7.2e9,
    "million": 7.2e6, "Million": 7.2e6,
}


# 客户名白名单（中国主要科技 / 工业 / 央企 + 全球大客户）
# 注：每个客户尽量提供 简体/繁体/英文 三套表达
_CUSTOMER_KEYWORDS = [
    # ─── 中国 三大运营商 ───
    "中国移动", "中國移動", "中移动", "China Mobile",
    "中国电信", "中國電信", "中电信", "China Telecom",
    "中国联通", "中國聯通", "中联通", "China Unicom",
    # ─── 中国 头部科技 / 互联网 ───
    "华为", "華為", "Huawei",
    "比亚迪", "比亞迪", "BYD",
    "腾讯", "騰訊", "Tencent",
    "阿里巴巴", "阿里", "Alibaba",
    "京东", "京東", "JD.com", "JD ",
    "字节跳动", "字節跳動", "字节", "字節", "ByteDance",
    "美团", "美團", "Meituan",
    "百度", "Baidu",
    "网易", "網易", "NetEase",
    "拼多多", "PDD",
    "快手", "Kuaishou",
    "B站", "哔哩哔哩", "嗶哩嗶哩", "Bilibili",
    "小米", "Xiaomi",
    "OPPO", "vivo", "荣耀", "榮耀", "Honor",
    "联想", "聯想", "Lenovo",
    # ─── 中国 新能源车企 ───
    "理想", "Li Auto", "LI Auto",
    "蔚来", "蔚來", "NIO",
    "小鹏", "小鵬", "XPeng",
    "极氪", "極氪", "Zeekr",
    "广汽", "廣汽", "GAC", "上汽", "SAIC", "一汽", "FAW",
    # ─── 中国 重要央企 ───
    "宁德时代", "寧德時代", "CATL",
    "国家电网", "國家電網", "State Grid",
    "南方电网", "南方電網",
    "中石油", "中石化", "中海油", "Sinopec", "PetroChina",
    "中国神华", "中國神華", "Shenhua",
    "中铁", "中鐵", "中交", "中铝", "中鋁", "中核", "中船", "中航", "中冶",
    # ─── 中国 金融 ───
    "工商银行", "工商銀行", "ICBC",
    "建设银行", "建設銀行", "CCB",
    "中国银行", "中國銀行", "BoC", "Bank of China",
    "农业银行", "農業銀行", "ABC",
    "招商银行", "招商銀行", "China Merchants",
    # ─── 海外 美国巨头 ───
    "苹果", "蘋果", "Apple", "AAPL",
    "微软", "微軟", "Microsoft", "MSFT",
    "谷歌", "Google", "GOOGL", "Alphabet",
    "亚马逊", "亞馬遜", "Amazon", "AMZN",
    "Meta", "Facebook", "META",
    "特斯拉", "Tesla", "TSLA",
    "英伟达", "英偉達", "NVIDIA", "NVDA",
    "AMD", "高通", "Qualcomm", "QCOM",
    "Intel", "英特尔", "英特爾",
    "OpenAI", "Anthropic",
    "甲骨文", "Oracle",
    "IBM",
    "Salesforce",
    "Netflix", "奈飞", "奈飛",
    # ─── 海外 半导体 / 设备 ───
    "TSMC", "台积电", "台積電",
    "ASML", "阿斯麦", "阿斯麥",
    "应用材料", "應用材料", "Applied Materials",
    "Lam Research", "泛林",
    "三星", "Samsung",
    # ─── 海外 车厂 ───
    "丰田", "豐田", "Toyota",
    "本田", "Honda",
    "大众", "大眾", "Volkswagen", "VW",
    "宝马", "寶馬", "BMW",
    "奔驰", "賓士", "Mercedes",
    "福特", "Ford",
    "通用", "General Motors", "GM",
    # ─── 海外 其他 ───
    "波音", "Boeing", "空客", "Airbus",
    "沃尔玛", "沃爾瑪", "Walmart",
    "迪士尼", "Disney",
]


def _pdf_cache_db():
    """打开 PDF summary 缓存 DuckDB（位于 data/cache/hkex_pdf_cache.duckdb）。"""
    try:
        import duckdb
    except ImportError:
        return None
    cache_db = REPO / "data" / "cache" / "hkex_pdf_cache.duckdb"
    cache_db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(cache_db))
    con.execute("""
        CREATE TABLE IF NOT EXISTS pdf_summary (
            news_id VARCHAR PRIMARY KEY,
            file_link VARCHAR,
            ticker VARCHAR,
            event_date DATE,
            summary VARCHAR,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    return con


def _pdf_cache_get(con, news_id: str) -> str | None:
    if not con or not news_id:
        return None
    try:
        row = con.execute("SELECT summary FROM pdf_summary WHERE news_id = ?", [news_id]).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _pdf_cache_set(con, news_id: str, file_link: str, ticker: str, event_date: str, summary: str) -> None:
    if not con or not news_id:
        return
    try:
        con.execute(
            "INSERT OR REPLACE INTO pdf_summary (news_id, file_link, ticker, event_date, summary) VALUES (?, ?, ?, ?, ?)",
            [news_id, file_link, ticker, event_date, summary],
        )
    except Exception as e:
        logger.debug("pdf cache write fail: %s", e)


def _fetch_pdf_summary(session, file_link: str, cache_con=None, news_id: str = "",
                       ticker: str = "", event_date: str = "") -> str:
    """拉 HKEX 公告 PDF，提取第一页前 400 字作为摘要。失败返回空。

    HKEX file_link 格式：/listedco/listconews/sehk/YYYY/MMDD/YYYYMMDDNNNNN_c.pdf
    完整 URL: https://www1.hkexnews.hk{file_link}

    cache_con: DuckDB connection — 命中 cache 直接返回，避免重拉同一份 PDF。
    """
    if not file_link or not file_link.endswith(".pdf"):
        return ""
    # 1. 查缓存
    cached = _pdf_cache_get(cache_con, news_id)
    if cached is not None:
        return cached
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    import io
    url = file_link if file_link.startswith("http") else f"https://www1.hkexnews.hk{file_link}"
    try:
        r = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if r.status_code != 200:
            return ""
        reader = PdfReader(io.BytesIO(r.content))
        if not reader.pages:
            return ""
        text = reader.pages[0].extract_text() or ""
    except Exception as e:
        logger.debug("PDF parse %s err: %s", url, e)
        return ""
    # 清洗：折叠空白 + 去掉常见免责声明前缀
    import re as _re3
    text = _re3.sub(r"\s+", " ", text).strip()
    # 跳过开头的"香港交易及結算所有限公司..."免责段（典型 100-200 字）
    skip_markers = [
        "概不對因本公告全部",
        "concerning the contents of this announcement",
        "對其準確性或完整性",
    ]
    for marker in skip_markers:
        idx = text.find(marker)
        if 0 < idx < 600:
            # 跳过免责段直到下一个章节标记
            text = text[idx + len(marker):].lstrip()
            # 找第一个段落级文本起点
            for sep in ["。", ".", ":", "："]:
                p = text.find(sep)
                if 0 < p < 200:
                    text = text[p + len(sep):].lstrip()
                    break
            break
    summary = text[:400]
    # 2. 写缓存
    _pdf_cache_set(cache_con, news_id, file_link, ticker, event_date, summary)
    return summary


_AUTO_WHITELIST_CACHE: list[str] | None = None


def _load_auto_whitelist() -> list[str]:
    """从 data/cache/customer_whitelist_auto.json 加载词频自动入库的客户名。"""
    global _AUTO_WHITELIST_CACHE
    if _AUTO_WHITELIST_CACHE is not None:
        return _AUTO_WHITELIST_CACHE
    p = REPO / "data" / "cache" / "customer_whitelist_auto.json"
    if not p.exists():
        _AUTO_WHITELIST_CACHE = []
        return _AUTO_WHITELIST_CACHE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        _AUTO_WHITELIST_CACHE = data.get("names") or []
    except Exception:
        _AUTO_WHITELIST_CACHE = []
    return _AUTO_WHITELIST_CACHE


def _extract_customer(text: str, self_name: str = "") -> str:
    """从 text 抽取客户名（白名单匹配 + 自动入库扩展）。
    self_name: 发公告的本公司名（含简繁版本），匹配到自己就跳过避免"联想公告里提到联想"误识别。
    """
    if not text:
        return ""
    # 拆 self_name 成多个变体
    self_tokens: set[str] = set()
    if self_name:
        for sep in (" ", "/", "(", "（", "－", "-"):
            self_name = self_name.replace(sep, "|")
        for t in self_name.split("|"):
            t = t.strip()
            if len(t) >= 2:
                self_tokens.add(t)
    # 静态白名单 + 自动入库扩展
    all_names = list(_CUSTOMER_KEYWORDS) + _load_auto_whitelist()
    for c in all_names:
        if c in text:
            # 自己被命中 → 跳过
            if any(c == st or c in st or st in c for st in self_tokens):
                continue
            return c
    return ""


def _amount_to_cny(amount_text: str) -> float:
    """金额文本 → CNY 等值估算。'5 亿美元' → 3.6e9。无法解析返回 0。"""
    if not amount_text:
        return 0.0
    import re as _re2
    m = _re2.search(r"(\d+(?:\.\d+)?)", amount_text)
    if not m:
        return 0.0
    num = float(m.group(1))
    for unit, factor in _UNIT_TO_CNY.items():
        if unit in amount_text:
            return num * factor
    return num  # 没单位假设是元

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


def _stock_id(session, raw_code: str) -> tuple[int | None, str]:
    """raw_code 形如 '0992' / '00992' → HKEX 内部 stockId (int) + status."""
    import requests

    digits = "".join(ch for ch in str(raw_code) if ch.isdigit())
    variants = []
    for value in (digits.zfill(5), digits.zfill(4), digits.lstrip("0") or digits):
        if value and value not in variants:
            variants.append(value)

    last_status = "not_found"
    for name in variants:
        try:
            r = session.get(
                PARTIAL_URL,
                params={"callback": "callback", "lang": "ZH", "type": "A", "name": name, "market": "SEHK"},
                headers=HEADERS,
                timeout=10,
            )
            if r.status_code != 200:
                last_status = f"http_{r.status_code}"
                continue
            if not (r.text or "").strip():
                last_status = "empty_response"
                continue
            # JSONP: callback({...});
            m = re.search(r"callback\((\{.*\})\);?", r.text, re.S)
            if not m:
                last_status = "invalid_jsonp"
                continue
            data = json.loads(m.group(1))
            info = data.get("stockInfo") or []
            for item in info:
                if str(item.get("code", "")).lstrip("0") == digits.lstrip("0"):
                    return int(item.get("stockId")), "ok"
            if info:
                return int(info[0].get("stockId")), "fallback_first"
            last_status = "no_stock_info"
        except Exception as e:
            last_status = f"{type(e).__name__}: {e}"
            logger.warning("partial.do fail %s/%s: %s", raw_code, name, e)
    return None, last_status


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
    pdf_cache = _pdf_cache_db()  # 持久化 PDF 摘要，避免每天重拉
    events: list[dict] = []
    hit, miss, errored = 0, [], []
    stock_id_failures: dict[str, str] = {}

    for ticker, name in sorted(universe.items()):
        raw_code = ticker.replace(".HK", "")
        sid, sid_status = _stock_id(session, raw_code)
        if not sid:
            stock_id_failures[ticker] = sid_status
            if sid_status in {"no_stock_info", "not_found"}:
                miss.append(ticker)
            else:
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
            title = _unescape(a.get("TITLE", ""))
            entry = {
                "ticker": ticker,
                "stock_code": _unescape(a.get("STOCK_CODE", "")).split("<")[0].strip(),
                "stock_id": sid,
                "event_date": event_date,
                "event_type": etype,
                "title": title,
                "long_text": _unescape(a.get("LONG_TEXT", "")),
                "file_link": a.get("FILE_LINK", ""),
                "news_id": a.get("NEWS_ID", ""),
                "source": "hkexnews.hk/titleSearchServlet",
            }
            # major_order 类公告：抽金额 + 客户名 + 转 CNY 估值 + 拉 PDF 摘要
            if etype == "major_order":
                self_name = name or _unescape(a.get("STOCK_NAME", "")).split("<")[0].strip()
                amt = _extract_amount(title)
                if amt:
                    entry["amount_text"] = amt
                    cny = _amount_to_cny(amt)
                    if cny > 0:
                        entry["amount_cny_approx"] = round(cny, 0)
                customer = _extract_customer(title, self_name=self_name)
                if customer:
                    entry["customer"] = customer
                # 拉 PDF 首页摘要（可能含合同期限/起止日期/cap 等关键细节）
                # 用 cache 避免重拉（同一份公告的 news_id 唯一）
                pdf_summary = _fetch_pdf_summary(
                    session, a.get("FILE_LINK", ""),
                    cache_con=pdf_cache,
                    news_id=a.get("NEWS_ID", ""),
                    ticker=ticker,
                    event_date=event_date,
                )
                if pdf_summary:
                    entry["context_summary"] = pdf_summary
                    # 如果 title 没抽到金额/客户，尝试从 summary 抽
                    if "amount_text" not in entry:
                        amt2 = _extract_amount(pdf_summary)
                        if amt2:
                            entry["amount_text"] = amt2
                            cny2 = _amount_to_cny(amt2)
                            if cny2 > 0:
                                entry["amount_cny_approx"] = round(cny2, 0)
                    if "customer" not in entry:
                        c2 = _extract_customer(pdf_summary, self_name=self_name)
                        if c2:
                            entry["customer"] = c2
            events.append(entry)
        hit += 1
        time.sleep(0.3)  # rate limit 礼貌

    events.sort(key=lambda e: e["event_date"], reverse=True)

    # 按类型统计
    from collections import Counter
    type_counts = Counter(e["event_type"] for e in events)

    source_status = "ok" if hit > 0 else ("degraded" if errored else "empty")
    payload = {
        "generated_at": datetime.now().isoformat(),
        "status": source_status,
        "n_tickers": len(universe),
        "n_announcements": len(events),
        "source_health": {
            "status": source_status,
            "reason": (
                "hkex_source_unavailable_or_blocked" if errored and hit == 0
                else "no_matching_announcements" if hit == 0
                else "ok"
            ),
            "stock_id_failures": stock_id_failures,
        },
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
    if source_status != "ok":
        print(f"   source status: {source_status} ({payload['source_health']['reason']})")
    for t, n in type_counts.most_common():
        print(f"   {t:24s} {n}")
    if pdf_cache:
        try:
            n_cached = pdf_cache.execute("SELECT COUNT(*) FROM pdf_summary").fetchone()[0]
            print(f"   pdf cache: {n_cached} 条记录（避免重拉同一份公告）")
            pdf_cache.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
