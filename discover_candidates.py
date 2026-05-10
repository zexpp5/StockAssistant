"""
候选发现 (Discovery)
─────────────────────────────────────────
扫描更广的 universe（半导体 + 软件 + 大科技 ETF 的全部成分股），
跑同一套学术因子模型，找出**不在当前 watchlist** 但因子得分前列的候选。

为什么要它？
  当前 daily_picks_v5 只对 watchlist 78 只打分排序，永远不会推荐
  watchlist 之外的股票。本脚本补足"发现"这一层。

数据来源（全部 iShares 公开 CSV，免费）:
  · SOXX — 半导体 (~30 只)
  · IGV  — 软件 (~120 只)
  · IGM  — 拓展科技 (~280 只)
  合并去重后 ~250-300 只候选 universe

流水线:
  1. 拉 3 个 ETF 的 holdings CSV → 合并去重
  2. 过滤美股（yfinance 财报齐全）
  3. 排除已在 watchlist 的
  4. 过滤市值 ≥ $5B（剔除小盘股，数据质量差）
  5. 跑 factor_model（Piotroski + 12-1 动量 + PEAD + 分析师）
  6. 取 composite score Top N → 写 JSON 给看板

用法:
  python3 discover_candidates.py                    # 默认全量跑
  python3 discover_candidates.py --top 10           # 只输出 Top 10
  python3 discover_candidates.py --max-universe 50  # 调试用，限制 universe 规模
  python3 discover_candidates.py --dry-run          # 不写 JSON

每周跑一次足够（财报 / ETF 成分股变化慢）。
"""
import sys
import os
import json
import argparse
import time
from io import StringIO
from datetime import datetime
import csv
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from feishu_auth import feishu_token
from daily_picks import fetch_watchlist
from factor_model import fetch_factors_for, combine_factors
from early_signals import fetch_signals_for, score_analyst


# ============================================================
# ETF holdings 数据源
# ============================================================
# iShares 公开 CSV（每天更新）。返回包含 Ticker / Name / Sector / Weight 的表。
# slug 是 iShares fund-id，可以从 fund 页面 URL 拿到。
ISHARES_ETFS = [
    # 美股 + 全球科技
    ("SOXX", "239705/ishares-semiconductor-etf",                   None),  # 半导体 (~45)
    ("IGM",  "239769/ishares-expanded-tech-sector-etf",            None),  # 拓展科技 (~303)
    ("IRBO", "297905/ishares-future-ai-tech-etf",                  None),  # 未来 AI (~88)
    ("BAI",  "339081/ishares-a-i-innovation-and-tech-active-etf",  None),  # AI Active (~66)
    # 中国全市场 → 只取 IT + Communication（剔除金融/消费/地产，~150 只 AI 相关）
    ("MCHI", "239619/ishares-msci-china-etf",
     {"Information Technology", "Communication"}),                         # 中国 (~150)
]


# ============================================================
# Ticker → yfinance 格式映射（基于 iShares Location/Exchange）
# ============================================================
# iShares CSV 里境外 ticker 是裸代码（"1810" / "300308" / "2330"），
# yfinance 需要带交易所后缀。
EXCHANGE_SUFFIX = {
    "China/Shanghai Stock Exchange":          ".SS",
    "China/Shenzhen Stock Exchange":          ".SZ",
    "China/Hong Kong Exchanges And Clearing Ltd": ".HK",
    "Hong Kong":                              ".HK",
    "Taiwan/Taiwan Stock Exchange":           ".TW",
    "Taiwan/Gretai Securities Market":        ".TWO",
    "Korea (South)/Korea Exchange (Stock Market)": ".KS",
    "Japan/Tokyo Stock Exchange":             ".T",
    "Australia/Asx - All Markets":            ".AX",
    "United Kingdom":                         ".L",
}


