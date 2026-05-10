"""产业链上下游分析（B 路线 Phase 4 — 70% 版）

为什么是 70%：
  机构级产业链图谱（FactSet RBICS / Bloomberg SPLC）是付费的，几万美元/年。
  本模块通过 3 条免费 + 半付费路径达成 70% 覆盖：

  路径 A: 10-K Item 1 / Item 1A 文本 → Claude LLM 提取客户/供应商
          覆盖率 60-70%（美股质量好；A 股 10-K 等价物在巨潮，需要爬虫）
  路径 B: Finnhub 新闻 → 关键词筛"long-term agreement / supply / partnership"
          覆盖率 +20%
  路径 C: 电话会议 transcripts → 分析师/管理层交叉提及（FMP Premium）
          覆盖率 +5-10%

输出：
  {
    "ticker": "NVDA",
    "customers": [{"name": "Microsoft", "evidence": "10-K Item 1: Top 10% revenue", "source": "..."}],
    "suppliers": [{"name": "TSMC", "evidence": "...", ...}],
    "partners": [...]
  }

CLI:
  python3 -m stock_research.core.supply_chain NVDA
  python3 -m stock_research.core.supply_chain NVDA --no-llm  # 仅新闻聚合
"""
from __future__ import annotations
import json
import logging
import re
from typing import Any

from . import claude_client, finnhub_client, sec_filings

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────
# 路径 A: LLM 从 10-K 提取客户/供应商
# ────────────────────────────────────────────────────────

LLM_EXTRACT_PROMPT = """你是一位资深财务分析师。从以下 SEC 10-K 文档章节中，提取明确披露的：
1. **Major Customers（重要客户）**：占营收 ≥10% 必须披露的，或文中明确提到的关键大客户
2. **Major Suppliers（重要供应商）**：晶圆代工、关键零部件、IP 授权、独家原材料等
3. **Major Partners（战略合作）**：联合开发、长期协议、独家分销等

# 输出格式（严格 JSON，不要其它内容）
```json
{
  "customers": [
    {"name": "客户名", "evidence": "原文片段（中英都行，限 200 字）", "concentration_pct": null}
  ],
  "suppliers": [
    {"name": "供应商名", "evidence": "原文片段", "category": "wafer/component/IP/other"}
  ],
  "partners": [
    {"name": "合作方名", "evidence": "原文片段", "type": "joint_dev/exclusive/licensing/other"}
  ]
}
```

# 严格规则
- 只输出文档明确披露的，不能推测/外补
- evidence 必须是原文片段（不要重新表述）
- 找不到时该 list 留空 []
- name 用规范公司名（Microsoft 而非 MS）
- concentration_pct 仅当文档明确给出占比（如 "Customer A accounted for 13%"）才填
"""


def extract_from_10k_with_llm(ticker: str,
                               sec_data: dict[str, Any] | None = None,
                               max_text_per_section: int = 25000) -> dict[str, Any]:
    """用 Claude 从 10-K 关键章节提取客户/供应商/合作方。"""
    if sec_data is None:
        sec_data = sec_filings.get_latest_10k_sections(
            ticker, sections_only=["item_1", "item_1a", "item_7"])

    if sec_data.get("error"):
        return {"error": sec_data["error"], "ticker": ticker}

    if not claude_client.is_available():
        return {"error": "ANTHROPIC_API_KEY not set", "ticker": ticker}

    secs = sec_data.get("sections") or {}
    parts = []
    for key, label in [("item_1", "## Item 1. Business"),
                       ("item_1a", "## Item 1A. Risk Factors"),
                       ("item_7", "## Item 7. MD&A")]:
        s = secs.get(key)
        if s and s.get("text"):
            parts.append(label)
            parts.append(s["text"][:max_text_per_section])
            parts.append("")

    if not parts:
        return {"error": "no usable 10-K sections", "ticker": ticker}

    user_content = "\n".join(parts) + f"\n\n# 任务：提取 {ticker} 的客户/供应商/合作方"

    client = claude_client.ChatClient(max_tokens=2000, temperature=0.0)
    resp = client.complete(system=LLM_EXTRACT_PROMPT, user=user_content,
                           cache_system=True)
    if not resp:
        return {"error": "Claude API call failed", "ticker": ticker}

    text = resp.get("text", "")
    # 提取 JSON（容错：可能被包在 ```json 里）
    parsed = _extract_json(text)

    return {
        "ticker": ticker,
        "method": "10-K + Claude extraction",
        "filing": sec_data.get("filing"),
        "result": parsed if parsed else {"raw_text": text, "parse_error": True},
        "usage": resp.get("usage"),
        "cost_usd": round(claude_client.estimate_cost(
            resp["usage"].get("input_tokens", 0),
            resp["usage"].get("output_tokens", 0),
            cache_write_tokens=resp["usage"].get("cache_creation_input_tokens", 0),
            cache_read_tokens=resp["usage"].get("cache_read_input_tokens", 0),
        ), 4),
    }


