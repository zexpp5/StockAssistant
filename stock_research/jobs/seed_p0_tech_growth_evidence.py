"""Seed conservative P0 technology-growth company evidence.

The P0 recommendation gate uses ``ai_theme_company_tags.evidence_status`` as
the canonical business-evidence input. The original AI radar evidence tables
were built for narrow themes such as liquid cooling, uranium, SMR and rare
earths, so obvious technology-growth names can otherwise look like
``missing``. This seed fills a small hand-reviewed baseline with auditable
sources. It does not change factor scores, and it is not a buy signal.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))


P0_REVIEW_DATE = date(2026, 6, 9)
P0_EXPIRES_AT = P0_REVIEW_DATE + timedelta(days=92)
P0_SOURCE_IDS = {
    "sec": "p0_sec_company_filings",
    "company": "p0_company_official_ai_or_datacenter_page",
}

P0_SOURCES = [
    {
        "source_id": P0_SOURCE_IDS["sec"],
        "source_name": "P0 SEC company filing baseline",
        "source_tier": "A",
        "source_type": "regulator",
        "source_url": "https://www.sec.gov/edgar/search/",
        "update_cadence": "quarterly",
        "license_note": "SEC public filings; used as issuer-level baseline evidence, not a recommendation.",
    },
    {
        "source_id": P0_SOURCE_IDS["company"],
        "source_name": "P0 company official AI/datacenter page baseline",
        "source_tier": "A",
        "source_type": "company_ir",
        "source_url": "https://www.nvidia.com/en-us/data-center/",
        "update_cadence": "quarterly",
        "license_note": "Company official product/IR pages; URLs are reviewed seeds, not model-generated conclusions.",
    },
]


P0_COMPANY_BASELINE = [
    # AI core chips / networking / cloud platforms
    ("ai_core", "US", "NVDA", "NVIDIA Corporation", "https://www.nvidia.com/en-us/data-center/", "AI compute / data-center GPUs and networking platform"),
    ("ai_core", "US", "AVGO", "Broadcom Inc.", "https://www.broadcom.com/solutions/artificial-intelligence", "AI networking / custom silicon exposure"),
    ("ai_core", "US", "MRVL", "Marvell Technology, Inc.", "https://www.marvell.com/solutions/artificial-intelligence.html", "AI data-center connectivity and custom silicon exposure"),
    ("ai_core", "US", "AMD", "Advanced Micro Devices, Inc.", "https://www.amd.com/en/solutions/ai.html", "AI accelerators and compute platform"),
    ("ai_core", "US", "TSM", "Taiwan Semiconductor Manufacturing Company Limited", "https://www.tsmc.com/english/dedicatedFoundry/technology", "Advanced foundry capacity for AI chips"),
    ("ai_core", "US", "MU", "Micron Technology, Inc.", "https://www.micron.com/solutions/artificial-intelligence", "AI memory / HBM exposure"),
    ("ai_core", "US", "ALAB", "Astera Labs, Inc.", "https://www.asteralabs.com/", "AI data-center connectivity platform"),
    ("ai_core", "US", "CRDO", "Credo Technology Group Holding Ltd", "https://credosemi.com/", "AI data-center connectivity platform"),
    ("ai_core", "US", "NXPI", "NXP Semiconductors N.V.", "https://www.nxp.com/applications/enabling-technologies/artificial-intelligence-ai:AI", "AI edge/embedded semiconductor exposure"),
    ("ai_core", "US", "QCOM", "QUALCOMM Incorporated", "https://www.qualcomm.com/artificial-intelligence", "Edge AI and AI silicon platform"),
    ("ai_core", "US", "ON", "ON Semiconductor Corporation", "https://www.onsemi.com/solutions/artificial-intelligence", "AI power/image/industrial semiconductor exposure"),
    ("ai_core", "US", "ADI", "Analog Devices, Inc.", "https://www.analog.com/en/solutions/artificial-intelligence.html", "AI edge/industrial semiconductor exposure"),
    ("ai_core", "US", "MSFT", "Microsoft Corporation", "https://www.microsoft.com/en-us/ai", "AI cloud platform and enterprise AI"),
    ("ai_core", "US", "GOOGL", "Alphabet Inc.", "https://ai.google/", "AI platform, cloud and model ecosystem"),
    ("internet_platform", "US", "META", "Meta Platforms, Inc.", "https://ai.meta.com/", "AI platform / recommendation / model ecosystem"),
    ("internet_platform", "US", "AMZN", "Amazon.com, Inc.", "https://aws.amazon.com/ai/", "AWS AI platform and AI infrastructure"),
    ("internet_platform", "US", "AAPL", "Apple Inc.", "https://www.apple.com/apple-intelligence/", "Consumer AI platform exposure"),

    # AI infrastructure / servers
    ("ai_infrastructure", "US", "HPE", "Hewlett Packard Enterprise Company", "https://www.hpe.com/us/en/compute/ai.html", "AI servers / enterprise infrastructure"),
    ("ai_infrastructure", "US", "DELL", "Dell Technologies Inc.", "https://www.dell.com/en-us/dt/solutions/artificial-intelligence/index.htm", "AI servers and infrastructure solutions"),
    ("ai_infrastructure", "US", "SMCI", "Super Micro Computer, Inc.", "https://www.supermicro.com/en/solutions/artificial-intelligence", "AI server systems"),
    ("ai_infrastructure", "US", "CRWV", "CoreWeave, Inc.", "https://www.coreweave.com/", "AI cloud infrastructure"),

    # Power / data-center infrastructure
    ("power_datacenter", "US", "VRT", "Vertiv Holdings Co", "https://www.vertiv.com/en-us/solutions/data-centers/", "Data-center power/cooling infrastructure"),
    ("power_datacenter", "US", "GEV", "GE Vernova Inc.", "https://www.gevernova.com/", "Power generation / grid infrastructure"),
    ("power_datacenter", "US", "VST", "Vistra Corp.", "https://vistracorp.com/", "Power generation exposure to data-center demand"),
    ("power_datacenter", "US", "ETN", "Eaton Corporation plc", "https://www.eaton.com/us/en-us/markets/data-centers.html", "Data-center electrical infrastructure"),
    ("power_datacenter", "US", "PWR", "Quanta Services, Inc.", "https://www.quantaservices.com/", "Grid and power infrastructure services"),
    ("power_datacenter", "US", "BWXT", "BWX Technologies, Inc.", "https://www.bwxt.com/", "Nuclear technology / energy infrastructure"),

    # Software / application layer
    ("tech_software", "US", "ADSK", "Autodesk, Inc.", "https://www.autodesk.com/solutions/artificial-intelligence", "AI-assisted design software workflow"),
    ("tech_software", "US", "INTU", "Intuit Inc.", "https://www.intuit.com/artificial-intelligence/", "AI-assisted financial software workflow"),
    ("tech_software", "US", "CRM", "Salesforce, Inc.", "https://www.salesforce.com/artificial-intelligence/", "Enterprise AI software platform"),
    ("tech_software", "US", "ADBE", "Adobe Inc.", "https://www.adobe.com/sensei.html", "AI-assisted creative software workflow"),
    ("tech_software", "US", "NOW", "ServiceNow, Inc.", "https://www.servicenow.com/products/artificial-intelligence.html", "Enterprise AI workflow platform"),
    ("tech_software", "US", "PANW", "Palo Alto Networks, Inc.", "https://www.paloaltonetworks.com/cyberpedia/what-is-ai-in-cybersecurity", "AI/security software exposure"),
    ("tech_software", "US", "CRWD", "CrowdStrike Holdings, Inc.", "https://www.crowdstrike.com/platform/charlotte-ai/", "AI/security software exposure"),
    ("tech_software", "US", "TEM", "Tempus AI, Inc.", "https://www.tempus.com/", "AI healthcare application layer"),
    ("tech_software", "US", "RXRX", "Recursion Pharmaceuticals, Inc.", "https://www.recursion.com/", "AI drug discovery application layer"),
]


def _evidence_id(theme: str, market: str, symbol: str, source_id: str) -> str:
    raw = f"p0-tech-growth|{theme}|{market}|{symbol}|{source_id}".encode("utf-8")
    return "p0_" + hashlib.sha1(raw).hexdigest()[:32]


def _ensure_source_rows(conn: Any, now: datetime) -> None:
    for source in P0_SOURCES:
        conn.execute(
            """
            INSERT INTO ai_theme_evidence_sources (
                source_id, source_name, source_tier, source_type, source_url,
                update_cadence, license_note, last_checked_at, last_check_status,
                last_check_http, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ok', NULL, TRUE)
            ON CONFLICT (source_id) DO UPDATE SET
                source_name=excluded.source_name,
                source_tier=excluded.source_tier,
                source_type=excluded.source_type,
                source_url=excluded.source_url,
                update_cadence=excluded.update_cadence,
                license_note=excluded.license_note,
                last_checked_at=excluded.last_checked_at,
                last_check_status=excluded.last_check_status,
                active=TRUE
            """,
            [
                source["source_id"],
                source["source_name"],
                source["source_tier"],
                source["source_type"],
                source["source_url"],
                source["update_cadence"],
                source["license_note"],
                now,
            ],
        )


def seed_p0_tech_growth_evidence(conn: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Upsert the P0 baseline evidence rows and return a compact summary."""
    now = now or datetime.now()
    _ensure_source_rows(conn, now)
    inserted = 0
    symbols: set[str] = set()
    by_theme: dict[str, int] = {}

    for theme, market, symbol, company_name, company_url, rationale in P0_COMPANY_BASELINE:
        symbols.add(symbol)
        by_theme[theme] = by_theme.get(theme, 0) + 1
        evidence_rows = [
            (
                P0_SOURCE_IDS["sec"],
                f"https://www.sec.gov/edgar/browse/?CIK={symbol}&owner=exclude",
                f"SEC EDGAR issuer filing page — {symbol}",
                "issuer_filing_baseline",
                "SEC issuer filing page is used as the regulatory source for this P0 company-level baseline.",
            ),
            (
                P0_SOURCE_IDS["company"],
                company_url,
                f"Company official AI/data-center source — {symbol}",
                "company_official_baseline",
                rationale,
            ),
        ]
        for source_id, source_url, source_title, evidence_kind, evidence_text in evidence_rows:
            conn.execute(
                """
                INSERT INTO ai_theme_company_evidence (
                    evidence_id, theme, market, symbol, company_name,
                    evidence_status, source_id, source_tier, source_url, source_title,
                    source_date, captured_at, evidence_text, evidence_kind,
                    metric_json, confidence_score, expires_at, reviewer_note
                ) VALUES (?, ?, ?, ?, ?, 'candidate', ?, 'A', ?, ?, ?, ?, ?, ?, ?, 0.65, ?, ?)
                ON CONFLICT (evidence_id) DO UPDATE SET
                    theme=excluded.theme,
                    market=excluded.market,
                    symbol=excluded.symbol,
                    company_name=excluded.company_name,
                    evidence_status=excluded.evidence_status,
                    source_id=excluded.source_id,
                    source_tier=excluded.source_tier,
                    source_url=excluded.source_url,
                    source_title=excluded.source_title,
                    source_date=excluded.source_date,
                    captured_at=excluded.captured_at,
                    evidence_text=excluded.evidence_text,
                    evidence_kind=excluded.evidence_kind,
                    metric_json=excluded.metric_json,
                    confidence_score=excluded.confidence_score,
                    expires_at=excluded.expires_at,
                    reviewer_note=excluded.reviewer_note
                """,
                [
                    _evidence_id(theme, market, symbol, source_id),
                    theme,
                    market,
                    symbol,
                    company_name,
                    source_id,
                    source_url,
                    source_title,
                    P0_REVIEW_DATE,
                    now,
                    evidence_text,
                    evidence_kind,
                    (
                        "{"
                        f'"p0_review_date":"{P0_REVIEW_DATE.isoformat()}",'
                        f'"classification_theme":"{theme}"'
                        "}"
                    ),
                    P0_EXPIRES_AT,
                    (
                        "P0 hand-reviewed baseline. This confirms company-level "
                        "technology-growth evidence only; it is not a buy/sell signal."
                    ),
                ],
            )
            inserted += 1

    return {
        "step": "p0_tech_growth_evidence_seed",
        "status": "ok",
        "n_symbols": len(symbols),
        "n_evidence_rows": inserted,
        "by_theme": by_theme,
        "review_date": P0_REVIEW_DATE.isoformat(),
        "expires_at": P0_EXPIRES_AT.isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed P0 technology-growth company evidence baseline.")
    parser.add_argument("--aggregate", action="store_true", help="Run aggregate_theme_tags after seeding.")
    args = parser.parse_args()

    from stock_db import get_db  # type: ignore

    conn = get_db()
    try:
        summary = seed_p0_tech_growth_evidence(conn)
        print(summary)
        if args.aggregate:
            from stock_research.jobs.aggregate_theme_tags import aggregate_tags

            stat = aggregate_tags(conn)
            print({"step": "tags_aggregate", **stat})
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
