"""美股 system_universe.theme 英文 → 中文映射（产业链地图 / AI 助手 tab 用）。

us_universe 写入时 theme 是英文小写短语（"AI servers" / "memory" 等），
中文界面里读起来不便。这里做一次平移，命中翻成带 emoji 的中文，
没命中保留原文（向后兼容新增主题）。

调用：
  from us_theme_zh import get_us_theme_zh
  theme_zh = get_us_theme_zh("AI servers")  # → "🖥 AI 服务器"
"""
from __future__ import annotations

US_THEME_ZH: dict[str, str] = {
    # ── 半导体 ─────────────────────────────
    "AI compute": "💾 AI 算力",
    "ASIC / networking": "💾 ASIC / 网络",
    "analog semiconductors": "💾 模拟半导体",
    "power semiconductors": "💾 电源半导体",
    "edge semiconductors": "💾 边缘半导体",
    "edge AI chips": "💾 边缘 AI 芯片",
    "memory": "🗄 存储",
    "foundry": "🏭 晶圆代工",
    "semiconductor equipment": "🔬 半导体设备",
    "semiconductor test": "🔬 半导体测试",
    "chip IP": "💾 芯片 IP",
    "EDA software": "💾 EDA 软件",

    # ── AI 应用 / 平台 / 互联 ──────────────
    "AI servers": "🖥 AI 服务器",
    "AI connectivity": "📡 AI 互联",
    "AI platform": "🤖 AI 平台",
    "AI software": "🤖 AI 软件",
    "AI drug discovery": "🤖 AI 药物研发",
    "AI healthcare": "🤖 AI 医疗",
    "enterprise AI": "🤖 企业 AI",
    "edge AI platform": "🤖 边缘 AI 平台",
    "physical AI": "🤖 物理 AI",

    # ── 云 / 软件 / 数据 ───────────────────
    "cloud / AI platform": "☁️ 云 / AI 平台",
    "cloud infrastructure": "☁️ 云基础设施",
    "edge cloud": "☁️ 边缘云",
    "data cloud": "☁️ 数据云",
    "database": "💽 数据库",
    "observability": "📊 可观测性",
    "cybersecurity": "🛡 网络安全",
    "design software": "💻 设计软件",
    "enterprise software": "💻 企业软件",
    "application software": "💻 应用软件",
    "workflow automation": "💻 工作流自动化",
    "collaboration software": "💻 协作软件",
    "life sciences software": "💻 生命科学软件",

    # ── 数据中心 / 电力 / 核能 ─────────────
    "data center REIT": "🏢 数据中心 REIT",
    "data center power / cooling": "❄️ 数据中心电源/散热",
    "power generation": "⚡ 发电",
    "grid / power": "⚡ 电网 / 电力",
    "grid construction": "⚡ 电网建设",
    "electrical equipment": "⚡ 电气设备",
    "nuclear power": "☢️ 核电",
    "nuclear equipment": "☢️ 核能设备",
    "nuclear fuel": "☢️ 核燃料",
    "uranium": "☢️ 铀",

    # ── 机器人 / 工业 / 材料 ───────────────
    "industrial automation": "⚙️ 工业自动化",
    "robotics": "🤖 机器人",
    "warehouse robotics": "🤖 仓储机器人",
    "rare earths": "🧪 稀土",
}


def get_us_theme_zh(en_theme: str | None) -> str | None:
    """英文 theme → 中文 emoji 主题；没命中保留原值（向后兼容新主题）。"""
    if not en_theme:
        return en_theme
    key = en_theme.strip()
    return US_THEME_ZH.get(key, en_theme)
