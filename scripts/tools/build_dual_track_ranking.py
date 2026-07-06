"""双轨并跑：现规则(prod_recheck) vs 候选规则(val_down_grade)。

🅿️0 公平对照硬要求（见 docs/V2/2026-06-25_推荐公式切换_val_down_grade_方案.md）：
新旧公式**必须基于同一批「全量候选池」factor_snapshot_universe（截断前~全宇宙）
各自独立打分、各自产出 Top5/Top10/Top20**，绝不在任一方 Top20 内重排——否则对照失真。

纯只读、纯展示——不改生产打分、不进 recommendation_picks。
注意：池源是全量 factor_snapshot_universe；但 US 会先套共同资格闸
eligibility in (buyable,research_only)，避免把已被身份/证据拦截的票
重新拉进公式对照。

产物：data/latest/dual_track_ranking.json
      data/latest/daily_strict_picks.json
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
STRICT_OUT = REPO / "data" / "latest" / "daily_strict_picks.json"
BASELINE = "legacy_baseline"   # 对外展示名：老公式影子基线
BASELINE_VARIANT = "prod_recheck"      # replay 里的老公式复算权重
CANDIDATE = "val_down_grade"   # 第一候选规则（降估值+评级）
TOP_NS = (5, 10, 20)
TOP_N = max(TOP_NS)
POOL_SOURCE = "factor_snapshot_universe"
US_MARKET = "US"
US_RECOMMENDABLE_ELIGIBILITY = {"buyable", "research_only"}
STRICT_PICK_N = 3
STRICT_FALLING_KNIFE_PCT = -20.0
STRICT_RISK_PULLBACK_PCT = -12.0

_PLAIN_INTROS = {
    "AMZN": "云计算和电商平台，AWS 是 AI 算力需求的核心承接方之一",
    "AVGO": "AI 定制芯片和高速网络芯片供应商，帮大厂把算力连起来",
    "NXPI": "车规和工业芯片公司，偏边缘计算和汽车电子",
    "ADSK": "设计软件龙头，服务工程、建筑和制造业数字化",
    "CRM": "企业软件平台，AI 助手和客户数据云是增长看点",
    "MSFT": "云和企业软件平台，Azure 与 Copilot 是 AI 商业化主线",
    "ORCL": "企业数据库和云基础设施供应商，受益 AI 云容量建设",
    "ON": "功率和传感芯片公司，覆盖汽车、电源和工业场景",
    "QCOM": "移动和边缘 AI 芯片公司，覆盖手机、车载和终端侧 AI",
    "INTU": "财税和中小企业软件平台，AI 用于自动化财务工作流",
}


def _position_is_expensive(zone: dict | None) -> bool:
    return str((zone or {}).get("position") or "") == "偏贵"


def _plain_intro(symbol: str, chain_intro: str | None = None) -> str:
    intro = str(chain_intro or "").strip()
    if intro:
        return intro
    return _PLAIN_INTROS.get(symbol.upper(), "科技/AI 产业链候选，需要结合买前研究确认业务弹性")


def _price_move_20d(conn, symbol: str) -> dict:
    """近 20 个交易日收盘涨跌幅。用最近 20 条收盘价(含最新日)测这一段走势。"""
    rows = conn.execute(
        """
        SELECT trade_date, close
        FROM price_daily
        WHERE market=? AND upper(symbol)=upper(?) AND close IS NOT NULL
        ORDER BY trade_date DESC
        LIMIT 20
        """,
        [US_MARKET, symbol],
    ).fetchall()
    if len(rows) < 20:
        return {"symbol": symbol.upper(), "n": len(rows), "pct": None}
    last_date, last_close = rows[0]
    start_date, start_close = rows[-1]
    try:
        pct = (float(last_close) / float(start_close) - 1.0) * 100.0
    except Exception:
        pct = None
    return {
        "symbol": symbol.upper(),
        "n": len(rows),
        "latest_date": str(last_date),
        "start_date": str(start_date),
        "latest_close": round(float(last_close), 4),
        "start_close": round(float(start_close), 4),
        "pct": round(pct, 4) if pct is not None else None,
    }


def _chain_intro_map(conn) -> dict[str, str]:
    try:
        rows = conn.execute(
            """
            SELECT symbol, layman_intro
            FROM chain_metadata
            WHERE market=? AND layman_intro IS NOT NULL AND layman_intro <> ''
            """,
            [US_MARKET],
        ).fetchall()
    except Exception:
        return {}
    return {str(sym).upper(): str(intro) for sym, intro in rows if sym and intro}


def _strict_reason(row: dict) -> str:
    new_rank = row.get("new_rank")
    prod_rank = row.get("prod_rank")
    if prod_rank and int(prod_rank) <= 10:
        agree = f"老公式也在前 10（第 {prod_rank}）"
    elif prod_rank and int(prod_rank) <= 20:
        agree = f"老公式也在前 20（第 {prod_rank}）"
    else:
        agree = f"老公式排第 {prod_rank or '—'}，属于新公式额外捞出"
    return f"新公式精选第 {new_rank}；{agree}；估值闸和接飞刀闸通过。"


def _strict_risk(move: dict) -> str:
    pct = move.get("pct")
    if isinstance(pct, (int, float)) and pct <= STRICT_RISK_PULLBACK_PCT:
        return f"近 20 个交易日跌 {pct:.1f}%，板块仍在调整；只适合分批小仓做买前研究。"
    return "未触发 20 日大跌过滤；仍需看盘前风险和买前研究。"


def _expectation_inputs(conn, symbol: str) -> dict:
    """拉预期消耗度的输入：最新估值行 + 行业文本。缺哪项返回哪项 None。"""
    out = {"close": None, "forward_pe": None, "peg_ratio": None,
           "one_year_pct": None, "industry_text": ""}
    try:
        row = conn.execute(
            """
            SELECT close, forward_pe, peg_ratio, one_year_pct
            FROM price_daily
            WHERE market=? AND upper(symbol)=upper(?) AND close IS NOT NULL
            ORDER BY trade_date DESC LIMIT 1
            """,
            [US_MARKET, symbol],
        ).fetchone()
        if row:
            out.update(close=row[0], forward_pe=row[1], peg_ratio=row[2], one_year_pct=row[3])
    except Exception:
        pass
    try:
        row = conn.execute(
            """
            SELECT COALESCE(su.theme,''), COALESCE(su.industry,''),
                   COALESCE(cm.chain,''), COALESCE(cm.chain_role,'')
            FROM system_universe su
            LEFT JOIN chain_metadata cm ON cm.market=su.market AND cm.symbol=su.symbol
            WHERE su.market=? AND upper(su.symbol)=upper(?) LIMIT 1
            """,
            [US_MARKET, symbol],
        ).fetchone()
        if row:
            out["industry_text"] = " ".join(str(x) for x in row if x)
    except Exception:
        pass
    return out


def _revision_events_map(conn, symbols: list[str], as_of: date) -> dict[str, list[dict]]:
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    start = date.fromordinal(as_of.toordinal() - 90)
    try:
        rows = conn.execute(
            f"""
            SELECT upper(symbol) AS symbol, event_date, action, price_target_action,
                   price_target, prior_price_target
            FROM analyst_grade_events
            WHERE market=? AND upper(symbol) IN ({placeholders})
              AND event_date BETWEEN ? AND ?
            ORDER BY event_date DESC
            """,
            [US_MARKET, *symbols, start, as_of],
        ).fetchall()
    except Exception:
        return {}
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    for sym, event_date, action, pt_action, target, prior in rows:
        out.setdefault(str(sym).upper(), []).append({
            "event_date": event_date,
            "action": action,
            "price_target_action": pt_action,
            "price_target": target,
            "prior_price_target": prior,
        })
    return out


def _insider_events_map(symbols: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    path = REPO / "data" / "event_calendar_us_form4.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return out
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        sym = str(event.get("ticker") or event.get("symbol") or "").upper()
        if sym in out:
            out[sym].append(event)
    return out


def _strict_pick_payload(data: dict, conn) -> dict:
    """从 US candidate_focus_top10 生成首屏严选 3 只。只读、只解释研究优先级。"""
    from stock_research.core import buy_zone
    from stock_research.core.expectation_meter import expectation_meter, format_meter_line
    from stock_research.core.insider_summary import format_insider_line, summarize_insider_events
    from stock_research.core.revision_trend import format_revision_line, summarize_revision_trend

    us = (data.get("markets") or {}).get(US_MARKET) or {}
    focus_rows = list(us.get("candidate_focus_top10") or [])
    symbols = [str(r.get("symbol") or "").upper() for r in focus_rows if r.get("symbol")]
    zones = buy_zone.compute_buy_zones(symbols, conn)
    intros = _chain_intro_map(conn)
    try:
        as_of = date.fromisoformat(str(us.get("run_date") or data.get("generated_at") or "")[:10])
    except Exception:
        as_of = date.today()
    revision_events = _revision_events_map(conn, symbols, as_of)
    insider_events = _insider_events_map(symbols)
    selected: list[dict] = []
    excluded: list[dict] = []

    for row in sorted(focus_rows, key=lambda r: int(r.get("new_rank") or 9999)):
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        zone = zones.get(symbol)
        move = _price_move_20d(conn, symbol)
        if _position_is_expensive(zone):
            excluded.append({
                "symbol": symbol, "name": row.get("name") or "",
                "reason": "剔贵", "detail": buy_zone.format_line(zone, compact=True),
                "new_rank": row.get("new_rank"),
            })
            continue
        if isinstance(move.get("pct"), (int, float)) and move["pct"] <= STRICT_FALLING_KNIFE_PCT:
            excluded.append({
                "symbol": symbol, "name": row.get("name") or "",
                "reason": "接飞刀", "detail": f"近 20 日 {move['pct']:.1f}%",
                "new_rank": row.get("new_rank"),
            })
            continue
        exp_in = _expectation_inputs(conn, symbol)
        meter = expectation_meter(
            price=exp_in["close"],
            target_price=(zone or {}).get("target"),
            peg_ratio=exp_in["peg_ratio"],
            forward_pe=exp_in["forward_pe"],
            one_year_pct=exp_in["one_year_pct"],
            industry_text=exp_in["industry_text"],
        )
        revision = summarize_revision_trend(revision_events.get(symbol) or [], as_of=as_of)
        insider = summarize_insider_events(insider_events.get(symbol) or [], as_of=as_of)
        selected.append({
            "symbol": symbol,
            "name": row.get("name") or "",
            "new_rank": row.get("new_rank"),
            "prod_rank": row.get("prod_rank"),
            "candidate_score": row.get("candidate_score"),
            "baseline_score": row.get("baseline_score"),
            "intro": _plain_intro(symbol, intros.get(symbol)),
            "reason": _strict_reason(row),
            "buy_zone": zone,
            "buy_zone_line": buy_zone.format_line(zone, compact=True),
            "price_position": (zone or {}).get("position") or "未知",
            "move_20d": move,
            # 🔴 预期透支票换成趋势仓纪律文案（MU 反验后定稿：警示+纪律，不做剔除闸）
            "risk": meter.get("discipline") or _strict_risk(move),
            "expectation": meter,
            "expectation_line": format_meter_line(meter),
            "revision_trend": revision,
            "revision_line": format_revision_line(revision),
            "insider": insider,
            "insider_line": format_insider_line(insider),
        })
        if len(selected) >= STRICT_PICK_N:
            break

    return {
        "generated_at": data.get("generated_at"),
        "market": US_MARKET,
        "source": "dual_track_ranking.markets.US.candidate_focus_top10",
        "source_run_date": us.get("run_date"),
        "formula": CANDIDATE,
        "advisory": "研究严选，不是买入指令；整套策略样本外未达标时仍需买前审查。",
        "rules": {
            "input": "candidate_focus_top10",
            "exclude_expensive": "buy_zone.position == 偏贵",
            "exclude_falling_knife": f"最近20条收盘价涨跌幅 <= {STRICT_FALLING_KNIFE_PCT}%",
            "take": STRICT_PICK_N,
        },
        "picks": selected,
        "excluded": excluded,
        "empty_slots": max(0, STRICT_PICK_N - len(selected)),
    }


def write_outputs(data: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    strict = data.get("strict_picks")
    if strict:
        STRICT_OUT.parent.mkdir(parents=True, exist_ok=True)
        STRICT_OUT.write_text(json.dumps(strict, ensure_ascii=False, indent=2), encoding="utf-8")


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
        out["strict_picks"] = _strict_pick_payload(out, conn)
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
        write_outputs(data)
        print(f"已写 {OUT}")
        print(f"已写 {STRICT_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
