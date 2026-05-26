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


def get_catalyst(ticker: str, *, lookback_days: int = LOOKBACK_DAYS, today: date | None = None) -> str | None:
    """返回单只 ticker 的最强催化句（**不带 📰 前缀**）；无可用催化返回 None。

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
        return _catalyst_from_earnings_dates(_events_hk().get(tk_upper) or [], today, lookback_days)

    # A 股
    if _is_a_share(tk_upper):
        code = tk_upper.split(".")[0]
        return _catalyst_from_cn_yjbb(_events_cn().get(code) or [], today, lookback_days)

    # 美股（裸 ticker）
    return _catalyst_from_earnings_dates(_events_us().get(tk_upper) or [], today, lookback_days)


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
