"""每日早安简报（飞书推送：今日决策台的镜像）。

⚠️ 定位：本脚本不是产品主入口 —— 产品主入口是 dashboard 的"今日决策台"
（[scripts/pipeline/build_stock_dashboard_html.py](../../scripts/pipeline/build_stock_dashboard_html.py) 默认首页）。
本脚本只把"今日决策台"涉及的 7 个数据源拼成飞书 card + 一份 markdown 镜像，
方便用户在手机端 / 群里看到当天结论。任何"今日能不能动手"的判定逻辑应与
今日决策台一致，灯色取最严（防御 + 质量闸门 + 生产验收）。

数据源（全部已经在跑，本脚本只做拼装，不产生新数据）：
  - plan_a_v5_constrained.json | plan_a_v5.json     -> 当前建议组合（兼容文件名，内容为 v6 risk-aware）
  - trade_delta.json                                 -> 本周调仓 delta
  - risk_metrics.json                                -> 历史风险指标 + NAV 时序
  - data/snapshots/audit/realtime_defense_*.json     -> regime gate（最新一份）
  - data/event_calendar.json                         -> A 股事件
  - factor_scores_today.json                         -> 美股因子打分（数据新鲜度用）
  - data/a_share_picks.json                          -> A 股选股（盘后才有）

输出：
  - data/reports/morning_brief_YYYY-MM-DD.md         -> 持久存档
  - morning_brief.md（根目录）                       -> 最新一份，方便快速打开

可选飞书推送（任一配置就激活，优先级 lark-cli > webhook）：
  # 方案 A: hermes lark-cli（推荐 — 已经登录的话零配置）
  export FEISHU_BRIEF_USER_ID='ou_xxx'   # 收件人 open_id（自己发给自己最简单）
  export FEISHU_BRIEF_CHAT_ID='oc_xxx'   # 或：发到指定群（与 USER_ID 二选一）
  # 方案 B: 群机器人 webhook（无 lark-cli 时 fallback）
  export FEISHU_BRIEF_WEBHOOK='https://open.feishu.cn/open-apis/bot/v2/hook/XXX'
"""
from __future__ import annotations
import argparse
import json
import logging
import math
import os
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any

import requests

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
PRODUCTION_METRICS_START_DATE = os.environ.get("STOCK_ASSISTANT_METRICS_START_DATE", "2026-05-25")

from stock_research import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────
# 数据读取（每个数据源都允许缺失，缺什么 brief 显示什么）
# ────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"读取 {path} 失败: {e}")
        return None


def _a_share_enabled() -> bool:
    return bool(config.A_SHARE_PRODUCTION_ENABLED)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except Exception:
        return None


def _payload_ts(payload: dict | None) -> datetime | None:
    if not isinstance(payload, dict):
        return None
    for key in ("generated_at", "updated_at", "completed_at", "as_of", "date", "timestamp"):
        dt = _parse_ts(payload.get(key))
        if dt:
            return dt
    return None


def _pipeline_status_payload() -> dict:
    for name in ("pipeline_status_production.json", "pipeline_status.json"):
        payload = _load_json(REPO / "data" / "latest" / name)
        if not isinstance(payload, dict):
            continue
        mode = str(payload.get("mode") or "").lower()
        role = str(payload.get("status_role") or "").lower()
        if mode == "research" or role == "research":
            continue
        return payload
    return {}


def _fmt_plain_ts(dt: datetime | None) -> str:
    return dt.strftime("%m-%d %H:%M") if dt else "?"


def _value_to_iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")


def _latest_reference_ts(*payloads: dict | None) -> datetime | None:
    values = [_payload_ts(p) for p in payloads]
    values = [v for v in values if v is not None]
    return max(values) if values else None


def _plan_weight_source(plan: dict | None) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {
            "kind": "missing",
            "label": "仓位来源缺失",
            "detail": "未找到 plan_a_v5.json",
            "is_fallback": True,
        }
    risk_aware = plan.get("risk_aware") if isinstance(plan.get("risk_aware"), dict) else {}
    constraints = plan.get("constraints") if isinstance(plan.get("constraints"), dict) else {}
    method = str(plan.get("method") or "")
    engine = str(risk_aware.get("engine") or "")
    use_legacy_mc = bool(constraints.get("use_legacy_mc"))
    stages = risk_aware.get("stages") if isinstance(risk_aware.get("stages"), list) else []
    stage_errors = [
        f"{s.get('label')}: {s.get('error')}"
        for s in stages
        if isinstance(s, dict) and s.get("error")
    ]
    fallback = use_legacy_mc or "fallback" in engine.lower() or "legacy_monte_carlo" in engine.lower()
    if fallback:
        return {
            "kind": "fallback",
            "label": "仓位来源=legacy_monte_carlo fallback",
            "detail": "PyPortfolioOpt risk-aware 阶段未产出，权重来自 fallback 优化/约束后结果",
            "engine": engine or "legacy_monte_carlo",
            "stage_errors": stage_errors[:4],
            "is_fallback": True,
        }
    if "risk_aware_optimize" in method or engine == "risk_aware_optimize":
        return {
            "kind": "risk_aware",
            "label": "仓位来源=risk-aware optimizer",
            "detail": "权重来自风险感知优化器",
            "engine": engine or "risk_aware_optimize",
            "stage_errors": stage_errors[:4],
            "is_fallback": False,
        }
    return {
        "kind": "unknown",
        "label": "仓位来源=未知/未标注",
        "detail": method or engine or "plan 未提供 method/engine",
        "engine": engine,
        "stage_errors": stage_errors[:4],
        "is_fallback": True,
    }


def _plan_cash_breakdown(plan: dict | None) -> dict[str, Any]:
    """从 plan_a_v5.json 拆出 cash 比例与"为什么不是满仓"。

    Why: 实测 risk_aware 跑出 cash_pct=5% 目标，但 plan 实际 cash_pct_effective=36%；
         用户/基金经理读早报会问"系统是不是看空 → 才只买六成"。其实是约束器
         （单股 max_w + ADV cap + 相关性剪枝 + IC 闸门）把不能放进去的额度推回现金，
         不是 AI 主动看空。
    How to apply: section_picks 美股段标题后调用一次，把这行解释挂出来。
    """
    if not isinstance(plan, dict):
        return {"cash_pct": None, "gross_pct": None, "explain": ""}
    cons = plan.get("constraints") if isinstance(plan.get("constraints"), dict) else {}
    ra = plan.get("risk_aware") if isinstance(plan.get("risk_aware"), dict) else {}
    cash = cons.get("cash_pct_effective")
    gross = cons.get("gross_exposure_effective")
    if cash is None and isinstance(plan.get("plan_v6"), list):
        try:
            s = sum(float(x.get("capped_weight") or x.get("target_weight") or 0) for x in plan["plan_v6"])
            gross = s
            cash = max(0.0, 1.0 - s)
        except Exception:
            pass
    if cash is None:
        return {"cash_pct": None, "gross_pct": None, "explain": ""}
    pieces = []
    target_cash = cons.get("cash_pct")
    if isinstance(target_cash, (int, float)) and abs(float(target_cash) - float(cash)) > 0.02:
        pieces.append(f"目标现金 {float(target_cash)*100:.0f}%，实际 {float(cash)*100:.1f}% 被约束器抬高")
    max_w = cons.get("max_weight")
    if isinstance(max_w, (int, float)):
        pieces.append(f"单股上限 {float(max_w)*100:.0f}%")
    adv_pct = cons.get("max_adv_pct")
    if isinstance(adv_pct, (int, float)):
        pieces.append(f"日均成交≤{float(adv_pct)*100:.0f}%（流动性）")
    max_corr = cons.get("max_corr")
    pruned = ra.get("pruned_dropped") if isinstance(ra.get("pruned_dropped"), list) else []
    if pruned and isinstance(max_corr, (int, float)):
        pieces.append(f"相关性 ρ<{float(max_corr):.1f} 剪掉 {len(pruned)} 只")
    ic_gate = plan.get("factor_ic_gate") if isinstance(plan.get("factor_ic_gate"), dict) else {}
    if ic_gate.get("reason"):
        pieces.append(f"IC 闸门={ic_gate.get('reason')}")
    explain = " · ".join(pieces) if pieces else ""
    return {
        "cash_pct": float(cash),
        "gross_pct": float(gross) if gross is not None else (1.0 - float(cash)),
        "explain": explain,
        "is_high_cash": float(cash) > 0.30,
    }


def _format_f_score(value: Any) -> str:
    if isinstance(value, bool):
        return "缺失"
    if isinstance(value, (int, float)):
        return f"{int(value)}/9" if float(value).is_integer() else f"{float(value):.1f}/9"
    return "缺失"


def _fmt_metric(value: Any, suffix: str = "") -> str:
    try:
        x = float(value)
    except Exception:
        return "缺失"
    if not math.isfinite(x):
        return "缺失"
    if x == 0:
        txt = "0"
    elif abs(x) >= 100:
        txt = f"{x:.0f}"
    else:
        txt = f"{x:.2f}".rstrip("0").rstrip(".")
    return f"{txt}{suffix}"


def _entry_f_score(entry: dict) -> Any:
    value = entry.get("f_score")
    if value is None and entry.get("f_score_norm") is not None:
        try:
            value = float(entry.get("f_score_norm")) * 9.0
        except Exception:
            value = None
    return value


# V2 picks factor_scores_json 实际可能出现的子因子 → 中文短标签
# 与 build_v2_recommendations 内 scoring 字段保持同步；新增因子时在此追加，不删除以保兼容
_V2_FACTOR_LABELS = {
    "valuation": "估值",
    "momentum": "动量",
    "data_quality": "数据质量",
    "coverage": "覆盖度",
    "f_score": "F-Score",
    "piotroski": "Piotroski",
    "reversal": "反转",
    "lhb": "龙虎榜",
    "north_flow": "北向",
    "south_flow": "南向",
    "pead": "PEAD",
    "policy_boost": "政策",
    "quality": "质量",
    "size": "规模",
    "analyst": "分析师",
    "insider": "内部人",
}
# 不应作为"因子"展示的统计字段（picks JSON 里混在 factor_scores 中的元信息）
_V2_FACTOR_META = {"total", "rank", "score"}


