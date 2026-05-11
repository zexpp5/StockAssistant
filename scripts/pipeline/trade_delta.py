"""
方案 A 调整清单（actionable trade list）
─────────────────────────────────────────
基于 v6 学术因子模型 + Markowitz 仓位生成的客观方案 vs 用户**真实持仓**（从 DuckDB holdings 表读），
输出具体的买/卖/调仓清单（金额 + 股数）

输入：
  · plan_a_v5.json (build_plan_a_v5.py 输出)
  · DuckDB holdings 表（前端 /api/holdings 写入的真实持仓 · 2026-05-12 起）

输出：
  · trade_delta.json
  · 控制台打印 Buy / Sell / Adjust 清单
"""
import sys
import os
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))  # 2026-05-11 lib 迁移
import json
from datetime import datetime

import yfinance as yf
import stock_db

try:
    TOTAL_CAPITAL = stock_db.get_config("total_capital")
except Exception:
    TOTAL_CAPITAL = 500000  # 默认 50 万 RMB（DuckDB 不可用时回退）
USD_TO_RMB = 7.10


def _infer_fx_to_rmb(ticker: str) -> float:
    """按 ticker 后缀粗略推断换 RMB 汇率。"""
    t = (ticker or "").upper()
    if t.endswith(".SS") or t.endswith(".SZ"):
        return 1.0           # A 股 RMB
    if t.endswith(".HK"):
        return 0.92          # HKD ≈ 0.92 RMB
    if t.endswith(".T"):
        return 0.048         # JPY ≈ 0.048 RMB
    if t.endswith(".KS"):
        return 0.0053        # KRW ≈ 0.0053 RMB
    if t.endswith(".L"):
        return 9.0           # GBP
    return USD_TO_RMB         # 默认 USD


def load_current_from_holdings(total_capital: float) -> dict:
    """从 DuckDB holdings 表构建当前持仓字典 {ticker: {name, weight, amount_rmb, shares}}。

    - 同一 ticker 多笔买入会聚合（合并 shares + 按持仓额加权平均 entry_price）
    - weight = 该 ticker 总 RMB 成本 / total_capital（按本金分母,跟旧硬编码语义一致）
    - name 从 watchlist 表 join（没匹配就用 code）
    """
    holdings = stock_db.fetch_all_holdings()
    if not holdings:
        return {}
    # name 映射：watchlist code → name
    try:
        watchlist = stock_db.fetch_all_watchlist()
        name_map = {r["code"]: r.get("name") or r["code"] for r in watchlist}
    except Exception:
        name_map = {}
    # 聚合同 ticker 多笔
    agg: dict = {}
    for h in holdings:
        code = h["code"]
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
    # 转输出格式
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
    try:
        h = yf.Ticker(ticker).history(period="2d")
        return float(h["Close"].iloc[-1])
    except Exception:
        return None


