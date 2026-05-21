"""组合约束：交易成本扣减 + ADV%（流动性）限制。

学术依据：
  - Almgren & Chriss (2001) "Optimal Execution of Portfolio Transactions" — 冲击成本理论
  - Frazzini, Israel & Moskowitz (2018) "Trading Costs" — 实证：流动性最差 1/5 股票，
    实际交易成本可达预期 alpha 的 50%+，足以毁掉 paper portfolio 的优势

为什么要做：
  Markowitz 优化生成的"理论权重"在现实里跑不动：
    1. 单日买入 100 万只能消化 ADV(平均成交额) 的 5-10%，否则推高股价
    2. 双边交易成本 5-15 bps，频繁调仓会把 alpha 吃掉
    3. 小盘股 / A 股低流动票尤其严重

用法：
  from stock_research.core.portfolio_constraints import (
      apply_transaction_cost, cap_by_adv, summarize_costs,
  )

  # 1. 限流（不让单日交易超过 ADV 的 5%）
  capped = cap_by_adv(target_w, prev_w, adv_dollars, portfolio_value, max_adv_pct=0.05)

  # 2. 算成本扣减
  cost_summary = apply_transaction_cost(capped, prev_w, portfolio_value,
                                        cost_bps=5, impact_bps_per_pct_adv=2)
"""
from __future__ import annotations
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ─────────── 行业敞口约束 ───────────

def cap_by_industry(weights: dict[str, float],
                    industries: dict[str, str],
                    max_industry_pct: float = 0.25) -> tuple[dict[str, float], dict[str, float]]:
    """限制单一行业总权重 ≤ max_industry_pct。超出按比例缩小，溢出权重转入"现金"。

    参数：
      weights              {ticker: weight}（Markowitz 输出）
      industries           {ticker: industry}（行业归类）
      max_industry_pct     单行业权重上限，默认 25%

    返回 (capped_weights, industry_summary)：
      capped_weights       被缩放后的权重（每行业 ≤ max_industry_pct）
      industry_summary     {industry: {"original": x, "capped": y, "overflow": z}}

    溢出权重不强行分给其他行业（避免连锁触顶），由 cash_pct 吸收。
    """
    if max_industry_pct <= 0 or max_industry_pct > 1:
        raise ValueError("max_industry_pct must be in (0, 1]")

    # 按行业分组
    by_industry: dict[str, list[tuple[str, float]]] = {}
    for ticker, w in weights.items():
        ind = industries.get(ticker, "Unknown") or "Unknown"
        by_industry.setdefault(ind, []).append((ticker, w))

    capped = dict(weights)
    summary: dict[str, dict[str, float]] = {}

    for industry, items in by_industry.items():
        total = sum(w for _, w in items)
        if total > max_industry_pct + 1e-9:
            scale = max_industry_pct / total
            overflow = 0.0
            for ticker, w in items:
                new_w = w * scale
                overflow += w - new_w
                capped[ticker] = new_w
            summary[industry] = {
                "original": round(total, 4),
                "capped": round(max_industry_pct, 4),
                "overflow": round(overflow, 4),
                "n_stocks": len(items),
            }
        else:
            summary[industry] = {
                "original": round(total, 4),
                "capped": round(total, 4),
                "overflow": 0.0,
                "n_stocks": len(items),
            }

    return capped, summary


# ─────────── 单股止损（个股层防御）───────────

def apply_stop_loss(price_path, entry_price: float | None = None,
                    stop_pct: float = 0.15) -> tuple:
    """对单只股票的价格序列应用 -stop_pct 止损规则。

    学术依据：
      - O'Neil (2002) "How to Make Money in Stocks" — 经典 -7% 到 -8% 止损
      - 我们用 -15% 做保守版（避免被日内噪音震出）

    参数：
      price_path   pandas.Series，索引为日期，值为价格
      entry_price  入场价格；默认用 price_path 第一个值
      stop_pct     止损百分比（默认 0.15 = -15%）

    返回 (capped_path, triggered_at)：
      capped_path  止损后的价格序列（触发后从那天起冻结在 -stop_pct 水平）
      triggered_at 触发止损的日期；None 表示未触发

    注意：
      - 触发后假设全部出局换现金（不再参与后续涨跌）
      - 这是保守模拟，实际止损可能因滑点更差，也可能因反弹追回更好
    """
    import pandas as pd

    if not isinstance(price_path, pd.Series) or len(price_path) == 0:
        return price_path, None

    entry = float(entry_price) if entry_price is not None else float(price_path.iloc[0])
    if entry <= 0:
        return price_path, None

    threshold = entry * (1 - stop_pct)
    capped = price_path.copy()
    triggered_at = None
    for i, (date, p) in enumerate(price_path.items()):
        if float(p) <= threshold:
            triggered_at = date
            # 从这天起冻结在止损线
            capped.iloc[i:] = threshold
            break
    return capped, triggered_at


