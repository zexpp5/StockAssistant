"""组合层 Factor Exposure 分解 — 二审 P0.5-A (2026-05-12)。

为什么需要：
  当前系统知道"组合是哪几只股票 + 各自权重"，但不知道这些股票在**风格因子
  上的暴露分布**。一旦某个风格因子集体失效（如 2022 H1 价值股反转），就
  无法预警。

风格因子（5 个学术标准 / AQR 风格盒）：
  1. **Market Beta**  — 已用 cov/var (vs SPY)
  2. **Size**         — ln(market_cap)，正值 = 小盘暴露
  3. **Value**        — FCFY（高 = 价值股）
  4. **Momentum**     — 12-1 月动量
  5. **Quality**      — ROIC + 负 Accruals 合成

输出：组合层暴露 z-score（universe 横截面）+ 加权后的"暴露偏离"。

学术依据：
  - Fama-French (1992) JF：Size / Value 5 因子模型
  - Carhart (1997) JF：加 Momentum
  - Asness-Frazzini-Pedersen (2013)：加 Quality / Profitability
  - Grinold-Kahn (2000) "Active Portfolio Management"：组合归因标准做法

⚠️ **不自动接入**：当前只暴露 compute_portfolio_exposures()，由调用方
   (morning_brief / dashboard) 显式展示。集成进 risk_metrics 待后续会话。
"""
from __future__ import annotations
import logging
import math
from typing import Sequence

logger = logging.getLogger(__name__)


def _zscore_value(value: float | None,
                  universe_values: Sequence[float]) -> float | None:
    """单个 value 在 universe 横截面里的 z-score。

    winsorize [2%, 98%] 防极值污染；缺失值返回 None。
    """
    if value is None:
        return None
    valid = [v for v in universe_values if v is not None]
    if len(valid) < 3:
        return None
    sorted_v = sorted(valid)
    n = len(sorted_v)
    p2 = sorted_v[max(0, int(n * 0.02))]
    p98 = sorted_v[min(n - 1, int(n * 0.98))]
    w_value = max(p2, min(p98, value))
    w_valid = [max(p2, min(p98, v)) for v in valid]
    mean = sum(w_valid) / len(w_valid)
    var = sum((v - mean) ** 2 for v in w_valid) / len(w_valid)
    sd = var ** 0.5
    if sd <= 0:
        return None
    return (w_value - mean) / sd


def compute_portfolio_exposures(
    weights: dict[str, float],
    factor_records: dict[str, dict],
) -> dict:
    """计算组合的 5 因子风格暴露。

    参数：
      weights         {ticker: portfolio_weight}（应 sum ≈ 1.0）
      factor_records  {ticker: {"beta", "size", "value", "momentum", "quality"}}
                      字段来自：
                        beta     → risk_metrics.beta_vs_spy
                        size     → ln(market_cap)
                        value    → quality.fcfy
                        momentum → momentum.momentum_12_1
                        quality  → quality.roic - quality.accruals × 100  （简化合成）

    返回：
      {
        "exposure": {factor: weighted_z_score},   组合层暴露（>0 = 偏高、<0 = 偏低）
        "by_stock": {ticker: {factor: z_score}},  每只股票的 z 值（debug 用）
        "alerts": [str],                          暴露超阈值告警
        "coverage": {factor: float},              每个因子 coverage (有数据股票占比)
      }
    """
    factors = ["beta", "size", "value", "momentum", "quality"]
    universe_values = {f: [r.get(f) for r in factor_records.values()] for f in factors}

    by_stock: dict[str, dict[str, float | None]] = {}
    for tk, rec in factor_records.items():
        by_stock[tk] = {f: _zscore_value(rec.get(f), universe_values[f]) for f in factors}

    # 加权汇总（缺失股票按权重排除）
    exposure: dict[str, float] = {}
    coverage: dict[str, float] = {}
    total_w = sum(weights.values()) or 1.0
    for f in factors:
        weighted_sum = 0.0
        weight_covered = 0.0
        for tk, w in weights.items():
            z = (by_stock.get(tk) or {}).get(f)
            if z is None:
                continue
            weighted_sum += z * w
            weight_covered += w
        exposure[f] = round(weighted_sum / weight_covered, 3) if weight_covered > 0 else None
        coverage[f] = round(weight_covered / total_w, 2)

    alerts: list[str] = []
    for f, z in exposure.items():
        if z is None:
            continue
        if abs(z) > 1.0:
            direction = "高" if z > 0 else "低"
            alerts.append(f"{f} 暴露 z={z:+.2f}（偏 {direction}，超 ±1.0 阈值）")
        if coverage.get(f, 1.0) < 0.6:
            alerts.append(f"{f} 数据覆盖仅 {coverage[f]:.0%} — 暴露估算不可靠")

    return {
        "exposure": exposure,
        "by_stock": by_stock,
        "alerts": alerts,
        "coverage": coverage,
        "factor_list": factors,
        "source": "Fama-French 5-factor + Carhart Momentum + Asness Quality",
    }


