#!/usr/bin/env python3
"""B1 严筛口径影子回算 —— 只读，找"中位数转正、不靠极端赢家"的候选口径。

见 docs/2026-06-08_严筛口径迭代_B1影子回算方案.md。

安全边界（只读）：
- 只读 pick_outcomes / recommendation_picks / recommendation_runs；
- 只写 data/latest/strict_caliber_backtest.json + data/reports/strict_caliber_backtest.md；
- 不改打分公式、不写 watchlist / 真实持仓、不自动买、不切策略版本。

评价纪律（按优先级，不能只看平均 alpha）：
  1) median alpha 是否转正  2) 胜率>50%  3) 最大单笔亏损是否下降  4) 平均 alpha
  + 留一法（去掉最佳 1 笔后 median 仍达标 = 不靠单只极端赢家）
  + 因子边际一致性（某动作在所有含它的组里都改善才算真信号）

产出只叫"候选新口径"，不是"已验证策略"。升级到可小仓试探须满足方案 §7 硬门槛
（前瞻≥20 笔 + median>0 + 胜率>50% + 最大亏损低于原口径 + 连续≥2 run 不靠极端赢家）。
本脚本统计的是历史样本，前瞻进度字段单列，0 起累积。
"""
from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import duckdb  # noqa: E402

from stock_research.core import strategy_eval as se  # noqa: E402

DB_PATH = REPO / "stock_history_v2.duckdb"
OUT_JSON = REPO / "data" / "latest" / "strict_caliber_backtest.json"
OUT_MD = REPO / "data" / "reports" / "strict_caliber_backtest.md"
BLOCKED_FLAG = "OVERHEATED_1Y"
MOMENTUM_LT = 80.0


# ── 6 组有逻辑的候选口径（变量受控防过拟合）──────────────
def _passes(row: dict[str, Any], *, max_rank: int, drop_overheated: bool, momentum_lt: float | None) -> bool:
    if row["market_rank"] > max_rank:
        return False
    if drop_overheated and BLOCKED_FLAG in row["risk_codes"]:
        return False
    if momentum_lt is not None:
        if row["momentum"] is None or row["momentum"] >= momentum_lt:
            return False
    return True


CALIBERS = [
    ("①Top5", dict(max_rank=5, drop_overheated=False, momentum_lt=None)),
    ("②Top5+排过热", dict(max_rank=5, drop_overheated=True, momentum_lt=None)),
    ("③Top5+mom<80", dict(max_rank=5, drop_overheated=False, momentum_lt=MOMENTUM_LT)),
    ("④Top5+排过热+mom<80", dict(max_rank=5, drop_overheated=True, momentum_lt=MOMENTUM_LT)),
    ("⑤Top3+排过热", dict(max_rank=3, drop_overheated=True, momentum_lt=None)),
    ("⑥Top3+排过热+mom<80", dict(max_rank=3, drop_overheated=True, momentum_lt=MOMENTUM_LT)),
]


def _fetch_pool(conn, market: str, horizon: str, strategy_version: str | None) -> list[dict[str, Any]]:
    """当前版本的 outcome 大样本（诊断口径：不去重，统计力优先）。"""
    sv = se.latest_strategy_version(conn) if strategy_version in (None, "latest") else strategy_version
    rows = conn.execute(
        """
        SELECT po.alpha_pct, po.is_success, rp.factor_scores_json, rp.risk_flags_json,
          CAST(rr.run_date AS VARCHAR) AS run_date,
          ROW_NUMBER() OVER (PARTITION BY po.run_id ORDER BY rp.total_score DESC NULLS LAST, rp.symbol ASC) AS mr
        FROM pick_outcomes po
        JOIN recommendation_runs rr ON rr.run_id = po.run_id
        JOIN recommendation_picks rp
          ON rp.run_id = po.run_id AND rp.market = po.market AND rp.symbol = po.symbol
        WHERE po.market = ? AND po.horizon = ? AND rr.strategy_version = ?
          AND po.alpha_pct IS NOT NULL AND isfinite(po.alpha_pct)
          AND LOWER(COALESCE(rp.signal, rp.rating, '')) IN ('buy', 'strong_buy')
        """,
        [market, horizon, sv],
    ).fetchall()
    out = []
    for alpha, succ, fj, rj, rd, mr in rows:
        out.append({
            "alpha_pct": float(alpha),
            "is_success": bool(succ),
            "momentum": se._momentum(fj),
            "risk_codes": se._risk_codes(rj),
            "run_date": str(rd),
            "market_rank": int(mr),
        })
    return out, sv


