#!/usr/bin/env python3
"""Enrich the v2 system tech universe without touching manual watchlist.

This job reads `system_universe`, fetches live/source data, and writes:
- `source_raw_snapshots`: latest per-symbol detail payload for dashboard/API.
- `financial_statements`: structured latest quarterly earnings rows.

It intentionally does not read old DuckDB backups or write `watchlist`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import DB_PATH  # noqa: E402

try:  # noqa: E402
    from stock_research.jobs.enrich_watchlist import (
        _format_for_feishu as _format_enrichment_fields,
        enrich_one as _fetch_enrichment,
    )
except (ModuleNotFoundError, ImportError):  # V2 cutover: legacy watchlist job may be removed.
    from stock_research.core import akshare_client, baostock_client, finnhub_client, trends  # noqa: E402
    from stock_research.core.watchlist_enrich import (  # noqa: E402
        fetch_earnings_quarters,
        fetch_earnings_summary,
    )

    def _is_us_stock(market: str, code: str) -> bool:
        if "美股" in market or str(market).upper() == "US":
            return True
        return bool(code) and code.replace("-", "").replace(".", "").isalpha()

    def _fetch_enrichment(
        name: str,
        code: str,
        market: str,
        do_trends: bool = True,
        do_finnhub: bool = True,
        do_akshare: bool = True,
        do_baostock: bool = True,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": name,
            "code": code,
            "market": market,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "sources_used": [],
        }
        if _is_us_stock(market, code):
            if do_finnhub and finnhub_client.is_available():
                fh = finnhub_client.fetch_enriched(code)
                if fh and fh.get("finnhub"):
                    out["finnhub"] = fh["finnhub"]
                    out["sources_used"].append("finnhub")
            if do_trends:
                keyword = name if any("\u4e00" <= c <= "\u9fff" for c in name) else code
                tr = trends.fetch_trend(keyword, geo="")
                if tr:
                    out["trends"] = tr
                    out["sources_used"].append("trends")
        else:
            if do_akshare:
                ak = akshare_client.fetch_enriched(code, market)
                if ak.get("akshare"):
                    out["akshare"] = ak["akshare"]
                    out["sources_used"].append("akshare")
            if do_baostock and "A股" in market:
                bs_quote = baostock_client.fetch_a_share_quote(code)
                if bs_quote:
                    out["baostock"] = bs_quote
                    out["sources_used"].append("baostock")
            if do_trends:
                tr = trends.fetch_trend(name, geo="CN" if "A股" in market else "")
                if tr:
                    out["trends"] = tr
                    out["sources_used"].append("trends")

        try:
            import yfinance as yf

            ticker_obj = yf.Ticker(code)
            info_yf = ticker_obj.info or {}
            quarters = fetch_earnings_quarters(info_yf, ticker_obj)
            if quarters:
                out["earnings_summary"] = fetch_earnings_summary(info_yf, ticker_obj)
                out["earnings_quarters"] = quarters
                out["sources_used"].append(f"yfinance_earnings({len(quarters)}q)")
        except Exception:
            pass
        return out

    def _format_enrichment_fields(enriched: dict[str, Any]) -> dict[str, str]:
        lines: list[str] = []
        sources: list[str] = []
        finn = enriched.get("finnhub") or {}
        if finn.get("insider"):
            ins = finn["insider"]
            lines.append(
                f"内部人交易（90天）: 共 {ins['count']} 笔，买 {ins['buy_count']} / "
                f"卖 {ins['sell_count']}，净 {ins['net_shares']:,} 股 [Finnhub]"
            )
            sources.append("Finnhub stock_insider_transactions")
        if finn.get("analyst_recommendations"):
            rec = finn["analyst_recommendations"]
            lines.append(
                f"分析师评级（{rec.get('period')}）: 强买 {rec['strong_buy']} / 买 {rec['buy']} / "
                f"持有 {rec['hold']} / 卖 {rec['sell']} / 强卖 {rec['strong_sell']} [Finnhub]"
            )
            sources.append("Finnhub recommendation_trends")
        if finn.get("price_target"):
            pt = finn["price_target"]
            lines.append(
                f"分析师目标价: 中位 ${pt.get('target_median')} / 均值 ${pt.get('target_mean')} / "
                f"区间 ${pt.get('target_low')} - ${pt.get('target_high')} [Finnhub]"
            )
            sources.append("Finnhub price_target")

        ak = enriched.get("akshare") or {}
        if ak.get("quote"):
            q = ak["quote"]
            lines.append(
                f"akshare 实时: {q.get('name','')} 价 {q.get('price')} / "
                f"涨幅 {q.get('change_pct')}% / PE {q.get('pe_ttm')} / PB {q.get('pb')} [akshare]"
            )
            sources.append(q.get("source", "akshare"))
        if enriched.get("trends"):
            tr = enriched["trends"]
            lines.append(
                f"Google Trends: 平均 {tr.get('avg')} / 最近 {tr.get('last')} / "
                f"趋势 {tr.get('trend_pct'):+.1f}% [Google Trends]"
            )
            sources.append("Google Trends")
        if enriched.get("earnings_summary"):
            lines.append(f"{enriched['earnings_summary'].split(chr(10))[0]} [yfinance]")
            sources.append("yfinance earnings")

        if not lines:
            return {"earnings": enriched["earnings_summary"]} if enriched.get("earnings_summary") else {}
        return {
            "info_breakdown": "\n".join(lines) + f"\n\n多源同步：{enriched.get('fetched_at')}",
            "source": "\n".join(f"· {s}" for s in dict.fromkeys(sources)),
            **({"earnings": enriched["earnings_summary"]} if enriched.get("earnings_summary") else {}),
        }


MARKET_LABEL = {"US": "美股", "HK": "港股", "CN": "A股"}


class EnrichmentTimeout(RuntimeError):
    pass


def _with_timeout(timeout_sec: float, fn, *args, **kwargs):
    if timeout_sec <= 0 or not hasattr(signal, "SIGALRM"):
        return fn(*args, **kwargs)

    def _handle_timeout(_signum, _frame):
        raise EnrichmentTimeout(f"external enrichment timed out after {timeout_sec:.1f}s")

    old_handler = signal.signal(signal.SIGALRM, _handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_sec)
    try:
        return fn(*args, **kwargs)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def _tables(conn: duckdb.DuckDBPyConnection) -> set[str]:
    return {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def _content_hash(payload: Any) -> str:
    return hashlib.sha1(_json_dumps(payload).encode("utf-8")).hexdigest()


def _parse_period_end(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _fiscal_quarter(period_end: date | None) -> tuple[int, str]:
    if period_end is None:
        today = date.today()
        return today.year, "TTM"
    q = ((period_end.month - 1) // 3) + 1
    return period_end.year, f"Q{q}"


def _load_targets(
    conn: duckdb.DuckDBPyConnection,
    *,
    symbols: set[str] | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    query = """
        SELECT pool_id, pool_name, market, symbol, raw_symbol, name, theme, industry, source
        FROM system_universe
        WHERE active = TRUE
        ORDER BY market, symbol
    """
    rows = conn.execute(query).fetchall()
    out: list[dict[str, Any]] = []
    for pool_id, pool_name, market, symbol, raw_symbol, name, theme, industry, source in rows:
        symbol_s = str(symbol or "").strip().upper()
        if not symbol_s:
            continue
        if symbols and symbol_s not in symbols and str(raw_symbol or "").strip().upper() not in symbols:
            continue
        market_s = str(market or "").upper()
        out.append(
            {
                "pool_id": pool_id,
                "pool_name": pool_name,
                "market": market_s,
                "market_label": MARKET_LABEL.get(market_s, market_s),
                "symbol": symbol_s,
                "raw_symbol": raw_symbol,
                "name": name or symbol_s,
                "theme": theme or "",
                "industry": industry or "",
                "source": source or pool_id or "system_tech_universe",
            }
        )
        if limit and len(out) >= limit:
            break
    return out


def _latest_recommendation(
    conn: duckdb.DuckDBPyConnection, market: str, symbol: str
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT rp.run_id, rr.generated_at, rp.rank, rp.rating, rp.signal,
               rp.total_score, rp.factor_scores_json, rp.recommendation_reason,
               rp.risk_flags_json, rp.entry_price, rp.entry_currency
        FROM recommendation_picks rp
        JOIN recommendation_runs rr ON rr.run_id = rp.run_id
        WHERE rp.market = ? AND rp.symbol = ?
        ORDER BY rr.generated_at DESC
        LIMIT 1
        """,
        [market, symbol],
    ).fetchall()
    if not rows:
        return {}
    (
        run_id,
        generated_at,
        rank,
        rating,
        signal,
        total_score,
        factor_scores_json,
        recommendation_reason,
        risk_flags_json,
        entry_price,
        entry_currency,
    ) = rows[0]
    factors = {}
    risks = []
    try:
        factors = json.loads(factor_scores_json) if factor_scores_json else {}
    except Exception:
        factors = {}
    try:
        risks = json.loads(risk_flags_json) if risk_flags_json else []
    except Exception:
        risks = []
    return {
        "run_id": run_id,
        "generated_at": str(generated_at) if generated_at else None,
        "rank": rank,
        "rating": rating,
        "signal": signal,
        "total_score": total_score,
        "factor_scores": factors,
        "recommendation_reason": recommendation_reason,
        "risk_flags": risks,
        "entry_price": entry_price,
        "entry_currency": entry_currency,
    }


