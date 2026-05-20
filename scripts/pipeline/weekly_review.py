"""
每日优选 · 回顾刷新器  (2026-05-11 PM 第二轮:飞书 100% 退役)
─────────────────────────────────────────
从 DuckDB picks 表读所有入选记录,对每只股票:
1. 用 yfinance 拉当前价
2. 计算累计涨跌%（vs 入选时价格）
3. 更新「持有天数」
4. 自动判断「命中评级」
5. 输出本周 / 本月 / 全部回顾报告 + 写入 DuckDB reviews 表

用法:
  python3 weekly_review.py              # 刷新所有记录 + 终端打印报告
  python3 weekly_review.py --period 7   # 仅看最近 7 天的入选
  python3 weekly_review.py --dry-run    # 不写 DuckDB reviews
"""
import sys
import os
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))  # 2026-05-11 lib 迁移
import json
import time
import argparse
from datetime import datetime, timedelta
from collections import defaultdict

from stock_db import DB_PATH, upsert_reviews, get_db  # noqa: E402

import yfinance as yf  # noqa: E402

DATA_DIR = _REPO


def to_yfinance_ticker(code, market):
    """与 fetch_stock_prices.py 中的逻辑一致。"""
    code = (code or "").strip()
    market = market or ""
    if not code:
        return None
    if "." in code:
        return code
    if "港股" in market:
        return f"{code}.HK"
    if "韩股" in market:
        return f"{code}.KS"
    if code.replace("-", "").replace(".", "").isalpha():
        return code
    if code.isdigit() and len(code) == 6:
        if "深交所" in market or code.startswith(("00", "30", "20")):
            return f"{code}.SZ"
        elif "北交所" in market or code.startswith(("8", "9")):
            return f"{code}.BJ"
        return f"{code}.SS"
    return None


def fetch_picks_from_db(*, include_legacy: bool = False):
    """从 DuckDB picks 表读所有入选记录."""
    conn = get_db()
    source_filter = ""
    if not include_legacy:
        source_filter = """
        WHERE COALESCE(model_source, 'legacy_unknown') IN ('v6_us', 'v6_hk', 'v6_cn')
        """
    rows = conn.execute(f"""
        SELECT pick_date, code, name, market, entry_price, entry_currency,
               rating, theme, ai_relevance, COALESCE(model_source, 'legacy_unknown') AS model_source,
               signal
        FROM picks
        {source_filter}
        ORDER BY pick_date DESC, code
    """).fetchall()
    cols = ["pick_date", "code", "name", "market", "entry_price",
            "entry_currency", "rating", "theme", "ai_relevance", "model_source", "signal"]
    out = [dict(zip(cols, r)) for r in rows]
    conn.close()
    return out


def fetch_current_price(yf_ticker):
    try:
        t = yf.Ticker(yf_ticker)
        info = t.info
        return info.get("currentPrice") or info.get("regularMarketPrice")
    except Exception as e:
        print(f"      yfinance 失败: {e}")
        return None


_BENCH_HIST_CACHE: dict[str, dict] = {}


def pick_benchmark(code: str, market: str, model_source: str) -> str:
    code = (code or "").upper()
    market = market or ""
    model_source = model_source or ""
    if model_source == "v6_cn" or code.endswith((".SS", ".SZ")) or "A股" in market:
        return "000300.SS"
    if model_source == "v6_hk" or code.endswith(".HK") or "港股" in market:
        return "^HSI"
    if code.endswith((".KS", ".KQ")) or "韩股" in market:
        return "^KS11"
    if code.endswith(".AX") or "澳股" in market:
        return "^AXJO"
    return "SPY"


def _load_benchmark_history(benchmark: str):
    """拉 benchmark 过去 ~400 天历史 + 最新价，cache module-level。"""
    if benchmark in _BENCH_HIST_CACHE:
        return _BENCH_HIST_CACHE[benchmark]
    try:
        t = yf.Ticker(benchmark)
        end = datetime.now()
        start = end - timedelta(days=400)
        hist = t.history(start=start, end=end)
        if hist is None or hist.empty:
            return {}
        # date → close
        closes = {d.date(): float(c) for d, c in zip(hist.index, hist["Close"])}
        info = t.info
        latest = info.get("currentPrice") or info.get("regularMarketPrice") or float(hist["Close"].iloc[-1])
        _BENCH_HIST_CACHE[benchmark] = {"closes": closes, "latest": float(latest)}
    except Exception as e:
        print(f"  ⚠️  {benchmark} 历史拉取失败 (alpha 字段将留空): {e}")
        _BENCH_HIST_CACHE[benchmark] = {}
    return _BENCH_HIST_CACHE[benchmark]


def _benchmark_at(benchmark: str, d):
    """返回 d 当天或之前最近交易日的 benchmark 收盘价；找不到返回 None。"""
    cache = (_BENCH_HIST_CACHE.get(benchmark) or {}).get("closes") or {}
    if not cache:
        return None
    # 当天或往前找最近 7 个日历日
    for i in range(8):
        key = d - timedelta(days=i)
        if key in cache:
            return cache[key]
    return None


