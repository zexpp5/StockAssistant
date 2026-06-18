"""月度真钱动作清单 —— 关键判定的单一来源（可单测）。

背景（2026-06-18）：原逻辑全写在 build_stock_dashboard_html.py 的
renderMonthlyActions() 这段 ~435 行 JS 里，测试只能查"文案在不在"，无法验证
≤3 上限 / 单赛道 15% / 同赛道去重 / 超配纠偏 这些判定是否真生效。本模块把这些
判定原样下沉为纯函数，前端只渲染本模块的输出（避免前后端双引擎，见
feedback_single_source_no_double_engine）。

安全边界：纯计算，不读 DB、不写任何状态。所有金额按比例返回，前端乘 total_capital
渲染。研究/advisory，不构成买卖建议，不自动交易。
"""
from __future__ import annotations

import re
from typing import Any

# ── 赛道分类（与原 JS themeOverride / normalizeTheme 同源）─────────────
THEME_OVERRIDE: dict[str, str] = {
    "GEV": "电力/核能基础设施", "VST": "电力/核能基础设施", "BWXT": "电力/核能基础设施",
    "CEG": "电力/核能基础设施", "OKLO": "电力/核能基础设施", "SMR": "电力/核能基础设施",
    "NNE": "电力/核能基础设施",
    "NVDA": "半导体/AI硬件", "AVGO": "半导体/AI硬件", "AMD": "半导体/AI硬件",
    "TSM": "半导体/AI硬件", "QCOM": "半导体/AI硬件", "NXPI": "半导体/AI硬件",
    "ADI": "半导体/AI硬件", "MU": "半导体/AI硬件", "MRVL": "半导体/AI硬件",
    "AMKR": "半导体/AI硬件",
    "VECO": "半导体设备", "ACMR": "半导体设备", "ICHR": "半导体设备",
    "GOOGL": "云与AI平台", "MSFT": "云与AI平台", "META": "云与AI平台", "AMZN": "云与AI平台",
    "ORCL": "云与AI平台",
    "HPE": "服务器/数据中心", "GDS": "服务器/数据中心",
    "AAPL": "端侧AI/消费电子",
    "9992.HK": "消费/潮玩",
    "IAUM": "黄金/避险",
}

_THEME_PATTERNS = [
    (re.compile(r"nuclear|uranium|utility|utilities|power|electric|energy|grid|reactor|vernova|vistra|bwxt|核|电力|电网|能源|发电|公用事业", re.I), "电力/核能基础设施"),
    (re.compile(r"semi|semiconductor|chip|gpu|asic|networking|foundry|memory|ai compute|半导体|芯片|存储|算力", re.I), "半导体/AI硬件"),
    (re.compile(r"equipment|tool|etch|deposition|process|wafer|封测|设备|专用设备", re.I), "半导体设备"),
    (re.compile(r"cloud|platform|software|llm|search|advertising|ai platform|云|平台|软件|搜索", re.I), "云与AI平台"),
    (re.compile(r"server|datacenter|data center|infrastructure|服务器|数据中心", re.I), "服务器/数据中心"),
    (re.compile(r"consumer|discretionary|brand|toy|pop mart|消费|潮玩|品牌", re.I), "消费/品牌"),
]


def classify_theme(ticker: str, raw_parts: str) -> str:
    """赛道归类：硬编码覆盖优先，否则按关键词正则，最后兜底取首段。"""
    tk = str(ticker or "").upper()
    if tk in THEME_OVERRIDE:
        return THEME_OVERRIDE[tk]
    parts = str(raw_parts or "")
    for pat, label in _THEME_PATTERNS:
        if pat.search(parts):
            return label
    head = re.split(r"[|/·,，;；]", parts)[0].strip()[:18]
    return head or "未分类"


# ── 集中度纠偏（与原 JS correctionPlanFor 同源；金额按比例，前端乘资金）──
def correction_plan_for(weight: float) -> dict[str, Any] | None:
    """单只持仓占比 → 纠偏建议。>50%严重/>40%高度/>25%超线，否则 None。"""
    w = float(weight or 0)
    if w > 0.50:
        return {"label": "严重集中", "target": 0.45, "reduce_pct": w - 0.45,
                "tier": "severe", "stop_add": True}
    if w > 0.40:
        return {"label": "高度集中", "target": 0.40, "reduce_pct": w - 0.40,
                "tier": "high", "stop_add": True}
    if w > 0.25:
        return {"label": "超过 25%", "target": 0.25, "reduce_pct": w - 0.25,
                "tier": "over25", "stop_add": True}
    return None


