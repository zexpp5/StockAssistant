"""统一策略验证口径 —— 单一来源，给 validation / acceptance / strict_trial 共用。

为什么存在（2026-06-08）：
  旧口径有两个系统性虚高来源，导致 user-facing alpha/命中比真实强一倍：
    1. 含同日多批重复 pick —— 同一天多个 run 推荐同一票，被算多次（系统 evidence n=49）；
    2. 常漏按 strategy_version 过滤 —— 把上一版 tech_ai_v1 的样本掺进当前版本。
  双源验证：当前版本（tech_ai_v2_price_action_gate）严筛口径去重后 1D 仅 n=14、
  alpha ≈ +0.5% 一档，而非页面显示的 +1.59%。

本模块统一口径 = **当前 strategy_version + 每天只取最后一批 + (推荐日,股票) 去重**。
horizon 取 pick_outcomes 已成熟的 outcome（1d 有样本；5d/20d 待积累，口径见 NOTE）。

NOTE（5d/20d 交易日口径）：outcome 由生成端写入 pick_outcomes，本模块只做统计去重。
若 5d/20d 的 outcome 仍按「+N 自然日」生成，需在生成端统一成「第 N 个交易日」，
非本模块职责。当前 1d 已可用，5d/20d 暂无成熟样本。
"""
from __future__ import annotations

import ast
import json
import math
from typing import Any

import duckdb

DEFAULT_UNIVERSE = "system_tech_universe"
BUY_SIGNALS = ("buy", "strong_buy")
DEFAULT_BLOCKED_FLAGS = ("OVERHEATED_1Y",)
POLICY_COLUMNS = (
    "eligibility",
    "action",
    "evidence_status",
    "eligibility_migration_status",
    "primary_layer",
    "secondary_layers_json",
    "ai_relevance_level",
    "layer_confidence",
    "classification_version",
    "market_phase_snapshot_id",
    "short_term_view_json",
    "six_month_view_json",
    "long_term_view_json",
)


# ────────────────────────────────────────────────────────
# JSON 解析（兼容 dict / json 字符串 / python repr）
# ────────────────────────────────────────────────────────

