"""
候选发现 (Discovery)
─────────────────────────────────────────
扫描更广的 universe（半导体 + 软件 + 大科技 ETF + A 股/港股科技池），
跑同一套学术因子模型，找出全池因子得分前列的候选。

为什么要它？
  daily_picks_v5 是"今日入选/交易建议"链路；本脚本是"全池发现/横向排名"链路。
  它不以 watchlist 作为筛选边界：已在自选股里的标的也可以进入 AI 推荐榜，
  这样你看到的是完整池子的统一排名，而不是只看自选股之外的补充名单。

数据来源（全部 iShares 公开 CSV，免费）:
  · SOXX — 半导体 (~30 只)
  · IGV  — 软件 (~120 只)
  · IGM  — 拓展科技 (~280 只)
  合并去重后 ~250-300 只候选 universe

流水线:
  1. 拉 3 个 ETF 的 holdings CSV → 合并去重
  2. 过滤美股（yfinance 财报齐全）
  3. 默认不排除 watchlist（可用 --exclude-watchlist 临时恢复旧逻辑）
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
import signal
from contextlib import contextmanager
from io import StringIO
from datetime import datetime
import csv
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts", "pipeline"))  # sibling: daily_picks  # 2026-05-11 lib 迁移

from factor_model import fetch_factors_for, combine_factors
from early_signals import fetch_signals_for, score_analyst


@contextmanager
def time_limit(seconds: int, label: str):
    """Prevent one stalled network call from blocking the whole discovery run."""
    if seconds <= 0:
        yield
        return

    def _raise_timeout(signum, frame):  # noqa: ARG001
        raise TimeoutError(f"{label} timed out after {seconds}s")

    old_handler = signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


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
    # 港股 yfinance 要求 4 位带前导零 (700→0700 / 09988→9988 / 03690→3690)
    if suffix == ".HK" and raw_tk.isdigit():
        raw_tk = raw_tk.lstrip("0").zfill(4)
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
def build_universe(skip_codes: set[str] | None = None) -> list[dict]:
    """合并多个数据源的成分股 → 去重 → 可选排除已知 watchlist。

    数据源：
      1. iShares ETF holdings（美股 + MCHI 中国）
      2. A 股增强 universe (2026-05-12 起，方案 B):
         - 沪深 300 科技子集 (~110)
         - 科创 50 (~50,去重后约 30)
         - 创业板指 (~100,去重后约 75)

    默认 skip_codes 为空，即全池扫描；只有 --exclude-watchlist 才会传入非空集合。
    skip_codes 同时按 yfinance 格式（300308.SZ）和裸代码（300308）匹配。
    """
    skip_codes = skip_codes or set()
    seen = {}
    # ── 1. ETF holdings
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

    # ── 2. A 股增强 universe (方案 B)
    try:
        from stock_research.core.a_share_universe import fetch_a_share_tech_universe
        print(f"  拉 A 股增强 universe (沪深 300 科技子集 + 科创 50 + 创业板指)...", end=" ", flush=True)
        a_share = fetch_a_share_tech_universe()
        print(f"{len(a_share)} 只")
        n_new = 0
        for item in a_share:
            tk = item["ticker"]
            raw = item["raw_ticker"]
            if tk in skip_codes or raw in skip_codes:
                continue
            if tk not in seen:
                seen[tk] = {
                    "ticker": tk,
                    "raw_ticker": raw,
                    "name": item["name"],
                    "sector": item["sector"],
                    "location": item["location"],
                    "etfs": [item["source"]],  # source 作为 "ETF" 标记追溯
                    "etf_weight_max": 0.0,
                }
                n_new += 1
            else:
                # 已在 ETF 里见过 → 追加来源标签
                seen[tk]["etfs"].append(item["source"])
        print(f"    A 股新增 {n_new} 只(其余已在 ETF universe 里)")
    except Exception as e:
        print(f"  ⚠️ A 股 universe 失败(继续用 ETF only): {e}")

    # ── 3. 港股科技龙头白名单(2026-05-11 新增,与 a_share_universe 对称)
    try:
        from stock_research.core.hk_universe import fetch_hk_tech_universe
        print(f"  拉港股科技龙头白名单 (互联网/半导体/新能源车/创新药)...", end=" ", flush=True)
        hk = fetch_hk_tech_universe()
        print(f"{len(hk)} 只", flush=True)
        n_new = 0
        for item in hk:
            tk = item["ticker"]
            raw = item["raw_ticker"]
            # 港股 watchlist 可能写法不一(0700.HK / 00700.HK / 700.HK),三种都要排除
            if tk in skip_codes or raw in skip_codes or f"0{raw}.HK" in skip_codes:
                continue
            if tk not in seen:
                seen[tk] = {
                    "ticker": tk,
                    "raw_ticker": raw,
                    "name": item["name"],
                    "sector": item["sector"],
                    "location": item["location"],
                    "etfs": [item["source"]],
                    "etf_weight_max": 0.0,
                }
                n_new += 1
            else:
                seen[tk]["etfs"].append(item["source"])
        print(f"    港股新增 {n_new} 只(其余已在 ETF universe 里,如 MCHI 含的腾讯/小米)")
    except Exception as e:
        print(f"  ⚠️ 港股 universe 失败(继续): {e}")

    return list(seen.values())


_FX_TO_USD_CACHE: dict[str, float] = {"USD": 1.0}


def _fx_to_usd(ccy: str) -> float:
    """本币 → USD 汇率。用静态 fallback，避免 quote 接口 401/限流影响主流程。"""
    ccy = (ccy or "USD").upper()
    if ccy in _FX_TO_USD_CACHE:
        return _FX_TO_USD_CACHE[ccy]
    fallback = {"CNY": 0.139, "HKD": 0.128, "JPY": 0.0067, "KRW": 0.00074,
                "TWD": 0.031, "EUR": 1.07, "GBP": 1.27, "AUD": 0.66}
    rate = fallback.get(ccy)
    if rate and rate > 0:
        _FX_TO_USD_CACHE[ccy] = float(rate)
        return float(rate)
    return 0.0  # 完全无法换算 → 该股被过滤


def _quote_market_cap(ticker: str) -> tuple[float | None, str]:
    """Fetch market cap and currency through yfinance fast_info.

    Ticker.info can hang for some cross-market tickers; fast_info is much
    lighter and still exposes marketCap/currency for this filter.
    """
    import yfinance as yf
    with time_limit(10, f"marketCap {ticker}"):
        info = dict(yf.Ticker(ticker).fast_info)
    cap = info.get("marketCap")
    ccy = (info.get("currency") or "USD").upper()
    return cap, ccy


def filter_by_market_cap(universe: list[dict], min_cap_usd: float = 5e9) -> list[dict]:
    """用 Yahoo quote 拉市值（含 currency 换算），剔除小盘股。

    Yahoo marketCap 是**本币**计价，A 股/港股/日股等必须按 FX 折算到 USD
    再比阈值，否则 A 股 5B RMB ≈ 700M USD 就能通过 5B USD 闸门（实际放水 7 倍）。
    """
    out = []
    print(f"  过滤市值 ≥ ${min_cap_usd / 1e9:.0f}B（共 {len(universe)} 只待筛）...", flush=True)
    for i, u in enumerate(universe, 1):
        try:
            cap_local, ccy = _quote_market_cap(u["ticker"])
            if cap_local is None:
                if i % 20 == 0:
                    print(f"    进度 {i}/{len(universe)}", flush=True)
                continue
            fx = _fx_to_usd(ccy) if ccy != "USD" else 1.0
            if fx <= 0:
                continue
            cap_usd = cap_local * fx
            if cap_usd < min_cap_usd:
                if i % 20 == 0:
                    print(f"    进度 {i}/{len(universe)}", flush=True)
                continue
            u["market_cap_usd"] = cap_usd
            u["market_cap_local"] = cap_local
            u["currency"] = ccy
            out.append(u)
        except Exception:
            continue
        if i % 20 == 0:
            print(f"    进度 {i}/{len(universe)}（已通过 {len(out)}）", flush=True)
        time.sleep(0.1)
    print(f"  ✅ 过市值后剩 {len(out)} 只 (FX cache: {dict(_FX_TO_USD_CACHE)})", flush=True)
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
    parser.add_argument("--exclude-watchlist", action="store_true",
                       help="旧逻辑：排除已在 watchlist 的标的。默认关闭，AI 推荐走全池排名。")
    parser.add_argument("--skip-cap-filter", action="store_true",
                       help="跳过市值过滤（加速调试）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("=" * 80)
    print("  🔍 候选发现 — 全池因子排名（默认不排除 watchlist）")
    print("=" * 80)

    # ============================================================
    # 1. 扫描范围
    # ============================================================
    if args.exclude_watchlist:
        print("\n[1/5] 拉当前 watchlist [DuckDB]（旧逻辑：用于排除）...")
        from daily_picks import fetch_watchlist
        watchlist = fetch_watchlist()
        skip_codes = {r["code"].strip() for r in watchlist if r.get("code")}
        universe_scope = "outside_watchlist"
        print(f"  watchlist 已有 {len(skip_codes)} 只（这些会被排除）")
    else:
        print("\n[1/5] 扫描范围：全池 universe（不读取/不排除 watchlist）")
        skip_codes = set()
        universe_scope = "all_pool"

    # ============================================================
    # 2. 构建 universe
    # ============================================================
    print("\n[2/5] 拉 ETF holdings 构建 universe...")
    universe = build_universe(skip_codes)
    scope_note = "已排除 watchlist" if args.exclude_watchlist else "未排除 watchlist"
    print(f"  合并去重后 universe = {len(universe)} 只（{scope_note}）")
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
    factor_detail_map: dict[str, dict] = {}   # ticker -> piotroski/momentum/pead 完整 dict
    signal_detail_map: dict[str, dict] = {}   # ticker -> insider/analyst 完整 dict
    for i, u in enumerate(universe, 1):
        tk = u["ticker"]
        try:
            print(f"  [{i}/{len(universe)}] {tk:6}", end=" ", flush=True)
            with time_limit(35, f"factors {tk}"):
                f = fetch_factors_for(tk)
            factors.append(f)
            factor_detail_map[tk] = f
            try:
                with time_limit(20, f"signals {tk}"):
                    sig = fetch_signals_for(tk)
            except Exception as e:
                sig = {}
                print(f"(signals 跳过: {e}) ", end="", flush=True)
            signal_detail_map[tk] = sig
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
        fd = factor_detail_map.get(tk, {}) or {}
        sd = signal_detail_map.get(tk, {}) or {}
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
            # 详细分解 — dashboard expand 区用于组装 ✅ 推荐理由 + ⚠️ 风险点
            "detail": {
                "piotroski_details": (fd.get("piotroski") or {}).get("details", {}),
                "momentum": fd.get("momentum") or {},
                "pead": fd.get("pead") or {},
                "analyst": sd.get("analyst") or {},
                "insider": sd.get("insider") or {},
            },
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

    # args.out 可以是绝对路径或相对当前 cwd 的路径（之前误用 dirname(__file__) 导致写到 scripts/tools/data/）
    out_path = args.out if os.path.isabs(args.out) else os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "universe_size": len(universe),
        "watchlist_excluded": len(skip_codes),
        "universe_scope": universe_scope,
        "exclude_watchlist": bool(args.exclude_watchlist),
        "etf_sources": [etf[0] for etf in ISHARES_ETFS],
        "method": "Piotroski F-Score + 12-1 momentum + PEAD + analyst (z-score 等权)",
        "min_market_cap_usd": args.min_cap_billion * 1e9,
        "candidates": candidates,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n✅ {out_path}")

    # 2026-05-11 PM: 同时落 DuckDB discovery_history 表(永不覆盖累积),
    # 给推荐准确度评估留下时间序列数据。
    try:
        import sys
        _REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))
        from stock_db import upsert_discovery_history
        from datetime import date as _date
        n = upsert_discovery_history(candidates, generated_date=_date.today())
        print(f"✅ 已落 DuckDB discovery_history 表({n} 条 @ {_date.today()})")
    except Exception as e:
        print(f"⚠️ 落 DuckDB discovery_history 失败: {e}")

    print(f"\n💡 下一步：把这份全池排名当作研究线索。")
    print("    不在 watchlist 的标的可加入自选；已在 watchlist 的标的可回到详情页复核。")
    print("    模型只是缩小搜索空间，不替代你的研究判断。")


if __name__ == "__main__":
    main()