def to_yfinance_ticker(raw_tk: str, location: str, exchange: str) -> str | None:
    """把 iShares CSV 的裸 ticker 转成 yfinance 能识别的格式。

    示例:
      "300308" + "China/Shenzhen ..."  → "300308.SZ"
      "1810"   + "China/Hong Kong ..." → "1810.HK"
      "2330"   + "Taiwan/Taiwan ..."   → "2330.TW"
      "AMD"    + "United States/..."   → "AMD"（美股不加后缀）
    """
    raw_tk = raw_tk.strip().strip('"')
    if not raw_tk or raw_tk == "-":
        return None
    # 美股直接返回原 ticker（不加 . 后缀）
    if location.startswith("United States"):
        # 排除货币代码 / index futures
        if not raw_tk.replace(".", "").replace("-", "").isalnum():
            return None
        if raw_tk.isalpha() and 1 <= len(raw_tk) <= 5:
            return raw_tk
        # 已经带 . 的 ADR（BRK.B 等），yfinance 接受
        if raw_tk.replace(".", "").isalnum() and 1 <= len(raw_tk) <= 6:
            return raw_tk
        return None
    # 境外：精确匹配，再降级到前缀匹配
    key = f"{location}/{exchange}"
    suffix = EXCHANGE_SUFFIX.get(key)
    if not suffix:
        for k, v in EXCHANGE_SUFFIX.items():
            if key.startswith(k):
                suffix = v
                break
    if not suffix:
        return None
    return f"{raw_tk}{suffix}"


def fetch_ishares_holdings(
    symbol: str, slug: str, sector_filter: set[str] | None = None, timeout: int = 30
) -> list[dict]:
    """拉 iShares ETF holdings CSV 并解析出 ticker 列表。

    iShares CSV 前 9 行是元信息（Fund Name / Inception Date 等），
    第 10 行起是表头 + 数据。

    sector_filter: 如果提供，只保留 sector ∈ filter 的标的（用于 MCHI 这种全市场 ETF
                   只取 IT + Communication，剔除金融/消费等 AI 无关的）。
    """
    url = (
        f"https://www.ishares.com/us/products/{slug}"
        f"/1467271812596.ajax?fileType=csv&fileName={symbol}_holdings&dataType=fund"
    )
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    r.raise_for_status()
    text = r.text.lstrip("﻿")  # 去 BOM

    lines = text.splitlines()
    header_idx = next((i for i, l in enumerate(lines) if l.startswith("Ticker,")), -1)
    if header_idx < 0:
        return []
    body = "\n".join(lines[header_idx:])
    reader = csv.DictReader(StringIO(body))
    out = []
    for row in reader:
        raw_tk = (row.get("Ticker") or "").strip().strip('"')
        if not raw_tk or raw_tk == "-":
            continue
        asset = (row.get("Asset Class") or "").strip().strip('"')
        if asset and asset != "Equity":
            continue
        sector = (row.get("Sector") or "").strip().strip('"')
        if sector_filter and sector not in sector_filter:
            continue
        location = (row.get("Location") or "").strip().strip('"')
        exchange = (row.get("Exchange") or "").strip().strip('"')
        yf_ticker = to_yfinance_ticker(raw_tk, location, exchange)
        if not yf_ticker:
            continue
        weight_str = (row.get("Weight (%)") or "0").replace(",", "").strip().strip('"')
        try:
            weight = float(weight_str)
        except ValueError:
            weight = 0.0
        out.append({
            "ticker": yf_ticker,        # yfinance 可识别的 ticker (300308.SZ 等)
            "raw_ticker": raw_tk,        # iShares 裸代码 (300308)
            "name": (row.get("Name") or "").strip().strip('"'),
            "sector": sector,
            "location": location,
            "weight_pct": weight,
            "etf": symbol,
        })
    return out


