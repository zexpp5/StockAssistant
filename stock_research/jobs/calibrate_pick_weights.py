"""校准 daily_picks 评分权重（基于 IC 实证，替代手拍 35/25/25/15）。

现状问题：
  daily_picks.py 的四维权重（AI 关联度 35 / 估值 25 / 趋势 25 / 可信度 15）
  以及各档分值（PEG<1→25 等）都是手拍的启发式，没有任何回测证据。
  本 job 用 16 只样本 × 6 个历史 regime，对**可量化**的趋势子项做
  Spearman IC 回归（Grinold 1994 行业标准），输出 factor_weights.json，
  让 daily_picks.py 不再写死。

可校准 vs 不可校准：
  ✅ 趋势子项：从 yfinance 历史价直接复算
       trend_1y_raw     — 1 年涨幅（线性 IC）
       trend_1w_raw     — 1 周涨幅（线性 IC）
       trend_composite  — daily_picks.score_trend 的复合分（验证"追高扣分"是否有效）
  ❌ AI 关联度：人工分类标注（极强/强/中/弱），无历史时间序列 → 沿用 heuristic
  ❌ 估值：PEG 历史快照需要历史 EPS 预测，yfinance 不稳定 → 沿用 heuristic
  ❌ 数据可信度：人工标注 → 沿用 heuristic

输出：
  - 控制台报告（各因子 IC / 状态 / 推荐子权重）
  - data/factor_weights.json（被 daily_picks.py 读取，缺失则 fallback 到硬编码）
  - data/snapshots/audit/calibrate_pick_weights_<timestamp>.json（历史审计）

CLI:
  python3 -m stock_research.jobs.calibrate_pick_weights
  python3 -m stock_research.jobs.calibrate_pick_weights --dry-run   # 不写 factor_weights.json
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

import pandas as pd

from .. import config
from ..core import factor_ic
from ..adapters import store

logger = logging.getLogger("stock_research.jobs.calibrate_pick_weights")


# ─────────── 因子计算 ───────────

def _score_trend_inline(one_year_pct, one_week_pct):  # noqa: ARG001
    """复刻 daily_picks.score_trend 的逻辑（避免触发 daily_picks 的飞书 import）。

    与 daily_picks.score_trend 保持完全一致：
    1Y 档位评分（含 >200% 追高扣分），无 1W 加分（IC 实证为噪声，已删除）。
    若两边逻辑后续分叉，必须同步更新这里。
    one_week_pct 参数保留是为了向后兼容因子计算调用方，函数内不再使用。
    """
    score = 0
    if one_year_pct is not None:
        if one_year_pct > 200:
            score += 12
        elif one_year_pct > 50:
            score += 20
        elif one_year_pct > 0:
            score += 15
        else:
            score += 8
    return min(score, 25)


def _calc_trend_factors_at(ticker, as_of):
    """从 yfinance 拉历史价，返回 {trend_1y_raw, trend_1w_raw, trend_composite}。"""
    import yfinance as yf
    target = pd.to_datetime(as_of)
    start = target - pd.Timedelta(days=400)
    end = target + pd.Timedelta(days=2)
    try:
        hist = yf.Ticker(ticker).history(start=start, end=end)
    except Exception:
        return None
    if hist is None or len(hist) < 252:
        return None
    if hist.index.tz:
        hist = hist[hist.index.tz_localize(None) <= target]
    else:
        hist = hist[hist.index <= target]
    if len(hist) < 252:
        return None

    close = hist["Close"]
    t_now = float(close.iloc[-1])
    t_1y = float(close.iloc[-253])
    t_1w = float(close.iloc[-6]) if len(close) >= 6 else None
    one_year_pct = (t_now / t_1y - 1) * 100
    one_week_pct = ((t_now / t_1w - 1) * 100) if t_1w else None

    composite = _score_trend_inline(one_year_pct, one_week_pct)

    return {
        "trend_1y_raw": one_year_pct,
        "trend_1w_raw": one_week_pct if one_week_pct is not None else float("nan"),
        "trend_composite": float(composite),
    }


# ─────────── 历史构建 ───────────

def _build_history(samples, regimes):
    """对每个 regime 起点构造 (factors, forward_returns)。"""
    from walk_forward_validate import forward_return
    history = []
    for start, end, label in regimes:
        factors_t, forwards_t = {}, {}
        for tk in samples:
            f = _calc_trend_factors_at(tk, start)
            if f is None:
                continue
            fwd = forward_return(tk, start, end)
            if fwd is None:
                continue
            factors_t[tk] = f
            forwards_t[tk] = fwd
        if factors_t:
            history.append((factors_t, forwards_t))
            logger.info("[%s] %s → %s: %d 有效样本", label, start, end, len(factors_t))
    return history


# ─────────── 权重推导 ───────────

def _ic_to_subweights(audit_result, total_budget=25, marginal_threshold=0.02):
    """把 IC^2 归一化为子权重；IC < marginal_threshold 的因子置零。

    若全部因子 IC 都不显著，返回 None（让 daily_picks 退回 heuristic）。
    """
    raw = {}
    for fname, info in audit_result["factors"].items():
        ic_mean = info["summary"].get("mean_ic")
        if ic_mean is None or ic_mean < marginal_threshold:
            raw[fname] = 0.0
        else:
            raw[fname] = ic_mean ** 2

    total = sum(raw.values())
    if total == 0:
        return None  # 全军覆没

    return {fname: round(total_budget * w / total, 2) for fname, w in raw.items()}


# ─────────── 主流程 ───────────

def run(dry_run: bool = False) -> dict:
    print("=" * 80)
    print("  📊 校准 daily_picks 评分权重（基于 IC 实证）")
    print("=" * 80)

    from walk_forward_validate import SAMPLES, REGIMES
    print(f"\n  样本: {len(SAMPLES)} 只 × {len(REGIMES)} 个 regime = {len(SAMPLES) * len(REGIMES)} 截面观测")
    print(f"  方法: Spearman 排序相关 IC（Grinold 1994 / Grinold-Kahn 2000）")
    print(f"  显著阈值: |IC| ≥ 0.05 强 / 0.02 边际 / < 0.02 失效")

    print("\n[1/3] 拉历史价 + 算因子值 + 前向收益（首次约 1-3 分钟）...")
    history = _build_history(SAMPLES, REGIMES)
    if not history:
        print("  ❌ 无可用数据（可能 yfinance 拉取全失败）")
        return {"error": "no_data"}
    print(f"  ✅ {len(history)} 个 regime 有数据")

    print("\n[2/3] 算每个趋势子因子的滚动 IC + 摘要 + 衰减告警...")
    factor_names = ["trend_1y_raw", "trend_1w_raw", "trend_composite"]
    audit = factor_ic.audit_factors(history, factor_names)

    # 控制台报告
    print(f"\n{'=' * 80}")
    print(f"  趋势因子 IC 排行（按 mean IC 降序）")
    print(f"{'=' * 80}")
    print(f"\n  {'因子':<22}{'mean IC':>10}{'IC IR':>8}{'hit rate':>10}  {'状态':<12}{'诊断'}")
    print(f"  {'-' * 88}")
    for fname, _ in audit["ranking"]:
        info = audit["factors"][fname]
        s = info["summary"]
        a = info["alert"]
        print(f"  {fname:<22}{s.get('mean_ic', 0):>+9.3f}{s.get('ic_ir', 0):>+8.2f}"
              f"{s.get('hit_rate', 0):>9.0%}  {a.get('icon', '?')} {a.get('status', ''):<10}"
              f"{a.get('verdict', '')}")

    # 各 regime IC 明细
    print(f"\n  各 regime IC 明细:")
    regime_labels = [r[2] for r in REGIMES][:len(history)]
    for fname in factor_names:
        ic_hist = audit["factors"].get(fname, {}).get("ic_history", [])
        print(f"\n    {fname}:")
        for label, rec in zip(regime_labels, ic_hist):
            ic_val = rec.get("ic")
            mark = "🟢" if ic_val and ic_val > 0.05 else ("🟡" if ic_val and ic_val > 0 else "🔴")
            ic_str = f"{ic_val:+.3f}" if ic_val is not None and ic_val == ic_val else "  N/A"
            print(f"      {mark} {label:<35} IC = {ic_str}  (n={rec.get('n', 0)})")

    print("\n[3/3] 由 IC^2 归一化算趋势预算（25 分）的子权重...")
    trend_subweights = _ic_to_subweights(audit, total_budget=25)
    if trend_subweights is None:
        print("  ⚠️  所有趋势因子 IC 都 < 0.02（边际线），无法校准——daily_picks 将沿用硬编码")
    else:
        print("  ✅ 子权重已校准（写入 factor_weights.json 后 daily_picks 自动读取）")

    # ─────────── 构造输出文档 ───────────
    weights_doc = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "method": "Spearman IC; budget 跨四维保持 heuristic（多数维度无历史可测），趋势 25 分内部按 IC² 归一化",
        "sample": {
            "tickers": SAMPLES,
            "n_tickers": len(SAMPLES),
            "regimes": [{"start": s, "end": e, "label": l} for s, e, l in REGIMES],
            "n_regimes_with_data": len(history),
        },
        "calibrated": {
            "trend": {
                "budget": 25,
                "subfactor_weights": trend_subweights,  # None 表示无显著因子
                "ic_audit": {f: audit["factors"][f]["summary"] for f in factor_names},
            }
        },
        "uncalibrated_heuristic": {
            "ai_relevance": {
                "budget": 35,
                "reason": "字段为人工分类标注（极强/强/中/弱），无历史时间序列，无法回测",
                "scoring_table": {"极强": 35, "强": 28, "中": 18, "弱": 8, "无": 0},
                "next_step": "积累 picks 表 ≥ 3 个月数据后做 logit calibration"
            },
            "valuation": {
                "budget": 25,
                "reason": "PEG 历史快照需历史 EPS 预测；yfinance 提供有限",
                "scoring_function_ref": "daily_picks.py:score_valuation",
                "next_step": "接入 FMP/Finnhub 历史财务后做 PEG IC 回测"
            },
            "credibility": {
                "budget": 15,
                "reason": "字段为人工标注（高/中/低），无历史标注",
                "scoring_table": {"高": 15, "中": 10, "低": 5, "未填": 3}
            }
        },
        "comparison": {
            "old_total_weights": {"ai": 35, "val": 25, "trend": 25, "cred": 15},
            "new_total_weights": {"ai": 35, "val": 25, "trend": 25, "cred": 15},
            "changed": "趋势 25 分内部由 IC² 重新分配子项；总四维权重待 picks 表 ≥ 3 个月后做 logit calibration"
        }
    }

    # 写入两处：data/factor_weights.json（daily_picks 读）+ snapshots/audit（历史审计）
    weights_path = _REPO_ROOT / "data" / "factor_weights.json"
    if not dry_run:
        weights_path.parent.mkdir(parents=True, exist_ok=True)
        weights_path.write_text(json.dumps(weights_doc, ensure_ascii=False, indent=2, default=str))
        print(f"\n  ✅ 写入 {weights_path}")
        store.save_json(weights_doc, config.AUDIT_DIR, "calibrate_pick_weights")
    else:
        print(f"\n  [dry-run] 不写文件；预期路径 {weights_path}")

    # ─────────── 简明对比报告 ───────────
    print(f"\n{'=' * 80}")
    print(f"  旧 vs 新权重对比")
    print(f"{'=' * 80}")
    print(f"\n  四维总权重（保持 35/25/25/15，因 3 维无历史可测）:")
    print(f"    AI 关联度 35   (heuristic, 不变)")
    print(f"    估值       25   (heuristic, 不变)")
    print(f"    趋势       25   (子项重新分配 ↓)")
    print(f"    可信度     15   (heuristic, 不变)")

    print(f"\n  趋势 25 分内部:")
    print(f"    旧（硬编码）: 1Y 涨幅按档位 0/8/12/15/20 + 1W >0 加 5")
    if trend_subweights:
        print(f"    新（IC² 归一化）:")
        for fname, w in sorted(trend_subweights.items(), key=lambda x: -x[1]):
            ic = audit["factors"][fname]["summary"].get("mean_ic", 0)
            note = ""
            if fname == "trend_composite":
                note = "  ← 验证「追高扣分」公式有效性"
            elif fname == "trend_1y_raw":
                note = "  ← 1Y 线性涨幅"
            elif fname == "trend_1w_raw":
                note = "  ← 1W 线性涨幅"
            print(f"       {fname:<22} {w:>5.1f} 分  (mean IC = {ic:+.3f}){note}")

        # 关键诊断
        ic_composite = audit["factors"]["trend_composite"]["summary"].get("mean_ic", 0)
        ic_1y_raw = audit["factors"]["trend_1y_raw"]["summary"].get("mean_ic", 0)
        print(f"\n  💡 关键诊断:")
        if ic_composite > ic_1y_raw + 0.01:
            print(f"     → daily_picks 现行的 score_trend（带追高扣分）IC ({ic_composite:+.3f}) 优于线性 1Y 涨幅 ({ic_1y_raw:+.3f})")
            print(f"     → 「追高扣分」公式经 6 regime 实证有效，建议保留")
        elif ic_1y_raw > ic_composite + 0.01:
            print(f"     → 线性 1Y 涨幅 IC ({ic_1y_raw:+.3f}) 优于 score_trend 复合分 ({ic_composite:+.3f})")
            print(f"     → 「追高扣分」公式实证反而损害预测力，建议简化为线性")
        else:
            print(f"     → 复合分 ({ic_composite:+.3f}) ≈ 线性 ({ic_1y_raw:+.3f})；公式中性，不必改")

    print(f"\n{'=' * 80}\n")
    return weights_doc


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    p = argparse.ArgumentParser(description="校准 daily_picks 评分权重（IC 实证）")
    p.add_argument("--dry-run", action="store_true", help="不写 factor_weights.json")
    args = p.parse_args()
    r = run(dry_run=args.dry_run)
    return 0 if "error" not in r else 1


if __name__ == "__main__":
    sys.exit(main())
