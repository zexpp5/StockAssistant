"""
每日优选生成器  ⚠️ LEGACY — v1 启发式打分（"我编的"权重）
─────────────────────────────────────────
**生产体系已迁移到 daily_picks_v5.py（学术因子）。本文件保留原因：**
  1. daily_refresh.sh 第 6 步仍调，作为 v5 的对照基线（A/B 对比）
  2. 提供 fetch_watchlist() 等基础函数，被 v5 / stock_research.jobs.a_share_picks 依赖
  3. 历史看板字段保留旧分数兼容性

**新需求请改 daily_picks_v5.py（美股）或 stock_research/jobs/a_share_picks.py（A 股）。**
**修改本文件前请确认：是否真的要动 legacy？还是新逻辑应该进 v5？**

从 watchlist 37 只里基于多维打分自动选出今日「优质标的」，写入「每日优选 · AI 投资」表。

打分规则（满分 100）：
  • AI 关联度（35 分）：极强 35 / 强 28 / 中 18 / 弱 8 / 无 0
  • 估值（25 分）：PEG < 1 → 25；1-2 → 18；2-3 → 10；>3 → 4；负 PE 或 PEG 缺失但 PE 合理 → 12
  • 趋势（25 分）：1Y > 0 → 15；> 50% → 20；> 200% → 12（已涨过头扣分）；< 0 → 8
                  + 1 周 > 0 → +5
  • 数据可信度（15 分）：高 → 15；中 → 10；低 → 5；无 → 3

⚠️ 上述权重和阈值的「证据状态」：
   跑 `python3 -m stock_research.jobs.calibrate_pick_weights` 生成 data/factor_weights.json，
   本脚本启动时自动加载并打印各因子是否经 IC 实证。无该文件则全部 fallback 到上述 heuristic。

入选规则：综合得分 ≥ 50 分进入「每日优选」
评分等级：
  ⭐⭐⭐ 强烈推荐 ≥ 75
  ⭐⭐ 推荐     ≥ 60
  ⭐ 关注      ≥ 50

用法：
  python3 daily_picks.py                # 生成今日优选并写飞书
  python3 daily_picks.py --top 10       # 只写前 10 只
  python3 daily_picks.py --dry-run      # 不写飞书，只打印
"""
import sys
import os
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))  # 2026-05-11 lib 迁移
import json
import argparse
from datetime import datetime
from pathlib import Path

from stock_db import upsert_picks, fetch_all_watchlist, get_db, latest_price  # noqa: E402


def fetch_watchlist(token=None):
    """从 DuckDB watchlist + prices 读 records 喂给 daily_picks 打分.

    2026-05-11 PM 第二轮:飞书 100% 退役.token 参数仅为兼容旧 caller 保留.
    """
    db_rows = fetch_all_watchlist()
    conn = get_db()
    out = []
    for r in db_rows:
        code = r.get("code")
        px = latest_price(code, conn=conn) if code else None
        px = px or {}
        out.append({
            "name": r.get("name"),
            "code": code,
            "market": r.get("market"),
            "ai_relevance": r.get("ai_relevance"),
            "ai_logic": r.get("ai_logic"),
            "industry": r.get("industry"),
            "conclusion": r.get("conclusion"),
            "risks": r.get("risks"),
            "credibility": r.get("credibility"),
            "latest_price": f"{px.get('price')} {px.get('currency') or ''}".strip()
                            if px.get("price") else "",
            "ytd_pct": px.get("ytd_pct"),
            "one_year_pct": px.get("one_year_pct"),
            "one_month_pct": px.get("one_month_pct"),
            "one_week_pct": px.get("one_week_pct"),
            "forward_pe": px.get("forward_pe"),
            "peg": px.get("peg_ratio"),
            "earnings_growth_pct": px.get("earnings_growth_pct"),
        })
    conn.close()
    return out


