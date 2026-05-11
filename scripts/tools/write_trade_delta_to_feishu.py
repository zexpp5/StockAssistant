"""
把 trade_delta.json 写入飞书「每日优选」表
─────────────────────────────────────────
把 v6 客观因子模型的调整建议（卖/买/调）写到飞书，
方便手机上随时查看
"""
import sys
import os
import json
import requests
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root
from feishu_auth import feishu_token, FEISHU_APP_TOKEN

PICKS_TABLE_ID = "tbl7K88JZ0ZMqPIE"
PICKS_BASE = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{PICKS_TABLE_ID}"


def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def main():
    # 2026-05-11 架构调整：飞书 trade_delta 表废弃为通知入口，trade_delta.json 是 single source of truth
    # 默认 no-op；FEISHU_WRITE_TABLES=1 强制启用（应急快照用）
    if os.environ.get("FEISHU_WRITE_TABLES", "0") != "1":
        print("⏭️  跳过 write_trade_delta_to_feishu（FEISHU_WRITE_TABLES=0）")
        print("    调仓清单已在 trade_delta.json，dashboard 直接读，飞书早安简报里也展示")
        print("    应急写飞书：FEISHU_WRITE_TABLES=1 python3 write_trade_delta_to_feishu.py")
        return

    delta_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_delta.json")
    if not os.path.exists(delta_file):
        print(f"❌ {delta_file} 不存在，先运行 trade_delta.py")
        return
    delta = json.load(open(delta_file, encoding="utf-8"))

    plan_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plan_a_v5.json")
    plan = json.load(open(plan_file, encoding="utf-8"))
    metrics = plan["portfolio_metrics"]

    cache_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factor_scores_today.json")
    cache = json.load(open(cache_file, encoding="utf-8"))
    factor_map = {f["ticker"]: f for f in cache["factors"]}

    token = feishu_token()
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_ts = int(datetime.strptime(today_str, "%Y-%m-%d").timestamp() * 1000)

    rows_written = 0

    print("=" * 90)
    print(f"  📤 写入飞书「每日优选」表（v6 学术因子 + Markowitz 调整建议）")
    print("=" * 90)
    print(f"\n  组合 Sharpe {metrics['annual_sharpe']} · 年化 {metrics['annual_return_pct']}% · 波动 {metrics['annual_vol_pct']}%")

    # ============================================================
    # 写 BUY（v6 推荐买入）
    # ============================================================
    print(f"\n  🟢 写入买入建议 ({len(delta['buys'])} 只)...")
    for b in delta["buys"]:
        tk = b["ticker"]
        f = factor_map.get(tk, {})
        piotroski = f.get("piotroski", {})
        momentum = f.get("momentum", {})
        pead = f.get("pead", {}) or {}

        reasons = [
            f"📊 v6 综合 z = {b['composite_z']:+.2f}（学术因子等权）",
            f"🎯 Markowitz 优化仓位：{b['v6_weight']*100:.1f}%（{b['amount_rmb']:,.0f} RMB）",
            f"📚 因子明细：",
            f"  · Piotroski F = {piotroski.get('f_score', '?')}/9",
            f"  · 12-1 月动量 = {momentum.get('momentum_12_1', '?')}%",
            f"  · 1 月反转 = {momentum.get('reversal_1m', '?')}%",
            f"  · PEAD 加速度 = {pead.get('acceleration', '?')} ({pead.get('method', '-')})",
            f"💡 建议：分批 3-5 个交易日买入约 {b['shares_estimate']} 股（@${b['price_usd']:.2f}）",
        ]

        fields = {
            "入选日期": today_ts,
            "股票名称": tk,
            "代码": tk,
            "市场": "美股",
            "入选评分": "🟢 v6 推荐买入",
            "综合得分": round(b["composite_z"] * 100, 1),
            "入选时价格": f"${b['price_usd']:.2f}" if b.get("price_usd") else "",
            "AI关联度": f"F-Score {piotroski.get('f_score', '?')}",
            "主题分类": "v6 学术因子",
            "入选理由": "\n".join(reasons),
            "关键看点（催化剂）": f"Markowitz Max Sharpe = {metrics['annual_sharpe']}（年化 {metrics['annual_return_pct']}%）",
            "风险提示": "⚠️ 熊市可能跑输 SPY 5-15%（walk-forward 实测）；分批建仓减少冲击",
            "跟踪状态": "🟢 在选中",
            "最近更新": int(datetime.now().timestamp() * 1000),
        }

        r = requests.post(f"{PICKS_BASE}/records", headers=headers(token),
                         json={"fields": fields})
        if r.json().get("code") == 0:
            rows_written += 1
            print(f"    + 买入 {tk:<8} z={b['composite_z']:+.2f} 仓位={b['v6_weight']*100:.1f}%")
        else:
            print(f"    ! 失败 {tk}: {r.json().get('msg')}")

    # ============================================================
    # 写 SELL（v6 不再推荐）
    # ============================================================
    print(f"\n  🔴 写入卖出建议 ({len(delta['sells'])} 只)...")
    for s in delta["sells"]:
        tk = s["ticker"]
        reason = (
            f"📊 v6 因子模型未把它排进 Top 19\n"
            f"💼 当前仓位：{s['current_weight']*100:.1f}%（{s['current_amount']:,.0f} RMB）\n"
            f"💡 建议全部卖出，资金转向 v6 推荐标的"
        )
        if tk in factor_map:
            f = factor_map[tk]
            piotroski = f.get("piotroski", {})
            momentum = f.get("momentum", {})
            reason += (
                f"\n📚 因子明细：\n"
                f"  · Piotroski F = {piotroski.get('f_score', '?')}/9\n"
                f"  · 12-1 月动量 = {momentum.get('momentum_12_1', '?')}%\n"
                f"  · 1 月反转 = {momentum.get('reversal_1m', '?')}%"
            )
        else:
            reason += f"\n⚠️ A 股 / 港股 yfinance 数据缺失，无法因子打分（盲区）"

        fields = {
            "入选日期": today_ts,
            "股票名称": s["name"],
            "代码": tk,
            "市场": "美股" if not (".SS" in tk or ".SZ" in tk or ".HK" in tk) else ("A股" if (".SS" in tk or ".SZ" in tk) else "港股"),
            "入选评分": "🔴 v6 建议卖出",
            "综合得分": -1,
            "AI关联度": "v6 未推荐",
            "主题分类": "卖出建议",
            "入选理由": reason,
            "风险提示": "若坚定看好，可保留观察；模型不能完全替代基本面研究",
            "跟踪状态": "🔴 已卖出",
            "最近更新": int(datetime.now().timestamp() * 1000),
        }

        r = requests.post(f"{PICKS_BASE}/records", headers=headers(token),
                         json={"fields": fields})
        if r.json().get("code") == 0:
            rows_written += 1
            print(f"    + 卖出 {tk:<14} 当前仓位 {s['current_weight']*100:.0f}%")
        else:
            print(f"    ! 失败 {tk}: {r.json().get('msg')}")

    # ============================================================
    # 写 ADJUST
    # ============================================================
    print(f"\n  🟡 写入调整建议 ({len(delta['adjusts'])} 只)...")
    for a in delta["adjusts"]:
        tk = a["ticker"]
        reason = (
            f"📊 v6 综合 z 进 Top 19，但建议{a['action']}\n"
            f"💼 当前 {a['cur_weight']*100:.0f}% → 目标 {a['new_weight']*100:.1f}%\n"
            f"💰 变动金额 {a['delta_amount']:+,.0f} RMB（约 {a['shares_to_trade']} 股）"
        )
        fields = {
            "入选日期": today_ts,
            "股票名称": a["name"],
            "代码": tk,
            "市场": "美股",
            "入选评分": f"🟡 v6 建议{a['action']}",
            "综合得分": 0,
            "AI关联度": "v6 调整",
            "主题分类": "仓位优化",
            "入选理由": reason,
            "跟踪状态": "🟡 调仓中",
            "最近更新": int(datetime.now().timestamp() * 1000),
        }

        r = requests.post(f"{PICKS_BASE}/records", headers=headers(token),
                         json={"fields": fields})
        if r.json().get("code") == 0:
            rows_written += 1
            print(f"    + {a['action']} {tk}: {a['cur_weight']*100:.0f}% → {a['new_weight']*100:.1f}%")
        else:
            print(f"    ! 失败 {tk}: {r.json().get('msg')}")

    # ============================================================
    print(f"\n✅ 共写入 {rows_written} 条到飞书「每日优选」表")
    print(f"  飞书表：https://w5scrwkn9y.feishu.cn/base/{FEISHU_APP_TOKEN}?table={PICKS_TABLE_ID}")


if __name__ == "__main__":
    main()