def _extract_json(text: str) -> dict | None:
    """容错抽 JSON：可能包在 ```json ``` 里或裸 JSON。"""
    # ```json ... ```
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 裸首个 { ... }
    m = re.search(r"(\{[\s\S]*\})", text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return None


# ────────────────────────────────────────────────────────
# 路径 B: 新闻关键词聚合
# ────────────────────────────────────────────────────────

PARTNERSHIP_KEYWORDS = [
    "long-term agreement", "long term agreement", "long-term contract",
    "supply agreement", "supply contract", "strategic partnership",
    "exclusive agreement", "multi-year deal", "multi year deal",
    "expand collaboration", "extend partnership", "joint development",
    "preferred supplier", "key customer", "anchor customer",
]


def fetch_partnership_news(ticker: str, days: int = 365) -> dict[str, Any]:
    """从 Finnhub 新闻找合作公告关键词。"""
    if not finnhub_client.is_available():
        return {"error": "FINNHUB_API_KEY not set", "ticker": ticker}

    news = finnhub_client.fetch_company_news(ticker, days=days)
    if not news:
        return {"error": "no news", "ticker": ticker, "n_news": 0}

    matches = []
    for n in news:
        text = ((n.get("headline") or "") + " " + (n.get("summary") or "")).lower()
        hit_kws = [kw for kw in PARTNERSHIP_KEYWORDS if kw in text]
        if hit_kws:
            matches.append({
                "date": n.get("date") or n.get("datetime"),
                "headline": n.get("headline"),
                "summary": (n.get("summary") or "")[:300],
                "keywords": hit_kws,
                "url": n.get("url"),
            })

    return {
        "ticker": ticker,
        "method": "Finnhub news + keyword filter",
        "n_news_total": len(news),
        "n_partnerships": len(matches),
        "partnerships": matches[:30],
        "source": "Finnhub /company-news",
    }


# ────────────────────────────────────────────────────────
# 整合：build supply chain (LLM + 新闻)
# ────────────────────────────────────────────────────────

def build(ticker: str, use_llm: bool = True,
          news_days: int = 365) -> dict[str, Any]:
    """端到端构建产业链图谱（70% 版）。"""
    out: dict[str, Any] = {"ticker": ticker, "sources": {}}

    if use_llm:
        llm_result = extract_from_10k_with_llm(ticker)
        out["sources"]["10k_llm"] = llm_result

    news_result = fetch_partnership_news(ticker, days=news_days)
    out["sources"]["news"] = news_result

    # 合并 customers / suppliers / partners 去重
    merged = {"customers": {}, "suppliers": {}, "partners": {}}
    if use_llm and not (out["sources"]["10k_llm"].get("error")):
        r = (out["sources"]["10k_llm"].get("result") or {})
        for cat in ["customers", "suppliers", "partners"]:
            for entity in r.get(cat, []) or []:
                name = (entity.get("name") or "").strip()
                if name:
                    if name not in merged[cat]:
                        merged[cat][name] = {"name": name, "evidence_sources": []}
                    merged[cat][name]["evidence_sources"].append({
                        "type": "10-K",
                        "evidence": entity.get("evidence"),
                        **{k: v for k, v in entity.items()
                           if k not in ("name", "evidence")},
                    })

    out["graph"] = {
        "customers": list(merged["customers"].values()),
        "suppliers": list(merged["suppliers"].values()),
        "partners": list(merged["partners"].values()),
    }
    out["counts"] = {
        "customers": len(merged["customers"]),
        "suppliers": len(merged["suppliers"]),
        "partners": len(merged["partners"]),
        "news_partnerships": len((news_result.get("partnerships") or [])
                                  if not news_result.get("error") else []),
    }
    out["coverage_estimate"] = "70% (10-K LLM + news; 缺产业链可视化付费源)"
    return out


# ────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="产业链上下游 70% 版")
    parser.add_argument("ticker")
    parser.add_argument("--no-llm", action="store_true", help="跳过 10-K LLM 提取")
    parser.add_argument("--news-days", type=int, default=365)
    parser.add_argument("--out")
    args = parser.parse_args()

    r = build(args.ticker, use_llm=not args.no_llm, news_days=args.news_days)
    print(json.dumps(r, indent=2, ensure_ascii=False))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(r, f, indent=2, ensure_ascii=False)
        print(f"\n💾 已保存: {args.out}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
