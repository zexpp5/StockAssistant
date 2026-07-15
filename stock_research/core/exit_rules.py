"""卖出三条线（退出规则）引擎 — 买卖闭环的卖出侧。

方案: docs/V2/2026-07-15_买卖闭环_可实盘化方案.md §2（参数预注册，不许事后挪）。

设计原则：新手可执行 > 理论最优。每只票只给 3 条线：
  1. 止损线 — 跌破就走（跌破 = 收盘价跌破，防盘中毛刺）
  2. 目标线 — 到了考虑落袋（= buy_zone 上沿；系统自己都说"偏贵别追"的价位）
  3. 时间线 — N 个交易日后复评（对照当日严选：还在→可续持；掉出→建议换仓）

文案全部 advisory（建议复查/考虑落袋），禁止"必须买/必须卖"。纯函数，无 IO。
"""
from __future__ import annotations

import math
from typing import Any

# ── 预注册参数（2026-07-15，依据 = 统一回测引擎持有期扫描 + 前向实测互证）────
# 美股: 5d 净alpha +10.13% 最优后快速衰减 → 复评 5 交易日；止损 -8% 与现有红警口径统一
# 港股: 20d 净alpha +7.08% 越拿越好 → 复评 20 交易日；波动大止损放宽 -10%
# A股:  5d 净alpha +5.70% 后归零 → 复评 5 交易日；反转周期短止损收紧 -6%
EXIT_PARAMS: dict[str, dict[str, float | int]] = {
    "US": {"stop_pct": -8.0, "review_days": 5, "fallback_target_pct": 15.0},
    "HK": {"stop_pct": -10.0, "review_days": 20, "fallback_target_pct": 15.0},
    "CN": {"stop_pct": -6.0, "review_days": 5, "fallback_target_pct": 15.0},
}

MARKET_ALIASES = {"A": "CN", "A股": "CN", "美股": "US", "港股": "HK"}


def _norm_market(market: str) -> str:
    m = str(market or "").strip().upper()
    return MARKET_ALIASES.get(m, m)


def _round_price(value: float, market: str) -> float:
    """港/A 股保留 2 位；美股价格>1000 保留整数，否则 2 位。"""
    if market == "US" and value >= 1000:
        return round(value)
    return round(value, 2)


def build_exit_plan(
    market: str,
    entry_price: float | None,
    buy_zone: dict[str, Any] | None = None,
    *,
    currency: str | None = None,
) -> dict[str, Any] | None:
    """给定市场+入场价（+可买区间），产出三条线。entry_price 无效时返回 None。

    返回:
      {market, entry_price, stop_price, stop_pct, target_price, target_source,
       review_days, lines: [止损/目标/复评 三句 advisory 文案]}
    """
    mkt = _norm_market(market)
    params = EXIT_PARAMS.get(mkt)
    if params is None:
        return None
    try:
        entry = float(entry_price)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(entry) or entry <= 0:
        return None

    stop_pct = float(params["stop_pct"])
    stop_price = _round_price(entry * (1.0 + stop_pct / 100.0), mkt)

    # 目标线 = buy_zone 上沿（系统"偏贵"分界）；无区间/区间无效则入场价 +15%
    target_price = None
    target_source = None
    zone_high = None
    if isinstance(buy_zone, dict):
        try:
            zh = float(buy_zone.get("high"))
            if math.isfinite(zh) and zh > entry:
                zone_high = zh
        except (TypeError, ValueError):
            pass
    if zone_high is not None:
        target_price = _round_price(zone_high, mkt)
        target_source = "buy_zone_high"
    else:
        target_price = _round_price(
            entry * (1.0 + float(params["fallback_target_pct"]) / 100.0), mkt)
        target_source = "entry_plus_15pct"

    review_days = int(params["review_days"])
    cur = (currency or {"US": "$", "HK": "HK$", "CN": "¥"}.get(mkt, "")) or ""

    lines = [
        f"🛑 止损线 {cur}{stop_price}（入场 {stop_pct:+.0f}%）：收盘跌破建议离场，别和它讲道理",
        f"🎯 目标线 {cur}{target_price}"
        + ("（可买区间上沿=系统认定的偏贵价）" if target_source == "buy_zone_high" else "（入场 +15%）")
        + "：到了考虑部分落袋",
        f"⏰ 时间线 {review_days} 个交易日：到期对照当日严选——还在名单可续持，掉出建议换仓",
    ]
    return {
        "market": mkt,
        "entry_price": _round_price(entry, mkt),
        "stop_price": stop_price,
        "stop_pct": stop_pct,
        "target_price": target_price,
        "target_source": target_source,
        "review_days": review_days,
        "lines": lines,
        "caliber": "preregistered_2026-07-15",
    }


def exit_plan_compact(plan: dict[str, Any] | None) -> str:
    """一行紧凑版（早报/卡片用）: 🛑止损X · 🎯目标Y · ⏰N日复评"""
    if not plan:
        return ""
    return (f"🛑止损{plan['stop_price']} · 🎯目标{plan['target_price']}"
            f" · ⏰{plan['review_days']}日复评")
