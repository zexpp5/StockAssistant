"""SEC EDGAR full-text search PoC — 把命中的公司灌入 candidate 证据（Phase 1C）。

⚠️ 这是 PoC 不是生产级：
  - 只跑 1 个主题（uranium）作为示例
  - 只用 1 组关键词（避免 SEC 限速）
  - 只灌 status=candidate（按文档 §十二，单源 + 关键词命中 ≠ confirmed）
  - 不做 cross-validation，不做 ticker→公司主营是否真为该主题的判断

未来扩展（不在本 PoC 内）：
  - 多主题多关键词扫描
  - CIK→ticker 映射（用 SEC 官方 company_tickers.json）
  - 关键词命中频率 → confidence score
  - 同 ticker 多次命中 → 两源确认 → 升 status=confirmed

输出：往 ai_theme_company_evidence 表灌 candidate 条目
       不灌 ai_theme_company_tags（那张表是聚合结果，要先有多条 evidence）
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import get_db  # noqa: E402  # type: ignore


# SEC 要求 User-Agent 必须含 email
EDGAR_HEADERS = {
    "User-Agent": "StockAssistant Research lance7in@gmail.com",
    "Accept": "application/json",
}
EDGAR_FULLTEXT = "https://efts.sec.gov/LATEST/search-index"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# 原文 snippet 抽取参数
SNIPPET_WINDOW_CHARS = 220          # 关键词命中前后各取多少字符
MAX_FILING_BYTES = 8 * 1024 * 1024  # 单个 filing 最大下载 8MB（防爆显存）
SNIPPET_FETCH_TIMEOUT = 30.0

# 主题 → 关键词组 + form 限制
# 用 SEC 官方查询语法：双引号包关键词；多个用空格 AND
SCAN_SPECS = {
    "uranium": [
        # 精确组合: 同时提到 U3O8（铀矿主营产品代号）+ production guidance
        # 比 "uranium"+"production guidance" 召回更精，过滤掉只是 risk factors 里提铀的票
        {
            "query": '"U3O8" "production guidance"',
            "forms": "10-K",
            "evidence_kind": "filing_metric",
            "evidence_text_template": "10-K 提及 U3O8 production guidance（铀主营）",
        },
        {
            "query": '"uranium mining"',
            "forms": "10-K",
            "evidence_kind": "filing_metric",
            "evidence_text_template": "10-K 提及 uranium mining（铀矿主营）",
        },
    ],
    "smr": [
        # SMR = small modular reactor。SEC 全文搜索接受短语
        {
            "query": '"small modular reactor"',
            "forms": "10-K",
            "evidence_kind": "filing_metric",
            "evidence_text_template": "10-K 提及 small modular reactor (SMR)",
        },
        {
            "query": '"advanced reactor" "NRC"',
            "forms": "10-K",
            "evidence_kind": "filing_metric",
            "evidence_text_template": "10-K 提及 advanced reactor + NRC（监管申请）",
        },
    ],
    "rare_earths": [
        # NdPr = 钕镨，AI 磁材关键稀土；REO = rare earth oxide
        {
            "query": '"NdPr"',
            "forms": "10-K",
            "evidence_kind": "filing_metric",
            "evidence_text_template": "10-K 提及 NdPr（钕镨磁材稀土）",
        },
        {
            "query": '"rare earth" "oxide"',
            "forms": "10-K",
            "evidence_kind": "filing_metric",
            "evidence_text_template": "10-K 提及 rare earth oxide（REO 主产品）",
        },
    ],
    "liquid_cooling": [
        # AI 服务器液冷主营关键词
        {
            "query": '"direct-to-chip" "liquid cooling"',
            "forms": "10-K",
            "evidence_kind": "filing_metric",
            "evidence_text_template": "10-K 提及 direct-to-chip liquid cooling（AI 服务器液冷）",
        },
        {
            "query": '"immersion cooling"',
            "forms": "10-K",
            "evidence_kind": "filing_metric",
            "evidence_text_template": "10-K 提及 immersion cooling（浸没式液冷）",
        },
    ],
    "ai_data": [
        # AI 训练数据 / 内容授权主营关键词
        {
            "query": '"data licensing" "artificial intelligence"',
            "forms": "10-K",
            "evidence_kind": "filing_metric",
            "evidence_text_template": "10-K 提及 data licensing for AI（AI 数据授权）",
        },
        {
            "query": '"training data" "large language model"',
            "forms": "10-K",
            "evidence_kind": "filing_metric",
            "evidence_text_template": "10-K 提及 training data for LLM",
        },
    ],
}

# 2026-06-01 评审 confirmed 工程：加 8-K 事件公告扫描作为第 2 独立 source_id
# 跟 10-K 时间维度不同（10-K=年度审计后；8-K=8 工作日内事件触发）
# aggregate_tags 算独立 source 时 sec_edgar_10k + sec_edgar_8k 算 2 个 → 满足 confirmed
#
# 关键词更聚焦"事件类": 项目里程碑/合同/产能投放等公告才会在 8-K 提
SCAN_SPECS_8K = {
    "uranium": [
        {"query": '"uranium" "off-take"', "forms": "8-K",
         "evidence_kind": "contract",
         "evidence_text_template": "8-K 提及 uranium off-take 长合同"},
        {"query": '"U3O8"', "forms": "8-K",
         "evidence_kind": "project_status",
         "evidence_text_template": "8-K 提及 U3O8 产能/合同事件"},
    ],
    "smr": [
        {"query": '"small modular reactor"', "forms": "8-K",
         "evidence_kind": "project_status",
         "evidence_text_template": "8-K 提及 SMR 项目里程碑"},
        {"query": '"NRC" "construction permit"', "forms": "8-K",
         "evidence_kind": "project_status",
         "evidence_text_template": "8-K 提及 NRC 施工许可"},
    ],
    "rare_earths": [
        {"query": '"rare earth" "off-take"', "forms": "8-K",
         "evidence_kind": "contract",
         "evidence_text_template": "8-K 提及 rare earth off-take 合同"},
        {"query": '"NdPr"', "forms": "8-K",
         "evidence_kind": "project_status",
         "evidence_text_template": "8-K 提及 NdPr 产能/合同事件"},
    ],
    "liquid_cooling": [
        {"query": '"immersion cooling"', "forms": "8-K",
         "evidence_kind": "project_status",
         "evidence_text_template": "8-K 提及 immersion cooling 订单/产品"},
        {"query": '"data center" "liquid cooling"', "forms": "8-K",
         "evidence_kind": "project_status",
         "evidence_text_template": "8-K 提及 data center liquid cooling"},
    ],
    "ai_data": [
        {"query": '"data licensing" "OpenAI"', "forms": "8-K",
         "evidence_kind": "contract",
         "evidence_text_template": "8-K 提及 OpenAI 数据授权合同"},
        {"query": '"content licensing" "artificial intelligence"', "forms": "8-K",
         "evidence_kind": "contract",
         "evidence_text_template": "8-K 提及 AI 内容授权合同"},
    ],
}


def _fetch_filing_index(filing_index_url: str) -> str | None:
    """拉 filing index page 文本（HTML）。"""
    req = urllib.request.Request(filing_index_url, headers=EDGAR_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=SNIPPET_FETCH_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            return resp.read(MAX_FILING_BYTES).decode("utf-8", errors="replace")
    except Exception:
        return None


def _find_primary_document_url(index_html: str, base_url: str) -> str | None:
    """从 filing index page HTML 找 primary document（10-K 主文档）链接。

    SEC index page 顶部有 SEC 站内导航（Rules/Regulations 等 .htm），
    不能用 "第一个 .htm" 当 primary。只接受 Archives/edgar/data 路径下的 doc，
    且排除 -index.htm 自身、xbrl/_cal/_def/_lab/_pre 等 XBRL 附属文档。
    """
    if not index_html:
        return None
    import re as _re

    # 候选：href 必须含 Archives/edgar/data
    candidates: list[str] = []
    for m in _re.finditer(r'href="([^"]+\.htm[lx]?)"', index_html, _re.IGNORECASE):
        href = m.group(1)
        # 解开 inline XBRL viewer wrapper /ix?doc=/Archives/...
        if href.startswith("/ix?doc="):
            href = href[len("/ix?doc="):]
        # 必须落在 Archives/edgar/data 路径内（排除 SEC 站内导航）
        if "/Archives/edgar/data/" not in href:
            continue
        # 排除 index page 自己 / exhibit / XBRL 附属
        if _re.search(r"-index\.html?$", href, _re.IGNORECASE):
            continue
        if _re.search(r"-ex\d", href, _re.IGNORECASE):
            continue
        if _re.search(r"_(cal|def|lab|pre|R\d+)\.htm", href, _re.IGNORECASE):
            continue
        candidates.append(href)

    if not candidates:
        return None

    href = candidates[0]
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return f"https://www.sec.gov{href}"
    # 相对路径拼 base_dir
    import os as _os
    base_dir = _os.path.dirname(base_url)
    return f"{base_dir}/{href}"


def _extract_snippet(text: str, query_keywords: list[str], window: int = SNIPPET_WINDOW_CHARS) -> str | None:
    """在 filing 文本里找关键词命中，返回前后 window 字符的 snippet。

    优先按 query_keywords 顺序查找（第一个关键词最相关），找不到再退后续。
    去 HTML 标签 + 压缩 whitespace + unescape entities。
    """
    if not text:
        return None
    import re as _re
    import html as _html

    plain = _re.sub(r"<script[\s\S]*?</script>", " ", text, flags=_re.IGNORECASE)
    plain = _re.sub(r"<style[\s\S]*?</style>", " ", plain, flags=_re.IGNORECASE)
    plain = _re.sub(r"<[^>]+>", " ", plain)
    plain = _html.unescape(plain)   # &amp; / &nbsp; / &#8221; 等
    plain = _re.sub(r"\s+", " ", plain).strip()

    plain_lower = plain.lower()
    for kw in query_keywords:
        kw_l = kw.strip('"').lower()
        if not kw_l:
            continue
        idx = plain_lower.find(kw_l)
        if idx < 0:
            continue
        start = max(0, idx - window // 2)
        end = min(len(plain), idx + len(kw_l) + window // 2)
        snippet = plain[start:end]
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(plain) else ""
        return f"{prefix}{snippet}{suffix}"
    return None


def _fetch_json(url: str, params: dict | None = None, timeout: float = 15.0) -> dict | None:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=EDGAR_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                print(f"  ⚠️ HTTP {resp.status}: {url[:120]}")
                return None
            data = resp.read()
            return json.loads(data)
    except urllib.error.HTTPError as e:
        print(f"  ⚠️ HTTPError {e.code}: {url[:120]}")
        return None
    except Exception as e:
        print(f"  ⚠️ {type(e).__name__}: {e} · {url[:120]}")
        return None


def load_cik_ticker_map() -> dict[str, dict]:
    """SEC 官方 CIK → ticker / company name 映射。"""
    print(f"  拉取 SEC company_tickers.json …")
    data = _fetch_json(SEC_TICKERS_URL)
    if not data:
        return {}
    # data 是 {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    cik_map = {}
    for item in data.values():
        cik = str(item.get("cik_str") or "").zfill(10)
        if cik:
            cik_map[cik] = {
                "ticker": item.get("ticker"),
                "name": item.get("title"),
            }
    print(f"  ✅ CIK→ticker 映射 {len(cik_map)} 条")
    return cik_map


def search_filings(query: str, forms: str, max_hits: int = 20) -> list[dict]:
    """EDGAR full-text search。返回 hits[*]._source 字典列表。"""
    params = {
        "q": query,
        "forms": forms,
    }
    data = _fetch_json(EDGAR_FULLTEXT, params=params)
    if not data:
        return []
    hits = (data.get("hits") or {}).get("hits") or []
    return [h.get("_source") or {} for h in hits[:max_hits]]


def upsert_evidence_candidate(con, theme: str, ticker: str | None, cik: str,
                              company_name: str, source_url: str,
                              source_title: str, source_date: str | None,
                              evidence_kind: str, evidence_text: str,
                              accession: str | None = None,
                              expires_at=None,
                              source_id: str = "sec_edgar_api") -> None:
    """灌一条 candidate 证据。同一 (theme, cik, source_url) 重跑会替换。

    source_id 参数化（2026-06-01 ↑）：
      不同 SEC form 用不同 source_id 让 aggregate_tags 算多独立来源:
        sec_edgar_api (历史，等价 10-K)
        sec_edgar_10k (新)
        sec_edgar_8k (新)

    accession 存进 metric_json（schema 没有独立列），便于后续审计追踪。
    expires_at = source_date + 180 天（文档 §九 stale 规则）；过期标 stale。
    """
    eid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{theme}|{cik}|{source_url}"))
    metric_json = json.dumps({"accession": accession, "cik": cik, "form_source": source_id}) if accession else None
    con.execute("""
        INSERT INTO ai_theme_company_evidence
          (evidence_id, theme, market, symbol, company_name,
           evidence_status, source_id, source_tier, source_url, source_title,
           source_date, evidence_text, evidence_kind, metric_json,
           confidence_score, expires_at, reviewer_note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (evidence_id) DO UPDATE SET
          source_id = excluded.source_id,
          source_title = excluded.source_title,
          source_date = excluded.source_date,
          evidence_text = excluded.evidence_text,
          evidence_kind = excluded.evidence_kind,
          metric_json = excluded.metric_json,
          confidence_score = excluded.confidence_score,
          expires_at = excluded.expires_at,
          evidence_status = excluded.evidence_status
    """, [
        eid, theme, "US" if ticker else None, ticker, company_name,
        "candidate",  # PoC 阶段一律 candidate
        source_id, "A", source_url, source_title,
        source_date, evidence_text, evidence_kind, metric_json,
        0.5, expires_at,
        "PoC 自动灌入；尚未做两源验证或主营业务核实。原文 snippet 待 SEC 全文下载工程补完。",
    ])


def mark_stale_evidence(con) -> int:
    """把 expires_at < CURRENT_DATE 且仍是 candidate/confirmed 的标 stale。

    文档 §九：超过 180 天无新证据，降级为 stale。
    """
    r = con.execute("""
        UPDATE ai_theme_company_evidence
        SET evidence_status = 'stale'
        WHERE evidence_status IN ('candidate', 'confirmed')
          AND expires_at IS NOT NULL
          AND expires_at < CURRENT_DATE
    """)
    # DuckDB UPDATE 不返回行数，用 SELECT 后查
    return con.execute("SELECT COUNT(*) FROM ai_theme_company_evidence WHERE evidence_status = 'stale'").fetchone()[0]


def main() -> int:
    print("=" * 70)
    print("AI 主题雷达 · SEC EDGAR 证据扫描 PoC（Phase 1C）")
    print("=" * 70)

    cik_map = load_cik_ticker_map()
    if not cik_map:
        print("⚠️ 无法获取 CIK 映射，退出")
        return 1

    con = get_db()
    try:
        total_evi = 0
        # 跑两轮：10-K (source_id=sec_edgar_10k) + 8-K (source_id=sec_edgar_8k)
        # 同公司在两轮里被命中 → aggregate_tags 算 2 独立来源 → satisfies §九 confirmed
        scan_rounds = [
            ("10-K 主营披露扫描", SCAN_SPECS, "sec_edgar_10k"),
            ("8-K 事件公告扫描", SCAN_SPECS_8K, "sec_edgar_8k"),
        ]
        for round_label, round_specs, round_source_id in scan_rounds:
            print(f"\n\n{'=' * 70}\n>>> {round_label} (source_id={round_source_id})\n{'=' * 70}")
            for theme, specs in round_specs.items():
                print(f"\n=== 主题 {theme} ===")
                for spec in specs:
                    print(f"  查询: q={spec['query']!r} forms={spec['forms']}")
                    hits = search_filings(spec["query"], spec["forms"], max_hits=20)
                    print(f"  命中 {len(hits)} 条 filing")

                    # 每条 filing 映射到一家公司（去重按 CIK）
                    seen_ciks: set[str] = set()
                    for h in hits:
                        cik_raw = h.get("ciks") or h.get("cik")
                        if isinstance(cik_raw, list):
                            cik_raw = cik_raw[0] if cik_raw else None
                        if not cik_raw:
                            continue
                        cik = str(cik_raw).zfill(10)
                        if cik in seen_ciks:
                            continue
                        seen_ciks.add(cik)

                        info = cik_map.get(cik, {})
                        ticker = info.get("ticker")
                        name = info.get("name") or (h.get("display_names") or [""])[0]

                        accession_dashed = h.get("adsh") or ""
                        accession_clean = accession_dashed.replace("-", "")
                        cik_int = int(cik)
                        if accession_clean and accession_dashed:
                            filing_url = (
                                f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
                                f"{accession_clean}/{accession_dashed}-index.htm"
                            )
                        else:
                            filing_url = (
                                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
                                f"&type={spec['forms']}"
                            )

                        source_date = h.get("file_date")
                        source_title = f"{spec['forms']} — {name}" if name else spec["forms"]

                        snippet = None
                        if accession_clean and accession_dashed:
                            index_html = _fetch_filing_index(filing_url)
                            primary_url = _find_primary_document_url(index_html, filing_url) if index_html else None
                            if primary_url:
                                primary_html = _fetch_filing_index(primary_url)
                                if primary_html:
                                    kws = [k.strip('" ') for k in spec["query"].replace('"', ' ').split()]
                                    kws = [k for k in kws if k]
                                    snippet = _extract_snippet(primary_html, kws)
                                    time.sleep(0.15)
                            time.sleep(0.15)

                        if snippet:
                            evidence_text = (
                                f"[原文片段 from {spec['forms']} filed {source_date or '?'}] "
                                f"{snippet} [accession={accession_dashed}]"
                            )
                        else:
                            evidence_text = (
                                f"{spec['forms']} filed {source_date or '?'} 全文搜索匹配关键词: "
                                f"{spec['query']}. accession={accession_dashed or 'unknown'}. "
                                f"⚠️ 未能下载 filing 原文 snippet — 需打开 filing URL 人工核实。"
                            )

                        expires_at = None
                        if source_date:
                            try:
                                from datetime import date as _date, timedelta as _td
                                sd = _date.fromisoformat(source_date) if isinstance(source_date, str) else source_date
                                expires_at = sd + _td(days=180)
                            except Exception:
                                pass

                        upsert_evidence_candidate(
                            con, theme=theme,
                            ticker=ticker, cik=cik, company_name=name,
                            source_url=filing_url,
                            source_title=source_title,
                            source_date=source_date,
                            evidence_kind=spec["evidence_kind"],
                            evidence_text=evidence_text,
                            accession=accession_dashed or None,
                            expires_at=expires_at,
                            source_id=round_source_id,
                        )
                        total_evi += 1

                    time.sleep(0.2)

        print(f"\n✅ 共 upsert {total_evi} 条 candidate 证据入 ai_theme_company_evidence")

        # 文档 §九：跑 stale 规则把过期 evidence 降级
        n_stale = mark_stale_evidence(con)
        print(f"  stale 规则: 现存 stale 条目 {n_stale} 条（180 天前的 evidence 自动降级）")

        # 分布统计
        by_theme = dict(con.execute("""
            SELECT theme, COUNT(*) FROM ai_theme_company_evidence
            WHERE evidence_status='candidate'
            GROUP BY theme
        """).fetchall())
        print(f"  按主题: {by_theme}")

        # 列出 uranium 主题命中的 ticker
        rows = con.execute("""
            SELECT symbol, company_name, source_date
            FROM ai_theme_company_evidence
            WHERE theme='uranium' AND symbol IS NOT NULL
            ORDER BY source_date DESC NULLS LAST LIMIT 10
        """).fetchall()
        if rows:
            print(f"\n  uranium 主题命中 ticker (前 10 条带 ticker)：")
            for sym, name, dt in rows:
                print(f"    {sym:<8} {(name or '')[:35]:<35} {dt}")

        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
