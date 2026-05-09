"""
每日优选 v5 - 学术因子驱动版
─────────────────────────────────────────
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
import json
import argparse
import time
import requests
from datetime import datetime
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from feishu_auth import feishu_token, FEISHU_APP_TOKEN
from factor_model import fetch_factors_for, combine_factors
from early_signals import fetch_signals_for, score_analyst, score_insider
from gics_classifier import classify, score_to_label
from daily_picks import (
    fetch_watchlist, fetch_existing_picks_today,
    PICKS_TABLE_ID, PICKS_BASE, headers,
)
from stock_db import upsert_picks


# 美股代码识别（yfinance 财报齐全的市场）
def is_us_ticker(code):
    return code.isalpha() and 1 <= len(code) <= 5


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["tertile", "median", "quartile"], default="tertile")
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cache", default="factor_scores_today.json",
                       help="因子缓存文件，避免重复拉")
    args = parser.parse_args()

    token = feishu_token()
    print("[1/5] 拉 watchlist...")
    records = fetch_watchlist(token)
    print(f"  共 {len(records)} 条")

    # 只保留 yfinance 财报齐全的美股（排除 .HK / .SS / .SZ / .KS / .AX）
    us_records = [r for r in records if is_us_ticker(r["code"])]
    print(f"  美股可用因子模型的: {len(us_records)} 只")

    # ============================================================
    # 2. 因子拉取（带缓存，避免重复跑 yfinance）
    # ============================================================
    cache_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.cache)
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
    else:
        print(f"\n[2/5] 拉因子（Piotroski + 动量 + 反转 + 分析师 + 内部人）...")
        factor_results, signal_results = [], []
        for r in us_records:
            tk = r["code"]
            print(f"  · {tk:8} ", end="", flush=True)
            try:
                f = fetch_factors_for(tk, as_of=None)
                factor_results.append(f)
                f_score = f["piotroski"].get("f_score")
                mom = f["momentum"].get("momentum_12_1")
                print(f"F={f_score} M={mom}%", end=" ")
                time.sleep(1.0)
                s = fetch_signals_for(tk, as_of=None, lookback_days=90)
                signal_results.append(s)
                ana_ok = s.get("analyst") and "error" not in (s.get("analyst") or {})
                print(f"分析师={'OK' if ana_ok else '-'}")
                time.sleep(1.0)
            except Exception as e:
                print(f" 失败: {e}")

        # 写缓存
        with open(cache_file, "w", encoding="utf-8") as cf:
            json.dump({"date": today, "factors": factor_results, "signals": signal_results},
                     cf, ensure_ascii=False, indent=2, default=str)

    # ============================================================
    # 3. 合成 + 决策
    # ============================================================
    print(f"\n[3/5] 4 因子合成 + 决策模式：{args.mode}")
    sig_map = {s["ticker"]: s for s in signal_results}
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
            })

    # 限制写入数量
    selected = selected[:args.top]

    if args.dry_run:
        print(f"\n[Dry-Run] 不写入飞书。共 {len(selected)} 只候选")
        return

    # ============================================================
    # 5. 写入飞书 + DuckDB
    # ============================================================
    print(f"\n[4/5] 写入飞书「每日优选」...")
    # v6 学术因子模型与 v1 旧体系并存，不互相跳过 —— 用「入选评分」字段区分
    exclude_codes = set()

    today_ts = int(datetime.strptime(today, "%Y-%m-%d").timestamp() * 1000)
    success = 0
    db_rows = []

    for s in selected:
        if s["code"] in exclude_codes:
            print(f"    · 跳过（今日已入选）: {s['name']}")
            continue

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

        reasons = [
            f"📊 综合 z-score = {z:+.2f}（排名 {s['rank']} / {len(composite_df)}）",
            f"📚 因子组合（学术 4 因子）：",
            f"  · Piotroski F-Score = {s['f_score']}/9（盈利质量）",
            f"  · 12-1 月动量 = {s['momentum_12_1']:+.1f}%（趋势确认）",
            f"  · 1 月反转 = {s['reversal_1m']:+.1f}%（短期 mean reversion）",
            f"  · 分析师上修 90d = {s['analyst_score']}/15 分",
            f"  · 内部人净买入 6m = {s['insider_score']}/15 分（参考）",
            f"🏷 GICS 分类：{ai_label} ({source})",
        ]

        fields = {
            "入选日期": today_ts,
            "股票名称": s["name"],
            "代码": s["code"],
            "市场": s["market"] or "美股",
            "入选评分": grade_label,
            "综合得分": round(z * 100, 2),  # z 转成百分制方便对比
            "AI关联度": ai_label,
            "主题分类": theme,
            "入选理由": "\n".join(reasons),
            "跟踪状态": "🟢 在选中",
            "最近更新": int(datetime.now().timestamp() * 1000),
        }
        fields = {k: v for k, v in fields.items() if v not in (None, "")}

        r = requests.post(f"{PICKS_BASE}/records", headers=headers(token),
                         json={"fields": fields})
        d = r.json()
        if d.get("code") == 0:
            success += 1
            print(f"    + {s['name']} ({s['code']}) → {grade_label}")
        else:
            print(f"    ! 失败 [{s['name']}]: {d.get('msg')}")

        db_rows.append({
            "code": s["code"],
            "name": s["name"],
            "market": s["market"] or "美股",
            "rating": grade_label,
            "total_score": round(z * 100, 2),
            "ai_score": ai_score * 10,  # 0-3 → 0-30
            "val_score": s["f_score"] * 3,  # 0-9 → 0-27
            "trend_score": min(int(abs(s["momentum_12_1"])), 25) if s["momentum_12_1"] else 0,
            "cred_score": s["analyst_score"],
            "ai_relevance": ai_label,
            "theme": theme,
        })

    print(f"\n[5/5] 写 DuckDB...")
    if db_rows:
        try:
            n = upsert_picks(db_rows)
            print(f"  DuckDB 写入 {n} 行")
        except Exception as e:
            print(f"  DuckDB 失败: {e}")

    print(f"\n✅ 已入选 {success} 只（v5 学术因子驱动）")
    print(f"  飞书表：https://w5scrwkn9y.feishu.cn/base/{FEISHU_APP_TOKEN}?table={PICKS_TABLE_ID}")


if __name__ == "__main__":
    main()
