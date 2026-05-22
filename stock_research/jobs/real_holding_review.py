"""Daily review for the user's real holdings.

This job evaluates only `real_holdings`. It does not create a stock pool, does
not write recommendation_picks, and does not mutate model_sim_holdings.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import fx_rates  # type: ignore
import stock_db  # type: ignore
from stock_research.jobs.morning_brief import compute_holdings_verdict


OUT_PATH = REPO / "data" / "latest" / "real_holding_review.json"

ACTION_PRIORITY = {
    "风险复查": 1,
    "减仓观察": 2,
    "补数据": 3,
    "事件观察": 4,
    "关注加仓": 5,
    "持有观察": 6,
    "仅风控跟踪": 7,
}


def _default_rules() -> dict[str, float | str]:
    return dict(stock_db.USER_CONFIG_DEFAULTS["real_holding_review_rules"])


def _load_review_rules(conn=None) -> dict[str, Any]:
    rules = _default_rules()
    try:
        configured = stock_db.get_config("real_holding_review_rules", conn=conn)
    except Exception:
        configured = None
    if isinstance(configured, dict):
        rules.update(configured)
    return rules


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
    rows = conn.execute(
        f"""
        WITH latest AS (
          SELECT symbol, MAX(trade_date) AS trade_date
          FROM price_daily
          WHERE symbol IN ({placeholders})
          GROUP BY symbol
        )
        SELECT p.market, p.symbol, p.trade_date, p.close, p.currency, p.source, p.fetched_at
        FROM price_daily p
        JOIN latest l ON p.symbol = l.symbol AND p.trade_date = l.trade_date
        ORDER BY p.symbol
        """,
        symbols,
    ).fetchall()
    out: dict[str, dict] = {}
    for market, symbol, trade_date, close, currency, source, fetched_at in rows:
        out[str(symbol)] = {
            "market": market,
            "symbol": symbol,
            "trade_date": trade_date,
            "close": close,
            "currency": currency,
            "source": source,
            "fetched_at": fetched_at,
        }
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
        if pnl_pct is not None and pnl_pct <= float(rules["tracking_loss_review_pct"]):
            return "风险复查"
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


def _build_item(
    holding: dict,
    *,
    rules: dict[str, Any] | None = None,
    price: dict | None,
    pick: dict | None,
    verdict: dict | None,
    total_capital: float,
    target_weights: dict[str, float],
) -> dict:
    rules = rules or _default_rules()
    symbol = str(holding.get("symbol") or holding.get("code"))
    coverage_class = (verdict or {}).get("coverage_class")
    treatment_class = (verdict or {}).get("treatment_class")
    if not treatment_class:
        treatment_class = {
            "ai_portfolio": "portfolio_model",
            "tracking_only": "risk_only",
            "needs_fix": "data_blocked",
        }.get(str(coverage_class or ""), "stock_score")
    else:
        treatment_class = {
            "ai_portfolio": "portfolio_model",
            "picks_only": "stock_score",
            "tracking_only": "risk_only",
            "needs_fix": "data_blocked",
        }.get(str(treatment_class), str(treatment_class))
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
    current_weight = (current_value_rmb / total_capital) if total_capital > 0 else None
    target_weight = target_weights.get(symbol)
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
    if pick and raw_score is not None:
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
    if pick and raw_score is not None:
        reasons.append(f"最新股票评分 {raw_score:.1f} · 评级 {pick.get('rating') or '-'}")
    elif treatment_class not in {"risk_only", "data_blocked"}:
        reasons.append("暂无当日股票评分,结论降级为观察")
        data_flags.append("no_model_score")
    if price and current_price is not None:
        reasons.append(f"最新行情 {price.get('trade_date')} · {current_price:.2f} {current_currency}")
    else:
        reasons.append("暂无最新行情,市值暂用锁定成本估算")
        data_flags.append("missing_price")
    if pnl_pct is not None:
        reasons.append(f"当前盈亏 {pnl_pct:+.2f}%")
    if weight_gap_pt is not None:
        reasons.append(f"当前仓位 vs AI目标差 {weight_gap_pt:+.1f}pt")
    for r in (verdict or {}).get("reasons") or []:
        txt = r.get("text")
        if txt:
            reasons.append(txt)

    for flag in (pick or {}).get("risk_flags") or []:
        if isinstance(flag, str):
            risk_flags.append(flag)
        elif isinstance(flag, dict):
            risk_flags.append(str(flag.get("text") or flag.get("flag") or flag))
    if current_weight is not None and current_weight >= 0.25:
        risk_flags.append("单一持仓超过总资产 25%")
    if pnl_pct is not None and pnl_pct <= float(rules["loss_review_pct"]):
        risk_flags.append(f"浮亏超过 {abs(float(rules['loss_review_pct'])):.0f}%,优先风险复查")

    return {
        "account": holding.get("account") or "default",
        "market": holding.get("market") or stock_db._infer_market_from_ticker(symbol),
        "symbol": symbol,
        "code": symbol,
        "asset_class": asset_class,
        "treatment_class": treatment_class,
        "score": score,
        "coverage_score": coverage_score,
        "rating": (pick or {}).get("rating") or ("tracking" if treatment_class == "tracking_only" else "unrated"),
        "action_label": action,
        "action_priority": ACTION_PRIORITY.get(action, 99),
        "current_price": current_price,
        "current_currency": current_currency,
        "current_value_rmb": round(current_value_rmb, 4),
        "cost_rmb_locked": round(cost_rmb, 4),
        "pnl_rmb": round(pnl_rmb, 4),
        "pnl_pct": round(pnl_pct, 4) if pnl_pct is not None else None,
        "current_weight": round(current_weight, 6) if current_weight is not None else None,
        "target_weight": target_weight,
        "weight_gap_pt": round(weight_gap_pt, 4) if weight_gap_pt is not None else None,
        "reasons": reasons[:8],
        "risk_flags": risk_flags[:8],
        "data_flags": data_flags,
    }


def build_real_holding_review(*, persist: bool = True) -> dict[str, Any]:
    conn = stock_db.get_db()
    try:
        holdings = stock_db.fetch_all_real_holdings(conn=conn)
        symbols = [str(h.get("symbol") or h.get("code")) for h in holdings if h.get("symbol") or h.get("code")]
        prices = _latest_prices_by_symbol(conn, symbols)
        picks = _latest_picks_by_symbol(conn, symbols)
        try:
            universe = stock_db.fetch_universe_for_ai_recommendations(conn=conn)
        except TypeError:
            universe = stock_db.fetch_universe_for_ai_recommendations()
        target_weights = _target_weights_from_plan()
        total_capital = float(stock_db.get_config("total_capital", conn=conn) or 500000)
        rules = _load_review_rules(conn)

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

        items = [
            _build_item(
                h,
                rules=rules,
                price=prices.get(str(h.get("symbol") or h.get("code"))),
                pick=picks.get(str(h.get("symbol") or h.get("code"))),
                verdict=verdict_by_code.get(str(h.get("symbol") or h.get("code"))),
                total_capital=total_capital,
                target_weights=target_weights,
            )
            for h in holdings
        ]

        today = date.today().isoformat()
        review_run_id = "realhold_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        data_quality = "OK"
        if any(i.get("data_flags") for i in items):
            data_quality = "WARN"
        if not holdings:
            data_quality = "EMPTY"
        run = {
            "review_run_id": review_run_id,
            "as_of_date": today,
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
