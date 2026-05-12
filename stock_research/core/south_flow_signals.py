"""南向资金信号 — 港股版的"内地聪明钱"信号。

为什么要做（与 north_flow_signals 对称）：
  港股的"南向资金"= 内地机构 / 险资通过沪港通 / 深港通买入港股，
  是港股市场的"聪明钱"指标。

  实证文献：
    - 邓鸣茂 et al. (2019)《港股通南向资金对港股回报的预测能力》
      南向连续净流入个股，未来 20 日超额收益显著
    - Bian, Su, Wang (2022) JBF: southbound mainland flow has
      predictive power for HK equity returns similar to QFII

实现策略（V1, 2026-05-12）：
  akshare 不提供个股层面的南向持股时序（北向有 stock_hsgt_individual_em，
  南向没有对称 API）。所以 V1 用两条可立即获取的信号合成：

  1. aggregate_score — 整体南向流向（所有港股共享同一值）
     从 ak.stock_hsgt_hist_em("南向资金") 取最近 5 / 20 日净买入对比
  2. individual_pct  — 当前个股南向持股 %（截面位置）
     从 ak.stock_hk_ggt_components_em() 拿所有港股通标的当前持股 %

V2（待 4-6 周个股时序累积后）：用 DuckDB 自建表存每日 components 快照，
  几个月后即可计算个股层面的连续加仓信号，对齐北向逻辑。

输出：
  SouthFlowSignal — 包含 aggregate / individual / 综合 score
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SouthFlowSignal:
    """单只港股的南向资金信号（V1：聚合 + 截面）。"""
    code: str
    # —— 聚合信号（所有港股共享）——
    aggregate_score: float = 0.5            # 0-1，整体南向最近活跃度
    aggregate_5d_avg: float | None = None    # 最近 5 日平均净买入（亿 HKD）
    aggregate_20d_avg: float | None = None   # 最近 20 日平均
    aggregate_regime: str = "neutral"        # strong_inflow / inflow / neutral / outflow / strong_outflow

    # —— 个股截面信号 ——
    individual_pct: float | None = None      # 当前南向持股占发行股本 %
    individual_rank: float = 0.5             # 0-1 截面 percent-rank（高 = 被关注度高）

    # —— 综合 ——
    score: float = 0.5
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ──────────────────────────────────────────────────
# 聚合信号（每日批量拉一次，全 HK 池共享）
# ──────────────────────────────────────────────────

def fetch_aggregate_south_flow(lookback_days: int = 20) -> dict[str, Any]:
    """整体南向资金最近 N 日的活跃度。

    返回 {
      "score": float,           # 0-1, 0.5=中性
      "regime": str,
      "avg_5d": float | None,   # 最近 5 日平均净买入 (亿 HKD)
      "avg_20d": float | None,
      "latest_date": str | None,
      "note": str,
    }
    """
    out = {"score": 0.5, "regime": "neutral", "avg_5d": None, "avg_20d": None,
           "latest_date": None, "note": ""}
    try:
        import akshare as ak
        df = ak.stock_hsgt_hist_em(symbol="南向资金")
    except Exception as e:
        out["note"] = f"akshare unavailable: {e}"
        logger.warning("south_flow aggregate fetch failed: %s", e)
        return out

    if df is None or df.empty:
        out["note"] = "南向时序为空"
        return out

    try:
        import pandas as pd
        # 净买入列：当日成交净买额（亿 HKD）
        col_date = "日期" if "日期" in df.columns else df.columns[0]
        col_net = None
        for c in ["当日成交净买额", "当日资金流入", "净买入"]:
            if c in df.columns:
                col_net = c
                break
        if col_net is None:
            out["note"] = f"未找到净买入列: {list(df.columns)[:6]}"
            return out

        df = df.copy()
        df[col_date] = pd.to_datetime(df[col_date], errors="coerce")
        df = df.dropna(subset=[col_date]).sort_values(col_date)
        df[col_net] = pd.to_numeric(df[col_net], errors="coerce")
        df = df.dropna(subset=[col_net])
        if len(df) < 10:
            out["note"] = f"南向时序数据不足: {len(df)} 行"
            return out

        recent_5 = df.tail(5)[col_net].mean()
        recent_20 = df.tail(lookback_days)[col_net].mean()
        std_20 = df.tail(lookback_days)[col_net].std()
        latest = df.tail(1)
        out["avg_5d"] = round(float(recent_5), 2)
        out["avg_20d"] = round(float(recent_20), 2)
        out["latest_date"] = latest[col_date].iloc[0].strftime("%Y-%m-%d")

        # 评分：5d 相对 20d 基线的偏离（以 20d std 标准化）
        if std_20 and std_20 > 0:
            z = (recent_5 - recent_20) / std_20
        else:
            z = 0.0

        if z >= 1.0:
            score, regime = 0.85, "strong_inflow"
        elif z >= 0.3:
            score, regime = 0.65, "inflow"
        elif z <= -1.0:
            score, regime = 0.15, "strong_outflow"
        elif z <= -0.3:
            score, regime = 0.35, "outflow"
        else:
            score, regime = 0.50, "neutral"

        out["score"] = score
        out["regime"] = regime
        out["note"] = (
            f"5d 均={recent_5:+.1f} 亿，20d 均={recent_20:+.1f}，z={z:+.2f} → {regime}"
        )
        return out

    except Exception as e:
        out["note"] = f"计算异常: {e}"
        logger.warning("south_flow aggregate compute failed: %s", e)
        return out


# ──────────────────────────────────────────────────
# 截面信号（每日批量拉一次，构建 code → pct 字典）
# ──────────────────────────────────────────────────

def fetch_components_snapshot() -> dict[str, float]:
    """所有港股通标的的当前南向持股 % 快照。

    返回 {code: pct}，code 是无前导 0 的统一格式（如 "700" 不是 "00700"）。

    优先级（八审 P0 修：避免每次 hk_picks 都触发 8 分钟拉取）：
      1. 读 data/cache/south_flow_components.json（独立 prefetch 写）
      2. cache miss / 过期 → fallback 到 stock_hk_ggt_components_em（已知列名失效但保留）
      3. 仍失败 → 返回空（个股 score 全 fallback 0.5）

    跑 prefetch 见 scripts/tools/prefetch_south_flow.py（8 分钟，每周跑一次足够）。
    """
    out: dict[str, float] = {}

    # 1. 优先读 cache
    from pathlib import Path
    import json as _json
    import os as _os
    cache_path = Path(__file__).resolve().parents[2] / "data" / "cache" / "south_flow_components.json"
    if cache_path.exists():
        try:
            cache = _json.loads(cache_path.read_text(encoding="utf-8"))
            ts = cache.get("fetched_at", "")
            # 7 天 TTL
            from datetime import datetime, timedelta
            fresh_cutoff = (datetime.now() - timedelta(days=7)).isoformat()
            if ts > fresh_cutoff and cache.get("components"):
                logger.info("south_flow components: cache HIT (%d 条, %s)",
                            len(cache["components"]), ts)
                return {k: float(v) for k, v in cache["components"].items()}
            logger.info("south_flow components: cache STALE (ts=%s < %s)", ts, fresh_cutoff)
        except Exception as e:
            logger.warning("south_flow cache 读取失败: %s", e)

    # 2. fallback 到旧 API（已知列名失效，但 graceful）
    try:
        import akshare as ak
        df = ak.stock_hk_ggt_components_em()
    except Exception as e:
        logger.warning("components snapshot fetch failed: %s", e)
        return out
    if df is None or df.empty:
        return out

    col_code = "代码" if "代码" in df.columns else df.columns[0]
    col_pct = None
    for c in ["持股占已发行股本百分比", "持股比例", "持股占已发行股份百分比"]:
        if c in df.columns:
            col_pct = c
            break
    if col_pct is None:
        logger.warning("components snapshot 找不到持股%% 列: %s — 跑 prefetch_south_flow.py 生成 cache",
                       list(df.columns)[:6])
        return out

    for _, row in df.iterrows():
        code = str(row[col_code]).strip().lstrip("0") or "0"
        try:
            pct = float(row[col_pct])
            if pct == pct:  # not NaN
                out[code] = pct
        except (TypeError, ValueError):
            continue
    return out


# ──────────────────────────────────────────────────
# 单股信号合成
# ──────────────────────────────────────────────────

def _norm_hk_code(code: str) -> str:
    """统一港股代码：'00700.HK' / '0700' / 'HK00700' → '700'。"""
    s = (code or "").upper().strip()
    for sfx in (".HK", ".HKSE", ".HKEX"):
        if s.endswith(sfx):
            s = s[:-len(sfx)]
    for prefix in ("HK",):
        if s.startswith(prefix):
            s = s[len(prefix):]
    s = s.lstrip("0") or "0"
    return s


def compute_south_flow_signal(
    code: str,
    components_pct: dict[str, float] | None = None,
    aggregate: dict[str, Any] | None = None,
    all_pcts: list[float] | None = None,
) -> SouthFlowSignal:
    """单只港股的南向资金信号。

    参数：
      code            港股代码（'00700.HK' / '0700' / '700' 任意格式）
      components_pct  fetch_components_snapshot() 的结果（按需复用）
      aggregate       fetch_aggregate_south_flow() 的结果
      all_pcts        所有港股 pct 排序，用于截面 percent-rank（不传则即时计算）
    """
    sig = SouthFlowSignal(code=code)

    # 聚合
    if aggregate:
        sig.aggregate_score = aggregate.get("score", 0.5)
        sig.aggregate_5d_avg = aggregate.get("avg_5d")
        sig.aggregate_20d_avg = aggregate.get("avg_20d")
        sig.aggregate_regime = aggregate.get("regime", "neutral")

    # 个股截面
    cmap = components_pct or fetch_components_snapshot()
    norm = _norm_hk_code(code)
    pct = cmap.get(norm)
    sig.individual_pct = pct

    if pct is not None and cmap:
        pcts = all_pcts if all_pcts is not None else sorted(cmap.values())
        if len(pcts) >= 4:
            below = sum(1 for x in pcts if x < pct)
            eq = sum(1 for x in pcts if x == pct)
            sig.individual_rank = round((below + 0.5 * eq) / len(pcts), 4)
        else:
            sig.individual_rank = 0.5

    # 合成：聚合 0.4 + 截面 0.6（截面更重要，因为对单股选择更直接）
    sig.score = round(0.4 * sig.aggregate_score + 0.6 * sig.individual_rank, 4)

    # 备注
    if pct is None:
        sig.notes.append("非港股通标的，南向资金不可买")
    else:
        sig.notes.append(f"南向持股 {pct:.2f}% (rank {sig.individual_rank:.2f})")
    if sig.aggregate_regime in ("strong_inflow", "strong_outflow"):
        emoji = "🟢" if "inflow" in sig.aggregate_regime else "🔴"
        sig.notes.append(f"{emoji} 整体南向 {sig.aggregate_regime}: "
                         f"5d 均 {sig.aggregate_5d_avg:+.1f} 亿")

    return sig


# ──────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────

def _main():
    """python -m stock_research.core.south_flow_signals [00700 09988 ...]"""
    import sys
    codes = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not codes:
        codes = ["00700", "09988", "03690", "01024", "09618", "00388"]

    print(f"\n🌊 南向资金信号扫描 — {datetime.now():%Y-%m-%d %H:%M}\n")
    print("[1/3] 拉整体南向流向...")
    agg = fetch_aggregate_south_flow(lookback_days=20)
    print(f"  {agg.get('note', '')}")
    print(f"  score = {agg['score']:.2f} | regime = {agg['regime']}")

    print("\n[2/3] 拉港股通成分快照...")
    cmap = fetch_components_snapshot()
    print(f"  {len(cmap)} 只港股通标的有南向持股数据")
    all_pcts = sorted(cmap.values()) if cmap else []

    print("\n[3/3] 计算单股信号:")
    for code in codes:
        sig = compute_south_flow_signal(code, cmap, agg, all_pcts)
        pct_str = f"{sig.individual_pct:.2f}%" if sig.individual_pct is not None else "—"
        print(f"  {code:8} pct={pct_str:>8}  rank={sig.individual_rank:.2f}  "
              f"score={sig.score:.2f}")
        for n in sig.notes:
            print(f"    └ {n}")


if __name__ == "__main__":
    _main()
