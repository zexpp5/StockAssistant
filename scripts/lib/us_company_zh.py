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
    "Credo Technology": "Credo 科技",
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
    "INTEL CORP": "英特尔",
    "Diodes": "Diodes 半导体",
    "DIODES INC": "Diodes 半导体",
    "AMKOR TECHNOLOGY INC": "安靠科技",
    "Amkor Technology": "安靠科技",
    "Keysight Technologies": "是德科技",
    "KEYSIGHT TECHNOLOGIES INC": "是德科技",
    "Monolithic Power Systems": "MPS 芯源系统",
    "Lattice Semiconductor": "莱迪思半导体",
    "LATTICE SEMICONDUCTOR CORP": "莱迪思半导体",
    "FORMFACTOR INC": "FormFactor",
    "Teradyne": "泰瑞达",
    # ── AI 服务器 / 硬件 ────────────────────
    "Dell Technologies": "戴尔",
    "Hewlett Packard Enterprise": "HPE 慧与",
    "Super Micro Computer": "超微电脑",
    "Supermicro": "超微电脑",
    "Foxconn": "鸿海(富士康)",
    "ARM Holdings": "ARM",
    "Arm Holdings": "ARM",
    # ── 云 / AI 平台 / 软件 ─────────────────
    "Alphabet": "字母(Google)",
    "Microsoft": "微软",
    "Amazon.com": "亚马逊",
    "Amazon": "亚马逊",
    "Meta Platforms": "Meta(原 Facebook)",
    "Apple": "苹果",
    "Oracle": "甲骨文",
    "Salesforce": "赛富时",
    "ServiceNow": "ServiceNow",
    "Snowflake": "Snowflake 雪花",
    "MongoDB": "MongoDB",
    "MongoDB": "MongoDB 数据库",
    "Datadog": "Datadog 数据狗",
    "Confluent": "Confluent",
    "Cloudflare": "Cloudflare",
    "Cloudflare": "Cloudflare 边缘云",
    "Palantir Technologies": "Palantir",
    "CrowdStrike": "CrowdStrike 安全",
    "CrowdStrike Holdings": "CrowdStrike 安全",
    "Palo Alto Networks": "Palo Alto",
    "Zscaler": "Zscaler 安全",
    "Fortinet": "飞塔",
    "Autodesk": "欧特克",
    "Intuit": "财捷",
    "Atlassian": "Atlassian",
    "Akamai Technologies": "阿卡迈",
    "Arista Networks": "Arista 网络",
    "CISCO SYSTEMS INC": "思科",
    "DigitalOcean Holdings": "DigitalOcean",
    "Applied Digital": "Applied Digital 数据中心",
    "CoreWeave": "CoreWeave AI 云",
    "Nebius Group": "Nebius",
    "ServiceNow": "ServiceNow 工作流",
    "Nutanix": "Nutanix 云平台",
    "Nutanix, Inc.": "Nutanix 云平台",
    "Lumen Technologies": "Lumen 光纤网络",
    "Okta": "Okta",
    "Okta, Inc.": "Okta",
    "F5, Inc.": "F5",
    "Rubrik, Inc.": "Rubrik",
    "SentinelOne, Inc.": "SentinelOne",
    "Tenable Holdings, Inc.": "Tenable",
    "Varonis Systems, Inc.": "Varonis",
    # ── 电网 / 电力 / 核能 ─────────────────
    "Vertiv": "维谛",
    "Vertiv Holdings": "维谛",
    "Eaton": "伊顿电气",
    "GE Vernova": "通用电气电力",
    "Constellation Energy": "Constellation 能源",
    "Vistra": "Vistra 能源",
    "Quanta Services": "Quanta 服务",
    "MasTec": "MasTec",
    "Modine Manufacturing": "Modine",
    "Xylem": "赛莱默",
    "Equinix": "Equinix 数据中心",
    "Digital Realty": "Digital Realty 数据中心",
    "Digital Realty Trust": "Digital Realty 数据中心",
    "Cameco": "Cameco 卡梅科",
    "Kazatomprom": "哈原工",
    "BWX Technologies": "BWX 技术",
    "Centrus Energy": "Centrus 能源",
    "Energy Fuels": "Energy Fuels",
    "Oklo": "Oklo 核电",
    "NuScale Power": "NuScale 核电",
    "NANO Nuclear Energy": "NANO 核能",
    "Rolls-Royce Holdings": "罗罗",
    "Sempra": "森普拉能源",
    "PG&E Corporation": "太平洋煤气电力",
    "NRG Energy": "NRG 能源",
    "Dominion Energy": "Dominion 能源",
    "Duke Energy": "杜克能源",
    "Duke Energy Corporation": "杜克能源",
    "Talen Energy Corporation": "Talen 能源",
    "Bloom Energy Corporation": "Bloom Energy",
    "Clearway Energy, Inc.": "Clearway Energy",
    "First Solar, Inc.": "第一太阳能",
    "Enphase Energy": "Enphase 微逆",
    "SolarEdge Technologies": "SolarEdge",
    "Nextracker Inc.": "Nextracker",
    "Plug Power Inc.": "Plug Power",
    "CSX Corp": "CSX 铁路",
    "Norfolk Southern Corp": "诺福克南方",
    "Union Pacific Corp": "联合太平洋",
    "Nucor Corp": "纽柯",
    "Trane Technologies PLC": "特灵科技",
    "Curtiss-Wright Corporation": "柯蒂斯-莱特",
    "Howmet Aerospace Inc": "Howmet Aerospace",
    # ── EV / 机器人 / 稀土 ─────────────────
    "Tesla": "特斯拉",
    "Symbotic": "Symbotic 机器人",
    "IonQ": "IonQ 离子量子",
    "Rigetti Computing": "Rigetti 量子",
    "D-Wave Quantum": "D-Wave 量子",
    "MP Materials": "MP 稀土",
    "Sociedad Química y Minera de Chile": "SQM 智利化工矿业",
    "Albemarle Corporation": "雅保",
    "Lithium Americas": "Lithium Americas",
    "Lithium Americas Corp.": "Lithium Americas",
    "Almonty Industries": "Almonty",
    "Almonty Industries Inc.": "Almonty",
    "Rio Tinto": "力拓",
    "Rio Tinto plc-Spon ADR": "力拓",
    "Uranium Energy": "Uranium Energy 铀能",
    "Uranium Energy Corp": "Uranium Energy 铀能",
    # ── 生物医药 ────────────────────────
    "Recursion Pharmaceuticals": "Recursion",
    "Schrodinger": "薛定谔",
    "Tempus AI": "Tempus AI",
    "Intuitive Surgical": "直觉外科",
    "Veeva Systems": "Veeva",
    # ── 互联网/社交 ─────────────────────────
    "Reddit": "Reddit",
    "PDD Holdings": "拼多多",
    "PDD Holdings Inc": "拼多多",
    "Vipshop Holdings": "唯品会",
    "Vipshop Holdings Ltd - Adr": "唯品会",
    "Kanzhun": "BOSS直聘",
    "Kanzhun Ltd - Adr": "BOSS直聘",
    "Tal Education Group- Adr": "好未来",
    "Full Truck Alliance -Spn Adr": "满帮",
    # ── 工业自动化 / 太空基础设施 ─────────────
    "Honeywell": "霍尼韦尔",
    "Rockwell Automation": "罗克韦尔自动化",
    "COGNEX CORP": "康耐视",
    "AST SpaceMobile": "AST SpaceMobile",
    "AST SpaceMobile": "AST 太空移动",
    "Rocket Lab": "Rocket Lab",
    "Rocket Lab": "Rocket Lab 火箭实验室",
    "ECHOSTAR CORP CLASS A": "EchoStar",
    "A10 Networks, Inc.": "A10 Networks",
    "TTM TECHNOLOGIES INC": "TTM 科技",
    "TERAWULF INC": "TeraWulf",
    "SoundHound AI": "SoundHound AI",
    "SoundHound AI": "SoundHound 语音 AI",
    "Solstice Advanced Materials, Inc.": "Solstice Advanced Materials",
    "General Dynamics Corp.": "通用动力",
    "IREN": "IREN 数据中心",
    "IBM": "IBM",
    "Atlassian": "Atlassian 协作软件",
    "Tempus AI": "Tempus AI 医疗",
    "Pure Storage (P)": "Pure Storage 纯存储",
}


