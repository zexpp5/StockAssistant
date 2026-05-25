"""港股每日优选 — 3 因子学术版（south_flow 临时降权至 0）  ✅ PRODUCTION（港股）

**这是当前港股选股的主流水线。** 与 daily_picks_v5（美股）/ a_share_picks（A 股）三线并行。

为什么不接进 daily_picks_v5：
  v5 写死了 yfinance + is_us_ticker，港股 fundamentals 走 akshare（factor_model._fetch_hk_financials_akshare），
  接进去会污染原有逻辑；港股有独立的南向资金信号（对应 A 股北向），
  独立 job 更清爽，3 条线互不干扰。

因子（当前合计 1.00）：
  1. Piotroski F-Score (factor_model.piotroski_f_score, akshare 港股财报)  权重 0.40
  2. 12-1 月动量      (factor_model fetch_momentum, yfinance 价格)         权重 0.35
  3. 1 月反转         (factor_model fetch_momentum.reversal_1m)            权重 0.25
  4. 南向资金        standby，权重 0.00；cache 验证稳定后再恢复

候选池：
  - 仅 watchlist 港股标的（用户手动加入，比如 6869/9992）
  - hk_universe 只保留给独立 AI 推荐/候选发现，不写入「自选股·AI 优选」

输出：
  - data/latest/hk_picks.json   完整结果（结构对齐 a_share_picks，让 morning_brief 统一渲染）

用法:
  python3 -m scripts.pipeline.hk_picks                 # tertile (前 1/3)
  python3 -m scripts.pipeline.hk_picks --mode median   # 前 1/2 激进
  python3 -m scripts.pipeline.hk_picks --top 8         # 限 8 只
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts" / "lib"))

from factor_model import fetch_factors_for
from stock_db import fetch_manual_watchlist_enriched
# 2026-05-21 V1 cutover：upsert_picks 已删；hk_picks 只写 JSON
from stock_research.core.south_flow_signals import (
    compute_south_flow_signal,
    fetch_aggregate_south_flow,
    fetch_components_snapshot,
)
from stock_research.core.hk_scoring import (
    HK_FACTOR_WEIGHTS as FACTOR_WEIGHTS,
    hk_grade_label,
    score_hk_entries,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
HK_FACTOR_CACHE = _REPO / "data" / "latest" / "hk_factor_cache.json"


assert abs(sum(FACTOR_WEIGHTS.values()) - 1.0) < 1e-9


@dataclass
class HKPickEntry:
    code: str
    name: str
    market: str = "港股"
    sector: str = ""

    f_score: int | None = None
    f_score_norm: float | None = None       # F-Score / 9
    momentum_12_1: float | None = None       # 原始 %
    momentum_norm: float | None = None       # 横截面 percent-rank
    reversal_1m: float | None = None
    reversal_norm: float | None = None

    south_pct: float | None = None            # 当前南向持股 %
    south_rank: float | None = None           # 截面 percent-rank
    south_score: float = 0.5                  # 综合（聚合 + 截面）

    composite: float = 0.0
    coverage_score: float = 0.0
    missing_factors: str = ""
    rank: int = 0
    recommended: bool = False

    data_quality: str = ""                   # full / partial / fail
    notes: list[str] = field(default_factory=list)


def _is_hk_code(code: str) -> bool:
    return (code or "").upper().endswith(".HK")


def fetch_hk_candidates() -> list[dict]:
    """读取 V2 manual_watchlist 港股候选；自选股完全由用户在 dashboard 手动维护。"""
    wl_records = fetch_manual_watchlist_enriched(market="HK")
    return [
        {"ticker": r["code"], "raw_ticker": r["code"].replace(".HK", "").lstrip("0"),
         "name": r.get("name", r["code"]), "sector": r.get("industry") or "",
         "source": "manual_watchlist"}
        for r in wl_records if _is_hk_code(r.get("code", ""))
    ]


def _load_hk_factor_cache() -> dict:
    try:
        data = json.loads(HK_FACTOR_CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"items": {}}
    except Exception:
        return {"items": {}}


def _save_hk_factor_cache(cache: dict) -> None:
    HK_FACTOR_CACHE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "items": cache.get("items", {}),
    }
    tmp = HK_FACTOR_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(HK_FACTOR_CACHE)


def _cached_factor(cache_items: dict, ticker: str, today: str) -> dict | None:
    item = cache_items.get(ticker)
    if isinstance(item, dict) and item.get("date") == today and isinstance(item.get("factor"), dict):
        return item["factor"]
    return None


def _standby_south_signal(tk: str):
    from stock_research.core.south_flow_signals import SouthFlowSignal
    return SouthFlowSignal(
        code=tk,
        aggregate_regime="standby",
        individual_pct=None,
        individual_rank=0.5,
        score=0.5,
        notes=["standby (FACTOR_WEIGHTS.south_flow=0)"],
    )


def _build_hk_entry(
    c: dict,
    *,
    today: str,
    cache_items: dict,
    south_weight: float,
    south_components: dict,
    south_agg: dict,
    south_all_pcts: list,
    sleep_sec: float,
) -> tuple[HKPickEntry, dict | None, str]:
    tk = c["ticker"]
    name = c.get("name", tk)
    try:
        f = _cached_factor(cache_items, tk, today)
        cache_update = None
        cache_hit = f is not None
        if f is None:
            f = fetch_factors_for(tk, as_of=None)
            cache_update = {"date": today, "factor": f}
            if sleep_sec > 0:
                time.sleep(sleep_sec)

        piotroski = f.get("piotroski") or {}
        momentum = f.get("momentum") or {}
        f_score = piotroski.get("f_score")
        data_q = piotroski.get("data_quality", "fail")
        mom = momentum.get("momentum_12_1")
        rev = momentum.get("reversal_1m")

        if south_weight > 0:
            south_sig = compute_south_flow_signal(
                tk, south_components, south_agg, south_all_pcts
            )
        else:
            south_sig = _standby_south_signal(tk)

        entry = HKPickEntry(
            code=tk,
            name=name,
            sector=c.get("sector", "") or "",
            f_score=int(f_score) if isinstance(f_score, (int, float)) else None,
            f_score_norm=(f_score / 9.0) if isinstance(f_score, (int, float)) else None,
            momentum_12_1=mom if isinstance(mom, (int, float)) else None,
            reversal_1m=rev if isinstance(rev, (int, float)) else None,
            south_pct=south_sig.individual_pct,
            south_rank=south_sig.individual_rank,
            south_score=south_sig.score,
            data_quality=data_q,
        )
        f_str = str(f_score) if f_score is not None else "?"
        m_str = f"{mom:+.0f}%" if isinstance(mom, (int, float)) else "?"
        status = f"F={f_str:<3} M={m_str:<6} [{data_q}]" + (" cache" if cache_hit else "")
        return entry, cache_update, status
    except Exception as e:
        entry = HKPickEntry(code=tk, name=name, sector=c.get("sector", ""),
                            data_quality="fail", notes=[f"err: {e}"])
        return entry, None, f"失败: {e}"


def run_hk_picks(top_k: int = 12, mode: str = "tertile", dry_run: bool = False,
                 sleep_sec: float = 1.0, bypass_audit_gate: bool = False,
                 workers: int = 3):
    """主入口。

    参数：
      top_k    最多写入的优选数
      mode     'tertile'（前 1/3）/ 'median'（前 1/2）/ 'quartile'（前 1/4）
      dry_run  仅打印不写 JSON
      sleep_sec  每次 akshare 请求间隔，防限流
      bypass_audit_gate  强制跳过 audit CONFLICT 闸门（数据系统性故障时仍推送，风险自担）
    """
    # 跨源 audit CONFLICT 闸门 — 与 A 股 / 美股 路径对称（2026-05-12 补缺）
    from stock_research.core.audit_gate import evaluate_gate as evaluate_audit_gate
    from stock_research.core.audit_gate import format_report as format_audit_report
    audit_gate = evaluate_audit_gate()
    print(format_audit_report(audit_gate))
    if not audit_gate.passed:
        if bypass_audit_gate:
            print("\n⚠️ --bypass-audit-gate：用户强制跳过闸门，继续（风险自担）\n")
        else:
            print("\n🔴 跨源 audit 闸门 FAIL → 强制 dry-run（不写 JSON / 不写 DB）")
            print("   修复：python3 scripts/tools/recommendation_quality_gate.py  或 --bypass-audit-gate\n")
            dry_run = True

    print(f"\n📊 港股每日优选 — {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 70)

    # 1. 候选池
    print("\n[1/4] 拉港股候选池...")
    cands = fetch_hk_candidates()
    print(f"  候选 {len(cands)} 只（仅来自手动 watchlist 港股）")
    if not cands:
        print("  (空候选，退出)")
        return 0

    # 2.0 南向资金一次性预取（聚合 + 截面，所有股共享）
    # 六审 P1-1：south_flow weight=0 (standby) 时跳过预取，避免无用调用
    south_weight = FACTOR_WEIGHTS.get("south_flow", 0)
    if south_weight > 0:
        print(f"\n[2/4] 预取南向资金信号...")
        south_agg = fetch_aggregate_south_flow(lookback_days=20)
        print(f"  聚合: {south_agg.get('note', '—')}")
        south_components = fetch_components_snapshot()
        south_all_pcts = sorted(south_components.values()) if south_components else []
        print(f"  截面: {len(south_components)} 只港股通标的有南向持股数据")
    else:
        print(f"\n[2/4] 跳过南向资金预取（FACTOR_WEIGHTS.south_flow={south_weight} standby）")
        south_agg = {"score": 0.5, "regime": "standby", "note": "权重 0 跳过"}
        south_components = {}
        south_all_pcts = []

    # 2. 因子拉取（F-Score + 动量 + 反转 + 南向）
    print(f"\n[2.1/4] 拉个股因子（akshare 港股财报 + yfinance 价格）...")
    entries: list[HKPickEntry] = []
    today = datetime.now().strftime("%Y-%m-%d")
    factor_cache = _load_hk_factor_cache()
    cache_items = factor_cache.setdefault("items", {})
    cache_dirty = False
    worker_count = max(1, min(workers, len(cands)))
    print(f"  并发 workers={worker_count} · 当天 cache={HK_FACTOR_CACHE}")
    with ThreadPoolExecutor(max_workers=worker_count) as ex:
        futures = {
            ex.submit(
                _build_hk_entry,
                c,
                today=today,
                cache_items=cache_items,
                south_weight=south_weight,
                south_components=south_components,
                south_agg=south_agg,
                south_all_pcts=south_all_pcts,
                sleep_sec=sleep_sec,
            ): (idx, c)
            for idx, c in enumerate(cands, 1)
        }
        for fut in as_completed(futures):
            idx, c = futures[fut]
            tk = c["ticker"]
            name = c.get("name", tk)
            entry, cache_update, status = fut.result()
            entries.append(entry)
            if cache_update is not None:
                cache_items[tk] = cache_update
                cache_dirty = True
            print(f"  [{idx:>2}/{len(cands)}] {tk:10} {name[:12]:<14} {status}")
    if cache_dirty:
        _save_hk_factor_cache(factor_cache)

    # 3. 合成 composite + 决策（与真实持仓港股评分共用同一个 helper）
    print(f"\n[3/4] 合成综合分（{len(entries)} 只）...")
    entries, selected, cutoff, sector_skipped = score_hk_entries(
        entries,
        mode=mode,
        top_k=top_k,
        factor_weights=FACTOR_WEIGHTS,
    )
    valid_composites = [e.composite for e in entries if e.data_quality != "fail"]
    if not valid_composites:
        print("  ⚠️ 无有效因子数据 — 所有候选 fail")
        return 0

    print(f"\n  cutoff = {cutoff:.3f} (mode={mode})")
    print(f"  推荐 {len(selected)} / 有效 {len(valid_composites)} / 总 {len(entries)}")
    if sector_skipped:
        print(f"  行业 cap 跳过 {len(sector_skipped)} 只: {', '.join(sector_skipped[:8])}")
    print(f"\n  {'排':<3}{'代码':<10}{'名称':<14}{'F':>3}{'动量':>8}{'反转':>8}{'南向%':>7}{'综合':>7}  状态")
    print(f"  {'-'*78}")
    for e in entries[:30]:
        f_str = str(e.f_score) if e.f_score is not None else "?"
        m_str = f"{e.momentum_12_1:+.0f}%" if e.momentum_12_1 is not None else "?"
        rv_str = f"{e.reversal_1m:+.1f}%" if e.reversal_1m is not None else "?"
        sp_str = f"{e.south_pct:.1f}" if e.south_pct is not None else "—"
        flag = "✅" if e.recommended else ("❌" if e.data_quality == "fail" else "  ")
        print(f"  {e.rank:<3}{e.code:<10}{e.name[:12]:<14}{f_str:>3}{m_str:>8}{rv_str:>8}"
              f"{sp_str:>7}{e.composite:>7.3f}  {flag}")

    # 5. 写文件
    out = _REPO / "data" / "latest" / "hk_picks.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(),
        "market": "港股",
        "mode": mode,
        "cutoff": round(cutoff, 4),
        "top_k": top_k,
        "factor_weights": FACTOR_WEIGHTS,
        "south_flow_aggregate": south_agg,
        "n_total": len(entries),
        "n_valid": len(valid_composites),
        "n_recommended": len(selected),
        "selected": [asdict(e) for e in selected],
        "all_entries": [asdict(e) for e in entries],
    }

    if dry_run:
        print(f"\n[Dry-Run] 不写 JSON / 不写 DB")
        return 0

    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n[4/4] ✅ {out}  ({len(selected)} 只推荐)")

    # 批量拉 selected 当前收盘价 → entry_price（让 weekly_review 不再 skip）
    price_map = {}
    if selected:
        try:
            import yfinance as yf
            tickers_str = " ".join(e.code for e in selected)
            df = yf.download(tickers_str, period="1d", progress=False,
                             group_by="ticker", threads=True, auto_adjust=False)
            for e in selected:
                try:
                    if len(selected) == 1:
                        close = df["Close"].iloc[-1]
                    else:
                        close = df[e.code]["Close"].iloc[-1]
                    price_map[e.code] = float(close) if close == close else None
                except Exception:
                    price_map[e.code] = None
        except Exception as price_e:
            print(f"  ⚠️  批量拉价格失败（entry_price 留 NULL）: {price_e}")

    # 写 DuckDB picks（让 dashboard #picks tab 自动展示港股）
    db_rows = []
    for e in selected:
        grade_label = hk_grade_label(e)
        db_rows.append({
            "code": e.code,
            "name": e.name,
            "market": "港股",
            "rating": grade_label,
            "total_score": round(e.composite * 100, 2),
            # V1 子分字段 (ai_score/val_score/trend_score/cred_score) 已删
            # 2026-05-21 V1 cutover：V2 recommendation_picks 不存这些列，且 db_rows 无消费方
            "ai_relevance": e.sector or "—",
            "theme": e.sector or "港股科技",
            "entry_price": price_map.get(e.code),
            "entry_currency": "HKD",
            "model_source": "v2_hk",
            "signal": "buy",
            "coverage_score": e.coverage_score,
            "missing_factors": e.missing_factors,
            "factor_weights_used": json.dumps(FACTOR_WEIGHTS, sort_keys=True),
        })
    # 2026-05-21 V1 cutover：picks 表已删；hk_picks 不再写 DuckDB
    if db_rows:
        filled = sum(1 for r in db_rows if r.get("entry_price") is not None)
        print(f"  ({len(db_rows)} 行 · entry_price 已算 {filled}/{len(db_rows)} · JSON 已落 hk_picks.json)")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["tertile", "median", "quartile"], default="tertile")
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep", type=float, default=1.0,
                        help="akshare 请求间隔秒数（默认 1.0 防限流）")
    parser.add_argument("--workers", type=int, default=int(os.getenv("STOCK_ASSISTANT_HK_WORKERS", "3")),
                        help="缓存未命中时并发拉港股因子的线程数（默认 3）")
    parser.add_argument("--bypass-audit-gate", action="store_true",
                        help="跳过跨源 audit CONFLICT 闸门（数据系统性故障时仍推送，风险自担）")
    args = parser.parse_args()
    return run_hk_picks(top_k=args.top, mode=args.mode, dry_run=args.dry_run,
                        sleep_sec=args.sleep, bypass_audit_gate=args.bypass_audit_gate,
                        workers=args.workers)


if __name__ == "__main__":
    sys.exit(main() or 0)