def _analysis_text(target: dict[str, Any], recommendation: dict[str, Any]) -> tuple[str, str]:
    if not recommendation:
        conclusion = (
            f"{target['name']} 属于 V2 系统科技/AI 股票池，当前尚无最新 recommendation_picks 记录；"
            "只能作为已拉取标的观察。"
        )
        risks = "暂无 V2 推荐风险 flags；需先生成 recommendation_picks 后再评估。"
        return conclusion, risks

    reason = recommendation.get("recommendation_reason") or ""
    signal = recommendation.get("signal") or "watch"
    score = recommendation.get("total_score")
    rating = recommendation.get("rating") or signal
    conclusion = (
        f"V2 推荐信号：{signal} / {rating}"
        + (f"，总分 {float(score):.1f}" if isinstance(score, (int, float)) else "")
        + (f"。核心依据：{reason}" if reason else "。")
    )
    risk_flags = recommendation.get("risk_flags") or []
    if risk_flags:
        risks = "\n".join(f"- {flag}" for flag in risk_flags)
    else:
        risks = "V2 当前未命中结构化风险 flags；仍需结合估值、财报、流动性和买前审查复核。"
    return conclusion, risks


def _fallback_info_breakdown(
    target: dict[str, Any],
    fields: dict[str, str],
    recommendation: dict[str, Any],
    fetched_at: datetime,
) -> str:
    existing = fields.get("info_breakdown")
    if existing:
        return existing
    lines = [
        f"🏷 系统池元数据：{target.get('theme') or '未标主题'} / {target.get('industry') or '未标行业'} "
        f"[system_universe · {target.get('source') or target.get('pool_id')}]"
    ]
    if fields.get("earnings"):
        first_line = fields["earnings"].split("\n")[0]
        earnings_source = "V2 fallback" if first_line.startswith("暂无可用财报源返回") else "yfinance"
        lines.append(f"💰 {first_line} [{earnings_source}]")
    if recommendation:
        reason = recommendation.get("recommendation_reason") or "暂无推荐理由文本"
        score = recommendation.get("total_score")
        signal = recommendation.get("signal") or "watch"
        score_text = f"，总分 {float(score):.1f}" if isinstance(score, (int, float)) else ""
        lines.append(f"🤖 V2 推荐：{signal}{score_text}；{reason} [recommendation_picks]")
    else:
        lines.append("🤖 V2 推荐：暂无 recommendation_picks 记录 [recommendation_picks]")
    lines.append(f"\n⏰ V2 enrichment：{fetched_at.isoformat(timespec='seconds')}")
    return "\n".join(lines)


