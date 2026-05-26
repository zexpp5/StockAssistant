"""产业政策事件抓取：从财经/官方新闻流中识别政策信号 + 主题映射。

为什么要做：
  A 股 60% 的板块行情由政策驱动（"提振信心 24 条"、"AI+ 行动计划"、
  "新质生产力"、"半导体大基金三期"等）。错过政策日 = 错过 30% 的板块超额。
  纯量化模型对政策免疫 → 必须有事件抓取层注入。

数据源（akshare，全部免费）：
  - ak.news_cctv(date)              新闻联播文本（央视，权威性最高）
  - ak.stock_zh_a_alerts_cls()       财联社电报（5 分钟级时效）
  - ak.news_economic_baidu()         百度经济新闻（多源聚合）

实现路径：
  1. 抓取最近 N 天新闻流
  2. 关键词过滤识别政策事件（"国务院 + 印发"、"发改委 + 通知"、"工信部 + 试点"）
  3. 主题映射（政策内容 → 受益板块 → 受益股票池）
  4. 输出 PolicyEvent 列表（供因子模型加权 + 看板展示）

学术依据：
  - 朱琪 & 朱武祥 (2018) "政策不确定性与中国 A 股横截面收益"：政策冲击日的
    板块超额可持续 5-15 个交易日
  - Pastor & Veronesi (2012) JF：政策不确定性是已被定价的系统性风险因子

简化设计：
  本模块只做"事件检测 + 主题打标"，不做股票池映射（交给 daily_picks 用主题
  标签直接加权）。这样保持模块解耦。
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


# ───────────── 政策识别规则 ─────────────

# 一级政策来源关键词（命中 = 高权威）
POLICY_SOURCE_KEYWORDS = [
    "国务院", "中央", "中共中央", "国家发改委", "发改委", "工信部", "工业和信息化部",
    "财政部", "央行", "人民银行", "证监会", "银保监会", "国家能源局",
    "科技部", "商务部", "交通运输部", "住建部", "国资委",
    # 港股相关权威：港澳办 + 港交所 + 香港金管局 / 证监会
    "港澳办", "香港特区政府", "港交所", "HKEX", "金管局", "HKMA", "香港证监会", "SFC",
]

# 政策动词（命中 = 政策动作）
POLICY_VERBS = [
    "印发", "出台", "发布", "通知", "意见", "办法", "规定", "试点", "通知",
    "支持", "扶持", "补贴", "免税", "减税", "税收优惠", "专项", "推进",
    "加快", "促进", "扩大", "鼓励", "深化", "强化", "实施",
]

# 主题映射规则：每个主题对应一组受益关键词
# 命中关键词 = 政策利好该主题
THEME_KEYWORDS: dict[str, list[str]] = {
    "AI 算力": ["人工智能", "AI", "算力", "智算中心", "大模型", "通用人工智能", "AGI"],
    "半导体": ["半导体", "集成电路", "芯片", "晶圆", "光刻", "封测", "EDA", "大基金"],
    "光通信": ["光通信", "光模块", "光纤", "硅光", "数据中心互联", "DCI"],
    "新能源车": ["新能源汽车", "新能源车", "电动汽车", "动力电池", "充电桩",
                "智能驾驶", "L3", "L4", "自动驾驶"],
    "机器人": ["机器人", "人形机器人", "具身智能", "智能制造装备", "工业机器人"],
    "光伏储能": ["光伏", "风电", "储能", "氢能", "绿电", "可再生能源", "新能源"],
    "数字经济": ["数字经济", "数字中国", "数据要素", "东数西算", "云计算"],
    "国产替代": ["国产化", "自主可控", "信创", "国产替代", "卡脖子"],
    "国企改革": ["国企改革", "央企", "市值管理", "整合重组"],
    "消费刺激": ["以旧换新", "消费券", "消费补贴", "汽车下乡", "家电下乡"],
    "房地产": ["房地产", "楼市", "保交楼", "保障房", "白名单"],
    "医药": ["医保", "集采", "创新药", "医疗器械", "中医药"],
    "军工": ["军工", "国防", "航天", "航空发动机", "装备建设"],
    # 港股专属主题（避免"互联互通"等多义词，否则会误击中外交新闻）
    "港股通": ["港股通", "沪深港通", "深港通", "南向资金", "南向通", "北向资金",
              "H 股", "H股", "红筹股", "互认基金", "跨境理财通", "港股 ETF"],
}

# 港股关键词集合（用来给 PolicyEvent.markets 标记 hk）
# 经过 2026-05-26 实测，必须只保留港股金融专属词，否则会误击中外交/航天新闻：
#   ❌ "香港特区政府" → 神舟发射现场提到香港代表
#   ❌ "互联互通" → 外交合作语境的"互联互通"
#   ❌ "香港" → 太宽，地理提及
# 保留的都是不可能用在非港股金融语境的词。
_HK_MARKET_KEYWORDS = [
    "港股", "港股通", "沪深港通", "深港通", "南向资金", "南向通", "北向资金",
    "H 股", "H股", "红筹股", "港交所", "HKEX", "香港金管局", "HKMA",
    "香港证监会", "互认基金", "跨境理财通", "港股 ETF",
]


@dataclass
class PolicyEvent:
    """一条政策事件。"""
    date: date
    title: str                              # 新闻标题/摘要
    source_authority: str = ""              # 命中的权威机构（如"国务院"）
    matched_themes: list[str] = field(default_factory=list)  # 命中的受益主题
    relevance_score: int = 0                # 1-5，综合权威性 + 主题命中数
    full_text: str = ""                     # 原文（截断）
    source: str = ""                        # akshare 接口名
    markets: list[str] = field(default_factory=list)  # 影响市场 ["cn"] / ["hk"] / ["cn","hk"]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["date"] = self.date.isoformat() if isinstance(self.date, date) else str(self.date)
        return d


# ───────────── 数据抓取 ─────────────

def _import_ak():
    try:
        import akshare as ak
        return ak
    except ImportError:
        logger.error("akshare not installed")
        return None


def fetch_cctv_news(target_date: date) -> list[dict]:
    """新闻联播文本（央视）。"""
    ak = _import_ak()
    if ak is None:
        return []
    try:
        df = ak.news_cctv(date=target_date.strftime("%Y%m%d"))
    except Exception as e:
        logger.warning("akshare news_cctv(%s) failed: %s", target_date, e)
        return []
    if df is None or df.empty:
        return []
    out = []
    for _, r in df.iterrows():
        out.append({
            "date": target_date,
            "title": str(r.get("title", "")),
            "content": str(r.get("content", "")),
            "source": "akshare/news_cctv",
        })
    return out


def fetch_cls_alerts() -> list[dict]:
    """财联社电报（最近一批）。"""
    ak = _import_ak()
    if ak is None:
        return []
    try:
        df = ak.stock_zh_a_alerts_cls()
    except Exception as e:
        logger.warning("akshare stock_zh_a_alerts_cls failed: %s", e)
        return []
    if df is None or df.empty:
        return []
    out = []
    title_col = _pick_col(df, ["标题", "title"])
    content_col = _pick_col(df, ["内容", "content"])
    date_col = _pick_col(df, ["发布日期", "date", "时间"])
    for _, r in df.iterrows():
        d = _parse_date(r.get(date_col)) if date_col else date.today()
        out.append({
            "date": d or date.today(),
            "title": str(r.get(title_col, "")) if title_col else "",
            "content": str(r.get(content_col, "")) if content_col else "",
            "source": "akshare/stock_zh_a_alerts_cls",
        })
    return out


# HKMA press 标题里这些词出现 = 日常运营公告 (噪音),不该当政策信号
_HKMA_NOISE_PATTERNS = [
    "Scam alert", "scam alert", "fraudulent website", "fraudulent",
    "Exchange Fund Notes Tender Results",
    "Exchange Fund Bills Tender Results",
    "Tender Results",
    "appointment",  # 任命公告（除非含 Chief Executive 等关键词）
    "Renminbi Bills",  # 央行例行人民币 bills issuance
]


def _is_hkma_noise(title: str) -> bool:
    """标题命中噪音模式 → True 跳过。"""
    if not title:
        return True
    return any(p in title for p in _HKMA_NOISE_PATTERNS)


def fetch_hkma_press(limit: int = 20) -> list[dict]:
    """香港金融管理局 (HKMA) 新闻发布 — 港股相关货币政策 / 监管声明。

    页面：https://www.hkma.gov.hk/eng/news-and-media/press-releases/
    解析 server-rendered HTML 拿 URL（含日期）+ 标题。不拉详情页（只 title 关键词匹配）。
    噪音过滤：日常 Tender Results / Scam alert 静默不进 events。
    """
    try:
        import requests
    except ImportError:
        logger.warning("requests not installed, skip HKMA")
        return []
    import re as _re
    url = "https://www.hkma.gov.hk/eng/news-and-media/press-releases/"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 LinearV"}, timeout=10)
        if r.status_code != 200:
            return []
        html = r.text
    except Exception as e:
        logger.warning("HKMA press fetch err: %s", e)
        return []

    pat = _re.compile(
        r'href="(/eng/news-and-media/press-releases/(\d{4})/(\d{2})/(\d{8}-\d+)/)"[^>]*>([^<]+)</a>'
    )
    out: list[dict] = []
    seen: set[str] = set()
    n_noise = 0
    for full_url, yyyy, mm, ymd_n, title in pat.findall(html)[:limit * 3]:
        if full_url in seen:
            continue
        seen.add(full_url)
        title = title.strip()
        if _is_hkma_noise(title):
            n_noise += 1
            continue
        try:
            d = date(int(yyyy), int(mm), int(ymd_n[6:8]))
        except Exception:
            continue
        out.append({
            "date": d,
            "title": title,
            "content": "",  # 不拉详情减少请求；title 已含主信号
            "source": "hkma.gov.hk/press-releases",
        })
        if len(out) >= limit:
            break
    if n_noise:
        logger.info("HKMA 噪音过滤 %d 条 (Scam alert / Tender Results 等)", n_noise)
    return out


def fetch_sfc_news(limit: int = 30) -> list[dict]:
    """香港证监会 (SFC) news — 港股监管 / 执法。

    SPA backend：POST /edistributionWeb/api/news/search
    新闻类型 newsType: GN (General News) / EF (Enforcement News) / CR (Corporate Communications)
    返回精简后的 item list 给 policy_events 用。
    """
    try:
        import requests
    except ImportError:
        return []
    from datetime import date as _d
    url = "https://apps.sfc.hk/edistributionWeb/api/news/search"
    payload = {
        "lang": "EN", "category": "all",
        "year": _d.today().year,
        "pageNo": 1, "pageSize": limit,
        "sort": {"field": "issueDate", "order": "desc"},
    }
    try:
        r = requests.post(url, json=payload, timeout=10,
                          headers={"User-Agent": "Mozilla/5.0 LinearV",
                                   "Content-Type": "application/json"})
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception as e:
        logger.warning("SFC news fetch err: %s", e)
        return []

    out: list[dict] = []
    for item in (data.get("items") or [])[:limit]:
        iso_dt = item.get("issueDate") or ""
        try:
            d = datetime.strptime(iso_dt[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        out.append({
            "date": d,
            "title": (item.get("title") or "").strip(),
            "content": "",
            "source": f"sfc.hk/news?type={item.get('newsType','')}",
        })
    return out


# ───────────── 政策识别 ─────────────

def _detect_authority(text: str) -> str:
    """识别新闻里的最高权威机构（取第一个命中的）。"""
    for kw in POLICY_SOURCE_KEYWORDS:
        if kw in text:
            return kw
    return ""


def _has_policy_verb(text: str) -> bool:
    return any(v in text for v in POLICY_VERBS)


def _match_themes(text: str) -> list[str]:
    matched = []
    for theme, keywords in THEME_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                matched.append(theme)
                break
    return matched


def _score(authority: str, themes: list[str], has_verb: bool) -> int:
    """综合权威性 + 主题数 + 是否带政策动词，给 1-5 分。"""
    s = 0
    if authority:
        # 国务院/中央 = 3 分；部委 = 2 分；其他 = 1 分
        if authority in ("国务院", "中央", "中共中央"):
            s += 3
        elif authority in ("国家发改委", "发改委", "工信部", "财政部", "央行", "证监会",
                            # 港股监管机构等同部委级
                            "HKMA", "香港金管局", "香港证监会", "SFC", "港交所", "HKEX", "港澳办"):
            s += 2
        else:
            s += 1
    if has_verb:
        s += 1
    if len(themes) >= 1:
        s += 1
    if len(themes) >= 3:
        s += 1
    return min(5, s)


def detect_policy_events(news_items: list[dict],
                         min_score: int = 2) -> list[PolicyEvent]:
    """从新闻列表中检测政策事件。

    规则：必须命中权威机构 OR （政策动词 + 至少 1 个主题）。
    """
    events: list[PolicyEvent] = []
    for item in news_items:
        title = item.get("title", "") or ""
        content = item.get("content", "") or ""
        full = f"{title} {content}"

        authority = _detect_authority(full)
        themes = _match_themes(full)
        has_verb = _has_policy_verb(full)

        # 来源是港股监管机构自身（HKMA / SFC / HKEX），自动当权威
        src_lower = (item.get("source", "") or "").lower()
        if not authority:
            if "hkma" in src_lower:
                authority = "HKMA"
            elif "sfc" in src_lower:
                authority = "香港证监会"
            elif "hkex" in src_lower:
                authority = "港交所"

        # 过滤：必须有权威或（政策动词+主题）
        if not authority and not (has_verb and themes):
            continue

        score = _score(authority, themes, has_verb)
        if score < min_score:
            continue

        # 市场判定：命中港股关键词 → +hk；命中 A 股主题（半导体/AI/光伏等）→ +cn
        # 默认（既不命中港股也不命中专属主题）→ cn（内地政策默认影响 A 股）
        markets: list[str] = []
        src = item.get("source", "") or ""
        # 来源直接来自港股监管机构 → 必标 hk（覆盖关键词匹配）
        if "hkma" in src.lower() or "sfc" in src.lower() or "hkex" in src.lower():
            markets.append("hk")
        if any(kw in full for kw in _HK_MARKET_KEYWORDS) or "港股通" in themes:
            if "hk" not in markets:
                markets.append("hk")
        # A 股标记规则：非港股主题命中 / 内地权威发文 → 影响 A 股
        if any(t != "港股通" for t in themes) or (authority and "香港" not in (authority or "")):
            if "hk" not in markets[:1]:  # 港股监管来源的不要又加 cn
                markets.insert(0, "cn")
        if not markets:
            markets = ["cn"]

        events.append(PolicyEvent(
            date=item.get("date") or date.today(),
            title=title[:200],
            source_authority=authority,
            matched_themes=themes,
            relevance_score=score,
            full_text=content[:500],
            source=item.get("source", ""),
            markets=markets,
        ))
    return events


# ───────────── 主入口 ─────────────

def scan_recent_policies(days: int = 7, min_score: int = 2) -> list[PolicyEvent]:
    """扫描最近 N 天的政策事件（新闻联播 + 财联社电报）。"""
    all_news = []

    # 1. 新闻联播（每天）
    today = date.today()
    for i in range(days):
        d = today - timedelta(days=i)
        all_news.extend(fetch_cctv_news(d))

    # 2. 财联社电报（最近一批）
    all_news.extend(fetch_cls_alerts())

    # 3. 香港金管局 (HKMA) press releases — 港股专属源
    all_news.extend(fetch_hkma_press(limit=30))

    # 4. 香港证监会 (SFC) news — 港股监管 / 执法
    all_news.extend(fetch_sfc_news(limit=30))

    events = detect_policy_events(all_news, min_score=min_score)
    # 按日期降序 + 相关性降序
    events.sort(key=lambda e: (e.date, e.relevance_score), reverse=True)
    return events


def themes_under_policy_tailwind(days: int = 14, min_count: int = 2) -> dict[str, int]:
    """计算最近 N 天有几条政策事件命中各主题（≥ min_count 视为政策受益）。

    返回 {theme: count}。可直接用于 daily_picks 的主题加权：
      score *= 1 + 0.1 * count（每条政策事件给主题 +10% 权重，上限 30%）。
    """
    events = scan_recent_policies(days=days, min_score=2)
    counts: dict[str, int] = {}
    for e in events:
        for t in e.matched_themes:
            counts[t] = counts.get(t, 0) + 1
    return {t: c for t, c in counts.items() if c >= min_count}


# ───────────── 工具 ─────────────

def _pick_col(df, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _parse_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    if isinstance(v, datetime):
        return v.date()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    try:
        import pandas as pd
        ts = pd.to_datetime(v, errors="coerce")
        if ts is not None and not (ts != ts):
            return ts.date()
    except Exception:
        pass
    return None


# ───────────── CLI ─────────────

def _main():
    """python -m stock_research.core.policy_events [--days 7] [--min-score 2]"""
    import sys
    days = 7
    min_score = 2
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    if "--min-score" in sys.argv:
        min_score = int(sys.argv[sys.argv.index("--min-score") + 1])

    print(f"📰 扫描最近 {days} 天政策事件（min_score={min_score}）...\n")
    events = scan_recent_policies(days=days, min_score=min_score)
    print(f"  共 {len(events)} 条事件\n")

    for e in events[:20]:
        themes = "/".join(e.matched_themes) if e.matched_themes else "?"
        auth = e.source_authority or "无机构标识"
        print(f"  [{e.date}] ⭐{e.relevance_score} 【{auth}】 {themes}")
        print(f"    {e.title[:120]}")
        print()

    print("\n📊 主题受益统计（最近 14 天 ≥ 2 次政策命中）：")
    tailwind = themes_under_policy_tailwind(days=14, min_count=2)
    for theme, count in sorted(tailwind.items(), key=lambda x: -x[1]):
        print(f"  {theme:<12} {count} 次")


if __name__ == "__main__":
    _main()