# ============================================================
# Universe 构建
# ============================================================
def build_universe(skip_codes: set[str]) -> list[dict]:
    """合并多个 ETF 的成分股 → 去重 → 排除已知 watchlist。

    skip_codes 同时按 yfinance 格式（300308.SZ）和裸代码（300308）匹配，
    保证不论 watchlist 用哪种写法都能正确排除。
    """
    seen = {}
    for symbol, slug, sector_filter in ISHARES_ETFS:
        try:
            print(f"  拉 {symbol} holdings...", end=" ", flush=True)
            holdings = fetch_ishares_holdings(symbol, slug, sector_filter=sector_filter)
            label = f"{len(holdings)} 只"
            if sector_filter:
                label += f"（已限定 sector: {', '.join(sector_filter)}）"
            print(label)
        except Exception as e:
            print(f"❌ 失败: {e}")
            continue
        for h in holdings:
            tk = h["ticker"]
            raw = h.get("raw_ticker", tk)
            # watchlist 排除（同时按 yfinance 格式和裸代码两种方式匹配）
            if tk in skip_codes or raw in skip_codes:
                continue
            if tk not in seen:
                seen[tk] = {
                    "ticker": tk,
                    "raw_ticker": raw,
                    "name": h["name"],
                    "sector": h["sector"],
                    "location": h["location"],
                    "etfs": [],
                    "etf_weight_max": 0.0,
                }
            seen[tk]["etfs"].append(symbol)
            seen[tk]["etf_weight_max"] = max(seen[tk]["etf_weight_max"], h["weight_pct"])
    return list(seen.values())


def filter_by_market_cap(universe: list[dict], min_cap_usd: float = 5e9) -> list[dict]:
    """用 yfinance 拉市值，剔除小盘股。

    小盘股的财报往往不全 / 滞后 / 噪声大，对学术因子模型（尤其 Piotroski）非常不友好。
    """
    import yfinance as yf
    out = []
    print(f"  过滤市值 ≥ ${min_cap_usd / 1e9:.0f}B（共 {len(universe)} 只待筛）...")
    for i, u in enumerate(universe, 1):
        try:
            t = yf.Ticker(u["ticker"])
            cap = t.info.get("marketCap")
            if cap is None or cap < min_cap_usd:
                if i % 20 == 0:
                    print(f"    进度 {i}/{len(universe)}")
                continue
            u["market_cap_usd"] = cap
            out.append(u)
        except Exception:
            continue
        if i % 20 == 0:
            print(f"    进度 {i}/{len(universe)}（已通过 {len(out)}）")
        time.sleep(0.1)  # yfinance rate limit 友好
    print(f"  ✅ 过市值后剩 {len(out)} 只")
    return out


