"""次新股 / IPO / 解禁雷达：三市场聚合（CN / US / HK）。

被 daily_refresh.sh 调用。下游消费：
  - dashboard 「📅 IPO & 次新股」tab（含市场切换）
  - 今日决策台「📅 本周市场事件」轻量提醒卡

输出：
  data/latest/junior_stock_radar.json
  data/cache/us_ipo_dates.json  (finnhub profile2 缓存，IPO 日期不变所以可缓存很久)

数据源（不同市场不同来源 — 港股最弱，A 股最强）：

  【A 股】
    - ipo_calendar.json (上游 ipo_daily.py 已生成)            IPO 日历
    - ak.stock_xgsr_ths()                                     次新股首日表现（3800+ 条）
    - ak.stock_restricted_release_detail_em(start, end)       未来 90 天个股解禁明细

  【美股】
    - stock_research.core.nasdaq_ipo.fetch_window()           NASDAQ 公开 API,过去 24 月 priced + 未来 2 月 filed
    - yfinance.download(batch)                                IPO universe 当前价批拉
    - 美股没有便宜的 lockup 数据源（S-1 招股书里），解禁雷达暂缺
    - IPO universe 独立于 system_universe,不污染主推荐流程

  【港股】
    - 没有便宜的开源 IPO/解禁源（finnhub 免费层不含港股，akshare hk 不可靠）
    - 仅显示 placeholder + HKEX 外链；后续需付费源（Wind / Choice / HKEX 官方）激活

设计原则：
  - 不算因子、不写库、不发飞书 — 只是聚合 + 打分 + 写 JSON
  - 美股次新股复用 system_universe 已有 ticker（69 只），不全网扫
  - finnhub 调用带文件缓存（IPO 日期是常量），首日跑 ~70s，之后秒级
"""
from __future__ import annotations

import json
import logging
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CACHE_DIR = REPO / "data" / "cache"
CN_INDUSTRY_CACHE = CACHE_DIR / "cn_industry_by_code.json"
CN_INDUSTRY_TTL_DAYS = 7  # 行业基本不变；缓存 7 天可覆盖周末 + 个别拉取失败


# ───────────── 通用工具 ─────────────

def _safe_float(x: Any) -> float | None:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except Exception:
        return None


def _norm6(code: Any) -> str:
    if code is None:
        return ""
    s = str(code).strip().split(".")[0]
    if s.isdigit():
        return s.zfill(6)[-6:]
    return s


def _to_iso(d: Any) -> str | None:
    if d is None:
        return None
    if isinstance(d, (date, datetime)):
        return d.isoformat() if isinstance(d, date) and not isinstance(d, datetime) else d.date().isoformat()
    s = str(d).strip()
    if not s or s in {"nan", "NaT", "None"}:
        return None
    return s[:10]


def _board_of_cn(code: str) -> str:
    c = _norm6(code)
    if not c:
        return "other"
    if c.startswith("688"):
        return "star"
    if c.startswith("300"):
        return "chinext"
    if c.startswith(("600", "601", "603", "605")):
        return "main"
    if c.startswith(("000", "001", "002", "003")):
        return "main"
    if c.startswith(("8", "9")):
        return "bse"
    return "other"


# ───────────── 持仓 / 自选股集合 ─────────────

def _load_pool_symbols() -> dict[str, dict[str, set[str]]]:
    """读 real_holdings + manual_watchlist，按市场返回代码集合。

    返回 {'cn': {holdings, watchlist}, 'us': {...}, 'hk': {...}}
    cn 用 6 位代码；us/hk 用原始 symbol（如 AAPL / 0700.HK）。
    """
    out = {m: {"holdings": set(), "watchlist": set()} for m in ("cn", "us", "hk")}
    try:
        import duckdb
        db_path = REPO / "stock_history_v2.duckdb"
        if not db_path.exists():
            return out
        con = duckdb.connect(str(db_path), read_only=True)
        for market, symbol in con.execute("SELECT market, symbol FROM real_holdings").fetchall():
            m = (market or "").lower()
            if m == "cn":
                out["cn"]["holdings"].add(_norm6(symbol))
            elif m == "us":
                out["us"]["holdings"].add(str(symbol).upper())
            elif m == "hk":
                out["hk"]["holdings"].add(str(symbol).upper())
        for market, symbol in con.execute("SELECT market, symbol FROM manual_watchlist").fetchall():
            m = (market or "").lower()
            if "a股" in m or m == "cn":
                out["cn"]["watchlist"].add(_norm6(symbol))
            elif "美股" in m or "us" in m:
                out["us"]["watchlist"].add(str(symbol).upper())
            elif "港股" in m or "hk" in m:
                out["hk"]["watchlist"].add(str(symbol).upper())
        con.close()
    except Exception as e:
        logger.warning("read holdings/watchlist failed: %s", e)
    return out


def _load_ipo_calendar() -> dict:
    p = REPO / "data" / "ipo_calendar.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("read ipo_calendar.json failed: %s", e)
        return {}


def _import_ak():
    try:
        import akshare as ak
        return ak
    except ImportError:
        logger.error("akshare not installed; pip install akshare")
        return None


# ═════════════════════════════════════════════════════
#  A 股
# ═════════════════════════════════════════════════════

