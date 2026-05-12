"""港股每日优选 — 3 因子学术版  ✅ PRODUCTION（港股）

**这是当前港股选股的主流水线。** 与 daily_picks_v5（美股）/ a_share_picks（A 股）三线并行。

为什么不接进 daily_picks_v5：
  v5 写死了 yfinance + is_us_ticker，港股 fundamentals 走 akshare（factor_model._fetch_hk_financials_akshare），
  接进去会污染原有逻辑；并且港股没分析师上修 / LHB / 北向 / PEAD / 政策这些信号，
  独立 job 更清爽，3 条线互不干扰。

因子（合计 1.00）：
  1. Piotroski F-Score (factor_model.piotroski_f_score, akshare 港股财报)  权重 0.40
  2. 12-1 月动量      (factor_model fetch_momentum, yfinance 价格)         权重 0.35
  3. 1 月反转         (factor_model fetch_momentum.reversal_1m)            权重 0.25

候选池：
  - hk_universe.HK_TECH_UNIVERSE 33 只科技龙头白名单（恒生科技指数对照）
  - ∪ watchlist 港股标的（用户自加，比如 6869/9992）

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
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts" / "lib"))

from factor_model import fetch_factors_for
from stock_db import fetch_all_watchlist, upsert_picks
from stock_research.core.hk_universe import fetch_hk_tech_universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


FACTOR_WEIGHTS = {
    "f_score":  0.40,   # Piotroski 财务质量（akshare 港股年报）
    "momentum": 0.35,   # 12-1 月动量
    "reversal": 0.25,   # 1 月反转
}
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

    composite: float = 0.0
    rank: int = 0
    recommended: bool = False

    data_quality: str = ""                   # full / partial / fail
    notes: list[str] = field(default_factory=list)


def _is_hk_code(code: str) -> bool:
    return (code or "").upper().endswith(".HK")


def fetch_hk_candidates() -> list[dict]:
    """合并 hk_universe 白名单 + watchlist 港股，去重。"""
    universe = fetch_hk_tech_universe()
    wl_records = fetch_all_watchlist()
    wl_hk = [
        {"ticker": r["code"], "raw_ticker": r["code"].replace(".HK", "").lstrip("0"),
         "name": r.get("name", r["code"]), "sector": r.get("industry") or "",
         "source": "watchlist"}
        for r in wl_records if _is_hk_code(r.get("code", ""))
    ]

    by_ticker: dict[str, dict] = {}
    for item in universe + wl_hk:
        tk = item["ticker"].upper()
        if tk not in by_ticker:
            by_ticker[tk] = item
        else:
            by_ticker[tk].setdefault("sector", item.get("sector", ""))
    return list(by_ticker.values())


def _winsorize_rank(values: list[float | None]) -> list[float | None]:
    """[1%, 99%] winsorize + percent-rank → [0,1]，缺失保留 None。"""
    valid = sorted([v for v in values if isinstance(v, (int, float))])
    n = len(valid)
    if n < 4:
        return [0.5 if isinstance(v, (int, float)) else None for v in values]
    lo_idx = max(0, int(0.01 * (n - 1)))
    hi_idx = min(n - 1, int(0.99 * (n - 1)))
    lo, hi = valid[lo_idx], valid[hi_idx]
    pool = sorted(max(lo, min(hi, v)) for v in valid)
    pn = len(pool)
    out = []
    for v in values:
        if not isinstance(v, (int, float)):
            out.append(None); continue
        clipped = max(lo, min(hi, v))
        below = sum(1 for x in pool if x < clipped)
        eq = sum(1 for x in pool if x == clipped)
        out.append((below + 0.5 * eq) / pn)
    return out


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(q * (len(s) - 1))
    return s[idx]


def run_hk_picks(top_k: int = 12, mode: str = "tertile", dry_run: bool = False,
                 sleep_sec: float = 1.0):
    """主入口。

    参数：
      top_k    最多写入的优选数
      mode     'tertile'（前 1/3）/ 'median'（前 1/2）/ 'quartile'（前 1/4）
      dry_run  仅打印不写 JSON
      sleep_sec  每次 akshare 请求间隔，防限流
    """
    print(f"\n📊 港股每日优选 — {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 70)

    # 1. 候选池
    print("\n[1/4] 拉港股候选池...")
    cands = fetch_hk_candidates()
    print(f"  候选 {len(cands)} 只（hk_universe 33 + watchlist 港股）")
    if not cands:
        print("  (空候选，退出)")
        return 0

    # 2. 因子拉取（F-Score + 动量 + 反转）
    print(f"\n[2/4] 拉因子（akshare 港股财报 + yfinance 价格）...")
    entries: list[HKPickEntry] = []
    for i, c in enumerate(cands, 1):
        tk = c["ticker"]
        name = c.get("name", tk)
        print(f"  [{i:>2}/{len(cands)}] {tk:10} {name[:12]:<14}", end=" ", flush=True)
        try:
            f = fetch_factors_for(tk, as_of=None)
            piotroski = f.get("piotroski") or {}
            momentum = f.get("momentum") or {}
            f_score = piotroski.get("f_score")
            data_q = piotroski.get("data_quality", "fail")
            mom = momentum.get("momentum_12_1")
            rev = momentum.get("reversal_1m")

            entry = HKPickEntry(
                code=tk,
                name=name,
                sector=c.get("sector", "") or "",
                f_score=int(f_score) if isinstance(f_score, (int, float)) else None,
                f_score_norm=(f_score / 9.0) if isinstance(f_score, (int, float)) else None,
                momentum_12_1=mom if isinstance(mom, (int, float)) else None,
                reversal_1m=rev if isinstance(rev, (int, float)) else None,
                data_quality=data_q,
            )
            entries.append(entry)
            f_str = str(f_score) if f_score is not None else "?"
            m_str = f"{mom:+.0f}%" if isinstance(mom, (int, float)) else "?"
            print(f"F={f_str:<3} M={m_str:<6} [{data_q}]")
        except Exception as e:
            entries.append(HKPickEntry(code=tk, name=name, sector=c.get("sector", ""),
                                        data_quality="fail", notes=[f"err: {e}"]))
            print(f"失败: {e}")
        time.sleep(sleep_sec)

    # 3. 横截面归一化 momentum / reversal
    moms = [e.momentum_12_1 for e in entries]
    revs = [e.reversal_1m for e in entries]
    mom_ranks = _winsorize_rank(moms)
    rev_ranks = _winsorize_rank(revs)
    for i, e in enumerate(entries):
        e.momentum_norm = mom_ranks[i]
        e.reversal_norm = rev_ranks[i]

    # 4. 合成 composite + 决策
    print(f"\n[3/4] 合成综合分（{len(entries)} 只）...")
    for e in entries:
        # 缺失（None）→ 0.5（中位补值）
        f_n = e.f_score_norm if e.f_score_norm is not None else 0.5
        m_n = e.momentum_norm if e.momentum_norm is not None else 0.5
        r_n = e.reversal_norm if e.reversal_norm is not None else 0.5
        composite = (
            FACTOR_WEIGHTS["f_score"] * f_n
            + FACTOR_WEIGHTS["momentum"] * m_n
            + FACTOR_WEIGHTS["reversal"] * r_n
        )
        e.composite = round(composite, 4)

    entries.sort(key=lambda e: -e.composite)
    valid_composites = [e.composite for e in entries if e.data_quality != "fail"]
    if not valid_composites:
        print("  ⚠️ 无有效因子数据 — 所有候选 fail")
        return 0
    cutoff_map = {
        "quartile": _quantile(valid_composites, 0.75),
        "tertile":  _quantile(valid_composites, 2/3),
        "median":   _quantile(valid_composites, 0.50),
    }
    cutoff = cutoff_map[mode]
    selected: list[HKPickEntry] = []
    for i, e in enumerate(entries, 1):
        e.rank = i
        e.recommended = (e.data_quality != "fail" and e.composite >= cutoff)
        if e.recommended and len(selected) < top_k:
            selected.append(e)

    print(f"\n  cutoff = {cutoff:.3f} (mode={mode})")
    print(f"  推荐 {len(selected)} / 有效 {len(valid_composites)} / 总 {len(entries)}")
    print(f"\n  {'排':<3}{'代码':<10}{'名称':<14}{'F':>3}{'动量':>8}{'反转':>8}{'综合':>7}  状态")
    print(f"  {'-'*70}")
    for e in entries[:30]:
        f_str = str(e.f_score) if e.f_score is not None else "?"
        m_str = f"{e.momentum_12_1:+.0f}%" if e.momentum_12_1 is not None else "?"
        rv_str = f"{e.reversal_1m:+.1f}%" if e.reversal_1m is not None else "?"
        flag = "✅" if e.recommended else ("❌" if e.data_quality == "fail" else "  ")
        print(f"  {e.rank:<3}{e.code:<10}{e.name[:12]:<14}{f_str:>3}{m_str:>8}{rv_str:>8}"
              f"{e.composite:>7.3f}  {flag}")

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
        # 评级（基于 composite 分位；港股 composite ∈ [0,1]，跟美股 z-score 量纲不同 → 自有阈值）
        if e.composite >= 0.75:
            grade_label = "⭐⭐⭐ 强烈推荐（综合 ≥0.75）"
        elif e.composite >= 0.60:
            grade_label = "⭐⭐ 推荐（综合 ≥0.60）"
        else:
            grade_label = "⭐ 关注"
        db_rows.append({
            "code": e.code,
            "name": e.name,
            "market": "港股",
            "rating": grade_label,
            "total_score": round(e.composite * 100, 2),
            "ai_score": (e.f_score or 0) * 10,
            "val_score": (e.f_score or 0) * 3,
            "trend_score": min(int(abs(e.momentum_12_1 or 0)), 25),
            "cred_score": 0,
            "ai_relevance": e.sector or "—",
            "theme": e.sector or "港股科技",
            "entry_price": price_map.get(e.code),
            "entry_currency": "HKD",
        })
    if db_rows:
        try:
            n = upsert_picks(db_rows)
            filled = sum(1 for r in db_rows if r.get("entry_price") is not None)
            print(f"  DuckDB picks 写入 {n} 行（市场=港股 · entry_price 已填 {filled}/{n}）")
        except Exception as db_e:
            print(f"  ⚠️  DuckDB picks 写入失败: {db_e}")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["tertile", "median", "quartile"], default="tertile")
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep", type=float, default=1.0,
                        help="akshare 请求间隔秒数（默认 1.0 防限流）")
    args = parser.parse_args()
    return run_hk_picks(top_k=args.top, mode=args.mode, dry_run=args.dry_run,
                        sleep_sec=args.sleep)


if __name__ == "__main__":
    sys.exit(main() or 0)
