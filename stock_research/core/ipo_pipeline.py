"""新股打新流水线：自动抓取 IPO 日历 + AI 主题标签 + 打新 ROI 估算。

为什么要做：
  系统目前手工梳理新股清单（见 docs/2026-05-10_新股打新清单梳理*.md）。
  这种工作必须自动化：
    1. 申购日错过就是错过，没有提醒等于白用系统
    2. 主题标签（AI/算力/光通信）能快速定位高赔率打新对象
    3. 历史首日溢价能给打新预期收益估算

数据源（akshare）：
  - ak.stock_xgsglb_em()       新股申购列表（含申购日、申购代码、发行价）
  - ak.stock_zh_a_new_em()      新股一览（已上市未满一年）
  - ak.stock_zh_a_st_em()       科创板新股动态
  - ak.stock_xgsr_ths()         新股上市首日表现（同花顺）—— 用于历史溢价均值

输出：
  IpoCalendar — 三段：
    - upcoming_subscription  即将申购（今天往后）
    - awaiting_listing       已申购未上市
    - recently_listed        近 30 日上市
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Any, Literal

logger = logging.getLogger(__name__)


# ───────────── 主题映射规则（A 股 IPO 业务描述关键词）─────────────
#
# 每条规则：(关键词列表, 主题标签, 相关性分 0-3)
# 分数：3 = 核心受益、2 = 直接相关、1 = 外延受益
#
# 关键词命中是 OR 关系，每只股可能命中多条主题（取最高分作为主分类）
A_SHARE_THEME_RULES: list[tuple[list[str], str, int]] = [
    # 3 分：AI 算力核心
    (["GPU", "图形处理器", "AI 芯片", "AI芯片", "智算", "算力芯片"], "AI 算力核心", 3),
    (["光模块", "硅光", "CPO", "1.6T", "800G", "OSFP"], "光通信", 3),
    (["掺铒光纤", "稀土光纤", "光纤激光器", "EDFA"], "光通信链", 3),
    (["大语言模型", "LLM", "大模型", "通用人工智能", "AGI"], "AI 大模型", 3),
    # 2 分：直接受益
    (["半导体", "集成电路", "晶圆", "芯片", "光刻", "刻蚀", "封装测试"], "半导体", 2),
    (["FPC", "柔性电路板", "高密度互连", "HDI"], "电子元件 (PCB)", 2),
    (["数据中心", "IDC", "液冷", "服务器"], "数据中心", 2),
    (["智能驾驶", "自动驾驶", "高阶辅助驾驶", "L3", "L4", "智驾"], "智能驾驶", 2),
    (["人形机器人", "机器人本体", "灵巧手", "机器人关节"], "人形机器人", 2),
    (["云计算", "SaaS", "公有云", "AI 训练"], "AI 软件", 2),
    # 1 分：基础设施 / 外延
    (["新能源车", "电动车", "动力电池", "电芯"], "新能源车", 1),
    (["储能", "光伏", "风电", "氢能"], "新能源", 1),
    (["叠层母排", "CCS", "电连接", "连接器", "电控", "电机驱动"], "电气元件", 1),
    (["特种钢", "稀土", "锂", "钴", "镍", "铂族金属"], "上游材料", 1),
    (["智能制造", "工业自动化", "工业机器人", "机器视觉"], "智能制造", 1),
    (["医疗器械", "创新药", "生物制药", "CDMO", "ADC"], "医药", 0),
    (["白酒", "调味品", "乳制品", "化妆品"], "消费", 0),
    (["银行", "保险", "证券", "信托"], "金融", 0),
]


@dataclass
class IpoEntry:
    """单只新股记录。"""
    code: str                              # 股票代码（6 位）
    subscribe_code: str = ""               # 申购代码
    name: str = ""
    board: str = ""                        # main / star / chinext / bse
    subscribe_date: date | None = None     # 申购日
    listing_date: date | None = None       # 上市日（上市后才有）
    issue_price: float | None = None       # 发行价
    pe_ratio: float | None = None          # 发行市盈率
    industry: str = ""                     # 招股说明书行业
    business_desc: str = ""                # 主营业务描述（短文本）

    # 派生字段
    theme: str = ""                        # 主题标签（如"光通信"）
    ai_relevance: int = 0                  # AI 相关性 0-3
    expected_first_day_premium: float | None = None  # 历史同行业首日溢价（百分比）
    status: Literal["upcoming", "awaiting_listing", "recently_listed", "unknown"] = "unknown"

    @property
    def days_to_subscribe(self) -> int | None:
        if self.subscribe_date is None:
            return None
        return (self.subscribe_date - date.today()).days

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("subscribe_date", "listing_date"):
            if d.get(k) and isinstance(d[k], date):
                d[k] = d[k].isoformat()
        d["days_to_subscribe"] = self.days_to_subscribe
        return d


@dataclass
class IpoCalendar:
    """IPO 日历容器。"""
    fetched_at: datetime = field(default_factory=datetime.now)
    upcoming_subscription: list[IpoEntry] = field(default_factory=list)
    awaiting_listing: list[IpoEntry] = field(default_factory=list)
    recently_listed: list[IpoEntry] = field(default_factory=list)
    # 数据源 fetch 状态：{"upcoming": "ok"/"failed", "recent": "ok"/"failed"}
    # "ok" 包括空 DataFrame（接口成功但当日无新股）；"failed" 表示 akshare 报错
    fetch_status: dict[str, str] = field(default_factory=dict)

    def all_entries(self) -> list[IpoEntry]:
        return self.upcoming_subscription + self.awaiting_listing + self.recently_listed

    def ai_relevant(self, min_score: int = 2) -> list[IpoEntry]:
        return [e for e in self.all_entries() if e.ai_relevance >= min_score]

    def fetch_ok(self) -> bool:
        """任一数据源拉成功（即使返回空 df）即视为 ok。"""
        return any(v == "ok" for v in self.fetch_status.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at.isoformat(),
            "fetch_status": dict(self.fetch_status),
            "upcoming_subscription": [e.to_dict() for e in self.upcoming_subscription],
            "awaiting_listing": [e.to_dict() for e in self.awaiting_listing],
            "recently_listed": [e.to_dict() for e in self.recently_listed],
        }


# ───────────── 数据抓取 ─────────────

def _import_ak():
    try:
        import akshare as ak
        return ak
    except ImportError:
        logger.error("akshare not installed; pip install akshare")
        return None


def fetch_upcoming_subscription_raw():
    """抓 akshare 新股申购列表（DataFrame）。"""
    ak = _import_ak()
    if ak is None:
        return None
    try:
        return ak.stock_xgsglb_em(symbol="即将发行")
    except Exception as e:
        logger.warning("akshare stock_xgsglb_em(即将发行) failed: %s", e)
        return None


def fetch_recent_listings_raw():
    """抓近期已上市新股（DataFrame）。"""
    ak = _import_ak()
    if ak is None:
        return None
    try:
        # akshare 提供了已上市的版本（参数因版本不同，"全部股票"或"近三月"）
        return ak.stock_xgsglb_em(symbol="已上市")
    except Exception as e:
        logger.warning("akshare stock_xgsglb_em(已上市) failed: %s", e)
        return None


# ───────────── 主题标签 + AI 相关性 ─────────────

def classify_theme(name: str, business_desc: str = "", industry: str = "") -> tuple[str, int]:
    """根据公司名 + 主营业务 + 行业字段，匹配主题。

    返回 (theme_label, ai_relevance_0_to_3)。
    多主题命中时，取**最高分**那条；同分时取最先匹配的。
    """
    text = f"{name} {business_desc} {industry}".lower()
    best: tuple[str, int] = ("其他", 0)
    for keywords, theme, score in A_SHARE_THEME_RULES:
        for kw in keywords:
            if kw.lower() in text:
                if score > best[1]:
                    best = (theme, score)
                break
    return best


# ───────────── 主入口 ─────────────

def build_ipo_calendar() -> IpoCalendar:
    """一站式构建 IPO 日历。"""
    cal = IpoCalendar()
    today = date.today()

    # 1. 即将申购
    df = fetch_upcoming_subscription_raw()
    cal.fetch_status["upcoming"] = "failed" if df is None else "ok"
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            entry = _row_to_entry(r)
            if entry is None:
                continue
            entry.theme, entry.ai_relevance = classify_theme(
                entry.name, entry.business_desc, entry.industry
            )
            sub = entry.subscribe_date
            if sub is None:
                continue
            if sub >= today:
                entry.status = "upcoming"
                cal.upcoming_subscription.append(entry)
            elif entry.listing_date is None or entry.listing_date >= today:
                entry.status = "awaiting_listing"
                cal.awaiting_listing.append(entry)

    # 2. 近期已上市
    df2 = fetch_recent_listings_raw()
    cal.fetch_status["recent"] = "failed" if df2 is None else "ok"
    if df2 is not None and not df2.empty:
        cutoff = today - timedelta(days=30)
        for _, r in df2.iterrows():
            entry = _row_to_entry(r)
            if entry is None or entry.listing_date is None:
                continue
            if cutoff <= entry.listing_date <= today:
                entry.theme, entry.ai_relevance = classify_theme(
                    entry.name, entry.business_desc, entry.industry
                )
                entry.status = "recently_listed"
                cal.recently_listed.append(entry)

    # 排序
    cal.upcoming_subscription.sort(key=lambda e: e.subscribe_date or date.max)
    cal.awaiting_listing.sort(key=lambda e: e.subscribe_date or date.max)
    cal.recently_listed.sort(key=lambda e: e.listing_date or date.min, reverse=True)

    return cal


def _row_to_entry(row) -> IpoEntry | None:
    """akshare DataFrame 行 → IpoEntry。

    akshare stock_xgsglb_em 字段（典型）：
      股票代码 / 股票简称 / 申购代码 / 发行总数 / 网上发行 / 上市日期 /
      申购日期 / 发行价 / 发行市盈率 / 中签号公布日 / 申购上限 / 中签率 /
      连续一字板数量 / 上市首日开板溢价 / 行业
    """
    code = _norm6(row.get("股票代码") or row.get("代码") or "")
    if not code:
        return None
    sub_code = str(row.get("申购代码") or "").strip()
    name = str(row.get("股票简称") or row.get("名称") or "").strip()
    sub_date = _parse_date(row.get("申购日期"))
    list_date = _parse_date(row.get("上市日期"))
    issue_price = _safe_float(row.get("发行价"))
    pe = _safe_float(row.get("发行市盈率"))
    industry = str(row.get("行业") or row.get("所属行业") or "").strip()
    biz = str(row.get("主营业务") or row.get("业务范围") or "").strip()

    board, _ = _classify_board(code)

    return IpoEntry(
        code=code, subscribe_code=sub_code, name=name, board=board,
        subscribe_date=sub_date, listing_date=list_date,
        issue_price=issue_price, pe_ratio=pe,
        industry=industry, business_desc=biz,
    )


# ───────────── 打新 ROI 估算 ─────────────

def estimate_first_day_premium(industry: str, board: str) -> float | None:
    """基于"行业 + 板块"的历史首日溢价均值估算。

    简化实现：返回经验值；后续可接 ak.stock_xgsr_ths() 真实抓取并加权。

    注：主板首日 44% 涨幅限制；科创/创业/北交无限制。
    """
    # 经验值（2024-2025 平均），按板块 × 行业类型粗略分类
    if board == "main":
        return 0.44                    # 主板新股普遍首日顶到 44% 限制
    elif board in ("star", "chinext"):
        # 科创/创业看行业
        ai_keywords = ["半导体", "通信", "计算机", "电子"]
        return 0.80 if any(k in industry for k in ai_keywords) else 0.45
    elif board == "bse":
        return 0.30                    # 北交所首日波动较小
    return None


# ───────────── 工具 ─────────────

def _classify_board(code: str) -> tuple[str, float]:
    if not code or not code.isdigit() or len(code) != 6:
        return ("unknown", 10.0)
    if code.startswith("688"):
        return ("star", 20.0)
    if code.startswith("300"):
        return ("chinext", 20.0)
    if code.startswith(("60", "000", "001", "002", "003")):
        return ("main", 10.0)
    if code.startswith(("8", "92", "43")):
        return ("bse", 30.0)
    return ("other", 10.0)


def _norm6(code) -> str:
    if not code:
        return ""
    s = str(code).upper().strip()
    for p in ("SH", "SZ", "BJ"):
        if s.startswith(p):
            s = s[len(p):]
    for sfx in (".SS", ".SH", ".SZ", ".BJ"):
        if s.endswith(sfx):
            s = s[:-len(sfx)]
    s = s.lstrip(".")
    digits = "".join(c for c in s if c.isdigit())
    return digits[:6] if len(digits) >= 6 else digits


def _safe_float(v) -> float | None:
    try:
        if v is None or v == "" or v == "-":
            return None
        f = float(v)
        if f != f:
            return None
        return f
    except (TypeError, ValueError):
        return None


def _parse_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    if isinstance(v, datetime):
        return v.date()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
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
    """python -m stock_research.core.ipo_pipeline [--ai-only]"""
    import sys
    ai_only = "--ai-only" in sys.argv

    print("📈 抓取 IPO 日历...")
    cal = build_ipo_calendar()
    print(f"   即将申购: {len(cal.upcoming_subscription)}")
    print(f"   已申购未上市: {len(cal.awaiting_listing)}")
    print(f"   近 30 日上市: {len(cal.recently_listed)}")
    print()

    sections = [
        ("🚀 即将申购", cal.upcoming_subscription),
        ("⏳ 已申购未上市", cal.awaiting_listing),
        ("📊 近 30 日上市", cal.recently_listed),
    ]
    for title, entries in sections:
        if ai_only:
            entries = [e for e in entries if e.ai_relevance >= 2]
        if not entries:
            continue
        print(f"\n{title}")
        print("-" * 110)
        print(f"  {'代码':<8}{'名称':<10}{'板块':<8}{'申购日':<12}{'发行价':>8}  {'AI':>3} {'主题':<14} {'业务'}")
        for e in entries[:30]:
            ai_flag = "🟢" if e.ai_relevance >= 2 else ("🟡" if e.ai_relevance == 1 else "⚪")
            sub_d = e.subscribe_date.strftime("%Y-%m-%d") if e.subscribe_date else "?"
            price = f"¥{e.issue_price:.2f}" if e.issue_price else "?"
            print(f"  {e.code:<8}{e.name:<10}{e.board:<8}{sub_d:<12}{price:>8}  {ai_flag} {e.theme:<14} "
                  f"{(e.business_desc or e.industry)[:40]}")


if __name__ == "__main__":
    _main()