def check_stop_loss_breach(entry_price: float | None,
                           current_price: float | None,
                           stop_pct: float = 0.15) -> tuple[bool, float]:
    """生产实时监控版：单点判断 (entry, current) 是否破止损线。

    与 apply_stop_loss（回测处理价格序列）共用阈值语义；
    morning_brief 每天读 holdings + 最新收盘价调用此函数生成告警。

    返回 (breached, drawdown_ratio)：
      breached       是否破 -stop_pct 止损线
      drawdown_ratio (current - entry) / entry，负值表示亏损
    """
    if entry_price is None or current_price is None or entry_price <= 0:
        return False, 0.0
    drawdown = (current_price - entry_price) / entry_price
    return drawdown <= -stop_pct, drawdown


def volatility_proxy_atr(closes: list[float], lookback: int = 14) -> float | None:
    """用收盘价序列估算 ATR-style 日均绝对涨跌幅（fraction，0.025 = ±2.5%/日）。

    真 ATR = EMA(True Range, 14)，需要 high/low；本函数仅用 close 做近似。
    morning_brief 的 history_data.json 仅含 close，故用此 proxy；
    后续若 history 补 high/low 可升级到真 ATR（见 true_atr 函数）。
    """
    clean_closes: list[float] = []
    for v in closes or []:
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv > 0:
            clean_closes.append(fv)
    if len(clean_closes) < lookback + 1:
        return None
    recent = clean_closes[-(lookback + 1):]
    abs_returns = [abs(recent[i] / recent[i - 1] - 1)
                   for i in range(1, len(recent))
                   if recent[i - 1] > 0]
    if not abs_returns:
        return None
    return sum(abs_returns) / len(abs_returns)


def true_atr(highs: list[float] | None, lows: list[float] | None,
             closes: list[float], period: int = 14) -> float | None:
    """真 ATR（Wilder 1978 smoothing），需要 high / low / close 三序列同长度。

    True Range = max(high - low, |high - prev_close|, |low - prev_close|)
    ATR(t) = (ATR(t-1) × (n-1) + TR(t)) / n   ← Wilder 平滑
    返回 NATR fraction (ATR / last_close)，跨标的可比。

    缺 high/low 时返回 None；上游应 fallback 到 volatility_proxy_atr。
    """
    if not highs or not lows or not closes:
        return None
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return None
    h, l, c = highs[-n:], lows[-n:], closes[-n:]
    trs: list[float] = []
    for i in range(1, n):
        try:
            hi = float(h[i])
            lo = float(l[i])
            prev_close = float(c[i - 1])
        except (TypeError, ValueError):
            continue
        if hi <= 0 or lo <= 0 or prev_close <= 0:
            continue
        trs.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
    if len(trs) < period:
        return None
    # Wilder smoothing：前 period 个 TR 做 SMA 做种子，之后递归
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    try:
        last_close = float(c[-1])
    except (TypeError, ValueError):
        return None
    if last_close <= 0:
        return None
    return atr / last_close


