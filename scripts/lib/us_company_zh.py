"""美股 system_universe.name 英文 → 中文公司名映射（持仓/AI 推荐/产业链地图通用）。

us_universe 写入时 name 是英文官方名（"NVIDIA" / "Dell Technologies" / "Marvell Technology"
等），中文界面里读起来不便。这里做一次平移，命中翻成中文短名，没命中保留原文
（向后兼容新增 ticker，避免缺失 fallback 到 ticker 自己）。

设计：
  - 优先收录核心 ticker（NVDA / DELL / MU / AVGO 等用户日常会看到的）
  - 用最通用的中文短名（"英伟达" 而非 "辉达科技"），避免方言/不规范译法
  - 命名跟 us_theme_zh.py 完全平行，方便维护

调用：
  from us_company_zh import get_us_company_zh
  name_zh = get_us_company_zh("NVIDIA")  # → "英伟达"
"""
from __future__ import annotations

US_COMPANY_ZH: dict[str, str] = {
    # ── AI 算力核心 ─────────────────────────
    "NVIDIA": "英伟达",
    "Advanced Micro Devices": "AMD",
    "Broadcom": "博通",
    "Marvell Technology": "美满电子",
    "Astera Labs": "Astera 实验室",
    "Astera Labs Inc": "Astera 实验室",
    # ── 内存 / 存储 ────────────────────────
    "Micron Technology": "美光",
    "SanDisk": "闪迪",
    "Western Digital": "西部数据",
    "Pure Storage": "Pure Storage 纯存储",
    # ── 晶圆代工 / 设备 ────────────────────
    "Taiwan Semiconductor Manufacturing": "台积电",
    "Taiwan Semiconductor Mfg": "台积电",
    "TSMC": "台积电",
    "ASML Holding": "阿斯麦",
    "Applied Materials": "应用材料",
    "Lam Research": "拉姆研究",
    "KLA": "科磊",
    "Synopsys": "新思科技",
    "Cadence Design Systems": "铿腾电子",
    # ── 模拟 / 边缘半导体 ────────────────────
    "NXP Semiconductors": "恩智浦",
    "ON Semiconductor": "安森美",
    "Texas Instruments": "德州仪器",
    "Analog Devices": "亚德诺",
    "Qualcomm": "高通",
    "Intel": "英特尔",
    # ── AI 服务器 / 硬件 ────────────────────
    "Dell Technologies": "戴尔",
    "Hewlett Packard Enterprise": "HPE 慧与",
    "Super Micro Computer": "超微电脑",
    "Supermicro": "超微电脑",
    "Foxconn": "鸿海(富士康)",
    "ARM Holdings": "ARM",
    # ── 云 / AI 平台 / 软件 ─────────────────
    "Alphabet": "字母(Google)",
    "Microsoft": "微软",
    "Amazon.com": "亚马逊",
    "Amazon": "亚马逊",
    "Meta Platforms": "Meta(原 Facebook)",
    "Apple": "苹果",
    "Oracle": "甲骨文",
    "Salesforce": "Salesforce",
    "ServiceNow": "ServiceNow",
    "Snowflake": "Snowflake 雪花",
    "MongoDB": "MongoDB",
    "Datadog": "Datadog 数据狗",
    "Confluent": "Confluent",
    "Cloudflare": "Cloudflare",
    "Palantir Technologies": "Palantir",
    "CrowdStrike Holdings": "CrowdStrike",
    "Palo Alto Networks": "Palo Alto",
    "Zscaler": "Zscaler",
    "Fortinet": "飞塔",
    # ── 电网 / 电力 / 核能 ─────────────────
    "Vertiv Holdings": "Vertiv 维谛",
    "Eaton": "伊顿电气",
    "GE Vernova": "通用电气电力",
    "Constellation Energy": "Constellation 能源",
    "Vistra": "Vistra 能源",
    "Quanta Services": "Quanta 服务",
    "MasTec": "MasTec",
    "Modine Manufacturing": "Modine",
    "Xylem": "赛莱默",
    "Equinix": "Equinix",
    "Digital Realty Trust": "Digital Realty",
    "Cameco": "Cameco 卡梅科",
    "Kazatomprom": "哈原工",
    "BWX Technologies": "BWX 技术",
    "Centrus Energy": "Centrus 能源",
    "Energy Fuels": "Energy Fuels",
    "Oklo": "Oklo",
    "NuScale Power": "NuScale 核电",
    "NANO Nuclear Energy": "NANO 核能",
    "Rolls-Royce Holdings": "罗罗",
    # ── EV / 机器人 / 稀土 ─────────────────
    "Tesla": "特斯拉",
    "Symbotic": "Symbotic 机器人",
    "IonQ": "IonQ 离子量子",
    "Rigetti Computing": "Rigetti 量子",
    "D-Wave Quantum": "D-Wave 量子",
    "MP Materials": "MP 稀土",
    # ── 生物医药 ────────────────────────
    "Recursion Pharmaceuticals": "Recursion",
    "Schrodinger": "薛定谔",
    "Tempus AI": "Tempus AI",
    "Veeva Systems": "Veeva",
    # ── 互联网/社交 ─────────────────────────
    "Reddit": "Reddit",
}


def get_us_company_zh(en_name: str | None) -> str | None:
    """英文公司名 → 中文短名；没命中保留原值（向后兼容新增 ticker）。"""
    if not en_name:
        return en_name
    key = str(en_name).strip()
    # 严格命中
    if key in US_COMPANY_ZH:
        return US_COMPANY_ZH[key]
    # 去掉常见后缀再试一次（", Inc." / " Inc" / " Corp" / ", Ltd" 等）
    for suffix in (", Inc.", " Inc.", ", Inc", " Inc", ", Corp.", " Corp.",
                   " Corporation", ", Ltd.", " Ltd.", ", Ltd", " Ltd",
                   ", LLC", " LLC", " plc", " PLC", " SA", " AG", " NV"):
        if key.endswith(suffix):
            stripped = key[: -len(suffix)]
            if stripped in US_COMPANY_ZH:
                return US_COMPANY_ZH[stripped]
    return en_name
