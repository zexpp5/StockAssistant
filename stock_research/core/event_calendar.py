"""A 股事件日历：解禁、大股东减持、财报公告 — 风险加权 + PEAD 因子修复。

为什么要做：
  当前系统的 PEAD 因子（业绩公告后漂移）用的是硬编码周期，等于半残废。
  另外解禁/减持是 A 股**短期负 alpha**事件（5 个交易日内显著下跌）：
    - 解禁前 5 日平均超额 -1.5% 至 -2.3%（孔东民 2008 经济研究）
    - 大股东减持公告后 30 日平均超额 -3.5%（叶建华 2014 金融研究）
  这两个不补，因子模型推荐 = 看着光鲜实则前面有坑。

学术依据：
  - Ball & Brown (1968) JAR ：业绩公告后漂移，需用真实公告日
  - Bernard & Thomas (1989) JAE：PEAD effect 在公告日后 60 个交易日衰减
  - 廖理 & 沈红波 (2009)：A 股限售股解禁前抛压，事件研究法 -3 至 +3 日窗口超额负
  - Manchiraju et al. (2017)：大股东减持是负面信息传递信号

数据源（akshare）：
  - ak.stock_restricted_release_queue_em()  限售股解禁队列
  - ak.stock_ggcg_em()                       高管/股东持股变动
  - ak.stock_yjbb_em(date=YYYYMMDD)          业绩报表（含公告日）
  - ak.stock_yjyg_em(date=YYYYMMDD)          业绩预告

输出：
  EventCalendar.events — list[StockEvent]
  EventCalendar.risk_score(code, today) — float ∈ [0, 1]，越低越要避开
  EventCalendar.next_earnings_date(code) — 下次财报公告日（PEAD 用）
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Any, Literal

logger = logging.getLogger(__name__)


EventType = Literal["unlock", "insider_reduce", "insider_increase", "earnings", "earnings_preview"]


@dataclass
class StockEvent:
    """单一事件记录。"""
    code: str
    event_date: date
    event_type: EventType
    magnitude: float = 0.0           # 事件量级（解禁市值/元，减持金额/元，业绩超预期 pct）
    description: str = ""
    source: str = ""                  # akshare 接口名

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["event_date"] = self.event_date.isoformat() if isinstance(self.event_date, date) else str(self.event_date)
        return d


@dataclass
class EventCalendar:
    """事件日历容器。"""
    events: list[StockEvent] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.now)

    def by_code(self, code: str) -> list[StockEvent]:
        c6 = _norm6(code)
        return [e for e in self.events if e.code == c6]

    def upcoming(self, code: str, today: date | None = None,
                 horizon_days: int = 30) -> list[StockEvent]:
        """获取 [today, today+horizon] 窗口内、该股票的事件（按日期升序）。"""
        today = today or date.today()
        c6 = _norm6(code)
        return sorted(
            [e for e in self.events
             if e.code == c6 and today <= e.event_date <= today + timedelta(days=horizon_days)],
            key=lambda e: e.event_date,
        )

    def risk_score(self, code: str, today: date | None = None) -> float:
        """事件风险评分（0-1，越低越要避开）。

        评分逻辑（线性叠加，最低 0.0）：
          基础 1.0
          - 7 日内有大额解禁（> 1 亿）→ -0.4
          - 30 日内有大额解禁（> 5 亿）→ -0.3
          - 30 日内有大股东减持公告 → -0.2
          + 30 日内有大股东增持公告 → +0.1
          + 5 日内业绩超预期公告 → +0.15
        """
        today = today or date.today()
        c6 = _norm6(code)
        score = 1.0
        for e in self.events:
            if e.code != c6:
                continue
            days_to = (e.event_date - today).days
            if e.event_type == "unlock" and 0 <= days_to <= 7 and e.magnitude > 1e8:
                score -= 0.4
            elif e.event_type == "unlock" and 0 <= days_to <= 30 and e.magnitude > 5e8:
                score -= 0.3
            elif e.event_type == "insider_reduce" and -30 <= days_to <= 30:
                score -= 0.2
            elif e.event_type == "insider_increase" and -30 <= days_to <= 30:
                score += 0.1
            elif e.event_type == "earnings" and -5 <= days_to <= 0 and e.magnitude > 0.10:
                score += 0.15
        return max(0.0, min(1.0, score))

    def next_earnings_date(self, code: str, today: date | None = None) -> date | None:
        """返回该股票下一次财报公告日（PEAD 因子用）。"""
        today = today or date.today()
        c6 = _norm6(code)
        future = sorted(
            [e for e in self.events
             if e.code == c6 and e.event_type in ("earnings", "earnings_preview")
             and e.event_date >= today],
            key=lambda e: e.event_date,
        )
        return future[0].event_date if future else None

    def last_earnings_date(self, code: str, today: date | None = None) -> date | None:
        """返回该股票最近一次已公告的财报日（PEAD 事件窗口起点）。"""
        today = today or date.today()
        c6 = _norm6(code)
        past = sorted(
            [e for e in self.events
             if e.code == c6 and e.event_type == "earnings" and e.event_date < today],
            key=lambda e: e.event_date,
            reverse=True,
        )
        return past[0].event_date if past else None


# ───────────── 数据抓取 ─────────────

def _import_ak():
    try:
        import akshare as ak
        return ak
    except ImportError:
        logger.error("akshare not installed; pip install akshare")
        return None


def fetch_unlock_events(start: date, end: date) -> list[StockEvent]:
    """限售股解禁队列（解禁市值/解禁日）。"""
    ak = _import_ak()
    if ak is None:
        return []
    out: list[StockEvent] = []
    try:
        df = ak.stock_restricted_release_queue_em()
    except Exception as e:
        logger.warning("akshare stock_restricted_release_queue_em failed: %s", e)
        return out
    if df is None or df.empty:
        return out

    code_col = _pick_col(df, ["代码", "股票代码", "证券代码"])
    date_col = _pick_col(df, ["解禁时间", "解禁日期"])
    value_col = _pick_col(df, ["解禁市值", "实际解禁市值", "解禁股份市值"])
    name_col = _pick_col(df, ["名称", "证券简称"])

    if not code_col or not date_col:
        logger.warning("unlock data missing required columns: %s", df.columns.tolist())
        return out

    for _, r in df.iterrows():
        d = _parse_date(r.get(date_col))
        if d is None or not (start <= d <= end):
            continue
        c6 = _norm6(str(r.get(code_col, "")))
        if not c6:
            continue
        v = _safe_float(r.get(value_col)) if value_col else 0.0
        name = str(r.get(name_col, "")) if name_col else ""
        out.append(StockEvent(
            code=c6, event_date=d, event_type="unlock",
            magnitude=v or 0.0,
            description=f"{name} 解禁，市值 ¥{(v or 0)/1e8:.2f}亿",
            source="akshare/stock_restricted_release_queue_em",
        ))
    return out


def fetch_insider_change_events(start: date, end: date) -> list[StockEvent]:
    """高管/大股东持股变动（增持 / 减持）。"""
    ak = _import_ak()
    if ak is None:
        return []
    out: list[StockEvent] = []
    try:
        df = ak.stock_ggcg_em()
    except Exception as e:
        logger.warning("akshare stock_ggcg_em failed: %s", e)
        return out
    if df is None or df.empty:
        return out

    code_col = _pick_col(df, ["代码", "股票代码", "证券代码"])
    date_col = _pick_col(df, ["变动日期", "变动截止日期", "公告日期"])
    direction_col = _pick_col(df, ["变动方向", "变动类型", "增减持"])
    qty_col = _pick_col(df, ["变动数量", "变动比例"])
    value_col = _pick_col(df, ["变动金额", "成交均价"])
    name_col = _pick_col(df, ["名称", "证券简称"])

    if not code_col or not date_col:
        logger.warning("insider data missing required columns: %s", df.columns.tolist())
        return out

    for _, r in df.iterrows():
        d = _parse_date(r.get(date_col))
        if d is None or not (start <= d <= end):
            continue
        c6 = _norm6(str(r.get(code_col, "")))
        if not c6:
            continue
        direction = str(r.get(direction_col, "")) if direction_col else ""
        is_reduce = ("减" in direction) or ("卖" in direction)
        is_increase = ("增" in direction) or ("买" in direction)
        if not (is_reduce or is_increase):
            continue
        qty = _safe_float(r.get(qty_col)) if qty_col else 0.0
        amt = _safe_float(r.get(value_col)) if value_col else 0.0
        name = str(r.get(name_col, "")) if name_col else ""
        out.append(StockEvent(
            code=c6, event_date=d,
            event_type="insider_reduce" if is_reduce else "insider_increase",
            magnitude=abs(qty or amt or 0.0),
            description=f"{name} {'减持' if is_reduce else '增持'} {qty or 0:.0f}股",
            source="akshare/stock_ggcg_em",
        ))
    return out


def fetch_earnings_events(year: int, quarter: int) -> list[StockEvent]:
    """业绩报表（含公告日）— 用于 PEAD 因子的真实事件窗口。

    年份 + 季度 → akshare 期望的 YYYYMMDD（季末日期）：
      Q1 → 0331, Q2 → 0630, Q3 → 0930, Q4 → 1231
    """
    ak = _import_ak()
    if ak is None:
        return []
    out: list[StockEvent] = []
    qend = {1: "0331", 2: "0630", 3: "0930", 4: "1231"}[quarter]
    period = f"{year}{qend}"
    try:
        df = ak.stock_yjbb_em(date=period)
    except Exception as e:
        logger.warning("akshare stock_yjbb_em(%s) failed: %s", period, e)
        return out
    if df is None or df.empty:
        return out

    code_col = _pick_col(df, ["股票代码", "代码"])
    name_col = _pick_col(df, ["股票简称", "名称"])
    notice_col = _pick_col(df, ["最新公告日期", "公告日期", "首次公告日期"])
    rev_yoy_col = _pick_col(df, ["营业收入-同比增长", "营收同比"])
    eps_yoy_col = _pick_col(df, ["净利润-同比增长", "净利润同比"])

    if not code_col or not notice_col:
        logger.warning("earnings data missing required columns: %s", df.columns.tolist())
        return out

    for _, r in df.iterrows():
        d = _parse_date(r.get(notice_col))
        if d is None:
            continue
        c6 = _norm6(str(r.get(code_col, "")))
        if not c6:
            continue
        rev_yoy = _safe_float(r.get(rev_yoy_col)) if rev_yoy_col else 0.0
        eps_yoy = _safe_float(r.get(eps_yoy_col)) if eps_yoy_col else 0.0
        # magnitude = 净利润同比增长率（百分比，用于 PEAD 加权）
        magnitude = (eps_yoy or 0.0) / 100.0
        name = str(r.get(name_col, "")) if name_col else ""
        out.append(StockEvent(
            code=c6, event_date=d, event_type="earnings",
            magnitude=magnitude,
            description=f"{name} {year}Q{quarter} 财报，营收同比 {rev_yoy or 0:+.1f}% / 利润同比 {eps_yoy or 0:+.1f}%",
            source=f"akshare/stock_yjbb_em@{period}",
        ))
    return out


# ───────────── 主入口：构建日历 ─────────────

def build_calendar(*,
                   horizon_unlock_days: int = 90,
                   horizon_insider_days: int = 60,
                   include_earnings: bool = True,
                   earnings_quarters: list[tuple[int, int]] | None = None
                   ) -> EventCalendar:
    """一站式构建事件日历。

    参数：
      horizon_unlock_days   解禁未来窗口（默认 90 天）
      horizon_insider_days  减持/增持过去 + 未来窗口（默认 ±60 天）
      include_earnings      是否抓财报（耗时，默认 True）
      earnings_quarters     [(year, quarter), ...]；默认抓最近 4 季

    用法：
      cal = build_calendar()
      cal.risk_score("600519")
      cal.next_earnings_date("000651")
    """
    today = date.today()
    events: list[StockEvent] = []

    # 1. 解禁
    unlock = fetch_unlock_events(today, today + timedelta(days=horizon_unlock_days))
    events.extend(unlock)
    logger.info("解禁事件 %d 条", len(unlock))

    # 2. 减持/增持
    insider = fetch_insider_change_events(
        today - timedelta(days=horizon_insider_days),
        today + timedelta(days=horizon_insider_days),
    )
    events.extend(insider)
    logger.info("增减持事件 %d 条", len(insider))

    # 3. 财报公告
    if include_earnings:
        if earnings_quarters is None:
            earnings_quarters = _recent_quarters(n=4, today=today)
        for y, q in earnings_quarters:
            er = fetch_earnings_events(y, q)
            events.extend(er)
            logger.info("财报事件 %d 条 (Q%dY%d)", len(er), q, y)

    return EventCalendar(events=events)


def _recent_quarters(n: int, today: date | None = None) -> list[tuple[int, int]]:
    """最近 n 个季度（按当前日期回溯）。"""
    today = today or date.today()
    cur_q = (today.month - 1) // 3 + 1
    cur_y = today.year
    out = []
    for _ in range(n):
        out.append((cur_y, cur_q))
        cur_q -= 1
        if cur_q < 1:
            cur_q = 4
            cur_y -= 1
    return out


# ───────────── PEAD 因子（基于事件日历）─────────────

def pead_factor(code: str, calendar: EventCalendar, today: date | None = None,
                window_days: int = 60) -> dict[str, Any]:
    """基于真实公告日的 PEAD 因子计算。

    替代 factor_model_china 里硬编码周期的逻辑。

    返回 {
      "in_event_window": bool,           # 当前是否在 PEAD 窗口内
      "days_since_announcement": int,    # 距上次公告天数
      "earnings_surprise": float,        # 同比增速（用作 surprise 代理）
      "score": float                     # 0-1 综合分（窗口内 + 高 surprise → 高分）
    }
    """
    today = today or date.today()
    last = calendar.last_earnings_date(code, today)
    if last is None:
        return {"in_event_window": False, "days_since_announcement": None,
                "earnings_surprise": 0.0, "score": 0.5,
                "note": "无最近财报记录"}

    days_since = (today - last).days
    in_window = 0 < days_since <= window_days

    # 取该次公告的 magnitude（净利润同比）
    last_event = next(
        (e for e in calendar.events
         if e.code == _norm6(code) and e.event_type == "earnings" and e.event_date == last),
        None,
    )
    surprise = last_event.magnitude if last_event else 0.0

    # 评分：在窗口内 × surprise 强度
    if not in_window:
        score = 0.5
    else:
        # surprise 0 → 0.5；surprise +30% → 1.0；surprise -30% → 0.0
        score = 0.5 + max(-0.5, min(0.5, surprise / 0.6))

    return {
        "in_event_window": in_window,
        "days_since_announcement": days_since,
        "earnings_surprise": surprise,
        "score": round(score, 4),
        "last_announcement": last.isoformat(),
    }


# ───────────── 工具 ─────────────

def _norm6(code: str) -> str:
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


def _pick_col(df, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


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
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    # 尝试 pandas
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
    """python -m stock_research.core.event_calendar [code1 code2 ...]"""
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        args = ["600519", "300308", "688256", "002594"]

    print(f"📅 事件日历（含解禁/减持/财报）— 抓取中...\n")
    cal = build_calendar()

    print(f"  共 {len(cal.events)} 条事件\n")
    today = date.today()
    for code in args:
        upcoming = cal.upcoming(code, today, horizon_days=90)
        risk = cal.risk_score(code, today)
        next_e = cal.next_earnings_date(code, today)
        last_e = cal.last_earnings_date(code, today)

        print(f"  {code} | 风险分 {risk:.2f} | 下次财报 {next_e or '?'} | 上次财报 {last_e or '?'}")
        for e in upcoming[:5]:
            d_to = (e.event_date - today).days
            print(f"    +{d_to:>3}d [{e.event_type:<18}] {e.description}")
        if not upcoming:
            print(f"    （未来 90 天无事件）")
        print()


if __name__ == "__main__":
    _main()
