"""产业链分类器 — 把 system_universe 的 theme/industry/name 映射成 chain/chain_tier/chain_role。

V1 watchlist.chain 字段在 2026-05-21 V1 cutover 时随表删除，V2 没等价字段，
导致 dashboard 产业链地图整段空壳。本模块用 system_universe 已有的 theme/industry/name
做规则分类，把结果写入 chain_metadata 表（V2 一等公民），同时支持 stock_chain_overrides.json
做人工 override。

设计：
  - 规则按 priority 顺序匹配（name 关键词 > theme 关键词 > 行业代码）
  - 多关键词命中以 priority 高者为准
  - 未命中的归为 chain=None（dashboard 会显示"未分类"而不是隐藏）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
# 放 scripts/tools/ 而非 data/（后者 .gitignore 匹配任意路径下的 data 目录）；
# overrides 是 source seed 要随仓库走
OVERRIDES_PATH = REPO / "scripts" / "tools" / "stock_chain_overrides.json"


@dataclass
class ChainTag:
    chain: str | None = None
    chain_tier: str | None = None
    chain_role: str | None = None
    layman_intro: str | None = None
    source: str = "rule_classify"


# 规则表：(优先级降序, 匹配字段, 关键词列表, 输出 ChainTag)
# 命中即返回第一条匹配。新增链条在此追加。
_RULES: list[tuple[int, str, list[str], ChainTag]] = [
    # ===== AI 算力链（最高优先级，关键词最具体）=====
    (100, "name", ["HBM", "海力士", "美光", "三星电子"],
        ChainTag("AI 算力", "上游", "HBM 内存", "AI 服务器最贵的内存芯片")),
    (100, "name", ["光模块", "中际旭创", "新易盛", "天孚", "源杰"],
        ChainTag("AI 算力", "中游", "光模块/CPO", "AI 服务器之间的高速光通信组件")),
    (95, "name", ["寒武纪", "海光信息", "NVIDIA", "英伟达", "AMD", "Broadcom", "博通", "Marvell", "Astera"],
        ChainTag("AI 算力", "上游", "AI 芯片", "训练/推理用 GPU/ASIC")),
    (95, "name", ["晶圆", "TSMC", "台积电", "中芯", "华虹"],
        ChainTag("AI 算力", "上游", "晶圆代工", "把芯片设计变成实体的工厂")),
    (95, "name", ["ASML", "应用材料", "Applied Materials", "Lam", "拉姆", "KLA", "SCREEN", "东京电子", "Tokyo Electron"],
        ChainTag("AI 算力", "上游", "半导体设备", "造芯片所需的光刻/蚀刻设备")),
    (90, "name", ["立讯", "工业富联", "鸿海", "Foxconn", "Dell", "戴尔", "SuperMicro", "Supermicro"],
        ChainTag("AI 算力", "下游", "AI 服务器组装", "把芯片+主板+散热组装成机柜")),
    (90, "name", ["液冷", "Vertiv", "维谛", "英维克", "高澜"],
        ChainTag("AI 算力", "下游", "数据中心散热/电力", "AI 机柜功耗暴涨带动散热/UPS 需求")),
    (88, "name", ["PCB", "沪电", "深南电路", "胜宏"],
        ChainTag("AI 算力", "中游", "AI PCB", "服务器主板高速电路板")),
    (85, "name", ["澜起", "兆易", "长电", "通富微电", "华天", "甬矽"],
        ChainTag("AI 算力", "中游", "封装测试 / 内存接口", "芯片封测与高速内存桥接")),

    # ===== 互联网/云 =====
    (80, "name", ["腾讯", "阿里巴巴", "字节", "百度", "Meta", "Google", "Microsoft", "Amazon",
                  "Tencent", "Alibaba", "京东", "美团", "拼多多", "Pinduoduo", "JD.com"],
        ChainTag("互联网/云", "下游", "互联网平台", "C 端流量+广告+电商主要赚钱方")),
    (78, "name", ["云", "Cloud", "AWS", "Azure", "Snowflake", "Databricks", "MongoDB", "Confluent",
                  "Datadog", "ServiceNow", "Salesforce", "Oracle", "金山办公"],
        ChainTag("互联网/云", "中游", "云/SaaS", "企业用云服务/订阅软件")),
    (76, "name", ["Cybersecurity", "CrowdStrike", "Palo Alto", "Zscaler", "Fortinet", "奇安信", "深信服"],
        ChainTag("互联网/云", "中游", "网络安全", "防黑客/防入侵的企业软件")),

    # ===== 新能源车 / 储能 =====
    (75, "name", ["Tesla", "特斯拉", "比亚迪", "BYD", "理想", "蔚来", "小鹏", "宁德时代", "CATL"],
        ChainTag("新能源车", "下游", "整车/电池", "电动车整车厂或动力电池")),
    (73, "name", ["阳光电源", "锦浪", "德业", "禾迈"],
        ChainTag("光伏储能", "中游", "逆变器", "把太阳能板/电池的直流转交流")),

    # ===== 创新药 / CDMO =====
    (70, "name", ["药明", "WuXi", "凯莱英", "康龙化成"],
        ChainTag("创新药", "中游", "CDMO/CRO", "替药企做研发/生产外包")),
    (70, "name", ["百济神州", "BeiGene", "信达", "Innovent", "再鼎", "君实", "恒瑞"],
        ChainTag("创新药", "下游", "创新药企", "自研新药的 biotech")),

    # ===== 国防/军工 =====
    (65, "name", ["军工", "国防", "航天", "Lockheed", "RTX", "Raytheon", "Northrop", "BWX", "BWXT", "Leidos"],
        ChainTag("军工/国防", "下游", "军工主机厂", "国防订单驱动")),

    # ===== 核能 / 铀 =====
    (60, "name", ["Cameco", "卡梅科", "Uranium", "铀", "OKLO", "SMR", "Nuscale", "NNE", "BWXT"],
        ChainTag("核能 / 铀", "上游", "铀矿/小型反应堆", "AI 数据中心电力 → 核能复兴")),

    # ===== 量子计算 =====
    (55, "name", ["Quantum", "量子", "IonQ", "Rigetti", "D-Wave", "QBTS"],
        ChainTag("量子计算", "上游", "量子硬件", "下一代算力实验阶段")),

    # ===== 机器人 / 自动化 =====
    (50, "name", ["机器人", "Robot", "Symbotic", "ABB", "FANUC", "发那科", "汇川"],
        ChainTag("机器人/自动化", "下游", "工业自动化", "工厂自动化与人形机器人")),

    # ===== 兜底：按 theme/industry 关键词模糊归类 =====
    (30, "theme", ["AI 算力", "半导体", "semiconductor", "AI", "芯片"],
        ChainTag("AI 算力", None, None, "AI/半导体相关，未细分上中下游")),
    (28, "theme", ["互联网", "internet", "cloud", "SaaS", "软件"],
        ChainTag("互联网/云", None, None, "互联网/云/软件相关")),
    (25, "theme", ["创新药", "医药", "biotech", "pharma"],
        ChainTag("创新药", None, None, "医药/创新药相关")),
    (22, "theme", ["新能源车", "EV", "光伏储能", "新能源"],
        ChainTag("新能源车 / 光伏储能", None, None, "新能源整车/储能/光伏相关")),
    (20, "theme", ["机器人", "robot"],
        ChainTag("机器人/自动化", None, None, "机器人相关")),
    (18, "theme", ["军工", "国防"],
        ChainTag("军工/国防", None, None, "军工/国防相关")),
]


def classify_one(name: str | None, theme: str | None, industry: str | None) -> ChainTag:
    """对单只股票做规则分类。三个输入都不区分大小写，None 视为空。"""
    name_s = (name or "").strip()
    theme_s = (theme or "").strip()
    industry_s = (industry or "").strip()

    for _, field, keywords, tag in sorted(_RULES, key=lambda x: -x[0]):
        if field == "name":
            haystack = name_s
        elif field == "theme":
            haystack = f"{theme_s} {industry_s}"
        else:
            haystack = industry_s
        for kw in keywords:
            if kw and kw.lower() in haystack.lower():
                return tag
    return ChainTag()


def load_overrides() -> dict[str, dict]:
    """读 data/stock_chain_overrides.json，dict[symbol -> {chain,chain_tier,chain_role,layman_intro}]。

    文件可不存在；存在则用 dict 字段覆盖 rule_classify 结果，source 改为 manual_override。
    """
    if not OVERRIDES_PATH.exists():
        return {}
    try:
        raw = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if not str(k).startswith("_") and isinstance(v, dict)}
    except Exception as e:
        logger.warning("load chain overrides failed: %s", e)
        return {}


def classify_universe(rows: Iterable[tuple]) -> list[dict]:
    """对一批 universe 行做分类。

    入参 rows: iterable of (market, symbol, name, theme, industry)
    返回 list of dict: market/symbol/chain/chain_tier/chain_role/layman_intro/source
    """
    overrides = load_overrides()
    out: list[dict] = []
    for row in rows:
        market, symbol, name, theme, industry = row[0], row[1], row[2], row[3], row[4]
        # rule 优先算一次
        tag = classify_one(name, theme, industry)
        source = tag.source
        # override 覆盖（按 symbol 匹配）
        ov = overrides.get(symbol)
        if ov:
            tag = ChainTag(
                chain=ov.get("chain") or tag.chain,
                chain_tier=ov.get("chain_tier") or tag.chain_tier,
                chain_role=ov.get("chain_role") or tag.chain_role,
                layman_intro=ov.get("layman_intro") or tag.layman_intro,
                source="manual_override",
            )
            source = "manual_override"
        out.append({
            "market": market,
            "symbol": symbol,
            "chain": tag.chain,
            "chain_tier": tag.chain_tier,
            "chain_role": tag.chain_role,
            "layman_intro": tag.layman_intro,
            "source": source,
        })
    return out
