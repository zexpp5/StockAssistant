"""
三市场调仓清单（actionable trade list）
─────────────────────────────────────────
拆分自原单一美股版（2026-05-12 三线独立化）：
  · 美股：plan_a_v5.json    × US 持仓 → trade_delta.json    (兼容文件名，内容来自 v6 risk-aware)
  · 港股：hk_picks.json     × HK 持仓 → trade_delta_hk.json
  · A 股：DuckDB recommendation_picks[CN]（fallback a_share_picks.json）× A 股持仓 → trade_delta_cn.json

为什么不合并到一张调仓单：
  - 三个市场账户独立（美元 / 港元 / 人民币），汇率不同
  - 交易时段不重合（港股 9:30 开盘时美股已收）
  - 仓位算法不同（US risk-aware / hk 等权 / cn sector cap）
  合并会把 weight 含义搞乱，新人无法直接拿去下单

输入持仓：DuckDB holdings 表（前端 /api/holdings 写入）
"""
import sys
import os
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))  # 2026-05-11 lib 迁移
import argparse
import json
from datetime import datetime

import yfinance as yf
import stock_db
from stock_research import config

try:
    TOTAL_CAPITAL = stock_db.get_config("total_capital")
except Exception:
    TOTAL_CAPITAL = 500000  # 默认 50 万 RMB
USD_TO_RMB = 7.10


def _a_share_enabled() -> bool:
    return bool(config.A_SHARE_PRODUCTION_ENABLED)


# ────────────────────────────────────────────────────────
# 市场识别（与 morning_brief / section_picks 对齐）
# ────────────────────────────────────────────────────────
def _market_of(ticker: str) -> str:
    """返回 'us' / 'hk' / 'cn'。"""
    t = (ticker or "").upper()
    if t.endswith(".HK"):
        return "hk"
    if t.endswith((".SS", ".SZ", ".BJ")) or (t.isdigit() and len(t) == 6):
        return "cn"
    return "us"


def _quality_gate_payload() -> dict:
    path = os.path.join(_REPO, "data", "latest", "recommendation_quality_gate.json")
    if not os.path.exists(path):
        return {}
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


def _quality_gate_status(payload: dict | None = None) -> str:
    payload = payload or _quality_gate_payload()
    return str(payload.get("status") or "UNKNOWN")


def _quality_issue_blocks_market(market: str, issue: dict) -> bool:
    """V2 quality_gate 大部分 issue 都是全局基础设施级别（v2_schema/universe/coverage/run）。

    2026-05-21 V1 cutover：原 v6_us/v6_hk/v6_cn source-prefix 路由已废；
    现行规则：
      • details 列表里带 `market` 字段 → 按 market 精确路由（未来增量字段预留）
      • 其它 → 视为全局，FAIL 时阻断所有市场调仓
    """
    details = issue.get("details")
    if isinstance(details, list):
        for row in details:
            if isinstance(row, dict):
                row_market = str(row.get("market") or "").lower()
                if row_market and row_market in {market, market.upper()}:
                    return True
    return True


def _quality_gate_block_reason(market: str, payload: dict) -> str | None:
    if _quality_gate_status(payload) == "FAIL":
        for issue in payload.get("issues") or []:
            if issue.get("level") == "FAIL" and _quality_issue_blocks_market(market, issue):
                return (
                    f"recommendation_quality_gate FAIL({issue.get('code')}) — "
                    "暂停本市场买入/卖出/调仓，先修复数据质量"
                )
    return None


def _infer_fx_to_rmb(ticker: str) -> float:
    """按 ticker 后缀粗略推断换 RMB 汇率。"""
    m = _market_of(ticker)
    if m == "cn":
        return 1.0
    if m == "hk":
        return 0.92          # HKD ≈ 0.92 RMB
    t = (ticker or "").upper()
    if t.endswith(".T"):
        return 0.048         # JPY
    if t.endswith(".KS"):
        return 0.0053        # KRW
    if t.endswith(".L"):
        return 9.0           # GBP
    return USD_TO_RMB         # 默认 USD


