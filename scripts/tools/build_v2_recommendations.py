#!/usr/bin/env python3
"""Build v2 system-pool recommendations from the clean database.

Inputs:
  system_universe + pool_membership + price_daily

Outputs:
  recommendation_runs + recommendation_picks + portfolio_plans

This script intentionally does not read legacy watchlist/prices/picks tables or
data/latest artifacts. It is the clean v2 path for AI Assistant recommendations.
When the newest same-day price row is a minimal market snapshot, factor fields
are backfilled from the most recent non-null price_daily row for the same symbol.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from stock_research import config  # noqa: E402
from stock_research.core.tech_growth_layers import (  # noqa: E402
    CLASSIFICATION_VERSION,
    classify_tech_growth_layer,
    is_buyable_layer,
)


def _clip(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


_NEGATIVE_VALUATION_SCORE = 27.5  # loss / earnings decline: not "cheap"
_PRICE_ACTION_REVIEW_SCORE_CAP = 59.99
_DATA_USABILITY_REVIEW_SCORE_CAP = 59.99
_DATA_USABILITY_MIN_BUY_SCORE = 70.0
_STALE_SOURCE_DAYS = 10
STRATEGY_VERSION = "tech_ai_v2_usable_data_gate"
MODEL_VERSION = "v2_rule_factor_2026_06_usable_data_gate"
DATA_USABILITY_AUDIT_PATH = REPO / "data" / "latest" / "recommendation_data_usability_audit.json"

EVIDENCE_STATUSES = {"confirmed", "needs_review", "candidate", "stale", "missing"}
BUYABLE_EVIDENCE_STATUS = "confirmed"
P0_US_MARKET = "US"
# P0 身份/资格闸：进入 AI 推荐名单(recommendation_picks)的美股只允许这两档。
# excluded（身份不符，如 VIPS）与 watch_only（仅 ETF/主题、无公司级证据）记入审计 gated_out，
# 不进推荐名单。这是 filter/tag 层的过滤，不改打分公式，也不改 rating(strong_buy/buy) 阈值。
RECOMMENDABLE_US_ELIGIBILITY = {"buyable", "research_only"}
BLOCKING_RISK_CODES = {
    "DATA_USABILITY_REVIEW_GATE",
    "STRUCTURAL_DOWNTREND_REVIEW_GATE",
    "PRICE_ACTION_REVIEW_GATE",
}
WAIT_ENTRY_RISK_CODES = {
    "ACUTE_PRICE_PULLBACK",
    "OVERHEATED_1Y",
}
MARKET_PHASE_ID = "ai_infra_buildout_to_inference"
MARKET_PHASE_NAME = "AI 数据中心建设后半段 + 推理/Agent/企业集成前半段"
MARKET_PHASE_SCOPE = "US/global_tech"


def _score_lower_better(value: Any, good: float, bad: float, missing: float = 30.0) -> float:
    try:
        x = float(value)
    except Exception:
        return missing
    if x <= 0:
        # PE / PEG 的 0 通常是缺失、哨兵值或口径不可用，不能当成"极便宜"。
        return _NEGATIVE_VALUATION_SCORE
    if x <= good:
        return 95.0
    if x >= bad:
        return 20.0
    return _clip(95.0 - (x - good) / (bad - good) * 75.0)


def _score_one_year_momentum(value: Any) -> float:
    # 驼峰评分：50%-150% 区间最优，>200% 反向扣分（追高惩罚）。
    # 参考 stock_research.jobs.calibrate_pick_weights._score_trend_inline 的 IC 实证分档。
    try:
        x = float(value)
    except Exception:
        return 45.0
    if x >= 400:
        return 20.0
    if x >= 200:
        return 50.0
    if x >= 50:
        return 100.0
    if x >= 0:
        return 75.0
    if x >= -35:
        return 50.0
    return 25.0


def _score_reversal(row: dict[str, Any]) -> float:
    """1 月反转分（mean reversion）— 最近 1 月跌得多 → 高分；涨得多 → 低分。

    定义跟 scripts/lib/factor_model.reversal_1m 一致：reversal_raw = -one_month_pct。
    映射：跌 15% → 100；持平 → 50；涨 15% → 0；线性 clip。
    缺数据 → 45（中性偏低，避免无数据撑高分）。

    Why: calibrated_factor_weights.json 的 IC audit 判定 reversal 🟢 strong
    (IC=0.062, hit_rate=72.2%)，是 V2 当前唯一被独立验证的 alpha 来源。
    """
    try:
        x = float(row.get("one_month_pct"))
    except (TypeError, ValueError):
        return 45.0
    return _clip(50.0 - x * (50.0 / 15.0))


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _date_gap_days(newer: Any, older: Any) -> int | None:
    a = _as_date(newer)
    b = _as_date(older)
    if a is None or b is None:
        return None
    return abs((a - b).days)


def _positive_valuation_count(row: dict[str, Any]) -> int:
    count = 0
    for key in ("peg_ratio", "forward_pe", "trailing_pe"):
        value = _as_float(row.get(key))
        if value is not None and value > 0:
            count += 1
    return count


def _data_usability_score(row: dict[str, Any], coverage: float) -> float:
    """Score whether this row is usable for real recommendations.

    Field coverage alone is not enough: a row can have many fields but no real
    valuation source, stale factor snapshots, or no latest price. Those names
    may still be worth researching, but they should not graduate to buy.
    """
    score = coverage * 100.0

    if row.get("close") is None:
        score -= 35.0
    if row.get("market_cap") is None:
        score -= 10.0

    if not row.get("momentum_trade_date"):
        score -= 25.0
    elif (_date_gap_days(row.get("trade_date"), row.get("momentum_trade_date")) or 0) > _STALE_SOURCE_DAYS:
        score -= 15.0

    if not row.get("fundamentals_trade_date"):
        score -= 25.0
    elif (_date_gap_days(row.get("trade_date"), row.get("fundamentals_trade_date")) or 0) > _STALE_SOURCE_DAYS:
        score -= 15.0

    valuation_count = _positive_valuation_count(row)
    if valuation_count == 0:
        score -= 20.0
    elif valuation_count == 1:
        score -= 8.0

    if row.get("one_month_pct") is None and row.get("one_year_pct") is None:
        score -= 10.0

    return _clip(score)


def _load_reversal_weight() -> float:
    """从 calibrated_factor_weights.json 读 reversal 启用状态。

    selected=True → V2 给 reversal 0.15 权重（从 valuation 0.65 切下来）。
    selected=False（IC 失效） → 不用，权重 0（valuation 回到 0.65）。
    文件缺失 / 解析失败 → 保守按 0（保持现状）。

    返回值是 V2 公式里 reversal 的子权重，**不是** calibrated 的原始 weight。
    """
    path = REPO / "data" / "calibrated_factor_weights.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        diag = (data.get("diagnostics") or {}).get("reversal") or {}
        if diag.get("selected"):
            return 0.15
    except Exception:
        pass
    return 0.0


def _score_momentum(row: dict[str, Any]) -> float:
    parts = []
    for key, weight, lo, hi in (
        ("one_week_pct", 0.20, -8.0, 8.0),
        ("one_month_pct", 0.25, -15.0, 20.0),
        ("ytd_pct", 0.25, -25.0, 60.0),
    ):
        value = row.get(key)
        try:
            x = float(value)
        except Exception:
            parts.append((45.0, weight))
            continue
        parts.append((_clip((x - lo) / (hi - lo) * 100.0), weight))
    parts.append((_score_one_year_momentum(row.get("one_year_pct")), 0.30))
    momentum = sum(score * weight for score, weight in parts)
    # 追高一票封顶：1Y > 300% 时，短期动量也多半是情绪驱动，整体动量分压到 40。
    try:
        y1 = float(row.get("one_year_pct"))
        if y1 > 300:
            momentum = min(momentum, 40.0)
    except Exception:
        pass
    return momentum


def _factor_scores(row: dict[str, Any]) -> dict[str, Any]:
    peg_score = _score_lower_better(row.get("peg_ratio"), good=1.0, bad=4.0)
    fpe_score = _score_lower_better(row.get("forward_pe"), good=20.0, bad=80.0)
    tpe_score = _score_lower_better(row.get("trailing_pe"), good=25.0, bad=100.0)
    valuation = 0.45 * peg_score + 0.35 * fpe_score + 0.20 * tpe_score
    momentum = _score_momentum(row)
    reversal = _score_reversal(row)
    coverage_fields = (
        "close", "market_cap", "forward_pe", "trailing_pe", "peg_ratio",
        "ytd_pct", "one_week_pct", "one_month_pct", "one_year_pct",
    )
    coverage = sum(1 for key in coverage_fields if row.get(key) is not None) / len(coverage_fields)
    field_coverage_quality = coverage * 100.0
    data_usability = _data_usability_score(row, coverage)
    # 2026-05-26: momentum 0.42→0.15、valuation 0.38→0.65（IC 审计判 momentum 失效）。
    # 2026-05-27: 引入 reversal 因子 — calibrated_factor_weights.json 判 reversal
    # 🟢 strong (IC=0.062, hit=72.2%)，是 V2 唯一被独立验证的 alpha。reversal 子权重
    # 0.15 由 _load_reversal_weight() 从 calibrated 读，IC 失效时自动归零、回到
    # 旧公式 (0.15·mom + 0.65·val + 0.20·dq)，跟 IC audit 单一来源对齐。
    rev_w = _load_reversal_weight()
    val_w = 0.65 - rev_w  # reversal 从 valuation 切，保持总权重 1.0
    total = (
        0.15 * momentum
        + val_w * valuation
        + rev_w * reversal
        + 0.20 * data_usability
    )
    scores: dict[str, Any] = {
        "valuation": round(valuation, 2),
        "momentum": round(momentum, 2),
        "reversal": round(reversal, 2),
        # data_quality is kept for downstream UI compatibility; it now means
        # real recommendation usability, not just raw field count.
        "data_quality": round(data_usability, 2),
        "data_usability": round(data_usability, 2),
        "field_coverage_quality": round(field_coverage_quality, 2),
        "coverage": round(coverage, 4),
        "total": round(total, 2),
    }
    # F-Score / Piotroski 已计算入 factor_metadata 时透传（compute_piotroski_v2.py 写入）
    # 当前数据接入：A 股 akshare 财报、美/港股 待 FMP/yfinance 财报源激活
    f_score = row.get("_factor_meta_f_score")
    if f_score is not None:
        scores["f_score"] = round(float(f_score) / 9.0 * 100.0, 2)  # 标准化到 0-100
    quality_score = row.get("_factor_meta_quality_score")
    if quality_score is not None:
        scores["quality"] = round(float(quality_score), 2)
    return scores


def _daily_pct(row: dict[str, Any]) -> float | None:
    close = _as_float(row.get("close"))
    prev_close = _as_float(row.get("prev_close"))
    if close is None or prev_close is None or prev_close <= 0:
        return None
    return (close / prev_close - 1.0) * 100.0


def _fmt_pct(label: str, value: float | None) -> str | None:
    if value is None:
        return None
    return f"{label} {value:+.1f}%"


def _structural_repair_confirmed(
    *,
    one_week: float | None,
    one_month: float | None,
    day: float | None,
) -> bool:
    """Conservative repair check for names already in a structural downtrend.

    A sharp one-week bounce alone is not enough. The one-month window must have
    turned clearly positive, and the latest day cannot be another hard selloff.
    """
    if one_month is None or one_week is None:
        return False
    if one_month < 5.0 or one_week < 0.0:
        return False
    if day is not None and day <= -3.0:
        return False
    return True


def _price_action_review_reasons(row: dict[str, Any]) -> list[str]:
    """Return price-action reasons that require buy-before review.

    Reversal is useful as a research signal, but a name that is down sharply on
    short and medium windows should not graduate directly into a buy signal. A
    single sharp down day inside an otherwise strong trend is warning material,
    not enough by itself to turn a candidate into a "fallen knife" review gate.
    """
    one_week = _as_float(row.get("one_week_pct"))
    one_month = _as_float(row.get("one_month_pct"))
    ytd = _as_float(row.get("ytd_pct"))
    one_year = _as_float(row.get("one_year_pct"))
    day = _daily_pct(row)

    deep_month_drop = one_month is not None and one_month <= -15.0
    severe_month_drop = one_month is not None and one_month <= -20.0
    structural_downtrend = (
        (ytd is not None and ytd <= -25.0)
        or (one_year is not None and one_year <= -25.0)
    )
    medium_weakness = structural_downtrend or (one_month is not None and one_month <= -8.0)

    reasons: list[str] = []
    if day is not None and day <= -8.0 and medium_weakness:
        reasons.append(f"单日 {day:+.1f}%")
    if one_week is not None and one_week <= -12.0 and (
        structural_downtrend or (one_month is not None and one_month <= -5.0)
    ):
        reasons.append(f"近1周 {one_week:+.1f}%")
    if severe_month_drop or (deep_month_drop and structural_downtrend):
        details = [
            _fmt_pct("近1月", one_month),
            _fmt_pct("YTD", ytd),
            _fmt_pct("1Y", one_year),
        ]
        reasons.append(" / ".join(x for x in details if x))
    if structural_downtrend and not _structural_repair_confirmed(
        one_week=one_week, one_month=one_month, day=day,
    ):
        details = [
            _fmt_pct("YTD", ytd),
            _fmt_pct("1Y", one_year),
            _fmt_pct("近1月", one_month),
            _fmt_pct("近1周", one_week),
        ]
        reasons.append("结构性下跌未确认修复：" + " / ".join(x for x in details if x))

    return reasons


def _price_action_warning_flags(row: dict[str, Any]) -> list[dict[str, Any]]:
    if _price_action_review_reasons(row):
        return []

    reasons: list[str] = []
    day = _daily_pct(row)
    one_week = _as_float(row.get("one_week_pct"))
    if day is not None and day <= -8.0:
        reasons.append(f"单日 {day:+.1f}%")
    if one_week is not None and one_week <= -12.0:
        reasons.append(f"近1周 {one_week:+.1f}%")
    if not reasons:
        return []
    return [{
        "code": "ACUTE_PRICE_PULLBACK",
        "severity": "medium",
        "message": (
            "短线价格异动："
            + "；".join(reasons)
            + "。未达到跌深反转降级条件，但需确认是否为事件性下跌。"
        ),
    }]


def _apply_price_action_review_gate(row: dict[str, Any], scores: dict[str, Any]) -> list[dict[str, Any]]:
    reasons = _price_action_review_reasons(row)
    if not reasons:
        return []

    raw_total = float(scores["total"])
    scores["raw_total"] = round(raw_total, 2)
    scores["review_gate"] = "price_action"
    scores["total"] = round(min(raw_total, _PRICE_ACTION_REVIEW_SCORE_CAP), 2)
    structural = any("结构性下跌" in r for r in reasons)

    return [{
        "code": "STRUCTURAL_DOWNTREND_REVIEW_GATE" if structural else "PRICE_ACTION_REVIEW_GATE",
        "severity": "high",
        "message": (
            "价格行为红旗："
            + "；".join(reasons)
            + ("。属于结构性下跌后的反弹候选，已从 buy 降为 watch；需先确认修复。"
               if structural else
               "。属于跌深反转候选，已从 buy 降为 watch；需先进入买前审查。")
        ),
    }]


def _data_usability_review_reasons(row: dict[str, Any], scores: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    coverage = _as_float(scores.get("coverage"))
    usability = _as_float(scores.get("data_usability") or scores.get("data_quality"))

    if row.get("close") is None:
        reasons.append("缺最新价格")
    if coverage is not None and coverage < 0.70:
        reasons.append(f"核心字段覆盖不足 {coverage * 100:.0f}%")
    if usability is not None and usability < _DATA_USABILITY_MIN_BUY_SCORE:
        reasons.append(f"数据可用性 {usability:.1f} 分低于 {_DATA_USABILITY_MIN_BUY_SCORE:.0f}")

    if not row.get("momentum_trade_date"):
        reasons.append("缺动量数据源")
    else:
        gap = _date_gap_days(row.get("trade_date"), row.get("momentum_trade_date"))
        if gap is not None and gap > _STALE_SOURCE_DAYS:
            reasons.append(f"动量数据已过期 {gap} 天")

    if not row.get("fundamentals_trade_date"):
        reasons.append("缺估值数据源")
    else:
        gap = _date_gap_days(row.get("trade_date"), row.get("fundamentals_trade_date"))
        if gap is not None and gap > _STALE_SOURCE_DAYS:
            reasons.append(f"估值数据已过期 {gap} 天")

    if _positive_valuation_count(row) == 0:
        reasons.append("没有可用正向估值字段")

    # Preserve order while removing duplicates caused by the generic usability line.
    deduped: list[str] = []
    for reason in reasons:
        if reason not in deduped:
            deduped.append(reason)
    return deduped


def _apply_data_usability_gate(row: dict[str, Any], scores: dict[str, Any]) -> list[dict[str, Any]]:
    reasons = _data_usability_review_reasons(row, scores)
    if not reasons:
        return []

    raw_total = float(scores.get("raw_total", scores["total"]))
    scores.setdefault("raw_total", round(raw_total, 2))
    scores["data_usability_gate"] = "core_data"
    scores["total"] = round(min(float(scores["total"]), _DATA_USABILITY_REVIEW_SCORE_CAP), 2)
    return [{
        "code": "DATA_USABILITY_REVIEW_GATE",
        "severity": "high",
        "message": (
            "数据可用性红旗："
            + "；".join(reasons)
            + "。已限制为非买入；只能先研究/补数据，不能直接进入可买推荐。"
        ),
    }]


def _data_gap_next_action(reasons: list[str]) -> str:
    text = "；".join(reasons)
    if "缺最新价格" in text or "缺动量数据源" in text or "动量数据已过期" in text:
        return "先补行情和涨跌幅数据，再重新生成推荐。"
    if "缺估值数据源" in text or "估值数据已过期" in text:
        return "先补估值/财务字段，再重新生成推荐。"
    if "没有可用正向估值字段" in text:
        return "多半是亏损或估值口径不可用；只做研究观察，不直接按低估值买入。"
    if "核心字段覆盖不足" in text or "数据可用性" in text:
        return "先查缺失字段来源，补齐后再允许进入买入候选。"
    return "进入买前研究前先核对行情、估值和财务来源。"


def _risk_codes(flags: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for flag in flags:
        if isinstance(flag, dict) and flag.get("code"):
            out.add(str(flag["code"]))
    return out


def _normalize_evidence_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    return status if status in EVIDENCE_STATUSES else "missing"


def _is_etf_only_source(row: dict[str, Any]) -> bool:
    source = str(row.get("universe_source") or row.get("membership_source") or row.get("source") or "")
    reason = str(row.get("discovery_reason") or "")
    return source.startswith("etf_theme:") or "theme_etf_holdings" in reason


def _derive_recommendation_policy(row: dict[str, Any]) -> dict[str, Any]:
    """Apply the P0 identity/evidence/action gate without changing old factor score."""
    layer = classify_tech_growth_layer(
        market=row.get("market"),
        symbol=str(row.get("symbol") or ""),
        source=row.get("universe_source") or row.get("membership_source") or row.get("source"),
        theme=row.get("theme"),
        industry=row.get("industry"),
        name=row.get("name"),
    )
    layer_info = layer.as_dict()
    evidence_status = _normalize_evidence_status(row.get("evidence_status"))
    market = str(row.get("market") or "").upper()
    codes = _risk_codes(row.get("risk_flags") or [])
    signal = str(row.get("signal") or "").lower()

    policy = {
        "eligibility": "research_only",
        "action": "research_only",
        "evidence_status": evidence_status,
        "eligibility_migration_status": "p0_us",
        "primary_layer": layer.primary_layer,
        "secondary_layers": layer_info["secondary_layers"],
        "secondary_layers_json": json.dumps(layer_info["secondary_layers"], ensure_ascii=False),
        "ai_relevance_level": layer.ai_relevance_level,
        "layer_confidence": layer.layer_confidence,
        "classification_version": layer.classification_version,
        "classification_rationale": layer.rationale,
    }

    if market != P0_US_MARKET:
        policy["eligibility_migration_status"] = "legacy"
        return policy

    if layer.primary_layer == "excluded":
        policy.update({
            "eligibility": "excluded",
            "action": "exclude",
            "eligibility_migration_status": "excluded_identity",
        })
        return policy

    if _is_etf_only_source(row) and evidence_status != BUYABLE_EVIDENCE_STATUS:
        policy.update({
            "eligibility": "watch_only",
            "action": "watch_only",
            "eligibility_migration_status": "etf_only_needs_company_evidence",
        })
        return policy

    if "DATA_USABILITY_REVIEW_GATE" in codes:
        policy.update({
            "eligibility": "research_only",
            "action": "research_only",
            "eligibility_migration_status": "data_usability_gate",
        })
        return policy

    if not is_buyable_layer(layer.primary_layer):
        policy.update({
            "eligibility": "research_only" if evidence_status == BUYABLE_EVIDENCE_STATUS else "watch_only",
            "action": "research_only" if evidence_status == BUYABLE_EVIDENCE_STATUS else "watch_only",
            "eligibility_migration_status": "layer_not_buyable",
        })
        return policy

    if evidence_status != BUYABLE_EVIDENCE_STATUS:
        policy.update({
            "eligibility": "research_only",
            "action": "research_only",
            "eligibility_migration_status": "needs_company_evidence",
        })
        return policy

    policy["eligibility"] = "buyable"
    if codes & BLOCKING_RISK_CODES:
        policy.update({
            "action": "blocked",
            "eligibility_migration_status": "risk_gate",
        })
    elif codes & WAIT_ENTRY_RISK_CODES or signal != "buy":
        policy.update({
            "action": "wait_entry",
            "eligibility_migration_status": "wait_entry",
        })
    else:
        policy.update({
            "action": "focus_research",
            "eligibility_migration_status": "passed_p0_gate",
        })
    return policy


def _table_columns(conn: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
        return {str(r[1]) for r in rows}
    except Exception:
        return set()


def _ensure_p0_columns(conn: duckdb.DuckDBPyConnection) -> None:
    for sql in (
        """
        CREATE TABLE IF NOT EXISTS market_phase_snapshot (
            snapshot_id   VARCHAR PRIMARY KEY,
            as_of_date    DATE NOT NULL,
            phase_id      VARCHAR NOT NULL,
            phase_name    VARCHAR NOT NULL,
            scope         VARCHAR NOT NULL,
            confidence    VARCHAR NOT NULL,
            evidence_json VARCHAR,
            review_cycle  VARCHAR,
            next_review_at DATE,
            owner         VARCHAR,
            status        VARCHAR NOT NULL DEFAULT 'active',
            created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "ALTER TABLE system_universe ADD COLUMN IF NOT EXISTS primary_layer VARCHAR",
        "ALTER TABLE system_universe ADD COLUMN IF NOT EXISTS secondary_layers_json VARCHAR",
        "ALTER TABLE system_universe ADD COLUMN IF NOT EXISTS ai_relevance_level VARCHAR",
        "ALTER TABLE system_universe ADD COLUMN IF NOT EXISTS layer_confidence VARCHAR",
        "ALTER TABLE system_universe ADD COLUMN IF NOT EXISTS classification_version VARCHAR",
        "ALTER TABLE system_universe ADD COLUMN IF NOT EXISTS classification_rationale VARCHAR",
        "ALTER TABLE recommendation_runs ADD COLUMN IF NOT EXISTS market_phase_snapshot_id VARCHAR",
        "ALTER TABLE recommendation_picks ADD COLUMN IF NOT EXISTS eligibility VARCHAR",
        "ALTER TABLE recommendation_picks ADD COLUMN IF NOT EXISTS action VARCHAR",
        "ALTER TABLE recommendation_picks ADD COLUMN IF NOT EXISTS evidence_status VARCHAR",
        "ALTER TABLE recommendation_picks ADD COLUMN IF NOT EXISTS eligibility_migration_status VARCHAR",
        "ALTER TABLE recommendation_picks ADD COLUMN IF NOT EXISTS primary_layer VARCHAR",
        "ALTER TABLE recommendation_picks ADD COLUMN IF NOT EXISTS secondary_layers_json VARCHAR",
        "ALTER TABLE recommendation_picks ADD COLUMN IF NOT EXISTS ai_relevance_level VARCHAR",
        "ALTER TABLE recommendation_picks ADD COLUMN IF NOT EXISTS layer_confidence VARCHAR",
        "ALTER TABLE recommendation_picks ADD COLUMN IF NOT EXISTS classification_version VARCHAR",
        "ALTER TABLE recommendation_picks ADD COLUMN IF NOT EXISTS classification_rationale VARCHAR",
        "ALTER TABLE recommendation_picks ADD COLUMN IF NOT EXISTS market_phase_snapshot_id VARCHAR",
        "ALTER TABLE recommendation_picks ADD COLUMN IF NOT EXISTS short_term_view_json VARCHAR",
        "ALTER TABLE recommendation_picks ADD COLUMN IF NOT EXISTS six_month_view_json VARCHAR",
        "ALTER TABLE recommendation_picks ADD COLUMN IF NOT EXISTS long_term_view_json VARCHAR",
    ):
        conn.execute(sql)


