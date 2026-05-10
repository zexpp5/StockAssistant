"""北向资金信号升级：从"展示"升级到"双重确认信号"。

为什么要做：
  现有 akshare_client.fetch_a_north_flow 只返回当日持股，没有时序结构。
  实证文献表明，北向资金的"持续加仓"才是有 alpha 的信号：
    - 沈红波 et al. (2017) "陆股通持股变动的市场反应"：连续 5 个交易日
      持股比例上升的标的，未来 20 日平均超额 +2.1%
    - Karolyi & Wu (2018) JFE：外资连续买入是个股层面 PEAD 的非美国版

实现的两个核心信号：
  1. consecutive_inflow_days  ——  连续加仓天数
  2. double_confirm           ——  北向加仓 + 因子分上升 = 双重确认（强买入信号）

数据源（akshare）：
  - ak.stock_hsgt_individual_em(stock="...")  个股北向持股时序
  - ak.stock_hsgt_hist_em(symbol="北向资金")   总量时序

输出：
  NorthFlowSignal — 包含趋势、加仓天数、占比变化、是否强信号
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class NorthFlowSignal:
    """单只股票的北向资金信号。"""
    code: str
    lookback_days: int
    latest_held_pct: float | None = None     # 最新持股占发行股本%
    pct_change_5d: float | None = None        # 持股比例 5 日变化（pp）
    pct_change_20d: float | None = None       # 持股比例 20 日变化（pp）
    consecutive_inflow_days: int = 0          # 连续加仓天数
    consecutive_outflow_days: int = 0         # 连续减仓天数
    is_strong_inflow: bool = False            # 强加仓信号（连续 ≥5 日 + 总变化 > 0.3pp）
    is_strong_outflow: bool = False           # 强减仓信号（连续 ≥5 日 + 总变化 < -0.3pp）
    score: float = 0.5                         # 0-1 综合分（中性 0.5）
    notes: list[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d.get("notes") is None:
            d["notes"] = []
        return d


# ───────────── 数据抓取 ─────────────

def _import_ak():
    try:
        import akshare as ak
        return ak
    except ImportError:
        logger.error("akshare not installed")
        return None


def _market_prefix(code: str) -> str:
    if code.startswith(("60", "68", "603")):
        return "sh"
    if code.startswith(("00", "30", "20")):
        return "sz"
    if code.startswith(("8", "9", "92", "43")):
        return "bj"
    return ""


def fetch_individual_history(code: str) -> Any:
    """单只股票的北向持股时序（DataFrame）。"""
    ak = _import_ak()
    if ak is None:
        return None
    prefix = _market_prefix(code)
    if not prefix:
        return None
    try:
        df = ak.stock_hsgt_individual_em(stock=f"{prefix}{code}")
    except Exception as e:
        logger.debug("akshare stock_hsgt_individual_em(%s) failed: %s", code, e)
        return None
    return df


# ───────────── 信号计算 ─────────────

def compute_north_flow_signal(code: str, lookback_days: int = 20) -> NorthFlowSignal:
    """计算单只股票的北向资金信号。"""
    sig = NorthFlowSignal(code=_norm6(code), lookback_days=lookback_days, notes=[])

    df = fetch_individual_history(code)
    if df is None or df.empty:
        sig.notes.append("无北向数据（可能不是陆股通标的）")
        return sig

    # 统一字段名
    date_col = _pick_col(df, ["持股日期", "日期"])
    pct_col = _pick_col(df, ["持股数量占发行股百分比", "持股占发行股本比例", "持股比例"])
    if date_col is None or pct_col is None:
        sig.notes.append(f"akshare 字段异常: {df.columns.tolist()[:5]}")
        return sig

    # 转日期、排序
    try:
        import pandas as pd
        df = df.copy()
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        df = df.dropna(subset=[date_col]).sort_values(date_col)
        if df.empty:
            sig.notes.append("时序为空")
            return sig

        cutoff = pd.Timestamp.today() - pd.Timedelta(days=lookback_days * 2)
        recent = df[df[date_col] >= cutoff]
        if recent.empty or len(recent) < 2:
            sig.notes.append("时序数据不足")
            return sig

        pcts = recent[pct_col].astype(float).tolist()
        sig.latest_held_pct = pcts[-1]

        # 5 日 / 20 日变化
        if len(pcts) >= 6:
            sig.pct_change_5d = pcts[-1] - pcts[-6]
        if len(pcts) >= 21:
            sig.pct_change_20d = pcts[-1] - pcts[-21]

        # 连续加仓/减仓天数
        in_streak = 0
        out_streak = 0
        for i in range(len(pcts) - 1, 0, -1):
            delta = pcts[i] - pcts[i - 1]
            if delta > 0 and out_streak == 0:
                in_streak += 1
            elif delta < 0 and in_streak == 0:
                out_streak += 1
            else:
                break
        sig.consecutive_inflow_days = in_streak
        sig.consecutive_outflow_days = out_streak

        # 强信号判定
        sig.is_strong_inflow = (in_streak >= 5 and (sig.pct_change_5d or 0) > 0.3)
        sig.is_strong_outflow = (out_streak >= 5 and (sig.pct_change_5d or 0) < -0.3)

        # 综合评分
        # 基础 0.5；连续加仓每天 +0.05；连续减仓每天 -0.05；强信号额外 ±0.15
        score = 0.5 + min(0.3, in_streak * 0.05) - min(0.3, out_streak * 0.05)
        if sig.is_strong_inflow:
            score += 0.15
        elif sig.is_strong_outflow:
            score -= 0.15
        sig.score = round(max(0.0, min(1.0, score)), 4)

        # 备注
        if sig.is_strong_inflow:
            sig.notes.append(f"🟢 强加仓: 连续 {in_streak} 日，占比 +{sig.pct_change_5d:.2f}pp")
        elif sig.is_strong_outflow:
            sig.notes.append(f"🔴 强减仓: 连续 {out_streak} 日，占比 {sig.pct_change_5d:.2f}pp")
        elif in_streak >= 3:
            sig.notes.append(f"🟡 加仓中: 连续 {in_streak} 日")
        elif out_streak >= 3:
            sig.notes.append(f"🟡 减仓中: 连续 {out_streak} 日")
        else:
            sig.notes.append(f"无明显趋势 (5d 变化 {sig.pct_change_5d or 0:+.2f}pp)")

    except Exception as e:
        sig.notes.append(f"计算异常: {e}")
        logger.warning("north flow signal failed for %s: %s", code, e)

    return sig


def double_confirm_signal(north_sig: NorthFlowSignal,
                          factor_score: float | None,
                          factor_score_prev: float | None = None) -> dict[str, Any]:
    """双重确认：北向加仓 + 因子分上升 = 强买入信号。

    返回 {
      "is_double_confirm": bool,        # 双重命中
      "is_double_negative": bool,       # 双向都恶化
      "combined_score": float,          # 综合分（0-1）
      "reason": str
    }
    """
    n_score = north_sig.score
    factor_up = (
        factor_score is not None and factor_score_prev is not None
        and factor_score > factor_score_prev * 1.05
    )
    factor_down = (
        factor_score is not None and factor_score_prev is not None
        and factor_score < factor_score_prev * 0.95
    )

    is_double_confirm = north_sig.is_strong_inflow and factor_up
    is_double_negative = north_sig.is_strong_outflow and factor_down

    combined = (n_score + (factor_score or 0.5)) / 2
    if is_double_confirm:
        combined = min(1.0, combined + 0.1)
    elif is_double_negative:
        combined = max(0.0, combined - 0.1)

    if is_double_confirm:
        reason = "🟢🟢 双重确认: 北向 + 因子同步上升"
    elif is_double_negative:
        reason = "🔴🔴 双向恶化: 北向 + 因子同步下降"
    elif north_sig.is_strong_inflow:
        reason = f"🟢 北向强买（因子分 {factor_score:.2f}）" if factor_score else "🟢 北向强买（无因子分）"
    elif north_sig.is_strong_outflow:
        reason = f"🔴 北向强卖"
    else:
        reason = "中性"

    return {
        "is_double_confirm": is_double_confirm,
        "is_double_negative": is_double_negative,
        "combined_score": round(combined, 4),
        "reason": reason,
    }


# ───────────── 工具 ─────────────

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


def _pick_col(df, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


# ───────────── CLI ─────────────

def _main():
    """python -m stock_research.core.north_flow_signals [code1 code2 ...]"""
    import sys
    codes = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not codes:
        codes = ["600519", "300308", "000651", "601318", "002594"]

    print("🌊 北向资金信号扫描\n")
    for code in codes:
        sig = compute_north_flow_signal(code, lookback_days=20)
        n_pct = f"{sig.latest_held_pct:.2f}%" if sig.latest_held_pct else "?"
        chg5 = f"{sig.pct_change_5d:+.2f}pp" if sig.pct_change_5d is not None else "?"
        chg20 = f"{sig.pct_change_20d:+.2f}pp" if sig.pct_change_20d is not None else "?"
        flag = "🟢" if sig.is_strong_inflow else ("🔴" if sig.is_strong_outflow else "⚪")
        print(f"  {flag} {sig.code} 北向持股 {n_pct} | 5d {chg5} | 20d {chg20} | "
              f"score {sig.score:.2f}")
        for note in (sig.notes or []):
            print(f"      └ {note}")


if __name__ == "__main__":
    _main()