def _to_yfinance_ticker(ticker: str) -> str:
    """Normalize local codes to yfinance tickers for price lookup."""
    t = (ticker or "").strip().upper()
    if "." in t:
        return t
    if t.isdigit() and len(t) == 6:
        if t.startswith(("60", "68")):
            return f"{t}.SS"
        if t.startswith(("8", "9", "43")):
            return f"{t}.BJ"
        return f"{t}.SZ"
    return t


def load_current_from_holdings(total_capital: float, market: str | None = None) -> dict:
    """从 DuckDB holdings 表构建当前持仓字典 {ticker: {name, weight, amount_rmb, shares}}。

    market 过滤：'us' / 'hk' / 'cn' / None（全部）
    """
    holdings = stock_db.fetch_all_holdings()
    if not holdings:
        return {}
    # V2 name lookup：manual_watchlist + system_universe（V1 watchlist 表已删）
    try:
        mw = stock_db.fetch_manual_watchlist()
        u = stock_db.fetch_universe_for_ai_recommendations()
        name_map = {r["code"]: r.get("name") or r["code"] for r in mw + u}
    except Exception:
        name_map = {}
    agg: dict = {}
    for h in holdings:
        code = h["code"]
        if market and _market_of(code) != market:
            continue
        shares = float(h.get("shares") or 0)
        ep = float(h.get("entry_price") or 0)
        cost_local = shares * ep
        fx = _infer_fx_to_rmb(code)
        cost_rmb = cost_local * fx
        if code not in agg:
            agg[code] = {"shares": 0.0, "cost_rmb": 0.0, "cost_local": 0.0}
        agg[code]["shares"] += shares
        agg[code]["cost_rmb"] += cost_rmb
        agg[code]["cost_local"] += cost_local
    out = {}
    for code, v in agg.items():
        weight = v["cost_rmb"] / total_capital if total_capital else 0
        out[code] = {
            "name": name_map.get(code, code),
            "weight": weight,
            "amount_rmb": v["cost_rmb"],
            "shares": v["shares"],
        }
    return out


def fetch_price(ticker):
    candidates = [ticker]
    t = (ticker or "").upper()
    for suffix in (".SS", ".SZ", ".BJ", ".HK"):
        if t.endswith(suffix):
            candidates.append(t[:-len(suffix)])
    try:
        conn = stock_db.get_db()
        # V2：price_daily 最新 close，再退到 recommendation_picks.entry_price
        for code in candidates:
            row = conn.execute(
                "SELECT close FROM price_daily WHERE symbol = ? "
                "ORDER BY trade_date DESC, fetched_at DESC LIMIT 1",
                [code],
            ).fetchone()
            if row and row[0]:
                conn.close()
                return float(row[0])
            row = conn.execute(
                "SELECT entry_price FROM recommendation_picks WHERE symbol = ? "
                "AND entry_price IS NOT NULL "
                "ORDER BY rowid DESC LIMIT 1",
                [code],
            ).fetchone()
            if row and row[0]:
                conn.close()
                return float(row[0])
        conn.close()
    except Exception:
        pass
    try:
        h = yf.Ticker(_to_yfinance_ticker(ticker)).history(period="2d")
        return float(h["Close"].iloc[-1])
    except Exception:
        return None


def _load_hk_plan_from_db() -> dict | None:
    """V2 港股 picks（recommendation_picks.market='HK'）作为 hk plan。
    hk_picks.py 走 V1 watchlist 路径，watchlist 空时不写 hk_picks.json。"""
    try:
        conn = stock_db.get_db()
        v2_run = conn.execute(
            """
            SELECT run_id, run_date FROM recommendation_runs
            WHERE universe_scope = 'system_tech_universe' AND status = 'generated'
            ORDER BY generated_at DESC LIMIT 1
            """
        ).fetchone()
        if not v2_run:
            conn.close()
            return None
        run_id, run_date = v2_run
        rows = conn.execute(
            """
            SELECT p.symbol,
                   COALESCE(NULLIF(u.name, p.symbol), p.name) AS name,
                   p.rating, p.total_score
            FROM recommendation_picks p
            LEFT JOIN system_universe u
              ON p.market = u.market AND p.symbol = u.symbol
            WHERE p.run_id = ? AND p.market = 'HK' AND p.signal = 'buy'
            ORDER BY p.total_score DESC NULLS LAST, p.symbol
            """,
            [run_id],
        ).fetchall()
        conn.close()
        if not rows:
            return None
        selected = [{
            "code": symbol, "ticker": symbol, "name": name or symbol,
            "market": "港股", "rating": rating,
            "composite": (float(total_score) / 100) if total_score is not None else None,
            "theme": "科技/AI", "industry": "科技",
        } for symbol, name, rating, total_score in rows]
        return {
            "generated_at": f"{str(run_date)[:10]}T00:00:00",
            "source": "duckdb:recommendation_picks.system_tech_universe[HK]",
            "selected": selected,
            "n_recommended": len(selected),
        }
    except Exception as e:
        print(f"  ⚠️  V2 港股 picks 读取失败：{e}")
        return None


