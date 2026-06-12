"""Early growth radar for US AI / hard-tech names.

This job is deliberately separate from the production AI recommendation list.
The recommendation list is a right-side ranking feed; this radar looks for
left-side or early-middle signals: strong theme, catalysts or 13F support, but
price action not yet overheated.

It is advisory-only. It writes JSON/Markdown artifacts and never writes
watchlist, recommendation picks, portfolio plans, or real holdings.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from stock_research import config

REPO = Path(__file__).resolve().parents[2]
DEFAULT_JSON = REPO / "data" / "latest" / "early_growth_radar.json"
DEFAULT_MD = REPO / "data" / "reports" / "early_growth_radar.md"

FOCUS_TICKERS = {
    "ALAB", "CRDO", "TEM", "RKLB",
    "CRWV", "NBIS", "IREN", "APLD",
    "ASTS", "OKLO", "SMR", "IONQ", "SOUN",
}

MATURE_CORE_TICKERS = {
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSM",
    "AVGO", "MRVL", "AMD", "QCOM", "ORCL", "DELL", "HPE", "SMCI",
    "MU", "ARM", "VRT", "GEV", "VST", "ETN", "PWR",
}

THEME_SCORES: list[tuple[tuple[str, ...], int, str]] = [
    (("ai cloud", "ai data center", "ai data centers"), 22, "AI 云/数据中心早期基础设施"),
    (("ai connectivity", "asic", "networking", "nvlink", "cxl"), 22, "AI 互联/定制芯片链"),
    (("advanced nuclear", "nuclear", "power generation", "grid", "electrical"), 20, "AI 电力/能源约束链"),
    (("space infrastructure", "satellite"), 18, "空间通信/硬科技基础设施"),
    (("quantum",), 16, "量子计算早期硬科技"),
    (("ai healthcare", "ai drug", "voice ai", "robotics", "physical ai"), 16, "AI 应用/物理 AI 早期链"),
    (("semiconductor", "memory", "chip ip", "eda"), 12, "半导体基础链"),
    (("cybersecurity", "data cloud", "edge cloud"), 10, "AI 软件/数据基础层"),
]


def _num(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def _latest_prices(conn: duckdb.DuckDBPyConnection) -> dict[tuple[str, str], dict[str, Any]]:
    """每只取最新行；动量/估值字段为空时回退最近一条带动量的行（限 7 天内）。

    2026-06-12 修复：早班批次只有隔日动量（完整因子行晚间才落库），最新行
    one_month/one_week/one_year 常为 NULL，曾把全部候选的价格分打到 4/35、
    early_or_watch 清零、页面整段消失。回退用的是历史行，PIT 安全。
    """
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT market, symbol, MAX(trade_date) AS trade_date
            FROM price_daily
            GROUP BY market, symbol
        ),
        latest_mom AS (
            SELECT market, symbol, MAX(trade_date) AS trade_date
            FROM price_daily
            WHERE one_month_pct IS NOT NULL
            GROUP BY market, symbol
        )
        SELECT pd.market, pd.symbol, pd.trade_date, pd.close, pd.currency,
               COALESCE(pd.market_cap,  pm.market_cap)  AS market_cap,
               COALESCE(pd.forward_pe,  pm.forward_pe)  AS forward_pe,
               COALESCE(pd.peg_ratio,   pm.peg_ratio)   AS peg_ratio,
               COALESCE(pd.one_week_pct,  pm.one_week_pct)  AS one_week_pct,
               COALESCE(pd.one_month_pct, pm.one_month_pct) AS one_month_pct,
               COALESCE(pd.one_year_pct,  pm.one_year_pct)  AS one_year_pct,
               COALESCE(pd.ytd_pct,       pm.ytd_pct)       AS ytd_pct,
               CASE WHEN pd.one_month_pct IS NULL AND pm.one_month_pct IS NOT NULL
                    THEN CAST(pm.trade_date AS VARCHAR) END AS momentum_as_of
        FROM price_daily pd
        JOIN latest
          ON latest.market = pd.market
         AND latest.symbol = pd.symbol
         AND latest.trade_date = pd.trade_date
        LEFT JOIN latest_mom lm
          ON lm.market = pd.market AND lm.symbol = pd.symbol
         AND lm.trade_date >= pd.trade_date - INTERVAL 7 DAY
        LEFT JOIN price_daily pm
          ON pm.market = lm.market AND pm.symbol = lm.symbol
         AND pm.trade_date = lm.trade_date
        """
    ).fetchall()
    cols = [
        "market", "symbol", "trade_date", "close", "currency",
        "market_cap", "forward_pe", "peg_ratio",
        "one_week_pct", "one_month_pct", "one_year_pct", "ytd_pct",
        "momentum_as_of",
    ]
    return {(r[0], r[1]): dict(zip(cols, r)) for r in rows}