def build_factor_records_from_pipeline(
    pipeline_factor_data: list[dict],
    market_caps: dict[str, float] | None = None,
    betas: dict[str, float] | None = None,
) -> dict[str, dict]:
    """从 daily_picks_v5 的 factor cache 构建 factor_records 输入。

    pipeline_factor_data: factor_scores_today.json 的 "factors" 字段
    market_caps:          {ticker: market_cap} 来自 yfinance.info（可选）
    betas:                {ticker: beta_vs_spy} 来自 risk_metrics（可选）
    """
    out: dict[str, dict] = {}
    for r in pipeline_factor_data:
        tk = r.get("ticker")
        if not tk:
            continue
        q = r.get("quality") or {}
        roic = q.get("roic") if not q.get("error") else None
        accruals = q.get("accruals") if not q.get("error") else None
        fcfy = q.get("fcfy") if not q.get("error") else None
        # quality 合成（高 ROIC + 低 Accruals = 高质量）
        quality_score = None
        if roic is not None and accruals is not None:
            quality_score = roic - accruals * 100  # 简化：减去 accruals*100 让两者尺度近似
        elif roic is not None:
            quality_score = roic

        mc = (market_caps or {}).get(tk)
        size = -math.log(mc) if (mc and mc > 0) else None  # 负 ln 让"小盘"为正向暴露
        out[tk] = {
            "beta": (betas or {}).get(tk),
            "size": size,
            "value": fcfy,
            "momentum": (r.get("momentum") or {}).get("momentum_12_1"),
            "quality": quality_score,
        }
    return out


def simulate_factor_stress(
    exposure_result: dict,
    shocks: dict[str, float] | None = None,
) -> dict:
    """因子层 stress test — 二审 P0.5-C (2026-05-12)。

    问题："如果某个 factor 一夜失效（return = shock_magnitude），组合损失多少？"

    简化模型：
      预期组合损失 ≈ portfolio_exposure[factor] × shock_magnitude
      （exposure z-score 是单位 σ 的因子暴露；shock 是因子收益）

    默认 shock 场景（基于历史最坏单月）：
      momentum -30% （如 2022/01 momentum crash）
      value    -20% （成长股 vs 价值股反转）
      quality  -15% （quality crash 罕见但存在）
      size     -25% （小盘股集体回撤）
      beta     -10% （β=1 组合在 SPY -10% 时同步）

    学术依据：
      - Daniel-Moskowitz (2016) JFE "Momentum Crashes"：1932/2009/2022 三次 momentum -30%+ 月
      - Asness 等 (2013) JoFE：value crash 案例
      - Grinold-Kahn (2000)：因子归因 stress 标准做法

    返回：{
      "scenarios": [{factor, shock, expected_pnl_pct, severity}],
      "worst": {factor, expected_pnl_pct},
      "combined_stress_pct": 三个最差因子叠加的预期损失,
    }
    """
    if shocks is None:
        shocks = {
            "momentum": -0.30,
            "value":    -0.20,
            "quality":  -0.15,
            "size":     -0.25,
            "beta":     -0.10,
        }

    exposure = exposure_result.get("exposure") or {}
    scenarios: list[dict] = []
    for f, shock in shocks.items():
        z = exposure.get(f)
        if z is None:
            scenarios.append({"factor": f, "shock_pct": shock * 100,
                              "expected_pnl_pct": None,
                              "severity": "N/A (no exposure data)"})
            continue
        # 预期 PnL = exposure × shock。注：exposure 是 z-score 单位，要解释为"σ 倍"
        # 实际工程里 z 直接乘 shock 略 hand-wavy，但作为 first-order 估计可用
        pnl = z * shock
        sev = "🔴 严重" if abs(pnl) > 0.10 else ("🟡 中等" if abs(pnl) > 0.05 else "🟢 可控")
        scenarios.append({
            "factor": f,
            "shock_pct": round(shock * 100, 1),
            "exposure_z": round(z, 2),
            "expected_pnl_pct": round(pnl * 100, 2),
            "severity": sev,
        })

    valid = [s for s in scenarios if s.get("expected_pnl_pct") is not None]
    worst = min(valid, key=lambda s: s["expected_pnl_pct"]) if valid else None
    # 最差三因子叠加（保守相关性假设：完全相关）
    top3_loss = sum(sorted([s["expected_pnl_pct"] for s in valid])[:3])

    return {
        "scenarios": scenarios,
        "worst": worst,
        "combined_stress_pct": round(top3_loss, 2) if valid else None,
        "n_factors_covered": len(valid),
        "source": "Grinold-Kahn 2000 + Daniel-Moskowitz 2016 momentum crash 标定",
    }


