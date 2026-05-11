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

    # 6. 元信息字段
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
