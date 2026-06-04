"""SEC EDGAR 13F fetcher — pure functions, no I/O side-effects.

权威源：SEC EDGAR (https://data.sec.gov/)。
直接解决「13F 占比 vs 变动幅度」误读陷阱。

公开 API（全部对外暴露的纯函数，未来可直接被 FastAPI 路由调用）：
  list_13f_filings(cik) -> list[dict]
  fetch_holdings(filing) -> list[dict]
  diff_holdings(latest, previous) -> list[dict]
  get_investor_changes(name, cik) -> dict   # 一站式：拉最新 + 上期 + 计算变动
  resolve_ticker(cusip, issuer) -> str | None
"""
from __future__ import annotations
import time
import logging
import re
import requests
from datetime import datetime
from typing import Any
from xml.etree import ElementTree as ET

from .. import config

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": config.SEC_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}


def _get(url: str, **kwargs) -> requests.Response:
    """带速率限制的 SEC EDGAR HTTP 请求。"""
    time.sleep(config.SEC_RATE_LIMIT_DELAY)
    return requests.get(url, headers=_HEADERS, timeout=30, **kwargs)


# ────────────────────────────────────────────────────────
# CIK 查找（按机构名模糊搜索）
# ────────────────────────────────────────────────────────

def find_cik_by_name(name: str) -> str | None:
    """按机构名调用 EDGAR 全文搜索，返回 10 位 CIK 字符串。"""
    q = requests.utils.quote(name)
    url = f"https://efts.sec.gov/LATEST/search-index?q=%22{q}%22&forms=13F-HR"
    r = _get(url)
    if r.status_code != 200:
        return None
    hits = r.json().get("hits", {}).get("hits", [])
    if not hits:
        return None
    cik = hits[0].get("_source", {}).get("ciks", [None])[0]
    return cik.zfill(10) if cik else None


# ────────────────────────────────────────────────────────
# 13F 提交记录
# ────────────────────────────────────────────────────────

def list_13f_filings(cik: str) -> list[dict[str, Any]]:
    """返回某 CIK 的所有 13F-HR 提交记录（按 report_date 倒序）。"""
    cik_padded = str(cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    r = _get(url)
    if r.status_code != 200:
        logger.warning("EDGAR submissions failed for %s: %d", cik_padded, r.status_code)
        return []
    data = r.json()
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    fdates = recent.get("filingDate", [])
    rdates = recent.get("reportDate", [])
    primary = recent.get("primaryDocument", [])
    name = data.get("name", "")

    out = []
    for i, form in enumerate(forms):
        if form in ("13F-HR", "13F-HR/A"):
            out.append({
                "form": form,
                "accession": accs[i],
                "filing_date": fdates[i],
                "report_date": rdates[i],
                "primary_doc": primary[i] if i < len(primary) else None,
                "investor_name": name,
                "cik": cik_padded,
            })
    out.sort(key=lambda x: x["report_date"], reverse=True)
    return out


# ────────────────────────────────────────────────────────
# 持仓明细解析
# ────────────────────────────────────────────────────────

def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def fetch_holdings(filing: dict[str, Any]) -> list[dict[str, Any]]:
    """从一个 13F-HR filing 拉持仓明细（解析 infotable XML）。

    返回 [{'issuer','cusip','value_thousands','shares','put_call'}].
    13F value 字段单位是「千美元」。
    """
    cik = filing["cik"].lstrip("0")
    acc = filing["accession"].replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}"

    idx = _get(f"{base}/index.json")
    if idx.status_code != 200:
        return []
    items = idx.json().get("directory", {}).get("item", []) or []

    info_xml = None
    for it in items:
        nm = it.get("name", "")
        if nm.endswith(".xml") and ("info" in nm.lower() or "table" in nm.lower()):
            info_xml = nm
            break
    if not info_xml:
        xmls = [it for it in items if it.get("name", "").endswith(".xml")]
        if not xmls:
            return []
        xmls.sort(key=lambda x: int(x.get("size") or 0), reverse=True)
        info_xml = xmls[0]["name"]

    r = _get(f"{base}/{info_xml}")
    if r.status_code != 200:
        return []

    holdings: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(r.content)
        for elem in root.iter():
            if _strip_ns(elem.tag) != "infoTable":
                continue
            d: dict[str, str] = {}
            for child in elem.iter():
                tag = _strip_ns(child.tag)
                if tag in ("nameOfIssuer", "cusip", "value", "sshPrnamt", "putCall"):
                    d[tag] = (child.text or "").strip()
            if d.get("cusip"):
                holdings.append({
                    "issuer": d.get("nameOfIssuer", ""),
                    "cusip": d.get("cusip", ""),
                    "value_thousands": int(d.get("value", "0") or 0),
                    "shares": int(d.get("sshPrnamt", "0") or 0),
                    "put_call": d.get("putCall", ""),
                })
    except Exception as e:
        logger.warning("XML parse failed for %s: %s", filing.get("accession"), e)
        return []

    return holdings


# ────────────────────────────────────────────────────────
# 变动差分
# ────────────────────────────────────────────────────────

