"""Brinson 业绩归因 — 把组合超额收益拆成"行业配置贡献 + 行业内选股 alpha"。

学术依据：
  - Brinson, Hood & Beebower (1986) "Determinants of Portfolio Performance",
    Financial Analysts Journal — 行业经典
  - Brinson & Fachler (1985) — 早期版本
  - 行业实证：long-only 基金 80%+ 收益来自行业配置，仅 20% 来自选股 alpha

公式：
  Total Active Return = Σ_i [Allocation_i + Selection_i + Interaction_i]
    Allocation_i  = (w_p_i - w_b_i) × R_b_i      （超配 / 低配某行业的贡献）
    Selection_i   = w_b_i × (R_p_i - R_b_i)      （行业内选股 alpha）
    Interaction_i = (w_p_i - w_b_i) × (R_p_i - R_b_i)  （交互项，通常合到 Selection）

为什么需要：
  v6 的 monthly_letter 现在只能说 "v6 跑赢 SPY +X%"，但回答不了：
    - 是因为押对了科技板块（行业 beta）？
    - 还是真的选股有 alpha（行业内跑赢同行）？
  Brinson 归因明确分离这两个来源。

输入：
  picks_today: 飞书 picks 表（含"主题分类" + "累计涨跌%"）
  benchmark_returns: 11 个 GICS ETF 同期收益（来自 sector_etf）
  benchmark_weights: SPY 中各行业的权重（可硬编码标准值）

输出：
  {
    'allocation_effect': float,  # 行业配置贡献
    'selection_effect': float,    # 行业内选股贡献
    'total_active_return': float,
    'by_industry': {industry: {alloc, sel, w_p, w_b, r_p, r_b}},
  }
"""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ─────────── SPY 行业权重（State Street 官方数据，按 GICS ETF 对应）───────────
# 数据源: SPDR S&P 500 ETF Trust 持仓报告（2026 Q1 估值）
# 这是大致权重，月度可微调
SPY_SECTOR_WEIGHTS = {
    "XLK": 0.31,   # 科技 31%
    "XLC": 0.10,   # 通信 10%
    "XLY": 0.10,   # 可选消费 10%
    "XLP": 0.06,   # 必需消费 6%
    "XLV": 0.10,   # 医疗 10%
    "XLF": 0.13,   # 金融 13%
    "XLI": 0.08,   # 工业 8%
    "XLU": 0.025,  # 公用事业 2.5%
    "XLRE": 0.025, # 房地产 2.5%
    "XLE": 0.04,   # 能源 4%
    "XLB": 0.02,   # 原材料 2%
}


# ─────────── 工具 ───────────

def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─────────── Brinson 归因 ───────────