def volatility_adaptive_stop_pct(closes: list[float],
                                 highs: list[float] | None = None,
                                 lows: list[float] | None = None,
                                 multiplier: float = 2.5,
                                 lookback: int = 14,
                                 holding_days: int = 21,
                                 min_stop: float = 0.07,
                                 max_stop: float = 0.25,
                                 fallback: float = 0.15) -> tuple[float, str]:
    """按个股波动率算动态止损百分比，替代跨标的不可比的固定 -15%。

    优先级：true_atr（含 high/low）→ volatility_proxy_atr（仅 close）→ fallback。

    模型：atr_fraction × multiplier × sqrt(holding_days)
    - 高波动股 (atr=4%) → -23%（避免日内噪声震出）
    - 低波动股 (atr=1%) → -11%（小幅破位即出）
    - cap 在 [min_stop, max_stop]

    返回 (stop_pct, source)：source 标注用了 true_atr / proxy_atr / fallback
    holding_days=21 假设月度调仓，stop_pct 反映"持有一个月可能的累积波动"。
    """
    atr_fraction = true_atr(highs, lows, closes, period=lookback)
    source = "true_atr"
    if atr_fraction is None:
        atr_fraction = volatility_proxy_atr(closes, lookback=lookback)
        source = "proxy_atr"
    if atr_fraction is None:
        return fallback, "fallback"
    scaled = atr_fraction * multiplier * (holding_days ** 0.5)
    return max(min_stop, min(max_stop, scaled)), source


# ─────────── Kelly 仓位上限（破产保护）───────────

def kelly_cap(weights: dict[str, float],
              max_single_pct: float = 0.15,
              kelly_fraction: float = 0.5) -> dict[str, float]:
    """半 Kelly 上限：单只仓位不超过 max_single_pct × kelly_fraction。

    默认 max=15% × 0.5 = 7.5% (半 Kelly)，比直接卡 15% 更保守。
    避免单只股票暴雷拖垮组合（Kelly 1956 / Thorp 2006 实证半 Kelly 风险调整后收益更高）。
    """
    cap = max_single_pct * kelly_fraction
    return {t: min(w, cap) for t, w in weights.items()}


@dataclass
class CostBreakdown:
    """单笔交易的成本分解。"""
    ticker: str
    delta_weight: float       # 权重变化（正=加仓 / 负=减仓）
    delta_dollars: float      # 美元金额变化
    pct_of_adv: float         # 占当日 ADV 的百分比
    commission_bps: float     # 佣金（bps）
    slippage_bps: float       # 滑点（bps）
    impact_bps: float         # 冲击成本（bps）
    total_cost_bps: float     # 合计 bps
    total_cost_dollars: float # 合计美元


# ─────────── ADV 限流（流动性约束）───────────

def cap_by_adv(target_weights: dict[str, float],
               prev_weights: dict[str, float],
               adv_dollars: dict[str, float],
               portfolio_value: float,
               max_adv_pct: float = 0.05) -> tuple[dict[str, float], list[str]]:
    """限制单日净买入/卖出 ≤ max_adv_pct × ADV。

    返回 (capped_weights, warnings)：
      - capped_weights：调整后的目标权重（可能比 target_weights 更接近 prev_weights）
      - warnings：超出 ADV 限额、被强制 cap 的标的列表

    简化假设：单日全部完成调仓。实际机构会拆 1-5 天，但本函数不做时间维度展开。
    """
    if portfolio_value <= 0:
        raise ValueError("portfolio_value must be > 0")

    capped = {}
    warnings = []
    for ticker, target in target_weights.items():
        prev = prev_weights.get(ticker, 0.0)
        delta_w = target - prev
        delta_dollars = delta_w * portfolio_value
        adv = adv_dollars.get(ticker, 0.0)

        if adv <= 0:
            # 没有 ADV 数据，保守不允许调仓（或者全量允许，视策略）
            # 默认：不调仓
            capped[ticker] = prev
            warnings.append(f"{ticker}: 无 ADV 数据，保持原权重")
            continue

        max_dollars_per_side = adv * max_adv_pct
        if abs(delta_dollars) > max_dollars_per_side:
            # 限流：把 delta 砍到 ADV 上限
            sign = 1 if delta_dollars > 0 else -1
            capped_delta_dollars = sign * max_dollars_per_side
            capped_delta_w = capped_delta_dollars / portfolio_value
            capped[ticker] = prev + capped_delta_w
            warnings.append(
                f"{ticker}: 目标 Δ={delta_w:+.2%}（${delta_dollars:,.0f}），"
                f"超 {max_adv_pct:.0%} ADV(${adv:,.0f}) 上限，截到 Δ={capped_delta_w:+.2%}"
            )
        else:
            capped[ticker] = target

    return capped, warnings


# ─────────── 交易成本扣减 ───────────

