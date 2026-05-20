"""
yfinance 价格抓取器
─────────────────────────────────────────
功能：
1. 从 DuckDB 手动 watchlist 或科技 universe 拉股票代码
2. 用 yfinance 抓取实时价格、YTD 涨幅、一年涨幅、市值、PE
3. 自动处理跨市场代码（美股/A股/港股/韩股）
4. 写回飞书表，并保存 JSON 快照

用法：
  python3 fetch_stock_prices.py              # 全量更新
  python3 fetch_stock_prices.py --code NVDA  # 仅更新单只
"""
import sys
import os
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))  # 2026-05-11 lib 迁移
import json
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import duckdb  # noqa: E402
from stock_db import DB_PATH, upsert_prices, fetch_all_watchlist  # noqa: E402
from stock_research.core import akshare_client  # noqa: E402
from stock_research.core.hk_universe import fetch_hk_tech_universe  # noqa: E402
from stock_research.core.us_universe import fetch_us_ai_tech_universe  # noqa: E402

import yfinance as yf  # noqa: E402
import pandas as pd  # noqa: E402

DATA_DIR = _REPO
INFO_CACHE_FILE = os.path.join(DATA_DIR, "data", "latest", "yf_info_cache.json")
DEFAULT_INFO_TTL_HOURS = 12
SOURCE_HEALTH_FILE = os.path.join(DATA_DIR, "data", "latest", "source_health.json")
# 2026-05-20：快照不再写根目录，统一进 data/snapshots/prices/
PRICES_SNAPSHOT_DIR = os.path.join(DATA_DIR, "data", "snapshots", "prices")


def _tables_in_db(db_path: str) -> set[str]:
    if not os.path.exists(db_path):
        return set()
    try:
        con = duckdb.connect(db_path)
        rows = con.execute("SHOW TABLES").fetchall()
        con.close()
        return {str(r[0]) for r in rows}
    except Exception:
        return set()


def _is_v2_db(db_path: str) -> bool:
    tables = _tables_in_db(db_path)
    return "price_daily" in tables and "pool_membership" in tables


def _market_code(code: str, market: str) -> str:
    text = f"{code or ''} {market or ''}"
    if "港股" in text or str(code).endswith(".HK"):
        return "HK"
    if "A股" in text or "上交所" in text or "深交所" in text or "北交所" in text:
        return "CN"
    return "US"


def _upsert_v2_price_daily(results: list[dict], db_path: str, *, total_count: int, fail_count: int) -> int:
    """Write freshly fetched prices into the clean v2 schema."""
    con = duckdb.connect(db_path)
    tables = {str(r[0]) for r in con.execute("SHOW TABLES").fetchall()}
    if "price_daily" not in tables:
        con.close()
        raise RuntimeError("当前 DB 不是 v2 schema：缺少 price_daily")

    now = datetime.now()
    trade_date = now.date()
    rows = []
    for r in results:
        code = str(r.get("code") or "").strip()
        market = _market_code(code, str(r.get("market") or ""))
        symbol = code.upper()
        rows.append((
            market,
            symbol,
            trade_date,
            "1d",
            r.get("price"),
            r.get("prev_close"),
            r.get("currency"),
            r.get("market_cap"),
            r.get("forward_pe"),
            r.get("trailing_pe"),
            r.get("peg_ratio"),
            r.get("ytd_pct"),
            r.get("one_week_pct"),
            r.get("one_month_pct"),
            r.get("one_year_pct"),
            "yfinance",
            now,
            now,
        ))

    con.executemany(
        "DELETE FROM price_daily WHERE market=? AND symbol=? AND trade_date=? AND interval=?",
        [(r[0], r[1], r[2], r[3]) for r in rows],
    )
    con.executemany(
        """
        INSERT INTO price_daily (
            market, symbol, trade_date, interval, close, prev_close, currency,
            market_cap, forward_pe, trailing_pe, peg_ratio, ytd_pct,
            one_week_pct, one_month_pct, one_year_pct, source,
            source_updated_at, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )

    if "source_fetch_log" in tables:
        status = "source_degraded" if fail_count else "success"
        status_code = "partial" if fail_count else "ok"
        con.execute(
            """
            INSERT INTO source_fetch_log (
                run_id, source, market, status, status_code, fallback_source,
                fetched_at, message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                f"price_daily_{now.strftime('%Y%m%d_%H%M%S')}",
                "yfinance",
                "ALL",
                status,
                status_code,
                None,
                now,
                f"写入 price_daily {len(rows)}/{total_count} 行，失败 {fail_count} 行",
            ],
        )
    con.close()
    return len(rows)