def _ensure_market_phase_snapshot(conn: duckdb.DuckDBPyConnection, now: datetime) -> str:
    snapshot_id = f"phase_{now.strftime('%Y%m%d')}_{MARKET_PHASE_ID}"
    evidence = [
        "大厂 CapEx 仍高，数据中心建设仍在主线内",
        "AI 核心芯片、网络和云平台收入仍在兑现，但估值与拥挤交易需要单独拦截",
        "数据中心电力、散热和电网基础设施需求继续上行",
        "推理、Agent 和企业软件集成进入前半段，软件股需要单独观察兑现节奏",
    ]
    conn.execute(
        """
        INSERT INTO market_phase_snapshot (
            snapshot_id, as_of_date, phase_id, phase_name, scope, confidence,
            evidence_json, review_cycle, next_review_at, owner, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'medium', ?, 'weekly', ?, 'human_review_required', 'active', ?, ?)
        ON CONFLICT (snapshot_id) DO UPDATE SET
            as_of_date=excluded.as_of_date,
            phase_id=excluded.phase_id,
            phase_name=excluded.phase_name,
            scope=excluded.scope,
            confidence=excluded.confidence,
            evidence_json=excluded.evidence_json,
            review_cycle=excluded.review_cycle,
            next_review_at=excluded.next_review_at,
            owner=excluded.owner,
            status=excluded.status,
            updated_at=excluded.updated_at
        """,
        [
            snapshot_id,
            now.date(),
            MARKET_PHASE_ID,
            MARKET_PHASE_NAME,
            MARKET_PHASE_SCOPE,
            json.dumps(evidence, ensure_ascii=False),
            now.date() + timedelta(days=7),
            now,
            now,
        ],
    )
    return snapshot_id


