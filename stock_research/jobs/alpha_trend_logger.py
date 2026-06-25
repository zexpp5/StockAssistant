"""每日记一笔生产推荐 alpha → data/latest/alpha_trend.json，攒走势线。

只读 strategy_eval（生产 picks 成熟样本），不改打分、不推送。每天一行，
按日期幂等（同日重跑覆盖当天那条）。配合 launchd 每早跑一次即可。

记录：US/HK × 1d/5d 的 n / 平均 alpha% / 胜率% / 样本档位。
口径与 dashboard、手动复算完全同源（strategy_eval.mature_samples + summarize），
起点固定 PRODUCTION_METRICS_START（[[project_metrics_cutoff_2026-05-25]]）。

用法:
  python3 -m stock_research.jobs.alpha_trend_logger            # 记当天
  python3 -m stock_research.jobs.alpha_trend_logger --dry-run  # 只打印不写
  python3 -m stock_research.jobs.alpha_trend_logger --show     # 打印已攒走势
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[2]
_DB = _REPO / "stock_history_v2.duckdb"
OUT = _REPO / "data" / "latest" / "alpha_trend.json"
METRICS_START = "2026-05-25"   # 生产 cutoff，别动
MARKETS = ("US", "HK", "A")
HORIZONS = ("1d", "5d")


def _connect():
    """只读连库，撞 enhancement_refresh 写锁退避重试。"""
    import duckdb
    for _ in range(20):
        try:
            return duckdb.connect(str(_DB), read_only=True)
        except Exception:
            time.sleep(2)
    return None


def compute(as_of: date | None = None) -> dict:
    sys.path.insert(0, str(_REPO / "scripts" / "lib"))
    import stock_research.core.strategy_eval as se
    conn = _connect()
    if conn is None:
        raise RuntimeError("DB 持续被写锁占用，跳过本轮 alpha 记录")
    try:
        ver = se.latest_strategy_version(conn)
        rec: dict = {
            "date": (as_of or date.today()).isoformat(),
            "strategy_version": ver,
            "metrics_start": METRICS_START,
            "markets": {},
        }
        for mkt in MARKETS:
            rec["markets"][mkt] = {}
            for hz in HORIZONS:
                s = se.mature_samples(conn, market=mkt, horizon=hz, metrics_start=METRICS_START)
                if not s:
                    rec["markets"][mkt][hz] = {"n": 0}
                    continue
                m = se.summarize(s)
                rec["markets"][mkt][hz] = {
                    "n": m["n"],
                    "avg_alpha_pct": round(m["avg_alpha_pct"], 4),
                    "win_rate_pct": round(m["win_rate_pct"], 2),
                    "worst_alpha_pct": round(m["worst_alpha_pct"], 4),
                    "sample_power": m["sample_power"],
                }
        return rec
    finally:
        conn.close()


def _load_history() -> list[dict]:
    if OUT.exists():
        try:
            d = json.loads(OUT.read_text(encoding="utf-8"))
            return d.get("trend") or []
        except Exception as exc:
            logger.warning("读历史失败: %s", exc)
    return []


def save(rec: dict) -> None:
    trend = [r for r in _load_history() if r.get("date") != rec["date"]]  # 当天幂等覆盖
    trend.append(rec)
    trend.sort(key=lambda r: r["date"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"trend": trend}, ensure_ascii=False, indent=2), encoding="utf-8")


def _fmt_row(r: dict) -> str:
    us = r["markets"].get("US", {})
    u1 = us.get("1d", {}); u5 = us.get("5d", {})
    def cell(c):
        return f"{c.get('avg_alpha_pct', float('nan')):+.2f}%/{c.get('win_rate_pct', 0):.0f}%(n{c.get('n', 0)})" if c.get("n") else "—"
    return f"{r['date']}  US 1d {cell(u1)}  5d {cell(u5)}"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="每日记 alpha 走势")
    p.add_argument("--dry-run", action="store_true", help="只算不写")
    p.add_argument("--show", action="store_true", help="打印已攒走势")
    args = p.parse_args()

    if args.show:
        hist = _load_history()
        if not hist:
            print("暂无走势数据。")
        for r in hist:
            print(_fmt_row(r))
        return 0

    rec = compute()
    print(_fmt_row(rec))
    if args.dry_run:
        print("[dry-run] 未写入")
        return 0
    save(rec)
    print(f"已记入 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
