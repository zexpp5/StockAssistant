"""因子 IC CI 闸门：在 daily_picks 写飞书前检查因子有效性。

为什么需要这个闸门：
  自审里 IC 已经做了 audit_ic（产出 mean IC / IR / hit rate），但**没人在生产路径
  上消费这个信号**。因子 IC = -0.017、IR = -0.07 时，daily_picks 还在照常写飞书。
  这违反 Grinold-Kahn 行业标准：因子 IR < 0.3 属于"不可投资 alpha"，应停止使用。

闸门规则（保守、明确、可解释）：
  1. **新鲜度**：最新 factor_ic snapshot 必须在 max_age_days 内（默认 35 天）
     —— 过期闸门不可信，宁可手动重跑也不放行
  2. **生产等权因子必须全部 healthy**：v6 composite 等权使用的每个因子都要满足
     mean_ic ≥ mean_ic_threshold 且 |ic_ir| ≥ ir_threshold（默认 0.03 / 0.30）
     —— 未验证/衰减因子不能继续以等权进入 buy 推荐
  3. **反向因子单独标记**：mean_ic < -mean_ic_threshold（inverted alpha）的因子
     报告里红字提示，可以反向使用但不计入"healthy"

阈值依据：
  - Grinold-Kahn 2000 Active Portfolio Management 行业标准：
    IR > 0.5 = 优秀；0.3-0.5 = 良好；< 0.2 = 边际，几乎和噪声无法区分
  - mean IC 0.03 是 quant 界 buy-side 的"边际有效"下限（Chincarini-Kim 2006）

API：
  evaluate_gate(snapshot=None, ...) → GateResult
    - 不传 snapshot 时自动从 data/snapshots/audit/ 读最新 factor_ic_*.json
    - 返回 (passed, reason, details)，调用方决定降级/中止/警告

设计原则：
  - 纯函数 + 可注入快照 → 单元测试友好
  - 闸门 fail 不抛异常，只返回 (False, reason)，由调用方决定行为
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────── 默认参数（Grinold-Kahn 2000 + Chincarini-Kim 2006）───────────────

DEFAULT_MEAN_IC_THRESHOLD = 0.03     # 边际有效下限
DEFAULT_IR_THRESHOLD = 0.30          # IR 良好下限
DEFAULT_MAX_AGE_DAYS = 35            # 超过 35 天的 IC 数据视为过期
DEFAULT_WATCH_FACTORS = ["f_score", "momentum", "reversal", "pead", "analyst", "quality"]


@dataclass
class FactorVerdict:
    factor: str
    mean_ic: float | None
    ic_ir: float | None
    hit_rate: float | None
    n_periods: int
    status: str          # healthy / marginal / decayed / inverted / no_data
    note: str


@dataclass
class GateResult:
    passed: bool
    reason: str
    snapshot_path: str | None
    snapshot_age_days: float | None
    factor_verdicts: list[FactorVerdict] = field(default_factory=list)
    healthy_factors: list[str] = field(default_factory=list)
    inverted_factors: list[str] = field(default_factory=list)
    thresholds: dict[str, Any] = field(default_factory=dict)


# ─────────────── 内部工具 ───────────────

def _classify_factor(mean_ic: float | None,
                     ic_ir: float | None,
                     n_periods: int,
                     mean_ic_threshold: float,
                     ir_threshold: float) -> tuple[str, str]:
    """单因子分类：healthy / marginal / decayed / inverted / no_data。

    healthy   = mean_ic ≥ mean_ic_threshold 且 |ir| ≥ ir_threshold
    marginal  = mean_ic > 0 但未达 healthy
    decayed   = -mean_ic_threshold < mean_ic ≤ mean_ic_threshold（≈ 0）
    inverted  = mean_ic ≤ -mean_ic_threshold（反向 alpha，可反用但不计正分）
    no_data   = n_periods < 3 或 mean_ic is None
    """
    if mean_ic is None or n_periods < 3:
        return "no_data", f"样本不足（n_periods={n_periods}）"

    if mean_ic <= -mean_ic_threshold:
        ir_str = f"{ic_ir:+.2f}" if ic_ir is not None else "N/A"
        return "inverted", f"反向 alpha（mean IC={mean_ic:+.3f}, IR={ir_str}）"

    if -mean_ic_threshold < mean_ic <= mean_ic_threshold:
        return "decayed", f"已失效（mean IC={mean_ic:+.3f} ≈ 0）"

    # 此分支：mean_ic > mean_ic_threshold
    if ic_ir is not None and abs(ic_ir) >= ir_threshold:
        return "healthy", f"有效（mean IC={mean_ic:+.3f}, IR={ic_ir:+.2f}）"

    ir_str = f"{ic_ir:+.2f}" if ic_ir is not None else "N/A"
    return "marginal", f"边际（mean IC={mean_ic:+.3f}, IR={ir_str}，IR 未达 {ir_threshold}）"


def _parse_snapshot_age(payload: dict, snapshot_path: Path | None) -> float | None:
    """从 payload['generated_at'] 或文件 mtime 推断 snapshot 年龄（天）。"""
    gen = payload.get("generated_at") if isinstance(payload, dict) else None
    if gen:
        try:
            dt = datetime.fromisoformat(str(gen).replace("Z", ""))
            return (datetime.now() - dt).total_seconds() / 86400.0
        except (ValueError, TypeError):
            pass
    if snapshot_path and snapshot_path.exists():
        mtime = datetime.fromtimestamp(snapshot_path.stat().st_mtime)
        return (datetime.now() - mtime).total_seconds() / 86400.0
    return None


# ─────────────── 主入口 ───────────────

def evaluate_gate(snapshot: dict | None = None,
                  *,
                  snapshot_path: Path | None = None,
                  watch_factors: list[str] | None = None,
                  mean_ic_threshold: float = DEFAULT_MEAN_IC_THRESHOLD,
                  ir_threshold: float = DEFAULT_IR_THRESHOLD,
                  max_age_days: float = DEFAULT_MAX_AGE_DAYS,
                  require_all_factors: bool = True,
                  audit_dir: Path | None = None
                  ) -> GateResult:
    """评估因子 IC 闸门状态。

    Args:
      snapshot:       直接传入的 audit dict（测试/重跑场景）。None 时从 audit_dir 读最新
      snapshot_path:  快照来源路径（仅用于报告）
      watch_factors:  要检查的因子名列表
      mean_ic_threshold / ir_threshold:  healthy 阈值
      max_age_days:   超过此天数视为过期 → 闸门 fail
      audit_dir:      若 snapshot=None，从这里读最新 factor_ic_*.json（默认 config.AUDIT_DIR）

    Returns:
      GateResult — passed=True 时调用方可继续；False 时应降级或中止
    """
    watch_factors = watch_factors or list(DEFAULT_WATCH_FACTORS)
    thresholds = {
        "mean_ic_threshold": mean_ic_threshold,
        "ir_threshold": ir_threshold,
        "max_age_days": max_age_days,
        "watch_factors": list(watch_factors),
        "require_all_factors": require_all_factors,
    }

    # ─── 1. 加载快照 ───
    if snapshot is None:
        if audit_dir is None:
            from .. import config
            audit_dir = config.AUDIT_DIR
        matches = sorted(Path(audit_dir).glob("factor_ic_*.json"), reverse=True)
        if not matches:
            return GateResult(
                passed=False,
                reason=f"未找到 factor_ic 快照（{audit_dir}/factor_ic_*.json）— 先跑 audit_ic",
                snapshot_path=None,
                snapshot_age_days=None,
                thresholds=thresholds,
            )
        snapshot_path = matches[0]
        import json
        with open(snapshot_path, encoding="utf-8") as f:
            snapshot = json.load(f)

    # ─── 2. 新鲜度检查 ───
    age = _parse_snapshot_age(snapshot, snapshot_path)
    if age is not None and age > max_age_days:
        return GateResult(
            passed=False,
            reason=f"factor_ic 快照已过期：{age:.1f} 天 > {max_age_days} 天上限，请重跑 audit_ic",
            snapshot_path=str(snapshot_path) if snapshot_path else None,
            snapshot_age_days=age,
            thresholds=thresholds,
        )

    # ─── 3. 单因子分类 ───
    factors_data = snapshot.get("factors", {}) if isinstance(snapshot, dict) else {}
    verdicts: list[FactorVerdict] = []
    for fname in watch_factors:
        info = factors_data.get(fname, {})
        summary = info.get("summary", {}) if isinstance(info, dict) else {}
        n = int(summary.get("n_periods", 0))
        mean_ic = summary.get("mean_ic")
        ic_ir = summary.get("ic_ir")
        hit = summary.get("hit_rate")
        status, note = _classify_factor(mean_ic, ic_ir, n,
                                         mean_ic_threshold, ir_threshold)
        verdicts.append(FactorVerdict(
            factor=fname, mean_ic=mean_ic, ic_ir=ic_ir,
            hit_rate=hit, n_periods=n, status=status, note=note,
        ))

    healthy = [v.factor for v in verdicts if v.status == "healthy"]
    inverted = [v.factor for v in verdicts if v.status == "inverted"]
    blocking = [v for v in verdicts if v.status != "healthy"]

    # ─── 4. 闸门判定 ───
    if not verdicts:
        return GateResult(
            passed=False, reason="watch_factors 为空（无因子可判定）",
            snapshot_path=str(snapshot_path) if snapshot_path else None,
            snapshot_age_days=age, factor_verdicts=verdicts,
            thresholds=thresholds,
        )

    if require_all_factors and blocking:
        statuses = ", ".join(f"{v.factor}={v.status}" for v in blocking)
        return GateResult(
            passed=False,
            reason=(f"生产等权因子未全部 healthy（{statuses}）— "
                    "未验证/衰减因子必须先补 IC，或从 composite 权重中降为 0"),
            snapshot_path=str(snapshot_path) if snapshot_path else None,
            snapshot_age_days=age,
            factor_verdicts=verdicts,
            healthy_factors=healthy,
            inverted_factors=inverted,
            thresholds=thresholds,
        )

    if not healthy:
        statuses = ", ".join(f"{v.factor}={v.status}" for v in verdicts)
        return GateResult(
            passed=False,
            reason=(f"无 healthy 因子（{statuses}）— "
                    f"按 Grinold-Kahn 行业标准，IR<{ir_threshold} 属于不可投资 alpha"),
            snapshot_path=str(snapshot_path) if snapshot_path else None,
            snapshot_age_days=age,
            factor_verdicts=verdicts,
            healthy_factors=healthy,
            inverted_factors=inverted,
            thresholds=thresholds,
        )

    return GateResult(
        passed=True,
        reason=(f"通过：{len(healthy)}/{len(verdicts)} 因子 healthy "
                f"({', '.join(healthy)})"),
        snapshot_path=str(snapshot_path) if snapshot_path else None,
        snapshot_age_days=age,
        factor_verdicts=verdicts,
        healthy_factors=healthy,
        inverted_factors=inverted,
        thresholds=thresholds,
    )


# ─────────────── 报告渲染 ───────────────

def format_report(result: GateResult) -> str:
    """把 GateResult 渲染成可读报告（写日志或 stderr）。"""
    icon = "🟢 PASS" if result.passed else "🔴 FAIL"
    age_line = (f"  快照年龄：{result.snapshot_age_days:.1f} 天"
                if result.snapshot_age_days is not None else "  快照年龄：N/A")
    lines = [
        f"{'='*72}",
        f"  因子 IC CI 闸门 — {icon}",
        f"{'='*72}",
        f"  快照：{result.snapshot_path or 'N/A'}",
        age_line,
        (f"  阈值：mean_ic ≥ {result.thresholds.get('mean_ic_threshold')}, "
         f"|IR| ≥ {result.thresholds.get('ir_threshold')}, "
         f"max_age = {result.thresholds.get('max_age_days')}d, "
         f"require_all = {result.thresholds.get('require_all_factors')}"),
        "",
        f"  {'因子':<16}{'mean IC':>10}{'IC IR':>10}{'hit':>8}{'periods':>10}  状态",
        f"  {'-'*70}",
    ]
    for v in result.factor_verdicts:
        ic_s = f"{v.mean_ic:+.3f}" if v.mean_ic is not None else "  N/A"
        ir_s = f"{v.ic_ir:+.2f}" if v.ic_ir is not None else " N/A"
        hit_s = f"{v.hit_rate:.0%}" if v.hit_rate is not None else "N/A"
        status_icon = {
            "healthy": "🟢", "marginal": "🟡", "decayed": "🔴",
            "inverted": "⛔", "no_data": "❓",
        }.get(v.status, "?")
        lines.append(
            f"  {v.factor:<16}{ic_s:>10}{ir_s:>10}{hit_s:>8}{v.n_periods:>10}  "
            f"{status_icon} {v.status} — {v.note}"
        )
    lines.append("")
    lines.append(f"  判定：{result.reason}")
    if result.inverted_factors:
        lines.append(f"  ⚠️ 反向因子（可考虑反用）：{', '.join(result.inverted_factors)}")
    lines.append(f"{'='*72}")
    return "\n".join(lines)