def format_stress_report(stress: dict) -> str:
    lines = []
    lines.append("\n=== 因子层 Stress Test（首次 P0.5-C）===\n")
    lines.append(f"{'因子':<12}{'shock':>10}{'exposure_z':>12}{'预期 PnL':>12}  {'严重度'}")
    lines.append("-" * 70)
    for s in stress["scenarios"]:
        shock = f"{s['shock_pct']:+.0f}%"
        z = f"{s.get('exposure_z', '?')}" if s.get('exposure_z') is not None else "—"
        pnl = f"{s['expected_pnl_pct']:+.2f}%" if s.get("expected_pnl_pct") is not None else "N/A"
        lines.append(f"{s['factor']:<12}{shock:>10}{z:>12}{pnl:>12}  {s['severity']}")
    if stress.get("worst"):
        w = stress["worst"]
        lines.append(f"\n🚨 单因子最差：{w['factor']} shock {w['shock_pct']:+.0f}% → 组合预期 {w['expected_pnl_pct']:+.2f}%")
    if stress.get("combined_stress_pct") is not None:
        lines.append(f"📉 最差 3 因子叠加（保守相关性=1）：{stress['combined_stress_pct']:+.2f}%")
    return "\n".join(lines)


def format_exposure_report(result: dict) -> str:
    """渲染 compute_portfolio_exposures 结果为可读文本。"""
    lines = []
    lines.append("=== 组合风格因子暴露分解（Fama-French + Carhart + Quality）===\n")
    lines.append(f"{'因子':<12}{'暴露 z':>12}{'覆盖率':>10}  {'解读'}")
    lines.append("-" * 70)
    explain = {
        "beta":     "组合 Beta 偏离市场",
        "size":     "向小盘倾斜（正）/ 大盘倾斜（负）",
        "value":    "高 FCFY 价值股（正）/ 成长股（负）",
        "momentum": "强动量股（正）/ 弱动量（负）",
        "quality":  "高 ROIC 低应计（正）/ 低质量（负）",
    }
    for f in result.get("factor_list", []):
        z = result["exposure"].get(f)
        cov = result["coverage"].get(f, 0)
        z_str = f"{z:+.2f}" if z is not None else "N/A"
        lines.append(f"{f:<12}{z_str:>12}{cov*100:>9.0f}%  {explain.get(f,'')}")

    if result.get("alerts"):
        lines.append("\n⚠️ 告警：")
        for a in result["alerts"]:
            lines.append(f"  · {a}")
    return "\n".join(lines)


if __name__ == "__main__":
    # CLI smoke test
    import json
    from pathlib import Path
    REPO = Path(__file__).resolve().parents[2]

    cache_path = REPO / "factor_scores_today.json"
    plan_path = REPO / "data" / "latest" / "plan_a_v5.json"
    if not cache_path.exists():
        print(f"❌ {cache_path} 不存在，先跑 daily_picks_v5")
        raise SystemExit(1)
    if not plan_path.exists():
        print(f"❌ {plan_path} 不存在")
        raise SystemExit(1)

    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    weights = {p["ticker"]: p.get("v5_weight", 0)
               for p in (plan.get("plan_v5") or [])}
    factor_records = build_factor_records_from_pipeline(
        cache.get("factors", []),
    )
    result = compute_portfolio_exposures(weights, factor_records)
    print(format_exposure_report(result))
    # Factor stress test
    stress = simulate_factor_stress(result)
    print(format_stress_report(stress))
