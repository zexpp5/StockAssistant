"""US production universe for AI/tech stock selection.

This file is deliberately static and boring.  The US production picker should
not disappear when the user clears the manually maintained watchlist, so we keep
a compact, auditable seed universe here and let the factor model decide what is
actually worth buying or avoiding each day.
"""
from __future__ import annotations


US_AI_TECH_UNIVERSE: list[dict] = [
    # AI compute / semiconductors
    {"ticker": "NVDA", "name": "NVIDIA", "sector": "AI compute", "source": "us_ai_compute"},
    {"ticker": "AMD", "name": "Advanced Micro Devices", "sector": "AI compute", "source": "us_ai_compute"},
    {"ticker": "AVGO", "name": "Broadcom", "sector": "ASIC / networking", "source": "us_ai_compute"},
    {"ticker": "MRVL", "name": "Marvell Technology", "sector": "ASIC / networking", "source": "us_ai_compute"},
    {"ticker": "ARM", "name": "Arm Holdings", "sector": "chip IP", "source": "us_ai_compute"},
    {"ticker": "QCOM", "name": "Qualcomm", "sector": "edge AI chips", "source": "us_ai_compute"},
    {"ticker": "MU", "name": "Micron Technology", "sector": "memory", "source": "us_ai_compute"},
    {"ticker": "TSM", "name": "Taiwan Semiconductor Manufacturing", "sector": "foundry", "source": "us_ai_compute"},
    {"ticker": "ASML", "name": "ASML Holding", "sector": "semiconductor equipment", "source": "us_semi_equipment"},
    {"ticker": "AMAT", "name": "Applied Materials", "sector": "semiconductor equipment", "source": "us_semi_equipment"},
    {"ticker": "LRCX", "name": "Lam Research", "sector": "semiconductor equipment", "source": "us_semi_equipment"},
    {"ticker": "KLAC", "name": "KLA", "sector": "semiconductor equipment", "source": "us_semi_equipment"},
    {"ticker": "TER", "name": "Teradyne", "sector": "semiconductor test", "source": "us_semi_equipment"},
    {"ticker": "TXN", "name": "Texas Instruments", "sector": "analog semiconductors", "source": "us_ai_compute"},
    {"ticker": "ADI", "name": "Analog Devices", "sector": "analog semiconductors", "source": "us_ai_compute"},
    {"ticker": "MPWR", "name": "Monolithic Power Systems", "sector": "power semiconductors", "source": "us_ai_compute"},
    {"ticker": "NXPI", "name": "NXP Semiconductors", "sector": "edge semiconductors", "source": "us_ai_compute"},
    {"ticker": "ON", "name": "ON Semiconductor", "sector": "power semiconductors", "source": "us_ai_compute"},
    {"ticker": "ALAB", "name": "Astera Labs", "sector": "AI connectivity", "source": "us_ai_connectivity"},
    {"ticker": "CRDO", "name": "Credo Technology", "sector": "AI connectivity", "source": "us_ai_connectivity"},
    {"ticker": "SMCI", "name": "Super Micro Computer", "sector": "AI servers", "source": "us_ai_hardware"},
    {"ticker": "DELL", "name": "Dell Technologies", "sector": "AI servers", "source": "us_ai_hardware"},
    {"ticker": "HPE", "name": "Hewlett Packard Enterprise", "sector": "AI servers", "source": "us_ai_hardware"},
    {"ticker": "CRWV", "name": "CoreWeave", "sector": "AI cloud", "source": "us_emerging_ai_cloud"},
    {"ticker": "NBIS", "name": "Nebius Group", "sector": "AI cloud", "source": "us_emerging_ai_cloud"},
    {"ticker": "IREN", "name": "IREN", "sector": "AI data centers", "source": "us_emerging_ai_infrastructure"},
    {"ticker": "APLD", "name": "Applied Digital", "sector": "AI data centers", "source": "us_emerging_ai_infrastructure"},

    # Platforms / hyperscalers
    {"ticker": "MSFT", "name": "Microsoft", "sector": "cloud / AI platform", "source": "us_hyperscaler"},
    {"ticker": "GOOGL", "name": "Alphabet", "sector": "cloud / AI platform", "source": "us_hyperscaler"},
    {"ticker": "META", "name": "Meta Platforms", "sector": "AI platform", "source": "us_hyperscaler"},
    {"ticker": "AMZN", "name": "Amazon", "sector": "cloud / AI platform", "source": "us_hyperscaler"},
    {"ticker": "ORCL", "name": "Oracle", "sector": "cloud infrastructure", "source": "us_hyperscaler"},
    {"ticker": "IBM", "name": "IBM", "sector": "enterprise AI", "source": "us_hyperscaler"},
    {"ticker": "AAPL", "name": "Apple", "sector": "edge AI platform", "source": "us_hyperscaler"},
    {"ticker": "TSLA", "name": "Tesla", "sector": "physical AI", "source": "us_physical_ai"},

    # Software / data / security
    {"ticker": "PLTR", "name": "Palantir Technologies", "sector": "AI software", "source": "us_ai_software"},
    {"ticker": "CRM", "name": "Salesforce", "sector": "enterprise software", "source": "us_ai_software"},
    {"ticker": "NOW", "name": "ServiceNow", "sector": "workflow automation", "source": "us_ai_software"},
    {"ticker": "SNOW", "name": "Snowflake", "sector": "data cloud", "source": "us_ai_software"},
    {"ticker": "DDOG", "name": "Datadog", "sector": "observability", "source": "us_ai_software"},
    {"ticker": "NET", "name": "Cloudflare", "sector": "edge cloud", "source": "us_ai_software"},
    {"ticker": "MDB", "name": "MongoDB", "sector": "database", "source": "us_ai_software"},
    {"ticker": "TEAM", "name": "Atlassian", "sector": "collaboration software", "source": "us_ai_software"},
    {"ticker": "INTU", "name": "Intuit", "sector": "application software", "source": "us_ai_software"},
    {"ticker": "ADSK", "name": "Autodesk", "sector": "design software", "source": "us_ai_software"},
    {"ticker": "CDNS", "name": "Cadence Design Systems", "sector": "EDA software", "source": "us_ai_software"},
    {"ticker": "SNPS", "name": "Synopsys", "sector": "EDA software", "source": "us_ai_software"},
    {"ticker": "PANW", "name": "Palo Alto Networks", "sector": "cybersecurity", "source": "us_ai_security"},
    {"ticker": "CRWD", "name": "CrowdStrike", "sector": "cybersecurity", "source": "us_ai_security"},
    {"ticker": "ZS", "name": "Zscaler", "sector": "cybersecurity", "source": "us_ai_security"},

    # Data center / power / physical infrastructure
    {"ticker": "VRT", "name": "Vertiv", "sector": "data center power / cooling", "source": "us_ai_infrastructure"},
    {"ticker": "ETN", "name": "Eaton", "sector": "electrical equipment", "source": "us_ai_infrastructure"},
    {"ticker": "GEV", "name": "GE Vernova", "sector": "grid / power", "source": "us_ai_infrastructure"},
    {"ticker": "PWR", "name": "Quanta Services", "sector": "grid construction", "source": "us_ai_infrastructure"},
    {"ticker": "CEG", "name": "Constellation Energy", "sector": "nuclear power", "source": "us_ai_power"},
    {"ticker": "VST", "name": "Vistra", "sector": "power generation", "source": "us_ai_power"},
    {"ticker": "NRG", "name": "NRG Energy", "sector": "power generation", "source": "us_ai_power"},
    {"ticker": "EQIX", "name": "Equinix", "sector": "data center REIT", "source": "us_ai_infrastructure"},
    {"ticker": "DLR", "name": "Digital Realty", "sector": "data center REIT", "source": "us_ai_infrastructure"},
    {"ticker": "CCJ", "name": "Cameco", "sector": "uranium", "source": "us_ai_power"},
    {"ticker": "BWXT", "name": "BWX Technologies", "sector": "nuclear equipment", "source": "us_ai_power"},
    {"ticker": "LEU", "name": "Centrus Energy", "sector": "nuclear fuel", "source": "us_ai_power"},
    {"ticker": "MP", "name": "MP Materials", "sector": "rare earths", "source": "us_ai_resources"},
    {"ticker": "OKLO", "name": "Oklo", "sector": "advanced nuclear", "source": "us_emerging_ai_power"},
    {"ticker": "SMR", "name": "NuScale Power", "sector": "advanced nuclear", "source": "us_emerging_ai_power"},

    # Physical AI / healthcare AI
    {"ticker": "ISRG", "name": "Intuitive Surgical", "sector": "robotics", "source": "us_physical_ai"},
    {"ticker": "SYM", "name": "Symbotic", "sector": "warehouse robotics", "source": "us_physical_ai"},
    {"ticker": "ROK", "name": "Rockwell Automation", "sector": "industrial automation", "source": "us_physical_ai"},
    {"ticker": "HON", "name": "Honeywell", "sector": "industrial automation", "source": "us_physical_ai"},
    {"ticker": "VEEV", "name": "Veeva Systems", "sector": "life sciences software", "source": "us_ai_healthcare"},
    {"ticker": "TEM", "name": "Tempus AI", "sector": "AI healthcare", "source": "us_ai_healthcare"},
    {"ticker": "RXRX", "name": "Recursion Pharmaceuticals", "sector": "AI drug discovery", "source": "us_ai_healthcare"},
    {"ticker": "RKLB", "name": "Rocket Lab", "sector": "space infrastructure", "source": "us_emerging_hard_tech"},
    {"ticker": "ASTS", "name": "AST SpaceMobile", "sector": "satellite communications", "source": "us_emerging_hard_tech"},
    {"ticker": "IONQ", "name": "IonQ", "sector": "quantum computing", "source": "us_emerging_hard_tech"},
    {"ticker": "SOUN", "name": "SoundHound AI", "sector": "voice AI", "source": "us_emerging_ai_software"},
]


def fetch_us_ai_tech_universe() -> list[dict]:
    """Return the US production universe in a common ticker/name/sector shape."""
    return [{**item, "location": "United States"} for item in US_AI_TECH_UNIVERSE]


if __name__ == "__main__":
    items = fetch_us_ai_tech_universe()
    print(f"US AI/tech universe: {len(items)} tickers")
    from collections import Counter

    by_source = Counter(r["source"] for r in items)
    for source, n in by_source.most_common():
        print(f"  {source}: {n}")