# ============================================================
# yfinance 代码格式转换（关键）
# ============================================================

def to_yfinance_ticker(code, market):
    """把飞书表里的代码转换成 yfinance 能识别的 ticker。
    优先看 market，市场字段缺失时根据代码格式自动判断。"""
    code = (code or "").strip()
    market = market or ""
    if not code:
        return None

    # 1) 已有交易所后缀 → 直接返回
    if "." in code:
        # 韩股 000660.KS、港股 3690.HK 等
        return code

    # 2) 港股提示
    if "港股" in market:
        return f"{code}.HK"

    # 3) 韩股
    if "韩股" in market or "其他" in market and code.startswith("00"):
        return f"{code}.KS"

    # 4) 美股：纯字母（含连字符），不论 market 字段
    if code.replace("-", "").replace(".", "").isalpha():
        return code

    # 5) A 股 6 位数字代码
    clean = code
    if clean.isdigit() and len(clean) == 6:
        if "深交所" in market or clean.startswith(("00", "30", "20")):
            return f"{clean}.SZ"
        elif "北交所" in market or clean.startswith(("8", "9")):
            return f"{clean}.BJ"
        else:
            # 默认上交所（含 60、68、78、73、603 等）
            return f"{clean}.SS"

    return None


# ============================================================
# 拉飞书数据 + 写回飞书
# ============================================================

def _market_for_a_share(raw_ticker: str) -> str:
    if raw_ticker.startswith(("00", "20", "30")):
        return "A股·深交所"
    if raw_ticker.startswith(("8", "9")):
        return "A股·北交所"
    return "A股·上交所"


def _tech_universe_items() -> list[dict]:
    """科技/AI 股票池。

    优先直接读取当前 DuckDB 里的 system_universe，这样行情拉取和推荐都跟
    产品定义的系统池保持同一口径；若系统池还没建好，再回退到代码定义的
    universe 生成器。
    """
    items: list[dict] = []
    db_path = DB_PATH
    if os.path.exists(db_path):
        try:
            con = duckdb.connect(db_path, read_only=True)
            tables = {str(r[0]) for r in con.execute("SHOW TABLES").fetchall()}
            if "system_universe" in tables:
                rows = con.execute(
                    """
                    SELECT pool_id, market, symbol, raw_symbol, name, theme, industry, source
                    FROM system_universe
                    WHERE active = TRUE
                    ORDER BY market, symbol
                    """
                ).fetchall()
                con.close()
                for pool_id, market, symbol, raw_symbol, name, theme, industry, source in rows:
                    items.append({
                        "code": str(symbol),
                        "name": name or symbol,
                        "market": "港股" if str(market).upper() == "HK" else ("A股" if str(market).upper() == "CN" else "美股"),
                        "industry": industry or theme or "",
                        "source": str(source or pool_id or "system_tech_universe"),
                    })
                if items:
                    return items
        except Exception as e:
            print(f"  ⚠️ system_universe 读取失败，回退到代码 universe: {e}")

    try:
        from scripts.tools.discover_candidates import build_universe as build_dynamic_universe
        discovered = build_dynamic_universe(skip_codes=set())
        if discovered:
            for item in discovered:
                items.append({
                    "code": item["ticker"],
                    "name": item["name"],
                    "market": "港股" if str(item.get("location") or "").lower().startswith("hong kong") else (
                        "A股" if "china" in str(item.get("location") or "").lower() else "美股"
                    ),
                    "industry": item.get("sector") or "",
                    "source": f"discover:{','.join((item.get('etfs') or [])[:2]) or 'dynamic'}",
                })
            if items:
                return items
    except Exception as e:
        print(f"  ⚠️ 动态 universe 发现失败: {e}")

    from stock_research.core.a_share_universe import fetch_a_share_tech_universe
    from stock_research.core.hk_universe import fetch_hk_tech_universe
    from stock_research.core.us_universe import fetch_us_ai_tech_universe

    for item in fetch_us_ai_tech_universe():
        items.append({
            "code": item["ticker"],
            "name": item["name"],
            "market": "美股",
            "industry": item.get("sector") or "",
            "source": f"fallback:{item.get('source') or 'us'}",
        })
    for item in fetch_hk_tech_universe():
        items.append({
            "code": item["ticker"],
            "name": item["name"],
            "market": "港股",
            "industry": item.get("sector") or "",
            "source": f"fallback:{item.get('source') or 'hk'}",
        })
    cn_items = fetch_a_share_tech_universe()
    if not cn_items:
        print("  ⚠️ A 股动态 universe 为空：不再使用静态种子补齐")
    for item in cn_items:
        raw = item.get("raw_ticker") or item["ticker"].split(".")[0]
        items.append({
            "code": item["ticker"],
            "name": item["name"],
            "market": _market_for_a_share(raw),
            "industry": item.get("sector") or "",
            "source": f"fallback:{item.get('source') or 'cn'}",
        })
    return items