def _fallback_earnings_status(
    target: dict[str, Any],
    enriched: dict[str, Any],
    enrich_error: str,
    fetched_at: datetime,
) -> str:
    sources = ", ".join(str(s) for s in enriched.get("sources_used") or [])
    if not sources:
        sources = "yfinance / Finnhub / market enrichment"
    reason = (
        f"外部源未在超时内返回：{enrich_error}"
        if enrich_error
        else "外部财报源本次未返回结构化季度财报"
    )
    return (
        "暂无可用财报源返回（V2 已记录源状态）。\n"
        f"• 标的：{target['name']} ({target['symbol']})\n"
        f"• 已尝试：{sources}\n"
        f"• 原因：{reason}\n"
        "• 处理：不使用旧库回填；下一次 V2 enrichment 会自动重试。"
        f"\n⏰ {fetched_at.isoformat(timespec='seconds')}"
    )


def _fallback_source_text(enriched: dict[str, Any], enrich_error: str) -> str:
    sources = list(dict.fromkeys(str(s) for s in enriched.get("sources_used") or [] if s))
    if sources:
        return "\n".join(f"· {s}" for s in sources)
    if enrich_error:
        return f"· V2 fallback：external enrichment timeout/error ({enrich_error})"
    return "· V2 fallback：external source returned no structured detail"