def _backfill_system_universe_layers(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT pool_id, market, symbol, source, theme, industry, name
        FROM system_universe
        WHERE active = TRUE
        """
    ).fetchall()
    counts: dict[str, int] = {}
    for pool_id, market, symbol, source, theme, industry, name in rows:
        layer = classify_tech_growth_layer(
            market=market,
            symbol=str(symbol or ""),
            source=source,
            theme=theme,
            industry=industry,
            name=name,
        )
        info = layer.as_dict()
        counts[layer.primary_layer] = counts.get(layer.primary_layer, 0) + 1
        conn.execute(
            """
            UPDATE system_universe
            SET primary_layer = ?,
                secondary_layers_json = ?,
                ai_relevance_level = ?,
                layer_confidence = ?,
                classification_version = ?,
                classification_rationale = ?,
                last_seen_at = CURRENT_TIMESTAMP
            WHERE pool_id = ? AND market = ? AND symbol = ?
            """,
            [
                layer.primary_layer,
                json.dumps(info["secondary_layers"], ensure_ascii=False),
                layer.ai_relevance_level,
                layer.layer_confidence,
                layer.classification_version,
                layer.rationale,
                pool_id,
                market,
                symbol,
            ],
        )
    return counts


def _risk_flag_messages(row: dict[str, Any], limit: int = 3) -> list[str]:
    out: list[str] = []
    for flag in row.get("risk_flags") or []:
        if isinstance(flag, dict):
            msg = str(flag.get("message") or flag.get("code") or "").strip()
            if msg:
                out.append(msg)
    return out[:limit]


def _build_period_views(row: dict[str, Any], *, market_phase_snapshot_id: str) -> dict[str, str]:
    factors = row.get("factor_scores") if isinstance(row.get("factor_scores"), dict) else {}
    risk_messages = _risk_flag_messages(row)
    eligibility = row.get("eligibility") or "research_only"
    action = row.get("action") or "research_only"
    layer = row.get("primary_layer") or "theme_watch"
    evidence = row.get("evidence_status") or "missing"
    valuation = _as_float(factors.get("valuation"))
    momentum = _as_float(factors.get("momentum"))
    reversal = _as_float(factors.get("reversal"))

    can_buy_today = eligibility == "buyable" and action == "focus_research"
    short_reasons = []
    if not can_buy_today:
        short_reasons.append(f"动作={action}，资格={eligibility}")
    if risk_messages:
        short_reasons.extend(risk_messages)
    if evidence != BUYABLE_EVIDENCE_STATUS:
        short_reasons.append(f"公司级证据={evidence}，不能直接进可买候选")
    if not short_reasons:
        short_reasons.append("通过 P0 身份/证据/数据/风险闸，仍需买前研究确认")

    six_month_status = "主线候选" if layer in {"ai_core", "ai_infrastructure", "power_datacenter"} else "观察兑现"
    if evidence != BUYABLE_EVIDENCE_STATUS:
        six_month_status = "证据待补"
    if valuation is not None and valuation < 45:
        six_month_status = "估值压力"

    long_term_status = "长期跟踪"
    if layer == "excluded":
        long_term_status = "不纳入科技成长主线"
    elif layer == "theme_watch":
        long_term_status = "主题观察，等证据增强"

    payloads = {
        "short_term_view_json": {
            "scope": "1d-8w",
            "can_buy_today": can_buy_today,
            "action": action,
            "eligibility": eligibility,
            "risk_scope": "short",
            "reasons": short_reasons[:4],
            "factor_hint": {
                "momentum": momentum,
                "reversal": reversal,
            },
            "market_phase_snapshot_id": market_phase_snapshot_id,
        },
        "six_month_view_json": {
            "scope": "3m-12m",
            "status": six_month_status,
            "primary_layer": layer,
            "evidence_status": evidence,
            "valuation_score": valuation,
            "mainline": layer in {"ai_core", "ai_infrastructure", "power_datacenter", "tech_software", "internet_platform"},
            "market_phase_snapshot_id": market_phase_snapshot_id,
        },
        "long_term_view_json": {
            "scope": "1y-5y",
            "status": long_term_status,
            "primary_layer": layer,
            "ai_relevance_level": row.get("ai_relevance_level"),
            "classification_rationale": row.get("classification_rationale"),
            "market_phase_snapshot_id": market_phase_snapshot_id,
        },
    }
    return {
        key: json.dumps(value, ensure_ascii=False, default=str)
        for key, value in payloads.items()
    }


def _audit_row(row: dict[str, Any], selected_rank: int | None, reasons: list[str]) -> dict[str, Any]:
    scores = row.get("factor_scores") or {}
    coverage = _as_float(scores.get("coverage"))
    return {
        "market": row.get("market"),
        "symbol": row.get("symbol"),
        "name": row.get("name"),
        "theme": row.get("theme"),
        "industry": row.get("industry"),
        "total_score": row.get("total_score"),
        "raw_total": scores.get("raw_total"),
        "signal": row.get("signal"),
        "rating": row.get("rating"),
        "in_recommendation_list": selected_rank is not None,
        "rank": selected_rank,
        "data_usability": scores.get("data_usability") or scores.get("data_quality"),
        "field_coverage_quality": scores.get("field_coverage_quality"),
        "coverage_pct": round(coverage * 100.0, 1) if coverage is not None else None,
        "trade_date": str(row.get("trade_date"))[:10] if row.get("trade_date") else None,
        "momentum_trade_date": str(row.get("momentum_trade_date"))[:10] if row.get("momentum_trade_date") else None,
        "fundamentals_trade_date": str(row.get("fundamentals_trade_date"))[:10] if row.get("fundamentals_trade_date") else None,
        "reasons": reasons,
        "next_action": _data_gap_next_action(reasons),
    }


def _build_data_usability_audit(
    scored: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    *,
    run_id: str,
    generated_at: datetime,
) -> dict[str, Any]:
    selected_rank: dict[tuple[Any, Any], int] = {}
    market_rank_counter: dict[str, int] = {}
    for row in selected:
        market = str(row.get("market") or "UNKNOWN")
        market_rank_counter[market] = market_rank_counter.get(market, 0) + 1
        selected_rank[(row.get("market"), row.get("symbol"))] = market_rank_counter[market]
    blocked: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []
    summary_by_market: dict[str, dict[str, int]] = {}

    attention_codes = {
        "MOMENTUM_REUSED_RECENT_V2_SNAPSHOT",
        "FUNDAMENTALS_REUSED_RECENT_V2_SNAPSHOT",
        "INVALID_VALUATION_RATIO",
        "ACUTE_PRICE_PULLBACK",
    }
    for row in scored:
        market = str(row.get("market") or "UNKNOWN")
        bucket = summary_by_market.setdefault(
            market,
            {"candidates": 0, "blocked": 0, "attention": 0, "selected_attention": 0},
        )
        bucket["candidates"] += 1
        key = (row.get("market"), row.get("symbol"))
        rank = selected_rank.get(key)
        scores = row.get("factor_scores") or {}
        reasons = _data_usability_review_reasons(row, scores)
        if reasons:
            bucket["blocked"] += 1
            blocked.append(_audit_row(row, rank, reasons))
            continue

        usability = _as_float(scores.get("data_usability") or scores.get("data_quality")) or 0.0
        coverage = _as_float(scores.get("field_coverage_quality")) or 0.0
        codes = _risk_codes(row.get("risk_flags") or [])
        weak_reasons: list[str] = []
        if usability < 90.0:
            weak_reasons.append(f"数据可用性 {usability:.1f} 分，未触发硬拦截但不算满格")
        if coverage < 90.0:
            weak_reasons.append(f"字段覆盖 {coverage:.1f}%")
        if codes & attention_codes:
            for flag in row.get("risk_flags") or []:
                if isinstance(flag, dict) and flag.get("code") in attention_codes:
                    weak_reasons.append(str(flag.get("message") or flag.get("code")))
        if weak_reasons:
            bucket["attention"] += 1
            if rank is not None:
                bucket["selected_attention"] += 1
            attention.append(_audit_row(row, rank, weak_reasons))

    blocked.sort(key=lambda r: (r.get("market") or "", -(r.get("raw_total") or r.get("total_score") or 0), r.get("symbol") or ""))
    attention.sort(key=lambda r: (
        0 if r.get("in_recommendation_list") else 1,
        r.get("data_usability") if r.get("data_usability") is not None else 999,
        -(r.get("total_score") or 0),
        r.get("symbol") or "",
    ))
    return {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "run_id": run_id,
        "strategy_version": STRATEGY_VERSION,
        "model_version": MODEL_VERSION,
        "candidate_count": len(scored),
        "selected_count": len(selected),
        "blocked_count": len(blocked),
        "attention_count": len(attention),
        "selected_attention_count": sum(1 for r in attention if r.get("in_recommendation_list")),
        "summary_by_market": summary_by_market,
        "blocked": blocked[:120],
        "attention": attention[:120],
        "note": "blocked=硬拦截，不能进入 buy；attention=数据偏弱或复用旧快照，仍可研究但需看原因。",
    }


def _eligibility_counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = str(r.get(key) or "unknown")
        out[k] = out.get(k, 0) + 1
    return out


def _build_p0_eligibility_gate_summary(
    selected: list[dict[str, Any]],
    us_gated_out: list[dict[str, Any]],
) -> dict[str, Any]:
    """P0 身份/资格闸的审计（对应规则文档 §18 第 ④ 项）。

    记录被挡在 AI 推荐名单外的美股，并用 eligibility_migration_status 区分排除原因
    （身份不符 / 仅 ETF 无证据 / 数据缺失 / 分层不可买 / 证据不足 / 风险），
    避免"靠运气漏掉"被误读成"系统识别了风险"。旧 rating 一并记录，证明
    "分数高也会被身份/证据闸挡掉"（如 VIPS strong_buy 仍被 excluded）。
    """
    us_selected = [r for r in selected if str(r.get("market") or "") == P0_US_MARKET]
    gated = sorted(us_gated_out, key=lambda r: -(_as_float(r.get("total_score")) or 0.0))
    return {
        "market": P0_US_MARKET,
        "recommendable_eligibility": sorted(RECOMMENDABLE_US_ELIGIBILITY),
        "us_selected_count": len(us_selected),
        "us_selected_eligibility": _eligibility_counts(us_selected, "eligibility"),
        "us_gated_out_count": len(us_gated_out),
        "gated_out_reason_breakdown": _eligibility_counts(us_gated_out, "eligibility_migration_status"),
        "gated_out": [
            {
                "symbol": r.get("symbol"),
                "name": r.get("name"),
                "total_score": r.get("total_score"),
                "rating": r.get("rating"),
                "signal": r.get("signal"),
                "primary_layer": r.get("primary_layer"),
                "eligibility": r.get("eligibility"),
                "exclusion_reason": r.get("eligibility_migration_status"),
            }
            for r in gated[:80]
        ],
        "note": (
            "AI 推荐名单仅含 buyable/research_only；下列美股分数可能达标但因身份/证据/数据/风险被挡，"
            "不进名单（rating 仅为历史打分，不代表推荐）。排除原因见 exclusion_reason。"
        ),
    }


def _write_data_usability_audit(payload: dict[str, Any]) -> None:
    DATA_USABILITY_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_USABILITY_AUDIT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)


def _rating(total: float) -> str:
    if total >= 75:
        return "strong_buy"
    if total >= 60:
        return "buy"
    if total >= 50:
        return "watch"
    return "avoid"


def _signal(total: float) -> str:
    if total >= 60:
        return "buy"
    if total >= 50:
        return "watch"
    return "avoid"


def _same_day(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    return str(a)[:10] == str(b)[:10]


def _quality_flags(row: dict[str, Any]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    invalid_valuation_fields = []
    for key, label in (
        ("peg_ratio", "PEG"),
        ("forward_pe", "Forward PE"),
        ("trailing_pe", "Trailing PE"),
    ):
        value = _as_float(row.get(key))
        if value is not None and value <= 0:
            invalid_valuation_fields.append(f"{label}={value:g}")
    if invalid_valuation_fields:
        flags.append({
            "code": "INVALID_VALUATION_RATIO",
            "severity": "medium",
            "message": (
                "估值字段异常："
                + " / ".join(invalid_valuation_fields)
                + "；已按亏损/缺失口径扣分，不视为便宜。"
            ),
        })
    try:
        y1 = float(row.get("one_year_pct"))
    except (TypeError, ValueError):
        y1 = None
    if y1 is not None and y1 > 200:
        flags.append({
            "code": "OVERHEATED_1Y",
            "severity": "high" if y1 > 300 else "medium",
            "message": f"1Y 涨幅 {y1:.0f}%（>200% 历史前向收益走弱，已对动量分追高扣分）",
        })
    if not row.get("momentum_trade_date"):
        flags.append({
            "code": "MOMENTUM_MISSING",
            "severity": "medium",
            "message": "该标的 price_daily 找不到任何非空动量字段，可能用了默认动量分。",
        })
    elif not _same_day(row.get("momentum_trade_date"), row.get("trade_date")):
        flags.append({
            "code": "MOMENTUM_REUSED_RECENT_V2_SNAPSHOT",
            "severity": "low",
            "message": (
                "最新行情动量字段为空，已回退到 "
                f"{str(row.get('momentum_trade_date'))[:10]} 最近一次有效快照。"
            ),
        })
    if not row.get("fundamentals_trade_date"):
        flags.append({
            "code": "FUNDAMENTALS_MISSING",
            "severity": "medium",
            "message": "该标的 price_daily 找不到任何非空估值字段。",
        })
    elif not _same_day(row.get("fundamentals_trade_date"), row.get("trade_date")):
        flags.append({
            "code": "FUNDAMENTALS_REUSED_RECENT_V2_SNAPSHOT",
            "severity": "low",
            "message": (
                "最新行情估值字段为空，已回退到 "
                f"{str(row.get('fundamentals_trade_date'))[:10]} 最近一次有效快照。"
            ),
        })
    return flags


def _reason(row: dict[str, Any]) -> str:
    scores = row["factor_scores"]
    momentum_date = str(row.get("momentum_trade_date") or "")[:10] or "missing"
    fundamentals_date = str(row.get("fundamentals_trade_date") or "")[:10] or "missing"
    return (
        f"momentum={scores['momentum']}, valuation={scores['valuation']}, "
        f"coverage={scores['coverage']}; "
        f"momentum_source=price_daily:{momentum_date}, "
        f"valuation_source=price_daily:{fundamentals_date}"
    )


def _load_candidates(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
    evidence_cte = ""
    evidence_join = ""
    evidence_select = """
            'missing' AS evidence_status,
            NULL AS ai_evidence_score,
            NULL AS ai_evidence_latest_source_date,
            NULL AS ai_evidence_rationale,
        """
    if "ai_theme_company_tags" in tables:
        evidence_cte = """
        ,
        latest_theme_tags AS (
            SELECT *
            FROM (
                SELECT
                    market, symbol, evidence_status, evidence_score,
                    latest_source_date, rationale, updated_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY market, symbol
                        ORDER BY
                            CASE LOWER(COALESCE(evidence_status, 'missing'))
                                WHEN 'confirmed' THEN 1
                                WHEN 'needs_review' THEN 2
                                WHEN 'candidate' THEN 3
                                WHEN 'stale' THEN 4
                                ELSE 5
                            END,
                            COALESCE(evidence_score, 0) DESC,
                            latest_source_date DESC NULLS LAST,
                            updated_at DESC NULLS LAST
                    ) AS rn
                FROM ai_theme_company_tags
            )
            WHERE rn = 1
        )
        """
        evidence_join = """
        LEFT JOIN latest_theme_tags tt
          ON tt.market = m.market AND tt.symbol = m.symbol
        """
        evidence_select = """
            COALESCE(tt.evidence_status, 'missing') AS evidence_status,
            tt.evidence_score AS ai_evidence_score,
            tt.latest_source_date AS ai_evidence_latest_source_date,
            tt.rationale AS ai_evidence_rationale,
        """
    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT market, symbol, MAX(trade_date) AS trade_date
            FROM price_daily
            GROUP BY market, symbol
        ),
        latest_price AS (
            SELECT p.*
            FROM price_daily p
            JOIN latest l
              ON l.market = p.market AND l.symbol = p.symbol AND l.trade_date = p.trade_date
        ),
        latest_momentum AS (
            SELECT *
            FROM (
                SELECT
                    market, symbol, trade_date, source, fetched_at,
                    ytd_pct, one_week_pct, one_month_pct, one_year_pct,
                    ROW_NUMBER() OVER (
                        PARTITION BY market, symbol
                        ORDER BY trade_date DESC, fetched_at DESC
                    ) AS rn
                FROM price_daily
                WHERE ytd_pct IS NOT NULL
                   OR one_week_pct IS NOT NULL
                   OR one_month_pct IS NOT NULL
                   OR one_year_pct IS NOT NULL
            )
            WHERE rn = 1
        ),
        latest_fundamentals AS (
            SELECT *
            FROM (
                SELECT
                    market, symbol, trade_date, source, fetched_at,
                    market_cap, forward_pe, trailing_pe, peg_ratio,
                    ROW_NUMBER() OVER (
                        PARTITION BY market, symbol
                        ORDER BY trade_date DESC, fetched_at DESC
                    ) AS rn
                FROM price_daily
                WHERE market_cap IS NOT NULL
                   OR forward_pe IS NOT NULL
                   OR trailing_pe IS NOT NULL
                   OR peg_ratio IS NOT NULL
            )
            WHERE rn = 1
        )
        {evidence_cte}
        SELECT
            m.pool_id, m.market, m.symbol, u.name, u.theme, u.industry,
            p.trade_date, p.close, p.prev_close, p.currency,
            COALESCE(p.market_cap, f.market_cap) AS market_cap,
            COALESCE(p.forward_pe, f.forward_pe) AS forward_pe,
            COALESCE(p.trailing_pe, f.trailing_pe) AS trailing_pe,
            COALESCE(p.peg_ratio, f.peg_ratio) AS peg_ratio,
            COALESCE(p.ytd_pct, mo.ytd_pct) AS ytd_pct,
            COALESCE(p.one_week_pct, mo.one_week_pct) AS one_week_pct,
            COALESCE(p.one_month_pct, mo.one_month_pct) AS one_month_pct,
            COALESCE(p.one_year_pct, mo.one_year_pct) AS one_year_pct,
            p.source, m.source AS membership_source, u.source AS universe_source,
            u.primary_layer AS universe_primary_layer,
            u.secondary_layers_json AS universe_secondary_layers_json,
            u.ai_relevance_level AS universe_ai_relevance_level,
            u.layer_confidence AS universe_layer_confidence,
            u.classification_version AS universe_classification_version,
            u.classification_rationale AS universe_classification_rationale,
            {evidence_select}
            p.fetched_at,
            mo.trade_date AS momentum_trade_date,
            mo.source AS momentum_source,
            f.trade_date AS fundamentals_trade_date,
            f.source AS fundamentals_source,
            fm.f_score AS _factor_meta_f_score,
            fm.quality_score AS _factor_meta_quality_score
        FROM pool_membership m
        JOIN latest_price p
          ON p.market = m.market AND p.symbol = m.symbol
        LEFT JOIN latest_momentum mo
          ON mo.market = m.market AND mo.symbol = m.symbol
        LEFT JOIN latest_fundamentals f
          ON f.market = m.market AND f.symbol = m.symbol
        LEFT JOIN system_universe u
          ON u.pool_id = m.pool_id AND u.market = m.market AND u.symbol = m.symbol
        LEFT JOIN factor_metadata fm
          ON fm.market = m.market AND fm.symbol = m.symbol
        {evidence_join}
        WHERE m.active = TRUE
          AND m.pool_type = 'system_tech_universe'
        """
    ).fetchall()
    cols = [
        "pool_id", "market", "symbol", "name", "theme", "industry",
        "trade_date", "close", "prev_close", "currency", "market_cap",
        "forward_pe", "trailing_pe", "peg_ratio", "ytd_pct",
        "one_week_pct", "one_month_pct", "one_year_pct", "source",
        "membership_source", "universe_source", "universe_primary_layer",
        "universe_secondary_layers_json", "universe_ai_relevance_level",
        "universe_layer_confidence", "universe_classification_version",
        "universe_classification_rationale", "evidence_status",
        "ai_evidence_score", "ai_evidence_latest_source_date",
        "ai_evidence_rationale", "fetched_at", "momentum_trade_date", "momentum_source",
        "fundamentals_trade_date", "fundamentals_source",
        "_factor_meta_f_score", "_factor_meta_quality_score",
    ]
    return [dict(zip(cols, row)) for row in rows]


