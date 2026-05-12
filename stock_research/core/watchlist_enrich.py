"""Watchlist 自动补全：给一个 ticker 自动算出名字 / 行业 / AI 关联 / 主题 / 产业链等。

数据来源：
  - yfinance Ticker.info（name, sector, industry, longBusinessSummary, market_cap...）
  - gics_classifier.classify（GICS industry → AI 关联度 + 主题）
  - 规则推断（chain / chain_tier / chain_role）
  - layman_intro 用规则模板（无 LLM，Anthropic key 0 余额）

用法：
    from stock_research.core.watchlist_enrich import enrich_one
    data = enrich_one("NVDA")  # → dict 含全字段
"""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Any

# 让 sibling 模块可 import
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts" / "lib"))  # 2026-05-11 lib 迁移


def _infer_market(code: str) -> str:
    """从 ticker 后缀推断市场。"""
    c = (code or "").strip().upper()
    if c.endswith(".SS") or c.endswith(".SH"):
        return "A股·沪交所"
    if c.endswith(".SZ"):
        return "A股·深交所"
    if c.endswith(".BJ"):
        return "A股·北交所"
    if c.endswith(".HK"):
        return "港股"
    if c.endswith(".KS") or c.endswith(".KQ"):
        return "韩股"
    if c.endswith(".AX"):
        return "澳股"
    if c.endswith(".L") or c.endswith(".IL"):
        return "英股"
    if c.endswith(".T") or c.endswith(".TYO"):
        return "日股"
    if c.isdigit() and len(c) == 6:
        # 裸 6 位数字 = A 股，按首位判断板块
        if c.startswith(("00", "30", "20")):
            return "A股·深交所"
        if c.startswith(("8", "9")):
            return "A股·北交所"
        return "A股·沪交所"
    # 纯字母 ticker → 美股
    if c.replace("-", "").replace(".", "").isalpha():
        return "美股"
    return ""


# 产业链推断规则：industry / theme 关键词 → (chain, chain_role)
_CHAIN_RULES = [
    # AI 算力主线
    ("Semiconductor", "AI 算力", "IDM"),
    ("半导体", "AI 算力", "IDM"),
    ("GPU", "AI 算力", "GPU"),
    ("光通信", "AI 算力", "网络芯片"),
    ("ASIC", "AI 算力", "网络芯片"),
    ("Foundry", "AI 算力", "代工"),
    # 数据中心电力
    ("Electrical Equipment", "数据中心电力", "基础设施"),
    ("电力", "数据中心电力", "基础设施"),
    ("Utilities", "数据中心电力", "基础设施"),
    ("Construction", "数据中心电力", "基础设施"),
    # 稀缺资源
    ("Mining", "稀缺资源", "材料"),
    ("Rare", "稀缺资源", "材料"),
    ("稀土", "稀缺资源", "材料"),
    ("Uranium", "稀缺资源", "材料"),
    # 软件 / 应用
    ("Software", "AI 应用", "应用层"),
    ("Cloud", "AI 应用", "服务"),
    # 兜底
]


def _infer_chain(industry: str, theme: str) -> tuple[str | None, str | None, str | None]:
    """根据 industry + theme 推断产业链 / 角色。

    chain_tier 留空让用户手填（核心/一线/二线/三线需要主观判断）。
    """
    blob = f"{industry or ''} {theme or ''}"
    for kw, chain, role in _CHAIN_RULES:
        if kw.lower() in blob.lower():
            return chain, None, role
    return None, None, None


def _make_layman_intro(name: str, industry: str, business: str) -> str:
    """规则模板 1 句话新人解释（<60 字）。

    优先用 yfinance 的 longBusinessSummary 第一句，砍到 60 字内。
    没有就用 「industry 的 N 家」 兜底。
    """
    if business:
        # 取第一句（句号或句首）
        s = business.split("。")[0].split(". ")[0].strip()
        if 5 <= len(s) <= 60:
            return s
        if len(s) > 60:
            return s[:58] + "…"
    if industry:
        return f"{industry} 行业的一家公司"
    return ""


def _fmt_money(v: float | None, currency: str = "") -> str:
    """财报金额格式化：5.95e9 → 'USD5.95B'。"""
    if v is None:
        return "?"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    if v != v:  # NaN
        return "?"
    sign = "-" if v < 0 else ""
    av = abs(v)
    if av >= 1e12: return f"{sign}{currency}{av/1e12:.2f}T"
    if av >= 1e9:  return f"{sign}{currency}{av/1e9:.2f}B"
    if av >= 1e6:  return f"{sign}{currency}{av/1e6:.2f}M"
    return f"{sign}{currency}{av:,.0f}"


def _safe_cell(qis, row: str, col) -> float | None:
    """从季报 DataFrame 里取 (row, col) 单元格；不存在 / NaN 返回 None。"""
    try:
        if row not in qis.index:
            return None
        v = qis.loc[row][col]
        if v is None:
            return None
        v = float(v)
        return v if v == v else None  # 过滤 NaN
    except Exception:
        return None