def _cn_junior_summary(
    months_listed: float,
    vs_issue_pct: float,
    vs_first_close_pct: float | None,
    first_chg_pct: float | None,
) -> str:
    """把次新股池打分维度翻译成"人话"摘要。

    朴实事实描述，不做未来预测；不做"建议买/不建议买"判断 —
    用户自行做基本面/行业/技术面三重判断。
    """
    m = int(round(months_listed))
    # 月数阶段
    if months_listed < 9:
        stage = f"上市 {m} 月刚解禁初期"
    elif months_listed < 12:
        stage = f"上市 {m} 月接近首发解禁窗口"
    elif months_listed <= 18:
        stage = f"上市 {m} 月正处首发解禁窗口"
    elif months_listed <= 21:
        stage = f"上市 {m} 月度过解禁压力期"
    else:
        stage = f"上市 {m} 月次新尾段"

    # vs 发行价
    if vs_issue_pct < -30:
        price_phrase = f"深度破发 {abs(vs_issue_pct):.0f}%"
    elif vs_issue_pct < 0:
        price_phrase = f"已破发 {abs(vs_issue_pct):.0f}%"
    elif vs_issue_pct < 50:
        price_phrase = f"较发行价 +{vs_issue_pct:.0f}%"
    elif vs_issue_pct < 100:
        price_phrase = f"较发行价 +{vs_issue_pct:.0f}% 偏强"
    else:
        price_phrase = f"较发行价 +{vs_issue_pct:.0f}% 主力强势"

    parts = [stage, price_phrase]

    # vs 首日收盘
    if vs_first_close_pct is not None:
        if vs_first_close_pct < -70:
            parts.append(f"较首日已跌 {abs(vs_first_close_pct):.0f}%（接近底部）")
        elif vs_first_close_pct < -50:
            parts.append(f"较首日跌 {abs(vs_first_close_pct):.0f}%（过半）")
        elif vs_first_close_pct < -20:
            parts.append(f"较首日跌 {abs(vs_first_close_pct):.0f}%")
        elif vs_first_close_pct < 0:
            parts.append(f"较首日小跌 {abs(vs_first_close_pct):.0f}%")
        elif vs_first_close_pct > 20:
            parts.append(f"较首日 +{vs_first_close_pct:.0f}%")
        # -20~+20 之间不啰嗦

    return " · ".join(parts)


def _load_cn_industry_cache() -> dict[str, str]:
    """读 code → industry 缓存。整体 TTL 7 天，过期就丢全部重拉。"""
    if not CN_INDUSTRY_CACHE.exists():
        return {}
    try:
        payload = json.loads(CN_INDUSTRY_CACHE.read_text(encoding="utf-8"))
        saved_at = datetime.fromisoformat(payload.get("saved_at", "1970-01-01"))
        if datetime.now() - saved_at > timedelta(days=CN_INDUSTRY_TTL_DAYS):
            return {}
        entries = payload.get("entries") or {}
        return {str(k): str(v) for k, v in entries.items() if v}
    except Exception:
        return {}


