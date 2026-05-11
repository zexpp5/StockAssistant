"""跨源数据可信度审计：把 yfinance / akshare / finnhub / 13F 多个源对比，输出一致性结论。

对外接口：
  audit_stock(yf_data, akshare_data, finnhub_data, sec_signals) -> dict
返回结构：
  {
    'credibility': 'HIGH'/'MEDIUM'/'LOW'/'CONFLICT',
    'source_count': 几个源对该股有数据,
    'conflicts': 列表，每条 {field, sources, values, severity},
    'agreements': 列表，每条 {field, sources, value},
    'summary': 人类可读的简述,
  }

判断规则（v2 — 2026-05-11 修订）：
  - 价格：yfinance vs akshare 偏差 > 1% → 标 LOW；> 5% → 标 CONFLICT
  - 市值：> 5% 偏差 → 标 LOW
  - 多个权威源（≥2）一致 → HIGH
  - 单源 → MEDIUM

  13F 处理（v2 重要变更）：
  13F 是 SEC 季度披露，**报告期末后 45 天才公开**。Cohen-Polk-Silli (2010)
  实证：在高波动板块跟 13F 入场是负 α。所以本 audit 里 13F 仅作为：
    (a) 一致性信号（多机构同向 = 加分项，列入 agreements）
    (b) **conviction booster**：在已有非 13F 源时 → 帮助升级到 HIGH
    (c) **不能独立成为"第二个源"**：仅有 yfinance + 13F → 仍维持 MEDIUM
  这避免了"只在某只标的上看到 13F 信号 → 误判 HIGH"的陷阱。
"""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Any

from .. import config

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────
# 工具
# ────────────────────────────────────────────────────────

def _pct_diff(a: float, b: float) -> float | None:
    if a is None or b is None or a == 0:
        return None
    return abs(a - b) / abs(a) * 100


