"""账本 v2 迁移测试：回填 buy trade + 聚合合并 + holding_id remap（含 discipline 表）+ 幂等。

对齐 docs/2026-06-02_卖出记录测试用例.md TC-MIG-001/002/003/004/005 + discipline 耦合。
不触碰生产库，全部在临时库构造旧数据。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "scripts" / "tools"))

import stock_db  # type: ignore
import migrate_holdings_ledger_v2 as mig  # type: ignore


def _legacy_lot(conn, *, account="default", market, symbol, name, price, shares, date, currency, fx):
    """直接插一条旧式 real_holdings 行（position_epoch 为 NULL = 未迁移）。"""
    new_id = int(conn.execute("SELECT nextval('real_holdings_id_seq')").fetchone()[0])
    conn.execute(
        "INSERT INTO real_holdings (id, account, market, symbol, name, entry_price, shares, "
        "entry_date, currency, entry_fx_rate, cost_rmb_locked, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP)",
        [new_id, account, market, symbol, name, price, shares, date, currency, fx, price * shares * fx],
    )
    return new_id


class LedgerMigrationTest(unittest.TestCase):
    def _db(self):
        d = tempfile.mkdtemp()
        return stock_db.get_db(str(Path(d) / "mig.duckdb"))

    def test_two_fx_lots_merge_and_remap_discipline(self):
        conn = self._db()
        try:
            # 旧数据：9992.HK 两 lot 不同锁定汇率（= 生产里泡泡玛特的形态）
            id1 = _legacy_lot(conn, market="HK", symbol="9992.HK", name="泡泡玛特",
                              price=100, shares=100, date="2026-06-01", currency="HKD", fx=0.90)
            id2 = _legacy_lot(conn, market="HK", symbol="9992.HK", name="泡泡玛特",
                              price=120, shares=100, date="2026-06-02", currency="HKD", fx=0.92)
            # 一条体检记录 + 一条纪律计划绑在“将被合并掉”的 id2 上
            conn.execute(
                "INSERT INTO real_holding_review_items (review_run_id, holding_id, symbol) VALUES (?,?,?)",
                ["run-1", id2, "9992.HK"])
            conn.execute(
                "INSERT INTO real_holding_discipline_plans (plan_id, holding_id, market, symbol, "
                "cost_basis_price, shares_snapshot) VALUES (?,?,?,?,?,?)",
                ["plan-x", id2, "HK", "9992.HK", 120, 100])
            conn.execute(
                "INSERT INTO real_holding_discipline_events (event_id, plan_id, trigger_id, "
                "holding_id, market, symbol) VALUES (?,?,?,?,?,?)",
                ["evt-1", "plan-x", "trg-1", id2, "HK", "9992.HK"])

            res = mig.migrate(conn=conn)

            # 回填 2 笔 buy
            self.assertEqual(res["backfilled_trades"], 2)
            # 合并成 1 行，复用最小 id（id1）
            holds = stock_db.fetch_all_real_holdings(conn=conn)
            self.assertEqual(len(holds), 1)
            h = holds[0]
            self.assertEqual(h["id"], min(id1, id2))
            self.assertEqual(h["remaining_shares"], 200)
            self.assertAlmostEqual(h["remaining_cost_rmb"], 20040)        # 9000 + 11040
            self.assertAlmostEqual(h["avg_cost_local_per_share"], 110)
            self.assertAlmostEqual(h["avg_cost_rmb_per_share"], 100.20)

            # id2 的引用全部 remap 到 id1
            for table in ("real_holding_review_items", "real_holding_discipline_plans",
                          "real_holding_discipline_events"):
                left = conn.execute(
                    f"SELECT count(*) FROM {table} WHERE holding_id = ?", [id2]).fetchone()[0]
                self.assertEqual(left, 0, f"{table} still points at merged-away id2")
                moved = conn.execute(
                    f"SELECT count(*) FROM {table} WHERE holding_id = ?", [id1]).fetchone()[0]
                self.assertEqual(moved, 1, f"{table} not remapped to id1")

            # 纪律计划快照刷新到合并后口径
            plan = conn.execute(
                "SELECT shares_snapshot, cost_basis_price FROM real_holding_discipline_plans "
                "WHERE plan_id = 'plan-x'").fetchone()
            self.assertEqual(plan[0], 200)               # remaining_shares
            self.assertAlmostEqual(plan[1], 110)         # avg_cost_local
        finally:
            conn.close()

    def test_single_lot_keeps_id_no_remap(self):
        conn = self._db()
        try:
            # 单 lot MRVL（= 生产里 holding_id=7 的形态），其纪律计划应原地不动
            mid = _legacy_lot(conn, market="US", symbol="MRVL", name=None,
                              price=272.54, shares=34, date="2026-06-02", currency="USD", fx=7.0)
            conn.execute(
                "INSERT INTO real_holding_discipline_plans (plan_id, holding_id, market, symbol, "
                "cost_basis_price, shares_snapshot) VALUES (?,?,?,?,?,?)",
                ["disc-mrvl", mid, "US", "MRVL", 272.54, 34])

            res = mig.migrate(conn=conn)

            self.assertEqual(res["holding_id_remaps"], {})   # 单 lot 无 remap
            h = [x for x in stock_db.fetch_all_real_holdings(conn=conn) if x["symbol"] == "MRVL"][0]
            self.assertEqual(h["id"], mid)                   # id 不变
            self.assertEqual(h["remaining_shares"], 34)
            plan = conn.execute(
                "SELECT holding_id, shares_snapshot FROM real_holding_discipline_plans "
                "WHERE plan_id = 'disc-mrvl'").fetchone()
            self.assertEqual(plan[0], mid)                   # 计划仍指向同一 id，未孤立
        finally:
            conn.close()

    def test_different_accounts_not_merged(self):
        conn = self._db()
        try:
            _legacy_lot(conn, account="default", market="US", symbol="MCD", name=None,
                        price=10, shares=100, date="2026-06-01", currency="USD", fx=7.0)
            _legacy_lot(conn, account="ira", market="US", symbol="MCD", name=None,
                        price=12, shares=50, date="2026-06-01", currency="USD", fx=7.0)
            mig.migrate(conn=conn)
            holds = [h for h in stock_db.fetch_all_real_holdings(conn=conn) if h["symbol"] == "MCD"]
            self.assertEqual(len(holds), 2)                  # 不同账户不合并
        finally:
            conn.close()

    def test_migration_idempotent(self):
        conn = self._db()
        try:
            _legacy_lot(conn, market="HK", symbol="9992.HK", name="泡泡玛特",
                        price=100, shares=100, date="2026-06-01", currency="HKD", fx=0.90)
            _legacy_lot(conn, market="HK", symbol="9992.HK", name="泡泡玛特",
                        price=120, shares=100, date="2026-06-02", currency="HKD", fx=0.92)
            first = mig.migrate(conn=conn)
            self.assertEqual(first["backfilled_trades"], 2)
            # 第二次：preflight 标记已迁移，且回填 0（幂等键挡住），持仓不翻倍
            second = mig.migrate(conn=conn)
            self.assertTrue(second["preflight"]["already_migrated"])
            self.assertEqual(second["backfilled_trades"], 0)
            n_trades = conn.execute(
                "SELECT count(*) FROM real_holding_trades WHERE source='migration'").fetchone()[0]
            self.assertEqual(n_trades, 2)
            h = [x for x in stock_db.fetch_all_real_holdings(conn=conn) if x["symbol"] == "9992.HK"][0]
            self.assertEqual(h["remaining_shares"], 200)     # 不翻倍
        finally:
            conn.close()

    def test_dry_run_writes_nothing(self):
        conn = self._db()
        try:
            _legacy_lot(conn, market="US", symbol="MCD", name=None,
                        price=10, shares=100, date="2026-06-01", currency="USD", fx=7.0)
            res = mig.migrate(conn=conn, dry_run=True)
            self.assertTrue(res["dry_run"])
            self.assertEqual(res["preflight"]["legacy_real_holdings_rows"], 1)
            n_trades = conn.execute("SELECT count(*) FROM real_holding_trades").fetchone()[0]
            self.assertEqual(n_trades, 0)                    # dry-run 不写
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
