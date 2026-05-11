"""跨市场风险监控 job — 算 SPY × CSI300 相关性 + USD/CNY 敞口。

输入：
  默认从 plan_a_v5_constrained.json（约束后方案）读组合
  若不存在，回退到 plan_a_v5.json
  若都不存在，仅算市场层指标（相关性 + 汇率）

输出：
  1. 控制台报告
  2. data/snapshots/audit/cross_market_risk_<date>_<time>.json

CLI:
  python3 -m stock_research.jobs.cross_market_risk
  python3 -m stock_research.jobs.cross_market_risk --plan some_plan.json
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from stock_research import config
from stock_research.core import cross_market_risk
from stock_research.adapters import store

logger = logging.getLogger("stock_research.jobs.cross_market_risk")


def run(plan_path: str | None = None, base_currency: str = "CNY") -> dict:
    repo = Path(_REPO_ROOT)
    plan = None
    used_path = None
    candidates = []
    if plan_path:
        candidates = [Path(plan_path)]
    else:
        candidates = [repo / "plan_a_v5_constrained.json", repo / "plan_a_v5.json"]
    for p in candidates:
        if p.exists():
            try:
                plan = json.loads(p.read_text(encoding="utf-8"))
                used_path = p
                break
            except Exception as e:
                logger.warning("failed to load %s: %s", p, e)

    print(f"\n🌐 跨市场风险监控")
    print(f"  Plan 输入：{used_path or '无（仅算市场层）'}")
    print(f"  基准货币：{base_currency}")
    print()
    print("[拉数据] SPY (yfinance) + CSI300 (akshare) + USDCNY (yfinance) ...")

    payload = cross_market_risk.compute_cross_market_risk(plan, base_currency=base_currency)
    print()
    print(cross_market_risk.format_report(payload))

    out_path = store.save_json(payload, config.AUDIT_DIR, "cross_market_risk")
    print(f"\n  已保存：{out_path}")
    return payload


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    parser = argparse.ArgumentParser(description="跨市场相关性 + USDCNY 敞口监控")
    parser.add_argument("--plan", default=None,
                        help="组合 JSON 路径（默认 plan_a_v5_constrained.json）")
    parser.add_argument("--base-currency", default="CNY", choices=["CNY", "USD"])
    args = parser.parse_args()
    run(plan_path=args.plan, base_currency=args.base_currency)
    return 0


if __name__ == "__main__":
    sys.exit(main())
