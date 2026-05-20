#!/usr/bin/env python3
"""Replay locally cached market history into the clean v2 database.

This is a recovery-only tool.

It does not call live market sources. It only replays the local artifacts that
already exist under `data/latest/` and writes them into the v2 schema with
explicit `cache_replay/...` source labels so the UI and acceptance checks can
distinguish replayed data from live fetches.

Inputs:
  - data/latest/history_data.json
  - data/latest/a_share_price_history_cache.json

Outputs:
  - system_universe / pool_membership rows for missing cache-backed CN names
  - price_daily rows for cache-backed US/HK/CN tickers
  - data/latest/source_health.json with replay metadata
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from stock_research import config  # noqa: E402


HISTORY_FILE = REPO / "data" / "latest" / "history_data.json"
A_SHARE_CACHE_FILE = REPO / "data" / "latest" / "a_share_price_history_cache.json"
SOURCE_HEALTH_FILE = REPO / "data" / "latest" / "source_health.json"


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _market_bucket(text: str | None) -> str | None:
    value = str(text or "").strip()
    if not value:
        return None
    if "港股" in value or value.upper() == "HK":
        return "HK"
    if "A股" in value or value.upper() == "CN":
        return "CN"
    if "美股" in value or value.upper() == "US":
        return "US"
    return None


def _canonical_symbol(raw_symbol: str, market: str, yf_ticker: str | None = None) -> str | None:
    code = str(yf_ticker or raw_symbol or "").strip()
    if not code:
        return None
    if "." in code:
        return code.upper()
    market = market.upper()
    if market == "HK":
        digits = "".join(c for c in code if c.isdigit())
        return f"{digits.zfill(4)}.HK" if digits else None
    if market == "CN":
        if code.isdigit() and len(code) == 6:
            if code.startswith(("00", "30", "20")):
                return f"{code}.SZ"
            if code.startswith(("8", "9")):
                return f"{code}.BJ"
            return f"{code}.SS"
        return code.upper()
    return code.upper()


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _snapshot_from_series(dates: list[Any], closes: list[Any]) -> dict[str, Any] | None:
    points: list[tuple[str, float]] = []
    for idx, close in enumerate(closes or []):
        c = _as_float(close)
        if c is None:
            continue
        dt = str(dates[idx])[:10] if idx < len(dates or []) else ""
        if not dt:
            continue
        points.append((dt, c))
    if not points:
        return None

    latest_date, latest_close = points[-1]
    prev_close = points[-2][1] if len(points) >= 2 else None

    def _pct(base: float | None) -> float | None:
        if base is None or base == 0:
            return None
        return round((latest_close - base) / base * 100.0, 2)

    year = latest_date[:4]
    year_points = [close for dt, close in points if dt[:4] == year]
    ytd_pct = _pct(year_points[0] if year_points else None)
    one_year_pct = _pct(points[0][1])
    one_week_pct = _pct(points[-5][1] if len(points) >= 5 else None)
    one_month_pct = _pct(points[-22][1] if len(points) >= 22 else None)

    return {
        "trade_date": latest_date,
        "close": round(latest_close, 4),
        "prev_close": round(prev_close, 4) if prev_close is not None else None,
        "ytd_pct": ytd_pct,
        "one_week_pct": one_week_pct,
        "one_month_pct": one_month_pct,
        "one_year_pct": one_year_pct,
    }


def _load_current_symbols(conn: duckdb.DuckDBPyConnection) -> set[str]:
    try:
        rows = conn.execute(
            "SELECT symbol FROM system_universe WHERE active = TRUE"
        ).fetchall()
        return {str(r[0]).upper() for r in rows}
    except Exception:
        return set()


def _load_history_replay(current_symbols: set[str]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    payload = _load_json(HISTORY_FILE) or {}
    tickers = payload.get("tickers") or {}
    rows: list[dict[str, Any]] = []
    cn_name_lookup: dict[str, str] = {}

    for key, item in tickers.items():
        market = _market_bucket(item.get("market"))
        if not market:
            continue
        yf_ticker = str(item.get("yf_ticker") or "").strip() or None
        symbol = _canonical_symbol(str(key), market, yf_ticker)
        if not symbol:
            continue
        name = str(item.get("name") or "").strip()
        if market == "CN":
            raw = str(key).strip().split(".")[0]
            if name:
                cn_name_lookup[raw] = name
            continue
        if current_symbols and symbol.upper() not in current_symbols:
            continue
        snap = _snapshot_from_series(item.get("ts") or [], item.get("close") or [])
        if not snap:
            continue
        rows.append({
            "market": market,
            "symbol": symbol,
            "raw_symbol": str(key).split(".")[0],
            "name": name or symbol,
            "source": "cache_replay/history_data",
            "currency": "USD" if market == "US" else ("HKD" if market == "HK" else "CNY"),
            "snapshot": snap,
        })

    return rows, cn_name_lookup


def _load_a_share_replay(cn_name_lookup: dict[str, str]) -> list[dict[str, Any]]:
    payload = _load_json(A_SHARE_CACHE_FILE) or {}
    items = payload.get("items") or {}
    rows: list[dict[str, Any]] = []
    for raw_code, item in items.items():
        raw_code = str(raw_code).strip()
        if not raw_code:
            continue
        symbol = _canonical_symbol(raw_code, "CN")
        if not symbol:
            continue
        series = item.get("rows") or []
        dates = [r.get("date") for r in series]
        closes = [r.get("close") for r in series]
        snap = _snapshot_from_series(dates, closes)
        if not snap:
            continue
        rows.append({
            "market": "CN",
            "symbol": symbol,
            "raw_symbol": raw_code,
            "name": cn_name_lookup.get(raw_code, symbol),
            "source": "cache_replay/a_share_price_history",
            "currency": "CNY",
            "snapshot": snap,
        })
    return rows


def _ensure_universe_rows(
    conn: duckdb.DuckDBPyConnection,
    rows: list[dict[str, Any]],
    *,
    pool_id: str = "system_tech_universe",
) -> int:
    existing = {
        (str(pool or ""), str(market or "").upper(), str(symbol or "").upper())
        for pool, market, symbol in conn.execute(
            "SELECT pool_id, market, symbol FROM system_universe"
        ).fetchall()
    }
    inserted = 0
    now = datetime.now()
    for row in rows:
        key = (pool_id, row["market"], row["symbol"])
        if key in existing:
            continue
        conn.execute(
            """
            INSERT INTO system_universe (
                pool_id, pool_name, market, symbol, raw_symbol, name,
                theme, industry, source, active, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?, ?)
            """,
            [
                pool_id,
                "系统科技/AI 股票池",
                row["market"],
                row["symbol"],
                row.get("raw_symbol"),
                row.get("name"),
                "科技/AI",
                "科技",
                row["source"],
                now,
                now,
            ],
        )
        conn.execute(
            """
            INSERT INTO pool_membership (
                pool_id, market, symbol, pool_type, source, active, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, 'system_tech_universe', ?, TRUE, ?, ?)
            """,
            [
                pool_id,
                row["market"],
                row["symbol"],
                row["source"],
                now,
                now,
            ],
        )
        inserted += 1
    return inserted


def _upsert_price_daily(conn: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> int:
    now = datetime.now()
    inserted = 0
    for row in rows:
        snap = row["snapshot"]
        trade_date = str(snap["trade_date"])
        conn.execute(
            "DELETE FROM price_daily WHERE market=? AND symbol=? AND trade_date=? AND interval='1d'",
            [row["market"], row["symbol"], trade_date],
        )
        conn.execute(
            """
            INSERT INTO price_daily (
                market, symbol, trade_date, interval, close, prev_close, currency,
                market_cap, forward_pe, trailing_pe, peg_ratio, ytd_pct,
                one_week_pct, one_month_pct, one_year_pct, source,
                source_updated_at, fetched_at
            ) VALUES (?, ?, ?, '1d', ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row["market"],
                row["symbol"],
                trade_date,
                snap["close"],
                snap["prev_close"],
                row["currency"],
                snap["ytd_pct"],
                snap["one_week_pct"],
                snap["one_month_pct"],
                snap["one_year_pct"],
                row["source"],
                datetime.fromisoformat(trade_date),
                now,
            ],
        )
        inserted += 1
    return inserted


