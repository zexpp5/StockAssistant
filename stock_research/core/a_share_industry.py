"""A 股行业分类（GICS via yfinance）— A-6 (2026-05-12)。

为什么用 GICS 不用申万：
  - 申万 cons API (akshare sw_index_*_cons) 有 bug，拉不到 ticker → 行业映射
  - 东方财富 API (push2) 被代理挡，不稳定
  - yfinance.Ticker.info 对 A 股稳定返回 sector + industry（GICS 体系）
  - GICS 是国际通行标准，便于跨市场对比（与美股 daily_picks_v5 的 gics_classifier 体系一致）

实测（2026-05-12）：
  600519.SS  茅台    → sector=Consumer Defensive, industry=Beverages - Wineries & Distilleries
  300750.SZ  宁德    → sector=Industrials,        industry=Electrical Equipment & Parts
  688256.SS  寒武纪  → sector=Technology,         industry=Semiconductors

用法：
  # 单只查询（带缓存）
  from stock_research.core.a_share_industry import get_industry
  info = get_industry("600519")  # → {"sector": "Consumer Defensive", "industry": "...", "z_prime_inapplicable": False}

  # 批量为 watchlist 构建缓存（每月跑一次，约 200 只 / 5 分钟）
  python3 -m stock_research.core.a_share_industry --refresh

  # 在 apply_a_share_constraints 里反查 industries map（优先用 cache）
  from stock_research.core.a_share_industry import bulk_get_industry
  industries_map = bulk_get_industry([entry["ticker"] for entry in entries])

缓存：
  data/cache/a_share_industry.json   每只 A 股的 GICS 分类 + 时间戳
  TTL：30 天（行业归属变化频率低，月度刷新足够）

Z'' / M-Score 适用性：
  Altman Z''-Score 对金融 / 地产 / 平台 / 公用事业 不适用（资产负债结构特殊）。
  本模块标注 z_prime_inapplicable=True 时，a_share_picks 的软红旗逻辑应跳过 Z 校验。
"""
from __future__ import annotations
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
CACHE_PATH = REPO / "data" / "cache" / "a_share_industry.json"
CACHE_TTL_DAYS = 30  # 30 天 TTL（行业归属稳定，月度刷新足够）

# Z'' / M-Score 不适用的 GICS sector
Z_PRIME_INAPPLICABLE_SECTORS = {
    "Financial Services",
    "Financials",           # 银行 / 证券 / 保险（资产负债结构特殊）
    "Real Estate",          # 地产（资产端是存货，负债端是预售款，不可比）
    "Utilities",            # 公用事业（受管制 ROE，会让 X3 偏低）
}

# 平台 / 互联网公司虽是 Technology / Communication Services，但商业模式特殊
# 这里不主动排除（GICS 没给"平台"维度），由 risk_flags 上游判断


def _normalize_a_share_ticker(ticker: str) -> str:
    """A 股代码 → yfinance ticker（与 a_share_fundamental_deep 对齐）。"""
    t = str(ticker).upper().strip()
    if t.endswith((".SS", ".SZ", ".BJ")):
        return t
    code = t.replace(".SS", "").replace(".SZ", "").replace(".BJ", "")
    if not (code.isdigit() and len(code) == 6):
        return t
    if code.startswith("6"):
        return f"{code}.SS"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return t


def _strip_code(ticker: str) -> str:
    """yfinance ticker → 6 位代码（与 apply_a_share_constraints 对齐）。"""
    t = str(ticker).upper().strip()
    for suffix in (".SS", ".SZ", ".BJ"):
        if t.endswith(suffix):
            return t[:-len(suffix)]
    return t


def _load_cache() -> dict:
    """读 cache JSON。结构: {"6位code": {"sector": "...", "industry": "...",
                                          "z_prime_inapplicable": bool,
                                          "fetched_at": "ISO timestamp"}}"""
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("a_share_industry cache 损坏: %s", e)
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _is_fresh(entry: dict, ttl_days: int = CACHE_TTL_DAYS) -> bool:
    """判断 cache entry 是否还在 TTL 内。"""
    ts = entry.get("fetched_at")
    if not ts:
        return False
    try:
        fetched = datetime.fromisoformat(ts)
    except Exception:
        return False
    return (datetime.now() - fetched) < timedelta(days=ttl_days)


