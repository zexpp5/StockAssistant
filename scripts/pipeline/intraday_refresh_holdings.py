"""刷新真实持仓的行情快照。

只针对 `real_holdings` 表里的 4-10 只个股，用 yfinance fast_info / daily bar
拉轻量行情，upsert 到 price_daily（覆盖当日 row），然后重跑 real_holding_review，
让 dashboard 的"今日持仓体检"板块看到最近有效价。

设计目标：
- 轻量：只动真实持仓那几只，跑完 < 10s
- 不推飞书：和 daily_refresh.sh --morning 区分开，避免盘中重复推送
- 容错：单只失败不中断；DuckDB 写锁冲突自动 retry
- 节能：周末直接跳过；工作日允许收盘后补写已出现的当日 daily bar
"""
from __future__ import annotations

import logging
import math
import sys
import time
import zoneinfo
from datetime import datetime, date
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
    """非交易日直接跳过，省 yfinance 配额 + 减少 DB 锁竞争。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return f"周末({['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][now.weekday()]})"
    return None


def _infer_market(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith(".HK"):
        return "HK"
    if s.endswith(".SS") or s.endswith(".SZ") or s.endswith(".BJ"):
        return "CN"
    return "US"


def _market_now(symbol: str) -> datetime:
    market = _infer_market(symbol)
    if market == "US":
        tz = zoneinfo.ZoneInfo("America/New_York")
    elif market == "HK":
        tz = zoneinfo.ZoneInfo("Asia/Hong_Kong")
    else:
        tz = zoneinfo.ZoneInfo("Asia/Shanghai")
    return datetime.now(tz)


def _regular_session_minutes(symbol: str) -> list[tuple[int, int]]:
    market = _infer_market(symbol)
    if market == "US":
        return [(570, 960)]  # 美股常规时段 ET 09:30-16:00
    if market == "HK":
        return [(570, 720), (780, 960)]  # 港股 9:30-12:00 + 13:00-16:00
    return [(570, 690), (780, 900)]  # A 股 9:30-11:30 + 13:00-15:00


def _in_regular_session(symbol: str, now: datetime) -> bool:
    minutes = now.hour * 60 + now.minute
    return any(start <= minutes < end for start, end in _regular_session_minutes(symbol))


def _coerce_trade_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _market_trade_date(symbol: str, fetched_trade_date=None) -> "date | None":
    """返回 symbol 当前应贴的 trade_date；无法确认当日行情时返回 None（不写 phantom 行）。

    Bug fix 2026-06-01: 原代码用 `now.date()`（北京日历日）当所有市场的 trade_date，
    导致盘外整点跑时 fast_info.lastPrice（上一交易日收盘）被误贴成今天。
    例：6-01 周一 09:01 跑美股 MCD，fast_info 返回的是 5-30 周五收盘价，但被写成
    `trade_date=2026-06-01` 的 phantom 行，real_holding_review 又据此算「今日盈亏」。

    2026-06-02: 收盘后 yfinance daily bar 已出现今天时，允许写入今天。
    这样港股 16:02 不会继续停在 6/1；若数据源还没给出今天日 K，仍跳过。
    """
    now = _market_now(symbol)
    if now.weekday() >= 5:
        return None
    today = now.date()
    fetched_date = _coerce_trade_date(fetched_trade_date)
    if fetched_date is not None:
        if fetched_date == today:
            return fetched_date
        if fetched_date > today:
            return None
        # 数据源只给到上一交易日时，只在常规交易时段内才允许 fast_info 作为今天盘中价。
        return today if _in_regular_session(symbol, now) else None
    return today if _in_regular_session(symbol, now) else None


def _to_yf_ticker(symbol: str) -> str:
    """real_holdings 里的 symbol 已经是 yfinance 格式（9992.HK / MCD / BRK-B），直接返回。"""
    return symbol.strip().upper()


def _currency_for_symbol(symbol: str) -> str:
    market = _infer_market(symbol)
    if market == "HK":
        return "HKD"
    if market == "CN":
        return "CNY"
    return "USD"


def _positive_float(value) -> float | None:
    try:
        f = float(value)
    except Exception:
        return None
    if math.isfinite(f) and f > 0:
        return f
    return None


def _latest_daily_bar(symbol: str) -> dict | None:
    yf_symbol = _to_yf_ticker(symbol)
    try:
        hist = yf.download(
            yf_symbol,
            period="7d",
            interval="1d",
            progress=False,
            auto_adjust=False,
            threads=False,
        )
    except Exception as exc:
        logger.warning("%s: daily bar 拉取失败 %s", symbol, exc)
        return None
    if hist is None or hist.empty:
        return None
    close_data = None
    if "Close" in hist:
        close_data = hist["Close"]
    elif getattr(hist, "columns", None) is not None:
        # yfinance 1.3 单 ticker 也可能返回 MultiIndex: (Price, Ticker)。
        for col in hist.columns:
            if isinstance(col, tuple) and "Close" in col:
                close_data = hist[col]
                break
    if close_data is None:
        return None

    close = None
    if getattr(close_data, "columns", None) is not None:
        for col in close_data.columns:
            series = close_data[col].dropna()
            if not series.empty:
                close = series
                break
    else:
        close = close_data.dropna()
    if close is None:
        return None
    if close.empty:
        return None
    try:
        latest_price = _positive_float(close.iloc[-1])
        if latest_price is None:
            return None
        latest_date = close.index[-1].date()
        prev_close = None
        if len(close) >= 2:
            prev_close = _positive_float(close.iloc[-2])
        return {"price": latest_price, "prev_close": prev_close, "trade_date": latest_date}
    except Exception:
        return None


def _fetch_one(symbol: str) -> dict | None:
    """yfinance fast_info 拉单只实时价。返回 None 表示拉失败。"""
    try:
        yf_symbol = _to_yf_ticker(symbol)
        daily = _latest_daily_bar(symbol) or {}
        price = None
        prev = None
        currency = None
        try:
            fi = yf.Ticker(yf_symbol).fast_info or {}
            price = _positive_float(fi.get("lastPrice") or fi.get("last_price"))
            prev = _positive_float(fi.get("previousClose") or fi.get("previous_close"))
            currency = fi.get("currency")
        except Exception as exc:
            logger.info("%s: fast_info 不可用，使用 daily bar 兜底: %s", symbol, exc)
        if price is None:
            price = daily.get("price")
        if prev is None:
            prev = daily.get("prev_close")
        currency = currency or _currency_for_symbol(symbol)
        if price is None:
            logger.warning("%s: fast_info/daily bar 都没有可用价格，跳过", symbol)
            return None
        return {
            "symbol": symbol.upper(),
            "price": price,
            "prev_close": prev,
            "currency": currency,
            "trade_date": daily.get("trade_date"),
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
        payload = []
        skipped_off_hours: list[str] = []
        for r in rows:
            market = _infer_market(r["symbol"])
            trade_date = _market_trade_date(r["symbol"], r.get("trade_date"))
            if trade_date is None:
                skipped_off_hours.append(r["symbol"])
                continue
            payload.append((
                market, r["symbol"], trade_date, "1d",
                r["price"], r.get("prev_close"), r.get("currency"),
                "yfinance_intraday", now, now,
            ))
        if skipped_off_hours:
            logger.info("未确认当日行情，跳过 (不写 phantom 行): %s", ", ".join(skipped_off_hours))
        if not payload:
            return 0
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