# ── 闸门：策略状态 → 月度/单只上限 ─────────────────────────────────
def resolve_caps(readiness: dict[str, Any]) -> dict[str, Any]:
    decision = (readiness or {}).get("decision") or {}
    code = str(decision.get("code") or "").upper()
    us = (readiness or {}).get("us") or {}
    trial_ready = us.get("trial_ready") is True or "TRIAL_READY" in code
    blocked = (readiness or {}).get("status") == "FAIL" or "BLOCKED" in code
    per_name = 0.0 if blocked else (0.02 if trial_ready else 0.01)
    total_month = 0.0 if blocked else (0.06 if trial_ready else 0.03)
    return {"blocked": blocked, "trial_ready": trial_ready,
            "per_name": per_name, "total_month": total_month}


THEME_LIMIT = 0.15
MAX_BUY_ROWS = 3
OVERHEAT_1Y_PCT = 200.0
SINGLE_NAME_CAP = 0.25
_RISK_RE = re.compile(r"OVERHEATED|过热|ACUTE|急跌|接飞刀")


def _risk_text(flag: Any) -> str:
    if isinstance(flag, str):
        return flag
    if isinstance(flag, dict):
        return str(flag.get("message") or flag.get("code") or "")
    return ""


def build_monthly_plan(
    *,
    plan_rows: list[dict[str, Any]],
    readiness: dict[str, Any],
    real_weights: dict[str, float],
    buy_zones: dict[str, dict[str, Any]],
    candidates: dict[str, dict[str, Any]] | None = None,
    review_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """月度动作判定单一来源。返回 caps/buy_rows/skip_rows/corrections/theme_exposure。

    金额一律按比例（*_pct / cap_pct），前端乘 total_capital 渲染。
    """
    candidates = {str(k).upper(): v for k, v in (candidates or {}).items()}
    real_weights = {str(k).upper(): float(v) for k, v in (real_weights or {}).items()}
    buy_zones = {str(k).upper(): v for k, v in (buy_zones or {}).items()}
    review_items = review_items or []

    caps = resolve_caps(readiness)
    blocked = caps["blocked"]
    trial_ready = caps["trial_ready"]
    per_name_cap = caps["per_name"]
    total_month_cap = caps["total_month"]

    def theme_of(tk: str) -> str:
        cand = candidates.get(tk, {})
        raw = " ".join(str(x) for x in [
            cand.get("theme"), cand.get("industry"), cand.get("sector"),
            cand.get("chain"), cand.get("chain_role"), cand.get("name"),
            (cand.get("detail") or {}).get("theme") if isinstance(cand.get("detail"), dict) else None,
        ] if x)
        return classify_theme(tk, raw)

    # 当前赛道暴露（按真实持仓权重聚合）
    theme_current_weight: dict[str, float] = {}
    for it in review_items:
        tk = str(it.get("symbol") or it.get("code") or "").upper()
        w = float(it.get("current_weight") or 0)
        if not tk or w <= 0:
            continue
        th = theme_of(tk)
        theme_current_weight[th] = theme_current_weight.get(th, 0.0) + w

    # 按目标仓位降序（与 JS sorted 同口径）
    def _target_w(r: dict[str, Any]) -> float:
        for k in ("capped_weight", "target_weight", "weight"):
            if r.get(k) is not None:
                try:
                    return float(r[k])
                except Exception:
                    pass
        return 0.0

    sorted_rows = sorted(
        plan_rows or [],
        key=lambda r: (-_target_w(r), -float(r.get("composite_z") or 0)),
    )

    buy_rows: list[dict[str, Any]] = []
    skip_rows: list[dict[str, Any]] = []
    theme_reserved: dict[str, float] = {}
    theme_picked: dict[str, list[str]] = {}
    remaining_pct = total_month_cap

    for r in sorted_rows:
        tk = str(r.get("ticker") or r.get("code") or "").upper()
        if not tk:
            continue
        cand = candidates.get(tk, {})
        theme = theme_of(tk)
        z = buy_zones.get(tk)
        cur_w = float(real_weights.get(tk, 0) or 0)
        target_w = _target_w(r)
        gap_w = max(0.0, target_w - cur_w)
        detail = cand.get("detail") or {}
        one_year = detail.get("one_year_pct", cand.get("one_year_pct"))
        try:
            one_year = float(one_year)
        except (TypeError, ValueError):
            one_year = None
        action = str(cand.get("action") or "").lower()
        eligibility = str(cand.get("eligibility") or "").lower()
        position = str((z or {}).get("position") or "")

        reasons: list[str] = []
        if blocked:
            reasons.append("策略状态阻断：本月不新买")
        if not z:
            reasons.append("缺少自动买入区间")
        elif position == "偏贵":
            reasons.append("高于买入区，不追高")
        if one_year is not None and one_year > OVERHEAT_1Y_PCT:
            reasons.append(f"近一年涨幅 {one_year:.0f}%，过热")
        if cur_w > SINGLE_NAME_CAP:
            reasons.append(f"真实持仓已 {cur_w*100:.1f}%，超过 25% 上限")
        if gap_w <= 0 and cur_w > 0:
            reasons.append("当前持仓已达到/超过模型目标仓位")
        if action == "blocked":
            reasons.append("推荐动作被红旗拦截")
        if action == "exclude" or eligibility == "excluded":
            reasons.append("已被规则剔除")
        if action == "watch_only" or eligibility == "watch_only":
            reasons.append("只观察，不进入本月买入")
        if theme_current_weight.get(theme, 0) >= THEME_LIMIT:
            reasons.append(f"{theme} 当前暴露 {theme_current_weight.get(theme,0)*100:.1f}%，超过 15% 赛道上限")
        if theme_picked.get(theme):
            reasons.append(f"本月已选择 {theme}，同赛道不重复下注")
        for f in (cand.get("risk_flags") or []):
            txt = _risk_text(f)
            if txt and _RISK_RE.search(txt):
                reasons.append(txt)

        zone_ok = position in ("便宜", "区间内")
        can_buy = (not reasons) and zone_ok and gap_w > 0 and remaining_pct > 0 and len(buy_rows) < MAX_BUY_ROWS
        if can_buy:
            zone_scale = 1.0 if position == "便宜" else 0.5
            theme_room = max(0.0, THEME_LIMIT - theme_current_weight.get(theme, 0) - theme_reserved.get(theme, 0))
            cap_pct = min(gap_w, per_name_cap * zone_scale, remaining_pct, theme_room)
            if cap_pct > 0.0001:
                remaining_pct -= cap_pct
                theme_reserved[theme] = theme_reserved.get(theme, 0) + cap_pct
                theme_picked.setdefault(theme, []).append(tk)
                buy_rows.append({
                    "ticker": tk, "theme": theme, "position": position,
                    "low": (z or {}).get("low"), "high": (z or {}).get("high"),
                    "current": (z or {}).get("current"),
                    "target_w": target_w, "cur_w": cur_w, "cap_pct": cap_pct,
                    "zone_scale": zone_scale,
                    "name": cand.get("name") or tk,
                    "theme_room_used_up": theme_room <= cap_pct + 0.0001,
                    "basis": ("小仓试探门槛已过，仍按月度上限分批" if trial_ready
                              else "策略小仓试探未达标，单只按 1% 内从严"),
                })
                continue
            reasons.append(f"{theme} 赛道剩余额度不足，本月不再加")

        if reasons or (z and not zone_ok):
            skip_rows.append({
                "ticker": tk, "theme": theme, "position": position,
                "low": (z or {}).get("low"), "high": (z or {}).get("high"),
                "current": (z or {}).get("current"),
                "name": cand.get("name") or tk,
                "has_zone": bool(z),
                "reasons": list(dict.fromkeys(reasons)) or ["买点未到"],
            })

    # 集中度纠偏（真实持仓）
    corrections: list[dict[str, Any]] = []
    for it in review_items:
        tk = str(it.get("symbol") or it.get("code") or "").upper()
        if not tk:
            continue
        w = float(it.get("current_weight") or 0)
        flags = [str(f) for f in (it.get("risk_flags") or [])]
        triggers: list[str] = []
        if w > 0.50:
            triggers.append(f"单只 {w*100:.1f}%，严重集中")
        elif w > 0.40:
            triggers.append(f"单只 {w*100:.1f}%，高度集中")
        elif w > 0.25:
            triggers.append(f"单只 {w*100:.1f}%，超过 25%")
        if any(("止损" in f) or ("跌破" in f) for f in flags):
            triggers.append("触发纪律线风险")
        if any(("过热" in f) or ("涨幅" in f) for f in flags):
            triggers.append("过热/涨幅风险")
        if not triggers:
            continue
        plan = correction_plan_for(w)
        corrections.append({
            "ticker": tk, "name": it.get("name") or "", "weight": w,
            "theme": theme_of(tk), "triggers": triggers,
            "plan": plan,
        })

    theme_exposure = {
        th: {
            "current": theme_current_weight.get(th, 0.0),
            "reserved": theme_reserved.get(th, 0.0),
            "picked": theme_picked.get(th, []),
        }
        for th in set(theme_current_weight) | set(theme_reserved)
    }

    return {
        "caps": caps,
        "buy_rows": buy_rows,
        "skip_rows": skip_rows,
        "corrections": corrections,
        "theme_exposure": theme_exposure,
        "new_buy_pct": sum(float(x["cap_pct"]) for x in buy_rows),
        "theme_limit": THEME_LIMIT,
        "max_buy_rows": MAX_BUY_ROWS,
    }