def _pct_yoy(now: float | None, yoy: float | None) -> float | None:
    if now is None or yoy is None or yoy == 0:
        return None
    try:
        return (now - yoy) / abs(yoy) * 100
    except Exception:
        return None


def fetch_earnings_quarters(info: dict, ticker_obj) -> list[dict]:
    """从 yfinance 拉**全部可用季度**财报，结构化返回（用于 earnings_history 写库）。

    返回 list[dict]，每条：
      fiscal_period (date), revenue, net_income, diluted_eps,
      revenue_yoy_pct, net_income_yoy_pct, eps_yoy_pct, currency, source
    最新季在 list[0]。

    主路径：quarterly_income_stmt（多季 + 可算 YoY）
    Fallback：info TTM 快照（单条 fiscal_period 用 mostRecentQuarter / lastFiscalYearEnd）
    """
    from datetime import datetime as _dt, date as _date
    currency = info.get("financialCurrency") or ""
    quarters: list[dict] = []

    # ─── 主路径：季报 DataFrame ───
    try:
        qis = ticker_obj.quarterly_income_stmt
    except Exception:
        qis = None

    if qis is not None and not qis.empty and len(qis.columns) > 0:
        cols = list(qis.columns)
        # 每个列代表一个季度（最新在前）
        for i, col in enumerate(cols):
            # 同比：i+4 个位置（标准日历季度有连续 4 季）
            yoy_col = cols[i + 4] if i + 4 < len(cols) else None
            rev = _safe_cell(qis, "Total Revenue", col)
            ni  = _safe_cell(qis, "Net Income", col)
            eps = _safe_cell(qis, "Diluted EPS", col)
            rev_yoy = _safe_cell(qis, "Total Revenue", yoy_col) if yoy_col is not None else None
            ni_yoy  = _safe_cell(qis, "Net Income", yoy_col) if yoy_col is not None else None
            eps_yoy = _safe_cell(qis, "Diluted EPS", yoy_col) if yoy_col is not None else None
            # 至少有一项主指标才入库
            if rev is None and ni is None and eps is None:
                continue
            try:
                fp = col.date() if hasattr(col, "date") else _date.fromisoformat(str(col)[:10])
            except Exception:
                continue
            quarters.append({
                "fiscal_period": fp,
                "revenue": rev,
                "net_income": ni,
                "diluted_eps": eps,
                "revenue_yoy_pct": _pct_yoy(rev, rev_yoy),
                "net_income_yoy_pct": _pct_yoy(ni, ni_yoy),
                "eps_yoy_pct": _pct_yoy(eps, eps_yoy),
                "currency": currency,
                "source": "yfinance_quarterly",
            })
        if quarters:
            return quarters

    # ─── Fallback：info TTM 快照（1 行）───
    rev = info.get("totalRevenue")
    teps = info.get("trailingEps")
    if not rev and teps is None:
        return []
    # 用 mostRecentQuarter 作为 fiscal_period，缺时用 lastFiscalYearEnd
    fp_ts = info.get("mostRecentQuarter") or info.get("lastFiscalYearEnd")
    if not fp_ts:
        return []
    try:
        fp = _dt.fromtimestamp(int(fp_ts)).date()
    except Exception:
        return []
    return [{
        "fiscal_period": fp,
        "revenue": rev,
        "net_income": None,
        "diluted_eps": teps,
        "revenue_yoy_pct": (info.get("revenueGrowth") * 100) if info.get("revenueGrowth") else None,
        "net_income_yoy_pct": None,
        "eps_yoy_pct": (info.get("earningsGrowth") * 100) if info.get("earningsGrowth") else None,
        "currency": currency,
        "source": "yfinance_ttm_fallback",
    }]


def _summary_from_quarter(q: dict) -> str | None:
    """把一条 fetch_earnings_quarters 的结果格式化成"最新一句话摘要"（给 watchlist.earnings 字段）。"""
    if not q:
        return None
    fp = q["fiscal_period"]
    fp_str = fp.isoformat() if hasattr(fp, "isoformat") else str(fp)[:10]
    currency = q.get("currency") or ""
    source = q.get("source") or ""
    header = f"{fp_str} 季报（yfinance）：" if source == "yfinance_quarterly" \
             else f"TTM 财报快照（yfinance info · 季报不可用 · {fp_str}）："
    lines = [header]
    if q.get("revenue") is not None:
        s = f"营收 {_fmt_money(q['revenue'], currency)}"
        yoy = q.get("revenue_yoy_pct")
        if yoy is not None:
            s += f" ({yoy:+.1f}% YoY)"
        lines.append(s)
    if q.get("net_income") is not None:
        s = f"净利润 {_fmt_money(q['net_income'], currency)}"
        yoy = q.get("net_income_yoy_pct")
        if yoy is not None:
            s += f" ({yoy:+.1f}% YoY)"
        lines.append(s)
    if q.get("diluted_eps") is not None:
        s = f"摊薄 EPS {q['diluted_eps']:.2f} {currency}".strip()
        yoy = q.get("eps_yoy_pct")
        if yoy is not None:
            s += f" ({yoy:+.1f}% YoY)"
        lines.append(s)
    return "\n• ".join([lines[0]] + lines[1:]) if len(lines) > 1 else None


