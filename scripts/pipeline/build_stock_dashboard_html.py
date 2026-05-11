"""
AI 投资研究 Dashboard - 专业研究报告风格

设计目标：让一个完全没看过这些数据的同伴，30 秒看懂全局，3 分钟看懂任何一只股票。

输出：stock_dashboard.html（脚本所在目录）
"""
import sys
import os
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))  # 2026-05-11 lib 迁移
import json
from datetime import datetime

# 2026-05-11 PM 第二轮:飞书 100% 退役 — 不再从 Bitable 拉数据
# records / picks 全部来自 DuckDB(watchlist + reviews JOIN picks + prices)
OUTPUT = os.path.join(_REPO, "stock_dashboard.html")


# ============================================================
# 主题分类（核心：把股票按"投资逻辑主题"分组而不是按行业）
# ============================================================

THEMES = [
    {
        "id": "compute",
        "icon": "🔥",
        "title": "AI 算力核心",
        "subtitle": "GPU + 芯片代工 + HBM 内存 三大支柱",
        "judgment": "已大涨，但仍是绝对核心",
        "judgment_color": "amber",
        "logic": (
            "AI 训练的物理基础。NVDA 在 GPU 端 90% 份额，TSMC 是几乎所有 AI 芯片"
            "的唯一代工，SK Hynix 在 HBM 端 62% 份额。三家任何一家停摆，全行业停摆。"
            "**最确定的赢家，但估值已不便宜**。"
        ),
        "tickers": ["NVDA", "TSM", "000660.KS", "AMD"],
    },
    {
        "id": "interconnect",
        "icon": "💡",
        "title": "AI 连接（光通信+ASIC）",
        "subtitle": "光模块 + 光学 DSP + 定制 ASIC",
        "judgment": "5/7 刚回调，关注是否扩散",
        "judgment_color": "red",
        "logic": (
            "AI 集群规模越大，互连需求越高。光通信链 1.6T/3.2T 时代刚开始；"
            "ASIC（Broadcom Google TPU、Meta MTIA）是 Nvidia 的潜在威胁。"
            "**5/7 板块大跌（COHR -10%, AAOI -14%），市场情绪转向**，需观察是否延续。"
        ),
        "tickers": ["MRVL", "AVGO", "300308", "300502", "688635"],
    },
    {
        "id": "power",
        "icon": "⚡",
        "title": "AI 电力链（确定性次主线）",
        "subtitle": "发电 → 输电 → 配电 → 冷却 → 建设",
        "judgment": "确定性最高的次主线",
        "judgment_color": "emerald",
        "logic": (
            "**「电力是 AI 的硬瓶颈」已经成为共识**。Eaton 数据中心订单 +240%、"
            "Quanta backlog $48.5B、Vertiv liquid cooling 默认标准、GEV 数据中心订单 1Q>2025全年。"
            "**5 家组合覆盖全产业链**，业绩兑现度极高，但都已涨过较多。"
        ),
        "tickers": ["GEV", "ETN", "PWR", "MTZ", "VRT", "VST"],
    },
    {
        "id": "scarce_resources",
        "icon": "💎",
        "title": "下一波稀缺资源（核心机会区）",
        "subtitle": "水 + 稀土 + 铀 + AI 数据 + SMR",
        "judgment": "🌟 最被低估的方向（重点关注）",
        "judgment_color": "violet",
        "logic": (
            "**SK Hynix(+920%) 和 SanDisk(+1200%) 的故事告诉我们：「冷门→热门」转换是百倍回报来源**。\n"
            "现在还在「冷门→热门」拐点的方向：\n"
            "• 水（Xylem 数据中心订单单 Q 超 2025 全年）\n"
            "• 稀土（MP Pentagon 10 年 $110/kg 价格底）\n"
            "• 铀（Cameco 净利 +87%）\n"
            "• SMR（BWXT 唯一规模化 TRISO 燃料）\n"
            "• AI 训练数据（Reddit 唯一上市标的）"
        ),
        "tickers": ["XYL", "MP", "CCJ", "BWXT", "RDDT"],
    },
    {
        "id": "data_center",
        "icon": "🏢",
        "title": "数据中心承载层",
        "subtitle": "REIT + 主权 AI + 设备",
        "judgment": "需求兑现度高",
        "judgment_color": "blue",
        "logic": (
            "AI 算力跑在哪里？Equinix（数据中心 REIT）、Oracle（OCI + UAE Stargate 主权 AI）、"
            "**HBM 设备厂 Lam Research**（每片晶圆需要的设备数量是传统 DRAM 的 2-3 倍）。"
        ),
        "tickers": ["EQIX", "ORCL", "LRCX"],
    },
    {
        "id": "applications",
        "icon": "🤖",
        "title": "AI 应用层（Agentic 浪潮）",
        "subtitle": "Agentic + 数据云 + 边缘 + 安全",
        "judgment": "5/5 Anthropic 推 Agent 是分水岭",
        "judgment_color": "indigo",
        "logic": (
            "Anthropic 5/5 推金融服务 Agent + Carlyle/FIS 部署 + Goldman 用 Devin —— "
            "Agentic AI 商业化第一波启动。受益方：Snowflake（数据云）、UiPath（Agent 平台）、"
            "Cloudflare（边缘推理）、Cadence（AI 设计芯片）、CrowdStrike（保护 AI）。"
        ),
        "tickers": ["GOOGL", "SNOW", "PATH", "NET", "CDNS", "CRWD"],
    },
    {
        "id": "physical_ai",
        "icon": "🦾",
        "title": "物理 AI（机器人/自动驾驶）",
        "subtitle": "仓储机器人已落地，人形机器人在路上",
        "judgment": "Symbotic 已兑现，Tesla 高赔率高风险",
        "judgment_color": "fuchsia",
        "logic": (
            "**Symbotic** 已和 Walmart 跑通仓储机器人（GAAP 盈利），是「物理 AI 」最早兑现的标的。"
            "**Tesla** Robotaxi 已扩 12 城但收入仍小，Optimus 量产 2026Q2 起。"
            "Figure / 1X / Boston Dynamics 都还是私募。"
        ),
        "tickers": ["SYM", "TSLA"],
    },
    {
        "id": "medical",
        "icon": "🧬",
        "title": "AI 医疗 / 药物发现",
        "subtitle": "Tempus Q1 +36%、Recursion 临床突破",
        "judgment": "高赔率高风险",
        "judgment_color": "pink",
        "logic": (
            "**Tempus AI** 是精准医疗 AI 第一股，Q1 +36%、调整 EBITDA 转正。"
            "**Recursion** 首个 AI 药物临床概念验证（FAP 患者息肉减 43%）。"
            "都是「故事+证据」早期股，适合小仓位。"
        ),
        "tickers": ["TEM", "RXRX"],
    },
    {
        "id": "platform_tech",
        "icon": "📱",
        "title": "平台/巨头/防御",
        "subtitle": "Apple+Intel 转型 + 港股 AI 间接",
        "judgment": "Apple 看 WWDC、Intel 看 Foundry",
        "judgment_color": "slate",
        "logic": (
            "Apple 的 WWDC 2026（6/8）是「Apple Intelligence 是否真兑现」关键节点。"
            "Intel 的 Foundry 14A 拿下 Tesla、连续 6 季度超预期，转折期。"
            "美团是「中国 AI 应用层」港股最相关间接受益方。"
        ),
        "tickers": ["AAPL", "INTC", "3690.HK"],
    },
    {
        "id": "defense",
        "icon": "🛡️",
        "title": "防御/对照（不是 AI 故事）",
        "subtitle": "回调时的避险",
        "judgment": "不应作为 AI 配置",
        "judgment_color": "stone",
        "logic": (
            "**KO/MCD 不是 AI 故事股**，放进自选股 是为了在 AI 板块回调时提供对照。"
            "如果你的组合 100% 是 AI 主题，加 5-10% 这种防御资产能降低波动。"
        ),
        "tickers": ["KO", "MCD"],
    },
]


# ============================================================
# AI 主线演进时间轴
# ============================================================
EVOLUTION = [
    {"year": "2023", "phase": "算力（GPU）", "winner": "NVDA", "return": "10x+", "stage": "已涨过"},
    {"year": "2024 H1", "phase": "芯片代工 / HBM 上游", "winner": "TSM, SK Hynix", "return": "3-9x", "stage": "已涨过"},
    {"year": "2024 H2", "phase": "网络 / 光通信", "winner": "COHR, LITE, 中际旭创", "return": "3-5x", "stage": "5/7 刚回调"},
    {"year": "2025 H1", "phase": "电力发电（核电+独立电力）", "winner": "VST, CEG", "return": "5-10x", "stage": "已涨过"},
    {"year": "2025 H2", "phase": "电力配电 / 电气化", "winner": "GEV, ETN", "return": "1-3x", "stage": "仍在兑现"},
    {"year": "2025 H2-2026", "phase": "HBM 内存", "winner": "SK Hynix +920%, Micron +90%", "return": "10x", "stage": "已涨过"},
    {"year": "2026 Q1-Q2", "phase": "NAND / HDD 存储", "winner": "SanDisk +1200%（一年）, WDC +176%", "return": "12x", "stage": "进行中"},
    {"year": "2026 H2 (?)", "phase": "🌟 水冷却 / 稀土 / SMR / AI 数据", "winner": "XYL, MP, BWXT, RDDT?", "return": "?", "stage": "潜伏期"},
    {"year": "2027+ (?)", "phase": "🌟 AR 眼镜 / 量子 / 聚变", "winner": "Meta? Apple? Lightmatter?", "return": "?", "stage": "早期"},
]


# ============================================================
# 6 月关键事件
# ============================================================
EVENTS = [
    {"date": "2026-06-01", "title": "NVIDIA GTC Taipei", "tickers": ["NVDA", "TSM"], "desc": "黄仁勋演讲：Rubin / CPO / AI Factory"},
    {"date": "2026-06-02", "title": "Computex 2026", "tickers": ["INTC", "AMD", "AAPL"], "desc": "AI PC + 机器人 + AR/VR"},
    {"date": "2026-06-08", "title": "Apple WWDC 2026", "tickers": ["AAPL"], "desc": "Apple Intelligence 关键节点"},
    {"date": "2026-07-22", "title": "AMD Advancing AI", "tickers": ["AMD"], "desc": "MI400 vs Rubin"},
    {"date": "2026-08-27", "title": "NVIDIA Q1 FY27 财报", "tickers": ["NVDA"], "desc": "Capex 平缓化最早信号期"},
    {"date": "2026-09-15", "title": "iPhone 18 发布", "tickers": ["AAPL"], "desc": "AI iPhone 周期能否延续"},
]


# ============================================================
# 百倍股的 5 个共同条件
# ============================================================
HUNDRED_X_CONDITIONS = [
    {"icon": "❄️", "title": "冷门到极点", "desc": "市场把它归为「夕阳行业」「周期股」，估值低、关注度低。SK Hynix 2022 年 HBM 还不被重视；SanDisk 拆分时 NAND 被认为是衰退业务。"},
    {"icon": "🚧", "title": "结构性短缺", "desc": "寡头供给（3-5 家）+ 长 Capex 周期（18-36 个月）。新供给跟不上需求增长。"},
    {"icon": "📋", "title": "真实订单兑现", "desc": "客户开始签长期合同（5-20 年）锁产能。SanDisk 已签 5 个长期供应协议，客户像锁电力一样锁 NAND。"},
    {"icon": "🔄", "title": "认知反转", "desc": "市场从「这玩意还有用吗」转向「这是 AI 必需品」。这是估值倍数扩张的核心。"},
    {"icon": "💰", "title": "市值起点低", "desc": "拆分、被忽视、小盘。SanDisk 拆分时市值 $70 亿，一年后 $2000 亿。市值天花板决定回报上限。"},
]


# ============================================================
# 我的核心观点
# ============================================================
MY_VIEW = {
    "headline": "AI 主线已轮动到「下一波稀缺资源」拐点",
    "summary": (
        "算力（NVDA）→ 网络（COHR）→ 电力（GEV/VST）→ 内存（SK Hynix）→ 存储（SanDisk）"
        "都已发生。下一波最可能的「百倍候选」在：水冷、稀土、铀、SMR、AI 数据。"
    ),
    "thesis": [
        ("✅", "确定性最高", "AI 电力链（PWR/MTZ/ETN/VRT），但已涨过较多"),
        ("🌟", "最值得潜伏", "水（Xylem）、稀土（MP）、铀（CCJ）、SMR（BWXT）"),
        ("⚠️", "需要警惕", "光通信 5/7 刚回调（COHR/LITE/中际旭创），可能扩散"),
        ("🎯", "高赔率/事件驱动", "Tesla 看 Robotaxi 兑现、Apple 看 WWDC、Intel 看 Foundry"),
        ("🔬", "早期+故事股", "Tempus（医疗）、Recursion（药物）、UiPath（Agentic）、Reddit（数据）"),
    ],
}


# ============================================================
# 标的快速分级
# ============================================================
def stock_signal(rec):
    """简化判断：基于 AI 关联度+研究状态返回简单的视觉信号。"""
    ar = rec.get("ai_relevance", "")
    st = rec.get("status", "")
    if "极强" in ar:
        return ("🔥", "极强", "red")
    if "强" in ar:
        return ("⚡", "强", "orange")
    if "中" in ar:
        return ("💧", "中", "blue")
    if "弱" in ar:
        return ("🛡️", "防御", "gray")
    if "实现层" in ar:
        return ("🧩", "组件", "purple")
    return ("·", "其他", "gray")


def yahoo_link(code, market):
    if "美股" in market:
        return f'<a href="https://finance.yahoo.com/quote/{code}" target="_blank" class="text-blue-600 hover:underline font-mono">{code} ↗</a>'
    if "A股" in market:
        prefix = "sz" if code.startswith(("0", "1", "2", "3")) else "sh"
        return f'<a href="https://quote.eastmoney.com/{prefix}{code}.html" target="_blank" class="text-blue-600 hover:underline font-mono">{code} ↗</a>'
    if "港股" in market:
        clean_code = code.split(".")[0]
        return f'<a href="https://www.aastocks.com/sc/stocks/quote/detailquote.aspx?symbol={clean_code}" target="_blank" class="text-blue-600 hover:underline font-mono">{code} ↗</a>'
    if "其他" in market or "韩股" in market:
        clean_code = code.split(".")[0]
        return f'<a href="https://finance.yahoo.com/quote/{clean_code}.KS" target="_blank" class="text-blue-600 hover:underline font-mono">{code} ↗</a>'
    return f'<span class="font-mono">{code}</span>'


# ============================================================
# HTML 模板
# ============================================================

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 投资研究 Dashboard</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Helvetica Neue", sans-serif; -webkit-font-smoothing: antialiased; }
  .field-block { white-space: pre-wrap; line-height: 1.65; }
  details > summary { cursor: pointer; user-select: none; list-style: none; outline: none; }
  details > summary::-webkit-details-marker { display: none; }
  details > summary .arrow::before { content: "▶"; display: inline-block; transition: transform 0.2s; font-size: 0.7em; margin-right: 4px; color: #94a3b8; }
  details[open] > summary .arrow::before { transform: rotate(90deg); }
  .gradient-bg { background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); }
  .glow-card { box-shadow: 0 4px 20px rgba(139, 92, 246, 0.15); }
  .timeline-line::before { content: ""; position: absolute; left: 14px; top: 24px; bottom: 24px; width: 2px; background: linear-gradient(to bottom, #06b6d4, #8b5cf6, #f59e0b); }
  .ticker-badge { display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 6px; font-size: 12px; font-family: monospace; background: #f1f5f9; color: #334155; margin: 2px; }
  .ticker-badge:hover { background: #e2e8f0; }
  /* 主题色卡片 */
  .theme-amber { border-left: 4px solid #f59e0b; }
  .theme-red { border-left: 4px solid #ef4444; }
  .theme-emerald { border-left: 4px solid #10b981; }
  .theme-violet { border-left: 4px solid #8b5cf6; background: linear-gradient(to right, #faf5ff 0%, white 30%); }
  .theme-blue { border-left: 4px solid #3b82f6; }
  .theme-indigo { border-left: 4px solid #6366f1; }
  .theme-fuchsia { border-left: 4px solid #d946ef; }
  .theme-pink { border-left: 4px solid #ec4899; }
  .theme-slate { border-left: 4px solid #64748b; }
  .theme-stone { border-left: 4px solid #78716c; }
  .pulse-dot { animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
</style>
</head>
<body class="bg-gradient-to-b from-slate-50 to-white" style="padding-left: 14rem;">

<!-- ============ 左侧 sidebar 导航（4 主入口 + 子项展开 + 次级 + 工具） ============ -->
<!-- 信息架构：why（投资框架）+ what（今天/发现/验证）两层 · 产业链地图取代原 主题分组 + 产业链全景 -->
<aside id="tab-nav" class="fixed left-0 top-0 h-screen w-56 bg-white border-r border-slate-200 shadow-sm overflow-y-auto z-50">
  <div class="p-4">
    <div class="text-base font-bold text-slate-900 mb-6 flex items-center gap-2">
      <span>📊</span><span>AI 投资</span>
    </div>

    <!-- 📚 投资框架（why · 方法论开屏第一眼 · 原"投资观点"）-->
    <div class="mb-4">
      <a href="#overview" data-tab="overview" class="tab-link block px-2 py-1 text-base font-bold text-slate-800 hover:text-violet-600 hover:bg-violet-50 rounded transition">📚 投资框架</a>
    </div>

    <!-- 🗂️ 我的池子 = 持仓 + 自选股 + 今日 Top -->
    <div class="mb-4">
      <div class="text-base font-bold text-slate-800 mb-2 px-2">🗂️ 我的池子</div>
      <a href="#portfolio" data-tab="portfolio" class="tab-link block pl-7 pr-3 py-1.5 text-[13px] text-slate-600 hover:text-violet-600 hover:bg-violet-50 rounded transition">💼 我的持仓</a>
      <a href="#watchlist-edit" data-tab="init-config" class="tab-link block pl-7 pr-3 py-1.5 text-[13px] text-slate-600 hover:text-violet-600 hover:bg-violet-50 rounded transition">⭐ 自选股</a>
      <a href="#picks" data-tab="picks" class="tab-link block pl-7 pr-3 py-1.5 text-[13px] text-slate-600 hover:text-violet-600 hover:bg-violet-50 rounded transition">🔝 自选股·今日 Top</a>
    </div>

    <!-- 🧠 AI 助手 = AI 推荐(个股) + AI 组合方案(组合层 含模拟回测) -->
    <div class="mb-4">
      <div class="text-base font-bold text-slate-800 mb-2 px-2">🧠 AI 助手</div>
      <a href="#discovery" data-tab="discovery" class="tab-link block pl-7 pr-3 py-1.5 text-[13px] text-slate-600 hover:text-violet-600 hover:bg-violet-50 rounded transition">🤖 AI 推荐</a>
      <a href="#backtest" data-tab="backtest" class="tab-link block pl-7 pr-3 py-1.5 text-[13px] text-slate-600 hover:text-violet-600 hover:bg-violet-50 rounded transition">🎼 AI 组合方案</a>
    </div>

    <!-- 🔬 深度研究 = 估值 + 产业链 + 反向审查 + 专业分析 -->
    <div class="mb-4">
      <div class="text-base font-bold text-slate-800 mb-2 px-2">🔬 深度研究</div>
      <a href="#valuation" data-tab="valuation" class="tab-link block pl-7 pr-3 py-1.5 text-[13px] text-slate-600 hover:text-violet-600 hover:bg-violet-50 rounded transition">📈 估值视角</a>
      <a href="#chain" data-tab="chain" class="tab-link block pl-7 pr-3 py-1.5 text-[13px] text-slate-600 hover:text-violet-600 hover:bg-violet-50 rounded transition">🌳 产业链地图</a>
      <a href="#audit" data-tab="audit" class="tab-link block pl-7 pr-3 py-1.5 text-[13px] text-slate-600 hover:text-violet-600 hover:bg-violet-50 rounded transition">🛡 反向审查</a>
      <a href="#professional" data-tab="professional" class="tab-link block pl-7 pr-3 py-1.5 text-[13px] text-slate-600 hover:text-violet-600 hover:bg-violet-50 rounded transition">📊 专业分析</a>
    </div>

    <hr class="my-4 border-slate-200">

    <!-- ⚙️ 管理：自选股编辑入口（DuckDB 权威 · 飞书已废） -->
    <div class="mb-4">
      <div class="text-base font-bold text-slate-800 mb-2 px-2">⚙️ 管理</div>
      <a href="#init-config" data-tab="init-config" class="tab-link block pl-7 pr-3 py-1.5 text-[13px] text-slate-600 hover:text-violet-600 hover:bg-violet-50 rounded transition">🛠️ 初始化配置</a>
      <a href="#upgrade" data-tab="upgrade" class="tab-link block pl-7 pr-3 py-1.5 text-[13px] text-slate-500 hover:text-violet-600 hover:bg-violet-50 rounded transition">💰 升级建议</a>
      <a href="#about" data-tab="about" class="tab-link block pl-7 pr-3 py-1.5 text-[13px] text-slate-500 hover:text-violet-600 hover:bg-violet-50 rounded transition">🧭 系统介绍</a>
    </div>

    <hr class="my-4 border-slate-200">

    <!-- 底部信息：数据源 + 更新时间 -->
    <div class="text-xs text-slate-500 px-2 space-y-2">
      <div class="flex items-center justify-between">
        <span title="数据源">数据源</span>
        <span class="text-[10px] font-mono px-2 py-0.5 rounded border border-emerald-300 bg-emerald-50 text-emerald-800" title="2026-05-11 起 DuckDB 是 single source of truth · 飞书仅作通知">DuckDB</span>
      </div>
      <div class="text-[10px] text-slate-400">{UPDATE_TIME}</div>
      <div class="text-[10px] text-slate-400 leading-snug pt-2 border-t border-slate-100">⚠️ 不构成投资建议<br>崩盘期 alpha = -9.77%</div>
    </div>
  </div>
</aside>

<!-- ============ HERO ============ -->
<header id="hero" class="gradient-bg text-white">
  <div class="max-w-7xl mx-auto px-6 py-10">
    <div class="flex items-center gap-3 mb-3">
      <span class="bg-violet-500/20 text-violet-300 text-xs font-bold px-3 py-1 rounded-full">AI 投资研究 · 资深分析员视角</span>
      <span class="text-slate-400 text-sm">数据更新 {UPDATE_TIME}</span>
    </div>
    <h1 class="text-4xl md:text-5xl font-bold mb-3 leading-tight">{HEADLINE}</h1>
    <p class="text-lg text-slate-300 max-w-4xl mb-6">{SUMMARY}</p>
    <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
      <div class="bg-white/5 backdrop-blur rounded-lg p-4 border border-white/10">
        <div class="text-3xl font-bold text-cyan-300">{TOTAL}</div>
        <div class="text-sm text-slate-400 mt-1">追踪股票总数</div>
      </div>
      <div class="bg-white/5 backdrop-blur rounded-lg p-4 border border-white/10">
        <div class="text-3xl font-bold text-emerald-300">{HIGH_AI}</div>
        <div class="text-sm text-slate-400 mt-1">AI 关联强 / 极强</div>
      </div>
      <div class="bg-white/5 backdrop-blur rounded-lg p-4 border border-white/10">
        <div class="text-3xl font-bold text-amber-300">{US_COUNT}</div>
        <div class="text-sm text-slate-400 mt-1">美股</div>
      </div>
      <div class="bg-white/5 backdrop-blur rounded-lg p-4 border border-white/10">
        <div class="text-3xl font-bold text-rose-300">{CN_COUNT}</div>
        <div class="text-sm text-slate-400 mt-1">中港 / 其他</div>
      </div>
    </div>
    <div class="mt-6 bg-rose-500/10 border-l-4 border-rose-400 p-3 rounded text-sm text-rose-100">
      ⚠️ 本看板仅作研究参考，**不构成任何买卖建议**。所有数据来自公开市场信息，可能滞后或错误。投资决策需自行判断。
    </div>
  </div>
</header>

<!-- ============ 💀 压力测试：崩盘期表现（诚实暴露）============ -->
<section id="stress-test" class="max-w-7xl mx-auto px-6 py-10">
  <div class="bg-gradient-to-br from-rose-50 to-amber-50 border-2 border-rose-300 rounded-2xl p-6">
    <div class="flex items-center justify-between mb-4">
      <div>
        <h2 class="text-2xl font-bold text-rose-900">💀 压力测试 — 历史崩盘期实测</h2>
        <p class="text-sm text-slate-700 mt-1">v6 模型在 <strong>4 个真实历史崩盘 regime</strong> 中的抗跌表现 · 平均 DD alpha <strong class="text-rose-700">-9.77%</strong> · 抗跌仅 <strong class="text-rose-700">1/4</strong></p>
      </div>
      <a href="docs/STRESS_TEST_REPORT.md" target="_blank" class="text-xs px-3 py-1.5 rounded bg-white border border-rose-300 text-rose-700 hover:bg-rose-50 transition whitespace-nowrap">📄 详细报告</a>
    </div>
    <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
      <div class="bg-white rounded-xl p-4 border-l-4 border-rose-500 shadow-sm">
        <div class="text-xs text-slate-500">2008 雷曼金融危机</div>
        <div class="text-3xl font-bold text-rose-700 mt-1">-9.72%</div>
        <div class="text-xs text-slate-600 mt-1">DD alpha · 🔴 放大版 SPY</div>
        <div class="text-[10px] text-slate-500 mt-2 font-mono">2008-09 → 2009-03</div>
      </div>
      <div class="bg-white rounded-xl p-4 border-l-4 border-amber-500 shadow-sm">
        <div class="text-xs text-slate-500">2018 贸易战 + 加息</div>
        <div class="text-3xl font-bold text-amber-700 mt-1">-2.79%</div>
        <div class="text-xs text-slate-600 mt-1">DD alpha · 🟡 中性</div>
        <div class="text-[10px] text-slate-500 mt-2 font-mono">2018-10 → 2018-12</div>
      </div>
      <div class="bg-white rounded-xl p-4 border-l-4 border-emerald-500 shadow-sm">
        <div class="text-xs text-slate-500">2020 新冠崩盘</div>
        <div class="text-3xl font-bold text-emerald-700 mt-1">+0.82%</div>
        <div class="text-xs text-slate-600 mt-1">DD alpha · 🟢 唯一抗跌</div>
        <div class="text-[10px] text-slate-500 mt-2 font-mono">2020-02 → 2020-03</div>
      </div>
      <div class="bg-white rounded-xl p-4 border-l-4 border-rose-500 shadow-sm">
        <div class="text-xs text-slate-500">2022 加息熊市</div>
        <div class="text-3xl font-bold text-rose-700 mt-1">-27.38%</div>
        <div class="text-xs text-slate-600 mt-1">DD alpha · 🔴 跌得更惨</div>
        <div class="text-[10px] text-slate-500 mt-2 font-mono">2022-01 → 2022-10</div>
      </div>
    </div>
    <p class="text-xs text-slate-600 mt-4">
      <strong>读法</strong>：DD alpha > 0 = 组合最大回撤比 SPY 小（抗跌）；< 0 = 跌得更惨。
      <strong>正确用法</strong>：把这些数字当作"模型有哪些系统性弱点"的诚实自评，<strong class="text-rose-700">不是性能广告</strong>。
      v7 防御层（VIX/200MA/-15% 止损）就是针对 2008/2022 这类回撤设计的，预计能把 DD alpha 拉回 -3% ~ +2% 区间。
    </p>
  </div>
</section>

<!-- ============ 我的核心观点 ============ -->
<!-- ============ 🧭 系统介绍（新人入口 · 资金分配方法论） ============ -->
<section id="about" class="max-w-7xl mx-auto px-6 py-10">
  <div class="flex items-center gap-3 mb-2">
    <span class="text-3xl">🧭</span>
    <h2 class="text-3xl font-bold text-slate-900">系统介绍</h2>
  </div>
  <p class="text-slate-600 mb-8 max-w-3xl">这是一份给"完全新人"的方法论说明。先讲<strong>钱怎么分</strong>，<strong>怎么推荐</strong>留待后续补充。</p>

  <!-- 一、两条资金线 -->
  <div class="bg-white rounded-2xl shadow-sm border border-slate-200 p-6 mb-6">
    <h3 class="text-xl font-bold text-slate-800 mb-4">一、两条资金线分别代表什么</h3>
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div class="bg-gradient-to-br from-emerald-50 to-teal-50 border border-emerald-200 rounded-xl p-5">
        <div class="text-xs font-bold text-emerald-700 uppercase tracking-wider mb-1">进场本金</div>
        <div class="text-3xl font-bold text-emerald-900 mb-2" data-cfg="total_capital">¥500,000</div>
        <div class="text-sm text-slate-700 leading-relaxed">
          代码里叫 <code class="text-xs bg-slate-100 px-1.5 py-0.5 rounded font-mono">TOTAL_CAPITAL</code>。
          整个组合按这个数算每只股票该买多少 —— 系统给出的所有金额建议，都是 50 万的某个百分比。
        </div>
      </div>
      <div class="bg-gradient-to-br from-rose-50 to-pink-50 border border-rose-200 rounded-xl p-5">
        <div class="text-xs font-bold text-rose-700 uppercase tracking-wider mb-1">撤退红线</div>
        <div class="text-3xl font-bold text-rose-900 mb-2" data-cfg="stoploss_line">¥300,000</div>
        <div class="text-sm text-slate-700 leading-relaxed">
          代码里叫 <code class="text-xs bg-slate-100 px-1.5 py-0.5 rounded font-mono">STOPLOSS_LINE</code>。
          组合市值跌到这条线就<strong>强制清仓</strong>，最大亏损封顶在 -40%，避免"赌不上头"越扛越深。
        </div>
      </div>
    </div>
    <div class="mt-4 text-sm text-slate-600 bg-slate-50 border-l-4 border-slate-400 p-3 rounded">
      💡 朴素理解：拿 50 万进场，最坏情况只允许自己亏 20 万；亏到 30 万剩下的就必须落袋，不再扛。这是<strong>硬规则</strong>。
    </div>
  </div>

  <!-- 二、每只股票怎么分配 -->
  <div class="bg-white rounded-2xl shadow-sm border border-slate-200 p-6 mb-6">
    <h3 class="text-xl font-bold text-slate-800 mb-4">二、每只股票分多少钱 —— 拆成 3 步</h3>

    <!-- Step 1 -->
    <div class="border-l-4 border-violet-400 pl-4 mb-5">
      <div class="flex items-baseline gap-2 mb-2">
        <span class="text-xs font-bold text-violet-700 bg-violet-100 px-2 py-0.5 rounded">STEP 1</span>
        <span class="font-bold text-slate-800">先选谁进组合（选股层）</span>
      </div>
      <p class="text-sm text-slate-700 leading-relaxed">
        系统先用 <strong>F-Score 因子打分</strong>，从几百只候选里挑出 <code class="text-xs bg-slate-100 px-1.5 py-0.5 rounded font-mono">TOP_N = 12</code> 只股票。这一步<strong>不分钱</strong>，只是把 12 个名字交给下一步。
      </p>
    </div>

    <!-- Step 2 -->
    <div class="border-l-4 border-amber-400 pl-4 mb-5">
      <div class="flex items-baseline gap-2 mb-2">
        <span class="text-xs font-bold text-amber-700 bg-amber-100 px-2 py-0.5 rounded">STEP 2</span>
        <span class="font-bold text-slate-800">在 12 只里"切蛋糕"（仓位层 · 核心）</span>
      </div>
      <p class="text-sm text-slate-700 leading-relaxed mb-3">
        采用 <strong>Markowitz 最大夏普比组合优化</strong>（1952 诺奖理论），并叠加几条工程上的硬约束：
      </p>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-2 mb-3 text-sm">
        <div class="bg-slate-50 rounded px-3 py-2"><strong>现金底线 5%</strong> · 留 <span data-cfg-pct="0.05">¥25,000</span> 应急/加仓</div>
        <div class="bg-slate-50 rounded px-3 py-2"><strong>单只下限 2%</strong> · 不到 2% 就不买（避免散碎仓位）</div>
        <div class="bg-slate-50 rounded px-3 py-2"><strong>单只上限 15%</strong> · 最多 <span data-cfg-pct="0.15">¥75,000</span>（避免单票爆雷干翻账户）</div>
        <div class="bg-slate-50 rounded px-3 py-2"><strong>流动性约束</strong> · 单日交易额 ≤ 该票日均成交量的 5%</div>
      </div>
      <p class="text-sm text-slate-700 leading-relaxed mb-2"><strong>"最优"的标准是什么？</strong> 用<strong>夏普比</strong>衡量：</p>
      <div class="bg-slate-900 text-slate-100 rounded-lg px-4 py-3 font-mono text-xs leading-relaxed">
              组合预期收益 − 无风险利率(4.5%)<br>
夏普比 = ────────────────────────────────────<br>
                    组合波动率
      </div>
      <p class="text-sm text-slate-700 leading-relaxed mt-3">
        翻译成人话：<strong>每承担 1 单位风险，能换来多少超额收益</strong>。系统跑 <strong>20,000 次蒙特卡洛模拟</strong>（用 Dirichlet 分布随机生成 20000 套权重组合），挑出夏普比最高的那一套就是最终分配。
      </p>
    </div>

    <!-- Step 3 -->
    <div class="border-l-4 border-cyan-400 pl-4">
      <div class="flex items-baseline gap-2 mb-2">
        <span class="text-xs font-bold text-cyan-700 bg-cyan-100 px-2 py-0.5 rounded">STEP 3</span>
        <span class="font-bold text-slate-800">把权重换成钱</span>
      </div>
      <p class="text-sm text-slate-700 leading-relaxed mb-3">假设第 2 步算出来：AAPL 12%、NVDA 10%、TSM 8%、…、现金 5%，那实际金额就是：</p>
      <div class="grid grid-cols-1 md:grid-cols-3 gap-2 text-sm">
        <div class="bg-cyan-50 border border-cyan-200 rounded px-3 py-2"><strong>AAPL</strong> = 500,000 × 12% = <strong class="text-cyan-700">¥60,000</strong></div>
        <div class="bg-cyan-50 border border-cyan-200 rounded px-3 py-2"><strong>NVDA</strong> = 500,000 × 10% = <strong class="text-cyan-700">¥50,000</strong></div>
        <div class="bg-cyan-50 border border-cyan-200 rounded px-3 py-2"><strong>现金</strong> = 500,000 × 5% = <strong class="text-cyan-700">¥25,000</strong></div>
      </div>
    </div>
  </div>

  <!-- 三、背后的理论 -->
  <div class="bg-white rounded-2xl shadow-sm border border-slate-200 p-6 mb-6">
    <h3 class="text-xl font-bold text-slate-800 mb-4">三、背后用了哪些理论模型</h3>
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr class="border-b-2 border-slate-200 text-left text-slate-600">
            <th class="py-2 pr-4">用到的东西</th>
            <th class="py-2 pr-4">解决什么问题</th>
            <th class="py-2">出处</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          <tr><td class="py-2.5 pr-4 font-medium">Markowitz 均值-方差优化</td><td class="py-2.5 pr-4 text-slate-700">同样的收益，怎么把风险压到最低</td><td class="py-2.5 text-slate-500 text-xs">Markowitz 1952（诺奖）</td></tr>
          <tr><td class="py-2.5 pr-4 font-medium">最大夏普比</td><td class="py-2.5 pr-4 text-slate-700">风险调整后收益最大化</td><td class="py-2.5 text-slate-500 text-xs">Sharpe 1966</td></tr>
          <tr><td class="py-2.5 pr-4 font-medium">Ledoit-Wolf 协方差收缩</td><td class="py-2.5 pr-4 text-slate-700">历史数据少时，避免相关性估计跑偏</td><td class="py-2.5 text-slate-500 text-xs">Ledoit & Wolf 2003</td></tr>
          <tr><td class="py-2.5 pr-4 font-medium">蒙特卡洛随机搜索</td><td class="py-2.5 pr-4 text-slate-700">解上面这个带约束的非凸优化</td><td class="py-2.5 text-slate-500 text-xs">工程惯例</td></tr>
          <tr><td class="py-2.5 pr-4 font-medium">15% 单票硬顶</td><td class="py-2.5 pr-4 text-slate-700">不用 Kelly 公式那种激进满仓</td><td class="py-2.5 text-slate-500 text-xs">风控经验值</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 四、新手必须知道的 3 个坑 -->
  <div class="bg-gradient-to-br from-amber-50 to-orange-50 border border-amber-200 rounded-2xl p-6 mb-6">
    <h3 class="text-xl font-bold text-amber-900 mb-4">⚠️ 四、新手必须知道的 3 个坑</h3>
    <ol class="space-y-3 text-sm text-slate-800">
      <li class="flex gap-3">
        <span class="font-bold text-amber-700 flex-shrink-0">1.</span>
        <div><strong>这不是预测，是"如果历史重演的最优分法"</strong> —— μ 和 Σ 都是用过去 252 个交易日估的，遇到 2020 年 3 月那种黑天鹅会失灵。</div>
      </li>
      <li class="flex gap-3">
        <span class="font-bold text-amber-700 flex-shrink-0">2.</span>
        <div><strong>15% 上限不是拍脑袋</strong> —— 哪怕系统说某只票期望收益爆表，最多也只能给它 <span data-cfg-pct="0.15">¥75,000</span>，因为单只满仓爆雷会让你直接撞到止损线。</div>
      </li>
      <li class="flex gap-3">
        <span class="font-bold text-amber-700 flex-shrink-0">3.</span>
        <div><strong>每周一才 rebalance</strong> —— 不是每天换仓，避免交易成本（4.8 bps）吃掉收益。</div>
      </li>
    </ol>
  </div>

  <!-- 五、用了哪些数据 + 做了哪些分析 -->
  <div class="bg-white rounded-2xl shadow-sm border border-slate-200 p-6 mb-6">
    <h3 class="text-xl font-bold text-slate-800 mb-2">五、用了哪些数据 + 做了哪些分析</h3>
    <p class="text-sm text-slate-600 mb-5">前面四节讲"钱怎么分"，这一节讲"数据从哪来、做什么计算"。系统覆盖 4 个市场，用 7 个免费源，做 8 个维度的分析。</p>

    <!-- 5.1 覆盖股市 -->
    <div class="mb-5">
      <h4 class="font-bold text-slate-700 mb-2">5.1 覆盖股市</h4>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b-2 border-slate-200 text-left text-slate-600">
              <th class="py-2 pr-4">市场</th>
              <th class="py-2 pr-4">代码识别</th>
              <th class="py-2">数据主源</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            <tr><td class="py-2 pr-4 font-medium">美股</td><td class="py-2 pr-4 text-slate-700">裸 ticker（AAPL、NVDA）</td><td class="py-2 text-slate-700">yfinance + SEC EDGAR + FMP + Finnhub</td></tr>
            <tr><td class="py-2 pr-4 font-medium">A 股（沪深京）</td><td class="py-2 pr-4 text-slate-700">.SS / .SZ / .BJ</td><td class="py-2 text-slate-700">akshare + baostock 二源</td></tr>
            <tr><td class="py-2 pr-4 font-medium">港股</td><td class="py-2 pr-4 text-slate-700">.HK</td><td class="py-2 text-slate-700">akshare</td></tr>
            <tr><td class="py-2 pr-4 font-medium">宏观/指数/商品</td><td class="py-2 pr-4 text-slate-700">—</td><td class="py-2 text-slate-700">yfinance + akshare</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 5.2 数据源 -->
    <div class="mb-5">
      <h4 class="font-bold text-slate-700 mb-2">5.2 数据源（7 个免费源在用）</h4>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b-2 border-slate-200 text-left text-slate-600">
              <th class="py-2 pr-4">源</th>
              <th class="py-2 pr-4">拿什么</th>
              <th class="py-2">凭证</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            <tr><td class="py-2 pr-4 font-medium">yfinance</td><td class="py-2 pr-4 text-slate-700">美股 K 线 / 市值 / PE / SPY 期权链 PCR</td><td class="py-2 text-slate-500 text-xs">无</td></tr>
            <tr><td class="py-2 pr-4 font-medium">akshare</td><td class="py-2 pr-4 text-slate-700">A 股财报 / 北向 / 龙虎榜 / IPO / 政策事件</td><td class="py-2 text-slate-500 text-xs">无</td></tr>
            <tr><td class="py-2 pr-4 font-medium">baostock</td><td class="py-2 pr-4 text-slate-700">A 股二源校验（akshare 失败时顶上）</td><td class="py-2 text-slate-500 text-xs">无</td></tr>
            <tr><td class="py-2 pr-4 font-medium">SEC EDGAR</td><td class="py-2 pr-4 text-slate-700">13F 大佬持仓（11 家机构季度）</td><td class="py-2 text-slate-500 text-xs">User-Agent</td></tr>
            <tr><td class="py-2 pr-4 font-medium">FMP（免费档）</td><td class="py-2 pr-4 text-slate-700">财报 / DCF / 分析师预期</td><td class="py-2 text-slate-500 text-xs">FMP_API_KEY，250/天</td></tr>
            <tr><td class="py-2 pr-4 font-medium">Finnhub（免费档）</td><td class="py-2 pr-4 text-slate-700">美股新闻 / 内部人交易 / 评级</td><td class="py-2 text-slate-500 text-xs">FINNHUB_API_KEY</td></tr>
            <tr><td class="py-2 pr-4 font-medium">Google Trends</td><td class="py-2 pr-4 text-slate-700">搜索热度（情绪面）</td><td class="py-2 text-slate-500 text-xs">无（限流）</td></tr>
          </tbody>
        </table>
      </div>
      <p class="text-xs text-slate-500 mt-2"><strong>已退役</strong>：OpenBB（2026-05-11 拆，源码只是 yfinance 包装层）。<strong>待充值激活</strong>：Tushare / Anthropic / FMP Starter。</p>
    </div>

    <!-- 5.3 分析维度 -->
    <div class="mb-5">
      <h4 class="font-bold text-slate-700 mb-2">5.3 分析维度（8 个）</h4>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b-2 border-slate-200 text-left text-slate-600">
              <th class="py-2 pr-4">维度</th>
              <th class="py-2">做什么</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            <tr><td class="py-2 pr-4 font-medium">基本面</td><td class="py-2 text-slate-700">财报深度 / 远期 PE / PEG / 三档 DCF / 同业对比 / F-Score 9 项</td></tr>
            <tr><td class="py-2 pr-4 font-medium">技术/价格</td><td class="py-2 text-slate-700">历史 K 线（274 天）/ MA20-60-200 / 最大回撤 / 趋势 emoji 7 档</td></tr>
            <tr><td class="py-2 pr-4 font-medium">资金流</td><td class="py-2 text-slate-700">13F 季度持仓 / A 股北向 / 龙虎榜 / 美股内部人</td></tr>
            <tr><td class="py-2 pr-4 font-medium">量化打分</td><td class="py-2 text-slate-700">美股 5 因子（Value/Quality/Momentum/Size/残差波动）+ A 股因子 + IC 校准 + 行业中性化</td></tr>
            <tr><td class="py-2 pr-4 font-medium">防御 regime</td><td class="py-2 text-slate-700">VIX / 200MA / 宏观 / PCR 四闸门 → NONE/LOW/HIGH/CRITICAL，每 15 分钟巡检</td></tr>
            <tr><td class="py-2 pr-4 font-medium">事件日历</td><td class="py-2 text-slate-700">earnings / 政策 / IPO 打新 / 电话会议</td></tr>
            <tr><td class="py-2 pr-4 font-medium">组合优化</td><td class="py-2 text-slate-700">Ledoit-Wolf 协方差收缩 + 相关性闸门 + A 股涨跌停/ST/停牌约束 + NAV 跟踪</td></tr>
            <tr><td class="py-2 pr-4 font-medium">数据审计</td><td class="py-2 text-slate-700">跨源 cross-check（yfinance vs akshare vs SEC）+ CONFLICT 比例闸门 + 因子 IC 闸门</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 5.4 输出口 -->
    <div>
      <h4 class="font-bold text-slate-700 mb-2">5.4 输出口</h4>
      <p class="text-sm text-slate-700 leading-relaxed mb-2">每天 08:30 自动推 <strong>飞书早报卡片</strong>，5 个 section：</p>
      <ol class="text-sm text-slate-700 list-decimal list-inside space-y-1">
        <li>经济日历（持仓 earnings + 政策）</li>
        <li>当前建议组合 + 60 天趋势</li>
        <li>NAV 净值 + Sharpe（"AI 有没有用"）</li>
        <li>红旗（防御信号升档时显示）</li>
        <li>今天做什么</li>
      </ol>
      <p class="text-xs text-slate-500 mt-3">中间产物存 <code class="bg-slate-100 px-1 py-0.5 rounded font-mono text-xs">data/latest/*.json</code>，历史快照存 <code class="bg-slate-100 px-1 py-0.5 rounded font-mono text-xs">data/snapshots/*.json</code>。完整技术清单见 <code class="bg-slate-100 px-1 py-0.5 rounded font-mono text-xs">docs/关于.md</code>。</p>
    </div>
  </div>
</section>

<section id="thesis" class="max-w-7xl mx-auto px-6 py-10">
  <h2 class="text-2xl font-bold text-slate-800 mb-6">📌 我的核心观点（5 条）</h2>
  <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-3">
    {THESIS_CARDS}
  </div>
</section>

<!-- ============ 百倍股的 5 个共同条件 ============ -->
<section id="hundred-x" class="max-w-7xl mx-auto px-6 py-10 bg-violet-50 rounded-2xl my-6">
  <div class="flex items-center justify-between mb-6">
    <div>
      <h2 class="text-2xl font-bold text-slate-800">🎯 百倍股的 5 个共同条件</h2>
      <p class="text-slate-600 mt-1">SK Hynix +920%（一年）、SanDisk +1200%（一年多）的共同模式</p>
    </div>
    <span class="text-sm text-slate-500">分析框架</span>
  </div>
  <div class="grid grid-cols-1 md:grid-cols-5 gap-4">
    {HUNDRED_X_CARDS}
  </div>
</section>

<!-- ============ AI 主线演进时间轴 ============ -->
<section id="evolution" class="max-w-7xl mx-auto px-6 py-10">
  <h2 class="text-2xl font-bold text-slate-800 mb-2">⏱ AI 主线演进时间轴</h2>
  <p class="text-slate-600 mb-6">钱已经从 GPU 轮动到电力到内存到存储 —— **下一波在哪里**？</p>
  <div class="relative timeline-line bg-white rounded-xl shadow-sm border border-slate-200 p-6">
    {TIMELINE_ITEMS}
  </div>
</section>

<!-- ============ 5 大稀缺资源主题（重点高亮） ============ -->
<section id="scarce" class="max-w-7xl mx-auto px-6 py-10 bg-gradient-to-br from-violet-100 to-fuchsia-50 rounded-2xl my-6 glow-card">
  <div class="flex items-center gap-3 mb-2">
    <span class="text-3xl">💎</span>
    <h2 class="text-2xl font-bold text-violet-900">下一波稀缺资源（重点关注区）</h2>
  </div>
  <p class="text-violet-800 mb-6 max-w-3xl">
    根据 SK Hynix / SanDisk 的历史路径推断，下一个百倍候选最可能在<strong>「冷门→热门」的拐点</strong>。
    以下 5 个方向都已有真实订单兑现，但市场关注度还不充分。
  </p>
  <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-3">
    {SCARCE_THEME_CARDS}
  </div>
</section>

<!-- ============ 关键事件日历 ============ -->
<section id="events" class="max-w-7xl mx-auto px-6 py-10">
  <h2 class="text-2xl font-bold text-slate-800 mb-6">📅 接下来 6 个月关键事件</h2>
  <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
    {EVENT_CARDS}
  </div>
</section>

<!-- ============ 打分规则说明（动态由 scoring_rules_panel_html 渲染） ============ -->
{SCORING_RULES_PANEL}

<!-- ============ 自选股·今日 Top回顾 ============ -->
<section id="picks-review" class="max-w-7xl mx-auto px-6 py-10 bg-gradient-to-br from-amber-50 to-orange-50 rounded-2xl my-6">
  <div class="flex items-start justify-between mb-4 gap-4">
    <div class="flex-1">
      <div class="flex items-center gap-3 mb-1">
        <span class="text-3xl">⭐</span>
        <h2 class="text-2xl font-bold text-slate-900">自选股·今日 Top</h2>
      </div>
      <p class="text-slate-700 text-sm">
        <strong class="text-violet-700">顶部 = 今日 top picks</strong>（系统综合评分最高的几只 · 飞书早安简报也会推送一份）·
        <strong class="text-slate-600">下方 = 30 天历史回顾</strong>（检验系统打分是否真的越高越涨）
      </p>
    </div>
    <div id="picks-summary" class="text-right flex-shrink-0"></div>
  </div>

  <!-- 🌟 今日 top picks 横幅（最新一批入选）-->
  <div class="mb-6 bg-gradient-to-r from-violet-100 to-fuchsia-50 border-2 border-violet-300 rounded-xl p-5">
    <div class="flex items-center gap-2 mb-3">
      <span class="text-2xl">🌟</span>
      <h3 class="text-lg font-bold text-violet-900">今日 top picks</h3>
      <span id="picks-today-meta" class="text-xs text-violet-700"></span>
    </div>
    <div id="picks-today-list" class="grid grid-cols-1 md:grid-cols-3 gap-3"></div>
  </div>

  <!-- 历史回顾分隔 -->
  <div class="border-t border-amber-200 pt-5 mb-3">
    <h3 class="text-base font-semibold text-slate-700">📊 30 天历史回顾 — 系统打分准不准</h3>
    <p class="text-xs text-slate-500 mt-1">关键看下方"⭐ 评分 vs 实际表现"是否单调（⭐⭐⭐ 平均涨幅 > ⭐⭐ > ⭐）</p>
  </div>

  <!-- 整体统计 -->
  <div id="picks-stats" class="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4"></div>

  <!-- 评分 vs 实际 + 主题表现 -->
  <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
    <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
      <h3 class="text-sm font-semibold text-slate-700 mb-2">⭐ 评分 vs 实际表现</h3>
      <p class="text-xs text-slate-500 mb-2">⭐⭐⭐ 是否真的更准？</p>
      <div id="picks-by-rating"></div>
    </div>
    <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
      <h3 class="text-sm font-semibold text-slate-700 mb-2">🗂 主题表现</h3>
      <p class="text-xs text-slate-500 mb-2">哪类主题最准</p>
      <div id="picks-by-theme"></div>
    </div>
  </div>

  <!-- 表现 Top / Bottom -->
  <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
    <div class="bg-white rounded-xl shadow-sm border-2 border-emerald-300 p-4">
      <h3 class="text-sm font-semibold text-emerald-700 mb-2">🚀 选股表现 Top 5</h3>
      <div id="picks-top" class="space-y-1"></div>
    </div>
    <div class="bg-white rounded-xl shadow-sm border-2 border-rose-300 p-4">
      <h3 class="text-sm font-semibold text-rose-700 mb-2">📉 选股表现 Bottom 5</h3>
      <div id="picks-bottom" class="space-y-1"></div>
    </div>
  </div>
</section>

{AUDIT_PANEL}

<!-- ============ 🔍 AI 推荐 Tab ============ -->
<section id="discovery" class="max-w-7xl mx-auto px-6 py-10 bg-gradient-to-br from-sky-50 to-indigo-50 rounded-2xl my-6">
  <div class="flex items-center gap-3 mb-2">
    <span class="text-3xl">🔍</span>
    <h2 class="text-2xl font-bold text-slate-900">AI 推荐 — 自选股之外的因子高分股</h2>
  </div>
  <p class="text-slate-700 mb-3 max-w-3xl">
    扫描 SOXX / IGM / IRBO / BAI 四个 ETF 的所有成分股（半导体 + 拓展科技 + AI 主题），
    跑同一套学术因子模型（Piotroski + 12-1 动量 + PEAD + 分析师上修），
    找出 <strong>不在你 自选股里</strong> 但综合得分前列的标的。
    <strong class="text-rose-600">仅缩小搜索空间，研究判断仍需你来做</strong>。
  </p>
  <div id="discovery-meta" class="text-xs text-slate-500 mb-4"></div>

  <!-- 2026-05-11 PM: 算法准确度面板(基于历史推荐回测) -->
  <div id="discovery-accuracy" class="mb-6"></div>

  <!-- 2026-05-11 PM: 顶部 sub-tab 切换(今日候选 / 推荐历史) -->
  <div class="flex items-center gap-1 mb-4 border-b border-slate-200">
    <button onclick="switchDiscoveryView('today')" id="disc-tab-today"
      class="px-4 py-2 text-sm font-medium border-b-2 border-violet-500 text-violet-600 transition">
      🔍 今日候选
    </button>
    <button onclick="switchDiscoveryView('history')" id="disc-tab-history"
      class="px-4 py-2 text-sm font-medium border-b-2 border-transparent text-slate-600 hover:text-violet-600 transition">
      📅 推荐历史
    </button>
    <span class="ml-auto text-xs text-slate-400" id="disc-tab-count"></span>
  </div>

  <!-- 今日候选 view -->
  <div id="disc-view-today">
    <div id="discovery-empty" class="hidden text-center py-12 text-slate-500 bg-white rounded-xl">
      暂无AI 推荐数据(运行 <code class="text-xs bg-slate-200 px-1.5 py-0.5 rounded">python3 scripts/tools/discover_candidates.py</code> 生成)
    </div>
    <div id="discovery-table-wrap" class="bg-white rounded-xl shadow-sm border border-slate-200 overflow-x-auto">
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
          <tr>
            <th class="px-3 py-2 text-left">排名</th>
            <th class="px-3 py-2 text-left">代码</th>
            <th class="px-3 py-2 text-left">名称</th>
            <th class="px-3 py-2 text-left">市场</th>
            <th class="px-3 py-2 text-left">行业</th>
            <th class="px-3 py-2 text-right">综合 z</th>
            <th class="px-3 py-2 text-right">F-Score</th>
            <th class="px-3 py-2 text-right">12-1 动量</th>
            <th class="px-3 py-2 text-right">分析师</th>
            <th class="px-3 py-2 text-right">市值 ($B)</th>
            <th class="px-3 py-2 text-right" title="入选后 5 天涨幅 - 同期 SPY 涨幅">5d α</th>
            <th class="px-3 py-2 text-right" title="入选后 20 天涨幅 - 同期 SPY 涨幅">20d α</th>
            <th class="px-3 py-2 text-left">来源</th>
            <th class="px-3 py-2 text-center">操作</th>
          </tr>
        </thead>
        <tbody id="discovery-table-body" class="divide-y divide-slate-100"></tbody>
      </table>
    </div>
    <p class="text-xs text-slate-500 mt-4">
      💡 <strong>怎么用</strong>:对感兴趣的标的去自选股管理页手动调研(业务 / AI 关联 / 风险),
      通过的加自选 —— 下次 daily_picks 会自动把它纳入排序池。
      <strong class="text-violet-700">α 列</strong>显示推荐后 5/20 交易日相对 SPY 的超额收益,
      数据来自 <code class="text-xs bg-slate-100 px-1 rounded">discovery_tracking</code> 表(每日刷新)。
    </p>
  </div>

  <!-- 推荐历史 view(默认隐藏,切换 sub-tab 后显示) -->
  <div id="disc-view-history" style="display:none">
    <div class="flex items-center gap-3 mb-4">
      <span class="text-xs text-slate-500">按推荐日倒序 · 点击展开查看当日 20 只候选 + 各自 alpha</span>
    </div>
    <div id="discovery-history-list" class="space-y-2"></div>
  </div>
</section>

<script>
// AI 推荐 sub-tab 切换(2026-05-11 PM)
function switchDiscoveryView(view) {
  const today = document.getElementById("disc-view-today");
  const history = document.getElementById("disc-view-history");
  const btnT = document.getElementById("disc-tab-today");
  const btnH = document.getElementById("disc-tab-history");
  if (!today || !history) return;
  if (view === "history") {
    today.style.display = "none";
    history.style.display = "";
    btnH.classList.add("border-violet-500", "text-violet-600");
    btnH.classList.remove("border-transparent", "text-slate-600");
    btnT.classList.remove("border-violet-500", "text-violet-600");
    btnT.classList.add("border-transparent", "text-slate-600");
  } else {
    today.style.display = "";
    history.style.display = "none";
    btnT.classList.add("border-violet-500", "text-violet-600");
    btnT.classList.remove("border-transparent", "text-slate-600");
    btnH.classList.remove("border-violet-500", "text-violet-600");
    btnH.classList.add("border-transparent", "text-slate-600");
  }
}
</script>

<!-- ============ 估值视角 ============ -->
<section id="valuation" class="max-w-7xl mx-auto px-6 py-10 bg-gradient-to-br from-cyan-50 to-blue-50 rounded-2xl my-6">
  <div class="flex items-center gap-3 mb-2">
    <span class="text-3xl">📈</span>
    <h2 class="text-2xl font-bold text-slate-900">估值视角（PE × 涨幅）</h2>
  </div>
  <p class="text-slate-700 mb-6 max-w-3xl">
    用 <strong>远期 PE</strong>（市场对未来 12 个月利润的预期估值）和 <strong>YTD 涨幅</strong>
    交叉分析，找「相对便宜+业绩在加速」的组合。<strong class="text-rose-600">PE 仅是单一维度参考，不构成投资建议</strong>。
  </p>

  <!-- 散点图 -->
  <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4 mb-4">
    <h3 class="text-sm font-semibold text-slate-700 mb-1">PE × YTD 四象限散点图</h3>
    <p class="text-xs text-slate-500 mb-2">鼠标悬停查看股票名 · 左下角象限是「相对便宜+涨幅落后」可能的机会区</p>
    <div id="chart-pe-ytd" style="height:380px"></div>
  </div>

  <!-- 排行榜（4 个一行） -->
  <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3 mb-4">
    <div class="bg-white rounded-xl shadow-sm border-2 border-emerald-300 p-4">
      <h3 class="text-sm font-semibold text-emerald-700 mb-2">⭐ PEG 最低 Top 5</h3>
      <p class="text-xs text-slate-500 mb-2"><strong>真便宜</strong>（PE 相对增速）</p>
      <div id="rank-peg-low" class="space-y-1"></div>
    </div>
    <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
      <h3 class="text-sm font-semibold text-cyan-700 mb-2">🔥 1 周涨幅 Top 5</h3>
      <p class="text-xs text-slate-500 mb-2">短期最热（资金动向）</p>
      <div id="rank-1w-high" class="space-y-1"></div>
    </div>
    <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
      <h3 class="text-sm font-semibold text-rose-700 mb-2">📉 1 周跌幅 Top 5</h3>
      <p class="text-xs text-slate-500 mb-2">短期回调（含错杀候选）</p>
      <div id="rank-1w-low" class="space-y-1"></div>
    </div>
    <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
      <h3 class="text-sm font-semibold text-amber-700 mb-2">💎 远期 PE 最低 Top 5</h3>
      <p class="text-xs text-slate-500 mb-2">表面便宜（看 PEG 才准）</p>
      <div id="rank-pe-low" class="space-y-1"></div>
    </div>
  </div>

  <div class="grid grid-cols-1 md:grid-cols-2 gap-3 mb-4">
    <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
      <h3 class="text-sm font-semibold text-rose-700 mb-2">🚀 YTD 涨幅 Top 5</h3>
      <div id="rank-ytd-high" class="space-y-1"></div>
    </div>
    <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
      <h3 class="text-sm font-semibold text-blue-700 mb-2">📉 YTD 跌幅 Top 5</h3>
      <div id="rank-ytd-low" class="space-y-1"></div>
    </div>
  </div>

  <!-- 「便宜+加速」高亮 -->
  <div class="bg-white rounded-xl shadow-sm border-2 border-emerald-300 p-4">
    <h3 class="text-sm font-semibold text-emerald-700 mb-2">⭐ 「相对便宜+业绩在兑现」候选区（PE ≤ 25 且 YTD > 0）</h3>
    <p class="text-xs text-slate-500 mb-3">这些股票远期 PE 不算贵 + 今年还在涨，是估值维度看相对最有性价比的（仅供参考）</p>
    <div id="cheap-and-rising" class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-2"></div>
  </div>
</section>

<!-- ============ 🌳 产业链地图 · 标题栏（与 chain-overview 一起构成 chain tab） ============ -->
<section id="chain-header" class="max-w-7xl mx-auto px-6 pt-8 pb-2" style="display:none">
  <div class="flex items-center gap-3 mb-1">
    <span class="text-3xl">🌳</span>
    <h1 class="text-2xl font-bold text-slate-900">产业链地图</h1>
  </div>
  <p class="text-sm text-slate-600">把自选股按 <strong class="text-violet-700">产业链 × 层级 × 角色</strong> 三维聚类。同一标的可属于多条链（如 SK Hynix 同时在 HBM 链 + AI 算力链）。</p>
</section>

<!-- ============ 全局分布图表（已退役 · 原属"主题分组"tab，2026-05-11 合并到「产业链地图」后不再展示） ============ -->
<section id="distribution" class="max-w-7xl mx-auto px-6 py-10" style="display:none">
  <h2 class="text-2xl font-bold text-slate-800 mb-6">📊 全局分布</h2>
  <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
    <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
      <h3 class="text-sm font-semibold text-slate-700 mb-2">AI 关联度分布</h3>
      <div id="chart-ai" style="height:240px"></div>
    </div>
    <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
      <h3 class="text-sm font-semibold text-slate-700 mb-2">市场分布</h3>
      <div id="chart-market" style="height:240px"></div>
    </div>
    <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
      <h3 class="text-sm font-semibold text-slate-700 mb-2">研究状态分布</h3>
      <div id="chart-status" style="height:240px"></div>
    </div>
  </div>
</section>

<!-- ============ 主题分组卡片（已退役 · 2026-05-11 合并到「产业链地图」，DOM 保留但默认隐藏，无 tab 引用） ============ -->
<section id="theme-groups" class="max-w-7xl mx-auto px-6 py-10" style="display:none">
  <div class="flex items-center justify-between mb-6">
    <h2 class="text-2xl font-bold text-slate-800">🗂 按主题分组（10 大主题）</h2>
    <input type="text" id="searchBox" placeholder="🔍 搜索股票..." class="px-4 py-2 border border-slate-300 rounded-lg text-sm w-72 focus:outline-none focus:ring-2 focus:ring-violet-400">
  </div>
  <div class="space-y-4">
    {THEME_SECTIONS}
  </div>
</section>

<!-- ============ 💼 持仓管理 Tab（localStorage） ============ -->
<section id="portfolio" class="max-w-7xl mx-auto px-6 py-10" style="display:none">
  <div class="flex items-center justify-between mb-4">
    <div>
      <h2 class="text-2xl font-bold text-slate-900">💼 我的持仓管理</h2>
      <p class="text-sm text-slate-600 mt-1">本地浏览器保存（localStorage）· 实时与 yfinance 数据计算盈亏 · <strong class="text-rose-600">不构成投资建议</strong></p>
    </div>
    <div class="flex gap-2">
      <button onclick="loadPlanAv6()" title="把 AI 组合方案推荐的 12 只批量抄进持仓 — 仅在你已经真下单后用，下一步要手动改成真实成交价" class="bg-amber-600 hover:bg-amber-700 text-white px-4 py-2 rounded-lg text-sm font-medium">📋 把 AI 组合方案抄进持仓（仅在你真下单后用）</button>
      <button onclick="addHolding()" class="bg-violet-600 hover:bg-violet-700 text-white px-4 py-2 rounded-lg text-sm font-medium">+ 添加持仓</button>
    </div>
  </div>

  <!-- ⚠️ 方法学小字说明 -->
  <div class="bg-amber-50 border-l-4 border-amber-400 p-3 mb-4 rounded-r-md">
    <p class="text-xs text-amber-900 mb-1">
      <strong>📅 数据窗口</strong>：方案 A v6 与蒙特卡洛模拟均基于过去 <strong>252 个交易日</strong>（约过去 1 年，~ 2025-05 至今）的 yfinance 真实日 K 数据。
    </p>
    <p class="text-xs text-amber-900 mb-1">
      <strong>⚠️ 怎么读"年化 95% / 夏普 2.95"</strong>：这是<strong class="text-rose-700">历史外推</strong>（"如果未来 1 年表现完全跟过去 1 年一样"），<strong class="text-rose-700">不是未来预测</strong>。要分两层看：
    </p>
    <ul class="text-xs text-amber-900 ml-4 list-disc space-y-0.5 mb-1">
      <li><strong>股价层面</strong>：过去 1 年 AI 标的涨幅极大（NVDA +330% / 中际旭创 +870% / AMD +240%），单纯数字层面很难复现。夏普 2.95 vs 巴菲特长期 0.76，是机构传奇水平 — 样本仅 1 年不可外推。</li>
      <li><strong>技术层面</strong>：AI 在企业渗透率 &lt;10% / 数据中心电力占比 ~3-4% / Robotaxi 渗透 &lt;1%，仍处<strong>早期</strong>。参考"百倍股 5 条件"中"认知反转"才刚发生 2 年。长期空间巨大。</li>
    </ul>
    <p class="text-xs text-amber-800">
      <strong>正确用法</strong>：把这些数字当作<strong>不同组合方案的相对优劣对比</strong>（哪个方案夏普更高、波动更小），而<strong>不是绝对收益预测</strong>。
    </p>
  </div>

  <!-- 📅 5 天蒙特卡洛模拟（仅有 simulation 数据时显示） -->
  <div id="simulation-section" class="bg-gradient-to-br from-cyan-50 to-blue-50 rounded-xl border border-cyan-200 p-5 mb-4" style="display:none">
    <div class="flex items-center justify-between mb-3">
      <div>
        <h3 class="text-lg font-bold text-slate-900">📅 5 天蒙特卡洛模拟（基于历史波动率）</h3>
        <p class="text-xs text-slate-600 mt-1">用每只股票过去 90 天的真实波动率，模拟 1000 次未来 5 个交易日 · ⚠️ 这是统计分布，不是预测</p>
      </div>
      <span id="sim-timestamp" class="text-xs text-slate-500"></span>
    </div>

    <!-- 关键概率 -->
    <div id="sim-probs" class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4"></div>

    <!-- 5 天分布折线图 -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
      <div class="bg-white rounded-lg p-4 shadow-sm">
        <h4 class="text-sm font-semibold text-slate-700 mb-2">5 天组合价值分布（5%/中位/95%）</h4>
        <div id="chart-sim-paths" style="height:280px"></div>
      </div>
      <div class="bg-white rounded-lg p-4 shadow-sm">
        <h4 class="text-sm font-semibold text-slate-700 mb-2">D5 终值分布</h4>
        <div id="chart-sim-final" style="height:280px"></div>
      </div>
    </div>

    <!-- 中位情景每只股票 -->
    <div class="bg-white rounded-lg p-4 shadow-sm">
      <h4 class="text-sm font-semibold text-slate-700 mb-2">💼 中位情景下每只股票 D5 预期表现</h4>
      <div id="sim-stock-table" class="overflow-x-auto"></div>
    </div>
  </div>

  <!-- 总览数字 -->
  <div id="portfolio-summary" class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-3 mb-4"></div>

  <!-- 📚 v6 Markowitz 模型预期（加载方案 A v6 后显示）-->
  <div id="v6-metrics-card" class="bg-gradient-to-r from-emerald-50 to-teal-50 border border-emerald-300 rounded-xl p-4 mb-4" style="display:none">
    <div class="flex items-center justify-between mb-3">
      <h3 class="text-sm font-bold text-emerald-900">📚 v6 Markowitz 模型预期（每日 rebalance 假设 · 基于过去 252 天均值）</h3>
      <span class="text-xs text-emerald-700 bg-emerald-100 px-2 py-1 rounded">⚠️ 模型期望，非未来预测</span>
    </div>
    <div class="grid grid-cols-2 md:grid-cols-4 gap-3" id="v6-metrics-content"></div>
    <p class="text-xs text-emerald-800 mt-3"><strong>⚠️ 注意</strong>：这是 Markowitz 优化器的"<strong>每日 rebalance 模型期望</strong>"（mean × 252，arithmetic）。「专业分析 → 风险指标」tab 用 <strong>buy-and-hold 复利</strong>口径会得到不同（通常更高）的数字 —— 两个都对，<strong>假设不同</strong>。仅用于<strong>不同方案的相对优劣对比</strong>，不是未来收益预测。</p>
  </div>

  <!-- 三层警戒线进度条 -->
  <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4 mb-4">
    <h3 class="text-sm font-semibold text-slate-700 mb-3">⚠️ 风险警戒线（基于本金 <span data-cfg="total_capital">50 万</span>）</h3>
    <div id="alert-line" class="space-y-2"></div>
  </div>

  <!-- 持仓列表 -->
  <div class="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
    <table class="w-full text-sm">
      <thead class="bg-slate-100">
        <tr>
          <th class="px-3 py-2 text-left">股票</th>
          <th class="px-3 py-2 text-left">行业</th>
          <th class="px-3 py-2 text-right">买入价</th>
          <th class="px-3 py-2 text-right">数量</th>
          <th class="px-3 py-2 text-right">成本</th>
          <th class="px-3 py-2 text-right">现价</th>
          <th class="px-3 py-2 text-right">市值</th>
          <th class="px-3 py-2 text-right">盈亏 RMB</th>
          <th class="px-3 py-2 text-right">盈亏%</th>
          <th class="px-3 py-2 text-right">仓位%</th>
          <th class="px-3 py-2 text-center">操作</th>
        </tr>
      </thead>
      <tbody id="holdings-table">
        <tr><td colspan="11" class="text-center text-slate-500 py-8">暂无持仓 · 点击「+ 添加持仓」开始记录</td></tr>
      </tbody>
    </table>
  </div>

  <!-- 仓位健康度饼图 -->
  <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mt-4">
    <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
      <h3 class="text-sm font-semibold text-slate-700 mb-2">📊 当前仓位分布</h3>
      <div id="chart-allocation" style="height:280px"></div>
    </div>
    <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
      <h3 class="text-sm font-semibold text-slate-700 mb-2">📈 主题分布</h3>
      <div id="chart-theme" style="height:280px"></div>
    </div>
  </div>

  <!-- 添加持仓表单（隐藏 + 弹窗） -->
  <div id="holding-modal" class="fixed inset-0 bg-black bg-opacity-50 z-50 hidden flex items-center justify-center">
    <div class="bg-white rounded-xl p-6 max-w-md w-full mx-4">
      <h3 class="text-lg font-bold mb-4">添加 / 编辑持仓</h3>
      <div class="space-y-3">
        <div>
          <label class="text-xs font-medium text-slate-600">股票（选择自选股）</label>
          <select id="form-code" class="w-full mt-1 px-3 py-2 border rounded text-sm"></select>
        </div>
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="text-xs font-medium text-slate-600">买入价</label>
            <input id="form-price" type="number" step="0.01" class="w-full mt-1 px-3 py-2 border rounded text-sm">
          </div>
          <div>
            <label class="text-xs font-medium text-slate-600">数量（股）</label>
            <input id="form-shares" type="number" step="1" class="w-full mt-1 px-3 py-2 border rounded text-sm">
          </div>
        </div>
        <div>
          <label class="text-xs font-medium text-slate-600">买入日期</label>
          <input id="form-date" type="date" class="w-full mt-1 px-3 py-2 border rounded text-sm">
        </div>
      </div>
      <div class="flex gap-2 mt-5">
        <button onclick="saveHolding()" class="flex-1 bg-violet-600 hover:bg-violet-700 text-white py-2 rounded font-medium">保存</button>
        <button onclick="closeModal()" class="flex-1 bg-slate-200 hover:bg-slate-300 py-2 rounded">取消</button>
      </div>
    </div>
  </div>

  <p class="text-xs text-slate-500 mt-4">
    💾 数据保存在你浏览器 localStorage，<strong>清缓存会丢失</strong>。建议每次买卖后导出 JSON 备份。
    <button onclick="exportHoldings()" class="text-violet-600 hover:underline">导出 JSON</button> ·
    <button onclick="importHoldings()" class="text-violet-600 hover:underline">导入 JSON</button> ·
    <button onclick="clearHoldings()" class="text-rose-600 hover:underline">清空全部</button>
  </p>
</section>

<!-- ============ 🤖 AI 组合方案 Tab ============ -->
<section id="backtest" class="max-w-7xl mx-auto px-6 py-10" style="display:none">
  <div class="mb-6">
    <h2 class="text-2xl font-bold text-slate-900">🆚 系统在跑两个方案 · 看 AI 到底有没有用</h2>
    <p class="text-sm text-slate-600 mt-1">每周一同时跑两套策略，让数据自然分胜负。<strong class="text-emerald-700">差距 C − A = AI 加的 alpha</strong>。基准 SPY · daily_refresh 自动累加。⚠️ <strong>这不是你的真实账户</strong>。</p>
  </div>

  <!-- 🆚 两个方案对比卡（让新人一眼看懂 "两个方案 + 比什么"）-->
  <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
    <!-- 方案 A · 静态死守（紫色，跟 NAV 曲线颜色一致） -->
    <div class="bg-violet-50 border-2 border-violet-300 rounded-xl p-5">
      <div class="flex items-center gap-2 mb-2">
        <span class="text-2xl">📦</span>
        <h3 class="text-lg font-bold text-violet-900">方案 A · 静态死守</h3>
      </div>
      <p class="text-sm text-violet-800 font-medium mb-3">5-10 锁定 11 只股 · 从此不调仓</p>
      <ul class="text-xs text-violet-700 space-y-1.5">
        <li>✓ 模拟「<strong>佛系投资者</strong>」</li>
        <li>✓ 不调仓 → <strong>0 手续费</strong>，无 look-ahead bias</li>
        <li>✓ 完全取决于锁定日的初始选股运气</li>
      </ul>
    </div>

    <!-- 方案 C · 动态调仓（橙色，跟 NAV 曲线颜色一致） -->
    <div class="bg-orange-50 border-2 border-orange-300 rounded-xl p-5">
      <div class="flex items-center gap-2 mb-2">
        <span class="text-2xl">🔄</span>
        <h3 class="text-lg font-bold text-orange-900">方案 C · 动态调仓</h3>
      </div>
      <p class="text-sm text-orange-800 font-medium mb-3">每周一按 AI 推荐重新优化</p>
      <ul class="text-xs text-orange-700 space-y-1.5">
        <li>✓ 模拟「<strong>听 AI 调仓</strong>」</li>
        <li>✓ 扣 <strong>10bps / 换股</strong> 手续费（A 股可调到 8bps）</li>
        <li>✓ 跟踪 AI 实时选股建议</li>
      </ul>
    </div>
  </div>

  <!-- 核心 KPI：C - A spread = AI alpha 的硬证据 -->
  <div class="bg-gradient-to-r from-emerald-50 via-emerald-50 to-blue-50 border-2 border-emerald-300 rounded-xl p-5 mb-6 text-center">
    <div class="text-[11px] uppercase tracking-widest text-emerald-700 font-bold mb-1">AI 加的 alpha</div>
    <div class="text-3xl md:text-4xl font-bold text-emerald-900 mb-1">
      <span id="ai-alpha-spread-display">—</span>
      <span class="text-sm text-emerald-600 font-normal">= C 累计 − A 累计</span>
    </div>
    <div class="text-xs text-slate-600 mt-2">C 一直跑赢 A → AI 动态调仓有价值；C 跟不上 A → AI 加价值不够覆盖手续费</div>
  </div>

  <div id="backtest-inception-banner" class="mb-4 hidden bg-violet-50 border-l-4 border-violet-500 rounded-r-lg p-3 text-sm text-slate-800"></div>

  <!-- A vs C 对比 + 调仓记录 -->
  <div id="backtest-rebalance-log" class="mb-4"></div>

  <!-- 关键指标卡 -->
  <div id="backtest-metrics" class="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4"></div>

  <!-- NAV 曲线 -->
  <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4 mb-4">
    <div class="flex items-center justify-between mb-3">
      <h3 class="text-sm font-semibold text-slate-700">📈 NAV 曲线（起点 = 100，A 静态紫 / C 动态橙 / SPY 红）</h3>
      <span id="backtest-coverage" class="text-xs text-slate-500"></span>
    </div>
    <div id="backtest-nav-chart" style="height:420px"></div>
  </div>

  <!-- 最近 60 天每日 P&L -->
  <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4 mb-4">
    <h3 class="text-sm font-semibold text-slate-700 mb-3">📅 最近 60 天每日 P&amp;L（红 = 涨 / 绿 = 跌）</h3>
    <div id="backtest-daily-chart" style="height:280px"></div>
  </div>

  <!-- 持仓贡献表 -->
  <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4 mb-4">
    <h3 class="text-sm font-semibold text-slate-700 mb-3">💼 单股贡献度（按贡献排序）</h3>
    <div id="backtest-contrib-table" class="overflow-x-auto"></div>
  </div>

  <!-- 缺数据提示 -->
  <div id="backtest-missing-warning" class="hidden bg-amber-50 border border-amber-300 rounded-lg p-3 text-xs text-amber-800"></div>
</section>

<!-- ============ 📊 专业分析 Tab ============ -->
<section id="professional" class="max-w-7xl mx-auto px-6 py-10" style="display:none">
  <div class="mb-6">
    <h2 class="text-2xl font-bold text-slate-900">📊 专业分析（华尔街标准）</h2>
    <p class="text-sm text-slate-600 mt-1">VaR / Sharpe / Sortino · 13F 机构持仓 · Kelly + Risk Parity + Markowitz 仓位优化 · <strong class="text-rose-600">不构成投资建议</strong></p>
  </div>

  <!-- 标签：风险指标 / 13F / 优化 -->
  <div class="flex gap-2 mb-4 border-b border-slate-200">
    <button onclick="switchProfTab('risk')" id="prof-tab-risk" class="prof-tab-btn px-4 py-2 text-sm font-medium border-b-2 border-violet-500 text-violet-600">📉 风险指标</button>
    <button onclick="switchProfTab('13f')" id="prof-tab-13f" class="prof-tab-btn px-4 py-2 text-sm font-medium border-b-2 border-transparent text-slate-600 hover:text-violet-600">🏛 13F 机构持仓</button>
    <button onclick="switchProfTab('optimize')" id="prof-tab-optimize" class="prof-tab-btn px-4 py-2 text-sm font-medium border-b-2 border-transparent text-slate-600 hover:text-violet-600">⚖️ 仓位优化</button>
  </div>

  <!-- 子 Tab 1: 风险指标 -->
  <div id="prof-pane-risk" class="prof-pane">
    <div class="bg-gradient-to-br from-rose-50 to-orange-50 rounded-xl border border-rose-200 p-5 mb-4">
      <h3 class="text-lg font-bold text-rose-900 mb-2">⚠️ VaR / CVaR · 在险价值</h3>
      <p class="text-xs text-slate-700 mb-3">基于过去 ~1 年历史，95%/99% 置信度下 1 天最大损失</p>
      <div id="risk-var" class="grid grid-cols-2 md:grid-cols-4 gap-3"></div>
    </div>

    <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
      <div class="bg-white rounded-xl border border-slate-200 p-4">
        <h4 class="text-sm font-semibold text-slate-700 mb-2">📈 收益指标</h4>
        <div id="risk-return"></div>
      </div>
      <div class="bg-white rounded-xl border border-slate-200 p-4">
        <h4 class="text-sm font-semibold text-slate-700 mb-2">📉 风险指标</h4>
        <div id="risk-vol"></div>
      </div>
      <div class="bg-white rounded-xl border border-slate-200 p-4">
        <h4 class="text-sm font-semibold text-slate-700 mb-2">🎯 风险调整收益</h4>
        <div id="risk-ratios"></div>
      </div>
    </div>

    <div class="bg-white rounded-xl border border-slate-200 p-4">
      <h4 class="text-sm font-semibold text-slate-700 mb-2">📅 组合每日价值（基于 50 万 RMB 假设建仓）</h4>
      <div id="chart-portfolio-history" style="height:380px"></div>
    </div>
  </div>

  <!-- 子 Tab 2: 13F -->
  <div id="prof-pane-13f" class="prof-pane" style="display:none">
    <div class="bg-emerald-50 border border-emerald-200 rounded-xl p-4 mb-4">
      <p class="text-sm text-emerald-900">✅ 数据源已升级为 <strong>SEC EDGAR 13F-HR</strong>（10 家机构 Q4 2025 真实季度持仓变动）— 能看到 Bridgewater 加仓 / Burry 新建仓 / Renaissance 清仓 等具体信号。13F 滞后 45 天披露，反映季度末持仓 ≠ 实时持仓。</p>
    </div>
    <div id="track-13f-content" class="space-y-4"></div>
  </div>

  <!-- 子 Tab 3: 仓位优化 -->
  <div id="prof-pane-optimize" class="prof-pane" style="display:none">
    <div class="bg-gradient-to-br from-emerald-50 to-cyan-50 rounded-xl border border-emerald-200 p-5 mb-4">
      <h3 class="text-lg font-bold text-emerald-900 mb-2">⚖️ 三种专业方法对比</h3>
      <p class="text-xs text-slate-700 mb-3">Kelly Half + Risk Parity + Markowitz Max Sharpe vs 当前方案 A</p>
      <div id="opt-comparison" class="grid grid-cols-1 md:grid-cols-4 gap-3"></div>
    </div>

    <div class="bg-white rounded-xl border border-slate-200 p-4 mb-4">
      <h4 class="text-sm font-semibold text-slate-700 mb-2">📊 仓位对比表</h4>
      <div id="opt-table" class="overflow-x-auto"></div>
    </div>

    <div class="bg-white rounded-xl border border-slate-200 p-4">
      <h4 class="text-sm font-semibold text-slate-700 mb-2">📈 仓位对比可视化</h4>
      <div id="chart-opt" style="height:380px"></div>
    </div>
  </div>
</section>

<!-- ============ 🌳 产业链全景 Tab（按链条聚类 watchlist，让新手看出层级 + 角色 + 一句话）============ -->
<section id="chain-overview" class="max-w-7xl mx-auto px-6 py-10" style="display:none">
  <div class="mb-6">
    <div class="flex items-center gap-3 mb-2">
      <span class="text-3xl">🌳</span>
      <h2 class="text-2xl font-bold text-slate-900">产业链全景</h2>
    </div>
    <p class="text-sm text-slate-600">
      把自选股按 <strong>产业链 × 层级 × 角色</strong> 三维聚类。
      <strong class="text-violet-700">层级颜色</strong>：
      <span class="px-1.5 py-0.5 bg-violet-100 text-violet-800 rounded text-xs font-semibold">核心</span>
      <span class="px-1.5 py-0.5 bg-blue-100 text-blue-800 rounded text-xs font-semibold">一线</span>
      <span class="px-1.5 py-0.5 bg-emerald-100 text-emerald-800 rounded text-xs font-semibold">二线</span>
      <span class="px-1.5 py-0.5 bg-amber-100 text-amber-800 rounded text-xs font-semibold">三线</span>
      <span class="px-1.5 py-0.5 bg-slate-100 text-slate-500 rounded text-xs font-semibold">N/A</span>
      。同一标的可属于多条链(如 SK Hynix 同时在 HBM 和 AI 算力链)。
    </p>
  </div>

  <div class="flex items-center gap-3 mb-4 flex-wrap text-xs">
    <span id="chain-api-status" class="px-2 py-0.5 rounded bg-slate-100 text-slate-500">检测中…</span>
    <button onclick="forceReloadChainOverview()" class="px-3 py-1 bg-slate-600 hover:bg-slate-700 text-white rounded">🔄 刷新</button>
    <span id="chain-count" class="ml-auto text-slate-500"></span>
  </div>

  <div id="chain-summary" class="mb-6 flex flex-wrap gap-2"></div>

  <div id="chain-cards-container" class="space-y-6"></div>
</section>

<!-- ============ ⚙️ 初始化配置（父 tab · 顶部二级切换 投资方案 / 关注股票）============ -->
<section id="init-config" class="max-w-7xl mx-auto px-6 pt-10 pb-2" style="display:none">
  <div class="mb-4">
    <div class="flex items-center gap-3 mb-1">
      <span class="text-3xl">⚙️</span>
      <h2 class="text-2xl font-bold text-slate-900">初始化配置</h2>
    </div>
    <p class="text-sm text-slate-600">系统跑批和前端展示都依赖这两组用户输入：<strong>投资方案</strong>（有多少钱）和<strong>自选股</strong>（追踪哪些标的）。</p>
  </div>
  <!-- 二级 tab 栏 -->
  <div class="border-b border-slate-200 flex gap-1">
    <button onclick="switchInitSub('portfolio')" id="init-sub-btn-portfolio"
            class="init-sub-btn px-4 py-2 text-sm font-medium border-b-2 transition">
      💰 投资方案
    </button>
    <button onclick="switchInitSub('watchlist')" id="init-sub-btn-watchlist"
            class="init-sub-btn px-4 py-2 text-sm font-medium border-b-2 transition">
      ✨ 自选股
    </button>
  </div>
</section>

<!-- ============ ⚙️ 投资方案配置（init-config 的子页之一）============ -->
<section id="portfolio-config" class="max-w-7xl mx-auto px-6 pt-4 pb-10" style="display:none">
  <!-- 加载/错误提示（API 离线 / 保存成功 / 校验失败 都用这一个 banner） -->
  <div id="pcfg-banner" class="hidden mb-4 px-4 py-3 rounded-lg text-sm"></div>

  <div class="grid grid-cols-1 lg:grid-cols-2 gap-5">
    <!-- 进场本金 -->
    <div class="bg-white rounded-2xl shadow-sm border border-slate-200 p-6">
      <label class="block">
        <div class="flex items-baseline justify-between mb-2">
          <span class="text-sm font-bold text-emerald-700">💰 进场本金</span>
          <span class="text-[11px] text-slate-400 font-mono">total_capital</span>
        </div>
        <div class="flex items-center gap-2">
          <span class="text-slate-500 font-medium">¥</span>
          <input id="pcfg-total-capital" type="number" min="1" step="10000"
                 class="flex-1 px-3 py-2 text-2xl font-bold text-slate-900 border border-slate-300 rounded-lg focus:border-emerald-400 focus:ring-2 focus:ring-emerald-100 outline-none">
        </div>
        <p class="text-xs text-slate-500 mt-2 leading-relaxed">
          整个组合按这个数算每只股票该买多少。修改后<strong>下一次跑批</strong>（build_plan_a_v5 / trade_delta / risk_metrics）就生效。
        </p>
      </label>
    </div>

    <!-- 止损红线 -->
    <div class="bg-white rounded-2xl shadow-sm border border-slate-200 p-6">
      <label class="block">
        <div class="flex items-baseline justify-between mb-2">
          <span class="text-sm font-bold text-rose-700">🛑 止损红线</span>
          <span class="text-[11px] text-slate-400 font-mono">stoploss_line</span>
        </div>
        <div class="flex items-center gap-2">
          <span class="text-slate-500 font-medium">¥</span>
          <input id="pcfg-stoploss-line" type="number" min="1" step="10000"
                 class="flex-1 px-3 py-2 text-2xl font-bold text-slate-900 border border-slate-300 rounded-lg focus:border-rose-400 focus:ring-2 focus:ring-rose-100 outline-none">
        </div>
        <p class="text-xs text-slate-500 mt-2 leading-relaxed">
          组合市值跌到这条线就<strong>强制清仓</strong>。要求 &lt; 进场本金。当前最大允许亏损：<strong id="pcfg-max-loss" class="text-rose-700">—</strong>
        </p>
      </label>
    </div>
  </div>

  <!-- 提交按钮 + 当前值显示 -->
  <div class="mt-5 flex items-center gap-3">
    <button id="pcfg-save" onclick="savePortfolioConfig()"
            class="px-5 py-2 bg-violet-600 hover:bg-violet-700 text-white text-sm font-medium rounded-lg transition disabled:opacity-50">
      💾 保存配置
    </button>
    <button onclick="loadPortfolioConfig()"
            class="px-4 py-2 text-sm text-slate-700 hover:text-violet-600 transition">
      ↻ 重新加载
    </button>
    <div class="ml-auto text-xs text-slate-500">
      当前生效值 · 最近更新：<span id="pcfg-updated-at" class="font-mono text-slate-700">—</span>
    </div>
  </div>

  <!-- 提示卡 -->
  <div class="mt-6 bg-slate-50 border-l-4 border-slate-400 rounded p-4 text-sm text-slate-700">
    <strong>💡 提示：</strong>修改后<strong>持仓页</strong>和<strong>系统介绍页</strong>会立即用新值显示；
    但 <strong>飞书早报里的资金分配建议</strong>需要等下一次跑批（daily_refresh.sh）才更新。
  </div>
</section>

<!-- ============ ✏️ Watchlist 编辑（init-config 子页 · DuckDB 权威 · 通过 FastAPI 增删改）============ -->
<section id="watchlist-edit" class="max-w-7xl mx-auto px-6 pt-4 pb-10" style="display:none">
  <!-- API 离线时才出现的命令提示 -->
  <div id="wl-offline-hint" class="hidden mb-4 px-4 py-3 rounded bg-amber-50 border border-amber-200 text-sm text-amber-900">
    本地 API 未启动 · 在终端跑：<code class="text-xs bg-amber-100 px-1 rounded ml-1">uvicorn stock_research.api.main:app --port 8765</code>
  </div>

  <!-- API 连接状态 + 操作按钮条 -->
  <div class="flex items-center gap-3 mb-4 flex-wrap">
    <span class="text-sm text-slate-600">API: </span>
    <code class="text-xs font-mono bg-slate-100 px-2 py-1 rounded" id="watchlist-api-base">http://127.0.0.1:8765</code>
    <span id="watchlist-api-status" class="text-xs px-2 py-0.5 rounded bg-slate-100 text-slate-500">检测中…</span>
    <button onclick="forceReloadWatchlist()" class="text-xs px-3 py-1 bg-slate-600 hover:bg-slate-700 text-white rounded">🔄 刷新</button>
    <button onclick="openWatchlistEditor()" class="text-xs px-3 py-1 bg-violet-600 hover:bg-violet-700 text-white rounded">➕ 添加新股</button>
    <span id="watchlist-count" class="ml-auto text-xs text-slate-500"></span>
  </div>

  <!-- 链条 / 层级 / 角色 多重筛选 -->
  <div class="flex items-center gap-2 mb-3 flex-wrap text-xs">
    <span class="text-slate-500">筛选:</span>
    <select id="wl-filter-chain" onchange="loadWatchlistTable()" class="px-2 py-1 border border-slate-300 rounded">
      <option value="">全部链条</option>
    </select>
    <select id="wl-filter-tier" onchange="loadWatchlistTable()" class="px-2 py-1 border border-slate-300 rounded">
      <option value="">全部层级</option>
      <option value="核心">核心</option>
      <option value="一线">一线</option>
      <option value="二线">二线</option>
      <option value="三线">三线</option>
      <option value="N/A">N/A</option>
    </select>
    <select id="wl-filter-role" onchange="loadWatchlistTable()" class="px-2 py-1 border border-slate-300 rounded">
      <option value="">全部角色</option>
    </select>
    <input id="wl-filter-keyword" oninput="loadWatchlistTable()" type="text" placeholder="搜代码/名称/一句话…" class="px-2 py-1 border border-slate-300 rounded w-48">
  </div>

  <!-- 主表格 -->
  <div class="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
    <table class="w-full text-sm">
      <thead class="bg-slate-50 text-xs text-slate-600">
        <tr>
          <th class="px-3 py-2 text-left">代码</th>
          <th class="px-3 py-2 text-left">名称</th>
          <th class="px-3 py-2 text-left">链条</th>
          <th class="px-3 py-2 text-left">层级</th>
          <th class="px-3 py-2 text-left">角色</th>
          <th class="px-3 py-2 text-left">一句话解释(新手向)</th>
          <th class="px-3 py-2 text-left">市场</th>
          <th class="px-3 py-2 text-left" title="基于学术因子 (F-Score + 12-1 动量 + PEAD + 分析师) 综合评分">AI 评级</th>
          <th class="px-3 py-2 text-left">状态</th>
          <th class="px-3 py-2 text-right">操作</th>
        </tr>
      </thead>
      <tbody id="watchlist-table-body" class="divide-y divide-slate-100"></tbody>
    </table>
  </div>

  <!-- 编辑 / 添加 Modal -->
  <div id="watchlist-modal" class="hidden fixed inset-0 bg-slate-900/50 z-[100] flex items-center justify-center p-4" onclick="if(event.target===this)closeWatchlistEditor()">
    <div class="bg-white rounded-xl shadow-2xl max-w-2xl w-full max-h-[90vh] overflow-y-auto">
      <div class="px-6 py-4 border-b border-slate-200 flex items-center justify-between sticky top-0 bg-white">
        <h3 class="text-lg font-bold text-slate-900" id="watchlist-modal-title">添加新股</h3>
        <button onclick="closeWatchlistEditor()" class="text-slate-400 hover:text-slate-700 text-xl">×</button>
      </div>
      <!-- 自动补全提示条 -->
      <div class="px-6 pt-3 pb-1">
        <div class="bg-violet-50 border border-violet-200 rounded-lg p-3 text-xs text-violet-800 leading-relaxed">
          💡 <strong>只填代码就行</strong>，点 <kbd class="bg-white border border-violet-300 px-1 rounded">🪄 自动补全</kbd> 按钮 →
          系统自动从 yfinance + GICS 拉取名字 / 行业 / AI 关联 / 主题 / 产业链 / 1 句话解释。然后你 review 一下点保存。
        </div>
      </div>
      <div class="px-6 py-4 grid grid-cols-1 md:grid-cols-2 gap-4">
        <div class="md:col-span-2">
          <label class="text-xs text-slate-500 block mb-1">代码 *</label>
          <div class="flex gap-2">
            <input id="wl-code" type="text" class="flex-1 px-3 py-2 border border-slate-300 rounded text-sm font-mono" placeholder="如 NVDA / 600519.SS">
            <button onclick="autoEnrichWatchlist()" id="wl-enrich-btn" class="px-3 py-2 text-sm bg-violet-600 hover:bg-violet-700 text-white rounded font-medium whitespace-nowrap">🪄 自动补全</button>
          </div>
          <div id="wl-enrich-status" class="text-xs text-slate-500 mt-1"></div>
        </div>
        <div>
          <label class="text-xs text-slate-500 block mb-1">名称</label>
          <input id="wl-name" type="text" class="w-full px-3 py-2 border border-slate-300 rounded text-sm" placeholder="如 NVIDIA">
        </div>
        <div>
          <label class="text-xs text-slate-500 block mb-1">市场</label>
          <input id="wl-market" type="text" class="w-full px-3 py-2 border border-slate-300 rounded text-sm" placeholder="美股 / A股·沪深 / 港股 …">
        </div>
        <div>
          <label class="text-xs text-slate-500 block mb-1">行业归类</label>
          <input id="wl-industry" type="text" class="w-full px-3 py-2 border border-slate-300 rounded text-sm" placeholder="半导体 / SaaS / …">
        </div>
        <div>
          <label class="text-xs text-slate-500 block mb-1">主营业务</label>
          <input id="wl-business" type="text" class="w-full px-3 py-2 border border-slate-300 rounded text-sm">
        </div>
        <!-- 2026-05-11 新增:产业链定位字段 -->
        <div>
          <label class="text-xs text-slate-500 block mb-1">产业链(逗号分隔多链)</label>
          <input id="wl-chain" type="text" class="w-full px-3 py-2 border border-slate-300 rounded text-sm" placeholder="HBM,AI 算力 / 数据中心电力 / …">
        </div>
        <div>
          <label class="text-xs text-slate-500 block mb-1">链条层级</label>
          <select id="wl-chain-tier" class="w-full px-3 py-2 border border-slate-300 rounded text-sm">
            <option value="">—</option>
            <option value="核心">核心</option>
            <option value="一线">一线</option>
            <option value="二线">二线</option>
            <option value="三线">三线</option>
            <option value="N/A">N/A(对照组)</option>
          </select>
        </div>
        <div>
          <label class="text-xs text-slate-500 block mb-1">链条角色</label>
          <select id="wl-chain-role" class="w-full px-3 py-2 border border-slate-300 rounded text-sm">
            <option value="">—</option>
            <option value="IDM">IDM(自产芯片)</option>
            <option value="GPU">GPU</option>
            <option value="网络芯片">网络芯片</option>
            <option value="设备">设备</option>
            <option value="材料">材料</option>
            <option value="封测">封测</option>
            <option value="EDA">EDA</option>
            <option value="服务器">服务器</option>
            <option value="应用层">应用层</option>
            <option value="服务">服务</option>
            <option value="基础设施">基础设施</option>
            <option value="对照">对照</option>
          </select>
        </div>
        <div class="md:col-span-2">
          <label class="text-xs text-slate-500 block mb-1">新手一句话解释(<60 字)</label>
          <input id="wl-layman-intro" type="text" maxlength="80" class="w-full px-3 py-2 border border-slate-300 rounded text-sm" placeholder="如:全球 HBM 内存第一,NVIDIA 主供应商,AI GPU 必须配它才跑得动">
        </div>
        <div>
          <label class="text-xs text-slate-500 block mb-1">AI 关联度</label>
          <input id="wl-ai-relevance" type="text" class="w-full px-3 py-2 border border-slate-300 rounded text-sm" placeholder="🟢 直接 / 🟡 间接 / 🔴 无关">
        </div>
        <div class="md:col-span-2">
          <label class="text-xs text-slate-500 block mb-1">AI 关联逻辑</label>
          <textarea id="wl-ai-logic" rows="2" class="w-full px-3 py-2 border border-slate-300 rounded text-sm"></textarea>
        </div>
        <div>
          <label class="text-xs text-slate-500 block mb-1">研究状态</label>
          <input id="wl-status" type="text" class="w-full px-3 py-2 border border-slate-300 rounded text-sm" placeholder="持仓 / 关注 / 待研究 …">
        </div>
        <div>
          <label class="text-xs text-slate-500 block mb-1">数据可信度</label>
          <input id="wl-credibility" type="text" class="w-full px-3 py-2 border border-slate-300 rounded text-sm" placeholder="HIGH / MEDIUM / LOW">
        </div>
        <div class="md:col-span-2">
          <label class="text-xs text-slate-500 block mb-1">研究结论</label>
          <textarea id="wl-conclusion" rows="2" class="w-full px-3 py-2 border border-slate-300 rounded text-sm"></textarea>
        </div>
        <div class="md:col-span-2">
          <label class="text-xs text-slate-500 block mb-1">关键风险</label>
          <textarea id="wl-risks" rows="2" class="w-full px-3 py-2 border border-slate-300 rounded text-sm"></textarea>
        </div>
        <div class="md:col-span-2">
          <label class="text-xs text-slate-500 block mb-1">最近季度业绩 (Q? 营收/利润/EPS/指引)</label>
          <textarea id="wl-earnings" rows="3" class="w-full px-3 py-2 border border-slate-300 rounded text-sm font-mono" placeholder="Q1 2026:\n· 营收 $XB (+XX% YoY)\n· 净利 $XB (+XX% YoY)\n· EPS $X.XX"></textarea>
        </div>
        <div>
          <label class="text-xs text-slate-500 block mb-1">双源验证</label>
          <input id="wl-verification" type="text" class="w-full px-3 py-2 border border-slate-300 rounded text-sm" placeholder="✅ 双源(≥2 个来源) / ⚠️ 单源 / ❓ 待补">
        </div>
        <div class="md:col-span-2">
          <label class="text-xs text-slate-500 block mb-1">信息构成 (多源 enrichment 摘要,通常自动生成)</label>
          <textarea id="wl-info-breakdown" rows="4" class="w-full px-3 py-2 border border-slate-300 rounded text-sm font-mono" placeholder="📊 实时事实 / 🧑‍💼 内部人交易 / 📰 近 7 天新闻 ... (enrich_watchlist 自动填充)"></textarea>
        </div>
        <div>
          <label class="text-xs text-slate-500 block mb-1">可比公司</label>
          <input id="wl-peers" type="text" class="w-full px-3 py-2 border border-slate-300 rounded text-sm" placeholder="逗号分隔 ticker">
        </div>
        <div>
          <label class="text-xs text-slate-500 block mb-1">跟踪节奏</label>
          <input id="wl-rhythm" type="text" class="w-full px-3 py-2 border border-slate-300 rounded text-sm" placeholder="日 / 周 / 月">
        </div>
        <div class="md:col-span-2">
          <label class="text-xs text-slate-500 block mb-1">备注</label>
          <textarea id="wl-notes" rows="2" class="w-full px-3 py-2 border border-slate-300 rounded text-sm"></textarea>
        </div>
      </div>
      <div class="px-6 py-4 border-t border-slate-200 flex items-center justify-end gap-2 sticky bottom-0 bg-white">
        <button onclick="closeWatchlistEditor()" class="px-4 py-2 text-sm border border-slate-300 hover:bg-slate-50 rounded">取消</button>
        <button onclick="saveWatchlistItem()" class="px-4 py-2 text-sm bg-violet-600 hover:bg-violet-700 text-white rounded font-medium">保存</button>
      </div>
    </div>
  </div>
</section>

<!-- ============ 💰 升级建议 Tab ============ -->
<section id="upgrade" class="max-w-7xl mx-auto px-6 py-10" style="display:none">
  <div class="mb-6">
    <h2 class="text-3xl font-bold text-slate-900">💰 系统体检 + 升级建议</h2>
    <p class="text-sm text-slate-600 mt-2">先看现状（已实现 / 在做 / 缺口），再看升级方案。给同事看决策用。</p>
  </div>

  <!-- ════════ 系统响应能力 ════════ -->
  <div class="bg-gradient-to-br from-emerald-50 to-cyan-50 border-2 border-emerald-300 rounded-xl p-6 mb-8">
    <h3 class="text-2xl font-bold text-emerald-900 mb-4">📡 系统响应能力（实时性总览）</h3>
    <p class="text-sm text-slate-700 mb-4">数据从市场发生 → 落入系统的实际延迟。系统不是真"实时"，是 <strong>每天 08:30 一次 daily 批处理</strong> + <strong>每 15 分钟 defense_watcher 巡检</strong>（升档才推飞书）。</p>

    <!-- 响应能力分级 -->
    <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
      <div class="bg-white rounded-lg p-4 border-l-4 border-emerald-500">
        <div class="text-3xl mb-2">⚡</div>
        <p class="font-bold text-slate-800 mb-1">T+0 / T+1 实时</p>
        <ul class="text-xs text-slate-600 space-y-0.5">
          <li>· 股价（盘中 15 分钟延迟）</li>
          <li>· Yahoo Trending 美股热门</li>
          <li>· Reddit WSB 情感（几分钟）</li>
          <li>· 个股新闻（yfinance）</li>
        </ul>
      </div>
      <div class="bg-white rounded-lg p-4 border-l-4 border-amber-500">
        <div class="text-3xl mb-2">📊</div>
        <p class="font-bold text-slate-800 mb-1">T+1-7 数日内</p>
        <ul class="text-xs text-slate-600 space-y-0.5">
          <li>· 分析师目标价上修</li>
          <li>· 业绩 surprise（earnings_history）</li>
          <li>· EPS 预期变化</li>
          <li>· 东方财富 A 股热度榜</li>
          <li>· yfinance 季度财报</li>
        </ul>
      </div>
      <div class="bg-white rounded-lg p-4 border-l-4 border-rose-500">
        <div class="text-3xl mb-2">📜</div>
        <p class="font-bold text-slate-800 mb-1">T+15-90 监管级</p>
        <ul class="text-xs text-slate-600 space-y-0.5">
          <li>· SEC Form 4 内部人买卖（T+3-7）</li>
          <li>· akshare A 股财报（T+5-15）</li>
          <li>· SEC 13F 机构持仓（T+45）</li>
          <li>· SEC 10-K 年报全文（T+60-90）</li>
        </ul>
      </div>
    </div>

    <!-- 系统实际架构：两条线 -->
    <div class="bg-white rounded-lg p-5 mb-4">
      <h4 class="font-bold text-slate-800 mb-3">🏗 系统实际架构 — 两条线、五个部件</h4>
      <div class="bg-amber-50 border-l-4 border-amber-400 rounded-r p-3 mb-4 text-xs text-slate-700">
        <strong>注意：v 编号不是"版本升级"，而是"部件标签"。</strong>不同 v 号管不同的事，
        <strong>同时在跑、互不替代</strong>——类比手机里的"屏幕 / 主板 / 电池 / 摄像头"，都需要、不替换。
        所以系统没有所谓"最新版本"，只有"哪些部件已经装上"。
      </div>

      <!-- 🇺🇸 美股线 -->
      <div class="bg-blue-50 border border-blue-200 rounded-lg p-3 mb-3">
        <p class="text-sm font-bold text-blue-900 mb-2">🇺🇸 美股线 — 每天 08:30 launchd 自动跑（<strong>v6 选股 + v7 防御 + v7.5 情报，三个一起干活</strong>）</p>
        <table class="w-full text-xs">
          <thead><tr class="border-b border-blue-200 text-left text-blue-700">
            <th class="py-1.5 px-2">部件</th>
            <th class="px-2">管什么</th>
            <th class="px-2">代码</th>
            <th class="px-2">状态</th>
          </tr></thead>
          <tbody>
            <tr class="border-b border-blue-100">
              <td class="py-1.5 px-2 font-semibold">v6 选股</td>
              <td class="px-2">4 学术因子（Piotroski + 12-1 动量 + PEAD + 分析师上修）+ Markowitz 仓位 → 每天 12 只</td>
              <td class="px-2 font-mono text-[11px]">daily_picks_v5.py · build_plan_a_v5.py</td>
              <td class="px-2 text-emerald-700 font-semibold">✅ 在跑</td>
            </tr>
            <tr class="border-b border-blue-100">
              <td class="py-1.5 px-2 font-semibold">v7 防御</td>
              <td class="px-2">VIX / 200MA / 单股 -15% 止损 / 宏观 / PCR — <strong>不选股，只出警告</strong></td>
              <td class="px-2 font-mono text-[11px]">realtime_defense.py</td>
              <td class="px-2 text-emerald-700 font-semibold">✅ 在跑</td>
            </tr>
            <tr>
              <td class="py-1.5 px-2 font-semibold">v7.5 情报</td>
              <td class="px-2">宏观 / 行业轮动 / 商品 / 内部人交易 / SPY 期权链 PCR（2026-05-11 OpenBB → yfinance 原生）</td>
              <td class="px-2 font-mono text-[11px]">openbb_intelligence.py · options_signals.py</td>
              <td class="px-2 text-emerald-700 font-semibold">✅ 在跑</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 🇨🇳 A 股线 -->
      <div class="bg-rose-50 border border-rose-200 rounded-lg p-3 mb-3">
        <p class="text-sm font-bold text-rose-900 mb-2">🇨🇳 A 股线 — 工作日 16:30 收盘后跑（<strong>v9 选股 + v8 事件，两个一起干活</strong>）</p>
        <table class="w-full text-xs">
          <thead><tr class="border-b border-rose-200 text-left text-rose-700">
            <th class="py-1.5 px-2">部件</th>
            <th class="px-2">管什么</th>
            <th class="px-2">代码</th>
            <th class="px-2">状态</th>
          </tr></thead>
          <tbody>
            <tr class="border-b border-rose-100">
              <td class="py-1.5 px-2 font-semibold">v9 选股</td>
              <td class="px-2">6 因子（Piotroski + 动量 + 反转 + 龙虎榜 + 北向 + PEAD + 政策）+ A 股实战约束</td>
              <td class="px-2 font-mono text-[11px]">a_share_picks.py · apply_a_share_constraints.py</td>
              <td class="px-2 text-emerald-700 font-semibold">✅ 在跑</td>
            </tr>
            <tr>
              <td class="py-1.5 px-2 font-semibold">v8 事件</td>
              <td class="px-2">IPO 打新 / 解禁 / 减增持 / 财报 / 政策扫描 — <strong>喂数据给 v9 用</strong></td>
              <td class="px-2 font-mono text-[11px]">ipo_daily.py · event_calendar_daily.py · policy_scan_daily.py</td>
              <td class="px-2 text-emerald-700 font-semibold">✅ 在跑</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 🔧 公共支撑 -->
      <div class="bg-slate-50 border border-slate-200 rounded-lg p-3 mb-3 text-xs text-slate-700">
        <p class="font-semibold text-slate-800 mb-1">🔧 两条线公用的基础设施</p>
        SEC 13F 机构持仓抓取 · 多源 enrichment · 跨源审计 · 反向审查 · 风险指标（VaR/Sharpe/Calmar）·
        仓位优化方法对比 · DuckDB 长期快照库（每天累加，将来做严肃回测）· AI 组合方案（v6 plan 锁定日为基线，往后看每日真实表现）
      </div>

      <!-- ⚠️ 已规划未做 -->
      <div class="bg-amber-50 border border-amber-200 rounded-lg p-3 text-xs text-slate-700">
        <p class="font-semibold text-amber-900 mb-1">⚠️ 已规划但还没做（不影响现在的系统正常工作）</p>
        <ul class="space-y-1 ml-4 list-disc">
          <li><strong>"v7 10 因子合成选股"</strong>：把上面所有信号再合成一个统一打分。<strong>分层架构其实更稳健</strong>，是否需要再合成存疑，<strong>没必要为凑版本号去做</strong></li>
          <li><strong>盘中 30 分钟轮询（intraday_refresh）</strong>：原计划盯盘中异动，目前 daily 批处理 + 飞书 webhook 已够用，未实施</li>
          <li><strong>B 路线个股深度研究</strong>：earnings call 解读 / DCF 多场景 / 同行对比 / SEC 财报深读 — 代码已写完，等付费数据源（FMP / Tushare Pro）激活</li>
        </ul>
      </div>
    </div>
  </div>

  <!-- ════════ 系统体检报告 ════════ -->
  <div class="bg-gradient-to-br from-indigo-50 to-blue-50 border-2 border-indigo-300 rounded-xl p-6 mb-8">
    <h3 class="text-2xl font-bold text-indigo-900 mb-4">🩺 系统体检报告</h3>

    <!-- ✅ 做得好的 -->
    <div class="mb-6">
      <h4 class="text-lg font-bold text-emerald-700 mb-3">✅ 已经做得好的（10 块基础坚实）</h4>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div class="bg-white rounded-lg p-3 border border-emerald-200">
          <p class="font-bold text-slate-800 mb-1">1. 多源数据体系</p>
          <p class="text-xs text-slate-600">yfinance + akshare + SEC EDGAR 13F + Finnhub + FMP + pytrends — 机构级 ~70% 水平</p>
        </div>
        <div class="bg-white rounded-lg p-3 border border-emerald-200">
          <p class="font-bold text-slate-800 mb-1">2. 跨源可信度审计</p>
          <p class="text-xs text-slate-600">core/audit.py 自动比对多源 → HIGH/MEDIUM/LOW/CONFLICT，彻底切断抖音陷阱（这是真护城河）</p>
        </div>
        <div class="bg-white rounded-lg p-3 border border-emerald-200">
          <p class="font-bold text-slate-800 mb-1">3. 学术因子模型 v1→v6</p>
          <p class="text-xs text-slate-600">Piotroski + 12-1 动量 + 反转 + PEAD（Ball-Brown 1968），每代论文背书</p>
        </div>
        <div class="bg-white rounded-lg p-3 border border-emerald-200">
          <p class="font-bold text-slate-800 mb-1">4. 6 大反向审查器</p>
          <p class="text-xs text-slate-600">主题集中度 / 13F 一致性 / 评分校准 / 估值理性 / 数据新鲜度 / 相关性矩阵</p>
        </div>
        <div class="bg-white rounded-lg p-3 border border-emerald-200">
          <p class="font-bold text-slate-800 mb-1">5. 组合优化 v6</p>
          <p class="text-xs text-slate-600">因子中性化 + Markowitz + ADV 限流 + 成本扣减 + Trade delta 自动写飞书</p>
        </div>
        <div class="bg-white rounded-lg p-3 border border-emerald-200">
          <p class="font-bold text-slate-800 mb-1">6. 工程化 + 文档</p>
          <p class="text-xs text-slate-600">仓库分层 (core/adapters/jobs/api) · 25 步 daily_refresh · 9 tab 仪表盘 · METHODOLOGY/MODEL_CARD · GitHub · launchd 自启</p>
        </div>
        <div class="bg-white rounded-lg p-3 border border-emerald-200">
          <p class="font-bold text-slate-800 mb-1">7. 执行层（2026-05-10 新增）</p>
          <p class="text-xs text-slate-600">A 股 6 因子闭环 + 实战约束 / IPO 打新日历 / 解禁减持事件 / 产业政策扫描 / 实盘防御 / OpenBB 综合情报 / DuckDB 持久化</p>
        </div>
        <div class="bg-white rounded-lg p-3 border border-emerald-200">
          <p class="font-bold text-slate-800 mb-1">8. 个股深度研究（B 路线 Phase 1）</p>
          <p class="text-xs text-slate-600">fundamental_deep / peer_compare / sec_filings / fmp_cache —— FMP 免费层可用，Phase 2-4 待付费源激活</p>
        </div>
        <div class="bg-white rounded-lg p-3 border border-emerald-200">
          <p class="font-bold text-slate-800 mb-1">9. 跨源 CONFLICT 比例闸门（2026-05-11 新增）</p>
          <p class="text-xs text-slate-600">audit_gate.py：CONFLICT &lt; 10% / min_sample 30 / max_age 36h，超阈值拦截 picks 推送（与 factor_ic_gate 对称设计）</p>
        </div>
        <div class="bg-white rounded-lg p-3 border border-emerald-200">
          <p class="font-bold text-slate-800 mb-1">10. A 股 baostock 二源校验（2026-05-11 新增）</p>
          <p class="text-xs text-slate-600">baostock_client + audit.py akshare↔baostock cross-check，价差 &gt; 2% LOW / &gt; 5% CONFLICT / 名字不一致 HIGH conflict（600519 实测 akshare 失败时已顶上）</p>
        </div>
      </div>
    </div>

    <!-- 🟡 在做但未完成 -->
    <div class="mb-6">
      <h4 class="text-lg font-bold text-amber-700 mb-3">🟡 在做但未完成（等数据/时间）</h4>
      <ul class="space-y-2 text-sm">
        <li class="bg-white rounded p-3 border border-amber-200"><strong>因子 IC 验证</strong> — 框架就位，需累积 30+ 天历史才有意义</li>
        <li class="bg-white rounded p-3 border border-amber-200"><strong>自选股·今日 Top hit rate 真实回测</strong> — picks 已积累 3 天 (2026-05-09 / 05-10 / 05-11 多次 snapshot)，仍需 1 个月数据才能可信验证</li>
        <li class="bg-white rounded p-3 border border-amber-200"><strong>B 路线 Phase 2-4</strong> — quarterly_trends / earnings_call / dcf_scenarios / forward_valuation 代码就绪，等付费数据源（FMP Starter / Tushare Pro）激活</li>
      </ul>
    </div>

    <!-- ❌ 明显缺口 -->
    <div class="mb-2">
      <h4 class="text-lg font-bold text-rose-700 mb-3">❌ 明显的缺口（按 ROI 排）</h4>
      <table class="w-full text-sm">
        <thead><tr class="border-b-2 border-rose-200 text-left text-rose-800 bg-rose-50">
          <th class="py-2 px-2">优先级</th>
          <th class="px-2">缺什么</th>
          <th class="px-2">怎么补</th>
        </tr></thead>
        <tbody>
          <tr class="border-b border-rose-100"><td class="py-2 px-2"><span class="bg-rose-500 text-white px-2 py-0.5 rounded text-xs">🔴 真痛点</span></td><td class="px-2 font-medium">A 股龙虎榜 + 北向资金明细</td><td class="px-2 text-emerald-700">Tushare Pro ¥200/年</td></tr>
          <tr class="border-b border-rose-100"><td class="py-2 px-2"><span class="bg-rose-500 text-white px-2 py-0.5 rounded text-xs">🔴 真痛点</span></td><td class="px-2 font-medium">中港股财务深度（akshare 残缺）</td><td class="px-2 text-emerald-700">Tushare Pro 同上</td></tr>
          <tr class="border-b border-rose-100"><td class="py-2 px-2"><span class="bg-rose-500 text-white px-2 py-0.5 rounded text-xs">🔴 真痛点</span></td><td class="px-2 font-medium">美股小盘 (RDDT/CCJ/BWXT) 财务深度</td><td class="px-2 text-emerald-700">FMP Starter $14/月</td></tr>
          <tr class="border-b border-rose-100"><td class="py-2 px-2"><span class="bg-emerald-500 text-white px-2 py-0.5 rounded text-xs">✅ 已建</span></td><td class="px-2 font-medium line-through text-slate-500">数据缓存层（重复请求多）</td><td class="px-2 text-emerald-700">已实现 adapters/fmp_cache.py（2026-05-10）</td></tr>
          <tr class="border-b border-rose-100"><td class="py-2 px-2"><span class="bg-emerald-500 text-white px-2 py-0.5 rounded text-xs">✅ 已建</span></td><td class="px-2 font-medium line-through text-slate-500">告警系统（只有 macOS notify）</td><td class="px-2 text-emerald-700">defense_watcher.py 15 min 巡检 + 飞书 webhook 升档推送（2026-05-11）</td></tr>
          <tr class="border-b border-rose-100"><td class="py-2 px-2"><span class="bg-amber-500 text-white px-2 py-0.5 rounded text-xs">🟡 体验</span></td><td class="px-2 font-medium">移动端适配</td><td class="px-2 text-slate-600">HTML 表格 responsive</td></tr>
          <tr class="border-b border-rose-100"><td class="py-2 px-2"><span class="bg-amber-500 text-white px-2 py-0.5 rounded text-xs">🟡 体验</span></td><td class="px-2 font-medium">AI 摘要对话（"今天有什么变化"）</td><td class="px-2 text-slate-600">集成 LLM（按需）</td></tr>
          <tr class="border-b border-rose-100"><td class="py-2 px-2"><span class="bg-amber-500 text-white px-2 py-0.5 rounded text-xs">🟡 体验</span></td><td class="px-2 font-medium">Web 服务部署（同事难协作）</td><td class="px-2 text-slate-600">api/main.py 已就绪，需上线</td></tr>
          <tr class="border-b border-rose-100"><td class="py-2 px-2"><span class="bg-emerald-500 text-white px-2 py-0.5 rounded text-xs">🟢 锦上</span></td><td class="px-2 font-medium">期权数据 / 隐含波动率</td><td class="px-2 text-slate-600">Polygon.io $29/月（不做日内可不上）</td></tr>
          <tr class="border-b border-rose-100"><td class="py-2 px-2"><span class="bg-emerald-500 text-white px-2 py-0.5 rounded text-xs">🟢 锦上</span></td><td class="px-2 font-medium">实时事件推送（13F RSS / Reddit）</td><td class="px-2 text-slate-600">免费，1 天工作量</td></tr>
          <tr class="border-b border-rose-100"><td class="py-2 px-2"><span class="bg-emerald-500 text-white px-2 py-0.5 rounded text-xs">🟢 锦上</span></td><td class="px-2 font-medium">单元测试 / CI</td><td class="px-2 text-slate-600">部署后改代码保险</td></tr>
        </tbody>
      </table>
    </div>

    <!-- 真心评估 -->
    <div class="bg-slate-900 text-white rounded-lg p-5 mt-6">
      <h4 class="text-lg font-bold mb-3">🎯 真心评估</h4>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
        <div>
          <p class="font-bold text-emerald-300 mb-1">维度上做到了"打败 70-85% 散户"</p>
          <p class="text-slate-300 text-xs">13F + 内部人 + 跨源审计 + 学术因子，<strong>这些 90% 散户都没有</strong></p>
        </div>
        <div>
          <p class="font-bold text-amber-300 mb-1">但还没真正的 alpha 输出</p>
          <p class="text-slate-300 text-xs">模型才跑 1 天，没法证明它能赚钱；要 1 个月真实数据才能证伪</p>
        </div>
        <div>
          <p class="font-bold text-blue-300 mb-1">基础设施 vs 专业机构</p>
          <p class="text-slate-300 text-xs">约 60-70%；缺的是另类数据 + 实时性</p>
        </div>
        <div>
          <p class="font-bold text-rose-300 mb-1">最大风险：沉没成本</p>
          <p class="text-slate-300 text-xs">基础已扎实，先用 1 个月看真实表现，再决定深化方向</p>
        </div>
      </div>
    </div>

    <!-- 推荐执行顺序 -->
    <div class="bg-white border-2 border-indigo-400 rounded-lg p-5 mt-4">
      <h4 class="text-lg font-bold text-indigo-900 mb-3">📅 我建议的下一步（按顺序，2026-05-11 更新）</h4>
      <ol class="space-y-2 text-sm">
        <li class="flex gap-3"><span class="bg-indigo-100 text-indigo-700 font-bold rounded-full w-7 h-7 flex items-center justify-center flex-shrink-0">1</span><div><strong>本周（无成本）</strong>：继续累积 picks 实盘数据，目标 30 天（当前 3/30，约还差 25 天）；监控 audit_gate CONFLICT 比例 + baostock 二源稳定性</div></li>
        <li class="flex gap-3"><span class="bg-indigo-100 text-indigo-700 font-bold rounded-full w-7 h-7 flex items-center justify-center flex-shrink-0">2</span><div><strong>下月（P0 充值 ¥200/年）</strong>：注册 Tushare Pro，按 baostock 接入模板做 A 股三源 reconcile（解锁龙虎榜 / 北向明细 / 一致预期）</div></li>
        <li class="flex gap-3"><span class="bg-indigo-100 text-indigo-700 font-bold rounded-full w-7 h-7 flex items-center justify-center flex-shrink-0">3</span><div><strong>30 天后（P1 充值，量计费 ¥100-300/月）</strong>：撤销旧泄漏 key 后充 Anthropic API，先在 claude_client.py 加 citation + 数字 anchor 强约束，再激活 B 路线 Phase 2-4（LLM 研报 / supply chain）</div></li>
        <li class="flex gap-3"><span class="bg-indigo-100 text-indigo-700 font-bold rounded-full w-7 h-7 flex items-center justify-center flex-shrink-0">4</span><div><strong>同时 P1.5（$14/月）</strong>：FMP Starter 解锁电话会议 transcript + 同业排名 + 小盘股 DCF 字段</div></li>
        <li class="flex gap-3"><span class="bg-indigo-100 text-indigo-700 font-bold rounded-full w-7 h-7 flex items-center justify-center flex-shrink-0">5</span><div><strong>下季度</strong>：30 天 hit rate 出来后，根据回测决定是否深化（Polygon / 期权）—— 不做日内可不上</div></li>
      </ol>
    </div>
  </div>

  <hr class="my-8 border-slate-300">

  <!-- ════════ 升级建议（原内容）════════ -->
  <h3 class="text-2xl font-bold text-slate-900 mb-6">💰 数据源升级方案详情</h3>
  <p class="text-sm text-slate-600 mb-6">下面是按 ROI 排序的付费数据源选项 — 体检报告里的"红色缺口"对应这里的具体方案。</p>

  <!-- 当前痛点 -->
  <div class="bg-amber-50 border border-amber-200 rounded-xl p-5 mb-6">
    <h3 class="text-lg font-bold text-amber-900 mb-3">🎯 系统当前的真实数据缺口</h3>
    <table class="w-full text-sm">
      <thead>
        <tr class="border-b border-amber-300 text-left text-amber-700">
          <th class="py-2">数据维度</th>
          <th>当前状态</th>
          <th>痛点严重度</th>
        </tr>
      </thead>
      <tbody>
        <tr class="border-b border-amber-100"><td class="py-2 font-medium">美股大盘股财报 / DCF</td><td>✅ FMP 免费层</td><td>已解决</td></tr>
        <tr class="border-b border-amber-100"><td class="py-2 font-medium">美股新闻 / 内部人 / 分析师</td><td>✅ Finnhub 免费层</td><td>已解决</td></tr>
        <tr class="border-b border-amber-100"><td class="py-2 font-medium">13F 大佬持仓变动</td><td>✅ SEC EDGAR 直拉</td><td>已解决</td></tr>
        <tr class="border-b border-amber-100"><td class="py-2 font-medium">A 股财报 / 龙虎榜 / 北向资金</td><td>⚠️ akshare 爬东财，常态化限流</td><td><span class="text-red-600 font-bold">🔴 大短板</span></td></tr>
        <tr class="border-b border-amber-100"><td class="py-2 font-medium">中港股财务深度</td><td>⚠️ 残缺，财报字段不全</td><td><span class="text-red-600 font-bold">🔴 大短板</span></td></tr>
        <tr class="border-b border-amber-100"><td class="py-2 font-medium">美股小盘 (RDDT/CCJ/BWXT/XYL) DCF</td><td>⚠️ FMP 免费层不覆盖</td><td><span class="text-amber-600 font-bold">🟡 中等</span></td></tr>
        <tr class="border-b border-amber-100"><td class="py-2 font-medium">日股 / 澳股 / 英股 ADR</td><td>⚠️ 完全没有</td><td><span class="text-amber-600 font-bold">🟡 中等</span></td></tr>
        <tr><td class="py-2 font-medium">期权 / 隐含波动率 / 短利</td><td>⚠️ 完全没有</td><td><span class="text-slate-500">🟢 不做日内不重要</span></td></tr>
      </tbody>
    </table>
  </div>

  <!-- 推荐组合 -->
  <div class="bg-gradient-to-br from-emerald-50 to-teal-50 border-2 border-emerald-300 rounded-xl p-6 mb-6">
    <h3 class="text-2xl font-bold text-emerald-900 mb-4">⭐ 推荐组合（年成本 ¥1400）</h3>
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div class="bg-white rounded-lg p-4 border border-emerald-200">
        <div class="flex justify-between items-start mb-2">
          <h4 class="text-lg font-bold text-emerald-700">🥇 FMP Starter</h4>
          <span class="text-xl font-bold">$14/月 (¥100)</span>
        </div>
        <p class="text-sm text-slate-700 mb-3"><strong>已经在试用免费层。</strong>升级后解决：</p>
        <ul class="text-sm text-slate-700 space-y-1 list-disc pl-5">
          <li>RDDT / CCJ / BWXT / XYL / VRT / AVGO 全部覆盖（你"下一波稀缺资源"主题股）</li>
          <li>Earnings transcripts 全文（财报会议文字稿）</li>
          <li>30 年历史财报（vs 5 年）</li>
          <li>API 限额 250→750 calls/天</li>
        </ul>
      </div>
      <div class="bg-white rounded-lg p-4 border border-emerald-200">
        <div class="flex justify-between items-start mb-2">
          <h4 class="text-lg font-bold text-emerald-700">🥈 Tushare Pro</h4>
          <span class="text-xl font-bold">¥200/年</span>
        </div>
        <p class="text-sm text-slate-700 mb-3"><strong>解决 自选股中 30%+ A股/港股的数据脆弱问题。</strong></p>
        <ul class="text-sm text-slate-700 space-y-1 list-disc pl-5">
          <li><strong>龙虎榜</strong>（A 股最强日内信号）</li>
          <li><strong>北向资金每日明细</strong>（外资动向）</li>
          <li>限售股解禁、股东户数变动、大宗交易</li>
          <li>公募基金重仓股（季度）</li>
          <li>国内量化圈一致认证最稳定的 A 股数据源</li>
        </ul>
      </div>
    </div>
    <p class="text-sm text-emerald-800 mt-4"><strong>总成本：¥1400/年（≈ ¥117/月）</strong> — 比一次全家火锅还便宜，但数据维度直接拉到机构级 70%</p>
  </div>

  <!-- 已试用证据：FMP NVDA 案例 -->
  <div class="bg-white rounded-xl shadow border border-slate-200 p-5 mb-6">
    <h3 class="text-lg font-bold text-slate-900 mb-3">🧪 试用证据：FMP 在 NVDA 上的真实表现</h3>
    <p class="text-sm text-slate-600 mb-4">5 年损益表立刻能看出 ChatGPT 引爆的精确拐点（这是 yfinance 永远做不到的）：</p>
    <div class="overflow-x-auto">
      <table class="w-full text-sm font-mono">
        <thead><tr class="border-b-2 border-slate-300 bg-slate-50">
          <th class="py-2 px-2 text-left">财年</th><th class="text-right">Revenue</th><th class="text-right">Net Income</th><th class="text-right">EPS</th><th class="text-right">毛利率</th><th class="text-right">净利率</th><th>注释</th>
        </tr></thead>
        <tbody>
          <tr class="border-b border-slate-100"><td class="py-1 px-2">FY2022</td><td class="text-right">$26.9B</td><td class="text-right">$9.8B</td><td class="text-right">$0.39</td><td class="text-right">64.9%</td><td class="text-right">36.2%</td><td class="text-slate-500">平庸期</td></tr>
          <tr class="border-b border-slate-100 bg-rose-50"><td class="py-1 px-2 font-bold">FY2023</td><td class="text-right">$27.0B</td><td class="text-right">$4.4B</td><td class="text-right">$0.18</td><td class="text-right">56.9%</td><td class="text-right text-red-600 font-bold">16.2%</td><td class="text-rose-600">❄️ 加密寒冬</td></tr>
          <tr class="border-b border-slate-100 bg-emerald-50"><td class="py-1 px-2 font-bold">FY2024</td><td class="text-right">$60.9B</td><td class="text-right">$29.8B</td><td class="text-right">$1.21</td><td class="text-right">72.7%</td><td class="text-right text-emerald-700 font-bold">48.8%</td><td class="text-emerald-700">🚀 ChatGPT 引爆</td></tr>
          <tr class="border-b border-slate-100"><td class="py-1 px-2">FY2025</td><td class="text-right">$130.5B</td><td class="text-right">$72.9B</td><td class="text-right">$2.97</td><td class="text-right">75.0%</td><td class="text-right">55.8%</td><td class="text-slate-500">续航</td></tr>
          <tr><td class="py-1 px-2 font-bold">FY2026</td><td class="text-right">$215.9B</td><td class="text-right">$120.1B</td><td class="text-right">$4.93</td><td class="text-right">71.1%</td><td class="text-right">55.6%</td><td class="text-violet-700 font-bold">4 年 8 倍营收</td></tr>
        </tbody>
      </table>
    </div>
    <div class="grid grid-cols-1 md:grid-cols-3 gap-3 mt-4">
      <div class="bg-emerald-50 border border-emerald-200 rounded p-3">
        <p class="text-xs text-emerald-700">FMP DCF 内在价值</p>
        <p class="text-2xl font-bold text-emerald-900">$246.58</p>
        <p class="text-xs text-emerald-600">vs 当前价 $215.66 → 🟢 +14.3% 上涨空间</p>
      </div>
      <div class="bg-blue-50 border border-blue-200 rounded p-3">
        <p class="text-xs text-blue-700">EV / EBITDA TTM</p>
        <p class="text-2xl font-bold text-blue-900">36.2x</p>
        <p class="text-xs text-blue-600">行业均值约 15-20，估值偏高</p>
      </div>
      <div class="bg-violet-50 border border-violet-200 rounded p-3">
        <p class="text-xs text-violet-700">分析师 2028 营收预期</p>
        <p class="text-2xl font-bold text-violet-900">$485B</p>
        <p class="text-xs text-violet-600">29 个分析师覆盖（vs 当前 $216B）</p>
      </div>
    </div>
  </div>

  <!-- 看你需要不 - 中等优先级 -->
  <div class="bg-yellow-50 border border-yellow-200 rounded-xl p-5 mb-6">
    <h3 class="text-lg font-bold text-yellow-900 mb-3">🟡 看你需要不（次优先级）</h3>
    <div class="space-y-3">
      <div class="bg-white rounded p-4 border border-yellow-300">
        <div class="flex justify-between mb-1">
          <span class="font-bold text-slate-800">Polygon.io Starter</span>
          <span class="font-bold">$29/月 (¥210)</span>
        </div>
        <p class="text-xs text-slate-600 mb-2"><strong>适合：</strong>想看期权数据（隐含波动率 / put-call ratio），比 yfinance 稳定 10 倍</p>
        <p class="text-xs text-slate-600"><strong>不适合：</strong>不做日内交易（K 线分钟级数据用不到）；不交易期权</p>
        <p class="text-xs text-violet-700 mt-1"><strong>建议：</strong>等想看期权数据时再上</p>
      </div>
      <div class="bg-white rounded p-4 border border-yellow-300">
        <div class="flex justify-between mb-1">
          <span class="font-bold text-slate-800">EODHD</span>
          <span class="font-bold">$20/月 (¥145)</span>
        </div>
        <p class="text-xs text-slate-600 mb-2"><strong>适合：</strong>想全面覆盖 SoftBank / Advantest（日股）/ Lynas / Appen（澳股）/ Rolls-Royce（英股 ADR）/ Kazatomprom（哈萨克）</p>
        <p class="text-xs text-violet-700 mt-1"><strong>建议：</strong>自选股里这类股 &lt; 5 只，性价比一般</p>
      </div>
    </div>
  </div>

  <!-- 不推荐 -->
  <div class="bg-rose-50 border border-rose-200 rounded-xl p-5 mb-6">
    <h3 class="text-lg font-bold text-rose-900 mb-3">❌ 不推荐订阅（ROI 低或够不上）</h3>
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead><tr class="border-b-2 border-rose-300 text-left text-rose-700">
          <th class="py-2 px-2">工具</th><th>价格</th><th>为什么不推荐</th>
        </tr></thead>
        <tbody>
          <tr class="border-b border-rose-100"><td class="py-2 px-2 font-medium">Finnhub Premium</td><td>$50/月</td><td>免费层 + FMP + SEC 已覆盖 80%；唯一新增的 Reuters/Bloomberg news 用 WebSearch 能补</td></tr>
          <tr class="border-b border-rose-100"><td class="py-2 px-2 font-medium">Alpha Vantage</td><td>$25/月</td><td>50+ 技术指标，但本系统不做技术分析</td></tr>
          <tr class="border-b border-rose-100"><td class="py-2 px-2 font-medium">AlphaSense</td><td>$1500+/月</td><td>机构产品，AI 搜 SEC filings；个人用不到</td></tr>
          <tr class="border-b border-rose-100"><td class="py-2 px-2 font-medium">YipitData</td><td>数千/月</td><td>另类数据（信用卡、APP 下载）— 对冲基金护城河，个人用不到</td></tr>
          <tr class="border-b border-rose-100"><td class="py-2 px-2 font-medium">Bloomberg Terminal</td><td>$24,000/年</td><td>不用想了</td></tr>
          <tr><td class="py-2 px-2 font-medium">WSJ / FT 订阅</td><td>$200/年</td><td>文章可以让 AI 帮忙摘要，不需要正式订阅</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 完全免费的补充 -->
  <div class="bg-sky-50 border border-sky-200 rounded-xl p-5 mb-6">
    <h3 class="text-lg font-bold text-sky-900 mb-3">❄️ 完全免费的补充（值得加，零成本）</h3>
    <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
      <div class="bg-white rounded p-3 border border-sky-200">
        <p class="font-medium text-slate-800">📡 SEC EDGAR RSS</p>
        <p class="text-xs text-slate-600 mt-1">大佬 13F 实时推送（vs 每天定时拉，领先市场 30 分钟）</p>
      </div>
      <div class="bg-white rounded p-3 border border-sky-200">
        <p class="font-medium text-slate-800">🗨️ Reddit API (PRAW)</p>
        <p class="text-xs text-slate-600 mt-1">监控 r/wallstreetbets 散户情绪</p>
      </div>
      <div class="bg-white rounded p-3 border border-sky-200">
        <p class="font-medium text-slate-800">👤 OpenInsider 爬虫</p>
        <p class="text-xs text-slate-600 mt-1">美股内部人交易补充（Finnhub 备份源）</p>
      </div>
      <div class="bg-white rounded p-3 border border-sky-200">
        <p class="font-medium text-slate-800">📊 OpenBB Hub</p>
        <p class="text-xs text-slate-600 mt-1">集成多个免费源，可作为容灾备份</p>
      </div>
    </div>
  </div>

  <!-- 决策矩阵 -->
  <div class="bg-slate-900 text-white rounded-xl p-6">
    <h3 class="text-xl font-bold mb-4">🎯 决策矩阵 — 给同事看的版本</h3>
    <table class="w-full text-sm">
      <thead><tr class="border-b border-slate-700 text-left text-slate-300">
        <th class="py-2">优先级</th><th>工具</th><th>年成本</th><th>解决什么</th><th>不上的代价</th>
      </tr></thead>
      <tbody>
        <tr class="border-b border-slate-700">
          <td class="py-3"><span class="bg-emerald-500 px-2 py-1 rounded text-xs font-bold">必上</span></td>
          <td class="font-bold">FMP Starter</td>
          <td>¥1200</td>
          <td>美股小盘股 DCF + earnings transcripts + 30 年财报</td>
          <td class="text-rose-300">看不到 RDDT/CCJ/BWXT 财务深度</td>
        </tr>
        <tr class="border-b border-slate-700">
          <td class="py-3"><span class="bg-emerald-500 px-2 py-1 rounded text-xs font-bold">必上</span></td>
          <td class="font-bold">Tushare Pro</td>
          <td>¥200</td>
          <td>A 股龙虎榜 + 北向 + 大宗交易 + 解禁</td>
          <td class="text-rose-300">akshare 限流 → 中港股数据时不时空</td>
        </tr>
        <tr class="border-b border-slate-700">
          <td class="py-3"><span class="bg-amber-500 px-2 py-1 rounded text-xs font-bold">看需求</span></td>
          <td class="font-bold">Polygon.io</td>
          <td>¥2500</td>
          <td>美股期权 + 实时分钟 K 线 + 短利</td>
          <td class="text-slate-400">不做日内/期权可不上</td>
        </tr>
        <tr class="border-b border-slate-700">
          <td class="py-3"><span class="bg-amber-500 px-2 py-1 rounded text-xs font-bold">看需求</span></td>
          <td class="font-bold">EODHD</td>
          <td>¥1700</td>
          <td>日股 / 澳股 / 英股 ADR</td>
          <td class="text-slate-400">这类股 自选股里 &lt; 5 只</td>
        </tr>
        <tr>
          <td class="py-3"><span class="bg-slate-500 px-2 py-1 rounded text-xs font-bold">不推荐</span></td>
          <td class="font-bold">其他</td>
          <td>—</td>
          <td>Finnhub Premium / AlphaSense / Bloomberg / YipitData 等</td>
          <td class="text-emerald-300">省钱</td>
        </tr>
      </tbody>
    </table>
    <div class="mt-5 pt-4 border-t border-slate-700">
      <p class="text-xs text-slate-400">💡 <strong>给同事的一句话</strong>：花 ¥1400/年（FMP + Tushare）就能让这套系统在数据维度上接近机构 70% 水平。再贵的就没有边际收益了。</p>
    </div>
  </div>

  <!-- 最后底部时间戳 -->
  <p class="text-xs text-slate-500 mt-4 text-right">本页面由 build_stock_dashboard_html.py 自动生成 · 决策内容来自系统实测 NVDA + 11 家 13F 机构数据</p>
</section>

<!-- ============ Footer ============ -->
<footer class="bg-slate-900 text-slate-300 py-8 mt-12">
  <div class="max-w-7xl mx-auto px-6 text-sm">
    <p class="mb-2"><strong class="text-white">免责声明</strong>：本看板由 Claude AI 基于公开信息生成，仅作研究学习参考，<strong class="text-rose-300">绝不构成任何投资建议</strong>。</p>
    <p class="mb-2">投资有风险，所有交易决策需自行判断、自负盈亏。本看板的数据可能滞后、错误或解读偏差。</p>
    <p class="text-slate-500">数据源：本地 DuckDB 自选股表 · WebSearch 抓取的最新公司财报 · 数据更新时间 {UPDATE_TIME}</p>
  </div>
</footer>

<script>
// ============ 数据注入 · 2026-05-11 起 DuckDB single source of truth ============
// RECORDS / PICKS / SIMULATION 来自飞书 watchlist API 实时拉（仍保留），其余全部走 DuckDB。
const RECORDS      = {RECORDS_JSON};
const PICKS        = {PICKS_JSON};
const WATCHLIST_RATINGS = {WATCHLIST_RATINGS_JSON};  // {code: {rating, total_score, ai_score}}  来自 picks 最新一日
const SIMULATION   = {SIMULATION_JSON};
// 2026-05-11 PM: watchlist 链条定位信息(chain/chain_tier/chain_role/layman_intro)
// 按 code 索引 → 任何 tab 显示股票时,Stock Pill 都能查到上下文
const WATCHLIST_CHAIN_INFO = {WATCHLIST_CHAIN_INFO_JSON};
// 2026-05-11 PM: AI 推荐的历史推荐 + 准确度跟踪(discovery_history JOIN tracking)
// 数组,每条 = 一次 (generated_date, ticker) 推荐 + 它的 alpha 数据
const DISCOVERY_HISTORY = {DISCOVERY_HISTORY_JSON};
const RISK_METRICS = {RISK_METRICS_JSON_DB};
const TRACK_13F    = {TRACK_13F_JSON_DB};
const OPTIMIZATION = {OPTIMIZATION_JSON_DB};
const PLAN_A_V6    = {PLAN_A_V6_JSON_DB};
const DISCOVERY    = {DISCOVERY_JSON};
// AI 组合方案数据：A 静态（buy-and-hold from inception） / C 动态（每周一 rebalance）
const _BACKTEST    = {PLAN_BACKTEST_JSON_DB};
const _DYNAMIC     = {PLAN_DYNAMIC_JSON_DB};

// ============ Watchlist CRUD（调本地 FastAPI · DuckDB 是权威）============
const WATCHLIST_API_BASE = "http://127.0.0.1:8765";
let _watchlistCache = [];
let _watchlistEditCode = null;  // null = 新增模式；非空 = 编辑该 code

async function _watchlistApiCall(method, path, body) {
  const opts = { method, headers: {"Content-Type": "application/json"} };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const r = await fetch(WATCHLIST_API_BASE + path, opts);
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(`HTTP ${r.status}: ${err.detail || r.statusText}`);
  }
  return r.json();
}

async function _checkApiStatus() {
  const el = document.getElementById("watchlist-api-status");
  const hint = document.getElementById("wl-offline-hint");
  if (!el) return;
  try {
    await _watchlistApiCall("GET", "/health");
    el.textContent = "✓ 已连接";
    el.className = "text-xs px-2 py-0.5 rounded bg-emerald-100 text-emerald-700";
    if (hint) hint.classList.add("hidden");
    return true;
  } catch (e) {
    el.textContent = "✗ 未启动";
    el.className = "text-xs px-2 py-0.5 rounded bg-rose-100 text-rose-700";
    if (hint) hint.classList.remove("hidden");
    return false;
  }
}

function _esc(s) {
  return (s || "").toString().replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// 持仓「持」标记 — 数据来自 _holdingsCache（DB holdings 表）。同 code 多笔自动聚合显示总股数
function _heldBadge(code) {
  if (!_holdingsCache || _holdingsCache.length === 0) return "";
  const lots = _holdingsCache.filter(h => h.code === code);
  if (lots.length === 0) return "";
  const totalShares = lots.reduce((a, b) => a + (b.shares || 0), 0);
  const lotInfo = lots.length > 1 ? `（${lots.length} 笔）` : "";
  return ` <span class="inline-flex px-1.5 py-0.5 rounded text-[10px] font-bold bg-blue-100 text-blue-700 align-middle" title="你已持仓 ${totalShares} 股${lotInfo}">💼 持</span>`;
}

// 自选股 AI 评级 badge — 数据来自 picks 表最新一日（daily_picks_v5 学术因子）
//   注: 命名为 _wlRatingBadge 避免和 line 4214 的 _ratingBadge(rating) 同名冲突
//   ⭐⭐⭐ 强烈推荐(z≥1) → ✅ 推荐
//   ⭐⭐ 推荐(z≥0.5) / ⭐ 关注 → ⚠️ 观察
//   不在 picks → — 未评级（z 未达入选门槛 或 数据缺失）
//   ❌ 不建议 待补：当前 daily_picks_v5 只存入选股，需要它输出 z < -0.5 的股才能给负向评级
function _wlRatingBadge(code) {
  const info = WATCHLIST_RATINGS[code];
  if (!info || !info.rating) {
    return '<span class="text-slate-400 text-xs" title="未在今日 daily_picks 入选池">— 未评级</span>';
  }
  const r = String(info.rating);
  const score = info.total_score != null ? ` · ${info.total_score}` : "";
  if (r.includes("⭐⭐⭐")) {
    return `<span class="inline-flex px-2 py-0.5 rounded text-xs font-bold bg-emerald-100 text-emerald-700" title="${_esc(r)}${score}">✅ 推荐</span>`;
  }
  if (r.includes("⭐⭐") || r.includes("⭐")) {
    return `<span class="inline-flex px-2 py-0.5 rounded text-xs font-bold bg-amber-100 text-amber-700" title="${_esc(r)}${score}">⚠️ 观察</span>`;
  }
  return '<span class="text-slate-400 text-xs">— 未评级</span>';
}

// 链条层级 → 颜色 / 序号(用于排序)
const TIER_STYLE = {
  "核心": { bg: "bg-violet-100", txt: "text-violet-800", order: 0 },
  "一线": { bg: "bg-blue-100",   txt: "text-blue-800",   order: 1 },
  "二线": { bg: "bg-emerald-100",txt: "text-emerald-800",order: 2 },
  "三线": { bg: "bg-amber-100",  txt: "text-amber-800",  order: 3 },
  "N/A":  { bg: "bg-slate-100",  txt: "text-slate-500",  order: 9 },
};
const ROLE_STYLE = {
  "IDM":      "bg-rose-50 text-rose-700",
  "GPU":      "bg-fuchsia-50 text-fuchsia-700",
  "网络芯片":  "bg-sky-50 text-sky-700",
  "设备":     "bg-cyan-50 text-cyan-700",
  "材料":     "bg-lime-50 text-lime-700",
  "封测":     "bg-teal-50 text-teal-700",
  "EDA":      "bg-indigo-50 text-indigo-700",
  "服务器":    "bg-orange-50 text-orange-700",
  "应用层":    "bg-violet-50 text-violet-700",
  "服务":     "bg-blue-50 text-blue-700",
  "基础设施":  "bg-emerald-50 text-emerald-700",
  "对照":     "bg-slate-50 text-slate-500",
};
function _tierBadge(tier) {
  const s = TIER_STYLE[tier] || TIER_STYLE["N/A"];
  return `<span class="inline-block px-2 py-0.5 rounded text-xs font-semibold ${s.bg} ${s.txt}">${_esc(tier || "—")}</span>`;
}
function _roleBadge(role) {
  if (!role) return `<span class="text-slate-400 text-xs">—</span>`;
  const cls = ROLE_STYLE[role] || "bg-slate-100 text-slate-700";
  return `<span class="inline-block px-2 py-0.5 rounded text-xs ${cls}">${_esc(role)}</span>`;
}
function _chainBadges(chain) {
  if (!chain) return `<span class="text-slate-400 text-xs">—</span>`;
  return chain.split(",").map(c => c.trim()).filter(Boolean).map(c =>
    `<span class="inline-block px-2 py-0.5 rounded text-xs bg-slate-100 text-slate-700 mr-1 mb-0.5">${_esc(c)}</span>`
  ).join("");
}

// ============ Stock Pill 组件 ============
// 复用组件:任何 tab 显示股票时,都用 stockPill(code) 替代裸 code/name
// 自动从 WATCHLIST_CHAIN_INFO 查上下文(链条/层级/角色/一句话),不在自选股 显示 [未观察]
// 用法:
//   stockPill("NVDA")                          → 完整 pill(代码+名字+badges+hover 提示一句话)
//   stockPill("NVDA", {layout: "mini"})        → 紧凑(只 badges,不重复代码名字)
//   stockPill("NVDA", {nameOverride: "英伟达"}) → 用自定义名字覆盖
function stockPill(code, opts) {
  opts = opts || {};
  const info = (typeof WATCHLIST_CHAIN_INFO !== "undefined" ? WATCHLIST_CHAIN_INFO : {})[code] || {};
  const layout = opts.layout || "inline";  // inline | mini
  const name = opts.nameOverride || info.name || "";
  const chain = (info.chain || "").split(",").map(s => s.trim()).filter(Boolean);
  const primaryChain = chain[0] || null;
  const tier = info.chain_tier;
  const role = info.chain_role;
  const intro = info.layman_intro || "";
  const inWatchlist = !!info.name;

  // 紧凑模式:只显示 chain · tier · role 三个 badge,不重复 code/name
  if (layout === "mini") {
    if (!inWatchlist) {
      return `<span class="text-slate-300 text-xs cursor-help" title="不在自选股 · 可以加入后标注链条信息">—</span>`;
    }
    const parts = [];
    if (primaryChain) parts.push(`<span class="px-1.5 py-0 rounded text-[10px] bg-slate-100 text-slate-700">${_esc(primaryChain)}</span>`);
    if (tier) {
      const s = TIER_STYLE[tier] || TIER_STYLE["N/A"];
      parts.push(`<span class="px-1.5 py-0 rounded text-[10px] font-semibold ${s.bg} ${s.txt}">${_esc(tier)}</span>`);
    }
    if (role) {
      const cls = ROLE_STYLE[role] || "bg-slate-100 text-slate-700";
      parts.push(`<span class="px-1.5 py-0 rounded text-[10px] ${cls}">${_esc(role)}</span>`);
    }
    const wrap = intro
      ? `<span title="${_esc(intro)}" class="inline-flex items-center gap-1 cursor-help">${parts.join(" ")}</span>`
      : `<span class="inline-flex items-center gap-1">${parts.join(" ")}</span>`;
    return wrap;
  }

  // inline 模式(默认):代码 + 名字 + 链条/层级/角色 badge,hover 显示一句话
  const titleAttr = intro
    ? ` title="${_esc(intro)}"`
    : (inWatchlist ? "" : ' title="不在自选股,无链条信息"');
  const codeName = `<span class="font-mono text-sm font-bold text-slate-900">${_esc(code)}</span>` +
                   (name ? ` <span class="text-sm text-slate-700">${_esc(name)}</span>` : "");
  const badges = [];
  if (!inWatchlist) {
    badges.push(`<span class="text-[10px] text-slate-400 cursor-help" title="不在自选股 · 可以加入后标注链条信息">— 待标注</span>`);
  } else {
    if (primaryChain) badges.push(`<span class="px-1.5 py-0 rounded text-[10px] bg-slate-100 text-slate-700">${_esc(primaryChain)}</span>`);
    if (tier) {
      const s = TIER_STYLE[tier] || TIER_STYLE["N/A"];
      badges.push(`<span class="px-1.5 py-0 rounded text-[10px] font-semibold ${s.bg} ${s.txt}">${_esc(tier)}</span>`);
    }
    if (role) {
      const cls = ROLE_STYLE[role] || "bg-slate-100 text-slate-700";
      badges.push(`<span class="px-1.5 py-0 rounded text-[10px] ${cls}">${_esc(role)}</span>`);
    }
  }
  return `<div class="inline-flex flex-col gap-0.5"${titleAttr}>
    <div class="flex items-center gap-2 flex-wrap">${codeName}</div>
    <div class="flex items-center gap-1 flex-wrap">${badges.join("")}</div>
  </div>`;
}

function _populateWatchlistFilters() {
  const chains = new Set(), roles = new Set();
  _watchlistCache.forEach(r => {
    (r.chain || "").split(",").map(s => s.trim()).filter(Boolean).forEach(c => chains.add(c));
    if (r.chain_role) roles.add(r.chain_role);
  });
  const chainSel = document.getElementById("wl-filter-chain");
  const roleSel = document.getElementById("wl-filter-role");
  if (chainSel && chainSel.options.length <= 1) {
    [...chains].sort().forEach(c => {
      const opt = document.createElement("option");
      opt.value = c; opt.textContent = c;
      chainSel.appendChild(opt);
    });
  }
  if (roleSel && roleSel.options.length <= 1) {
    [...roles].sort().forEach(c => {
      const opt = document.createElement("option");
      opt.value = c; opt.textContent = c;
      roleSel.appendChild(opt);
    });
  }
}

async function loadWatchlistTable() {
  const ok = await _checkApiStatus();
  const tbody = document.getElementById("watchlist-table-body");
  const countEl = document.getElementById("watchlist-count");
  if (!ok) {
    tbody.innerHTML = `<tr><td colspan="9" class="px-3 py-8 text-center text-rose-700 text-sm">
      ⚠️ 本地 API 未启动 — 请在 terminal 跑：<br>
      <code class="text-xs bg-rose-50 px-2 py-1 mt-2 inline-block rounded">uvicorn stock_research.api.main:app --port 8765</code>
    </td></tr>`;
    countEl.textContent = "";
    return;
  }
  try {
    if (_watchlistCache.length === 0) {
      _watchlistCache = await _watchlistApiCall("GET", "/api/watchlist");
    }
    // 确保持仓 cache 已加载,这样 _heldBadge 能正确显示「💼 持」标记
    if (!_holdingsLoaded) await _ensureHoldingsLoaded();
    // 总是调用（line 2263 内部 guard 保证幂等）— 修复:cache 可能被其他 tab 先填(如 AI 推荐的"✓ 已在自选"标记)导致筛选项漏初始化
    _populateWatchlistFilters();
    const fChain = document.getElementById("wl-filter-chain")?.value || "";
    const fTier  = document.getElementById("wl-filter-tier")?.value || "";
    const fRole  = document.getElementById("wl-filter-role")?.value || "";
    const fKw    = (document.getElementById("wl-filter-keyword")?.value || "").trim().toLowerCase();
    const filtered = _watchlistCache.filter(r => {
      if (fChain && !(r.chain || "").split(",").map(s => s.trim()).includes(fChain)) return false;
      if (fTier  && r.chain_tier !== fTier) return false;
      if (fRole  && r.chain_role !== fRole) return false;
      if (fKw) {
        const hay = [r.code, r.name, r.layman_intro, r.industry].map(x => (x || "").toLowerCase()).join(" ");
        if (!hay.includes(fKw)) return false;
      }
      return true;
    });
    filtered.sort((a, b) => {
      const ca = (a.chain || "zz").split(",")[0].trim();
      const cb = (b.chain || "zz").split(",")[0].trim();
      if (ca !== cb) return ca.localeCompare(cb);
      const ta = (TIER_STYLE[a.chain_tier] || TIER_STYLE["N/A"]).order;
      const tb = (TIER_STYLE[b.chain_tier] || TIER_STYLE["N/A"]).order;
      return ta - tb;
    });
    countEl.textContent = `${filtered.length} / ${_watchlistCache.length} 条`;
    if (filtered.length === 0) {
      tbody.innerHTML = `<tr><td colspan="10" class="px-3 py-8 text-center text-slate-500 text-sm">没有匹配的记录</td></tr>`;
      return;
    }
    tbody.innerHTML = filtered.map(r => `
      <tr class="hover:bg-slate-50">
        <td class="px-3 py-2 font-mono text-xs font-bold text-slate-900 whitespace-nowrap">${_esc(r.code)}${_heldBadge(r.code)}</td>
        <td class="px-3 py-2 text-sm text-slate-800 whitespace-nowrap">${_esc(r.name)}</td>
        <td class="px-3 py-2">${_chainBadges(r.chain)}</td>
        <td class="px-3 py-2">${_tierBadge(r.chain_tier)}</td>
        <td class="px-3 py-2">${_roleBadge(r.chain_role)}</td>
        <td class="px-3 py-2 text-xs text-slate-700 max-w-md">${_esc(r.layman_intro) || '<span class="text-slate-400">—</span>'}</td>
        <td class="px-3 py-2 text-xs text-slate-500 whitespace-nowrap">${_esc(r.market)}</td>
        <td class="px-3 py-2 whitespace-nowrap">${_wlRatingBadge(r.code)}</td>
        <td class="px-3 py-2 text-xs whitespace-nowrap">${_esc(r.status)}</td>
        <td class="px-3 py-2 text-right space-x-1 whitespace-nowrap">
          <button onclick="openWatchlistEditor('${_esc(r.code)}')" class="text-xs px-2 py-1 bg-slate-100 hover:bg-violet-100 text-slate-700 rounded">✏️</button>
          <button onclick="deleteWatchlistItem('${_esc(r.code)}')" class="text-xs px-2 py-1 bg-rose-100 hover:bg-rose-200 text-rose-700 rounded">🗑️</button>
        </td>
      </tr>
    `).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="10" class="px-3 py-8 text-center text-rose-700 text-sm">加载失败：${_esc(e.message)}</td></tr>`;
  }
}
async function forceReloadWatchlist() { _watchlistCache = []; await loadWatchlistTable(); }

// ============ 🌳 产业链全景渲染 ============
async function loadChainOverview() {
  const statusEl = document.getElementById("chain-api-status");
  const countEl = document.getElementById("chain-count");
  const summaryEl = document.getElementById("chain-summary");
  const container = document.getElementById("chain-cards-container");
  if (!container) return;

  const ok = await _checkApiStatus();
  if (statusEl) statusEl.className = ok
    ? "px-2 py-0.5 rounded bg-emerald-100 text-emerald-700"
    : "px-2 py-0.5 rounded bg-rose-100 text-rose-700";
  if (statusEl) statusEl.textContent = ok ? "✓ API 已连接" : "✗ API 未启动";
  if (!ok) {
    container.innerHTML = `<div class="bg-rose-50 border border-rose-200 rounded p-4 text-sm text-rose-700">
      本地 API 未启动 — 请运行: <code class="bg-rose-100 px-2 py-0.5 rounded">uvicorn stock_research.api.main:app --port 8765</code>
    </div>`;
    return;
  }
  try {
    if (_watchlistCache.length === 0) {
      _watchlistCache = await _watchlistApiCall("GET", "/api/watchlist");
    }
    const byChain = {};
    _watchlistCache.forEach(r => {
      const chains = (r.chain || "").split(",").map(s => s.trim()).filter(Boolean);
      if (chains.length === 0) chains.push("(未分类)");
      chains.forEach(c => { (byChain[c] = byChain[c] || []).push(r); });
    });
    const chainsSorted = Object.keys(byChain).sort((a, b) => byChain[b].length - byChain[a].length);
    countEl.textContent = `${chainsSorted.length} 条链 · ${_watchlistCache.length} 个标的(去重)`;

    summaryEl.innerHTML = chainsSorted.map(c => {
      const n = byChain[c].length;
      const anchor = "chain-card-" + c.replace(/[^a-zA-Z0-9]/g, "_");
      return `<a href="#${anchor}" onclick="document.getElementById('${anchor}')?.scrollIntoView({behavior:'smooth',block:'start'});return false;"
        class="inline-flex items-center gap-1.5 px-3 py-1 bg-white border border-slate-200 hover:border-violet-400 hover:bg-violet-50 rounded-full text-xs text-slate-700 transition">
        <span class="font-medium">${_esc(c)}</span>
        <span class="text-slate-400">·</span>
        <span class="text-violet-600 font-semibold">${n}</span>
      </a>`;
    }).join("");

    container.innerHTML = chainsSorted.map(c => {
      const rows = byChain[c];
      rows.sort((a, b) => {
        const ta = (TIER_STYLE[a.chain_tier] || TIER_STYLE["N/A"]).order;
        const tb = (TIER_STYLE[b.chain_tier] || TIER_STYLE["N/A"]).order;
        if (ta !== tb) return ta - tb;
        return (a.code || "").localeCompare(b.code || "");
      });
      const tiers = ["核心", "一线", "二线", "三线", "N/A"];
      const byTier = {};
      tiers.forEach(t => byTier[t] = []);
      rows.forEach(r => {
        const t = r.chain_tier && byTier[r.chain_tier] !== undefined ? r.chain_tier : "N/A";
        byTier[t].push(r);
      });
      const anchor = "chain-card-" + c.replace(/[^a-zA-Z0-9]/g, "_");

      const tierBlocks = tiers.filter(t => byTier[t].length > 0).map(t => {
        const s = TIER_STYLE[t];
        const stockCards = byTier[t].map(r => `
          <div class="bg-white border border-slate-200 hover:border-violet-300 rounded-lg p-3 cursor-pointer transition group"
               onclick="location.hash='#watchlist-edit';setTimeout(()=>openWatchlistEditor('${_esc(r.code)}'),200);">
            <div class="flex items-start justify-between gap-2 mb-1.5">
              <div class="flex-1 min-w-0">
                <div class="font-mono text-xs font-bold text-slate-900 truncate">${_esc(r.code)}</div>
                <div class="text-sm text-slate-700 truncate">${_esc(r.name)}</div>
              </div>
              ${_roleBadge(r.chain_role)}
            </div>
            <div class="text-xs text-slate-600 leading-snug line-clamp-3">${_esc(r.layman_intro) || '<span class="text-slate-400">(未填一句话解释)</span>'}</div>
          </div>
        `).join("");
        return `<div class="mb-4">
          <div class="flex items-center gap-2 mb-2">
            <span class="px-2 py-0.5 rounded ${s.bg} ${s.txt} text-xs font-semibold">${t}</span>
            <span class="text-xs text-slate-400">${byTier[t].length} 只</span>
          </div>
          <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">${stockCards}</div>
        </div>`;
      }).join("");

      return `<div id="${anchor}" class="bg-slate-50 rounded-xl border border-slate-200 p-5">
        <div class="flex items-center gap-3 mb-4 pb-3 border-b border-slate-200">
          <h3 class="text-xl font-bold text-slate-900">🔗 ${_esc(c)}</h3>
          <span class="text-sm text-slate-500">${rows.length} 个标的</span>
        </div>
        ${tierBlocks || '<div class="text-sm text-slate-400">本链暂无标的</div>'}
      </div>`;
    }).join("");
  } catch (e) {
    container.innerHTML = `<div class="bg-rose-50 border border-rose-200 rounded p-4 text-sm text-rose-700">加载失败：${_esc(e.message)}</div>`;
  }
}
async function forceReloadChainOverview() { _watchlistCache = []; await loadChainOverview(); }

function openWatchlistEditor(code) {
  _watchlistEditCode = code || null;
  const title = code ? `编辑 · ${code}` : "添加新股";
  document.getElementById("watchlist-modal-title").textContent = title;
  const fields = ["code", "name", "market", "industry", "business", "ai-relevance", "ai-logic", "status", "credibility", "conclusion", "risks", "peers", "rhythm", "notes", "chain", "chain-tier", "chain-role", "layman-intro"];
  fields.forEach(f => {
    const el = document.getElementById("wl-" + f);
    if (el) el.value = "";
  });
  if (code) {
    const row = _watchlistCache.find(r => r.code === code) || {};
    document.getElementById("wl-code").value = row.code || "";
    document.getElementById("wl-code").disabled = true;  // 编辑模式 code 不可改
    document.getElementById("wl-name").value = row.name || "";
    document.getElementById("wl-market").value = row.market || "";
    document.getElementById("wl-industry").value = row.industry || "";
    document.getElementById("wl-business").value = row.business || "";
    document.getElementById("wl-ai-relevance").value = row.ai_relevance || "";
    document.getElementById("wl-ai-logic").value = row.ai_logic || "";
    document.getElementById("wl-status").value = row.status || "";
    document.getElementById("wl-credibility").value = row.credibility || "";
    document.getElementById("wl-conclusion").value = row.conclusion || "";
    document.getElementById("wl-risks").value = row.risks || "";
    document.getElementById("wl-earnings").value = row.earnings || "";
    document.getElementById("wl-verification").value = row.verification || "";
    document.getElementById("wl-info-breakdown").value = row.info_breakdown || "";
    document.getElementById("wl-peers").value = row.peers || "";
    document.getElementById("wl-rhythm").value = row.rhythm || "";
    document.getElementById("wl-notes").value = row.notes || "";
    document.getElementById("wl-chain").value = row.chain || "";
    document.getElementById("wl-chain-tier").value = row.chain_tier || "";
    document.getElementById("wl-chain-role").value = row.chain_role || "";
    document.getElementById("wl-layman-intro").value = row.layman_intro || "";
  } else {
    document.getElementById("wl-code").disabled = false;
  }
  document.getElementById("watchlist-modal").classList.remove("hidden");
}

function closeWatchlistEditor() {
  document.getElementById("watchlist-modal").classList.add("hidden");
  _watchlistEditCode = null;
}

async function saveWatchlistItem() {
  const code = document.getElementById("wl-code").value.trim();
  if (!code) {
    alert("代码必填");
    return;
  }
  const item = {
    code,
    name: document.getElementById("wl-name").value.trim() || null,
    market: document.getElementById("wl-market").value.trim() || null,
    industry: document.getElementById("wl-industry").value.trim() || null,
    business: document.getElementById("wl-business").value.trim() || null,
    ai_relevance: document.getElementById("wl-ai-relevance").value.trim() || null,
    ai_logic: document.getElementById("wl-ai-logic").value.trim() || null,
    status: document.getElementById("wl-status").value.trim() || null,
    credibility: document.getElementById("wl-credibility").value.trim() || null,
    conclusion: document.getElementById("wl-conclusion").value.trim() || null,
    risks: document.getElementById("wl-risks").value.trim() || null,
    earnings: document.getElementById("wl-earnings").value.trim() || null,
    verification: document.getElementById("wl-verification").value.trim() || null,
    info_breakdown: document.getElementById("wl-info-breakdown").value.trim() || null,
    peers: document.getElementById("wl-peers").value.trim() || null,
    rhythm: document.getElementById("wl-rhythm").value.trim() || null,
    notes: document.getElementById("wl-notes").value.trim() || null,
    chain: document.getElementById("wl-chain").value.trim() || null,
    chain_tier: document.getElementById("wl-chain-tier").value || null,
    chain_role: document.getElementById("wl-chain-role").value || null,
    layman_intro: document.getElementById("wl-layman-intro").value.trim() || null,
  };
  try {
    if (_watchlistEditCode) {
      await _watchlistApiCall("PUT", "/api/watchlist/" + encodeURIComponent(_watchlistEditCode), item);
    } else {
      await _watchlistApiCall("POST", "/api/watchlist", item);
    }
    closeWatchlistEditor();
    await forceReloadWatchlist();
  } catch (e) {
    alert("保存失败：" + e.message);
  }
}

async function autoEnrichWatchlist() {
  const code = document.getElementById("wl-code").value.trim();
  const statusEl = document.getElementById("wl-enrich-status");
  const btn = document.getElementById("wl-enrich-btn");
  if (!code) {
    statusEl.innerHTML = '<span class="text-rose-600">⚠️ 先填代码</span>';
    return;
  }
  const origText = btn.textContent;
  btn.textContent = "🔄 抓取中...";
  btn.disabled = true;
  statusEl.innerHTML = '<span class="text-slate-500">正在从 yfinance + GICS 拉取...</span>';
  try {
    const data = await _watchlistApiCall("POST", "/api/watchlist/auto-enrich", {code});
    // 把 enrich 结果填进表单 — 只填非空字段，不覆盖用户已经手填的（如有）
    const fillIfEmpty = (id, val) => {
      if (val == null) return;
      const el = document.getElementById(id);
      if (el && !el.value.trim()) el.value = val;
    };
    fillIfEmpty("wl-name", data.name);
    fillIfEmpty("wl-market", data.market);
    fillIfEmpty("wl-industry", data.industry);
    fillIfEmpty("wl-business", data.business);
    fillIfEmpty("wl-ai-relevance", data.ai_relevance);
    fillIfEmpty("wl-ai-logic", data.ai_logic);
    fillIfEmpty("wl-chain", data.chain);
    fillIfEmpty("wl-chain-role", data.chain_role);
    fillIfEmpty("wl-chain-tier", data.chain_tier);
    fillIfEmpty("wl-layman-intro", data.layman_intro);
    fillIfEmpty("wl-credibility", data.credibility);
    const meta = data._enrich_meta || {};
    const sources = (meta.sources || []).join(" · ");
    const warnings = (meta.warnings || []).join("; ");
    let msg = `<span class="text-emerald-700">✓ 已补全</span> <span class="text-slate-500">${_esc(sources)}</span>`;

    // 防拼写错误：GICS 没匹配（default:0）→ 警告用户可能 ticker 错
    if (sources.includes("gics:default:0")) {
      const yfName = data.name || "";
      msg += `<br><span class="text-amber-700 font-medium">⚠️ GICS 未识别此 ticker（AI 关联度 = 0/3）</span>`;
      msg += `<br><span class="text-amber-700 text-xs">yfinance 找到的是 <strong>${_esc(yfName)}</strong> — 如果不是你想要的公司，改正代码后重新点补全</span>`;
    }
    if (warnings) msg += `<br><span class="text-amber-700">⚠️ ${_esc(warnings)}</span>`;
    statusEl.innerHTML = msg;
  } catch (e) {
    statusEl.innerHTML = `<span class="text-rose-600">✗ 补全失败：${_esc(e.message)}</span>`;
  } finally {
    btn.textContent = origText;
    btn.disabled = false;
  }
}

async function deleteWatchlistItem(code) {
  if (!confirm(`确定删除 ${code} 吗？`)) return;
  try {
    await _watchlistApiCall("DELETE", "/api/watchlist/" + encodeURIComponent(code));
    await loadWatchlistTable();
  } catch (e) {
    alert("删除失败：" + e.message);
  }
}

// ============ AI 推荐 → 一键加自选 ============
function _setDiscoveryBtnAdded(btn) {
  btn.textContent = "✓ 已在自选";
  btn.disabled = true;
  btn.classList.remove("bg-violet-600", "hover:bg-violet-700");
  btn.classList.add("bg-slate-300", "text-slate-600", "cursor-not-allowed");
}

async function _markDiscoveryAddedButtons() {
  // 渲染完表格后,异步把已在 自选股里的票按钮标灰
  try {
    if (_watchlistCache.length === 0) {
      _watchlistCache = await _watchlistApiCall("GET", "/api/watchlist");
    }
  } catch (e) {
    return;  // API 没起来就静默,按钮维持可点状态(点击时会再报错)
  }
  const existingCodes = new Set(_watchlistCache.map(r => (r.code || "").toUpperCase()));
  document.querySelectorAll(".discovery-add-btn").forEach(btn => {
    const ticker = (btn.dataset.ticker || "").toUpperCase();
    if (existingCodes.has(ticker)) _setDiscoveryBtnAdded(btn);
  });
}

async function addDiscoveryToWatchlist(ticker, btnEl) {
  const origText = btnEl.textContent;
  btnEl.disabled = true;
  btnEl.textContent = "⏳ 加入中…";
  try {
    // 1. 确保 cache 已加载,避免重复添加
    if (_watchlistCache.length === 0) {
      try { _watchlistCache = await _watchlistApiCall("GET", "/api/watchlist"); } catch (e) {}
    }
    const tickerUpper = (ticker || "").toUpperCase();
    if (_watchlistCache.find(r => (r.code || "").toUpperCase() === tickerUpper)) {
      _setDiscoveryBtnAdded(btnEl);
      return;
    }
    // 2. auto-enrich 拿完整字段(失败也允许只用 code 入库)
    let enriched = {};
    try {
      enriched = await _watchlistApiCall("POST", "/api/watchlist/auto-enrich", {code: ticker});
    } catch (e) {
      console.warn("auto-enrich failed, will save with code only:", e.message);
    }
    // 3. POST 入库 — source = ai_discovery 留追溯
    const item = {
      code: ticker,
      name: enriched.name || null,
      market: enriched.market || null,
      industry: enriched.industry || null,
      business: enriched.business || null,
      ai_relevance: enriched.ai_relevance || null,
      ai_logic: enriched.ai_logic || null,
      chain: enriched.chain || null,
      chain_tier: enriched.chain_tier || null,
      chain_role: enriched.chain_role || null,
      layman_intro: enriched.layman_intro || null,
      credibility: enriched.credibility || null,
      source: "ai_discovery",
      status: "待研究",
    };
    await _watchlistApiCall("POST", "/api/watchlist", item);
    _watchlistCache.push(item);
    _setDiscoveryBtnAdded(btnEl);
  } catch (e) {
    alert("加自选失败: " + e.message);
    btnEl.textContent = origText;
    btnEl.disabled = false;
  }
}


// ============ Tab 切换框架 ============
const TAB_SECTIONS = {
  about: ["about"],
  overview: ["hero", "stress-test", "thesis", "evolution", "scarce", "events", "hundred-x"],
  portfolio: ["portfolio"],
  picks: ["scoring-rules", "picks-review"],
  discovery: ["discovery"],
  audit: ["audit-panel"],
  valuation: ["valuation"],
  // chain = 「产业链地图」· 取代原 themes + chain-overview（主题分组已退役）
  chain: ["chain-header", "chain-overview"],
  backtest: ["backtest"],
  professional: ["professional"],
  upgrade: ["upgrade"],
  "init-config": ["init-config", "portfolio-config", "watchlist-edit"],
};

// 二级 tab：在 #init-config 父 tab 里切换 投资方案 / 关注股票
let _initSubCurrent = "portfolio";
function switchInitSub(sub) {
  _initSubCurrent = sub;
  const pcfg = document.getElementById("portfolio-config");
  const wled = document.getElementById("watchlist-edit");
  if (pcfg) pcfg.style.display = (sub === "portfolio") ? "" : "none";
  if (wled) wled.style.display = (sub === "watchlist") ? "" : "none";
  // 按钮 active 样式
  document.querySelectorAll(".init-sub-btn").forEach(b => {
    const active = b.id === "init-sub-btn-" + sub;
    b.classList.toggle("border-violet-500", active);
    b.classList.toggle("text-violet-700", active);
    b.classList.toggle("border-transparent", !active);
    b.classList.toggle("text-slate-500", !active);
    b.classList.toggle("hover:text-violet-600", !active);
  });
  // 触发子页 lazy load
  if (sub === "portfolio") setTimeout(loadPortfolioConfig, 50);
  if (sub === "watchlist") setTimeout(loadWatchlistTable, 50);
}

function switchTab(tab) {
  // 收集所有需要管理的 section id
  const allSections = new Set();
  Object.values(TAB_SECTIONS).forEach(arr => arr.forEach(id => allSections.add(id)));
  // 显示当前 tab 的 sections，隐藏其他
  const visible = new Set(TAB_SECTIONS[tab] || TAB_SECTIONS.overview);
  allSections.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = visible.has(id) ? "" : "none";
  });
  // 更新 nav active state
  document.querySelectorAll(".tab-link").forEach(a => {
    if (a.dataset.tab === tab) {
      a.classList.add("text-violet-600", "border-violet-500");
      a.classList.remove("border-transparent");
    } else {
      a.classList.remove("text-violet-600", "border-violet-500");
      a.classList.add("border-transparent");
    }
  });
  // 滚到顶部
  window.scrollTo(0, 0);
  // tab 特定的延迟初始化
  if (tab === "portfolio") setTimeout(async () => { await _ensureHoldingsLoaded(); renderPortfolio(); }, 50);
  if (tab === "backtest") setTimeout(renderPlanBacktest, 100);
  if (tab === "professional") setTimeout(renderProfessional, 50);
  if (tab === "chain") setTimeout(loadChainOverview, 50);
  if (tab === "init-config") setTimeout(() => switchInitSub(_initSubCurrent), 50);
}

function getTabFromHash() {
  const h = location.hash.replace("#", "");
  // 老 hash 兼容：portfolio-config / watchlist-edit → init-config（并切到对应子页）
  if (h === "portfolio-config") { _initSubCurrent = "portfolio"; return "init-config"; }
  if (h === "watchlist-edit")   { _initSubCurrent = "watchlist"; return "init-config"; }
  // 老 hash 兼容：themes / chain-overview / landscape → chain（2026-05-11 合并 + 改名后）
  if (h === "themes" || h === "chain-overview" || h === "landscape") return "chain";
  // 默认首屏改为 portfolio（"📋 我的池子" 的核心）— 用户每天打开问"我现在赚还是亏"
  return TAB_SECTIONS[h] ? h : "portfolio";
}
window.addEventListener("hashchange", () => switchTab(getTabFromHash()));
window.addEventListener("DOMContentLoaded", () => switchTab(getTabFromHash()));

// ============ 持仓管理（DuckDB · /api/holdings · 2026-05-12 从 localStorage 迁过来） ============
const STORAGE_KEY = "ai_portfolio_holdings_v1";  // 仅用于首次启动检测+一次性导入到 DuckDB,导入完清空
// 投资方案配置 · 默认值,会被 /api/config 覆盖
let TOTAL_CAPITAL = 500000;
let STOPLOSS_LINE = 300000;
let WARNING_LINE = 400000;   // 中间警戒线（衍生）
let TARGET_LINE = 550000;    // 盈利目标线（衍生）

function _recomputePortfolioLines() {
  WARNING_LINE = Math.round((TOTAL_CAPITAL + STOPLOSS_LINE) / 2);
  TARGET_LINE = Math.round(TOTAL_CAPITAL * 1.1);
}

// 启动时拉一次配置；API 不可用时静默用默认值
(async function _initPortfolioConfig() {
  try {
    const r = await fetch(WATCHLIST_API_BASE + "/api/config");
    if (!r.ok) return;
    const cfg = await r.json();
    if (typeof cfg.total_capital === "number") TOTAL_CAPITAL = cfg.total_capital;
    if (typeof cfg.stoploss_line === "number") STOPLOSS_LINE = cfg.stoploss_line;
    _recomputePortfolioLines();
    // 如果持仓页正在显示,重渲染让新值生效
    if (typeof renderPortfolio === "function") {
      const portfolioEl = document.getElementById("portfolio");
      if (portfolioEl && portfolioEl.style.display !== "none") renderPortfolio();
    }
    // 系统介绍页里的金额展示也刷新
    _refreshAboutPageNumbers();
  } catch (e) {
    // API 离线 · 静默回退默认值
  }
})();

// 把 #about 页面里的写死数字替换成当前 TOTAL_CAPITAL/STOPLOSS_LINE
function _refreshAboutPageNumbers() {
  document.querySelectorAll("[data-cfg='total_capital']").forEach(el => el.textContent = "¥" + TOTAL_CAPITAL.toLocaleString());
  document.querySelectorAll("[data-cfg='stoploss_line']").forEach(el => el.textContent = "¥" + STOPLOSS_LINE.toLocaleString());
  // 衍生：按 TOTAL_CAPITAL × 百分比计算（现金底线/单只上限等）
  document.querySelectorAll("[data-cfg-pct]").forEach(el => {
    const pct = parseFloat(el.dataset.cfgPct);
    if (!isNaN(pct)) el.textContent = "¥" + Math.round(TOTAL_CAPITAL * pct).toLocaleString();
  });
  // 最大允许亏损（TOTAL_CAPITAL - STOPLOSS_LINE）
  document.querySelectorAll("[data-cfg='max_loss']").forEach(el => {
    el.textContent = "¥" + (TOTAL_CAPITAL - STOPLOSS_LINE).toLocaleString();
  });
}

// ====== 投资方案配置 Tab · load / save ======
async function loadPortfolioConfig() {
  const banner = document.getElementById("pcfg-banner");
  const tcInput = document.getElementById("pcfg-total-capital");
  const slInput = document.getElementById("pcfg-stoploss-line");
  const maxLossEl = document.getElementById("pcfg-max-loss");
  const updatedEl = document.getElementById("pcfg-updated-at");
  if (!tcInput || !slInput) return;
  try {
    const r = await fetch(WATCHLIST_API_BASE + "/api/config");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const cfg = await r.json();
    tcInput.value = cfg.total_capital ?? 500000;
    slInput.value = cfg.stoploss_line ?? 300000;
    _updateMaxLossDisplay();
    if (updatedEl) updatedEl.textContent = new Date().toLocaleString("zh-CN");
    if (banner) banner.classList.add("hidden");
  } catch (e) {
    if (banner) {
      banner.className = "mb-4 px-4 py-3 rounded-lg text-sm bg-amber-50 border border-amber-200 text-amber-800";
      banner.innerHTML = `⚠️ 无法连接 API（${e.message}）· 请先启动本地服务：<code class="bg-amber-100 px-1 rounded text-xs">uvicorn stock_research.api.main:app --port 8765</code>`;
      banner.classList.remove("hidden");
    }
  }
}

function _updateMaxLossDisplay() {
  const tc = parseInt(document.getElementById("pcfg-total-capital")?.value || "0", 10);
  const sl = parseInt(document.getElementById("pcfg-stoploss-line")?.value || "0", 10);
  const el = document.getElementById("pcfg-max-loss");
  if (!el) return;
  if (tc > 0 && sl > 0 && sl < tc) {
    const loss = tc - sl;
    const pct = (loss / tc * 100).toFixed(1);
    el.textContent = `-¥${loss.toLocaleString()} (-${pct}%)`;
  } else {
    el.textContent = "—";
  }
}

// 输入变化时实时更新 "最大允许亏损"
document.addEventListener("input", e => {
  if (e.target && (e.target.id === "pcfg-total-capital" || e.target.id === "pcfg-stoploss-line")) {
    _updateMaxLossDisplay();
  }
});

async function savePortfolioConfig() {
  const banner = document.getElementById("pcfg-banner");
  const btn = document.getElementById("pcfg-save");
  const tc = parseInt(document.getElementById("pcfg-total-capital").value, 10);
  const sl = parseInt(document.getElementById("pcfg-stoploss-line").value, 10);
  // 前端校验
  if (!Number.isFinite(tc) || tc <= 0) { _pcfgErr("进场本金必须是正数"); return; }
  if (!Number.isFinite(sl) || sl <= 0) { _pcfgErr("止损红线必须是正数"); return; }
  if (sl >= tc) { _pcfgErr("止损红线必须小于进场本金"); return; }
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(WATCHLIST_API_BASE + "/api/config", {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({total_capital: tc, stoploss_line: sl}),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || ("HTTP " + r.status));
    }
    // 同步到全局变量并刷新所有显示
    TOTAL_CAPITAL = tc;
    STOPLOSS_LINE = sl;
    _recomputePortfolioLines();
    _refreshAboutPageNumbers();
    if (typeof renderPortfolio === "function") {
      const portfolioEl = document.getElementById("portfolio");
      if (portfolioEl && portfolioEl.style.display !== "none") renderPortfolio();
    }
    if (banner) {
      banner.className = "mb-4 px-4 py-3 rounded-lg text-sm bg-emerald-50 border border-emerald-200 text-emerald-800";
      banner.textContent = `✅ 已保存 · 持仓页和系统介绍页已用新值刷新。飞书早报需等下一次跑批生效。`;
      banner.classList.remove("hidden");
    }
    document.getElementById("pcfg-updated-at").textContent = new Date().toLocaleString("zh-CN");
  } catch (e) {
    _pcfgErr(e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function _pcfgErr(msg) {
  const banner = document.getElementById("pcfg-banner");
  if (!banner) return;
  banner.className = "mb-4 px-4 py-3 rounded-lg text-sm bg-rose-50 border border-rose-200 text-rose-800";
  banner.textContent = "❌ " + msg;
  banner.classList.remove("hidden");
}

// 主题映射（从 RECORDS 提取）
const THEME_OF = {};
RECORDS.forEach(r => {
  const ind = (r.industry || "").toLowerCase();
  const ai = r.ai_relevance || "";
  if (ind.includes("光通信") || ind.includes("光模块") || ind.includes("asic") || ind.includes("dsp") || ind.includes("互连")) THEME_OF[r.code] = "💡 AI 连接";
  else if (ind.includes("电力") || ind.includes("液冷") || ind.includes("冷却")) THEME_OF[r.code] = "⚡ AI 电力链";
  else if (ind.includes("稀土") || ind.includes("水处理") || ind.includes("铀") || ind.includes("smr") || ind.includes("微反应堆")) THEME_OF[r.code] = "💎 稀缺资源";
  else if (ind.includes("数据中心")) THEME_OF[r.code] = "🏢 数据中心";
  else if (ind.includes("医疗") || ind.includes("药物")) THEME_OF[r.code] = "🧬 AI 医疗";
  else if (ind.includes("机器人")) THEME_OF[r.code] = "🦾 物理 AI";
  else if (ai.includes("极强")) THEME_OF[r.code] = "🔥 AI 算力核心";
  else if (ai.includes("强")) THEME_OF[r.code] = "🤖 AI 应用层";
  else THEME_OF[r.code] = "📱 其他";
});

// 解析价格字符串「215.2 USD」→ {price, currency}
function parsePrice(s) {
  if (!s) return null;
  const m = s.match(/([\d,]+\.?\d*)\s*([A-Z]{3})?/);
  if (!m) return null;
  return { price: parseFloat(m[1].replace(/,/g, "")), currency: m[2] || "USD" };
}

// 简化汇率（用于跨币种统一为 RMB）—— 真实使用应该实时拉
const FX_TO_RMB = { USD: 7.1, HKD: 0.91, KRW: 0.0052, JPY: 0.046, AUD: 4.6, CNY: 1, GBP: 9.0 };

function getCurrentPriceRMB(code) {
  const r = RECORDS.find(x => x.code === code);
  if (!r || !r.latest_price) return null;
  const p = parsePrice(r.latest_price);
  if (!p) return null;
  const fx = FX_TO_RMB[p.currency] || 1;
  return { rmb_price: p.price * fx, raw_price: p.price, currency: p.currency, fx };
}

let _holdingsCache = [];          // 同步快照,供 renderPortfolio 用
let _holdingsLoaded = false;      // 是否已从 API 拉过一次(含 legacy 迁移)

async function _ensureHoldingsLoaded() {
  if (_holdingsLoaded) return;
  // 1. 一次性迁移:首次启动如果 localStorage 还有老数据,导入 DuckDB 再清空
  try {
    const legacy = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    if (Array.isArray(legacy) && legacy.length > 0) {
      const items = legacy.map(h => ({
        code: h.code,
        entry_price: h.entry_price,
        shares: h.shares,
        date: h.date,
        source: h._plan_a_v6 ? "ai_plan" : "manual",
      }));
      const r = await fetch(WATCHLIST_API_BASE + "/api/holdings/bulk-replace", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(items),
      });
      if (r.ok) {
        localStorage.removeItem(STORAGE_KEY);
        console.log(`✓ 已迁移 ${legacy.length} 条持仓 localStorage → DuckDB`);
      } else {
        console.warn("legacy holdings migration failed:", r.status);
      }
    }
  } catch (e) {
    console.warn("legacy holdings migration error:", e);
  }
  // 2. 拉最新
  try {
    const r = await fetch(WATCHLIST_API_BASE + "/api/holdings");
    _holdingsCache = r.ok ? await r.json() : [];
  } catch (e) {
    _holdingsCache = [];
  }
  _holdingsLoaded = true;
}

// 同步返回 cache(供同步代码用)
function loadHoldings() { return _holdingsCache; }

// 重新拉最新数据并 render
async function refreshHoldingsAndRender() {
  try {
    const r = await fetch(WATCHLIST_API_BASE + "/api/holdings");
    if (r.ok) _holdingsCache = await r.json();
  } catch (e) {}
  _holdingsLoaded = true;
  renderPortfolio();
}

// 整批替换(loadPlanAv6 / importHoldings / clearHoldings)
async function saveHoldings(holdings) {
  const items = holdings.map(h => ({
    code: h.code,
    entry_price: h.entry_price,
    shares: h.shares,
    date: h.date,
    source: h._plan_a_v6 ? "ai_plan" : (h.source || "manual"),
  }));
  try {
    const r = await fetch(WATCHLIST_API_BASE + "/api/holdings/bulk-replace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(items),
    });
    if (!r.ok) { alert("保存持仓失败: HTTP " + r.status); return; }
  } catch (e) { alert("保存持仓失败: " + e.message); return; }
  await refreshHoldingsAndRender();
}

let editingId = null;  // null = 新增, 数字 = 编辑该 id

function addHolding() { editingId = null; openModal(); }

function editHolding(id) {
  editingId = id;
  const h = _holdingsCache.find(x => x.id === id);
  if (!h) return;
  document.getElementById("form-code").value = h.code;
  document.getElementById("form-price").value = h.entry_price;
  document.getElementById("form-shares").value = h.shares;
  document.getElementById("form-date").value = h.entry_date || "";
  openModal();
}

async function deleteHolding(id) {
  if (!confirm("确定删除？")) return;
  try {
    const r = await fetch(WATCHLIST_API_BASE + "/api/holdings/" + id, { method: "DELETE" });
    if (!r.ok) { alert("删除失败: HTTP " + r.status); return; }
  } catch (e) { alert("删除失败: " + e.message); return; }
  await refreshHoldingsAndRender();
}

function openModal() {
  const sel = document.getElementById("form-code");
  sel.innerHTML = RECORDS.map(r => `<option value="${r.code}">${r.name} (${r.code})</option>`).join("");
  if (editingId == null) {
    document.getElementById("form-price").value = "";
    document.getElementById("form-shares").value = "";
    document.getElementById("form-date").value = new Date().toISOString().split("T")[0];
  }
  document.getElementById("holding-modal").classList.remove("hidden");
}
function closeModal() { document.getElementById("holding-modal").classList.add("hidden"); }

async function saveHolding() {
  const code = document.getElementById("form-code").value;
  const entry_price = parseFloat(document.getElementById("form-price").value);
  const shares = parseFloat(document.getElementById("form-shares").value);
  const date = document.getElementById("form-date").value;
  if (!code || !entry_price || !shares) { alert("请填完整"); return; }
  const item = { code, entry_price, shares, date };
  try {
    let r;
    if (editingId != null) {
      r = await fetch(WATCHLIST_API_BASE + "/api/holdings/" + editingId, {
        method: "PUT", headers: {"Content-Type":"application/json"}, body: JSON.stringify(item),
      });
    } else {
      r = await fetch(WATCHLIST_API_BASE + "/api/holdings", {
        method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(item),
      });
    }
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      alert("保存失败: " + (err.detail || r.status));
      return;
    }
  } catch (e) { alert("保存失败: " + e.message); return; }
  closeModal();
  await refreshHoldingsAndRender();
}

function renderPortfolio() {
  const holdings = loadHoldings();
  const tbody = document.getElementById("holdings-table");
  if (!tbody) return;

  if (holdings.length === 0) {
    tbody.innerHTML = '<tr><td colspan="11" class="text-center text-slate-500 py-8">暂无持仓 · 点击「+ 添加持仓」开始记录</td></tr>';
    document.getElementById("portfolio-summary").innerHTML = "";
    document.getElementById("alert-line").innerHTML = '<div class="text-sm text-slate-500">添加持仓后会显示警戒线</div>';
    return;
  }

  let totalCost = 0, totalValue = 0;
  const themeAlloc = {};
  const stockAlloc = [];

  const rows = holdings.map((h, idx) => {
    const r = RECORDS.find(x => x.code === h.code);
    const name = r ? r.name : h.code;
    const industry = r ? (r.industry || "-") : "-";
    const cur = getCurrentPriceRMB(h.code);
    if (!cur) {
      return `<tr class="border-t border-slate-100">
        <td class="px-3 py-2">${stockPill(h.code, {nameOverride: name})}</td>
        <td class="px-3 py-2 text-xs text-slate-600">${industry}</td>
        <td class="px-3 py-2 text-right">${h.entry_price}</td>
        <td class="px-3 py-2 text-right">${h.shares}</td>
        <td class="px-3 py-2 text-right">-</td>
        <td class="px-3 py-2 text-right text-slate-400" colspan="5">无价格数据</td>
        <td class="px-3 py-2 text-center">
          <button onclick="editHolding(${h.id})" class="text-violet-600 text-xs">编辑</button>
          <button onclick="deleteHolding(${h.id})" class="text-rose-500 text-xs ml-2">删除</button>
        </td>
      </tr>`;
    }

    // 找入选时币种（基于 raw_price 推断）
    const cost_local = h.entry_price * h.shares;
    const cost_rmb = cost_local * cur.fx;
    const value_local = cur.raw_price * h.shares;
    const value_rmb = value_local * cur.fx;
    const pnl_rmb = value_rmb - cost_rmb;
    const pnl_pct = (cur.raw_price / h.entry_price - 1) * 100;

    totalCost += cost_rmb;
    totalValue += value_rmb;

    const theme = THEME_OF[h.code] || "📱 其他";
    themeAlloc[theme] = (themeAlloc[theme] || 0) + value_rmb;
    stockAlloc.push({ name, value: value_rmb });

    const pnlColor = pnl_rmb >= 0 ? "text-emerald-600" : "text-rose-600";
    return `<tr class="border-t border-slate-100 hover:bg-slate-50">
      <td class="px-3 py-2 font-medium">${stockPill(h.code, {nameOverride: name})}</td>
      <td class="px-3 py-2 text-xs text-slate-700 max-w-[140px]">${industry}</td>
      <td class="px-3 py-2 text-right font-mono">${h.entry_price.toFixed(2)} ${cur.currency}</td>
      <td class="px-3 py-2 text-right font-mono">${h.shares}</td>
      <td class="px-3 py-2 text-right font-mono">${cost_rmb.toFixed(0)}</td>
      <td class="px-3 py-2 text-right font-mono">${cur.raw_price.toFixed(2)}</td>
      <td class="px-3 py-2 text-right font-mono">${value_rmb.toFixed(0)}</td>
      <td class="px-3 py-2 text-right font-mono ${pnlColor}">${pnl_rmb >= 0 ? '+' : ''}${pnl_rmb.toFixed(0)}</td>
      <td class="px-3 py-2 text-right font-mono ${pnlColor}">${pnl_pct >= 0 ? '+' : ''}${pnl_pct.toFixed(2)}%</td>
      <td class="px-3 py-2 text-right font-mono">${(value_rmb / TOTAL_CAPITAL * 100).toFixed(1)}%</td>
      <td class="px-3 py-2 text-center">
        <button onclick="editHolding(${h.id})" class="text-violet-600 text-xs">编辑</button>
        <button onclick="deleteHolding(${h.id})" class="text-rose-500 text-xs ml-2">删除</button>
      </td>
    </tr>`;
  }).join("");

  tbody.innerHTML = rows;

  // 总览数字
  const total_pnl = totalValue - totalCost;
  const total_pnl_pct = totalCost > 0 ? (total_pnl / totalCost * 100) : 0;
  const cash = TOTAL_CAPITAL - totalCost;
  const portfolio_value = totalValue + cash;
  const stockColor = total_pnl >= 0 ? "text-emerald-600" : "text-rose-600";

  document.getElementById("portfolio-summary").innerHTML = `
    <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200" title="本金 + 股票当前市值变化">
      <div class="text-2xl font-bold text-slate-900">${portfolio_value.toLocaleString(undefined, {maximumFractionDigits:0})}</div>
      <div class="text-xs text-slate-500 mt-1">组合总值 RMB（含现金）</div>
    </div>
    <div class="bg-blue-50 rounded-lg p-3 shadow-sm border border-blue-200" title="所有持仓股票按入选时价格 × 股数 求和（你买入花了多少钱）">
      <div class="text-2xl font-bold text-blue-900">${totalCost.toLocaleString(undefined, {maximumFractionDigits:0})}</div>
      <div class="text-xs text-blue-700 mt-1">已投入成本 RMB <span class="text-blue-500">(买股花的钱)</span></div>
    </div>
    <div class="bg-violet-50 rounded-lg p-3 shadow-sm border border-violet-200" title="所有持仓股票按现价 × 股数 求和（现在能值多少钱）">
      <div class="text-2xl font-bold text-violet-900">${totalValue.toLocaleString(undefined, {maximumFractionDigits:0})}</div>
      <div class="text-xs text-violet-700 mt-1">当前股票市值 RMB <span class="text-violet-500">(现价 × 股数)</span></div>
    </div>
    <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200">
      <div class="text-2xl font-bold ${stockColor}">${total_pnl >= 0 ? '+' : ''}${total_pnl.toLocaleString(undefined, {maximumFractionDigits:0})}</div>
      <div class="text-xs text-slate-500 mt-1">股票仓盈亏 <span class="text-slate-400">(市值 - 成本)</span></div>
    </div>
    <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200">
      <div class="text-2xl font-bold ${stockColor}">${total_pnl_pct >= 0 ? '+' : ''}${total_pnl_pct.toFixed(2)}%</div>
      <div class="text-xs text-slate-500 mt-1">股票仓收益率</div>
    </div>
    <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200" title="50 万本金 - 已投入成本 = 还没买的钱">
      <div class="text-2xl font-bold text-slate-900">${cash.toLocaleString(undefined, {maximumFractionDigits:0})}</div>
      <div class="text-xs text-slate-500 mt-1">未持仓现金 RMB <span class="text-slate-400">(本金 - 成本)</span></div>
    </div>
    <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200">
      <div class="text-2xl font-bold text-slate-900">${holdings.length}</div>
      <div class="text-xs text-slate-500 mt-1">持仓数量</div>
    </div>
  `;

  // 警戒线
  const distance_to_stop = portfolio_value - STOPLOSS_LINE;
  const distance_to_warn = portfolio_value - WARNING_LINE;
  const distance_to_target = TARGET_LINE - portfolio_value;
  const stopColor = portfolio_value > WARNING_LINE ? "bg-emerald-500" : (portfolio_value > STOPLOSS_LINE ? "bg-amber-500" : "bg-rose-500");

  document.getElementById("alert-line").innerHTML = `
    <div class="flex items-center justify-between text-xs">
      <span>30 万止损线</span><span>50 万本金</span><span>55 万止盈参考</span>
    </div>
    <div class="relative h-8 bg-slate-100 rounded overflow-hidden">
      <div class="absolute inset-y-0 left-0 ${stopColor} transition-all" style="width: ${Math.min(100, Math.max(0, (portfolio_value - STOPLOSS_LINE) / (TARGET_LINE - STOPLOSS_LINE) * 100)).toFixed(1)}%"></div>
      <div class="absolute top-0 bottom-0 left-[40%] w-px bg-amber-600"></div>
      <div class="absolute inset-0 flex items-center justify-center text-sm font-bold text-slate-800">${portfolio_value.toLocaleString(undefined, {maximumFractionDigits:0})} RMB</div>
    </div>
    <div class="grid grid-cols-3 gap-2 text-xs mt-2">
      <div>距止损线：<strong class="${distance_to_stop > 0 ? 'text-emerald-600' : 'text-rose-600'}">${distance_to_stop >= 0 ? '+' : ''}${distance_to_stop.toLocaleString(undefined, {maximumFractionDigits:0})}</strong></div>
      <div>距预警线：<strong class="${distance_to_warn > 0 ? 'text-emerald-600' : 'text-rose-600'}">${distance_to_warn >= 0 ? '+' : ''}${distance_to_warn.toLocaleString(undefined, {maximumFractionDigits:0})}</strong></div>
      <div>距止盈线：<strong>${distance_to_target.toLocaleString(undefined, {maximumFractionDigits:0})}</strong></div>
    </div>
  `;

  // 仓位分布饼图
  echarts.init(document.getElementById("chart-allocation")).setOption({
    tooltip: { trigger: "item", formatter: "{b}<br/>{c} RMB ({d}%)" },
    legend: { type: "scroll", orient: "vertical", right: 0, top: "center", textStyle: { fontSize: 11 } },
    series: [{
      name: "持仓", type: "pie", radius: ["40%","65%"], center:["35%","50%"],
      data: stockAlloc.sort((a,b) => b.value - a.value),
      label: { show: false },
    }]
  });

  echarts.init(document.getElementById("chart-theme")).setOption({
    tooltip: { trigger: "item", formatter: "{b}<br/>{c} RMB ({d}%)" },
    legend: { type: "scroll", orient: "vertical", right: 0, top: "center", textStyle: { fontSize: 11 } },
    series: [{
      name: "主题", type: "pie", radius: ["40%","65%"], center:["35%","50%"],
      data: Object.entries(themeAlloc).map(([k,v])=>({ name: k, value: v })),
      label: { show: false },
    }]
  });
}

function exportHoldings() {
  const data = loadHoldings();
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `portfolio_${new Date().toISOString().split("T")[0]}.json`;
  a.click();
}
function importHoldings() {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".json";
  input.onchange = (e) => {
    const f = e.target.files[0];
    const reader = new FileReader();
    reader.onload = ev => {
      try {
        const data = JSON.parse(ev.target.result);
        if (Array.isArray(data)) saveHoldings(data);
        else alert("格式错误");
      } catch { alert("JSON 解析失败"); }
    };
    reader.readAsText(f);
  };
  input.click();
}
async function clearHoldings() {
  if (!confirm("确定清空所有持仓？")) return;
  await saveHoldings([]);
}

// ============ 一键加载方案 A v6（学术因子 + Markowitz 客观仓位） ============
async function loadPlanAv6() {
  if (!PLAN_A_V6 || !PLAN_A_V6.plan_v5 || PLAN_A_V6.plan_v5.length === 0) {
    alert("还没有方案 A v6 数据，请先跑：python3 build_plan_a_v5.py");
    return;
  }
  if (!confirm(
    "⚠️ 这个按钮会把方案 v6 推荐的 12 只股票批量抄进「我的持仓」，把今天的现价当作买入价。\n\n" +
    "✅ 适用场景：你已经按方案在券商下单了，想快速录入再手动改成真实成交价\n" +
    "❌ 不要用：你还没真买，只想看方案表现 → 请直接看顶部「📊 AI 组合方案」tab\n\n" +
    "继续吗？"
  )) return;
  if (loadHoldings().length > 0) {
    if (!confirm("当前已有持仓数据，加载方案 A v6 会覆盖。继续？")) return;
  }
  const today = new Date().toISOString().split("T")[0];
  const USD_TO_RMB = 7.10;
  const holdings = [];
  let totalAmount = 0;
  PLAN_A_V6.plan_v5.forEach(p => {
    const amountRmb = p.amount_rmb || 0;
    if (amountRmb < 100) return;
    const rec = RECORDS.find(r => r.code === p.ticker);
    let priceUsd = null;
    if (rec && rec.latest_price) {
      const m = String(rec.latest_price).match(/([\d,]+\.?\d*)/);
      if (m) priceUsd = parseFloat(m[1].replace(/,/g, ""));
    }
    if (!priceUsd) return;
    const shares = Math.max(1, Math.round(amountRmb / (priceUsd * USD_TO_RMB)));
    holdings.push({
      code: p.ticker,
      entry_price: priceUsd,
      shares: shares,
      date: today,
      _plan_a_v6: true,
    });
    totalAmount += shares * priceUsd * USD_TO_RMB;
  });
  await saveHoldings(holdings);
  const metrics = PLAN_A_V6.portfolio_metrics || {};
  // 渲染持久指标卡片
  renderV6Metrics(metrics);
  alert(`✅ 已加载 ${holdings.length} 只（总额 ¥${Math.round(totalAmount).toLocaleString()}）\n\n` +
        `🚨 重要下一步：买入价现在 = 今天现价。请逐行点【编辑】改成你真实成交价和真实数量，否则盈亏算不准。\n\n` +
        `📚 backtest 数据（参考，非未来承诺）:\n` +
        `  · Sharpe ${metrics.annual_sharpe || '?'} | 年化收益 ${metrics.annual_return_pct || '?'}% | 波动 ${metrics.annual_vol_pct || '?'}%`);
}

function renderV6Metrics(metrics) {
  if (!metrics || (!metrics.annual_sharpe && !metrics.annual_return_pct)) {
    document.getElementById("v6-metrics-card").style.display = "none";
    return;
  }
  const sharpe = metrics.annual_sharpe ? Number(metrics.annual_sharpe).toFixed(2) : "?";
  const ret = metrics.annual_return_pct ? Number(metrics.annual_return_pct).toFixed(1) : "?";
  const vol = metrics.annual_vol_pct ? Number(metrics.annual_vol_pct).toFixed(1) : "?";
  document.getElementById("v6-metrics-content").innerHTML = `
    <div class="bg-white rounded-lg p-3 border border-emerald-200">
      <div class="text-3xl font-bold text-emerald-700">${sharpe}</div>
      <div class="text-xs text-slate-600 mt-1">年化夏普比率</div>
      <div class="text-[10px] text-slate-500">巴菲特长期 0.76</div>
    </div>
    <div class="bg-white rounded-lg p-3 border border-emerald-200">
      <div class="text-3xl font-bold text-emerald-700">+${ret}%</div>
      <div class="text-xs text-slate-600 mt-1">年化收益率</div>
      <div class="text-[10px] text-slate-500">每日 rebalance 假设（≠ 实测）</div>
    </div>
    <div class="bg-white rounded-lg p-3 border border-emerald-200">
      <div class="text-3xl font-bold text-amber-700">${vol}%</div>
      <div class="text-xs text-slate-600 mt-1">年化波动率</div>
      <div class="text-[10px] text-slate-500">SPY 长期约 15-18%</div>
    </div>
    <div class="bg-white rounded-lg p-3 border border-emerald-200">
      <div class="text-sm font-bold text-slate-700">5 因子 + Markowitz</div>
      <div class="text-xs text-slate-600 mt-1">Piotroski / 12-1 动量 / 1月反转</div>
      <div class="text-xs text-slate-600">PEAD / 分析师上修</div>
    </div>
  `;
  document.getElementById("v6-metrics-card").style.display = "";
}

// 加载时如果已有方案 A v6 数据 + ai_plan 来源持仓，就显示指标
window.addEventListener("DOMContentLoaded", async () => {
  await _ensureHoldingsLoaded();
  if (PLAN_A_V6 && PLAN_A_V6.portfolio_metrics && _holdingsCache.some(h => h.source === "ai_plan")) {
    renderV6Metrics(PLAN_A_V6.portfolio_metrics);
  }
});

// ============ 5 天蒙特卡洛模拟显示 ============
function renderSimulation() {
  if (!SIMULATION || !SIMULATION.stock_stats || Object.keys(SIMULATION.stock_stats).length === 0) {
    return;
  }
  const sec = document.getElementById("simulation-section");
  if (!sec) return;
  sec.style.display = "";

  // 时间戳
  const ts = SIMULATION.generated_at ? SIMULATION.generated_at.replace("T", " ").split(".")[0] : "";
  document.getElementById("sim-timestamp").textContent = `生成于 ${ts}`;

  // 关键概率
  const probs = SIMULATION.probabilities || {};
  const dist = SIMULATION.value_distribution_d5 || {};
  document.getElementById("sim-probs").innerHTML = `
    <div class="bg-white rounded-lg p-3 shadow-sm border border-emerald-200">
      <div class="text-2xl font-bold text-emerald-600">${probs.profit_after_5d || 0}%</div>
      <div class="text-xs text-slate-500 mt-1">5 天后保持盈利</div>
    </div>
    <div class="bg-white rounded-lg p-3 shadow-sm border border-amber-200">
      <div class="text-2xl font-bold text-amber-600">${probs.warning_40w || 0}%</div>
      <div class="text-xs text-slate-500 mt-1">触及 40 万预警线</div>
    </div>
    <div class="bg-white rounded-lg p-3 shadow-sm border border-rose-200">
      <div class="text-2xl font-bold text-rose-600">${probs.stoploss_30w || 0}%</div>
      <div class="text-xs text-slate-500 mt-1">触及 30 万止损线</div>
    </div>
    <div class="bg-white rounded-lg p-3 shadow-sm border border-blue-200">
      <div class="text-2xl font-bold text-blue-600">${probs.target_55w || 0}%</div>
      <div class="text-xs text-slate-500 mt-1">触及 55 万止盈线</div>
    </div>
  `;

  // 5 天分布折线图
  const dp = SIMULATION.daily_paths || {};
  const days = (dp.p50 || []).map((_, i) => `D${i}`);
  echarts.init(document.getElementById("chart-sim-paths")).setOption({
    tooltip: { trigger: "axis", formatter: p => p.map(x => `${x.seriesName}: ${x.data.toLocaleString()}`).join("<br>") },
    legend: { top: 0, textStyle: { fontSize: 11 } },
    grid: { left: 60, right: 30, top: 30, bottom: 40 },
    xAxis: { type: "category", data: days },
    yAxis: { type: "value", name: "RMB", scale: true, axisLabel: { formatter: v => v.toLocaleString() } },
    series: [
      { name: "牛市 95%", type: "line", smooth: true, data: dp.p95, lineStyle: { color: "#10b981", width: 2 }, areaStyle: { color: "rgba(16,185,129,0.1)" } },
      { name: "中位 50%", type: "line", smooth: true, data: dp.p50, lineStyle: { color: "#8b5cf6", width: 3 } },
      { name: "熊市 5%", type: "line", smooth: true, data: dp.p5, lineStyle: { color: "#ef4444", width: 2 }, areaStyle: { color: "rgba(239,68,68,0.1)" } },
      // 警戒线
      { type: "line", markLine: { silent: true, symbol: "none", lineStyle: { color: "#cbd5e1", type: "dashed" }, data: [
        { yAxis: TOTAL_CAPITAL, label: { formatter: `本金 ${(TOTAL_CAPITAL/10000).toFixed(0)} 万` } },
        { yAxis: WARNING_LINE,  label: { formatter: `预警 ${(WARNING_LINE/10000).toFixed(0)} 万` } },
        { yAxis: STOPLOSS_LINE, label: { formatter: `止损 ${(STOPLOSS_LINE/10000).toFixed(0)} 万` } },
      ]} },
    ],
  });

  // D5 终值分布柱状图
  const final = [
    { label: "5%（最差）", value: dist.p5, color: "#ef4444" },
    { label: "25%", value: dist.p25, color: "#f59e0b" },
    { label: "50%（中位）", value: dist.p50, color: "#8b5cf6" },
    { label: "75%", value: dist.p75, color: "#06b6d4" },
    { label: "95%（最好）", value: dist.p95, color: "#10b981" },
  ];
  echarts.init(document.getElementById("chart-sim-final")).setOption({
    tooltip: { trigger: "axis", formatter: p => `${p[0].name}: ${(p[0].data || 0).toLocaleString()} RMB<br>${(((p[0].data || 0) - TOTAL_CAPITAL) / TOTAL_CAPITAL * 100).toFixed(2)}%` },
    grid: { left: 80, right: 20, top: 20, bottom: 40 },
    xAxis: { type: "category", data: final.map(f => f.label), axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", name: "RMB", scale: true, axisLabel: { formatter: v => v.toLocaleString() } },
    series: [{ type: "bar", data: final.map(f => ({ value: f.value, itemStyle: { color: f.color }})) }],
  });

  // 每只股票 D5 预期
  const stocks = SIMULATION.median_per_stock || [];
  const tableHtml = `
    <table class="w-full text-sm">
      <thead class="bg-slate-50">
        <tr>
          <th class="px-3 py-2 text-left">股票</th>
          <th class="px-3 py-2 text-right">起始价</th>
          <th class="px-3 py-2 text-right">D5 中位</th>
          <th class="px-3 py-2 text-right">涨跌%</th>
          <th class="px-3 py-2 text-right">仓位 RMB 盈亏</th>
        </tr>
      </thead>
      <tbody>
        ${stocks.map(s => {
          const pnlColor = s.pnl_rmb >= 0 ? "text-emerald-600" : "text-rose-600";
          return `<tr class="border-t border-slate-100">
            <td class="px-3 py-2 font-medium">${s.name} <span class="text-xs text-slate-400 font-mono">${s.ticker}</span></td>
            <td class="px-3 py-2 text-right font-mono">${s.entry.toFixed(2)}</td>
            <td class="px-3 py-2 text-right font-mono">${s.d5_median.toFixed(2)}</td>
            <td class="px-3 py-2 text-right font-mono ${pnlColor}">${s.delta_pct >= 0 ? '+' : ''}${s.delta_pct.toFixed(2)}%</td>
            <td class="px-3 py-2 text-right font-mono ${pnlColor}">${s.pnl_rmb >= 0 ? '+' : ''}${s.pnl_rmb.toFixed(0)}</td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>
  `;
  document.getElementById("sim-stock-table").innerHTML = tableHtml;
}

// 持仓 tab 打开时同时渲染模拟
const _origRenderPortfolio = renderPortfolio;
renderPortfolio = function() {
  _origRenderPortfolio();
  setTimeout(renderSimulation, 50);
};

// ============ 📊 专业分析 Tab ============
function switchProfTab(name) {
  ["risk", "13f", "optimize"].forEach(n => {
    const btn = document.getElementById("prof-tab-" + n);
    const pane = document.getElementById("prof-pane-" + n);
    if (n === name) {
      btn.classList.add("border-violet-500", "text-violet-600");
      btn.classList.remove("border-transparent", "text-slate-600");
      pane.style.display = "";
    } else {
      btn.classList.remove("border-violet-500", "text-violet-600");
      btn.classList.add("border-transparent", "text-slate-600");
      pane.style.display = "none";
    }
  });
  if (name === "risk") setTimeout(renderRiskPane, 30);
  if (name === "13f") setTimeout(render13FPane, 30);
  if (name === "optimize") setTimeout(renderOptPane, 30);
}

function renderProfessional() {
  // 默认显示 risk pane
  renderRiskPane();
}

function fmtNum(n, decimals=2) {
  if (n == null || isNaN(n)) return "-";
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: decimals, minimumFractionDigits: decimals });
}

function renderRiskPane() {
  if (!RISK_METRICS || !RISK_METRICS.sharpe) {
    document.getElementById("risk-var").innerHTML = '<div class="text-slate-500 col-span-4">暂无数据，请先跑：python3 risk_metrics.py</div>';
    return;
  }
  const m = RISK_METRICS;

  // VaR/CVaR
  document.getElementById("risk-var").innerHTML = `
    <div class="bg-white rounded-lg p-3 shadow-sm">
      <div class="text-2xl font-bold text-rose-600">${fmtNum(m.var_95_rmb, 0)}</div>
      <div class="text-xs text-slate-600 mt-1">95% VaR（${fmtNum(m.var_95_pct)}%）<br>1 天最大损失阈值</div>
    </div>
    <div class="bg-white rounded-lg p-3 shadow-sm">
      <div class="text-2xl font-bold text-rose-700">${fmtNum(m.var_99_rmb, 0)}</div>
      <div class="text-xs text-slate-600 mt-1">99% VaR（${fmtNum(m.var_99_pct)}%）<br>极端 1% 阈值</div>
    </div>
    <div class="bg-white rounded-lg p-3 shadow-sm">
      <div class="text-2xl font-bold text-rose-600">${fmtNum(m.cvar_95_rmb, 0)}</div>
      <div class="text-xs text-slate-600 mt-1">95% CVaR<br>最差 5% 的平均损失</div>
    </div>
    <div class="bg-white rounded-lg p-3 shadow-sm">
      <div class="text-2xl font-bold text-rose-800">${fmtNum(m.cvar_99_rmb, 0)}</div>
      <div class="text-xs text-slate-600 mt-1">99% CVaR<br>极端日子的平均损失</div>
    </div>
  `;

  // 收益指标
  document.getElementById("risk-return").innerHTML = `
    <div class="space-y-2 text-sm">
      <div class="flex justify-between"><span class="text-slate-600">回测期</span><span class="font-mono text-xs">${m.period_start} → ${m.period_end}</span></div>
      <div class="flex justify-between"><span class="text-slate-600">交易日数</span><span class="font-mono">${m.n_days}</span></div>
      <div class="flex justify-between"><span class="text-slate-600">累计收益</span><span class="font-mono font-bold text-emerald-600">+${fmtNum(m.total_return_pct)}%</span></div>
      <div class="flex justify-between"><span class="text-slate-600">年化收益（CAGR）</span><span class="font-mono font-bold text-emerald-600">+${fmtNum(m.cagr_pct)}%</span></div>
    </div>
    <p class="text-xs text-amber-700 mt-3 p-2 bg-amber-50 rounded">⚠️ 历史 CAGR ≠ 未来。AI 主升浪 + 4 月低点入场是异常值，正常预期 +10-20%</p>
  `;

  // 风险指标
  document.getElementById("risk-vol").innerHTML = `
    <div class="space-y-2 text-sm">
      <div class="flex justify-between"><span class="text-slate-600">年化波动率</span><span class="font-mono">${fmtNum(m.annual_vol_pct)}%</span></div>
      <div class="flex justify-between"><span class="text-slate-600">下行波动率</span><span class="font-mono">${fmtNum(m.downside_vol_pct)}%</span></div>
      <div class="flex justify-between"><span class="text-slate-600">最大回撤</span><span class="font-mono ${m.max_drawdown_pct < -25 ? 'text-rose-600' : 'text-amber-600'}">${fmtNum(m.max_drawdown_pct)}%</span></div>
      <div class="flex justify-between"><span class="text-slate-600">回撤日期</span><span class="font-mono text-xs">${m.max_dd_date}</span></div>
      ${m.beta_vs_spy != null ? `<div class="flex justify-between"><span class="text-slate-600">Beta vs SPY</span><span class="font-mono">${fmtNum(m.beta_vs_spy)}</span></div>` : ''}
    </div>
    <p class="text-xs ${Math.abs(m.max_drawdown_pct) < 40 ? 'text-emerald-700 bg-emerald-50' : 'text-rose-700 bg-rose-50'} mt-3 p-2 rounded">${Math.abs(m.max_drawdown_pct) < 40 ? '🟢 最大回撤在 -40% 红线内' : '🔴 历史最大回撤已突破 -40% 红线'}</p>
  `;

  // 风险调整收益
  function ratio_color(r, good) { return r > good ? "text-emerald-600" : "text-amber-600"; }
  document.getElementById("risk-ratios").innerHTML = `
    <div class="space-y-2 text-sm">
      <div class="flex justify-between items-baseline">
        <span class="text-slate-600">Sharpe 比率</span>
        <span class="font-mono font-bold text-xl ${ratio_color(m.sharpe, 1.5)}">${fmtNum(m.sharpe)}</span>
      </div>
      <div class="flex justify-between items-baseline">
        <span class="text-slate-600">Sortino 比率</span>
        <span class="font-mono font-bold text-xl ${ratio_color(m.sortino, 2)}">${fmtNum(m.sortino)}</span>
      </div>
      <div class="flex justify-between items-baseline">
        <span class="text-slate-600">Calmar 比率</span>
        <span class="font-mono font-bold text-xl ${ratio_color(m.calmar, 3)}">${fmtNum(m.calmar)}</span>
      </div>
    </div>
    <p class="text-xs text-slate-600 mt-3 p-2 bg-slate-50 rounded">
      Sharpe>1.5 优秀 / Sortino>2 优秀 / Calmar>3 优秀。<strong>当前过去 1 年值偏高，下年大概率回归到 1.5-2.5。</strong>
    </p>
  `;

  // 组合每日价值历史曲线
  if (m.daily_values && m.daily_values.length > 0) {
    const data = m.daily_values.map(d => [d.date, d.value]);
    echarts.init(document.getElementById("chart-portfolio-history")).setOption({
      tooltip: { trigger: "axis", formatter: p => `${p[0].axisValue}<br/>${p[0].data[1].toLocaleString()} RMB` },
      grid: { left: 60, right: 30, top: 30, bottom: 40 },
      xAxis: { type: "time" },
      yAxis: { type: "value", scale: true, axisLabel: { formatter: v => v.toLocaleString() } },
      series: [{
        type: "line", smooth: true, data: data, lineStyle: { color: "#8b5cf6", width: 2 },
        areaStyle: { color: "rgba(139,92,246,0.1)" },
        markLine: { silent: true, symbol: "none", lineStyle: { color: "#cbd5e1", type: "dashed" }, data: [
          { yAxis: TOTAL_CAPITAL, label: { formatter: `起点 ${(TOTAL_CAPITAL/10000).toFixed(0)} 万` } },
          { yAxis: STOPLOSS_LINE, label: { formatter: `止损 ${(STOPLOSS_LINE/10000).toFixed(0)} 万` } },
        ]},
      }],
    });
  }
}

function render13FPane() {
  if (!TRACK_13F || !TRACK_13F.tickers) {
    document.getElementById("track-13f-content").innerHTML = '<div class="text-slate-500">暂无数据，请先跑：python3 _build_track_13f_from_sec.py</div>';
    return;
  }
  // 顶部数据源标识
  const meta = `<div class="bg-emerald-50 border border-emerald-200 rounded-lg p-3 mb-4 text-xs">
    <div class="font-semibold text-emerald-900">📊 数据源: ${TRACK_13F.data_source || "SEC EDGAR 13F-HR"}</div>
    <div class="text-emerald-700 mt-1">报告期: <strong>${TRACK_13F.report_quarter || "?"}</strong> · 跟踪 ${(TRACK_13F.investors_tracked || []).length} 家机构 (${(TRACK_13F.investors_tracked || []).slice(0, 4).join(" / ")}${(TRACK_13F.investors_tracked || []).length > 4 ? " 等" : ""})</div>
    <div class="text-emerald-600 mt-1">⚠️ 13F 滞后 45 天披露，反映季度末持仓 ≠ 实时持仓</div>
  </div>`;

  const tickers = TRACK_13F.tickers;
  const cards = Object.entries(tickers).map(([code, t]) => {
    const signals = t.institutional_signals || [];
    const summary = t.summary || {};

    // 净方向徽章
    const dirColor = summary.investors_adding > summary.investors_cutting ? "bg-emerald-100 text-emerald-800" :
                     (summary.investors_cutting > summary.investors_adding ? "bg-rose-100 text-rose-800" : "bg-slate-100 text-slate-700");

    // 信号表
    const rows = signals.map((s, i) => {
      const pct = s.shares_change_pct != null ? (s.shares_change_pct >= 0 ? "+" : "") + s.shares_change_pct.toFixed(1) + "%" : "—";
      const pctColor = s.shares_change_pct > 0 ? "text-emerald-600" : (s.shares_change_pct < 0 ? "text-rose-600" : "text-slate-600");
      const value = s.value_curr_kusd ? "$" + (s.value_curr_kusd / 1e6).toFixed(0) + "M" : "";
      return `<tr class="border-t border-slate-100">
        <td class="px-2 py-1">${i+1}</td>
        <td class="px-2 py-1 truncate" style="max-width:240px">${s.investor || "?"}</td>
        <td class="px-2 py-1">${s.action || ""}</td>
        <td class="px-2 py-1 text-right font-mono">${(s.shares_prev || 0).toLocaleString()}</td>
        <td class="px-2 py-1 text-right font-mono">${(s.shares_curr || 0).toLocaleString()}</td>
        <td class="px-2 py-1 text-right font-mono ${pctColor}">${pct}</td>
        <td class="px-2 py-1 text-right font-mono text-slate-600">${value}</td>
      </tr>`;
    }).join("");

    return `<div class="bg-white rounded-xl border border-slate-200 overflow-hidden">
      <div class="bg-slate-100 px-4 py-2 flex items-center justify-between">
        <div class="font-semibold flex items-center gap-2">
          <span>${t.name}</span><span class="text-xs text-slate-500 font-mono">${code}</span>
        </div>
        <div class="flex items-center gap-2 text-xs">
          <span class="${dirColor} px-2 py-0.5 rounded font-semibold">${summary.net_direction || "?"}</span>
          <span class="text-slate-600">📈 ${summary.investors_adding || 0} 加仓 / 📉 ${summary.investors_cutting || 0} 减仓</span>
        </div>
      </div>
      <div class="p-3 text-xs">
        <table class="w-full">
          <thead class="bg-slate-50 text-slate-600">
            <tr>
              <th class="px-2 py-1 text-left">#</th>
              <th class="px-2 py-1 text-left">机构</th>
              <th class="px-2 py-1 text-left">动作</th>
              <th class="px-2 py-1 text-right">上期</th>
              <th class="px-2 py-1 text-right">本期</th>
              <th class="px-2 py-1 text-right">变动%</th>
              <th class="px-2 py-1 text-right">市值</th>
            </tr>
          </thead>
          <tbody>${rows || '<tr><td colspan="7" class="text-center text-slate-400 py-2">无信号</td></tr>'}</tbody>
        </table>
      </div>
    </div>`;
  }).join("");

  document.getElementById("track-13f-content").innerHTML = meta + cards;
}

function renderOptPane() {
  if (!OPTIMIZATION || !OPTIMIZATION.current_plan) {
    document.getElementById("opt-comparison").innerHTML = '<div class="text-slate-500 col-span-4">暂无数据，请先跑：python3 -m stock_research.jobs.optimize_portfolio</div>';
    return;
  }
  const plan = OPTIMIZATION.current_plan;
  const cur = OPTIMIZATION.method_comparison && OPTIMIZATION.method_comparison.current;

  // 顶部对比卡片（4 种方法）
  const methods = [
    { label: "当前方案 A", color: "violet", key: "current_pct" },
    { label: "Kelly Half", color: "amber", key: "kelly_half_norm_pct" },
    { label: "Risk Parity", color: "blue", key: "risk_parity_pct" },
    { label: "Markowitz", color: "emerald", key: "markowitz_pct" },
  ];

  // 计算每种方法的预期收益和波动（近似）
  // OPTIMIZATION.method_comparison 只有 current，其他需要前端不准。直接显示 current
  document.getElementById("opt-comparison").innerHTML = methods.map(m => {
    return `<div class="bg-white rounded-lg p-3 shadow-sm border border-${m.color}-200">
      <div class="text-sm font-semibold text-${m.color}-700">${m.label}</div>
      <div class="text-xs text-slate-500 mt-1">仓位策略</div>
      ${m.key === 'current_pct' && cur ? `
        <div class="text-xs mt-2 space-y-1">
          <div>年化收益 <span class="font-mono font-bold">+${cur.annual_return}%</span></div>
          <div>年化波动 <span class="font-mono">${cur.annual_vol}%</span></div>
          <div>Sharpe <span class="font-mono font-bold">${cur.sharpe}</span></div>
        </div>
      ` : '<div class="text-xs text-slate-400 mt-2">见下方表格</div>'}
    </div>`;
  }).join("");

  // 表格对比
  const tbl = `<table class="w-full text-sm">
    <thead class="bg-slate-100">
      <tr>
        <th class="px-3 py-2 text-left">股票</th>
        <th class="px-3 py-2 text-right">当前</th>
        <th class="px-3 py-2 text-right">Kelly Half</th>
        <th class="px-3 py-2 text-right">Risk Parity</th>
        <th class="px-3 py-2 text-right">Markowitz</th>
        <th class="px-3 py-2 text-right">Markowitz 差异</th>
      </tr>
    </thead>
    <tbody>
      ${plan.map(s => {
        const diff = s.markowitz_pct - s.current_pct;
        const diffColor = Math.abs(diff) < 0.02 ? "text-slate-500" : (diff > 0 ? "text-emerald-600" : "text-rose-600");
        const sign = diff > 0 ? "+" : "";
        return `<tr class="border-t border-slate-100 hover:bg-slate-50">
          <td class="px-3 py-2 font-medium">${s.name} <span class="text-xs text-slate-400 font-mono">${s.ticker}</span></td>
          <td class="px-3 py-2 text-right font-mono">${(s.current_pct * 100).toFixed(1)}%</td>
          <td class="px-3 py-2 text-right font-mono">${(s.kelly_half_norm_pct * 100).toFixed(1)}%</td>
          <td class="px-3 py-2 text-right font-mono">${(s.risk_parity_pct * 100).toFixed(1)}%</td>
          <td class="px-3 py-2 text-right font-mono">${(s.markowitz_pct * 100).toFixed(1)}%</td>
          <td class="px-3 py-2 text-right font-mono ${diffColor}">${sign}${(diff * 100).toFixed(1)}%</td>
        </tr>`;
      }).join("")}
    </tbody>
  </table>`;
  document.getElementById("opt-table").innerHTML = tbl;

  // 仓位对比柱状图
  echarts.init(document.getElementById("chart-opt")).setOption({
    tooltip: { trigger: "axis" },
    legend: { top: 0 },
    grid: { left: 60, right: 30, top: 40, bottom: 60 },
    xAxis: { type: "category", data: plan.map(s => s.name), axisLabel: { interval: 0, rotate: 30, fontSize: 10 } },
    yAxis: { type: "value", name: "仓位 %", axisLabel: { formatter: v => (v * 100).toFixed(0) + "%" } },
    series: [
      { name: "当前", type: "bar", data: plan.map(s => s.current_pct), itemStyle: { color: "#8b5cf6" } },
      { name: "Kelly Half", type: "bar", data: plan.map(s => s.kelly_half_norm_pct), itemStyle: { color: "#f59e0b" } },
      { name: "Risk Parity", type: "bar", data: plan.map(s => s.risk_parity_pct), itemStyle: { color: "#3b82f6" } },
      { name: "Markowitz", type: "bar", data: plan.map(s => s.markowitz_pct), itemStyle: { color: "#10b981" } },
    ],
  });
}

// ============ AI 组合方案 Tab ============
function renderPlanBacktest() {
  const data = _BACKTEST || {};
  const dynData = _DYNAMIC || {};
  const metricsEl = document.getElementById('backtest-metrics');
  const navEl = document.getElementById('backtest-nav-chart');
  const dailyEl = document.getElementById('backtest-daily-chart');
  const tableEl = document.getElementById('backtest-contrib-table');
  const warnEl = document.getElementById('backtest-missing-warning');
  const covEl = document.getElementById('backtest-coverage');
  const bannerEl = document.getElementById('backtest-inception-banner');
  const rebalanceEl = document.getElementById('backtest-rebalance-log');
  if (!metricsEl) return;

  if (!data.dates || data.dates.length === 0) {
    metricsEl.innerHTML = '<div class="col-span-5 text-slate-500 p-4 bg-white rounded-lg">暂无数据 — 请先跑：<code>python3 build_plan_a_v5.py && python3 _fetch_history_for_dashboard.py</code></div>';
    if (navEl) navEl.innerHTML = '';
    if (dailyEl) dailyEl.innerHTML = '';
    if (tableEl) tableEl.innerHTML = '';
    return;
  }
  const m = data.metrics || {};
  const fmtPct = (v) => (v == null) ? '-' : (Number(v).toFixed(2) + '%');
  const cls = (v) => (v >= 0) ? 'text-emerald-600' : 'text-rose-600';
  const nTracked = m.n_tracked_days || 0;

  // 顶部 inception banner
  if (bannerEl) {
    if (data.inception_date) {
      bannerEl.classList.remove('hidden');
      const daysWord = nTracked === 0
        ? '<span class="text-rose-600 font-semibold">还没有交易日</span>（等下一个开盘日 daily_refresh 跑完后会出现第 1 个数据点）'
        : `已跟踪 <strong class="text-violet-700">${nTracked}</strong> 个交易日（${m.tracked_start} → ${m.tracked_end}）`;
      bannerEl.innerHTML = `📍 v6 plan 锁定日：<strong class="font-mono text-violet-700">${data.inception_date}</strong> · 基线日（最近交易日）：<strong class="font-mono">${data.baseline_date}</strong> · ${daysWord}`;
    } else {
      bannerEl.classList.add('hidden');
    }
  }

  // 数据少时（<2 天 tracked）特殊态：不显示百分比指标，只显示状态
  if (nTracked < 1) {
    metricsEl.innerHTML = `
      <div class="col-span-5 bg-amber-50 border border-amber-300 rounded-lg p-4">
        <div class="text-base font-semibold text-amber-900">⏳ 锁定日刚定，还没有完整交易日数据</div>
        <div class="text-sm text-amber-800 mt-1">基线日 <span class="font-mono">${data.baseline_date}</span> 收盘 = NAV 100。下一个交易日开盘后跑 daily_refresh，曲线就会出现第 1 个 forward 数据点。</div>
        <div class="text-xs text-amber-700 mt-2">下方曲线显示的是锁定日之前 30 天的市场上下文（灰色），不是组合表现。</div>
      </div>
    `;
  } else {
    metricsEl.innerHTML = `
      <div class="bg-white rounded-lg p-3 shadow-sm">
        <div class="text-2xl font-bold ${cls(m.cumulative_return_pct)}">${fmtPct(m.cumulative_return_pct)}</div>
        <div class="text-xs text-slate-600 mt-1">实盘累计收益<br/>vs SPY ${fmtPct(m.bench_cumulative_return_pct)}（α ${fmtPct(m.alpha_pct)}）</div>
      </div>
      <div class="bg-white rounded-lg p-3 shadow-sm">
        <div class="text-2xl font-bold ${cls(m.annual_return_pct)}">${fmtPct(m.annual_return_pct)}</div>
        <div class="text-xs text-slate-600 mt-1">年化（${nTracked} 日推算）<br/>样本太短仅供参考</div>
      </div>
      <div class="bg-white rounded-lg p-3 shadow-sm">
        <div class="text-2xl font-bold text-slate-900">${(m.sharpe || 0).toFixed(2)}</div>
        <div class="text-xs text-slate-600 mt-1">Sharpe Ratio<br/>年化波动 ${fmtPct(m.annual_vol_pct)}</div>
      </div>
      <div class="bg-white rounded-lg p-3 shadow-sm">
        <div class="text-2xl font-bold text-rose-600">${fmtPct(m.max_drawdown_pct)}</div>
        <div class="text-xs text-slate-600 mt-1">最大回撤<br/>跟踪期内最差</div>
      </div>
      <div class="bg-white rounded-lg p-3 shadow-sm">
        <div class="text-2xl font-bold text-slate-900">${(m.win_rate_pct || 0).toFixed(1)}%</div>
        <div class="text-xs text-slate-600 mt-1">胜率<br/>${m.win_days || 0} / ${m.total_days || 0} 天</div>
      </div>
    `;
  }

  if (covEl) {
    covEl.textContent = `锁定日 ${data.baseline_date || '?'} · 跟踪 ${nTracked} 个交易日 · ${(data.tickers_used || []).length} 只成分股`
      + ((data.tickers_missing && data.tickers_missing.length) ? ` · 缺数据 ${data.tickers_missing.length} 只` : '');
  }
  if (warnEl) {
    if (data.tickers_missing && data.tickers_missing.length) {
      warnEl.classList.remove('hidden');
      warnEl.innerHTML = `⚠️ 历史数据缺失：${data.tickers_missing.join(', ')} — 已剔除（权重已重归一化）。`;
    } else {
      warnEl.classList.add('hidden');
    }
  }

  // NAV chart：3 条曲线 — A 静态(紫) / C 动态(橙) / SPY(红)
  if (navEl && typeof echarts !== 'undefined') {
    let navChart = echarts.getInstanceByDom(navEl);
    if (!navChart) navChart = echarts.init(navEl);
    const baselineIdx = data.baseline_idx_in_window != null ? data.baseline_idx_in_window : 0;
    const navPct = (data.nav || []).map(v => v == null ? null : +(v * 100).toFixed(2));
    const benchPct = (data.bench_nav || []).map(v => +(v * 100).toFixed(2));
    // A 静态：拆成 context（含 baseline 那点）+ tracked（baseline 之后），用 null 隔开避免重叠
    const ctxNav = navPct.map((v, i) => i <= baselineIdx ? v : null);
    const trkNav = navPct.map((v, i) => i >= baselineIdx ? v : null);
    const ctxBench = benchPct.map((v, i) => i <= baselineIdx ? v : null);
    const trkBench = benchPct.map((v, i) => i >= baselineIdx ? v : null);
    const baselineDate = data.baseline_date || (data.dates[baselineIdx] || '');

    // C 动态：同样的窗口（dates 一致），独立的 nav
    const dynNav = (dynData.nav || []).map(v => v == null ? null : +(v * 100).toFixed(2));
    const dynTrk = dynNav.length ? dynNav.map((v, i) => i >= baselineIdx ? v : null) : [];

    // 调仓日竖线（多条）
    const rebalanceLines = (dynData.rebalance_dates || []).map(d => ({
      xAxis: d,
      label: { formatter: '↻', fontSize: 11, color: '#f97316', position: 'insideEndTop' },
      lineStyle: { color: '#f97316', width: 1, type: 'dashed', opacity: 0.5 }
    }));

    const series = [
      {
        name: 'A 静态（锁定前·上下文）', type: 'line', smooth: true, showSymbol: false,
        data: ctxNav,
        lineStyle: { width: 1.2, color: '#cbd5e1', type: 'dashed' },
      },
      {
        name: 'A 静态（buy-and-hold 死守）', type: 'line', smooth: true, showSymbol: true, symbolSize: 6,
        data: trkNav,
        lineStyle: { width: 2.5, color: '#7c3aed' },
        markLine: {
          symbol: ['none', 'none'], silent: true,
          data: [
            { xAxis: baselineDate, label: { formatter: '📍 锁定 ' + baselineDate, fontSize: 10, color: '#7c3aed', position: 'insideEndTop' }, lineStyle: { color: '#7c3aed', width: 1.5, type: 'dashed' } },
            ...rebalanceLines,
          ]
        }
      },
    ];
    if (dynTrk.length) {
      series.push({
        name: 'C 动态（每周一调仓·含手续费）', type: 'line', smooth: true, showSymbol: true, symbolSize: 6,
        data: dynTrk,
        lineStyle: { width: 2.5, color: '#f97316' },
        areaStyle: { color: 'rgba(249,115,22,0.06)' },
      });
    }
    if (benchPct.length) {
      series.push({
        name: 'SPY（锁定前·上下文）', type: 'line', smooth: true, showSymbol: false,
        data: ctxBench,
        lineStyle: { width: 1, color: '#fda4af', type: 'dashed' },
      });
      series.push({
        name: 'SPY 基准', type: 'line', smooth: true, showSymbol: true, symbolSize: 5,
        data: trkBench,
        lineStyle: { width: 1.8, color: '#f43f5e' },
      });
    }
    navChart.setOption({
      tooltip: { trigger: 'axis' },
      legend: { top: 0, textStyle: { fontSize: 10 } },
      xAxis: { type: 'category', data: data.dates, boundaryGap: false, axisLabel: { fontSize: 10 } },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: '{value}', fontSize: 10 }, splitLine: { lineStyle: { type: 'dashed', color: '#e2e8f0' } } },
      grid: { left: 50, right: 30, top: 50, bottom: 30 },
      series: series,
    });
    requestAnimationFrame(() => navChart.resize());
    setTimeout(() => navChart.resize(), 200);
    if (!window._BACKTEST_NAV_RESIZE_HOOKED) {
      window._BACKTEST_NAV_RESIZE_HOOKED = true;
      window.addEventListener('resize', () => navChart.resize());
    }
  }

  // C-A spread + 调仓记录
  const dynM = dynData.metrics || {};
  if (rebalanceEl) {
    const nReb = dynM.n_rebalances || 0;
    const commPct = dynData.total_commission_pct || 0;
    const dynCum = dynM.cumulative_return_pct;
    const staCum = m.cumulative_return_pct;
    const spread = (dynCum != null && staCum != null) ? (dynCum - staCum) : null;
    const spreadStr = spread == null ? '—' :
      (spread > 0 ? `+${spread.toFixed(2)}%` : `${spread.toFixed(2)}%`);
    const spreadCls = spread == null ? 'text-slate-500'
      : (spread > 0 ? 'text-emerald-700' : 'text-rose-700');

    // 顶部 KPI 横幅同步 spread（正绿/负红/未知灰）
    const topSpreadEl = document.getElementById('ai-alpha-spread-display');
    if (topSpreadEl) {
      topSpreadEl.textContent = spreadStr;
      topSpreadEl.style.color = spread == null ? '#64748b'
        : (spread > 0 ? '#047857' : '#be123c');
    }

    let summaryHtml = `
      <div class="grid grid-cols-1 md:grid-cols-4 gap-3 mb-3">
        <div class="bg-white rounded-lg p-3 border border-violet-200">
          <div class="text-xs text-slate-500">A 静态（死守不动）累计</div>
          <div class="text-xl font-bold text-violet-700">${staCum == null ? '—' : (staCum >= 0 ? '+' : '') + staCum.toFixed(2) + '%'}</div>
        </div>
        <div class="bg-white rounded-lg p-3 border border-orange-200">
          <div class="text-xs text-slate-500">C 动态（每周一调仓）累计</div>
          <div class="text-xl font-bold text-orange-600">${dynCum == null ? '—' : (dynCum >= 0 ? '+' : '') + dynCum.toFixed(2) + '%'}</div>
        </div>
        <div class="bg-white rounded-lg p-3 border border-emerald-200">
          <div class="text-xs text-slate-500">C − A = AI 持续选股 alpha</div>
          <div class="text-xl font-bold ${spreadCls}">${spreadStr}</div>
        </div>
        <div class="bg-white rounded-lg p-3 border border-slate-200">
          <div class="text-xs text-slate-500">调仓次数 · 累计手续费</div>
          <div class="text-base font-semibold text-slate-800">${nReb} 次 · ${commPct.toFixed(3)}%</div>
        </div>
      </div>`;

    const log = (dynData.rebalance_log || []).slice(-12).reverse();
    if (log.length) {
      const rows = log.map(r => {
        const turnoverPct = r.pre_nav > 0 ? (r.turnover_dollar / r.pre_nav * 100) : 0;
        const costPct = r.pre_nav > 0 ? (r.commission_dollar / r.pre_nav * 100) : 0;
        const adds = (r.tickers_added || []).join(' / ') || '—';
        const rems = (r.tickers_removed || []).join(' / ') || '—';
        return `<tr class="border-b border-slate-100">
          <td class="px-3 py-1.5 font-mono text-xs">${r.date}</td>
          <td class="px-3 py-1.5 text-xs">${r.n_tickers}</td>
          <td class="px-3 py-1.5 text-xs">${turnoverPct.toFixed(1)}%</td>
          <td class="px-3 py-1.5 text-xs text-rose-600">-${costPct.toFixed(3)}%</td>
          <td class="px-3 py-1.5 text-xs text-emerald-700">${adds}</td>
          <td class="px-3 py-1.5 text-xs text-rose-700">${rems}</td>
        </tr>`;
      }).join('');
      summaryHtml += `
        <details class="bg-white rounded-lg border border-slate-200 p-3">
          <summary class="text-sm font-semibold text-slate-700 cursor-pointer">📋 最近 ${log.length} 次调仓明细（点开展开）</summary>
          <table class="w-full text-left mt-2">
            <thead class="text-xs text-slate-600">
              <tr><th class="px-3 py-1.5">日期</th><th class="px-3 py-1.5">N</th>
                  <th class="px-3 py-1.5">换手率</th><th class="px-3 py-1.5">手续费</th>
                  <th class="px-3 py-1.5">买入</th><th class="px-3 py-1.5">卖出</th></tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>
        </details>`;
    } else if (nReb === 0 && (dynM.n_tracked_days || 0) === 0) {
      summaryHtml += `<div class="text-xs text-slate-500 italic">⏳ 锁定日刚定，还没经历过 rebalance — 第一个周一收盘后会出现第 1 次调仓记录。</div>`;
    }

    rebalanceEl.innerHTML = summaryHtml;
  }

  // Daily P&L last 60 days
  if (dailyEl && typeof echarts !== 'undefined' && data.daily_returns) {
    const N = Math.min(60, data.daily_returns.length);
    const slice = data.daily_returns.slice(-N);
    const dates = data.dates.slice(-N);
    let dailyChart = echarts.getInstanceByDom(dailyEl);
    if (!dailyChart) dailyChart = echarts.init(dailyEl);
    dailyChart.setOption({
      tooltip: { trigger: 'axis', formatter: p => p[0].axisValue + '<br/>' + p[0].value.toFixed(2) + '%' },
      xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10, rotate: 30 } },
      yAxis: { type: 'value', axisLabel: { formatter: '{value}%', fontSize: 10 } },
      grid: { left: 50, right: 20, top: 20, bottom: 50 },
      series: [{
        type: 'bar',
        data: slice.map(v => ({
          value: v,
          itemStyle: { color: v >= 0 ? '#10b981' : '#f43f5e' }
        })),
      }],
    });
    requestAnimationFrame(() => dailyChart.resize());
    setTimeout(() => dailyChart.resize(), 200);
    if (!window._BACKTEST_DAILY_RESIZE_HOOKED) {
      window._BACKTEST_DAILY_RESIZE_HOOKED = true;
      window.addEventListener('resize', () => dailyChart.resize());
    }
  }

  // Contribution table
  if (tableEl && data.per_ticker) {
    const rows = data.per_ticker.map((r, i) => `
      <tr class="border-b border-slate-100 hover:bg-slate-50">
        <td class="px-3 py-2 text-xs text-slate-500">${i + 1}</td>
        <td class="px-3 py-2 font-mono text-sm font-semibold">${r.ticker}</td>
        <td class="px-3 py-2 text-sm text-slate-700">${r.name || '—'}</td>
        <td class="px-3 py-2 text-sm">${(r.weight * 100).toFixed(2)}%</td>
        <td class="px-3 py-2 text-sm">${r.close_first.toFixed(2)} → ${r.close_last.toFixed(2)}</td>
        <td class="px-3 py-2 text-sm font-semibold ${cls(r.return_pct)}">${fmtPct(r.return_pct)}</td>
        <td class="px-3 py-2 text-sm font-bold ${cls(r.contribution_pct)}">${fmtPct(r.contribution_pct)}</td>
      </tr>
    `).join('');
    tableEl.innerHTML = `
      <table class="w-full text-left">
        <thead class="bg-slate-50 text-xs text-slate-600">
          <tr>
            <th class="px-3 py-2">#</th>
            <th class="px-3 py-2">代码</th>
            <th class="px-3 py-2">公司名</th>
            <th class="px-3 py-2">权重</th>
            <th class="px-3 py-2">期初 → 期末</th>
            <th class="px-3 py-2">个股累计</th>
            <th class="px-3 py-2">组合贡献</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }
}

const aiCount = {};
RECORDS.forEach(r => { const k = r.ai_relevance || "未分类"; aiCount[k] = (aiCount[k] || 0) + 1; });
echarts.init(document.getElementById("chart-ai")).setOption({
  tooltip: { trigger: "item" },
  legend: { bottom: 0, left: "center", textStyle: { fontSize: 11 } },
  series: [{ name: "AI 关联度", type: "pie", radius: ["35%","65%"], center:["50%","45%"],
    data: Object.entries(aiCount).map(([k,v])=>({ name: k, value: v })),
    label: { show: true, formatter: "{b}\n{c}" } }]
});

const marketCount = {};
RECORDS.forEach(r => { const k = r.market || "未知"; marketCount[k] = (marketCount[k] || 0) + 1; });
echarts.init(document.getElementById("chart-market")).setOption({
  tooltip: { trigger: "item" },
  legend: { bottom: 0, left: "center", textStyle: { fontSize: 11 } },
  series: [{ name: "市场", type: "pie", radius: ["35%","65%"], center:["50%","45%"],
    data: Object.entries(marketCount).map(([k,v])=>({ name: k, value: v })),
    label: { show: true, formatter: "{b}\n{c}" } }]
});

const statusCount = {};
RECORDS.forEach(r => { const k = r.status || "未分类"; statusCount[k] = (statusCount[k] || 0) + 1; });
echarts.init(document.getElementById("chart-status")).setOption({
  tooltip: { trigger: "axis" },
  xAxis: { type: "category", data: Object.keys(statusCount), axisLabel:{ fontSize: 10, interval: 0, rotate: 15 } },
  yAxis: { type: "value" },
  series: [{ type: "bar", data: Object.values(statusCount), itemStyle:{ color: "#8b5cf6" } }]
});

const searchBox = document.getElementById("searchBox");
searchBox.addEventListener("input", () => {
  const q = searchBox.value.toLowerCase();
  document.querySelectorAll("[data-search]").forEach(card => {
    const m = card.getAttribute("data-search").toLowerCase();
    card.style.display = m.includes(q) ? "" : "none";
  });
});

// ============ 🌟 今日 top picks（最新一批入选，按评分排序）============
function _ratingScore(rating) {
  if (!rating) return 0;
  if (rating.startsWith("⭐⭐⭐")) return 3;
  if (rating.startsWith("⭐⭐")) return 2;
  if (rating.startsWith("⭐")) return 1;
  return 0;
}

let _latestPickDate = null;
PICKS.forEach(p => {
  if (p.pick_date && (_latestPickDate == null || p.pick_date > _latestPickDate)) {
    _latestPickDate = p.pick_date;
  }
});

// 数据问题：同一只股同一天可能有多档评分记录（⭐⭐⭐/⭐⭐/🟡 共存），
// 且 daily_refresh 偶尔重复写入导致 2 倍重复。按 code 去重，保留评分最高那条。
// TODO（数据端）：daily_picks_v5 写入飞书时应该 upsert 而非 insert，避免重复。
const _todayDedup = new Map();
PICKS.forEach(p => {
  if (p.pick_date !== _latestPickDate) return;
  const key = p.code || p.name || "";
  if (!key) return;
  const cur = _todayDedup.get(key);
  if (!cur) {
    _todayDedup.set(key, p);
    return;
  }
  const sNew = _ratingScore(p.rating);
  const sCur = _ratingScore(cur.rating);
  // 评分高的胜出；评分相同时取 score 大的
  if (sNew > sCur || (sNew === sCur && (parseFloat(p.score) || 0) > (parseFloat(cur.score) || 0))) {
    _todayDedup.set(key, p);
  }
});

const todayPicks = Array.from(_todayDedup.values()).sort((a, b) => {
  const ra = _ratingScore(a.rating), rb = _ratingScore(b.rating);
  if (rb !== ra) return rb - ra;
  return (parseFloat(b.score) || 0) - (parseFloat(a.score) || 0);
});

if (_latestPickDate) {
  const d = new Date(_latestPickDate);
  const dateStr = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  document.getElementById("picks-today-meta").textContent = `· ${dateStr} 入选 ${todayPicks.length} 只 · 按评分+综合分排序`;
}

function _ratingBadge(rating) {
  const s = _ratingScore(rating);
  const cls = s === 3 ? "bg-rose-100 text-rose-700"
            : s === 2 ? "bg-amber-100 text-amber-700"
            : s === 1 ? "bg-slate-100 text-slate-600"
            : "bg-slate-100 text-slate-400";
  return `<span class="text-[10px] ${cls} px-1.5 py-0.5 rounded font-medium whitespace-nowrap">${rating || "—"}</span>`;
}

function _pctBadge(pct) {
  if (pct == null || pct === "") return '<span class="text-xs text-slate-400">— 持有 <1 天</span>';
  const v = parseFloat(pct);
  const cls = v > 0 ? "text-emerald-600" : v < 0 ? "text-rose-600" : "text-slate-500";
  const sign = v > 0 ? "+" : "";
  return `<span class="text-xs font-mono font-bold ${cls}">${sign}${v.toFixed(2)}%</span>`;
}

document.getElementById("picks-today-list").innerHTML = todayPicks.length > 0
  ? todayPicks.map(p => `
      <div class="bg-white rounded-lg p-3 border border-violet-200 hover:border-violet-400 transition">
        <div class="flex items-center justify-between gap-2 mb-1.5">
          <span class="font-bold text-slate-900 truncate font-mono">${p.code || "?"}</span>
          ${_ratingBadge(p.rating)}
        </div>
        <div class="text-xs text-slate-700 truncate mb-1.5">${p.name || ""}</div>
        <div class="flex items-center justify-between text-xs gap-2">
          <span class="text-slate-500 truncate">${p.theme || ""}</span>
          ${_pctBadge(p.pct)}
        </div>
      </div>
    `).join("")
  : '<div class="col-span-3 text-center text-slate-500 text-sm py-4">暂无入选数据 — daily_picks_v5 跑完后会显示</div>';


// ============ 自选股·今日 Top回顾 ============
const validPicks = PICKS.filter(p => p.pct != null && p.pct !== "");
const totalPicks = PICKS.length;
const validCount = validPicks.length;
const avgPct = validCount > 0 ? validPicks.reduce((s, p) => s + parseFloat(p.pct), 0) / validCount : 0;
const winCount = validPicks.filter(p => parseFloat(p.pct) > 5).length;
const flatCount = validPicks.filter(p => { const v = parseFloat(p.pct); return v >= -5 && v <= 5; }).length;
const lossCount = validPicks.filter(p => parseFloat(p.pct) < -5).length;
const winRate = validCount > 0 ? (winCount / validCount * 100) : 0;

// 30 天不重复股票数（按 code 去重，反映"系统在选股集合的广度"）
const _uniqueCodes30d = new Set();
PICKS.forEach(p => { if (p.code) _uniqueCodes30d.add(p.code); });
const uniqueStocks30d = _uniqueCodes30d.size;

document.getElementById("picks-summary").innerHTML = totalPicks > 0
  ? `<div class="text-xs text-slate-500 leading-relaxed">
       <div>最近 30 天：<strong class="text-amber-700">${uniqueStocks30d}</strong> 只不重复股票被选过</div>
       <div class="text-[10px] text-slate-400 mt-0.5">原始 ${totalPicks} 行 · 含多档评分 + 数据重复（待修）</div>
     </div>`
  : "";

document.getElementById("picks-stats").innerHTML = totalPicks > 0 ? `
  <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200">
    <div class="text-2xl font-bold text-slate-900">${uniqueStocks30d}</div>
    <div class="text-xs text-slate-500 mt-1">不重复股票数（30 天内被选过）</div>
  </div>
  <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200">
    <div class="text-2xl font-bold ${avgPct > 0 ? 'text-emerald-600' : 'text-rose-600'}">${avgPct > 0 ? '+' : ''}${avgPct.toFixed(2)}%</div>
    <div class="text-xs text-slate-500 mt-1">入选后平均涨跌</div>
  </div>
  <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200">
    <div class="text-2xl font-bold text-emerald-600">${winRate.toFixed(0)}%</div>
    <div class="text-xs text-slate-500 mt-1">大涨命中率（>+5%）</div>
  </div>
  <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200">
    <div class="text-lg font-bold"><span class="text-emerald-600">${winCount}</span> <span class="text-amber-500">/ ${flatCount}</span> <span class="text-rose-600">/ ${lossCount}</span></div>
    <div class="text-[11px] text-slate-500 mt-1">命中 <span class="text-slate-400">/</span> 跟随 (±5%) <span class="text-slate-400">/</span> 失败 (&lt;-5%)</div>
  </div>
  <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200">
    <div class="text-2xl font-bold text-slate-900">${validCount}</div>
    <div class="text-xs text-slate-500 mt-1">有持有天数的样本（可统计涨跌）</div>
  </div>
` : '<div class="col-span-5 text-center text-slate-500 py-4">暂无入选记录</div>';

// 评分 vs 实际表现
const byRating = {};
validPicks.forEach(p => {
  const r = p.rating || "未分级";
  if (!byRating[r]) byRating[r] = [];
  byRating[r].push(parseFloat(p.pct));
});
const ratingHtml = Object.entries(byRating)
  .sort((a, b) => b[0].localeCompare(a[0]))
  .map(([rating, pcts]) => {
    const avg = pcts.reduce((s, v) => s + v, 0) / pcts.length;
    const color = avg > 0 ? "text-emerald-600" : "text-rose-600";
    return `<div class="flex justify-between items-center py-1 border-b border-slate-100 last:border-0 text-sm">
      <span class="text-slate-700">${rating} <span class="text-xs text-slate-400">(${pcts.length} 只)</span></span>
      <span class="${color} font-mono font-bold">${avg > 0 ? '+' : ''}${avg.toFixed(2)}%</span>
    </div>`;
  }).join("");
document.getElementById("picks-by-rating").innerHTML = ratingHtml || '<div class="text-slate-500 text-sm">暂无数据</div>';

// 主题表现
const byTheme = {};
validPicks.forEach(p => {
  const t = p.theme || "未分类";
  if (!byTheme[t]) byTheme[t] = [];
  byTheme[t].push(parseFloat(p.pct));
});
const themeHtml = Object.entries(byTheme)
  .map(([t, pcts]) => ({ t, avg: pcts.reduce((s, v) => s + v, 0) / pcts.length, n: pcts.length }))
  .sort((a, b) => b.avg - a.avg)
  .map(d => {
    const color = d.avg > 0 ? "text-emerald-600" : "text-rose-600";
    return `<div class="flex justify-between items-center py-1 border-b border-slate-100 last:border-0 text-sm">
      <span class="text-slate-700 truncate flex-1">${d.t} <span class="text-xs text-slate-400">(${d.n})</span></span>
      <span class="${color} font-mono font-bold flex-shrink-0">${d.avg > 0 ? '+' : ''}${d.avg.toFixed(2)}%</span>
    </div>`;
  }).join("");
document.getElementById("picks-by-theme").innerHTML = themeHtml || '<div class="text-slate-500 text-sm">暂无数据</div>';

// Top 5 / Bottom 5 — 按 code 去重,每只股保留 days_held 最大那条(持有最久,最有代表性)
const dedupByCode = new Map();
for (const p of validPicks) {
  const prev = dedupByCode.get(p.code);
  if (!prev || (parseFloat(p.days_held) || 0) > (parseFloat(prev.days_held) || 0)) {
    dedupByCode.set(p.code, p);
  }
}
const sortedPicks = [...dedupByCode.values()].sort((a, b) => parseFloat(b.pct) - parseFloat(a.pct));
function pickRow(p) {
  const v = parseFloat(p.pct);
  const color = v > 0 ? "text-emerald-600" : "text-rose-600";
  const sign = v > 0 ? "+" : "";
  return `<div class="flex items-center justify-between py-1 px-2 rounded hover:bg-slate-50 text-sm">
    <div class="flex items-center gap-2 min-w-0 flex-1">
      <span class="font-mono text-xs text-slate-500 w-12 truncate">${p.code}</span>
      <span class="text-slate-700 truncate">${p.name}</span>
    </div>
    <div class="flex items-center gap-2 flex-shrink-0">
      <span class="text-xs text-slate-400">${p.days_held || 0}天</span>
      <span class="${color} font-mono font-bold text-sm">${sign}${v.toFixed(1)}%</span>
    </div>
  </div>`;
}
document.getElementById("picks-top").innerHTML = sortedPicks.slice(0, 5).map(pickRow).join("") || '<div class="text-slate-500 text-sm">暂无数据</div>';
document.getElementById("picks-bottom").innerHTML = sortedPicks.slice(-5).reverse().map(pickRow).join("") || '<div class="text-slate-500 text-sm">暂无数据</div>';

// ============ 🔍 AI 推荐 ============
// 工具:格式化 alpha 数字 +color
function _fmtAlpha(v) {
  if (v == null || v === undefined || isNaN(v)) return '<span class="text-slate-300">—</span>';
  const sign = v >= 0 ? "+" : "";
  const cls = v >= 0 ? "text-emerald-600" : "text-rose-600";
  return `<span class="font-mono ${cls}">${sign}${v.toFixed(1)}%</span>`;
}

// 工具:从 DISCOVERY_HISTORY 找 (date, ticker) 对应的 alpha 记录
function _lookupTrackingForToday(ticker) {
  if (!Array.isArray(DISCOVERY_HISTORY) || !DISCOVERY_HISTORY.length) return null;
  // 找最早一次推荐这个 ticker 的 alpha (最早推荐说明跟踪时间最长)
  const recs = DISCOVERY_HISTORY.filter(h => h.ticker === ticker);
  if (!recs.length) return null;
  recs.sort((a, b) => (a.generated_date || "").localeCompare(b.generated_date || ""));
  return recs[0];  // 最早那次
}

// 工具:聚合统计 — 用于准确度面板
function _computeDiscoveryStats() {
  const all = (DISCOVERY_HISTORY || []).filter(r => r.alpha_5d != null || r.alpha_20d != null);
  if (!all.length) {
    return { totalRecs: (DISCOVERY_HISTORY || []).length, evaluated: 0, panel: null };
  }
  const mean = arr => arr.length ? arr.reduce((s, v) => s + v, 0) / arr.length : null;
  const a1 = all.map(r => r.alpha_1d).filter(v => v != null);
  const a5 = all.map(r => r.alpha_5d).filter(v => v != null);
  const a20 = all.map(r => r.alpha_20d).filter(v => v != null);
  const a60 = all.map(r => r.alpha_60d).filter(v => v != null);
  const hit20 = a20.filter(v => v > 5).length;
  const hitRate = a20.length ? (hit20 / a20.length * 100) : null;
  // composite_z 分位 vs alpha_20d 看相关性(简化 IC: 高分组 vs 低分组的 alpha 差异)
  const zRanked = all.filter(r => r.composite_z != null && r.alpha_20d != null)
    .sort((a, b) => b.composite_z - a.composite_z);
  let topAlpha20 = null, botAlpha20 = null;
  if (zRanked.length >= 6) {
    const tier = Math.floor(zRanked.length / 3);
    topAlpha20 = mean(zRanked.slice(0, tier).map(r => r.alpha_20d));
    botAlpha20 = mean(zRanked.slice(-tier).map(r => r.alpha_20d));
  }
  return {
    totalRecs: (DISCOVERY_HISTORY || []).length,
    evaluated: all.length,
    panel: {
      alpha_1d: mean(a1),
      alpha_5d: mean(a5),
      alpha_20d: mean(a20),
      alpha_60d: mean(a60),
      hitRate20d: hitRate,
      hitCount20d: hit20,
      hitTotal20d: a20.length,
      topAlpha20: topAlpha20,
      botAlpha20: botAlpha20,
      ic_spread: (topAlpha20 != null && botAlpha20 != null) ? (topAlpha20 - botAlpha20) : null,
    }
  };
}

(function renderDiscovery() {
  const wrap = document.getElementById("discovery-table-wrap");
  const empty = document.getElementById("discovery-empty");
  const meta = document.getElementById("discovery-meta");
  const tbody = document.getElementById("discovery-table-body");
  const accuracyEl = document.getElementById("discovery-accuracy");
  const historyEl = document.getElementById("discovery-history-list");
  const cands = (DISCOVERY && DISCOVERY.candidates) || [];

  // ── sub-tab 计数显示
  const tabCountEl = document.getElementById("disc-tab-count");
  if (tabCountEl) {
    const nHist = Array.isArray(DISCOVERY_HISTORY) ? DISCOVERY_HISTORY.length : 0;
    const nDates = nHist ? new Set(DISCOVERY_HISTORY.map(h => h.generated_date)).size : 0;
    tabCountEl.textContent = `今日 ${cands.length} 只 · 历史 ${nDates} 天 / ${nHist} 条推荐`;
  }

  // ── 准确度面板(基于 DISCOVERY_HISTORY 历史回测数据)
  const stats = _computeDiscoveryStats();
  if (accuracyEl) {
    if (!stats.totalRecs) {
      accuracyEl.innerHTML = `<div class="bg-amber-50 border border-amber-200 rounded-lg p-4 text-sm text-amber-800">
        <strong>📊 算法准确度</strong> · 暂无历史推荐数据 — 每天跑一次 discover_candidates.py + evaluate_discovery.py,数据将自动累积。1 周后回来看第一组 5d alpha,1 个月后看 20d alpha。</div>`;
    } else if (!stats.evaluated) {
      accuracyEl.innerHTML = `<div class="bg-sky-50 border border-sky-200 rounded-lg p-4 text-sm text-sky-800">
        <strong>📊 算法准确度</strong> · 已记录 ${stats.totalRecs} 条推荐,但**还没有 alpha 数据**(可能 evaluate_discovery 还没跑,或推荐才生成不久 yfinance 拉不到对应日期的价格)。</div>`;
    } else {
      const p = stats.panel;
      const cell = (label, v, hint) => {
        const fmt = (v == null) ? "—" : ((v >= 0 ? "+" : "") + v.toFixed(2) + "%");
        const cls = v == null ? "text-slate-400" : (v >= 0 ? "text-emerald-600" : "text-rose-600");
        return `<div class="bg-white rounded-lg p-3 border border-slate-200">
          <div class="text-xs text-slate-500">${label}</div>
          <div class="text-2xl font-bold ${cls} font-mono">${fmt}</div>
          ${hint ? `<div class="text-[10px] text-slate-400 mt-0.5">${hint}</div>` : ""}
        </div>`;
      };
      const hitCell = (p.hitRate20d == null) ? "" : `<div class="bg-white rounded-lg p-3 border border-slate-200">
        <div class="text-xs text-slate-500">20d 大涨命中率</div>
        <div class="text-2xl font-bold text-violet-700 font-mono">${p.hitRate20d.toFixed(0)}%</div>
        <div class="text-[10px] text-slate-400 mt-0.5">${p.hitCount20d}/${p.hitTotal20d} 推荐 alpha >+5%</div>
      </div>`;
      const icCell = (p.ic_spread == null) ? "" : `<div class="bg-white rounded-lg p-3 border border-slate-200">
        <div class="text-xs text-slate-500">高低分组 20d α 差(信号 IC)</div>
        <div class="text-xl font-bold font-mono ${p.ic_spread >= 0 ? 'text-emerald-600' : 'text-rose-600'}">${p.ic_spread >= 0 ? '+' : ''}${p.ic_spread.toFixed(2)}%</div>
        <div class="text-[10px] text-slate-400 mt-0.5">Top 1/3 vs Bot 1/3 平均 α 差</div>
      </div>`;

      accuracyEl.innerHTML = `<div class="bg-gradient-to-br from-violet-50 to-sky-50 rounded-xl p-4 border border-violet-200">
        <div class="flex items-center gap-2 mb-3">
          <span class="text-xl">📊</span>
          <h3 class="text-lg font-bold text-slate-900">算法准确度</h3>
          <span class="text-xs text-slate-500">基于过去 ${stats.totalRecs} 次推荐 · ${stats.evaluated} 条已评估</span>
        </div>
        <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
          ${cell("平均 1d α", p.alpha_1d, "1 个交易日 vs SPY")}
          ${cell("平均 5d α", p.alpha_5d, "5 个交易日 vs SPY")}
          ${cell("平均 20d α", p.alpha_20d, "20 个交易日 ≈ 1 个月")}
          ${cell("平均 60d α", p.alpha_60d, "60 个交易日 ≈ 3 个月")}
          ${hitCell}
          ${icCell}
        </div>
        <p class="text-xs text-slate-500 mt-3">
          📌 <strong>α (alpha)</strong> = 推荐后涨幅 - 同期 SPY 涨幅。正值 = 跑赢基准,负值 = 跑输。
          <strong>信号 IC</strong> 正值越大,说明 composite_z 高分组真的更准。
        </p>
      </div>`;
    }
  }

  // ── 今日推荐表格
  if (!cands.length) {
    if (wrap) wrap.classList.add("hidden");
    if (empty) empty.classList.remove("hidden");
  } else {
    meta.innerHTML = `生成于 <strong>${DISCOVERY.generated_at || "?"}</strong> · `
      + `universe ${DISCOVERY.universe_size || "?"} 只(已排除 watchlist ${DISCOVERY.watchlist_excluded || "?"} 只) · `
      + `数据源 ${(DISCOVERY.etf_sources || []).join(" / ")} · `
      + `市值门槛 $${((DISCOVERY.min_market_cap_usd || 0) / 1e9).toFixed(0)}B`;
    tbody.innerHTML = cands.map(c => {
      const cap = c.market_cap_usd ? (c.market_cap_usd / 1e9).toFixed(1) : "-";
      const f = c.f_score == null ? "-" : Math.round(c.f_score);
      const fColor = c.f_score >= 7 ? "text-emerald-600" : (c.f_score >= 4 ? "text-amber-600" : "text-rose-600");
      const mom = c.momentum_12_1 == null ? "-" : (c.momentum_12_1 > 0 ? "+" : "") + c.momentum_12_1.toFixed(1) + "%";
      const momColor = (c.momentum_12_1 || 0) > 0 ? "text-emerald-600" : "text-rose-600";
      const zColor = c.composite_z > 0 ? "text-emerald-600 font-bold" : "text-rose-600";
      const etfs = (c.etfs || []).map(e => `<span class="inline-block px-1.5 py-0.5 mr-1 text-xs bg-indigo-100 text-indigo-700 rounded">${e}</span>`).join("");
      const market = (() => {
        const t = c.ticker || "";
        if (t.endsWith(".SS")) return "🇨🇳 沪 A";
        if (t.endsWith(".SZ")) return "🇨🇳 深 A";
        if (t.endsWith(".HK")) return "🇭🇰 港股";
        if (t.endsWith(".TW") || t.endsWith(".TWO")) return "🇹🇼 台股";
        if (t.endsWith(".KS")) return "🇰🇷 韩股";
        if (t.endsWith(".T"))  return "🇯🇵 日股";
        if (t.endsWith(".AX")) return "🇦🇺 澳股";
        if (t.endsWith(".L"))  return "🇬🇧 英股";
        return "🇺🇸 美股";
      })();
      // 从历史里查 alpha (用本 ticker 最早一次推荐的 5d/20d alpha — 它跟踪时间最长)
      const track = _lookupTrackingForToday(c.ticker);
      const alpha5 = track ? track.alpha_5d : null;
      const alpha20 = track ? track.alpha_20d : null;
      return `<tr class="hover:bg-slate-50">
        <td class="px-3 py-2 font-mono text-slate-500">${c.rank}</td>
        <td class="px-3 py-2 font-mono font-semibold text-slate-900">${c.ticker}</td>
        <td class="px-3 py-2 text-slate-700">
          <div>${c.name || ""}</div>
          <div class="mt-0.5">${stockPill(c.ticker, {layout: "mini"})}</div>
        </td>
        <td class="px-3 py-2 text-xs text-slate-700 whitespace-nowrap">${market}</td>
        <td class="px-3 py-2 text-xs text-slate-500">${c.sector || ""}</td>
        <td class="px-3 py-2 text-right font-mono ${zColor}">${c.composite_z >= 0 ? "+" : ""}${c.composite_z.toFixed(2)}</td>
        <td class="px-3 py-2 text-right font-mono ${fColor}">${f}</td>
        <td class="px-3 py-2 text-right font-mono ${momColor}">${mom}</td>
        <td class="px-3 py-2 text-right font-mono text-slate-700">${c.analyst_score || 0}</td>
        <td class="px-3 py-2 text-right font-mono text-slate-700">${cap}</td>
        <td class="px-3 py-2 text-right">${_fmtAlpha(alpha5)}</td>
        <td class="px-3 py-2 text-right">${_fmtAlpha(alpha20)}</td>
        <td class="px-3 py-2">${etfs}</td>
        <td class="px-3 py-2 text-center">
          <button data-ticker="${_esc(c.ticker)}"
                  onclick="addDiscoveryToWatchlist('${_esc(c.ticker)}', this)"
                  class="discovery-add-btn px-2 py-1 text-xs bg-violet-600 hover:bg-violet-700 text-white rounded transition whitespace-nowrap">
            ➕ 加自选
          </button>
        </td>
      </tr>`;
    }).join("");

    // 异步加载 watchlist,把已在自选里的票按钮置灰
    _markDiscoveryAddedButtons();
  }

  // ── 推荐历史(按日期分组,折叠/展开)
  if (historyEl) {
    if (!Array.isArray(DISCOVERY_HISTORY) || !DISCOVERY_HISTORY.length) {
      historyEl.innerHTML = `<div class="text-sm text-slate-500 bg-white rounded-lg p-4 border border-slate-200">
        暂无历史推荐数据。每次跑 discover_candidates.py 会自动追加一批,跨日累积。
      </div>`;
    } else {
      // 按 generated_date 分组
      const byDate = {};
      DISCOVERY_HISTORY.forEach(h => {
        const d = h.generated_date || "未知";
        (byDate[d] = byDate[d] || []).push(h);
      });
      const dates = Object.keys(byDate).sort().reverse();  // 倒序

      historyEl.innerHTML = dates.map(d => {
        const recs = byDate[d];
        recs.sort((a, b) => (a.rank || 0) - (b.rank || 0));
        // 当日均 alpha
        const a5s = recs.map(r => r.alpha_5d).filter(v => v != null);
        const a20s = recs.map(r => r.alpha_20d).filter(v => v != null);
        const meanA5 = a5s.length ? (a5s.reduce((s, v) => s + v, 0) / a5s.length) : null;
        const meanA20 = a20s.length ? (a20s.reduce((s, v) => s + v, 0) / a20s.length) : null;
        const hit5 = a5s.filter(v => v > 5).length;

        const summary = `<div class="flex items-center gap-3 text-sm">
          <span class="font-bold text-slate-900">${d}</span>
          <span class="text-slate-500">推荐 ${recs.length} 只</span>
          <span class="text-slate-400">·</span>
          <span class="text-xs text-slate-600">5d α: ${meanA5 == null ? "—" : (meanA5 >= 0 ? "+" : "") + meanA5.toFixed(2) + "%"}</span>
          <span class="text-xs text-slate-600">20d α: ${meanA20 == null ? "—" : (meanA20 >= 0 ? "+" : "") + meanA20.toFixed(2) + "%"}</span>
          <span class="text-xs text-slate-600">大涨命中: ${hit5}/${a5s.length || "—"}</span>
        </div>`;

        const rows = recs.map(r => {
          const a1 = _fmtAlpha(r.alpha_1d);
          const a5 = _fmtAlpha(r.alpha_5d);
          const a20 = _fmtAlpha(r.alpha_20d);
          const a60 = _fmtAlpha(r.alpha_60d);
          return `<tr class="hover:bg-slate-50">
            <td class="px-2 py-1 font-mono text-xs text-slate-500">#${r.rank}</td>
            <td class="px-2 py-1 font-mono text-xs font-bold">${r.ticker}</td>
            <td class="px-2 py-1 text-xs text-slate-700 max-w-xs truncate">${r.name || ""}</td>
            <td class="px-2 py-1 text-xs">${stockPill(r.ticker, {layout: "mini"})}</td>
            <td class="px-2 py-1 text-right text-xs font-mono">${r.composite_z != null ? (r.composite_z >= 0 ? "+" : "") + r.composite_z.toFixed(2) : "—"}</td>
            <td class="px-2 py-1 text-right text-xs">${a1}</td>
            <td class="px-2 py-1 text-right text-xs">${a5}</td>
            <td class="px-2 py-1 text-right text-xs">${a20}</td>
            <td class="px-2 py-1 text-right text-xs">${a60}</td>
          </tr>`;
        }).join("");

        const id = "disc-hist-" + d.replace(/[^a-zA-Z0-9]/g, "_");
        return `<details class="bg-white rounded-lg border border-slate-200 overflow-hidden">
          <summary class="cursor-pointer px-4 py-3 hover:bg-slate-50">${summary}</summary>
          <div class="px-4 py-3 border-t border-slate-100 overflow-x-auto">
            <table class="w-full text-sm">
              <thead class="text-[10px] text-slate-500 uppercase">
                <tr>
                  <th class="px-2 py-1 text-left">排名</th>
                  <th class="px-2 py-1 text-left">代码</th>
                  <th class="px-2 py-1 text-left">名字</th>
                  <th class="px-2 py-1 text-left">链条</th>
                  <th class="px-2 py-1 text-right">z 分</th>
                  <th class="px-2 py-1 text-right">1d α</th>
                  <th class="px-2 py-1 text-right">5d α</th>
                  <th class="px-2 py-1 text-right">20d α</th>
                  <th class="px-2 py-1 text-right">60d α</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-100">${rows}</tbody>
            </table>
          </div>
        </details>`;
      }).join("");
    }
  }
})();

// ============ 估值视角 ============
const validForVal = RECORDS.filter(r => r.forward_pe != null && r.forward_pe !== "" && r.ytd_pct != null && r.ytd_pct !== "");

function pctColor(p) { return p > 0 ? "#10b981" : "#ef4444"; }
function aiColor(ar) {
  if ((ar||"").includes("极强")) return "#ef4444";
  if ((ar||"").includes("强")) return "#f59e0b";
  if ((ar||"").includes("中")) return "#3b82f6";
  if ((ar||"").includes("弱")) return "#94a3b8";
  return "#cbd5e1";
}

// 散点图：PE × YTD
const scatterData = validForVal
  .filter(r => parseFloat(r.forward_pe) > 0)  // 排除负 PE（亏损）
  .map(r => {
    const pe = Math.min(parseFloat(r.forward_pe), 100);  // PE > 100 截断到 100
    const ytd = parseFloat(r.ytd_pct);
    return {
      value: [pe, ytd],
      name: r.name,
      code: r.code,
      itemStyle: { color: aiColor(r.ai_relevance), opacity: 0.85 },
    };
  });

echarts.init(document.getElementById("chart-pe-ytd")).setOption({
  tooltip: {
    trigger: "item",
    formatter: p => `<strong>${p.data.name}</strong> (${p.data.code})<br>远期 PE: ${p.data.value[0]}<br>YTD: ${p.data.value[1] > 0 ? '+' : ''}${p.data.value[1]}%`
  },
  grid: { left: 60, right: 30, top: 30, bottom: 50 },
  xAxis: {
    name: "远期 PE →",
    nameLocation: "end",
    nameGap: 25,
    type: "value",
    min: 0,
    max: 100,
    splitLine: { show: true, lineStyle: { color: "#e2e8f0" } },
  },
  yAxis: {
    name: "YTD %",
    nameGap: 30,
    type: "value",
    splitLine: { show: true, lineStyle: { color: "#e2e8f0" } },
  },
  series: [
    {
      type: "scatter",
      symbolSize: 14,
      data: scatterData,
      label: {
        show: true,
        position: "right",
        fontSize: 10,
        formatter: p => p.data.code,
        color: "#475569",
      },
      markLine: {
        silent: true,
        symbol: "none",
        lineStyle: { color: "#cbd5e1", type: "dashed" },
        data: [{ yAxis: 0 }, { xAxis: 25 }],
      },
    },
  ],
});

// 排行榜
function rankCard(r, valueLabel, valueClass) {
  return `<div class="flex items-center justify-between py-1 px-2 rounded hover:bg-slate-50 text-sm">
    <div class="flex items-center gap-2 min-w-0 flex-1">
      <span class="font-mono text-xs text-slate-500 w-12 truncate">${r.code}</span>
      <span class="text-slate-700 truncate">${r.name}</span>
    </div>
    <span class="${valueClass} font-mono font-bold text-sm flex-shrink-0">${valueLabel}</span>
  </div>`;
}

// PEG 低 Top 5（最关键 — 真便宜）
const pegLow = RECORDS
  .filter(r => r.peg != null && r.peg !== "" && parseFloat(r.peg) > 0)
  .sort((a, b) => parseFloat(a.peg) - parseFloat(b.peg))
  .slice(0, 5);
document.getElementById("rank-peg-low").innerHTML = pegLow.map(r =>
  rankCard(r, parseFloat(r.peg).toFixed(2), "text-emerald-600")
).join("");

// 1 周涨幅 Top 5
const wkHigh = RECORDS
  .filter(r => r.one_week_pct != null && r.one_week_pct !== "")
  .sort((a, b) => parseFloat(b.one_week_pct) - parseFloat(a.one_week_pct))
  .slice(0, 5);
document.getElementById("rank-1w-high").innerHTML = wkHigh.map(r => {
  const v = parseFloat(r.one_week_pct);
  return rankCard(r, (v > 0 ? "+" : "") + v.toFixed(1) + "%", "text-emerald-600");
}).join("");

// 1 周跌幅 Top 5
const wkLow = RECORDS
  .filter(r => r.one_week_pct != null && r.one_week_pct !== "")
  .sort((a, b) => parseFloat(a.one_week_pct) - parseFloat(b.one_week_pct))
  .slice(0, 5);
document.getElementById("rank-1w-low").innerHTML = wkLow.map(r => {
  const v = parseFloat(r.one_week_pct);
  return rankCard(r, (v > 0 ? "+" : "") + v.toFixed(1) + "%", "text-rose-600");
}).join("");

// PE 低 Top 5（排除负 PE）
const peLow = validForVal
  .filter(r => parseFloat(r.forward_pe) > 0)
  .sort((a, b) => parseFloat(a.forward_pe) - parseFloat(b.forward_pe))
  .slice(0, 5);
document.getElementById("rank-pe-low").innerHTML = peLow.map(r =>
  rankCard(r, parseFloat(r.forward_pe).toFixed(1), "text-emerald-600")
).join("");

// YTD 高 Top 5
const ytdHigh = validForVal
  .sort((a, b) => parseFloat(b.ytd_pct) - parseFloat(a.ytd_pct))
  .slice(0, 5);
document.getElementById("rank-ytd-high").innerHTML = ytdHigh.map(r => {
  const v = parseFloat(r.ytd_pct);
  return rankCard(r, (v > 0 ? "+" : "") + v.toFixed(1) + "%", "text-emerald-600");
}).join("");

// YTD 低 Top 5
const ytdLow = validForVal
  .sort((a, b) => parseFloat(a.ytd_pct) - parseFloat(b.ytd_pct))
  .slice(0, 5);
document.getElementById("rank-ytd-low").innerHTML = ytdLow.map(r => {
  const v = parseFloat(r.ytd_pct);
  return rankCard(r, (v > 0 ? "+" : "") + v.toFixed(1) + "%", "text-rose-600");
}).join("");

// 「便宜+加速」候选区（PE ≤ 25 且 YTD > 0）
const cheapRising = validForVal.filter(r => {
  const pe = parseFloat(r.forward_pe);
  const ytd = parseFloat(r.ytd_pct);
  return pe > 0 && pe <= 25 && ytd > 0;
}).sort((a, b) => parseFloat(b.ytd_pct) - parseFloat(a.ytd_pct));

document.getElementById("cheap-and-rising").innerHTML = cheapRising.map(r => {
  const pe = parseFloat(r.forward_pe).toFixed(1);
  const ytd = parseFloat(r.ytd_pct);
  return `<div class="bg-emerald-50 border border-emerald-200 rounded p-2 text-xs">
    <div class="font-bold text-slate-800 truncate">${r.name}</div>
    <div class="text-slate-500 font-mono">${r.code} · PE ${pe}</div>
    <div class="text-emerald-600 font-mono font-bold">YTD +${ytd.toFixed(1)}%</div>
  </div>`;
}).join("") || '<div class="text-slate-500 text-sm">暂无符合条件的标的</div>';
</script>

</body>
</html>
"""


def thesis_card_html(thesis):
    icon, label, content = thesis
    return f'''<div class="bg-white rounded-xl border border-slate-200 p-4 hover:shadow-md transition">
  <div class="text-2xl mb-2">{icon}</div>
  <div class="text-xs font-bold text-violet-600 uppercase tracking-wider mb-1">{label}</div>
  <div class="text-sm text-slate-700 leading-relaxed">{content}</div>
</div>'''


def hundred_x_card_html(c):
    return f'''<div class="bg-white rounded-xl p-4 shadow-sm border border-violet-200">
  <div class="text-3xl mb-2">{c['icon']}</div>
  <div class="font-bold text-slate-800 mb-2">{c['title']}</div>
  <div class="text-xs text-slate-600 leading-relaxed">{c['desc']}</div>
</div>'''


def timeline_item_html(item):
    is_future = "?" in item.get("return", "") or "潜伏" in item.get("stage", "") or "早期" in item.get("stage", "")
    dot_class = "bg-violet-500 pulse-dot" if is_future else "bg-cyan-500"
    bg = "bg-violet-50" if is_future else "bg-white"
    return f'''<div class="relative pl-12 py-3 {bg} rounded-r-lg mb-2">
  <div class="absolute left-3 top-5 w-5 h-5 rounded-full border-4 border-white {dot_class}"></div>
  <div class="flex flex-wrap items-baseline gap-2 mb-1">
    <span class="text-xs font-mono bg-slate-200 text-slate-700 px-2 py-0.5 rounded">{item['year']}</span>
    <span class="font-bold text-slate-800">{item['phase']}</span>
    <span class="text-sm text-slate-600">→ {item['winner']}</span>
  </div>
  <div class="text-sm">
    <span class="text-emerald-600 font-bold">{item['return']}</span>
    <span class="text-slate-400 mx-2">·</span>
    <span class="text-slate-500">{item['stage']}</span>
  </div>
</div>'''


def scarce_theme_card_html(theme):
    """5 大稀缺资源主题（特殊高亮）"""
    return f'''<div class="bg-white rounded-xl p-4 border-2 border-violet-300 shadow-sm">
  <div class="text-2xl mb-2">{theme['emoji']}</div>
  <div class="font-bold text-slate-900 mb-1">{theme['title']}</div>
  <div class="text-xs text-violet-600 font-semibold mb-2">{theme['highlight']}</div>
  <div class="text-xs text-slate-700 leading-relaxed mb-3">{theme['logic']}</div>
  <div>{theme['tickers']}</div>
</div>'''


SCARCE_THEMES = [
    {
        "emoji": "💧",
        "title": "数据中心冷却水",
        "highlight": "Xylem 数据中心订单单 Q1 已超 2025 全年",
        "logic": "AI 数据中心+半导体厂+电力设施 三重水需求叠加。AI 将驱动 +129% 水需求增长（到 2050）。",
        "tickers": ["XYL"],
    },
    {
        "emoji": "🪨",
        "title": "稀土国产化",
        "highlight": "Pentagon 10 年 $110/kg 价格底",
        "logic": "AI 数据中心磁铁+电池+电网 全要稀土。中国控制 70%+ 加工，美国必须自主化。",
        "tickers": ["MP"],
    },
    {
        "emoji": "☢️",
        "title": "铀矿（核能燃料）",
        "highlight": "Cameco 净利 +87%，多年合同到 2030",
        "logic": "Microsoft/Amazon/Google 全部签 SMR 长期 PPA → 直接拉升铀需求。",
        "tickers": ["CCJ"],
    },
    {
        "emoji": "⚛️",
        "title": "SMR 小型核反应堆",
        "highlight": "BWXT 唯一规模化 TRISO 燃料",
        "logic": "数据中心专用核电是 2027-2030 兑现的故事，BWXT 有海军核业务底盘。",
        "tickers": ["BWXT"],
    },
    {
        "emoji": "📊",
        "title": "AI 训练数据",
        "highlight": "Reddit 是唯一上市标的",
        "logic": "Google + OpenAI 长期 licensing 客户。Scale AI（私募 $140亿）说明数据值钱，Reddit 是唯一可买的。",
        "tickers": ["RDDT"],
    },
]


def event_card_html(ev):
    tickers_html = " ".join(f'<span class="ticker-badge">{t}</span>' for t in ev['tickers'])
    return f'''<div class="bg-white rounded-xl border-l-4 border-blue-400 shadow-sm p-4 hover:shadow-md transition">
  <div class="text-xs font-mono text-blue-600 mb-1">📌 {ev['date']}</div>
  <div class="font-bold text-slate-900 mb-1">{ev['title']}</div>
  <div class="text-xs text-slate-600 mb-3">{ev['desc']}</div>
  <div>{tickers_html}</div>
</div>'''


def stock_card_html(rec):
    safe = lambda s: (s or "").replace("\n", "<br>").replace("**", "")
    search_text = f"{rec['name']} {rec['code']} {rec['business']} {rec['industry']} {rec['ai_relevance']}"
    icon, label, color = stock_signal(rec)
    color_class = {
        "red": "bg-red-100 text-red-700",
        "orange": "bg-orange-100 text-orange-700",
        "blue": "bg-blue-100 text-blue-700",
        "purple": "bg-purple-100 text-purple-700",
        "gray": "bg-slate-100 text-slate-600",
    }[color]

    info_breakdown_block = ""
    if rec.get("info_breakdown"):
        info_breakdown_block = f'''<details class="mt-1">
    <summary class="text-xs font-semibold text-violet-700 hover:text-violet-900"><span class="arrow"></span>📋 信息构成（事实/推断/训练数据）</summary>
    <p class="text-xs text-slate-700 mt-1 field-block pl-4 bg-violet-50 p-2 rounded">{safe(rec['info_breakdown'])}</p>
  </details>'''

    cred_badge = ""
    if rec.get("credibility"):
        c = rec["credibility"]
        if "高" in c:
            cred_badge = '<span class="text-xs px-1.5 py-0.5 rounded bg-emerald-100 text-emerald-700 font-mono" title="数据可信度">🟢</span>'
        elif "中" in c:
            cred_badge = '<span class="text-xs px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 font-mono" title="数据可信度">🟡</span>'
        elif "低" in c:
            cred_badge = '<span class="text-xs px-1.5 py-0.5 rounded bg-rose-100 text-rose-700 font-mono" title="数据可信度">🔴</span>'

    verif_badge = ""
    if rec.get("verification"):
        v = rec["verification"]
        if "✅" in v or "已交叉" in v:
            verif_badge = '<span class="text-xs px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700" title="双源验证">✅ 双源</span>'
        elif "⚠️" in v or "单源" in v:
            verif_badge = '<span class="text-xs px-1.5 py-0.5 rounded bg-amber-50 text-amber-700" title="双源验证">⚠️ 单源</span>'
        elif "❓" in v or "未验证" in v:
            verif_badge = '<span class="text-xs px-1.5 py-0.5 rounded bg-slate-100 text-slate-500" title="双源验证">❓ 待补</span>'

    # 价格 + YTD% 显示块（红涨绿跌？这里用美股惯例：绿涨红跌）
    price_block = ""
    if rec.get("latest_price"):
        ytd = rec.get("ytd_pct")
        oy = rec.get("one_year_pct")
        pe = rec.get("forward_pe")

        def fmt_pct(p):
            if p is None or p == "":
                return ""
            try:
                p = float(p)
                color = "text-emerald-600" if p > 0 else "text-rose-600"
                sign = "+" if p > 0 else ""
                return f'<span class="{color} font-mono font-bold">{sign}{p:.1f}%</span>'
            except (ValueError, TypeError):
                return ""

        ytd_html = fmt_pct(ytd)
        oy_html = fmt_pct(oy)
        wk = rec.get("one_week_pct")
        mo = rec.get("one_month_pct")
        wk_html = fmt_pct(wk)
        mo_html = fmt_pct(mo)

        pe_str = f"{float(pe):.1f}" if pe and pe != "" else "-"
        try:
            pe_negative = pe is not None and pe != "" and float(pe) < 0
        except (ValueError, TypeError):
            pe_negative = False
        pe_class = "text-rose-500" if pe_negative else "text-slate-700"

        peg = rec.get("peg")
        peg_str = "-"
        peg_class = "text-slate-700"
        if peg and peg != "":
            try:
                peg_v = float(peg)
                peg_str = f"{peg_v:.2f}"
                if peg_v < 1:
                    peg_class = "text-emerald-600 font-bold"  # 便宜
                elif peg_v > 2:
                    peg_class = "text-rose-500"  # 偏贵
                else:
                    peg_class = "text-amber-600"
            except (ValueError, TypeError):
                pass

        price_block = f'''<div class="bg-slate-50 rounded-lg p-2 mb-2 border border-slate-100">
      <div class="flex items-baseline justify-between mb-1">
        <span class="text-lg font-bold text-slate-900 font-mono">{rec['latest_price']}</span>
        <div class="text-xs text-slate-500">
          PE: <span class="{pe_class} font-mono">{pe_str}</span>
          <span class="ml-1">PEG: <span class="{peg_class} font-mono">{peg_str}</span></span>
        </div>
      </div>
      <div class="grid grid-cols-4 gap-1 text-xs mt-1">
        <div class="text-slate-500"><div class="text-[10px]">1W</div>{wk_html or '-'}</div>
        <div class="text-slate-500"><div class="text-[10px]">1M</div>{mo_html or '-'}</div>
        <div class="text-slate-500"><div class="text-[10px]">YTD</div>{ytd_html or '-'}</div>
        <div class="text-slate-500"><div class="text-[10px]">1Y</div>{oy_html or '-'}</div>
      </div>
    </div>'''

    return f'''<div data-search="{search_text}" class="bg-white rounded-xl shadow-sm border border-slate-200 p-4 hover:shadow-lg transition">
  <div class="flex items-start justify-between mb-2">
    <div class="flex-1">
      <h4 class="text-base font-bold text-slate-900">{rec['name']}</h4>
      <div class="text-xs mt-0.5">{yahoo_link(rec['code'], rec['market'])} <span class="text-slate-500">· {rec['market']}</span></div>
    </div>
    <div class="flex flex-col items-end gap-1 flex-shrink-0">
      <span class="text-xs px-2 py-1 rounded {color_class}">{icon} {label}</span>
      <div class="flex gap-1">{cred_badge}{verif_badge}</div>
    </div>
  </div>
  <div class="text-xs text-slate-500 mb-2">{rec['industry']}</div>
  {price_block}

  <details class="mt-2">
    <summary class="text-xs font-semibold text-slate-700 hover:text-violet-700"><span class="arrow"></span>主营业务</summary>
    <p class="text-xs text-slate-600 mt-1 field-block pl-4">{safe(rec['business'])}</p>
  </details>

  <details class="mt-1">
    <summary class="text-xs font-semibold text-slate-700 hover:text-violet-700"><span class="arrow"></span>AI 关联逻辑</summary>
    <p class="text-xs text-slate-600 mt-1 field-block pl-4">{safe(rec['ai_logic'])}</p>
  </details>

  <details class="mt-1">
    <summary class="text-xs font-semibold text-slate-700 hover:text-violet-700"><span class="arrow"></span>最近季度业绩</summary>
    <p class="text-xs text-slate-600 mt-1 field-block pl-4">{safe(rec['earnings'])}</p>
  </details>

  <details class="mt-1">
    <summary class="text-xs font-semibold text-emerald-700 hover:text-emerald-900"><span class="arrow"></span>研究结论</summary>
    <p class="text-xs text-slate-700 mt-1 field-block pl-4 bg-emerald-50 p-2 rounded">{safe(rec['conclusion'])}</p>
  </details>

  <details class="mt-1">
    <summary class="text-xs font-semibold text-rose-700 hover:text-rose-900"><span class="arrow"></span>关键风险</summary>
    <p class="text-xs text-slate-700 mt-1 field-block pl-4 bg-rose-50 p-2 rounded">{safe(rec['risks'])}</p>
  </details>

  {info_breakdown_block}

  <div class="mt-3 pt-2 border-t border-slate-100 text-xs text-slate-500">
    <div>市值：{rec['market_cap']}</div>
    <div>跟踪：{rec['rhythm']} · 状态：{rec['status']}</div>
  </div>
</div>'''


def theme_section_html(theme, all_records):
    """每个主题展开 - 包含主题说明 + 该主题的股票卡片"""
    theme_records = [r for r in all_records if r["code"] in theme["tickers"]]
    if not theme_records:
        return ""

    cards = "\n".join(stock_card_html(r) for r in theme_records)

    judgment_color_map = {
        "amber": "bg-amber-100 text-amber-800",
        "red": "bg-red-100 text-red-800",
        "emerald": "bg-emerald-100 text-emerald-800",
        "violet": "bg-violet-100 text-violet-800",
        "blue": "bg-blue-100 text-blue-800",
        "indigo": "bg-indigo-100 text-indigo-800",
        "fuchsia": "bg-fuchsia-100 text-fuchsia-800",
        "pink": "bg-pink-100 text-pink-800",
        "slate": "bg-slate-100 text-slate-800",
        "stone": "bg-stone-100 text-stone-800",
    }
    badge_class = judgment_color_map.get(theme["judgment_color"], "bg-slate-100 text-slate-800")

    return f'''<details class="theme-{theme['judgment_color']} bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden" open>
  <summary class="px-6 py-4 hover:bg-slate-50 transition">
    <div class="flex items-start justify-between gap-4">
      <div class="flex-1">
        <div class="flex items-center gap-3 mb-1">
          <span class="text-2xl">{theme['icon']}</span>
          <h3 class="text-xl font-bold text-slate-900">{theme['title']}</h3>
          <span class="text-xs px-2 py-1 rounded {badge_class} font-semibold">{theme['judgment']}</span>
          <span class="text-xs text-slate-500">{len(theme_records)} 只</span>
        </div>
        <div class="text-sm text-slate-600 ml-9">{theme['subtitle']}</div>
      </div>
      <span class="arrow text-slate-400 mt-2"></span>
    </div>
  </summary>
  <div class="px-6 pb-6">
    <div class="bg-slate-50 rounded-lg p-3 mb-4 text-sm text-slate-700 leading-relaxed">
      <strong class="text-slate-900">📖 主题逻辑：</strong>{theme['logic']}
    </div>
    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
      {cards}
    </div>
  </div>
</details>'''


def load_calibration_snapshot():
    """读最新的因子权重校准（stock_research.jobs.calibrate_pick_weights 写出）。"""
    path = os.path.join(_REPO,
                        "data", "factor_weights.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _ic_badge(mean_ic):
    """根据 mean IC 返回带状态徽章的 HTML。Grinold-Kahn 阈值。"""
    if mean_ic is None:
        return '<span class="text-xs px-2 py-0.5 rounded bg-slate-200 text-slate-600 font-mono">无 IC 数据</span>'
    if mean_ic >= 0.05:
        cls, icon, label = "bg-emerald-100 text-emerald-800", "🟢", "实证有效"
    elif mean_ic >= 0.02:
        cls, icon, label = "bg-amber-100 text-amber-800", "🟡", "边际有效"
    elif mean_ic >= -0.02:
        cls, icon, label = "bg-rose-100 text-rose-800", "🔴", "失效"
    else:
        cls, icon, label = "bg-rose-200 text-rose-900", "⛔", "反向 alpha"
    return f'<span class="text-xs px-2 py-0.5 rounded {cls} font-mono">{icon} {label} IC={mean_ic:+.3f}</span>'


def _uncal_badge(reason_short):
    return (f'<span class="text-xs px-2 py-0.5 rounded bg-slate-200 text-slate-700 font-mono" '
            f'title="{reason_short}">⚪ 未实证</span>')


def scoring_rules_panel_html(calib):
    """渲染「自选股·今日 Top · 打分规则」面板。

    calib=None 时显示"未校准"警告 + 全部维度标⚪未实证
    calib 存在时按 factor_weights.json 内容动态展示每个因子的 IC 实证状态
    """
    if not calib:
        evidence_html = '''
      <div class="bg-amber-50 border-l-4 border-amber-400 p-4 rounded mb-4">
        <strong class="text-amber-900">⚠️ 当前所有打分规则均为手拍 heuristic（无 IC 实证）</strong>
        <p class="text-sm text-slate-700 mt-1">跑 <code class="bg-amber-100 px-1.5 py-0.5 rounded text-xs">python3 -m stock_research.jobs.calibrate_pick_weights</code> 生成实证证据</p>
      </div>'''
        ai_badge = _uncal_badge("人工分类标注，无历史时间序列可测")
        val_badge = _uncal_badge("PEG 历史快照需历史 EPS 预测")
        trend_badge = _uncal_badge("尚未运行 IC 校准")
        cred_badge = _uncal_badge("人工分类标注，无历史时间序列可测")
        trend_subblock = ""
    else:
        gen_at = calib.get("generated_at", "未知")
        sample = calib.get("sample", {})
        n_tickers = sample.get("n_tickers", 0)
        n_regimes = sample.get("n_regimes_with_data", 0)
        trend_audit = calib.get("calibrated", {}).get("trend", {}).get("ic_audit", {})
        composite_ic = trend_audit.get("trend_composite", {}).get("mean_ic")

        evidence_html = f'''
      <div class="bg-emerald-50 border-l-4 border-emerald-400 p-4 rounded mb-4 text-sm">
        <div class="flex items-center gap-2 flex-wrap">
          <strong class="text-emerald-900">📊 已加载 IC 实证</strong>
          <span class="text-xs text-slate-500">({n_tickers} 只样本 × {n_regimes} 个 regime · Spearman IC · Grinold-Kahn 2000)</span>
        </div>
        <div class="text-xs text-slate-600 mt-1">最近校准: <span class="font-mono">{gen_at}</span> · 重跑命令: <code class="bg-emerald-100 px-1.5 py-0.5 rounded text-xs">python3 -m stock_research.jobs.calibrate_pick_weights</code></div>
      </div>'''
        ai_badge = _uncal_badge("人工分类标注，无历史时间序列可测")
        val_badge = _uncal_badge("PEG 历史快照需历史 EPS 预测，yfinance 提供有限")
        trend_badge = _ic_badge(composite_ic)
        cred_badge = _uncal_badge("人工分类标注，无历史时间序列可测")

        # 趋势子因子 IC 明细表
        rows = []
        label_map = {
            "trend_composite": "复合分 (1Y 档位 + 追高扣分)",
            "trend_1y_raw": "1Y 线性涨幅",
            "trend_1w_raw": "1W 线性涨幅 (已删)",
        }
        for fname, summary in trend_audit.items():
            ic = summary.get("mean_ic")
            ir = summary.get("ic_ir", 0)
            label = label_map.get(fname, fname)
            mark = "🟢" if ic and ic >= 0.05 else ("🟡" if ic and ic >= 0.02 else "🔴")
            ic_str = f"{ic:+.3f}" if ic is not None else "  N/A"
            rows.append(
                f'<tr class="border-b border-cyan-100"><td class="py-1 pr-2 text-slate-700">{mark} {label}</td>'
                f'<td class="py-1 font-mono text-right">{ic_str}</td>'
                f'<td class="py-1 font-mono text-right text-slate-500">{ir:+.2f}</td></tr>'
            )
        trend_subblock = f'''
        <div class="mt-3 pt-3 border-t border-cyan-200 text-xs">
          <div class="font-bold text-cyan-800 mb-1">子因子 IC 实证 ({n_tickers} 只 × {n_regimes} regime):</div>
          <table class="w-full text-xs">
            <thead class="text-slate-500"><tr><th class="text-left font-normal">子因子</th><th class="text-right font-normal">mean IC</th><th class="text-right font-normal">IR</th></tr></thead>
            <tbody>{"".join(rows)}</tbody>
          </table>
          <div class="text-xs text-slate-500 mt-1">IC ≥ 0.05 = 有效；0.02-0.05 = 边际；&lt; 0.02 = 失效</div>
        </div>'''

    return f'''
<section id="scoring-rules" class="max-w-7xl mx-auto px-6 py-10">
  <details class="bg-white rounded-2xl shadow-sm border border-slate-200 overflow-hidden">
    <summary class="px-6 py-4 hover:bg-slate-50 cursor-pointer">
      <div class="flex items-center justify-between gap-3">
        <div>
          <h2 class="text-2xl font-bold text-slate-900 flex items-center gap-3">
            <span class="text-3xl">📐</span>
            自选股·今日 Top · 打分规则（透明 + IC 实证）
          </h2>
          <p class="text-sm text-slate-600 mt-1 ml-12">点击展开 — 看每个维度有没有数据实证支撑</p>
        </div>
        <span class="arrow text-slate-400"></span>
      </div>
    </summary>

    <div class="px-6 pb-6">
      {evidence_html}

      <div class="bg-slate-900 text-white rounded-xl p-5 mb-4 font-mono">
        <div class="text-xs text-slate-400 mb-2">综合公式（满分 100）</div>
        <div class="text-base md:text-lg">
          <span class="text-amber-300">综合得分</span> =
          <span class="text-rose-300">AI 关联度</span> (35) +
          <span class="text-emerald-300">估值</span> (25) +
          <span class="text-cyan-300">趋势</span> (25) +
          <span class="text-violet-300">数据可信度</span> (15)
        </div>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3 mb-6">

        <!-- AI 关联度 -->
        <div class="bg-rose-50 border-2 border-rose-200 rounded-xl p-4">
          <div class="flex items-center justify-between mb-2">
            <h3 class="font-bold text-rose-800">🎯 AI 关联度</h3>
            <span class="text-2xl font-mono font-bold text-rose-600">35</span>
          </div>
          <div class="mb-3">{ai_badge}</div>
          <div class="text-xs text-slate-700 space-y-1.5">
            <div class="flex justify-between border-b border-rose-100 pb-1"><span>极强（核心标的）</span><span class="font-mono font-bold">35</span></div>
            <div class="flex justify-between border-b border-rose-100 pb-1"><span>强（直接受益）</span><span class="font-mono font-bold">28</span></div>
            <div class="flex justify-between border-b border-rose-100 pb-1"><span>中（间接受益）</span><span class="font-mono">18</span></div>
            <div class="flex justify-between border-b border-rose-100 pb-1"><span>弱（沾边）</span><span class="font-mono">8</span></div>
            <div class="flex justify-between"><span>无</span><span class="font-mono">0</span></div>
          </div>
          <div class="text-xs text-rose-700 mt-3 pt-2 border-t border-rose-200">
            <strong>未实证</strong>：人工分类无历史标注；待 picks 表 ≥ 3 个月做 logit 校准
          </div>
        </div>

        <!-- 估值 -->
        <div class="bg-emerald-50 border-2 border-emerald-200 rounded-xl p-4">
          <div class="flex items-center justify-between mb-2">
            <h3 class="font-bold text-emerald-800">💰 估值（PEG/PE）</h3>
            <span class="text-2xl font-mono font-bold text-emerald-600">25</span>
          </div>
          <div class="mb-3">{val_badge}</div>
          <div class="text-xs text-slate-700 space-y-1.5">
            <div class="text-slate-500 mb-1 italic">优先看 PEG（PE÷增速）：</div>
            <div class="flex justify-between border-b border-emerald-100 pb-1"><span>PEG &lt; 1（便宜）</span><span class="font-mono font-bold">25</span></div>
            <div class="flex justify-between border-b border-emerald-100 pb-1"><span>PEG 1-2（合理）</span><span class="font-mono font-bold">18</span></div>
            <div class="flex justify-between border-b border-emerald-100 pb-1"><span>PEG 2-3（偏贵）</span><span class="font-mono">10</span></div>
            <div class="flex justify-between border-b border-emerald-100 pb-1"><span>PEG &gt; 3（贵）</span><span class="font-mono">4</span></div>
            <div class="flex justify-between"><span>PEG 缺失，PE &lt; 25</span><span class="font-mono">15</span></div>
          </div>
          <div class="text-xs text-emerald-700 mt-3 pt-2 border-t border-emerald-200">
            <strong>未实证</strong>：PEG 历史需 EPS 预测（yfinance 不稳定）；接 FMP/Finnhub 后做 IC 回测
          </div>
        </div>

        <!-- 趋势 -->
        <div class="bg-cyan-50 border-2 border-cyan-200 rounded-xl p-4">
          <div class="flex items-center justify-between mb-2">
            <h3 class="font-bold text-cyan-800">📈 趋势（1Y）</h3>
            <span class="text-2xl font-mono font-bold text-cyan-600">25</span>
          </div>
          <div class="mb-3">{trend_badge}</div>
          <div class="text-xs text-slate-700 space-y-1.5">
            <div class="text-slate-500 mb-1 italic">1 年涨幅档位：</div>
            <div class="flex justify-between border-b border-cyan-100 pb-1"><span>涨 50%-200%（健康）</span><span class="font-mono font-bold">20</span></div>
            <div class="flex justify-between border-b border-cyan-100 pb-1"><span>涨 0%-50%（稳健）</span><span class="font-mono">15</span></div>
            <div class="flex justify-between border-b border-cyan-100 pb-1"><span>涨 &gt; 200%（追高）</span><span class="font-mono">12</span></div>
            <div class="flex justify-between"><span>跌（逆势）</span><span class="font-mono">8</span></div>
          </div>
          <div class="text-xs text-cyan-700 mt-3 pt-2 border-t border-cyan-200">
            <strong>实证：「追高扣分」复合分 IC 优于线性 1Y</strong>，6 regime 已验证
          </div>
          {trend_subblock}
        </div>

        <!-- 数据可信度 -->
        <div class="bg-violet-50 border-2 border-violet-200 rounded-xl p-4">
          <div class="flex items-center justify-between mb-2">
            <h3 class="font-bold text-violet-800">🔍 数据可信度</h3>
            <span class="text-2xl font-mono font-bold text-violet-600">15</span>
          </div>
          <div class="mb-3">{cred_badge}</div>
          <div class="text-xs text-slate-700 space-y-1.5">
            <div class="flex justify-between border-b border-violet-100 pb-1"><span>🟢 高（官方+多源）</span><span class="font-mono font-bold">15</span></div>
            <div class="flex justify-between border-b border-violet-100 pb-1"><span>🟡 中（权威媒体单源）</span><span class="font-mono">10</span></div>
            <div class="flex justify-between border-b border-violet-100 pb-1"><span>🔴 低（二手/推断）</span><span class="font-mono">5</span></div>
            <div class="flex justify-between"><span>未填</span><span class="font-mono">3</span></div>
          </div>
          <div class="text-xs text-violet-700 mt-3 pt-2 border-t border-violet-200">
            <strong>未实证</strong>：人工分类无历史标注，无 IC 可测
          </div>
        </div>
      </div>

      <!-- 评级阈值 -->
      <div class="bg-gradient-to-r from-amber-50 to-orange-50 rounded-xl p-5 mb-4">
        <div class="flex items-center justify-between mb-3 flex-wrap gap-2">
          <h3 class="font-bold text-slate-900">📊 评级阈值</h3>
          <span class="text-xs px-2 py-0.5 rounded bg-slate-200 text-slate-700 font-mono">⚪ 未实证（待 picks ≥ 3 个月做 logit calibration）</span>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div class="bg-white rounded-lg p-3 border-l-4 border-amber-500">
            <div class="font-bold text-lg">⭐⭐⭐ 强烈推荐</div>
            <div class="text-sm text-slate-600 mt-1">综合得分 ≥ <strong class="text-amber-700">75</strong></div>
            <div class="text-xs text-slate-500 mt-2">4 维度都接近满分，确定性较高的标的</div>
          </div>
          <div class="bg-white rounded-lg p-3 border-l-4 border-orange-400">
            <div class="font-bold text-lg">⭐⭐ 推荐</div>
            <div class="text-sm text-slate-600 mt-1">综合得分 ≥ <strong class="text-orange-700">60</strong></div>
            <div class="text-xs text-slate-500 mt-2">某个维度优秀但不是全维度都好</div>
          </div>
          <div class="bg-white rounded-lg p-3 border-l-4 border-yellow-400">
            <div class="font-bold text-lg">⭐ 关注</div>
            <div class="text-sm text-slate-600 mt-1">综合得分 ≥ <strong class="text-yellow-700">50</strong></div>
            <div class="text-xs text-slate-500 mt-2">有亮点但风险较大，谨慎跟踪</div>
          </div>
        </div>
      </div>

      <!-- 命中评级（持有后判断）-->
      <div class="bg-gradient-to-r from-slate-100 to-blue-50 rounded-xl p-5 mb-4">
        <h3 class="font-bold text-slate-900 mb-2">🎯 命中评级（入选后实际表现）</h3>
        <p class="text-xs text-slate-600 mb-3">入选后我会持续跟踪，按累计涨跌幅自动评级，验证选股策略是否有效</p>
        <div class="grid grid-cols-2 md:grid-cols-5 gap-2 text-xs">
          <div class="bg-emerald-100 text-emerald-800 rounded p-2 text-center"><div class="font-bold">🚀 大涨</div><div class="font-mono">&gt; +15%</div></div>
          <div class="bg-emerald-50 text-emerald-700 rounded p-2 text-center"><div class="font-bold">✅ 命中</div><div class="font-mono">+5% ~ +15%</div></div>
          <div class="bg-slate-100 text-slate-700 rounded p-2 text-center"><div class="font-bold">🟢 跟随</div><div class="font-mono">-5% ~ +5%</div></div>
          <div class="bg-amber-50 text-amber-700 rounded p-2 text-center"><div class="font-bold">⚠️ 不及</div><div class="font-mono">-5% ~ -15%</div></div>
          <div class="bg-rose-100 text-rose-800 rounded p-2 text-center"><div class="font-bold">❌ 大跌</div><div class="font-mono">&lt; -15%</div></div>
        </div>
      </div>

      <!-- 限制 + 说明 -->
      <div class="bg-rose-50 border-l-4 border-rose-400 p-4 rounded">
        <h3 class="font-bold text-rose-900 mb-2">⚠️ 这套打分系统的限制</h3>
        <ul class="text-sm text-slate-700 space-y-1 list-disc pl-5">
          <li><strong>是定量框架，不是买卖建议</strong>：满分 100 不代表「一定会涨」，0 分不代表「一定会跌」</li>
          <li><strong>4 维度中 3 维（AI/估值/可信度）权重未经 IC 实证</strong>：等 picks 表 ≥ 3 个月可做 logit 校准</li>
          <li><strong>不考虑宏观/政策风险</strong>：地缘冲突、关税、监管这些黑天鹅打分没法量化</li>
          <li><strong>2018 类熊市趋势 IC 反转</strong>：所有趋势因子在系统性下跌中变负 alpha，依赖 v7 防御信号</li>
          <li><strong>只用 自选股内 37 只</strong>：不是从全市场万只里挑，覆盖范围有限</li>
        </ul>
      </div>
    </div>
  </details>
</section>'''


def _find_plan_inception_date() -> str | None:
    """DuckDB 里最早的 v6 plan snapshot 日期（YYYY-MM-DD）—— 即 v6 方向定下来那天。"""
    db_path = os.path.join(_REPO, "stock_history.duckdb")
    if os.path.exists(db_path):
        try:
            import duckdb
            con = duckdb.connect(db_path, read_only=True)
            row = con.execute(
                "SELECT MIN(taken_at) FROM snapshots "
                "WHERE category='optimize' AND name='plan_v6'"
            ).fetchone()
            con.close()
            if row and row[0]:
                return str(row[0])[:10]
        except Exception:
            pass
    try:
        with open(os.path.join(_REPO, "data", "latest", "plan_a_v5.json")) as f:
            return (json.load(f).get("generated_at") or "")[:10] or None
    except Exception:
        return None


def _load_inception_plan_from_duckdb() -> dict | None:
    """读 DuckDB plan_v6 最早一条 payload —— inception 那天的原始 plan。

    用来给 compute_plan_forward_track() 提供"冻结在锁定日"的 tickers，
    避免 look-ahead bias：之前用今天最新 plan_a_v5.json 的 tickers + 5-08 锚定，
    会把后换入的股票回填到锁定日轨迹里（不可实操、用了未来信息）。
    """
    db_path = os.path.join(_REPO, "stock_history.duckdb")
    if not os.path.exists(db_path):
        return None
    try:
        import duckdb
        con = duckdb.connect(db_path, read_only=True)
        row = con.execute(
            "SELECT payload FROM snapshots "
            "WHERE category='optimize' AND name='plan_v6' "
            "ORDER BY taken_at ASC LIMIT 1"
        ).fetchone()
        con.close()
        if row and row[0]:
            return json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except Exception:
        pass
    return None


def _load_plan_v6_at_or_before(date_str: str) -> dict | None:
    """读 DuckDB plan_v6 在 date_str 当天（含）及之前的最新一条 payload。

    用于 P1 动态 rebalance：每周一找"截至本周一最新的推荐方案"调仓。
    若当天没有快照（系统未跑日 / 节假日），自然 fallback 到上一次落库的方案。
    """
    db_path = os.path.join(_REPO, "stock_history.duckdb")
    if not os.path.exists(db_path):
        return None
    try:
        import duckdb
        con = duckdb.connect(db_path, read_only=True)
        row = con.execute(
            "SELECT payload FROM snapshots "
            "WHERE category='optimize' AND name='plan_v6' "
            "AND taken_at <= CAST(? AS TIMESTAMP) + INTERVAL 1 DAY - INTERVAL 1 SECOND "
            "ORDER BY taken_at DESC LIMIT 1",
            [date_str]
        ).fetchone()
        con.close()
        if row and row[0]:
            return json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except Exception:
        pass
    return None


def _extract_plan_weights(plan: dict) -> dict:
    """从 plan payload 抽出 {ticker: normalized_weight}，处理 v5/v6/plan 三种字段名 + 各种 weight 字段。"""
    if not plan or not isinstance(plan, dict):
        return {}
    plan_list = plan.get("plan_v5") or plan.get("plan_v6") or plan.get("plan") or []
    raw = []
    for p in plan_list:
        t = p.get("ticker")
        w = (p.get("v5_weight") or p.get("v6_weight") or p.get("weight")
             or p.get("target_weight") or p.get("capped_weight") or 0.0)
        if t and w and float(w) > 0:
            raw.append((t, float(w)))
    if not raw:
        return {}
    total = sum(w for _, w in raw) or 1.0
    return {t: w / total for t, w in raw}


def compute_dynamic_rebalance_track(history: dict, benchmark: str = "SPY",
                                     commission_per_movement: float = 0.0005,
                                     rebalance_dow: int = 0) -> dict:
    """P1 + P2 动态 rebalance NAV 跟踪（C 类设计）。

    用户实际用法：每周一根据系统最新推荐换仓。这条曲线反映"持续按 AI 推荐调仓"
    的真实回报，与 compute_plan_forward_track() 的"buy-and-hold 死守"对照看，
    差距 = AI 持续动态选股带来的真实价值（P2 双曲线对比的核心）。

    参数：
      • commission_per_movement: 每"流动 1 元"的手续费成本（默认 5 bps）。
        一次"卖 ¥1000 of A + 买 ¥1000 of B"的 swap，total_movement = ¥2000，
        cost = ¥2000 × 0.0005 = ¥1 = 0.10% 完整 round-trip。涵盖 spread + 印花税
        粗略估算（A 股稍高，美股稍低，取中间值）。
      • rebalance_dow: 每周哪一天 rebalance（0=Monday）；周一节假日时自动顺延到本周
        第一个交易日。
    """
    if not history or "tickers" not in history:
        return {}

    inception_date = _find_plan_inception_date()
    if not inception_date:
        return {}

    inception_plan = _load_inception_plan_from_duckdb()
    if not inception_plan:
        return {}

    initial_weights = _extract_plan_weights(inception_plan)
    if not initial_weights:
        return {}

    tickers_data = history["tickers"]

    # 构造 ticker → {date_str: close} 加速查询
    ticker_close: dict[str, dict[str, float]] = {}
    for tkr, d in tickers_data.items():
        if d and d.get("ts") and d.get("close"):
            ticker_close[tkr] = dict(zip(d["ts"], [float(c) for c in d["close"]]))

    # common_dates：所有 initial_tickers 都有价格的日期交集
    common: set | None = None
    for tkr in initial_weights:
        if tkr not in ticker_close:
            continue
        ts_set = set(ticker_close[tkr].keys())
        common = ts_set if common is None else (common & ts_set)
    if not common:
        return {"inception_date": inception_date, "error": "no common dates in initial plan"}

    common_dates = sorted(common)

    # baseline_idx：最后一个 ≤ inception_date 的交易日
    baseline_idx = None
    for i, d in enumerate(common_dates):
        if d <= inception_date:
            baseline_idx = i
        else:
            break
    if baseline_idx is None:
        baseline_idx = 0
    baseline_date = common_dates[baseline_idx]

    # 在 baseline 那天按 inception plan 建仓
    holdings: dict[str, float] = {}
    for t, w in initial_weights.items():
        p0 = ticker_close.get(t, {}).get(baseline_date)
        if p0 and p0 > 0:
            holdings[t] = w / p0  # NAV=1.0 → shares = w / price

    # 用 context_days 之前作为图表上下文（虚线），跟 static 函数对齐
    context_days = 30
    win_start = max(0, baseline_idx - context_days)
    win_dates = common_dates[win_start:]
    rel_baseline = baseline_idx - win_start

    from datetime import datetime as _dt

    def _iso_week(date_str: str) -> tuple[int, int]:
        """返回 (iso_year, iso_week)，跨年周一也算同一周。"""
        y, m, d = date_str.split("-")
        return _dt(int(y), int(m), int(d)).isocalendar()[:2]

    # rebalance 触发：今天的 iso-week 跟上一个交易日不同（即跨周了）
    # 等价于"本周的第一个交易日就 rebalance"，自然处理周一节假日顺延
    nav_list: list[float | None] = []
    rebalance_log: list[dict] = []
    rebalance_dates_used: list[str] = []
    total_commission = 0.0
    prev_iso_week: tuple[int, int] | None = None

    # 先在 win_start..baseline 段填 None（context 段，不算 NAV）
    for _ in range(rel_baseline):
        nav_list.append(None)

    # 从 baseline 开始算
    for i in range(baseline_idx, len(common_dates)):
        date = common_dates[i]
        cur_iso = _iso_week(date)

        # 当前 NAV（rebalance 前）
        current_value = 0.0
        for t, shares in holdings.items():
            p = ticker_close.get(t, {}).get(date)
            if p is not None:
                current_value += shares * p

        # rebalance：跨周（prev_iso_week 存在且不同）且今天 != baseline 那天
        if prev_iso_week is not None and cur_iso != prev_iso_week and date != baseline_date:
            new_plan = _load_plan_v6_at_or_before(date)
            new_weights = _extract_plan_weights(new_plan) if new_plan else {}
            # 过滤掉今天没有价格数据的 tickers
            available_w = {t: w for t, w in new_weights.items()
                           if t in ticker_close and date in ticker_close[t]}
            wsum = sum(available_w.values())
            if available_w and wsum > 0 and current_value > 0:
                available_w = {t: w / wsum for t, w in available_w.items()}
                new_alloc = {t: w * current_value for t, w in available_w.items()}
                old_alloc = {t: holdings.get(t, 0) * ticker_close.get(t, {}).get(date, 0)
                             for t in (set(holdings.keys()) | set(available_w.keys()))}
                all_t = set(new_alloc.keys()) | set(old_alloc.keys())
                total_movement = sum(abs(new_alloc.get(t, 0) - old_alloc.get(t, 0)) for t in all_t)
                cost = total_movement * commission_per_movement
                post_value = current_value - cost
                total_commission += cost

                new_holdings: dict[str, float] = {}
                for t, w in available_w.items():
                    p_t = ticker_close[t][date]
                    new_holdings[t] = w * post_value / p_t

                rebalance_log.append({
                    "date": date,
                    "pre_nav": round(current_value, 6),
                    "post_nav": round(post_value, 6),
                    "turnover_dollar": round(total_movement, 6),
                    "commission_dollar": round(cost, 6),
                    "n_tickers": len(available_w),
                    "tickers_added": sorted(set(available_w.keys()) - set(holdings.keys())),
                    "tickers_removed": sorted(set(holdings.keys()) - set(available_w.keys())),
                })
                holdings = new_holdings
                current_value = post_value
                rebalance_dates_used.append(date)

        nav_list.append(current_value)
        prev_iso_week = cur_iso

    # 基准 SPY（同样锚到 baseline）
    bench_nav: list[float | None] = []
    bench_d = ticker_close.get(benchmark, {})
    b_anchor = bench_d.get(baseline_date) if bench_d else None
    if bench_d and b_anchor:
        last_b = 1.0
        for d in win_dates:
            v = bench_d.get(d)
            if v and b_anchor:
                last_b = v / b_anchor
            bench_nav.append(last_b)

    # tracked 段（baseline 之后）算指标
    tracked = [v for v in nav_list[rel_baseline:] if v is not None]
    tracked_dates = win_dates[rel_baseline:rel_baseline + len(tracked)]
    bench_tracked = bench_nav[rel_baseline:rel_baseline + len(tracked)] if bench_nav else []

    import math
    def _daily_rets(curve: list[float]) -> list[float]:
        return [0.0] + [
            (curve[i] / curve[i - 1] - 1.0) if curve[i - 1] else 0.0
            for i in range(1, len(curve))
        ]

    def _max_dd(curve: list[float]) -> float:
        if not curve: return 0.0
        peak = curve[0]; mdd = 0.0
        for v in curve:
            if v > peak: peak = v
            dd = (v - peak) / peak if peak else 0.0
            if dd < mdd: mdd = dd
        return mdd

    n_tracked = max(0, len(tracked) - 1)
    daily_rets = _daily_rets(tracked) if tracked else []
    cumret = (tracked[-1] - 1.0) if len(tracked) >= 1 else 0.0
    bench_cumret = (bench_tracked[-1] - 1.0) if len(bench_tracked) >= 1 else 0.0
    if n_tracked > 0:
        r_avg = sum(daily_rets[1:]) / n_tracked
        r_var = sum((r - r_avg) ** 2 for r in daily_rets[1:]) / max(1, n_tracked - 1) if n_tracked >= 2 else 0
        r_std = math.sqrt(r_var)
        annvol = r_std * math.sqrt(252)
        sharpe = (r_avg * 252) / annvol if annvol > 1e-9 else 0.0
        win_days = sum(1 for r in daily_rets[1:] if r > 0)
        win_rate = win_days / n_tracked
        annret = (1 + cumret) ** (252 / n_tracked) - 1.0 if n_tracked > 0 else 0.0
    else:
        annvol = sharpe = win_rate = annret = 0.0
        win_days = 0

    universe = set(initial_weights.keys())
    for r in rebalance_log:
        universe.update(r["tickers_added"])

    return {
        "dates": win_dates,
        "nav": [round(v, 6) if v is not None else None for v in nav_list],
        "bench_nav": [round(v, 6) for v in bench_nav] if bench_nav else [],
        "benchmark": benchmark if bench_nav else None,
        "inception_date": inception_date,
        "baseline_date": baseline_date,
        "baseline_idx_in_window": rel_baseline,
        "rebalance_dates": rebalance_dates_used,
        "rebalance_log": rebalance_log,
        "total_commission": round(total_commission, 6),
        "total_commission_pct": round(total_commission * 100, 4),
        "commission_per_movement_bps": round(commission_per_movement * 10000, 1),
        "rebalance_freq": "weekly_monday",
        "tickers_universe": sorted(universe),
        "metrics": {
            "n_tracked_days": n_tracked,
            "tracked_start": tracked_dates[1] if len(tracked_dates) > 1 else None,
            "tracked_end": tracked_dates[-1] if tracked_dates else None,
            "cumulative_return_pct": round(cumret * 100, 2),
            "bench_cumulative_return_pct": round(bench_cumret * 100, 2),
            "alpha_pct": round((cumret - bench_cumret) * 100, 2),
            "annual_return_pct": round(annret * 100, 2),
            "annual_vol_pct": round(annvol * 100, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown_pct": round(_max_dd(tracked) * 100, 2),
            "win_rate_pct": round(win_rate * 100, 1),
            "win_days": win_days,
            "total_days": n_tracked,
            "n_rebalances": len(rebalance_log),
        },
    }


def compute_plan_forward_track(plan: dict, history: dict, benchmark: str = "SPY",
                                context_days: int = 30) -> dict:
    """从 plan 锁定日往后跟踪真实表现（forward 视角）。

    思路：
      • inception_date = v6 plan 第一次落库的日期（方向定下来那天）
      • baseline_date  = history 中 ≤ inception_date 的最后一个交易日 → NAV=100 锚点
      • context window = baseline_date 之前 N 个交易日（仅作视觉上下文，灰色虚线）
      • tracked window = baseline_date 之后的所有交易日（真实实盘营收）

    指标只在 tracked 段算；context 仅供图表参考。
    刚锁定时 tracked = 0 天，曲线只有 context + 锁定基线点；每日 daily_refresh 自动累加。
    """
    if not plan or not isinstance(plan, dict):
        return {}
    if not history or "tickers" not in history:
        return {}

    # P0 修复 look-ahead bias（2026-05-10）：tickers 从 DuckDB inception 那天的 plan 读，
    # 不再用传入的"今天最新 plan_a_v5.json"——避免把后换入的股票回填到锁定日轨迹。
    inception_plan = _load_inception_plan_from_duckdb()
    if inception_plan:
        src_plan = inception_plan
        tickers_source = "duckdb_inception"
    else:
        # fallback：DuckDB 还没数据时用传入的 plan（首次部署兜底）
        src_plan = plan
        tickers_source = "latest_plan_a_v5_fallback"

    plan_list = src_plan.get("plan_v5") or src_plan.get("plan_v6") or src_plan.get("plan") or []
    if not plan_list:
        return {}

    inception_date = _find_plan_inception_date()
    tickers_data = history["tickers"]

    raw = [(p.get("ticker"), float(p.get("v5_weight") or p.get("weight") or p.get("target_weight") or 0.0))
           for p in plan_list if p.get("ticker")]
    total_w = sum(w for _, w in raw) or 1.0
    weights = [(t, w / total_w) for t, w in raw if w > 0]

    used: list[tuple[str, float, list[str], list[float]]] = []
    missing = []
    for tkr, w in weights:
        d = tickers_data.get(tkr)
        if not d or not d.get("ts") or not d.get("close"):
            missing.append(tkr)
            continue
        used.append((tkr, w, d["ts"], [float(c) for c in d["close"]]))

    if not used:
        return {"tickers_missing": missing, "tickers_used": [], "inception_date": inception_date}

    common = set(used[0][2])
    for _, _, ts, _ in used[1:]:
        common &= set(ts)
    common_dates = sorted(common)
    if not common_dates:
        return {"tickers_missing": missing, "tickers_used": [t for t, *_ in used], "inception_date": inception_date}

    aligned = []
    for tkr, w, ts, close in used:
        ts_to_close = dict(zip(ts, close))
        aligned_close = [ts_to_close[d] for d in common_dates]
        aligned.append((tkr, w, aligned_close))

    # 找 baseline_date：最后一个 <= inception 的交易日
    baseline_idx = None
    if inception_date:
        for i, d in enumerate(common_dates):
            if d <= inception_date:
                baseline_idx = i
            else:
                break
    if baseline_idx is None:
        # plan 锁定在 history 最早日期之前 → 用最早日期
        baseline_idx = 0

    # window：[baseline-context_days, end]
    win_start = max(0, baseline_idx - context_days)
    win_dates = common_dates[win_start:]
    rel_baseline = baseline_idx - win_start  # 在 win_dates 中的位置

    # NAV：以 baseline 那天的收盘为锚点（NAV[baseline]=1.0）
    nav = []
    for i in range(win_start, len(common_dates)):
        v = sum(w * (closes[i] / closes[baseline_idx]) for _, w, closes in aligned)
        nav.append(v)

    # 基准 SPY，同样锚到 baseline 日期
    bench_nav = []
    bench_d = tickers_data.get(benchmark)
    if bench_d and bench_d.get("ts") and bench_d.get("close"):
        bts_to_close = dict(zip(bench_d["ts"], [float(c) for c in bench_d["close"]]))
        baseline_date = common_dates[baseline_idx]
        b_anchor = bts_to_close.get(baseline_date)
        if b_anchor:
            for d in win_dates:
                if d in bts_to_close:
                    bench_nav.append(bts_to_close[d] / b_anchor)
                else:
                    bench_nav.append(None)
            # 把 None 用前值填充（防止图断线）
            last = 1.0
            bench_nav = [(last := v) if v is not None else last for v in bench_nav]

    # 仅 tracked 段（baseline 之后）算指标
    tracked_nav = nav[rel_baseline:]
    tracked_bench = bench_nav[rel_baseline:] if bench_nav else []
    tracked_dates = win_dates[rel_baseline:]

    def _daily_rets(curve):
        return [0.0] + [
            (curve[i] / curve[i - 1] - 1.0) if curve[i - 1] else 0.0
            for i in range(1, len(curve))
        ]

    daily_rets_full = _daily_rets(nav)
    daily_rets_tracked = _daily_rets(tracked_nav)

    def _max_dd(curve):
        if not curve: return 0.0
        peak = curve[0]; mdd = 0.0
        for v in curve:
            if v > peak: peak = v
            dd = (v - peak) / peak if peak else 0.0
            if dd < mdd: mdd = dd
        return mdd

    n_tracked = max(0, len(tracked_dates) - 1)  # 锁定日不算 1 天，从锁定日次日起算
    days_per_year = 252
    cumret = (tracked_nav[-1] - 1.0) if len(tracked_nav) > 1 else 0.0
    bench_cumret = (tracked_bench[-1] - 1.0) if len(tracked_bench) > 1 else 0.0
    import math
    if n_tracked > 0:
        r_avg = sum(daily_rets_tracked[1:]) / n_tracked
        r_var = sum((r - r_avg) ** 2 for r in daily_rets_tracked[1:]) / max(1, n_tracked - 1) if n_tracked >= 2 else 0
        r_std = math.sqrt(r_var)
        annvol = r_std * math.sqrt(days_per_year)
        sharpe = (r_avg * days_per_year) / annvol if annvol > 1e-9 else 0.0
        win_days = sum(1 for r in daily_rets_tracked[1:] if r > 0)
        win_rate = win_days / n_tracked
        annret = (1 + cumret) ** (days_per_year / n_tracked) - 1.0 if n_tracked > 0 else 0.0
    else:
        annvol = sharpe = win_rate = annret = 0.0
        win_days = 0

    # 单股贡献：用 baseline → 最新收盘
    last_idx = len(common_dates) - 1

    # 从最新 audit 快照取 ticker → 中文公司名（78 只 watchlist 全覆盖）
    name_map: dict = {}
    try:
        audit_dir = os.path.join(_REPO,
                                 "data", "snapshots", "audit")
        if os.path.isdir(audit_dir):
            audit_files = sorted([f for f in os.listdir(audit_dir)
                                   if f.startswith("audit_") and f.endswith(".json")],
                                  reverse=True)
            if audit_files:
                with open(os.path.join(audit_dir, audit_files[0]), encoding="utf-8") as af:
                    audit = json.load(af)
                    if isinstance(audit, list):
                        name_map = {r.get("ticker"): r.get("name") for r in audit
                                    if r.get("ticker") and r.get("name")}
    except Exception:
        pass

    per_ticker = []
    for tkr, w, closes in aligned:
        first, last = closes[baseline_idx], closes[last_idx]
        tret = (last / first - 1.0) if first else 0.0
        per_ticker.append({
            "ticker": tkr,
            "name": name_map.get(tkr, ""),
            "weight": round(w, 4),
            "close_first": round(first, 2),
            "close_last": round(last, 2),
            "return_pct": round(tret * 100, 2),
            "contribution_pct": round(w * tret * 100, 2),
        })
    per_ticker.sort(key=lambda x: -x["contribution_pct"])

    return {
        "inception_date": inception_date,
        "baseline_date": common_dates[baseline_idx],
        "baseline_idx_in_window": rel_baseline,
        "dates": win_dates,
        "nav": [round(v, 6) for v in nav],
        "bench_nav": [round(v, 6) for v in bench_nav] if bench_nav else [],
        "daily_returns": [round(r * 100, 4) for r in daily_rets_full],
        "benchmark": benchmark if bench_nav else None,
        "metrics": {
            "n_tracked_days": n_tracked,
            "tracked_start": tracked_dates[0] if tracked_dates else None,
            "tracked_end": tracked_dates[-1] if tracked_dates else None,
            "cumulative_return_pct": round(cumret * 100, 2),
            "bench_cumulative_return_pct": round(bench_cumret * 100, 2),
            "alpha_pct": round((cumret - bench_cumret) * 100, 2),
            "annual_return_pct": round(annret * 100, 2),
            "annual_vol_pct": round(annvol * 100, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown_pct": round(_max_dd(tracked_nav) * 100, 2),
            "win_rate_pct": round(win_rate * 100, 1),
            "win_days": win_days,
            "total_days": n_tracked,
        },
        "per_ticker": per_ticker,
        "tickers_used": [t for t, *_ in aligned],
        "tickers_missing": missing,
        "tickers_source": tickers_source,  # P0：标识 tickers 来源（duckdb_inception 或 latest_plan_a_v5_fallback）
    }


# 兼容旧调用名（一段时间后清理）
compute_plan_backtest = compute_plan_forward_track


def load_audit_snapshot():
    """读最新一次 picks 反向审查快照（JSON 文件路径）。"""
    audit_dir = os.path.join(_REPO,
                             "data", "snapshots", "audit")
    if not os.path.isdir(audit_dir):
        return None
    files = sorted([f for f in os.listdir(audit_dir) if f.startswith("picks_audit_")],
                   reverse=True)
    if not files:
        return None
    try:
        with open(os.path.join(audit_dir, files[0]), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_audit_snapshot_from_db():
    """读最新一次 picks 反向审查快照（DuckDB snapshots 表）。"""
    db_path = os.path.join(_REPO,
                           "stock_history.duckdb")
    if not os.path.exists(db_path):
        return None
    try:
        import duckdb
    except ImportError:
        return None
    try:
        con = duckdb.connect(db_path, read_only=True)
        row = con.execute(
            "SELECT payload FROM snapshots "
            "WHERE category=? AND name=? "
            "ORDER BY taken_at DESC LIMIT 1",
            ["audit", "picks_audit"],
        ).fetchone()
        con.close()
        if not row:
            return None
        payload = row[0]
        return json.loads(payload) if isinstance(payload, str) else payload
    except Exception as e:
        print(f"  ⚠️  从 DuckDB 读 picks_audit 失败: {e}")
        return None


def audit_panel_html(snap):
    """渲染 picks 反向审查面板（Risk Parity + 估值 + 13F + Markowitz）。"""
    if not snap:
        return """
<section id="audit-panel" class="max-w-7xl mx-auto px-6 py-10 bg-gradient-to-br from-slate-50 to-slate-100 rounded-2xl my-6">
  <div class="flex items-center gap-3 mb-2">
    <span class="text-3xl">🛡</span>
    <h2 class="text-2xl font-bold text-slate-900">反向审查 · 自我校验</h2>
  </div>
  <p class="text-slate-600">尚无审查快照。先跑：<code class="bg-slate-200 px-2 py-0.5 rounded">python3 -m stock_research.jobs.audit_picks --fast</code></p>
</section>
"""

    # 总评
    issues = []
    tc = snap.get("theme_concentration", {})
    if tc.get("level") == "严重":
        issues.append(f"主题严重失衡（{tc.get('top_theme')} 占 {tc.get('top_pct', 0):.0f}%）")
    if snap.get("valuation_sanity", {}).get("warn_count", 0) > 0:
        issues.append(f"{snap['valuation_sanity']['warn_count']} 只估值警告")
    if snap.get("thirteen_f_consistency", {}).get("warn_count", 0) > 0:
        issues.append(f"{snap['thirteen_f_consistency']['warn_count']} 只与 13F 矛盾")

    overall_color = "emerald" if not issues else ("amber" if len(issues) <= 2 else "rose")
    overall_text = "🟢 通过六项审查，无重大问题" if not issues else f"⚠️ 发现 {len(issues)} 项问题"

    # 1. 主题集中度（条形图）
    tc_html = ""
    if tc.get("status") == "ok":
        bars = []
        for d in tc.get("distribution", []):
            pct = d.get("pct", 0)
            bars.append(f'''
                <div class="flex items-center gap-2 text-xs mb-1">
                  <span class="w-32 truncate">{d.get("theme", "")}</span>
                  <span class="w-10 text-right text-slate-500">{d.get("n", 0)} 只</span>
                  <div class="flex-1 bg-slate-100 rounded h-3 relative overflow-hidden">
                    <div class="absolute left-0 top-0 h-full bg-violet-400" style="width:{pct}%"></div>
                  </div>
                  <span class="w-12 text-right font-mono">{pct:.1f}%</span>
                </div>''')
        tc_html = f'''
        <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
          <h3 class="text-sm font-semibold text-slate-700 mb-1">🗂 主题集中度（Risk Parity）</h3>
          <p class="text-xs mb-2">{tc.get("verdict", "")}</p>
          <div>{"".join(bars)}</div>
        </div>'''
    else:
        tc_html = f'''
        <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
          <h3 class="text-sm font-semibold text-slate-700 mb-1">🗂 主题集中度</h3>
          <p class="text-xs text-slate-500">跳过：{tc.get("reason", "")}</p>
        </div>'''

    # 2. 估值警告
    vs = snap.get("valuation_sanity", {})
    if vs.get("warn_count", 0) == 0:
        vs_html = '''
        <div class="bg-white rounded-xl shadow-sm border border-emerald-200 p-4">
          <h3 class="text-sm font-semibold text-emerald-700 mb-1">💰 估值合理性</h3>
          <p class="text-xs text-emerald-600">🟢 当日 ⭐⭐⭐ 推荐估值均在合理范围</p>
        </div>'''
    else:
        items = []
        for w in vs.get("warnings", []):
            flags = " / ".join(w.get("flags", []))
            items.append(f'<li class="text-xs"><span class="font-semibold">{w.get("name")}</span> ({w.get("code")}): <span class="text-rose-600">{flags}</span></li>')
        vs_html = f'''
        <div class="bg-white rounded-xl shadow-sm border border-amber-200 p-4">
          <h3 class="text-sm font-semibold text-amber-700 mb-1">💰 估值合理性</h3>
          <p class="text-xs mb-2">⚠️ {vs["warn_count"]} 只 ⭐⭐⭐ 推荐有估值警告</p>
          <ul class="space-y-1 list-disc list-inside">{"".join(items)}</ul>
        </div>'''

    # 3. 13F 一致性
    tf = snap.get("thirteen_f_consistency", {})
    if tf.get("status") == "ok" and tf.get("items"):
        items = []
        for it in tf["items"]:
            items.append(f'<li class="text-xs">{it["verdict"]} <span class="font-semibold">{it["name"]}</span> ({it["code"]})</li>')
        tf_html = f'''
        <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
          <h3 class="text-sm font-semibold text-slate-700 mb-1">📋 13F 一致性</h3>
          <p class="text-xs mb-2">{tf.get("total", 0)} 只 ⭐⭐⭐ 推荐有 13F 信号，矛盾 {tf.get("warn_count", 0)} 只</p>
          <ul class="space-y-1 list-disc list-inside">{"".join(items)}</ul>
        </div>'''
    else:
        tf_html = '''
        <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
          <h3 class="text-sm font-semibold text-slate-700 mb-1">📋 13F 一致性</h3>
          <p class="text-xs text-slate-500">跳过：当日推荐无 13F 信号匹配</p>
        </div>'''

    # 4. 相关性矩阵
    cr = snap.get("correlation", {})
    if cr.get("status") == "ok":
        pairs = cr.get("high_corr_pairs", [])
        items = [f'<li class="text-xs">{p.get("name_a")} ↔ {p.get("name_b")}: <span class="font-mono">r={p.get("r"):.2f}</span></li>' for p in pairs[:8]]
        body = "".join(items) if items else f'<p class="text-xs text-emerald-600">🟢 无相关 > {cr.get("threshold", 0.85)} 的"伪分散"对</p>'
        cr_html = f'''
        <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
          <h3 class="text-sm font-semibold text-slate-700 mb-1">📐 相关性矩阵（Markowitz）</h3>
          <p class="text-xs mb-2">分析 {cr.get("n_tickers", 0)} 只 ⭐⭐⭐ · 阈值 r &gt; {cr.get("threshold", 0.85)}</p>
          {f'<ul class="space-y-1 list-disc list-inside">{body}</ul>' if items else body}
        </div>'''
    else:
        cr_html = f'''
        <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
          <h3 class="text-sm font-semibold text-slate-700 mb-1">📐 相关性矩阵</h3>
          <p class="text-xs text-slate-500">跳过：{cr.get("reason", "")}</p>
        </div>'''

    return f'''
<!-- ============ 反向审查 · 自我校验 ============ -->
<section id="audit-panel" class="max-w-7xl mx-auto px-6 py-10 bg-gradient-to-br from-violet-50 to-fuchsia-50 rounded-2xl my-6">
  <div class="flex items-center justify-between mb-4">
    <div>
      <div class="flex items-center gap-3 mb-1">
        <span class="text-3xl">🛡</span>
        <h2 class="text-2xl font-bold text-slate-900">反向审查 · 自我校验</h2>
      </div>
      <p class="text-slate-700">用经典金融理论（Risk Parity / Markowitz / 13F / 估值）每日审查 ⭐⭐⭐ 推荐</p>
    </div>
    <div class="text-right">
      <div class="inline-block px-4 py-2 rounded-lg bg-{overall_color}-100 text-{overall_color}-800 font-semibold text-sm">{overall_text}</div>
      <p class="text-xs text-slate-500 mt-1">快照：{snap.get("ts", "?")} · {snap.get("picks_today_count", 0)} 只 picks</p>
    </div>
  </div>

  <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
    {tc_html}
    {vs_html}
    {tf_html}
    {cr_html}
  </div>

  <p class="text-xs text-slate-500 mt-4">
    数据来源：<code>data/snapshots/audit/picks_audit_*.json</code> · 重新生成：
    <code class="bg-slate-200 px-2 py-0.5 rounded">python3 -m stock_research.jobs.audit_picks</code>
  </p>
</section>
'''


def _fmt_market_cap(usd: float | None) -> str:
    """yfinance market_cap (USD float) → "$1.23T" / "$45.6B" / "$8.7M"."""
    if not usd or usd <= 0:
        return ""
    abs_u = float(usd)
    if abs_u >= 1e12:
        return f"${abs_u/1e12:.2f}T"
    if abs_u >= 1e9:
        return f"${abs_u/1e9:.1f}B"
    if abs_u >= 1e6:
        return f"${abs_u/1e6:.1f}M"
    return f"${abs_u:,.0f}"


def build():
    print("[1/3] 拉取数据 [DuckDB]...")
    from stock_db import fetch_records_view, fetch_picks_view
    records = fetch_records_view()
    # 兼容旧 schema: market_cap 字段(人工填写"$2.8T")现在格式化自 yf_market_cap
    for r in records:
        r["market_cap"] = _fmt_market_cap(r.get("yf_market_cap"))
    print(f"  共 {len(records)} 条 watchlist (含 prices JOIN)")
    picks = fetch_picks_view()
    print(f"  共 {len(picks)} 条自选股·今日 Top (来自 reviews JOIN picks)")

    # 读取模拟结果（如果存在）
    sim_file = os.path.join(_REPO, "simulation_plan_a.json")
    simulation = {}
    if os.path.exists(sim_file):
        with open(sim_file, encoding="utf-8") as f:
            simulation = json.load(f)
        print(f"  共 {len(simulation.get('stock_stats', {}))} 条模拟数据")

    # 读取专业分析数据 —— 双源：本地 JSON 文件 + DuckDB pipeline 镜像
    def _load_json(name):
        p = os.path.join(_REPO, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _load_pipeline_db(name_no_ext):
        """从 DuckDB snapshots 表读 category='pipeline' 最新快照。"""
        db_path = os.path.join(_REPO, "stock_history.duckdb")
        if not os.path.exists(db_path):
            return {}
        try:
            import duckdb
        except ImportError:
            return {}
        try:
            con = duckdb.connect(db_path, read_only=True)
            row = con.execute(
                "SELECT payload FROM snapshots WHERE category='pipeline' AND name=? "
                "ORDER BY taken_at DESC LIMIT 1",
                [name_no_ext],
            ).fetchone()
            con.close()
            if not row:
                return {}
            payload = row[0]
            return json.loads(payload) if isinstance(payload, str) else payload
        except Exception as e:
            print(f"  ⚠️  从 DuckDB 读 {name_no_ext} 失败: {e}")
            return {}

    # 文件源
    risk_metrics = _load_json("data/latest/risk_metrics.json")
    track_13f = _load_json("data/latest/track_13f.json")
    optimization = _load_json("data/latest/optimization_result.json")
    plan_a_v6 = _load_json("data/latest/plan_a_v5.json")
    history_data = _load_json("data/latest/history_data.json")
    discovery = _load_json("data/discovery_candidates.json")

    # DuckDB 源
    risk_metrics_db = _load_pipeline_db("risk_metrics")
    track_13f_db = _load_pipeline_db("track_13f")
    optimization_db = _load_pipeline_db("optimization_result")
    plan_a_v6_db = _load_pipeline_db("plan_a_v5")
    history_data_db = _load_pipeline_db("history_data")

    if risk_metrics:
        print(f"  风险指标已加载 (Sharpe={risk_metrics.get('sharpe', 'N/A')})")
    if track_13f:
        print(f"  13F 数据已加载 ({len(track_13f.get('tickers', {}))} 只美股)")
    if optimization:
        print(f"  优化结果已加载 ({len(optimization.get('current_plan', []))} 只)")
    if plan_a_v6:
        print(f"  方案 A v6 已加载 ({len(plan_a_v6.get('plan_v5', []))} 只 · Sharpe {plan_a_v6.get('portfolio_metrics', {}).get('annual_sharpe', 'N/A')})")
    if history_data:
        print(f"  历史数据已加载 ({len(history_data.get('tickers', {}))} 只 × 2 年日K)")
    if discovery:
        print(f"  AI 推荐已加载 ({len(discovery.get('candidates', []))} 只 · universe {discovery.get('universe_size', 0)})")
    print(f"  [DuckDB 镜像] risk={'✓' if risk_metrics_db else '✗'} 13f={'✓' if track_13f_db else '✗'} "
          f"opt={'✓' if optimization_db else '✗'} plan={'✓' if plan_a_v6_db else '✗'} "
          f"hist={'✓' if history_data_db else '✗'}")

    us_count = sum(1 for r in records if "美股" in r["market"])
    cn_count = len(records) - us_count
    high_ai = sum(1 for r in records if "极强" in r["ai_relevance"] or "强（直接受益）" == r["ai_relevance"])

    print("[2/3] 渲染 HTML...")
    html = HTML_TEMPLATE
    html = html.replace("{UPDATE_TIME}", datetime.now().strftime("%Y-%m-%d %H:%M"))
    html = html.replace("{HEADLINE}", MY_VIEW["headline"])
    html = html.replace("{SUMMARY}", MY_VIEW["summary"])
    html = html.replace("{TOTAL}", str(len(records)))
    html = html.replace("{HIGH_AI}", str(high_ai))
    html = html.replace("{US_COUNT}", str(us_count))
    html = html.replace("{CN_COUNT}", str(cn_count))

    html = html.replace("{THESIS_CARDS}", "\n".join(thesis_card_html(t) for t in MY_VIEW["thesis"]))
    html = html.replace("{HUNDRED_X_CARDS}", "\n".join(hundred_x_card_html(c) for c in HUNDRED_X_CONDITIONS))
    html = html.replace("{TIMELINE_ITEMS}", "\n".join(timeline_item_html(t) for t in EVOLUTION))

    scarce_with_ticker_html = []
    for s in SCARCE_THEMES:
        s_copy = dict(s)
        s_copy["tickers"] = " ".join(f'<span class="ticker-badge">{t}</span>' for t in s["tickers"])
        scarce_with_ticker_html.append(s_copy)
    html = html.replace("{SCARCE_THEME_CARDS}", "\n".join(scarce_theme_card_html(t) for t in scarce_with_ticker_html))

    html = html.replace("{EVENT_CARDS}", "\n".join(event_card_html(e) for e in EVENTS))

    theme_sections = "\n".join(theme_section_html(t, records) for t in THEMES)
    html = html.replace("{THEME_SECTIONS}", theme_sections)

    # 反向审查面板（picks_audit 快照）— 2026-05-11 起单源走 DuckDB
    audit_snap_db = load_audit_snapshot_from_db()
    panel_db_inner = (
        audit_panel_html(audit_snap_db) if audit_snap_db else
        '<section class="max-w-7xl mx-auto px-6 py-10 bg-rose-50 rounded-2xl my-6">'
        '<p class="text-rose-700">⚠️ DuckDB <code>snapshots</code> 表中暂无 picks_audit 数据，待 daily_refresh 跑完累积。</p>'
        '</section>'
    )
    ts_db = (audit_snap_db or {}).get("ts", "—")[:16] if audit_snap_db else "—"

    audit_panel_combined = f'''
<div id="audit-panel">
  <div class="max-w-7xl mx-auto px-6 pt-6">
    <div class="text-xs text-slate-500 mb-1">数据源：<span class="font-mono px-1.5 py-0.5 bg-emerald-50 text-emerald-800 rounded">DuckDB · {ts_db}</span></div>
  </div>
  <div id="audit-panel-db-wrap" data-source="duckdb" data-ts="{ts_db}">{panel_db_inner}</div>
</div>
'''
    html = html.replace("{AUDIT_PANEL}", audit_panel_combined)

    # 打分规则面板（动态读 factor_weights.json，缺失则 fallback 到「未实证」版本）
    calib_snap = load_calibration_snapshot()
    html = html.replace("{SCORING_RULES_PANEL}", scoring_rules_panel_html(calib_snap))
    if audit_snap_db:
        n_picks_db = audit_snap_db.get("picks_today_count", 0)
        print(f"  反向审查快照已加载 [DuckDB]（{n_picks_db} 只 picks @ {ts_db}）")

    # RECORDS / PICKS / SIMULATION 来自飞书 watchlist 实时拉，其它走 DuckDB
    # 从 DuckDB 拉 watchlist 链条信息(chain/chain_tier/chain_role/layman_intro)
    # 用于 Stock Pill 组件 — 任何 tab 显示股票时都能查到链条上下文
    try:
        from stock_db import fetch_all_watchlist
        wl_rows = fetch_all_watchlist()
        chain_info = {
            r["code"]: {
                "name": r.get("name"),
                "chain": r.get("chain"),
                "chain_tier": r.get("chain_tier"),
                "chain_role": r.get("chain_role"),
                "layman_intro": r.get("layman_intro"),
            }
            for r in wl_rows
        }
        n_wl = len(chain_info)
        # 2026-05-11 PM: 合并 candidate / 其他非 watchlist ticker 的链条覆盖文件
        # (让AI 推荐里的 ticker 也能在 stockPill 显示链条信息)
        overrides_path = os.path.join(_REPO, "data", "stock_chain_overrides.json")
        if os.path.exists(overrides_path):
            with open(overrides_path, encoding="utf-8") as f:
                overrides = json.load(f)
            n_added = 0
            for code, meta in overrides.items():
                if code.startswith("_"):  # 跳过 _comment / _schema_version 这些元数据键
                    continue
                if code not in chain_info:  # watchlist 优先
                    chain_info[code] = meta
                    n_added += 1
            print(f"  Stock Pill 链条上下文已加载 [DuckDB+overrides]"
                  f"({n_wl} watchlist + {n_added} overrides = {len(chain_info)} 条)")
        else:
            print(f"  Stock Pill 链条上下文已加载 [DuckDB]({n_wl} 条)")
    except Exception as e:
        print(f"  ⚠️ 链条上下文加载失败: {e}")
        chain_info = {}
    html = html.replace("{WATCHLIST_CHAIN_INFO_JSON}", json.dumps(chain_info, ensure_ascii=False))

    # AI 推荐的历史推荐 + 准确度跟踪(DuckDB discovery_history JOIN tracking)
    try:
        from stock_db import fetch_discovery_history
        disc_hist = fetch_discovery_history(days=90)  # 过去 90 天
        # date / datetime → ISO string,方便 JSON
        for r in disc_hist:
            for k in ("generated_date", "last_refreshed_at"):
                v = r.get(k)
                if v is not None and hasattr(v, "isoformat"):
                    r[k] = v.isoformat()
        print(f"  Discovery 历史已加载 [DuckDB]({len(disc_hist)} 条推荐记录)")
    except Exception as e:
        print(f"  ⚠️ Discovery 历史加载失败: {e}")
        disc_hist = []
    html = html.replace("{DISCOVERY_HISTORY_JSON}", json.dumps(disc_hist, ensure_ascii=False, default=str))

    # AI 评级:自选股每只的当前评级(来自 picks 最新一日,daily_picks_v5 学术因子产出)
    watchlist_ratings = {}
    try:
        from stock_db import get_db as _get_db
        _conn = _get_db()
        for code, rating, total_score, ai_score in _conn.execute(
            "SELECT code, rating, total_score, ai_score FROM picks "
            "WHERE pick_date = (SELECT MAX(pick_date) FROM picks)"
        ).fetchall():
            watchlist_ratings[code] = {
                "rating": rating,
                "total_score": total_score,
                "ai_score": ai_score,
            }
        _conn.close()
        print(f"  自选股 AI 评级已加载 [DuckDB picks]({len(watchlist_ratings)} 只)")
    except Exception as e:
        print(f"  ⚠️ AI 评级加载失败: {e}")
    html = html.replace("{WATCHLIST_RATINGS_JSON}", json.dumps(watchlist_ratings, ensure_ascii=False))
    html = html.replace("{RECORDS_JSON}", json.dumps(records, ensure_ascii=False, default=str))
    html = html.replace("{PICKS_JSON}", json.dumps(picks, ensure_ascii=False, default=str))
    html = html.replace("{SIMULATION_JSON}", json.dumps(simulation, ensure_ascii=False))
    html = html.replace("{RISK_METRICS_JSON_DB}", json.dumps(risk_metrics_db, ensure_ascii=False))
    html = html.replace("{TRACK_13F_JSON_DB}", json.dumps(track_13f_db, ensure_ascii=False))
    html = html.replace("{OPTIMIZATION_JSON_DB}", json.dumps(optimization_db, ensure_ascii=False))
    html = html.replace("{PLAN_A_V6_JSON_DB}", json.dumps(plan_a_v6_db, ensure_ascii=False))
    html = html.replace("{DISCOVERY_JSON}", json.dumps(discovery, ensure_ascii=False))

    # AI 组合方案 — Static (A 类: buy-and-hold from inception) — DuckDB 优先，fallback JSON
    plan_for_bt = plan_a_v6_db or plan_a_v6
    history_for_bt = history_data_db or history_data
    backtest_db = compute_plan_forward_track(plan_for_bt, history_for_bt)
    if backtest_db and backtest_db.get("metrics"):
        m = backtest_db["metrics"]
        inception = backtest_db.get("inception_date") or "?"
        baseline = backtest_db.get("baseline_date") or "?"
        print(f"  AI 组合方案 A 静态: 锁定 {inception} → 基线 {baseline} · 跟踪 {m['n_tracked_days']} 日"
              f" · 累计 {m['cumulative_return_pct']}% (vs SPY {m['bench_cumulative_return_pct']}%)")
    html = html.replace("{PLAN_BACKTEST_JSON_DB}", json.dumps(backtest_db, ensure_ascii=False))

    # AI 组合方案 — Dynamic (C 类: weekly Monday rebalance, P1+P2)
    dynamic_db = compute_dynamic_rebalance_track(history_for_bt)
    if dynamic_db and dynamic_db.get("metrics"):
        m = dynamic_db["metrics"]
        print(f"  AI 组合方案 C 动态: {m['n_rebalances']} 次调仓 · 累计手续费 {dynamic_db.get('total_commission_pct', 0)}% · "
              f"跟踪 {m['n_tracked_days']} 日 · 累计 {m['cumulative_return_pct']}% (vs SPY {m['bench_cumulative_return_pct']}%)")
    html = html.replace("{PLAN_DYNAMIC_JSON_DB}", json.dumps(dynamic_db, ensure_ascii=False))

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[3/3] 已生成：{OUTPUT}")
    print(f"\n用浏览器打开：file://{OUTPUT}")


if __name__ == "__main__":
    build()
