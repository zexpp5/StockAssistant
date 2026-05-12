"""对 plan_a_v5.json 应用 A 股实战约束（后处理，不改动 build_plan_a_v5.py）。

为什么后处理而不修改原文件：
  build_plan_a_v5.py 是核心 Markowitz 优化脚本，与 v6 学术因子体系强耦合。
  侵入式修改容易引入 regression。后处理路径：
    输入: plan_a_v5.json （Markowitz 优化结果）
    输出: plan_a_v5_constrained.json （应用 A 股约束后的最终方案）

应用的约束（仅对 A 股标的）：
  1. **可买性硬过滤**：ST/涨停/停牌的 A 股权重清零，溢出权重转入现金
  2. **流动性约束（v2 收紧）**：单日成交 ≤ 20 日均成交额 × 1.5%（主板）/ 1.0%（创业/科创/小盘）
     —— Almgren-Chriss 2001 经验值：>1.5% 即开始吃 ≥30bps 冲击成本
     —— 旧版 3% 仅适合机构盘前撮合 / 大流动性龙头
  3. **事件风险加权**：7 日内大额解禁 / 30 日内减持的标的，权重 × event_risk_score
  4. **T+1 警示（v2 新增）**：标注首次入仓的标的为"T+1 锁仓"，下日不可减仓；
     给出"如果明日 plan 又变化"的风险提示
  5. **涨跌停 follow-up（v2 新增）**：被硬过滤清零的标的写入 followup_pending 列表，
     下日 pipeline 可读取此列表自动复评 → 避免"涨停日永远买不到"负 α
  6. **报告**：所有调整列出原因（哪条约束触发了什么变化）

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
    fetch_spot_snapshot, filter_tradable, _strip_code, _classify_board,
)
from stock_research.core.event_calendar import build_calendar
from stock_research.core import portfolio_constraints as pc


# v2 收紧后的 ADV 上限（Almgren-Chriss 2001 经验值）
ADV_CAP_MAIN = 0.015          # 主板：1.5%
ADV_CAP_GROWTH = 0.010        # 创业板/科创板/北交所：1.0%（流动性差 + 个股波动大）

# A 股交易成本（双边平均，单位 bps）
#   印花税 10 bps（卖出收）/2 = 5  +  券商佣金 2.5（双边）+ 过户费 0.1（沪市过户）
#   双边汇总约 12-13 bps；冲击成本 30 bps / 1% ADV（小盘 conservative）
A_SHARE_COST_BPS = 13.0
A_SHARE_IMPACT_BPS_PER_PCT_ADV = 30.0
A_SHARE_NOTIONAL_YUAN = 500_000.0  # 与 ADV cap 段用同一假定


def _adv_cap_for_board(code: str) -> float:
    board, _ = _classify_board(code)
    if board in ("chinext", "star", "bse"):
        return ADV_CAP_GROWTH
    return ADV_CAP_MAIN

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def is_a_share(ticker: str) -> bool:
    """识别 A 股 ticker（6 位数字，带不带 .SS/.SZ 后缀都行）。"""
    s = ticker.upper().replace(".SS", "").replace(".SZ", "").replace(".BJ", "")
    return s.isdigit() and len(s) == 6


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(REPO / "data" / "latest" / "plan_a_v5.json"))
    parser.add_argument("--output", default=str(REPO / "data" / "latest" / "plan_a_v5_constrained.json"))
    parser.add_argument("--max-volume-pct", type=float, default=None,
                        help="单日成交占 20 日均成交额上限（默认按板块：主板 1.5%% / 创业科创北交 1.0%%）")
    parser.add_argument("--followup-pending-out",
                        default=str(REPO / "followup_pending_a_share.json"),
                        help="被涨跌停清零的标的写入此文件，明日 pipeline 复评")
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
    spillover = 0.0      # 因约束被砍下来的权重，转入现金
    followup_pending: list[dict] = []   # 被涨跌停清零，待下日复评
    t1_locked_tickers: list[str] = []   # 首次入仓 → T+1 锁仓警示
    adv_capped_tickers: list[str] = []  # 被流动性 cap 截到的
    today = datetime.now().strftime("%Y-%m-%d")

    for p in a_share_entries:
        orig_w = p.get("v5_weight", 0.0)
        prev_w = p.get("current_weight", 0.0)
        code6 = _strip_code(p["ticker"])
        reasons: list[str] = []
        new_w = orig_w
        followup_reason = None      # 若非空，加入 followup_pending

        # 约束 1: 可买性硬过滤
        if snapshot is not None:
            tradable_codes, blocked = filter_tradable([code6], snapshot,
                                                       allow_st=False,
                                                       allow_limit_up=False,
                                                       allow_suspended=False)
            if code6 not in tradable_codes:
                block_str = "/".join(blocked.get(code6, ["无快照"]))
                reasons.append(block_str)
                # 仅"涨停"/"接近涨停"/"停牌"应该等明日复评（基本面无变化）；ST/退市不复评
                if any(k in block_str for k in ("涨停", "接近涨停", "停牌")):
                    followup_reason = block_str
                new_w = 0.0

        # 约束 2: 流动性约束（按板块的 ADV cap）
        if new_w > 0 and snapshot is not None:
            st = snapshot.by_code.get(code6)
            avg_amount = (st.amount if st and st.amount else 0.0)
            adv_cap = (args.max_volume_pct
                       if args.max_volume_pct is not None
                       else _adv_cap_for_board(code6))
            delta_w = new_w - prev_w
            # 假设总组合规模 = 1（权重口径），单日交易额按权重换算到"成交额"需 portfolio_value
            # 这里没 portfolio_value，退而用相对比：要求 |delta_w| × notional ≤ adv_cap × avg_amount
            # 若 avg_amount=0（数据缺失）跳过约束，避免误杀
            if avg_amount > 0:
                # 假定组合 notional = 500K（与 build_plan_a_v5 默认一致）
                notional = 500_000.0
                trade_amount = abs(delta_w) * notional
                cap_amount = avg_amount * adv_cap
                if trade_amount > cap_amount and cap_amount > 0:
                    cap_delta_w = (1 if delta_w > 0 else -1) * cap_amount / notional
                    capped_new_w = prev_w + cap_delta_w
                    reasons.append(
                        f"ADV cap {adv_cap:.1%}：Δ {delta_w*100:+.2f}pp → {cap_delta_w*100:+.2f}pp"
                    )
                    adv_capped_tickers.append(p["ticker"])
                    new_w = max(0.0, capped_new_w)

        # 约束 3: 事件风险加权（仅当还有仓位时）
        if new_w > 0 and cal.events:
            risk = cal.risk_score(code6)
            if risk < 1.0:
                upcoming = cal.upcoming(code6, horizon_days=7)
                event_desc = ("/".join(e.event_type for e in upcoming[:2])
                              or "近期事件")
                reasons.append(f"事件风险 ×{risk:.2f} ({event_desc})")
                new_w = new_w * risk

        # 约束 4: T+1 警示 — 从 0 仓位变成有仓位 → 明日不可减仓
        if prev_w <= 1e-9 and new_w > 0:
            t1_locked_tickers.append(p["ticker"])

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
        new_p["t1_locked"] = p["ticker"] in t1_locked_tickers
        adjusted.append(new_p)

        if followup_reason:
            followup_pending.append({
                "ticker": p["ticker"],
                "intended_weight": orig_w,
                "blocked_at": today,
                "reason": followup_reason,
                "f_score": p.get("f_score"),
                "composite_z": p.get("composite_z"),
            })

    # ── 约束 4.5: A 股内部行业上限 — P0-4b (2026-05-12) ──
    # 单行业 ≤ A 股部分的 40% 或整组合 25%（取较紧的）；溢出转现金
    # industry 映射从 watchlist 反查（A 股 watchlist 自带 industry 字段）
    try:
        sys.path.insert(0, str(REPO / "scripts" / "lib"))
        from stock_db import fetch_all_watchlist  # type: ignore
        wl_by_code = {r["code"]: r for r in fetch_all_watchlist()}
        industries_map_a = {
            a["ticker"]: (wl_by_code.get(_strip_code(a["ticker"]), {}).get("industry")
                          or "未分类")
            for a in adjusted
        }
    except Exception as e:
        logger.warning("watchlist 拉取失败，跳过 A 股行业上限: %s", e)
        industries_map_a = {}

    a_industry_summary: dict = {}
    if industries_map_a:
        a_share_total = sum(a["v5_weight"] for a in adjusted)
        # 单行业上限 = min(A 股部分 40%, 整组合 25%)，避免 A 股仓位本身就小时 cap 空转
        industry_cap_abs = min(0.40 * a_share_total, 0.25) if a_share_total > 0 else 0.25
        new_a_only = {a["ticker"]: a["v5_weight"] for a in adjusted}
        capped_a, a_industry_summary = pc.cap_by_industry(
            new_a_only, industries_map_a, max_industry_pct=industry_cap_abs,
        )
        # 把 cap 后权重写回 adjusted
        for a in adjusted:
            new_w = capped_a.get(a["ticker"])
            if new_w is not None and abs(new_w - a["v5_weight"]) > 1e-9:
                spillover += a["v5_weight"] - new_w
                a["v5_weight"] = new_w
                a["constraint_reasons"].append(
                    f"行业 cap (单行业 ≤ {industry_cap_abs:.1%})"
                )
        triggered_industries = [
            ind for ind, s in a_industry_summary.items()
            if s["original"] > s["capped"] + 1e-6
        ]
        if triggered_industries:
            print(f"\n  ⚠️ A 股行业上限触发（≤ {industry_cap_abs:.1%}）：")
            for ind in triggered_industries:
                s = a_industry_summary[ind]
                print(f"    · {ind}: {s['original']:.1%} → {s['capped']:.1%}"
                      f"（溢出 {s['overflow']:.1%}）")
        else:
            top_industry_pct = max((s["original"] for s in a_industry_summary.values()),
                                   default=0.0)
            print(f"\n  🟢 A 股行业上限检查通过（最高 {top_industry_pct:.1%}）")

    # ── 约束 5: A 股调仓交易成本扣减（印花税 + 佣金 + 过户费 + 冲击）──
    # 用 portfolio_constraints.apply_transaction_cost，参数走 A 股专属常量
    prev_a_weights = {p["ticker"]: p.get("current_weight", 0.0) for p in a_share_entries}
    new_a_weights = {a["ticker"]: a["v5_weight"] for a in adjusted}
    adv_dict_yuan: dict[str, float] = {}
    if snapshot is not None:
        for p in a_share_entries:
            code6 = _strip_code(p["ticker"])
            st = snapshot.by_code.get(code6)
            if st and st.amount:
                adv_dict_yuan[p["ticker"]] = float(st.amount)

    a_cost = pc.apply_transaction_cost(
        new_a_weights, prev_a_weights, A_SHARE_NOTIONAL_YUAN,
        adv_dollars=adv_dict_yuan,
        cost_bps=A_SHARE_COST_BPS,
        impact_bps_per_pct_adv=A_SHARE_IMPACT_BPS_PER_PCT_ADV,
    )

    # A-5 (2026-05-12)：把 industry / 单股成本明细 reverse-map 到 entries，
    # 让 dashboard / morning_brief 不用反查 constraints_summary 就能显示
    cost_by_ticker: dict[str, dict] = {}
    for b in a_cost.get("breakdowns", []):
        cost_by_ticker[b.ticker] = {
            "delta_weight_pp": round(b.delta_weight * 100, 3),
            "delta_yuan": round(b.delta_dollars, 2),
            "pct_of_adv": round(b.pct_of_adv, 2),
            "total_cost_bps": round(b.total_cost_bps, 2),
            "total_cost_yuan": round(b.total_cost_dollars, 2),
        }
    for a in adjusted:
        a["industry"] = industries_map_a.get(a["ticker"], "未分类")
        a["cost"] = cost_by_ticker.get(a["ticker"])  # 可能 None（无调仓的标的）

    # 重新组合：调整后的 A 股 + 原始美股 + 现金（含 spillover）
    constraints_summary = {
        "n_a_share_blocked": sum(1 for a in adjusted if a["v5_weight"] == 0 and a["original_weight"] > 0),
        "n_a_share_reduced": sum(1 for a in adjusted if 0 < a["v5_weight"] < a["original_weight"]),
        "n_adv_capped": len(adv_capped_tickers),
        "spillover_to_cash": round(spillover, 4),
        "t1_locked_tickers": t1_locked_tickers,
        "n_followup_pending": len(followup_pending),
        "a_share_cost": {
            "total_cost_yuan": round(a_cost["total_cost_dollars"], 2),
            "total_cost_bps_of_portfolio": round(a_cost["total_cost_bps_of_portfolio"], 2),
            "single_side_turnover": round(a_cost["turnover"], 4),
            "cost_bps": A_SHARE_COST_BPS,
            "impact_bps_per_pct_adv": A_SHARE_IMPACT_BPS_PER_PCT_ADV,
            "notional_yuan": A_SHARE_NOTIONAL_YUAN,
        },
        "a_share_industry_summary": a_industry_summary,
    }

    print(f"\n  汇总：")
    print(f"    A 股被完全剔除：{constraints_summary['n_a_share_blocked']}")
    print(f"    A 股部分降权：  {constraints_summary['n_a_share_reduced']}")
    print(f"    流动性 cap 截：{constraints_summary['n_adv_capped']}")
    print(f"    转入现金的权重：{spillover*100:.2f}pp")
    print(f"    A 股调仓成本：  ¥{a_cost['total_cost_dollars']:,.2f} "
          f"({a_cost['total_cost_bps_of_portfolio']:.1f} bps) · "
          f"单边换手 {a_cost['turnover']:.1%}")
    if t1_locked_tickers:
        print(f"    ⚠️ T+1 锁仓（明日不可卖）：{', '.join(t1_locked_tickers)}")
    if followup_pending:
        print(f"    📋 涨跌停 follow-up（明日复评）：{len(followup_pending)} 只 → {args.followup_pending_out}")

    out = dict(plan)
    out["plan_v5"] = adjusted + other_entries
    out["a_share_constraints_applied"] = True
    out["a_share_constraints_summary"] = constraints_summary
    out["a_share_constraints_at"] = datetime.now().isoformat()
    out["original_plan_v5"] = plan_v5

    Path(args.output).write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    # 写 followup_pending（明日 daily pipeline 可读取此文件，把这些 ticker 重新喂入选股）
    followup_path = Path(args.followup_pending_out)
    if followup_pending:
        followup_path.write_text(
            json.dumps({"generated_at": today, "items": followup_pending},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    elif followup_path.exists():
        # 清空昨日残留，避免日复一日累积
        followup_path.write_text(
            json.dumps({"generated_at": today, "items": []}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(f"\n✅ 输出: {args.output}")
    if followup_pending:
        print(f"   涨跌停 follow-up: {args.followup_pending_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
