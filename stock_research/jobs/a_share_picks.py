"""A 股每日优选 — 6 因子闭环  ✅ PRODUCTION（A 股）

**这是当前 A 股选股的主流水线。** daily_refresh.sh 第 21 步调本文件，
仅在收盘后（≥16:00 工作日 或 周末）执行，避免北向 T+1 / 龙虎榜盘后才出导致的脏数据。

美股不走这个 — 美股看 daily_picks_v5.py（学术因子 + 分析师上修）。
v1 的 daily_picks.py 是 LEGACY，仅保留作对照基线。

—— 以下原 docstring ——

把 6 个新模块串成闭环。

闭环数据流（输入 → 信号合成 → 过滤 → 排序）：

  1. 输入：飞书 watchlist 中的 A 股
  2. 信号合成（横截面 0-1 评分）：
     a. Piotroski F-Score (factor_model_china)         权重 0.25
     b. 12-1 月动量      (factor_model_china)          权重 0.15
     c. 1 月反转         (factor_model_china)          权重 0.10
     d. 龙虎榜机构净买入  (lhb_signals)                  权重 0.15
     e. 北向资金信号      (north_flow_signals)           权重 0.15
     f. PEAD 真实公告日   (event_calendar.pead_factor)   权重 0.10
     g. 政策主题受益      (policy_events.themes_tailwind) 权重 0.10
  3. 风险加权：event_calendar.risk_score (×0.0~1.0)
  4. 硬过滤：a_share_filters.filter_tradable (剔除 ST/涨停/停牌)
  5. 横截面排序 → top K → 写入 飞书 + JSON

输出：
  data/a_share_picks.json   完整结果（用于看板/streamlit）
  飞书"每日优选"表（与 v5 美股版并行写入，市场字段区分）

为什么不改 daily_picks_v5.py：
  v5 写死了 yfinance + is_us_ticker，硬塞 A 股会污染原有逻辑。
  这里做并行 job，daily_refresh.sh 同时调两个，互不干扰。
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from stock_research.core.a_share_filters import (
    fetch_spot_snapshot, filter_tradable, _strip_code,
)
from stock_research.core.lhb_signals import compute_lhb_factors
from stock_research.core.north_flow_signals import (
    compute_north_flow_signal, double_confirm_signal,
)
from stock_research.core.event_calendar import (
    build_calendar, pead_factor, EventCalendar,
)
from stock_research.core.policy_events import themes_under_policy_tailwind

# 复用现有 A 股因子模型（Piotroski + 动量 + 反转）
from factor_model_china import fetch_factors_a_share

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ─────────── 因子权重（合计 1.00）───────────
#
# ⚠️ 当前权重是启发式（拍脑袋）值，未经 IC 历史验证。建议：
#   1. 跑 stock_research.jobs.calibrate_pick_weights 做 IC-based 校准；
#   2. 输出落到 data/calibrated_factor_weights.json；
#   3. 本模块通过 load_weights() 优先读校准文件，无文件时 fallback 到启发式。
#
# 任何对启发式权重的临时调整必须经过 walk-forward backtest 验证 Sharpe > Equal-Weight + 0.3，
# 否则不应进 production（防止 overfitting watchlist 历史）。

DEFAULT_FACTOR_WEIGHTS = {
    "f_score":       0.25,   # Piotroski 财务质量（基本面）
    "momentum":      0.15,   # 12-1 月动量（趋势）
    "reversal":      0.10,   # 1 月反转（短期均值回归）
    "lhb":           0.15,   # 龙虎榜机构净买入（A 股短线 alpha）
    "north_flow":    0.15,   # 北向资金信号（外资偏好）
    "pead":          0.10,   # PEAD（业绩公告漂移）
    "policy_theme":  0.10,   # 政策主题受益
}
assert abs(sum(DEFAULT_FACTOR_WEIGHTS.values()) - 1.0) < 1e-9


def load_weights() -> tuple[dict[str, float], str]:
    """优先读 IC 校准结果，没有则用启发式默认值。

    返回 (weights, source) — source 用于报告里标注权重来源。
    """
    calib = REPO / "data" / "calibrated_factor_weights.json"
    if calib.exists():
        try:
            data = json.loads(calib.read_text(encoding="utf-8"))
            w = data.get("weights") if isinstance(data, dict) else None
            if isinstance(w, dict) and abs(sum(w.values()) - 1.0) < 1e-6:
                return w, f"ic_calibrated@{calib.name}"
            logger.warning("calibrated_factor_weights.json 存在但格式无效，回退到默认")
        except Exception as e:
            logger.warning("加载 calibrated_factor_weights.json 失败: %s", e)
    return DEFAULT_FACTOR_WEIGHTS, "heuristic_default"


# 兼容旧调用（外部 import FACTOR_WEIGHTS）
FACTOR_WEIGHTS = DEFAULT_FACTOR_WEIGHTS


@dataclass
class APickEntry:
    """单只 A 股优选结果。"""
    code: str
    name: str
    market: str = "A 股"
    industry: str = ""                       # 用于 sector cap

    # 子因子分（0-1）
    f_score_norm: float | None = None        # F-Score / 9
    momentum_norm: float | None = None       # 横截面分位
    reversal_norm: float | None = None       # 横截面分位
    lhb_score: float = 0.5
    north_score: float = 0.5
    pead_score: float = 0.5
    policy_boost: float = 0.0                # 政策受益主题加成（0-0.3）

    # 风险加权
    event_risk_score: float = 1.0            # 0-1，越低越避开

    # 综合
    composite: float = 0.0
    rank: int = 0
    recommended: bool = False

    # 拦截原因（可买性）
    tradable: bool = True
    block_reasons: list[str] = None

    # 备注
    notes: list[str] = None

    def to_dict(self):
        d = asdict(self)
        if d.get("notes") is None:
            d["notes"] = []
        if d.get("block_reasons") is None:
            d["block_reasons"] = []
        return d


# ─────────── watchlist 读取（兼容 daily_picks.py 的接口）───────────

def fetch_a_share_watchlist() -> list[dict]:
    """从飞书 watchlist 拉所有 A 股（市场字段含"A股"或代码 6 位纯数字）。"""
    from feishu_auth import feishu_token
    from daily_picks import fetch_watchlist
    token = feishu_token()
    records = fetch_watchlist(token)
    out = []
    for r in records:
        code = r.get("code", "")
        market = r.get("market", "") or ""
        is_a = (
            "A股" in market or "A 股" in market or
            "深交所" in market or "上交所" in market or
            "科创" in market or "北交" in market or
            (code.isdigit() and len(code) == 6)
        )
        if is_a:
            out.append(r)
    return out


# ─────────── 主流程 ───────────

def run_a_share_picks(top_k: int = 12, mode: str = "tertile",
                      dry_run: bool = False, theme_field: str = "industry",
                      sector_cap_count: int = 3,
                      require_after_close: bool = False):
    """主入口。

    参数：
      top_k                最多写入的优选数
      mode                 'tertile'（前 1/3）/ 'median'（前 1/2）/ 'quartile'（前 1/4）
      dry_run              仅打印不写飞书
      theme_field          watchlist 里用哪个字段做主题匹配（默认 industry）
      sector_cap_count     单 industry 在 top_k 内最多入选的股票数（默认 3，组合分散）
      require_after_close  仅在 A 股收盘后允许执行，盘前/盘中直接退出（避免脏数据）
                           触发条件：北向 T+1、龙虎榜盘后才出，未收盘信号都不可信
    """
    # 盘前/盘中守卫：北向 T+1 + LHB 盘后 + spot volume=0，跑出来全是脏数据
    if require_after_close and not _is_after_a_share_close():
        now = datetime.now()
        print(f"⛔ a_share_picks: 当前 {now:%Y-%m-%d %H:%M} 非 A 股收盘后时段（要求 ≥16:00），")
        print(f"   --require-after-close 设置下退出。北向 T+1、龙虎榜盘后发布、spot 盘前 volume=0。")
        print(f"   收盘后单跑：python -m stock_research.jobs.a_share_picks --dry-run")
        return 0

    print(f"\n📊 A 股每日优选 — {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 70)

    # 1. 拉 watchlist
    print("\n[1/7] 拉飞书 A 股 watchlist...")
    records = fetch_a_share_watchlist()
    print(f"  {len(records)} 只 A 股标的")
    if not records:
        print("  (无 A 股标的，退出)")
        return 0

    # 2. 抓全市场 spot 快照（一次性，用于过滤）
    print("\n[2/7] 抓 A 股全市场 spot 快照...")
    snapshot = fetch_spot_snapshot()
    if snapshot is None:
        print("  ⚠️ 快照获取失败 — 跳过 ST/涨停过滤（保守策略：所有标的 tradable=True）")
    else:
        print(f"  抓取 {snapshot.raw_count} 只全 A 股，分类完成")

    # 3. 抓 LHB / 北向 / 事件日历 (一次性)
    print("\n[3/7] 抓龙虎榜 (近 5 日) ...")
    codes = [r["code"] for r in records]
    lhb_factors = compute_lhb_factors(codes, lookback_days=5)
    print(f"  覆盖 {len(lhb_factors)} 只")

    print("\n[4/7] 构建事件日历（解禁/减持/财报）...")
    cal: EventCalendar = build_calendar(
        horizon_unlock_days=90, horizon_insider_days=60, include_earnings=True,
    )
    print(f"  {len(cal.events)} 条事件")

    print("\n[5/7] 扫描政策受益主题（最近 14 天）...")
    try:
        tailwind = themes_under_policy_tailwind(days=14, min_count=2)
    except Exception as e:
        logger.warning("policy scan failed: %s", e)
        tailwind = {}
    if tailwind:
        print(f"  受益主题：{', '.join(f'{t}({c})' for t, c in sorted(tailwind.items(), key=lambda x: -x[1])[:5])}")
    else:
        print(f"  无明显主题受益")

    # 4. 逐股算因子 + 信号 (北向是 per-stock API，无法批量；其他都已批量)
    print(f"\n[6/7] 逐股计算因子 ({len(records)} 只)...")
    entries: list[APickEntry] = []
    raw_metrics = []  # 用于横截面归一化
    for i, r in enumerate(records, 1):
        code = r["code"]
        name = r.get("name", code)
        print(f"  [{i:>2}/{len(records)}] {code} {name[:10]:<10}", end=" ", flush=True)

        # Piotroski + 动量 + 反转
        try:
            f_data = fetch_factors_a_share(code)
            f_score = f_data["piotroski"].get("f_score")
            mom = f_data["momentum"].get("momentum_12_1")
            rev = f_data["momentum"].get("reversal_1m")
        except Exception as e:
            f_score, mom, rev = None, None, None
            logger.debug("factor_model_china failed for %s: %s", code, e)

        # 北向（per-stock API，慢）
        try:
            n_sig = compute_north_flow_signal(code, lookback_days=20)
        except Exception as e:
            from stock_research.core.north_flow_signals import NorthFlowSignal
            n_sig = NorthFlowSignal(code=_strip_code(code), lookback_days=20, score=0.5, notes=[f"err: {e}"])
        time.sleep(0.3)  # akshare 限流

        # PEAD（用真实公告日）
        try:
            pead = pead_factor(code, cal)
        except Exception as e:
            pead = {"score": 0.5, "in_event_window": False}
            logger.debug("pead failed for %s: %s", code, e)

        # 政策主题加成（基于 watchlist 的 industry / theme 字段匹配）
        theme_text = (r.get(theme_field, "") or r.get("industry", "")
                      or r.get("ai_logic", "") or "")
        policy_boost = 0.0
        for theme, count in tailwind.items():
            if theme in theme_text:
                policy_boost = max(policy_boost, min(0.30, count * 0.05))

        # 事件风险（解禁/减持降权）
        event_risk = cal.risk_score(code) if cal.events else 1.0

        # 可买性
        if snapshot is not None:
            tradable_codes, blocked = filter_tradable([code], snapshot,
                                                      allow_st=False,
                                                      allow_limit_up=False,
                                                      allow_suspended=False)
            tradable = code in tradable_codes
            block_reasons = blocked.get(code, [])
        else:
            tradable, block_reasons = True, []

        # LHB
        lhb = lhb_factors.get(code)
        lhb_score = lhb.score if lhb else 0.5

        entry = APickEntry(
            code=_strip_code(code), name=name,
            market=r.get("market", "A 股") or "A 股",
            industry=(r.get("industry") or r.get(theme_field) or "")[:32],
            f_score_norm=(f_score / 9.0) if isinstance(f_score, (int, float)) else None,
            momentum_norm=mom,   # 暂存原值，后面横截面归一化
            reversal_norm=rev,
            lhb_score=lhb_score,
            north_score=n_sig.score,
            pead_score=pead.get("score", 0.5),
            policy_boost=policy_boost,
            event_risk_score=event_risk,
            tradable=tradable,
            block_reasons=block_reasons,
            notes=[],
        )
        entries.append(entry)
        raw_metrics.append((mom, rev))
        f_str = f"F={f_score}" if f_score is not None else "F=?"
        m_str = f"M={mom:+.0f}%" if isinstance(mom, (int, float)) else "M=?"
        flag = "✅" if tradable else "❌"
        print(f"{f_str:<6}{m_str:<9} LHB={lhb_score:.2f} N={n_sig.score:.2f} PEAD={pead.get('score', 0.5):.2f} {flag}")

    # 5. 横截面归一化 momentum / reversal — winsorize + rank
    # 原 min-max 对极值零防御（一只异常股拉爆 max，其他股全挤在 0 端）。
    # 改用 [1%, 99%] winsorize 截断尾部 → 横截面 percent rank → uniform [0,1]，机构标配。
    moms = [m for m, _ in raw_metrics]   # 含 None
    revs = [r for _, r in raw_metrics]
    mom_ranks = _winsorize_rank(moms)
    rev_ranks = _winsorize_rank(revs)

    weights, weights_source = load_weights()

    # 6. 合成综合分
    print(f"\n[7/7] 合成综合分（{len(entries)} 只，权重源={weights_source}）...")
    for idx, e in enumerate(entries):
        # 缺失（None）→ 0.5（中位补值），避免缺失变成 0 拉低
        e.momentum_norm = mom_ranks[idx] if mom_ranks[idx] is not None else 0.5
        e.reversal_norm = rev_ranks[idx] if rev_ranks[idx] is not None else 0.5

        composite = (
            weights["f_score"] * (e.f_score_norm if e.f_score_norm is not None else 0.5)
            + weights["momentum"] * e.momentum_norm
            + weights["reversal"] * e.reversal_norm
            + weights["lhb"] * e.lhb_score
            + weights["north_flow"] * e.north_score
            + weights["pead"] * e.pead_score
            + weights["policy_theme"] * (0.5 + e.policy_boost)  # 基础 0.5 + 加成
        )
        # 风险加权（解禁/减持等）
        composite *= e.event_risk_score
        e.composite = round(composite, 4)

    # 7. 排序 + 决策门槛 + 硬过滤
    entries.sort(key=lambda e: -e.composite)
    composites = [e.composite for e in entries if e.tradable]
    if not composites:
        print("  ⚠️ 无可买标的（全部被过滤）")
        return 0
    cutoff_map = {
        "quartile": _quantile(composites, 0.75),
        "tertile":  _quantile(composites, 2/3),
        "median":   _quantile(composites, 0.50),
    }
    cutoff = cutoff_map[mode]
    for i, e in enumerate(entries, 1):
        e.rank = i
        e.recommended = e.tradable and e.composite >= cutoff

    # 8. Sector cap：贪心选 top_k 时单 industry 不超过 sector_cap_count
    selected, sector_skipped = _select_with_sector_cap(
        entries, top_k=top_k, sector_cap_count=sector_cap_count,
    )

    print(f"\n  cutoff = {cutoff:.3f} (mode={mode})")
    print(f"  推荐 {len(selected)} / 可买 {sum(1 for e in entries if e.tradable)}"
          f" / 总 {len(entries)}")
    if sector_skipped:
        print(f"  sector cap 跳过 {len(sector_skipped)} 只（单 industry ≤ {sector_cap_count}）：")
        for e in sector_skipped[:5]:
            print(f"    · {e.code} {e.name[:8]:<8} [{e.industry}] composite={e.composite}")

    print(f"\n  {'排':<3}{'代码':<8}{'名称':<10}{'F分':>5}{'LHB':>6}{'北':>6}{'PEAD':>6}"
          f"{'政策':>6}{'风险':>6}{'综合':>7}  状态")
    print(f"  {'-'*78}")
    for e in entries[:30]:
        f_str = f"{e.f_score_norm*9:.0f}" if e.f_score_norm is not None else "?"
        flag = "✅" if e.recommended else ("❌" if not e.tradable else "  ")
        block = "/".join(e.block_reasons[:2]) if e.block_reasons else ""
        print(f"  {e.rank:<3}{e.code:<8}{e.name[:8]:<10}{f_str:>5}{e.lhb_score:>6.2f}"
              f"{e.north_score:>6.2f}{e.pead_score:>6.2f}{e.policy_boost:>6.2f}"
              f"{e.event_risk_score:>6.2f}{e.composite:>7.3f}  {flag} {block}")

    # 9. 写文件
    out = REPO / "data" / "a_share_picks.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(),
        "mode": mode,
        "cutoff": cutoff,
        "top_k": top_k,
        "sector_cap_count": sector_cap_count,
        "factor_weights": weights,
        "factor_weights_source": weights_source,
        "n_total": len(entries),
        "n_tradable": sum(1 for e in entries if e.tradable),
        "n_recommended": len(selected),
        "n_sector_capped": len(sector_skipped),
        "selected": [e.to_dict() for e in selected],
        "all_entries": [e.to_dict() for e in entries],
        "policy_tailwind": tailwind,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n✅ JSON: {out}")

    if dry_run:
        print("  (dry-run 模式，跳过飞书写入)")
        return 0

    # 10. 写飞书（复用 daily_picks_v5 的写入逻辑会复杂，先只写 JSON；
    #     未来可单独加 write_a_share_picks_to_feishu.py）
    print("  (飞书写入暂未启用 — 后续可加 write_a_share_picks_to_feishu.py)")
    return 0


# ─────────── 工具 ───────────

def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(q * (len(s) - 1))
    return s[idx]


def _winsorize_rank(values: list[float | None],
                    lo_pct: float = 0.01, hi_pct: float = 0.99
                    ) -> list[float | None]:
    """横截面归一化：[lo_pct, hi_pct] winsorize 截尾后做 percent-rank。

    比 min-max 稳健：单只异常股不会污染整个范围，缺失值保留 None（上层补 0.5）。
    返回与输入等长的 list，每个元素 ∈ [0,1] ∪ {None}。
    样本量 < 4 时无统计意义，全返 0.5（避免 watchlist 太小时归一化失真）。
    """
    valid = sorted([v for v in values if isinstance(v, (int, float))])
    n = len(valid)
    if n < 4:
        return [0.5 if isinstance(v, (int, float)) else None for v in values]

    lo_idx = int(lo_pct * (n - 1))
    hi_idx = int(hi_pct * (n - 1))
    lo, hi = valid[lo_idx], valid[hi_idx]

    # 截尾后的 sorted 池子（用于排名）
    pool = sorted(max(lo, min(hi, v)) for v in valid)
    pool_n = len(pool)

    out: list[float | None] = []
    for v in values:
        if not isinstance(v, (int, float)):
            out.append(None)
            continue
        clipped = max(lo, min(hi, v))
        below = sum(1 for x in pool if x < clipped)
        eq = sum(1 for x in pool if x == clipped)
        out.append((below + 0.5 * eq) / pool_n)
    return out


def _select_with_sector_cap(entries: list[APickEntry], top_k: int,
                            sector_cap_count: int
                            ) -> tuple[list[APickEntry], list[APickEntry]]:
    """贪心选 top_k：按 composite 排序，单 industry 不超过 sector_cap_count。

    返回 (selected, skipped_due_to_cap)。industry 为空时归类到 "_unknown_"，
    限制条件相同（避免空 industry 集中度爆炸）。
    """
    selected: list[APickEntry] = []
    skipped: list[APickEntry] = []
    sector_counts: dict[str, int] = {}

    for e in entries:  # entries 已按 composite 降序
        if not e.recommended:
            continue
        if len(selected) >= top_k:
            break
        sec = e.industry or "_unknown_"
        if sector_counts.get(sec, 0) >= sector_cap_count:
            skipped.append(e)
            e.notes = (e.notes or []) + [f"sector_cap: {sec} 已达 {sector_cap_count} 只上限"]
            continue
        selected.append(e)
        sector_counts[sec] = sector_counts.get(sec, 0) + 1
    return selected, skipped


def _is_after_a_share_close(now: datetime | None = None) -> bool:
    """A 股是否已收盘（含周末）。

    判定：周末 → True（上一交易日数据已稳定）；工作日 → 16:00 后 → True。
    16:00 而不是 15:00 是为了等龙虎榜/北向 T+1 数据落库。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:  # 周六周日
        return True
    return now.hour >= 16


# ─────────── CLI ───────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["tertile", "median", "quartile"], default="tertile")
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sector-cap", type=int, default=3,
                        help="单 industry 在 top_k 内最多入选股票数（默认 3）")
    parser.add_argument("--require-after-close", action="store_true",
                        help="仅在 A 股收盘后允许执行（北向 T+1 + LHB 盘后才出）")
    args = parser.parse_args()
    return run_a_share_picks(
        top_k=args.top, mode=args.mode, dry_run=args.dry_run,
        sector_cap_count=args.sector_cap,
        require_after_close=args.require_after_close,
    )


if __name__ == "__main__":
    sys.exit(main() or 0)