def _save_cn_industry_cache(entries: dict[str, str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "entries": entries,
    }
    CN_INDUSTRY_CACHE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _enrich_cn_industry(items: list[dict]) -> None:
    """给 A 股 items inplace 补 industry 字段。

    数据源：akshare stock_individual_info_em（东财个股资讯）。
    fail-soft：拉不到就保持空字符串，不抛异常、不阻塞主流程。
    限流：每只 0.3s sleep；缓存命中跳过。
    """
    ak = _import_ak()
    if ak is None:
        return
    cache = _load_cn_industry_cache()
    # 先用缓存填
    for it in items:
        code = it.get("code")
        if code and not it.get("industry") and code in cache:
            it["industry"] = cache[code]
    # 找还缺的
    missing = [it for it in items if not it.get("industry") and it.get("code")]
    if not missing:
        return
    import time as _time
    fetched = 0
    failed = 0
    for it in missing:
        code = it["code"]
        try:
            df = ak.stock_individual_info_em(symbol=code)
            row = dict(zip(df["item"], df["value"]))
            industry = str(row.get("行业") or "").strip()
            if industry:
                it["industry"] = industry
                cache[code] = industry
                fetched += 1
            _time.sleep(0.3)
        except Exception:
            failed += 1
            # 单只失败不阻塞剩下的；多个连续失败说明源挂了，提前终止
            if failed >= 5 and fetched == 0:
                logger.warning("[CN industry] 连续失败 %d 次，akshare EM 可能限流，跳过剩余 %d 只", failed, len(missing) - missing.index(it))
                break
    if fetched > 0:
        _save_cn_industry_cache(cache)
    logger.info("[CN industry] 补全 %d 只 (失败 %d / 缓存 %d / 总 %d)", fetched, failed, len(cache), len(items))


def fetch_cn_unlock_radar(holdings: set[str], watchlist: set[str], horizon_days: int = 90) -> list[dict]:
    """A 股未来 horizon_days 内个股解禁明细，按"解禁压力"排序。

    压力分 = 占流通市值比例(0..1) × 80 + log10(市值亿/1) × 5 + 10，封顶 100。
    """
    ak = _import_ak()
    if ak is None:
        return []
    today = date.today()
    end = today + timedelta(days=horizon_days)
    try:
        df = ak.stock_restricted_release_detail_em(
            start_date=today.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    except Exception as e:
        logger.warning("stock_restricted_release_detail_em failed: %s", e)
        return []
    if df is None or df.empty:
        return []

    out: list[dict] = []
    for _, r in df.iterrows():
        code = _norm6(r.get("股票代码"))
        if not code:
            continue
        unlock_date = _to_iso(r.get("解禁时间"))
        if not unlock_date:
            continue
        try:
            d_days = (datetime.fromisoformat(unlock_date).date() - today).days
        except Exception:
            d_days = 0
        if d_days < 0 or d_days > horizon_days:
            continue
        market_value = _safe_float(r.get("实际解禁市值")) or 0.0
        pct_float = _safe_float(r.get("占解禁前流通市值比例")) or 0.0
        if pct_float > 1.5:
            pct_float = pct_float / 100.0

        mv_yi = market_value / 1e8 if market_value > 0 else 0.0
        log_term = math.log10(max(mv_yi, 0.1)) * 5
        stress = min(100.0, max(0.0, pct_float * 80.0 + log_term + 10.0))

        out.append({
            "code": code,
            "name": str(r.get("股票简称") or ""),
            "board": _board_of_cn(code),
            "unlock_date": unlock_date,
            "days_to_unlock": d_days,
            "market_value_yi": round(mv_yi, 2),
            "pct_of_float": round(pct_float * 100.0, 2),
            "stress_score": round(stress, 1),
            "category": str(r.get("限售股类型") or ""),
            "pre_price": _safe_float(r.get("解禁前一交易日收盘价")),
            "in_holdings": code in holdings,
            "in_watchlist": code in watchlist,
        })
    out.sort(key=lambda x: (-x["stress_score"], x["days_to_unlock"]))
    return out


def fetch_cn_junior_pool(holdings: set[str], watchlist: set[str],
                          months_min: int = 6, months_max: int = 24) -> list[dict]:
    """A 股次新股底部打分（4 维：折发行价 / 时间衰减 / 首日溢价 / 较首日跌幅）。"""
    ak = _import_ak()
    if ak is None:
        return []
    try:
        df = ak.stock_xgsr_ths()
    except Exception as e:
        logger.warning("stock_xgsr_ths failed: %s", e)
        return []
    if df is None or df.empty:
        return []

    today = date.today()
    out: list[dict] = []
    for _, r in df.iterrows():
        code = _norm6(r.get("股票代码"))
        if not code:
            continue
        list_date_str = _to_iso(r.get("上市日期"))
        if not list_date_str:
            continue
        try:
            list_date = datetime.fromisoformat(list_date_str).date()
        except Exception:
            continue
        days_listed = (today - list_date).days
        months_listed = days_listed / 30.4
        if months_listed < months_min or months_listed > months_max:
            continue

        issue_price = _safe_float(r.get("发行价"))
        current_price = _safe_float(r.get("最新价"))
        first_close = _safe_float(r.get("首日收盘价"))
        first_chg = _safe_float(r.get("首日涨跌幅"))
        broken = str(r.get("是否破发") or "").strip() in {"是", "Y", "true", "True"}
        if issue_price is None or current_price is None or issue_price <= 0:
            continue

        vs_issue_pct = (current_price - issue_price) / issue_price * 100.0
        vs_first_close_pct = None
        if first_close and first_close > 0:
            vs_first_close_pct = (current_price - first_close) / first_close * 100.0

        s_discount = min(25.0, abs(vs_issue_pct) / 2.0) if vs_issue_pct <= 0 else 0.0
        if 12 <= months_listed <= 18:
            s_time = 25.0
        elif 9 <= months_listed < 12 or 18 < months_listed <= 21:
            s_time = 20.0
        elif 6 <= months_listed < 9 or 21 < months_listed <= 24:
            s_time = 15.0
        else:
            s_time = 10.0
        if first_chg is None or first_chg <= 0:
            s_first = 0.0
        elif first_chg >= 200:
            s_first = 25.0
        elif first_chg >= 100:
            s_first = 20.0
        elif first_chg >= 50:
            s_first = 15.0
        else:
            s_first = 8.0
        s_vs_first = min(25.0, abs(vs_first_close_pct) / 3.0) if (vs_first_close_pct is not None and vs_first_close_pct < 0) else 0.0

        total = round(s_discount + s_time + s_first + s_vs_first, 1)
        tags = []
        if broken or vs_issue_pct < 0:
            tags.append("已破发")
        if 12 <= months_listed <= 18:
            tags.append("首发解禁窗口")
        if first_chg and first_chg >= 100:
            tags.append("首日爆炒")
        if vs_first_close_pct is not None and vs_first_close_pct < -50:
            tags.append("较首日腰斩")

        summary = _cn_junior_summary(months_listed, vs_issue_pct, vs_first_close_pct, first_chg)

        out.append({
            "code": code,
            "name": str(r.get("股票简称") or ""),
            "board": _board_of_cn(code),
            "industry": "",  # TODO: 等 akshare 网络恢复 + 加 _enrich_cn_industry() 补
            "list_date": list_date_str,
            "months_listed": round(months_listed, 1),
            "issue_price": round(issue_price, 2),
            "current_price": round(current_price, 2),
            "vs_issue_pct": round(vs_issue_pct, 1),
            "first_day_change_pct": round(first_chg, 1) if first_chg is not None else None,
            "first_close": round(first_close, 2) if first_close else None,
            "vs_first_close_pct": round(vs_first_close_pct, 1) if vs_first_close_pct is not None else None,
            "broken_issue": broken or vs_issue_pct < 0,
            "score": total,
            "score_breakdown": {
                "discount_to_issue": round(s_discount, 1),
                "time_decay": round(s_time, 1),
                "first_day_premium": round(s_first, 1),
                "vs_first_close": round(s_vs_first, 1),
            },
            "tags": tags,
            "summary": summary,
            "in_holdings": code in holdings,
            "in_watchlist": code in watchlist,
        })
    out.sort(key=lambda x: -x["score"])
    return out


def slim_cn_ipo_calendar(raw: dict) -> dict:
    """复用 ipo_calendar.json，精简前端字段。"""
    today = date.today()

    def _e(e: dict) -> dict:
        sub_d = e.get("subscribe_date")
        try:
            d_days = (datetime.fromisoformat(str(sub_d)[:10]).date() - today).days if sub_d else None
        except Exception:
            d_days = None
        return {
            "code": e.get("code"),
            "subscribe_code": e.get("subscribe_code"),
            "name": e.get("name"),
            "board": e.get("board"),
            "subscribe_date": sub_d,
            "listing_date": e.get("listing_date"),
            "issue_price": e.get("issue_price"),
            "pe_ratio": e.get("pe_ratio"),
            "industry": e.get("industry"),
            "theme": e.get("theme"),
            "ai_relevance": e.get("ai_relevance"),
            "days_to_subscribe": d_days,
        }

    return {
        "fetched_at": raw.get("fetched_at"),
        "fetch_status": raw.get("fetch_status"),
        "upcoming_subscription": [_e(x) for x in raw.get("upcoming_subscription") or []],
        "awaiting_listing": [_e(x) for x in raw.get("awaiting_listing") or []],
        "recently_listed": [_e(x) for x in (raw.get("recently_listed") or [])[:30]],
    }


# ═════════════════════════════════════════════════════
#  美股 (NASDAQ 公开 API + yfinance 批拉价格)
# ═════════════════════════════════════════════════════

US_IPO_UNIVERSE_CACHE = CACHE_DIR / "us_ipo_universe.json"
US_IPO_PRICE_CACHE = CACHE_DIR / "us_ipo_prices.json"
US_IPO_META_CACHE = CACHE_DIR / "us_ipo_meta.json"
US_IPO_PRICE_TTL_HOURS = 24
US_IPO_META_TTL_HOURS = 168  # 7 天 — sector/industry 几乎不变,market cap 不需要每天更新


def _is_spac(name: str, symbol: str, issue_price: float | None) -> bool:
    """SPAC 启发式判断:发行价正好 $10 + (名称含 Acquisition 或 ticker 后缀 U/WS)。"""
    nm = (name or "").upper()
    sym = (symbol or "").upper()
    if issue_price is not None and abs(issue_price - 10.0) < 0.01:
        if any(kw in nm for kw in ["ACQUISITION", "ACQUISTION", "SPAC"]):
            return True
        if sym.endswith(("U", "WS")) and len(sym) >= 4:
            return True
    return False


def _batch_yf_history(symbols: list[str], ipo_dates: dict[str, str],
                       batch: int = 100, period: str = "2y") -> dict[str, dict]:
    """yfinance 批拉历史 — 同时算 current_price / low_since_ipo / high_since_ipo / avg_volume_30d。

    ipo_dates: {SYM: 'YYYY-MM-DD'} — 用于过滤"上市前"的脏数据(yfinance 偶尔回填)。
    """
    if not symbols:
        return {}
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        logger.warning("yfinance/pandas not installed; skip US history refresh")
        return {}

    out: dict[str, dict] = {}
    for i in range(0, len(symbols), batch):
        chunk = symbols[i:i + batch]
        try:
            data = yf.download(chunk, period=period, progress=False, threads=True, auto_adjust=False)
        except Exception as e:
            logger.warning("yfinance batch %d-%d failed: %s", i, i + batch, e)
            continue
        if data is None or data.empty:
            continue
        # MultiIndex (level0=Field, level1=Ticker) vs flat
        is_multi = isinstance(data.columns, pd.MultiIndex)

        for sym in chunk:
            try:
                if is_multi:
                    if ("Close", sym) not in data.columns:
                        continue
                    close = data["Close"][sym].dropna()
                    volume = data["Volume"][sym].dropna() if ("Volume", sym) in data.columns else None
                else:
                    close = data["Close"].dropna() if "Close" in data.columns else None
                    volume = data["Volume"].dropna() if "Volume" in data.columns else None
                if close is None or close.empty:
                    continue
                # 过滤上市前的脏数据
                ipo_s = ipo_dates.get(sym)
                if ipo_s:
                    try:
                        ipo_d = datetime.fromisoformat(ipo_s).date()
                        close = close[close.index.date >= ipo_d]
                        if volume is not None:
                            volume = volume[volume.index.date >= ipo_d]
                    except Exception:
                        pass
                if close.empty:
                    continue
                low = float(close.min())
                high = float(close.max())
                cur = float(close.iloc[-1])
                low_idx = close.idxmin()
                low_date = str(low_idx.date()) if hasattr(low_idx, "date") else None
                avg_vol = None
                if volume is not None and len(volume) >= 5:
                    avg_vol = int(volume.tail(30).mean())
                out[sym.upper()] = {
                    "price": cur,
                    "date": str(close.index[-1].date()),
                    "low_since_ipo": low,
                    "low_date": low_date,
                    "high_since_ipo": high,
                    "avg_volume_30d": avg_vol,
                }
            except Exception as e:
                logger.debug("history parse %s failed: %s", sym, e)
                continue
    return out


def _batch_yf_info(symbols: list[str], max_workers: int = 8) -> dict[str, dict]:
    """yfinance Ticker.info 拿 sector / industry / marketCap — 并行,因为单只 ~1s。"""
    if not symbols:
        return {}
    try:
        import yfinance as yf
        from concurrent.futures import ThreadPoolExecutor, as_completed
    except ImportError:
        return {}
    out: dict[str, dict] = {}

    def _one(sym: str) -> tuple[str, dict] | None:
        try:
            t = yf.Ticker(sym)
            info = t.info or {}
            return sym.upper(), {
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "market_cap": info.get("marketCap"),  # raw USD
                "long_name": info.get("longName"),
            }
        except Exception as e:
            logger.debug("info %s failed: %s", sym, e)
            return None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for fut in as_completed([pool.submit(_one, s) for s in symbols]):
            r = fut.result()
            if r:
                out[r[0]] = r[1]
    return out


def _load_cache(path: Path, ttl_hours: int) -> dict:
    if not path.exists():
        return {"fetched_at": None, "entries": {}}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if d.get("fetched_at"):
            age_h = (datetime.now() - datetime.fromisoformat(d["fetched_at"])).total_seconds() / 3600
            if age_h > ttl_hours:
                return {"fetched_at": None, "entries": {}}
        return d
    except Exception:
        return {"fetched_at": None, "entries": {}}


def _save_cache(path: Path, entries: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"fetched_at": datetime.now().isoformat(timespec="seconds"), "entries": entries}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _enrich_with_price(items: list[dict], hist_cache: dict, meta_cache: dict) -> list[dict]:
    """给 IPO 日历条目附上 current_price / vs_issue / sector / market_cap (如果有缓存)。"""
    out = []
    for e in items:
        sym = (e.get("symbol") or "").upper()
        h = hist_cache.get(sym) or {}
        m = meta_cache.get(sym) or {}
        cur = _safe_float(h.get("price"))
        issue = _safe_float(e.get("issue_price"))
        vs_issue = None
        if cur and issue and issue > 0:
            vs_issue = round((cur - issue) / issue * 100.0, 1)
        out.append({
            **e,
            "current_price": round(cur, 2) if cur else None,
            "vs_issue_pct": vs_issue,
            "sector": m.get("sector"),
            "industry": m.get("industry"),
            "market_cap_m": round(m.get("market_cap") / 1e6, 1) if m.get("market_cap") else None,
        })
    return out


def build_us_ipo_calendar(nasdaq_window: dict) -> dict:
    """从 NASDAQ window 构造 dashboard 三栏:
       - upcoming_filing  (即将申购): filed 列表,近 60 天内
       - awaiting_listing (已申购未上市): priced 列表,priced_date 在过去 7 天内
       - recently_listed  (近 30 日上市): priced 列表,priced_date 在过去 30 天内
       这里 awaiting_listing 和 recently_listed 会有部分重叠(过去 7 天内的同时属于两者),
       前端处理:awaiting 只显示「最近 7 天 priced」,recently 显示「7-30 天前 priced」避免重复
    """
    today = date.today()
    priced = nasdaq_window.get("priced") or []
    filed = nasdaq_window.get("filed") or []

    def _delta_days(s: str | None) -> int | None:
        if not s:
            return None
        try:
            return (today - datetime.fromisoformat(s).date()).days
        except Exception:
            return None

    upcoming_filing = []
    for f in filed:
        d = _delta_days(f.get("filed_date"))
        if d is None or d > 60:
            continue
        if d < 0:
            continue  # 不应出现 (filed 日期未来),但防御
        upcoming_filing.append({**f, "days_since_filed": d})
    upcoming_filing.sort(key=lambda x: x.get("days_since_filed", 999))

    awaiting = []
    recently = []
    for p in priced:
        d = _delta_days(p.get("priced_date"))
        if d is None:
            continue
        item = {**p, "days_since_priced": d}
        if 0 <= d <= 7:
            awaiting.append(item)
        if 0 <= d <= 30:
            recently.append(item)
    awaiting.sort(key=lambda x: x.get("days_since_priced", 999))
    recently.sort(key=lambda x: x.get("days_since_priced", 999))

    return {
        "upcoming_filing": upcoming_filing[:30],
        "awaiting_listing": awaiting[:30],
        "recently_listed": recently[:50],
    }


def fetch_us_junior_pool(holdings: set[str], watchlist: set[str],
                          nasdaq_priced: list[dict],
                          months_min: int = 6, months_max: int = 24,
                          exclude_spac: bool = True,
                          min_price_usd: float = 1.0,
                          min_market_cap_usd: float = 50_000_000) -> list[dict]:
    """美股次新股底部观察池 — 基于 NASDAQ 24 月 priced 列表。

    质量闸门 (硬过滤,不入池):
      - 现价 < min_price_usd ($1)        → 排除仙股 (NASDAQ < $1 持续 6 月触发退市)
      - 市值 < min_market_cap_usd ($50M) → 排除超微盘 (going concern 风险)
      - SPAC (issue=$10 + Acquisition 名)→ 排除空壳
      - 上市 < 6 月 / > 24 月            → 不在"次新股"窗口

    打分维度 (5 维,总分 100):
      - discount_to_issue (35):  跌破发行价越深越高分
      - time_decay        (25):  上市 12-18 月最高
      - liquidity         (15):  日均成交额 (注: 是金额,不是股数,避免仙股偏差)
      - rebound_bonus     (15):  从最低反弹 20-60% (转折信号)
      - in_your_pool      (10):  你的持仓/自选股加成
    """
    today = date.today()
    # 过滤窗口 + 去掉 SPAC + 必须有 issue_price + 必须有 symbol
    candidates: list[dict] = []
    for e in nasdaq_priced:
        sym = e.get("symbol")
        if not sym:
            continue
        priced_date_s = e.get("priced_date")
        if not priced_date_s:
            continue
        try:
            ipo_d = datetime.fromisoformat(priced_date_s).date()
        except Exception:
            continue
        days_listed = (today - ipo_d).days
        months_listed = days_listed / 30.4
        if months_listed < months_min or months_listed > months_max:
            continue
        issue_price = _safe_float(e.get("issue_price"))
        if issue_price is None or issue_price <= 0:
            continue
        if exclude_spac and _is_spac(e.get("name") or "", sym, issue_price):
            continue
        candidates.append({**e, "months_listed": months_listed, "days_listed": days_listed})

    if not candidates:
        return []

    # 批拉历史 (yfinance) — 24h 缓存
    ipo_dates = {c["symbol"]: c.get("priced_date") for c in candidates}
    hist_cache = _load_cache(US_IPO_PRICE_CACHE, US_IPO_PRICE_TTL_HOURS)
    cached_hist = hist_cache.get("entries") or {}
    syms_need_hist = [c["symbol"] for c in candidates if c["symbol"] not in cached_hist]
    if syms_need_hist:
        logger.info("US 次新股池: %d 只需拉历史 (yfinance batch,缓存 %d)",
                    len(syms_need_hist), len(cached_hist))
        fresh = _batch_yf_history(syms_need_hist, ipo_dates, batch=100, period="2y")
        cached_hist.update(fresh)
        for sym in syms_need_hist:
            if sym not in cached_hist:
                cached_hist[sym] = {"price": None}
        _save_cache(US_IPO_PRICE_CACHE, cached_hist)
    else:
        logger.info("US 次新股池: 历史缓存命中 (%d 只)", len(candidates))

    # info enrich (sector/marketCap) — 7d 缓存,只对有当前价的拉
    meta_cache = _load_cache(US_IPO_META_CACHE, US_IPO_META_TTL_HOURS)
    cached_meta = meta_cache.get("entries") or {}
    syms_with_price = [c["symbol"] for c in candidates if (cached_hist.get(c["symbol"]) or {}).get("price")]
    syms_need_meta = [s for s in syms_with_price if s not in cached_meta]
    if syms_need_meta:
        logger.info("US 次新股池: %d 只需拉 info (yfinance 并行,缓存 %d)",
                    len(syms_need_meta), len(cached_meta))
        fresh = _batch_yf_info(syms_need_meta, max_workers=8)
        cached_meta.update(fresh)
        for sym in syms_need_meta:
            if sym not in cached_meta:
                cached_meta[sym] = {"sector": None}
        _save_cache(US_IPO_META_CACHE, cached_meta)
    else:
        logger.info("US 次新股池: info 缓存命中 (%d 只)", len(syms_with_price))

    # 打分
    out: list[dict] = []
    rejected_penny = 0
    rejected_micro = 0
    rejected_no_price = 0
    for c in candidates:
        sym = c["symbol"]
        issue_price = float(c["issue_price"])
        h = cached_hist.get(sym) or {}
        meta = cached_meta.get(sym) or {}
        current_price = _safe_float(h.get("price"))
        low = _safe_float(h.get("low_since_ipo"))
        high = _safe_float(h.get("high_since_ipo"))
        avg_vol = h.get("avg_volume_30d")
        market_cap = _safe_float(meta.get("market_cap"))
        sector = meta.get("sector") or ""
        industry = meta.get("industry") or ""
        months_listed = c["months_listed"]

        # ─── 质量闸门 ─── (硬过滤,不入池)
        if current_price is None:
            rejected_no_price += 1
            continue
        if current_price < min_price_usd:
            rejected_penny += 1
            continue
        if market_cap is not None and market_cap < min_market_cap_usd:
            rejected_micro += 1
            continue
        # market_cap 为 None (yfinance 没拿到) 时不能判,默认放行 — 让流动性维度兜底

        # 派生:vs_issue / rebound_from_low / drawdown_from_high
        vs_issue_pct = None
        rebound_pct = None
        drawdown_pct = None
        if current_price is not None and current_price > 0:
            vs_issue_pct = (current_price - issue_price) / issue_price * 100.0
            if low and low > 0:
                rebound_pct = (current_price - low) / low * 100.0
            if high and high > 0:
                drawdown_pct = (current_price - high) / high * 100.0

        # 打分 (100 制)
        # discount_to_issue (35): 跌破越深越高分
        s_discount = min(35.0, abs(vs_issue_pct) / 1.5) if (vs_issue_pct is not None and vs_issue_pct <= 0) else 0.0
        # time_decay (25)
        if 12 <= months_listed <= 18:
            s_time = 25.0
        elif 9 <= months_listed < 12 or 18 < months_listed <= 21:
            s_time = 20.0
        elif 6 <= months_listed < 9 or 21 < months_listed <= 24:
            s_time = 14.0
        else:
            s_time = 8.0
        # liquidity (15): 用日均成交额 ($), 不是股数 (避免仙股偏差)
        # $5M+/day = 满分 · $1M = 10 · $200K = 5 · 更低 = 0
        dollar_vol = (avg_vol * current_price) if (avg_vol and current_price) else None
        if dollar_vol and dollar_vol >= 5_000_000:
            s_liquid = 15.0
        elif dollar_vol and dollar_vol >= 1_000_000:
            s_liquid = 10.0
        elif dollar_vol and dollar_vol >= 200_000:
            s_liquid = 5.0
        else:
            s_liquid = 0.0
        # rebound_bonus (15): 已经从最低反弹 20-50% 算"开始反转"信号
        if rebound_pct is not None:
            if 20 <= rebound_pct <= 60:
                s_rebound = 15.0
            elif 10 <= rebound_pct < 20 or 60 < rebound_pct <= 100:
                s_rebound = 8.0
            else:
                s_rebound = 0.0
        else:
            s_rebound = 0.0
        # 科技股识别 (Technology + Communication Services 都算,后者含 GOOG/META 类)
        # 注: 仅用于标签 + 前端 filter,不计入打分 — 打分保持客观,用户偏好走筛选
        is_tech = sector in {"Technology", "Communication Services"}
        # in_your_pool (10)
        s_pool = 10.0 if (sym in holdings or sym in watchlist) else 0.0

        total = round(s_discount + s_time + s_liquid + s_rebound + s_pool, 1)

        tags = []
        if is_tech:
            tags.append("🔬 科技")
        if vs_issue_pct is not None and vs_issue_pct < 0:
            tags.append("已破发")
        if 12 <= months_listed <= 18:
            tags.append("解禁窗口")
        if vs_issue_pct is not None and vs_issue_pct < -50:
            tags.append("腰斩")
        if rebound_pct is not None and 20 <= rebound_pct <= 60:
            tags.append("从底反弹")
        if dollar_vol is not None and dollar_vol < 200_000:
            tags.append("低流动性")

        out.append({
            "symbol": sym,
            "name": c.get("name") or "",
            "exchange": c.get("exchange") or "",
            "sector": sector,
            "industry": industry,
            "ipo_date": c.get("priced_date"),
            "months_listed": round(months_listed, 1),
            "issue_price": round(issue_price, 2),
            "current_price": round(current_price, 2) if current_price else None,
            "low_since_ipo": round(low, 2) if low else None,
            "low_date": h.get("low_date"),
            "high_since_ipo": round(high, 2) if high else None,
            "vs_issue_pct": round(vs_issue_pct, 1) if vs_issue_pct is not None else None,
            "rebound_pct": round(rebound_pct, 1) if rebound_pct is not None else None,
            "drawdown_pct": round(drawdown_pct, 1) if drawdown_pct is not None else None,
            "market_cap_m": round(market_cap / 1e6, 1) if market_cap else None,
            "avg_volume_30d": avg_vol,
            "deal_value_usd": c.get("deal_value_usd"),
            "shares_offered": c.get("shares_offered"),
            "score": total,
            "is_tech": is_tech,
            "dollar_volume_30d": int(dollar_vol) if dollar_vol else None,
            "score_breakdown": {
                "discount_to_issue": round(s_discount, 1),
                "time_decay": round(s_time, 1),
                "liquidity": round(s_liquid, 1),
                "rebound_bonus": round(s_rebound, 1),
                "in_your_pool": round(s_pool, 1),
            },
            "tags": tags,
            "in_holdings": sym in holdings,
            "in_watchlist": sym in watchlist,
        })

    out.sort(key=lambda x: -x["score"])
    logger.info("US 次新股池 质量闸门: 候选 %d → 入池 %d (剔除: 仙股 %d / 微盘 %d / 无价 %d)",
                len(candidates), len(out), rejected_penny, rejected_micro, rejected_no_price)
    return out


# ═════════════════════════════════════════════════════
#  港股（数据源受限的 placeholder）
# ═════════════════════════════════════════════════════

HK_DATA_NOTE = (
    "港股 IPO/解禁/次新股的开源数据源极有限：finnhub 免费层不含港股，"
    "akshare 的港股 IPO 接口（stock_ipo_hk_ths）实际返回的是 A 股数据（命名 bug）。"
    "目前推荐外部跟踪：① HKEX 官网 disclosure；② 富途/老虎 app 的「打新」入口；"
    "③ AAStocks.com 的 IPO 频道。本系统计划在接入 Wind/Choice 或 HKEX 付费 API 后补全。"
)
HK_LINKS = [
    {"label": "HKEX 新上市公司", "url": "https://www.hkexnews.hk/listedco/listconews/newlist/sehknewlist.htm"},
    {"label": "AAStocks IPO 频道", "url": "http://www.aastocks.com/sc/stocks/market/ipo/upcomingipo/companysummary"},
]


def build_hk_placeholder(holdings: set[str], watchlist: set[str]) -> dict:
    return {
        "available": False,
        "note": HK_DATA_NOTE,
        "external_links": HK_LINKS,
        "ipo_calendar": {"upcoming_subscription": [], "awaiting_listing": [], "recently_listed": []},
        "unlock_radar": [],
        "junior_pool": [],
        "your_pool_size": len(holdings) + len(watchlist),
    }


# ═════════════════════════════════════════════════════
#  主入口
# ═════════════════════════════════════════════════════

def build_radar() -> dict:
    pools = _load_pool_symbols()
    logger.info("持仓: CN %d / US %d / HK %d  ·  自选: CN %d / US %d / HK %d",
                len(pools["cn"]["holdings"]), len(pools["us"]["holdings"]), len(pools["hk"]["holdings"]),
                len(pools["cn"]["watchlist"]), len(pools["us"]["watchlist"]), len(pools["hk"]["watchlist"]))

    # —— A 股 ——
    ipo_raw = _load_ipo_calendar()
    cn_ipo = slim_cn_ipo_calendar(ipo_raw) if ipo_raw else {
        "fetched_at": None, "fetch_status": {},
        "upcoming_subscription": [], "awaiting_listing": [], "recently_listed": [],
    }
    if not ipo_raw:
        logger.warning("ipo_calendar.json 缺失 — 跑过 ipo_daily.py 了吗？")
    logger.info("[CN] 拉解禁雷达（未来 90 天）...")
    cn_unlock = fetch_cn_unlock_radar(pools["cn"]["holdings"], pools["cn"]["watchlist"], horizon_days=90)
    logger.info("[CN]   → %d 条解禁", len(cn_unlock))
    logger.info("[CN] 拉次新股池（上市 6-24 月）...")
    cn_junior = fetch_cn_junior_pool(pools["cn"]["holdings"], pools["cn"]["watchlist"], months_min=6, months_max=24)
    logger.info("[CN]   → %d 只候选", len(cn_junior))
    logger.info("[CN] 补 industry 字段（缓存 7 天 + fail-soft）...")
    _enrich_cn_industry(cn_junior)

    # —— 美股 (NASDAQ 公开 API + yfinance) ——
    logger.info("[US] 拉 NASDAQ IPO window (过去 24 月 priced + 未来 2 月 filed)...")
    try:
        from stock_research.core import nasdaq_ipo
        nasdaq_win = nasdaq_ipo.fetch_window(months_back=24, months_forward=2)
        logger.info("[US]   → priced %d · filed %d · 月份 %d",
                    len(nasdaq_win["priced"]), len(nasdaq_win["filed"]),
                    len(nasdaq_win["months_pulled"]))
    except Exception as e:
        logger.warning("[US] NASDAQ window 失败: %s", e)
        nasdaq_win = {"priced": [], "filed": [], "months_pulled": []}
    us_ipo_cal = build_us_ipo_calendar(nasdaq_win)
    logger.info("[US]   IPO 日历: 即将申报 %d · 已定价未上市 %d · 近 30 日上市 %d",
                len(us_ipo_cal["upcoming_filing"]),
                len(us_ipo_cal["awaiting_listing"]),
                len(us_ipo_cal["recently_listed"]))
    logger.info("[US] 拉次新股池 (NASDAQ priced ∩ 上市 6-24 月,非 SPAC)...")
    us_junior = fetch_us_junior_pool(
        pools["us"]["holdings"], pools["us"]["watchlist"],
        nasdaq_priced=nasdaq_win["priced"],
        months_min=6, months_max=24,
    )
    logger.info("[US]   → %d 只候选 (有当前价: %d)",
                len(us_junior),
                sum(1 for x in us_junior if x.get("current_price")))

    # 近 30 日上市 + 已定价未上市 也借同一份 yfinance 缓存补当前价
    # (fetch_us_junior_pool 已经把这些 ticker 拉过且写进缓存)
    hist_cache = (_load_cache(US_IPO_PRICE_CACHE, US_IPO_PRICE_TTL_HOURS).get("entries") or {})
    meta_cache = (_load_cache(US_IPO_META_CACHE, US_IPO_META_TTL_HOURS).get("entries") or {})
    # 但「近 30 日上市」的 ticker 可能不在 junior_pool 缓存里(<6 月),需要额外补拉
    recent_syms = [e["symbol"] for e in us_ipo_cal["recently_listed"]
                   if e.get("symbol") and e["symbol"] not in hist_cache]
    if recent_syms:
        logger.info("[US] 补拉「近 30 日上市」%d 只价格...", len(recent_syms))
        recent_dates = {e["symbol"]: e.get("priced_date") for e in us_ipo_cal["recently_listed"]}
        fresh = _batch_yf_history(recent_syms, recent_dates, batch=50, period="60d")
        hist_cache.update(fresh)
        for s in recent_syms:
            if s not in hist_cache:
                hist_cache[s] = {"price": None}
        _save_cache(US_IPO_PRICE_CACHE, hist_cache)
    us_ipo_cal["recently_listed"] = _enrich_with_price(us_ipo_cal["recently_listed"], hist_cache, meta_cache)
    us_ipo_cal["awaiting_listing"] = _enrich_with_price(us_ipo_cal["awaiting_listing"], hist_cache, meta_cache)

    # —— 港股 ——
    logger.info("[HK] 数据源受限，输出 placeholder")
    hk = build_hk_placeholder(pools["hk"]["holdings"], pools["hk"]["watchlist"])

    # —— 本周事件（A 股为主，其他市场摘要）——
    today = date.today()
    week_end = today + timedelta(days=7)

    def _in_week(date_str: str | None) -> bool:
        if not date_str:
            return False
        try:
            d = datetime.fromisoformat(str(date_str)[:10]).date()
            return today <= d <= week_end
        except Exception:
            return False

    week_cn_subs = [x for x in cn_ipo["upcoming_subscription"] if _in_week(x.get("subscribe_date"))]
    week_cn_listings = [x for x in cn_ipo["awaiting_listing"] if _in_week(x.get("listing_date"))]
    week_cn_unlocks = [x for x in cn_unlock if x["days_to_unlock"] <= 7]
    week_cn_unlocks_in_pool = [x for x in week_cn_unlocks if x["in_holdings"] or x["in_watchlist"]]
    # 美股「本周事件」= 已定价未上市 + 近 7 日定价 + 近 7 日 filed (即将申报)
    week_us_priced = [x for x in us_ipo_cal["awaiting_listing"] if x.get("days_since_priced", 99) <= 7]
    week_us_filed = [x for x in us_ipo_cal["upcoming_filing"] if x.get("days_since_filed", 99) <= 7]

    summary = {
        "cn": {
            "subscribe_count": len(week_cn_subs),
            "listing_count": len(week_cn_listings),
            "unlock_count": len(week_cn_unlocks),
            "unlock_in_pool_count": len(week_cn_unlocks_in_pool),
            "unlock_in_pool_codes": [
                {"code": x["code"], "name": x["name"], "date": x["unlock_date"], "stress": x["stress_score"]}
                for x in week_cn_unlocks_in_pool[:5]
            ],
            "junior_top3": [
                {"code": x["code"], "name": x["name"], "score": x["score"], "vs_issue_pct": x["vs_issue_pct"]}
                for x in cn_junior[:3]
            ],
        },
        "us": {
            "priced_7d_count": len(week_us_priced),
            "filed_7d_count": len(week_us_filed),
            "junior_count": len(us_junior),
            "broken_count": sum(1 for x in us_junior if (x.get("vs_issue_pct") or 0) < 0),
            "ipo_top3": [
                {"symbol": x["symbol"], "name": x["name"], "date": x.get("priced_date"),
                 "exchange": x.get("exchange"), "issue_price": x.get("issue_price")}
                for x in week_us_priced[:3]
            ],
            "junior_top3": [
                {"symbol": x["symbol"], "name": x["name"], "vs_issue_pct": x["vs_issue_pct"],
                 "score": x["score"]}
                for x in us_junior[:3]
            ],
        },
        "hk": {"available": False},
    }

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary_week": summary,
        "markets": {
            "cn": {
                "available": True,
                "ipo_calendar": cn_ipo,
                "unlock_radar": cn_unlock,
                "junior_pool": cn_junior,
            },
            "us": {
                "available": True,
                "ipo_calendar": us_ipo_cal,
                "unlock_radar": [],
                "junior_pool": us_junior,
                "data_source": "NASDAQ public API + yfinance batch",
                "note": "美股 lockup（IPO 锁定期）数据需付费源解析 S-1 招股书，暂未接入。",
            },
            "hk": hk,
        },
        "params": {
            "unlock_horizon_days": 90,
            "junior_months_range": [6, 24],
            "us_ipo_horizon_days": 120,
        },
    }


def main() -> int:
    radar = build_radar()
    out = REPO / "data" / "latest" / "junior_stock_radar.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(radar, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"✅ junior_stock_radar.json 已写入 {out}")
    cn = radar["markets"]["cn"]
    us = radar["markets"]["us"]
    print(f"   [CN] IPO 申购 {len(cn['ipo_calendar']['upcoming_subscription'])} · "
          f"解禁 {len(cn['unlock_radar'])} · 次新股 {len(cn['junior_pool'])}")
    print(f"   [US] 即将申报 {len(us['ipo_calendar']['upcoming_filing'])} · "
          f"已定价未上市 {len(us['ipo_calendar']['awaiting_listing'])} · "
          f"近 30 日上市 {len(us['ipo_calendar']['recently_listed'])} · "
          f"次新股 {len(us['junior_pool'])}")
    print(f"   [HK] 数据源受限：placeholder + 2 外链")
    s = radar["summary_week"]
    print(f"   本周事件: CN 申购 {s['cn']['subscribe_count']} / 上市 {s['cn']['listing_count']} / "
          f"解禁 {s['cn']['unlock_count']}（池子内 {s['cn']['unlock_in_pool_count']}）· "
          f"US 定价 {s['us']['priced_7d_count']} / 申报 {s['us']['filed_7d_count']}")
    has_data = bool(cn["junior_pool"] or cn["unlock_radar"] or us["ipo_calendar"] or us["junior_pool"])
    return 0 if has_data else 2


if __name__ == "__main__":
    sys.exit(main())