def _upsert_financial_statements(
    conn: duckdb.DuckDBPyConnection,
    target: dict[str, Any],
    quarters: list[dict[str, Any]],
    fetched_at: datetime,
) -> int:
    n = 0
    for q in quarters:
        period_end = _parse_period_end(q.get("fiscal_period"))
        fiscal_year, fiscal_quarter = _fiscal_quarter(period_end)
        payload_json = _json_dumps(q)
        source = str(q.get("source") or "yfinance")
        conn.execute(
            """
            INSERT INTO financial_statements (
                market, symbol, fiscal_year, fiscal_quarter, period_end_date,
                statement_type, source, reported_at, source_updated_at,
                fetched_at, content_hash, payload_json, is_current
            )
            VALUES (?, ?, ?, ?, ?, 'income_statement', ?, ?, ?, ?, ?, ?, TRUE)
            ON CONFLICT (market, symbol, fiscal_year, fiscal_quarter, statement_type, source)
            DO UPDATE SET
                period_end_date=excluded.period_end_date,
                source_updated_at=excluded.source_updated_at,
                fetched_at=excluded.fetched_at,
                content_hash=excluded.content_hash,
                payload_json=excluded.payload_json,
                is_current=TRUE
            """,
            [
                target["market"],
                target["symbol"],
                fiscal_year,
                fiscal_quarter,
                period_end,
                source,
                None,
                fetched_at,
                fetched_at,
                _content_hash(q),
                payload_json,
            ],
        )
        n += 1
    return n


def _upsert_snapshot(
    conn: duckdb.DuckDBPyConnection,
    target: dict[str, Any],
    payload: dict[str, Any],
    fetched_at: datetime,
) -> None:
    business_date = fetched_at.date()
    snapshot_key = f"v2_system_enrichment:{target['market']}:{target['symbol']}:{business_date.isoformat()}"
    snapshot_id = hashlib.sha1(snapshot_key.encode("utf-8")).hexdigest()
    conn.execute(
        """
        INSERT INTO source_raw_snapshots (
            snapshot_id, source, market, business_date, source_updated_at,
            fetched_at, payload_json
        )
        VALUES (?, 'v2_system_enrichment', ?, ?, ?, ?, ?)
        ON CONFLICT (snapshot_id) DO UPDATE SET
            source_updated_at=excluded.source_updated_at,
            fetched_at=excluded.fetched_at,
            payload_json=excluded.payload_json
        """,
        [
            snapshot_id,
            target["market"],
            business_date,
            fetched_at,
            fetched_at,
            _json_dumps(payload),
        ],
    )


