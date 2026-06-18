"""AI 产业链覆盖 taxonomy + 缺口扫描（需求 docs/V2/2026-06-18_AI产业链覆盖机制与缺口扫描器.md）。

定位：回答"system_universe 对 AI 产业链每个关键环节覆盖了几只、缺哪些龙头"。
之前这类问题只能靠人临时排查（2026-06-17 才聊出 SIMO/WDC/SNDK 没进池），
本模块把它变成系统可每天自检的机制。

⚠️ 语义边界（避免与现有字段冲突，见 feedback_verify_identifier_semantics）：
  - 本模块的 `segment`（产业链关键环节）粒度比 chain_classifier 的 `chain` 更细。
    chain_classifier 把 GPU/存储/光互联/封装 都归在粗粒度 "AI 算力" 一桶；
    这里把它拆成 memory_storage / optical_interconnect / advanced_packaging 等环节，
    专供"覆盖度量"，不替代、不回写 chain_metadata.chain。
  - `anchors`（代表龙头）是"度量覆盖用的参照集"，不是"入池清单"。
    一只 anchor 出现在这里，只代表"它是该环节标杆、用来衡量我们覆没覆盖到"，
    不代表它已可交易、也不会因此自动进 system_universe。
    真正入池前的可交易校验（能抓价→写 price_daily）是另一层闸门（见需求文档 §闸门）。

本模块纯函数、只读、无副作用：输入一组宇宙 symbol，输出每环节覆盖结论。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


# 覆盖状态
COVERED = "covered"  # 已覆盖（present >= min_covered）
THIN = "thin"        # 偏薄（1 <= present < min_covered）
GAP = "gap"          # 盲区（present == 0）


@dataclass(frozen=True)
class ChainSegment:
    """AI 产业链一个关键环节。

    anchors: 代表龙头参照集（度量用，非入池清单）。
    min_covered: 达到几只 anchor 命中才算"已覆盖"。
    """

    key: str
    name: str
    anchors: tuple[str, ...]
    min_covered: int = 2
    note: str = ""


# ─────────────── AI 产业链关键环节清单（taxonomy）───────────────
# 设计原则：每个环节必须有"至少一家代表公司"；环节定义对标 AI 资本开支价值链，
# 不是"所有沾 AI 的票"。新增/调整环节在此一处维护（单一来源）。
AI_SUPPLY_CHAIN_SEGMENTS: list[ChainSegment] = [
    ChainSegment(
        "ai_compute_chips", "算力芯片(GPU/ASIC/CPU/IP)",
        ("NVDA", "AMD", "AVGO", "MRVL", "ARM", "QCOM", "TSM"),
        min_covered=3,
        note="AI 算力核心，目前覆盖最强",
    ),
    ChainSegment(
        "memory_storage", "存储/内存(HBM/NAND/控制器)",
        ("MU", "WDC", "STX", "SNDK", "SIMO"),
        min_covered=2,
        note="2026-06 内存税主线；当前仅 MU 一只 → 盲区",
    ),
    ChainSegment(
        "optical_interconnect", "光互联/光模块(收发/激光)",
        ("COHR", "LITE", "FN", "CIEN", "AAOI"),
        min_covered=2,
        note="800G→1.6T 光模块；当前 0 只纯光 → 盲区",
    ),
    ChainSegment(
        "advanced_packaging", "先进封装/OSAT(CoWoS/HBM封装)",
        ("AMKR", "ASX", "ACLS", "COHU"),
        min_covered=2,
        note="AI 芯片真正产能卡点；当前仅 AMKR → 偏薄",
    ),
    ChainSegment(
        "semi_equipment", "半导体设备(光刻/沉积/刻蚀/测试)",
        ("ASML", "AMAT", "LRCX", "KLAC", "TER"),
        min_covered=3,
    ),
    ChainSegment(
        "server_odm", "服务器/ODM",
        ("DELL", "HPE", "SMCI"),
        min_covered=2,
    ),
    ChainSegment(
        "power_cooling_dc", "电力/散热/数据中心",
        ("VRT", "ETN", "GEV", "VST", "CEG", "EQIX", "DLR"),
        min_covered=3,
    ),
    ChainSegment(
        "cloud_software", "云平台/软件",
        ("MSFT", "GOOGL", "AMZN", "ORCL", "PLTR", "SNOW"),
        min_covered=3,
    ),
    ChainSegment(
        "edge_robotics", "边缘AI/机器人",
        ("ARM", "QCOM", "ISRG", "SYM", "TSLA"),
        min_covered=2,
    ),
]


def _norm(symbols: Iterable[str]) -> set[str]:
    return {str(s).strip().upper() for s in symbols if s and str(s).strip()}


def _status(present_count: int, min_covered: int) -> str:
    if present_count <= 0:
        return GAP
    if present_count < min_covered:
        return THIN
    return COVERED


def compute_chain_coverage(
    universe_symbols: Iterable[str],
    segments: Iterable[ChainSegment] | None = None,
) -> list[dict]:
    """对每个产业链环节算覆盖结论。纯函数，按 segments 定义顺序返回。

    返回 list of dict:
      key / name / status(covered|thin|gap) / present / missing /
      present_count / min_covered / note
    """
    universe = _norm(universe_symbols)
    segs = list(segments) if segments is not None else AI_SUPPLY_CHAIN_SEGMENTS
    out: list[dict] = []
    for seg in segs:
        present = [a for a in seg.anchors if a in universe]
        missing = [a for a in seg.anchors if a not in universe]
        out.append(
            {
                "key": seg.key,
                "name": seg.name,
                "status": _status(len(present), seg.min_covered),
                "present": present,
                "missing": missing,
                "present_count": len(present),
                "min_covered": seg.min_covered,
                "note": seg.note,
            }
        )
    return out


def coverage_gaps(
    universe_symbols: Iterable[str],
    segments: Iterable[ChainSegment] | None = None,
) -> list[dict]:
    """只返回未达标的环节（status != covered），按 gap 优先排序，供告警/早报用。"""
    rows = compute_chain_coverage(universe_symbols, segments)
    order = {GAP: 0, THIN: 1, COVERED: 2}
    return sorted(
        [r for r in rows if r["status"] != COVERED],
        key=lambda r: (order[r["status"]], r["key"]),
    )


def coverage_summary(
    universe_symbols: Iterable[str],
    segments: Iterable[ChainSegment] | None = None,
) -> dict:
    """汇总计数，供 coverage_audit JSON / dashboard 顶部用。"""
    rows = compute_chain_coverage(universe_symbols, segments)
    return {
        "total": len(rows),
        "covered": sum(1 for r in rows if r["status"] == COVERED),
        "thin": sum(1 for r in rows if r["status"] == THIN),
        "gap": sum(1 for r in rows if r["status"] == GAP),
        "rows": rows,
    }
