"""Daily review for the user's real holdings.

This job evaluates only `real_holdings`. It does not create a stock pool, does
not write recommendation_picks, and does not mutate model_sim_holdings.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import zoneinfo
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import fx_rates  # type: ignore
import stock_db  # type: ignore
from early_signals import score_analyst  # type: ignore
from factor_model import combine_factors  # type: ignore
from stock_research.core.hk_scoring import (
    HK_FACTOR_WEIGHTS,
    HK_RECOMMEND_THRESHOLD,
    hk_grade_label,
    score_hk_entries,
)
from stock_research.core.us_risk_flags import build_us_equity_risk_flags_from_fundamental
from stock_research.core.industry_heat import _load_sector_rotation, resolve_industry_heat
from stock_research.core.portfolio_constraints import kelly_cap
from stock_research.jobs.morning_brief import compute_holdings_verdict


OUT_PATH = REPO / "data" / "latest" / "real_holding_review.json"
logger = logging.getLogger(__name__)

ACTION_PRIORITY = {
    "风险复查": 1,
    "减仓观察": 2,
    "补数据": 3,
    "事件观察": 4,
    "关注加仓": 5,
    "持有观察": 6,
    "仅风控跟踪": 7,
}

_TREATMENT_CLASS_ALIASES = {
    "ai_portfolio": "portfolio_model",
    "portfolio_model": "portfolio_model",
    "picks_only": "stock_score",
    "stock_score": "stock_score",
    "tracking_only": "risk_only",
    "risk_only": "risk_only",
    "needs_fix": "data_blocked",
    "data_blocked": "data_blocked",
}


def _default_rules() -> dict[str, float | str]:
    return dict(stock_db.USER_CONFIG_DEFAULTS["real_holding_review_rules"])


def _load_review_rules(conn=None) -> dict[str, Any]:
    rules = _default_rules()
    try:
        configured = stock_db.get_config("real_holding_review_rules", conn=conn)
    except Exception as exc:
        logger.warning("real_holding_review_rules 读取失败,使用默认规则: %s", exc)
        configured = None
    if isinstance(configured, dict):
        rules.update(configured)
    return rules


def _normalize_treatment_class(treatment_class: Any, coverage_class: Any = None) -> str:
    for raw in (treatment_class, coverage_class):
        if raw:
            raw_text = str(raw)
            return _TREATMENT_CLASS_ALIASES.get(raw_text, raw_text)
    return "stock_score"


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except Exception:
        return None
    if not math.isfinite(v):
        return None
    return v


def _latest_prices_by_symbol(conn, symbols: list[str]) -> dict[str, dict]:
    if not symbols:
        return {}
    placeholders = ",".join(["?"] * len(symbols))
    # 取每只票的最近两条 close,用于计算"今日盈亏"(latest vs prev)。
    # row_num=1 是 latest,row_num=2 是 prev_close;只有一条历史时 prev_close 缺失。
    rows = conn.execute(
        f"""
        SELECT market, symbol, trade_date, close, currency, source, fetched_at, row_num
        FROM (
          SELECT market, symbol, trade_date, close, currency, source, fetched_at,
                 ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS row_num
          FROM price_daily
          WHERE symbol IN ({placeholders})
        )
        WHERE row_num <= 2
        ORDER BY symbol, row_num
        """,
        symbols,
    ).fetchall()
    out: dict[str, dict] = {}
    for market, symbol, trade_date, close, currency, source, fetched_at, row_num in rows:
        key = str(symbol)
        if row_num == 1:
            out[key] = {
                "market": market,
                "symbol": symbol,
                "trade_date": trade_date,
                "close": close,
                "currency": currency,
                "source": source,
                "fetched_at": fetched_at,
            }
        elif row_num == 2 and key in out:
            out[key]["prev_close"] = close
            out[key]["prev_trade_date"] = trade_date
    return out


def _latest_picks_by_symbol(conn, symbols: list[str]) -> dict[str, dict]:
    if not symbols:
        return {}
    placeholders = ",".join(["?"] * len(symbols))
    rows = conn.execute(
        f"""
        SELECT *
        FROM (
          SELECT rp.market, rp.symbol, rp.name, rp.rating, rp.signal, rp.total_score,
                 rp.recommendation_reason, rp.risk_flags_json, rp.universe_scope,
                 rr.run_id, rr.run_date, rr.generated_at,
                 ROW_NUMBER() OVER (PARTITION BY rp.symbol ORDER BY rr.generated_at DESC) AS rn
          FROM recommendation_picks rp
          JOIN recommendation_runs rr ON rp.run_id = rr.run_id
          WHERE rp.symbol IN ({placeholders})
            AND rr.status = 'generated'
        )
        WHERE rn = 1
        ORDER BY symbol
        """,
        symbols,
    ).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        (
            market, symbol, name, rating, signal, total_score, reason, risk_flags_json,
            universe_scope, run_id, run_date, generated_at, _rn,
        ) = row
        try:
            risk_flags = json.loads(risk_flags_json) if risk_flags_json else []
        except Exception:
            risk_flags = []
        out[str(symbol)] = {
            "market": market,
            "symbol": symbol,
            "name": name,
            "rating": rating,
            "signal": signal,
            "total_score": total_score,
            "recommendation_reason": reason,
            "risk_flags": risk_flags,
            "universe_scope": universe_scope,
            "run_id": run_id,
            "run_date": run_date,
            "generated_at": generated_at,
        }
    return out


def _symbol_aliases(symbol: str) -> set[str]:
    s = str(symbol or "").strip()
    aliases = {s}
    if "-" in s:
        aliases.add(s.replace("-", "."))
    if "." in s:
        aliases.add(s.replace(".", "-"))
    return {x for x in aliases if x}


def _us_watchlist_rating(z: float, coverage: float, cutoff: float) -> tuple[str, str]:
    neg_cutoff = -0.5
    if coverage < 0.50:
        return f"⭐ 观察（数据覆盖 {coverage:.0%} < 50%，不进 buy）", "watch"
    if z >= 1.0:
        return "⭐⭐⭐ 强烈推荐（z ≥ 1）", "buy"
    if z >= 0.5:
        return "⭐⭐ 推荐（z ≥ 0.5）", "buy"
    if z <= neg_cutoff:
        return f"⛔ 不建议（z ≤ {neg_cutoff}）", "avoid"
    if z >= cutoff:
        return f"⭐ 关注（z ≥ {cutoff:.2f}）", "buy"
    return f"⭐ 观察（-0.5 < z < {cutoff:.2f}）", "watch"


def _factor_weights_from_cache(cache: dict) -> dict[str, float] | None:
    raw = cache.get("factor_weights_used")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    if not isinstance(raw, dict):
        return None
    out: dict[str, float] = {}
    for key, value in raw.items():
        v = _as_float(value)
        if v is not None:
            out[str(key)] = v
    return out or None


def _pick_from_watchlist_score_row(row: dict, *, matched_symbol: str, cache: dict) -> dict:
    score = _as_float(row.get("total_score"))
    z = _as_float(row.get("composite_z"))
    if z is None and score is not None:
        z = score / 100.0
    coverage = _as_float(row.get("coverage_score")) or 0.0
    cutoff = _as_float(cache.get("cutoff")) or 0.0
    rating = row.get("rating")
    signal = row.get("signal")
    if not rating or not signal:
        rating, signal = _us_watchlist_rating(z or 0.0, coverage, cutoff)
    risk_flags = row.get("risk_flags") or []
    if isinstance(risk_flags, str):
        risk_flags = [risk_flags] if risk_flags else []
    return {
        "market": row.get("market") or stock_db._infer_market_from_ticker(matched_symbol),
        "symbol": matched_symbol,
        "name": row.get("name") or matched_symbol,
        "rating": rating,
        "signal": signal,
        "total_score": round(score, 2) if score is not None else (round((z or 0.0) * 100, 2) if z is not None else None),
        "recommendation_reason": "由 daily_picks 自选股评分快照兜底，与主路径同公式",
        "risk_flags": risk_flags,
        "universe_scope": "manual_watchlist",
        "run_id": "factor_scores_today",
        "run_date": cache.get("date"),
        "generated_at": cache.get("generated_at") or cache.get("date"),
        "coverage_score": coverage,
        "missing_factors": row.get("missing_factors"),
        "factor_weights_used": row.get("factor_weights_used") or cache.get("factor_weights_used"),
    }


def _manual_watchlist_score_fallbacks(symbols: list[str]) -> dict[str, dict]:
    """Fallback for real holdings that are not in system-universe recommendation_picks.

    `daily_picks_v5.py` writes factor_scores_today.json for manual watchlist ratings,
    while real_holding_review needs a pick-like shape. This keeps real holdings from
    showing "no score" when the manual-watchlist factor cache already has enough data.
    """
    needed: dict[str, str] = {}
    for sym in symbols:
        for alias in _symbol_aliases(sym):
            needed[alias.upper()] = sym
    if not needed:
        return {}

    cache = _load_json(REPO / "data" / "latest" / "factor_scores_today.json")
    scored_rows = cache.get("watchlist_scores") if isinstance(cache, dict) else None
    if isinstance(scored_rows, list):
        out: dict[str, dict] = {}
        for row in scored_rows:
            if not isinstance(row, dict):
                continue
            tk = str(row.get("code") or row.get("symbol") or row.get("ticker") or "")
            matched = None
            for alias in _symbol_aliases(tk):
                matched = needed.get(alias.upper())
                if matched:
                    break
            if not matched:
                continue
            exact_match = tk.upper() == matched.upper()
            if matched in out and not exact_match:
                continue
            out[matched] = _pick_from_watchlist_score_row(row, matched_symbol=matched, cache=cache)
        return out

    factors = cache.get("factors") if isinstance(cache, dict) else None
    signals = cache.get("signals") if isinstance(cache, dict) else None
    fundamentals = cache.get("fundamentals") if isinstance(cache, dict) else None
    if not isinstance(factors, list) or not factors:
        return {}

    try:
        signal_by_ticker = {str(s.get("ticker")): s for s in (signals or []) if isinstance(s, dict)}
        analyst_scores = {}
        for tk, sig in signal_by_ticker.items():
            analyst = sig.get("analyst") or {}
            if isinstance(analyst, dict) and "error" not in analyst:
                analyst_scores[tk] = score_analyst(analyst)[0]
            else:
                analyst_scores[tk] = None
        df = combine_factors(
            factors,
            analyst_signals=analyst_scores,
            include_reversal=True,
            factor_weights=_factor_weights_from_cache(cache),
        )
    except Exception as exc:
        logger.warning("factor_scores_today fallback 评分失败: %s", exc)
        return {}

    out: dict[str, dict] = {}
    fundamental_by_code = {
        str(x.get("ticker")): x
        for x in (fundamentals or [])
        if isinstance(x, dict) and x.get("ticker")
    }
    try:
        cutoff = float(df["composite"].quantile(2 / 3))
    except Exception:
        cutoff = 0.0
    for _, row in df.iterrows():
        tk = str(row.get("ticker") or "")
        matched = None
        for alias in _symbol_aliases(tk):
            matched = needed.get(alias.upper())
            if matched:
                break
        if not matched:
            continue
        z = _as_float(row.get("composite")) or 0.0
        coverage = _as_float(row.get("coverage_score")) or 0.0
        rating, signal = _us_watchlist_rating(z, coverage, cutoff)
        risk_flags = build_us_equity_risk_flags_from_fundamental(fundamental_by_code.get(tk))
        if risk_flags:
            rating = rating + " · " + "｜".join(risk_flags)
        out[matched] = {
            "market": stock_db._infer_market_from_ticker(matched),
            "symbol": matched,
            "name": matched,
            "rating": rating,
            "signal": signal,
            "total_score": round(z * 100, 2),
            "recommendation_reason": "由 daily_picks 自选股因子缓存兜底打分，与主路径同公式",
            "risk_flags": risk_flags,
            "universe_scope": "manual_watchlist",
            "run_id": "factor_scores_today",
            "run_date": cache.get("date"),
            "generated_at": cache.get("date"),
            "coverage_score": coverage,
            "missing_factors": row.get("missing_factors"),
            "factor_weights_used": row.get("factor_weights_used") or cache.get("factor_weights_used"),
        }
    return out


def _parse_iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


_MARKET_TZ = {
    "HK": "Asia/Hong_Kong",
    "港股": "Asia/Hong_Kong",
    "CN": "Asia/Shanghai",
    "A股": "Asia/Shanghai",
    "US": "America/New_York",
    "美股": "America/New_York",
}


def _market_local_date(symbol: str, market: Any = None) -> date:
    """该 symbol 所属市场的"当前本地日期",用于判断行情是否还停留在上一交易日。

    盘前/闭市时 price_daily 最新一条仍是上一交易日收盘(intraday 刷新拒绝写
    phantom 行),其 trade_date < 本地今天 → 据此把价格标成"昨收"而非当日实时。
    """
    s = str(symbol or "").upper()
    if s.endswith(".HK"):
        tz_name = "Asia/Hong_Kong"
    elif s.endswith((".SS", ".SZ", ".SH")):
        tz_name = "Asia/Shanghai"
    else:
        tz_name = _MARKET_TZ.get(str(market or "").strip(), "America/New_York")
    try:
        return datetime.now(zoneinfo.ZoneInfo(tz_name)).date()
    except Exception:
        return datetime.now().date()


def _hk_watchlist_score_fallbacks(symbols: list[str], *, max_age_days: float = 3.0) -> dict[str, dict]:
    """Fallback HK manual-watchlist scoring from hk_factor_cache.json.

    `hk_picks.py` may be forced to dry-run by production audit gates, but it
    still refreshes factor cache. Real holding review can use that cache for an
    advisory stock score without writing production recommendations. The scoring
    formula is shared with hk_picks via `stock_research.core.hk_scoring`.
    """
    hk_symbols = [s for s in symbols if str(s or "").upper().endswith(".HK")]
    if not hk_symbols:
        return {}
    cache = _load_json(REPO / "data" / "latest" / "hk_factor_cache.json")
    items = cache.get("items") if isinstance(cache, dict) else None
    if not isinstance(items, dict):
        return {}

    today = date.today()
    entries: list[SimpleNamespace] = []
    item_by_symbol: dict[str, dict] = {}
    for sym in hk_symbols:
        item = items.get(sym)
        factor = item.get("factor") if isinstance(item, dict) else None
        item_date = _parse_iso_date(item.get("date") if isinstance(item, dict) else None)
        if not isinstance(factor, dict) or item_date is None:
            continue
        age_days = (today - item_date).days
        if age_days < 0 or age_days > max_age_days:
            continue
        piotroski = factor.get("piotroski") or {}
        momentum = factor.get("momentum") or {}
        f_score = _as_float(piotroski.get("f_score"))
        data_quality = str(piotroski.get("data_quality") or "partial")
        entries.append(SimpleNamespace(
            code=sym,
            name=sym,
            market="港股",
            sector="",
            f_score=int(f_score) if f_score is not None else None,
            f_score_norm=(f_score / 9.0) if f_score is not None else None,
            momentum_12_1=_as_float(momentum.get("momentum_12_1")),
            reversal_1m=_as_float(momentum.get("reversal_1m")),
            south_pct=None,
            south_rank=None,
            south_score=0.5,
            data_quality=data_quality,
            notes=[],
        ))
        item_by_symbol[sym] = item
    if not entries:
        return {}

    entries, _selected, _cutoff, _sector_skipped = score_hk_entries(
        entries,
        mode="tertile",
        top_k=max(1, len(entries)),
        factor_weights=HK_FACTOR_WEIGHTS,
    )

    out: dict[str, dict] = {}
    for e in entries:
        sym = e.code
        composite = float(getattr(e, "composite", 0.0) or 0.0)
        item = item_by_symbol.get(sym) or {}
        item_date = str(item.get("date") or "")
        out[sym] = {
            "market": "HK",
            "symbol": sym,
            "name": sym,
            "rating": hk_grade_label(e),
            "signal": "buy" if composite >= HK_RECOMMEND_THRESHOLD else "watch",
            "total_score": round(composite * 100, 2),
            "recommendation_reason": "由 hk_picks 因子缓存兜底打分，与主路径同公式",
            "risk_flags": [],
            "universe_scope": "manual_watchlist",
            "run_id": "hk_factor_cache",
            "run_date": item_date,
            "generated_at": cache.get("updated_at") or item_date,
            "coverage_score": float(getattr(e, "coverage_score", 0.0) or 0.0),
            "missing_factors": getattr(e, "missing_factors", ""),
            "factor_weights_used": json.dumps(HK_FACTOR_WEIGHTS, ensure_ascii=False, sort_keys=True),
        }
    return out


def _target_weights_from_plan() -> dict[str, float]:
    plan = _load_json(REPO / "data" / "latest" / "plan_a_v5.json")
    rows = plan.get("plan_v6") or plan.get("plan_v5") or plan.get("plan") or []
    out: dict[str, float] = {}
    for it in rows:
        sym = it.get("ticker") or it.get("symbol") or it.get("code")
        w = it.get("target_weight") or it.get("capped_weight") or it.get("weight")
        val = _as_float(w)
        if sym and val is not None:
            out[str(sym)] = val
    return out


def _review_action(
    *,
    rules: dict[str, Any],
    verdict: dict | None,
    treatment_class: str,
    score: float | None,
    pnl_pct: float | None,
    weight_gap_pt: float | None,
    missing_price: bool,
) -> str:
    label_kind = (verdict or {}).get("label_kind")
    if treatment_class == "data_blocked" or missing_price:
        return "补数据"
    if treatment_class == "risk_only":
        if label_kind == "stop_breach":
            return "风险复查"
        if pnl_pct is not None and pnl_pct <= float(rules["tracking_loss_review_pct"]):
            return "风险复查"
        if label_kind == "stop_watch":
            return "减仓观察"
        return "仅风控跟踪"
    if score is None:
        return "补数据"
    if pnl_pct is not None and pnl_pct <= float(rules["loss_review_pct"]):
        return "风险复查"
    if label_kind == "stop_breach":
        return "风险复查"
    if label_kind in {"stop_watch", "model_weak"}:
        return "减仓观察"
    if score is not None and score < float(rules["weak_score_threshold"]):
        return "减仓观察"
    if label_kind == "near_event":
        return "事件观察"
    if (
        weight_gap_pt is not None
        and weight_gap_pt <= float(rules["underweight_add_gap_pt"])
        and score is not None
        and score >= float(rules["add_watch_min_score"])
    ):
        return "关注加仓"
    return "持有观察"


def _bounded_score(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(100.0, round(float(value), 1)))


# Disobedient action labels — 模型反对你仍加仓时算"没听话"，
# 给 weekly_self_review 等下游 import 用，避免 magic string 两处 drift。
DISOBEDIENT_ACTIONS: frozenset[str] = frozenset({"风险复查", "减仓观察"})


def _market_lot_size(symbol: str) -> int:
    """最小交易单位（一手）。A 股 100；港股保守默认 100（实际 lot 因股而异 50-1000）；
    其他 1。advisory 性质，宁可少建议也不建议下不了单的散股。"""
    s = (symbol or "").upper().strip()
    if s.endswith((".SS", ".SZ", ".SH", ".BJ")):
        return 100
    if s.endswith(".HK"):
        return 100
    return 1


def _round_shares_down_to_lot(shares_float: float, lot: int) -> int:
    if shares_float <= 0:
        return 0
    n = int(shares_float)
    if lot <= 1:
        return n
    return (n // lot) * lot


def _suggest_size_advisory(
    *,
    rules: dict[str, Any],
    action: str,
    symbol: str,
    shares: float,
    current_price: float | None,
    fx: float,
    current_value_rmb: float,
    current_weight: float | None,
    target_weight: float | None,
    total_capital: float,
    treatment_class: str,
    is_fallback_pick: bool = False,
) -> dict[str, Any] | None:
    """把 verdict 转成可参考的规模提示（advisory，不自动下单）。"""
    if treatment_class in {"data_blocked", "risk_only"} and action in {"补数据", "仅风控跟踪"}:
        # 2026-05-29: 不再因为占比 ≥ 25% 自动建议减仓。
        # 保留 over_hard_cap 提示让前端显示「仓位偏重」红字，但动作档保持 hold。
        if current_weight is not None and current_weight >= float(rules.get("hard_single_cap_pct", 0.25)):
            hard = float(rules.get("hard_single_cap_pct", 0.25))
            return {
                "advisory_only": True,
                "direction": "hold",
                "suggested_action_rmb": None,
                "suggested_shares": None,
                "suggested_batches": int(rules.get("suggested_batches", 3)),
                "suggested_batch_note": "仓位偏重提示（不是减仓建议）",
                "kelly_cap_pct": None,
                "hard_cap_pct": hard,
                "over_hard_cap": True,
            }
        return None

    if current_price is None or current_price <= 0 or total_capital <= 0:
        return None

    kelly_fraction = float(rules.get("kelly_fraction", 0.5))
    max_single = float(rules.get("max_single_pct", 0.15))
    hard_cap = float(rules.get("hard_single_cap_pct", 0.25))
    kelly_pct = max_single * kelly_fraction
    batches = int(rules.get("suggested_batches", 3))
    batch_note = f"可考虑分{batches}批，每批不超过建议变动额的 1/3"
    price_rmb = current_price * fx
    over_hard = current_weight is not None and current_weight >= hard_cap
    lot = _market_lot_size(symbol)

    advisory: dict[str, Any] = {
        "advisory_only": True,
        "kelly_cap_pct": round(kelly_pct, 4),
        "hard_cap_pct": hard_cap,
        "over_hard_cap": over_hard,
        "suggested_batches": batches,
        "suggested_batch_note": batch_note,
        "lot_size": lot,
    }

    # 2026-05-29: 不再因为占比 ≥ 25% 自动建议减仓。
    # over_hard_cap 仍写入 advisory（前端显示「仓位偏重」红字提示），
    # 但 direction=trim 仅对 DISOBEDIENT_ACTIONS（违规清单）触发。
    # 进一步：fallback 评分（universe_scope=manual_watchlist，非 V2 系统推荐池）
    # 不触发 trim — 因子尺子可能不适用（如 popmart 用 HK 科技因子）。
    if action in DISOBEDIENT_ACTIONS and not is_fallback_pick:
        trim_to = min(hard_cap, kelly_pct) if target_weight is None else min(
            hard_cap, max(target_weight, kelly_pct)
        )
        target_val = trim_to * total_capital
        trim_rmb = max(0.0, current_value_rmb - target_val)
        # 减仓建议不能超过当前持有股数；按市场最小一手向下取整
        raw_trim_shares = trim_rmb / price_rmb if trim_rmb > 0 else 0.0
        trim_shares = min(int(shares), _round_shares_down_to_lot(raw_trim_shares, lot))
        advisory.update({
            "direction": "trim",
            "suggested_action_rmb": round(trim_rmb, 2) if trim_rmb > 0 else None,
            "suggested_shares": trim_shares if trim_shares > 0 else None,
        })
        return advisory

    # over_hard 单独路径：动作档 hold（仓位偏重提示），不出减仓数量
    if over_hard:
        advisory.update({
            "direction": "hold",
            "suggested_action_rmb": None,
            "suggested_shares": None,
            "suggested_batch_note": "仓位偏重提示（不是减仓建议）",
        })
        return advisory

    if action == "关注加仓" and target_weight is not None and current_weight is not None:
        capped = kelly_cap({symbol: target_weight}, max_single_pct=max_single, kelly_fraction=kelly_fraction)
        eff_target = capped.get(symbol, target_weight)
        target_val = eff_target * total_capital
        add_rmb = max(0.0, target_val - current_value_rmb)
        # 缺口不足一手就不出建议（避免推"加仓 30 股 A 股"这种下不了的单）
        min_meaningful_rmb = max(price_rmb * 0.5, price_rmb * lot)
        if add_rmb < min_meaningful_rmb:
            return None
        add_shares = _round_shares_down_to_lot(add_rmb / price_rmb, lot)
        if add_shares < lot:
            return None
        advisory.update({
            "direction": "add",
            "suggested_action_rmb": round(add_rmb, 2),
            "suggested_shares": add_shares,
        })
        return advisory

    if action == "持有观察" and target_weight is not None and current_weight is not None:
        gap = target_weight - (current_weight or 0.0)
        if abs(gap) < 0.005:
            advisory.update({"direction": "hold", "suggested_action_rmb": None, "suggested_shares": None})
            return advisory

    return None


def _build_item(
    holding: dict,
    *,
    rules: dict[str, Any] | None = None,
    price: dict | None,
    pick: dict | None,
    verdict: dict | None,
    total_capital: float,
    target_weights: dict[str, float],
    industry_heat: dict | None = None,
) -> dict:
    rules = rules or _default_rules()
    symbol = str(holding.get("symbol") or holding.get("code"))
    coverage_class = (verdict or {}).get("coverage_class")
    treatment_class = _normalize_treatment_class((verdict or {}).get("treatment_class"), coverage_class)
    asset_class = (verdict or {}).get("asset_class") or "equity"
    shares = _as_float(holding.get("shares")) or 0.0
    entry_price = _as_float(holding.get("entry_price")) or 0.0
    cost_rmb = _as_float(holding.get("cost_rmb_locked"))
    if cost_rmb is None:
        entry_fx = _as_float(holding.get("entry_fx_rate")) or fx_rates.get_fx_to_rmb(holding.get("currency"))
        cost_rmb = entry_price * shares * entry_fx

    current_price = _as_float((price or {}).get("close"))
    current_currency = (price or {}).get("currency") or fx_rates.infer_currency_from_ticker(symbol)
    fx = fx_rates.get_fx_to_rmb(current_currency)
    missing_price = current_price is None
    current_value_rmb = cost_rmb if missing_price else current_price * shares * fx
    pnl_rmb = current_value_rmb - cost_rmb
    pnl_pct = (pnl_rmb / cost_rmb * 100.0) if cost_rmb else None
    # 当日盈亏: 只有行情日期已到市场本地今天时才计算。
    # 若仍停留在上一交易日收盘(盘前/未刷新),不能把上一交易日涨跌冒充成今天盈亏。
    # 2026-05-29 修复: 当日新建仓 (entry_date > prev_trade_date) 时改用 entry_price 作基准 —
    # 否则会把"持有一整天的市场涨跌"算给今天才进场的仓位,造成虚假浮盈/浮亏。
    # 2026-05-29 修复 v2: 当日建仓直接复用累计盈亏 (= 现市值 − 锁定成本),把汇兑变动也算进去,
    # 与"累计盈亏"列同口径;否则港股/美股会出现今日 ≠ 累计,新手看不懂。
    prev_close = _as_float((price or {}).get("prev_close"))
    prev_trade_date = (price or {}).get("prev_trade_date")
    price_trade_date = (price or {}).get("trade_date")
    price_source = str((price or {}).get("source") or "")
    hk_yfinance_unconfirmed = (
        symbol.upper().endswith(".HK")
        and price_source.lower().startswith("yfinance")
    )
    large_move_unconfirmed = "large_move" in price_source.lower()
    # 盘前/闭市信号: 最新可用收盘的 trade_date 早于该市场本地今天 → 仍是上一交易日收盘,
    # 不是当日实时价。比"current==prev_close"启发式稳(后者在 173.40≠161.5 时漏判)。
    price_is_prior_session = False
    if current_price is not None and price_trade_date is not None:
        _td = _parse_iso_date(price_trade_date)
        if _td is not None and _td < _market_local_date(symbol, holding.get("market")):
            price_is_prior_session = True
    entry_date_raw = holding.get("entry_date")
    entry_date_str = str(entry_date_raw)[:10] if entry_date_raw else None
    prev_date_str = str(prev_trade_date)[:10] if prev_trade_date else None
    use_entry_as_baseline = (
        entry_date_str is not None and prev_date_str is not None
        and entry_date_str > prev_date_str
        and entry_price > 0
    )
    day_change_rmb = None
    day_change_pct = None
    day_change_basis = None
    if price_is_prior_session:
        day_change_basis = "prior_session"
    elif use_entry_as_baseline and not missing_price:
        day_change_rmb = pnl_rmb
        day_change_pct = pnl_pct
        day_change_basis = "entry_cost"
    elif not missing_price and prev_close is not None and prev_close > 0:
        day_change_rmb = (current_price - prev_close) * shares * fx
        day_change_pct = (current_price / prev_close - 1.0) * 100.0
        day_change_basis = "prev_close"
    current_weight = (current_value_rmb / total_capital) if total_capital > 0 else None
    target_weight = target_weights.get(symbol)
    if treatment_class == "risk_only":
        target_weight = None
    weight_gap_pt = None
    if current_weight is not None and target_weight is not None:
        weight_gap_pt = (current_weight - target_weight) * 100.0

    raw_score = _as_float((pick or {}).get("total_score"))
    score = raw_score
    if score is not None:
        if pnl_pct is not None and pnl_pct <= float(rules["loss_review_pct"]):
            score = min(score, float(rules["loss_score_cap"]))
        label_kind = (verdict or {}).get("label_kind")
        if label_kind == "stop_breach":
            score = min(score, float(rules["stop_breach_score_cap"]))
        elif label_kind in {"stop_watch", "model_weak"}:
            score = min(score, float(rules["watch_score_cap"]))
        elif label_kind == "near_event":
            score -= float(rules["near_event_score_penalty"])
        if missing_price:
            score -= float(rules["missing_price_score_penalty"])
    score = _bounded_score(score)

    if treatment_class == "risk_only":
        score = None

    coverage_score = float(rules["coverage_base"])
    if not missing_price:
        coverage_score += float(rules["coverage_price"])
    has_model_score = bool(pick and raw_score is not None and treatment_class != "risk_only")
    if has_model_score:
        coverage_score += float(rules["coverage_model_score"])
    if target_weight is not None:
        coverage_score += float(rules["coverage_target_or_tracking"])
    elif treatment_class == "risk_only":
        coverage_score += float(rules["coverage_target_or_tracking"])
    coverage_score = round(min(1.0, coverage_score), 2)

    action = _review_action(
        rules=rules,
        verdict=verdict,
        treatment_class=treatment_class,
        score=score,
        pnl_pct=pnl_pct,
        weight_gap_pt=weight_gap_pt,
        missing_price=missing_price,
    )

    reasons: list[str] = []
    risk_flags: list[str] = []
    data_flags: list[str] = []

    if treatment_class == "risk_only":
        reasons.append("这类资产不适用股票因子模型,只做市值、盈亏和风控跟踪")
    if has_model_score and score is not None:
        score_line = f"最新股票评分 {score:.1f}"
        if raw_score is not None and raw_score > score + 1e-6:
            score_line += f"（原始 {raw_score:.1f}，展示封顶 100）"
        reasons.append(f"{score_line} · 评级 {pick.get('rating') or '-'}")
    elif treatment_class not in {"risk_only", "data_blocked"}:
        reasons.append("暂无当日股票评分,结论降级为观察")
        data_flags.append("no_model_score")
    if price and current_price is not None:
        # 行情 trade_date 早于市场本地今天 → 仍是上一交易日收盘（盘前/闭市未刷新），
        # 文案标"昨收"而非"最新"，并显示这条收盘的真实日期，避免误当成当日实时价。
        if price_is_prior_session:
            reasons.append(f"昨收价 {price_trade_date} · {current_price:.2f} {current_currency}（盘前/未开盘，待盘中刷新）")
            data_flags.append("prior_session_price")
        else:
            reasons.append(f"最新行情 {price_trade_date} · {current_price:.2f} {current_currency}")
        if hk_yfinance_unconfirmed:
            reasons.append("港股行情仅来自 yfinance，未通过本地二源确认；当日盈亏暂不展示")
            data_flags.append("hk_yfinance_unconfirmed")
        if large_move_unconfirmed:
            reasons.append("行情较前收大幅跳动，已保留为盘中价；请结合行情源复核")
            data_flags.append("large_move_unconfirmed")
    else:
        reasons.append("暂无最新行情,市值暂用锁定成本估算")
        data_flags.append("missing_price")
    if pnl_pct is not None:
        reasons.append(f"当前盈亏 {pnl_pct:+.2f}%")
    if weight_gap_pt is not None:
        reasons.append(f"当前仓位 vs AI目标差 {weight_gap_pt:+.1f}pt")
    if treatment_class == "risk_only":
        if current_weight is not None:
            reasons.append(f"当前仓位 {current_weight * 100:.1f}%（不与 AI 目标比较）")
        threshold = float(rules["tracking_loss_review_pct"])
        if pnl_pct is not None:
            margin = pnl_pct - threshold  # threshold 为负数；margin>=0 即仍在风控线上方
            if margin >= 0:
                reasons.append(f"距 {threshold:+.0f}% 风控线还有 {margin:.1f}pt 缓冲")
            else:
                reasons.append(f"已跌破 {threshold:+.0f}% 风控线 {abs(margin):.1f}pt")
    for r in (verdict or {}).get("reasons") or []:
        # compute_holdings_verdict 的 weight_off 仍是早报用的简化口径；体检页
        # 已在上面用 RMB 锁定成本/汇率重新计算，避免同一行出现两个差距。
        if r.get("kind") == "weight_off":
            continue
        txt = r.get("text")
        if txt:
            reasons.append(txt)

    for flag in (pick or {}).get("risk_flags") or []:
        if isinstance(flag, str):
            risk_flags.append(flag)
        elif isinstance(flag, dict):
            # V2 结构是 {code, severity, message}（message 已是中文句子）；
            # 任何情况下都不允许 str(dict) 的 repr 文本漏进展示层。
            txt = flag.get("message") or flag.get("text") or flag.get("flag") or flag.get("code")
            if txt:
                risk_flags.append(str(txt))
    if current_weight is not None and current_weight >= 0.25:
        risk_flags.append("单一持仓超过总资产 25%")
    if pnl_pct is not None and pnl_pct <= float(rules["loss_review_pct"]):
        risk_flags.append(f"浮亏超过 {abs(float(rules['loss_review_pct'])):.0f}%,优先风险复查")

    is_fallback_pick = bool(pick and (pick.get("universe_scope") == "manual_watchlist"))
    if is_fallback_pick and action in DISOBEDIENT_ACTIONS:
        reasons.append("⚠️ 该评分由因子缓存 fallback 算出（不在系统主推荐池），减仓动作降级为提示，决策请你自己判断")
    size_advisory = _suggest_size_advisory(
        rules=rules,
        action=action,
        symbol=symbol,
        shares=shares,
        current_price=current_price,
        fx=fx,
        current_value_rmb=current_value_rmb,
        current_weight=current_weight,
        target_weight=target_weight,
        total_capital=total_capital,
        treatment_class=treatment_class,
        is_fallback_pick=is_fallback_pick,
    )
    if size_advisory and size_advisory.get("over_hard_cap") and not any("25%" in f for f in risk_flags):
        risk_flags.append("单一持仓超过总资产 25%（建议规模已按红线折算）")

    if industry_heat and industry_heat.get("industry_heat_badge") == "hot":
        etf = industry_heat.get("etf_ticker", "")
        ret = industry_heat.get("sector_return_60d_pct")
        if ret is not None:
            reasons.append(f"所属板块 {etf} 60d {float(ret):+.1f}%（偏强）")
    elif industry_heat and industry_heat.get("industry_heat_badge") == "cold":
        etf = industry_heat.get("etf_ticker", "")
        ret = industry_heat.get("sector_return_60d_pct")
        if ret is not None:
            reasons.append(f"所属板块 {etf} 60d {float(ret):+.1f}%（偏弱）")

    return {
        # 2026-05-29 lot accounting: holding_id 关联 real_holdings.id, 让同 symbol
        # 多次买入的 lot 各自独立显示 / 各自算 day_change
        "holding_id": int(holding["id"]) if holding.get("id") is not None else None,
        "account": holding.get("account") or "default",
        "market": holding.get("market") or stock_db._infer_market_from_ticker(symbol),
        "symbol": symbol,
        "code": symbol,
        "asset_class": asset_class,
        "treatment_class": treatment_class,
        "score": score,
        "coverage_score": coverage_score,
        "rating": "tracking" if treatment_class == "risk_only" else ((pick or {}).get("rating") or "unrated"),
        "action_label": action,
        "action_priority": ACTION_PRIORITY.get(action, 99),
        "current_price": current_price,
        "current_currency": current_currency,
        "current_value_rmb": round(current_value_rmb, 4),
        "cost_rmb_locked": round(cost_rmb, 4),
        "pnl_rmb": round(pnl_rmb, 4),
        "pnl_pct": round(pnl_pct, 4) if pnl_pct is not None else None,
        "price_trade_date": price_trade_date,
        "prev_close": prev_close,
        "prev_trade_date": prev_trade_date,
        "trade_date": price_trade_date,
        "price_is_prior_session": price_is_prior_session,
        "day_change_basis": day_change_basis,
        "day_change_rmb": round(day_change_rmb, 4) if day_change_rmb is not None else None,
        "day_change_pct": round(day_change_pct, 4) if day_change_pct is not None else None,
        "current_weight": round(current_weight, 6) if current_weight is not None else None,
        "target_weight": target_weight,
        "weight_gap_pt": round(weight_gap_pt, 4) if weight_gap_pt is not None else None,
        "reasons": reasons[:8],
        "risk_flags": risk_flags[:8],
        "data_flags": data_flags,
        "size_advisory": size_advisory,
        "industry_heat": industry_heat,
    }


def build_real_holding_review(*, persist: bool = True) -> dict[str, Any]:
    conn = stock_db.get_db()
    try:
        holdings = stock_db.fetch_all_real_holdings(conn=conn)
        symbols = [str(h.get("symbol") or h.get("code")) for h in holdings if h.get("symbol") or h.get("code")]
        prices = _latest_prices_by_symbol(conn, symbols)
        picks = _latest_picks_by_symbol(conn, symbols)
        rules = _load_review_rules(conn)
        for sym, pick in _manual_watchlist_score_fallbacks(symbols).items():
            picks.setdefault(sym, pick)
        max_age = float(rules.get("score_snapshot_max_age_days", 3.0) or 3.0)
        for sym, pick in _hk_watchlist_score_fallbacks(symbols, max_age_days=max_age).items():
            picks.setdefault(sym, pick)
        universe = stock_db.fetch_universe_for_ai_recommendations(conn=conn)
        target_weights = _target_weights_from_plan()
        total_capital = float(stock_db.get_config("total_capital", conn=conn) or 500000)

        history_doc = _load_json(REPO / "data" / "latest" / "history_data.json")
        history = history_doc.get("tickers") if isinstance(history_doc, dict) else {}
        events_data = _load_json(REPO / "data" / "event_calendar.json")
        verdict = compute_holdings_verdict(
            holdings,
            history=history or {},
            picks=list(picks.values()),
            universe=universe,
            events_data=events_data,
            target_weights=target_weights,
            total_capital=total_capital,
        )
        verdict_by_code = {v.get("code"): v for v in verdict.get("holdings", [])}

        sector_rotation = _load_sector_rotation()
        active_discipline_plans = {
            int(p["holding_id"]): p
            for p in stock_db.fetch_real_holding_discipline_plans(status="active", conn=conn)
            if p.get("holding_id") is not None
        }
        items = []
        for h in holdings:
            sym = str(h.get("symbol") or h.get("code"))
            mkt = str(h.get("market") or stock_db._infer_market_from_ticker(sym))
            v = verdict_by_code.get(sym) or {}
            heat = resolve_industry_heat(
                conn,
                sym,
                mkt,
                rotation=sector_rotation,
                asset_class=v.get("asset_class") or h.get("asset_class"),
            )
            item = _build_item(
                h,
                rules=rules,
                price=prices.get(sym),
                pick=picks.get(sym),
                verdict=verdict_by_code.get(sym),
                total_capital=total_capital,
                target_weights=target_weights,
                industry_heat=heat,
            )
            plan = active_discipline_plans.get(int(h["id"])) if h.get("id") is not None else None
            if plan:
                item["discipline"] = stock_db.evaluate_real_holding_discipline_plan(
                    plan,
                    current_price=item.get("current_price"),
                    price_trade_date=item.get("price_trade_date"),
                    price_is_stale=bool(item.get("price_is_prior_session")),
                )
            items.append(item)

        today = date.today().isoformat()
        now = datetime.now()
        review_run_id = "realhold_" + now.strftime("%Y%m%d_%H%M%S")
        data_quality = "OK"
        if any(i.get("data_flags") for i in items):
            data_quality = "WARN"
        if not holdings:
            data_quality = "EMPTY"
        run = {
            "review_run_id": review_run_id,
            "as_of_date": today,
            "generated_at": now.isoformat(timespec="seconds"),
            "status": "generated",
            "holding_count": len(items),
            "data_quality": data_quality,
            "notes": f"real_holdings daily review; advisory only; rules={rules.get('version')}",
        }
        payload = {
            "run": run,
            "items": items,
            "rules": rules,
            "verdict_summary": verdict.get("summary", {}),
        }
        if persist:
            stock_db.save_real_holding_review(run, items, conn=conn)
            OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            OUT_PATH.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build real holdings daily review")
    parser.add_argument("--no-persist", action="store_true", help="compute only; do not write DuckDB/json")
    args = parser.parse_args()
    payload = build_real_holding_review(persist=not args.no_persist)
    run = payload.get("run") or {}
    print(json.dumps(_json_safe({
        "status": run.get("status"),
        "review_run_id": run.get("review_run_id"),
        "holding_count": run.get("holding_count"),
        "data_quality": run.get("data_quality"),
    }), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
