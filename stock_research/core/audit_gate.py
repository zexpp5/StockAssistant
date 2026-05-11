"""跨源 audit CONFLICT 比例闸门：在 daily_picks 写飞书前检查数据层健康度。

为什么需要这个闸门：
  daily_audit 已经在产出 HIGH/MEDIUM/LOW/CONFLICT 4 档可信度标签，但
  **没人在生产路径上消费这个信号**。如果某天 yfinance 价格全错（API 改动 /
  缓存毒化）或 akshare 接口断了，audit 会标一堆 CONFLICT，但 daily_picks_v5 /
  a_share_picks 照样写飞书 — 等于把脏数据当 alpha 推送。

闸门规则（保守、明确、可解释）：
  1. **新鲜度**：最新 audit snapshot 必须在 max_age_hours 内（默认 36h）
     —— 超过 36h（一日 + 半日缓冲）视为过期，宁可手动重跑 daily_audit
  2. **样本量**：audited 总数 ≥ min_sample（默认 30）
     —— 否则闸门统计不可靠，放行但 warn（避免 --code 单测时误伤）
  3. **CONFLICT 比例**：CONFLICT / audited < conflict_threshold（默认 10%）
     —— 超过阈值说明某个数据源系统性故障，停止下游推送

阈值依据：
  - CONFLICT severity HIGH 在 audit.py 是"价格偏差 > 5%"，多源真冲突很罕见
  - 89 标的样本里偶发 1-2 个 CONFLICT 是正常噪声（~2%），≥10% 必然是数据源出事
  - 36h 新鲜度对应 daily_refresh 每日 08:30 跑一次的节奏

API（与 factor_ic_gate.py 对齐）：
  evaluate_gate(snapshot=None, ...) → GateResult
  format_report(result) → str
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


DEFAULT_MAX_AGE_HOURS = 36.0
DEFAULT_MIN_SAMPLE = 30
DEFAULT_CONFLICT_THRESHOLD = 0.10


@dataclass
class GateResult:
    passed: bool
    reason: str
    snapshot_path: str | None
    snapshot_age_hours: float | None
    bucket: dict[str, int] = field(default_factory=dict)
    total: int = 0
    conflict_ratio: float = 0.0
    conflict_tickers: list[str] = field(default_factory=list)
    thresholds: dict[str, Any] = field(default_factory=dict)


def _parse_snapshot_age(snapshot: list | dict, snapshot_path: Path | None) -> float | None:
    """从快照里取 audited_at（取记录最大值）或 fallback 到文件 mtime，返回小时。"""
    latest_ts: datetime | None = None
    if isinstance(snapshot, list):
        for row in snapshot:
            if not isinstance(row, dict):
                continue
            ts = row.get("audited_at")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", ""))
            except (ValueError, TypeError):
                continue
            if latest_ts is None or dt > latest_ts:
                latest_ts = dt
    if latest_ts is not None:
        return (datetime.now() - latest_ts).total_seconds() / 3600.0
    if snapshot_path and snapshot_path.exists():
        mtime = datetime.fromtimestamp(snapshot_path.stat().st_mtime)
        return (datetime.now() - mtime).total_seconds() / 3600.0
    return None


def evaluate_gate(snapshot: list | None = None,
                  *,
                  snapshot_path: Path | None = None,
                  max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
                  min_sample: int = DEFAULT_MIN_SAMPLE,
                  conflict_threshold: float = DEFAULT_CONFLICT_THRESHOLD,
                  audit_dir: Path | None = None,
                  ) -> GateResult:
    """评估 audit CONFLICT 闸门。

    Args:
      snapshot:            直接传入 audit list（测试场景）。None 时从 audit_dir 读最新
      snapshot_path:       快照来源路径（仅报告用）
      max_age_hours:       超过此小时数视为过期 → fail
      min_sample:          样本量门槛，低于此放行 + warn（避免单股审计误伤）
      conflict_threshold:  CONFLICT 比例触发上限
      audit_dir:           snapshot=None 时从这里 glob 最新 audit_*.json
    """
    thresholds = {
        "max_age_hours": max_age_hours,
        "min_sample": min_sample,
        "conflict_threshold": conflict_threshold,
    }

    # 1. 加载快照
    if snapshot is None:
        if audit_dir is None:
            from .. import config
            audit_dir = config.AUDIT_DIR
        matches = sorted(Path(audit_dir).glob("audit_*.json"), reverse=True)
        if not matches:
            return GateResult(
                passed=False,
                reason=f"未找到 audit 快照（{audit_dir}/audit_*.json）— 先跑 daily_audit",
                snapshot_path=None,
                snapshot_age_hours=None,
                thresholds=thresholds,
            )
        snapshot_path = matches[0]
        with open(snapshot_path, encoding="utf-8") as f:
            snapshot = json.load(f)

    if not isinstance(snapshot, list):
        return GateResult(
            passed=False,
            reason=f"audit 快照格式异常：期望 list，实际 {type(snapshot).__name__}",
            snapshot_path=str(snapshot_path) if snapshot_path else None,
            snapshot_age_hours=None,
            thresholds=thresholds,
        )

    # 2. 新鲜度
    age = _parse_snapshot_age(snapshot, snapshot_path)
    if age is not None and age > max_age_hours:
        return GateResult(
            passed=False,
            reason=f"audit 快照已过期：{age:.1f}h > {max_age_hours}h 上限，请重跑 daily_audit",
            snapshot_path=str(snapshot_path) if snapshot_path else None,
            snapshot_age_hours=age,
            thresholds=thresholds,
        )

    # 3. 统计 bucket
    bucket = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "CONFLICT": 0}
    conflict_tickers: list[str] = []
    for row in snapshot:
        if not isinstance(row, dict):
            continue
        cred = row.get("credibility", "LOW")
        bucket[cred] = bucket.get(cred, 0) + 1
        if cred == "CONFLICT":
            conflict_tickers.append(str(row.get("ticker", "?")))

    total = sum(bucket.values())
    conflict_ratio = bucket.get("CONFLICT", 0) / total if total else 0.0

    # 4. 样本量守卫
    if total < min_sample:
        return GateResult(
            passed=True,
            reason=(f"放行 + warn：样本量 {total} < min_sample={min_sample}，"
                    f"闸门统计不可靠（CONFLICT={bucket['CONFLICT']}, ratio={conflict_ratio:.1%}）"),
            snapshot_path=str(snapshot_path) if snapshot_path else None,
            snapshot_age_hours=age,
            bucket=bucket,
            total=total,
            conflict_ratio=conflict_ratio,
            conflict_tickers=conflict_tickers,
            thresholds=thresholds,
        )

    # 5. CONFLICT 比例判定
    if conflict_ratio >= conflict_threshold:
        return GateResult(
            passed=False,
            reason=(f"CONFLICT 比例 {conflict_ratio:.1%} ≥ {conflict_threshold:.0%} 上限，"
                    f"数据源疑似系统性故障（{bucket['CONFLICT']}/{total} 冲突）"),
            snapshot_path=str(snapshot_path) if snapshot_path else None,
            snapshot_age_hours=age,
            bucket=bucket,
            total=total,
            conflict_ratio=conflict_ratio,
            conflict_tickers=conflict_tickers,
            thresholds=thresholds,
        )

    return GateResult(
        passed=True,
        reason=(f"通过：CONFLICT {bucket['CONFLICT']}/{total} = {conflict_ratio:.1%} "
                f"< {conflict_threshold:.0%}"),
        snapshot_path=str(snapshot_path) if snapshot_path else None,
        snapshot_age_hours=age,
        bucket=bucket,
        total=total,
        conflict_ratio=conflict_ratio,
        conflict_tickers=conflict_tickers,
        thresholds=thresholds,
    )


def format_report(result: GateResult) -> str:
    """把 GateResult 渲染成可读报告（写日志或 stderr）。"""
    icon = "🟢 PASS" if result.passed else "🔴 FAIL"
    age_line = (f"  快照年龄：{result.snapshot_age_hours:.1f}h"
                if result.snapshot_age_hours is not None else "  快照年龄：N/A")
    b = result.bucket
    lines = [
        f"{'='*72}",
        f"  跨源 audit CONFLICT 闸门 — {icon}",
        f"{'='*72}",
        f"  快照：{result.snapshot_path or 'N/A'}",
        age_line,
        (f"  阈值：CONFLICT < {result.thresholds.get('conflict_threshold'):.0%}, "
         f"min_sample = {result.thresholds.get('min_sample')}, "
         f"max_age = {result.thresholds.get('max_age_hours')}h"),
        "",
        (f"  样本：{result.total} 只  "
         f"🟢 HIGH {b.get('HIGH', 0)} · 🟡 MEDIUM {b.get('MEDIUM', 0)} · "
         f"🔴 LOW {b.get('LOW', 0)} · ⚠️ CONFLICT {b.get('CONFLICT', 0)} "
         f"({result.conflict_ratio:.1%})"),
    ]
    if result.conflict_tickers:
        preview = ", ".join(result.conflict_tickers[:10])
        suffix = f" 等 {len(result.conflict_tickers)} 只" if len(result.conflict_tickers) > 10 else ""
        lines.append(f"  冲突标的：{preview}{suffix}")
    lines.append("")
    lines.append(f"  判定：{result.reason}")
    lines.append(f"{'='*72}")
    return "\n".join(lines)
