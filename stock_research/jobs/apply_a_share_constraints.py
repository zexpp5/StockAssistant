"""对 plan_a_v5.json 应用 A 股实战约束（后处理，不改动 build_plan_a_v5.py）。

为什么后处理而不修改原文件：
  build_plan_a_v5.py 是核心 Markowitz 优化脚本，与 v6 学术因子体系强耦合。
  侵入式修改容易引入 regression。后处理路径：
    输入: plan_a_v5.json （Markowitz 优化结果）
    输出: plan_a_v5_constrained.json （应用 A 股约束后的最终方案）

应用的约束（仅对 A 股标的）：
  1. **可买性硬过滤**：ST/涨停/停牌的 A 股权重清零，溢出权重转入现金
  2. **流动性约束**：单日交易额 ≤ 20 日均成交额 × 3%（cap_by_volume_cn）
  3. **事件风险加权**：7 日内大额解禁 / 30 日内减持的标的，权重 × event_risk_score
  4. **报告**：所有调整列出原因（哪条约束触发了什么变化）

设计原则：
  - 不接触美股权重（保持 Markowitz 结果不变）
  - A 股调整产生的"溢出权重"统一加到现金
  - 输出表格让用户可肉眼审计每一笔调整
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from stock_research.core.a_share_filters import (
    fetch_spot_snapshot, filter_tradable, _strip_code,
)
from stock_research.core.event_calendar import build_calendar

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def is_a_share(ticker: str) -> bool:
    """识别 A 股 ticker（6 位数字，带不带 .SS/.SZ 后缀都行）。"""
    s = ticker.upper().replace(".SS", "").replace(".SZ", "").replace(".BJ", "")
    return s.isdigit() and len(s) == 6


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(REPO / "plan_a_v5.json"))
    parser.add_argument("--output", default=str(REPO / "plan_a_v5_constrained.json"))
    parser.add_argument("--max-volume-pct", type=float, default=0.03,
                        help="单日成交占 20 日均成交额上限（默认 3%）")
    args = parser.parse_args()

    inp = Path(args.input)
    if not inp.exists():
        print(f"❌ 找不到输入: {inp}")
        return 1

    plan = json.loads(inp.read_text(encoding="utf-8"))
    plan_v5 = plan.get("plan_v5", [])
    if not plan_v5:
        print("⚠️ plan_a_v5.json 无 plan_v5 字段")
        return 1

    # 区分 A 股 / 非 A 股
    a_share_entries = [p for p in plan_v5 if is_a_share(p["ticker"])]
    other_entries = [p for p in plan_v5 if not is_a_share(p["ticker"])]

    print(f"📊 后处理 plan_a_v5.json — {datetime.now():%Y-%m-%d %H:%M}")
    print(f"  原始组合：A 股 {len(a_share_entries)} 只 / 其他 {len(other_entries)} 只")

    if not a_share_entries:
        print("  无 A 股仓位，无需处理。直接复制原方案。")
        Path(args.output).write_text(inp.read_text(encoding="utf-8"), encoding="utf-8")
        return 0

    # 抓全 A 股 spot 快照
    print(f"\n[1/3] 抓 A 股 spot 快照...")
    snapshot = fetch_spot_snapshot()
    if snapshot is None:
        print("  ⚠️ 快照不可用 — 无法做 ST/涨停过滤，仅做事件风险调整")

    # 抓事件日历
    print(f"[2/3] 构建事件日历...")
    cal = build_calendar(horizon_unlock_days=30, horizon_insider_days=30,
                         include_earnings=False)
    print(f"  {len(cal.events)} 条事件")

    # 应用过滤
    print(f"\n[3/3] 应用约束...")
    print(f"\n  {'代码':<10}{'原仓位':>10}{'调整后':>10}{'变化':>10}  原因")
    print(f"  {'-'*78}")

    adjusted: list[dict] = []
    spillover = 0.0    # 因约束被砍下来的权重，转入现金

    for p in a_share_entries:
        orig_w = p.get("v5_weight", 0.0)
        code6 = _strip_code(p["ticker"])
        reasons: list[str] = []
        new_w = orig_w

        # 约束 1: 可买性硬过滤
        if snapshot is not None:
            tradable_codes, blocked = filter_tradable([code6], snapshot,
                                                       allow_st=False,
                                                       allow_limit_up=False,
                                                       allow_suspended=False)
            if code6 not in tradable_codes:
                reasons.append("/".join(blocked.get(code6, ["无快照"])))
                new_w = 0.0

        # 约束 2: 事件风险加权（仅当还有仓位时）
        if new_w > 0 and cal.events:
            risk = cal.risk_score(code6)
            if risk < 1.0:
                upcoming = cal.upcoming(code6, horizon_days=7)
                event_desc = ("/".join(e.event_type for e in upcoming[:2])
                              or "近期事件")
                reasons.append(f"事件风险 ×{risk:.2f} ({event_desc})")
                new_w = new_w * risk

        delta = new_w - orig_w
        spillover += orig_w - new_w
        if abs(delta) > 1e-9:
            change_str = f"{delta*100:+.2f}pp"
        else:
            change_str = "0.00pp"

        reason_str = "; ".join(reasons) if reasons else "✓ 无调整"
        print(f"  {p['ticker']:<10}{orig_w*100:>+9.2f}%{new_w*100:>+9.2f}%{change_str:>10}  {reason_str}")

        new_p = dict(p)
        new_p["v5_weight"] = new_w
        new_p["original_weight"] = orig_w
        new_p["constraint_reasons"] = reasons
        adjusted.append(new_p)

    # 重新组合：调整后的 A 股 + 原始美股 + 现金（含 spillover）
    constraints_summary = {
        "n_a_share_blocked": sum(1 for a in adjusted if a["v5_weight"] == 0 and a["original_weight"] > 0),
        "n_a_share_reduced": sum(1 for a in adjusted if 0 < a["v5_weight"] < a["original_weight"]),
        "spillover_to_cash": round(spillover, 4),
    }

    print(f"\n  汇总：")
    print(f"    A 股被完全剔除：{constraints_summary['n_a_share_blocked']}")
    print(f"    A 股部分降权：  {constraints_summary['n_a_share_reduced']}")
    print(f"    转入现金的权重：{spillover*100:.2f}pp")

    out = dict(plan)
    out["plan_v5"] = adjusted + other_entries
    out["a_share_constraints_applied"] = True
    out["a_share_constraints_summary"] = constraints_summary
    out["a_share_constraints_at"] = datetime.now().isoformat()
    out["original_plan_v5"] = plan_v5

    Path(args.output).write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n✅ 输出: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
