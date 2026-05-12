"""
每日优选 v5 - 学术因子驱动版  ✅ PRODUCTION（美股）
─────────────────────────────────────────
**这是当前美股选股的主流水线。** daily_refresh.sh 第 9 步调本文件。
A 股不走这个 — A 股看 stock_research/jobs/a_share_picks.py（学术因子 + 龙虎榜 + 北向 + 政策）。
v1 的 daily_picks.py 是 LEGACY，保留作对照基线。

替换 daily_picks.py 中所有"我编的"打分，改用 4 个学术因子：
  1. Piotroski F-Score (Stanford 2000)
  2. 12-1 月动量 (Jegadeesh-Titman 1993)
  3. 1 月反转 (Jegadeesh 1990)
  4. 分析师上修 (Stickel 1991, Womack 1996)

行业分类：用 gics_classifier（industry + override 带 URL）替换 THEME_MAPPING

决策门槛：top tertile（前 1/3） / median（前 1/2，激进）/ quartile（前 1/4，保守）

数据要求：watchlist 中 yfinance 财报齐全的标的（A 股/港股 yfinance 财报缺失，跳过）

用法:
  python3 daily_picks_v5.py --mode tertile        # 默认前 1/3
  python3 daily_picks_v5.py --mode median         # 前 1/2 (更激进)
  python3 daily_picks_v5.py --mode quartile       # 前 1/4 (保守)
  python3 daily_picks_v5.py --dry-run             # 不写飞书
  python3 daily_picks_v5.py --top 10              # 限制写入数量
"""
import sys
import os
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))  # 2026-05-11 lib 迁移
sys.path.insert(0, os.path.join(_REPO, "scripts", "pipeline"))  # sibling: daily_picks
import json
import argparse
import time
from datetime import datetime
import pandas as pd

from factor_model import fetch_factors_for, combine_factors
from early_signals import fetch_signals_for, score_analyst, score_insider
from gics_classifier import classify, score_to_label
from daily_picks import fetch_watchlist
from stock_db import upsert_picks
from stock_research.core import fundamental_deep  # Altman Z / Beneish M 软红旗


# 美股代码识别（yfinance 财报齐全的市场）
def is_us_ticker(code):
    return code.isalpha() and 1 <= len(code) <= 5