def evaluate(sub: list[dict[str, Any]]) -> dict[str, Any] | None:
    """评价纪律：median 优先 + 留一法 + 最大亏损。"""
    al = sorted(r["alpha_pct"] for r in sub)
    if len(al) < 3:
        return None
    n = len(al)
    return {
        "n": n,
        "avg_alpha_pct": round(sum(al) / n, 4),
        "median_alpha_pct": round(st.median(al), 4),
        "win_rate_pct": round(100 * sum(1 for r in sub if r["is_success"]) / n, 2),
        "max_loss_pct": round(min(al), 4),
        # 留一：去掉最佳 1 笔后的 median（查是否靠单只极端赢家撑住）
        "median_drop_best": round(st.median(al[:-1]), 4),
    }


def _is_candidate(e: dict[str, Any] | None) -> bool:
    """候选口径门槛（历史回算层；前瞻验证另算）：
    median>0 且 胜率>50% 且 留一后 median 仍>=0（不靠极端赢家）。"""
    if not e:
        return False
    return (e["median_alpha_pct"] or 0) > 0 and (e["win_rate_pct"] or 0) > 50 and (e["median_drop_best"] or 0) >= 0


def _read_forward_start(json_path: Path) -> str | None:
    """读已有输出里的前瞻起始日（持久化，每天读同一个，不重置）。"""
    try:
        d = json.loads(json_path.read_text(encoding="utf-8"))
        return (d.get("forward_progress") or {}).get("forward_start")
    except Exception:
        return None


def _upgrade_ready(fwd: dict[str, Any] | None, base_max_loss: float | None) -> bool:
    """方案 §7 硬门槛：前瞻≥20 + median>0 + 胜率>50% + 留一不靠极端赢家 + 最大亏损低于原口径。"""
    if not fwd or (fwd.get("n") or 0) < 20:
        return False
    ok = (fwd["median_alpha_pct"] or 0) > 0 and (fwd["win_rate_pct"] or 0) > 50 \
        and (fwd["median_drop_best"] or 0) >= 0
    if base_max_loss is not None:
        ok = ok and (fwd["max_loss_pct"] or 0) > base_max_loss  # 负数：更大=亏损更小
    return ok


def build_payload(*, db_path: Path = DB_PATH, market: str = "US", horizon: str = "1d",
                  strategy_version: str | None = "latest", now: datetime | None = None,
                  out_json: Path = OUT_JSON) -> dict[str, Any]:
    now = now or datetime.now()
    if not db_path.exists():
        return {"schema_version": "strict_caliber_backtest_v1", "generated_at": now.isoformat(timespec="seconds"),
                "status": "FAIL", "blockers": [f"DB 不存在：{db_path}"]}
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        pool, sv = _fetch_pool(conn, market, horizon, strategy_version)
    finally:
        conn.close()

    results = []
    for name, crit in CALIBERS:
        sub = [r for r in pool if _passes(r, **crit)]
        e = evaluate(sub)
        results.append({"caliber": name, "criteria": crit, "stats": e, "is_candidate": _is_candidate(e)})

    # 因子边际一致性（中位数 pt 差；某动作在所有含它的组都改善才算真信号）
    by = {r["caliber"]: (r["stats"] or {}).get("median_alpha_pct") for r in results}
    def diff(a, b):
        return round(by[a] - by[b], 4) if by.get(a) is not None and by.get(b) is not None else None
    margins = {
        "drop_overheated": {"①→②": diff("②Top5+排过热", "①Top5"),
                            "③→④": diff("④Top5+排过热+mom<80", "③Top5+mom<80")},
        "momentum_lt_80": {"①→③": diff("③Top5+mom<80", "①Top5"),
                          "②→④": diff("④Top5+排过热+mom<80", "②Top5+排过热")},
    }
    # 候选里挑"最稳"（median 优先 → 留一 → 胜率 → 最大亏损）
    cands = [r for r in results if r["is_candidate"]]
    best = max(cands, key=lambda r: (r["stats"]["median_alpha_pct"], r["stats"]["median_drop_best"],
                                     r["stats"]["win_rate_pct"], r["stats"]["max_loss_pct"]), default=None)

    # 前瞻验证：锁定起始日（持久化，每天读同一个），用最佳候选口径统计「起始日之后」的新成熟样本。
    # 今天的推荐当天还没成熟 → 前瞻从 0 起累积；真钱冻结直到 upgrade_ready=true（方案 §7）。
    forward_start = _read_forward_start(out_json) or now.date().isoformat()
    base_max_loss = (results[0]["stats"] or {}).get("max_loss_pct")  # ①Top5 作原口径基准
    if best:
        best_crit = next((dict(c[1]) for c in CALIBERS if c[0] == best["caliber"]), None)
        fwd_pool = [r for r in pool if r["run_date"] >= forward_start and _passes(r, **best_crit)]
        fwd_stats = evaluate(fwd_pool)
        forward_progress = {
            "forward_start": forward_start,
            "best_caliber": best["caliber"],
            "n": (fwd_stats or {}).get("n", 0),
            "stats": fwd_stats,
            "upgrade_ready": _upgrade_ready(fwd_stats, base_max_loss),
            "target": "前瞻≥20 + median>0 + 胜率>50% + 留一不靠极端赢家 + 最大亏损<原口径①",
            "note": "前瞻样本=起始日之后符合最佳候选口径的成熟样本；真钱保持冻结直到 upgrade_ready=true。",
        }
    else:
        forward_progress = {"forward_start": forward_start, "best_caliber": None, "n": 0,
                            "upgrade_ready": False, "note": "无候选口径达标，前瞻不启动。"}

    return {
        "schema_version": "strict_caliber_backtest_v1",
        "generated_at": now.isoformat(timespec="seconds"),
        "safety_boundary": "只读影子回算；不改公式、不写持仓、不碰真钱。产出为候选口径，非已验证策略。",
        "market": market, "horizon": horizon, "strategy_version": sv,
        "sample_note": "历史大样本（当前版本，不去重，诊断用统计力优先）；前瞻进度单列见 forward_progress。",
        "results": results,
        "factor_margins": margins,
        "best_candidate": best["caliber"] if best else None,
        "forward_progress": forward_progress,
        "status": "OK",
    }


