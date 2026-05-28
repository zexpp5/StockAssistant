"""盘中刷新真实持仓的行情快照。

只针对 `real_holdings` 表里的 4-10 只个股，用 yfinance fast_info 拉实时价，
upsert 到 price_daily（覆盖当日 row），然后重跑 real_holding_review，
让 dashboard 的"今日持仓体检"板块看到盘中价。

设计目标：
- 轻量：只动真实持仓那几只，跑完 < 10s
- 不推飞书：和 daily_refresh.sh --morning 区分开，避免盘中重复推送
- 容错：单只失败不中断；DuckDB 写锁冲突自动 retry
- 节能：周末 / 北京时间 05-08 + 17-20 这两段三大市场都不交易的时段直接跳过
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import duckdb  # type: ignore
import yfinance as yf  # type: ignore

import stock_db  # type: ignore

DB_PATH = str(REPO / "stock_history_v2.duckdb")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("intraday_refresh")

# yfinance 在闭市时 history(period=1y) 会抛 "possibly delisted"，但 fast_info 仍可用。
# 我们只用 fast_info，所以 history 的 ERROR 输出是噪音，全部静默。
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def _skip_reason() -> str | None:
    """三大市场都不交易的整点直接跳过，省 yfinance 配额 + 减少 DB 锁竞争。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return f"周末({['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][now.weekday()]})"
    h = now.hour
    # 北京时间 05-08: A/HK 未开 + 美股 04:00 后已收盘
    # 北京时间 17-20: A/HK 已收盘 + 美股 21:30 还没开
    if 5 <= h <= 8 or 17 <= h <= 20:
        return f"盘外({h:02d}:00 三大市场均不交易)"
    return None


def _infer_market(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith(".HK"):
        return "HK"
    if s.endswith(".SS") or s.endswith(".SZ") or s.endswith(".BJ"):
        return "CN"
    return "US"


def _to_yf_ticker(symbol: str) -> str:
    """real_holdings 里的 symbol 已经是 yfinance 格式（9992.HK / MCD / BRK-B），直接返回。"""
    return symbol.strip().upper()


def _fetch_one(symbol: str) -> dict | None:
    """yfinance fast_info 拉单只实时价。返回 None 表示拉失败。"""
    try:
        t = yf.Ticker(_to_yf_ticker(symbol))
        fi = t.fast_info
        price = fi.get("lastPrice") or fi.get("last_price")
        prev = fi.get("previousClose") or fi.get("previous_close")
        currency = fi.get("currency")
        if price is None:
            logger.warning("%s: fast_info 没有 lastPrice，跳过", symbol)
            return None
        return {
            "symbol": symbol.upper(),
            "price": float(price),
            "prev_close": float(prev) if prev is not None else None,
            "currency": currency,
        }
    except Exception as exc:
        logger.warning("%s: 拉价失败 %s", symbol, exc)
        return None


def _upsert_price_daily(rows: list[dict]) -> int:
    if not rows:
        return 0
    con = duckdb.connect(DB_PATH)
    try:
        now = datetime.now()
        trade_date = now.date()
        payload = []
        for r in rows:
            market = _infer_market(r["symbol"])
            payload.append((
                market, r["symbol"], trade_date, "1d",
                r["price"], r.get("prev_close"), r.get("currency"),
                "yfinance_intraday", now, now,
            ))
        con.executemany(
            "DELETE FROM price_daily WHERE market=? AND symbol=? AND trade_date=? AND interval=?",
            [(p[0], p[1], p[2], p[3]) for p in payload],
        )
        con.executemany(
            """
            INSERT INTO price_daily (
                market, symbol, trade_date, interval, close, prev_close, currency,
                source, source_updated_at, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        return len(payload)
    finally:
        con.close()


def _run_with_lock_retry(max_attempts: int = 3, wait_sec: int = 60) -> int:
    """整段主流程在 DB 写锁冲突时自动 retry。

    锁来源：常见是 daily_refresh.sh / refresh_system_universe_v2.py 等长进程占着写锁。
    锁释放后立刻成功；3 次还不行才放弃。
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            holdings = stock_db.fetch_all_real_holdings()
            symbols = sorted({(h.get("symbol") or "").strip().upper() for h in holdings if h.get("symbol")})
            if not symbols:
                logger.info("real_holdings 为空，跳过。")
                return 0

            logger.info("拉取 %d 只真实持仓的盘中价: %s", len(symbols), ", ".join(symbols))
            fetched = [r for r in (_fetch_one(s) for s in symbols) if r is not None]
            logger.info("成功拉到 %d / %d 只", len(fetched), len(symbols))

            written = _upsert_price_daily(fetched)
            logger.info("upsert price_daily %d 行", written)

            from stock_research.jobs.real_holding_review import build_real_holding_review
            payload = build_real_holding_review(persist=True)
            run = payload.get("run") or {}
            logger.info(
                "real_holding_review 重跑完成 · run_id=%s · generated_at=%s · holdings=%s · quality=%s",
                run.get("review_run_id"), run.get("generated_at"),
                run.get("holding_count"), run.get("data_quality"),
            )
            return 0
        except duckdb.IOException as exc:
            last_exc = exc
            if "lock" in str(exc).lower() and attempt < max_attempts:
                logger.warning("DB 写锁冲突 (attempt %d/%d)，%d s 后重试: %s",
                               attempt, max_attempts, wait_sec, exc)
                time.sleep(wait_sec)
                continue
            logger.error("DB 锁重试 %d 次仍失败，放弃本轮: %s", attempt, exc)
            return 1
    logger.error("意外退出 retry loop: %s", last_exc)
    return 1


def main() -> int:
    skip = _skip_reason()
    if skip:
        logger.info("跳过本轮刷新: %s", skip)
        return 0
    return _run_with_lock_retry()


if __name__ == "__main__":
    sys.exit(main())