def signal_from_rating(rating: str) -> str:
    """从 rating 文本推断 signal —— 仅作为 DB signal 缺失时的 fallback。

    新代码（2026-05-14+）应直接读 picks.signal 字段；此函数仅给历史 picks（signal IS NULL）兜底。
    """
    text = (rating or "").strip()
    if "⛔" in text or "不建议" in text:
        return "avoid"
    if "观察" in text:
        return "watch"
    return "buy"


def resolve_signal(item: dict) -> str:
    """从 picks item 取 signal，优先用 DB 字段，缺失时按 rating 文本兜底。"""
    sig = item.get("signal")
    if sig:
        return sig
    return signal_from_rating(item.get("rating") or "")


def grade_hit(pct):
    if pct is None:
        return None
    if pct > 15:
        return "🚀 大涨（>15%）"
    if pct > 5:
        return "✅ 命中（5%-15%）"
    if pct >= -5:
        return "🟢 跟随（-5%~+5%）"
    if pct >= -15:
        return "⚠️ 不及预期（-5%~-15%）"
    return "❌ 大跌（<-15%）"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", type=int, default=None, help="只看最近 N 天的入选")
    parser.add_argument("--include-legacy", action="store_true", help="同时回顾 legacy picks（默认只评估 v6 生产线）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("[1/3] 拉取 picks [DuckDB]...")
    items = fetch_picks_from_db(include_legacy=args.include_legacy)
    print(f"  共 {len(items)} 条入选记录")

    print("\n[2/3] 抓当前价格 + 计算回顾...")
    today_date = datetime.now().date()

    period_cutoff = None
    if args.period:
        period_cutoff = today_date - timedelta(days=args.period)

    results = []
    for item in items:
        name = item.get("name") or ""
        code = item.get("code") or ""
        market = item.get("market") or ""
        model_source = item.get("model_source") or "legacy_unknown"
        pick_date = item.get("pick_date")  # DuckDB DATE -> python date
        entry_price = item.get("entry_price")
        currency = item.get("entry_currency") or "USD"

        if not pick_date or not code:
            continue

        # 期限过滤
        if period_cutoff and pick_date < period_cutoff:
            continue

        days_held = (today_date - pick_date).days
        if days_held < 1:
            print(f"  [跳过] {name} — 当日入选未成熟")
            continue

        if not entry_price:
            print(f"  [跳过] {name} — 无入选价")
            continue

        yf_code = to_yfinance_ticker(code, market)
        if not yf_code:
            print(f"  [跳过] {name} — 无法转换 ticker")
            continue

        print(f"  抓取 {name} ({yf_code})...", end=" ")
        current = fetch_current_price(yf_code)
        if not current:
            print("❌ 失败")
            continue

        # 计算
        pct = round((current - entry_price) / entry_price * 100, 2)
        grade = grade_hit(pct)

        # Alpha: stock pct - same-market benchmark pct
        benchmark = pick_benchmark(code, market, model_source)
        bench_cache = _load_benchmark_history(benchmark)
        bench_latest = bench_cache.get("latest")
        entry_spy = _benchmark_at(benchmark, pick_date)
        benchmark_pct = None
        alpha_pct = None
        if entry_spy and bench_latest and entry_spy > 0:
            benchmark_pct = (bench_latest - entry_spy) / entry_spy * 100
            alpha_pct = round(pct - benchmark_pct, 2)
        signal = resolve_signal(item)
        is_success = None
        if alpha_pct is not None:
            is_success = alpha_pct < 0 if signal == "avoid" else alpha_pct > 0

        results.append({
            "name": name,
            "code": code,
            "entry_price": entry_price,
            "current_price": current,
            "pct": pct,
            "days_held": days_held,
            "grade": grade,
            "pick_date": pick_date.strftime("%Y-%m-%d"),
            "rating": item.get("rating") or "",
            "theme": item.get("theme") or "",
            "ai_relevance": item.get("ai_relevance") or "",
            "entry_spy_price": entry_spy,
            "current_spy_price": bench_latest,
            "alpha_pct": alpha_pct,
            "model_source": model_source,
            "signal": signal,
            "benchmark_code": benchmark,
            "benchmark_pct": round(benchmark_pct, 2) if benchmark_pct is not None else None,
            "is_success": is_success,
        })
        sign = "+" if pct > 0 else ""
        alpha_txt = "" if alpha_pct is None else f" · alpha {alpha_pct:+.1f}% vs {benchmark}"
        print(f"{current} {currency} · {sign}{pct:.1f}% · {days_held} 天 · {grade}{alpha_txt}")
        time.sleep(0.4)

    print(f"\n[3/3] 回顾报告")
    print("=" * 60)

    if not results:
        print("  无可回顾的记录")
        return

    # 整体表现
    avg_pct = sum(r["pct"] for r in results) / len(results)
    win_count = sum(1 for r in results if r["pct"] > 5)
    flat_count = sum(1 for r in results if -5 <= r["pct"] <= 5)
    loss_count = sum(1 for r in results if r["pct"] < -5)
    win_rate = win_count / len(results) * 100

    print(f"\n📊 整体表现：")
    print(f"  • 入选总数：{len(results)} 只")
    print(f"  • 平均涨跌：{'+' if avg_pct > 0 else ''}{avg_pct:.2f}%")
    print(f"  • 命中（>+5%）：{win_count} 只 ({win_rate:.1f}%)")
    print(f"  • 跟随（-5%~+5%）：{flat_count} 只")
    print(f"  • 失败（<-5%）：{loss_count} 只")
    alpha_results = [r for r in results if r.get("alpha_pct") is not None]
    if alpha_results:
        avg_alpha = sum(r["alpha_pct"] for r in alpha_results) / len(alpha_results)
        signal_hits = sum(1 for r in alpha_results if r.get("is_success") is True)
        print(f"  • 平均 alpha：{'+' if avg_alpha > 0 else ''}{avg_alpha:.2f}%")
        print(f"  • 按信号命中：{signal_hits} 只 ({signal_hits / len(alpha_results) * 100:.1f}%)")

    # TOP / BOTTOM
    sorted_by_pct = sorted(results, key=lambda x: x["pct"], reverse=True)
    print(f"\n🚀 表现最好 Top 5：")
    for r in sorted_by_pct[:5]:
        sign = "+" if r["pct"] > 0 else ""
        print(f"  • {r['name']:<22} {sign}{r['pct']:>6.2f}% ({r['days_held']} 天) [{r['rating']}]")

    print(f"\n📉 表现最差 Top 5：")
    for r in sorted_by_pct[-5:]:
        sign = "+" if r["pct"] > 0 else ""
        print(f"  • {r['name']:<22} {sign}{r['pct']:>6.2f}% ({r['days_held']} 天) [{r['rating']}]")

    # 评分组别表现
    by_rating = defaultdict(list)
    for r in results:
        if r["rating"]:
            by_rating[r["rating"]].append(r["pct"])

    print(f"\n⭐ 评分 vs 实际表现：")
    for rating, pcts in sorted(by_rating.items(), reverse=True):
        avg = sum(pcts) / len(pcts)
        sign = "+" if avg > 0 else ""
        print(f"  • {rating}（{len(pcts)} 只）：平均 {sign}{avg:.2f}%")

    # 主题表现
    by_theme = defaultdict(list)
    for r in results:
        if r["theme"]:
            by_theme[r["theme"]].append(r["pct"])

    print(f"\n🗂  主题表现：")
    theme_avg = [(t, sum(p)/len(p), len(p)) for t, p in by_theme.items()]
    theme_avg.sort(key=lambda x: x[1], reverse=True)
    for theme, avg, n in theme_avg:
        sign = "+" if avg > 0 else ""
        print(f"  • {theme:<26} 平均 {sign}{avg:>6.2f}% ({n} 只)")

    # 落 DuckDB（每条 review 一行，按 review_date + pick_date + code 去重覆盖）
    try:
        db_rows = [{
            "pick_date": r["pick_date"],
            "code": r["code"],
            "name": r["name"],
            "entry_price": r["entry_price"],
            "current_price": r["current_price"],
            "pct": r["pct"],
            "days_held": r["days_held"],
            "grade": r["grade"],
            "rating": r["rating"],
            "theme": r["theme"],
            "entry_spy_price": r.get("entry_spy_price"),
            "current_spy_price": r.get("current_spy_price"),
            "alpha_pct": r.get("alpha_pct"),
            "model_source": r.get("model_source"),
            "signal": r.get("signal"),
            "benchmark_code": r.get("benchmark_code"),
            "benchmark_pct": r.get("benchmark_pct"),
            "is_success": r.get("is_success"),
        } for r in results]
        n = upsert_reviews(db_rows)
        print(f"\n  DuckDB：已写入 {n} 行 ({DB_PATH} · reviews)")
    except Exception as e:
        print(f"\n  DuckDB 写入失败（不阻塞主流程）：{e}")

    # 保存 JSON 快照
    out_file = os.path.join(DATA_DIR, f"review_{datetime.now().strftime('%Y-%m-%d_%H%M')}.json")
    with open(out_file, "w", encoding="utf-8") as fout:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "summary": {
                "total": len(results),
                "avg_pct": round(avg_pct, 2),
                "win_count": win_count,
                "flat_count": flat_count,
                "loss_count": loss_count,
                "win_rate": round(win_rate, 1),
            },
            "by_rating": {k: round(sum(v)/len(v), 2) for k, v in by_rating.items()},
            "by_theme": {k: round(sum(v)/len(v), 2) for k, v in by_theme.items()},
            "results": results,
        }, fout, ensure_ascii=False, indent=2, default=str)
    print(f"\n  快照已保存：{out_file}")


if __name__ == "__main__":
    main()