# ============================================================
# 主题分类映射（基于代码硬编码）
# ============================================================
THEME_MAPPING = {
    # 🔥 AI 算力核心（GPU/HBM/晶圆代工/超大科技）
    "NVDA": "🔥 AI 算力核心", "AMD": "🔥 AI 算力核心",
    "TSM": "🔥 AI 算力核心", "000660.KS": "🔥 AI 算力核心",
    "GOOGL": "🔥 AI 算力核心", "MSFT": "🔥 AI 算力核心",
    "META": "🔥 AI 算力核心", "AMZN": "🔥 AI 算力核心",
    "688256": "🔥 AI 算力核心", "CRWV": "🔥 AI 算力核心",
    "688041": "🔥 AI 算力核心",
    # 💡 AI 连接（光通信+ASIC+IP）
    "MRVL": "💡 AI 连接（光通信+ASIC）", "AVGO": "💡 AI 连接（光通信+ASIC）",
    "300308": "💡 AI 连接（光通信+ASIC）", "300502": "💡 AI 连接（光通信+ASIC）",
    "688635": "💡 AI 连接（光通信+ASIC）",
    "ALAB": "💡 AI 连接（光通信+ASIC）", "ARM": "💡 AI 连接（光通信+ASIC）",
    # ⚡ AI 电力链
    "GEV": "⚡ AI 电力链", "ETN": "⚡ AI 电力链", "PWR": "⚡ AI 电力链",
    "MTZ": "⚡ AI 电力链", "VRT": "⚡ AI 电力链", "VST": "⚡ AI 电力链",
    "MOD": "⚡ AI 电力链", "002837": "⚡ AI 电力链",
    # 💎 下一波稀缺资源（水/稀土/铀/SMR/AI 数据）
    "XYL": "💎 下一波稀缺资源", "MP": "💎 下一波稀缺资源",
    "CCJ": "💎 下一波稀缺资源", "BWXT": "💎 下一波稀缺资源",
    "RDDT": "💎 下一波稀缺资源",
    "LYC.AX": "💎 下一波稀缺资源", "600111": "💎 下一波稀缺资源",
    "UUUU": "💎 下一波稀缺资源", "KAP.IL": "💎 下一波稀缺资源",
    "LEU": "💎 下一波稀缺资源", "NNE": "💎 下一波稀缺资源",
    "SMR": "💎 下一波稀缺资源", "RYCEY": "💎 下一波稀缺资源",
    "OKLO": "💎 下一波稀缺资源",
    # 🏢 数据中心承载层
    "EQIX": "🏢 数据中心承载层", "ORCL": "🏢 数据中心承载层",
    "LRCX": "🏢 数据中心承载层",
    # 🤖 AI 应用层（SaaS / Agentic / 数据云）
    "SNOW": "🤖 AI 应用层", "PATH": "🤖 AI 应用层",
    "NET": "🤖 AI 应用层", "CDNS": "🤖 AI 应用层", "CRWD": "🤖 AI 应用层",
    "PLTR": "🤖 AI 应用层", "CRM": "🤖 AI 应用层", "NOW": "🤖 AI 应用层",
    "MDB": "🤖 AI 应用层", "CFLT": "🤖 AI 应用层", "DDOG": "🤖 AI 应用层",
    "APX.AX": "🤖 AI 应用层", "688111": "🤖 AI 应用层",
    # 🦾 物理 AI（机器人）
    "SYM": "🦾 物理 AI", "TSLA": "🦾 物理 AI",
    # 🧬 AI 医疗
    "TEM": "🧬 AI 医疗", "RXRX": "🧬 AI 医疗",
    "VEEV": "🧬 AI 医疗", "SDGR": "🧬 AI 医疗",
    # 🇨🇳 中国 AI（中概+港股+A股 AI 主线）
    "3690.HK": "🇨🇳 中国 AI", "9988.HK": "🇨🇳 中国 AI",
    "0700.HK": "🇨🇳 中国 AI", "002230": "🇨🇳 中国 AI",
    "0020.HK": "🇨🇳 中国 AI",
    # 🌏 海外 AI 生态（日股半导体+亚洲 AI）
    "9984.T": "🌏 海外 AI 生态", "6857.T": "🌏 海外 AI 生态",
    "8035.T": "🌏 海外 AI 生态",
    # ⚛️ 量子计算
    "IONQ": "⚛️ 量子计算",
    # 📱 平台/转型
    "AAPL": "📱 平台/转型", "INTC": "📱 平台/转型",
    # 🛡️ 防御 / 消费对照
    "KO": "🛡️ 防御对照", "MCD": "🛡️ 防御对照", "9992.HK": "🛡️ 防御对照",
}


