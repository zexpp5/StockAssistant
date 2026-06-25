"""双轨并跑：现规则(prod_recheck) vs 候选规则(val_down_grade) 对最新一批 picks 重排。

纯只读、纯展示——不改生产打分、不进 recommendation_picks。让用户看见候选规则
把哪些票提前/降级，配合「先观察再切」的纪律（见记忆 project_weight_variant_shadow_pipeline）。

产物：data/latest/dual_track_ranking.json
  { generated_at, candidate, baseline, markets: { US: { run_date, rows: [
       {symbol,name,prod_rank,new_rank,delta} ... ] } } }

用法:
  python3 -m scripts.tools.build_dual_track_ranking            # 写 JSON
  python3 -m scripts.tools.build_dual_track_ranking --show     # 打印不写
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

OUT = REPO / "data" / "latest" / "dual_track_ranking.json"
BASELINE = "prod_recheck"      # 现规则复算
CANDIDATE = "val_down_grade"   # 第一候选规则（降估值+评级）


def _connect():
    import duckdb
    from stock_db import DB_PATH
    for _ in range(20):
        try:
            return duckdb.connect(str(DB_PATH), read_only=True)
        except Exception:
            time.sleep(2)
    return None


def compute() -> dict:
    import scripts.tools.replay_weight_variants as rp
    conn = _connect()
    if conn is None:
        raise RuntimeError("DB 持续被写锁占用")
    try:
        picks = rp.load_picks(conn)
        rp.inject_grade_scores(conn, picks)
        base_w = rp.VARIANTS[BASELINE]
        cand_w = rp.VARIANTS[CANDIDATE]
        out: dict = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "baseline": BASELINE,
            "candidate": CANDIDATE,
            "note": "纯展示，不改生产打分；grade 因子美股专属，港/A 退化为估值/反转主导",
            "markets": {},
        }
        for mkt in ("US", "HK", "CN"):
            pool = [p for p in picks if p["market"] == mkt]
            if not pool:
                continue
            last = max(p["run_date"] for p in pool)
            pool = [p for p in pool if p["run_date"] == last]
            for p in pool:
                bw = rp.weights_for_market(base_w, mkt)
                cw = rp.weights_for_market(cand_w, mkt)
                p["_b"] = rp.variant_score(p["scores"], bw)[0]
                p["_c"] = rp.variant_score(p["scores"], cw)[0]
            b_rank = {p["symbol"]: i + 1 for i, p in enumerate(sorted(pool, key=lambda x: -x["_b"]))}
            c_rank = {p["symbol"]: i + 1 for i, p in enumerate(sorted(pool, key=lambda x: -x["_c"]))}
            rows = []
            for p in sorted(pool, key=lambda x: c_rank[x["symbol"]]):
                s = p["symbol"]
                rows.append({
                    "symbol": s,
                    "name": p.get("name") or "",
                    "prod_rank": b_rank[s],
                    "new_rank": c_rank[s],
                    "delta": b_rank[s] - c_rank[s],  # >0 = 候选规则提前
                })
            out["markets"][mkt] = {"run_date": last, "rows": rows}
        return out
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="双轨并跑 现规则 vs 候选规则 重排")
    ap.add_argument("--show", action="store_true", help="打印不写")
    args = ap.parse_args()
    data = compute()
    for mkt, blk in data["markets"].items():
        print(f"== {mkt} {blk['run_date']} ==")
        for r in blk["rows"]:
            d = r["delta"]
            arrow = f"↑{d}" if d > 0 else (f"↓{-d}" if d < 0 else "—")
            print(f"  {r['symbol']:<6} 现{r['prod_rank']:>2} 新{r['new_rank']:>2} {arrow}")
    if not args.show:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