def fetch_earnings_summary(info: dict, ticker_obj) -> str | None:
    """从 yfinance 拉最近一季财报，生成文本摘要（给 watchlist.earnings 字段用）。

    内部复用 fetch_earnings_quarters，所以"摘要文本"和"history 表"永远来自同一个数据源、同一种解析。
    """
    quarters = fetch_earnings_quarters(info, ticker_obj)
    return _summary_from_quarter(quarters[0]) if quarters else None


def enrich_one(code: str, name: str | None = None) -> dict[str, Any]:
    """给一个 ticker 自动算出 watchlist 全字段（除用户主观字段如 conclusion / risks / status）。

    返回的 dict 字段与 stock_db.WATCHLIST_COLS 对齐；用户没传的字段也尽量填上，
    返回里有 `_enrich_meta` 子字段说明数据来源 + warnings。
    """
    code = (code or "").strip().upper()
    if not code:
        return {"_enrich_meta": {"errors": ["code is empty"]}}

    meta = {"sources": [], "warnings": []}
    out: dict[str, Any] = {"code": code}

    # 1. 市场（从 code 后缀）
    market = _infer_market(code)
    if market:
        out["market"] = market
        meta["sources"].append("market: code suffix")

    # 2. yfinance 拉公司信息
    info: dict[str, Any] = {}
    ticker = None
    yf_code = code  # 简化：直接用 code（带后缀的 yfinance 都能识别）
    try:
        import yfinance as yf
        ticker = yf.Ticker(yf_code)
        info = ticker.info or {}
        meta["sources"].append(f"yfinance:{yf_code}")
    except Exception as e:
        meta["warnings"].append(f"yfinance failed: {e}")
        info = {}

    long_name = info.get("longName") or info.get("shortName") or ""
    sector = info.get("sector") or ""
    industry = info.get("industry") or ""
    long_business = info.get("longBusinessSummary") or ""

    out["name"] = name or long_name or code
    if industry or sector:
        out["industry"] = industry or sector
    if long_business:
        # 砍到 200 字内防长
        out["business"] = long_business[:200]

    # 3. GICS 分类
    try:
        from gics_classifier import classify, score_to_label
        ai_score, theme, _sector, _industry, src = classify(code, info=info)
        out["ai_relevance"] = score_to_label(ai_score)
        out["ai_logic"] = f"GICS: {src} (score {ai_score}/3)"
        out["theme"] = theme
        meta["sources"].append(f"gics:{src}")
    except Exception as e:
        meta["warnings"].append(f"gics classify failed: {e}")

    # 4. 产业链推断（chain / chain_tier / chain_role）
    chain, chain_tier, chain_role = _infer_chain(out.get("industry", ""), out.get("theme", ""))
    if chain:
        out["chain"] = chain
    if chain_tier:
        out["chain_tier"] = chain_tier
    if chain_role:
        out["chain_role"] = chain_role

    # 5. 新手 1 句话
    out["layman_intro"] = _make_layman_intro(out.get("name", ""), out.get("industry", ""), long_business)

    # 6. 财报：watchlist.earnings（最新一句摘要）+ earnings_history（结构化全季归档）
    if ticker is not None:
        try:
            quarters = fetch_earnings_quarters(info, ticker)
            if quarters:
                out["earnings"] = _summary_from_quarter(quarters[0])
                meta["sources"].append(f"earnings: yfinance {quarters[0]['source']}")
                # 写 history 表（API 入库时立即归档全季度）
                try:
                    import stock_db  # type: ignore
                    n = stock_db.upsert_earnings_history(code, quarters)
                    if n:
                        meta["sources"].append(f"earnings_history: {n} 季 upsert")
                except Exception as e:
                    meta["warnings"].append(f"earnings_history upsert failed: {e}")
            else:
                meta["warnings"].append("earnings: yfinance 无季报 & TTM 数据")
        except Exception as e:
            meta["warnings"].append(f"earnings fetch failed: {e}")

    # 7. 元信息字段
    out["source"] = "yfinance + GICS"
    out["credibility"] = "HIGH" if info else "LOW (yfinance 拉取失败)"

    out["_enrich_meta"] = meta
    return out


if __name__ == "__main__":
    import json
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("code", help="如 NVDA / 600519.SS")
    parser.add_argument("--name", help="可选：公司名")
    args = parser.parse_args()
    result = enrich_one(args.code, args.name)
    print(json.dumps(result, ensure_ascii=False, indent=2))
