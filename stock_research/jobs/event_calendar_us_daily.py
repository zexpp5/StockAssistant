"""美股事件日历刷新：财报日 + EPS 超预期幅度 → data/event_calendar_us.json

跟港股版（event_calendar_hk_daily.py）逻辑一致，只换 universe 来源：
  - DuckDB system_universe (market='US')  系统科技池白名单
  - 当前 trade_delta.json (美股) 出现的 ticker
  - plan_a_v5.json 里的 ticker
  - manual_watchlist (market='US') 自选股

数据源同 hk：yfinance.Ticker(symbol).earnings_dates / .calendar

下游消费：
  - morning_brief._build_catalyst  美股分支
  - dashboard build_catalyst_index  统一注入 catalyst 字段
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _gather_universe() -> dict[str, str]:
    """ticker → name；合并 system_universe[US] + 当前 trade_delta + plan_a_v5 + manual_watchlist[US]"""
    out: dict[str, str] = {}

    # 1. DuckDB system_universe (market='US')
    try:
        import duckdb
        db_path = REPO / "stock_history_v2.duckdb"
        if db_path.exists():
            con = duckdb.connect(str(db_path), read_only=True)
            try:
                rows = con.execute(
                    "SELECT symbol, name FROM system_universe WHERE market = 'US'"
                ).fetchall()
                for sym, name in rows:
                    if sym:
                        out[sym.upper()] = name or ""
                # manual_watchlist 美股
                try:
                    rows = con.execute(
                        "SELECT symbol, name FROM manual_watchlist WHERE market = 'US' OR market IS NULL"
                    ).fetchall()
                    for sym, name in rows:
                        s = (sym or "").upper()
                        if s and not _is_non_us(s):
                            out.setdefault(s, name or "")
                except Exception:
                    pass
            finally:
                con.close()
    except Exception as e:
        logger.warning("DuckDB universe 加载失败: %s", e)

    # 2. trade_delta.json (美股) 当前快照
    td = REPO / "data" / "latest" / "trade_delta.json"
    if td.exists():
        try:
            d = json.loads(td.read_text(encoding="utf-8"))
            for bucket in ("buys", "sells", "holds"):
                for item in (d.get(bucket) or []):
                    t = (item.get("ticker") or "").upper()
                    if t and not _is_non_us(t):
                        out.setdefault(t, item.get("name", ""))
        except Exception as e:
            logger.warning("trade_delta.json 解析失败: %s", e)

    # 3. plan_a_v5.json
    plan = REPO / "data" / "latest" / "plan_a_v5.json"
    if plan.exists():
        try:
            d = json.loads(plan.read_text(encoding="utf-8"))
            entries = d.get("plan_v5") or d.get("plan_v6") or d.get("plan") or []
            for e in entries:
                t = (e.get("ticker") or "").upper()
                if t and not _is_non_us(t):
                    out.setdefault(t, e.get("name", ""))
        except Exception as e:
            logger.warning("plan_a_v5.json 解析失败: %s", e)

    return out


def _is_non_us(ticker: str) -> bool:
    """裸 ticker 视为美股；带 .HK / .SS / .SZ / .BJ / .TW / .KS / .T / .AX / .L 视为非美股。"""
    return any(ticker.endswith(s) for s in (".HK", ".SS", ".SZ", ".BJ", ".TW", ".TWO", ".KS", ".T", ".AX", ".L"))


def _fetch_yf_earnings(ticker: str, name: str) -> tuple[list[dict], str]:
    """跟 hk 版完全一样的实现 — 拉 earnings_dates 历史 + 兜底 calendar 下次财报日。
    返回 (events_list, status); status ∈ {"ok", "no_data", "error"}。
    """
    try:
        import yfinance as yf
    except ImportError:
        return [], "error"

    out: list[dict] = []
    try:
        t = yf.Ticker(ticker)
        ed = t.earnings_dates
        if ed is None or ed.empty:
            cal = t.calendar or {}
            nxt = cal.get("Earnings Date")
            if nxt:
                d_ = nxt[0] if isinstance(nxt, list) else nxt
                if isinstance(d_, date):
                    out.append({
                        "ticker": ticker,
                        "name": name,
                        "event_date": d_.isoformat(),
                        "event_type": "earnings_upcoming",
                        "eps_estimate": cal.get("Earnings Average"),
                        "eps_actual": None,
                        "surprise_pct": None,
                        "description": f"{name or ticker} 下次财报预计 {d_.strftime('%m-%d')}",
                        "source": "yfinance/calendar",
                    })
                    return out, "ok"
            return [], "no_data"

        today = date.today()
        for idx, row in ed.iterrows():
            d_ = idx.date() if hasattr(idx, "date") else idx
            if not isinstance(d_, date):
                continue
            est = _safe_float(row.get("EPS Estimate"))
            act = _safe_float(row.get("Reported EPS"))
            surp = _safe_float(row.get("Surprise(%)"))

            if act is not None:
                if surp is None and est not in (None, 0):
                    surp = (act - est) / abs(est) * 100
                surp_label = f"{surp:+.1f}%" if surp is not None else "n/a"
                desc = f"{name or ticker} {d_.strftime('%m-%d')} EPS {act:.2f} / 估 {est:.2f} 超预期 {surp_label}" if est is not None \
                    else f"{name or ticker} {d_.strftime('%m-%d')} EPS {act:.2f}（无估计）"
                out.append({
                    "ticker": ticker,
                    "name": name,
                    "event_date": d_.isoformat(),
                    "event_type": "earnings",
                    "eps_estimate": est,
                    "eps_actual": act,
                    "surprise_pct": round(surp, 2) if surp is not None else None,
                    "description": desc,
                    "source": "yfinance/earnings_dates",
                })
            elif d_ >= today:
                out.append({
                    "ticker": ticker,
                    "name": name,
                    "event_date": d_.isoformat(),
                    "event_type": "earnings_upcoming",
                    "eps_estimate": est,
                    "eps_actual": None,
                    "surprise_pct": None,
                    "description": f"{name or ticker} 下次财报 {d_.strftime('%m-%d')}（EPS 估 {est:.2f}）" if est is not None
                        else f"{name or ticker} 下次财报 {d_.strftime('%m-%d')}",
                    "source": "yfinance/earnings_dates",
                })
        return (out, "ok") if out else ([], "no_data")
    except Exception as e:
        logger.warning("yfinance fetch %s failed: %s", ticker, e)
        return [], "error"


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:
            return None
        return f
    except (TypeError, ValueError):
        return None


def main() -> int:
    universe = _gather_universe()
    logger.info("覆盖 %d 只美股 ticker", len(universe))

    all_events: list[dict] = []
    hit, miss, errored = 0, [], []
    for ticker, name in sorted(universe.items()):
        evs, status = _fetch_yf_earnings(ticker, name)
        if status == "ok":
            all_events.extend(evs)
            hit += 1
        elif status == "no_data":
            miss.append(ticker)
        else:
            errored.append(ticker)

    all_events.sort(key=lambda e: e["event_date"], reverse=True)

    payload = {
        "generated_at": datetime.now().isoformat(),
        "n_tickers": len(universe),
        "n_events": len(all_events),
        "coverage": {
            "hit": hit,
            "miss": len(miss),
            "errored": len(errored),
            "miss_tickers": miss[:20],
            "errored_tickers": errored[:20],
        },
        "events": all_events,
    }

    out = REPO / "data" / "event_calendar_us.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✅ 美股事件日历已写入 {out}")
    print(f"   tickers: {len(universe)} (hit {hit} / miss {len(miss)} / err {len(errored)})")
    print(f"   events:  {len(all_events)}")
    if miss:
        print(f"   miss 样本: {miss[:5]}")
    if errored:
        print(f"   error 样本: {errored[:5]}")
    return 0 if hit > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
