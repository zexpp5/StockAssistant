#!/usr/bin/env python3
"""补分析师目标价覆盖 → 喂 buy_zone 估值锚点。yfinance 主源 + FMP 兜底。

背景(2026-06-16):buy_zone 估值锚定靠 analyst_grade_events.price_target,
但现有来源(yfinance grade history)只盖 ~33% 推荐池。

数据源抉择(实测):
  - yfinance .info.targetMeanPrice → 14/14=100% 覆盖,带分析师人数,免费,
    且系统每天抓 forward_pe 时已在调 info → 零额外成本。**主源**。
  - FMP /price-target-summary → 可用但配额极低(批量必 402,见 project_fmp_historical_quota_limit),
    仅作 yfinance 缺失时的**兜底**。

口径:
  - 目标宇宙 = 最新推荐池 + manual_watchlist 里的美股
  - 默认只抓"近 120 天没有任何目标价"的票;--all 刷新全宇宙
  - 写入 analyst_grade_events,event_date=今天,source 标 yfinance/info_target 或 FMP/price-target-summary
  - 幂等:每只票先删自己这两个 source 的旧行再插(只碰自己 source,
    遵守 incident_price_daily_multi_writer_clobber 教训)

用法:
  python3 -m scripts.tools.fetch_fmp_price_targets            # 跑全部缺口
  python3 -m scripts.tools.fetch_fmp_price_targets --symbols GOOGL,MRVL
  python3 -m scripts.tools.fetch_fmp_price_targets --all      # 全宇宙刷新
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))

from stock_research.core import fmp_client  # noqa: E402

SOURCE_YF = "yfinance/info_target"
SOURCE_FMP = "FMP/price-target-summary"
MY_SOURCES = (SOURCE_YF, SOURCE_FMP)
TARGET_MAX_AGE_DAYS = 120


def _yf_target(ticker: str) -> dict | None:
    """yfinance .info.targetMeanPrice — 主源(实测 100% 覆盖,免费,带分析师人数)。"""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
    except Exception:
        return None
    tm = info.get("targetMeanPrice")
    if isinstance(tm, (int, float)) and tm > 0:
        return {
            "price_target": float(tm),
            "n_analysts": int(info.get("numberOfAnalystOpinions") or 0),
            "window": "yf均值",
            "source": SOURCE_YF,
        }
    return None


def _fetch_target(ticker: str) -> dict | None:
    """先 yfinance(主),无则 FMP(兜底,配额低)。"""
    pt = _yf_target(ticker)
    if pt:
        return pt
    try:
        fmp_client.reset_throttle_disable()
    except Exception:
        pass
    return fmp_client.fetch_price_target(ticker)


def _us_universe(conn) -> list[str]:
    """最新推荐池 + manual_watchlist 的美股代号(无后缀=美股)。"""
    syms: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM recommendation_picks "
            "WHERE run_id=(SELECT run_id FROM recommendation_runs "
            "ORDER BY generated_at DESC LIMIT 1)"
        ).fetchall()
        syms.update(r[0] for r in rows if r[0])
    except Exception as e:
        print(f"  (推荐池读取失败: {e})")
    try:
        rows = conn.execute(
            "SELECT symbol FROM manual_watchlist WHERE market IN ('US','us')"
        ).fetchall()
        syms.update(r[0] for r in rows if r[0])
    except Exception as e:
        print(f"  (watchlist 读取失败: {e})")
    # 美股 = 无市场后缀(.HK/.SS/.SZ 等的排除)
    return sorted(s for s in syms if "." not in s)


def _symbols_lacking_target(conn, universe: list[str], today: date) -> list[str]:
    cutoff = today - timedelta(days=TARGET_MAX_AGE_DAYS)
    covered = set()
    if universe:
        ph = ",".join(["?"] * len(universe))
        rows = conn.execute(
            f"SELECT DISTINCT upper(symbol) FROM analyst_grade_events "
            f"WHERE upper(symbol) IN ({ph}) AND price_target IS NOT NULL "
            f"AND price_target > 0 AND event_date >= ?",
            [s.upper() for s in universe] + [cutoff],
        ).fetchall()
        covered = {r[0] for r in rows}
    return [s for s in universe if s.upper() not in covered]


def _upsert(conn, ticker: str, pt: float, now: datetime, today: date, source: str) -> None:
    # 幂等:删掉本写入器自己两个 source 的旧行(不碰 yfinance/upgrades_downgrades 等别人的)
    conn.execute(
        "DELETE FROM analyst_grade_events WHERE upper(symbol)=upper(?) AND source IN (?, ?)",
        [ticker, MY_SOURCES[0], MY_SOURCES[1]],
    )
    # grade 文本列有 NOT NULL 约束;这是目标价共识不是评级变动,给非空占位,
    # 下游评级分析靠 source='FMP/price-target-summary' 排除即可(不污染 grade IC)
    conn.execute(
        """
        INSERT INTO analyst_grade_events
          (market, symbol, event_date, grading_company, previous_grade, new_grade,
           action, price_target_action, price_target, prior_price_target, source, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        ["US", ticker, today, "analyst consensus", "consensus", "consensus",
         "consensus", "set", pt, source, now],
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="逗号分隔,只跑这些(覆盖宇宙逻辑)")
    ap.add_argument("--limit", type=int, default=0, help="最多跑前 N 只(0=不限)")
    ap.add_argument("--all", action="store_true", help="不止缺口,刷新整个宇宙")
    args = ap.parse_args()

    if not fmp_client.is_available():
        print("❌ FMP_API_KEY 缺失,无法抓取")
        return 1

    import stock_db  # noqa: E402
    conn = stock_db.get_db()
    today = date.today()
    now = datetime.now()

    if args.symbols:
        targets = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        universe = targets
    else:
        universe = _us_universe(conn)
        targets = universe if args.all else _symbols_lacking_target(conn, universe, today)

    if args.limit:
        targets = targets[: args.limit]

    print(f"美股宇宙 {len(universe)} 只 · 本次抓取 {len(targets)} 只"
          + ("(全刷)" if args.all else "(仅缺口)" if not args.symbols else "(指定)"))
    if not targets:
        print("✅ 无缺口,无需抓取")
        conn.close()
        return 0

    ok = miss = 0
    for i, sym in enumerate(targets, 1):
        try:
            pt = _fetch_target(sym)
        except Exception as e:
            pt = None
            print(f"  [{i}/{len(targets)}] {sym}: ERR {e}")
        if pt and pt.get("price_target"):
            _upsert(conn, sym, pt["price_target"], now, today, pt["source"])
            ok += 1
            tag = "yf" if pt["source"] == SOURCE_YF else "fmp"
            print(f"  [{i}/{len(targets)}] {sym}: ${pt['price_target']:.0f} "
                  f"({pt['n_analysts']}人·{tag})")
        else:
            miss += 1
            print(f"  [{i}/{len(targets)}] {sym}: 无目标价")

    conn.close()
    print(f"\n完成:写入 {ok} 只 · 未获 {miss} 只")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
