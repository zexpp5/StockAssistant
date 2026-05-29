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

# 主题 → 关键词组 + form 限制
# 用 SEC 官方查询语法：双引号包关键词；多个用空格 AND
SCAN_SPECS = {
    "uranium": [
        {
            "query": '"uranium" "production guidance"',
            "forms": "10-K",
            "evidence_kind": "filing_metric",
            "evidence_text_template": "10-K 提及 uranium production guidance",
        },
    ],
    # 后续主题先不扫，避免 PoC 失控
}


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
                              expires_at=None) -> None:
    """灌一条 candidate 证据。同一 (theme, cik, source_url) 重跑会替换。

    accession 存进 metric_json（schema 没有独立列），便于后续审计追踪。
    expires_at = source_date + 180 天（文档 §九 stale 规则）；过期标 stale。
    """
    eid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{theme}|{cik}|{source_url}"))
    metric_json = json.dumps({"accession": accession, "cik": cik}) if accession else None
    con.execute("""
        INSERT INTO ai_theme_company_evidence
          (evidence_id, theme, market, symbol, company_name,
           evidence_status, source_id, source_tier, source_url, source_title,
           source_date, evidence_text, evidence_kind, metric_json,
           confidence_score, expires_at, reviewer_note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (evidence_id) DO UPDATE SET
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
        "sec_edgar_api", "A", source_url, source_title,
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
        for theme, specs in SCAN_SPECS.items():
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

                    # 构造具体 filing URL（而非公司搜索页，让用户能直接打开 10-K）
                    # accession 格式 "0001193125-26-001234"；archives 路径用去 dashes 版本
                    accession_dashed = h.get("adsh") or ""
                    accession_clean = accession_dashed.replace("-", "")
                    cik_int = int(cik)
                    if accession_clean and accession_dashed:
                        # 直接进 filing index page，里面列了所有 exhibit
                        filing_url = (
                            f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
                            f"{accession_clean}/{accession_dashed}-index.htm"
                        )
                    else:
                        # accession 缺失时退到公司 EDGAR 主页
                        filing_url = (
                            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
                            f"&type={spec['forms']}"
                        )

                    source_date = h.get("file_date")
                    source_title = f"{spec['forms']} — {name}" if name else spec["forms"]

                    # evidence_text 保留实际可复核线索：query keywords + accession
                    # 而非空模板。原文 snippet 需独立下载 filing 解析，留作后续工程。
                    evidence_text = (
                        f"{spec['forms']} filed {source_date or '?'} 全文搜索匹配关键词: "
                        f"{spec['query']}. accession={accession_dashed or 'unknown'}. "
                        f"具体段落需打开 filing 阅读核实。"
                    )

                    # 180 天 stale 规则（文档 §九.evidence_status）
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
                    )
                    total_evi += 1

                # SEC 建议 10 req/s 之内
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
