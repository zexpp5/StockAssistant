"""预期消耗度（expectation meter）— 回答「这只票的故事讲到第几章了」。

单一来源纯函数：输入现价/目标价/估值/一年涨幅/行业文本，输出三档灯 + 人话理由。
不改打分、不出买卖指令；供严选卡 / 买前研究展示。

原理（预期投资学）：股价涨幅 = 盈利增长 × 估值扩张，估值扩张部分就是已消耗的预期。
V1 用三个可即时计算的代理 + 一个周期股独立警示：
  ① 目标价消耗度   = 现价 / 最近分析师目标价（越接近 1 预期越满）
  ② 隐含增速压力   = PEG（>2.5 = 估值要求的增长远超预期增速）
  ③ 涨幅透支       = 近一年涨幅（>200% 说明大量预期已入价）
  ④ 周期顶警示(独立旗) = 周期行业 + 一年涨幅≥200% + 低 forward PE
     —— 周期股在盈利顶部 PE 最低，「低 PE ≠ 便宜」，命中直接置红灯。

已知边界（诚实声明，展示层必须带 caveat）：
  - 目标价取最近一条分析师目标（与 buy_zone 同源），分析师在周期顶会集体追涨目标价；
  - V1 未接 EPS 历史（估值扩张占比精确分解）与毛利率历史分位，列 P1；
  - 这是研究参考，不是精确科学，更不是买卖信号。
"""
from __future__ import annotations

import re
from typing import Any

LIGHT_LOW = "🟢"      # 预期不满
LIGHT_MID = "🟡"      # 预期打了一半
LIGHT_HIGH = "🔴"     # 预期透支
LIGHT_UNKNOWN = "⚪"  # 数据不足

LABELS = {
    LIGHT_LOW: "预期不满",
    LIGHT_MID: "预期打了一半",
    LIGHT_HIGH: "预期透支",
    LIGHT_UNKNOWN: "数据不足",
}

# 🔴 票的持有纪律（2026-07-06 MU 反验后定稿：🔴 不做剔除闸只做警示，
# 因为周期主升段远涨过"合理预期"——$285 就会踢掉后面还有 3 倍的美光。
# 正确姿势是换纪律不换名单：趋势仓规矩拿，靠止损闸离场，不靠入场禁令。）
TREND_DISCIPLINE_ADVICE = (
    "预期已透支 → 只能按趋势仓玩法：仓位减半 + 跟踪止损，"
    "跌破位就走，别因“它是好公司”扛单。"
)

# 周期性行业关键词（V1 启发式；毛利率历史分位是 P1 的精确化方向）
_CYCLICAL_PATTERN = re.compile(
    r"内存|存储|闪存|晶圆|面板|航运|海运|钢铁|煤炭|有色|化工|锂|稀土|铀|油气|光伏|养殖|"
    r"memory|nand|dram|hbm|storage|flash|semiconductor memory|shipping|steel|"
    r"chemical|lithium|uranium|solar|panel",
    re.IGNORECASE,
)

# 各代理的档位切点（预注册，改动须走文档评审，不许看着结果调）
TARGET_CONSUMPTION_HIGH = 0.90   # 现价已吃掉目标价 90%+
TARGET_CONSUMPTION_MID = 0.70
PEG_HIGH = 2.5
PEG_MID = 1.5
RUNUP_HIGH_PCT = 200.0
RUNUP_MID_PCT = 100.0
CYCLICAL_RUNUP_PCT = 200.0
CYCLICAL_LOW_FPE = 15.0
RATIO_HIGH = 0.66                # 总分占比 → 红灯
RATIO_MID = 0.33                 # → 黄灯


def _as_float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN → None


def is_cyclical_industry(*texts: Any) -> bool:
    hay = " ".join(str(t) for t in texts if t)
    return bool(_CYCLICAL_PATTERN.search(hay))