def apply_transaction_cost(
    new_weights: dict[str, float],
    prev_weights: dict[str, float],
    portfolio_value: float,
    adv_dollars: dict[str, float] | None = None,
    cost_bps: float = 5.0,
    impact_bps_per_pct_adv: float = 2.0,
) -> dict:
    """计算从 prev_weights 调到 new_weights 的全部交易成本。

    成本模型（线性冲击）：
      total_bps = commission + slippage + impact
        commission = cost_bps（默认 5 bps，券商+交易所合计）
        slippage   = 0（这里假设 limit 单，不计；market 单可加 1-3 bps）
        impact     = impact_bps_per_pct_adv × (|Δ$| / ADV × 100)
                     例：impact 2 bps × (Δ占 ADV 的 5%) = 10 bps

    参数：
      cost_bps                  双边总佣金/交易税（bps），默认 5
      impact_bps_per_pct_adv    每占 1% ADV 的冲击成本（bps），默认 2
                                (等于 100 bps × 占 ADV 比例 × 2)

    返回 {
      "total_cost_dollars": $,
      "total_cost_bps_of_portfolio": bps,
      "turnover": 0.xx (单边换手率),
      "breakdowns": [CostBreakdown, ...]
    }
    """
    if portfolio_value <= 0:
        raise ValueError("portfolio_value must be > 0")

    breakdowns: list[CostBreakdown] = []
    total_dollars = 0.0
    one_side_turnover = 0.0  # 单边换手 = sum(|Δw|) / 2

    all_tickers = set(new_weights) | set(prev_weights)
    for ticker in all_tickers:
        new_w = new_weights.get(ticker, 0.0)
        prev_w = prev_weights.get(ticker, 0.0)
        delta_w = new_w - prev_w
        if abs(delta_w) < 1e-9:
            continue
        delta_dollars = delta_w * portfolio_value
        one_side_turnover += abs(delta_w) / 2

        adv = (adv_dollars or {}).get(ticker, 0.0)
        if adv > 0:
            pct_adv = abs(delta_dollars) / adv * 100  # 占 ADV 的百分比
            impact = impact_bps_per_pct_adv * pct_adv
        else:
            pct_adv = 0.0
            # 没 ADV 数据时给保守的固定 impact
            impact = 10.0

        commission = cost_bps
        slippage = 0.0
        total_bps = commission + slippage + impact
        cost_dollars = abs(delta_dollars) * total_bps / 10_000

        breakdowns.append(CostBreakdown(
            ticker=ticker,
            delta_weight=delta_w,
            delta_dollars=delta_dollars,
            pct_of_adv=pct_adv,
            commission_bps=commission,
            slippage_bps=slippage,
            impact_bps=impact,
            total_cost_bps=total_bps,
            total_cost_dollars=cost_dollars,
        ))
        total_dollars += cost_dollars

    return {
        "total_cost_dollars": total_dollars,
        "total_cost_bps_of_portfolio": (total_dollars / portfolio_value * 10_000) if portfolio_value else 0,
        "turnover": one_side_turnover,
        "breakdowns": breakdowns,
    }


# ─────────── 报告 ───────────

def summarize_costs(cost_result: dict, top_n: int = 10) -> str:
    """把 apply_transaction_cost 结果格式化成可读报告。"""
    lines = []
    lines.append(f"组合层成本：${cost_result['total_cost_dollars']:,.2f} "
                 f"({cost_result['total_cost_bps_of_portfolio']:.1f} bps)")
    lines.append(f"单边换手率：{cost_result['turnover']:.1%}")
    lines.append("")
    lines.append("Top 调仓成本明细：")
    bd_sorted = sorted(cost_result["breakdowns"],
                       key=lambda b: -b.total_cost_dollars)
    for b in bd_sorted[:top_n]:
        action = "买" if b.delta_weight > 0 else "卖"
        lines.append(f"  {action} {b.ticker:<8} Δw={b.delta_weight:+.2%} "
                     f"Δ$={b.delta_dollars:+,.0f} | {b.pct_of_adv:5.1f}% ADV | "
                     f"cost={b.total_cost_bps:5.1f} bps = ${b.total_cost_dollars:,.0f}")
    return "\n".join(lines)


def alpha_after_cost(gross_alpha_pct: float, cost_bps: float) -> float:
    """从总 alpha 扣除交易成本，得到净 alpha。

    例：gross alpha = 12% (1200 bps)，total cost = 35 bps → net alpha = 11.65%
    """
    return gross_alpha_pct - (cost_bps / 100.0)
