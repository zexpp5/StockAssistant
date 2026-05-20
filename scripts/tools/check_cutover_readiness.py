#!/usr/bin/env python3
"""Check whether code is ready for a clean v2 DB cutover.

This is a static guardrail. It does not prove the app is fully migrated, but it
surfaces risky hardcoded reads that could accidentally pull old DuckDB or
data/latest artifacts into the new production line.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]

SCAN_DIRS = ["scripts", "stock_research"]
IGNORE_PATH_PARTS = {
    "__pycache__",
    ".pyc",
}

DB_ALLOWLIST = {
    "scripts/lib/stock_db.py",
    "stock_research/config.py",
    "scripts/tools/init_stock_db_v2.py",
    "scripts/tools/current_state_report.py",
    "scripts/tools/check_cutover_readiness.py",
}

LATEST_ALLOWLIST = {
    "scripts/tools/current_state_report.py",
    "scripts/tools/check_cutover_readiness.py",
}

DB_PATTERN = re.compile(r"stock_history\.duckdb")
LATEST_PATTERN = re.compile(r"data/latest|latest/.*\.json")


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for dirname in SCAN_DIRS:
        root = REPO / dirname
        if not root.exists():
            continue
        for path in root.rglob("*"):
            rel = path.relative_to(REPO).as_posix()
            if not path.is_file():
                continue
            if any(part in rel for part in IGNORE_PATH_PARTS):
                continue
            if path.suffix not in {".py", ".sh"}:
                continue
            files.append(path)
    return sorted(files)


def _scan(pattern: re.Pattern[str], allowlist: set[str]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for path in _iter_files():
        rel = path.relative_to(REPO).as_posix()
        if rel in allowlist:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for lineno, line in enumerate(lines, start=1):
            if pattern.search(line):
                issues.append({"path": rel, "line": lineno, "text": line.strip()[:220]})
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Check v2 cutover readiness.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    parser.add_argument("--fail-on-risk", action="store_true", help="Exit non-zero when risks are found.")
    args = parser.parse_args()

    hardcoded_db = _scan(DB_PATTERN, DB_ALLOWLIST)
    latest_reads = _scan(LATEST_PATTERN, LATEST_ALLOWLIST)
    payload = {
        "hardcoded_old_db_refs": hardcoded_db,
        "latest_artifact_refs": latest_reads,
        "summary": {
            "hardcoded_old_db_refs": len(hardcoded_db),
            "latest_artifact_refs": len(latest_reads),
        },
        "notes": [
            "Hardcoded stock_history.duckdb references should be replaced by STOCK_DB_PATH/config.DUCKDB_PATH before cutover.",
            "data/latest references may be valid during transition, but must not be used as fallback for v2 production truth.",
        ],
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("Cutover readiness scan")
        print(f"  hardcoded old DB refs: {len(hardcoded_db)}")
        for item in hardcoded_db[:20]:
            print(f"    {item['path']}:{item['line']} {item['text']}")
        if len(hardcoded_db) > 20:
            print(f"    ... {len(hardcoded_db) - 20} more")
        print(f"  data/latest refs: {len(latest_reads)}")
        for item in latest_reads[:20]:
            print(f"    {item['path']}:{item['line']} {item['text']}")
        if len(latest_reads) > 20:
            print(f"    ... {len(latest_reads) - 20} more")

    if args.fail_on_risk and (hardcoded_db or latest_reads):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
