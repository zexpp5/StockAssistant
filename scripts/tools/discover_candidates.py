#!/usr/bin/env python3
"""Build a wider, auditable system universe for V2.

This module is intentionally conservative.  It widens the US AI/tech universe
from the core hand-audited list with ETF-theme holdings that already exist in
DuckDB, but it does not create buy recommendations and never writes the manual
watchlist or holdings tables.

Output shape matches the universe helpers used by init_stock_db_v2.py:
  {ticker, raw_ticker, name, sector, location, source, etfs}
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_research import config  # noqa: E402
from stock_research.core.us_universe import fetch_us_ai_tech_universe  # noqa: E402


LOCATION_US = "United States"
POOL_SOURCE_PREFIX = "etf_theme"

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,6}$")

# ETFs whose holdings are mostly US-listed or US-traded names.  A no-suffix
# ticker from these is a safer universe candidate than a no-suffix ticker from
# global resource ETFs, where local tickers can look like US symbols.
_US_DOMESTIC_ETFS = {
    "AIQ",
    "HACK",
    "PAVE",
    "SKYY",
    "SOXX",
}

# Global/thematic ETFs can still contain valid US-listed names.  Keep this
# allowlist explicit to avoid mapping foreign local symbols to unrelated US
# tickers (for example AMG Critical Materials N.V. -> US AMG).
_GLOBAL_SAFE_US_TICKERS = {
    "ALB",
    "AII",
    "BE",
    "CCJ",
    "CEG",
    "CW",
    "CWEN",
    "D",
    "DUK",
    "ENPH",
    "FSLR",
    "LAC",
    "LEU",
    "NXT",
    "OKLO",
    "PCG",
    "PLUG",
    "RIO",
    "SEDG",
    "SOLS",
    "SQM",
    "TLN",
    "UEC",
    "VST",
}

_KNOWN_FOREIGN_LOCAL_TICKERS = {
    "AMG",
    "CEZ",
    "ELE",
    "FORTUM",
}

_FOREIGN_LOCAL_NAME_HINTS = (
    " a. s.",
    " s.a.",
    " oyj",
)


def _clean_symbol(raw: Any) -> str:
    symbol = str(raw or "").strip().upper()
    # ETF feeds use a space suffix for non-US markets ("6954 JP", "0700 HK").
    # We only accept no-space US-style tickers here.
    if " " in symbol:
        return ""
    return symbol if _TICKER_RE.match(symbol) else ""


def _norm_skip_codes(skip_codes: set[str] | None) -> set[str]:
    out: set[str] = set()
    for code in skip_codes or set():
        c = str(code or "").strip().upper()
        if not c:
            continue
        out.add(c)
        out.add(c.split(".")[0])
    return out


def _is_likely_us_listed_candidate(
    *,
    symbol: str,
    company_name: str,
    etf_ticker: str,
    market_inferred: str | None,
) -> bool:
    """Return True only when the ETF holding is safe to treat as a US ticker."""
    if not symbol or symbol in _KNOWN_FOREIGN_LOCAL_TICKERS:
        return False
    if market_inferred and str(market_inferred).upper() not in {"US", "USA"}:
        return False

    etf = str(etf_ticker or "").upper()
    if etf in _US_DOMESTIC_ETFS:
        return True
    if symbol in _GLOBAL_SAFE_US_TICKERS:
        return True

    name = f" {company_name or ''} ".lower()
    if any(hint in name for hint in _FOREIGN_LOCAL_NAME_HINTS):
        return False
    if " adr" in name or "spon adr" in name:
        return True
    if any(hint in name for hint in (" inc", " corp", " corporation", " holdings", " technologies")):
        return True
    return False


def _theme_from_etfs(etfs: list[str], theme_lookup: dict[str, str]) -> str:
    labels = [theme_lookup.get(e) for e in etfs if theme_lookup.get(e)]
    if labels:
        return " / ".join(dict.fromkeys(labels))
    if etfs:
        return f"ETF theme: {','.join(etfs[:3])}"
    return "AI / tech"


def _source_for_etfs(etfs: list[str]) -> str:
    if not etfs:
        return "us_ai_core"
    return f"{POOL_SOURCE_PREFIX}:{','.join(etfs[:4])}"


def _load_core_us(skip: set[str]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for item in fetch_us_ai_tech_universe():
        symbol = _clean_symbol(item.get("ticker"))
        if not symbol or symbol in skip:
            continue
        rows[symbol] = {
            "ticker": symbol,
            "raw_ticker": symbol,
            "name": item.get("name") or symbol,
            "sector": item.get("sector") or item.get("theme") or "AI / tech",
            "industry": item.get("industry") or item.get("sector") or "AI / tech",
            "location": LOCATION_US,
            "source": item.get("source") or "us_ai_core",
            "etfs": [],
            "discovery_reason": "core_us_ai_tech_universe",
        }
    return rows


def _connect_default_readonly() -> duckdb.DuckDBPyConnection | None:
    db_path = Path(config.DUCKDB_PATH)
    if not db_path.exists():
        return None
    try:
        return duckdb.connect(str(db_path), read_only=True)
    except Exception:
        return None


def _table_exists(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    try:
        return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]).fetchone())
    except Exception:
        return False


def _load_etf_candidates(
    conn: duckdb.DuckDBPyConnection | None,
    skip: set[str],
) -> dict[str, dict[str, Any]]:
    if conn is None:
        return {}
    if not _table_exists(conn, "ai_theme_etf_holdings"):
        return {}

    theme_lookup: dict[str, str] = {}
    if _table_exists(conn, "ai_theme_etf_universe"):
        for etf, label, theme_id in conn.execute(
            """
            SELECT etf_ticker, theme_label, theme_id
            FROM ai_theme_etf_universe
            WHERE COALESCE(active, TRUE) = TRUE
            """
        ).fetchall():
            theme_lookup[str(etf).upper()] = str(theme_id or label or etf)

    rows = conn.execute(
        """
        SELECT h.etf_ticker, h.raw_ticker, h.company_name, h.weight,
               h.market_inferred, h.universe_match
        FROM ai_theme_etf_holdings h
        ORDER BY h.etf_ticker, h.rank
        """
    ).fetchall()

    by_symbol: dict[str, dict[str, Any]] = {}
    etfs_by_symbol: dict[str, list[str]] = defaultdict(list)
    weight_by_symbol: dict[str, float] = defaultdict(float)

    for etf, raw_ticker, company_name, weight, market_inferred, universe_match in rows:
        symbol = _clean_symbol(universe_match or raw_ticker)
        if not symbol or symbol in skip:
            continue
        etf_s = str(etf or "").upper()
        name = str(company_name or symbol).strip()
        if not _is_likely_us_listed_candidate(
            symbol=symbol,
            company_name=name,
            etf_ticker=etf_s,
            market_inferred=str(market_inferred or "US"),
        ):
            continue

        if symbol not in by_symbol:
            by_symbol[symbol] = {
                "ticker": symbol,
                "raw_ticker": symbol,
                "name": name or symbol,
                "location": LOCATION_US,
            }
        if etf_s and etf_s not in etfs_by_symbol[symbol]:
            etfs_by_symbol[symbol].append(etf_s)
        try:
            weight_by_symbol[symbol] += float(weight or 0.0)
        except Exception:
            pass

    for symbol, item in by_symbol.items():
        etfs = sorted(etfs_by_symbol.get(symbol) or [])
        theme = _theme_from_etfs(etfs, theme_lookup)
        item.update({
            "sector": theme,
            "industry": theme,
            "source": _source_for_etfs(etfs),
            "etfs": etfs,
            "etf_weight_sum": round(weight_by_symbol.get(symbol, 0.0), 4),
            "discovery_reason": "theme_etf_holdings",
        })
    return by_symbol


def _load_snapshot_candidates(skip: set[str]) -> dict[str, dict[str, Any]]:
    """Load a small checked-in historical discovery sample if present.

    This is only an additive seed for old high-signal names.  It is not a
    substitute for live ETF holdings or price validation.
    """
    path = REPO / "scripts" / "tools" / "data" / "discovery_candidates.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    generated_at = str(payload.get("generated_at") or "")
    out: dict[str, dict[str, Any]] = {}
    for c in payload.get("candidates") or []:
        if str(c.get("location") or "").lower() != "united states":
            continue
        symbol = _clean_symbol(c.get("ticker"))
        if not symbol or symbol in skip:
            continue
        sector = str(c.get("sector") or "ETF discovery sample")
        out[symbol] = {
            "ticker": symbol,
            "raw_ticker": symbol,
            "name": c.get("name") or symbol,
            "sector": sector,
            "industry": sector,
            "location": LOCATION_US,
            "source": "offline_discovery_snapshot",
            "etfs": list(c.get("etfs") or []),
            "discovery_reason": f"checked_in_discovery_sample:{generated_at}",
        }
    return out


def build_universe(
    skip_codes: set[str] | None = None,
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
    include_core: bool = True,
    include_etf: bool = True,
    include_snapshot: bool = True,
) -> list[dict[str, Any]]:
    """Return a deduplicated US AI/tech universe.

    Args:
      skip_codes: tickers to exclude; both full and raw symbols are honored.
      conn: optional DuckDB connection.  Passing the caller connection avoids
        reading production DB while initializing a temporary DB.
      include_core: include stock_research.core.us_universe.
      include_etf: include current ai_theme_etf_holdings candidates.
      include_snapshot: include the checked-in discovery sample.
    """
    skip = _norm_skip_codes(skip_codes)
    merged: dict[str, dict[str, Any]] = {}

    if include_core:
        merged.update(_load_core_us(skip))

    own_conn = None
    if include_etf and conn is None:
        own_conn = _connect_default_readonly()
        conn = own_conn
    try:
        if include_etf:
            for symbol, item in _load_etf_candidates(conn, skip).items():
                if symbol in merged:
                    existing = merged[symbol]
                    etfs = sorted(set(existing.get("etfs") or []) | set(item.get("etfs") or []))
                    existing["etfs"] = etfs
                    if etfs:
                        existing["source"] = f"{existing.get('source')};{_source_for_etfs(etfs)}"
                    existing["etf_weight_sum"] = item.get("etf_weight_sum")
                    existing["discovery_reason"] = f"{existing.get('discovery_reason')};theme_etf_holdings"
                else:
                    merged[symbol] = item
    finally:
        if own_conn is not None:
            own_conn.close()

    if include_snapshot:
        for symbol, item in _load_snapshot_candidates(skip).items():
            merged.setdefault(symbol, item)

    return sorted(merged.values(), key=lambda r: (str(r.get("location") or ""), str(r.get("ticker") or "")))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the widened US AI/tech discovery universe.")
    parser.add_argument("--no-core", action="store_true")
    parser.add_argument("--no-etf", action="store_true")
    parser.add_argument("--no-snapshot", action="store_true")
    parser.add_argument("--out", default="", help="Optional JSON output path.")
    args = parser.parse_args()

    rows = build_universe(
        include_core=not args.no_core,
        include_etf=not args.no_etf,
        include_snapshot=not args.no_snapshot,
    )
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "universe_size": len(rows),
        "method": "core_us_ai_tech_universe + conservative theme ETF holdings + optional checked-in sample",
        "candidates": rows,
    }
    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = REPO / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ("generated_at", "universe_size", "method")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
