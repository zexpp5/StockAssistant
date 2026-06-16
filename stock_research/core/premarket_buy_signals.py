"""盘前可买/别追名单 — 把 buy_zone 套到「自选 + 推荐池 + 瓶颈宇宙」上。

单一来源：盘前飞书日报卡 与 首页盘前预警横幅 都读这里产出的 green/red 名单，
不各算各的（memory: feedback_single_source_no_double_engine）。

定位（研究参考，非投资建议）：
  🟢 可研究 = 现价已跌到 buy_zone 下沿之下（偏便宜）
  🔴 别追   = 现价高于 buy_zone 上沿（偏贵，别追高）
  区间内/未知 不进名单（既不喊买也不喊别追，保持低噪）

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


def _compact_line(sym: str, market: str, sources: list[str], zone: dict) -> str:
    """一行人话：MU(自选/瓶颈) 现价$182 · 可买$150~$182 · 锚:目标价。"""
    src = "/".join(sources)
    cur = zone.get("current")
    low, high = zone.get("low"), zone.get("high")
    cur_str = f"现价 ${cur:.0f}" if cur else "现价未知"
    band = f"区间 ${low:.0f}~${high:.0f}" if (low is not None and high is not None) else ""
    anchor = "锚:目标价" if zone.get("method") == "估值" else "锚:MA回撤"
    mkt = f"·{market}" if market and market != "US" else ""
    return f"**{sym}**（{src}{mkt}） {cur_str} · {band} · {anchor}"


def _is_a_share(symbol: str, market: str | None) -> bool:
    """A 股识别：market==CN 或 .SZ/.SS/.SH 后缀。"""
    if (market or "").upper() == "CN":
        return True
    s = (symbol or "").upper()
    return s.endswith(".SZ") or s.endswith(".SS") or s.endswith(".SH")


def compute_buy_avoid(conn=None, *, today: date | None = None,
                      include_a_share: bool = False) -> dict[str, Any]:
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
        # A 股默认不放（memory: feedback_brief_no_a_share — 新 surface 默认隐藏 A 股）
        if not include_a_share:
            uni = {s: m for s, m in uni.items() if not _is_a_share(s, m.get("market"))}
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
                "line": _compact_line(sym, market, sources, zone),
            }
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
