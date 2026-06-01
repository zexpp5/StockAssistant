"""get_db force_read_only 锁感知 retry 回归测试.

背景(2026-06-01): DuckDB 单写者文件锁 —— 写进程持锁时只读连接也打不开
("Could not set lock ... Conflicting lock is held")。force_read_only 本身绕不开
正在写库的进程,所以在该路径加了 backoff retry。本测试锁住这套重试控制流:
  - 撞锁(消息含 lock)→ retry,写锁释放后成功
  - 非锁错误 → 立刻抛,不浪费 retry
  - retry=False(API/写路径)→ 立刻抛,绝不阻塞
  - 重试耗尽 → 抛 RuntimeError(带可读说明)
用 monkeypatch 模拟,不依赖真实锁/子进程,确定性且快。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import stock_db  # type: ignore

_LOCK_MSG = 'IO Error: Could not set lock on file: Conflicting lock is held by PID 123'


class ConnectWithLockRetryTest(unittest.TestCase):
    def test_retry_succeeds_after_transient_lock(self):
        """前 2 次撞锁,第 3 次成功 —— 应 retry 到成功并 sleep 了 2 次。"""
        calls = {"n": 0}
        sentinel = object()

        def fake_connect(path, read_only=False):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError(_LOCK_MSG)
            return sentinel

        with mock.patch.object(stock_db.duckdb, "connect", side_effect=fake_connect), \
             mock.patch.object(stock_db.time, "sleep") as sleep_mock:
            conn = stock_db._connect_with_lock_retry(
                "/x.duckdb", True, retry=True, attempts=8, backoff_s=0.01
            )
        self.assertIs(conn, sentinel)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleep_mock.call_count, 2)  # 2 次失败后各 sleep 一次

    def test_no_retry_raises_immediately(self):
        """retry=False(API/写路径)撞锁立刻抛,不重试不阻塞。"""
        calls = {"n": 0}

        def fake_connect(path, read_only=False):
            calls["n"] += 1
            raise RuntimeError(_LOCK_MSG)

        with mock.patch.object(stock_db.duckdb, "connect", side_effect=fake_connect):
            with self.assertRaises(RuntimeError):
                stock_db._connect_with_lock_retry("/x.duckdb", False, retry=False)
        self.assertEqual(calls["n"], 1)

    def test_non_lock_error_not_retried(self):
        """非锁错误(消息无 'lock')即使 retry=True 也立刻抛,不浪费重试。"""
        calls = {"n": 0}

        def fake_connect(path, read_only=False):
            calls["n"] += 1
            raise RuntimeError("Catalog Error: table does not exist")

        with mock.patch.object(stock_db.duckdb, "connect", side_effect=fake_connect), \
             mock.patch.object(stock_db.time, "sleep"):
            with self.assertRaises(RuntimeError):
                stock_db._connect_with_lock_retry(
                    "/x.duckdb", True, retry=True, attempts=8, backoff_s=0.01
                )
        self.assertEqual(calls["n"], 1)

    def test_exhausts_attempts_raises_runtimeerror(self):
        """一直撞锁 → 耗尽 attempts → 抛 RuntimeError(可读说明),而非原始 IOError。"""
        def fake_connect(path, read_only=False):
            raise RuntimeError(_LOCK_MSG)

        with mock.patch.object(stock_db.duckdb, "connect", side_effect=fake_connect), \
             mock.patch.object(stock_db.time, "sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                stock_db._connect_with_lock_retry(
                    "/x.duckdb", True, retry=True, attempts=3, backoff_s=0.01
                )
        self.assertIn("写锁", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