def _load_cn_plan_from_db() -> dict | None:
    """V2 A-share production picks：最新 system_tech_universe run + market='CN'。"""
    try:
        conn = stock_db.get_db()
        v2_run = conn.execute(
            """
            SELECT run_id, run_date FROM recommendation_runs
            WHERE universe_scope = 'system_tech_universe' AND status = 'generated'
            ORDER BY generated_at DESC LIMIT 1
            """
        ).fetchone()
        if v2_run:
            run_id, run_date = v2_run
            v2_rows = conn.execute(
                """
                SELECT p.symbol,
                       COALESCE(NULLIF(u.name, p.symbol), p.name) AS name,
                       p.market, p.rating, p.total_score
                FROM recommendation_picks p
                LEFT JOIN system_universe u
                  ON p.market = u.market AND p.symbol = u.symbol
                WHERE p.run_id = ? AND p.market = 'CN' AND p.signal = 'buy'
                ORDER BY p.total_score DESC NULLS LAST, p.symbol
                """,
                [run_id],
            ).fetchall()
            if v2_rows:
                conn.close()
                selected = [{
                    "code": symbol, "ticker": symbol, "name": name or symbol,
                    "market": market or "A股", "rating": rating,
                    "composite": (float(total_score) / 100) if total_score is not None else None,
                    "theme": "科技/AI", "industry": "科技",
                } for symbol, name, market, rating, total_score in v2_rows]
                return {
                    "generated_at": f"{str(run_date)[:10]}T00:00:00",
                    "source": "duckdb:recommendation_picks.system_tech_universe",
                    "selected": selected,
                    "n_recommended": len(selected),
                }

        conn.close()
    except Exception as e:
        print(f"  ⚠️  V2 A 股 picks 读取失败：{e}")
        return None
    return None


# ────────────────────────────────────────────────────────
# 抽象：把 plan/picks 不同结构统一成 {ticker: {weight, amount_rmb, f_score, composite}}
# ────────────────────────────────────────────────────────
def _normalize_plan(plan_data: dict, market: str, total_capital: float) -> dict:
    """三市场 JSON 结构不同 → 统一返回 {ticker: {weight, amount_rmb, f_score, composite_z}}。"""
    out = {}
    if market == "us":
        for p in plan_data.get("plan_v5", []):
            tk = p["ticker"]
            out[tk] = {
                "weight": p.get("v5_weight", 0),
                "amount_rmb": p.get("amount_rmb", p.get("v5_weight", 0) * total_capital),
                "f_score": p.get("f_score"),
                "composite_z": p.get("composite_z"),
                "name": p.get("name", tk),
            }
    elif market == "hk":
        sel = plan_data.get("selected", [])
        # 港股 hk_picks 没有仓位优化，等权分配
        n = len(sel)
        eq_w = (1.0 / n) if n else 0
        for p in sel:
            tk = p["code"]
            out[tk] = {
                "weight": eq_w,
                "amount_rmb": eq_w * total_capital,
                "f_score": p.get("f_score"),
                "composite_z": p.get("composite"),
                "name": p.get("name", tk),
            }
    elif market == "cn":
        sel = plan_data.get("selected", [])
        n = len(sel)
        eq_w = (1.0 / n) if n else 0
        for p in sel:
            tk = p.get("code") or p.get("ticker")
            f_norm = p.get("f_score_norm")
            out[tk] = {
                "weight": eq_w,
                "amount_rmb": eq_w * total_capital,
                "f_score": int(f_norm * 9) if f_norm is not None else p.get("f_score"),
                "composite_z": p.get("composite"),
                "name": p.get("name", tk),
            }
    return out


