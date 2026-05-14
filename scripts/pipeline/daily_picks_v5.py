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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import pandas as pd

from factor_model import DEFAULT_FACTOR_WEIGHTS, fetch_factors_for, combine_factors
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


def _load_entry_prices(codes: list[str]) -> dict[str, dict]:
    """Use local daily price snapshots first; fall back to yfinance when needed."""
    out: dict[str, dict] = {}
    if not codes:
        return out

    conn = None
    try:
        from stock_db import get_db, latest_price
        conn = get_db()
        for code in codes:
            px = latest_price(code, conn=conn)
            price = px.get("price") if px else None
            if price:
                out[code] = {
                    "price": float(price),
                    "currency": px.get("currency") or "USD",
                }
    except Exception as e:
        print(f"  ⚠️ 本地 prices 取入选价失败: {e}")
    finally:
        if conn is not None:
            conn.close()

    missing = [c for c in codes if c not in out]
    if not missing:
        return out

    try:
        import yfinance as yf
        df = yf.download(
            " ".join(missing),
            period="5d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
        if df is None or df.empty:
            return out
        for code in missing:
            try:
                if len(missing) == 1 and "Close" in df:
                    close = df["Close"].dropna()
                else:
                    close = df[code]["Close"].dropna()
                if not close.empty:
                    out[code] = {"price": float(close.iloc[-1]), "currency": "USD"}
            except Exception:
                continue
    except Exception as e:
        print(f"  ⚠️ yfinance 入选价兜底失败: {e}")
    return out


def _fetch_factor_bundle(record: dict, as_of_today: str) -> dict:
    """Network-heavy per-ticker bundle for the US picker.

    Keeping this as one unit avoids serial waits between factor, signal, and
    soft-risk calls while still making the main flow easy to cache as before.
    """
    tk = record["code"]
    out = {"ticker": tk, "factor": None, "signal": None, "fundamental": None, "error": None}
    try:
        f = fetch_factors_for(tk, as_of=as_of_today)
        out["factor"] = f
    except Exception as e:
        out["error"] = f"factor: {e}"
        return out

    try:
        out["signal"] = fetch_signals_for(tk, as_of=as_of_today, lookback_days=90)
    except Exception as e:
        out["signal"] = {"ticker": tk, "as_of": as_of_today, "insider": {"error": str(e)}, "analyst": {"error": str(e)}}

    try:
        altman = fundamental_deep.altman_z_score(tk)
        beneish = fundamental_deep.beneish_m_score(tk)
        out["fundamental"] = {"ticker": tk, "altman": altman, "beneish": beneish}
    except Exception as e:
        out["fundamental"] = {"ticker": tk, "error": str(e)}
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["tertile", "median", "quartile"], default="tertile")
    parser.add_argument("--top", type=int, default=12,
                        help="正向非自选候选写入上限；watchlist 自选股始终写入评级")
    parser.add_argument("--neg-top", type=int, default=10,
                        help="负向(⛔不建议)非自选候选写入上限；watchlist 负向股始终写入")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cache", default="data/latest/factor_scores_today.json",
                       help="因子缓存文件，避免重复拉")
    parser.add_argument("--workers", type=int, default=int(os.getenv("STOCK_ASSISTANT_US_WORKERS", "4")),
                       help="缓存未命中时并发拉美股因子的线程数（默认 4）")
    parser.add_argument("--bypass-ic-gate", action="store_true",
                       help="⚠️ 强行跳过因子 IC 闸门（需自担风险，建议先 audit_ic）")
    parser.add_argument("--bypass-audit-gate", action="store_true",
                       help="⚠️ 强行跳过跨源 audit CONFLICT 闸门（需自担风险）")
    args = parser.parse_args()

    # ────────────────────────────────────────────────────────
    # 因子 IC CI 闸门（Grinold-Kahn 行业标准）
    # 生产 composite 等权使用的因子只要未验证/衰减 → 强制 dry-run，避免把噪声当 alpha 写入 buy 推荐
    # ────────────────────────────────────────────────────────
    from stock_research.core.factor_ic_gate import evaluate_gate, format_report
    gate = evaluate_gate()
    print(format_report(gate))
    factor_weights = dict(DEFAULT_FACTOR_WEIGHTS)
    if not gate.passed:
        if args.bypass_ic_gate:
            print("\n⚠️ --bypass-ic-gate：用户强制跳过闸门，继续写入（风险自担）\n")
        else:
            healthy = set(gate.healthy_factors or [])
            if healthy:
                factor_weights = {
                    k: (v if k in healthy else 0.0)
                    for k, v in DEFAULT_FACTOR_WEIGHTS.items()
                }
                dropped = [k for k, v in factor_weights.items() if v <= 0]
                print(
                    "\n🟡 因子 IC 闸门未全通过 → 未验证/衰减因子降权为 0，"
                    f"继续使用 healthy 因子: {', '.join(sorted(healthy))}"
                )
                print(f"   降权因子: {', '.join(dropped)}\n")
            else:
                print("\n🔴 因子 IC 闸门 FAIL 且无 healthy 因子 → 强制 dry-run（不写 DuckDB）")
                print("   修复方法：python3 -m stock_research.jobs.audit_ic  补齐生产因子 IC")
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

    wl_us_records = [r for r in records if is_us_ticker(r["code"])]
    watchlist_codes = {r["code"] for r in wl_us_records}
    us_records = wl_us_records
    print(f"  美股可用因子模型的: {len(us_records)} 只（仅来自手动 watchlist）")
    if not us_records:
        print("  watchlist 为空或无美股标的；不生成自选股 AI 优选。")
        return

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
        workers = max(1, min(args.workers, len(us_records)))
        print(f"  并发 workers={workers}（首次无缓存时生效）")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_fetch_factor_bundle, r, as_of_today): r["code"] for r in us_records}
            for fut in as_completed(futures):
                tk = futures[fut]
                try:
                    bundle = fut.result()
                except Exception as e:
                    print(f"  · {tk:8} 失败: {e}")
                    continue

                if bundle.get("factor"):
                    f = bundle["factor"]
                    factor_results.append(f)
                    f_score = f["piotroski"].get("f_score")
                    mom = f["momentum"].get("momentum_12_1")
                    f_txt = f"F={f_score} M={mom}%"
                else:
                    f_txt = f"失败: {bundle.get('error')}"

                s = bundle.get("signal")
                if s:
                    signal_results.append(s)
                ana_ok = s and s.get("analyst") and "error" not in (s.get("analyst") or {})

                fd = bundle.get("fundamental") or {"ticker": tk, "error": "missing"}
                fundamentals_results.append(fd)
                altman = fd.get("altman") if isinstance(fd, dict) else None
                beneish = fd.get("beneish") if isinstance(fd, dict) else None
                z_val = altman.get("z_score") if altman and not altman.get("error") else None
                m_val = beneish.get("m_score_adjusted") if beneish and not beneish.get("error") else None
                print(
                    f"  · {tk:8} {f_txt} 分析师={'OK' if ana_ok else '-'} "
                    f"Z={z_val if z_val is not None else '-'} M={m_val if m_val is not None else '-'}"
                )

        # 写缓存（增加 fundamentals 字段，旧版可向后兼容读取）
        with open(cache_file, "w", encoding="utf-8") as cf:
            json.dump({"date": today, "factors": factor_results, "signals": signal_results,
                       "fundamentals": fundamentals_results},
                     cf, ensure_ascii=False, indent=2, default=str)

        try:
            from stock_research.core import fmp_client
            health = fmp_client.write_source_health(pipeline="v6_us")
            fmp = (health.get("sources") or {}).get("FMP") or {}
            if fmp.get("status") != "ok":
                print(
                    f"\n  ⚠️ 数据源降级: FMP={fmp.get('status')} "
                    f"({fmp.get('reason')})；Z/M-Score 等软红旗会在界面标注为空"
                )
        except Exception as e:
            print(f"  ⚠️ source_health 写入失败: {e}")

    # ============================================================
    # 3. 合成 + 决策
    # ============================================================
    print(f"\n[3/5] 6 因子合成 + 决策模式：{args.mode}")
    sig_map = {s["ticker"]: s for s in signal_results}
    fundamental_by_code = {x["ticker"]: x for x in fundamentals_results}
    analyst_scores = {
        tk: (None if not s.get("analyst") or "error" in (s.get("analyst") or {})
             else score_analyst(s.get("analyst"))[0])
        for tk, s in sig_map.items()
    }
    insider_scores = {tk: score_insider(s.get("insider"))[0] for tk, s in sig_map.items()}

    composite_df = combine_factors(
        factor_results,
        analyst_signals=analyst_scores,
        include_reversal=True,
        factor_weights=factor_weights,
    )

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
    classify_info_by_code = {
        r["code"]: {
            "sector": r.get("market") or "",
            "industry": r.get("industry") or r.get("ai_relevance") or "",
        }
        for r in us_records
    }

    print(f"\n  cutoff = {cutoff:.2f} ({args.mode})")
    print(f"  推荐 {composite_df['recommended'].sum()} 只 / {len(composite_df)} 只")
    print(f"\n  {'排':<3}{'股票':<22}{'F':>3}{'动量':>8}{'反转':>8}{'分析师':>7}{'内部人':>7}{'综合z':>8}{'推荐'}")
    print(f"  {'-'*85}")

    selected = []
    negatives = []
    watchlist_neutral = []
    NEG_CUTOFF = -0.5  # z ≤ -0.5 标 ⛔ 不建议
    for _, r in composite_df.iterrows():
        tk = r["ticker"]
        name = name_by_code.get(tk, tk)
        f_str = str(int(r['f_score'])) if pd.notna(r['f_score']) else "-"
        m_str = f"{r['momentum']:+.0f}%" if pd.notna(r['momentum']) else "N/A"
        rv_str = f"{r['reversal']:+.1f}%" if pd.notna(r['reversal']) else "N/A"
        ins_score = insider_scores.get(tk, 0)
        rec = bool(r["recommended"])
        z = float(r["composite"]) if pd.notna(r["composite"]) else 0.0
        is_neg = z <= NEG_CUTOFF
        is_watchlist = tk in watchlist_codes
        flag = "✅" if rec else ("⛔" if is_neg else " ")
        print(f"  {int(r['rank']):<3}{name[:18]:<22}{f_str:>3}{m_str:>8}{rv_str:>8}"
              f"{int(r['analyst']):>7}{ins_score:>7}{r['composite']:>+7.2f}    {flag}")

        # picks 表既承载"今日 AI 优选"，也给 dashboard 的 watchlist AI 评级列供数。
        # 因此：生产推荐保留 top/neg-top 限流，但 watchlist 自选股必须全量写入当前评级。
        if rec or is_neg or is_watchlist:
            fd = fundamental_by_code.get(tk) or {}
            altman = fd.get("altman") if fd.get("altman") and not fd.get("altman", {}).get("error") else None
            beneish = fd.get("beneish") if fd.get("beneish") and not fd.get("beneish", {}).get("error") else None
            risk_flags = _build_risk_flags(fd.get("altman"), fd.get("beneish"))
            row = {
                "code": tk,
                "name": name,
                "market": market_by_code.get(tk, "美股"),
                "f_score": int(r["f_score"]) if pd.notna(r["f_score"]) else None,
                "momentum_12_1": float(r["momentum"]) if pd.notna(r["momentum"]) else None,
                "reversal_1m": float(r["reversal"]) if pd.notna(r["reversal"]) else None,
                "analyst_score": int(r["analyst"]),
                "insider_score": int(ins_score),
                "composite_z": z,
                "coverage_score": float(r.get("coverage_score", 0.0)),
                "missing_factors": str(r.get("missing_factors") or ""),
                "factor_weights_used": str(r.get("factor_weights_used") or "{}"),
                "rank": int(r["rank"]),
                "altman_z": (altman or {}).get("z_score"),
                "beneish_m": (beneish or {}).get("m_score_adjusted"),
                "risk_flags": risk_flags,
                "is_watchlist": is_watchlist,
            }
            if rec:
                selected.append(row)
            elif is_neg:
                negatives.append(row)
            else:
                watchlist_neutral.append(row)

    # 限制写入数量: 正向 top N + 负向 bottom N；watchlist 自选股不受限流影响。
    top_selected = selected[:args.top]
    selected_codes = {x["code"] for x in top_selected}
    for x in selected:
        if x["is_watchlist"] and x["code"] not in selected_codes:
            top_selected.append(x)
            selected_codes.add(x["code"])
    selected = top_selected

    negatives.sort(key=lambda x: x["composite_z"])
    watchlist_negatives = [x for x in negatives if x["is_watchlist"]]
    extra_negatives = [x for x in negatives if not x["is_watchlist"]]
    neg_top = min(getattr(args, "neg_top", 10), len(extra_negatives))
    negatives = watchlist_negatives + extra_negatives[:neg_top]
    if negatives:
        print(f"\n  ⛔ 不建议（z ≤ {NEG_CUTOFF}）{len(negatives)} 只: " +
              ", ".join(f"{x['code']}({x['composite_z']:+.2f})" for x in negatives))
    if watchlist_neutral:
        print(f"  ⚠️ 观察（watchlist 中性档）{len(watchlist_neutral)} 只: " +
              ", ".join(f"{x['code']}({x['composite_z']:+.2f})" for x in watchlist_neutral[:20]) +
              (" ..." if len(watchlist_neutral) > 20 else ""))

    if args.dry_run:
        print(
            f"\n[Dry-Run] 不写 DuckDB。"
            f"正向 {len(selected)} + 负向 {len(negatives)} + 观察 {len(watchlist_neutral)} = "
            f"{len(selected) + len(negatives) + len(watchlist_neutral)} 行"
        )
        return

    # ============================================================
    # 4-5. 写 DuckDB picks (2026-05-11 PM 第二轮:飞书 100% 退役)
    # ============================================================
    db_rows = []
    success = 0
    # 结构化 signal 字段：buy/avoid/watch 取代 rating 文本前缀。
    # 消费方默认 WHERE signal='buy'，避免负向标的混入推荐/调仓视图。
    selected_codes_set = {s["code"] for s in selected}
    negative_codes_set = {s["code"] for s in negatives}
    rows_to_write = selected + negatives + watchlist_neutral
    entry_prices = _load_entry_prices([s["code"] for s in rows_to_write])
    if entry_prices:
        print(f"\n  入选价：已填 {len(entry_prices)}/{len(rows_to_write)} 只")
    for s in rows_to_write:
        # GICS 客观分类
        # 使用本地 watchlist/universe 行业标签，避免写 DB 阶段再触发 yfinance.info 网络请求。
        ai_score, theme, sector, industry, source = classify(
            s["code"],
            info=classify_info_by_code.get(s["code"], {}),
        )
        ai_label = score_to_label(ai_score)

        # 星级评定（基于 z-score 客观分位）
        z = s["composite_z"]
        coverage = float(s.get("coverage_score") or 0.0)
        if coverage < 0.50:
            grade_label = f"⭐ 观察（数据覆盖 {coverage:.0%} < 50%，不进 buy）"
        elif z >= 1.0:
            grade_label = "⭐⭐⭐ 强烈推荐（z ≥ 1）"
        elif z >= 0.5:
            grade_label = "⭐⭐ 推荐（z ≥ 0.5）"
        elif z <= NEG_CUTOFF:
            grade_label = f"⛔ 不建议（z ≤ {NEG_CUTOFF}）"
        elif z >= cutoff:
            grade_label = f"⭐ 关注（z ≥ {cutoff:.2f}）"
        else:
            grade_label = f"⭐ 观察（-0.5 < z < {cutoff:.2f}）"

        # 软红旗追加（Z/M-Score 不淘汰，只挂在评级后）
        if s.get("risk_flags"):
            grade_label = grade_label + " · " + "｜".join(s["risk_flags"])

        if coverage < 0.50:
            sig = "watch"
        elif s["code"] in selected_codes_set:
            sig = "buy"
        elif s["code"] in negative_codes_set:
            sig = "avoid"
        else:
            sig = "watch"
        db_rows.append({
            "code": s["code"],
            "name": s["name"],
            "market": s["market"] or "美股",
            "rating": grade_label,
            "total_score": round(z * 100, 2),
            "ai_score": ai_score * 10,
            "val_score": (s["f_score"] or 0) * 3,
            "trend_score": min(int(abs(s["momentum_12_1"])), 25) if s["momentum_12_1"] else 0,
            "cred_score": s["analyst_score"],
            "ai_relevance": ai_label,
            "theme": theme,
            "entry_price": (entry_prices.get(s["code"]) or {}).get("price"),
            "entry_currency": (entry_prices.get(s["code"]) or {}).get("currency"),
            "model_source": "v6_us",
            "signal": sig,
            "coverage_score": coverage,
            "missing_factors": s.get("missing_factors"),
            "factor_weights_used": s.get("factor_weights_used"),
        })
        success += 1

    print(f"\n[4/5] 写 DuckDB picks...")
    if db_rows:
        try:
            n = upsert_picks(db_rows)
            print(f"  DuckDB 写入 {n} 行")
        except Exception as e:
            print(f"  DuckDB 失败: {e}")

    print(
        f"\n✅ 已写入 {success} 行评级"
        f"（正向 {len(selected)} · 负向 {len(negatives)} · 观察 {len(watchlist_neutral)} · "
        "v5 学术因子驱动 · DuckDB picks 已落地）"
    )


if __name__ == "__main__":
    main()
