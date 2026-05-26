"""推荐表"追涨 vs 早期信号"标签 — morning_brief / dashboard 共享逻辑。

为什么单独抽出来：
  之前在 morning_brief._signal_quality_tag、
  build_stock_dashboard_html._signal_quality_tag_for_pick、
  build_stock_dashboard_html._qualityTagHtml (JS) 三处复制实现，
  违反"后端单一来源"红线。Python 端两份现在合并；
  JS 端只做样式渲染，逻辑走 Python 算好的 quality_tag 字段。

判断规则：
  - ⚠️ 追涨进表: 综合分高 + (60d ≥ +15% 或 近30d 单日 ≥ 涨停阈值)
  - ✅ 早期信号: 综合分高 + 60d 在 ±5% 平台 + 近30d 无单日涨停
  - 中性: 返回 None

涨停阈值按市场：A 股 9.5%、港股 8%、美股 7%（港美股无涨停限制，
是"日内波动偏热"门槛而非真实涨停）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Market-aware "涨停"/"单日异常"阈值
SPIKE_THRESHOLD_BY_MARKET: dict[str, float] = {
    "A":  9.5,   # A 股涨停 9.8%（除 ST/创业板/科创板）
    "HK": 8.0,   # 港股无涨停，8% 属偏热
    "US": 7.0,   # 美股无涨停，7% 属偏热
}

CHASE_60D_THRESHOLD = 15.0   # 60d 涨幅触发追涨
EARLY_60D_LOW = -5.0
EARLY_60D_HIGH = 5.0

# 综合分门槛 — 不同打分体系不同阈值
SCORE_THRESHOLDS: dict[str, float] = {
    "normalized": 0.80,   # morning_brief A 股/港股 V2 composite 0-1
    "score_0_100": 80.0,  # dashboard total_score 0-100
    "z_score":     -1e9,  # 美股 plan_v5 z-score；不卡分数（picks 进表已过滤）
}


@dataclass(frozen=True)
class QualityTag:
    kind: Literal["chase", "early"]
    label: str
    detail: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label, "detail": self.detail}

    def as_markdown_line(self, indent: str = "  ") -> str:
        suffix = "，不是早期信号" if self.kind == "chase" else " + 高分"
        return f"{indent}{self.label}（{self.detail}{suffix}）"


def detect_market(ticker: str) -> str:
    """从 ticker 后缀识别市场（A/HK/US）。未知后缀默认 US。"""
    t = (ticker or "").upper()
    if t.endswith(".SS") or t.endswith(".SZ") or t.endswith(".BJ"):
        return "A"
    if t.endswith(".HK"):
        return "HK"
    return "US"


def compute_pct60_and_max_daily(
    closes: list,
    window: int = 60,
    spike_window: int = 30,
    end_idx: int | None = None,
) -> tuple[float | None, float | None]:
    """从 close 价格列表算 60d 涨幅 + 近 30d 单日最大涨幅。

    end_idx: 指定终点 index（exclusive，即 closes[:end_idx]）。None=末尾。
    用于 PIT 回溯：算"某天往前 60d"的窗口而不是"今天往前"。
    """
    series = closes if end_idx is None else closes[:end_idx]
    recent: list[float] = []
    for c in series[-window:]:
        try:
            if c is not None:
                recent.append(float(c))
        except (TypeError, ValueError):
            continue
    if len(recent) < 2:
        return None, None
    pct60 = ((recent[-1] - recent[0]) / recent[0] * 100.0) if recent[0] else None
    rets = []
    start = max(1, len(recent) - spike_window)
    for i in range(start, len(recent)):
        prev = recent[i - 1]
        if prev:
            rets.append((recent[i] - prev) / prev * 100.0)
    max_daily = max(rets) if rets else None
    return pct60, max_daily


def _resolve_end_idx(ts_list: list, as_of_date: str | None) -> int | None:
    """找 ts_list 中 ≤ as_of_date 的最后一个 index +1（exclusive 终点）。

    None / 找不到匹配 → None（走默认末尾窗口）。
    ts_list 必须按日期升序。as_of_date 形如 "2026-05-26"。
    """
    if not as_of_date or not ts_list:
        return None
    target = str(as_of_date)[:10]
    last = -1
    for i, t in enumerate(ts_list):
        if str(t)[:10] <= target:
            last = i
        else:
            break
    return (last + 1) if last >= 0 else None


def classify(
    ticker: str,
    score: float | None,
    pct60: float | None,
    max_daily: float | None,
    mode: str = "normalized",
    score_threshold: float | None = None,
) -> QualityTag | None:
    """核心判断函数。

    Args:
        ticker: 用于识别市场（A 股阈值 9.5%、港股 8%、美股 7%）
        score: 综合分；mode 决定阈值含义
        pct60: 60d 涨跌幅%
        max_daily: 近 30d 单日最大涨幅%
        mode: "normalized" | "score_0_100" | "z_score"
        score_threshold: 覆盖默认阈值
    """
    if score_threshold is None:
        score_threshold = SCORE_THRESHOLDS.get(mode, SCORE_THRESHOLDS["normalized"])
    if score is None or score < score_threshold:
        return None

    market = detect_market(ticker)
    spike_threshold = SPIKE_THRESHOLD_BY_MARKET.get(market, 9.5)

    chase_60 = pct60 is not None and pct60 >= CHASE_60D_THRESHOLD
    chase_spike = max_daily is not None and max_daily >= spike_threshold
    if chase_60 or chase_spike:
        why = []
        if chase_60:
            why.append(f"60d 已涨 {pct60:+.1f}%")
        if chase_spike:
            why.append(f"近30d 单日 {max_daily:+.1f}%")
        return QualityTag(kind="chase", label="⚠️ 追涨进表", detail=" · ".join(why))

    if (pct60 is not None
            and EARLY_60D_LOW <= pct60 <= EARLY_60D_HIGH
            and (max_daily is None or max_daily < spike_threshold)):
        return QualityTag(kind="early", label="✅ 早期信号", detail="60d 在 ±5% 平台")

    return None


def classify_from_history(
    ticker: str,
    score: float | None,
    history_tickers: dict,
    mode: str = "normalized",
    score_threshold: float | None = None,
    as_of_date: str | None = None,
) -> QualityTag | None:
    """便利函数：直接从 history_data tickers dict 取 closes 并分类。

    history_tickers 形如 {"000725.SZ": {"close": [...], "ts": [...], ...}, ...}
    as_of_date: 形如 "2026-05-26"。给了就用"那天往前 60d"的价格窗口（PIT 回溯，
    例如算 30 天前推荐时该票是不是追涨）。不给就用最新窗口。
    需要 ticker info 里有 "ts" 字段（按日期升序）配合，否则忽略 as_of_date。
    """
    info = (history_tickers or {}).get(ticker) if history_tickers else None
    if not info:
        return None
    closes = info.get("close") or []
    end_idx = _resolve_end_idx(info.get("ts") or [], as_of_date) if as_of_date else None
    pct60, max_daily = compute_pct60_and_max_daily(closes, end_idx=end_idx)
    return classify(ticker, score, pct60, max_daily, mode, score_threshold)