def _load_price_items(source: str) -> list[dict]:
    rows: list[dict] = []
    if source in {"watchlist", "both"}:
        wl = fetch_all_watchlist()
        print(f"  手动 watchlist: {len(wl)} 条")
        rows.extend({**r, "_price_source": "watchlist"} for r in wl)
    if source in {"tech-universe", "both"}:
        tech = _tech_universe_items()
        print(f"  科技/AI universe: {len(tech)} 条")
        rows.extend({**r, "_price_source": "tech_universe"} for r in tech)

    dedup: dict[str, dict] = {}
    for row in rows:
        code = (row.get("code") or "").strip()
        if not code:
            continue
        # 手动 watchlist 优先，避免同代码时覆盖用户维护的名称/市场。
        if code not in dedup or row.get("_price_source") == "watchlist":
            dedup[code] = row
    return list(dedup.values())

# ============================================================
# yfinance 抓取
# ============================================================

def _load_info_cache() -> dict:
    try:
        with open(INFO_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"items": {}}
    except Exception:
        return {"items": {}}


def _save_info_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(INFO_CACHE_FILE), exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "items": cache.get("items", {}),
    }
    tmp = INFO_CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, INFO_CACHE_FILE)


def _market_label(value: str) -> str:
    text = str(value or "").upper()
    if "HK" in text or "港股" in text:
        return "HK"
    if "CN" in text or "A股" in text or "SH" in text or "SZ" in text or "BJ" in text:
        return "CN"
    return "US"


def _write_source_health(
    *,
    total_count: int,
    success_count: int,
    fail_count: int,
    source: str,
    market_summary: dict[str, dict[str, int]] | None = None,
) -> None:
    if total_count <= 0:
        return
    if success_count <= 0:
        status = "source_down"
        reason = "price source returned no usable rows"
        operator_action = "检查 yfinance / DNS / 外部网络；当前不应把空结果当成功"
    elif fail_count > 0:
        status = "source_degraded"
        reason = f"partial_success {success_count}/{total_count}"
        operator_action = "补修失败市场或 ticker 映射，再重跑行情拉取"
    else:
        status = "ok"
        reason = "all_rows_fetched"
        operator_action = "无"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pipeline": "price_daily",
        "source": source,
        "markets": market_summary or {},
        "sources": {
            "yfinance": {
                "status": status,
                "reason": reason,
                "affected_fields": ["price", "prev_close", "market_cap", "forward_pe", "trailing_pe", "peg_ratio", "history"],
                "unaffected_fields": ["system_universe", "pool_membership", "strategy_versions"],
                "impact": "行情不可用时，AI 推荐无法形成有效候选，运行状态应显示为降级而不是静默空白。",
                "operator_action": operator_action,
            }
        },
        "summary": {
            "total_count": total_count,
            "success_count": success_count,
            "fail_count": fail_count,
        },
    }
    out = SOURCE_HEALTH_FILE
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)