def _safe_obj(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return fallback
    text = str(value).strip()
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(text)
            if parsed is not None:
                return parsed
        except Exception:
            continue
    return fallback


def _momentum(factor_scores_json: Any) -> float | None:
    obj = _safe_obj(factor_scores_json, {})
    if not isinstance(obj, dict):
        return None
    try:
        v = float(obj.get("momentum"))
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _risk_codes(risk_flags_json: Any) -> list[str]:
    obj = _safe_obj(risk_flags_json, [])
    if not isinstance(obj, list):
        obj = [obj]
    codes: list[str] = []
    for item in obj:
        if isinstance(item, dict):
            code = item.get("code") or item.get("name") or item.get("flag")
            if code:
                codes.append(str(code))
        elif item not in (None, "", "None"):
            codes.append(str(item))
    return codes


def _table_columns(conn: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    except Exception:
        return set()
    return {str(row[1]) for row in rows}


def _optional_pick_expr(columns: set[str], column: str) -> str:
    if column in columns:
        return f"rp.{column} AS {column}"
    return f"NULL AS {column}"


def _safe_list(value: Any) -> list[Any]:
    obj = _safe_obj(value, [])
    return obj if isinstance(obj, list) else []


# ────────────────────────────────────────────────────────
# 口径核心
# ────────────────────────────────────────────────────────

def latest_strategy_version(conn: duckdb.DuckDBPyConnection,
                            *, universe_scope: str = DEFAULT_UNIVERSE) -> str | None:
    """当前生产策略版本 = 最新一条 generated run 的 strategy_version（不写死）。"""
    row = conn.execute(
        """
        SELECT strategy_version FROM recommendation_runs
        WHERE universe_scope = ? AND status = 'generated'
          AND strategy_version IS NOT NULL AND strategy_version <> ''
        ORDER BY generated_at DESC LIMIT 1
        """,
        [universe_scope],
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def last_batch_run_ids(conn: duckdb.DuckDBPyConnection, *,
                       strategy_version: str | None,
                       universe_scope: str = DEFAULT_UNIVERSE,
                       metrics_start: str | None = None) -> set[str]:
    """每个 run_date 内 generated_at 最晚的 run_id（每天最后一批，天然去同日重复）。

    strategy_version=None 表示不按版本过滤（一般应传当前版本）。
    """
    rows = conn.execute(
        """
        SELECT run_id FROM (
            SELECT run_id,
                   ROW_NUMBER() OVER (PARTITION BY run_date ORDER BY generated_at DESC) AS rn
            FROM recommendation_runs
            WHERE universe_scope = ? AND status = 'generated'
              AND (? IS NULL OR strategy_version = ?)
              AND (? IS NULL OR run_date >= ?)
        ) WHERE rn = 1
        """,
        [universe_scope, strategy_version, strategy_version, metrics_start, metrics_start],
    ).fetchall()
    return {str(r[0]) for r in rows}


def mature_samples(conn: duckdb.DuckDBPyConnection, *,
                   market: str,
                   horizon: str = "1d",
                   strategy_version: str | None = "latest",
                   universe_scope: str = DEFAULT_UNIVERSE,
                   metrics_start: str | None = None) -> list[dict[str, Any]]:
    """统一口径的去重成熟样本。

    每行 = (推荐日, 股票) 唯一，含 market_rank / momentum / risk_codes /
    alpha_pct / is_success / return_pct。口径：当前版本 + 每天最后一批 + 已成熟 outcome。
    strategy_version="latest" 自动解析当前版本；传 None 则不过滤版本。
    """
    sv = latest_strategy_version(conn, universe_scope=universe_scope) \
        if strategy_version == "latest" else strategy_version
    run_ids = last_batch_run_ids(
        conn, strategy_version=sv, universe_scope=universe_scope, metrics_start=metrics_start)
    if not run_ids:
        return []
    placeholders = ",".join(["?"] * len(run_ids))
    columns = _table_columns(conn, "recommendation_picks")
    optional_selects = ",\n                   ".join(
        _optional_pick_expr(columns, column) for column in POLICY_COLUMNS
    )
    optional_final_selects = ", ".join(f"p.{column}" for column in POLICY_COLUMNS)
    sql = f"""
        WITH picks AS (
            SELECT rp.run_id, CAST(rr.run_date AS VARCHAR) AS run_date, rp.symbol,
                   rp.factor_scores_json, rp.risk_flags_json,
                   {optional_selects},
                   ROW_NUMBER() OVER (
                       PARTITION BY rp.run_id
                       ORDER BY rp.total_score DESC NULLS LAST, rp.symbol ASC
                   ) AS market_rank
            FROM recommendation_picks rp
            JOIN recommendation_runs rr ON rr.run_id = rp.run_id
            WHERE rp.run_id IN ({placeholders})
              AND rp.market = ?
              AND LOWER(COALESCE(rp.signal, rp.rating, '')) IN ('buy', 'strong_buy')
        )
        SELECT p.run_date, p.symbol, p.market_rank, p.factor_scores_json, p.risk_flags_json,
               {optional_final_selects},
               po.horizon, po.alpha_pct, po.is_success, po.return_pct
        FROM picks p
        JOIN pick_outcomes po
          ON po.run_id = p.run_id AND po.market = ? AND po.symbol = p.symbol AND po.horizon = ?
        WHERE po.alpha_pct IS NOT NULL AND isfinite(po.alpha_pct)
        ORDER BY p.run_date, p.market_rank
    """
    params = list(run_ids) + [market, market, horizon]
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in conn.execute(sql, params).fetchall():
        rd, sym, mr, fj, rj = row[:5]
        policy_values = row[5:5 + len(POLICY_COLUMNS)]
        horizon_value, alpha, succ, ret = row[5 + len(POLICY_COLUMNS):]
        key = (str(rd), str(sym))
        if key in seen:  # (推荐日,股票) 去重（每天最后一批已基本唯一，双保险）
            continue
        seen.add(key)
        sample = {
            "run_date": str(rd),
            "market": market,
            "symbol": str(sym),
            "horizon": str(horizon_value),
            "market_rank": int(mr),
            "momentum": _momentum(fj),
            "risk_codes": _risk_codes(rj),
            "alpha_pct": float(alpha) if alpha is not None else None,
            "is_success": bool(succ),
            "return_pct": float(ret) if ret is not None else None,
        }
        sample.update(dict(zip(POLICY_COLUMNS, policy_values)))
        sample["secondary_layers"] = _safe_list(sample.get("secondary_layers_json"))
        out.append(sample)
    return out


# ────────────────────────────────────────────────────────
# 子集谓词 + 汇总
# ────────────────────────────────────────────────────────

def is_top(sample: dict[str, Any], max_rank: int = 5) -> bool:
    return sample.get("market_rank", 9999) <= max_rank


def is_strict(sample: dict[str, Any], *,
              max_rank: int = 5, momentum_lt: float = 80.0,
              blocked_flags: tuple[str, ...] = DEFAULT_BLOCKED_FLAGS) -> bool:
    """严筛口径：Top market_rank + momentum<阈值 + 无过热红旗。"""
    mom = sample.get("momentum")
    return (
        sample.get("market_rank", 9999) <= max_rank
        and mom is not None and mom < momentum_lt
        and not (set(sample.get("risk_codes") or []) & set(blocked_flags))
    )


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """对一组样本算 n / 平均 alpha / 命中率（alpha>0 占比）。"""
    return summarize_distribution(samples)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def sample_power(n: int) -> dict[str, Any]:
    """样本量等级。n<20 只能展示，不能作为升级/调参依据。"""
    if n < 20:
        key = "display_only"
        label = "只展示：样本不足 20，不能作为升级依据"
    elif n < 50:
        key = "weak_reference"
        label = "弱参考：样本 20-49，只能辅助观察"
    elif n < 100:
        key = "initial_reference"
        label = "初步参考：样本 50-99，可用于复核方向"
    else:
        key = "upgrade_evidence"
        label = "可验证：样本 >=100，才可讨论升级"
    return {
        "sample_power": key,
        "sample_power_label": label,
        "sample_floor_pass": n >= 20,
    }


def summarize_distribution(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """验证用分布统计：中位数、胜率、最大亏损、去极端赢家。

    平均 alpha 容易被一两个大赢家拉高；用户侧判断优先看
    median_alpha_pct / win_rate_pct / avg_without_best_alpha_pct。
    """
    alphas = [s["alpha_pct"] for s in samples
              if s.get("alpha_pct") is not None and math.isfinite(s["alpha_pct"])]
    if not alphas:
        out = {
            "n": 0,
            "wins": 0,
            "losses": 0,
            "avg_alpha_pct": None,
            "median_alpha_pct": None,
            "win_rate_pct": None,
            "best_alpha_pct": None,
            "worst_alpha_pct": None,
            "max_loss_pct": None,
            "avg_without_best_alpha_pct": None,
            "median_without_best_alpha_pct": None,
            "extreme_winner_dependency": False,
        }
        out.update(sample_power(0))
        return out
    wins = sum(1 for a in alphas if a > 0)
    losses = sum(1 for a in alphas if a < 0)
    best = max(alphas)
    worst = min(alphas)
    without_best = list(alphas)
    without_best.remove(best)
    avg = sum(alphas) / len(alphas)
    avg_without_best = (sum(without_best) / len(without_best)) if without_best else None
    median = _median(alphas)
    median_without_best = _median(without_best)
    extreme_winner_dependency = (
        len(alphas) >= 5
        and avg > 0
        and (
            (median is not None and median <= 0)
            or (avg_without_best is not None and avg_without_best <= 0)
        )
    )
    out = {
        "n": len(alphas),
        "wins": wins,
        "losses": losses,
        "avg_alpha_pct": round(avg, 4),
        "median_alpha_pct": round(median, 4) if median is not None else None,
        "win_rate_pct": round(wins / len(alphas) * 100, 2),
        "best_alpha_pct": round(best, 4),
        "worst_alpha_pct": round(worst, 4),
        "max_loss_pct": round(min(0.0, worst), 4),
        "avg_without_best_alpha_pct": round(avg_without_best, 4) if avg_without_best is not None else None,
        "median_without_best_alpha_pct": (
            round(median_without_best, 4) if median_without_best is not None else None
        ),
        "extreme_winner_dependency": extreme_winner_dependency,
    }
    out.update(sample_power(len(alphas)))
    return {
        **out,
    }