# ────────────────────────────────────────────────────────
# 单市场 delta 计算
# ────────────────────────────────────────────────────────
def build_delta(market: str, plan_file: str, out_file: str,
                total_capital: float, currency: str = "RMB"):
    market_label = {"us": "🇺🇸 美股", "hk": "🇭🇰 港股", "cn": "🇨🇳 A 股"}.get(market, market)
    print("=" * 100)
    print(f"  💼 {market_label} 调整清单（基于 {total_capital/10000:.0f} 万 {currency}）")
    print("=" * 100)

    if market == "cn" and not _a_share_enabled():
        payload = {
            "generated_at": datetime.now().isoformat(),
            "market": market,
            "market_label": market_label,
            "total_capital_rmb": total_capital,
            "a_share_production_enabled": False,
            "trade_blocked": True,
            "disabled": True,
            "block_reason": (
                "A 股生产推荐未启用：缺少已验证的 data/calibrated_factor_weights.json；"
                "设置 A_SHARE_PRODUCTION_ENABLED=1 后才生成 A 股调仓单"
            ),
            "sells": [],
            "buys": [],
            "adjusts": [],
            "summary": {
                "total_sell_rmb": 0,
                "total_buy_rmb": 0,
                "total_adjust_rmb": 0,
                "net_cash_need_rmb": 0,
            },
        }
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        print(f"  ⏸️  {payload['block_reason']}")
        print(f"  ✅ {out_file}")
        return payload

    qgate_payload = _quality_gate_payload()
    qgate = _quality_gate_status(qgate_payload)
    block_reason = _quality_gate_block_reason(market, qgate_payload)
    if block_reason:
        payload = {
            "generated_at": datetime.now().isoformat(),
            "market": market,
            "market_label": market_label,
            "total_capital_rmb": total_capital,
            "quality_gate_status": qgate,
            "trade_blocked": True,
            "block_reason": block_reason,
            "sells": [],
            "buys": [],
            "adjusts": [],
            "summary": {
                "total_sell_rmb": 0,
                "total_buy_rmb": 0,
                "total_adjust_rmb": 0,
                "net_cash_need_rmb": 0,
            },
        }
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        print(f"  🔴 {block_reason}")
        print(f"  ✅ {out_file}")
        return payload

    plan = None
    source_label = plan_file
    if market == "cn":
        plan = _load_cn_plan_from_db()
        if plan:
            source_label = plan.get("source", "duckdb:recommendation_picks[CN]")
    elif market == "hk":
        plan = _load_hk_plan_from_db()
        if plan:
            source_label = plan.get("source", "duckdb:recommendation_picks[HK]")

    if plan is None and not os.path.exists(plan_file):
        print(f"  ⚠️  {plan_file} 不存在 — 该市场跳过。")
        print(f"     美股：python3 -m stock_research.jobs.optimize_portfolio / 港股：hk_picks.py / A 股：a_share_picks")
        return None

    if plan is None:
        plan = json.load(open(plan_file, encoding="utf-8"))
    print(f"  目标来源：{source_label}")
    target = _normalize_plan(plan, market, total_capital)
    if not target:
        print(f"  ⚠️  {plan_file} 推荐为空 — 跳过")
        return None
    print(f"  目标组合：{len(target)} 只")

    current = load_current_from_holdings(total_capital, market=market)
    if not current:
        print(f"  ⚠️  无 {market_label} 持仓 → 输出 = 全新建仓清单")
    else:
        print(f"  当前持仓：{len(current)} 只")

    # SELL
    sells = []
    for tk, info in current.items():
        if tk not in target:
            sells.append({
                "ticker": tk, "name": info["name"],
                "current_weight": info["weight"],
                "current_amount": info["amount_rmb"],
            })
    if sells:
        print(f"\n  🔴 卖出（{len(sells)} 只）")
        for s in sells[:8]:
            print(f"    · {s['ticker']:<10} {s['name'][:10]:<12} 当前 {s['current_weight']*100:.1f}% / {s['current_amount']:.0f}")

    # BUY
    buys = []
    for tk, p in target.items():
        if tk not in current:
            price = fetch_price(tk)
            fx = _infer_fx_to_rmb(tk)
            shares = int(p["amount_rmb"] / (price * fx)) if price else None
            buys.append({
                "ticker": tk,
                "name": p["name"],
                "v6_weight": p["weight"],
                "amount_rmb": p["amount_rmb"],
                "price_local": price,
                "shares_estimate": shares,
                "f_score": p["f_score"],
                "composite_z": p["composite_z"],
            })
    if buys:
        print(f"\n  🟢 买入（{len(buys)} 只）")
        for b in buys[:8]:
            f_str = str(b['f_score']) if b['f_score'] is not None else "-"
            z_str = f"{b['composite_z']:+.2f}" if b['composite_z'] is not None else "-"
            price_str = f"{b['price_local']:.2f}" if b['price_local'] else "-"
            shares_str = f"~{b['shares_estimate']}股" if b['shares_estimate'] else "-"
            print(f"    · {b['ticker']:<10} F={f_str:<3} z={z_str:<6} 目标 {b['v6_weight']*100:.1f}% / {b['amount_rmb']:.0f} @ {price_str:<8} {shares_str}")

    # ADJUST
    adjusts = []
    for tk, p in target.items():
        if tk in current:
            cur_w = current[tk]["weight"]
            new_w = p["weight"]
            delta = new_w - cur_w
            if abs(delta) >= 0.01:
                price = fetch_price(tk)
                fx = _infer_fx_to_rmb(tk)
                delta_amount = delta * total_capital
                shares = int(abs(delta_amount) / (price * fx)) if price else None
                adjusts.append({
                    "ticker": tk, "name": current[tk]["name"],
                    "cur_weight": cur_w, "new_weight": new_w,
                    "delta_pct": delta, "delta_amount": delta_amount,
                    "shares_to_trade": shares,
                    "action": "加仓" if delta > 0 else "减仓",
                })
    if adjusts:
        print(f"\n  🟡 调整（{len(adjusts)} 只）")
        for a in adjusts[:8]:
            sign = "+" if a["delta_pct"] > 0 else ""
            print(f"    · {a['ticker']:<10} {a['name'][:10]:<12} {a['cur_weight']*100:.1f}% → {a['new_weight']*100:.1f}% ({sign}{a['delta_pct']*100:.1f}%, {a['action']})")

    # 资金汇总
    total_sell = sum(s["current_amount"] for s in sells)
    total_buy = sum(b["amount_rmb"] for b in buys)
    total_adjust = sum(a["delta_amount"] for a in adjusts)
    net = total_buy + total_adjust - total_sell
    print(f"\n  💰 卖 {total_sell:.0f} / 买 {total_buy:.0f} / 调 {total_adjust:+.0f} → 净 {net:+.0f} {currency}")

    payload = {
        "generated_at": datetime.now().isoformat(),
        "market": market,
        "market_label": market_label,
        "quality_gate_status": qgate,
        "trade_blocked": False,
        "total_capital_rmb": total_capital,
        "sells": sells,
        "buys": buys,
        "adjusts": adjusts,
        "summary": {
            "total_sell_rmb": round(total_sell, 2),
            "total_buy_rmb": round(total_buy, 2),
            "total_adjust_rmb": round(total_adjust, 2),
            "net_cash_need_rmb": round(net, 2),
        },
    }
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  ✅ {out_file}")
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", choices=["us", "hk", "cn", "all"], default="all",
                        help="跑哪个市场（默认 all = 三个都跑）")
    args = parser.parse_args()

    plans = {
        "us": (os.path.join(_REPO, "data", "latest", "plan_a_v5.json"),
               os.path.join(_REPO, "data", "latest", "trade_delta.json")),
        "hk": (os.path.join(_REPO, "data", "latest", "hk_picks.json"),
               os.path.join(_REPO, "data", "latest", "trade_delta_hk.json")),
        "cn": (os.path.join(_REPO, "data", "a_share_picks.json"),
               os.path.join(_REPO, "data", "latest", "trade_delta_cn.json")),
    }
    markets = ["us", "hk", "cn"] if args.market == "all" else [args.market]
    for m in markets:
        plan_file, out_file = plans[m]
        build_delta(m, plan_file, out_file, total_capital=TOTAL_CAPITAL)
        print()


if __name__ == "__main__":
    main()
