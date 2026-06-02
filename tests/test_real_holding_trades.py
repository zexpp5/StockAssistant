"""账本 v2 交易流水回归测试：对齐 docs/2026-06-02_卖出记录测试用例.md 的 P0 用例。

覆盖：加仓加权平均、部分卖出、买入手续费进成本基、持仓轮次、亏损卖出、
部分卖出后加仓、乱序补录按 trade_date 回放、同日 executed_at 排序、
原币/RMB 双口径、realized 锁定 fx vs unrealized 当前 fx、撤销最近一笔、幂等键。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import stock_db  # type: ignore


def _db():
    d = tempfile.mkdtemp()
    return stock_db.get_db(str(Path(d) / "ledger.duckdb"))


def _buy(conn, **kw):
    kw.setdefault("market", "US")
    kw.setdefault("fx_rate", 7.0)
    return stock_db.insert_real_holding_buy(kw, conn=conn)


def _sell(conn, **kw):
    kw.setdefault("market", "US")
    kw.setdefault("fx_rate", 7.0)
    return stock_db.insert_real_holding_sell(kw, conn=conn)


def _holding(conn, symbol, market="US"):
    for h in stock_db.fetch_all_real_holdings(conn=conn):
        if h["symbol"] == symbol and h["market"] == market:
            return h
    return None


class LedgerTradesTest(unittest.TestCase):
    def test_tc002_add_position_weighted_average(self):
        conn = _db()
        try:
            _buy(conn, symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-01")
            _buy(conn, symbol="MCD", trade_price=14, quantity=100, trade_date="2026-06-02")
            h = _holding(conn, "MCD")
            self.assertEqual(h["remaining_shares"], 200)
            self.assertEqual(h["total_buy_shares"], 200)
            self.assertAlmostEqual(h["remaining_cost_rmb"], 16800)
            self.assertAlmostEqual(h["avg_cost_rmb_per_share"], 84)
            self.assertAlmostEqual(h["avg_cost_local_per_share"], 12)  # 原币加权，非 RMB 倒推
            self.assertEqual(h["close_status"], "open")
        finally:
            conn.close()

    def test_tc003_partial_sell(self):
        conn = _db()
        try:
            _buy(conn, symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-01")
            _buy(conn, symbol="MCD", trade_price=14, quantity=100, trade_date="2026-06-02")
            res = _sell(conn, symbol="MCD", trade_price=16, quantity=50, trade_date="2026-06-02")
            tr = [t for t in stock_db.fetch_real_holding_records(
                account="default", market="US", symbol="MCD", conn=conn) if t["side"] == "sell"][0]
            self.assertAlmostEqual(tr["realized_pnl_rmb"], 1400)      # 5600 - 4200
            self.assertAlmostEqual(tr["realized_pnl_pct"], 1400 / 4200)
            h = _holding(conn, "MCD")
            self.assertEqual(h["remaining_shares"], 150)
            self.assertAlmostEqual(h["remaining_cost_rmb"], 12600)
            self.assertEqual(h["close_status"], "partial")
            self.assertIsNotNone(res["trade_id"])
        finally:
            conn.close()

    def test_tc020_buy_fee_into_cost_basis(self):
        conn = _db()
        try:
            _buy(conn, symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-02", fee_amount=2)
            h = _holding(conn, "MCD")
            self.assertAlmostEqual(h["remaining_cost_rmb"], 7014)     # 7000 + 14
            self.assertAlmostEqual(h["avg_cost_rmb_per_share"], 70.14)
        finally:
            conn.close()

    def test_tc021_epoch_not_polluted_after_close(self):
        conn = _db()
        try:
            _buy(conn, symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-01")
            _sell(conn, symbol="MCD", trade_price=12, quantity=100, trade_date="2026-06-02")
            self.assertIsNone(_holding(conn, "MCD"))                  # 清仓后不在当前持仓
            _buy(conn, symbol="MCD", trade_price=13, quantity=20, trade_date="2026-06-03")
            h = _holding(conn, "MCD")
            self.assertEqual(h["remaining_shares"], 20)
            self.assertAlmostEqual(h["avg_cost_rmb_per_share"], 91)   # 13*7，绝不是上一轮 84/70
            self.assertEqual(h["position_epoch"], 2)
        finally:
            conn.close()

    def test_tc025_loss_sell_negative_realized(self):
        conn = _db()
        try:
            _buy(conn, symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-01")
            _sell(conn, symbol="MCD", trade_price=8, quantity=40, trade_date="2026-06-02")
            tr = [t for t in stock_db.fetch_real_holding_trade_history(conn=conn)][0]
            self.assertAlmostEqual(tr["realized_pnl_rmb"], -560)      # 2240 - 2800
            self.assertAlmostEqual(tr["realized_pnl_pct"], -0.20)
            h = _holding(conn, "MCD")
            self.assertEqual(h["remaining_shares"], 60)
            self.assertAlmostEqual(h["remaining_cost_rmb"], 4200)
            summ = stock_db.fetch_pnl_summary(conn=conn)
            self.assertAlmostEqual(summ["realized_pnl_rmb"], -560)    # 累计带符号，不取绝对值
        finally:
            conn.close()

    def test_tc026_partial_sell_then_add_recompute_avg(self):
        conn = _db()
        try:
            _buy(conn, symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-01")
            _sell(conn, symbol="MCD", trade_price=12, quantity=50, trade_date="2026-06-02")
            h = _holding(conn, "MCD")
            self.assertAlmostEqual(h["remaining_cost_rmb"], 3500)
            self.assertAlmostEqual(h["avg_cost_rmb_per_share"], 70)   # 卖出不改剩余均价
            _buy(conn, symbol="MCD", trade_price=14, quantity=100, trade_date="2026-06-03")
            h = _holding(conn, "MCD")
            self.assertEqual(h["remaining_shares"], 150)
            self.assertAlmostEqual(h["remaining_cost_rmb"], 13300)    # 3500 + 9800
            self.assertAlmostEqual(h["avg_cost_rmb_per_share"], 13300 / 150)
            self.assertAlmostEqual(h["avg_cost_local_per_share"], 1900 / 150)  # (500+1400)/150
        finally:
            conn.close()

    def test_tc027_out_of_order_replays_by_trade_date(self):
        conn = _db()
        try:
            # 录入顺序乱：先 6-02 买，再补 5-31 买，最后补 6-01 卖
            _buy(conn, symbol="MCD", trade_price=14, quantity=100, trade_date="2026-06-02")
            _buy(conn, symbol="MCD", trade_price=10, quantity=100, trade_date="2026-05-31")
            _sell(conn, symbol="MCD", trade_price=16, quantity=50, trade_date="2026-06-01")
            sells = stock_db.fetch_real_holding_trade_history(conn=conn)
            # 6-01 卖出时只有 5-31 那笔买入存在 → 成本按 70 算
            self.assertAlmostEqual(sells[0]["realized_pnl_rmb"], 16 * 50 * 7 - 70 * 50)  # 2100
            h = _holding(conn, "MCD")
            self.assertEqual(h["remaining_shares"], 150)
            self.assertAlmostEqual(h["remaining_cost_rmb"], 13300)    # 3500 + 9800，非 4200 口径
        finally:
            conn.close()

    def test_tc028_same_day_executed_at_ordering(self):
        conn = _db()
        try:
            _buy(conn, symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-02",
                 executed_at="2026-06-02 09:30:00")
            _sell(conn, symbol="MCD", trade_price=12, quantity=50, trade_date="2026-06-02",
                  executed_at="2026-06-02 10:00:00")
            sells = stock_db.fetch_real_holding_trade_history(conn=conn)
            self.assertAlmostEqual(sells[0]["realized_pnl_rmb"], 12 * 50 * 7 - 70 * 50)  # 700
            h = _holding(conn, "MCD")
            self.assertEqual(h["remaining_shares"], 50)
            self.assertAlmostEqual(h["remaining_cost_rmb"], 3500)
        finally:
            conn.close()

    def test_tc029_dual_basis_hk_two_fx_lots(self):
        conn = _db()
        try:
            stock_db.insert_real_holding_buy(
                {"symbol": "9992.HK", "market": "HK", "trade_price": 100, "quantity": 100,
                 "trade_date": "2026-06-01", "fx_rate": 0.90}, conn=conn)
            stock_db.insert_real_holding_buy(
                {"symbol": "9992.HK", "market": "HK", "trade_price": 120, "quantity": 100,
                 "trade_date": "2026-06-02", "fx_rate": 0.92}, conn=conn)
            h = _holding(conn, "9992.HK", market="HK")
            self.assertAlmostEqual(h["avg_cost_local_per_share"], 110)        # (100+120)/2
            self.assertAlmostEqual(h["remaining_cost_rmb"], 20040)            # 9000 + 11040
            self.assertAlmostEqual(h["avg_cost_rmb_per_share"], 100.20)
            self.assertAlmostEqual(h["entry_price"], 110)                     # 兼容字段=原币均价
            self.assertEqual(h["shares"], 200)
            self.assertAlmostEqual(h["cost_rmb_locked"], 20040)
        finally:
            conn.close()

    def test_tc030_realized_locked_fx_unrealized_current_fx(self):
        conn = _db()
        try:
            stock_db.insert_real_holding_buy(
                {"symbol": "9992.HK", "market": "HK", "trade_price": 100, "quantity": 100,
                 "trade_date": "2026-06-01", "fx_rate": 0.90}, conn=conn)
            stock_db.insert_real_holding_sell(
                {"symbol": "9992.HK", "market": "HK", "trade_price": 110, "quantity": 40,
                 "trade_date": "2026-06-02", "fx_rate": 0.92}, conn=conn)
            sells = stock_db.fetch_real_holding_trade_history(conn=conn)
            self.assertAlmostEqual(sells[0]["realized_pnl_rmb"], 110 * 40 * 0.92 - 90 * 40)  # 448
            # 当前价 HKD120 / 当前汇率 0.95 盯市
            summ = stock_db.fetch_pnl_summary(
                conn=conn, price_lookup=lambda m, s: (120, 0.95))
            self.assertAlmostEqual(summ["realized_pnl_rmb"], 448)
            self.assertAlmostEqual(summ["unrealized_pnl_rmb"], 120 * 60 * 0.95 - 5400)  # 1440
            self.assertAlmostEqual(summ["total_pnl_rmb"], 1888)
        finally:
            conn.close()

    def test_oversell_rejected_and_rolled_back(self):
        conn = _db()
        try:
            _buy(conn, symbol="MCD", trade_price=10, quantity=20, trade_date="2026-06-01")
            with self.assertRaises(stock_db.LedgerConflict):
                _sell(conn, symbol="MCD", trade_price=12, quantity=21, trade_date="2026-06-02")
            # 冲突卖出被回滚，持仓不变，且没有残留 sell trade
            h = _holding(conn, "MCD")
            self.assertEqual(h["remaining_shares"], 20)
            self.assertEqual(stock_db.fetch_real_holding_trade_history(conn=conn), [])
        finally:
            conn.close()

    def test_void_latest_sell_restores_position(self):
        conn = _db()
        try:
            _buy(conn, symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-01")
            _sell(conn, symbol="MCD", trade_price=12, quantity=40, trade_date="2026-06-02")
            self.assertEqual(_holding(conn, "MCD")["remaining_shares"], 60)
            stock_db.void_latest_real_holding_trade("default", "US", "MCD", conn=conn)
            self.assertEqual(_holding(conn, "MCD")["remaining_shares"], 100)
            self.assertEqual(stock_db.fetch_pnl_summary(conn=conn)["realized_pnl_rmb"], 0)
            # 软删：voided 仍在表里
            allt = stock_db.fetch_real_holding_records(
                account="default", market="US", symbol="MCD", include_voided=True, conn=conn)
            self.assertTrue(any(t["status"] == "voided" for t in allt))
        finally:
            conn.close()

    def test_void_latest_buy_recomputes_avg(self):
        conn = _db()
        try:
            _buy(conn, symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-01")
            _buy(conn, symbol="MCD", trade_price=12, quantity=50, trade_date="2026-06-02")
            stock_db.void_latest_real_holding_trade("default", "US", "MCD", conn=conn)
            h = _holding(conn, "MCD")
            self.assertEqual(h["remaining_shares"], 100)
            self.assertAlmostEqual(h["avg_cost_rmb_per_share"], 70)
        finally:
            conn.close()

    def test_idempotency_same_key_one_trade(self):
        conn = _db()
        try:
            a = _buy(conn, symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-01",
                     client_request_id="req-001")
            b = _buy(conn, symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-01",
                     client_request_id="req-001")
            self.assertTrue(a["created"])
            self.assertTrue(b["deduped"])
            self.assertEqual(a["trade_id"], b["trade_id"])
            self.assertEqual(_holding(conn, "MCD")["remaining_shares"], 100)
        finally:
            conn.close()

    def test_idempotency_diff_keys_two_trades(self):
        conn = _db()
        try:
            _buy(conn, symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-01",
                 client_request_id="req-101")
            _buy(conn, symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-01",
                 client_request_id="req-102")
            h = _holding(conn, "MCD")
            self.assertEqual(h["remaining_shares"], 200)              # 同价同量两笔合法加仓
            self.assertAlmostEqual(h["remaining_cost_rmb"], 14000)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