def diff_holdings(latest: list[dict[str, Any]],
                  previous: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对两期 13F 求差，按 cusip 归并相同 issuer 的多行（call/put 区分会被合并）。"""
    def _agg(rows):
        m = {}
        for h in rows:
            k = h["cusip"]
            if k not in m:
                m[k] = {"issuer": h["issuer"], "cusip": k,
                        "value_thousands": 0, "shares": 0}
            m[k]["value_thousands"] += h.get("value_thousands", 0)
            m[k]["shares"] += h.get("shares", 0)
        return m

    prev = _agg(previous) if previous else {}
    curr = _agg(latest)

    changes = []
    for cusip, c in curr.items():
        p = prev.get(cusip)
        if not p:
            changes.append({
                "issuer": c["issuer"], "cusip": cusip,
                "action": "🆕 新建仓",
                "shares_curr": c["shares"], "shares_prev": 0,
                "shares_change_pct": None,
                "value_curr_kusd": c["value_thousands"],
            })
        else:
            ch_pct = None
            if p["shares"] > 0:
                ch_pct = (c["shares"] - p["shares"]) / p["shares"] * 100
            if c["shares"] > p["shares"]:
                action = "📈 加仓"
            elif c["shares"] < p["shares"]:
                action = "📉 减仓"
            else:
                action = "➡️ 持平"
            changes.append({
                "issuer": c["issuer"], "cusip": cusip,
                "action": action,
                "shares_curr": c["shares"], "shares_prev": p["shares"],
                "shares_change_pct": round(ch_pct, 2) if ch_pct is not None else None,
                "value_curr_kusd": c["value_thousands"],
            })

    for cusip, p in prev.items():
        if cusip not in curr:
            changes.append({
                "issuer": p["issuer"], "cusip": cusip,
                "action": "❌ 清仓",
                "shares_curr": 0, "shares_prev": p["shares"],
                "shares_change_pct": -100.0,
                "value_curr_kusd": 0,
            })
    return changes


# ────────────────────────────────────────────────────────
# 一站式 API
# ────────────────────────────────────────────────────────

def get_investor_changes(investor_name: str, cik: str) -> dict[str, Any] | None:
    """拉某机构最新 + 上期 13F 并计算变动。返回完整 snapshot dict 或 None。"""
    filings = list_13f_filings(cik)
    if not filings:
        logger.info("no 13F filings for %s", investor_name)
        return None
    latest = filings[0]
    previous = filings[1] if len(filings) >= 2 else None

    holdings_latest = fetch_holdings(latest)
    holdings_prev = fetch_holdings(previous) if previous else []
    changes = diff_holdings(holdings_latest, holdings_prev)

    return {
        "investor": investor_name,
        "cik": cik,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "latest_filing": latest,
        "previous_filing": previous,
        "holdings_count_latest": len(holdings_latest),
        "holdings_count_previous": len(holdings_prev),
        "changes": changes,
    }


# ────────────────────────────────────────────────────────
# CUSIP/issuer → ticker 解析
# ────────────────────────────────────────────────────────

def resolve_ticker(cusip: str, issuer: str) -> str | None:
    """用 CUSIP 高频映射 + issuer 关键词模糊匹配。失败返回 None。"""
    if cusip in config.CUSIP_TO_TICKER:
        return config.CUSIP_TO_TICKER[cusip]
    issuer_lc = (issuer or "").lower()
    for kw, tk in config.ISSUER_TO_TICKER_KEYWORDS:
        pattern = rf"(?<![a-z0-9]){re.escape(kw.lower())}(?![a-z0-9])"
        if re.search(pattern, issuer_lc):
            return tk
    return None


def aggregate_signals_by_ticker(snapshots: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """聚合多个机构的快照，按 ticker 归并所有信号。

    返回 {ticker: [{investor, action, shares_curr, ...}]}。
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for snap in snapshots:
        if not snap:
            continue
        latest = snap.get("latest_filing", {}) or {}
        for ch in snap.get("changes", []):
            tk = resolve_ticker(ch.get("cusip", ""), ch.get("issuer", ""))
            if not tk:
                continue
            out.setdefault(tk, []).append({
                "investor": snap["investor"],
                "report_date": latest.get("report_date"),
                "filing_date": latest.get("filing_date"),
                "cik": snap.get("cik"),
                **ch,
            })
    # 按变动绝对幅度排序（同 ticker 多机构信号时，最显著的在前）
    for tk in out:
        out[tk].sort(key=lambda c: abs(c.get("shares_change_pct") or 0), reverse=True)
    return out


def format_signal_text(signals: list[dict[str, Any]], max_lines: int = 8) -> str:
    """把某只股票的多机构信号格式化成人类可读文本（用于飞书字段）。"""
    if not signals:
        return ""
    lines = []
    for s in signals[:max_lines]:
        pct = ""
        if s.get("shares_change_pct") is not None:
            pct = f" ({s['shares_change_pct']:+.1f}%)"
        cik_str = f"CIK {s['cik']}" if s.get('cik') else ""
        lines.append(
            f"{s['action']} · {s['investor']} · "
            f"{s['shares_prev']:,}→{s['shares_curr']:,} 股{pct} · "
            f"{s.get('report_date') or '?'} 数据 / 公布于 {s.get('filing_date') or '?'} · "
            f"SEC 13F-HR · {cik_str}"
        )
    lines.append(f"\n📊 数据源：SEC EDGAR 13F-HR · 抓取于 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    return "\n".join(lines)