def _fetch_industry_from_yfinance(ticker: str) -> dict | None:
    """从 yfinance.Ticker.info 拉 sector + industry。失败返回 None。"""
    yf_tk = _normalize_a_share_ticker(ticker)
    try:
        import yfinance as yf
        info = yf.Ticker(yf_tk).info or {}
    except Exception as e:
        logger.debug("yfinance %s failed: %s", yf_tk, e)
        return None
    sector = info.get("sector")
    industry = info.get("industry")
    if not sector and not industry:
        return None
    return {
        "code": _strip_code(ticker),
        "yf_ticker": yf_tk,
        "sector": sector,
        "industry": industry,
        "z_prime_inapplicable": sector in Z_PRIME_INAPPLICABLE_SECTORS,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": "yfinance.info",
    }


def get_industry(ticker: str, force_refresh: bool = False) -> dict | None:
    """单只查询，带 cache（TTL 30 天）。返回 None 表示拉取失败。"""
    code = _strip_code(ticker)
    cache = _load_cache()
    entry = cache.get(code)
    if not force_refresh and entry and _is_fresh(entry):
        return entry
    fetched = _fetch_industry_from_yfinance(ticker)
    if fetched is None:
        return entry  # 拉取失败，返回旧 cache（即使过期，总比 None 强）
    cache[code] = fetched
    _save_cache(cache)
    return fetched


def bulk_get_industry(tickers: list[str], force_refresh: bool = False,
                     throttle_seconds: float = 0.3) -> dict[str, dict]:
    """批量查询，复用 cache。返回 {code: industry_info}。

    新拉的会自动写 cache。throttle_seconds 控制 yfinance 限流速度。
    """
    cache = _load_cache()
    out: dict[str, dict] = {}
    n_new = 0
    n_cached = 0
    n_failed = 0
    for tk in tickers:
        code = _strip_code(tk)
        entry = cache.get(code)
        if not force_refresh and entry and _is_fresh(entry):
            out[code] = entry
            n_cached += 1
            continue
        fetched = _fetch_industry_from_yfinance(tk)
        if fetched is None:
            n_failed += 1
            if entry:
                out[code] = entry  # 用旧 cache 兜底
            continue
        cache[code] = fetched
        out[code] = fetched
        n_new += 1
        if throttle_seconds > 0:
            time.sleep(throttle_seconds)
    _save_cache(cache)
    logger.info("bulk_get_industry: %d cached / %d new / %d failed", n_cached, n_new, n_failed)
    return out


def cli_refresh():
    """CLI 入口：刷新 watchlist 所有 A 股的行业 cache。"""
    import argparse
    parser = argparse.ArgumentParser(description="刷新 A 股行业 cache (GICS via yfinance)")
    parser.add_argument("--force", action="store_true", help="强制重新拉取所有股票，忽略 TTL")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 只（debug 用）")
    args = parser.parse_args()

    import sys as _sys
    _sys.path.insert(0, str(REPO / "scripts" / "lib"))
    from stock_db import fetch_universe_for_ai_recommendations  # V2 system_universe

    # 2026-05-21 V1 cutover：从 V1 watchlist → V2 system_universe（CN market）
    records = fetch_universe_for_ai_recommendations()
    a_share_codes = [
        (r.get("symbol") or "").strip()
        for r in records
        if (r.get("market") or "").upper() == "CN" and (r.get("symbol") or "").strip()
    ]

    if args.limit:
        a_share_codes = a_share_codes[:args.limit]

    print(f"=== 刷新 A 股行业 cache ({len(a_share_codes)} 只) ===\n")
    t0 = time.time()
    result = bulk_get_industry(a_share_codes, force_refresh=args.force)
    elapsed = time.time() - t0

    # 统计 sector 分布 + Z'' 不适用计数
    sectors: dict[str, int] = {}
    n_z_inapplicable = 0
    for info in result.values():
        s = info.get("sector") or "Unknown"
        sectors[s] = sectors.get(s, 0) + 1
        if info.get("z_prime_inapplicable"):
            n_z_inapplicable += 1

    print(f"\n✅ 完成 ({elapsed:.1f}s) — cache: {CACHE_PATH}\n")
    print(f"=== Sector 分布（GICS）===")
    for s, n in sorted(sectors.items(), key=lambda x: -x[1]):
        flag = "🚫 Z'' 不适用" if s in Z_PRIME_INAPPLICABLE_SECTORS else ""
        print(f"  {s:<28} {n:>3} 只  {flag}")
    print(f"\nZ'' 不适用合计: {n_z_inapplicable} 只（金融 / 地产 / 公用事业 …）")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(cli_refresh())
