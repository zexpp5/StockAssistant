"""Technology-growth layer classification for the v2 recommendation gate.

This module is intentionally conservative. It does not score stocks and it does
not use price action. Its job is to provide a stable identity layer that the
recommendation builder can combine with evidence and risk gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CLASSIFICATION_VERSION = "tech_growth_layers_2026_06_09_p0"

PRIMARY_LAYERS = {
    "ai_core",
    "ai_infrastructure",
    "tech_software",
    "internet_platform",
    "power_datacenter",
    "theme_watch",
    "excluded",
}

BUYABLE_ALLOWED_LAYERS = {
    "ai_core",
    "ai_infrastructure",
    "tech_software",
    "internet_platform",
    "power_datacenter",
}

_EXCLUDED_US_SYMBOLS = {
    "VIPS": "中概折扣电商/零售平台，不能因 KWEB/互联网标签进入 AI 科技成长推荐",
    "MCD": "餐饮消费股，不属于科技成长主线",
    "ADM": "农产品加工股，不属于科技成长主线",
    "IAUM": "黄金 ETF，不属于科技成长股票",
    "BRK-B": "综合控股/保险，不属于科技成长股票",
    "BRK.B": "综合控股/保险，不属于科技成长股票",
    "BKRDX": "基金产品，不属于单只科技成长股票",
}

_AI_CORE_BY_SYMBOL = {
    "NVDA",
    "AMD",
    "AVGO",
    "MRVL",
    "ARM",
    "QCOM",
    "MU",
    "TSM",
    "ASML",
    "AMAT",
    "LRCX",
    "KLAC",
    "TER",
    "TXN",
    "ADI",
    "MPWR",
    "NXPI",
    "ON",
    "ALAB",
    "CRDO",
    "MSFT",
    "ORCL",
    "IBM",
}

_AI_NETWORK_BY_SYMBOL = {"AVGO", "MRVL", "ALAB", "CRDO"}
_CUSTOM_SILICON_BY_SYMBOL = {"AVGO", "MRVL", "GOOGL", "AMZN", "META", "MSFT"}
_AI_INFRA_BY_SYMBOL = {"SMCI", "DELL", "HPE", "CRWV", "NBIS", "IREN", "APLD"}
_INTERNET_PLATFORM_BY_SYMBOL = {"GOOGL", "GOOG", "META", "AMZN", "NFLX", "UBER", "SHOP", "AAPL", "TSLA"}
_TECH_SOFTWARE_BY_SYMBOL = {
    "PLTR",
    "CRM",
    "NOW",
    "SNOW",
    "DDOG",
    "NET",
    "MDB",
    "TEAM",
    "INTU",
    "ADSK",
    "CDNS",
    "SNPS",
    "PANW",
    "CRWD",
    "ZS",
    "VEEV",
    "TEM",
    "RXRX",
    "SOUN",
}
_POWER_DATACENTER_BY_SYMBOL = {
    "VRT",
    "ETN",
    "GEV",
    "PWR",
    "CEG",
    "VST",
    "NRG",
    "EQIX",
    "DLR",
    "CCJ",
    "BWXT",
    "LEU",
    "MP",
    "OKLO",
    "SMR",
}
_THEME_WATCH_BY_SYMBOL = {
    "ISRG",
    "SYM",
    "ROK",
    "HON",
    "RKLB",
    "ASTS",
    "IONQ",
}


@dataclass(frozen=True)
class TechGrowthLayer:
    primary_layer: str
    secondary_layers: tuple[str, ...] = ()
    ai_relevance_level: str = "unknown"
    layer_confidence: str = "medium"
    rationale: str = ""
    classification_version: str = CLASSIFICATION_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "primary_layer": self.primary_layer,
            "secondary_layers": list(self.secondary_layers),
            "ai_relevance_level": self.ai_relevance_level,
            "layer_confidence": self.layer_confidence,
            "rationale": self.rationale,
            "classification_version": self.classification_version,
        }


def _norm_symbol(symbol: Any) -> str:
    return str(symbol or "").strip().upper()


def _source_text(*values: Any) -> str:
    return " ".join(str(v or "") for v in values).lower()


def classify_tech_growth_layer(
    *,
    market: str | None,
    symbol: str,
    source: str | None = None,
    theme: str | None = None,
    industry: str | None = None,
    name: str | None = None,
) -> TechGrowthLayer:
    """Return the conservative P0 technology-growth identity layer."""
    sym = _norm_symbol(symbol)
    market_norm = str(market or "").upper()
    text = _source_text(source, theme, industry, name)

    if market_norm != "US":
        return TechGrowthLayer(
            primary_layer="theme_watch",
            ai_relevance_level="unknown",
            layer_confidence="low",
            rationale="P0 只迁移 US；非 US 市场保留 legacy 状态，不套用美股阈值",
        )

    if sym in _EXCLUDED_US_SYMBOLS:
        return TechGrowthLayer(
            primary_layer="excluded",
            ai_relevance_level="none",
            layer_confidence="high",
            rationale=_EXCLUDED_US_SYMBOLS[sym],
        )

    if sym in _POWER_DATACENTER_BY_SYMBOL:
        return TechGrowthLayer(
            primary_layer="power_datacenter",
            secondary_layers=("ai_infrastructure",),
            ai_relevance_level="enabler",
            layer_confidence="high",
            rationale="数据中心电力、散热、核电或电网基础设施",
        )

    if sym in _AI_INFRA_BY_SYMBOL:
        return TechGrowthLayer(
            primary_layer="ai_infrastructure",
            secondary_layers=("data_center",),
            ai_relevance_level="enabler",
            layer_confidence="high",
            rationale="AI 服务器、云基础设施或数据中心硬件集成",
        )

    if sym in _AI_CORE_BY_SYMBOL or any(k in text for k in ("ai compute", "asic", "networking", "foundry", "semiconductor")):
        secondary: list[str] = []
        if sym in _AI_NETWORK_BY_SYMBOL or "connectivity" in text or "network" in text:
            secondary.append("ai_network")
        if sym in _CUSTOM_SILICON_BY_SYMBOL or "asic" in text:
            secondary.append("custom_silicon")
        if "cloud" in text or sym in {"MSFT", "ORCL", "IBM"}:
            secondary.append("cloud_ai")
        return TechGrowthLayer(
            primary_layer="ai_core",
            secondary_layers=tuple(dict.fromkeys(secondary)),
            ai_relevance_level="core",
            layer_confidence="high",
            rationale="AI 核心芯片、网络、云平台或半导体使能层",
        )

    if any(k in text for k in ("ai servers", "ai cloud", "ai data centers")):
        return TechGrowthLayer(
            primary_layer="ai_infrastructure",
            secondary_layers=("data_center",),
            ai_relevance_level="enabler",
            layer_confidence="high",
            rationale="AI 服务器、云基础设施或数据中心硬件集成",
        )

    if any(k in text for k in ("power", "grid", "cooling", "electrical", "nuclear", "uranium")):
        return TechGrowthLayer(
            primary_layer="power_datacenter",
            secondary_layers=("ai_infrastructure",),
            ai_relevance_level="enabler",
            layer_confidence="high",
            rationale="数据中心电力、散热、核电或电网基础设施",
        )

    if sym in _TECH_SOFTWARE_BY_SYMBOL or any(k in text for k in ("software", "cybersecurity", "data cloud", "workflow", "database")):
        return TechGrowthLayer(
            primary_layer="tech_software",
            secondary_layers=("ai_application",),
            ai_relevance_level="adjacent",
            layer_confidence="medium",
            rationale="企业软件、数据、安全或 AI 应用工作流",
        )

    if sym in _INTERNET_PLATFORM_BY_SYMBOL or any(k in text for k in ("platform", "advertising", "internet", "ecommerce")):
        secondary = ["ai_core"] if sym in {"GOOGL", "GOOG", "META", "AMZN", "MSFT"} else []
        if "cloud" in text:
            secondary.append("cloud_ai")
        return TechGrowthLayer(
            primary_layer="internet_platform",
            secondary_layers=tuple(dict.fromkeys(secondary)),
            ai_relevance_level="adjacent",
            layer_confidence="medium",
            rationale="互联网平台/流量/数据生态，AI 受益需看业务兑现",
        )

    if sym in _THEME_WATCH_BY_SYMBOL or any(k in text for k in ("robotics", "space", "quantum", "satellite", "hard tech")):
        return TechGrowthLayer(
            primary_layer="theme_watch",
            ai_relevance_level="adjacent",
            layer_confidence="medium",
            rationale="主题相关但 P0 证据不足，先观察或研究",
        )

    if source and str(source).startswith("etf_theme:"):
        return TechGrowthLayer(
            primary_layer="theme_watch",
            ai_relevance_level="unknown",
            layer_confidence="low",
            rationale="仅 ETF 主题来源，缺公司级证据前不进入可买候选",
        )

    return TechGrowthLayer(
        primary_layer="theme_watch",
        ai_relevance_level="unknown",
        layer_confidence="low",
        rationale="暂未进入手审科技成长分层，默认观察",
    )


def is_buyable_layer(primary_layer: str | None) -> bool:
    return str(primary_layer or "") in BUYABLE_ALLOWED_LAYERS