def _cache_entry_fresh(entry: dict | None, ttl_hours: float) -> bool:
    if not entry:
        return False
    fetched_at = entry.get("fetched_at")
    if not fetched_at:
        return False
    try:
        ts = datetime.fromisoformat(str(fetched_at))
    except Exception:
        return False
    return datetime.now() - ts <= timedelta(hours=ttl_hours)


def _fetch_info_fields(yf_ticker: str) -> dict:
    """拉估值/基本面快照；这些字段变化慢，允许用本地 TTL cache。"""
    try:
        t = yf.Ticker(yf_ticker)
        info = t.info
        if not info:
            return {"error": "empty info"}

        fields = {
            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "prev_close": info.get("previousClose"),
            "currency": info.get("currency", "USD"),
            "market_cap": info.get("marketCap"),
            "forward_pe": info.get("forwardPE"),
            "trailing_pe": info.get("trailingPE"),
            "peg_ratio": info.get("pegRatio") or info.get("trailingPegRatio"),
            "earnings_growth": info.get("earningsGrowth"),
            "revenue_growth": info.get("revenueGrowth"),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
    except Exception as e:
        return {"error": str(e), "fetched_at": datetime.now().isoformat(timespec="seconds")}

    return fields


def _download_history_batch(yf_tickers: list[str]) -> dict[str, pd.DataFrame]:
    """一次下载 1 年历史价，替代每只股票单独 history(period='1y')。"""
    if not yf_tickers:
        return {}
    unique = sorted(set(yf_tickers))
    try:
        df = yf.download(
            " ".join(unique),
            period="1y",
            progress=False,
            group_by="ticker",
            threads=True,
            auto_adjust=False,
        )
    except Exception as e:
        print(f"  ⚠️ 批量历史价失败，将退化为空历史：{e}")
        return {}
    if df is None or df.empty:
        return {}

    out: dict[str, pd.DataFrame] = {}
    if isinstance(df.columns, pd.MultiIndex):
        level0 = set(df.columns.get_level_values(0))
        for tk in unique:
            if tk in level0:
                sub = df[tk].dropna(how="all")
                if not sub.empty:
                    out[tk] = sub
    elif len(unique) == 1:
        out[unique[0]] = df.dropna(how="all")
    return out


def _history_metrics(hist: pd.DataFrame | None, price: float | None) -> dict:
    ytd_pct = one_year_pct = one_month_pct = one_week_pct = None
    prev_close = None
    hist_price = None
    if hist is None or hist.empty or "Close" not in hist:
        return {
            "history_price": None, "history_prev_close": None,
            "ytd_pct": None, "one_year_pct": None,
            "one_month_pct": None, "one_week_pct": None,
        }

    close = hist["Close"].dropna()
    if close.empty:
        return {
            "history_price": None, "history_prev_close": None,
            "ytd_pct": None, "one_year_pct": None,
            "one_month_pct": None, "one_week_pct": None,
        }

    try:
        hist_price = float(close.iloc[-1])
        if len(close) >= 2:
            prev_close = float(close.iloc[-2])
        effective_price = float(price) if price is not None else hist_price

        this_year = datetime.now().year
        year_start = close[close.index.year == this_year]
        if len(year_start) > 0:
            ytd_start = float(year_start.iloc[0])
            if ytd_start > 0:
                ytd_pct = round((effective_price - ytd_start) / ytd_start * 100, 2)

        one_year_start = float(close.iloc[0])
        if one_year_start > 0:
            one_year_pct = round((effective_price - one_year_start) / one_year_start * 100, 2)

        if len(close) >= 22:
            m_ago = float(close.iloc[-22])
            if m_ago > 0:
                one_month_pct = round((effective_price - m_ago) / m_ago * 100, 2)
        if len(close) >= 5:
            w_ago = float(close.iloc[-5])
            if w_ago > 0:
                one_week_pct = round((effective_price - w_ago) / w_ago * 100, 2)
    except Exception:
        pass

    return {
        "history_price": hist_price,
        "history_prev_close": prev_close,
        "ytd_pct": ytd_pct,
        "one_year_pct": one_year_pct,
        "one_month_pct": one_month_pct,
        "one_week_pct": one_week_pct,
    }


def fetch_price_data(yf_ticker: str, *, hist: pd.DataFrame | None = None,
                     info_fields: dict | None = None):
    """组合批量历史价 + 缓存估值字段，返回标准化字典。失败返回 None。"""
    info_fields = info_fields or {}
    if info_fields.get("error") and (hist is None or hist.empty):
        return None

    price = info_fields.get("price")
    try:
        price = float(price) if price is not None else None
    except Exception:
        price = None

    hist_metrics = _history_metrics(hist, price)
    if price is None:
        price = hist_metrics.get("history_price")
    if price is None:
        return None

    prev_close = info_fields.get("prev_close") or hist_metrics.get("history_prev_close")
    currency = info_fields.get("currency") or "USD"
    market_cap = info_fields.get("market_cap")
    forward_pe = info_fields.get("forward_pe")
    trailing_pe = info_fields.get("trailing_pe")
    peg_ratio = info_fields.get("peg_ratio")
    earnings_growth = info_fields.get("earnings_growth")
    revenue_growth = info_fields.get("revenue_growth")

    # PEG 兜底计算（pegRatio 不可用时用 forward PE / 利润增速）
    peg_calculated = None
    if peg_ratio is None and forward_pe and earnings_growth and earnings_growth > 0:
        peg_calculated = round(forward_pe / (earnings_growth * 100), 2)

    return {
        "price": price,
        "prev_close": prev_close,
        "currency": currency,
        "market_cap": market_cap,
        "forward_pe": round(forward_pe, 2) if forward_pe else None,
        "trailing_pe": round(trailing_pe, 2) if trailing_pe else None,
        "peg_ratio": round(peg_ratio, 2) if peg_ratio else peg_calculated,
        "earnings_growth_pct": round(earnings_growth * 100, 2) if earnings_growth else None,
        "revenue_growth_pct": round(revenue_growth * 100, 2) if revenue_growth else None,
        "ytd_pct": hist_metrics["ytd_pct"],
        "one_year_pct": hist_metrics["one_year_pct"],
        "one_month_pct": hist_metrics["one_month_pct"],
        "one_week_pct": hist_metrics["one_week_pct"],
    }


def _market_snapshot_fallback(code: str, market: str) -> dict | None:
    """当 yfinance 失败时，尝试用市场原生快照补一个最小可写入结果。

    目标不是补全所有估值，而是保证 price_daily 至少有真实价格落点，
    让系统能把市场按实际覆盖情况拆开，而不是整体空白。
    """
    raw = str(code or "").split(".")[0]
    market_text = str(market or "")
    try:
        if "A股" in market_text or raw.isdigit():
            q = akshare_client.fetch_a_stock_quote(raw)
            if not q:
                return None
            price = q.get("price")
            change_pct = q.get("change_pct")
            prev_close = None
            if price is not None and change_pct not in (None, 0):
                try:
                    prev_close = float(price) / (1.0 + float(change_pct) / 100.0)
                except Exception:
                    prev_close = None
            return {
                "price": price,
                "prev_close": prev_close,
                "currency": "CNY",
                "market_cap": q.get("market_cap_yuan"),
                "forward_pe": q.get("pe_ttm"),
                "trailing_pe": q.get("pe_ttm"),
                "peg_ratio": None,
                "ytd_pct": None,
                "one_week_pct": None,
                "one_month_pct": None,
                "one_year_pct": None,
                "fallback_source": q.get("source") or "akshare/stock_zh_a_spot_em",
            }
        if "港股" in market_text or raw.endswith(".HK"):
            q = akshare_client.fetch_hk_stock_quote(raw)
            if not q:
                return None
            price = q.get("price")
            change_pct = q.get("change_pct")
            prev_close = None
            if price is not None and change_pct not in (None, 0):
                try:
                    prev_close = float(price) / (1.0 + float(change_pct) / 100.0)
                except Exception:
                    prev_close = None
            return {
                "price": price,
                "prev_close": prev_close,
                "currency": "HKD",
                "market_cap": q.get("market_cap_hkd"),
                "forward_pe": None,
                "trailing_pe": None,
                "peg_ratio": None,
                "ytd_pct": None,
                "one_week_pct": None,
                "one_month_pct": None,
                "one_year_pct": None,
                "fallback_source": q.get("source") or "akshare/stock_hk_spot_em",
            }
    except Exception as e:
        print(f"  ⚠️ 市场快照 fallback 失败 {code}: {e}")
    return None


def format_market_cap(mc, currency):
    if not mc:
        return ""
    units = {"USD": "美元", "CNY": "人民币", "HKD": "港元", "KRW": "韩元"}
    unit = units.get(currency, currency)
    if currency == "KRW":
        if mc >= 1e12:
            return f"₩{mc/1e12:.2f}万亿（{unit}）"
        return f"₩{mc/1e9:.0f}亿（{unit}）"
    if mc >= 1e12:
        return f"${mc/1e12:.2f}T（{unit}）"
    if mc >= 1e9:
        return f"${mc/1e9:.1f}B（{unit}）"
    return f"${mc/1e6:.0f}M（{unit}）"


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", help="只更新某只股票")
    parser.add_argument(
        "--source",
        choices=["watchlist", "tech-universe", "both"],
        default="watchlist",
        help="价格输入来源：手动 watchlist、科技/AI universe，或两者合并",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印,不写 DuckDB")
    parser.add_argument("--workers", type=int, default=int(os.getenv("STOCK_ASSISTANT_PRICE_WORKERS", "8")),
                        help="并发拉 yfinance info 的线程数（默认 8）")
    parser.add_argument("--info-ttl-hours", type=float,
                        default=float(os.getenv("STOCK_ASSISTANT_INFO_TTL_HOURS", DEFAULT_INFO_TTL_HOURS)),
                        help="估值/基本面 info 缓存小时数（默认 12）")
    parser.add_argument("--refresh-fundamentals", action="store_true",
                        help="忽略 info 缓存，强制刷新 PE/PEG/市值/增长率")
    parser.add_argument(
        "--db-schema",
        choices=["auto", "legacy", "v2"],
        default="auto",
        help="写库模式：auto 自动识别 v2/旧库，v2 写 price_daily，legacy 写 prices",
    )
    args = parser.parse_args()

    print(f"[1/3] 拉取价格输入池 [source={args.source}]...")
    items = _load_price_items(args.source)
    print(f"  共 {len(items)} 条")

    jobs = []
    fail_codes = []
    for item in items:
        name = item.get("name") or ""
        code = item.get("code") or ""
        market = item.get("market") or ""

        if args.code and args.code != code:
            continue

        yf_code = to_yfinance_ticker(code, market)
        if not yf_code:
            print(f"  [跳过] {name} ({code}) — 无法转换 ticker")
            fail_codes.append(code)
            continue
        jobs.append({"item": item, "name": name, "code": code, "yf_code": yf_code})

    print("\n[2/3] 抓取价格（yfinance 批量历史价 + 并发 info）...")
    yf_codes = [j["yf_code"] for j in jobs]
    history_by_ticker = _download_history_batch(yf_codes)
    print(f"  历史价批量完成：{len(history_by_ticker)} / {len(set(yf_codes))} 个 ticker")

    info_cache = _load_info_cache()
    cache_items = info_cache.setdefault("items", {})
    info_by_ticker: dict[str, dict] = {}
    missing_info = []
    for tk in sorted(set(yf_codes)):
        cached = cache_items.get(tk)
        if not args.refresh_fundamentals and _cache_entry_fresh(cached, args.info_ttl_hours):
            info_by_ticker[tk] = cached
        else:
            missing_info.append(tk)

    if missing_info:
        workers = max(1, min(args.workers, len(missing_info)))
        print(f"  刷新 info：{len(missing_info)} 个 ticker · workers={workers}")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_fetch_info_fields, tk): tk for tk in missing_info}
            for fut in as_completed(futures):
                tk = futures[fut]
                fields = fut.result()
                if fields.get("error") and cache_items.get(tk):
                    fields = cache_items[tk]
                else:
                    cache_items[tk] = fields
                info_by_ticker[tk] = fields
        _save_info_cache(info_cache)
    else:
        print(f"  info 全部命中缓存（TTL={args.info_ttl_hours:g}h）")

    results = []
    success_count = 0
    market_summary: dict[str, dict[str, int]] = {}
    for j in jobs:
        name = j["name"]
        code = j["code"]
        yf_code = j["yf_code"]
        market = str(j["item"].get("market") or "")
        market_key = _market_label(market)
        bucket = market_summary.setdefault(market_key, {"total": 0, "success": 0, "fail": 0})
        bucket["total"] += 1

        print(f"  抓取 {name} ({yf_code})...", end=" ")
        data = fetch_price_data(
            yf_code,
            hist=history_by_ticker.get(yf_code),
            info_fields=info_by_ticker.get(yf_code),
        )
        price_source = j["item"].get("_price_source") or args.source
        if not data:
            fallback = _market_snapshot_fallback(code, market)
            if fallback:
                data = fallback
                price_source = fallback.get("fallback_source") or price_source
                print(f"    ↳ 使用市场快照 fallback: {fallback.get('fallback_source')}")
        if not data:
            print("❌ 失败")
            fail_codes.append(code)
            bucket["fail"] += 1
            continue

        success_count += 1
        bucket["success"] += 1
        price_str = f"{data['price']} {data['currency']}"
        ytd_str = f"{data['ytd_pct']:+.1f}%" if data["ytd_pct"] is not None else "N/A"
        oy_str = f"{data['one_year_pct']:+.1f}%" if data["one_year_pct"] is not None else "N/A"
        wk_str = f"{data['one_week_pct']:+.1f}%" if data["one_week_pct"] is not None else "N/A"
        peg_str = f"{data['peg_ratio']}" if data["peg_ratio"] else "N/A"
        print(f"{price_str} · 1W {wk_str} · YTD {ytd_str} · 1Y {oy_str} · PEG {peg_str}")

        results.append({
            "code": code,
            "name": name,
            "market": j["item"].get("market") or "",
            "price_source": price_source,
            "yf_ticker": yf_code,
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            **data,
        })

    print(f"\n[3/3] 完成：成功 {success_count} / 总 {len(items)}")
    if fail_codes:
        print(f"  失败标的：{', '.join(fail_codes)}")
    _write_source_health(
        total_count=len(items),
        success_count=success_count,
        fail_count=len(fail_codes),
        source=args.source,
        market_summary=market_summary,
    )
    print(f"  来源健康已写入：{SOURCE_HEALTH_FILE}")

    # 保存 JSON 快照（统一到 data/snapshots/prices/，不再污染根目录）
    os.makedirs(PRICES_SNAPSHOT_DIR, exist_ok=True)
    out_file = os.path.join(PRICES_SNAPSHOT_DIR, f"prices_{datetime.now().strftime('%Y-%m-%d_%H%M')}.json")
    with open(out_file, "w", encoding="utf-8") as f_out:
        json.dump(results, f_out, ensure_ascii=False, indent=2, default=str)
    print(f"  快照已保存：{out_file}")

    if args.dry_run:
        print("  [Dry-Run] 跳过 DuckDB 写入")
        return

    # 落 DuckDB（按 fetched_at 的日期，同日多次抓取会覆盖）
    if results:
        try:
            use_v2 = args.db_schema == "v2" or (args.db_schema == "auto" and _is_v2_db(DB_PATH))
            if use_v2:
                n = _upsert_v2_price_daily(results, DB_PATH, total_count=len(items), fail_count=len(fail_codes))
                print(f"  DuckDB：已写入 {n} 行 ({DB_PATH} · price_daily)")
            else:
                n = upsert_prices(results)
                print(f"  DuckDB：已写入 {n} 行 ({DB_PATH} · prices)")
        except Exception as e:
            print(f"  DuckDB 写入失败（不阻塞主流程）：{e}")


if __name__ == "__main__":
    main()
