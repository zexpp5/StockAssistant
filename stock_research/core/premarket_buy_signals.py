"""盘前可买/别追名单 — 把 buy_zone 套到「自选 + 推荐池 + 瓶颈宇宙」上。

单一来源：盘前飞书日报卡 与 首页盘前预警横幅 都读这里产出的 green/red 名单，
不各算各的（memory: feedback_single_source_no_double_engine）。

定位（研究参考，非投资建议）：
  🟢 可研究 = 现价已跌到 buy_zone 下沿之下（偏便宜）
  🔴 别追   = 现价高于 buy_zone 上沿（偏贵，别追高）
  区间内/未知 不进名单（既不喊买也不喊别追，保持低噪）

默认 US-only：港股 + A 股暂不进名单（用户 2026-06-16：港股和 A 股暂时不需要推荐）。
  这是美股盘前卡，标的就该是美股；include_hk / include_a_share 参数留作以后放开。

票池三来源（去重）：
  ① manual_watchlist        你手动加的自选
  ② recommendation_picks    最新一批 AI 推荐（system_tech_universe）
  ③ bottleneck 宇宙          bottleneck_signals.TICKER_GROUP（GEV/VRT/MU + 云厂 capex 组）

⚠️ buy_zone 目标价覆盖仅 ~33%（memory: project_buy_zone_feature），无区间的票直接不收录，
   所以名单可能偏短、偏美股 —— 这是已知数据缺口，不是 bug。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from stock_research.core import buy_zone
from stock_research.core.bottleneck_signals import TICKER_GROUP

# 票池来源标签（展示用）
SRC_WATCHLIST = "自选"
SRC_PICK = "推荐"
SRC_BOTTLENECK = "瓶颈"


def _gather_universe(conn) -> dict[str, dict[str, str]]:
    """拢三来源票池，返回 {SYMBOL: {"market": .., "sources": "自选/推荐/瓶颈"}}。去重合并来源。"""
    uni: dict[str, dict[str, Any]] = {}

    def _add(sym: str, market: str | None, src: str) -> None:
        s = (sym or "").strip().upper()
        if not s:
            return
        rec = uni.setdefault(s, {"market": market or "US", "sources": []})
        if market and not rec.get("market"):
            rec["market"] = market
        if src not in rec["sources"]:
            rec["sources"].append(src)

    # ① 自选
    try:
        for market, sym in conn.execute(
            "SELECT market, symbol FROM manual_watchlist"
        ).fetchall():
            _add(sym, market, SRC_WATCHLIST)
    except Exception:
        pass

    # ② 最新一批推荐（system_tech_universe，按市场取各自最新 run）
    try:
        rows = conn.execute(
            """
            WITH latest_run AS (
                SELECT rp.market, MAX(rr.generated_at) AS latest_at
                FROM recommendation_runs rr
                JOIN recommendation_picks rp ON rp.run_id = rr.run_id
                WHERE rr.universe_scope = 'system_tech_universe'
                GROUP BY rp.market
            )
            SELECT rp.market, rp.symbol
            FROM recommendation_runs rr
            JOIN recommendation_picks rp ON rp.run_id = rr.run_id
            JOIN latest_run l ON l.market = rp.market AND l.latest_at = rr.generated_at
            """
        ).fetchall()
        for market, sym in rows:
            _add(sym, market, SRC_PICK)
    except Exception:
        pass

    # ③ 瓶颈宇宙（单一来源 bottleneck_signals.TICKER_GROUP，均为美股）
    for sym in TICKER_GROUP:
        _add(sym, "US", SRC_BOTTLENECK)

    return uni


# 富集阈值（研究参考用，非验证过的交易参数）
TARGET_STALE_DAYS = 60       # 目标价超过这么多天 → 标"偏旧"（analyst 一般随季度财报更新）
FALLING_KNIFE_DD_PCT = -20.0  # 近 20 日从高点回撤超过这个 % → 标"可能接飞刀"
KNIFE_WINDOW = 20


def _days_since(d, today: date) -> int | None:
    """目标价事件日距今多少天。兼容 date / datetime / 'YYYY-MM-DD' 字符串。"""
    if d is None:
        return None
    try:
        if isinstance(d, str):
            d = date.fromisoformat(d[:10])
        elif isinstance(d, datetime):
            d = d.date()
        return (today - d).days
    except Exception:
        return None


def _recent_drawdown(conn, symbol: str, window: int = KNIFE_WINDOW):
    """近 window 日「从区间内最高收盘回撤多少 %」。返回 (回撤%, 期间高点)，负数=跌。"""
    try:
        rows = conn.execute(
            "SELECT close FROM price_daily WHERE upper(symbol)=upper(?) AND close IS NOT NULL "
            "ORDER BY trade_date DESC LIMIT ?",
            [symbol, window],
        ).fetchall()
        closes = [float(r[0]) for r in rows if r[0] is not None]
        if len(closes) < 5:
            return None, None
        cur, hi = closes[0], max(closes)
        if hi <= 0:
            return None, None
        return (cur / hi - 1.0) * 100.0, hi
    except Exception:
        return None, None


def _enrich(conn, sym: str, zone: dict, today: date) -> dict:
    """给一只票算 A 折价% / B 目标价新鲜度 / C 接飞刀，返回要并进 item 的字段。"""
    current, target = zone.get("current"), zone.get("target")
    # A：折价/溢价 vs 分析师目标价（仅估值口径有）
    discount_pct = round((current / target - 1.0) * 100.0) if (target and current) else None
    # B：目标价新鲜度
    target_age = _days_since(zone.get("target_date"), today)
    target_stale = target_age is not None and target_age > TARGET_STALE_DAYS
    # C：近期急跌（接飞刀）
    dd_pct, _hi = _recent_drawdown(conn, sym)
    falling_knife = dd_pct is not None and dd_pct <= FALLING_KNIFE_DD_PCT
    flags: list[str] = []
    if target_stale:
        flags.append(f"⚠️目标价{target_age}天前(偏旧,可能没反映最新情况)")
    if falling_knife:
        flags.append(f"⚠️近20日跌{abs(round(dd_pct))}%·可能接飞刀,先查为什么跌")
    return {
        "discount_pct": discount_pct,
        "target_age_days": target_age,
        "target_stale": target_stale,
        "drawdown_pct": round(dd_pct) if dd_pct is not None else None,
        "falling_knife": falling_knife,
        "flags": flags,
    }


def _compact_line(item: dict) -> str:
    """一行人话：方向词 + 折价% + 锚定 + ⚠️旗标。
    MU(自选/瓶颈) 现价 $182 · 低于可买区间 $150~$180 · 比目标价低 24% · 锚:分析师目标价 ⚠️…
    """
    src = "/".join(item.get("sources") or [])
    cur, low, high = item.get("current"), item.get("low"), item.get("high")
    pos, market = item.get("position"), item.get("market", "US")
    mkt = f"·{market}" if market and market != "US" else ""
    parts = [f"**{item['symbol']}**（{src}{mkt}）"]
    parts.append(f"现价 ${cur:.0f}" if cur else "现价未知")
    if low is not None and high is not None:
        rel = "低于" if pos == "便宜" else ("高于" if pos == "偏贵" else "处于")
        parts.append(f"{rel}可买区间 ${low:.0f}~${high:.0f}")
    dp = item.get("discount_pct")
    if dp is not None:
        parts.append(f"比目标价{'低' if dp < 0 else '高'} {abs(dp):.0f}%")
    parts.append("锚:分析师目标价" if item.get("method") == "估值" else "锚:均线回撤")
    line = " · ".join(p for p in parts if p)
    if item.get("flags"):
        line += " " + " ".join(item["flags"])
    return line


def _is_a_share(symbol: str, market: str | None) -> bool:
    """A 股识别：market==CN 或 .SZ/.SS/.SH 后缀。"""
    if (market or "").upper() == "CN":
        return True
    s = (symbol or "").upper()
    return s.endswith(".SZ") or s.endswith(".SS") or s.endswith(".SH")


def _is_hk(symbol: str, market: str | None) -> bool:
    """港股识别：market==HK 或 .HK 后缀。"""
    if (market or "").upper() == "HK":
        return True
    return (symbol or "").upper().endswith(".HK")


def compute_buy_avoid(conn=None, *, today: date | None = None,
                      include_a_share: bool = False,
                      include_hk: bool = False) -> dict[str, Any]:
    """产出盘前 🟢可买 / 🔴别追 名单。

    返回:
      {
        "green": [ {symbol, market, sources, position, current, low, high, method, target, line}, ... ],
        "red":   [ 同上 ],
        "universe_size": N,        # 票池去重后总数
        "zoned": M,                # 其中 buy_zone 算得出区间的数量（覆盖率分子）
        "as_of": "YYYY-MM-DD",
      }
    无数据/连接失败时返回空名单而非抛错（盘前 job 不能因此崩）。
    """
    today = today or date.today()
    own = conn is None
    if own:
        conn, ok = buy_zone._open_conn()
        if not ok or conn is None:
            return {"green": [], "red": [], "universe_size": 0, "zoned": 0,
                    "as_of": today.isoformat()}
    try:
        uni = _gather_universe(conn)
        # 港股 + A 股暂不进推荐名单（用户 2026-06-16：港股和 A 股暂时不需要推荐；
        # 这是美股盘前卡，默认 US-only。memory: feedback_brief_no_a_share）
        def _hidden(sym: str, meta: dict) -> bool:
            mkt = meta.get("market")
            return (not include_a_share and _is_a_share(sym, mkt)) or \
                   (not include_hk and _is_hk(sym, mkt))
        uni = {s: m for s, m in uni.items() if not _hidden(s, m)}
        zones = buy_zone.compute_buy_zones(list(uni.keys()), conn, today=today)
        green: list[dict] = []
        red: list[dict] = []
        for sym, zone in zones.items():
            meta = uni.get(sym, {"market": "US", "sources": []})
            market = meta.get("market", "US")
            sources = meta.get("sources", [])
            item = {
                "symbol": sym,
                "market": market,
                "sources": sources,
                "position": zone.get("position"),
                "current": zone.get("current"),
                "low": zone.get("low"),
                "high": zone.get("high"),
                "method": zone.get("method"),
                "target": zone.get("target"),
                "target_date": zone.get("target_date"),
            }
            item.update(_enrich(conn, sym, zone, today))  # A 折价 / B 新鲜度 / C 接飞刀
            item["line"] = _compact_line(item)
            if zone.get("position") == "便宜":
                green.append(item)
            elif zone.get("position") == "偏贵":
                red.append(item)
        # 便宜的按「离下沿多远」排（越便宜越靠前）；偏贵同理
        green.sort(key=lambda x: (x["current"] / x["low"]) if x.get("low") else 9e9)
        red.sort(key=lambda x: -(x["current"] / x["high"]) if x.get("high") else 0)
        return {
            "green": green,
            "red": red,
            "universe_size": len(uni),
            "zoned": len(zones),
            "as_of": today.isoformat(),
        }
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass
