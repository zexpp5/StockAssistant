"""当日 picks 横截面审查 job：Risk Parity + 估值理性 + Markowitz 相关性。

与已有的 jobs/daily_audit.py（跨源价格/13F 一致性）+ reverse_validate v6（时间维度回测）互补。

CLI:
  python3 -m stock_research.jobs.audit_picks
  python3 -m stock_research.jobs.audit_picks --no-correlation   # 跳过 yfinance 相关性
  python3 -m stock_research.jobs.audit_picks --fast             # 同上
"""
from __future__ import annotations
import argparse
import logging
import sys
from datetime import datetime
from typing import Any

from .. import config
from ..adapters import legacy_shim as feishu, store
from ..core import picks_audit

logger = logging.getLogger("stock_research.jobs.audit_picks")


def _select_today(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """优先取今日入选；今日无则取最近一次入选日的所有 picks。"""
    today_ts = int(datetime.combine(datetime.now().date(), datetime.min.time()).timestamp() * 1000)
    today = [p for p in picks if (p["normalized"].get("pick_date") or 0) >= today_ts]
    if today:
        return today
    if not picks:
        return []
    latest_ts = max((p["normalized"].get("pick_date") or 0 for p in picks), default=0)
    return [p for p in picks if (p["normalized"].get("pick_date") or 0) == latest_ts]


def run(skip_correlation: bool = False) -> dict[str, Any]:
    logger.info("fetching watchlist + picks...")
    watchlist = feishu.fetch_watchlist()
    picks = feishu.fetch_picks()

    picks_today = _select_today(picks)
    logger.info("watchlist=%d picks=%d picks_today=%d",
                len(watchlist), len(picks), len(picks_today))

    strong_count = len(picks_audit.filter_strong_picks(picks_today))
    result = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "picks_today_count": len(picks_today),
        "strong_picks_count": strong_count,
        "theme_concentration": picks_audit.theme_concentration(picks_today),
        "valuation_sanity": picks_audit.valuation_sanity(picks_today, watchlist),
        "correlation": (
            {"status": "skip", "reason": "用户指定 --no-correlation"}
            if skip_correlation
            else picks_audit.correlation_matrix(picks_today)
        ),
    }

    print_report(result)
    store.save_json(result, config.AUDIT_DIR, "picks_audit")
    return result


def print_report(r: dict[str, Any]) -> None:
    print(f"\n{'═' * 72}")
    print(f"  📋 当日 picks 横截面审查 · {r['ts']}")
    print(f"     当日 picks 共 {r['picks_today_count']} 只 · 其中 ⭐⭐⭐ 强推荐 {r.get('strong_picks_count', 0)} 只")
    print(f"{'═' * 72}\n")

    tc = r["theme_concentration"]
    print("【1/3 主题集中度（Risk Parity）】")
    if tc.get("status") == "ok":
        print(f"  {tc['verdict']}")
        for d in tc["distribution"]:
            bar = "█" * int(d["pct"] / 5)
            print(f"    {d['theme']:<24} {d['n']:>2} 只 {d['pct']:>5.1f}% {bar}")
    else:
        print(f"  跳过：{tc.get('reason')}")
    print()

    v = r["valuation_sanity"]
    print("【2/3 估值合理性】")
    if v["warn_count"] == 0:
        print("  🟢 当日 ⭐⭐⭐ 推荐估值均在合理范围")
    else:
        print(f"  ⚠️ {v['warn_count']} 只 ⭐⭐⭐ 推荐有估值警告：")
        for w in v["warnings"]:
            print(f"    · {w['name']} ({w['code']}): {' / '.join(w['flags'])}")
    print()

    c = r["correlation"]
    print("【3/3 相关性矩阵（Markowitz）】")
    if c.get("status") == "ok":
        print(f"  分析 {c['n_tickers']} 只 ⭐⭐⭐ 推荐过去 6 个月日收益相关性")
        print(f"  相关 > {c['threshold']}（伪分散对）：{len(c['high_corr_pairs'])} 对")
        for p in c["high_corr_pairs"][:10]:
            print(f"    · {p['name_a']} ↔ {p['name_b']}: r = {p['r']}")
    else:
        print(f"  跳过：{c.get('reason')}")
    print(f"{'═' * 72}\n")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    p = argparse.ArgumentParser(description="当日 picks 横截面审查（Risk Parity + 估值 + Markowitz）")
    p.add_argument("--no-correlation", action="store_true", help="跳过相关性矩阵（避免 yfinance 慢）")
    p.add_argument("--fast", action="store_true", help="同 --no-correlation")
    args = p.parse_args()
    run(skip_correlation=args.no_correlation or args.fast)
    return 0


if __name__ == "__main__":
    sys.exit(main())