# ============================================================
# 打分函数
# ============================================================

def score_ai_relevance(ar):
    if "极强" in ar:
        return 35
    if "强（直接" in ar or ar == "强":
        return 28
    if "中" in ar:
        return 18
    if "弱" in ar:
        return 8
    return 0


def score_valuation(peg, forward_pe):
    if peg is not None and peg > 0:
        if peg < 1:
            return 25
        if peg < 2:
            return 18
        if peg < 3:
            return 10
        return 4
    # PEG 缺失：用 PE 近似
    if forward_pe is not None and forward_pe > 0:
        if forward_pe < 25:
            return 15
        if forward_pe < 40:
            return 10
        if forward_pe < 60:
            return 6
        return 3
    return 5  # 无估值数据


def score_trend(one_year_pct, one_week_pct, ytd_pct):
    """趋势打分。

    实证依据（calibrate_pick_weights 2026-05-09 跑 16 只 × 6 regime）:
      - 1Y 档位评分（带 >200% 追高扣分）: mean IC = +0.143, IR = +0.42 🟢 strong
      - 1W >0 加 5 分: mean IC = -0.015 🔴 decayed —— 已删除（无预测力）

    one_week_pct / ytd_pct 参数保留是为了向后兼容调用方，函数内不再使用。
    """
    score = 0
    if one_year_pct is not None:
        if one_year_pct > 200:
            score += 12  # 涨太多扣分（追高风险，IC 实证有效）
        elif one_year_pct > 50:
            score += 20
        elif one_year_pct > 0:
            score += 15
        else:
            score += 8
    return min(score, 25)


def score_credibility(cred):
    if "高" in cred:
        return 15
    if "中" in cred:
        return 10
    if "低" in cred:
        return 5
    return 3


def score_record(rec):
    s_ai = score_ai_relevance(rec["ai_relevance"])
    s_val = score_valuation(rec["peg"], rec["forward_pe"])
    s_trend = score_trend(rec["one_year_pct"], rec["one_week_pct"], rec["ytd_pct"])
    s_cred = score_credibility(rec["credibility"])
    total = s_ai + s_val + s_trend + s_cred
    return {
        "total": total,
        "ai": s_ai,
        "val": s_val,
        "trend": s_trend,
        "cred": s_cred,
    }


def grade(total):
    if total >= 75:
        return "⭐⭐⭐ 强烈推荐"
    if total >= 60:
        return "⭐⭐ 推荐"
    if total >= 50:
        return "⭐ 关注"
    return None


# ============================================================
# 入选理由 + 关键看点 + 风险（自动生成）
# ============================================================

