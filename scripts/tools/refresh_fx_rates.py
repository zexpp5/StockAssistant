#!/usr/bin/env python3
"""Refresh the repo-wide FX cache used by dashboard/API/pipeline jobs."""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "scripts", "lib"))

import fx_rates  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh currency-to-RMB rates")
    parser.add_argument("--timeout-sec", type=float, default=6.0)
    parser.add_argument("--no-write", action="store_true", help="Fetch but do not update data/latest/fx_rates.json")
    parser.add_argument("--json", action="store_true", help="Print full JSON payload")
    args = parser.parse_args()

    payload = fx_rates.refresh_fx_rates(timeout_sec=args.timeout_sec, write_cache=not args.no_write)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        rates = payload.get("rates") or {}
        print(
            "[fx_rates] "
            f"status={payload.get('status')} source={payload.get('source')} as_of={payload.get('as_of')} "
            f"USD={rates.get('USD')} HKD={rates.get('HKD')} CNY={rates.get('CNY')}"
        )
        errors = payload.get("errors") or []
        if errors:
            print(f"[fx_rates] fallback/errors={len(errors)}")
            for err in errors[:3]:
                print(f"  - {err}")
    # Source failures are non-fatal: the cache contains fallback rates and the
    # rest of the daily pipeline can continue with a single consistent table.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