def _build_risk_flags(altman: dict | None, beneish: dict | None) -> list[str]:
    """Altman Z / Beneish M 生成软红旗清单（参考二审意见：不淘汰，只标注）。

    Beneish 优先用 m_score_adjusted（已 growth-adjusted）规避高增长伪阳性。
    A 股 / 港股 FMP 无数据时 altman/beneish 含 error，本函数返回空列表。
    """
    flags: list[str] = []
    if altman and not altman.get("error"):
        z = altman.get("z_score")
        if z is not None and z < 1.81:
            flags.append(f"🚨 Altman Z={z:.2f}<1.81 破产警示")
    if beneish and not beneish.get("error"):
        if beneish.get("risk_level") == "high":
            m_adj = beneish.get("m_score_adjusted")
            flags.append(f"🚨 Beneish M={m_adj:.2f}>-1.78 造假风险")
    return flags


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["tertile", "median", "quartile"], default="tertile")
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cache", default="factor_scores_today.json",
                       help="因子缓存文件，避免重复拉")
    parser.add_argument("--bypass-ic-gate", action="store_true",
                       help="⚠️ 强行跳过因子 IC 闸门（需自担风险，建议先 audit_ic）")
    parser.add_argument("--bypass-audit-gate", action="store_true",
                       help="⚠️ 强行跳过跨源 audit CONFLICT 闸门（需自担风险）")
    args = parser.parse_args()

    # ────────────────────────────────────────────────────────
    # 因子 IC CI 闸门（Grinold-Kahn 行业标准）
    # IC<0.03 或 |IR|<0.30 全部因子失效 → 强制 dry-run，避免把噪声当 alpha 写飞书
    # ────────────────────────────────────────────────────────
    from stock_research.core.factor_ic_gate import evaluate_gate, format_report
    gate = evaluate_gate()
    print(format_report(gate))
    if not gate.passed:
        if args.bypass_ic_gate:
            print("\n⚠️ --bypass-ic-gate：用户强制跳过闸门，继续写入（风险自担）\n")
        else:
            print("\n🔴 因子 IC 闸门 FAIL → 强制 dry-run（不写飞书）")
            print("   修复方法：python3 -m stock_research.jobs.audit_ic  然后看哪个因子 healthy")
            print("   或：使用 --bypass-ic-gate 强行通过（不推荐）\n")
            args.dry_run = True

    # ────────────────────────────────────────────────────────
    # 跨源 audit CONFLICT 闸门
    # CONFLICT 比例 ≥ 10% → 数据源疑似系统性故障，强制 dry-run
    # ────────────────────────────────────────────────────────
    from stock_research.core.audit_gate import evaluate_gate as evaluate_audit_gate
    from stock_research.core.audit_gate import format_report as format_audit_report
    audit_gate = evaluate_audit_gate()
    print(format_audit_report(audit_gate))
    if not audit_gate.passed:
        if args.bypass_audit_gate:
            print("\n⚠️ --bypass-audit-gate：用户强制跳过闸门，继续写入（风险自担）\n")
        else:
            print("\n🔴 跨源 audit 闸门 FAIL → 强制 dry-run（不写飞书）")
            print("   修复方法：python3 -m stock_research.jobs.daily_audit  排查冲突源")
            print("   或：使用 --bypass-audit-gate 强行通过（不推荐）\n")
            args.dry_run = True

    print("[1/5] 拉 watchlist [DuckDB]...")
    records = fetch_watchlist()
    print(f"  共 {len(records)} 条")

    # 只保留 yfinance 财报齐全的美股（排除 .HK / .SS / .SZ / .KS / .AX）
    us_records = [r for r in records if is_us_ticker(r["code"])]
    print(f"  美股可用因子模型的: {len(us_records)} 只")

    # ============================================================
    # 2. 因子拉取（带缓存，避免重复跑 yfinance）
    # ============================================================
    cache_file = os.path.join(_REPO, args.cache)
    today = datetime.now().strftime("%Y-%m-%d")
    use_cache = False
    if os.path.exists(cache_file):
        try:
            cached = json.load(open(cache_file, encoding="utf-8"))
            if cached.get("date") == today:
                use_cache = True
        except Exception:
            pass

    if use_cache:
        print(f"\n[2/5] 使用今日因子缓存（{cache_file}）")
        factor_results = cached["factors"]
        signal_results = cached["signals"]
        fundamentals_results = cached.get("fundamentals", [])  # 旧 cache 无此字段
    else:
        print(f"\n[2/5] 拉因子（Piotroski + 动量 + 反转 + 分析师 + 内部人 + Z/M-Score）...")
        # PIT (C-5)：as_of=今日，让 factor_model 过滤掉"今天还没披露的"财报
        as_of_today = datetime.now().strftime("%Y-%m-%d")
        factor_results, signal_results, fundamentals_results = [], [], []
        for r in us_records:
            tk = r["code"]
            print(f"  · {tk:8} ", end="", flush=True)
            try:
                f = fetch_factors_for(tk, as_of=as_of_today)
                factor_results.append(f)
                f_score = f["piotroski"].get("f_score")
                mom = f["momentum"].get("momentum_12_1")
                print(f"F={f_score} M={mom}%", end=" ")
                time.sleep(1.0)
                s = fetch_signals_for(tk, as_of=as_of_today, lookback_days=90)
                signal_results.append(s)
                ana_ok = s.get("analyst") and "error" not in (s.get("analyst") or {})
                print(f"分析师={'OK' if ana_ok else '-'}", end=" ")
                time.sleep(1.0)
                # Altman Z + Beneish M 软红旗 — 失败不影响主流程（FMP 24h 缓存）
                try:
                    altman = fundamental_deep.altman_z_score(tk)
                    beneish = fundamental_deep.beneish_m_score(tk)
                    fundamentals_results.append({"ticker": tk, "altman": altman, "beneish": beneish})
                    z_val = altman.get("z_score") if not altman.get("error") else None
                    m_val = beneish.get("m_score_adjusted") if not beneish.get("error") else None
                    print(f"Z={z_val if z_val is not None else '-'} M={m_val if m_val is not None else '-'}")
                except Exception as fe:
                    fundamentals_results.append({"ticker": tk, "error": str(fe)})
                    print(f"Z/M 跳过: {fe}")
            except Exception as e:
                print(f" 失败: {e}")

        # 写缓存（增加 fundamentals 字段，旧版可向后兼容读取）
        with open(cache_file, "w", encoding="utf-8") as cf:
            json.dump({"date": today, "factors": factor_results, "signals": signal_results,
                       "fundamentals": fundamentals_results},
                     cf, ensure_ascii=False, indent=2, default=str)

    # ============================================================
    # 3. 合成 + 决策
    # ============================================================
    print(f"\n[3/5] 4 因子合成 + 决策模式：{args.mode}")
    sig_map = {s["ticker"]: s for s in signal_results}
    fundamental_by_code = {x["ticker"]: x for x in fundamentals_results}
    analyst_scores = {tk: score_analyst(s.get("analyst"))[0] for tk, s in sig_map.items()}
    insider_scores = {tk: score_insider(s.get("insider"))[0] for tk, s in sig_map.items()}

    composite_df = combine_factors(factor_results, analyst_signals=analyst_scores, include_reversal=True)

    # 决策门槛
    cutoffs = {
        "quartile": composite_df["composite"].quantile(0.75),
        "tertile": composite_df["composite"].quantile(2/3),
        "median": composite_df["composite"].quantile(0.50),
    }
    cutoff = cutoffs[args.mode]
    composite_df["recommended"] = composite_df["composite"] >= cutoff

    # ============================================================
    # 4. 详细输出
    # ============================================================
    name_by_code = {r["code"]: r["name"] for r in us_records}
    market_by_code = {r["code"]: r["market"] for r in us_records}

    print(f"\n  cutoff = {cutoff:.2f} ({args.mode})")
    print(f"  推荐 {composite_df['recommended'].sum()} 只 / {len(composite_df)} 只")
    print(f"\n  {'排':<3}{'股票':<22}{'F':>3}{'动量':>8}{'反转':>8}{'分析师':>7}{'内部人':>7}{'综合z':>8}{'推荐'}")
    print(f"  {'-'*85}")

    selected = []
    for _, r in composite_df.iterrows():
        tk = r["ticker"]
        name = name_by_code.get(tk, tk)
        f_str = str(int(r['f_score'])) if pd.notna(r['f_score']) else "-"
        m_str = f"{r['momentum']:+.0f}%" if pd.notna(r['momentum']) else "N/A"
        rv_str = f"{r['reversal']:+.1f}%" if pd.notna(r['reversal']) else "N/A"
        ins_score = insider_scores.get(tk, 0)
        rec = bool(r["recommended"])
        flag = "✅" if rec else " "
        print(f"  {int(r['rank']):<3}{name[:18]:<22}{f_str:>3}{m_str:>8}{rv_str:>8}"
              f"{int(r['analyst']):>7}{ins_score:>7}{r['composite']:>+7.2f}    {flag}")

        if rec:
            fd = fundamental_by_code.get(tk) or {}
            altman = fd.get("altman") if fd.get("altman") and not fd.get("altman", {}).get("error") else None
            beneish = fd.get("beneish") if fd.get("beneish") and not fd.get("beneish", {}).get("error") else None
            risk_flags = _build_risk_flags(fd.get("altman"), fd.get("beneish"))
            selected.append({
                "code": tk,
                "name": name,
                "market": market_by_code.get(tk, "美股"),
                "f_score": int(r["f_score"]) if pd.notna(r["f_score"]) else None,
                "momentum_12_1": float(r["momentum"]) if pd.notna(r["momentum"]) else None,
                "reversal_1m": float(r["reversal"]) if pd.notna(r["reversal"]) else None,
                "analyst_score": int(r["analyst"]),
                "insider_score": int(ins_score),
                "composite_z": float(r["composite"]),
                "rank": int(r["rank"]),
                "altman_z": (altman or {}).get("z_score"),
                "beneish_m": (beneish or {}).get("m_score_adjusted"),
                "risk_flags": risk_flags,
            })

    # 限制写入数量
    selected = selected[:args.top]

    if args.dry_run:
        print(f"\n[Dry-Run] 不写 DuckDB。共 {len(selected)} 只候选")
        return

    # ============================================================
    # 4-5. 写 DuckDB picks (2026-05-11 PM 第二轮:飞书 100% 退役)
    # ============================================================
    db_rows = []
    success = 0
    for s in selected:
        # GICS 客观分类
        ai_score, theme, sector, industry, source = classify(s["code"])
        ai_label = score_to_label(ai_score)

        # 星级评定（基于 z-score 客观分位）
        z = s["composite_z"]
        if z >= 1.0:
            grade_label = "⭐⭐⭐ 强烈推荐（z ≥ 1）"
        elif z >= 0.5:
            grade_label = "⭐⭐ 推荐（z ≥ 0.5）"
        else:
            grade_label = "⭐ 关注"

        # 软红旗追加（Z/M-Score 不淘汰，只挂在评级后）
        if s.get("risk_flags"):
            grade_label = grade_label + " · " + "｜".join(s["risk_flags"])

        db_rows.append({
            "code": s["code"],
            "name": s["name"],
            "market": s["market"] or "美股",
            "rating": grade_label,
            "total_score": round(z * 100, 2),
            "ai_score": ai_score * 10,
            "val_score": s["f_score"] * 3,
            "trend_score": min(int(abs(s["momentum_12_1"])), 25) if s["momentum_12_1"] else 0,
            "cred_score": s["analyst_score"],
            "ai_relevance": ai_label,
            "theme": theme,
        })
        success += 1

    print(f"\n[4/5] 写 DuckDB picks...")
    if db_rows:
        try:
            n = upsert_picks(db_rows)
            print(f"  DuckDB 写入 {n} 行")
        except Exception as e:
            print(f"  DuckDB 失败: {e}")

    print(f"\n✅ 已入选 {success} 只（v5 学术因子驱动 · DuckDB picks 已落地）")


if __name__ == "__main__":
    main()