def build_reasons(rec, scores):
    """从打分维度生成入选理由"""
    reasons = []

    # AI 关联
    if scores["ai"] >= 28:
        reasons.append(f"✅ AI 关联度高（{rec['ai_relevance']}）")
    elif scores["ai"] >= 18:
        reasons.append(f"⚪ AI 关联度中等（{rec['ai_relevance']}）")

    # 估值
    if rec["peg"] is not None and rec["peg"] > 0:
        if rec["peg"] < 1:
            reasons.append(f"💎 PEG = {rec['peg']:.2f}（< 1，相对便宜）")
        elif rec["peg"] < 2:
            reasons.append(f"✅ PEG = {rec['peg']:.2f}（合理估值）")
        elif rec["peg"] > 3:
            reasons.append(f"⚠️ PEG = {rec['peg']:.2f}（偏贵）")

    if rec["forward_pe"] is not None and rec["forward_pe"] > 0:
        if rec["forward_pe"] < 25:
            reasons.append(f"💰 远期 PE = {rec['forward_pe']:.1f}（不贵）")
        elif rec["forward_pe"] > 60:
            reasons.append(f"⚠️ 远期 PE = {rec['forward_pe']:.1f}（高估值）")

    # 趋势
    if rec["one_year_pct"] is not None:
        if 0 < rec["one_year_pct"] < 50:
            reasons.append(f"📈 一年 +{rec['one_year_pct']:.1f}%（稳健上行）")
        elif 50 <= rec["one_year_pct"] < 200:
            reasons.append(f"🚀 一年 +{rec['one_year_pct']:.1f}%（强势上涨）")
        elif rec["one_year_pct"] >= 200:
            reasons.append(f"🔥 一年 +{rec['one_year_pct']:.1f}%（已涨过较多，注意追高风险）")
        else:
            reasons.append(f"📉 一年 {rec['one_year_pct']:.1f}%（回调中，逆势 / 错杀候选）")

    if rec["one_week_pct"] is not None:
        if rec["one_week_pct"] > 5:
            reasons.append(f"⚡ 1 周 +{rec['one_week_pct']:.1f}%（短期热度高）")
        elif rec["one_week_pct"] < -5:
            reasons.append(f"🔻 1 周 {rec['one_week_pct']:.1f}%（短期承压）")

    # 利润增速
    if rec["earnings_growth_pct"] is not None:
        if rec["earnings_growth_pct"] > 50:
            reasons.append(f"📊 利润增速 +{rec['earnings_growth_pct']:.0f}%（业绩强劲）")
        elif rec["earnings_growth_pct"] > 0:
            reasons.append(f"📊 利润增速 +{rec['earnings_growth_pct']:.0f}%（业绩在兑现）")

    # 数据可信度
    if "高" in rec["credibility"]:
        reasons.append("🟢 数据可信度高（≥2 来源验证）")

    return "\n".join(reasons)


def build_catalysts(rec):
    """从研究结论中提取催化剂线索（简化：取结论前 200 字 + 行业归类）"""
    parts = []
    if rec["industry"]:
        parts.append(f"📂 行业：{rec['industry']}")
    if rec["conclusion"]:
        # 取结论的精华
        conc = rec["conclusion"].replace("\n\n", "\n").strip()
        if len(conc) > 300:
            conc = conc[:300] + "..."
        parts.append(f"💡 研究结论：\n{conc}")
    return "\n\n".join(parts)


def build_risks(rec):
    """从风险字段抽取关键提示"""
    if not rec["risks"]:
        return ""
    risks = rec["risks"].replace("\n\n", "\n").strip()
    if len(risks) > 300:
        risks = risks[:300] + "..."
    return risks


# ============================================================
# 主流程
# ============================================================

_CALIBRATION_PATH = Path(__file__).parent / "data" / "factor_weights.json"


