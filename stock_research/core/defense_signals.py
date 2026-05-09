"""实盘防御信号 — 把 stress test 验证过的 C 终极版集成到实盘检查。

学术依据（与 stress_test 一致）：
  - Faber (2007)   : SPY < 200MA → 减仓 50%
  - Whaley (2009)  : VIX > 30 → 市场恐慌，减仓 50%
  - O'Neil (2002)  : 个股从入场价跌 -15% → 强制止损

**与 stress_test.py 的区别：**
  stress_test 是模拟回测（"如果当时持有 12 只股，现在会怎样"）；
  defense_signals 是**实盘检查**（"今天 watchlist + picks 实际持仓里，哪些触发了止损"）。

输入：
  - picks_today: 今日 picks 表（含"入选时价格"和"累计涨跌%"）
  - market_signals: VIX + SPY/200MA 当前值

输出：
  - alerts: 警报列表（market_panic / trend_break / stop_loss）
  - 每条 alert: {type, severity, ticker, drop_pct, suggested_action}

边界：
  - 只输出建议，不直接执行交易
  - 保留用户最终决策权
"""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ─────────── 常量（与 stress_test 一致，便于学术追溯）───────────

STOP_LOSS_THRESHOLD_PCT = -15.0   # O'Neil 2002 保守版（-7% 是激进版）
VIX_PANIC_THRESHOLD = 30.0        # Whaley 2009 标准
VIX_EXTREME_THRESHOLD = 40.0      # 极端恐慌

# ─────────── 工具 ───────────

def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─────────── 单股止损检查 ───────────

def check_stop_loss(picks_today: list[dict[str, Any]],
                    threshold_pct: float = STOP_LOSS_THRESHOLD_PCT) -> list[dict[str, Any]]:
    """对每个 pick 检查累计涨跌 ≤ threshold_pct 是否触发止损。

    输入 picks_today：每条记录的 normalized 字段含：
      - name, code, rating
      - cum_pct (累计涨跌%)
      - days_held
      - peg_at_pick / pe_at_pick / y1_at_pick (入选时指标)

    返回 alerts 列表。
    """
    alerts = []
    for p in picks_today:
        n = p.get("normalized", {})
        cum_pct = _to_float(n.get("cum_pct"))
        if cum_pct is None:
            continue
        if cum_pct <= threshold_pct:
            severity = "CRITICAL" if cum_pct <= threshold_pct * 1.3 else "HIGH"
            alerts.append({
                "type": "STOP_LOSS",
                "severity": severity,
                "ticker": n.get("code"),
                "name": n.get("name"),
                "rating": n.get("rating"),
                "current_drop_pct": round(cum_pct, 2),
                "threshold_pct": threshold_pct,
                "days_held": _to_float(n.get("days_held")) or 0,
                "suggested_action": (
                    f"⚠️ 跌幅 {cum_pct:+.1f}% 已触发 -15% 止损线，建议立刻减半仓位 / "
                    f"重新审视入选理由是否成立"
                ),
            })
    return alerts


# ─────────── 市场层检查 ───────────