def _write_source_health(total: int, success: int, fail: int, markets: dict[str, dict[str, int]]) -> None:
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pipeline": "price_daily",
        "source": "cache_replay",
        "replay_mode": True,
        "markets": markets,
        "sources": {
            "yfinance": {
                "status": "source_degraded",
                "reason": "live sources are still unavailable; local cache replay was used",
                "affected_fields": ["live freshness", "point_in_time discovery", "new symbols"],
                "unaffected_fields": ["system_universe", "pool_membership", "price_daily", "recommendation_runs"],
                "impact": "This run restores readable and testable v2 data, but it is not a live market pull.",
                "operator_action": "Restore live market discovery/fetch, then rerun the live price pipeline.",
            },
            "cache_replay": {
                "status": "source_replayed",
                "reason": f"replayed {success}/{total} cached rows into v2",
                "affected_fields": ["history-based price_daily", "recommendation_picks"],
                "unaffected_fields": ["live source health diagnosis"],
                "impact": "Data is usable for verification, dashboard rendering, and recommendation generation, but source is local replay.",
                "operator_action": "Use only as recovery mode until live sources recover.",
            },
        },
        "summary": {
            "total_count": total,
            "success_count": success,
            "fail_count": fail,
        },
    }
    SOURCE_HEALTH_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay locally cached market data into v2 DuckDB.")
    parser.add_argument("--db", default=os.environ.get("STOCK_DB_PATH") or str(config.DUCKDB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    conn = duckdb.connect(str(db_path))
    try:
        if "price_daily" not in {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}:
            raise RuntimeError("当前数据库不是 v2 schema，缺少 price_daily")

        current_symbols = _load_current_symbols(conn)
        history_rows, cn_name_lookup = _load_history_replay(current_symbols)
        a_share_rows = _load_a_share_replay(cn_name_lookup)
        replay_rows = history_rows + a_share_rows

        # Deduplicate by (market, symbol); later rows win, so a-share cache can refine CN names.
        dedup: dict[tuple[str, str], dict[str, Any]] = {}
        for row in replay_rows:
            dedup[(row["market"], row["symbol"])] = row

        final_rows = list(dedup.values())
        markets: dict[str, dict[str, int]] = {}
        for row in final_rows:
            bucket = markets.setdefault(row["market"], {"total": 0, "success": 0, "fail": 0})
            bucket["total"] += 1

        if args.dry_run:
            print(json.dumps({
                "db_path": str(db_path),
                "current_symbols": len(current_symbols),
                "universe_seed_rows": sum(1 for r in final_rows if r["market"] == "CN"),
                "price_rows": len(final_rows),
                "markets": markets,
                "dry_run": True,
            }, ensure_ascii=False, indent=2))
            return 0

        universe_seeded = _ensure_universe_rows(conn, [r for r in final_rows if r["market"] == "CN"])
        price_written = _upsert_price_daily(conn, final_rows)
        conn.close()
        conn = None

        for row in final_rows:
            markets[row["market"]]["success"] += 1

        _write_source_health(len(final_rows), price_written, 0, markets)
        print(
            f"cache replay complete: universe_seeded={universe_seeded} "
            f"price_daily={price_written} db={db_path}"
        )
        print(f"source health written: {SOURCE_HEALTH_FILE}")
        return 0
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
