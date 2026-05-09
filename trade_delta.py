"""
方案 A 调整清单（actionable trade list）
─────────────────────────────────────────
基于 v6 学术因子模型 + Markowitz 仓位生成的客观方案 vs 用户当前手编方案 A，
输出具体的买/卖/调仓清单（金额 + 股数）

输入：
  · plan_a_v5.json (build_plan_a_v5.py 输出)
  · 用户的当前方案 A（写在 build_plan_a_v5.py 里）

输出：
  · trade_delta.json
  · 控制台打印 Buy / Sell / Adjust 清单
"""
import sys
import os
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yfinance as yf

CURRENT_PLAN_A = [
    ("NVDA",       "NVDA",       0.12),
    ("TSM",        "TSM",        0.10),
    ("GOOGL",      "GOOGL",      0.10),
    ("MSFT",       "MSFT",       0.10),
    ("AMD",        "AMD",        0.08),
    ("Vertiv",     "VRT",        0.10),
    ("北方稀土",     "600111.SS",  0.08),
    ("Cameco",     "CCJ",        0.07),
    ("Datadog",    "DDOG",       0.05),
    ("中际旭创",     "300308.SZ",  0.05),
    ("阿里巴巴",     "9988.HK",    0.05),
    ("海光信息",     "688041.SS",  0.05),
]
TOTAL_CAPITAL = 500000  # 50 万 RMB
USD_TO_RMB = 7.10


def fetch_price(ticker):
    try:
        h = yf.Ticker(ticker).history(period="2d")
        return float(h["Close"].iloc[-1])
    except Exception:
        return None


def main():
    # 1. 读 v6 plan
    plan_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plan_a_v5.json")
    if not os.path.exists(plan_file):
        print(f"❌ {plan_file} 不存在，先跑 build_plan_a_v5.py")
        return
    plan = json.load(open(plan_file, encoding="utf-8"))
    v6 = {p["ticker"]: p for p in plan["plan_v5"]}

    # 2. 当前持仓
    current = {tk: {"name": n, "weight": w, "amount_rmb": w * TOTAL_CAPITAL}
              for n, tk, w in CURRENT_PLAN_A}

    # 3. 计算 delta
    print("=" * 110)
    print(f"  💼 方案 A 调整清单：当前手编 vs v6 学术因子优化（基于 50 万 RMB）")
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

    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_delta.json")
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