def attribute(picks_today: list[dict[str, Any]],
              etf_returns: dict[str, dict[str, Any]],
              benchmark_weights: dict[str, float] | None = None) -> dict[str, Any]:
    """对组合做 Brinson 业绩归因。

    参数：
      picks_today: 每条记录 normalized 含 theme + cum_pct
      etf_returns: {"XLK": {"cum_return_pct": 25.78}, ...}
      benchmark_weights: 默认用 SPY_SECTOR_WEIGHTS

    返回归因 dict。
    """
    from .sector_etf import THEME_TO_ETF

    if benchmark_weights is None:
        benchmark_weights = SPY_SECTOR_WEIGHTS

    # 1. 把 picks 按 ETF 分组
    by_etf: dict[str, list[dict[str, Any]]] = {}
    for p in picks_today:
        n = p.get("normalized", p)
        theme = n.get("theme", "")
        etf = THEME_TO_ETF.get(theme, "XLK")
        by_etf.setdefault(etf, []).append(p)

    # 2. 算组合在每个 ETF 的权重 + 收益
    n_total = sum(len(picks) for picks in by_etf.values())
    if n_total == 0:
        return {"error": "no picks"}

    portfolio_weights = {etf: len(picks) / n_total for etf, picks in by_etf.items()}
    portfolio_returns = {}
    for etf, picks in by_etf.items():
        rets = [_to_float(p.get("normalized", p).get("cum_pct")) for p in picks]
        rets = [r for r in rets if r is not None]
        portfolio_returns[etf] = (sum(rets) / len(rets)) if rets else 0.0

    # 3. 算每个行业的归因
    by_industry: dict[str, dict[str, float]] = {}
    total_alloc = 0.0
    total_sel = 0.0

    # 遍历所有相关 ETF（组合持有的 + benchmark 含的）
    all_etfs = set(portfolio_weights.keys()) | set(benchmark_weights.keys())
    for etf in all_etfs:
        w_p = portfolio_weights.get(etf, 0.0)
        w_b = benchmark_weights.get(etf, 0.0)
        r_p = portfolio_returns.get(etf, 0.0)  # %
        r_b_info = etf_returns.get(etf, {})
        r_b = r_b_info.get("cum_return_pct", 0.0)

        # Brinson 公式
        alloc_effect = (w_p - w_b) * r_b
        sel_effect = w_p * (r_p - r_b)  # 把 interaction 归到 selection（标准做法）

        by_industry[etf] = {
            "etf_name": r_b_info.get("name", etf),
            "w_p": round(w_p, 4),
            "w_b": round(w_b, 4),
            "r_p_pct": round(r_p, 2),
            "r_b_pct": round(r_b, 2),
            "allocation_effect_pct": round(alloc_effect, 2),
            "selection_effect_pct": round(sel_effect, 2),
            "total_effect_pct": round(alloc_effect + sel_effect, 2),
            "n_picks": len(by_etf.get(etf, [])),
        }
        total_alloc += alloc_effect
        total_sel += sel_effect

    # 4. 总组合表现 vs 基准
    benchmark_return = sum(
        benchmark_weights.get(etf, 0) * etf_returns.get(etf, {}).get("cum_return_pct", 0)
        for etf in all_etfs
    )
    portfolio_return = sum(
        portfolio_weights.get(etf, 0) * portfolio_returns.get(etf, 0)
        for etf in all_etfs
    )
    active_return = portfolio_return - benchmark_return

    return {
        "portfolio_return_pct": round(portfolio_return, 2),
        "benchmark_return_pct": round(benchmark_return, 2),
        "active_return_pct": round(active_return, 2),
        "allocation_effect_pct": round(total_alloc, 2),
        "selection_effect_pct": round(total_sel, 2),
        "by_industry": by_industry,
        "n_picks": n_total,
    }


def format_text(result: dict[str, Any]) -> str:
    """格式化归因结果。"""
    if "error" in result:
        return f"❌ {result['error']}"
    lines = [
        "📊 Brinson 业绩归因（Brinson-Hood-Beebower 1986）",
        "",
        f"  组合收益 = {result['portfolio_return_pct']:+.2f}%",
        f"  基准（GICS 加权 SPY）= {result['benchmark_return_pct']:+.2f}%",
        f"  活跃收益 (Active) = {result['active_return_pct']:+.2f}%",
        "",
        f"  ├─ 行业配置贡献 (Allocation) = {result['allocation_effect_pct']:+.2f}%",
        f"  └─ 行业内选股 alpha (Selection) = {result['selection_effect_pct']:+.2f}%",
        "",
        "按行业拆解：",
        f"  {'ETF':<6}{'组合w':>8}{'基准w':>8}{'组合R':>9}{'基准R':>9}{'Alloc':>9}{'Select':>9}",
    ]
    rows = sorted(result["by_industry"].items(),
                  key=lambda x: -abs(x[1]["total_effect_pct"]))
    for etf, info in rows:
        if info["w_p"] == 0 and info["w_b"] == 0:
            continue
        lines.append(
            f"  {etf:<6}{info['w_p']*100:>+7.1f}%{info['w_b']*100:>+7.1f}%"
            f"{info['r_p_pct']:>+8.2f}%{info['r_b_pct']:>+8.2f}%"
            f"{info['allocation_effect_pct']:>+8.2f}%{info['selection_effect_pct']:>+8.2f}%"
        )
    return "\n".join(lines)
