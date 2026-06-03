"""现金账本测试：入金/出金 + 买卖自动现金进出 → 现金余额 + 总资产口径。"""
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
    return stock_db.get_db(str(Path(tempfile.mkdtemp()) / "cash.duckdb"))


class CashLedgerTest(unittest.TestCase):
    def test_cash_balance_deposit_buy_sell(self):
        conn = _db()
        try:
            # 入金 100,000
            stock_db.insert_cash_flow({"flow_type": "deposit", "amount_rmb": 100000, "flow_date": "2026-06-01"}, conn=conn)
            self.assertEqual(stock_db.fetch_cash_summary(conn=conn)["cash_rmb"], 100000)
            # 买入 MCD 100@$10 fx7 = 7000 → 现金 93,000
            stock_db.insert_real_holding_buy(
                {"symbol": "MCD", "market": "US", "trade_price": 10, "quantity": 100,
                 "trade_date": "2026-06-02", "fx_rate": 7.0}, conn=conn)
            self.assertAlmostEqual(stock_db.fetch_cash_summary(conn=conn)["cash_rmb"], 93000)
            # 卖出 40@$16 fx7 = 4480 回笼 → 现金 97,480
            stock_db.insert_real_holding_sell(
                {"symbol": "MCD", "market": "US", "trade_price": 16, "quantity": 40,
                 "trade_date": "2026-06-03", "fx_rate": 7.0}, conn=conn)
            s = stock_db.fetch_cash_summary(conn=conn)
            self.assertAlmostEqual(s["cash_rmb"], 97480)         # 100000 - 7000 + 4480
            self.assertAlmostEqual(s["buy_outflow_rmb"], 7000)
            self.assertAlmostEqual(s["sell_inflow_rmb"], 4480)
        finally:
            conn.close()

    def test_total_asset_includes_cash(self):
        conn = _db()
        try:
            stock_db.insert_cash_flow({"flow_type": "deposit", "amount_rmb": 10000}, conn=conn)
            stock_db.insert_real_holding_buy(
                {"symbol": "MCD", "market": "US", "trade_price": 10, "quantity": 100,
                 "trade_date": "2026-06-01", "fx_rate": 7.0}, conn=conn)
            stock_db.insert_real_holding_sell(
                {"symbol": "MCD", "market": "US", "trade_price": 12, "quantity": 100,
                 "trade_date": "2026-06-02", "fx_rate": 7.0}, conn=conn)
            # 全卖光：现金 = 10000 - 7000 + 8400 = 11400；持仓市值 0；总资产应=现金
            cash = stock_db.fetch_cash_summary(conn=conn)["cash_rmb"]
            self.assertAlmostEqual(cash, 11400)
            # 卖出回笼的钱(8400)落在现金里，没有凭空消失
        finally:
            conn.close()

    def test_withdraw_and_fees(self):
        conn = _db()
        try:
            stock_db.insert_cash_flow({"flow_type": "deposit", "amount_rmb": 50000}, conn=conn)
            stock_db.insert_cash_flow({"flow_type": "withdraw", "amount_rmb": 5000}, conn=conn)
            # 买入带手续费 $2 → 现金扣 7000+14
            stock_db.insert_real_holding_buy(
                {"symbol": "MCD", "market": "US", "trade_price": 10, "quantity": 100,
                 "trade_date": "2026-06-01", "fx_rate": 7.0, "fee_amount": 2}, conn=conn)
            s = stock_db.fetch_cash_summary(conn=conn)
            self.assertAlmostEqual(s["cash_rmb"], 50000 - 5000 - 7014)  # 37986
            self.assertAlmostEqual(s["withdrawals_rmb"], 5000)
        finally:
            conn.close()

    def test_flow_crud_and_validation(self):
        conn = _db()
        try:
            fid = stock_db.insert_cash_flow({"flow_type": "deposit", "amount_rmb": 1000}, conn=conn)
            self.assertEqual(len(stock_db.fetch_cash_flows(conn=conn)), 1)
            self.assertEqual(stock_db.delete_cash_flow(fid, conn=conn), 1)
            self.assertEqual(len(stock_db.fetch_cash_flows(conn=conn)), 0)
            with self.assertRaises(stock_db.LedgerError):
                stock_db.insert_cash_flow({"flow_type": "deposit", "amount_rmb": -5}, conn=conn)
            with self.assertRaises(stock_db.LedgerError):
                stock_db.insert_cash_flow({"flow_type": "bogus", "amount_rmb": 5}, conn=conn)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