def _record_fetch_log(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    fetched_at: datetime,
    *,
    fetched: int,
    failed: int,
    degraded: int,
    financial_rows: int,
) -> None:
    if failed == 0 and degraded == 0:
        status = "success"
        status_code = "ok"
        fallback_source = None
    elif fetched > 0:
        status = "source_degraded"
        status_code = "partial"
        fallback_source = "v2_fallback"
    else:
        status = "error"
        status_code = "error"
        fallback_source = "v2_fallback"
    conn.execute(
        """
        INSERT INTO source_fetch_log (
            run_id, source, market, status, status_code, fallback_source,
            fetched_at, message
        )
        VALUES (?, 'v2_system_enrichment', 'ALL', ?, ?, ?, ?, ?)
        """,
        [
            run_id,
            status,
            status_code,
            fallback_source,
            fetched_at,
            (
                f"写入 V2 enrichment {fetched} 条，降级 {degraded} 条，"
                f"失败 {failed} 条，financial_statements {financial_rows} 行"
            ),
        ],
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db or DB_PATH).expanduser().resolve()
    conn = duckdb.connect(str(db_path))
    tables = _tables(conn)
    required = {"system_universe", "source_raw_snapshots", "financial_statements", "recommendation_picks"}
    missing = required - tables
    if missing:
        conn.close()
        raise RuntimeError(f"当前 DB 不是完整 v2 schema，缺少: {', '.join(sorted(missing))}")

    symbols = None
    if args.symbols:
        symbols = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
    targets = _load_targets(conn, symbols=symbols, limit=args.limit)
    if args.missing_only:
        existing = {
            str(r[0] or "").strip().upper()
            for r in conn.execute(
                """
                SELECT DISTINCT json_extract_string(payload_json, '$.symbol')
                FROM source_raw_snapshots
                WHERE source = 'v2_system_enrichment'
                  AND business_date = CURRENT_DATE
                """
            ).fetchall()
        }
        targets = [t for t in targets if t["symbol"] not in existing]
    run_id = f"v2_enrichment_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    fetched = 0
    failed = 0
    degraded = 0
    financial_rows = 0
    failures: list[dict[str, str]] = []

    for target in targets:
        print(f"  → {target['symbol']} {target['name']} ({target['market']})", flush=True)
        try:
            enrich_error = ""
            try:
                enriched = _with_timeout(
                    args.per_symbol_timeout_sec,
                    _fetch_enrichment,
                    target["name"],
                    target["symbol"],
                    target["market_label"],
                    do_trends=not args.skip_trends,
                    do_finnhub=not args.skip_finnhub,
                    do_akshare=not args.skip_akshare,
                    do_baostock=not args.skip_baostock,
                )
            except Exception as exc:
                degraded += 1
                enrich_error = str(exc)
                enriched = {
                    "name": target["name"],
                    "code": target["symbol"],
                    "market": target["market_label"],
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                    "sources_used": [],
                }
            fields = _format_enrichment_fields(enriched)
            recommendation = _latest_recommendation(conn, target["market"], target["symbol"])
            conclusion, risks = _analysis_text(target, recommendation)
            fetched_at = datetime.now()
            earnings = (
                fields.get("earnings")
                or enriched.get("earnings_summary")
                or _fallback_earnings_status(target, enriched, enrich_error, fetched_at)
            )
            fields = {**fields, "earnings": earnings}
            info_breakdown = _fallback_info_breakdown(target, fields, recommendation, fetched_at)
            payload = {
                "schema_version": "v2_system_enrichment_v1",
                "run_id": run_id,
                "fetched_at": fetched_at.isoformat(timespec="seconds"),
                "pool_id": target["pool_id"],
                "market": target["market"],
                "symbol": target["symbol"],
                "raw_symbol": target.get("raw_symbol"),
                "name": target["name"],
                "theme": target.get("theme") or "",
                "industry": target.get("industry") or "",
                "pool_source": target.get("source") or "",
                "sources_used": enriched.get("sources_used") or [],
                "earnings": earnings,
                "info_breakdown": info_breakdown,
                "source_text": fields.get("source") or _fallback_source_text(enriched, enrich_error),
                "conclusion": conclusion,
                "risks": risks,
                "notes": "",
                "external_source_error": enrich_error,
                "recommendation": recommendation,
            }
            _upsert_snapshot(conn, target, payload, fetched_at)
            quarters = enriched.get("earnings_quarters") or []
            financial_rows += _upsert_financial_statements(conn, target, quarters, fetched_at)
            fetched += 1
            source_label = ",".join(payload["sources_used"]) or ("v2_fallback" if enrich_error else "none")
            print(f"     ✓ sources={source_label}", flush=True)
        except Exception as exc:
            failed += 1
            failures.append({"symbol": target["symbol"], "error": str(exc)})
            print(f"     ✗ {exc}", flush=True)
        if args.sleep_sec > 0:
            time.sleep(args.sleep_sec)

    _record_fetch_log(
        conn,
        run_id,
        datetime.now(),
        fetched=fetched,
        failed=failed,
        degraded=degraded,
        financial_rows=financial_rows,
    )
    conn.close()
    return {
        "run_id": run_id,
        "db_path": str(db_path),
        "target_count": len(targets),
        "fetched": fetched,
        "failed": failed,
        "degraded": degraded,
        "financial_statement_rows": financial_rows,
        "failures": failures[:20],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch/analyze v2 system universe details.")
    parser.add_argument("--db", default=str(DB_PATH), help="DuckDB path.")
    parser.add_argument("--symbols", help="Comma-separated symbols, e.g. NVDA,TSM,300308.SZ")
    parser.add_argument("--limit", type=int, help="Limit rows for smoke tests.")
    parser.add_argument("--missing-only", action="store_true", help="Only enrich active system_universe symbols without today's v2 snapshot.")
    parser.add_argument("--skip-trends", action="store_true", help="Skip Google Trends.")
    parser.add_argument("--skip-finnhub", action="store_true", help="Skip Finnhub US enrichment.")
    parser.add_argument("--skip-akshare", action="store_true", help="Skip akshare CN/HK enrichment.")
    parser.add_argument("--skip-baostock", action="store_true", help="Skip baostock A-share quote.")
    parser.add_argument("--sleep-sec", type=float, default=0.1, help="Sleep between symbols.")
    parser.add_argument("--per-symbol-timeout-sec", type=float, default=15.0, help="Timeout for external per-symbol enrichment; V2 fallback is still written.")
    parser.add_argument("--json", action="store_true", help="Print JSON summary.")
    args = parser.parse_args()
    summary = run(args)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            f"\nV2 enrichment: fetched={summary['fetched']}/{summary['target_count']} "
            f"failed={summary['failed']} degraded={summary['degraded']} "
            f"financial_rows={summary['financial_statement_rows']}"
        )
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
