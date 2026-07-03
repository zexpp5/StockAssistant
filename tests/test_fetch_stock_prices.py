from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "scripts" / "pipeline" / "fetch_stock_prices.py"
spec = importlib.util.spec_from_file_location("fetch_stock_prices", MODULE_PATH)
fetch_stock_prices = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(fetch_stock_prices)


def test_fetch_price_data_accepts_cached_numeric_strings():
    hist = pd.DataFrame(
        {"Close": [100.0, 110.0]},
        index=pd.to_datetime(["2026-06-25", "2026-06-26"]),
    )

    data = fetch_stock_prices.fetch_price_data(
        "TEST",
        hist=hist,
        info_fields={
            "price": "111.0",
            "prev_close": "109.5",
            "currency": "USD",
            "market_cap": "1,234,567,890",
            "forward_pe": "12.345",
            "trailing_pe": "22.226",
            "peg_ratio": "1.234",
            "earnings_growth": "0.12",
            "revenue_growth": "11.5%",
        },
    )

    assert data is not None
    assert data["price"] == 110.0
    assert data["market_cap"] == 1234567890.0
    assert data["forward_pe"] == 12.35
    assert data["trailing_pe"] == 22.23
    assert data["peg_ratio"] == 1.23
    assert data["earnings_growth_pct"] == 12.0
    assert data["revenue_growth_pct"] == 11.5


def test_fetch_price_data_ignores_invalid_cached_numeric_strings():
    hist = pd.DataFrame(
        {"Close": [100.0, 110.0]},
        index=pd.to_datetime(["2026-06-25", "2026-06-26"]),
    )

    data = fetch_stock_prices.fetch_price_data(
        "TEST",
        hist=hist,
        info_fields={
            "currency": "USD",
            "forward_pe": "N/A",
            "trailing_pe": "--",
            "peg_ratio": "nan",
            "earnings_growth": "bad",
            "revenue_growth": None,
        },
    )

    assert data is not None
    assert data["forward_pe"] is None
    assert data["trailing_pe"] is None
    assert data["peg_ratio"] is None
    assert data["earnings_growth_pct"] is None
    assert data["revenue_growth_pct"] is None


def test_duckdb_lock_errors_are_detected():
    assert fetch_stock_prices._is_duckdb_lock_error(Exception("Conflicting lock is held"))
    assert fetch_stock_prices._is_duckdb_lock_error(Exception("Could not set lock on file"))
    assert not fetch_stock_prices._is_duckdb_lock_error(Exception("type str doesn't define __round__ method"))
