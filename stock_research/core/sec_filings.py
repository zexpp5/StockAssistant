"""SEC EDGAR 10-K / 10-Q / 8-K 全文抓取（B 路线 Phase 2A）

设计原则：
  - 完全免费，遵守 SEC 速率限制（10 req/sec，已有 config.SEC_RATE_LIMIT_DELAY）
  - 输出纯文本（去 HTML、去 XBRL inline tags），便于喂给 LLM
  - 切分章节（Item 1 / 1A / 7 / 8 等），LLM 可以按需摘取，省 tokens

公开 API：
  ticker_to_cik(ticker) -> str
  list_filings(cik, form="10-K", limit=5) -> list[dict]
  fetch_filing_text(filing) -> str
  extract_sections(text) -> dict[item_id, section_text]
  get_latest_10k_sections(ticker) -> dict (一站式)
"""
from __future__ import annotations
import logging
import re
import time
from typing import Any

import requests

from .. import config

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": config.SEC_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}

_TICKER_CIK_CACHE: dict[str, str] = {}


def _get(url: str, **kwargs) -> requests.Response:
    time.sleep(config.SEC_RATE_LIMIT_DELAY)
    return requests.get(url, headers=_HEADERS, timeout=30, **kwargs)


# ────────────────────────────────────────────────────────
# Ticker → CIK
# ────────────────────────────────────────────────────────

def ticker_to_cik(ticker: str) -> str | None:
    """SEC 提供的官方 ticker→CIK 映射（每天 cache 一次，全量小文件）。"""
    if not _TICKER_CIK_CACHE:
        url = "https://www.sec.gov/files/company_tickers.json"
        r = _get(url)
        if r.status_code != 200:
            logger.warning("ticker map fetch failed: %d", r.status_code)
            return None
        for _, v in r.json().items():
            _TICKER_CIK_CACHE[v["ticker"].upper()] = str(v["cik_str"]).zfill(10)
    return _TICKER_CIK_CACHE.get(ticker.upper())


# ────────────────────────────────────────────────────────
# 列出某公司的 filings
# ────────────────────────────────────────────────────────

