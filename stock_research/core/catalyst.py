"""Catalyst (📰 为啥推) — 三市场统一 helper。

合并 A 股 + 港股 + 美股的事件日历，给定 ticker 返回近 60 天最强催化一句话。
之前在 morning_brief._build_catalyst 和 build_stock_dashboard_html._build_catalyst_index
两处各有一份实现；这里抽成 single source，两边都引用。

数据源：
  - data/event_calendar.json          A 股 akshare 财报 + 解禁 + 减增持
  - data/event_calendar_hk.json       港股 yfinance 财报 + EPS 超预期
  - data/event_calendar_us.json       美股 yfinance 财报 + EPS 超预期

返回的句子**不带 📰 前缀和缩进**——caller 自己拼，例如：
  - morning_brief: f"  📰 {sentence}"
  - dashboard:      f"📰 {sentence}"
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

LOOKBACK_DAYS = 60
UPCOMING_DAYS = 14

# Module-level cache（一次 build 内多次调用复用）
_CACHE: dict[str, dict[str, list[dict]] | None] = {
    "hk": None,
    "cn": None,
    "us": None,
    "hkex": None,
    "us_sec": None,
    "us_form4": None,
}

# 港股 HKEX 事件类型 → 优先级（数字小 = 优先用作 catalyst 句）
# 盈警/盈喜最强信号，并购次之；业绩公告跟 yfinance 重叠（yfinance 含 EPS 数字更可读，不用 HKEX）
# 股东减增持 / 回购最弱（腾讯天天回购，无信号量）
HKEX_PRIORITY = {
    "earnings_preview": 0,   # 盈警/盈喜
    "major_order":      1,   # 重大订单 / 合约 / 战略合作（A5 NLP）
    "ma_takeover":      1,   # 并购/私有化/要约
    "trading_halt":     2,   # 停牌/复牌
    # earnings_announcement: 不优先（yfinance 含 EPS 数字更可用）
    "insider_change":   4,   # 股东减增持
    "buyback":          5,   # 回购（弱信号）
}

# major_order 金额阈值（CNY），低于此值降为弱信号 priority=4
MAJOR_ORDER_CNY_THRESHOLD = 1e8  # 1 亿 CNY

# 美股 SEC 事件优先级
US_SEC_PRIORITY = {
    "active_holder_change":  0,  # SC 13D - 主动持仓往往含收购意图
    "passive_holder_change": 1,  # SC 13G - 大资金进入
    # 8-K material_event 暂时不放强催化（title 太 generic，要等 Item 解析）
    "material_event":        4,
    "proxy":                 5,  # 股东大会通知（信号弱）
}


def _load_json(rel: str) -> dict:
    p = REPO / rel
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _events_hk() -> dict[str, list[dict]]:
    if _CACHE["hk"] is None:
        d = _load_json("data/event_calendar_hk.json")
        idx: dict[str, list[dict]] = {}
        for e in (d.get("events") or []):
            t = (e.get("ticker") or "").upper()
            if t:
                idx.setdefault(t, []).append(e)
        _CACHE["hk"] = idx
    return _CACHE["hk"]


def _events_us() -> dict[str, list[dict]]:
    if _CACHE["us"] is None:
        d = _load_json("data/event_calendar_us.json")
        idx: dict[str, list[dict]] = {}
        for e in (d.get("events") or []):
            t = (e.get("ticker") or "").upper()
            if t:
                idx.setdefault(t, []).append(e)
        _CACHE["us"] = idx
    return _CACHE["us"]


def _events_hkex() -> dict[str, list[dict]]:
    """HKEX 披露易公告（按 ticker 分组）。"""
    if _CACHE["hkex"] is None:
        d = _load_json("data/event_calendar_hk_hkex.json")
        idx: dict[str, list[dict]] = {}
        for e in (d.get("events") or []):
            t = (e.get("ticker") or "").upper()
            if t:
                idx.setdefault(t, []).append(e)
        _CACHE["hkex"] = idx
    return _CACHE["hkex"]


def _events_us_form4() -> dict[str, list[dict]]:
    """Form 4 内部人交易（每只 ticker 一条聚合事件）。"""
    if _CACHE["us_form4"] is None:
        d = _load_json("data/event_calendar_us_form4.json")
        idx: dict[str, list[dict]] = {}
        for e in (d.get("events") or []):
            t = (e.get("ticker") or "").upper()
            if t:
                idx.setdefault(t, []).append(e)
        _CACHE["us_form4"] = idx
    return _CACHE["us_form4"]


def _events_us_sec() -> dict[str, list[dict]]:
    """美股 SEC EDGAR filings（按 ticker 分组）。"""
    if _CACHE["us_sec"] is None:
        d = _load_json("data/event_calendar_us_sec.json")
        idx: dict[str, list[dict]] = {}
        for e in (d.get("events") or []):
            t = (e.get("ticker") or "").upper()
            if t:
                idx.setdefault(t, []).append(e)
        _CACHE["us_sec"] = idx
    return _CACHE["us_sec"]


def _events_cn() -> dict[str, list[dict]]:
    if _CACHE["cn"] is None:
        d = _load_json("data/event_calendar.json")
        idx: dict[str, list[dict]] = {}
        for e in (d.get("events") or []):
            code = e.get("code", "")
            if code:
                idx.setdefault(code, []).append(e)
        _CACHE["cn"] = idx
    return _CACHE["cn"]


def reset_cache() -> None:
    """供测试或 daily_refresh 中途切换数据后强制重读。"""
    for k in _CACHE:
        _CACHE[k] = None


def _is_a_share(ticker: str) -> bool:
    """ticker 是否 A 股（.SH/.SS/.SZ/.BJ 后缀）。"""
    t = (ticker or "").upper()
    return t.endswith(".SH") or t.endswith(".SS") or t.endswith(".SZ") or t.endswith(".BJ")


def get_catalyst(ticker: str, *, lookback_days: int = LOOKBACK_DAYS, today: date | None = None,
                 include_summary: bool = True) -> str | None:
    """返回单只 ticker 的最强催化句（**不带 📰 前缀**）；无可用催化返回 None。

    include_summary=False 时美股 8-K 不拼英文原文摘录段，只留中文标签 + 日期
    （早报卡片用：原文摘录截断后是半句英文噪声；dashboard 保持默认 True）。

    例子：
      `get_catalyst("0992.HK")` → `"5/21 EPS 实际 0.04 / 估 0.03，超预期 +58.0%（4d 前）"`
      `get_catalyst("300001.SZ")` → `"4/28 财报：净利润同比 +11.1%（27d 前）"`
      `get_catalyst("MCD")` → `"5/7 EPS 实际 2.83 / 估 2.74，超预期 +3.1%（19d 前）"`
    """
    if not ticker:
        return None
    today = today or date.today()
    tk_upper = ticker.upper()

    # 港股
    if tk_upper.endswith(".HK"):
        # 强催化优先：HKEX 盈警/并购/停牌 (priority 0-2)
        strong = _catalyst_from_hkex(_events_hkex().get(tk_upper) or [], today, lookback_days, max_priority=2)
        if strong:
            return strong
        # 次强：yfinance EPS 超预期（有数字最直观）
        eps = _catalyst_from_earnings_dates(_events_hk().get(tk_upper) or [], today, lookback_days)
        if eps:
            return eps
        # 兜底：HKEX 弱催化（股东减增持/回购，priority 3-5）
        return _catalyst_from_hkex(_events_hkex().get(tk_upper) or [], today, lookback_days, max_priority=9)

    # A 股
    if _is_a_share(tk_upper):
        code = tk_upper.split(".")[0]
        return _catalyst_from_cn_yjbb(_events_cn().get(code) or [], today, lookback_days)

    # 美股（裸 ticker）— 四段优先级链
    # 1. SEC 强催化 (max_priority=2) — 含 13D/13G + 8-K 强 Items (1.01 重大协议 / 5.02 高管变动 / 1.03 破产 / 2.01 收购 / 3.01 退市)
    strong = _catalyst_from_sec(_events_us_sec().get(tk_upper) or [], today, lookback_days,
                                max_priority=2, include_summary=include_summary)
    if strong:
        return strong
    # 2. Form 4 内部人净买入/卖出（已经按 |净额|≥$1M 过滤）
    form4 = _catalyst_from_form4(_events_us_form4().get(tk_upper) or [], today, lookback_days)
    if form4:
        return form4
    # 3. yfinance EPS 超预期
    eps = _catalyst_from_earnings_dates(_events_us().get(tk_upper) or [], today, lookback_days)
    if eps:
        return eps
    # 4. SEC 弱催化兜底（8-K 弱 Item priority 3-5 / DEF 14A）
    return _catalyst_from_sec(_events_us_sec().get(tk_upper) or [], today, lookback_days,
                              max_priority=9, include_summary=include_summary)


def _catalyst_from_hkex(events: list[dict], today: date, lookback_days: int, max_priority: int = 9) -> str | None:
    """HKEX 公告 → 催化句。按事件类型优先级 + 日期新近度选最强。
    `max_priority` 控制只考虑优先级 ≤ max_priority 的事件类型。
    """
    if not events:
        return None
    # 收集 lookback 窗口内符合优先级的事件
    candidates: list[tuple[int, date, dict]] = []
    for e in events:
        etype = e.get("event_type", "")
        prio = HKEX_PRIORITY.get(etype)
        if prio is None:
            continue
        # major_order 金额阈值：< 1 亿 CNY 降为弱信号
        if etype == "major_order":
            amt_cny = e.get("amount_cny_approx") or 0
            if amt_cny > 0 and amt_cny < MAJOR_ORDER_CNY_THRESHOLD:
                prio = 4  # 降级
        if prio > max_priority:
            continue
        try:
            ed = datetime.strptime(e.get("event_date", ""), "%Y-%m-%d").date()
        except Exception:
            continue
        if not (0 <= (today - ed).days <= lookback_days):
            continue
        candidates.append((prio, ed, e))
    if not candidates:
        return None
    # 先按 priority 升序，再按日期降序（更新优先）
    candidates.sort(key=lambda x: (x[0], -(x[1].toordinal())))
    _, ed, e = candidates[0]
    days_ago = (today - ed).days
    etype = e.get("event_type", "")
    title = (e.get("title") or "").strip()
    # 标题截断到 30 字避免过长
    if len(title) > 30:
        title = title[:28] + "…"

    label_map = {
        "earnings_preview": "📢 业绩预告",
        "major_order":      "📜 重大订单/合约",
        "ma_takeover":      "🤝 并购/要约",
        "trading_halt":     "🛑 停牌/复牌",
        "insider_change":   "👥 股东权益变动",
        "buyback":          "💰 回购",
        "earnings_announcement": "📋 业绩公告",
    }
    prefix = label_map.get(etype, "📄 公告")
    # major_order 优先展示「客户 + 金额」(更直观),普通的 title 跟在后面
    customer = e.get("customer") or ""
    amt = e.get("amount_text") or ""
    if etype == "major_order" and (customer or amt):
        parts = []
        if customer:
            parts.append(f"客户 {customer}")
        if amt:
            parts.append(amt)
        kv = " · ".join(parts)
        return f"{ed.strftime('%-m/%-d')} {prefix}：{kv}（{days_ago}d 前）"
    amt_suffix = f"（{amt}）" if amt else ""
    return f"{ed.strftime('%-m/%-d')} {prefix}：{title}{amt_suffix}（{days_ago}d 前）"


def _catalyst_from_form4(events: list[dict], today: date, lookback_days: int) -> str | None:
    """Form 4 内部人净买入/净卖出 → 催化句。
    输入 events 一般只有 1 条（每 ticker 聚合 60 天）。
    """
    if not events:
        return None
    # 取最大净额的一条（防御性，正常只 1 条）
    e = max(events, key=lambda x: abs(x.get("net_amount_usd") or 0))
    try:
        ed = datetime.strptime(e.get("event_date", ""), "%Y-%m-%d").date()
    except Exception:
        return None
    if (today - ed).days > lookback_days + 7:  # 容忍 7d 缓冲
        return None
    net = e.get("net_amount_usd") or 0
    abs_m = abs(net) / 1_000_000
    days_ago = (today - ed).days
    if e.get("event_type") == "insider_net_buy":
        return f"{ed.strftime('%-m/%-d')} 🟢 内部人净买入 ${abs_m:.1f}M（60d 累计，最新申报 {days_ago}d 前）"
    return f"{ed.strftime('%-m/%-d')} 🔴 内部人净卖出 ${abs_m:.1f}M（60d 累计，最新申报 {days_ago}d 前）"


def _catalyst_from_sec(events: list[dict], today: date, lookback_days: int, max_priority: int = 9,
                       include_summary: bool = True) -> str | None:
    """SEC EDGAR filings → 催化句。按 event_type 优先级 + 日期新近度。
    8-K 用 item_priority 覆盖 form-level priority（item 解析后能精确分级）。
    """
    if not events:
        return None
    candidates: list[tuple[int, date, dict]] = []
    for e in events:
        etype = e.get("event_type", "")
        prio = US_SEC_PRIORITY.get(etype)
        if prio is None:
            continue
        # 8-K：用 item_priority 修正（item 5.02 高管变动 = 2 比 form-level 4 强）
        if etype == "material_event" and isinstance(e.get("item_priority"), int):
            prio = e["item_priority"]
        if prio > max_priority:
            continue
        try:
            ed = datetime.strptime(e.get("event_date", ""), "%Y-%m-%d").date()
        except Exception:
            continue
        if not (0 <= (today - ed).days <= lookback_days):
            continue
        candidates.append((prio, ed, e))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], -(x[1].toordinal())))
    _, ed, e = candidates[0]
    days_ago = (today - ed).days
    etype = e.get("event_type", "")
    form = e.get("form", "")
    # 8-K 用 item_label 覆盖
    if etype == "material_event" and e.get("item_label"):
        label = e["item_label"]
    else:
        label_map = {
            "active_holder_change":   "🎯 大股东主动持仓（13D 常含收购意图）",
            "passive_holder_change":  "👥 大股东被动持仓（13G）",
            "material_event":         "📣 8-K 重大事件",
            "proxy":                  "🗳️ 股东大会",
        }
        label = label_map.get(etype, f"📄 {form}")
    # 8-K 有 item_summaries 时拼一段摘要（取主标 item 对应的段落）
    summary = ""
    if include_summary and etype == "material_event" and isinstance(e.get("item_summaries"), dict):
        sums = e["item_summaries"]
        primary_item = e.get("primary_item") or ""
        # 优先取主标对应段落，没有则按 priority 顺序回退
        if primary_item and primary_item in sums and sums[primary_item]:
            txt = sums[primary_item]
        else:
            # 回退：取 dict 第一个非空段落
            txt = ""
            for it, t in sums.items():
                if t:
                    txt = t
                    break
        if txt:
            # 截 90 字预览（够看出主要意思,catalyst 行不要太长）
            summary = txt[:90].strip()
            if len(txt) > 90:
                summary += "…"
    if summary:
        return f"{ed.strftime('%-m/%-d')} {label}（{days_ago}d 前）— {summary}"
    return f"{ed.strftime('%-m/%-d')} {label}（{days_ago}d 前）"


def _catalyst_from_earnings_dates(events: list[dict], today: date, lookback_days: int) -> str | None:
    """yfinance schema (HK/US): 用 surprise_pct 绝对值最大的近期 earnings。"""
    if not events:
        return None
    recent: list[tuple[date, dict]] = []
    upcoming: list[tuple[date, dict]] = []
    for e in events:
        try:
            ed = datetime.strptime(e.get("event_date", ""), "%Y-%m-%d").date()
        except Exception:
            continue
        if e.get("event_type") == "earnings" and 0 <= (today - ed).days <= lookback_days:
            recent.append((ed, e))
        elif e.get("event_type") == "earnings_upcoming" and 0 <= (ed - today).days <= UPCOMING_DAYS:
            upcoming.append((ed, e))

    if recent:
        recent.sort(key=lambda x: abs((x[1].get("surprise_pct") or 0)), reverse=True)
        ed, e = recent[0]
        days_ago = (today - ed).days
        surp = e.get("surprise_pct")
        est = e.get("eps_estimate")
        act = e.get("eps_actual")
        if surp is not None and est is not None and act is not None:
            sign = "超预期" if surp > 0 else "差预期"
            return f"{ed.strftime('%-m/%-d')} EPS 实际 {act:.2f} / 估 {est:.2f}，{sign} {surp:+.1f}%（{days_ago}d 前）"
        return f"{ed.strftime('%-m/%-d')} 财报已披露（{days_ago}d 前）"
    if upcoming:
        upcoming.sort(key=lambda x: x[0])
        ed, e = upcoming[0]
        days_to = (ed - today).days
        est = e.get("eps_estimate")
        est_label = f"EPS 估 {est:.2f}" if isinstance(est, (int, float)) else "EPS 估 n/a"
        return f"{ed.strftime('%-m/%-d')} 财报临近（+{days_to}d，{est_label}）"
    return None


def _catalyst_from_cn_yjbb(events: list[dict], today: date, lookback_days: int) -> str | None:
    """A 股 akshare 业绩报表：magnitude = 净利润同比小数。"""
    if not events:
        return None
    recent: list[tuple[date, dict]] = []
    for e in events:
        if e.get("event_type") != "earnings":
            continue
        try:
            ed = datetime.strptime(e.get("event_date", ""), "%Y-%m-%d").date()
        except Exception:
            continue
        if 0 <= (today - ed).days <= lookback_days:
            recent.append((ed, e))
    if not recent:
        return None
    recent.sort(key=lambda x: x[0], reverse=True)
    ed, e = recent[0]
    days_ago = (today - ed).days
    mag = e.get("magnitude") or 0
    return f"{ed.strftime('%-m/%-d')} 财报：净利润同比 {mag*100:+.1f}%（{days_ago}d 前）"


def build_index(*, prefix: str = "📰 ") -> dict[str, str]:
    """三市场全量 ticker → 带 📰 前缀的催化句字典。供 dashboard 一次性注入用。

    A 股 candidate 端 ticker 可能含 .SH/.SS/.SZ/.BJ 多种后缀，这里 6 位 code 都对照填充。
    """
    out: dict[str, str] = {}

    for tk in _events_hk():
        s = get_catalyst(tk)
        if s:
            out[tk] = f"{prefix}{s}"
    for tk in _events_us():
        s = get_catalyst(tk)
        if s:
            out[tk] = f"{prefix}{s}"
    for code in _events_cn():
        s = get_catalyst(f"{code}.SS")  # 用任一 A 股后缀触发分支即可
        if s:
            for suffix in (".SH", ".SS", ".SZ", ".BJ", ""):
                out[f"{code}{suffix}".upper()] = f"{prefix}{s}"

    return out


# CLI 自检
if __name__ == "__main__":
    for t in ["0992.HK", "300001.SZ", "MCD", "NVDA", "601318.SS"]:
        print(f"{t:12s} -> {get_catalyst(t)}")
    print(f"\nbuild_index() 共 {len(build_index())} 条")