def check_market_regime(as_of: str | None = None,
                        vix_panic: float = VIX_PANIC_THRESHOLD,
                        vix_extreme: float = VIX_EXTREME_THRESHOLD,
                        include_macro: bool = True,
                        include_options: bool = True) -> list[dict[str, Any]]:
    """检查市场层信号：VIX + SPY/200MA + 宏观 (FRED) + SPY put/call ratio。

    include_macro: 加宏观经济 regime（FRED Fed/CPI/失业率/收益率曲线倒挂）
    include_options: 加 SPY 期权 PCR（领先于 VIX）
    """
    alerts = []
    try:
        from .regime_filter import _spy_above_200ma, _vix_below_panic
    except ImportError:
        return alerts

    # SPY/200MA
    spy_above, ma_info = _spy_above_200ma(as_of)
    if not spy_above and "error" not in ma_info:
        alerts.append({
            "type": "TREND_BREAK",
            "severity": "HIGH",
            "trigger": "SPY < 200MA (Faber 2007)",
            "spy_close": ma_info.get("spy_close"),
            "spy_200ma": ma_info.get("spy_200ma"),
            "distance_pct": ma_info.get("distance_pct"),
            "suggested_action": (
                f"📉 SPY ({ma_info.get('spy_close')}) 跌破 200MA ({ma_info.get('spy_200ma')})，"
                f"距离 {ma_info.get('distance_pct')}%。Faber 2007 规则：建议把组合减仓到 50%。"
            ),
        })

    # VIX
    _, vix_info = _vix_below_panic(as_of, panic_threshold=vix_panic)
    vix_now = vix_info.get("vix_close")
    if vix_now is not None:
        if vix_now >= vix_extreme:
            alerts.append({
                "type": "MARKET_PANIC",
                "severity": "CRITICAL",
                "trigger": f"VIX = {vix_now} > {vix_extreme}（极端恐慌）",
                "vix_close": vix_now,
                "suggested_action": (
                    f"🚨🚨 VIX = {vix_now} 突破极端恐慌阈值（>{vix_extreme}）。"
                    f"Whaley 2009：建议组合减仓至 30% 或更低，避免极端波动。"
                ),
            })
        elif vix_now >= vix_panic:
            alerts.append({
                "type": "MARKET_PANIC",
                "severity": "HIGH",
                "trigger": f"VIX = {vix_now} > {vix_panic}（恐慌）",
                "vix_close": vix_now,
                "suggested_action": (
                    f"⚠️ VIX = {vix_now} 突破恐慌阈值（>{vix_panic}）。"
                    f"Whaley 2009：建议组合减仓 50%，可考虑买 put 对冲。"
                ),
            })

    # 宏观经济 regime（FRED + yf fallback）
    if include_macro and as_of is None:
        # 仅当前实时检查时跑（历史回测不算宏观，避免数据获取慢）
        try:
            from .macro_data import macro_regime
            macro = macro_regime()
            for a in macro.get("alerts", []):
                alerts.append({
                    "type": f"MACRO_{a['type']}",
                    "severity": a["severity"],
                    "trigger": a["msg"][:80],
                    "suggested_action": a["msg"],
                    "macro_data": {
                        "fed_rate": macro.get("fed_rate"),
                        "ten_year_yield": macro.get("ten_year_yield"),
                        "yield_curve": macro.get("yield_curve"),
                    },
                })
        except Exception as e:
            logger.warning("macro check 失败: %s", str(e)[:80])

    # SPY put/call ratio（OpenBB 期权链，领先于 VIX）
    if include_options and as_of is None:
        try:
            from .options_signals import diagnose as opt_diagnose
            opt = opt_diagnose()
            sig_vol = opt.get("signal_volume", {})
            if sig_vol.get("severity") in ("HIGH", "CRITICAL"):
                alerts.append({
                    "type": "PUT_CALL_RATIO",
                    "severity": sig_vol["severity"],
                    "trigger": sig_vol.get("label", ""),
                    "pcr_volume": opt.get("pcr_volume"),
                    "pcr_oi": opt.get("pcr_oi"),
                    "suggested_action": (
                        f"SPY PCR (volume) = {opt.get('pcr_volume')} · "
                        f"PCR (OI) = {opt.get('pcr_oi')}。"
                        f"建议：{sig_vol.get('action', '—')}"
                    ),
                })
        except Exception as e:
            logger.warning("PCR check 失败: %s", str(e)[:80])

    return alerts


# ─────────── 综合诊断 ───────────

def diagnose_all(picks_today: list[dict[str, Any]],
                 as_of: str | None = None) -> dict[str, Any]:
    """一站式诊断：检查市场层 + 个股层所有防御信号。

    返回 {
      'alerts': [...],          # 全部警报
      'severity': 'NONE'|'LOW'|'HIGH'|'CRITICAL',
      'market_alerts': [...],
      'stop_loss_alerts': [...],
      'summary': '...',
    }
    """
    market_alerts = check_market_regime(as_of=as_of)
    stop_alerts = check_stop_loss(picks_today)
    all_alerts = market_alerts + stop_alerts

    # 总体严重度
    if any(a.get("severity") == "CRITICAL" for a in all_alerts):
        overall = "CRITICAL"
    elif any(a.get("severity") == "HIGH" for a in all_alerts):
        overall = "HIGH"
    elif all_alerts:
        overall = "LOW"
    else:
        overall = "NONE"

    # 摘要
    parts = []
    if market_alerts:
        parts.append(f"{len(market_alerts)} 条市场层")
    if stop_alerts:
        parts.append(f"{len(stop_alerts)} 条个股止损")
    summary = " · ".join(parts) if parts else "🟢 无警报"

    return {
        "alerts": all_alerts,
        "severity": overall,
        "market_alerts": market_alerts,
        "stop_loss_alerts": stop_alerts,
        "summary": summary,
    }
