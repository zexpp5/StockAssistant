"""双轨并跑：现规则(prod_recheck) vs 候选规则(val_down_grade)。

🅿️0 公平对照硬要求（见 docs/V2/2026-06-25_推荐公式切换_val_down_grade_方案.md）：
新旧公式**必须基于同一批「全量候选池」factor_snapshot_universe（截断前~全宇宙）
各自独立打分、各自产出 Top5/Top10/Top20**，绝不在任一方 Top20 内重排——否则对照失真。

纯只读、纯展示——不改生产打分、不进 recommendation_picks。
注意：池源是全量 factor_snapshot_universe；但 US 会先套共同资格闸
eligibility in (buyable,research_only)，避免把已被身份/证据拦截的票
重新拉进公式对照。

产物：data/latest/dual_track_ranking.json
  { generated_at, candidate, baseline, pool_source, markets: { US: {
      run_date, pool_size,
      rank_slices: {top5/top10/top20: {rows, dropped}},
      candidate_focus_top10: [ ...候选Top10 ],
      rows: [ {symbol,name,new_rank,prod_rank,delta,is_new} ...候选Top20 ],
      dropped: [ {symbol,name,prod_rank,new_rank} ...老进新出 ] } } }

用法:
  python3 -m scripts.tools.build_dual_track_ranking            # 写 JSON
  python3 -m scripts.tools.build_dual_track_ranking --show     # 打印不写
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

OUT = REPO / "data" / "latest" / "dual_track_ranking.json"
BASELINE = "legacy_baseline"   # 对外展示名：老公式影子基线
BASELINE_VARIANT = "prod_recheck"      # replay 里的老公式复算权重
CANDIDATE = "val_down_grade"   # 第一候选规则（降估值+评级）
TOP_NS = (5, 10, 20)
TOP_N = max(TOP_NS)
POOL_SOURCE = "factor_snapshot_universe"
US_MARKET = "US"
US_RECOMMENDABLE_ELIGIBILITY = {"buyable", "research_only"}


def _connect():
    import duckdb
    from stock_db import DB_PATH
    for _ in range(20):
        try:
            return duckdb.connect(str(DB_PATH), read_only=True)
        except Exception:
            time.sleep(2)
    return None


def _names(conn) -> dict[str, str]:
    """symbol → name（system_universe + manual_watchlist 兜底）。"""
    out: dict[str, str] = {}
    for tbl in ("system_universe", "manual_watchlist"):
        try:
            for sym, name in conn.execute(f"SELECT symbol, name FROM {tbl}").fetchall():
                if sym and name and str(sym).upper() not in out:
                    out[str(sym).upper()] = name
        except Exception:
            pass
    return out


def _table_columns(conn, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
    except Exception:
        return set()


def _inject_grade(conn, rows: list[dict], run_date: str) -> None:
    """美股按 run_date PIT 注入评级分；已有快照 grade 时不覆盖。"""
    from stock_research.core.analyst_grade_factor import (
        NEUTRAL_GRADE_SCORE,
        fetch_grade_events,
        score_symbol_from_events,
    )
    ev = fetch_grade_events(conn, market=US_MARKET)
    asof = date.fromisoformat(run_date)
    for r in rows:
        if r.get("market") != US_MARKET:
            continue
        if r["scores"].get("grade") is not None:
            continue
        r["scores"]["grade"] = (
            score_symbol_from_events(ev, r["symbol"], asof)
            if ev else NEUTRAL_GRADE_SCORE
        )


def _candidate_weights_for_market(rp, market: str) -> dict[str, float]:
    """只有 US 切候选公式；非 US 与 baseline 相同，用来证明港/A 未切。"""
    if market == US_MARKET:
        return rp.weights_for_market(rp.VARIANTS[CANDIDATE], market)
    return rp.weights_for_market(rp.VARIANTS[BASELINE_VARIANT], market)


def compute() -> dict:
    import scripts.tools.replay_weight_variants as rp
    conn = _connect()
    if conn is None:
        raise RuntimeError("DB 持续被写锁占用")
    try:
        names = _names(conn)
        base_w = rp.VARIANTS[BASELINE_VARIANT]
        out: dict = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "baseline": BASELINE,
            "baseline_variant": BASELINE_VARIANT,
            "candidate": CANDIDATE,
            "pool_source": POOL_SOURCE,
            "top_n": TOP_N,
            "note": "全量候选池同池各选再比；本切换只作用于 US，HK/CN candidate=baseline 用于证明未切。",
            "markets": {},
        }
        for mkt in ("US", "HK", "CN"):
            last = conn.execute(
                "SELECT max(run_date) FROM factor_snapshot_universe WHERE market=?", [mkt]
            ).fetchone()[0]
            if last is None:
                continue
            run_date = str(last)
            cols = _table_columns(conn, "factor_snapshot_universe")
            grade_expr = "grade" if "grade" in cols else "NULL AS grade"
            formula_expr = "formula" if "formula" in cols else "NULL AS formula"
            recs = conn.execute(
                f"""
                SELECT symbol, momentum, valuation, reversal, data_usability, f_score,
                       {grade_expr}, {formula_expr}, eligibility, action
                FROM factor_snapshot_universe
                WHERE market=? AND run_date=?
                """, [mkt, last]
            ).fetchall()
            pool = []
            for sym, mom, val, rev, du, fs, grade, formula, eligibility, action in recs:
                if mkt == US_MARKET and str(eligibility or "") not in US_RECOMMENDABLE_ELIGIBILITY:
                    continue
                pool.append({
                    "market": mkt,
                    "symbol": str(sym).upper(),
                    "scores": {"momentum": mom, "valuation": val, "reversal": rev,
                               "data_usability": du, "f_score": fs, "grade": grade},
                    "snapshot_formula": formula,
                    "eligibility": eligibility,
                    "action": action,
                })
            if not pool:
                continue
            _inject_grade(conn, pool, run_date)
            bw = rp.weights_for_market(base_w, mkt)
            cw = _candidate_weights_for_market(rp, mkt)
            for p in pool:
                p["_b"] = rp.variant_score(p["scores"], bw)[0]
                p["_c"] = rp.variant_score(p["scores"], cw)[0]
            # 全池各自排名
            base_sorted = sorted(pool, key=lambda x: -x["_b"])
            cand_sorted = sorted(pool, key=lambda x: -x["_c"])
            b_rank = {p["symbol"]: i + 1 for i, p in enumerate(base_sorted)}
            c_rank = {p["symbol"]: i + 1 for i, p in enumerate(cand_sorted)}
            by_symbol = {p["symbol"]: p for p in pool}

            def _slice_payload(top_n: int) -> dict:
                base_top = {p["symbol"] for p in base_sorted[:top_n]}
                cand_top = [p["symbol"] for p in cand_sorted[:top_n]]
                rows = []
                for s in cand_top:
                    item = by_symbol.get(s, {})
                    rows.append({
                        "symbol": s, "name": names.get(s, ""),
                        "new_rank": c_rank[s], "prod_rank": b_rank[s],
                        "delta": b_rank[s] - c_rank[s],
                        "is_new": s not in base_top,   # 新公式捞进、老公式同档没有
                        "candidate_score": round(float(item.get("_c") or 0), 4),
                        "baseline_score": round(float(item.get("_b") or 0), 4),
                    })
                dropped = [{
                    "symbol": s, "name": names.get(s, ""),
                    "prod_rank": b_rank[s], "new_rank": c_rank[s],
                    "candidate_score": round(float((by_symbol.get(s) or {}).get("_c") or 0), 4),
                    "baseline_score": round(float((by_symbol.get(s) or {}).get("_b") or 0), 4),
                } for s in sorted(base_top - set(cand_top), key=lambda x: b_rank[x])]
                return {
                    "top_n": top_n,
                    "rows": rows,
                    "dropped": dropped,
                    "new_count": sum(1 for r in rows if r.get("is_new")),
                    "candidate_symbols": cand_top,
                    "baseline_symbols": [p["symbol"] for p in base_sorted[:top_n]],
                }

            rank_slices = {f"top{n}": _slice_payload(n) for n in TOP_NS}
            rows = rank_slices[f"top{TOP_N}"]["rows"]
            dropped = rank_slices[f"top{TOP_N}"]["dropped"]
            out["markets"][mkt] = {
                "run_date": run_date, "pool_size": len(pool),
                "common_gate": (
                    "US eligibility in buyable/research_only before each formula ranks"
                    if mkt == US_MARKET else "legacy market: no P0 US eligibility filter"
                ),
                "candidate_active": mkt == US_MARKET,
                "baseline_weights": bw,
                "candidate_weights": cw,
                "top_ns": list(TOP_NS),
                "rank_slices": rank_slices,
                "candidate_focus_top10": (
                    rank_slices.get("top10", {}).get("rows", []) if mkt == US_MARKET else []
                ),
                # 兼容旧面板：顶层 rows/dropped 仍代表 Top20。
                "rows": rows, "dropped": dropped,
            }
        return out
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="双轨并跑 现规则 vs 候选规则（全池）")
    ap.add_argument("--show", action="store_true", help="打印不写")
    args = ap.parse_args()
    data = compute()
    for mkt, blk in data["markets"].items():
        print(f"== {mkt} {blk['run_date']} · 全池 {blk['pool_size']} 只 → 各选 Top{TOP_N} ==")
        for top_key in ("top5", "top10", "top20"):
            sl = (blk.get("rank_slices") or {}).get(top_key) or {}
            if not sl:
                continue
            print(f"-- {top_key.upper()} · 新捞入 {sl.get('new_count', 0)} 只")
            for r in sl.get("rows", []):
                d = r["delta"]
                arrow = f"↑{d}" if d > 0 else (f"↓{-d}" if d < 0 else "—")
                flag = " 🆕" if r["is_new"] else ""
                print(f"  新{r['new_rank']:>2}  老{r['prod_rank']:>3}  {arrow:>5}  {r['symbol']:<8}{flag}")
            if sl.get("dropped"):
                print(f"  -- 老 {top_key.upper()} 被新公式挤出: " +
                      "、".join(f"{x['symbol']}(老{x['prod_rank']}→新{x['new_rank']})" for x in sl["dropped"]))
    if not args.show:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