def build(db_path: Path, *, top_per_market: int, portfolio_size: int, dry_run: bool) -> dict[str, Any]:
    conn = duckdb.connect(str(db_path), read_only=dry_run)
    tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
    required = {"pool_membership", "system_universe", "price_daily", "recommendation_runs", "recommendation_picks", "portfolio_plans"}
    missing = sorted(required - tables)
    if missing:
        conn.close()
        raise RuntimeError(f"当前 DB 缺少 v2 推荐表: {missing}")
    now = datetime.now()
    layer_counts: dict[str, int] = {}
    evidence_seed_summary: dict[str, Any] | None = None
    market_phase_snapshot_id: str | None = None
    if not dry_run:
        _ensure_p0_columns(conn)
        layer_counts = _backfill_system_universe_layers(conn)
        market_phase_snapshot_id = _ensure_market_phase_snapshot(conn, now)
        from stock_research.jobs.aggregate_theme_tags import aggregate_tags
        from stock_research.jobs.seed_p0_tech_growth_evidence import seed_p0_tech_growth_evidence

        evidence_seed_summary = seed_p0_tech_growth_evidence(conn, now=now)
        evidence_seed_summary["tags_aggregate"] = aggregate_tags(conn)

    pool_membership_count = int(conn.execute(
        "SELECT COUNT(*) FROM pool_membership WHERE active = TRUE AND pool_type = 'system_tech_universe'"
    ).fetchone()[0])
    price_daily_count = int(conn.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0])
    candidates = _load_candidates(conn)
    if market_phase_snapshot_id is None:
        market_phase_snapshot_id = f"phase_{now.strftime('%Y%m%d')}_{MARKET_PHASE_ID}"
    run_id = f"rec_{now.strftime('%Y%m%d_%H%M%S')}_system_tech"
    strategy_version = STRATEGY_VERSION
    model_version = MODEL_VERSION
    scored: list[dict[str, Any]] = []
    for row in candidates:
        scores = _factor_scores(row)
        price_action_flags = _apply_price_action_review_gate(row, scores)
        if not price_action_flags:
            price_action_flags = _price_action_warning_flags(row)
        data_usability_flags = _apply_data_usability_gate(row, scores)
        total = scores["total"]
        scored_row = {
            **row,
            "total_score": total,
            "factor_scores": scores,
            "risk_flags": data_usability_flags + price_action_flags + _quality_flags(row),
            "rating": _rating(total),
            "signal": _signal(total),
        }
        policy = _derive_recommendation_policy(scored_row)
        scored_row.update(policy)
        period_views = _build_period_views(scored_row, market_phase_snapshot_id=market_phase_snapshot_id)
        scored_row.update(period_views)
        scores["eligibility"] = policy["eligibility"]
        scores["action"] = policy["action"]
        scores["primary_layer"] = policy["primary_layer"]
        scores["evidence_status"] = policy["evidence_status"]
        scored.append(scored_row)

    selected: list[dict[str, Any]] = []
    us_gated_out: list[dict[str, Any]] = []
    for market in ("US", "HK", "CN"):
        market_rows = [r for r in scored if r["market"] == market]
        if market == P0_US_MARKET:
            # P0 身份/资格闸：AI 推荐名单只收 buyable / research_only。
            # excluded（身份不符，如 VIPS）与 watch_only（仅 ETF/主题、无公司级证据）
            # 不进名单，但记入 us_gated_out 供审计与「红旗拦截/只观察」区使用。
            # 这是 filter 层，不改打分公式与 rating 阈值；CN/HK 维持 legacy 不过滤。
            us_gated_out = [r for r in market_rows if r.get("eligibility") not in RECOMMENDABLE_US_ELIGIBILITY]
            market_rows = [r for r in market_rows if r.get("eligibility") in RECOMMENDABLE_US_ELIGIBILITY]
        market_rows.sort(key=lambda x: (-x["total_score"], x["symbol"]))
        selected.extend(market_rows[:top_per_market])
    selected.sort(key=lambda x: (x["market"], -x["total_score"], x["symbol"]))
    data_usability_audit = _build_data_usability_audit(
        scored,
        selected,
        run_id=run_id,
        generated_at=now,
    )
    data_usability_audit["p0_eligibility_gate"] = _build_p0_eligibility_gate_summary(selected, us_gated_out)

    if dry_run:
        conn.close()
        return {
            "db_path": str(db_path),
            "run_id": run_id,
            "dry_run": True,
            "pool_membership_count": pool_membership_count,
            "price_daily_count": price_daily_count,
            "candidate_count": len(candidates),
            "selected_count": len(selected),
            "data_usability_audit": {
                "blocked_count": data_usability_audit["blocked_count"],
                "attention_count": data_usability_audit["attention_count"],
                "selected_attention_count": data_usability_audit["selected_attention_count"],
                "summary_by_market": data_usability_audit["summary_by_market"],
            },
            "p0_eligibility_gate": data_usability_audit["p0_eligibility_gate"],
            "market_phase_snapshot_id": market_phase_snapshot_id,
            "layer_counts": layer_counts,
            "evidence_seed_summary": evidence_seed_summary,
            "selected": [
                {
                    "market": r["market"],
                    "symbol": r["symbol"],
                    "name": r["name"],
                    "score": r["total_score"],
                    "signal": r["signal"],
                    "eligibility": r["eligibility"],
                    "action": r["action"],
                    "primary_layer": r["primary_layer"],
                    "evidence_status": r["evidence_status"],
                }
                for r in selected
            ],
        }

    conn.execute(
        """
        INSERT INTO strategy_versions (
            strategy_version, status, description, params_json, created_at, activated_at
        ) VALUES (?, 'active', ?, ?, ?, ?)
        ON CONFLICT (strategy_version) DO UPDATE SET
            status='active',
            description=excluded.description,
            params_json=excluded.params_json
        """,
        [
            strategy_version,
            (
                "V2 system tech universe rule-factor strategy with usable-data gate, "
                "price-action review gate, invalid valuation-ratio guard, and buy-only portfolio eligibility."
            ),
            json.dumps({
                "top_per_market": top_per_market,
                "portfolio_size": portfolio_size,
                "score_cap_price_action_review": _PRICE_ACTION_REVIEW_SCORE_CAP,
                "score_cap_data_usability_review": _DATA_USABILITY_REVIEW_SCORE_CAP,
                "data_usability_min_buy_score": _DATA_USABILITY_MIN_BUY_SCORE,
                "stale_source_days": _STALE_SOURCE_DAYS,
                "invalid_valuation_ratio_score": _NEGATIVE_VALUATION_SCORE,
                "tech_growth_layer_version": CLASSIFICATION_VERSION,
                "p0_evidence_seed": evidence_seed_summary,
                "p0_us_eligibility_gate": (
                    "old score stays as baseline; US action requires identity layer + confirmed evidence "
                    "+ usable data + risk-scope gate"
                ),
                "market_phase_snapshot_id": market_phase_snapshot_id,
                "structural_repair_requires": "one_month>=+5%, one_week>=0%, latest_day>-3%",
                "formula": (
                    "total=0.15*momentum+(0.65-reversal_w)*valuation+"
                    "reversal_w*reversal+0.20*data_usability; "
                    "data_usability gates buy eligibility"
                ),
            }, ensure_ascii=False),
            now,
            now,
        ],
    )
    conn.execute(
        """
        INSERT INTO recommendation_runs (
            run_id, run_date, strategy_version, model_version, universe_scope,
            market_phase_snapshot_id, data_cutoff_at, generated_at, status, notes
        ) VALUES (?, ?, ?, ?, 'system_tech_universe', ?, ?, ?, 'generated', ?)
        """,
        [
            run_id,
            now.date(),
            strategy_version,
            model_version,
            market_phase_snapshot_id,
            now,
            now,
            (
                f"candidate_count={len(candidates)}; selected_count={len(selected)}; "
                f"pool_membership={pool_membership_count}; price_daily={price_daily_count}; "
                f"market_phase_snapshot_id={market_phase_snapshot_id}; "
                f"p0_evidence_seed_symbols={(evidence_seed_summary or {}).get('n_symbols')}; "
                "scoring_change=usable_data_gate+price_action_review_gate+"
                "structural_downtrend_gate+invalid_zero_valuation_guard; "
                "p0_gate=identity_layer+company_evidence+action"
            ),
        ],
    )
    for rank, row in enumerate(selected, start=1):
        conn.execute(
            """
            INSERT INTO recommendation_picks (
                run_id, market, symbol, name, rank, rating, signal,
                total_score, factor_scores_json, recommendation_reason,
                risk_flags_json, eligibility, action, evidence_status,
                eligibility_migration_status, primary_layer, secondary_layers_json,
                ai_relevance_level, layer_confidence, classification_version,
                classification_rationale, market_phase_snapshot_id,
                short_term_view_json, six_month_view_json, long_term_view_json,
                entry_price, entry_currency, universe_scope, source_origin, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'system_tech_universe', 'system_pool', ?)
            """,
            [
                run_id,
                row["market"],
                row["symbol"],
                row["name"],
                rank,
                row["rating"],
                row["signal"],
                row["total_score"],
                json.dumps(row["factor_scores"], ensure_ascii=False),
                _reason(row),
                json.dumps(row["risk_flags"], ensure_ascii=False),
                row["eligibility"],
                row["action"],
                row["evidence_status"],
                row["eligibility_migration_status"],
                row["primary_layer"],
                row["secondary_layers_json"],
                row["ai_relevance_level"],
                row["layer_confidence"],
                row["classification_version"],
                row["classification_rationale"],
                market_phase_snapshot_id,
                row["short_term_view_json"],
                row["six_month_view_json"],
                row["long_term_view_json"],
                row["close"],
                row["currency"],
                now,
            ],
        )

    buy_rows = [
        r for r in selected
        if r["signal"] == "buy"
        and r.get("eligibility") == "buyable"
        and r.get("action") == "focus_research"
    ]
    buy_rows.sort(key=lambda x: x["total_score"], reverse=True)
    portfolio_rows = buy_rows[:portfolio_size]
    target_weight = round(1.0 / len(portfolio_rows), 6) if portfolio_rows else 0.0
    for row in portfolio_rows:
        conn.execute(
            """
            INSERT INTO portfolio_plans (
                run_id, plan_version, strategy_scope, market, symbol,
                target_weight, action, risk_limit_json, transaction_cost_bps,
                benchmark_symbol, created_at
            ) VALUES (?, 'v2_pre_optimizer_equal_weight', 'system_tech_universe', ?, ?, ?, 'pre_optimizer_placeholder', ?, 10, ?, ?)
            """,
            [
                run_id,
                row["market"],
                row["symbol"],
                target_weight,
                json.dumps({
                    "max_single_weight": 0.12,
                    "min_sample_note": "pre_optimizer_placeholder",
                    "replaced_by": "stock_research.jobs.optimize_portfolio -> v6_risk_aware",
                }, ensure_ascii=False),
                "SPY" if row["market"] == "US" else ("2800.HK" if row["market"] == "HK" else "000300.SH"),
                now,
            ],
        )

    if "pipeline_runs" in tables:
        conn.execute(
            """
            INSERT INTO pipeline_runs (
                run_id, mode, status, planned_at, started_at, completed_at, trigger_source
            ) VALUES (?, 'v2_recommendation', 'success', ?, ?, ?, 'manual')
            ON CONFLICT (run_id) DO UPDATE SET status='success', completed_at=excluded.completed_at
            """,
            [run_id, now, now, now],
        )
    if "pipeline_steps" in tables:
        conn.execute(
            """
            INSERT INTO pipeline_steps (
                run_id, step_name, status, started_at, ended_at, duration_seconds, sink, error_summary
            ) VALUES (?, 'build_v2_recommendations', 'success', ?, ?, 0, 'recommendation_runs,recommendation_picks,portfolio_plans', NULL)
            """,
            [run_id, now, now],
        )
    _write_data_usability_audit(data_usability_audit)
    conn.close()
    return {
        "db_path": str(db_path),
        "run_id": run_id,
        "dry_run": False,
        "pool_membership_count": pool_membership_count,
        "price_daily_count": price_daily_count,
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "portfolio_count": len(portfolio_rows),
        "data_usability_blocked_count": data_usability_audit["blocked_count"],
        "data_usability_attention_count": data_usability_audit["attention_count"],
        "p0_us_gated_out_count": data_usability_audit["p0_eligibility_gate"]["us_gated_out_count"],
        "p0_gated_out_reason_breakdown": data_usability_audit["p0_eligibility_gate"]["gated_out_reason_breakdown"],
        "market_phase_snapshot_id": market_phase_snapshot_id,
        "layer_counts": layer_counts,
        "evidence_seed_summary": evidence_seed_summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build v2 recommendations from system tech universe.")
    parser.add_argument("--db", default=os.environ.get("STOCK_DB_PATH") or str(config.DUCKDB_PATH))
    parser.add_argument("--top-per-market", type=int, default=20,
                        help="每个市场写入 recommendation_picks 的候选数；需足够大，供风险优化器选满目标持仓")
    parser.add_argument("--portfolio-size", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = build(
        Path(args.db).expanduser().resolve(),
        top_per_market=args.top_per_market,
        portfolio_size=args.portfolio_size,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"v2 recommendations: run_id={payload['run_id']}")
        print(
            f"  pool_membership={payload.get('pool_membership_count', 0)} "
            f"price_daily={payload.get('price_daily_count', 0)} "
            f"candidates={payload['candidate_count']} selected={payload['selected_count']}"
        )
        if not args.dry_run:
            print(f"  portfolio={payload['portfolio_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