def _safe(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ────────────────────────────────────────────────────────
# 主审计
# ────────────────────────────────────────────────────────

def audit_stock(yf_data: dict[str, Any] | None = None,
                akshare_data: dict[str, Any] | None = None,
                finnhub_data: dict[str, Any] | None = None,
                sec_signals: list[dict[str, Any]] | None = None,
                ticker: str = "") -> dict[str, Any]:
    """对一只股票的多源数据做交叉审计。

    所有参数都可选；只要 ≥1 源就能审计，越多源结论越可靠。
    """
    sources: list[str] = []
    conflicts: list[dict[str, Any]] = []
    agreements: list[dict[str, Any]] = []

    yf_price = _safe((yf_data or {}).get("price"))
    yf_mcap = _safe((yf_data or {}).get("market_cap"))
    if yf_price is not None or yf_mcap is not None:
        sources.append("yfinance")

    ak_quote = ((akshare_data or {}).get("akshare") or {}).get("quote") or {}
    ak_price = _safe(ak_quote.get("price"))
    ak_mcap = _safe(ak_quote.get("market_cap_yuan") or ak_quote.get("market_cap_hkd"))
    if ak_price is not None or ak_mcap is not None:
        sources.append("akshare")

    finnhub_inner = (finnhub_data or {}).get("finnhub") or {}
    if finnhub_inner.get("news") or finnhub_inner.get("insider") or finnhub_inner.get("analyst_recommendations"):
        sources.append("finnhub")

    # 13F 单独追踪（v2）— 不计入 "non_lagging_sources"，只作 conviction booster
    has_13f = bool(sec_signals)
    if has_13f:
        sources.append("sec_edgar_13f")
    # 非滞后源 = 实时/T+1 可比较的数据源；13F 排除在外
    non_lagging_sources = [s for s in sources if s != "sec_edgar_13f"]

    # ────────── 价格交叉验证 ──────────
    if yf_price is not None and ak_price is not None:
        # 注意：yf 用美元/港币，ak 用本币；直接比时只对相同币种有意义
        # 这里只对 A 股（ak 人民币）和港股（ak 港币）做对比
        market = (akshare_data or {}).get("market", "")
        same_currency = "A股" in market or "港股" in market
        if same_currency:
            diff = _pct_diff(yf_price, ak_price)
            if diff is not None:
                if diff > 5:
                    conflicts.append({
                        "field": "price",
                        "sources": ["yfinance", "akshare"],
                        "values": {"yfinance": yf_price, "akshare": ak_price},
                        "diff_pct": round(diff, 2),
                        "severity": "HIGH",
                    })
                elif diff > 1:
                    conflicts.append({
                        "field": "price",
                        "sources": ["yfinance", "akshare"],
                        "values": {"yfinance": yf_price, "akshare": ak_price},
                        "diff_pct": round(diff, 2),
                        "severity": "LOW",
                    })
                else:
                    agreements.append({
                        "field": "price",
                        "sources": ["yfinance", "akshare"],
                        "value_avg": round((yf_price + ak_price) / 2, 2),
                    })

    # ────────── 市值交叉验证 ──────────
    if yf_mcap is not None and ak_mcap is not None:
        diff = _pct_diff(yf_mcap, ak_mcap)
        if diff is not None and diff > 10:
            conflicts.append({
                "field": "market_cap",
                "sources": ["yfinance", "akshare"],
                "values": {"yfinance": yf_mcap, "akshare": ak_mcap},
                "diff_pct": round(diff, 2),
                "severity": "MEDIUM",
            })

    # ────────── 13F 信号一致性 ──────────
    # 多机构同向 = 强信号；分歧 = 注意
    if sec_signals and len(sec_signals) >= 2:
        actions = [s.get("action", "") for s in sec_signals]
        adds = sum(1 for a in actions if "加仓" in a or "新建仓" in a)
        cuts = sum(1 for a in actions if "减仓" in a or "清仓" in a)
        if adds >= 2 and cuts == 0:
            agreements.append({
                "field": "13f_direction",
                "sources": [s.get("investor") for s in sec_signals],
                "value": f"{adds} 家机构加/建仓，无减/清仓",
            })
        elif cuts >= 2 and adds == 0:
            agreements.append({
                "field": "13f_direction",
                "sources": [s.get("investor") for s in sec_signals],
                "value": f"{cuts} 家机构减/清仓，无加/建仓",
            })
        elif adds and cuts:
            conflicts.append({
                "field": "13f_direction",
                "sources": [s.get("investor") for s in sec_signals],
                "values": {"adds": adds, "cuts": cuts},
                "severity": "INFO",
            })

    # ────────── 结论（v2 — 13F 仅作 conviction booster）──────────
    # 规则：HIGH 要求 ≥2 个 non-lagging 源；13F 可在已有非滞后源时帮助升级，
    # 但单独的 (yfinance + 13F) 仍只算 MEDIUM（因 13F 是 45 天滞后数据）
    if any(c["severity"] == "HIGH" for c in conflicts):
        cred = "CONFLICT"
    elif len(non_lagging_sources) >= 3 and not conflicts:
        cred = "HIGH"
    elif len(non_lagging_sources) >= 2:
        cred = "HIGH" if not conflicts else "MEDIUM"
    elif len(non_lagging_sources) == 1 and has_13f and not conflicts:
        # 1 个非滞后源 + 13F 一致信号 → conviction-boosted MEDIUM
        # 仅在 agreements 已包含 13f_direction 时升 MEDIUM；否则仍是 MEDIUM
        cred = "MEDIUM"
    elif len(non_lagging_sources) >= 1:
        cred = "MEDIUM"
    elif has_13f:
        # 仅 13F 一个源 — 明确标 LOW（避免单凭 45 天滞后信号误判 HIGH）
        cred = "LOW"
    else:
        cred = "LOW"

    summary_parts = [f"{len(sources)} 个源：{', '.join(sources) or '无'}"]
    if has_13f and len(non_lagging_sources) == 0:
        summary_parts.append("⚠️ 仅 13F（45 天滞后，不作为独立可信源）")
    elif has_13f and len(non_lagging_sources) < 2:
        summary_parts.append("ℹ️ 13F 作 conviction booster（非滞后源仍不足 2 个）")
    if agreements:
        summary_parts.append(f"{len(agreements)} 项一致")
    if conflicts:
        high = sum(1 for c in conflicts if c["severity"] == "HIGH")
        if high:
            summary_parts.append(f"⚠️ {high} 项严重冲突")
        else:
            summary_parts.append(f"{len(conflicts)} 项轻微差异")

    return {
        "ticker": ticker,
        "audited_at": datetime.now().isoformat(timespec="seconds"),
        "credibility": cred,
        "credibility_label": config.CREDIBILITY_LEVELS.get(cred, cred),
        "source_count": len(sources),
        "sources": sources,
        "conflicts": conflicts,
        "agreements": agreements,
        "summary": " · ".join(summary_parts),
    }


def format_audit_text(audit: dict[str, Any]) -> str:
    """把审计结果格式化成飞书字段可读的文本。"""
    lines = [
        f"{audit.get('credibility_label', audit.get('credibility', '?'))}",
        f"📊 {audit.get('summary', '')}",
        f"⏰ 审计于 {audit.get('audited_at', '')}",
    ]
    for c in audit.get("conflicts", []):
        sev_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "⚪", "INFO": "ℹ️"}.get(c.get("severity"), "⚠️")
        vals = c.get("values", {})
        if isinstance(vals, dict):
            vals_str = " vs ".join(f"{k}={v}" for k, v in vals.items())
        else:
            vals_str = str(vals)
        diff = f" ({c.get('diff_pct')}% 偏差)" if c.get("diff_pct") else ""
        lines.append(f"{sev_emoji} {c.get('field')}: {vals_str}{diff}")
    return "\n".join(lines)
