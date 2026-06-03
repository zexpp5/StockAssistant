"""账本 v2 HTTP API 回归：buy/add/close/records/trade-history/pnl-summary/void + GET 兼容。

全程走 TestClient + 临时库（patch stock_db.get_db），不碰生产库。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import stock_db  # type: ignore


class LedgerApiTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dbpath = str(Path(self._dir.name) / "api.duckdb")
        real_get = stock_db.get_db
        # 把所有 get_db() 重定向到临时库（端点内部不传 path）。
        self._patch = mock.patch.object(
            stock_db, "get_db",
            side_effect=lambda *a, **k: real_get(self.dbpath, read_only=k.get("read_only", False)),
        )
        self._patch.start()
        # 自选同步/评级会起子进程，stub 掉，避免真跑 daily_picks。
        from fastapi.testclient import TestClient
        from stock_research.api import main as main_mod
        self._patch_sync = mock.patch.object(stock_db, "upsert_manual_watchlist", return_value=1)
        self._patch_popen = mock.patch("subprocess.Popen", return_value=mock.Mock(pid=99999))
        self._patch_sync.start()
        self._patch_popen.start()
        self.client = TestClient(main_mod.create_app())

    def tearDown(self):
        self._patch_popen.stop()
        self._patch_sync.stop()
        self._patch.stop()
        self._dir.cleanup()

    def _buy(self, **kw):
        kw.setdefault("market", "US")
        kw.setdefault("fx_rate", 7.0)
        return self.client.post("/api/real-holdings/buy", json=kw)

    def test_buy_add_close_history_pnl_void_flow(self):
        # 开仓
        r = self._buy(symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-01")
        self.assertEqual(r.status_code, 200, r.text)
        hid = r.json()["holding"]["id"]
        # 加仓
        r = self.client.post(f"/api/real-holdings/{hid}/add",
                             json={"trade_price": 14, "quantity": 100, "trade_date": "2026-06-02", "fx_rate": 7.0})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["holding"]["remaining_shares"], 200)
        self.assertAlmostEqual(r.json()["holding"]["avg_cost_rmb_per_share"], 84)

        # GET 列表（默认隐藏 closed，这里应有 1 行 open）
        lst = self.client.get("/api/real-holdings").json()
        self.assertEqual(len([h for h in lst if h["symbol"] == "MCD"]), 1)

        # 部分卖出 50 股 @ $16
        r = self.client.post(f"/api/real-holdings/{hid}/close",
                             json={"trade_price": 16, "quantity": 50, "trade_date": "2026-06-02", "fx_rate": 7.0})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["holding"]["remaining_shares"], 150)
        self.assertEqual(r.json()["holding"]["close_status"], "partial")

        # 交易历史含这笔卖出，已实现 1400
        hist = self.client.get("/api/real-holdings/trade-history").json()["sells"]
        self.assertEqual(len(hist), 1)
        self.assertAlmostEqual(hist[0]["realized_pnl_rmb"], 1400)

        # 收益摘要：已实现 1400
        pnl = self.client.get("/api/real-holdings/pnl-summary").json()
        self.assertAlmostEqual(pnl["realized_pnl_rmb"], 1400)

        # records 时间线
        recs = self.client.get(f"/api/real-holdings/{hid}/records").json()["records"]
        self.assertEqual(len(recs), 3)  # 2 buy + 1 sell

        # 撤销最近一笔（卖出）→ 持仓恢复 200，已实现归零
        sell_tid = hist[0]["trade_id"]
        r = self.client.post(f"/api/real-holdings/trades/{sell_tid}/void", json={})
        self.assertEqual(r.status_code, 200, r.text)
        h = [x for x in self.client.get("/api/real-holdings").json() if x["symbol"] == "MCD"][0]
        self.assertEqual(h["remaining_shares"], 200)
        self.assertAlmostEqual(self.client.get("/api/real-holdings/pnl-summary").json()["realized_pnl_rmb"], 0)

    def test_oversell_returns_400(self):
        hid = self._buy(symbol="MCD", trade_price=10, quantity=20, trade_date="2026-06-01").json()["holding"]["id"]
        r = self.client.post(f"/api/real-holdings/{hid}/close",
                             json={"trade_price": 12, "quantity": 21, "trade_date": "2026-06-02", "fx_rate": 7.0})
        self.assertEqual(r.status_code, 400)

    def test_void_non_latest_returns_409(self):
        hid = self._buy(symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-01").json()["holding"]["id"]
        self.client.post(f"/api/real-holdings/{hid}/add",
                         json={"trade_price": 12, "quantity": 50, "trade_date": "2026-06-02", "fx_rate": 7.0})
        # 撤销第一笔（非最近）→ 409
        recs = self.client.get(f"/api/real-holdings/{hid}/records").json()["records"]
        first_tid = recs[0]["trade_id"]
        r = self.client.post(f"/api/real-holdings/trades/{first_tid}/void", json={})
        self.assertEqual(r.status_code, 409)

    def test_full_sell_hides_from_default_list(self):
        hid = self._buy(symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-01").json()["holding"]["id"]
        self.client.post(f"/api/real-holdings/{hid}/close",
                         json={"trade_price": 12, "quantity": 100, "trade_date": "2026-06-02", "fx_rate": 7.0})
        lst = self.client.get("/api/real-holdings").json()
        self.assertEqual([h for h in lst if h["symbol"] == "MCD"], [])  # 清仓后默认不显示
        # 但交易历史仍在
        self.assertEqual(len(self.client.get("/api/real-holdings/trade-history").json()["sells"]), 1)

    def test_close_inherits_holding_currency(self):
        # 买入按 CNY 计价（ticker「测试」后端会推断成 USD，但持仓币种是 CNY）
        r = self.client.post("/api/real-holdings/buy", json={
            "symbol": "测试", "market": "CN", "currency": "CNY",
            "trade_price": 10, "quantity": 10, "trade_date": "2026-06-01", "fx_rate": 1.0})
        self.assertEqual(r.status_code, 200, r.text)
        hid = r.json()["holding"]["id"]
        # 卖出不带 currency → 必须继承持仓的 CNY，而不是按 ticker 推断成 USD
        r = self.client.post(f"/api/real-holdings/{hid}/close",
                             json={"trade_price": 12, "quantity": 10, "trade_date": "2026-06-02", "fx_rate": 1.0})
        self.assertEqual(r.status_code, 200, r.text)
        sells = self.client.get("/api/real-holdings/trade-history").json()["sells"]
        self.assertEqual(sells[0]["currency"], "CNY")          # 不是 USD
        self.assertAlmostEqual(sells[0]["realized_pnl_rmb"], 20)  # (12-10)*10*1.0，不是 fx 串成几百
        self.assertAlmostEqual(sells[0]["realized_pnl_pct"], 0.20)

    def test_edit_ledger_holding_cannot_change_shares(self):
        hid = self._buy(symbol="MCD", trade_price=10, quantity=10, trade_date="2026-06-01").json()["holding"]["id"]
        # 直接编辑（PUT）试图把数量改成 999 → 账本持仓应忽略数量，只改名称
        r = self.client.put(f"/api/real-holdings/{hid}", json={"name": "改个名", "shares": 999, "entry_price": 1})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("ledger_managed"))
        h = [x for x in self.client.get("/api/real-holdings").json() if x["id"] == hid][0]
        self.assertEqual(h["remaining_shares"], 10)   # 数量没被编辑改掉
        self.assertEqual(h["name"], "改个名")          # 名称改了

    def test_idempotent_buy_via_api(self):
        a = self._buy(symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-01",
                      client_request_id="req-1").json()
        b = self._buy(symbol="MCD", trade_price=10, quantity=100, trade_date="2026-06-01",
                      client_request_id="req-1").json()
        self.assertTrue(a.get("created"))
        self.assertTrue(b.get("deduped"))
        h = [x for x in self.client.get("/api/real-holdings").json() if x["symbol"] == "MCD"][0]
        self.assertEqual(h["remaining_shares"], 100)


if __name__ == "__main__":
    unittest.main()
