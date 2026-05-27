"""完整组合优化流水线（v6）— 中性化 + Markowitz + ADV 约束 + 成本扣减。

与 build_plan_a_v5.py 互补：v5 是"裸 Markowitz"（无中性化、无成本约束）。
v6 在 v5 基础上加了三个关键质量门：

  1. 行业+市值中性化（core.neutralization）
     → 因子分数剔除行业 beta 和小盘溢价后再排名
     → Top N 选股不会被某个行业（如半导体动量）一边倒污染

  2. ADV 限流（core.portfolio_constraints.cap_by_adv）
     → 单日交易 ≤ 5% × ADV，避免冲击成本
     → 小盘 / A 股低流动票自动减仓

  3. 交易成本扣减（core.portfolio_constraints.apply_transaction_cost）
     → 净 alpha = 总 alpha - 佣金 - 冲击
     → 高换手策略会被成本吃掉的部分明确暴露

2026-05-10 P1 升级（默认开启）：
  4. **风险感知优化**：核心 Markowitz 步从蒙特卡洛改用 portfolio_optimizer_pro
     的 risk_aware_optimize（Ledoit-Wolf 协方差 + 相关性 < 0.7 剪枝 + 多级降风险）
     - 加 --legacy-mc 开关回退到 20000 次蒙特卡洛（兼容老 review）

CLI:
  python3 -m stock_research.jobs.optimize_portfolio
  python3 -m stock_research.jobs.optimize_portfolio --capital 1000000
  python3 -m stock_research.jobs.optimize_portfolio --no-neutralize  # 关闭中性化对比
  python3 -m stock_research.jobs.optimize_portfolio --legacy-mc      # 用旧蒙特卡洛
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

# 让本 job 能 import 根目录的 factor_model + early_signals + build_plan_a_v5 helpers
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))  # 2026-05-11 lib 迁移
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "pipeline"))  # build_plan_a_v5 等 sibling

from .. import config
from ..core import neutralization as nz
from ..core import portfolio_constraints as pc
from ..core import portfolio_optimizer_pro as opt_pro
from ..adapters import store

import pandas as pd

logger = logging.getLogger("stock_research.jobs.optimize_portfolio")


def _default_capital() -> float:
    """与旧 build_plan_a_v5.py 对齐：优先用 DuckDB 配置的组合规模。"""
    try:
        import stock_db
        return float(stock_db.get_config("total_capital") or 500_000)
    except Exception:
        return 500_000.0


DEFAULT_CAPITAL = _default_capital()
_HISTORY_CACHE: dict | None = None


def _safe_float(value, default=None):
    try:
        if value is None:
            return default
        f = float(value)
        return f if np.isfinite(f) else default
    except Exception:
        return default


def _write_latest_plan(result: dict) -> None:
    """写生产读路径。

    历史上下游仍读 data/latest/plan_a_v5.json / plan_v5 字段；这里保留旧文件名和
    字段形状，但 payload 明确标注 method=risk_aware_optimize，避免生产继续吃旧
    裸 Markowitz 结果。
    """
    latest_dir = _REPO_ROOT / "data" / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    for name in ("plan_a_v5.json", "plan_v6.json"):
        out = latest_dir / name
        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        print(f"✅ 生产 plan 已写入: {out}")


def _infer_market(symbol: str) -> str:
    s = str(symbol or "").upper()
    if s.endswith((".SS", ".SZ", ".SH")):
        return "CN"
    if s.endswith(".HK"):
        return "HK"
    return "US"


def _benchmark_for_market(market: str) -> str:
    return {"US": "SPY", "HK": "2800.HK", "CN": "000300.SH"}.get(str(market).upper(), "SPY")


def _latest_v2_run_id(conn) -> str | None:
    row = conn.execute(
        """
        SELECT run_id
        FROM recommendation_runs
        WHERE universe_scope = 'system_tech_universe' AND status = 'generated'
        ORDER BY generated_at DESC
        LIMIT 1
        """
    ).fetchone()
    return str(row[0]) if row else None


def _sync_risk_aware_plan_to_duckdb(result: dict, run_id: str | None) -> dict:
    """Make DuckDB portfolio_plans reflect the actionable risk-aware plan.

    build_v2_recommendations can create a short-lived equal-weight placeholder so
    a fresh v2 rebuild has a portfolio artifact. Production, however, must use
    the optimizer's constrained weights. This function replaces all current-run
    system_tech_universe plan rows with v6_risk_aware weights.
    """
    try:
        import duckdb
    except Exception as e:  # pragma: no cover - production dependency
        raise RuntimeError(f"DuckDB unavailable for portfolio_plans sync: {e}") from e

    plan_rows = result.get("plan") or result.get("plan_v6") or []
    if not isinstance(plan_rows, list):
        plan_rows = []
    actionable = []
    for row in plan_rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("ticker") or row.get("symbol") or row.get("code") or "").upper()
        weight = _safe_float(row.get("capped_weight"), _safe_float(row.get("target_weight"), 0.0))
        if not symbol or weight is None or weight <= 1e-9:
            continue
        actionable.append((symbol, float(weight), row))
    if not actionable:
        raise RuntimeError("risk-aware plan has no positive target weights to sync")

    db_path = Path(config.DUCKDB_PATH)
    conn = duckdb.connect(str(db_path))
    try:
        tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
        required = {"recommendation_runs", "recommendation_picks", "portfolio_plans"}
        missing = sorted(required - tables)
        if missing:
            raise RuntimeError(f"DuckDB missing required tables for plan sync: {missing}")
        run_id = run_id or _latest_v2_run_id(conn)
        if not run_id:
            raise RuntimeError("No latest V2 recommendation run found for portfolio_plans sync")

        meta_rows = conn.execute(
            """
            SELECT symbol, market
            FROM recommendation_picks
            WHERE run_id = ?
            """,
            [run_id],
        ).fetchall()
        market_by_symbol = {str(symbol).upper(): str(market).upper() for symbol, market in meta_rows}

        conn.execute(
            """
            DELETE FROM portfolio_plans
            WHERE run_id = ? AND strategy_scope = 'system_tech_universe'
            """,
            [run_id],
        )

        now = datetime.now()
        constraints = result.get("constraints") if isinstance(result.get("constraints"), dict) else {}
        risk_limit = {
            "source": "data/latest/plan_a_v5.json",
            "plan_source": "risk_aware_optimize",
            "engine": (result.get("risk_aware") or {}).get("engine") if isinstance(result.get("risk_aware"), dict) else None,
            "method": result.get("method"),
            "cash_pct_effective": constraints.get("cash_pct_effective"),
            "gross_exposure_effective": constraints.get("gross_exposure_effective"),
            "max_weight": constraints.get("max_weight"),
            "min_weight": constraints.get("min_weight"),
            "max_corr": constraints.get("max_corr"),
            "max_adv_pct": constraints.get("max_adv_pct"),
        }
        for symbol, weight, _row in actionable:
            market = market_by_symbol.get(symbol) or _infer_market(symbol)
            conn.execute(
                """
                INSERT INTO portfolio_plans (
                    run_id, plan_version, strategy_scope, market, symbol,
                    target_weight, action, risk_limit_json, transaction_cost_bps,
                    benchmark_symbol, created_at
                ) VALUES (?, 'v6_risk_aware', 'system_tech_universe', ?, ?, ?, 'target_weight', ?, ?, ?, ?)
                """,
                [
                    run_id,
                    market,
                    symbol,
                    weight,
                    json.dumps(risk_limit, ensure_ascii=False),
                    _safe_float(constraints.get("cost_bps"), 5.0),
                    _benchmark_for_market(market),
                    now,
                ],
            )
    finally:
        conn.close()

    gross = sum(weight for _, weight, _ in actionable)
    summary = {
        "db_path": str(db_path),
        "run_id": run_id,
        "plan_version": "v6_risk_aware",
        "rows": len(actionable),
        "gross_exposure": round(gross, 6),
        "cash_pct": round(max(0.0, 1.0 - gross), 6),
    }
    print(
        "✅ DuckDB portfolio_plans 已同步为 risk-aware: "
        f"run_id={run_id} rows={summary['rows']} gross={summary['gross_exposure']:.1%}"
    )
    return summary


def _limit_positions_by_optimizer_weight(
    weights: dict[str, float],
    ranked_tickers: list[str],
    max_positions: int | None,
) -> tuple[dict[str, float], list[str]]:
    if not max_positions or max_positions <= 0:
        return dict(weights), []
    positive = {t: float(w) for t, w in weights.items() if float(w) > 1e-9}
    if len(positive) <= max_positions:
        return positive, []
    rank = {t: i for i, t in enumerate(ranked_tickers)}
    keep = {
        t for t, _w in sorted(
            positive.items(),
            key=lambda item: (-item[1], rank.get(item[0], 10_000)),
        )[:max_positions]
    }
    limited = {t: w for t, w in positive.items() if t in keep}
    dropped = [t for t in ranked_tickers if t in positive and t not in keep]
    return limited, dropped


def _redistribute_to_target_gross(
    weights: dict[str, float],
    ranked_tickers: list[str],
    industries: dict[str, str],
    adv_dollars: dict[str, float],
    *,
    portfolio_value: float,
    target_gross: float,
    max_single_pct: float,
    max_industry_pct: float,
    max_adv_pct: float,
    max_rounds: int = 8,
) -> tuple[dict[str, float], dict[str, float | int]]:
    """Reallocate cap overflow to eligible names before letting it become cash."""
    out = {t: max(0.0, float(w)) for t, w in weights.items()}
    target_gross = max(0.0, min(1.0, target_gross))
    if not out or target_gross <= 0:
        return out, {"before_gross": sum(out.values()), "after_gross": sum(out.values()), "redistributed": 0.0}

    before = sum(out.values())
    rank = {t: i for i, t in enumerate(ranked_tickers)}

    def industry_totals() -> dict[str, float]:
        totals: dict[str, float] = {}
        for t, w in out.items():
            ind = industries.get(t, "Unknown") or "Unknown"
            totals[ind] = totals.get(ind, 0.0) + w
        return totals

    for _ in range(max_rounds):
        gross = sum(out.values())
        remaining = target_gross - gross
        if remaining <= 1e-6:
            break
        ind_totals = industry_totals()
        capacities: dict[str, float] = {}
        for t in out:
            single_room = max_single_pct - out[t]
            ind = industries.get(t, "Unknown") or "Unknown"
            industry_room = max_industry_pct - ind_totals.get(ind, 0.0)
            adv = float(adv_dollars.get(t) or 0.0)
            adv_room = (adv * max_adv_pct / portfolio_value - out[t]) if adv > 0 and portfolio_value > 0 else 0.0
            cap = min(single_room, industry_room, adv_room)
            if cap > 1e-6:
                # Tilt toward higher-ranked names while still using capacity.
                score_tilt = max(1.0, len(rank) - rank.get(t, len(rank)) + 1.0)
                capacities[t] = cap * score_tilt
        if not capacities:
            break
        raw_total = sum(capacities.values())
        added_total = 0.0
        for t, weighted_cap in sorted(capacities.items(), key=lambda item: rank.get(item[0], 10_000)):
            # Convert tilted capacity back to real cap for the hard limit.
            score_tilt = max(1.0, len(rank) - rank.get(t, len(rank)) + 1.0)
            real_cap = weighted_cap / score_tilt
            add = min(real_cap, remaining * weighted_cap / raw_total)
            if add > 0:
                out[t] += add
                added_total += add
        if added_total <= 1e-8:
            break

    after = sum(out.values())
    return out, {
        "before_gross": round(before, 6),
        "after_gross": round(after, 6),
        "target_gross": round(target_gross, 6),
        "redistributed": round(max(0.0, after - before), 6),
        "residual_cash": round(max(0.0, 1.0 - after), 6),
        "rounds": max_rounds,
    }


def _portfolio_stats_for_weights(
    weights: dict[str, float],
    mean_ret_map: dict[str, float],
    cov_df: pd.DataFrame,
    *,
    risk_free_annual: float = 0.045,
) -> tuple[float, float, float]:
    tickers = [t for t, w in weights.items() if w > 1e-9 and t in mean_ret_map and t in cov_df.index]
    if not tickers:
        return 0.0, 0.0, 0.0
    w = np.array([weights[t] for t in tickers], dtype=float)
    mu = np.array([mean_ret_map[t] for t in tickers], dtype=float)
    sub_cov = cov_df.loc[tickers, tickers].to_numpy(dtype=float)
    gross = float(w.sum())
    cash = max(0.0, 1.0 - gross)
    annual_ret = float(mu @ w) * 252 + risk_free_annual * cash
    annual_vol = float(np.sqrt(max(0.0, w @ sub_cov @ w)) * np.sqrt(252))
    annual_sharpe = (annual_ret - risk_free_annual) / annual_vol if annual_vol > 0 else 0.0
    return annual_ret, annual_vol, annual_sharpe


# ─────────── 数据获取 ───────────

def _load_factor_scores(market_scope: str = "US") -> dict | None:
    """AI 组合方案候选池 = 系统 V2 AI 推荐池 (recommendation_picks)。

    Why: 历史版本主路径走 daily_picks_v5 写的 factor_scores_today.json cache,
    但 daily_picks_v5 只覆盖手动 watchlist —— 让组合方案变成"在自选股里挑",违背
    产品三层架构(自选股 ≠ AI 推荐)。修正后：候选池始终来自 V2 系统 AI 推荐池;
    cache 仅做 enrichment(若 V2 推荐池在 cache 中 ≥90% 命中,走完整 combine_factors
    + 中性化路径;否则走 V2 lite —— 用 V2 total_score 排序,跳过 combine_factors,
    但风险感知优化器、Markowitz、相关性剪枝、ADV/行业 cap 仍生效)。
    """
    market_scope = str(market_scope or "US").upper()

    def _in_scope(ticker: str, explicit_market: str | None = None) -> bool:
        if market_scope in {"ALL", "*"}:
            return True
        market = (explicit_market or _infer_market(ticker)).upper()
        return market == market_scope

    # ────── 1. 候选池：V2 recommendation_picks(主路径,不再从 cache 取候选) ──────
    try:
        import sys as _sys
        _sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
        from stock_db import fetch_latest_recommendation_picks  # type: ignore
    except Exception as e:
        logger.error("加载 stock_db 失败: %s", e)
        return None
    picks = [
        p for p in fetch_latest_recommendation_picks()
        if _in_scope(str(p.get("symbol") or ""), str(p.get("market") or ""))
    ]
    if not picks:
        logger.error("V2 recommendation_picks 无 %s 最新 run", market_scope)
        return None
    pick_symbols = {p["symbol"] for p in picks}

    # ────── 2. cache enrichment：检查 V2 推荐池在 cache 中的命中率 ──────
    cache_path = _REPO_ROOT / "data" / "latest" / "factor_scores_today.json"
    cache_factors: dict = {}
    cache_signals: dict = {}
    if cache_path.exists():
        with open(cache_path, encoding="utf-8") as f:
            cache_data = json.load(f)
        today_str = datetime.now().strftime("%Y-%m-%d")
        if (cache_data.get("date") or "")[:10] == today_str:
            for fac in (cache_data.get("factors") or []):
                tk = fac.get("ticker") or fac.get("symbol")
                if tk and tk in pick_symbols:
                    cache_factors[tk] = fac
            for sig in (cache_data.get("signals") or []):
                tk = sig.get("ticker") or sig.get("symbol")
                if tk and tk in pick_symbols:
                    cache_signals[tk] = sig

    hit_ratio = len(cache_factors) / max(1, len(picks))

    # ────── 3a. cache ≥90% 命中 → 走完整因子路径(Piotroski + 中性化) ──────
    if hit_ratio >= 0.9:
        factors = [cache_factors[p["symbol"]] for p in picks if p["symbol"] in cache_factors]
        signals = [cache_signals.get(p["symbol"], {"ticker": p["symbol"], "analyst": None})
                   for p in picks if p["symbol"] in cache_factors]
        logger.info(
            "AI 组合候选池 %s：V2 推荐池 %d 只,cache 命中 %d/%d (%.0f%%) → 完整因子模式",
            market_scope, len(factors), len(cache_factors), len(picks), hit_ratio * 100,
        )
        return {
            "factors": factors,
            "signals": signals,
            "v2_universe_run_id": picks[0].get("run_id"),
            "v2_universe_run_date": str(picks[0].get("run_date")),
            "v2_universe_count": len(picks),
            "market_scope": market_scope,
        }

    # ────── 3b. cache 命中不足 → V2 lite：用 total_score 当 composite ──────
    factors = []
    signals = []
    for p in picks:
        ticker = p["symbol"]
        factors.append({
            "ticker": ticker,
            "composite": p.get("total_score"),
            "piotroski": {"f_score": None},
            "momentum": {"momentum_12_1": None, "reversal_1m": None},
            "pead": {"acceleration": None},
            "quality": None,
        })
        signals.append({"ticker": ticker, "analyst": None})
    logger.info(
        "AI 组合候选池 %s：V2 推荐池 %d 只,cache 命中 %d/%d (%.0f%%) → V2 lite 模式",
        market_scope, len(factors), len(cache_factors), len(picks), hit_ratio * 100,
    )
    return {
        "factors": factors,
        "signals": signals,
        "fallback_v2": True,  # 保留旧名,触发 run() 中的 lite 分支
        "fallback_run_id": picks[0].get("run_id"),
        "fallback_run_date": str(picks[0].get("run_date")),
        "v2_universe_run_id": picks[0].get("run_id"),
        "v2_universe_run_date": str(picks[0].get("run_date")),
        "v2_universe_count": len(picks),
        "market_scope": market_scope,
    }


def _load_history_cache() -> dict:
    global _HISTORY_CACHE
    if _HISTORY_CACHE is not None:
        return _HISTORY_CACHE
    path = _REPO_ROOT / "data" / "latest" / "history_data.json"
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        _HISTORY_CACHE = payload.get("tickers") or {}
    except Exception as e:
        logger.warning("history_data.json 不可用: %s", e)
        _HISTORY_CACHE = {}
    return _HISTORY_CACHE


def _cached_close_series(ticker: str, lookback_days: int) -> pd.Series | None:
    row = _load_history_cache().get(ticker)
    if not isinstance(row, dict):
        return None
    dates = row.get("ts") or row.get("dates") or []
    closes = row.get("close") or row.get("closes") or []
    if not dates or not closes or len(dates) != len(closes):
        return None
    try:
        series = pd.Series(pd.to_numeric(closes, errors="coerce"), index=pd.to_datetime(dates), name=ticker).dropna()
    except Exception:
        return None
    cutoff = pd.Timestamp(datetime.now() - timedelta(days=lookback_days + 60))
    series = series[series.index >= cutoff]
    return series if len(series) >= 60 else None


def _adv_from_cache_or_price_daily(ticker: str, close_series: pd.Series | None) -> float | None:
    row = _load_history_cache().get(ticker)
    if isinstance(row, dict) and row.get("volume") and close_series is not None and len(close_series) >= 30:
        try:
            volume = pd.Series(pd.to_numeric(row["volume"], errors="coerce")).dropna().tail(30)
            if len(volume) > 0:
                return float(volume.mean()) * float(close_series.iloc[-1])
        except Exception:
            pass
    try:
        import duckdb
        import stock_db

        con = duckdb.connect(str(stock_db.DB_PATH), read_only=True)
        row_db = con.execute(
            """
            SELECT market_cap
            FROM price_daily
            WHERE symbol = ? AND market_cap IS NOT NULL
            ORDER BY trade_date DESC, fetched_at DESC
            LIMIT 1
            """,
            [ticker],
        ).fetchone()
        con.close()
        mcap = _safe_float(row_db[0] if row_db else None)
        if mcap and mcap > 0:
            # history_data.json currently has closes but not volume. Use a
            # conservative turnover proxy so ADV gates stay active without a
            # live yfinance dependency.
            return mcap * 0.003
    except Exception as e:
        logger.debug("price_daily ADV proxy failed for %s: %s", ticker, e)
    return None


def _fetch_returns_and_adv(ticker: str, lookback_days: int = 252):
    """历史收益 + 当前 ADV；优先用本地 V2 history/price_daily，缺失再打 yfinance。"""
    cached = _cached_close_series(ticker, lookback_days)
    if cached is not None:
        rets = cached.pct_change().dropna().values
        adv = _adv_from_cache_or_price_daily(ticker, cached)
        if len(rets) >= 60 and adv and adv > 0:
            return rets[-lookback_days:], adv
    try:
        import yfinance as yf
    except ImportError:
        return None, None
    try:
        end = datetime.now()
        start = end - timedelta(days=lookback_days + 60)
        t = yf.Ticker(ticker)
        hist = t.history(start=start, end=end)
        if len(hist) < 60:
            return None, None
        rets = hist["Close"].pct_change().dropna().values
        if len(rets) < 60:
            return None, None
        # ADV = 过去 30 天平均成交量 × 当前价
        last_30 = hist.tail(30)
        avg_vol = float(last_30["Volume"].mean())
        last_close = float(hist["Close"].iloc[-1])
        adv = avg_vol * last_close
        return rets[-lookback_days:], adv
    except Exception as e:
        logger.warning("yfinance %s 失败: %s", ticker, e)
        return None, None


def _industry_for(ticker: str, watchlist_lookup: dict) -> str:
    """行业归类（用作中性化 + 行业敞口约束分组）。

    策略：优先用 daily_picks.THEME_MAPPING（用户维护的 76 只主题映射，粒度合适），
    回退到 watchlist「行业归类」字段的第二段（剔除"美股·"前缀）。
    """
    # 1. 优先 THEME_MAPPING（粗粒度主题，每个主题通常 5-15 只）
    try:
        from theme_mapping import THEME_MAPPING  # V2: extracted from daily_picks.py
        theme = THEME_MAPPING.get(ticker.upper())
        if theme:
            return theme
    except ImportError:
        pass

    # 2. 回退：watchlist 行业字段第二段
    wf = watchlist_lookup.get(ticker.upper(), {})
    industry = wf.get("行业归类", "") or ""
    if isinstance(industry, list):
        industry = (industry[0].get("text", "") if industry else "") or ""
    if isinstance(industry, dict):
        industry = industry.get("text", "") or industry.get("name", "")
    # "美股·半导体/AI 算力霸主" → "半导体/AI 算力霸主"；取第一个 / 之前
    parts = industry.split("·") if industry else []
    second = parts[1] if len(parts) > 1 else (parts[0] if parts else "")
    return second.split("/")[0].strip() if second else "Unknown"


def _market_cap_for(ticker: str, watchlist_lookup: dict) -> float | None:
    """从 watchlist「yf市值」字段取值，单位转成美元。"""
    wf = watchlist_lookup.get(ticker.upper(), {})
    mcap_str = wf.get("yf市值", "")
    if isinstance(mcap_str, list):
        mcap_str = (mcap_str[0].get("text", "") if mcap_str else "") or ""
    if not mcap_str or not isinstance(mcap_str, str):
        return None
    s = str(mcap_str).strip().upper()
    try:
        if s.endswith("T"):
            return float(s[:-1]) * 1e12
        if s.endswith("B"):
            return float(s[:-1]) * 1e9
        if s.endswith("M"):
            return float(s[:-1]) * 1e6
        return float(s)
    except ValueError:
        return None


# ─────────── Markowitz（蒙特卡洛带约束）───────────

def markowitz_constrained(mean_rets: np.ndarray, cov: np.ndarray,
                          risk_free: float = 0.045 / 252,
                          n_iter: int = 20000, max_w: float = 0.15,
                          min_w: float = 0.02, cash_pct: float = 0.05,
                          seed: int = 42) -> tuple[np.ndarray | None, float]:
    """带 max/min 仓位约束的 Markowitz Max Sharpe。"""
    n = len(mean_rets)
    invest = 1 - cash_pct
    best_sharpe = -np.inf
    best_w = None
    np.random.seed(seed)
    for _ in range(n_iter):
        w = np.random.dirichlet(np.ones(n) * 1.5)
        w = np.clip(w, min_w / invest, max_w / invest)
        w = w / w.sum()
        if w.max() > max_w / invest + 1e-6:
            continue
        port_var = w @ cov @ w
        if port_var <= 0:
            continue
        port_ret = w @ mean_rets
        sharpe = (port_ret - risk_free) / np.sqrt(port_var)
        if sharpe > best_sharpe:
            best_sharpe = sharpe
            best_w = w
    return (best_w * invest, best_sharpe) if best_w is not None else (None, best_sharpe)


# ─────────── 主流水线 ───────────

def run(capital: float = DEFAULT_CAPITAL,
        top_n: int = 24,
        max_positions: int = 15,
        market_scope: str = "US",
        max_weight: float = 0.15,
        min_weight: float = 0.02,
        cash_pct: float = 0.05,
        max_adv_pct: float = 0.05,
        cost_bps: float = 5.0,
        impact_bps_per_pct_adv: float = 2.0,
        skip_neutralize: bool = False,
        use_legacy_mc: bool = False,
        max_corr: float = 0.7,
        kelly_fraction: float = 0.5,
        vol_target_annual: float | None = None) -> dict:
    """完整 v6 流水线。返回结果 dict。"""
    print("=" * 92)
    print(f"  📊 方案 A v6（中性化 + Markowitz + ADV 约束 + 成本扣减）")
    print("=" * 92)

    # ────── 1. 读因子缓存 ──────
    market_scope = str(market_scope or "US").upper()
    cached = _load_factor_scores(market_scope=market_scope)
    if not cached:
        return {"error": f"{market_scope} factor scores 不存在；先跑对应推荐生成"}

    factors = cached["factors"]
    sig_map = {s["ticker"]: s for s in cached.get("signals", [])}

    # ────── 2. 读 V2 universe 拿行业 + 市值（用于中性化 + 行业 cap）──────
    print("\n[1/5] 读 V2 system_universe 拿行业 + 市值...")
    wl_lookup: dict = {}
    try:
        import sys as _sys
        _sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
        from stock_db import fetch_universe_for_ai_recommendations  # type: ignore
        v2_added = 0
        for u in fetch_universe_for_ai_recommendations():
            key = (u["symbol"] or "").upper()
            if key and key not in wl_lookup:
                wl_lookup[key] = {
                    "行业归类": u.get("industry") or u.get("theme") or "",
                    "yf市值": "",
                }
                v2_added += 1
        if v2_added:
            print(f"  V2 universe 补齐: +{v2_added} 条")
    except Exception as e:
        print(f"  ⚠️ V2 universe 加载失败: {e}")
    if not wl_lookup:
        skip_neutralize = True
    print(f"  合并 lookup: {len(wl_lookup)} 条")

    # ────── 3. 用因子 + 中性化重新排序 ──────
    fallback_v2 = bool(cached.get("fallback_v2"))
    if fallback_v2:
        # V2 fallback：跳过 combine_factors + IC gate（V2 推荐 run 已合成 total_score）
        print(f"\n[2/5] V2 fallback 模式：直接用 recommendation_picks.total_score 作为 composite "
              f"（run_id={cached.get('fallback_run_id')}）")
        df = pd.DataFrame([
            {"ticker": f["ticker"], "composite": f.get("composite") or 0.0}
            for f in factors
        ])
        df = df.sort_values("composite", ascending=False).reset_index(drop=True)
        df["composite_neutral"] = df["composite"]
        factor_weights = {}
        skip_neutralize = True
    else:
        print(f"\n[2/5] 因子合成{'（含中性化）' if not skip_neutralize else '（不中性化，对照模式）'}...")
        from factor_model import DEFAULT_FACTOR_WEIGHTS, combine_factors
        from early_signals import score_analyst
        from stock_research.core.factor_ic_gate import evaluate_gate, format_report

        gate = evaluate_gate()
        print(format_report(gate))
        factor_weights = dict(DEFAULT_FACTOR_WEIGHTS)
        if not gate.passed:
            healthy = set(gate.healthy_factors or [])
            if not healthy:
                return {"error": "factor_ic_gate_no_healthy_factors", "gate_reason": gate.reason}
            factor_weights = {
                k: (v if k in healthy else 0.0)
                for k, v in DEFAULT_FACTOR_WEIGHTS.items()
            }
            dropped = [k for k, v in factor_weights.items() if v <= 0]
            print(
                "  🟡 IC gate 未全通过：组合优化只使用 healthy 因子 "
                f"{sorted(healthy)}；降权 {dropped}"
            )

        analyst = {
            tk: (None if not s.get("analyst") or "error" in (s.get("analyst") or {})
                 else score_analyst(s.get("analyst"))[0])
            for tk, s in sig_map.items()
        }
        df = combine_factors(
            factors,
            analyst_signals=analyst,
            include_reversal=True,
            factor_weights=factor_weights,
        )

    if not fallback_v2 and not skip_neutralize and not df.empty and len(wl_lookup) > 0:
        df["industry"] = df["ticker"].apply(lambda t: _industry_for(t, wl_lookup))
        df["market_cap"] = df["ticker"].apply(lambda t: _market_cap_for(t, wl_lookup))
        # 对各因子做行业 + 市值中性化
        neutralizable = {
            "momentum": "z_mom",
            "reversal": "z_rev",
            "pead": "z_pead",
        }
        factor_cols = [
            col for factor, col in neutralizable.items()
            if factor_weights.get(factor, 0.0) > 0 and col in df.columns
        ]
        if factor_cols:
            df = nz.neutralize_all(df, factor_cols, industry_col="industry",
                                   market_cap_col="market_cap")
            # 用中性化后的 z 列重新合成 composite
            n_z_cols = [f"n_{c}" for c in factor_cols if f"n_{c}" in df.columns]
            if n_z_cols:
                df["composite_neutral"] = df[n_z_cols].mean(axis=1)
                df = df.sort_values("composite_neutral", ascending=False).reset_index(drop=True)
                print(f"  ✅ 中性化使用列: {n_z_cols}")
                print(f"  ↳ Top 5（中性化）: {df.head(5)['ticker'].tolist()}")
                print(f"  ↳ Top 5（原始）  : {combine_factors(factors, analyst_signals=analyst, factor_weights=factor_weights).head(5)['ticker'].tolist()}")

    top = df.head(top_n)
    tickers = top["ticker"].tolist()
    print(f"\n  候选 Top {top_n}: {tickers}")

    # ────── 4. 拉历史收益 + ADV ──────
    print(f"\n[3/5] 拉 {len(tickers)} 只标的历史收益 + ADV...")
    returns_dict = {}
    adv_dict = {}
    for tk in tickers:
        rets, adv = _fetch_returns_and_adv(tk, lookback_days=252)
        if rets is not None and adv is not None:
            returns_dict[tk] = rets
            adv_dict[tk] = adv
            print(f"  ✅ {tk:<8} ADV=${adv/1e6:8.1f}M  样本 {len(rets)} 天")
        else:
            print(f"  ❌ {tk:<8} 数据获取失败")

    if len(returns_dict) < 3:
        return {"error": "可用样本不足"}

    # 对齐
    min_len = min(len(r) for r in returns_dict.values())
    aligned = {tk: r[-min_len:] for tk, r in returns_dict.items()}
    final_tickers = list(aligned.keys())
    matrix = np.array([aligned[t] for t in final_tickers])
    mean_rets = matrix.mean(axis=1)
    cov = np.cov(matrix)
    mean_ret_map = {final_tickers[i]: float(mean_rets[i]) for i in range(len(final_tickers))}
    cov_df = pd.DataFrame(cov, index=final_tickers, columns=final_tickers)

    # ────── 5. 核心组合优化 ──────
    desired_cash_pct = cash_pct
    if use_legacy_mc:
        print(f"\n[4/5] Markowitz 蒙特卡洛 20000 次（legacy）...")
        weights, sharpe = markowitz_constrained(mean_rets, cov,
                                                n_iter=20000, max_w=max_weight,
                                                min_w=min_weight, cash_pct=cash_pct)
        if weights is None:
            return {"error": "Markowitz 优化失败"}
        annual_sharpe = sharpe * np.sqrt(252)
        annual_ret = (mean_rets @ weights) * 252
        annual_vol = np.sqrt(weights @ cov @ weights) * np.sqrt(252)
        target_w = {final_tickers[i]: float(weights[i]) for i in range(len(final_tickers))}
        risk_aware_meta: dict = {"engine": "legacy_monte_carlo"}
    else:
        # 新路径：Ledoit-Wolf cov + 相关性剪枝 + 风险闸门多级降级
        print(f"\n[4/5] risk_aware_optimize（Ledoit-Wolf + 相关性<{max_corr} + 风险闸门）...")
        # final_tickers 已按 watchlist 顺序排，但传入 ranked_tickers 给剪枝
        # 用因子 composite 排序（df 在第 [2/5] 步已 sort 过，按 df 顺序取交集）
        ranked = [t for t in df["ticker"].tolist() if t in final_tickers]
        rdf = pd.DataFrame({tk: aligned[tk] for tk in final_tickers})
        out = opt_pro.risk_aware_optimize(
            rdf, ranked_tickers=ranked,
            max_weight=max_weight, min_weight=min_weight,
            cash_pct=cash_pct, max_corr=max_corr,
        )
        if "error" in out and "weights" not in out:
            print(f"  ⚠️ risk_aware 全级失败：{out['error']}；回退到 legacy MC")
            weights, sharpe = markowitz_constrained(mean_rets, cov,
                                                    n_iter=20000, max_w=max_weight,
                                                    min_w=min_weight, cash_pct=cash_pct)
            if weights is None:
                return {"error": "Markowitz 优化失败（含 fallback）"}
            annual_sharpe = sharpe * np.sqrt(252)
            annual_ret = (mean_rets @ weights) * 252
            annual_vol = np.sqrt(weights @ cov @ weights) * np.sqrt(252)
            target_w = {final_tickers[i]: float(weights[i]) for i in range(len(final_tickers))}
            risk_aware_meta = {"engine": "legacy_monte_carlo (fallback)",
                               "stages": out.get("stages", [])}
        else:
            # risk_aware_optimize 现在返回 weights sum=1（100% 投资视角）+ cash_pct 元数据。
            # 调用方按 stage 实际建议的 cash 缩股票权重，结果 sum = 1 - effective_cash，
            # 与 legacy MC 路径（markowitz_constrained 内部已缩）保持一致。
            effective_cash = float(out.get("cash_pct", cash_pct))
            desired_cash_pct = effective_cash
            deployed = max(0.0, 1.0 - effective_cash)
            target_w = {t: float(w) * deployed for t, w in out["weights"].items()}
            target_w, optimizer_dropped = _limit_positions_by_optimizer_weight(
                target_w,
                ranked,
                max_positions,
            )
            if optimizer_dropped:
                print(f"  最终组合限 {max_positions} 只，按优化权重剔除 {len(optimizer_dropped)} 只：")
                for tk in optimizer_dropped[:8]:
                    print(f"    · {tk}")
            # stage_metrics 是 100% 投资基线；下游显示用，乘 deployed 才是实盘组合的年化数字
            # （cash 部分按 rf=4.5% 计入收益；vol 假设 cash 零方差）
            stage_metrics = out["stages"][out["risk_aware_stage"]].get("metrics") or {}
            stock_ret = float(stage_metrics.get("annual_return", 0.0))
            stock_vol = float(stage_metrics.get("annual_vol", 0.0))
            annual_ret = stock_ret * deployed + 0.045 * effective_cash
            annual_vol = stock_vol * deployed
            annual_sharpe = (annual_ret - 0.045) / annual_vol if annual_vol > 0 else 0.0
            risk_aware_meta = {
                "engine": "risk_aware_optimize",
                "stage": out["risk_aware_stage"],
                "stage_label": out["stages"][out["risk_aware_stage"]].get("label"),
                "effective_cash_pct": effective_cash,
                "pruned_dropped": out.get("pruned_dropped", []),
                "selected_tickers": out.get("selected_tickers", []),
                "max_positions": max_positions,
                "position_dropped": optimizer_dropped,
                "warning": out.get("warning"),
            }
            if out.get("pruned_dropped"):
                print(f"  相关性剪枝丢弃 {len(out['pruned_dropped'])} 只：")
                for d in out["pruned_dropped"][:5]:
                    print(f"    · {d['dropped']} vs {d['vs']} ρ={d['rho']}")
            print(f"  最终采用 stage={out['risk_aware_stage']} "
                  f"（{out['stages'][out['risk_aware_stage']].get('label')}）")
            for s in out["stages"]:
                m = s.get("metrics") or {}
                br = s.get("breached") or s.get("error") or "—"
                print(f"    [{s['stage']}] {s['label']}: vol={m.get('annual_vol', 0):.2%} "
                      f"DD={m.get('max_drawdown', 0):.2%} → {br}")

    print(f"  组合年化 Sharpe = {annual_sharpe:.2f}")
    print(f"  组合年化收益   = {annual_ret*100:+.1f}%")
    print(f"  组合年化波动   = {annual_vol*100:.1f}%")

    # ────── 5vol/6. 波动率目标（Moreira-Muir 2017 JF · 可选）──────
    # 学术依据：Moreira-Muir 2017 JF "Volatility-Managed Portfolios" — 按目标
    # 年化波动率反向缩放仓位；Cederburg-O'Doherty-Wang-Yan 2020 JFE 复现挑战
    # 指出某些样本期效应消失，故默认关闭，需用户显式开启。
    vol_scale = 1.0
    vol_target_info: dict | None = None
    if vol_target_annual is not None and vol_target_annual > 0 and annual_vol > 0:
        raw_scale = vol_target_annual / annual_vol
        vol_scale = min(1.0, raw_scale)  # 不加杠杆（cap 在 1.0）
        if vol_scale < 0.999:
            print(f"\n[5vol/6] ⚠️ 波动率目标触发（实测 {annual_vol*100:.1f}% > 目标 "
                  f"{vol_target_annual*100:.1f}%）：组合缩放 ×{vol_scale:.2f}")
            target_w = {tk: w * vol_scale for tk, w in target_w.items()}
            # 缩放后 annual_vol / sharpe / return 需重算（线性缩放后 sharpe 不变）
            annual_vol = annual_vol * vol_scale
            annual_ret = annual_ret * vol_scale + 0.045 * (1 - vol_scale)  # cash 部分按 rf
            annual_sharpe = (annual_ret - 0.045) / annual_vol if annual_vol > 0 else 0.0
        else:
            print(f"\n[5vol/6] 🟢 波动率目标检查通过（实测 {annual_vol*100:.1f}% ≤ 目标 "
                  f"{vol_target_annual*100:.1f}%）")
        vol_target_info = {
            "target_annual": vol_target_annual,
            "raw_scale": round(raw_scale, 4),
            "applied_scale": round(vol_scale, 4),
            "annual_vol_after": round(annual_vol, 4),
        }

    # ────── 5pre/6. 半 Kelly 单股上限（破产保护 / Thorp 2006）──────
    # 个股层 cap，先于行业/ADV 组合层约束。kelly_fraction=0 时禁用。
    kelly_clipped: list[str] = []
    if kelly_fraction > 0:
        cap_val = max_weight * kelly_fraction
        kelly_capped = pc.kelly_cap(target_w, max_single_pct=max_weight,
                                    kelly_fraction=kelly_fraction)
        kelly_clipped = [tk for tk, w in target_w.items()
                         if kelly_capped[tk] < w - 1e-9]
        if kelly_clipped:
            print(f"\n[5/6] ⚠️ 半 Kelly 单股上限触发（≤ {cap_val:.1%}）：")
            for tk in kelly_clipped[:5]:
                print(f"    · {tk}: {target_w[tk]*100:+.1f}% → {kelly_capped[tk]*100:+.1f}%")
        else:
            print(f"\n[5/6] 🟢 半 Kelly 单股上限检查通过（≤ {cap_val:.1%}）")
        target_w = kelly_capped  # 进入组合层 cap

    # ────── 6a. 行业敞口约束（≤ 25% / 行业）──────
    industries_map = {tk: _industry_for(tk, wl_lookup) for tk in target_w}
    industry_capped, industry_summary = pc.cap_by_industry(
        target_w, industries_map, max_industry_pct=0.25,
    )
    capped_industries = [ind for ind, s in industry_summary.items()
                         if s["original"] > s["capped"] + 1e-6]
    if capped_industries:
        print(f"\n[5a/6] ⚠️ 行业敞口约束触发（≤ 25%）：")
        for ind in capped_industries:
            s = industry_summary[ind]
            print(f"    · {ind}: {s['original']:.1%} → {s['capped']:.1%}（溢出 {s['overflow']:.1%}）")
    else:
        print(f"\n[5a/6] 🟢 行业敞口检查通过（最高 {max(s['original'] for s in industry_summary.values()):.1%}）")
    target_w = industry_capped  # 用约束后的权重继续下一步

    # ────── 6a2. 约束后现金再分配 ──────
    target_gross = max(0.0, 1.0 - desired_cash_pct)
    single_cap_after_kelly = max_weight * kelly_fraction if kelly_fraction > 0 else max_weight
    redistributed, redistribution_summary = _redistribute_to_target_gross(
        target_w,
        ranked if not use_legacy_mc else final_tickers,
        industries_map,
        adv_dict,
        portfolio_value=capital,
        target_gross=target_gross,
        max_single_pct=single_cap_after_kelly,
        max_industry_pct=0.25,
        max_adv_pct=max_adv_pct,
    )
    if redistribution_summary.get("redistributed", 0) > 0:
        print(
            f"\n[5a2/6] 🟢 约束后现金再分配："
            f"{float(redistribution_summary['before_gross']):.1%} → "
            f"{float(redistribution_summary['after_gross']):.1%}"
        )
    else:
        print(f"\n[5a2/6] 约束后无可再分配额度（gross={sum(target_w.values()):.1%}）")
    target_w = redistributed

    # ────── 6b. ADV 限流 + 交易成本 ──────
    print(f"\n[5b/6] 应用 ADV 限流（≤ {max_adv_pct:.0%} ADV/单日）+ 成本扣减...")
    prev_w: dict[str, float] = {}  # 假设当前空仓
    capped, warns = pc.cap_by_adv(target_w, prev_w, adv_dict, capital,
                                  max_adv_pct=max_adv_pct)
    if warns:
        print(f"  ⚠️ ADV 限流触发 {len(warns)} 项：")
        for w in warns[:5]:
            print(f"    · {w}")

    cost = pc.apply_transaction_cost(capped, prev_w, capital,
                                     adv_dollars=adv_dict,
                                     cost_bps=cost_bps,
                                     impact_bps_per_pct_adv=impact_bps_per_pct_adv)
    annual_ret, annual_vol, annual_sharpe = _portfolio_stats_for_weights(
        capped,
        mean_ret_map,
        cov_df,
    )
    print(f"\n  组合层成本: ${cost['total_cost_dollars']:,.2f} "
          f"({cost['total_cost_bps_of_portfolio']:.1f} bps)")
    print(f"  单边换手率: {cost['turnover']:.1%}")
    net_alpha_pct = pc.alpha_after_cost(annual_ret * 100, cost['total_cost_bps_of_portfolio'])
    print(f"  Gross alpha = {annual_ret*100:+.1f}% → "
          f"Net alpha = {net_alpha_pct:+.1f}%（扣 {cost['total_cost_bps_of_portfolio']:.1f} bps 成本）")

    # ────── 7. 输出 ──────
    print(f"\n  {'股票':<10}{'目标w':>9}{'限流后':>9}{'金额':>12}{'ADV(M)':>10}")
    print("  " + "-" * 55)
    plan = []
    plan_v5 = []
    df_lookup = {str(r.get("ticker")): r for r in df.to_dict("records")}
    output_tickers = [t for t in (ranked if not use_legacy_mc else final_tickers) if t in target_w]
    output_tickers.extend([t for t in target_w if t not in set(output_tickers)])
    for tk in output_tickers:
        v_target = target_w.get(tk, 0)
        v_capped = capped.get(tk, 0)
        if abs(v_target) <= 1e-9 and abs(v_capped) <= 1e-9:
            continue
        amount = v_capped * capital
        adv_m = adv_dict.get(tk, 0) / 1e6
        flag = " ⚠️" if abs(v_target - v_capped) > 1e-6 else ""
        print(f"  {tk:<10}{v_target*100:>+8.1f}%{v_capped*100:>+8.1f}%{amount:>11,.0f}{adv_m:>9.1f}{flag}")
        info = df_lookup.get(tk, {})
        composite = _safe_float(info.get("composite_neutral"), _safe_float(info.get("composite"), 0.0))
        entry = {
            "ticker": tk,
            "target_weight": _safe_float(v_target, 0.0),
            "capped_weight": _safe_float(v_capped, 0.0),
            "amount": round(_safe_float(amount, 0.0), 2),
            "amount_rmb": round(_safe_float(amount, 0.0), 2),
            "adv_dollars": _safe_float(adv_dict.get(tk), 0.0),
            "f_score": _safe_float(info.get("f_score")),
            "composite_z": composite,
            "composite_neutral": _safe_float(info.get("composite_neutral")),
        }
        plan.append(entry)
        # 兼容旧生产读路径：plan_v5/v5_weight 字段名保留，但值来自 v6 风险感知优化器。
        plan_v5.append({
            **entry,
            "v5_weight": _safe_float(v_capped, 0.0),
            "v6_weight": _safe_float(v_capped, 0.0),
            "current_weight": 0.0,
        })

    used_total = capital * sum(capped.values())
    actual_cash = capital - used_total
    print(f"  {'现金':<10}{'':>9}{actual_cash/capital*100:>+8.1f}%{actual_cash:>11,.0f}")

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "method": ("v6+: factor neutralization + risk_aware_optimize "
                   "(Ledoit-Wolf + corr<0.7 + risk gate) + ADV cap + cost"
                   if not use_legacy_mc else
                   "v6: factor neutralization + Markowitz MC + ADV cap + cost"),
        "capital": capital,
        "constraints": {
            "market_scope": market_scope,
            "max_weight": max_weight, "min_weight": min_weight,
            "cash_pct": cash_pct, "max_adv_pct": max_adv_pct,
            "cash_pct_effective": round(actual_cash / capital, 6) if capital else cash_pct,
            "gross_exposure_effective": round(sum(capped.values()), 6),
            "cost_bps": cost_bps, "impact_bps_per_pct_adv": impact_bps_per_pct_adv,
            "neutralize": not skip_neutralize,
            "max_corr": max_corr,
            "kelly_fraction": kelly_fraction,
            "vol_target_annual": vol_target_annual,
            "max_positions": max_positions,
            "use_legacy_mc": use_legacy_mc,
            "factor_weights_used": factor_weights,
        },
        "factor_ic_gate": (
            {
                "passed": gate.passed,
                "reason": gate.reason,
                "healthy_factors": gate.healthy_factors,
                "inverted_factors": gate.inverted_factors,
            } if not fallback_v2 else {
                "passed": None,
                "reason": "v2_fallback_skipped",
                "healthy_factors": None,
                "inverted_factors": None,
                "fallback_run_id": cached.get("fallback_run_id"),
            }
        ),
        "kelly_clipped": kelly_clipped,
        "cash_redistribution": redistribution_summary,
        "vol_target": vol_target_info,
        "portfolio_metrics": {
            "annual_sharpe": round(annual_sharpe, 2),
            "annual_return_pct": round(annual_ret * 100, 2),
            "annual_vol_pct": round(annual_vol * 100, 2),
            "gross_alpha_pct": round(annual_ret * 100, 2),
            "total_cost_bps": round(cost['total_cost_bps_of_portfolio'], 2),
            "net_alpha_pct": round(net_alpha_pct, 2),
            "turnover": round(cost["turnover"], 4),
        },
        "risk_aware": risk_aware_meta,
        "candidate_universe": {
            "source": "v2_recommendation_picks",
            "run_id": cached.get("v2_universe_run_id"),
            "run_date": cached.get("v2_universe_run_date"),
            "market_scope": market_scope,
            "count": cached.get("v2_universe_count"),
            "mode": "v2_lite" if fallback_v2 else "full_factor",
        },
        "plan_v5": plan_v5,
        "plan_v6": plan,
        "plan": plan,
        "adv_warnings": warns,
    }
    store.save_json(result, config.AUDIT_DIR.parent / "optimize", "plan_v6")
    result["portfolio_plan_sync"] = _sync_risk_aware_plan_to_duckdb(
        result,
        cached.get("fallback_run_id"),
    )
    _write_latest_plan(result)
    print(f"\n✅ 快照已保存")
    return result


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    p = argparse.ArgumentParser(description="组合优化 v6: 中性化 + Markowitz + ADV + 成本")
    p.add_argument("--capital", type=float, default=DEFAULT_CAPITAL,
                   help=f"组合规模（默认读 DuckDB total_capital={DEFAULT_CAPITAL:,.0f}）")
    p.add_argument("--top-n", type=int, default=24, help="优化候选数")
    p.add_argument("--max-positions", type=int, default=15, help="最终组合最多持仓数")
    p.add_argument("--market", default="US",
                   help="组合优化市场范围；plan_a_v5 生产默认 US，避免美股调仓单混入 A/H")
    p.add_argument("--max-weight", type=float, default=0.15)
    p.add_argument("--min-weight", type=float, default=0.02)
    p.add_argument("--max-adv-pct", type=float, default=0.05, help="单日交易上限占 ADV 的比例")
    p.add_argument("--cost-bps", type=float, default=5.0, help="双边佣金（bps）")
    p.add_argument("--no-neutralize", action="store_true", help="跳过中性化（对照模式）")
    p.add_argument("--legacy-mc", action="store_true",
                   help="用旧蒙特卡洛 Markowitz（默认走 risk_aware_optimize）")
    p.add_argument("--max-corr", type=float, default=0.7,
                   help="选股层 pairwise 相关性上限（贪心剪枝阈值）")
    p.add_argument("--kelly-fraction", type=float, default=0.5,
                   help="半 Kelly 系数（Thorp 2006，默认 0.5；传 0 禁用单股 cap）")
    p.add_argument("--vol-target", type=float, default=None,
                   help="目标年化波动率（Moreira-Muir 2017，如 0.20 = 20%%；默认关闭）")
    args = p.parse_args()
    r = run(capital=args.capital, top_n=args.top_n,
            max_positions=args.max_positions,
            market_scope=args.market,
            max_weight=args.max_weight, min_weight=args.min_weight,
            max_adv_pct=args.max_adv_pct, cost_bps=args.cost_bps,
            skip_neutralize=args.no_neutralize,
            use_legacy_mc=args.legacy_mc,
            max_corr=args.max_corr,
            kelly_fraction=args.kelly_fraction,
            vol_target_annual=args.vol_target)
    return 0 if "error" not in r else 1


if __name__ == "__main__":
    sys.exit(main())
