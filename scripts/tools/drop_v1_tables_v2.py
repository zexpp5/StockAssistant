#!/usr/bin/env python3
"""V1 表 DROP 守卫 — 每日 pipeline 调用，防止 legacy CREATE TABLE 把 V1 表带回 V2 库。

背景：fb6ac0b（2026-05-21）一次性 DROP 了 7 张 V1 表（prices/picks/reviews/
watchlist/discovery_history/discovery_tracking/earnings_history）。但 V2 产品
基线 §七 要求"旧表不得迁入新库"是常态约束 — 单次 DROP 不等于约束生效，
任何残留的 legacy 写路径（CREATE TABLE IF NOT EXISTS / autocreate）下次跑批
又会把这些表偷偷建出来。本脚本作为 daily_refresh.sh 早班第一步，强制清空。

排除 snapshots：产品基线 §七列其为 V1，但生产代码（adapters/store.py、
build_stock_dashboard_html.py、api/main.py）仍在用作 pipeline 归档表。
属于"待重构 V1"，单独处理，不在守卫范围。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("STOCK_DB_PATH") or REPO / "stock_history_v2.duckdb")

V1_TABLES_TO_DROP = [
    "prices",
    "picks",
    "reviews",
    "watchlist",
    "discovery_history",
    "discovery_tracking",
    "earnings_history",
]


def _connect_with_retry(path: str, *, read_only: bool = False,
                        retries: int = 10, delay: float = 3.0):
    """重试连库 — DuckDB 同进程间互斥；API server (launchd 常驻) 偶尔占锁。"""
    last_err = None
    for i in range(retries):
        try:
            return duckdb.connect(path, read_only=read_only)
        except duckdb.IOException as e:
            last_err = e
            if i < retries - 1:
                time.sleep(delay)
    raise last_err  # type: ignore[misc]


def main() -> int:
    if not DB_PATH.exists():
        print(f"[drop_v1_tables_v2] db not found: {DB_PATH}", file=sys.stderr)
        return 0

    try:
        con = _connect_with_retry(str(DB_PATH))
    except duckdb.IOException as e:
        # 写锁拿不到 — 退到 read-only 只做检测，不阻塞 pipeline
        print(f"[drop_v1_tables_v2] WARN: cannot acquire write lock after retries ({e}); fallback read-only check", file=sys.stderr)
        try:
            ro_con = _connect_with_retry(str(DB_PATH), read_only=True, retries=3, delay=2.0)
        except duckdb.IOException as e2:
            print(f"[drop_v1_tables_v2] WARN: even read-only connect failed: {e2}; skip", file=sys.stderr)
            return 0
        try:
            existing = {r[0] for r in ro_con.execute("SHOW TABLES").fetchall()}
            leaked = [t for t in V1_TABLES_TO_DROP if t in existing]
            if leaked:
                print(f"[drop_v1_tables_v2] WARN: V1 tables present but cannot drop (lock held): {leaked}", file=sys.stderr)
            else:
                print(f"[drop_v1_tables_v2] OK (read-only) — 0 V1 表残留")
            return 0
        finally:
            ro_con.close()

    try:
        existing = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        leaked = [t for t in V1_TABLES_TO_DROP if t in existing]
        if not leaked:
            print(f"[drop_v1_tables_v2] OK — V1 守卫通过，0 张 V1 表残留（{DB_PATH.name}）")
            return 0
        for t in leaked:
            row_count = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            con.execute(f'DROP TABLE IF EXISTS "{t}"')
            print(f"[drop_v1_tables_v2] DROP {t} (rows={row_count}) — V1 表被 legacy 代码偷建回来")
        print(f"[drop_v1_tables_v2] cleaned {len(leaked)} V1 tables: {leaked}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