def _us_universe(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT market, symbol, name, theme, industry, source, active
        FROM system_universe
        WHERE market = 'US' AND active = TRUE
        ORDER BY symbol
        """
    ).fetchall()
    cols = ["market", "symbol", "name", "theme", "industry", "source", "active"]
    return [dict(zip(cols, r)) for r in rows]


def _latest_pick_positions(conn: duckdb.DuckDBPyConnection) -> tuple[str | None, dict[str, dict[str, Any]]]:
    row = conn.execute(
        """
        SELECT run_id
        FROM recommendation_runs
        ORDER BY generated_at DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None, {}
    run_id = str(row[0])
    rows = conn.execute(
        """
        SELECT symbol, name, rank, rating, signal, total_score, entry_price
        FROM recommendation_picks
        WHERE run_id = ? AND market = 'US'
        ORDER BY rank NULLS LAST, total_score DESC NULLS LAST
        """,
        [run_id],
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for pos, r in enumerate(rows, 1):
        out[str(r[0])] = {
            "symbol": r[0],
            "name": r[1],
            "rank": r[2],
            "market_position": pos,
            "rating": r[3],
            "signal": r[4],
            "total_score": r[5],
            "entry_price": r[6],
        }
    return run_id, out


def price_early_score(row: dict[str, Any]) -> tuple[int, list[str], list[str]]:
    one_week = _num(row.get("one_week_pct"))
    one_month = _num(row.get("one_month_pct"))
    one_year = _num(row.get("one_year_pct"))
    score = 0
    reasons: list[str] = []
    flags: list[str] = []

    if one_month is None and one_week is None:
        return 4, ["行情动量缺失，只能作覆盖检查"], ["MISSING_PRICE_MOMENTUM"]

    if one_month is None:
        score += 10
    elif one_month > 45:
        score += 0
        flags.append("OVERHEATED_1M")
        reasons.append(f"1个月已涨 {one_month:.1f}%，不是早期")
    elif one_month > 30:
        score += 10
        flags.append("WARM_1M")
        reasons.append(f"1个月已涨 {one_month:.1f}%，只能等回调")
    elif one_month >= -10:
        score += 28
        reasons.append(f"1个月 {one_month:+.1f}%，尚未明显过热")
    elif one_month >= -30:
        score += 18
        reasons.append(f"1个月 {one_month:+.1f}%，左侧修复观察")
    else:
        score += 5
        flags.append("BROKEN_TREND")
        reasons.append(f"1个月 {one_month:+.1f}%，趋势仍弱")

    if one_week is not None:
        if one_week > 22:
            score -= 8
            flags.append("OVERHEATED_1W")
            reasons.append(f"1周已涨 {one_week:.1f}%，短线拥挤")
        elif -8 <= one_week <= 12:
            score += 5
            reasons.append(f"1周 {one_week:+.1f}%，节奏未失控")

    if one_year is not None and one_year > 250 and (one_month or 0) > 20:
        flags.append("OVERHEATED_1Y")
        reasons.append(f"1年已涨 {one_year:.1f}%，右侧确认多于早期")

    return max(0, min(35, score)), reasons, flags


def theme_score(row: dict[str, Any]) -> tuple[int, str]:
    text = " ".join(
        str(row.get(k) or "").lower()
        for k in ("theme", "industry", "source", "name", "symbol")
    )
    for keys, score, label in THEME_SCORES:
        if any(k in text for k in keys):
            return score, label
    return 5, "科技池内普通相关"


def recommendation_gap_score(pick: dict[str, Any] | None) -> tuple[int, str, bool]:
    if not pick:
        return 12, "未进入今日正式推荐，适合做早发现补盲", False
    pos = pick.get("market_position")
    if isinstance(pos, int) and pos <= 10:
        return 0, f"今日美股正式推荐第 {pos}，已经是右侧前排", True
    if isinstance(pos, int) and pos <= 20:
        return 4, f"今日美股正式推荐第 {pos}，已被系统注意", False
    if isinstance(pos, int):
        return 8, f"今日美股正式推荐第 {pos}，尚非前排", False
    return 8, "有推荐记录但排名弱", False


def catalyst_score(catalyst: dict[str, Any] | None) -> tuple[int, str, dict[str, int]]:
    if not catalyst:
        return 0, "近 7 日投行/新闻催化未覆盖", {"bullish": 0, "bearish": 0, "neutral": 0}
    counts = {"bullish": 0, "bearish": 0, "neutral": 0}
    for events in (catalyst.get("categories") or {}).values():
        if not isinstance(events, list):
            continue
        for item in events:
            sent = str((item or {}).get("sentiment") or "neutral").lower()
            if sent in counts:
                counts[sent] += 1
            else:
                counts["neutral"] += 1
    score = min(20, max(0, counts["bullish"] * 4 + counts["neutral"] - counts["bearish"] * 3))
    if counts["bullish"] or counts["bearish"]:
        label = f"近 7 日催化：多 {counts['bullish']} / 空 {counts['bearish']}"
    else:
        label = "近 7 日催化中性或信息不足"
    return score, label, counts


def ownership_score(track: dict[str, Any] | None) -> tuple[int, str]:
    if not track:
        return 0, "13F 暂无覆盖"
    summary = track.get("summary") or {}
    direction = str(summary.get("net_direction") or "")
    adds = int(summary.get("investors_adding") or 0)
    cuts = int(summary.get("investors_cutting") or 0)
    if "买入" in direction:
        return min(15, 8 + adds * 2), f"13F {direction}：加仓 {adds} / 减仓 {cuts}"
    if "卖出" in direction:
        return max(0, 4 - cuts), f"13F {direction}：加仓 {adds} / 减仓 {cuts}"
    if adds or cuts:
        return 6, f"13F 分歧：加仓 {adds} / 减仓 {cuts}"
    return 0, "13F 无明显变化"


def valuation_score(row: dict[str, Any]) -> tuple[int, str]:
    fpe = _num(row.get("forward_pe"))
    peg = _num(row.get("peg_ratio"))
    score = 0
    parts: list[str] = []
    if peg is not None and peg > 0:
        if peg <= 1.0:
            score += 5
            parts.append(f"PEG {peg:.2f} 低")
        elif peg <= 1.8:
            score += 3
            parts.append(f"PEG {peg:.2f} 可接受")
        else:
            parts.append(f"PEG {peg:.2f} 偏贵")
    if fpe is not None and fpe > 0:
        if fpe <= 30:
            score += 5
            parts.append(f"Forward PE {fpe:.1f}")
        elif fpe <= 60:
            score += 3
            parts.append(f"Forward PE {fpe:.1f} 偏成长")
        else:
            parts.append(f"Forward PE {fpe:.1f} 高")
    if not parts:
        return 2, "估值数据缺失或仍亏损"
    return min(10, score), "；".join(parts)


def is_early_fit(row: dict[str, Any]) -> bool:
    symbol = str(row.get("symbol") or "").upper()
    source = str(row.get("source") or "").lower()
    text = " ".join(
        str(row.get(k) or "").lower()
        for k in ("theme", "industry", "source")
    )
    if symbol in FOCUS_TICKERS:
        return True
    if "emerging" in source:
        return True
    early_terms = (
        "ai cloud", "ai data center", "ai data centers",
        "ai connectivity", "ai healthcare", "ai drug",
        "advanced nuclear", "quantum", "space infrastructure",
        "satellite", "voice ai",
    )
    return any(term in text for term in early_terms)


def is_mature_core(row: dict[str, Any]) -> bool:
    symbol = str(row.get("symbol") or "").upper()
    if symbol in MATURE_CORE_TICKERS:
        return True
    cap = _num(row.get("market_cap"))
    source = str(row.get("source") or "").lower()
    # 超大市值且不是 emerging 来源时，不把它伪装成早期股。
    return bool(cap and cap > 150_000_000_000 and "emerging" not in source)


def classify_candidate(
    score: int,
    flags: list[str],
    front_pick: bool,
    has_price: bool,
    *,
    early_fit: bool = True,
    mature_core: bool = False,
) -> tuple[str, str]:
    if not has_price:
        return "覆盖缺口", "补行情/收录后再评分"
    if mature_core or not early_fit:
        return "成熟/非早期", "看正式推荐或持仓纪律，不当早发现股追"
    if any(f.startswith("OVERHEATED") for f in flags) or front_pick:
        return "已涨出右侧", "不追，等回撤或财报确认后再研究"
    if score >= 65:
        return "早发现候选", "进入买前研究；只允许小仓试探，不直接重仓"
    if score >= 50:
        return "潜伏观察", "继续等订单/财报/放量确认"
    return "仅跟踪", "证据不足，先不动作"


def build_payload(db_path: str | os.PathLike[str] | None = None, limit: int = 30) -> dict[str, Any]:
    db = Path(db_path or config.DUCKDB_PATH)
    catalyst_payload = _load_json(REPO / "data" / "latest" / "investment_bank_catalyst_scan.json")
    catalyst_by_ticker = {
        str(item.get("ticker") or "").upper(): item
        for item in (catalyst_payload.get("items") or [])
        if item.get("ticker")
    }
    track_payload = _load_json(REPO / "data" / "latest" / "track_13f.json")
    track_by_ticker = {
        str(k).upper(): v
        for k, v in (track_payload.get("tickers") or {}).items()
    }

    with duckdb.connect(str(db), read_only=True) as conn:
        universe = _us_universe(conn)
        prices = _latest_prices(conn)
        latest_run_id, picks = _latest_pick_positions(conn)

    by_symbol = {str(r["symbol"]).upper(): r for r in universe}
    rows: list[dict[str, Any]] = []
    coverage_gaps: list[dict[str, Any]] = []

    for symbol in sorted(set(by_symbol) | FOCUS_TICKERS):
        u = by_symbol.get(symbol)
        price = prices.get(("US", symbol)) if u else None
        base = {
            "market": "US",
            "symbol": symbol,
            "name": (u or {}).get("name") or symbol,
            "theme": (u or {}).get("theme") or "",
            "industry": (u or {}).get("industry") or "",
            "source": (u or {}).get("source") or "",
            "in_system_universe": bool(u),
            "has_price": bool(price),
        }
        if not u or not price:
            label, action = classify_candidate(0, [], False, False)
            row = {
                **base,
                "score": 0,
                "label": label,
                "suggested_action": action,
                "reasons": [
                    "不在系统 universe" if not u else "已在系统 universe 但缺最新行情",
                    "这是早发现覆盖缺口，不是买入建议",
                ],
                "flags": ["MISSING_UNIVERSE"] if not u else ["MISSING_PRICE"],
            }
            coverage_gaps.append(row)
            continue

        merged = {**base, **price}
        p_score, p_reasons, p_flags = price_early_score(merged)
        t_score, t_reason = theme_score(merged)
        r_score, r_reason, front_pick = recommendation_gap_score(picks.get(symbol))
        c_score, c_reason, c_counts = catalyst_score(catalyst_by_ticker.get(symbol))
        o_score, o_reason = ownership_score(track_by_ticker.get(symbol))
        v_score, v_reason = valuation_score(merged)
        total = min(100, int(round(p_score + t_score + r_score + c_score + o_score + v_score)))
        early_fit = is_early_fit(merged)
        mature_core = is_mature_core(merged)
        label, action = classify_candidate(
            total,
            p_flags,
            front_pick,
            True,
            early_fit=early_fit,
            mature_core=mature_core,
        )
        reasons = [
            *p_reasons[:2],
            t_reason,
            r_reason,
            c_reason,
            o_reason,
            v_reason,
        ]
        rows.append({
            **merged,
            "score": total,
            "label": label,
            "suggested_action": action,
            "score_breakdown": {
                "price_early": p_score,
                "theme": t_score,
                "recommendation_gap": r_score,
                "catalyst": c_score,
                "ownership_13f": o_score,
                "valuation": v_score,
            },
            "latest_recommendation_run_id": latest_run_id,
            "latest_pick": picks.get(symbol),
            "catalyst_counts": c_counts,
            "reasons": reasons,
            "flags": [
                *p_flags,
                *(["NOT_EARLY_FIT"] if not early_fit else []),
                *(["MATURE_CORE"] if mature_core else []),
            ],
        })

    priority = {"早发现候选": 0, "潜伏观察": 1, "仅跟踪": 2, "已涨出右侧": 3, "成熟/非早期": 4}
    rows.sort(key=lambda r: (priority.get(r["label"], 9), -int(r.get("score") or 0), str(r["symbol"])))
    early_rows = [r for r in rows if r["label"] in {"早发现候选", "潜伏观察"}]
    overheated = [r for r in rows if r["label"] == "已涨出右侧"]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scope": "US system_tech_universe + emerging hard-tech coverage focus",
        "latest_recommendation_run_id": latest_run_id,
        "method": (
            "早发现分 = 价格未过热 + 赛道强度 + 未进入正式推荐前排 + "
            "近7日催化 + 13F + 估值 sanity；advisory only"
        ),
        "guardrails": {
            "does_not_write_watchlist": True,
            "does_not_write_real_holdings": True,
            "overheat_rules": ["1M > 45%", "1W > 22%", "1Y > 250% 且 1M > 20%"],
        },
        "counts": {
            "scored": len(rows),
            "early_or_watch": len(early_rows),
            "overheated": len(overheated),
            "coverage_gaps": len(coverage_gaps),
        },
        "candidates": rows[:limit],
        "early_or_watch": early_rows[:limit],
        "overheated": overheated[:limit],
        "coverage_gaps": coverage_gaps,
    }


def write_report(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# 早发现雷达",
        "",
        f"- 生成时间：{payload.get('generated_at')}",
        f"- 最新推荐 run：{payload.get('latest_recommendation_run_id') or '无'}",
        f"- 方法：{payload.get('method')}",
        "- 说明：只读研究输出，不自动写自选股/真实持仓/组合方案。",
        "",
        "## 早发现/潜伏候选",
        "",
    ]
    for row in payload.get("early_or_watch") or []:
        lines.append(
            f"- **{row['symbol']}** {row.get('name') or ''}：{row['label']} "
            f"score={row['score']}，动作={row['suggested_action']}"
        )
        lines.append(f"  - {'；'.join((row.get('reasons') or [])[:4])}")
    if not (payload.get("early_or_watch") or []):
        lines.append("- 暂无。")
    lines.extend(["", "## 已涨出右侧", ""])
    for row in (payload.get("overheated") or [])[:10]:
        lines.append(
            f"- **{row['symbol']}**：score={row['score']}，"
            f"1M={row.get('one_month_pct')}%，动作={row['suggested_action']}"
        )
    lines.extend(["", "## 覆盖缺口", ""])
    for row in payload.get("coverage_gaps") or []:
        lines.append(f"- **{row['symbol']}**：{'; '.join(row.get('reasons') or [])}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build early growth radar.")
    parser.add_argument("--db", default=os.environ.get("STOCK_DB_PATH") or str(config.DUCKDB_PATH))
    parser.add_argument("--out", default=str(DEFAULT_JSON))
    parser.add_argument("--report", default=str(DEFAULT_MD))
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()

    payload = build_payload(args.db, limit=args.limit)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    write_report(payload, Path(args.report))

    print(f"early_growth_radar 写入 {out}")
    print(
        f"  early/watch={payload['counts']['early_or_watch']} · "
        f"overheated={payload['counts']['overheated']} · "
        f"coverage_gaps={payload['counts']['coverage_gaps']}"
    )
    for row in (payload.get("early_or_watch") or [])[:8]:
        print(f"  {row['label']} {row['symbol']} score={row['score']} · {row['suggested_action']}")
    if payload.get("coverage_gaps"):
        gaps = ", ".join(r["symbol"] for r in payload["coverage_gaps"][:10])
        print(f"  coverage gaps: {gaps}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
