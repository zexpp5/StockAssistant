"""SEC EDGAR 13F 刷新 job：拉所有跟踪机构最新 13F → 与 watchlist 交叉 → 写飞书。

CLI 入口：
  python3 -m stock_research.jobs.refresh_13f             # 默认全量 + 交叉
  python3 -m stock_research.jobs.refresh_13f --refresh   # 只拉，不交叉
  python3 -m stock_research.jobs.refresh_13f --crossref  # 只交叉（用本地缓存）

Web 部署时直接 import：
  from stock_research.jobs.refresh_13f import run_refresh_all, run_crossref
"""
from __future__ import annotations
import argparse
import logging
import sys
from typing import Any

from .. import config
from ..core import edgar
from ..adapters import legacy_shim as feishu, store

logger = logging.getLogger("stock_research.jobs.refresh_13f")


def run_refresh_all() -> dict[str, Any]:
    """对每个 INVESTORS_13F 跑一次 get_investor_changes，存 JSON 快照。"""
    summary = {"investors": [], "failures": []}
    for name, cik in config.INVESTORS_13F.items():
        try:
            print(f"[13F] {name}  CIK={cik}")
            snap = edgar.get_investor_changes(name, cik)
            if not snap:
                summary["failures"].append(name)
                continue
            latest = snap.get("latest_filing") or {}
            print(f"  · 最新 {latest.get('report_date')} 数据 / 公布于 {latest.get('filing_date')} · "
                  f"{snap['holdings_count_latest']} 只持仓")
            store.save_json(
                snap,
                config.SEC_13F_DIR / cik,
                f"snapshot_{latest.get('report_date', 'unknown')}",
            )
            summary["investors"].append({
                "name": name,
                "cik": cik,
                "report_date": latest.get("report_date"),
                "holdings_count": snap["holdings_count_latest"],
            })
        except Exception as e:
            logger.exception("refresh failed for %s", name)
            summary["failures"].append(f"{name}: {e}")
    return summary


def _load_all_latest_snapshots() -> list[dict[str, Any]]:
    """从 SEC_13F_DIR/<cik>/ 各取最新一份。"""
    snaps = []
    if not config.SEC_13F_DIR.exists():
        return snaps
    for cik_dir in config.SEC_13F_DIR.iterdir():
        if not cik_dir.is_dir():
            continue
        latest = store.load_latest_json(cik_dir, "snapshot")
        if latest:
            snaps.append(latest)
    return snaps


def run_crossref() -> dict[str, Any]:
    """读所有快照 → 按 ticker 聚合 → 写飞书 INSTITUTIONAL_13F 字段。"""
    snaps = _load_all_latest_snapshots()
    if not snaps:
        print("[13F] 无快照，先跑 --refresh")
        return {"updated": 0}
    by_ticker = edgar.aggregate_signals_by_ticker(snaps)
    print(f"[13F] 聚合后 {len(by_ticker)} 只股票有信号")

    # 13F 信号已落 snapshots(category='13f/...') + track_13f.json,dashboard 直接读那两个源.
    # 2026-05-20 V2 cutover：交叉对象从 V1 watchlist 改为 V2 system_universe（系统科技/AI 池）。
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "scripts" / "lib"))
    from stock_db import fetch_universe_for_ai_recommendations
    universe = fetch_universe_for_ai_recommendations()
    matched = 0
    for u in universe:
        code = str(u.get("symbol") or "").upper().strip()
        if not code:
            continue
        signals = by_ticker.get(code)
        if not signals:
            continue
        matched += 1
        print(f"  · {u.get('name') or code} ({code}): {len(signals)} 条信号")

    print(f"[13F] system_universe 命中 {matched} 只 (信号已在 snapshots / track_13f.json)")
    return {
        "snapshots_used": len(snaps),
        "tickers_with_signals": len(by_ticker),
        "universe_matched": matched,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    p = argparse.ArgumentParser(description="SEC EDGAR 13F refresh + crossref")
    p.add_argument("--refresh", action="store_true", help="只刷新快照")
    p.add_argument("--crossref", action="store_true", help="只交叉（用本地快照）")
    args = p.parse_args()

    do_refresh = args.refresh or not (args.refresh or args.crossref)
    do_crossref = args.crossref or not (args.refresh or args.crossref)

    if do_refresh:
        s = run_refresh_all()
        print(f"\n[refresh] 成功 {len(s['investors'])} 家 / 失败 {len(s['failures'])}")
        for f in s["failures"]:
            print(f"  ❌ {f}")

    if do_crossref:
        run_crossref()

    return 0


if __name__ == "__main__":
    sys.exit(main())