def main():
    # 1. 读 v6 plan
    plan_file = os.path.join(_REPO, "data", "latest", "plan_a_v5.json")
    if not os.path.exists(plan_file):
        print(f"❌ {plan_file} 不存在，先跑 build_plan_a_v5.py")
        return
    plan = json.load(open(plan_file, encoding="utf-8"))
    v6 = {p["ticker"]: p for p in plan["plan_v5"]}

    # 2. 当前持仓（从 DuckDB holdings 表读）
    current = load_current_from_holdings(TOTAL_CAPITAL)

    # 3. 计算 delta
    print("=" * 110)
    if current:
        print(f"  💼 方案 A 调整清单：你的真实持仓（{len(current)} 只）vs v6 学术因子优化（基于 {TOTAL_CAPITAL/10000:.0f} 万 RMB）")
    else:
        print(f"  💼 方案 A 调整清单：⚠️ 你还没有持仓（holdings 表空）→ 输出 = 全新建仓清单（基于 {TOTAL_CAPITAL/10000:.0f} 万 RMB）")
        print(f"     提示：先在 dashboard「💼 我的持仓」添加持仓，trade_delta 才能给你真实调仓指令")
    print("=" * 110)
    print(f"\n  组合预期年化 Sharpe = {plan['portfolio_metrics']['annual_sharpe']}")
    print(f"  组合预期年化收益 = {plan['portfolio_metrics']['annual_return_pct']}%")
    print(f"  组合预期年化波动 = {plan['portfolio_metrics']['annual_vol_pct']}%")

    # ============================================================
    # SELL (在当前但 v6 没选)
    # ============================================================
    sells = []
    for tk, info in current.items():
        if tk not in v6:
            sells.append({
                "ticker": tk,
                "name": info["name"],
                "current_weight": info["weight"],
                "current_amount": info["amount_rmb"],
            })

    if sells:
        print(f"\n  🔴 卖出（{len(sells)} 只）—— 当前持有但 v6 因子模型未推荐：")
        print(f"  {'股票':<10}{'代码':<14}{'当前仓位':>10}{'当前金额':>14}{'卖出金额'}")
        print(f"  {'-'*70}")
        for s in sells:
            print(f"  {s['name']:<10}{s['ticker']:<14}{s['current_weight']*100:>9.1f}%"
                  f"{s['current_amount']:>13,.0f}{s['current_amount']:>13,.0f}")

    # ============================================================
    # BUY (v6 有但当前没有)
    # ============================================================
    buys = []
    for tk, p in v6.items():
        if tk not in current:
            price_usd = fetch_price(tk)
            shares = None
            if price_usd:
                price_rmb = price_usd * USD_TO_RMB
                shares = int(p["amount_rmb"] / price_rmb)
            buys.append({
                "ticker": tk,
                "v6_weight": p["v5_weight"],
                "amount_rmb": p["amount_rmb"],
                "price_usd": price_usd,
                "shares_estimate": shares,
                "f_score": p.get("f_score"),
                "composite_z": p.get("composite_z"),
            })

    if buys:
        print(f"\n  🟢 买入（{len(buys)} 只）—— v6 学术因子推荐：")
        print(f"  {'股票':<8}{'F':>3}{'综合z':>7}{'目标仓位':>10}{'目标金额':>12}{'美元价':>10}{'估算股数'}")
        print(f"  {'-'*78}")
        for b in buys:
            f_str = str(b['f_score']) if b['f_score'] is not None else "-"
            z_str = f"{b['composite_z']:+.2f}"
            price_str = f"${b['price_usd']:.2f}" if b['price_usd'] else "-"
            shares_str = f"~{b['shares_estimate']} 股" if b['shares_estimate'] else "-"
            print(f"  {b['ticker']:<8}{f_str:>3}{z_str:>7}{b['v6_weight']*100:>+9.1f}%"
                  f"{b['amount_rmb']:>11,.0f}{price_str:>10}  {shares_str}")

    # ============================================================
    # ADJUST (双方都有但权重不同)
    # ============================================================
    adjusts = []
    for tk, p in v6.items():
        if tk in current:
            cur_w = current[tk]["weight"]
            new_w = p["v5_weight"]
            delta = new_w - cur_w
            if abs(delta) >= 0.01:
                price_usd = fetch_price(tk)
                delta_amount = delta * TOTAL_CAPITAL
                shares = None
                if price_usd:
                    price_rmb = price_usd * USD_TO_RMB
                    shares = int(abs(delta_amount) / price_rmb)
                adjusts.append({
                    "ticker": tk,
                    "name": current[tk]["name"],
                    "cur_weight": cur_w,
                    "new_weight": new_w,
                    "delta_pct": delta,
                    "delta_amount": delta_amount,
                    "shares_to_trade": shares,
                    "action": "加仓" if delta > 0 else "减仓",
                })

    if adjusts:
        print(f"\n  🟡 调整（{len(adjusts)} 只）—— 权重变化超过 1%：")
        print(f"  {'股票':<10}{'代码':<14}{'当前':>7}{'目标':>7}{'变化':>9}{'变动金额':>12}{'股数'}")
        print(f"  {'-'*70}")
        for a in adjusts:
            sign = "+" if a["delta_pct"] > 0 else ""
            print(f"  {a['name']:<10}{a['ticker']:<14}{a['cur_weight']*100:>6.1f}%{a['new_weight']*100:>6.1f}%"
                  f"{sign}{a['delta_pct']*100:>+7.1f}%{a['delta_amount']:>+11,.0f}  ~{a['shares_to_trade'] or '?'}股 {a['action']}")

    # ============================================================
    # 资金流向汇总
    # ============================================================
    total_sell = sum(s["current_amount"] for s in sells)
    total_buy = sum(b["amount_rmb"] for b in buys)
    total_adjust = sum(a["delta_amount"] for a in adjusts)
    print(f"\n  💰 资金流向汇总：")
    print(f"     卖出释放资金:       {total_sell:>12,.0f} RMB")
    print(f"     买入新仓位需要:     {total_buy:>12,.0f} RMB")
    print(f"     调整净变动:         {total_adjust:>+12,.0f} RMB")
    net = total_buy + total_adjust - total_sell
    print(f"     净资金需求:         {net:>+12,.0f} RMB  ({'需要追加现金' if net > 0 else '应有现金结余'})")

    # ============================================================
    # 限制说明
    # ============================================================
    print(f"\n  ⚠️ 实操注意事项：")
    print(f"  1. v6 模型只覆盖 yfinance 财报齐全的美股，A 股（北方稀土/中际/海光）需手动评估")
    print(f"  2. Markowitz 用过去 252 天数据，未来 ≠ 过去")
    print(f"  3. 在熊市这套组合可能比 SPY 多跌 5-15%（已用 walk-forward 验证）")
    print(f"  4. 建议分批建仓（3-5 个交易日内分批买入）减少冲击成本")

    out_file = os.path.join(_REPO, "data", "latest", "trade_delta.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "total_capital_rmb": TOTAL_CAPITAL,
            "sells": sells,
            "buys": buys,
            "adjusts": adjusts,
            "summary": {
                "total_sell_rmb": round(total_sell, 2),
                "total_buy_rmb": round(total_buy, 2),
                "total_adjust_rmb": round(total_adjust, 2),
                "net_cash_need_rmb": round(net, 2),
            },
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n✅ {out_file}")


if __name__ == "__main__":
    main()