def _md(p: dict[str, Any]) -> str:
    rows = []
    for r in p.get("results", []):
        s = r.get("stats") or {}
        mark = " ✅候选" if r.get("is_candidate") else ""
        rows.append(f"| {r['caliber']} | {s.get('n','—')} | {s.get('median_alpha_pct','—')}% | "
                    f"{s.get('win_rate_pct','—')}% | {s.get('max_loss_pct','—')}% | {s.get('median_drop_best','—')}%{mark} |")
    m = p.get("factor_margins", {})
    fp = p.get("forward_progress") or {}
    return f"""# B1 严筛口径影子回算（只读）

- 生成：{p.get('generated_at')} · 市场 {p.get('market')} · {p.get('horizon')} · 版本 {p.get('strategy_version')}
- {p.get('safety_boundary')}
- 样本口径：{p.get('sample_note')}

## 六组口径

| 口径 | n | 中位 alpha | 胜率 | 最大亏损 | 留一后中位 |
|---|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

## 因子边际一致性（中位数 pt）

- 排除过热：①→② {m.get('drop_overheated',{}).get('①→②')}pt · ③→④ {m.get('drop_overheated',{}).get('③→④')}pt
- momentum<80：①→③ {m.get('momentum_lt_80',{}).get('①→③')}pt · ②→④ {m.get('momentum_lt_80',{}).get('②→④')}pt

## 最佳候选口径：{p.get('best_candidate') or '（无组达标）'}

### 前瞻验证进度
- 起始日：{fp.get('forward_start')} · 前瞻样本：**{fp.get('n', 0)}** 笔 · 可升级：**{fp.get('upgrade_ready')}**
- 升级门槛：{fp.get('target', '—')}
- {fp.get('note', '')}

⚠️ 历史回算候选 ≠ 已验证策略。真钱保持冻结直到 upgrade_ready=true。
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="B1 严筛口径影子回算（只读）")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--market", default="US")
    ap.add_argument("--horizon", default="1d")
    ap.add_argument("--strategy-version", default="latest")
    ap.add_argument("--json", default=str(OUT_JSON))
    ap.add_argument("--md", default=str(OUT_MD))
    a = ap.parse_args(argv)
    p = build_payload(db_path=Path(a.db), market=a.market, horizon=a.horizon,
                      strategy_version=a.strategy_version, out_json=Path(a.json))
    Path(a.json).parent.mkdir(parents=True, exist_ok=True)
    Path(a.md).parent.mkdir(parents=True, exist_ok=True)
    Path(a.json).write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(a.md).write_text(_md(p), encoding="utf-8")
    fp = p.get("forward_progress") or {}
    print(f"strict_caliber_backtest: {p.get('status')} · best={p.get('best_candidate')} · "
          f"前瞻 {fp.get('n', 0)} 笔(起 {fp.get('forward_start')}) · upgrade_ready={fp.get('upgrade_ready')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
