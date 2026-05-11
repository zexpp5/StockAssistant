"""系统自评分（v2）— 单维度可一票否决，不再加权平均掩盖失败。

为什么改：
  v1（roadmap 写死的"综合 89/100"）把"量化 97 / 个股深度 82 / 数据效率 90"加权平均
  得到 89 分，但**完全忽略了风控维度**：
    - 4 个崩盘 regime 实测，平均 drawdown α = -9.77%
    - 3/4 跑输 SPY；2022 加息熊跑输 27%
  按机构 risk-management 标准，这种结果不允许"被其它维度救回"。

新评分规则（min-aggregation + veto）：
  overall = min(quant, deep, data, risk)
  任一维度跌破 veto 阈值 → overall 直接降到该维度的分数
  报告里同时列出 4 个维度的分数，谁拖了后腿一眼可见

维度 → 数据源 → 阈值（每档分数定锚）：
  1. quant      ← factor_ic_gate          healthy 因子数 / 总因子数
  2. deep       ← 最近 fundamental_report 数量
  3. data       ← 关键 snapshot 新鲜度（factor_ic / stress_test / audit）
  4. risk       ← stress_test mean_alpha_dd  ★ veto dimension ★

设计原则：
  - 纯函数，注入快照路径或 dict → 单元测试友好
  - 不写文件 / 不打印；由 jobs/self_score.py 负责 I/O
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────── 阈值（机构 risk-management 经验值）───────────────

# Risk 维度（mean alpha DD）：负值越大越糟
RISK_GREAT = 0.0       # ≥ 0 ：与 SPY 持平或更抗跌
RISK_OK = -0.03        # -3% 内：可接受
RISK_WARN = -0.07      # -7% 内：警示
# < -7% ： veto，直接降到 30

# 每档对应分数
SCORE_GREAT = 90
SCORE_OK = 70
SCORE_WARN = 50
SCORE_VETO = 30


@dataclass
class DimensionScore:
    name: str
    score: int
    evidence: str
    raw: dict = field(default_factory=dict)


@dataclass
class SelfScoreResult:
    overall: int
    bottleneck: str           # 哪个维度拖了后腿
    dimensions: list[DimensionScore]
    generated_at: str
    veto_active: bool         # 是否有维度跌破 veto 阈值


# ─────────────── 单维度评分 ───────────────

def score_quant(factor_ic_snapshot: dict | None,
                mean_ic_threshold: float = 0.03,
                ir_threshold: float = 0.30) -> DimensionScore:
    """量化维度：从 factor_ic snapshot 数 healthy 因子数。"""
    if not factor_ic_snapshot or "factors" not in factor_ic_snapshot:
        return DimensionScore("quant", 30, "无 factor_ic 快照 — 跑 audit_ic", {})

    factors = factor_ic_snapshot["factors"]
    n_total = len(factors)
    if n_total == 0:
        return DimensionScore("quant", 30, "factor_ic 为空", {})

    n_healthy = 0
    n_marginal = 0
    n_inverted = 0
    for fname, info in factors.items():
        summary = info.get("summary", {}) if isinstance(info, dict) else {}
        mean_ic = summary.get("mean_ic")
        ic_ir = summary.get("ic_ir")
        if mean_ic is None or summary.get("n_periods", 0) < 3:
            continue
        if mean_ic >= mean_ic_threshold and ic_ir is not None and abs(ic_ir) >= ir_threshold:
            n_healthy += 1
        elif mean_ic <= -mean_ic_threshold:
            n_inverted += 1
        elif mean_ic > mean_ic_threshold:
            n_marginal += 1

    if n_healthy == n_total:
        score = SCORE_GREAT
    elif n_healthy >= max(1, n_total // 2):
        score = SCORE_OK
    elif n_healthy >= 1:
        score = SCORE_WARN
    elif n_inverted >= 1:
        score = SCORE_VETO
    else:
        score = SCORE_WARN

    evidence = (f"{n_healthy}/{n_total} healthy, {n_marginal} marginal, "
                f"{n_inverted} inverted（Grinold-Kahn 行业标准）")
    return DimensionScore("quant", score, evidence,
                           {"healthy": n_healthy, "marginal": n_marginal,
                            "inverted": n_inverted, "total": n_total})


def score_risk(stress_test_snapshot: dict | None) -> DimensionScore:
    """风控维度（★ veto dimension ★）：从 stress_test mean alpha DD 分档。"""
    if not stress_test_snapshot or "results" not in stress_test_snapshot:
        return DimensionScore("risk", 30, "无 stress_test 快照 — 跑 stress_test", {})

    results = stress_test_snapshot["results"]
    if not results:
        return DimensionScore("risk", 30, "stress_test 无 regime 结果", {})

    alpha_dds = [r.get("alpha_dd_pct", 0) / 100.0 for r in results
                 if r.get("alpha_dd_pct") is not None]
    if not alpha_dds:
        return DimensionScore("risk", 30, "stress_test 无 alpha_dd_pct 字段", {})

    mean_alpha = sum(alpha_dds) / len(alpha_dds)
    worst_alpha = min(alpha_dds)
    n_outperform = sum(1 for a in alpha_dds if a > 0)

    if mean_alpha >= RISK_GREAT:
        score = SCORE_GREAT
    elif mean_alpha >= RISK_OK:
        score = SCORE_OK
    elif mean_alpha >= RISK_WARN:
        score = SCORE_WARN
    else:
        score = SCORE_VETO    # ← veto active

    evidence = (f"{n_outperform}/{len(alpha_dds)} regime 抗跌 SPY；"
                f"mean α_DD={mean_alpha*100:+.2f}%；worst={worst_alpha*100:+.2f}%")
    return DimensionScore("risk", score, evidence,
                           {"mean_alpha_dd": mean_alpha,
                            "worst_alpha_dd": worst_alpha,
                            "n_regimes": len(alpha_dds),
                            "n_outperform": n_outperform})


def score_deep(reports_dir: Path | None, days_window: int = 30) -> DimensionScore:
    """个股深度维度：最近 N 天 fundamental_report 数。

    阈值（buy-side analyst 一年 ~ 30 份覆盖标的 → 每月 2-3 份）：
      ≥ 5 篇/30d   → 90 (覆盖充分)
      2-4 篇       → 70 (起步)
      1 篇         → 50
      0 篇         → 30
    """
    if reports_dir is None or not Path(reports_dir).exists():
        return DimensionScore("deep", 30, f"{reports_dir} 不存在", {})

    cutoff = datetime.now().timestamp() - days_window * 86400
    recent = [p for p in Path(reports_dir).glob("*.md")
              if p.stat().st_mtime >= cutoff]
    n = len(recent)
    if n >= 5:
        score = SCORE_GREAT
    elif n >= 2:
        score = SCORE_OK
    elif n >= 1:
        score = SCORE_WARN
    else:
        score = SCORE_VETO
    evidence = f"近 {days_window} 天 {n} 份研报"
    return DimensionScore("deep", score, evidence,
                           {"n_reports_30d": n, "days_window": days_window})


def score_data(audit_dir: Path | None,
               required_prefixes: tuple[str, ...] = ("factor_ic", "stress_test", "audit"),
               max_age_days: int = 7) -> DimensionScore:
    """数据效率维度：关键 snapshot 是否在 max_age_days 内有更新。"""
    if audit_dir is None or not Path(audit_dir).exists():
        return DimensionScore("data", 30, f"{audit_dir} 不存在", {})

    cutoff = datetime.now().timestamp() - max_age_days * 86400
    fresh = []
    stale = []
    for prefix in required_prefixes:
        matches = sorted(Path(audit_dir).glob(f"{prefix}_*.json"), reverse=True)
        if matches and matches[0].stat().st_mtime >= cutoff:
            fresh.append(prefix)
        else:
            stale.append(prefix)

    n_fresh = len(fresh)
    n_total = len(required_prefixes)
    if n_fresh == n_total:
        score = SCORE_GREAT
    elif n_fresh >= n_total - 1:
        score = SCORE_OK
    elif n_fresh >= 1:
        score = SCORE_WARN
    else:
        score = SCORE_VETO

    evidence = f"{n_fresh}/{n_total} 数据源 {max_age_days} 天内新鲜"
    return DimensionScore("data", score, evidence,
                           {"fresh": fresh, "stale": stale,
                            "max_age_days": max_age_days})


# ─────────────── 主入口 ───────────────

def compute_self_score(*,
                       factor_ic_snapshot: dict | None,
                       stress_test_snapshot: dict | None,
                       reports_dir: Path | None,
                       audit_dir: Path | None) -> SelfScoreResult:
    """计算系统自评分 = min(quant, deep, data, risk)。

    任一维度 < SCORE_VETO（30）会拖低 overall；
    risk 是显式 veto dimension（mean DD alpha < -7% → 30）。
    """
    quant = score_quant(factor_ic_snapshot)
    risk = score_risk(stress_test_snapshot)
    deep = score_deep(reports_dir)
    data = score_data(audit_dir)

    dims = [quant, deep, data, risk]
    overall = min(d.score for d in dims)
    bottleneck = min(dims, key=lambda d: d.score).name
    veto_active = risk.score <= SCORE_VETO

    return SelfScoreResult(
        overall=overall,
        bottleneck=bottleneck,
        dimensions=dims,
        generated_at=datetime.now().isoformat(timespec="seconds"),
        veto_active=veto_active,
    )


# ─────────────── 报告渲染 ───────────────

def format_report(result: SelfScoreResult) -> str:
    """渲染为可读报告。"""
    lines = [
        "=" * 72,
        f"  系统自评分（min-aggregation，单维度可一票否决）",
        "=" * 72,
        f"  Overall : {result.overall} / 100   ← min(...)",
        f"  瓶颈    : {result.bottleneck} 维度",
        f"  生成时间: {result.generated_at}",
    ]
    if result.veto_active:
        lines.append(f"  ⛔ Veto active：risk 维度跌破阈值，整体分数被限定")
    lines.append("")
    lines.append(f"  {'维度':<10}{'分数':>6}   证据")
    lines.append(f"  {'-'*70}")
    for d in result.dimensions:
        icon = ("🟢" if d.score >= SCORE_GREAT else
                "🟡" if d.score >= SCORE_OK else
                "🟠" if d.score >= SCORE_WARN else "🔴")
        lines.append(f"  {d.name:<10}{d.score:>4}/100  {icon} {d.evidence}")
    lines.append("")
    lines.append(f"  解读：overall = min(...)，任一维度差全局即差。")
    lines.append(f"  对比 v1 加权平均：综合 89 → 实际 {result.overall}（差距即 v1 的盲点）")
    lines.append("=" * 72)
    return "\n".join(lines)


# ─────────────── 序列化（用于 self_score.json）───────────────

def to_json(result: SelfScoreResult) -> dict:
    return {
        "overall": result.overall,
        "bottleneck": result.bottleneck,
        "veto_active": result.veto_active,
        "generated_at": result.generated_at,
        "dimensions": [
            {"name": d.name, "score": d.score, "evidence": d.evidence, "raw": d.raw}
            for d in result.dimensions
        ],
        "methodology": "min-aggregation; risk dim is veto (mean DD alpha < -7% → 30)",
    }