def _parse_factor_scores(raw: Any) -> dict:
    """把 recommendation_picks.factor_scores_json 解析成 dict（容错空/坏 JSON）。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _label_v2_factor_set(selected: list[dict]) -> str:
    """从 V2 selected 抽出实际有值的因子字段，渲染成 '4 因子 lite（估值 + 动量 + ...）'。

    Why: morning_brief 之前写死 '6 因子 + 龙虎榜+北向' / '3 因子 + 港股年报'，
         但 V2 lite scoring 实际只算 valuation/momentum/data_quality/coverage 4 项；
         为防止 V2 后续接入新因子时文案再次过时，这里完全由实际 keys 驱动。
    How to apply: 任何走 V2 recommendation_picks 的市场段标题都该调它，不再写死因子数。
    """
    from collections import Counter
    counter: Counter = Counter()
    for s in selected:
        fs = s.get("factor_scores") or {}
        for k, v in fs.items():
            if k in _V2_FACTOR_META or v is None:
                continue
            counter[k] += 1
    keys = [k for k, _ in counter.most_common()]
    if not keys:
        return "因子明细缺失（factor_scores 为空，请查 V2 pipeline）"
    labels = [_V2_FACTOR_LABELS.get(k, k) for k in keys]
    return f"{len(keys)} 因子 lite（{' + '.join(labels)}）"


def _load_us_plan() -> dict | None:
    """读取美股生产 plan，避免旧 constrained 文件盖过最新 risk-aware plan。"""
    base = _load_json(REPO / "data" / "latest" / "plan_a_v5.json")
    constrained = _load_json(REPO / "data" / "latest" / "plan_a_v5_constrained.json")
    base = base if isinstance(base, dict) else None
    constrained = constrained if isinstance(constrained, dict) else None
    if not base:
        return constrained
    if not constrained:
        return base

    base_ts = _parse_ts(base.get("generated_at"))
    constrained_ts = (
        _parse_ts(constrained.get("a_share_constraints_at"))
        or _parse_ts(constrained.get("generated_at"))
    )
    same_plan = constrained.get("generated_at") == base.get("generated_at")
    if same_plan or (base_ts and constrained_ts and constrained_ts >= base_ts):
        return constrained
    logger.warning("忽略旧 plan_a_v5_constrained.json，使用最新 plan_a_v5.json")
    return base


def _load_hk_picks() -> dict | None:
    """Load HK picks: V2 first（recommendation_picks.market='HK'）, fall back to JSON."""
    json_payload = _load_json(REPO / "data" / "latest" / "hk_picks.json")
    try:
        lib_path = str(REPO / "scripts" / "lib")
        if lib_path not in sys.path:
            sys.path.insert(0, lib_path)
        from stock_db import get_db

        conn = get_db(read_only=True)
        v2_run = conn.execute(
            """
            SELECT run_id, run_date, generated_at FROM recommendation_runs
            WHERE universe_scope = 'system_tech_universe' AND status = 'generated'
            ORDER BY generated_at DESC LIMIT 1
            """
        ).fetchone()
        if v2_run:
            run_id, run_date, generated_at = v2_run
            v2_rows = conn.execute(
                """
                SELECT p.symbol,
                       COALESCE(NULLIF(u.name, p.symbol), p.name) AS name,
                       p.rating, p.total_score, p.factor_scores_json
                FROM recommendation_picks p
                LEFT JOIN system_universe u
                  ON p.market = u.market AND p.symbol = u.symbol
                WHERE p.run_id = ? AND p.market = 'HK' AND p.signal = 'buy'
                ORDER BY p.total_score DESC NULLS LAST, p.symbol
                """,
                [run_id],
            ).fetchall()
            if v2_rows:
                conn.close()
                selected = [{
                    "code": symbol, "ticker": symbol, "name": name or symbol,
                    "market": "港股", "rating": rating,
                    "composite": (float(total_score) / 100) if total_score is not None else 0,
                    "industry": "科技", "theme": "科技/AI",
                    "factor_scores": _parse_factor_scores(factor_scores_json),
                } for symbol, name, rating, total_score, factor_scores_json in v2_rows]
                return {
                    "generated_at": _value_to_iso(generated_at),
                    "run_date": str(run_date)[:10] if run_date else None,
                    "source": "duckdb:recommendation_picks.system_tech_universe[HK]",
                    "n_recommended": len(selected),
                    "selected": selected,
                    "all_entries": selected,
                }
        conn.close()
    except Exception as e:
        logger.warning(f"读取 V2 港股 picks 失败，回退 JSON: {e}")
    return json_payload if isinstance(json_payload, dict) else None


def _load_a_share_picks() -> dict | None:
    """Load A-share picks JSON, with DuckDB as fresher source of truth.

    2026-05-21 V1 cutover：优先 V2 recommendation_picks（最新 system_tech_universe
    run 的 CN 筛选），V1 picks.v6_cn 兜底已删；空就兜 a_share_picks.json。
    """
    json_payload = _load_json(REPO / "data" / "a_share_picks.json")
    try:
        lib_path = str(REPO / "scripts" / "lib")
        if lib_path not in sys.path:
            sys.path.insert(0, lib_path)
        from stock_db import get_db

        conn = get_db(read_only=True)
        # ── V2 优先 ──
        v2_run = conn.execute(
            """
            SELECT run_id, run_date, generated_at FROM recommendation_runs
            WHERE universe_scope = 'system_tech_universe' AND status = 'generated'
            ORDER BY generated_at DESC LIMIT 1
            """
        ).fetchone()
        if v2_run:
            run_id, run_date, generated_at = v2_run
            # JOIN system_universe 拿最新 name（picks 表里的 name 可能是 stale=symbol）
            v2_rows = conn.execute(
                """
                SELECT p.symbol,
                       COALESCE(NULLIF(u.name, p.symbol), p.name) AS name,
                       p.market, p.rating, p.total_score, p.factor_scores_json
                FROM recommendation_picks p
                LEFT JOIN system_universe u
                  ON p.market = u.market AND p.symbol = u.symbol
                WHERE p.run_id = ? AND p.market = 'CN' AND p.signal = 'buy'
                ORDER BY p.total_score DESC NULLS LAST, p.symbol
                """,
                [run_id],
            ).fetchall()
            if v2_rows:
                conn.close()
                db_date = str(run_date)[:10]
                json_date = str((json_payload or {}).get("generated_at") or "")[:10]
                if isinstance(json_payload, dict) and json_date > db_date:
                    return json_payload  # 极少情况 JSON 比 V2 还新
                selected = [{
                    "code": symbol, "ticker": symbol, "name": name,
                    "market": market or "A股", "rating": rating,
                    "composite": (float(total_score) / 100) if total_score is not None else 0,
                    "industry": "科技", "theme": "科技/AI",
                    "factor_scores": _parse_factor_scores(factor_scores_json),
                } for symbol, name, market, rating, total_score, factor_scores_json in v2_rows]
                return {
                    "generated_at": _value_to_iso(generated_at),
                    "run_date": db_date,
                    "source": "duckdb:recommendation_picks.system_tech_universe",
                    "n_recommended": len(selected),
                    "selected": selected,
                    "all_entries": selected,
                }

        # 2026-05-21 V1 cutover：删 V1 picks v6_cn 兜底
        conn.close()
        return json_payload if isinstance(json_payload, dict) else None
    except Exception as e:
        logger.warning(f"读取 V2 A 股 picks 失败，回退 JSON: {e}")
        return json_payload if isinstance(json_payload, dict) else None


def _quality_gate_payload() -> dict:
    gate = _load_json(REPO / "data" / "latest" / "recommendation_quality_gate.json")
    return gate if isinstance(gate, dict) else {}


def _quality_gate_status() -> str:
    return str((_quality_gate_payload() or {}).get("status") or "UNKNOWN")


def _quality_gate_blocks_trade() -> bool:
    return _quality_gate_status() == "FAIL"


def _acceptance_payload() -> dict:
    accept = _load_json(REPO / "data" / "latest" / "production_acceptance_check.json")
    return accept if isinstance(accept, dict) else {}


def _acceptance_status() -> str:
    return str((_acceptance_payload() or {}).get("status") or "UNKNOWN")


# severity 档位排序：NONE < LOW < HIGH < CRITICAL，与 defense_signals.py:201 对齐
_SEVERITY_RANK = {"NONE": 0, "LOW": 1, "HIGH": 2, "CRITICAL": 3}
_RANK_TO_SEVERITY = {0: "NONE", 1: "LOW", 2: "HIGH", 3: "CRITICAL"}


def _combined_severity(defense: dict | None,
                       qgate: dict | None,
                       acceptance: dict | None) -> tuple[str, str, str, list[str]]:
    """三道闸门取最严：defense / 质量闸门 / 生产验收 → 统一灯色 + 升档理由。

    升档规则：
      - 起点 = defense.severity（NONE/LOW/HIGH/CRITICAL）
      - 质量闸门或生产验收 == WARN → 至少升到 LOW
      - 质量闸门或生产验收 == FAIL → 至少升到 HIGH
      - defense CRITICAL 始终最高
    返回 (severity, icon, header_template, reasons[])。
    """
    defense_sev = (defense or {}).get("severity") or "NONE"
    rank = _SEVERITY_RANK.get(defense_sev, 0)
    reasons: list[str] = []

    def _collect(payload: dict | None, label: str) -> None:
        for it in (payload or {}).get("issues", []) or []:
            if it.get("level") in ("WARN", "FAIL"):
                reasons.append(f"[{label} {it.get('level')}] {it.get('message','')}")

    qgate_status = str((qgate or {}).get("status") or "").upper()
    if qgate_status == "WARN":
        rank = max(rank, 1)
        _collect(qgate, "质量闸门")
    elif qgate_status == "FAIL":
        rank = max(rank, 2)
        _collect(qgate, "质量闸门")

    accept_status = str((acceptance or {}).get("status") or "").upper()
    if accept_status == "WARN":
        rank = max(rank, 1)
        _collect(acceptance, "生产验收")
    elif accept_status == "FAIL":
        rank = max(rank, 2)
        _collect(acceptance, "生产验收")

    severity = _RANK_TO_SEVERITY.get(rank, defense_sev)
    icon = {"NONE": "🟢", "LOW": "🟡", "HIGH": "🟠", "CRITICAL": "🔴"}.get(severity, "⚪")
    header_template = {"NONE": "blue", "LOW": "yellow", "HIGH": "orange", "CRITICAL": "red"}.get(severity, "grey")
    return severity, icon, header_template, reasons[:6]


def _quality_gate_lines(max_items: int = 4) -> list[str]:
    gate = _quality_gate_payload()
    if not gate or gate.get("status") == "PASS":
        return []
    status = gate.get("status", "UNKNOWN")
    summary = gate.get("summary") or {}
    icon = "🔴" if status == "FAIL" else "🟡"
    lines = [
        f"{icon} **数据质量闸门 = {status}** "
        f"(fail={summary.get('fail', 0)}, warn={summary.get('warn', 0)})"
    ]
    for item in (gate.get("issues") or [])[:max_items]:
        level = item.get("level", "?")
        if level == "INFO":
            continue
        lines.append(f"• **{level}** {item.get('message', '')}")
    if status == "FAIL":
        lines.append("• 建议：今天只读不交易，先修复 FAIL 项。")
    return lines


def section_quality_gate() -> str:
    lines = _quality_gate_lines()
    if not lines:
        return ""
    return "#### 🧯 数据质量闸门\n" + "\n".join(lines) + "\n"


def section_source_health() -> str:
    payload = _load_json(REPO / "data" / "latest" / "source_health.json")
    if not isinstance(payload, dict):
        return ""
    rows = []
    for name, info in (payload.get("sources") or {}).items():
        if (info or {}).get("status") in (None, "", "ok", "healthy"):
            continue
        affected = "、".join((info.get("affected_fields") or [])[:4]) or "部分字段"
        unaffected = "、".join((info.get("unaffected_fields") or [])[:5]) or "主流程"
        rows.append(
            f"• **{name} 降级**：{info.get('reason') or 'unknown'}；"
            f"受影响：{affected}；仍可用：{unaffected}"
        )
    if not rows:
        return ""
    rows.insert(0, "🟡 数据源有降级，但不等于整套建议失败；是否能交易以质量闸门为准。")
    return "#### 🧯 数据源健康\n" + "\n".join(rows) + "\n"


def _evidence_lines() -> list[str]:
    ev = _load_json(REPO / "data" / "latest" / "recommendation_evidence.json")
    if not isinstance(ev, dict):
        return []
    plan = _load_us_plan()
    gate = _quality_gate_payload()
    pipeline = _pipeline_status_payload()
    ev_ts = _payload_ts(ev)
    ref_ts = _latest_reference_ts(plan, gate, pipeline)
    is_stale = bool(ev_ts and ref_ts and ev_ts < ref_ts - timedelta(minutes=5))
    pipeline_status = str(pipeline.get("status") or "").upper()
    pipeline_open = pipeline_status not in {"", "OK", "PASS", "SUCCESS"}
    grade = ev.get("evidence_grade", "UNKNOWN")
    cov = ev.get("review_coverage") or {}
    total_r = cov.get("total_reviewed", 0)
    total_m = cov.get("total_mature", 0)
    coverage = cov.get("coverage")
    cov_txt = "—" if coverage is None else f"{coverage * 100:.1f}%"
    if is_stale or pipeline_open:
        lines = [
            f"⚠️ **有效性证据 = {grade}（历史文件，不能单独证明本轮推荐）** "
            f"· 成熟回顾 {total_r}/{total_m} · 覆盖 {cov_txt}"
        ]
        details = []
        if is_stale:
            details.append(f"证据 {_fmt_plain_ts(ev_ts)} 早于本轮产物 {_fmt_plain_ts(ref_ts)}")
        if pipeline_open:
            details.append(f"pipeline_status={pipeline_status or 'UNKNOWN'}")
        if details:
            lines.append("• " + "；".join(details) + "。今日结论以质量闸门/生产验收为准。")
    else:
        lines = [
            f"📐 **有效性证据 = {grade}** · 成熟回顾 {total_r}/{total_m} · 覆盖 {cov_txt} "
            f"· 证据时间 {_fmt_plain_ts(ev_ts)}"
        ]
    rows = [r for r in (ev.get("review_metrics_by_source") or []) if r.get("signal") == "buy"]
    if rows:
        bits = []
        for r in rows[:3]:
            alpha = r.get("avg_alpha_pct")
            alpha_txt = "—" if alpha is None else f"{alpha:+.2f}%"
            bits.append(f"{r.get('model_source')} alpha {alpha_txt} n={r.get('n', 0)}")
        lines.append("• " + " ｜ ".join(bits))
    if grade == "INSUFFICIENT_EVIDENCE":
        lines.append("• 证据仍在积累：清空重跑后至少等 1/5/20 日窗口成熟，再判断模型是否真有效。")
    return lines


def section_evidence() -> str:
    lines = _evidence_lines()
    if not lines:
        return ""
    return "#### 📐 推荐有效性证据\n" + "\n".join(lines) + "\n"


def _latest_defense_snapshot() -> dict | None:
    """读最新的 realtime_defense_*.json（按文件名时间戳排序）。"""
    snap_dir = REPO / "data" / "snapshots" / "audit"
    if not snap_dir.exists():
        return None
    files = sorted(snap_dir.glob("realtime_defense_*.json"))
    if not files:
        return None
    return _load_json(files[-1])


def _is_a_share(ticker: str) -> bool:
    """A 股 / 港股识别（与 apply_a_share_constraints.py 对齐）。"""
    t = ticker.upper()
    return t.endswith((".SS", ".SZ", ".BJ", ".HK"))


# ────────────────────────────────────────────────────────
# 趋势可视化 — emoji 5 档 + 百分比（取代 Unicode block sparkline，
# 原因：飞书 markdown 字体只渲染 ▁ 和 █ 两档，中间字符被画成同高度实心块）
# ────────────────────────────────────────────────────────

def _load_history() -> dict:
    """读 history_data.json 的 tickers map，缺则返回空 dict。"""
    d = _load_json(REPO / "data" / "latest" / "history_data.json")
    if not isinstance(d, dict):
        return {}
    return d.get("tickers") or {}


def _fmt_ts(iso: str | None) -> str:
    """格式化各 picks JSON 的 generated_at → "🕐 算于 MM-DD HH:MM (Y 小时前)"。

    阈值与 dashboard 端的 _fmtTs 对齐：≥24 小时 标 ⚠️ 已过期。
    用途：让用户一眼判断这批推荐是不是今天最新算的，防止误用陈旧数据。
    """
    if not iso:
        return "🕐 算于 ?"
    try:
        if "T" in iso:
            dt = datetime.fromisoformat(iso.split(".")[0])
        else:
            dt = datetime.strptime(iso[:10], "%Y-%m-%d")
    except Exception:
        return f"🕐 算于 {iso[:16]}"
    now = datetime.now()
    delta_s = (now - dt).total_seconds()
    short = dt.strftime("%m-%d %H:%M")
    hrs = int(delta_s / 3600)
    days = int(delta_s / 86400)
    if days >= 2:
        age = f"（{days} 天前 ⚠️ 已过期）"
    elif hrs >= 24:
        age = f"（{hrs} 小时前 ⚠️ 已过期）"
    elif hrs >= 1:
        age = f"（{hrs} 小时前）"
    else:
        age = "（刚刚）"
    return f"🕐 算于 {short} {age}"


def _trend_emoji(pct: float | None) -> str:
    """根据涨跌幅% 选 7 档趋势 emoji（区分度比 5 档更高）。"""
    if pct is None:
        return "❓"
    if pct >= 50:
        return "🚀"  # 飙涨 ≥50%
    if pct >= 15:
        return "📈"  # 强涨 15-50%
    if pct >= 3:
        return "↗️"  # 小涨 3-15%
    if pct > -3:
        return "➡️"  # 横盘 -3 ~ +3%
    if pct > -15:
        return "↘️"  # 小跌 -15 ~ -3%
    if pct > -50:
        return "📉"  # 强跌 -50 ~ -15%
    return "💀"  # 暴跌 ≤-50%


# 卡片自带的趋势图例（新人能看懂——配合 _trend_emoji 7 档同步使用）
TREND_LEGEND_SHORT = (
    "📖 **60d 趋势图例**："
    "🚀 ≥+50% 飙涨 ｜ 📈 +15~50% 强涨 ｜ ↗️ +3~15% 小涨 ｜ "
    "➡️ ±3% 横盘 ｜ ↘️ -3~-15% 小跌 ｜ 📉 -15~-50% 强跌 ｜ 💀 ≤-50% 暴跌"
)


def _ticker_sparkline(history: dict, ticker: str, window: int = 60) -> tuple[str, float | None]:
    """返回 (趋势 emoji, window 天涨跌%)。

    注：函数名保留 "sparkline" 避免大改 caller 签名，但内部已改用 emoji。
    飞书 markdown 不能稳定渲染 ▁▂▃▄▅▆▇█，emoji 跨平台兼容性更好。
    """
    if not history or ticker not in history:
        return "❓", None
    closes = history[ticker].get("close") or []
    if len(closes) < 2:
        return "❓", None
    recent = []
    for value in closes[-window:]:
        try:
            if value is not None:
                recent.append(float(value))
        except Exception:
            continue
    if len(recent) < 2:
        return "❓", None
    pct = ((recent[-1] - recent[0]) / recent[0] * 100) if recent[0] else None
    return _trend_emoji(pct), pct


def _nav_sparkline(risk_metrics: dict | None, length: int = 15) -> dict | None:
    """组合 NAV 时序 → 趋势 emoji + 关键指标。返回 dict 或 None。

    注：保留 spark 字段名（caller 已用），但内容改为 emoji 趋势。
    """
    if not risk_metrics:
        return None
    daily = risk_metrics.get("daily_values") or []
    if len(daily) < 5:
        return None
    points: list[tuple[Any, float]] = []
    for d in daily:
        try:
            value = float(d.get("value", 0))
        except Exception:
            continue
        if math.isfinite(value) and value > 0:
            points.append((d.get("date", "?"), value))
    values = [v for _, v in points]
    if not values or values[0] <= 0:
        return None
    total_pct = (values[-1] - values[0]) / values[0] * 100
    # 算阶段分析：最近 30d 趋势
    recent_30 = values[-30:] if len(values) >= 30 else values
    pct_30 = ((recent_30[-1] - recent_30[0]) / recent_30[0] * 100) if recent_30[0] else 0
    if not math.isfinite(total_pct) or not math.isfinite(pct_30):
        return None
    return {
        "spark": _trend_emoji(total_pct),
        "spark_30d": _trend_emoji(pct_30),
        "pct_30d": pct_30,
        "total_pct": total_pct,
        "start_date": points[0][0],
        "end_date": points[-1][0],
        "start_value": values[0],
        "end_value": values[-1],
        "n_days": len(values),
        "maxdd_pct": risk_metrics.get("max_drawdown_pct"),
    }


# ────────────────────────────────────────────────────────
# Section 0: 今天 / 3 天内会发生什么（持仓 earnings + 高相关政策）
# ────────────────────────────────────────────────────────

def section_calendar(plan: dict | None) -> str:
    """读 event_calendar.json + policy_events.json，挑出今天到 +3d 关键事件。

    - 持仓 earnings：仅列持仓股（A 股）next 3d 内的财报日
    - 政策事件：relevance_score ≥ 4 的近 3d 政策
    - 都空则返回 ""（整 section 不显示，避免占版面）
    """
    events_data = _load_json(REPO / "data" / "event_calendar.json")
    policy_data = _load_json(REPO / "data" / "policy_events.json")
    today = date.today()
    horizon = today + timedelta(days=3)

    held_codes: set[str] = set()
    if plan:
        for e in (plan.get("plan_v5") or []):
            t = e.get("ticker", "")
            if _is_a_share(t):
                held_codes.add(t.split(".")[0])

    earnings: list[tuple[date, dict]] = []
    for ev in (events_data or {}).get("events", []) or []:
        try:
            ed = datetime.strptime(ev.get("event_date", ""), "%Y-%m-%d").date()
        except Exception:
            continue
        if today <= ed <= horizon and ev.get("code", "") in held_codes:
            earnings.append((ed, ev))

    policies: list[tuple[date, dict]] = []
    for ev in (policy_data or {}).get("events", []) or []:
        if (ev.get("relevance_score") or 0) < 4:
            continue
        try:
            ed = datetime.strptime(ev.get("date", ""), "%Y-%m-%d").date()
        except Exception:
            continue
        if today <= ed <= horizon:
            policies.append((ed, ev))

    if not earnings and not policies:
        return ""

    def _day_label(d: date) -> str:
        if d == today:
            return "🔥今天"
        if d == today + timedelta(days=1):
            return "明天"
        return d.strftime("%m-%d")

    lines = ["#### 0. 今天 / 3 天内会发生什么"]
    if earnings:
        lines.append(f"📅 **持仓事件**（{len(earnings)} 个 earnings）")
        for ed, ev in sorted(earnings)[:5]:
            lines.append(f"• {_day_label(ed)} · {ev.get('code')} · {ev.get('description','')[:60]}")
    if policies:
        if earnings:
            lines.append("")
        lines.append(f"📰 **高相关政策**（{len(policies)} 条 · relevance≥4）")
        for ed, ev in sorted(policies, key=lambda x: (-int(x[1].get('relevance_score') or 0), x[0]))[:3]:
            themes = "/".join(ev.get('matched_themes') or [])
            theme_str = f" · {themes}" if themes else ""
            lines.append(f"• {_day_label(ed)}{theme_str} · {ev.get('title','')[:60]}")
    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────
# Section 1: regime gate — 今天能不能动手
# ────────────────────────────────────────────────────────

# ────────────────────────────────────────────────────────
# 真实持仓 7 档判断（pure function · 早报 + dashboard endpoint 共用）
# ────────────────────────────────────────────────────────
#
# 2026-05-22: 此前持仓止损告警只去飞书早报,dashboard 看不到。
# 拆出 compute_holdings_verdict() 作单一计算源:
#   - 早报: section_holdings_stoploss 调本函数取 stop/watch/avwap 部分拼 markdown
#   - dashboard: GET /api/real-holdings/daily-verdict 调本函数返回完整 dict
# 前端只渲染不重算 —— feedback_single_source_no_double_engine 原则。

# 7 档标签 (label_kind, emoji, 中文文案, 优先级 1=最严重)
_VERDICT_LABELS = {
    "stop_breach":  ("🔴", "破止损线",  1),
    "stop_watch":   ("🟡", "接近止损",  2),
    "model_weak":   ("⚠️", "模型转弱",  3),
    "near_event":   ("📅", "临近事件",  4),
    "weight_off":   ("🎯", "偏离目标",  5),
    "normal":       ("🟢", "持有观察",  6),
    "ai_uncovered": ("⚪", "未评分",    7),
}

# 持仓覆盖分类 (coverage_class, emoji, 中文文案, group 顺序)
# Part 3 · 2026-05-22: 不同类型持仓字段集不同,模型边界诚实表达
#   ai_portfolio  — AI 组合方案给目标仓位,完整评级 + 调仓建议
#   picks_only    — 普通股,系统能评但不进 AI 组合,只看评级 + 风险
#   tracking_only — ETF/黄金/债券,股票因子模型不适用,只看市值/盈亏/止损
#   needs_fix     — 行情拉不到 / ticker 非法,需用户修正
_COVERAGE_CLASSES = {
    "ai_portfolio":  ("💼", "AI 组合覆盖",       1),
    "picks_only":    ("📊", "普通股票评分",       2),
    "tracking_only": ("🛡️", "仅风控跟踪",        3),
    "needs_fix":     ("⚠️", "待修正",             4),
}

# 系统处理方式 (treatment_class)。
# 与 coverage_class 不再同值:
#   coverage_class 说明"数据覆盖到哪一层"
#   treatment_class 说明"本系统实际采用哪种分析方法"
_TREATMENT_CLASSES = {
    "portfolio_model": ("💼", "AI组合模型",   "使用 AI 组合目标 + 股票评分 + 风控判断"),
    "stock_score":     ("📊", "股票评分",     "使用普通股票评分 + 风控判断；不进入 AI 组合目标"),
    "risk_only":       ("🛡️", "仅风控跟踪",  "黄金/ETF/债券等不套股票因子模型，只看市值、盈亏和风控"),
    "data_blocked":    ("⚠️", "数据阻塞",    "代码或行情/评分缺失，先补数据再判断"),
}

# 市面常用资产类别 (asset_class)。
# 这层回答"它是什么资产";下面的 coverage_class 回答"本系统怎么处理它"。
_ASSET_CLASSES = {
    "equity":       ("股票",       "公司股权/普通股",          1),
    "fund_etf":     ("ETF/基金",   "指数/行业/跨市场基金",      2),
    "commodity":    ("商品/黄金",  "黄金/白银/商品类工具",      3),
    "fixed_income": ("债券/固收",  "债券/固收类工具",          4),
    "crypto":       ("加密资产",   "加密资产 ETF/信托",         5),
    "cash":         ("现金",       "账户现金",                 6),
    "unknown":      ("待修正",     "代码或行情需要修正",        9),
}

_GOLD_ETFS = {"IAUM", "GLD", "GLDM", "IAU", "SLV", "SGOL", "BAR", "PHYS"}
_INDEX_ETFS = {"SPY", "QQQ", "VOO", "VTI", "IWM", "DIA", "VTV", "VUG"}
_BOND_ETFS = {"TLT", "BND", "AGG", "GOVT", "LQD", "HYG", "TIP", "MUB", "IEF", "SHY"}
_INTERNATIONAL_ETFS = {"VEA", "VWO", "EFA", "EEM"}
_COMMODITY_ETFS = {"USO", "UNG", "DBA", "DBC"}
_CRYPTO_ETFS = {"GBTC", "IBIT", "FBTC"}

# ETF / 大盘指数 / 商品/债券基金硬编码白名单。
# 这类工具不适用 Piotroski Z-Score / Beneish M 等股票因子模型 —
# 硬给评级是噪音,只展示市值/盈亏/止损/风控线即可。
# 长期方案: fetch_stock_prices.py 拉 yfinance quote_type=ETF 自动归类,
# 当前先用 keyword 覆盖账户里常见的 18 只。
_TRACKING_ONLY_TICKERS = (
    _GOLD_ETFS | _INDEX_ETFS | _BOND_ETFS | _INTERNATIONAL_ETFS | _COMMODITY_ETFS | _CRYPTO_ETFS
)


def _classify_asset_class(code: str, coverage_class: str | None = None) -> tuple[str, str, str]:
    """按市面常见口径给持仓归资产类别。

    返回 (asset_class, asset_label, asset_hint)。这不等于系统覆盖方式:
      - 9992.HK / MCD / BRK-B: equity,但未必进 AI 组合
      - IAUM: commodity,系统只做风控跟踪
      - SPY/QQQ: fund_etf,系统只做风控跟踪
    """
    c = (code or "").upper()
    if coverage_class == "needs_fix":
        key = "unknown"
    elif c in _GOLD_ETFS or c in _COMMODITY_ETFS:
        key = "commodity"
    elif c in _BOND_ETFS:
        key = "fixed_income"
    elif c in _INDEX_ETFS or c in _INTERNATIONAL_ETFS:
        key = "fund_etf"
    elif c in _CRYPTO_ETFS:
        key = "crypto"
    else:
        key = "equity"
    label, hint, _order = _ASSET_CLASSES[key]
    return key, label, hint


def _is_needs_fix(code: str, current: float | None) -> tuple[bool, str]:
    """检测 ticker 是否结构性错误(永远拉不到)。返回 (是否, 原因)。

    只判"真坏":
      1. ticker 含非 ASCII 字符(如中文公司名),yfinance/akshare 永远拉不到
      2. 美股子类股 .B / .A(雅虎用 -B / -A 连字符),且 current 拉不到 → 大概率结构错误

    不判"暂时没行情":
      - current=None 单独不算 needs_fix(可能只是 history 数据 staleness,
        会被 daily_refresh 第二天补上)。
    """
    if not code:
        return False, ""
    if not code.isascii():
        return True, "ticker 含非 ASCII 字符(中文名等),需改成标准代码"
    if current is None and "." in code and not any(
        code.upper().endswith(suffix) for suffix in
        (".HK", ".SS", ".SZ", ".BJ", ".SH", ".T", ".KS", ".AX", ".L", ".IL")
    ):
        return True, f"行情拉不到 — {code} 可能需改成 {code.replace('.', '-')}(雅虎子类股用连字符)"
    return False, ""


def _classify_coverage(
    code: str,
    current: float | None,
    in_picks: bool,
    in_target: bool,
) -> str:
    """对单只持仓分类覆盖度(4 档)。

    优先级: needs_fix(结构性错误) > tracking_only(ETF) > ai_portfolio(在 AI 组合) > picks_only(普通股,默认)
    """
    is_bad, _ = _is_needs_fix(code, current)
    if is_bad:
        return "needs_fix"
    if (code or "").upper() in _TRACKING_ONLY_TICKERS:
        return "tracking_only"
    if in_target:
        return "ai_portfolio"
    return "picks_only"


def _treatment_for_coverage(coverage_class: str) -> tuple[str, str, str, str]:
    if coverage_class == "ai_portfolio":
        key = "portfolio_model"
    elif coverage_class == "tracking_only":
        key = "risk_only"
    elif coverage_class == "needs_fix":
        key = "data_blocked"
    else:
        key = "stock_score"
    emoji, text, hint = _TREATMENT_CLASSES[key]
    return key, emoji, text, hint


def compute_holdings_verdict(
    holdings: list[dict],
    history: dict | None = None,
    *,
    picks: list[dict] | None = None,
    universe: list[dict] | None = None,
    events_data: dict | None = None,
    target_weights: dict | None = None,
    total_capital: float = 500000,
    stop_pct: float = 0.15,
    weight_off_threshold_pt: float = 3.0,
    today_date: date | None = None,
) -> dict:
    """对真实持仓做判断:资产类别 + 系统处理方式 + 7 档风险/动作标签。

    纯函数,不做 IO — 调用方负责拉:
      - holdings: stock_db.fetch_all_real_holdings()
      - history: 各 ticker {close, high, low, volume, ts} 字典
      - picks: stock_db.fetch_latest_recommendation_picks()(决定 model_weak/normal)
      - universe: stock_db.fetch_universe_for_ai_recommendations()(决定 ai_uncovered)
      - events_data: 解析 data/event_calendar.json(决定 near_event)
      - target_weights: {ticker: target_weight 0-1} 来自 plan_v6(决定 weight_off)
      - total_capital: 计算 current_weight 的分母

    返回:
      {
        "as_of": "YYYY-MM-DD",
        "holdings": [{code, asset_class, asset_label, coverage_class,
                      label_kind, label_emoji, label_text, current, dd_pct,
                      stop_pct, reasons[{kind, text}], ...}, ...],
        "summary": {stoploss_breached, stoploss_watched, model_weakened,
                    near_event, weight_off, ai_uncovered(未评分), normal,
                    coverage_*, asset_*},
      }
    """
    from stock_research.core.portfolio_constraints import (
        check_stop_loss_breach, volatility_adaptive_stop_pct,
    )
    from stock_research.core.technical_indicators import anchored_vwap

    today = today_date or date.today()
    horizon = today + timedelta(days=3)

    # ── 索引: picks by code, universe by code, events by code(A 股 6 位代码) ──
    picks_by_code: dict[str, dict] = {}
    for p in (picks or []):
        c = p.get("code") or p.get("symbol")
        if c:
            picks_by_code[c] = p
    universe_codes: set[str] = set()
    for u in (universe or []):
        c = u.get("code") or u.get("symbol")
        if c:
            universe_codes.add(c)
    events_by_code: dict[str, list[dict]] = {}
    for ev in (events_data or {}).get("events", []) or []:
        try:
            ed = datetime.strptime(ev.get("event_date", ""), "%Y-%m-%d").date()
        except Exception:
            continue
        if not (today <= ed <= horizon):
            continue
        code6 = ev.get("code", "")
        events_by_code.setdefault(code6, []).append({"date": ed, "desc": ev.get("description", "")})

    history = history or {}
    out_holdings: list[dict] = []
    summary = {"stoploss_breached": 0, "stoploss_watched": 0, "model_weakened": 0,
               "near_event": 0, "weight_off": 0, "ai_uncovered": 0, "normal": 0}

    for h in holdings:
        code = h.get("code") or h.get("symbol")
        entry = h.get("entry_price")
        entry_date = h.get("entry_date")
        if not code or entry is None:
            continue

        reasons: list[dict] = []
        candidate_labels: list[str] = []  # 收集触发的 label_kind,最终取优先级最高

        # ── 价格相关计算 ──
        ticker_hist = history.get(code) or {}
        closes = [float(v) for v in (ticker_hist.get("close") or []) if _is_pos_num(v)]
        highs = ticker_hist.get("high") or None
        lows = ticker_hist.get("low") or None
        volumes = ticker_hist.get("volume") or None
        ts_list = ticker_hist.get("ts") or []

        current = closes[-1] if closes else None
        dd_pct = None
        dyn_stop_pct = None
        if current is not None:
            dyn_stop, _atr_src = volatility_adaptive_stop_pct(
                closes, highs=highs, lows=lows, fallback=stop_pct,
            )
            dyn_watch = max(0.05, dyn_stop - 0.05)
            triggered, dd = check_stop_loss_breach(float(entry), current, stop_pct=dyn_stop)
            dd_pct = dd * 100
            dyn_stop_pct = dyn_stop * 100
            if triggered:
                candidate_labels.append("stop_breach")
                reasons.append({"kind": "stop_breach",
                                "text": f"回撤 {dd_pct:+.1f}% · 已破止损线 -{dyn_stop_pct:.0f}%"})
            elif dd <= -dyn_watch:
                candidate_labels.append("stop_watch")
                reasons.append({"kind": "stop_watch",
                                "text": f"回撤 {dd_pct:+.1f}% · 接近止损线 -{dyn_stop_pct:.0f}%"})

            # AVWAP 跌破成本线 — 作为辅助 reason,不抢主标签
            if volumes and entry_date and ts_list:
                try:
                    entry_str = str(entry_date)[:10]
                    anchor_idx = next((i for i, d in enumerate(ts_list) if d >= entry_str), None)
                    if anchor_idx is not None and anchor_idx < len(closes) - 5:
                        avw = anchored_vwap(closes, volumes, anchor_idx=anchor_idx)
                        dev = avw.get("deviation_pct")
                        if dev is not None and dev < -3.0:
                            reasons.append({"kind": "avwap_below",
                                            "text": f"现价低于买入以来 AVWAP {dev:+.1f}%"})
                except Exception:
                    pass

        # ── 持仓覆盖分类 (Part 3 · 2026-05-22) ──
        # 4 档分类决定字段集 + 是否跑后续标签判定
        pick = picks_by_code.get(code)
        target_w = (target_weights or {}).get(code)
        coverage_class = _classify_coverage(
            code, current, in_picks=(pick is not None), in_target=(target_w is not None),
        )
        is_needs_fix, fix_hint = _is_needs_fix(code, current)
        coverage_emoji, coverage_text, _grp = _COVERAGE_CLASSES[coverage_class]
        asset_class, asset_label, asset_hint = _classify_asset_class(code, coverage_class)
        treatment_class, treatment_emoji, treatment_text, treatment_hint = _treatment_for_coverage(coverage_class)

        # needs_fix 跳过 model_weak / 临近事件 / weight_off 评判 — 数据不全,
        # 强行评出来会误导。tracking_only 也跳过 model_weak(ETF 不适用股票因子)。
        skip_model_eval = coverage_class in ("needs_fix", "tracking_only")
        skip_weight_eval = coverage_class in ("needs_fix", "tracking_only", "picks_only")

        if is_needs_fix:
            reasons.append({"kind": "needs_fix", "text": fix_hint})

        # ── 模型转弱 / AI 未覆盖 (跳过 needs_fix / tracking_only) ──
        if not skip_model_eval:
            if pick is None:
                if code in universe_codes:
                    candidate_labels.append("model_weak")
                    reasons.append({"kind": "model_weak",
                                    "text": "今日未入系统推荐池(此前在覆盖范围内)"})
                else:
                    candidate_labels.append("ai_uncovered")
                    reasons.append({"kind": "ai_uncovered",
                                    "text": "不在 AI 组合或普通评分结果内,暂不下结论"})
            else:
                rating = (pick.get("rating") or "").lower()
                if rating == "watch":
                    candidate_labels.append("model_weak")
                    reasons.append({"kind": "model_weak",
                                    "text": f"系统评级 watch(此前 {pick.get('signal') or '-'})"})

        # ── 临近事件 ──
        code6 = code.split(".")[0] if "." in code else code
        evs = events_by_code.get(code6) or []
        if evs:
            candidate_labels.append("near_event")
            ev_text = ", ".join(f"{e['date'].strftime('%m-%d')} {e['desc'][:30]}" for e in evs[:2])
            reasons.append({"kind": "near_event", "text": f"3 天内事件: {ev_text}"})

        # ── 偏离 AI 目标 (target_w 已在上面取过) ──
        if target_w is not None and current is not None and total_capital > 0:
            shares = float(h.get("shares") or 0)
            # 注意: 这里 current 是本币价,目标权重通常按 RMB 计。
            # 不精确换汇,只在已有持仓 RMB 估值后做权重比对 — 简化为接受小偏差,
            # weight_off 阈值 3pt 远大于汇率误差。
            value_local = current * shares
            current_w = value_local / total_capital  # 同币种比较为简化
            gap_pt = (current_w - target_w) * 100
            if abs(gap_pt) >= weight_off_threshold_pt:
                candidate_labels.append("weight_off")
                reasons.append({"kind": "weight_off",
                                "text": f"当前 {current_w*100:.1f}% vs 目标 {target_w*100:.1f}% · 差 {gap_pt:+.1f}pt"})

        # ── 取最严重标签 (priority 数字最小) ──
        if candidate_labels:
            label_kind = min(candidate_labels, key=lambda k: _VERDICT_LABELS[k][2])
        else:
            label_kind = "normal"
        emoji, label_text, _prio = _VERDICT_LABELS[label_kind]

        # 累计 summary
        if label_kind == "stop_breach":
            summary["stoploss_breached"] += 1
        elif label_kind == "stop_watch":
            summary["stoploss_watched"] += 1
        elif label_kind == "model_weak":
            summary["model_weakened"] += 1
        elif label_kind == "near_event":
            summary["near_event"] += 1
        elif label_kind == "weight_off":
            summary["weight_off"] += 1
        elif label_kind == "ai_uncovered":
            summary["ai_uncovered"] += 1
        else:
            summary["normal"] += 1

        out_holdings.append({
            "code": code,
            "label_kind": label_kind,
            "label_emoji": emoji,
            "label_text": label_text,
            "coverage_class": coverage_class,
            "coverage_emoji": coverage_emoji,
            "coverage_text": coverage_text,
            "asset_class": asset_class,
            "asset_label": asset_label,
            "asset_hint": asset_hint,
            "treatment_class": treatment_class,
            "treatment_emoji": treatment_emoji,
            "treatment_text": treatment_text,
            "treatment_hint": treatment_hint,
            "fix_hint": fix_hint or None,
            "entry": float(entry),
            "current": current,
            "dd_pct": dd_pct,
            "stop_pct": dyn_stop_pct,
            "reasons": reasons,
        })
        # 累计 coverage 分组计数
        cov_key = f"coverage_{coverage_class}"
        summary[cov_key] = summary.get(cov_key, 0) + 1
        asset_key = f"asset_{asset_class}"
        summary[asset_key] = summary.get(asset_key, 0) + 1

    return {
        "as_of": today.strftime("%Y-%m-%d"),
        "holdings": out_holdings,
        "summary": summary,
    }


def _is_pos_num(v) -> bool:
    """numeric > 0 检查（用于过滤 history close 序列里的脏数据）。"""
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False


def section_holdings_stoploss(history: dict | None = None,
                              stop_pct: float = 0.15,
                              watch_pct: float = 0.10) -> str:
    """读 real_holdings 表 + 最新收盘价,告警触发各自动态止损线的真实持仓。

    生产实时监控(vs apply_stop_loss 的回测语义):每天早上扫一遍,
    破线→🔴 复查;接近线→🟡 留意。空持仓 / 无告警时返回 "" → build_brief 整段省略。

    2026-05-22: 重构为调 compute_holdings_verdict() 单一源,
    措辞从"建议清仓或减半"软化为"建议复查"(feedback_advisory_not_directive)。
    """
    try:
        sys.path.insert(0, str(REPO / "scripts" / "lib"))
        import stock_db  # type: ignore
        holdings = stock_db.fetch_all_real_holdings()
    except Exception:
        return ""

    if not holdings:
        return ""

    verdict = compute_holdings_verdict(holdings, history=history, stop_pct=stop_pct)
    breached = [h for h in verdict["holdings"] if h["label_kind"] == "stop_breach"]
    watched = [h for h in verdict["holdings"] if h["label_kind"] == "stop_watch"]
    avwap_only = [h for h in verdict["holdings"]
                  if h["label_kind"] not in ("stop_breach", "stop_watch")
                  and any(r["kind"] == "avwap_below" for r in h["reasons"])]

    if not breached and not watched and not avwap_only:
        return ""

    lines = ["#### 1.5 持仓止损告警（ATR-proxy + AVWAP 成本线双闸门）"]
    if breached:
        lines.append(f"🔴 **{len(breached)} 只破各自动态止损线**（建议复查）：")
        for r in breached[:10]:
            lines.append(f"• **{r['code']}** {r['dd_pct']:+.1f}% (止损线 -{r['stop_pct']:.0f}%) · "
                         f"entry {r['entry']:.2f} → now {r['current']:.2f}")
    if watched:
        if breached:
            lines.append("")
        lines.append(f"🟡 **{len(watched)} 只接近各自止损线**（留意）：")
        for r in watched[:10]:
            lines.append(f"• {r['code']} {r['dd_pct']:+.1f}% (止损线 -{r['stop_pct']:.0f}%) · "
                         f"entry {r['entry']:.2f} → now {r['current']:.2f}")
    if avwap_only:
        if breached or watched:
            lines.append("")
        lines.append(f"⚠️ **{len(avwap_only)} 只跌破 entry 后 AVWAP 成本线** "
                     f"(市场平均买入成本已失守 > 3%)：")
        for h in avwap_only[:10]:
            avw_reason = next((r for r in h["reasons"] if r["kind"] == "avwap_below"), None)
            extra = avw_reason["text"] if avw_reason else ""
            lines.append(f"• {h['code']} 现价 {h['current']:.2f} · {extra}")
    return "\n".join(lines) + "\n"


def section_regime(defense: dict | None,
                   qgate: dict | None = None,
                   acceptance: dict | None = None) -> str:
    """读 defense + 质量闸门 + 生产验收，三道闸门取最严，告诉用户今天能不能动手。

    数据源 schema:
      defense:    realtime_defense_*.json → severity (NONE/LOW/HIGH/CRITICAL) + summary + alerts
      qgate:      recommendation_quality_gate.json → status (PASS/WARN/FAIL) + issues
      acceptance: production_acceptance_check.json → status (PASS/WARN/FAIL) + issues
    """
    if not defense:
        return (
            "#### 1. 今天能不能动手？\n"
            "⚠️ 未找到 realtime_defense 输出 — **保守起见今天按已有计划执行，不要加仓**。\n"
        )

    if qgate is None:
        qgate = _quality_gate_payload()
    if acceptance is None:
        acceptance = _acceptance_payload()

    severity, icon, _tpl, reasons = _combined_severity(defense, qgate, acceptance)
    defense_sev = defense.get("severity", "UNKNOWN")
    defense_summary = defense.get("summary", "")
    alerts = defense.get("alerts", []) or []
    qgate_status = str((qgate or {}).get("status") or "—").upper()
    accept_status = str((acceptance or {}).get("status") or "—").upper()

    def _badge(status: str) -> str:
        return {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}.get(status, "·") + " " + status

    advice = {
        "NONE": "👉 **今天可以正常调仓**（三道闸门都没亮灯）。",
        "LOW": "👉 **留意别加仓**，单笔不超 5% 仓位；先看升档理由。",
        "HIGH": "👉 **只读不交易 / 减仓 30-50%**，先修验收或闸门里的 FAIL 项。",
        "CRITICAL": "👉 **清仓 sit out** — 崩盘期历史 alpha = -9.77%，等灯转回 LOW 再回来。",
    }.get(severity, "👉 保守按已有计划执行。")

    lines = [
        "#### 1. 今天能不能动手？（防御 + 质量闸门 + 生产验收 · 取最严）",
        f"{icon} **{severity}** — 防御 {defense_sev} · 质量闸门 {_badge(qgate_status)} · 生产验收 {_badge(accept_status)}",
        advice,
        "",
        "📖 灯色规则（取最严）：🟢 三道全绿 ｜ 🟡 任一 WARN ｜ 🟠 任一 FAIL 或防御 HIGH ｜ 🔴 防御 CRITICAL",
        "🛡️ 防御：VIX / 200MA / 单股止损 ｜ 质量闸门：picks/factor 完整性 ｜ 生产验收：今日 pipeline 是否真跑通",
    ]
    if reasons:
        lines.append("")
        lines.append(f"**为什么不是 🟢**（{len(reasons)} 条升档理由）:")
        for r in reasons:
            lines.append(f"• {r}")
    if defense_summary:
        lines.append("")
        lines.append(f"**防御原文**：{defense_summary}")
    if alerts:
        lines.append("")
        lines.append("**防御告警**：")
        for a in alerts[:5]:
            kind = a.get("kind") or a.get("type") or "alert"
            msg = (a.get("message") or a.get("detail") or a.get("suggested_action")
                   or a.get("trigger") or json.dumps(a, ensure_ascii=False))
            lines.append(f"• {kind} · {msg}")
    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────
# Section 2: 今天的候选（A 股 + 美股）
# ────────────────────────────────────────────────────────

def _factor_scores_index(factor_scores: dict | None) -> tuple[dict, dict]:
    """把 factor_scores_today.json 的 factors[] / signals[] 数组建成 ticker 索引。"""
    if not isinstance(factor_scores, dict):
        return {}, {}
    factors = {e.get("ticker"): e for e in (factor_scores.get("factors") or []) if e.get("ticker")}
    signals = {e.get("ticker"): e for e in (factor_scores.get("signals") or []) if e.get("ticker")}
    return factors, signals


def _build_us_reasons(ticker: str, factors_map: dict, signals_map: dict) -> tuple[list[str], list[str]]:
    """美股理由 — 从 factor_scores_today.json 5 维度组装：F-Score / 12-1 动量 / PEAD / 分析师 / 内部人。"""
    pros: list[str] = []
    cons: list[str] = []
    f = factors_map.get(ticker, {}) or {}
    s = signals_map.get(ticker, {}) or {}

    piot = f.get("piotroski") or {}
    fs = piot.get("f_score")
    if isinstance(fs, (int, float)):
        details = piot.get("details") or {}
        green = sum(1 for v in details.values() if v)
        if fs >= 7:
            pros.append(f"F-Score {fs}/9 基本面优（{green} 项绿灯）")
        elif fs >= 5:
            pros.append(f"F-Score {fs}/9 基本面中性（{green} 项绿灯）")
        else:
            cons.append(f"F-Score {fs}/9 基本面偏弱（仅 {green} 项绿灯）")

    mom = (f.get("momentum") or {}).get("momentum_12_1")
    if isinstance(mom, (int, float)):
        if mom >= 200:
            cons.append(f"12-1 月动量 +{mom:.0f}% 异常高（可能数据问题/拆股，注意均值回归）")
        elif mom >= 30:
            pros.append(f"12-1 月动量 +{mom:.0f}% 强势")
        elif mom <= -20:
            cons.append(f"12-1 月动量 {mom:+.0f}% 下行")

    pead = f.get("pead") or {}
    acc = pead.get("acceleration")
    if isinstance(acc, (int, float)):
        if acc >= 3:
            pros.append(
                f"PEAD 盈利加速 +{acc:.1f}%（本季 QoQ {pead.get('qoq_now_pct',0):.1f}% vs 上季 {pead.get('qoq_prev_pct',0):.1f}%）"
            )
        elif acc <= -3:
            cons.append(f"PEAD 盈利减速 {acc:+.1f}%")

    an = s.get("analyst") or {}
    raises = an.get("raises") or 0
    lowers = an.get("lowers") or 0
    tgt = an.get("avg_target_raise_pct")
    if raises + lowers >= 3:
        if raises >= 5 and raises >= 3 * max(lowers, 1):
            tgt_str = f"，平均目标价 +{tgt:.0f}%" if isinstance(tgt, (int, float)) else ""
            pros.append(f"近 90d {raises} 家分析师上调 vs {lowers} 家下调{tgt_str}")
        elif lowers >= 3 and lowers > raises:
            cons.append(f"近 90d {lowers} 家分析师下调 vs {raises} 家上调")

    ins = s.get("insider") or {}
    net_val = ins.get("net_value_usd_approx")
    if isinstance(net_val, (int, float)) and abs(net_val) >= 1e7:
        mn = net_val / 1e6
        # 跟 Form 4 60d 数据交叉，方向相反时合并成「反转」描述（避免 ✅ 6m 净买入 + 🔴 60d 净卖出 视觉冲突）
        form4_60d = _form4_60d_net_m(ticker)
        if form4_60d is not None and (mn * form4_60d) < 0:
            # 方向相反
            if mn > 0:
                cons.append(
                    f"内部人 6m 净买入 ${mn:.0f}M，但 **近 60d 转向**净卖出 ${-form4_60d:.0f}M（管理层近期改变方向）"
                )
            else:
                pros.append(
                    f"内部人 6m 净卖出 ${-mn:.0f}M，但 **近 60d 转向**净买入 ${form4_60d:.0f}M（管理层近期改变方向）"
                )
        elif mn > 0:
            pros.append(f"内部人 6m 净买入 ${mn:.0f}M（管理层看好信号）")
        else:
            cons.append(f"内部人 6m 净卖出 ${-mn:.0f}M（注意管理层抛售）")

    return pros, cons


# 缓存：避免 _build_us_reasons 每次重读 Form 4 文件
_FORM4_60D_CACHE: dict[str, float] | None = None


def _form4_60d_net_m(ticker: str) -> float | None:
    """读 Form 4 60d 聚合净额（百万美元，正=买负=卖），不存在返回 None。"""
    global _FORM4_60D_CACHE
    if _FORM4_60D_CACHE is None:
        d = _load_json(REPO / "data" / "event_calendar_us_form4.json") or {}
        idx: dict[str, float] = {}
        for e in (d.get("events") or []):
            t = (e.get("ticker") or "").upper()
            net = e.get("net_amount_usd")
            if t and isinstance(net, (int, float)):
                idx[t] = net / 1e6
        _FORM4_60D_CACHE = idx
    return _FORM4_60D_CACHE.get(ticker.upper())


def _build_hk_reasons(entry: dict) -> tuple[list[str], list[str]]:
    """港股理由 — 直接读 hk_picks entry（f_score / momentum_12_1 / reversal_1m）。"""
    pros: list[str] = []
    cons: list[str] = []

    fs = entry.get("f_score")
    if isinstance(fs, (int, float)):
        if fs >= 7:
            pros.append(f"F-Score {fs}/9 基本面优（akshare 港股年报）")
        elif fs >= 5:
            pros.append(f"F-Score {fs}/9 基本面中性")
        else:
            cons.append(f"F-Score {fs}/9 基本面偏弱")

    mom = entry.get("momentum_12_1")
    if isinstance(mom, (int, float)):
        if mom >= 30:
            pros.append(f"12-1 月动量 +{mom:.0f}% 强势")
        elif mom <= -20:
            cons.append(f"12-1 月动量 {mom:+.0f}% 下行")

    rev = entry.get("reversal_1m")
    if isinstance(rev, (int, float)) and rev <= -10:
        pros.append(f"近 1 月反转 {rev:+.1f}%（超跌候选）")

    sector = entry.get("sector")
    if sector and sector not in ("", "未知"):
        pros.append(f"行业：{sector}")

    for n in (entry.get("notes") or []):
        if n:
            cons.append(str(n))

    return pros, cons


def _build_a_share_reasons(entry: dict) -> tuple[list[str], list[str]]:
    """A 股理由 — 读 a_share_picks entry 的子因子（f_score_norm/lhb/north/pead/policy）+ 风险标志。"""
    pros: list[str] = []
    cons: list[str] = []

    f_norm = entry.get("f_score_norm")
    if isinstance(f_norm, (int, float)):
        f_int = round(f_norm * 9)
        if f_int >= 7:
            pros.append(f"F-Score {f_int}/9 基本面优")
        elif f_int >= 5:
            pros.append(f"F-Score {f_int}/9 基本面中性")
        else:
            cons.append(f"F-Score {f_int}/9 基本面偏弱")

    lhb = entry.get("lhb_score")
    if isinstance(lhb, (int, float)):
        if lhb >= 0.7:
            pros.append(f"龙虎榜机构净买入（分 {lhb:.2f}）")
        elif lhb <= 0.3:
            cons.append(f"龙虎榜机构净卖出（分 {lhb:.2f}）")

    nv = entry.get("north_score")
    if isinstance(nv, (int, float)):
        if nv >= 0.7:
            pros.append(f"北向资金加仓（分 {nv:.2f}）")
        elif nv <= 0.3:
            cons.append(f"北向资金减持（分 {nv:.2f}）")

    pead = entry.get("pead_score")
    if isinstance(pead, (int, float)) and pead >= 0.7:
        pros.append(f"PEAD 盈利加速信号（分 {pead:.2f}）")

    pb = entry.get("policy_boost")
    if isinstance(pb, (int, float)) and pb > 0.05:
        pros.append(f"政策受益主题 +{pb*100:.0f}%")

    er = entry.get("event_risk_score")
    if isinstance(er, (int, float)) and er < 0.7:
        cons.append(f"事件风险分 {er:.2f}（earnings/政策密集期）")

    for rf in (entry.get("risk_flags") or []):
        if rf:
            cons.append(f"红旗：{rf}")
    for br in (entry.get("block_reasons") or []):
        if br:
            cons.append(f"约束器命中：{br}")

    return pros, cons


def _format_reason_lines(pros: list[str], cons: list[str],
                        max_pros: int = 2, max_cons: int = 1) -> list[str]:
    """把 pros/cons 拼成 1-3 行缩进文本。默认 top-2 ✅ + top-1 ⚠️。"""
    lines: list[str] = []
    for p in pros[:max_pros]:
        lines.append(f"  ✅ {p}")
    for c in cons[:max_cons]:
        lines.append(f"  ⚠️ {c}")
    return lines


# ───────────── why now catalyst (近 60 天事件解释 X 为啥被推荐) ─────────────
# 实现抽到 stock_research/core/catalyst.py 与 dashboard 共享，避免双引擎漂移。

from stock_research.core.catalyst import get_catalyst as _catalyst_sentence


def _build_catalyst(ticker: str, lookback_days: int = 60) -> str | None:
    """morning_brief 端 catalyst wrapper：返回缩进 + 📰 + 句子的完整行。
    无可用催化返回 None。
    """
    s = _catalyst_sentence(ticker, lookback_days=lookback_days)
    return f"  📰 {s}" if s else None


def _ticker_signal_lines(ticker: str) -> list[str]:
    """ticker 主行下方应当追加的 signal 行列表（按重要度排序）：
    🆕 首次进入 / 📈 跃升 在前，📰 催化在后。无信号返回 []。
    """
    out: list[str] = []
    rise = _build_rise_signal(ticker)
    if rise:
        out.append(rise)
    cat = _build_catalyst(ticker)
    if cat:
        out.append(cat)
    return out


# ───────────── 推荐异动（🆕/📈/📉）— 读 dashboard dump 的 picks_appearance.json ─────────────
# dashboard build 时算好（read DuckDB once），morning_brief 这里只读 JSON 避免锁冲突。

_APPEARANCE_CACHE: dict | None = None


def _load_appearance() -> dict:
    global _APPEARANCE_CACHE
    if _APPEARANCE_CACHE is None:
        d = _load_json(REPO / "data" / "latest" / "picks_appearance.json") or {}
        _APPEARANCE_CACHE = d
    return _APPEARANCE_CACHE


def _build_rise_signal(ticker: str) -> str | None:
    """📈 跃升 / 🆕 首次 一行（不带缩进，caller 自己加 2 空格）。
    无触发返回 None。
    门槛同 dashboard：rank_up >= 3 或 score_up >= 2.0；🆕 = count==1 && total_runs>=2。
    """
    if not ticker:
        return None
    ap = _load_appearance()
    total_runs = int(ap.get("total_runs") or 0)
    if total_runs < 2:
        return None
    info = (ap.get("tickers") or {}).get(ticker.upper())
    if not info:
        return None
    count = int(info.get("count") or 0)
    if count == 1:
        return f"  🆕 首次进入推荐（{info.get('first_seen_date') or '今日'}）"
    rank_up = info.get("rank_up")
    score_up = info.get("score_up")
    parts = []
    if isinstance(rank_up, (int, float)) and rank_up >= 3:
        parts.append(f"排名 {info.get('prev_rank')}→{info.get('cur_rank')}（+{rank_up} 位）")
    if isinstance(score_up, (int, float)) and score_up >= 2.0:
        parts.append(f"评分 {info.get('prev_score')}→{info.get('cur_score')}（+{score_up:.1f}）")
    return f"  📈 {' · '.join(parts)}" if parts else None


def _signal_quality_tag(
    ticker: str,
    composite: float | None,
    pct60: float | None,
    history: dict | None,
    score_threshold: float | None = None,
    mode: str = "normalized",
) -> str | None:
    """morning_brief 端的标签 wrapper — 真正的判断在 stock_research.core.quality_tag。

    返回 markdown 行（带 2 空格缩进），或 None。
    """
    from stock_research.core.quality_tag import classify_from_history
    tag = classify_from_history(ticker, composite, history or {},
                                mode=mode, score_threshold=score_threshold)
    return tag.as_markdown_line() if tag else None


def section_dropouts() -> str:
    """跌出 Top section — 上批次在 picks、本批次不在的票（含持仓警示）。"""
    ap = _load_appearance()
    drops = ap.get("dropouts") or []
    if not drops:
        return ""
    # 持仓 set
    review = _load_json(REPO / "data" / "latest" / "real_holding_review.json") or {}
    held = {(it.get("symbol") or it.get("code") or "").upper() for it in (review.get("items") or [])}
    # 按市场分组
    by_market = {"US": [], "CN": [], "HK": []}
    for d in drops:
        m = (d.get("market") or "").upper()
        if m in by_market:
            by_market[m].append(d)
    parts = ["#### 3.5 📉 上批次在 Top、本批次跌出"]
    parts.append(f"_总 {len(drops)} 只 · 跌出 ≠ 退出科技 universe，可能只是排名滑出前 20。若手上有，建议复查持有理由。_")
    for m_key, m_label in [("CN", "🇨🇳 A 股"), ("HK", "🇭🇰 港股"), ("US", "🇺🇸 美股")]:
        rows = by_market.get(m_key) or []
        if not rows:
            continue
        rows = sorted(rows, key=lambda x: (x.get("prev_rank") or 99))
        parts.append(f"**{m_label}** · {len(rows)} 只")
        for d in rows:
            tk = (d.get("ticker") or "").upper()
            name = d.get("name") or ""
            rank = d.get("prev_rank")
            score = d.get("prev_score")
            rating = d.get("prev_rating") or ""
            rating_cn = {"strong_buy": "强买", "buy": "买入", "watch": "观察", "avoid": "回避"}.get(rating, rating)
            held_warn = " ⚠️ **你持仓**" if tk in held else ""
            parts.append(f"• 上次第 {rank} 名 · **{tk}** {name} · 综合 {score} / {rating_cn}{held_warn}")
    return "\n".join(parts)


def _humanize_picks(plan: list[dict], a_share: bool, history: dict | None = None,
                    factor_scores: dict | None = None) -> list[str]:
    """把 plan_v5 entry 排成一句话/只 + 推荐理由行（扁平 list，每只股 1-4 行）。"""
    factors_map, signals_map = _factor_scores_index(factor_scores)
    out = []
    for entry in plan:
        ticker = entry.get("ticker", "?")
        if a_share and not _is_a_share(ticker):
            continue
        if not a_share and _is_a_share(ticker):
            continue
        weight = entry.get("v5_weight") or entry.get("weight") or 0
        f_score = _format_f_score(_entry_f_score(entry))
        z = entry.get("composite_z", entry.get("composite", 0))
        spark, pct60 = _ticker_sparkline(history or {}, ticker)
        spark_str = f" · {spark}" + (f" {pct60:+.1f}% 60d" if pct60 is not None else "")
        # F-Score 缺失时不在主行展示「F-Score 缺失」噪声；section header 已说明基本面未覆盖
        f_str = f" · F-Score {f_score}" if f_score != "缺失" else ""
        out.append(
            f"• **{ticker}** {weight*100:.1f}%{f_str} · 综合 {z:+.2f}{spark_str}"
        )
        out.extend(_ticker_signal_lines(ticker))
        if not a_share:
            qtag = _signal_quality_tag(ticker, z, pct60, history, mode="z_score")
            if qtag:
                out.append(qtag)
            pros, cons = _build_us_reasons(ticker, factors_map, signals_map)
            out.extend(_format_reason_lines(pros, cons))
    return out


def _humanize_picks_grouped(plan: list[dict], a_share: bool, history: dict | None = None,
                            factor_scores: dict | None = None) -> list[str]:
    """每只股聚合成 1 个多行 markdown 块（含 ticker 主行 + 缩进 reasons）。供飞书卡片 2 列拆分用。"""
    factors_map, signals_map = _factor_scores_index(factor_scores)
    out = []
    for entry in plan:
        ticker = entry.get("ticker", "?")
        if a_share and not _is_a_share(ticker):
            continue
        if not a_share and _is_a_share(ticker):
            continue
        weight = entry.get("v5_weight") or entry.get("weight") or 0
        f_score = _format_f_score(_entry_f_score(entry))
        z = entry.get("composite_z", entry.get("composite", 0))
        spark, pct60 = _ticker_sparkline(history or {}, ticker)
        spark_str = f" · {spark}" + (f" {pct60:+.1f}% 60d" if pct60 is not None else "")
        # F-Score 缺失时不在主行展示「F-Score 缺失」噪声；section header 已说明基本面未覆盖
        f_str = f" · F-Score {f_score}" if f_score != "缺失" else ""
        head = f"• **{ticker}** {weight*100:.1f}%{f_str} · 综合 {z:+.2f}{spark_str}"
        block_lines: list[str] = [head]
        block_lines.extend(_ticker_signal_lines(ticker))
        if not a_share:
            qtag = _signal_quality_tag(ticker, z, pct60, history, mode="z_score")
            if qtag:
                block_lines.append(qtag)
            pros, cons = _build_us_reasons(ticker, factors_map, signals_map)
            block_lines.extend(_format_reason_lines(pros, cons))
        out.append("\n".join(block_lines))
    return out


def section_picks(plan: dict | None, a_share_picks: dict | None,
                  hk_picks: dict | None = None, history: dict | None = None,
                  factor_scores: dict | None = None,
                  read_only: bool = False) -> str:
    """三线独立展示：🇺🇸 美股 (plan_v5) / 🇭🇰 港股 (hk_picks) / 🇨🇳 A 股 (a_share_picks)。

    每条线独立数据源、独立因子、独立优选 — 不混排。
    每只股下追加 top-2 ✅ 推荐理由 + top-1 ⚠️ 风险点（来自子因子分解）。
    """
    if not plan and not hk_picks and not a_share_picks:
        return (
            "#### 2. 🔝 AI 推荐与模型组合（三线独立）\n"
            "⚠️ 美股/港股/A 股三套数据源全部缺失 — 检查 daily_refresh.sh 是否跑完。\n"
        )

    head = "#### 2. 🔝 AI 推荐与模型组合（三线独立 · 每只股附 ✅ 推荐理由 + ⚠️ 风险点）"
    if read_only:
        head += "\n🔴 **质量闸门 FAIL：以下只读观察，不作为买入/加仓清单。**"
    if plan:
        pm = plan.get("portfolio_metrics") or {}
        if pm:
            weight_src = _plan_weight_source(plan)
            head += (
                f"  ·  模型回测 Sharpe {pm.get('annual_sharpe', '?')} · "
                f"回测年化 {pm.get('annual_return_pct', '?')}% · "
                f"波动 {pm.get('annual_vol_pct', '?')}% · "
                f"{weight_src['label']}"
            )
    lines = [head]

    # 🇺🇸 美股（plan_v5 兼容字段 · v6 risk-aware optimize）
    if plan:
        plan_v5 = plan.get("plan_v5") or []
        us_lines = _humanize_picks(plan_v5, a_share=False, history=history, factor_scores=factor_scores)
        if us_lines:
            n_us = sum(1 for l in us_lines if l.startswith("•"))
            ts_us = _fmt_ts(plan.get("generated_at"))
            weight_src = _plan_weight_source(plan)
            # 检测本批次 F-Score 是否全部缺失，用以调整 section header 文案
            us_plan_v5 = plan.get("plan_v5") or []
            us_f_present = any(_entry_f_score(e) is not None for e in us_plan_v5)
            factor_label = "动量 + 估值 + 数据覆盖" + ("" if us_f_present else "（基本面 Piotroski 暂未覆盖）")
            lines.append(f"**🇺🇸 美股 ({n_us} 只 · {factor_label} · {weight_src['label']})** · {ts_us}")
            if weight_src.get("is_fallback"):
                lines.append(f"⚠️ {weight_src['detail']}。这些百分比不是新鲜 risk-aware optimizer 输出。")
                if weight_src.get("stage_errors"):
                    lines.append("• optimizer 失败摘要：" + " ｜ ".join(weight_src["stage_errors"]))
            cash_info = _plan_cash_breakdown(plan)
            if cash_info.get("cash_pct") is not None:
                tag = "⚠️ " if cash_info.get("is_high_cash") else "💼 "
                head_line = (
                    f"{tag}组合现金 **{cash_info['cash_pct']*100:.1f}%** · "
                    f"已配 {cash_info['gross_pct']*100:.1f}%（{n_us} 只）"
                )
                lines.append(head_line)
                if cash_info.get("explain"):
                    lines.append(f"• 现金不是看空，是约束器把放不下的额度推回：{cash_info['explain']}")
            lines.extend(us_lines)
        else:
            lines.append("**🇺🇸 美股** — _plan_v5 为空_")

    # 🇭🇰 港股（V2 lite scoring · 段标题按 factor_scores 实际字段动态生成）
    if hk_picks and hk_picks.get("selected"):
        sel = hk_picks["selected"][:10]
        ts_hk = _fmt_ts(hk_picks.get("generated_at"))
        hk_factor_label = _label_v2_factor_set(sel)
        lines.append(f"**🇭🇰 港股 ({len(sel)} 只 · {hk_factor_label})** · {ts_hk}")
        for entry in sel:
            ticker = entry.get("code", "?")
            name = entry.get("name", "")
            score = entry.get("composite", 0)
            f_score = _entry_f_score(entry)
            f_fmt = _format_f_score(f_score)
            f_str = f" · F-Score {f_fmt}" if f_fmt != "缺失" else ""
            spark, pct60 = _ticker_sparkline(history or {}, ticker)
            spark_str = f" · {spark}" + (f" {pct60:+.1f}% 60d" if pct60 is not None else "")
            lines.append(f"• **{ticker}** {name} · 综合 {score:.3f}{f_str}{spark_str}")
            lines.extend(_ticker_signal_lines(ticker))
            qtag = _signal_quality_tag(ticker, score, pct60, history)
            if qtag:
                lines.append(qtag)
            pros, cons = _build_hk_reasons(entry)
            lines.extend(_format_reason_lines(pros, cons))
    else:
        lines.append("**🇭🇰 港股** — _hk_picks.json 缺失，跑 `python3 -m scripts.pipeline.hk_picks`_")

    # 🇨🇳 A 股（a_share_picks · 6 因子）
    if not _a_share_enabled():
        lines.append(
            "**🇨🇳 A 股** — _生产推荐未启用：缺少已验证的 A 股 IC 校准权重；"
            "当前只保留研究观察，不进入调仓清单_"
        )
    elif a_share_picks and a_share_picks.get("selected"):
        sel = a_share_picks["selected"][:10]
        ts_cn = _fmt_ts(a_share_picks.get("generated_at"))
        cn_factor_label = _label_v2_factor_set(sel)
        lines.append(f"**🇨🇳 A 股 ({len(sel)} 只 · {cn_factor_label})** · {ts_cn}")
        for entry in sel:
            ticker = entry.get("ticker", entry.get("code", "?"))
            name = entry.get("name", "")
            score = entry.get("composite", 0)
            f_score = _entry_f_score(entry)
            f_fmt = _format_f_score(f_score)
            f_str = f" · F-Score {f_fmt}" if f_fmt != "缺失" else ""
            spark, pct60 = _ticker_sparkline(history or {}, ticker)
            spark_str = f" · {spark}" + (f" {pct60:+.1f}% 60d" if pct60 is not None else "")
            lines.append(f"• **{ticker}** {name} · 综合 {score:.3f}{f_str}{spark_str}")
            lines.extend(_ticker_signal_lines(ticker))
            qtag = _signal_quality_tag(ticker, score, pct60, history)
            if qtag:
                lines.append(qtag)
            pros, cons = _build_a_share_reasons(entry)
            lines.extend(_format_reason_lines(pros, cons))
    elif plan:
        # fallback: 盘前 a_share_picks 没出，用 plan_v5 里残留的 A 股代号兜底
        plan_v5 = plan.get("plan_v5") or []
        a_lines = _humanize_picks(plan_v5, a_share=True, history=history)
        if a_lines:
            lines.append(f"**🇨🇳 A 股 ({sum(1 for l in a_lines if l.startswith('•'))} 只 · 盘前数据，16:30 后更准)**")
            lines.extend(a_lines)
        else:
            lines.append("**🇨🇳 A 股** — _a_share_picks.json 缺失（盘后才跑）_")
    else:
        lines.append("**🇨🇳 A 股** — _a_share_picks.json 缺失（盘后才跑）_")

    # 趋势图例（让新人能看懂每只股后面那个 emoji 是什么意思）
    lines.append("")
    lines.append(TREND_LEGEND_SHORT)
    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────
# Section 2.5: A 股被拒清单（top 20 综合分高但被约束器拦下）
# ────────────────────────────────────────────────────────

def _rejected_a_share_entries(a_share_picks: dict | None, top_n: int = 20) -> list[dict]:
    """从 a_share_picks 的 all_entries 里挑被拒清单。

    定义"被拒"：在 entries 池里但不在 selected 中，按 composite 取前 top_n。
    覆盖三类：硬拦截（tradable=False，含 block_reasons）/ 软拒（recommended=False，分数不够）/
    sector cap 跳过的（notes 里有 sector_cap）。
    """
    if not a_share_picks:
        return []
    all_e = a_share_picks.get("all_entries") or []
    sel = a_share_picks.get("selected") or []
    sel_codes = {e.get("code") for e in sel}
    rest = [e for e in all_e if e.get("code") not in sel_codes]
    rest.sort(key=lambda e: float(e.get("composite") or 0), reverse=True)
    return rest[:top_n]


def section_rejected_a_share(a_share_picks: dict | None, top_n: int = 20) -> str:
    """A 股被拒 top N — 让用户审计"约束器是不是过严了"。"""
    rejected = _rejected_a_share_entries(a_share_picks, top_n=top_n)
    if not rejected:
        return ""

    lines = [
        f"#### 2.5 A 股候选但被拒 · top {len(rejected)}（综合分高但没进推荐）",
        "_审计用：如果好票频繁出现在这里，可能约束器过严；可对照原因调阈值_",
        "",
    ]
    for e in rejected:
        code = e.get("code", "?")
        name = e.get("name", "")
        comp = float(e.get("composite") or 0)
        industry = e.get("industry", "")
        ind_str = f" [{industry}]" if industry else ""
        lines.append(f"• **{code}** {name}{ind_str} · 综合 {comp:.3f}")
        pros, cons = _build_a_share_reasons(e)
        # 拒绝列表反过来：风险/拒绝原因为主，1 条优点作上下文
        reject_lines: list[str] = []
        # 优先展示 block_reasons / risk_flags（最关键的拒绝原因）
        for br in (e.get("block_reasons") or [])[:2]:
            if br:
                reject_lines.append(f"  ❌ 约束器命中：{br}")
        for rf in (e.get("risk_flags") or [])[:1]:
            if rf:
                reject_lines.append(f"  ⚠️ 红旗：{rf}")
        # 如果没硬拦截，从 cons 里挑（即软拒）
        if not reject_lines:
            for c in cons[:2]:
                reject_lines.append(f"  ⚠️ {c}")
        # 上下文：附 1 条优点（不然全是 ❌ 看不出为什么综合分能挤进 top）
        if pros:
            reject_lines.append(f"  ✅ {pros[0]}（综合分高的原因）")
        # 兜底
        if not reject_lines:
            tradable = e.get("tradable", True)
            reject_lines.append(f"  ❌ {'不可买' if not tradable else '分数未达 cutoff'}")
        lines.extend(reject_lines)
    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────
# Section 2.5: 周一专属 — 上周命中率回顾（评级 + AI 推荐）
#   只在周一显示,其他 6 天为空
#   回答用户"AI 给的评级/推荐到底准不准"
# ────────────────────────────────────────────────────────

def section_walk_forward_oos(today: date | None = None) -> str:
    """周一专属 — 展示最新一次 walk_forward_backtest 的 OOS 校验结果。

    daily_refresh.sh 25b 段每周一跑 walk_forward_backtest 落地 JSON 到 data/。
    Bailey-Lopez de Prado (2014) JPM：walk-forward 是减少 backtest overfit 的金标准。
    """
    today = today or date.today()
    if today.weekday() != 0:
        return ""
    import glob as _glob
    candidates = sorted(_glob.glob(str(REPO / "data" / "walk_forward_*.json")))
    if not candidates:
        return ""
    latest = Path(candidates[-1])
    data = _load_json(latest)
    if not isinstance(data, dict):
        return ""
    summary = data.get("summary") or {}
    months = data.get("months") or []
    if not months:
        return ""

    lines = ["#### 🔬 12 月 OOS 校验（周一专属 · walk-forward）"]
    lines.append(f"窗口 {data.get('start_month')} ~ {data.get('end_month')} · "
                 f"benchmark {data.get('benchmark', 'SPY')} · top-k {data.get('top_k', 5)}")
    sh = summary.get("sharpe_annual")
    ex = summary.get("total_excess_return_pct")
    mdd = summary.get("max_drawdown_pct")
    n = summary.get("n_months")
    lines.append(f"")
    lines.append(f"📊 **年化 Sharpe {sh:+.2f}** · 总超额 {ex:+.1f}% · 最大回撤 {mdd:.1f}% · {n} 月样本")
    lines.append(f"")
    lines.append("📅 最近 4 月明细：")
    for m in months[-4:]:
        ret = m.get("monthly_return", 0)
        bench = m.get("benchmark_return", 0)
        excess = m.get("excess_return", 0)
        picks = ",".join(m.get("selected", [])[:4])
        lines.append(f"• {m.get('month')}: 组合 {ret:+.1f}% / 基准 {bench:+.1f}% / "
                     f"超额 {excess:+.1f}% · {picks}")
    lines.append("")
    lines.append("📖 学术依据：Bailey & Lopez de Prado (2014) JPM — walk-forward "
                 "是减少 backtest overfit 的金标准；单次回测 Sharpe 严重高估")
    return "\n".join(lines) + "\n"


def section_weekly_hitrate(today: date | None = None) -> str:
    """V2: 周一回顾 — 由 pick_outcomes alpha 数据驱动（V1 reviews/discovery_tracking 已删）。"""
    today = today or date.today()
    if today.weekday() != 0:
        return ""
    try:
        import sys
        import os
        _repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        sys.path.insert(0, os.path.join(_repo, "scripts", "lib"))
        import stock_db
    except Exception:
        return ""
    try:
        conn = stock_db.get_db(read_only=True)
    except Exception:
        return ""
    lines = ["#### 🧪 上周回顾 · AI 准不准（周一专属 · V2 pick_outcomes）"]
    try:
        rows = conn.execute(
            """
            SELECT po.horizon, COUNT(*) n,
                   ROUND(AVG(po.return_pct), 2) avg_ret,
                   ROUND(AVG(po.alpha_pct), 2) avg_alpha,
                   ROUND(SUM(CASE WHEN po.is_success THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 0) win_rate
            FROM pick_outcomes po
            JOIN recommendation_runs rr ON rr.run_id = po.run_id
            WHERE po.outcome_date >= ?
              AND po.alpha_pct IS NOT NULL
              AND rr.universe_scope = 'system_tech_universe'
              AND rr.run_date >= ?
              AND rr.strategy_version = (
                SELECT strategy_version
                FROM recommendation_runs
                WHERE universe_scope = 'system_tech_universe' AND status = 'generated'
                ORDER BY generated_at DESC LIMIT 1
              )
            GROUP BY po.horizon ORDER BY po.horizon
            """,
            [today - timedelta(days=30), PRODUCTION_METRICS_START_DATE],
        ).fetchall()
        if rows:
            lines.append(f"**V2 推荐 alpha（近 30 天成熟样本，生产统计自 {PRODUCTION_METRICS_START_DATE} 起）**")
            lines.append("| Horizon | 样本 | 平均涨幅 | 平均 alpha | 胜率 |")
            lines.append("|---|---:|---:|---:|---:|")
            for h, n, ret, alpha, win in rows:
                sign_r = "+" if (ret or 0) >= 0 else ""
                sign_a = "+" if (alpha or 0) >= 0 else ""
                lines.append(f"| {h} | {n} | {sign_r}{ret}% | {sign_a}{alpha}% | {int(win or 0)}% |")
        else:
            lines.append("_pick_outcomes 近 30 天暂无成熟样本（evaluate_v2_picks 每天累积）_")
    except Exception as e:
        lines.append(f"_pick_outcomes 查询失败: {e}_")
    conn.close()
    return "\n".join(lines) + "\n"


def section_ai_alpha(risk_metrics: dict | None) -> str:
    """系统同时跑两个方案（A 静态 vs C 动态），让用户一眼看懂"AI 到底有没有用"。

    TODO（4 周后）：切到 build_stock_dashboard_html.compute_plan_forward_track
    + compute_dynamic_rebalance_track 的真实 forward 数据。当前 tracked=0 / rebalance=0。
    """
    today = date.today()
    inception_date = date(2026, 5, 10)
    days_tracked = max(0, (today - inception_date).days - 1)

    lines = [
        "#### 3. 系统在跑两个方案 · 看 AI 到底有没有用",
        "_系统每周一同时跑两套策略，让数据自然分胜负_",
        "",
        "**📦 方案 A · 静态死守**：5-10 锁定 12 只股，从此不动（佛系基准）",
        "**🔄 方案 C · 动态调仓**：每周一按 AI 重新优化（扣 10bps/换股 手续费）",
        "",
    ]
    if days_tracked < 7:
        lines.append(f"📅 **Forward tracking 累积中**：已 {days_tracked} / 7 天（第一周还没结束）")
        lines.append("🆚 等下周起每周一较量 → **C − A spread** 就是 AI 加的 alpha")
    else:
        lines.append(f"📅 已 forward tracked {days_tracked} 天 — 真实数据见 dashboard")

    if risk_metrics:
        rm = risk_metrics
        lines.append("")
        lines.append("⚠️ **以下是历史回测/模拟，不是实盘业绩；forward 样本仍很短，不能据此证明策略有效。**")
        # NAV 净值趋势 — emoji 双时间窗（近 30d + 总累计）
        nav = _nav_sparkline(rm)
        if nav:
            lines.append(
                f"历史 NAV：近 30d {nav['spark_30d']} {nav['pct_30d']:+.1f}% · "
                f"累计 {nav['spark']} {nav['total_pct']:+.1f}% ({nav['n_days']}d)"
            )
        lines.append(
            f"回测 Sharpe {_fmt_metric(rm.get('sharpe'))} · "
            f"MaxDD **{_fmt_metric(rm.get('max_drawdown_pct'), '%')}** · "
            f"95% VaR {_fmt_metric(rm.get('var_95_pct'), '%')} · "
            "崩盘期 alpha **-9.77%**（4/4 regime 3 跑输 SPY）"
        )

    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────
# Section 3.5: 组合风格暴露 + 因子层 stress（2026-05-12 三审 P0.5）
# ────────────────────────────────────────────────────────

def _fetch_stock_meta_for_exposure(tickers: list[str]) -> tuple[dict, dict]:
    """拉 market_cap + beta（per-ticker），传给 factor_exposure 算 size + beta 暴露。

    cache 到 data/cache/stock_meta.json，TTL 7 天（这两个字段月度稳定）。
    yfinance.Ticker.info 慢（~1-2s/股），12 只组合 ~12-24s 加到 morning_brief；
    有 cache 后只在 cache miss / TTL 过期时拉。
    """
    cache_path = REPO / "data" / "cache" / "stock_meta.json"
    cache: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    market_caps: dict[str, float] = {}
    betas: dict[str, float] = {}
    n_new = 0
    for tk in tickers:
        entry = cache.get(tk)
        if entry and entry.get("fetched_at", "") > cutoff:
            if entry.get("market_cap"):
                market_caps[tk] = float(entry["market_cap"])
            if entry.get("beta"):
                betas[tk] = float(entry["beta"])
            continue
        # cache miss / 过期 → 拉 yfinance.info
        try:
            import yfinance as yf
            info = yf.Ticker(tk).info or {}
            mc = info.get("marketCap")
            b = info.get("beta")
            entry = {
                "market_cap": float(mc) if mc else None,
                "beta": float(b) if b else None,
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            }
            cache[tk] = entry
            if entry["market_cap"]:
                market_caps[tk] = entry["market_cap"]
            if entry["beta"]:
                betas[tk] = entry["beta"]
            n_new += 1
        except Exception as e:
            logger.debug("yfinance.info 失败 %s: %s", tk, e)

    if n_new > 0:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except Exception as e:
            logger.warning("stock_meta cache 写入失败: %s", e)

    return market_caps, betas


def section_factor_risk(plan: dict | None, factor_scores: dict | None) -> str:
    """组合层 Factor Exposure + Factor Stress 摘要。

    数据流：
      plan_a_v5_constrained.plan_v5  → weights
      factor_scores_today.factors    → quality / momentum / pead 信号
      yfinance (optional)            → market_cap / beta（当前 coverage 不足时 skip）

    告警阈值：
      |exposure z| > 1.0 → 风格集中
      stress worst PnL < -10% → 严重
    """
    if not plan or not factor_scores:
        return ""
    plan_entries = plan.get("plan_v5") or []
    factors_list = factor_scores.get("factors") or []
    if not plan_entries or not factors_list:
        return ""

    try:
        from stock_research.core.factor_exposure import (
            compute_portfolio_exposures, simulate_factor_stress,
            build_factor_records_from_pipeline,
        )
    except Exception:
        return ""

    weights = {p["ticker"]: p.get("v5_weight", 0) or 0 for p in plan_entries}
    if not weights:
        return ""

    # 拉 yfinance.info 的 market_cap + beta（per-ticker），传给 factor_exposure
    # 让 5 维风格暴露真完整，不只 momentum 单维有数（七审 P1）
    # cache 30 天（market_cap / beta 月度稳定，避免每次 brief 多 12-24s）
    market_caps, betas = _fetch_stock_meta_for_exposure(list(weights.keys()))
    factor_records = build_factor_records_from_pipeline(
        factors_list, market_caps=market_caps, betas=betas,
    )
    try:
        exposures = compute_portfolio_exposures(weights, factor_records)
        stress = simulate_factor_stress(exposures)
    except Exception:
        return ""

    # 仅在有真实告警或显著暴露时才展示（避免空版面）
    has_alert = bool(exposures.get("alerts"))
    worst = stress.get("worst")
    severe_stress = worst and worst.get("expected_pnl_pct") is not None and abs(worst["expected_pnl_pct"]) >= 5.0
    if not has_alert and not severe_stress:
        return ""

    lines = ["#### 3.5 组合风格暴露 + 因子 Stress（Fama-French + Carhart）"]
    # 暴露
    exp = exposures.get("exposure") or {}
    cov = exposures.get("coverage") or {}
    lines.append("**风格暴露 z-score**（>1 = 偏高 / <-1 = 偏低）：")
    factor_names_zh = {"beta": "β市场", "size": "规模", "value": "价值",
                       "momentum": "动量", "quality": "质量"}
    for f in exposures.get("factor_list", []):
        z = exp.get(f)
        c = cov.get(f, 0)
        z_str = f"{z:+.2f}" if z is not None else "—"
        cov_flag = f"({c*100:.0f}%)" if c < 0.6 else ""
        lines.append(f"• {factor_names_zh.get(f,f)} z={z_str} {cov_flag}")

    if has_alert:
        lines.append("")
        lines.append("⚠️ 暴露告警：")
        for a in exposures["alerts"][:5]:
            lines.append(f"• {a}")

    # Stress
    if worst and worst.get("expected_pnl_pct") is not None:
        lines.append("")
        lines.append(f"💥 **单因子最差**：{factor_names_zh.get(worst['factor'], worst['factor'])} "
                     f"shock {worst['shock_pct']:+.0f}% → 组合预期 **{worst['expected_pnl_pct']:+.2f}%** "
                     f"{worst.get('severity','')}")
        combined = stress.get("combined_stress_pct")
        if combined is not None:
            lines.append(f"💀 最差 3 因子叠加（保守相关性=1）：**{combined:+.2f}%**")
    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────
# Section 4: 红旗
# ────────────────────────────────────────────────────────

def _load_junior_radar() -> dict | None:
    """读 data/latest/junior_stock_radar.json (junior_stock_watcher 输出)"""
    p = REPO / "data" / "latest" / "junior_stock_radar.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("read junior_stock_radar.json failed: %s", exc)
        return None


def _diff_emoji(flag: str | None) -> str:
    return {
        "new": "🆕",
        "upgraded": "📈",
        "downgraded": "📉",
        "jumped": "↑",
        "slipped": "↓",
    }.get(flag or "", "")


def section_junior_radar(top_n: int = 5) -> str:
    """🎯 次新股触底候选 — 只列 🟢 可小仓试探 + 🟡 可研究, 每条带 diff 标记 + 一句 why。

    数据源: data/latest/junior_stock_radar.json (junior_stock_watcher 输出)
    不渲染条件: 池子里没有 actionable (可研究/可小仓试探)
    """
    radar = _load_junior_radar()
    if not radar:
        return ""
    markets = radar.get("markets") or {}
    sections = []
    for mk, label in [("us", "🇺🇸 美股"), ("cn", "🇨🇳 A 股")]:
        pool = (markets.get(mk) or {}).get("junior_pool") or []
        actionable = [x for x in pool if x.get("tier") in ("可小仓试探", "可研究")]
        actionable.sort(key=lambda x: (
            0 if x.get("tier") == "可小仓试探" else 1,
            x.get("percentile") or 999,
        ))
        if not actionable:
            continue
        lines = [f"**{label}** ({len(actionable)} 只 actionable, 显示前 {min(top_n, len(actionable))}):"]
        for x in actionable[:top_n]:
            code = x.get("symbol") or x.get("code")
            name = x.get("name") or ""
            tier_emoji = "🟢" if x.get("tier") == "可小仓试探" else "🟡"
            diff = _diff_emoji(x.get("diff_flag"))
            pct = x.get("percentile")
            pct_str = f"前 {pct}%" if pct is not None else ""
            ready = x.get("readiness_score")
            ready_str = f" · 准备度 {int(ready)}" if ready is not None else ""
            audit = x.get("audit_card") or {}
            why = audit.get("why_bottom_like") or ""
            missing = audit.get("whats_missing") or ""
            head = f"- {tier_emoji} **{code}** {name} {diff} ({pct_str}{ready_str})"
            lines.append(head)
            if why:
                lines.append(f"  ✓ {why}")
            if missing and missing != "三层信号齐全,可正式进入买前研究":
                lines.append(f"  ⚠ {missing}")
        sections.append("\n".join(lines))
    if not sections:
        return ""
    body = "\n\n".join(sections)
    return (
        "#### 🎯 次新股触底候选\n"
        "_仅作研究起点,非买入建议;来自 IPO & 次新股 tab 触底分前 10/30% + 过解禁窗口的候选。_\n\n"
        f"{body}\n\n"
        "_完整列表 + 买前审查卡见 dashboard IPO & 次新股 tab → 次新股底部观察池。_\n"
    )


def section_red_flags(
    plan: dict | None,
    events: dict | None,
    factor_scores: dict | None,
    defense: dict | None,
) -> str:
    """汇总 4 类红旗：数据陈旧 / 事件迫近 / 持仓异常 / regime 告警。

    无红旗时返回空字符串，整 section 不显示（避免占空间）。
    """
    flags: list[str] = []

    # 1. 数据陈旧
    if factor_scores:
        try:
            scored_date = factor_scores.get("date")
            if scored_date:
                d = datetime.strptime(scored_date, "%Y-%m-%d").date()
                age = (date.today() - d).days
                if age >= 2:
                    flags.append(
                        f"⚠️ **因子分数过期** — factor_scores_today 是 {scored_date}（{age} 天前）"
                    )
        except Exception:
            pass

    # 2. 持仓里 3 天内的事件
    if plan and events:
        held_tickers = {e.get("ticker", "") for e in (plan.get("plan_v5") or [])}
        held_codes = {t.split(".")[0] for t in held_tickers if _is_a_share(t)}
        today_d = date.today()
        soon = today_d + timedelta(days=3)
        urgent_events = []
        for ev in (events.get("events") or []):
            code = ev.get("code", "")
            if code not in held_codes:
                continue
            try:
                ev_d = datetime.strptime(ev.get("event_date", ""), "%Y-%m-%d").date()
            except Exception:
                continue
            if today_d <= ev_d <= soon:
                urgent_events.append(ev)
        if urgent_events:
            flags.append(f"⚠️ **持仓 3 天内有 {len(urgent_events)} 个事件：**")
            for ev in urgent_events[:5]:
                flags.append(
                    f"• {ev.get('code')} {ev.get('event_date')} · "
                    f"{ev.get('event_type')} · {ev.get('description', '')[:60]}"
                )

    # 3. regime 告警重复一遍（HIGH/CRITICAL 时才上红旗，对齐 defense_signals 4 档）
    if defense and defense.get("severity") in ("HIGH", "CRITICAL"):
        flags.append(
            f"⚠️ **regime = {defense.get('severity')}** — 见 section 1 详情。"
        )

    if not flags:
        return ""  # 无红旗时整 section 不显示

    return "#### 4. 红旗\n" + "\n".join(flags) + "\n"


# ────────────────────────────────────────────────────────
# Section 5: 今天必须做的动作
# ────────────────────────────────────────────────────────

def section_actions(trade_delta: dict | None, defense: dict | None,
                    quality_status: str | None = None) -> str:
    """0-3 条具体动作。原则：越少越好；多了说明系统在乱报。"""
    lines = ["#### 5. 今天必须做的动作"]
    actions: list[str] = []

    weekday = date.today().weekday()  # 0 = 周一
    is_monday = (weekday == 0)
    if quality_status == "FAIL":
        actions.append("🔴 **数据质量闸门 = FAIL：今天暂停买入/加仓/调仓**，只读观察，先修复 FAIL 项")
        lines.extend(actions)
        return "\n".join(lines) + "\n"

    # regime CRITICAL = 任何动作都让位（HIGH 时仅减仓不加仓，不阻断 rebalance 卖出）
    sev = defense.get("severity") if defense else None
    if sev == "CRITICAL":
        actions.append("🔴 **regime = CRITICAL，今天清仓 sit out**，等信号转回 LOW 再回来")
        lines.extend(actions)
        return "\n".join(lines) + "\n"
    if sev == "HIGH":
        actions.append('🟠 **regime = HIGH：今天只减仓不加仓**（rebalance 的「卖」照做，「买」暂停）')

    # 周一 rebalance
    if is_monday and trade_delta:
        sells = trade_delta.get("sells") or []
        buys = [] if sev == "HIGH" else (trade_delta.get("buys") or [])
        if sells or buys:
            actions.append(f"**周一 rebalance**：卖 {len(sells)} 只 / 买 {len(buys)} 只")
            for s in sells[:3]:
                actions.append(
                    f"• 卖 **{s.get('ticker')}** {s.get('name','')} "
                    f"(当前 {s.get('current_weight', 0)*100:.0f}%)"
                )
            for b in buys[:3]:
                actions.append(
                    f"• 买 **{b.get('ticker')}** "
                    f"目标 {b.get('v6_weight', 0)*100:.0f}% (≈¥{b.get('amount_rmb', 0):.0f})"
                )
            if len(sells) + len(buys) > 6:
                actions.append("• … 完整清单见 trade_delta.json")

    if not actions:
        actions.append("无 — 系统建议你今天不动手")

    lines.extend(actions)
    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────
# 头部 + 拼装
# ────────────────────────────────────────────────────────

def build_brief(share_mode: bool = False) -> str:
    """读所有数据源 + 拼装 markdown，返回 brief 文本。

    Args:
      share_mode: True 时为"共享版" — section 5 替换为脱敏提示。
                  当前默认 webhook 走完整版，share_mode 留作以后切换余地。

    格式约定（适配飞书 interactive card 渲染）：
      - 不再写"# 早安 · 日期"标题（卡片 header 已包含日期）
      - section 标题用 #### (H4)，飞书不会降级渲染
      - ticker 用 **加粗** 而非反引号（反引号在飞书会留下字符）
      - section 间用空行分隔，不用 `---`（飞书会渲染成粗水平线）
      - 红旗 section 为空时整段省略
      - 免责声明放最末一行（每天看一遍即可）
    """
    plan = _load_us_plan()
    trade_delta = _load_json(REPO / "data" / "latest" / "trade_delta.json")
    risk_metrics = _load_json(REPO / "data" / "latest" / "risk_metrics.json")
    factor_scores = _load_json(REPO / "data" / "latest" / "factor_scores_today.json")
    events = _load_json(REPO / "data" / "event_calendar.json")
    a_share_picks = _load_a_share_picks()
    hk_picks = _load_hk_picks()
    defense = _latest_defense_snapshot()
    history = _load_history()
    qgate_payload = _quality_gate_payload()
    acceptance_payload = _acceptance_payload()
    qgate_status = str((qgate_payload or {}).get("status") or "UNKNOWN")
    trade_blocked = qgate_status == "FAIL"
    read_only = trade_blocked

    parts: list[str] = []
    cal = section_calendar(plan)
    if cal:
        parts.extend([cal, "\n"])
    parts.extend([
        section_regime(defense, qgate_payload, acceptance_payload),
        "\n",
    ])
    qgate = section_quality_gate()
    if qgate:
        parts.extend([qgate, "\n"])
    source_health = section_source_health()
    if source_health:
        parts.extend([source_health, "\n"])
    evidence = section_evidence()
    if evidence:
        parts.extend([evidence, "\n"])
    # 1.5 持仓止损告警（无告警时整段省略，避免空版面）
    stoploss_warn = section_holdings_stoploss(history)
    if stoploss_warn:
        parts.extend([stoploss_warn, "\n"])
    parts.extend([
        section_picks(plan, a_share_picks, hk_picks=hk_picks, history=history,
                      factor_scores=factor_scores, read_only=read_only),
        "\n",
    ])
    # 📉 跌出 Top — 上批次在 picks、本批次跌出（含持仓警示）
    dropouts_md = section_dropouts()
    if dropouts_md:
        parts.extend([dropouts_md, "\n"])
    # A 股被拒 top 20 — 审计约束器是否过严（无 a_share_picks 时整段省略）
    rejected_md = section_rejected_a_share(a_share_picks)
    if rejected_md:
        parts.extend([rejected_md, "\n"])
    # 周一专属:命中率回顾(评级 + AI 推荐准确度)
    hitrate = section_weekly_hitrate()
    if hitrate:
        parts.extend([hitrate, "\n"])
    # 周一专属:walk-forward OOS 校验展示
    wf_oos = section_walk_forward_oos()
    if wf_oos:
        parts.extend([wf_oos, "\n"])
    parts.extend([
        section_ai_alpha(risk_metrics),
        "\n",
    ])
    # 3.5 组合风格暴露 + Factor Stress（仅在有告警时显示）
    factor_risk = section_factor_risk(plan, factor_scores)
    if factor_risk:
        parts.extend([factor_risk, "\n"])
    # 3.7 次新股触底候选 (junior_stock_watcher 输出 → 列 actionable + diff 标)
    junior_radar_md = section_junior_radar()
    if junior_radar_md:
        parts.extend([junior_radar_md, "\n"])
    red_flags = section_red_flags(plan, events, factor_scores, defense)
    if red_flags:
        parts.append(red_flags)
        parts.append("\n")

    if not share_mode:
        parts.append(section_actions(trade_delta, defense, quality_status=qgate_status))
    else:
        parts.append(
            "#### 5. 今天必须做的动作\n"
            "_共享版隐藏个人调仓明细。请参考自己持仓 + section 2 建议组合自行决策。_\n"
        )

    parts.append(
        "\n———\n"
        "⚠️ 不构成投资建议。崩盘期历史 alpha = -9.77%（4/4 regime 3 跑输 SPY）。"
    )
    return "".join(parts)


# ────────────────────────────────────────────────────────
# 飞书推送 — 优先 lark-cli (hermes)，回退 webhook
# ────────────────────────────────────────────────────────

def _lark_cli_send(brief: str, target_kind: str, target_id: str) -> bool:
    """单次推送：通过 lark-cli 把 brief 发到 user_id 或 chat_id。

    必须用 bot identity：user → 自己 user_id 会进 user_count=0 的孤儿 P2P，
    飞书客户端不会显示。bot identity 走"机器人 → 用户/群"路径才会正常出现。
    """
    import shutil
    import subprocess

    if shutil.which("lark-cli") is None:
        return False
    if not target_id:
        return False

    today = date.today().strftime("%Y-%m-%d")
    payload = f"📊 早安简报 · {today}\n\n{brief}"

    cmd = ["lark-cli", "--as", "bot", "im", "+messages-send", "--markdown", payload]
    if target_kind == "user":
        cmd.extend(["--user-id", target_id])
    elif target_kind == "chat":
        cmd.extend(["--chat-id", target_id])
    else:
        return False

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True
        logger.warning(
            f"lark-cli 推送失败 ({target_kind}={target_id}, rc={r.returncode}): "
            f"{r.stderr[:300]}"
        )
        return False
    except Exception as e:
        logger.warning(f"lark-cli 推送异常: {e}")
        return False


def _push_via_lark_cli(brief_personal: str, brief_shared: str) -> list[str]:
    """双通道推送：自己 P2P 完整版 + 群共享版（已脱敏）。

    Args:
      brief_personal: 完整版 brief（含 section 5 个人调仓）
      brief_shared:   共享版 brief（section 5 已脱敏，去掉 ¥ 仓位）

    Returns:
      已成功推送的通道列表，例如 ["user", "chat"]。
    """
    sent: list[str] = []
    user_id = os.environ.get("FEISHU_BRIEF_USER_ID", "").strip()
    chat_id = os.environ.get("FEISHU_BRIEF_CHAT_ID", "").strip()

    if user_id and _lark_cli_send(brief_personal, "user", user_id):
        sent.append("user")
    if chat_id and _lark_cli_send(brief_shared, "chat", chat_id):
        sent.append("chat")
    return sent


def _color_block(bg: str, elements: list[dict]) -> dict:
    """v1 schema 把任意 elements 包进一个 column_set 色块。

    bg 取 "default"（无色）/ "grey"（浅灰）/ saturated 颜色名（wathet/violet/turquoise/...）。
    v1 没有真正的 pastel 浅色，所以默认用 default 干净版；section 5 调仓建议
    可以单独用 "grey" 轻分块。
    """
    block: dict = {
        "tag": "column_set",
        "flex_mode": "none",
        "horizontal_spacing": "default",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": elements}
        ],
    }
    if bg and bg != "default":
        block["background_style"] = bg
    return block


def _kpi_row(items: list[tuple[str, str]]) -> dict:
    """3 列（或 N 列）均分横排 KPI: items 是 [(label, value), ...]。"""
    return {
        "tag": "column_set",
        "flex_mode": "stretch",
        "horizontal_spacing": "default",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": f"**{label}**\n{value}"}}],
            }
            for label, value in items
        ],
    }


def _two_col_lines(lines_left: list[str], lines_right: list[str]) -> dict:
    """美股 12 只之类长列表拆 2 列横排。"""
    return {
        "tag": "column_set",
        "flex_mode": "stretch",
        "horizontal_spacing": "default",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1,
             "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines_left)}}]},
            {"tag": "column", "width": "weighted", "weight": 1,
             "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines_right)}}]},
        ],
    }


def _hitrate_card_lines(today: date) -> list[str]:
    """飞书卡片专用：近 7 天 reviews 分档 + 近 30 天 discovery alpha → bullet 行。

    复用 section_weekly_hitrate() 的两条 SQL；卡片 lark_md 不擅长渲染 markdown 表格，
    所以这里输出更紧凑的"｜"分隔行（KPI 风格）。
    """
    try:
        import sys
        import os
        _repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        sys.path.insert(0, os.path.join(_repo, "scripts", "lib"))
        import stock_db
        conn = stock_db.get_db(read_only=True)
    except Exception:
        return []
    lines: list[str] = []
    # 2026-05-21 V1 cutover：V1 reviews / discovery_tracking 表已删
    # V2 替代：pick_outcomes 按 horizon 聚合
    try:
        rows = conn.execute(
            """
            SELECT po.horizon, COUNT(*) n,
                   ROUND(AVG(po.return_pct), 2) avg_ret,
                   ROUND(AVG(po.alpha_pct), 2) avg_alpha,
                   ROUND(SUM(CASE WHEN po.is_success THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 0) win
            FROM pick_outcomes po
            JOIN recommendation_runs rr ON rr.run_id = po.run_id
            WHERE po.outcome_date >= ?
              AND po.alpha_pct IS NOT NULL
              AND rr.universe_scope = 'system_tech_universe'
              AND rr.run_date >= ?
              AND rr.strategy_version = (
                SELECT strategy_version
                FROM recommendation_runs
                WHERE universe_scope = 'system_tech_universe' AND status = 'generated'
                ORDER BY generated_at DESC LIMIT 1
              )
            GROUP BY po.horizon ORDER BY po.horizon
            """,
            [today - timedelta(days=30), PRODUCTION_METRICS_START_DATE],
        ).fetchall()
        if rows:
            lines.append(f"**V2 推荐 alpha（近 30 天成熟样本，自 {PRODUCTION_METRICS_START_DATE} 起）**")
            for h, n, ret, alpha, win in rows:
                sr = "+" if (ret or 0) >= 0 else ""
                sa = "+" if (alpha or 0) >= 0 else ""
                lines.append(f"• {h} ｜ n={n} ｜ 涨幅 {sr}{ret}% ｜ alpha {sa}{alpha}% ｜ 胜 {int(win or 0)}%")
        else:
            lines.append("_pick_outcomes 近 30 天暂无成熟样本_")
    except Exception as e:
        lines.append(f"_pick_outcomes 查询失败: {e}_")
    conn.close()
    return lines


def _build_card_payload() -> dict:
    """构造飞书 card v1 schema dict — 每个 section 上色块 + 横排 KPI + 长列表分 2 列。

    每个 section 用 column_set + background_style 包装：
      regime  → 动态色（NONE blue / LOW yellow / HIGH orange / CRITICAL red；与 defense_signals 4 档对齐）
      建议组合 → wathet 浅蓝（专业凉爽）
      AI alpha → violet 紫（数据回顾）
      红旗    → carmine 红粉（警示，仅非空时）
      调仓    → turquoise 青绿（最醒目 · 用户最关心的"今天做什么"）
    """
    plan = _load_us_plan()
    trade_delta = _load_json(REPO / "data" / "latest" / "trade_delta.json")
    risk_metrics = _load_json(REPO / "data" / "latest" / "risk_metrics.json")
    factor_scores = _load_json(REPO / "data" / "latest" / "factor_scores_today.json")
    events = _load_json(REPO / "data" / "event_calendar.json")
    policy_events = _load_json(REPO / "data" / "policy_events.json")
    a_share_picks = _load_a_share_picks()
    hk_picks = _load_json(REPO / "data" / "latest" / "hk_picks.json")
    defense = _latest_defense_snapshot()
    history = _load_history()
    qgate_payload = _quality_gate_payload()
    acceptance_payload = _acceptance_payload()
    trade_blocked = str((qgate_payload or {}).get("status") or "").upper() == "FAIL"

    today = date.today()
    weekday_cn = "一二三四五六日"[today.weekday()]
    # 三道闸门取最严：defense + 质量闸门 + 生产验收
    severity, severity_icon, header_template, regime_reasons = _combined_severity(
        defense, qgate_payload, acceptance_payload)
    defense_sev = (defense or {}).get("severity", "UNKNOWN")
    qgate_status = str((qgate_payload or {}).get("status") or "—").upper()
    accept_status = str((acceptance_payload or {}).get("status") or "—").upper()
    _status_badge = lambda s: {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}.get(s, "·") + " " + s

    blocks: list[dict] = []

    # ─── Section 0: 今天 / 3 天内会发生什么（持仓 earnings + 高相关政策）───
    horizon = today + timedelta(days=3)
    held_codes: set[str] = set()
    if plan:
        for e in (plan.get("plan_v5") or []):
            t = e.get("ticker", "")
            if _is_a_share(t):
                held_codes.add(t.split(".")[0])
    earnings_soon: list[tuple[date, dict]] = []
    for ev in (events or {}).get("events", []) or []:
        ed_p = _safe_parse_date(ev.get("event_date", ""))
        if ed_p and today <= ed_p <= horizon and ev.get("code", "") in held_codes:
            earnings_soon.append((ed_p, ev))
    policy_soon: list[tuple[date, dict]] = []
    for ev in (policy_events or {}).get("events", []) or []:
        if (ev.get("relevance_score") or 0) < 4:
            continue
        ed_p = _safe_parse_date(ev.get("date", ""))
        if ed_p and today <= ed_p <= horizon:
            policy_soon.append((ed_p, ev))
    if earnings_soon or policy_soon:
        def _day_label(d: date) -> str:
            if d == today:
                return "🔥今天"
            if d == today + timedelta(days=1):
                return "明天"
            return d.strftime("%m-%d")
        sec0_lines: list[str] = ["**📅 今天 / 3 天内会发生什么**"]
        if earnings_soon:
            sec0_lines.append(f"**持仓事件**（{len(earnings_soon)} 个 earnings）")
            for ed, ev in sorted(earnings_soon)[:5]:
                sec0_lines.append(f"• {_day_label(ed)} · {ev.get('code')} · {ev.get('description','')[:60]}")
        if policy_soon:
            if earnings_soon:
                sec0_lines.append("")
            sec0_lines.append(f"**高相关政策**（{len(policy_soon)} 条 · relevance≥4）")
            for ed, ev in sorted(policy_soon, key=lambda x: (-int(x[1].get('relevance_score') or 0), x[0]))[:3]:
                themes = "/".join(ev.get('matched_themes') or [])
                theme_str = f" · {themes}" if themes else ""
                sec0_lines.append(f"• {_day_label(ed)}{theme_str} · {ev.get('title','')[:60]}")
        blocks.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(sec0_lines)}})
        blocks.append({"tag": "hr"})

    # ─── Section 1: regime（白底；三道闸门真实状态 + 升档理由）───
    advice = {
        "NONE": "👉 **今天可以正常调仓**（三道闸门都没亮灯）",
        "LOW": "👉 **留意别加仓**，单笔不超 5% 仓位；先看升档理由",
        "HIGH": "👉 **只读不交易 / 减仓 30-50%**，先修验收或闸门里的 FAIL 项",
        "CRITICAL": "👉 **清仓 sit out** — 崩盘期 alpha = -9.77%，等灯转回 LOW 再回来",
    }.get(severity, "👉 保守按已有计划执行")
    sec1_lines = [
        f"{severity_icon} **regime = {severity}** — {advice}",
        "",
        f"**三道闸门状态**：防御 {defense_sev} · 质量闸门 {_status_badge(qgate_status)} · 生产验收 {_status_badge(accept_status)}",
        "",
        "📖 **灯色规则（取最严）**：🟢 三道全绿 ｜ 🟡 任一 WARN ｜ 🟠 任一 FAIL 或防御 HIGH ｜ 🔴 防御 CRITICAL",
        "🛡️ **三道闸门含义**：防御=VIX/200MA/单股止损 ｜ 质量闸门=picks/factor 完整性 ｜ 生产验收=今日 pipeline 是否真跑通",
    ]
    if regime_reasons:
        sec1_lines.append("")
        sec1_lines.append(f"**为什么不是 🟢**（{len(regime_reasons)} 条升档理由）：")
        for r in regime_reasons:
            sec1_lines.append(f"• {r}")
    blocks.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(sec1_lines)}})
    blocks.append({"tag": "hr"})

    # 质量闸门完整 issues（reasons 已在上面摘要，这里只在 FAIL 时再单列建议）
    qgate_lines = _quality_gate_lines()
    if qgate_lines and qgate_status == "FAIL":
        blocks.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(qgate_lines)}})
        blocks.append({"tag": "hr"})
    source_health_md = section_source_health()
    if source_health_md:
        blocks.append({"tag": "div", "text": {"tag": "lark_md", "content": source_health_md.replace("#### ", "")}})
        blocks.append({"tag": "hr"})
    evidence_lines = _evidence_lines()
    if evidence_lines:
        blocks.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(evidence_lines)}})
        blocks.append({"tag": "hr"})

    # ─── Section 2: 🔝 AI 推荐与模型组合（三线独立 · 白底）───
    if plan or hk_picks or a_share_picks:
        section2: list[dict] = [
            {"tag": "div", "text": {"tag": "lark_md", "content":
                "**🔝 AI 推荐与模型组合（三线独立）**\n"
                "_系统科技/AI 股票池推荐；自选股池不自动混入_"
                + ("\n🔴 **质量闸门 FAIL：本区只读观察，不作为买入/加仓清单。**" if trade_blocked else "")}}
        ]
        if plan:
            pm = plan.get("portfolio_metrics") or {}
            if pm:
                weight_src = _plan_weight_source(plan)
                section2.append(_kpi_row([
                    ("回测Sharpe", str(pm.get("annual_sharpe", "?"))),
                    ("回测年化", f"{pm.get('annual_return_pct', '?')}%"),
                    ("仓位来源", weight_src["kind"]),
                ]))

        # 🇺🇸 美股 — 每只股聚合多行 (ticker + ✅/⚠️ reason)，再 2 列拆分
        plan_v5 = (plan or {}).get("plan_v5") or []
        us_blocks = _humanize_picks_grouped(plan_v5, a_share=False, history=history,
                                            factor_scores=factor_scores) if plan else []
        if us_blocks:
            ts_us = _fmt_ts((plan or {}).get("generated_at"))
            weight_src = _plan_weight_source(plan)
            section2.append({"tag": "div", "text": {"tag": "lark_md",
                "content": (
                    f"**🇺🇸 美股 ({len(us_blocks)} 只 · {weight_src['label']})** · {ts_us}\n"
                    "_每只股附 ✅ 推荐理由 + ⚠️ 风险点_"
                    + (f"\n⚠️ {weight_src['detail']}。这些百分比不是新鲜 risk-aware optimizer 输出。"
                       if weight_src.get("is_fallback") else "")
                )}})
            half = (len(us_blocks) + 1) // 2
            # 块间用空行分隔，避免上下两只股的 reasons 粘连
            section2.append(_two_col_lines(
                ["\n".join(["", b]) if i > 0 else b for i, b in enumerate(us_blocks[:half])],
                ["\n".join(["", b]) if i > 0 else b for i, b in enumerate(us_blocks[half:])],
            ))

        # 🇭🇰 港股 — 每只股聚合 ticker + reasons
        if hk_picks and hk_picks.get("selected"):
            hk_sel = hk_picks["selected"][:10]
            hk_blocks: list[str] = []
            for e in hk_sel:
                t = e.get("code", "?")
                spark, pct60 = _ticker_sparkline(history or {}, t)
                spark_str = f" · {spark}" + (f" {pct60:+.1f}% 60d" if pct60 is not None else "")
                f_score = _entry_f_score(e)
                f_str = f" · F-Score {_format_f_score(f_score)}"
                head = f"• **{t}** {e.get('name','')} · {e.get('composite', 0):.2f}{f_str}{spark_str}"
                pros, cons = _build_hk_reasons(e)
                rl = _format_reason_lines(pros, cons)
                block_parts = [head] + _ticker_signal_lines(t) + rl
                hk_blocks.append("\n".join(block_parts))
            ts_hk = _fmt_ts(hk_picks.get("generated_at"))
            section2.append({"tag": "div", "text": {"tag": "lark_md",
                "content": f"**🇭🇰 港股 ({len(hk_sel)} 只)** · {ts_hk}\n_每只股附 ✅ 推荐理由 + ⚠️ 风险点_"}})
            if len(hk_blocks) >= 4:
                half_h = (len(hk_blocks) + 1) // 2
                section2.append(_two_col_lines(
                    ["\n".join(["", b]) if i > 0 else b for i, b in enumerate(hk_blocks[:half_h])],
                    ["\n".join(["", b]) if i > 0 else b for i, b in enumerate(hk_blocks[half_h:])],
                ))
            else:
                section2.append({"tag": "div", "text": {"tag": "lark_md",
                    "content": "\n\n".join(hk_blocks)}})

        # 🇨🇳 A 股 — 每只股聚合 ticker + reasons
        if not _a_share_enabled():
            section2.append({"tag": "div", "text": {"tag": "lark_md",
                "content": (
                    "**🇨🇳 A 股** — 生产推荐未启用：缺少已验证的 A 股 IC 校准权重；"
                    "当前只保留研究观察，不进入调仓清单"
                )}})
        elif a_share_picks and a_share_picks.get("selected"):
            sel = a_share_picks["selected"][:10]
            a_blocks: list[str] = []
            for e in sel:
                t = e.get("ticker", e.get("code", "?"))
                spark, pct60 = _ticker_sparkline(history or {}, t)
                spark_str = f" · {spark}" + (f" {pct60:+.1f}% 60d" if pct60 is not None else "")
                f_score = _entry_f_score(e)
                f_str = f" · F-Score {_format_f_score(f_score)}"
                head = f"• **{t}** {e.get('name','')} · {e.get('composite', 0):.2f}{f_str}{spark_str}"
                pros, cons = _build_a_share_reasons(e)
                rl = _format_reason_lines(pros, cons)
                block_parts = [head] + _ticker_signal_lines(t) + rl
                a_blocks.append("\n".join(block_parts))
            ts_cn = _fmt_ts(a_share_picks.get("generated_at"))
            section2.append({"tag": "div", "text": {"tag": "lark_md",
                "content": f"**🇨🇳 A 股 ({len(sel)} 只)** · {ts_cn}\n_每只股附 ✅ 推荐理由 + ⚠️ 风险点_"}})
            if len(a_blocks) >= 4:
                half_a = (len(a_blocks) + 1) // 2
                section2.append(_two_col_lines(
                    ["\n".join(["", b]) if i > 0 else b for i, b in enumerate(a_blocks[:half_a])],
                    ["\n".join(["", b]) if i > 0 else b for i, b in enumerate(a_blocks[half_a:])],
                ))
            else:
                section2.append({"tag": "div", "text": {"tag": "lark_md",
                    "content": "\n\n".join(a_blocks)}})
        elif plan:
            a_blocks_pre = _humanize_picks_grouped(plan_v5, a_share=True, history=history)
            if a_blocks_pre:
                section2.append({"tag": "div", "text": {"tag": "lark_md",
                    "content": f"**🇨🇳 A 股 ({len(a_blocks_pre)} 只 · 盘前 · 16:30 后更准)**"}})
                if len(a_blocks_pre) >= 4:
                    half_a = (len(a_blocks_pre) + 1) // 2
                    section2.append(_two_col_lines(a_blocks_pre[:half_a], a_blocks_pre[half_a:]))
                else:
                    section2.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": "\n\n".join(a_blocks_pre)}})
        # 趋势图例 — note 元素（灰色小字，参照"灯色对照"的展示模式）
        section2.append({"tag": "note", "elements": [
            {"tag": "plain_text", "content": (
                "📖 60d 趋势图例：🚀 ≥+50% 飙涨 ｜ 📈 +15~50% 强涨 ｜ ↗️ +3~15% 小涨 ｜ "
                "➡️ ±3% 横盘 ｜ ↘️ -3~-15% 小跌 ｜ 📉 -15~-50% 强跌 ｜ 💀 ≤-50% 暴跌"
            )}
        ]})
        blocks.extend(section2)
        blocks.append({"tag": "hr"})

    # ─── Section 2.5: A 股被拒 top 20（审计约束器是否过严）───
    rejected_pool = _rejected_a_share_entries(a_share_picks, top_n=20)
    if rejected_pool:
        rej_blocks: list[str] = []
        for e in rejected_pool:
            code = e.get("code", "?")
            name = e.get("name", "")
            comp = float(e.get("composite") or 0)
            industry = e.get("industry", "")
            ind_str = f" [{industry}]" if industry else ""
            head = f"• **{code}** {name}{ind_str} · 综合 {comp:.3f}"
            pros, cons = _build_a_share_reasons(e)
            reject_lines: list[str] = []
            for br in (e.get("block_reasons") or [])[:2]:
                if br:
                    reject_lines.append(f"  ❌ 约束器：{br}")
            for rf in (e.get("risk_flags") or [])[:1]:
                if rf:
                    reject_lines.append(f"  ⚠️ 红旗：{rf}")
            if not reject_lines:
                for c in cons[:2]:
                    reject_lines.append(f"  ⚠️ {c}")
            if pros:
                reject_lines.append(f"  ✅ {pros[0]}（综合分高的原因）")
            if not reject_lines:
                tradable = e.get("tradable", True)
                reject_lines.append(f"  ❌ {'不可买' if not tradable else '分数未达 cutoff'}")
            rej_blocks.append("\n".join([head] + reject_lines))

        blocks.append({"tag": "div", "text": {"tag": "lark_md", "content":
            f"**🚫 A 股候选但被拒 · top {len(rej_blocks)}**\n"
            "_审计用：好票频繁出现在这里 → 约束器可能过严，可对照原因调阈值_"}})
        if len(rej_blocks) >= 4:
            half_r = (len(rej_blocks) + 1) // 2
            blocks.append(_two_col_lines(
                ["\n".join(["", b]) if i > 0 else b for i, b in enumerate(rej_blocks[:half_r])],
                ["\n".join(["", b]) if i > 0 else b for i, b in enumerate(rej_blocks[half_r:])],
            ))
        else:
            blocks.append({"tag": "div", "text": {"tag": "lark_md",
                "content": "\n\n".join(rej_blocks)}})
        blocks.append({"tag": "hr"})

    # ─── Section 3: 两个方案对比（核心 — 让新人一眼看懂"AI 有没有用"）───
    inception_date = date(2026, 5, 10)
    days_tracked = max(0, (today - inception_date).days - 1)

    section3: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content":
            "**🆚 系统在跑两个方案**\n_每周一同时跑两套策略，让数据自然分胜负_"}}
    ]
    # 2 列横向对比卡：A 静态 vs C 动态
    section3.append({
        "tag": "column_set",
        "flex_mode": "stretch",
        "horizontal_spacing": "default",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1,
             "elements": [{"tag": "div", "text": {"tag": "lark_md", "content":
                "**📦 方案 A · 静态死守**\n5-10 锁定 12 只股\n从此不调仓\n_模拟「佛系投资者」_"}}]},
            {"tag": "column", "width": "weighted", "weight": 1,
             "elements": [{"tag": "div", "text": {"tag": "lark_md", "content":
                "**🔄 方案 C · 动态调仓**\n每周一按 AI rebalance\n扣 10bps/换股 手续费\n_模拟「听 AI 调仓」_"}}]},
        ],
    })
    if days_tracked < 7:
        section3.append({"tag": "div", "text": {"tag": "lark_md", "content":
            f"📅 **Forward tracking 累积中**：已 {days_tracked} / 7 天 — 等下周起每周一较量\n"
            f"🆚 **C − A spread = AI 加的 alpha**（等数据累积）"}})
    else:
        section3.append({"tag": "div", "text": {"tag": "lark_md", "content":
            f"📅 已 forward tracked {days_tracked} 天 — 真实曲线见 dashboard"}})

    if risk_metrics:
        rm = risk_metrics
        section3.append({"tag": "div", "text": {"tag": "lark_md", "content":
            "⚠️ **历史回测/模拟，不是实盘业绩；forward 样本仍很短，不能证明策略有效。**"}})
        # NAV 净值趋势 — emoji 双时间窗（近 30d + 总累计）
        nav = _nav_sparkline(rm)
        if nav:
            section3.append({"tag": "div", "text": {"tag": "lark_md", "content":
                f"历史 NAV：近 30 天 {nav['spark_30d']} {nav['pct_30d']:+.1f}% · "
                f"累计 {nav['spark']} {nav['total_pct']:+.1f}%\n"
                f"_{nav['start_date']} → {nav['end_date']} ({nav['n_days']} 天)_"}})
        section3.append(_kpi_row([
            ("回测 Sharpe", _fmt_metric(rm.get("sharpe"))),
            ("Max DD", _fmt_metric(rm.get("max_drawdown_pct"), "%")),
            ("95% VaR", _fmt_metric(rm.get("var_95_pct"), "%")),
        ]))
        section3.append({"tag": "note", "elements": [
            {"tag": "plain_text", "content": "回测含 survivorship bias 不代表未来；崩盘期实测 alpha = -9.77%，4/4 regime 3 跑输 SPY。"}
        ]})
    blocks.extend(section3)

    # ─── Section 3.7: 🎯 次新股触底候选 (junior_stock_watcher → 列 actionable + diff) ───
    radar = _load_junior_radar()
    if radar:
        markets = radar.get("markets") or {}
        radar_lines: list[str] = []
        any_actionable = False
        for mk, label in [("us", "🇺🇸 美股"), ("cn", "🇨🇳 A 股")]:
            pool = (markets.get(mk) or {}).get("junior_pool") or []
            actionable = [x for x in pool if x.get("tier") in ("可小仓试探", "可研究")]
            if not actionable:
                continue
            any_actionable = True
            actionable.sort(key=lambda x: (
                0 if x.get("tier") == "可小仓试探" else 1,
                x.get("percentile") or 999,
            ))
            radar_lines.append(f"**{label}** ({len(actionable)} 只 actionable)")
            for x in actionable[:3]:
                code = x.get("symbol") or x.get("code")
                name = (x.get("name") or "")[:24]
                te = "🟢" if x.get("tier") == "可小仓试探" else "🟡"
                diff = _diff_emoji(x.get("diff_flag"))
                pct = x.get("percentile")
                pct_str = f"前 {pct}%" if pct is not None else ""
                ready = x.get("readiness_score")
                ready_str = f" · 准备度 {int(ready)}" if ready is not None else ""
                radar_lines.append(f"  {te} **{code}** {name} {diff} · {pct_str}{ready_str}")
        if any_actionable:
            radar_lines.append("")
            radar_lines.append("_仅作研究起点,非买入建议; 完整列表 + 买前审查卡见 dashboard。_")
            blocks.append({"tag": "div", "text": {"tag": "lark_md",
                "content": "**🎯 次新股触底候选**\n" + "\n".join(radar_lines)}})
            blocks.append({"tag": "hr"})

    # ─── Section 4: 红旗（仅非空时，唯一例外用色块强警示）───
    flags: list[str] = []
    if factor_scores:
        try:
            scored_date = factor_scores.get("date")
            if scored_date:
                d = datetime.strptime(scored_date, "%Y-%m-%d").date()
                age = (today - d).days
                if age >= 2:
                    flags.append(f"⚠️ 因子分数过期 {age} 天（{scored_date}）")
        except Exception:
            pass
    if plan and events:
        held_tickers = {e.get("ticker", "") for e in (plan.get("plan_v5") or [])}
        held_codes = {t.split(".")[0] for t in held_tickers if _is_a_share(t)}
        soon = today + timedelta(days=3)
        urgent = [
            ev for ev in (events.get("events") or [])
            if ev.get("code", "") in held_codes
            and (_safe_parse_date(ev.get("event_date", "")) is not None
                 and today <= _safe_parse_date(ev.get("event_date", "")) <= soon)
        ]
        if urgent:
            flags.append(f"⚠️ 持仓 3 天内有 {len(urgent)} 个事件")
    if defense and severity in ("HIGH", "CRITICAL"):
        flags.append(f"⚠️ regime = {severity}（见上方）")
    if flags:
        blocks.append({"tag": "hr"})
        blocks.append(_color_block("carmine", [
            {"tag": "div", "text": {"tag": "lark_md",
                "content": "**🚨 红旗**\n" + "\n".join(f"• {f}" for f in flags)}}
        ]))

    blocks.append({"tag": "hr"})

    # ─── Section 4.5: 上周回顾（周一专属 · AI 准不准）───
    if today.weekday() == 0:
        hitrate_lines = _hitrate_card_lines(today)
        if hitrate_lines:
            blocks.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content":
                    "**🧪 上周回顾 · AI 准不准（周一专属）**\n" + "\n".join(hitrate_lines)}
            })
            blocks.append({"tag": "hr"})

    # ─── Section 5: 今天必须做的动作（卖/买左右两列）───
    is_monday = (today.weekday() == 0)
    if trade_blocked:
        blocks.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                "content": "**✅ 今天必须做的动作**\n🔴 **数据质量闸门 FAIL：暂停买入/加仓/调仓**，先修复 FAIL 项"}
        })
    elif severity == "HIGH":
        blocks.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                "content": "**✅ 今天必须做的动作**\n🟠 **regime HIGH：只减仓不加仓**，暂停所有买入"}
        })
    elif is_monday and trade_delta and ((trade_delta.get("sells") or []) or (trade_delta.get("buys") or [])):
        sells = trade_delta.get("sells") or []
        buys = trade_delta.get("buys") or []
        blocks.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                "content": f"**✅ 今天必须做的动作**\n**周一 rebalance**：卖 {len(sells)} 只 / 买 {len(buys)} 只"}
        })
        sell_lines: list[str] = []
        for s in sells:
            name = s.get("name", "")
            name_str = f" {name}" if name and name != s.get("ticker", "") else ""
            sell_lines.append(
                f"• 卖 **{s.get('ticker')}**{name_str} ({s.get('current_weight', 0)*100:.0f}%, ¥{s.get('current_amount', 0):.0f})"
            )
        buy_lines: list[str] = []
        for b in buys:
            buy_lines.append(
                f"• 买 **{b.get('ticker')}** {b.get('v6_weight', 0)*100:.1f}% (≈¥{b.get('amount_rmb', 0):.0f})"
            )
        # 卖在左、买在右；行数不同时短的一边自动留白
        blocks.append(_two_col_lines(sell_lines or ["• 无卖出"], buy_lines or ["• 无买入"]))
    else:
        blocks.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**✅ 今天必须做的动作**\n无 — 系统建议你今天不动手"}
        })

    # ─── 底部注脚 ───
    blocks.append({"tag": "note", "elements": [
        {"tag": "plain_text", "content": "⚠️ 不构成投资建议 · 崩盘期历史 alpha = -9.77%（4/4 regime 3 跑输 SPY）"}
    ]})

    n_picks = len(plan.get("plan_v5") or []) if plan else 0
    n_actions = (len(trade_delta.get("sells", [])) + len(trade_delta.get("buys", []))) if trade_delta else 0
    subtitle = f"regime {severity} · {n_picks} 只候选 · 调仓 {n_actions} 笔"

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"📊 早安简报 · {today} (周{weekday_cn})"},
                "subtitle": {"tag": "plain_text", "content": subtitle},
                "template": header_template,
            },
            "elements": blocks,
        },
    }


def _safe_parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def _push_via_webhook() -> bool:
    """群机器人 webhook 推送 — 走结构化 card v1 schema。

    适用于跨租户 external 群 — lark-cli bot 无法进 external 群，但自定义
    机器人 webhook 不受限制。payload 由 _build_card_payload() 独立构造，
    所以本函数不再接受 brief markdown 参数。
    """
    webhook = os.environ.get("FEISHU_BRIEF_WEBHOOK", "").strip()
    if not webhook:
        return False
    payload = _build_card_payload()
    try:
        r = requests.post(webhook, json=payload, timeout=15)
        ok = r.status_code == 200 and r.json().get("StatusCode", 0) == 0
        if not ok:
            logger.warning(f"webhook 推送返回非成功: {r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:
        logger.warning(f"webhook 推送异常: {e}")
        return False


def push_to_feishu(brief_personal: str, brief_shared: str) -> list[str]:
    """统一推送入口 — 双通道（个人 P2P + 群 webhook）。

    Returns:
      已成功推送的通道名字列表，例如 ["user", "chat", "webhook"]。
    """
    sent = _push_via_lark_cli(brief_personal, brief_shared)
    # webhook 走结构化 card payload（与 brief markdown 解耦，独立构造）
    if _push_via_webhook():
        sent.append("webhook")
    return sent


# ────────────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Build and optionally push the morning brief.")
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Only generate local brief files; used while the pipeline is still running.",
    )
    args = parser.parse_args()

    brief_personal = build_brief(share_mode=False)
    brief_shared = build_brief(share_mode=True)

    today = date.today().strftime("%Y-%m-%d")
    reports_dir = REPO / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    archive_path = reports_dir / f"morning_brief_{today}.md"
    archive_path.write_text(brief_personal, encoding="utf-8")

    latest_path = REPO / "morning_brief.md"
    latest_path.write_text(brief_personal, encoding="utf-8")

    logger.info(f"✅ 早安简报已生成: {archive_path}")
    logger.info(f"   最新版镜像:    {latest_path}")
    logger.info(f"   完整版字数: {len(brief_personal)} chars · 共享版字数: {len(brief_shared)} chars")

    if args.no_push:
        logger.info("📨 --no-push：pipeline 仍在验收/收尾，本次只生成本地简报，不推送飞书")
        return 0

    sent = push_to_feishu(brief_personal, brief_shared)
    if sent:
        logger.info(f"📨 已推送到飞书（{', '.join(sent)}）")
    else:
        logger.info("📨 未配置 FEISHU_BRIEF_USER_ID/CHAT_ID/WEBHOOK，跳过推送")

    return 0


if __name__ == "__main__":
    sys.exit(main())
