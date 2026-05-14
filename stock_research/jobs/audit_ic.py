"""因子 IC 监测 job：用历史数据算每个因子的滚动 IC，告警衰减。

数据源：复用 walk_forward_validate.py 的 6 个 regime 数据，对每个 regime：
  - factors:    在 regime 起点用 calc_factors_at 算 12-1 动量 + 1 月反转；
                其他生产因子若历史点不可得，会显式产出 no_data，供 gate 阻断。
  - forward:    regime 起点 → 终点的实际收益率

输出：
  1. 控制台报告（每个因子 mean IC / IR / hit rate / 状态）
  2. data/snapshots/audit/factor_ic_<date>_<time>.json

CLI:
  python3 -m stock_research.jobs.audit_ic
  python3 -m stock_research.jobs.audit_ic --factors f_score momentum reversal pead analyst quality
"""
from __future__ import annotations
import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "archive" / "legacy"))

from .. import config
from ..core import factor_ic
from ..adapters import store

logger = logging.getLogger("stock_research.jobs.audit_ic")

PRODUCTION_FACTOR_NAMES = ["f_score", "momentum", "reversal", "pead", "analyst", "quality"]


def _build_history_from_walkforward(samples: list[str], regimes: list[tuple]):
    """对每个 regime，build (factors, forward_returns) 元组。

    复用 walk_forward_validate 的 calc_factors_at + forward_return 函数。
    """
    from walk_forward_validate import calc_factors_at, forward_return

    history = []
    for start, end, label in regimes:
        factors_t = {}  # {ticker: {factor_name: score}}
        forwards_t = {}
        for tk in samples:
            mom, rev = calc_factors_at(tk, start)
            if mom is None:
                continue
            fwd = forward_return(tk, start, end)
            if fwd is None:
                continue
            factors_t[tk] = {"momentum": mom, "reversal": rev}
            forwards_t[tk] = fwd
        if factors_t:
            history.append((factors_t, forwards_t))
            logger.info("[%s] %s → %s: %d valid samples", label, start, end, len(factors_t))
    return history


def run(factor_names: list[str] | None = None) -> dict:
    if factor_names is None:
        factor_names = list(PRODUCTION_FACTOR_NAMES)

    print("=" * 80)
    print("  📊 因子 IC 监测（Grinold-Kahn 行业标准）")
    print("=" * 80)
    print(f"\n  监测因子: {factor_names}")
    print(f"  方法: Spearman 排序相关 IC（Grinold 1994）")

    from walk_forward_validate import SAMPLES, REGIMES
    print(f"  样本: {len(SAMPLES)} 只 × {len(REGIMES)} 个 regime")

    print("\n[1/2] 拉取每个 regime 的因子分数 + 前向收益...")
    history = _build_history_from_walkforward(SAMPLES, REGIMES)
    if not history:
        print("  ❌ 无可用数据")
        return {"error": "no_data"}
    print(f"  ✅ {len(history)} 个 regime 有数据")

    print("\n[2/2] 算每个因子的滚动 IC + 摘要 + 告警...")
    audit = factor_ic.audit_factors(history, factor_names)

    # 报告
    print(f"\n{'='*80}")
    print(f"  因子状态（按 mean IC 降序）")
    print(f"{'='*80}")
    print(f"\n  {'因子':<12}{'mean IC':>10}{'IC IR':>8}{'hit rate':>10}{'状态':<8}{'诊断'}")
    print(f"  {'-'*78}")
    for fname, _ in audit["ranking"]:
        info = audit["factors"][fname]
        s = info["summary"]
        a = info["alert"]
        print(f"  {fname:<12}{s.get('mean_ic', 0):>+9.3f}{s.get('ic_ir', 0):>+8.2f}"
              f"{s.get('hit_rate', 0):>9.0%}  {a.get('icon', '?')}{a.get('status', ''):<7}"
              f"{a.get('verdict', '')}")

    # 历史 IC 时序
    print(f"\n  各 regime IC 明细（按因子）:")
    from walk_forward_validate import REGIMES as _R
    regime_labels = [r[2] for r in _R][:len(history)]
    for fname in factor_names:
        ic_hist = audit["factors"].get(fname, {}).get("ic_history", [])
        print(f"\n  {fname}:")
        for label, rec in zip(regime_labels, ic_hist):
            ic_val = rec.get("ic")
            mark = "🟢" if ic_val and ic_val > 0.05 else ("🟡" if ic_val and ic_val > 0 else "🔴")
            ic_str = f"{ic_val:+.3f}" if ic_val is not None and ic_val == ic_val else "  N/A"
            print(f"    {mark} {label:<35} IC = {ic_str}  (n={rec.get('n', 0)})")

    print(f"\n{'='*80}\n")

    # 写快照
    audit["generated_at"] = datetime.now().isoformat(timespec="seconds")
    store.save_json(audit, config.AUDIT_DIR, "factor_ic")
    return audit


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    p = argparse.ArgumentParser(description="因子 IC 监测（Grinold-Kahn 行业标准）")
    p.add_argument("--factors", nargs="*", default=PRODUCTION_FACTOR_NAMES,
                   help="要监测的因子名")
    args = p.parse_args()
    r = run(factor_names=args.factors)
    return 0 if "error" not in r else 1


if __name__ == "__main__":
    sys.exit(main())
