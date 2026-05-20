"""每日跨源可信度审计 job。

读 SNAPSHOT_DIR 下最新的：
  - 价格快照（fetch_stock_prices.py 的 prices_*.json）
  - enrichment 快照（jobs.enrich_watchlist 的 watchlist_*.json）
  - 13F 快照（jobs.refresh_13f）

→ 调 core.audit 对每只股票审计 → 写飞书「数据可信度」「双源验证」字段。

CLI:
  python3 -m stock_research.jobs.daily_audit
  python3 -m stock_research.jobs.daily_audit --code NVDA
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import config
from ..core import audit, edgar
from ..adapters import legacy_shim as feishu, store

logger = logging.getLogger("stock_research.jobs.daily_audit")


# ────────────────────────────────────────────────────────
# 数据加载
# ────────────────────────────────────────────────────────

def _load_latest_prices() -> dict[str, dict[str, Any]]:
    """读 fetch_stock_prices.py 落的最新 prices_*.json 快照。

    2026-05-20: 快照已挪到 data/snapshots/prices/；优先新路径，兼容旧根目录文件。
    """
    snapshot_dir = Path(config.BASE_DIR) / "data" / "snapshots" / "prices"
    files = sorted(snapshot_dir.glob("prices_*.json"), reverse=True) if snapshot_dir.exists() else []
    if not files:
        files = sorted(Path(config.BASE_DIR).glob("prices_*.json"), reverse=True)
    if not files:
        return {}
    with open(files[0], encoding="utf-8") as f:
        rows = json.load(f) or []
    return {(r.get("code") or "").upper(): r for r in rows}


def _load_latest_enrichment() -> dict[str, dict[str, Any]]:
    rows = store.load_latest_json(config.ENRICH_DIR, "watchlist") or []
    if not isinstance(rows, list):
        return {}
    return {(r.get("code") or "").upper(): r for r in rows}


def _load_13f_signals_by_ticker() -> dict[str, list[dict[str, Any]]]:
    snaps = []
    if not config.SEC_13F_DIR.exists():
        return {}
    for cik_dir in config.SEC_13F_DIR.iterdir():
        if cik_dir.is_dir():
            s = store.load_latest_json(cik_dir, "snapshot")
            if s:
                snaps.append(s)
    return edgar.aggregate_signals_by_ticker(snaps)


# ────────────────────────────────────────────────────────
# 审计
# ────────────────────────────────────────────────────────

def run_audit(only_code: str | None = None) -> dict[str, Any]:
    prices = _load_latest_prices()
    enrich = _load_latest_enrichment()
    sec_signals = _load_13f_signals_by_ticker()

    watchlist = feishu.fetch_watchlist()
    audits = []
    updates = []
    for w in watchlist:
        code = (w["normalized"]["code"] or "").upper()
        if not code:
            continue
        if only_code and only_code.upper() != code:
            continue

        yf_data = prices.get(code)
        ak_data = enrich.get(code)  # 内含 akshare/finnhub/trends/baostock 子结构
        finnhub_data = enrich.get(code)
        baostock_data = (enrich.get(code) or {}).get("baostock")
        sec = sec_signals.get(code, [])

        result = audit.audit_stock(
            yf_data=yf_data,
            akshare_data=ak_data,
            finnhub_data=finnhub_data,
            sec_signals=sec,
            baostock_data=baostock_data,
            ticker=code,
        )
        result["name"] = w["normalized"]["name"]
        audits.append(result)

        # 写飞书
        cred_label = result.get("credibility_label", "")
        # 数据可信度是单选字段；要求选项已存在。失败也不阻塞。
        cred_select = None
        if "🟢" in cred_label:
            cred_select = "🟢 高（多权威源一致）"
        elif "🟡" in cred_label:
            cred_select = "🟡 中（权威媒体单源）"
        elif "🔴" in cred_label or "⚠️" in cred_label:
            cred_select = "🔴 低（仅二手聚合）"

        # 双源验证
        if result["source_count"] >= 2:
            dual = "✅ 双源（≥2 个来源）"
        elif result["source_count"] == 1:
            dual = "⚠️ 单源（仅 1 个来源）"
        else:
            dual = "❓ 待补"

        # 2026-05-11 PM 第二轮:飞书 100% 退役 → 直接 UPDATE DuckDB watchlist.
        db_fields: dict[str, Any] = {"verification": dual}
        if cred_select:
            db_fields["credibility"] = cred_select
        updates.append({"code": code, "fields": db_fields})

    store.save_json(audits, config.AUDIT_DIR, "audit")

    import sys as _sys
    _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "scripts" / "lib"))
    from stock_db import update_watchlist_fields as _update_wl
    db_updated = sum(_update_wl(u["code"], u["fields"]) for u in updates)
    summary = {"success": db_updated, "failed": len(updates) - db_updated}

    # 跨市场风险快照（SPY × CSI300 相关性 + USDCNY 敞口）
    # 单独跑：python3 -m stock_research.jobs.cross_market_risk
    if not only_code:
        try:
            from . import cross_market_risk as cmr_job
            print("\n[cross-market] 算 SPY × CSI300 相关性 + USDCNY 敞口 ...")
            cmr_job.run()
        except Exception as e:
            logger.warning("cross_market_risk failed: %s", e)

    # 控制台简报
    bucket = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "CONFLICT": 0}
    for a in audits:
        bucket[a.get("credibility", "LOW")] = bucket.get(a.get("credibility", "LOW"), 0) + 1
    print(f"\n[audit] {len(audits)} 只股票审计完成")
    print(f"  🟢 高 {bucket['HIGH']} · 🟡 中 {bucket['MEDIUM']} · 🔴 低 {bucket['LOW']} · ⚠️ 冲突 {bucket['CONFLICT']}")
    conflicts = [a for a in audits if a.get("credibility") == "CONFLICT"]
    if conflicts:
        print(f"  ⚠️ 冲突标的：")
        for a in conflicts:
            print(f"    - {a.get('name')} ({a['ticker']}): {a['summary']}")
    print(f"  飞书写入 {summary['success']} 成功 / {summary['failed']} 失败")
    return {
        "audited": len(audits),
        "buckets": bucket,
        "conflicts": [a["ticker"] for a in conflicts],
        **summary,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    p = argparse.ArgumentParser(description="Cross-source credibility audit")
    p.add_argument("--code", help="只审计某只股票")
    args = p.parse_args()
    run_audit(only_code=args.code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
