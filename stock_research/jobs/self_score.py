"""系统自评分 job — 跑一次拿到 overall + 4 维度分。

数据源：
  - data/snapshots/audit/factor_ic_*.json     最新一份
  - data/snapshots/audit/stress_test_*.json   最新一份
  - data/reports/*.md                          fundamental_report 输出
  - data/snapshots/audit/                      整体新鲜度

输出：
  1. 控制台报告
  2. data/snapshots/audit/self_score_<date>_<time>.json

CLI:
  python3 -m stock_research.jobs.self_score
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from stock_research import config
from stock_research.core import self_score
from stock_research.adapters import store

logger = logging.getLogger("stock_research.jobs.self_score")


def _load_latest_json(audit_dir: Path, prefix: str) -> dict | None:
    matches = sorted(audit_dir.glob(f"{prefix}_*.json"), reverse=True)
    if not matches:
        return None
    with open(matches[0], encoding="utf-8") as f:
        return json.load(f)


def run() -> dict:
    audit_dir = config.AUDIT_DIR
    reports_dir = config.DATA_DIR / "reports"

    print(f"\n📊 系统自评分 — {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 72)

    factor_ic_snap = _load_latest_json(audit_dir, "factor_ic")
    stress_snap = _load_latest_json(audit_dir, "stress_test")
    print(f"  快照状态：factor_ic={'✓' if factor_ic_snap else '✗'}  "
          f"stress_test={'✓' if stress_snap else '✗'}  "
          f"reports={reports_dir.exists()}")

    result = self_score.compute_self_score(
        factor_ic_snapshot=factor_ic_snap,
        stress_test_snapshot=stress_snap,
        reports_dir=reports_dir,
        audit_dir=audit_dir,
    )
    print()
    print(self_score.format_report(result))

    payload = self_score.to_json(result)
    out_path = store.save_json(payload, audit_dir, "self_score")
    print(f"\n  已保存：{out_path}")

    return payload


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    parser = argparse.ArgumentParser(description="系统自评分（min-aggregation + veto）")
    parser.parse_args()
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
