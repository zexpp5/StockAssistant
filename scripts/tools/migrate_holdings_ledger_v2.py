#!/usr/bin/env python3
"""账本 v2 迁移：把 real_holdings 旧 lot 行回填为 real_holding_trades，并聚合重建。

这是在**已有生产库**里改账本，不是离线初始化。务必按 runbook 操作：
  1. 停 launchd 常驻 API，释放 stock_history_v2.duckdb 写连接
  2. 停/跳过盘中刷新、持仓体检、早报触发链路
  3. 备份 DuckDB
  4. 先 --dry-run 看 preflight
  5. 正式 migrate
  6. 校验通过后重启 API

迁移内容：
  - 每个旧 real_holdings 行 → 一条 side=buy trade（保留锁定汇率/成本），幂等键
    migrate_v2:<old_id>，可重复执行不重复回填。
  - rebuild_real_holdings_from_trades() 按 account+market+symbol 聚合（同股多 lot 合并，
    复用该 key 最小旧 id，单 lot 票 id 不变）。
  - holding_id remap：把 real_holding_review_items / real_holding_discipline_plans /
    real_holding_discipline_events 里指向被合并旧 id 的引用，改到新聚合 id。
  - discipline 计划快照刷新：合并后的持仓刷新 shares_snapshot=remaining_shares、
    cost_basis_price=avg_cost_local_per_share（单 lot 不变的票不动），并打印 old→new。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import stock_db  # noqa: E402


def _preflight(conn) -> dict:
    legacy = conn.execute(
        "SELECT count(*) FROM real_holdings WHERE position_epoch IS NULL"
    ).fetchone()[0]
    already = conn.execute(
        "SELECT count(*) FROM real_holding_trades WHERE source = 'migration'"
    ).fetchone()[0]
    keys = conn.execute(
        "SELECT count(DISTINCT (account || '|' || market || '|' || UPPER(symbol))) "
        "FROM real_holdings WHERE position_epoch IS NULL"
    ).fetchone()[0]
    # 旧 split 脚本痕迹：legacy holdings 表是否仍有行（仅作提示，不阻断）。
    try:
        legacy_holdings = conn.execute("SELECT count(*) FROM holdings").fetchone()[0]
    except Exception:
        legacy_holdings = None
    return {
        "legacy_real_holdings_rows": int(legacy),
        "will_aggregate_into_keys": int(keys),
        "existing_migration_trades": int(already),
        "legacy_holdings_table_rows": legacy_holdings,
        "already_migrated": already > 0,
    }


def _apply_holding_id_remap(conn, old_id: int, new_id: int) -> dict:
    """只 remap 活引用（discipline plans/events）。

    real_holding_review_items 故意**不**改写：它是历史体检快照，且 PK=(review_run_id,
    holding_id)——同一次 run 里被合并的多个 lot 各有一行，强行改成同一 new_id 会撞主键。
    历史 run 保留旧 holding_id 作快照；迁移后重跑 real_holding_review 即可让最新体检
    引用合并后的新聚合 id（见 runbook）。
    """
    if old_id == new_id:
        return {}
    counts = {}
    for table in (
        "real_holding_discipline_plans",
        "real_holding_discipline_events",
    ):
        if not stock_db._table_has_column(conn, table, "holding_id"):
            continue
        n = conn.execute(
            f"SELECT count(*) FROM {table} WHERE holding_id = ?", [old_id]
        ).fetchone()[0]
        if n:
            conn.execute(
                f"UPDATE {table} SET holding_id = ? WHERE holding_id = ?", [new_id, old_id]
            )
            counts[table] = int(n)
    return counts


def _refresh_discipline_snapshots(conn, holding_id: int) -> list[dict]:
    """合并后的持仓：把绑定它的 active 纪律计划快照刷新到新聚合口径，并返回变更日志。"""
    h = stock_db.fetch_real_holding_by_id(holding_id, conn=conn)
    if not h:
        return []
    new_shares = h.get("remaining_shares")
    new_cost_local = h.get("avg_cost_local_per_share")
    logs = []
    rows = conn.execute(
        "SELECT plan_id, cost_basis_price, shares_snapshot FROM real_holding_discipline_plans "
        "WHERE holding_id = ? AND status = 'active'",
        [holding_id],
    ).fetchall()
    for plan_id, old_cost, old_shares in rows:
        conn.execute(
            "UPDATE real_holding_discipline_plans SET shares_snapshot = ?, cost_basis_price = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE plan_id = ?",
            [new_shares, new_cost_local, plan_id],
        )
        logs.append({
            "plan_id": plan_id,
            "shares_snapshot": [old_shares, new_shares],
            "cost_basis_price": [old_cost, new_cost_local],
        })
    return logs


def migrate(*, conn=None, dry_run: bool = False) -> dict:
    own = conn is None
    if own:
        conn = stock_db.get_db()
    try:
        pre = _preflight(conn)
        if dry_run:
            return {"dry_run": True, "preflight": pre}

        # 1) 回填 buy trade（幂等键 migrate_v2:<old_id>），不逐行 rebuild。
        legacy_rows = conn.execute(
            "SELECT id, account, market, symbol, name, entry_price, shares, entry_date, "
            "currency, entry_fx_rate, entry_fx_as_of, entry_fx_source, cost_rmb_locked, notes "
            "FROM real_holdings WHERE position_epoch IS NULL ORDER BY id"
        ).fetchall()
        backfilled = 0
        for r in legacy_rows:
            (old_id, account, market, symbol, name, entry_price, shares, entry_date,
             currency, fx_rate, fx_as_of, fx_source, cost_rmb_locked, notes) = r
            item = {
                "account": account or "default", "market": market, "symbol": symbol, "name": name,
                "trade_price": entry_price, "quantity": shares, "trade_date": entry_date,
                "currency": currency, "fx_rate": fx_rate, "entry_fx_as_of": fx_as_of,
                "entry_fx_source": fx_source or "migration", "notes": notes,
                "client_request_id": f"migrate_v2:{old_id}", "source": "migration",
            }
            res = stock_db.insert_real_holding_trade_raw(item, "buy", conn=conn)
            if res.get("created"):
                backfilled += 1

        # 2) 一次性 rebuild 所有 key，拿到 old->new 映射。
        remap = stock_db.rebuild_real_holdings_from_trades(conn=conn)

        # 3) holding_id remap + discipline 快照刷新。
        remap_counts, snapshot_logs = {}, []
        for key, info in remap.items():
            new_id = info["holding_id"]
            merged_from = info["merged_from"]
            if new_id is None:
                continue
            merged = False
            for old_id in merged_from:
                c = _apply_holding_id_remap(conn, int(old_id), int(new_id))
                if c:
                    remap_counts[f"{old_id}->{new_id}"] = c
                    merged = True
            if len(merged_from) > 1 or merged:
                logs = _refresh_discipline_snapshots(conn, int(new_id))
                if logs:
                    snapshot_logs.extend(logs)

        return {
            "dry_run": False,
            "preflight": pre,
            "backfilled_trades": backfilled,
            "aggregated_holdings": sum(1 for v in remap.values() if v["holding_id"] is not None),
            "holding_id_remaps": remap_counts,
            "discipline_snapshot_refreshes": snapshot_logs,
        }
    finally:
        if own:
            conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="账本 v2 迁移（回填 trade + 聚合 + holding_id remap）")
    parser.add_argument("--dry-run", action="store_true", help="只跑 preflight，不写入。")
    args = parser.parse_args()
    result = migrate(dry_run=args.dry_run)
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