def load_calibration():
    """读 factor_weights.json；缺失/损坏则返回 None（行为退回硬编码 heuristic）。

    本函数仅供透明展示「打分规则是否有 IC 实证支撑」，不直接改变 score_xxx 的数值，
    要把校准权重落到实际打分上，需要进一步重构 score_xxx（见 calibrate_pick_weights.py）。
    """
    if not _CALIBRATION_PATH.exists():
        return None
    try:
        return json.loads(_CALIBRATION_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def print_calibration_status():
    """开跑时打印一行：当前打分规则是否有 IC 实证证据。"""
    calib = load_calibration()
    if not calib:
        print("⚠️  factor_weights.json 缺失 — 当前权重全部为手拍 heuristic（35/25/25/15）")
        print("    跑 `python3 -m stock_research.jobs.calibrate_pick_weights` 生成 IC 实证")
        return
    gen_at = calib.get("generated_at", "未知")
    print(f"📊 factor_weights.json 已加载（{gen_at}）")
    trend_audit = calib.get("calibrated", {}).get("trend", {}).get("ic_audit", {})
    if trend_audit:
        print(f"   趋势子因子 IC 实证:")
        for fname, summary in trend_audit.items():
            ic = summary.get("mean_ic", 0)
            mark = "🟢" if ic >= 0.05 else ("🟡" if ic >= 0.02 else "🔴")
            print(f"     {mark} {fname:<22} mean IC = {ic:+.3f}  IR = {summary.get('ic_ir', 0):+.2f}")
    uncal = calib.get("uncalibrated_heuristic", {})
    if uncal:
        print(f"   未校准（沿用 heuristic，无历史可测）: {', '.join(uncal.keys())}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=12, help="最多写入多少只")
    parser.add_argument("--min-score", type=float, default=50, help="最低入选分数")
    parser.add_argument("--dry-run", action="store_true", help="只打印,不写 DuckDB picks")
    args = parser.parse_args()

    print_calibration_status()
    print()

    print("[1/3] 拉取 watchlist [DuckDB]...")
    records = fetch_watchlist()
    print(f"  共 {len(records)} 条")

    print("\n[2/3] 打分排序...")
    scored = []
    for rec in records:
        s = score_record(rec)
        scored.append((rec, s))

    # 按总分降序
    scored.sort(key=lambda x: x[1]["total"], reverse=True)

    # 截取 top + 应用分数门槛
    selected = [(r, s) for r, s in scored if s["total"] >= args.min_score][: args.top]

    print(f"\n[3/3] 今日入选 {len(selected)} 只（总分 ≥ {args.min_score}）：\n")
    print(f"  {'排名':<4}{'分数':<8}{'股票':<22}{'AI关联':<6}{'估值':<6}{'趋势':<6}{'可信':<6}")
    print(f"  {'-'*60}")
    for i, (r, s) in enumerate(selected, 1):
        print(f"  {i:<4}{s['total']:<8.1f}"
              f"{r['name'][:20]:<22}"
              f"{s['ai']:<6}{s['val']:<6}{s['trend']:<6}{s['cred']:<6}")

    if args.dry_run:
        print("\n[Dry-Run] 未写 DuckDB picks")
        return

    # 落 DuckDB（无论是否新写入飞书，selected 名单都落库做历史回测用）
    if selected:
        try:
            import re
            db_rows = []
            for r, s in selected:
                price_str = r["latest_price"] or ""
                m_price = re.search(r"([\d,]+\.?\d*)", price_str.replace(",", ""))
                m_curr = re.search(r"\b([A-Z]{3})\b", price_str)
                db_rows.append({
                    "code": r["code"],
                    "name": r["name"],
                    "market": r["market"],
                    "rating": grade(s["total"]),
                    "total_score": s["total"],
                    "ai_score": s["ai"],
                    "val_score": s["val"],
                    "trend_score": s["trend"],
                    "cred_score": s["cred"],
                    "ai_relevance": r["ai_relevance"],
                    "theme": THEME_MAPPING.get(r["code"], ""),
                    "entry_price": float(m_price.group(1)) if m_price else None,
                    "entry_currency": m_curr.group(1) if m_curr else None,
                    "peg_at_pick": r["peg"],
                    "fpe_at_pick": r["forward_pe"],
                    "ytd_at_pick": r["ytd_pct"],
                    "one_week_at_pick": r["one_week_pct"],
                    "one_year_at_pick": r["one_year_pct"],
                })
            n = upsert_picks(db_rows)
            print(f"  DuckDB：已写入 {n} 行 (stock_history.duckdb · picks)")
        except Exception as e:
            print(f"  DuckDB 写入失败（不阻塞主流程）：{e}")


if __name__ == "__main__":
    main()