def get_us_company_zh(en_name: str | None) -> str | None:
    """英文公司名 → 中文短名；没命中保留原值（向后兼容新增 ticker）。"""
    if not en_name:
        return en_name
    key = str(en_name).strip()
    # 严格命中
    if key in US_COMPANY_ZH:
        return US_COMPANY_ZH[key]
    # 大小写不稳定的源（如 INTEL CORP / AMKOR TECHNOLOGY INC）也要稳定命中。
    lower_map = {k.lower(): v for k, v in US_COMPANY_ZH.items()}
    if key.lower() in lower_map:
        return lower_map[key.lower()]
    # 去掉常见后缀再试一次（", Inc." / " Inc" / " Corp" / ", Ltd" 等）
    for suffix in (", Inc.", " Inc.", ", Inc", " Inc", ", Corp.", " Corp.",
                   " Corporation", ", Ltd.", " Ltd.", ", Ltd", " Ltd",
                   ", LLC", " LLC", " plc", " PLC", " SA", " AG", " NV"):
        if key.endswith(suffix):
            stripped = key[: -len(suffix)]
            if stripped in US_COMPANY_ZH:
                return US_COMPANY_ZH[stripped]
            if stripped.lower() in lower_map:
                return lower_map[stripped.lower()]
    return en_name