def expectation_meter(
    *,
    price: Any = None,
    target_price: Any = None,
    peg_ratio: Any = None,
    forward_pe: Any = None,
    one_year_pct: Any = None,
    industry_text: Any = None,
) -> dict[str, Any]:
    """算预期消耗度。缺哪项就跳过哪项（不硬凑），全缺返回 ⚪。"""
    price_f = _as_float(price)
    target_f = _as_float(target_price)
    peg_f = _as_float(peg_ratio)
    fpe_f = _as_float(forward_pe)
    runup_f = _as_float(one_year_pct)

    components: dict[str, Any] = {}
    score = 0
    max_score = 0
    reasons: list[str] = []

    # ① 目标价消耗度
    if price_f and target_f and target_f > 0:
        consumption = price_f / target_f
        tier = 2 if consumption >= TARGET_CONSUMPTION_HIGH else (
            1 if consumption >= TARGET_CONSUMPTION_MID else 0)
        remaining_pct = (1 - consumption) * 100
        components["target_consumption"] = {
            "value": round(consumption, 4), "pct": round(consumption * 100, 1),
            "remaining_pct": round(remaining_pct, 1), "tier": tier,
        }
        score += tier
        max_score += 2
        if consumption >= 1:
            reasons.append(f"现价已超过分析师目标价 {(consumption-1)*100:.0f}%，再涨要靠分析师追加目标")
        elif tier == 2:
            reasons.append(f"离分析师目标价只剩 {remaining_pct:.0f}% 空间，上行所剩无几")
        elif tier == 1:
            reasons.append(f"离分析师目标价还剩 {remaining_pct:.0f}% 空间")

    # ② 隐含增速压力（PEG）
    if peg_f is not None and peg_f > 0:
        tier = 2 if peg_f > PEG_HIGH else (1 if peg_f > PEG_MID else 0)
        components["peg_pressure"] = {"value": round(peg_f, 2), "tier": tier}
        score += tier
        max_score += 2
        if tier == 2:
            reasons.append(f"PEG {peg_f:.1f}，估值要求的增长远超分析师预期增速")

    # ③ 涨幅透支
    if runup_f is not None:
        tier = 2 if runup_f >= RUNUP_HIGH_PCT else (1 if runup_f >= RUNUP_MID_PCT else 0)
        components["runup"] = {"one_year_pct": round(runup_f, 1), "tier": tier}
        score += tier
        max_score += 2
        if tier == 2:
            reasons.append(f"近一年已涨 {runup_f:+.0f}%，大量预期已入价")
        elif tier == 1:
            reasons.append(f"近一年涨 {runup_f:+.0f}%")

    # ④ 周期顶警示（独立旗，命中直接红灯）
    cyclical_top = bool(
        is_cyclical_industry(industry_text)
        and runup_f is not None and runup_f >= CYCLICAL_RUNUP_PCT
        and fpe_f is not None and 0 < fpe_f < CYCLICAL_LOW_FPE
    )
    if cyclical_top:
        reasons.append(
            f"⚠️ 周期股顶部特征：一年 {runup_f:+.0f}% + forward PE 仅 {fpe_f:.1f} —— "
            "周期股在盈利顶部 PE 最低，低 PE ≠ 便宜"
        )

    if max_score == 0:
        light = LIGHT_UNKNOWN
        ratio = None
    else:
        ratio = score / max_score
        light = LIGHT_HIGH if ratio >= RATIO_HIGH else (
            LIGHT_MID if ratio >= RATIO_MID else LIGHT_LOW)
        if cyclical_top:
            light = LIGHT_HIGH

    return {
        "light": light,
        "label": LABELS[light],
        "score": score,
        "max_score": max_score,
        "ratio": round(ratio, 4) if ratio is not None else None,
        "cyclical_top_risk": cyclical_top,
        "components": components,
        "reasons": reasons,
        "discipline": TREND_DISCIPLINE_ADVICE if light == LIGHT_HIGH else None,
        "caveats": [
            "目标价为最近一条分析师目标，周期顶分析师常集体追涨目标价",
            "研究参考，非买卖信号；V1 未含 EPS 历史分解与毛利率分位（P1）",
        ],
    }


def format_meter_line(meter: dict[str, Any] | None) -> str:
    """严选卡/早报用的一行摘要。"""
    if not meter or meter.get("light") == LIGHT_UNKNOWN:
        return "⚪ 预期消耗：数据不足"
    parts = [f'{meter["light"]} 预期消耗：{meter["label"]}']
    tc = (meter.get("components") or {}).get("target_consumption")
    if tc:
        rem = tc.get("remaining_pct")
        if isinstance(rem, (int, float)) and rem < 0:
            parts.append(f'已超分析师目标 {-rem:.0f}%')
        elif isinstance(rem, (int, float)):
            parts.append(f'离分析师目标剩 {rem:.0f}% 空间')
    ru = (meter.get("components") or {}).get("runup")
    if ru:
        parts.append(f'一年 {ru["one_year_pct"]:+.0f}%')
    return " · ".join(parts)