def list_filings(cik: str, form: str = "10-K", limit: int = 5) -> list[dict[str, Any]]:
    """列出指定 form 类型的 filings（按 filingDate 倒序）。"""
    cik_padded = str(cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    r = _get(url)
    if r.status_code != 200:
        logger.warning("submissions failed for %s: %d", cik_padded, r.status_code)
        return []
    data = r.json()
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    fdates = recent.get("filingDate", [])
    rdates = recent.get("reportDate", [])
    primary = recent.get("primaryDocument", [])

    out = []
    for i, f in enumerate(forms):
        if f != form:
            continue
        acc = accs[i]
        acc_clean = acc.replace("-", "")
        out.append({
            "form": f,
            "accession": acc,
            "filing_date": fdates[i],
            "report_date": rdates[i],
            "primary_document": primary[i] if i < len(primary) else None,
            "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{primary[i]}",
            "cik": cik_padded,
        })
        if len(out) >= limit:
            break
    return out


# ────────────────────────────────────────────────────────
# 抓全文 + 转纯文本
# ────────────────────────────────────────────────────────

def fetch_filing_text(filing: dict[str, Any]) -> str | None:
    """拉某个 filing 的主文档，转纯文本（去 HTML / inline XBRL）。"""
    url = filing.get("url")
    if not url:
        return None
    r = _get(url)
    if r.status_code != 200:
        logger.warning("filing fetch failed: %d %s", r.status_code, url)
        return None
    return _html_to_text(r.text)


def _html_to_text(html: str) -> str:
    """简洁 HTML → 文本转换（不引入 BeautifulSoup 依赖）。"""
    # 移除 script / style / 注释
    s = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    s = re.sub(r"<style[^>]*>.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    # 块级标签换行
    s = re.sub(r"<(br|/p|/div|/tr|/h[1-6]|/li)[^>]*>", "\n", s, flags=re.I)
    # 去剩余 tag
    s = re.sub(r"<[^>]+>", " ", s)
    # HTML 实体
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
    s = s.replace("&gt;", ">").replace("&quot;", '"').replace("&#160;", " ")
    s = re.sub(r"&#\d+;", " ", s)
    # 折叠空白
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ────────────────────────────────────────────────────────
# 章节切分（10-K 标准 Item 编号）
# ────────────────────────────────────────────────────────

# 10-K 标准章节（按出现顺序）
ITEM_PATTERNS_10K = [
    ("item_1", r"item\s+1\.?\s+business", "Business"),
    ("item_1a", r"item\s+1a\.?\s+risk\s+factors", "Risk Factors"),
    ("item_1b", r"item\s+1b\.?\s+unresolved\s+staff\s+comments", "Unresolved Staff Comments"),
    ("item_2", r"item\s+2\.?\s+properties", "Properties"),
    ("item_3", r"item\s+3\.?\s+legal\s+proceedings", "Legal Proceedings"),
    ("item_4", r"item\s+4\.?\s+mine\s+safety", "Mine Safety"),
    ("item_5", r"item\s+5\.?\s+market\s+for\s+registrant", "Market for Registrant's Common Equity"),
    ("item_6", r"item\s+6\.?\s+(\[?reserved\]?|selected\s+financial)", "Selected Financial Data"),
    ("item_7", r"item\s+7\.?\s+management\s*['’]?s\s+discussion", "MD&A"),
    ("item_7a", r"item\s+7a\.?\s+quantitative", "Market Risk"),
    ("item_8", r"item\s+8\.?\s+financial\s+statements", "Financial Statements"),
    ("item_9", r"item\s+9\.?\s+changes\s+in\s+and\s+disagreements", "Disagreements"),
    ("item_9a", r"item\s+9a\.?\s+controls\s+and\s+procedures", "Controls"),
    ("item_10", r"item\s+10\.?\s+directors", "Directors"),
    ("item_11", r"item\s+11\.?\s+executive\s+compensation", "Executive Compensation"),
    ("item_12", r"item\s+12\.?\s+security\s+ownership", "Security Ownership"),
    ("item_13", r"item\s+13\.?\s+certain\s+relationships", "Related Transactions"),
    ("item_14", r"item\s+14\.?\s+principal", "Accountant Fees"),
    ("item_15", r"item\s+15\.?\s+exhibit", "Exhibits"),
]


def extract_sections(text: str, patterns: list[tuple[str, str, str]] = None) -> dict[str, dict[str, Any]]:
    """按 Item 编号切分 10-K 全文。

    算法 — 反向贪心：从最后一个 Item 起倒序选位置，每个选 < 已选位置 的"最大匹配"。
    原理：正文里每个 Item 标题只出现一次且按编号顺序排，目录里所有 Item 在文档前
    密集出现。从后往前选时，自然选中正文位置（最大的 < 下一个的位置），
    跳过目录条目（位置太小）。

    返回 {item_id: {label, start, end, text, n_chars}}
    """
    if patterns is None:
        patterns = ITEM_PATTERNS_10K

    text_lower = text.lower()
    # 收集每个 item_id 的所有匹配位置
    item_matches: dict[str, list[int]] = {}
    for item_id, pat, _ in patterns:
        item_matches[item_id] = [m.start() for m in re.finditer(pat, text_lower)]

    # 反向贪心：每个 item 选 "< next_pos 的最大匹配位置"
    selected_reverse: list[tuple[int, str, str]] = []
    next_pos = len(text)
    for item_id, _, label in reversed(patterns):
        chosen = None
        for p in reversed(item_matches.get(item_id, [])):  # 从大到小
            if p < next_pos:
                chosen = p
                break
        if chosen is not None:
            selected_reverse.append((chosen, item_id, label))
            next_pos = chosen

    selected = list(reversed(selected_reverse))

    # 切分章节
    sections = {}
    for i, (start, item_id, label) in enumerate(selected):
        end = selected[i + 1][0] if i + 1 < len(selected) else len(text)
        sec_text = text[start:end].strip()
        sections[item_id] = {
            "label": label,
            "start": start,
            "end": end,
            "text": sec_text,
            "n_chars": len(sec_text),
        }
    return sections


# ────────────────────────────────────────────────────────
# 一站式：按 ticker 拿最新 10-K 章节
# ────────────────────────────────────────────────────────

def get_latest_10k_sections(ticker: str, sections_only: list[str] = None) -> dict[str, Any]:
    """按 ticker 直接拿最新 10-K 的章节切分。

    sections_only: 只保留指定 item_id 的章节（省内存）
                   推荐 ["item_1", "item_1a", "item_7"] = Business + Risk + MD&A
    """
    cik = ticker_to_cik(ticker)
    if not cik:
        return {"error": f"CIK not found for {ticker}", "ticker": ticker}

    filings = list_filings(cik, form="10-K", limit=1)
    if not filings:
        return {"error": "no 10-K filings", "ticker": ticker, "cik": cik}

    f = filings[0]
    text = fetch_filing_text(f)
    if not text:
        return {"error": "fetch failed", "ticker": ticker, "filing": f}

    sections = extract_sections(text)

    if sections_only:
        sections = {k: v for k, v in sections.items() if k in sections_only}

    return {
        "ticker": ticker,
        "cik": cik,
        "filing": f,
        "sections": sections,
        "total_chars": len(text),
        "source": "SEC EDGAR",
    }


# ────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="SEC 10-K 全文章节切分")
    parser.add_argument("ticker", help="股票代码 e.g. NVDA")
    parser.add_argument("--form", default="10-K", choices=["10-K", "10-Q", "8-K"])
    parser.add_argument("--list", action="store_true", help="只列 filings 不下载")
    parser.add_argument("--sections", nargs="+", help="只保留指定 item，如 item_1 item_1a item_7")
    parser.add_argument("--out", help="保存切分后的 JSON")
    args = parser.parse_args()

    cik = ticker_to_cik(args.ticker)
    if not cik:
        print(f"❌ CIK not found for {args.ticker}")
        return 1
    print(f"📋 {args.ticker} CIK: {cik}")

    filings = list_filings(cik, form=args.form, limit=5)
    if args.list:
        print(f"\n最近 {len(filings)} 份 {args.form}:")
        for f in filings:
            print(f"  · {f['filing_date']} (report {f['report_date']}) — {f['url']}")
        return 0

    if not filings:
        print(f"❌ No {args.form} found")
        return 1

    f = filings[0]
    print(f"📥 抓取: {f['filing_date']} — {f['url']}")
    text = fetch_filing_text(f)
    if not text:
        print("❌ fetch failed")
        return 1
    print(f"✅ 全文 {len(text):,} chars")

    sections = extract_sections(text)
    if args.sections:
        sections = {k: v for k, v in sections.items() if k in args.sections}

    print(f"\n切分到 {len(sections)} 个章节:")
    for item_id, sec in sections.items():
        preview = sec["text"][:150].replace("\n", " ")
        print(f"  · {item_id} [{sec['label']}] {sec['n_chars']:,} chars: {preview}...")

    if args.out:
        import json
        with open(args.out, "w") as fp:
            payload = {
                "ticker": args.ticker, "cik": cik, "filing": f,
                "sections": sections,
            }
            json.dump(payload, fp, indent=2, ensure_ascii=False)
        print(f"\n💾 已保存: {args.out}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