# ============================================================
# 主流程
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=10, help="输出 Top N 候选")
    parser.add_argument("--max-universe", type=int, default=None,
                       help="限制 universe 规模（调试用）")
    parser.add_argument("--min-cap-billion", type=float, default=5.0,
                       help="最低市值（十亿美元）")
    parser.add_argument("--out", default="data/discovery_candidates.json")
    parser.add_argument("--skip-cap-filter", action="store_true",
                       help="跳过市值过滤（加速调试）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("=" * 80)
    print("  🔍 候选发现 — 在 watchlist 之外找因子打分高的股")
    print("=" * 80)

    # ============================================================
    # 1. 当前 watchlist（避免推荐你已经研究过的）
    # ============================================================
    print("\n[1/5] 拉当前 watchlist（用于排除）...")
    token = feishu_token()
    watchlist = fetch_watchlist(token)
    skip_codes = {r["code"].strip() for r in watchlist if r.get("code")}
    print(f"  watchlist 已有 {len(skip_codes)} 只（这些会被排除）")

    # ============================================================
    # 2. 构建 universe
    # ============================================================
    print("\n[2/5] 拉 ETF holdings 构建 universe...")
    universe = build_universe(skip_codes)
    print(f"  合并去重后 universe = {len(universe)} 只（已排除 watchlist）")
    if args.max_universe:
        universe = sorted(universe, key=lambda x: -x["etf_weight_max"])[:args.max_universe]
        print(f"  --max-universe 截断到 {len(universe)} 只")

    # ============================================================
    # 3. 市值过滤（小盘股财报数据质量差，跳过）
    # ============================================================
    if not args.skip_cap_filter:
        print(f"\n[3/5] 市值过滤...")
        universe = filter_by_market_cap(universe, min_cap_usd=args.min_cap_billion * 1e9)
    else:
        print(f"\n[3/5] 跳过市值过滤")

    if not universe:
        print("❌ universe 为空，退出")
        return

    # ============================================================
    # 4. 跑因子模型
    # ============================================================
    print(f"\n[4/5] 跑因子模型（{len(universe)} 只）...")
    print("  · Piotroski F-Score / 12-1 动量 / PEAD / 分析师上修")

    factors = []
    signals = {}
    for i, u in enumerate(universe, 1):
        tk = u["ticker"]
        try:
            print(f"  [{i}/{len(universe)}] {tk:6}", end=" ", flush=True)
            f = fetch_factors_for(tk)
            factors.append(f)
            sig = fetch_signals_for(tk)
            ana_score, _ = score_analyst(sig.get("analyst"))
            signals[tk] = ana_score
            f_v = f["piotroski"]["f_score"]
            m_v = f["momentum"]["momentum_12_1"]
            print(f"F={f_v} mom={m_v}")
            time.sleep(0.8)  # 单只慢一点防 yfinance 限流
        except Exception as e:
            print(f"❌ {e}")
            continue

    if not factors:
        print("❌ 没拉到任何因子数据，退出")
        return

    # ============================================================
    # 5. 横截面合成 + 排序
    # ============================================================
    print(f"\n[5/5] 因子合成 + 输出 Top {args.top}...")
    df = combine_factors(factors, analyst_signals=signals, include_reversal=True)

    # 合并 universe meta（市值 / sector / 来源 ETF）
    meta_map = {u["ticker"]: u for u in universe}
    candidates = []
    for _, row in df.head(args.top).iterrows():
        tk = row["ticker"]
        meta = meta_map.get(tk, {})
        candidates.append({
            "ticker": tk,
            "name": meta.get("name", ""),
            "sector": meta.get("sector", ""),
            "location": meta.get("location", ""),
            "etfs": meta.get("etfs", []),
            "market_cap_usd": meta.get("market_cap_usd"),
            "f_score": None if row["f_score"] != row["f_score"] else float(row["f_score"]),
            "momentum_12_1": None if row["momentum"] != row["momentum"] else float(row["momentum"]),
            "pead": None if row["pead"] != row["pead"] else float(row["pead"]),
            "analyst_score": float(row["analyst"]),
            "composite_z": float(row["composite"]),
            "rank": int(row["rank"]),
        })

    print()
    print(f"  {'排名':<4}{'代码':<8}{'综合z':>8}{'F':>4}{'动量%':>9}{'分析师':>7}{'市值($B)':>11}")
    print(f"  {'-' * 60}")
    for c in candidates:
        cap_b = (c["market_cap_usd"] or 0) / 1e9
        f_str = str(int(c["f_score"])) if c["f_score"] is not None else "-"
        m_str = f"{c['momentum_12_1']:+.1f}" if c["momentum_12_1"] is not None else "-"
        print(
            f"  {c['rank']:<4}{c['ticker']:<8}{c['composite_z']:>+8.2f}"
            f"{f_str:>4}{m_str:>9}{c['analyst_score']:>7.0f}{cap_b:>11.1f}"
        )

    if args.dry_run:
        print("\n[Dry-Run] 未写 JSON")
        return

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "universe_size": len(universe),
        "watchlist_excluded": len(skip_codes),
        "etf_sources": [etf[0] for etf in ISHARES_ETFS],
        "method": "Piotroski F-Score + 12-1 momentum + PEAD + analyst (z-score 等权)",
        "min_market_cap_usd": args.min_cap_billion * 1e9,
        "candidates": candidates,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n✅ {out_path}")
    print(f"\n💡 下一步：在飞书 watchlist 表里手动研究这些标的（行业 / 业务 / 风险），")
    print("    通过的就加入 watchlist；通不过的就丢掉。")
    print("    模型只是缩小搜索空间，不替代你的研究判断。")


if __name__ == "__main__":
    main()
