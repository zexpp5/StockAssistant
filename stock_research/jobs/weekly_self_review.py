"""周末复盘：对齐「模型推了什么」与「你实际做了什么」。

只做动作 vs 信号对照，不做因子归因。推荐侧只用 recommendation_runs + recommendation_picks
（PIT run_id）；持仓侧用 real_holdings + state_backup diff + real_holding_review 历史。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import stock_db  # type: ignore
from stock_research.jobs.real_holding_review import DISOBEDIENT_ACTIONS

logger = logging.getLogger(__name__)

OUT_DIR = REPO / "data" / "latest"
LETTERS_DIR = REPO / "docs" / "letters"
STATE_BACKUP_DIR = REPO / "state_backup"

DEFAULT_TOP_N = 10


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(REPO / ".env")


def _calendar_week_bounds(ref: date | None = None) -> tuple[date, date]:
    d = ref or date.today()
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _iso_week_label(week_end: date) -> str:
    y, w, _ = week_end.isocalendar()
    return f"{y}-W{w:02d}"


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _holdings_from_backup(path: Path) -> dict[str, float]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = (doc.get("tables") or {}).get("real_holdings", {}).get("rows") or []
    out: dict[str, float] = {}
    for r in rows:
        sym = str(r.get("symbol") or r.get("code") or "").strip()
        if not sym:
            continue
        try:
            out[sym] = float(r.get("shares") or 0)
        except (TypeError, ValueError):
            out[sym] = 0.0
    return out


def _backup_snapshots_between(
    start: date,
    end: date,
    *,
    baseline_lookback_days: int = 3,
) -> list[tuple[date, dict[str, float]]]:
    """收集 [start - baseline_lookback, end] 内的所有持仓快照（按日期升序）。

    Why baseline_lookback：state_backup 文件在每日 morning 流程末尾才生成；
    周日跑复盘时，"本周一开盘前"的基线快照实际落在上周六/日（start - 1 ~ 2）。
    严格按 [start, end] 取窗会把基线漏掉，导致 week_start_shares == week_end_shares，
    所有"增持/减持"判定恒等空。窗口前移 3 天兜底周末/节假日缺数据。
    """
    snaps: list[tuple[date, dict[str, float]]] = []
    if not STATE_BACKUP_DIR.is_dir():
        return snaps
    earliest = start - timedelta(days=max(0, int(baseline_lookback_days)))
    for p in sorted(STATE_BACKUP_DIR.glob("state_*.json")):
        try:
            d = date.fromisoformat(p.stem.replace("state_", "")[:10])
        except ValueError:
            continue
        if earliest <= d <= end:
            snaps.append((d, _holdings_from_backup(p)))
    return snaps


def _current_holdings(conn) -> dict[str, float]:
    out: dict[str, float] = {}
    for h in stock_db.fetch_all_real_holdings(conn=conn):
        sym = str(h.get("symbol") or h.get("code") or "").strip()
        if sym:
            out[sym] = float(h.get("shares") or 0)
    return out


def _infer_market_code(symbol: str, market: str | None = None) -> str:
    if market:
        m = str(market).upper().strip()
        if m in {"CN", "CHINA", "A_SHARE", "A股"}:
            return "CN"
        if m in {"HK", "HONG KONG", "港股"}:
            return "HK"
        if m in {"US", "USA", "美股", "UNITED STATES"}:
            return "US"
        if len(m) <= 3:
            return m
    s = str(symbol).upper()
    if s.endswith(".HK"):
        return "HK"
    if s.endswith((".SS", ".SZ", ".BJ", ".SH")):
        return "CN"
    return "US"


def _market_display(market_code: str) -> str:
    return {"CN": "A股", "US": "美股", "HK": "港股"}.get(market_code, market_code or "—")


def _format_price(price: float | None, currency: str | None, market_code: str) -> str:
    if price is None:
        return "—"
    c = (currency or "").upper()
    if c in {"CNY", "RMB"} or market_code == "CN":
        return f"¥{price:.2f}"
    if c == "HKD" or market_code == "HK":
        return f"HK${price:.2f}"
    if c == "USD" or market_code == "US":
        return f"${price:.2f}"
    return f"{price:.2f} {currency or ''}".strip()


def _latest_prices_by_symbol(conn, symbols: list[str]) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    placeholders = ",".join(["?"] * len(symbols))
    rows = conn.execute(
        f"""
        SELECT market, symbol, close, currency, trade_date
        FROM (
          SELECT market, symbol, close, currency, trade_date,
                 ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
          FROM price_daily
          WHERE symbol IN ({placeholders}) AND interval = '1d' AND close IS NOT NULL
        ) t
        WHERE rn = 1
        """,
        symbols,
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for market, symbol, close, currency, trade_date in rows:
        td = trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date)[:10]
        out[str(symbol)] = {
            "close": float(close),
            "currency": currency,
            "trade_date": td,
            "market": market,
        }
    return out


def _trailing_returns_by_symbol(
    conn, symbols: list[str], *, lookback_trade_days: int = 5,
) -> dict[str, dict[str, Any]]:
    """每只 symbol 的过去 N 个交易日 trailing 累计涨跌幅，直接从 price_daily 算。

    与 pick_outcomes 不同：这是市场回报口径（lookback 窗口固定 = N 个交易日），
    不依赖 pick 时点，picks 当天就有值。N 由 price_daily 实际行数封顶 —— 数据
    不足时退化为「目前能看到的最长窗口」并把实际跨度写进 lookback_trade_days。
    """
    if not symbols:
        return {}
    placeholders = ",".join(["?"] * len(symbols))
    baseline_rn = lookback_trade_days + 1
    rows = conn.execute(
        f"""
        SELECT symbol, rn, close, trade_date FROM (
          SELECT symbol, close, trade_date,
                 ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
          FROM price_daily
          WHERE symbol IN ({placeholders}) AND interval = '1d' AND close IS NOT NULL
        ) t
        WHERE rn <= ?
        """,
        [*symbols, baseline_rn],
    ).fetchall()
    by_sym: dict[str, list[tuple[int, float, Any]]] = {}
    for sym, rn, close, td in rows:
        by_sym.setdefault(str(sym), []).append((int(rn), float(close), td))
    out: dict[str, dict[str, Any]] = {}
    for sym, items in by_sym.items():
        items.sort(key=lambda x: x[0])
        if len(items) < 2:
            continue
        latest_rn, latest_close, latest_td = items[0]
        baseline_rn_actual, baseline_close, baseline_td = items[-1]
        if baseline_close <= 0 or baseline_rn_actual == latest_rn:
            continue
        pct = (latest_close - baseline_close) / baseline_close * 100.0
        out[sym] = {
            "trailing_pct": pct,
            "lookback_trade_days": baseline_rn_actual - 1,
            "latest_close": latest_close,
            "latest_date": latest_td.isoformat() if hasattr(latest_td, "isoformat") else str(latest_td)[:10],
            "baseline_close": baseline_close,
            "baseline_date": baseline_td.isoformat() if hasattr(baseline_td, "isoformat") else str(baseline_td)[:10],
        }
    return out


def _lookup_symbol_info(conn, symbols: list[str]) -> dict[str, dict[str, str]]:
    if not symbols:
        return {}
    placeholders = ",".join(["?"] * len(symbols))
    params = symbols + symbols
    rows = conn.execute(
        f"""
        SELECT symbol,
               COALESCE(MAX(NULLIF(TRIM(name), '')), '') AS name,
               COALESCE(MAX(NULLIF(TRIM(market), '')), '') AS market
        FROM (
          SELECT symbol, name, market FROM system_universe
          WHERE symbol IN ({placeholders}) AND active = TRUE
          UNION ALL
          SELECT symbol, name, market FROM manual_watchlist
          WHERE symbol IN ({placeholders})
        ) t
        GROUP BY symbol
        """,
        params,
    ).fetchall()
    return {
        str(sym): {"name": str(name or ""), "market": str(mkt or "")}
        for sym, name, mkt in rows
    }


def _enrich_row_display(
    row: dict[str, Any],
    *,
    prices: dict[str, dict[str, Any]],
    info: dict[str, dict[str, str]],
    model_meta: dict[str, Any] | None = None,
    trailing: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    sym = str(row.get("symbol") or "")
    meta = model_meta or {}
    db_info = info.get(sym) or {}
    name = (row.get("name") or meta.get("name") or db_info.get("name") or "").strip() or "—"
    market_code = _infer_market_code(sym, row.get("market") or meta.get("market") or db_info.get("market"))
    px = prices.get(sym) or {}
    close = px.get("close")
    currency = px.get("currency")
    row.update({
        "name": name,
        "market": market_code,
        "market_label": _market_display(market_code),
        "current_price": close,
        "price_currency": currency,
        "price_trade_date": px.get("trade_date"),
        "current_price_display": _format_price(close, currency, market_code),
    })
    tr = (trailing or {}).get(sym) or {}
    if tr:
        row.update({
            "trailing_5d_pct": tr.get("trailing_pct"),
            "trailing_lookback_days": tr.get("lookback_trade_days"),
            "trailing_baseline_date": tr.get("baseline_date"),
            "trailing_baseline_close": tr.get("baseline_close"),
        })
    return row


def _collect_weekly_model_picks(
    conn,
    week_start: date,
    week_end: date,
    *,
    top_n: int,
    universe_scope: str | list[str] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """返回 (symbol -> meta, runs 列表)。

    universe_scope=None 表示扫所有 scope（兼容未来 HK/US picks 接入独立 PIT scope）。
    rank 处理：build_v2_recommendations 写入 picks 时 rank 是 global market-segmented
    （CN=1..20、HK=21..40、US=41..60）。这里取 per_market_top_n 后，在 Python 里把
    rank 重算为 per-market（CN=1..N、HK=1..N、US=1..N），best_rank 因此对三市场可比。
    """
    runs = stock_db.fetch_recommendation_runs_between(
        week_start, week_end, universe_scope=universe_scope, conn=conn,
    )
    by_symbol: dict[str, Any] = {}
    for run in runs:
        run_id = str(run["run_id"])
        run_date = run.get("run_date")
        if hasattr(run_date, "isoformat"):
            run_date_s = run_date.isoformat()
        else:
            run_date_s = str(run_date)[:10]
        picks = stock_db.fetch_recommendation_picks_for_run(
            run_id, per_market_top_n=top_n, conn=conn,
        )
        # QUALIFY 已按 market 分组 + global rank 升序返回。组内 enumerate 即得 per-market rank。
        market_counters: dict[str, int] = {}
        for p in picks:
            mkt = str(p.get("market") or "")
            market_counters[mkt] = market_counters.get(mkt, 0) + 1
            sym = str(p.get("symbol") or p.get("code") or "")
            if not sym:
                continue
            market_rank = market_counters[mkt]
            slot = by_symbol.setdefault(sym, {
                "symbol": sym,
                "name": p.get("name"),
                "market": mkt,
                "first_run_id": run_id,
                "first_run_date": run_date_s,
                "best_rank": market_rank,
                "appearances": 0,
                "run_ids": [],
            })
            slot["appearances"] += 1
            if run_id not in slot["run_ids"]:
                slot["run_ids"].append(run_id)
            if market_rank < slot["best_rank"]:
                slot["best_rank"] = market_rank
                slot["first_run_id"] = run_id
                slot["first_run_date"] = run_date_s
    return by_symbol, runs


def _shares_delta(start: dict[str, float], end: dict[str, float], symbol: str) -> float:
    return float(end.get(symbol, 0)) - float(start.get(symbol, 0))


def _had_increase_during_week(
    symbol: str,
    week_start_shares: dict[str, float],
    week_end_shares: dict[str, float],
    backup_snaps: list[tuple[date, dict[str, float]]],
) -> bool:
    if _shares_delta(week_start_shares, week_end_shares, symbol) > 1e-6:
        return True
    prev = week_start_shares.get(symbol, 0.0)
    for _, snap in backup_snaps:
        cur = snap.get(symbol, 0.0)
        if cur > prev + 1e-6:
            return True
        prev = cur
    return False


def _latest_review_action_in_week(
    symbol: str,
    week_start: date,
    week_end: date,
    conn,
) -> str | None:
    hist = stock_db.fetch_real_holding_review_history(symbols=[symbol], days=14, conn=conn)
    rows = hist.get(symbol) or []
    best: str | None = None
    best_date: date | None = None
    for row in rows:
        try:
            d = date.fromisoformat(str(row.get("as_of_date"))[:10])
        except ValueError:
            continue
        if week_start <= d <= week_end:
            if best_date is None or d >= best_date:
                best_date = d
                best = row.get("action_label")
    return best


def build_weekly_self_review(
    *,
    ref_date: date | None = None,
    top_n: int = DEFAULT_TOP_N,
    universe_scope: str | list[str] | None = None,
) -> dict[str, Any]:
    week_start, week_end = _calendar_week_bounds(ref_date)
    week_label = _iso_week_label(week_end)
    conn = stock_db.get_db()
    try:
        model_by_sym, runs = _collect_weekly_model_picks(
            conn, week_start, week_end, top_n=top_n, universe_scope=universe_scope,
        )
        week_end_shares = _current_holdings(conn)
        backup_snaps = _backup_snapshots_between(week_start, week_end)
        if backup_snaps:
            week_start_shares = backup_snaps[0][1]
        else:
            week_start_shares = week_end_shares

        all_syms = set(model_by_sym) | set(week_end_shares) | set(week_start_shares)
        for _, snap in backup_snaps:
            all_syms |= set(snap)
        outcomes = stock_db.fetch_pick_outcomes_for_symbols(
            list(model_by_sym.keys()), horizon="5d", conn=conn,
        )

        rows_missed: list[dict] = []
        rows_disobeyed: list[dict] = []
        rows_aligned: list[dict] = []
        rows_lucky: list[dict] = []

        # 按 (best_rank, market) 排序：让 CN/HK/US 三市场的高排名 pick 都能挤进 missed 前 15。
        # 若纯按 symbol 字母序：002463.SZ < 0763.HK < AAPL，US picks 永远被切。
        def _pick_sort_key(s: str) -> tuple[int, str, str]:
            meta = model_by_sym[s]
            return (int(meta.get("best_rank") or 999), str(meta.get("market") or ""), s)
        for sym in sorted(model_by_sym.keys(), key=_pick_sort_key):
            meta = model_by_sym[sym]
            held_end = week_end_shares.get(sym, 0) > 1e-6
            increased = _had_increase_during_week(
                sym, week_start_shares, week_end_shares, backup_snaps,
            )
            review_action = _latest_review_action_in_week(sym, week_start, week_end, conn)
            oc = outcomes.get(sym) or {}
            ret5 = oc.get("return_pct")

            if not held_end and not increased:
                rows_missed.append({
                    "category": "missed",
                    "symbol": sym,
                    "name": meta.get("name"),
                    "model_rank": meta.get("best_rank"),
                    "first_run_id": meta.get("first_run_id"),
                    "first_run_date": meta.get("first_run_date"),
                    "return_5d_pct": ret5,
                    "note": "周内进入模型 Top，但未买入/未增持",
                })
                continue

            if increased and review_action in DISOBEDIENT_ACTIONS:
                row = {
                    "category": "disobeyed",
                    "symbol": sym,
                    "name": meta.get("name"),
                    "review_action": review_action,
                    "shares_delta": _shares_delta(week_start_shares, week_end_shares, sym),
                    "return_5d_pct": ret5,
                    "note": "模型建议谨慎/减仓，但你增持了",
                }
                rows_disobeyed.append(row)
                if ret5 is not None and float(ret5) > 0:
                    rows_lucky.append({**row, "category": "lucky_disobey"})
                continue

            if held_end or increased:
                rows_aligned.append({
                    "category": "aligned",
                    "symbol": sym,
                    "name": meta.get("name"),
                    "review_action": review_action or "—",
                    "held_end": held_end,
                    "increased": increased,
                    "return_5d_pct": ret5,
                    "note": "持仓/增持与模型方向大体一致",
                })

        for sym, shares in week_end_shares.items():
            if shares <= 1e-6 or sym in model_by_sym:
                continue
            review_action = _latest_review_action_in_week(sym, week_start, week_end, conn)
            increased = _had_increase_during_week(
                sym, week_start_shares, week_end_shares, backup_snaps,
            )
            if increased and review_action in DISOBEDIENT_ACTIONS:
                rows_disobeyed.append({
                    "category": "disobeyed",
                    "symbol": sym,
                    "review_action": review_action,
                    "shares_delta": _shares_delta(week_start_shares, week_end_shares, sym),
                    "note": "非本周模型 Top，但体检反对时仍增持",
                })

        all_symbols: set[str] = set()
        for bucket in (rows_missed, rows_disobeyed, rows_aligned, rows_lucky):
            for row in bucket:
                if row.get("symbol"):
                    all_symbols.add(str(row["symbol"]))
        prices = _latest_prices_by_symbol(conn, sorted(all_symbols))
        info = _lookup_symbol_info(conn, sorted(all_symbols))
        trailing = _trailing_returns_by_symbol(conn, sorted(all_symbols), lookback_trade_days=5)

        def _apply_enrich(bucket: list[dict]) -> list[dict]:
            out: list[dict] = []
            for row in bucket:
                sym = str(row.get("symbol") or "")
                out.append(
                    _enrich_row_display(
                        row,
                        prices=prices,
                        info=info,
                        model_meta=model_by_sym.get(sym),
                        trailing=trailing,
                    )
                )
            return out

        rows_missed = _apply_enrich(rows_missed)
        rows_disobeyed = _apply_enrich(rows_disobeyed)
        rows_aligned = _apply_enrich(rows_aligned)
        rows_lucky = _apply_enrich(rows_lucky)

        summary = {
            "missed": len(rows_missed),
            "disobeyed": len(rows_disobeyed),
            "aligned": len(rows_aligned),
            "lucky_disobey": len(rows_lucky),
            "model_run_days": len({str(r.get("run_date"))[:10] for r in runs}),
            "model_pick_symbols": len(model_by_sym),
        }
        payload = {
            "week_label": week_label,
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "universe_scope": universe_scope,
            "top_n": top_n,
            "advisory_only": True,
            "summary": summary,
            "missed": rows_missed[:15],
            "disobeyed": rows_disobeyed[:15],
            "aligned": rows_aligned[:15],
            "lucky_disobey": rows_lucky[:10],
            "data_sources": {
                "recommendation_runs": "PIT per run_date",
                "holdings_delta": "state_backup + real_holdings",
                "review_verdict": "real_holding_review_items",
            },
        }
        return payload
    finally:
        conn.close()


def _markdown_report(payload: dict[str, Any]) -> str:
    s = payload.get("summary") or {}
    lines = [
        f"# 周末复盘 · {payload.get('week_label')}",
        "",
        f"区间：{payload.get('week_start')} ~ {payload.get('week_end')}  ",
        f"生成：{payload.get('generated_at')}  ",
        "**advisory only · 不构成投资建议**",
        "",
        "## 汇总",
        f"- 错过（模型 Top 未买）：**{s.get('missed', 0)}**",
        f"- 没听话（反对仍增持）：**{s.get('disobeyed', 0)}**",
        f"- 对齐：**{s.get('aligned', 0)}**",
        f"- 逆势赚钱（没听话但 5d>0）：**{s.get('lucky_disobey', 0)}**",
        "",
    ]

    def _section(title: str, rows: list[dict]) -> None:
        lines.append(f"## {title}")
        if not rows:
            lines.append("_（无）_")
            lines.append("")
            return
        lines.append("| 代码 | 名称 | 市场 | 说明 | 近N日% | 窗口 | run |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for r in rows[:10]:
            sym = r.get("symbol", "")
            name = r.get("name") or "—"
            mkt = r.get("market_label") or r.get("market") or "—"
            note = r.get("note", "")
            trail = r.get("trailing_5d_pct")
            ret = trail if trail is not None else r.get("return_5d_pct")
            ret_s = f"{float(ret):+.1f}%" if ret is not None else "—"
            lookback = r.get("trailing_lookback_days")
            window_s = f"{int(lookback)}d" if lookback else "—"
            run = r.get("first_run_id") or r.get("review_action") or "—"
            lines.append(f"| {sym} | {name} | {mkt} | {note} | {ret_s} | {window_s} | `{run}` |")
        lines.append("")

    _section("错过", payload.get("missed") or [])
    _section("没听话", payload.get("disobeyed") or [])
    _section("对齐", payload.get("aligned") or [])
    return "\n".join(lines)


def _build_feishu_card(payload: dict[str, Any]) -> dict:
    s = payload.get("summary") or {}
    week_label = payload.get("week_label", "")
    lines = [
        f"**错过** {s.get('missed', 0)} · **没听话** {s.get('disobeyed', 0)} · **对齐** {s.get('aligned', 0)}",
        "",
    ]
    for title, key in (("错过", "missed"), ("没听话", "disobeyed")):
        rows = payload.get(key) or []
        if not rows:
            continue
        lines.append(f"**{title}**")
        for r in rows[:5]:
            sym = r.get("symbol", "")
            name = (r.get("name") or "").strip()
            mkt = (r.get("market_label") or r.get("market") or "").strip()
            head = name or sym
            tag = f"{sym} · {mkt}" if mkt else sym
            trail = r.get("trailing_5d_pct")
            ret = trail if trail is not None else r.get("return_5d_pct")
            if ret is not None:
                lookback = r.get("trailing_lookback_days") if trail is not None else None
                label = f"近{int(lookback)}日" if lookback else "5d"
                ret_s = f" · {label} {float(ret):+.1f}%"
            else:
                ret_s = ""
            extra = r.get("review_action") or f"Top{r.get('model_rank', '?')}"
            lines.append(f"- **{head}** ({tag} · {extra}){ret_s}")
        lines.append("")

    content = "\n".join(lines).strip() or "本周无模型推荐样本或持仓无变化。"
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"📋 周末复盘 · {week_label}"},
                "subtitle": {"tag": "plain_text", "content": "动作 vs 信号 · advisory"},
                "template": "purple",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}},
                {"tag": "note", "elements": [{
                    "tag": "plain_text",
                    "content": "模型推=当日 recommendation_picks(run_id)；你做=state_backup 持仓 diff + 体检 verdict",
                }]},
            ],
        },
    }


def _push_feishu(card: dict) -> bool:
    webhook = (
        os.environ.get("FEISHU_WEEKLY_WEBHOOK", "").strip()
        or os.environ.get("FEISHU_BRIEF_WEBHOOK", "").strip()
        or os.environ.get("FEISHU_ALERT_WEBHOOK", "").strip()
    )
    if not webhook:
        logger.info("未配置 FEISHU_WEEKLY_WEBHOOK / FEISHU_BRIEF_WEBHOOK，跳过推送")
        return False
    try:
        r = requests.post(webhook, json=card, timeout=15)
        return r.status_code == 200 and r.json().get("StatusCode", 0) == 0
    except Exception as exc:
        logger.warning("飞书推送失败: %s", exc)
        return False


def persist_weekly_review(payload: dict[str, Any]) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LETTERS_DIR.mkdir(parents=True, exist_ok=True)
    label = payload.get("week_label", "unknown")
    json_path = OUT_DIR / f"weekly_self_review_{label}.json"
    md_path = LETTERS_DIR / f"weekly_self_review_{label}.md"
    latest = OUT_DIR / "weekly_self_review_latest.json"
    safe = _json_safe(payload)
    text = json.dumps(safe, ensure_ascii=False, indent=2)
    json_path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    md_path.write_text(_markdown_report(payload), encoding="utf-8")
    return json_path, md_path


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="周末复盘：模型推 vs 你做")
    p.add_argument("--date", default=None, help="参考日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    p.add_argument("--dry-run", action="store_true", help="只写本地文件，不推飞书")
    p.add_argument("--push", action="store_true", help="推送到飞书 webhook")
    args = p.parse_args()
    ref = date.fromisoformat(args.date[:10]) if args.date else None
    payload = build_weekly_self_review(ref_date=ref, top_n=args.top_n)
    json_path, md_path = persist_weekly_review(payload)
    logger.info("已写入 %s", json_path)
    logger.info("已写入 %s", md_path)
    pushed = False
    if args.push and not args.dry_run:
        pushed = _push_feishu(_build_feishu_card(payload))
        logger.info("飞书推送: %s", "ok" if pushed else "skip/fail")
    print(json.dumps(_json_safe({
        "week_label": payload.get("week_label"),
        "summary": payload.get("summary"),
        "json_path": str(json_path),
        "md_path": str(md_path),
        "feishu_pushed": pushed,
    }), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
