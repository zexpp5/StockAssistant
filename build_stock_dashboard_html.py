"""
AI 投资研究 Dashboard - 专业研究报告风格

设计目标：让一个完全没看过这些数据的同伴，30 秒看懂全局，3 分钟看懂任何一只股票。

输出：/Users/yanli/.hermes/scripts/stock_dashboard.html
"""
import sys
import os
import json
import requests
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from feishu_auth import feishu_token, FEISHU_APP_TOKEN  # noqa: E402

TABLE_ID = "tblaEuCPOlXBlSvP"
PICKS_TABLE_ID = "tbl7K88JZ0ZMqPIE"
BASE_URL = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{TABLE_ID}"
PICKS_URL = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{PICKS_TABLE_ID}"
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_dashboard.html")


def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def fetch_records(token, base_url=None):
    all_items = []
    page_token = None
    url = (base_url or BASE_URL)
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(f"{url}/records", headers=headers(token), params=params)
        d = resp.json()
        all_items.extend(d.get("data", {}).get("items", []))
        if not d.get("data", {}).get("has_more"):
            break
        page_token = d["data"]["page_token"]
    return all_items


def extract_picks(items):
    """提取每日优选记录关键字段。"""
    out = []
    for item in items:
        f = item.get("fields", {})
        out.append({
            "name": normalize_field(f.get("股票名称")),
            "code": normalize_field(f.get("代码")),
            "rating": normalize_field(f.get("入选评分")),
            "score": f.get("综合得分"),
            "entry_price": normalize_field(f.get("入选时价格")),
            "current_price": normalize_field(f.get("当前价格")),
            "pct": f.get("累计涨跌%"),
            "days_held": f.get("持有天数"),
            "grade": normalize_field(f.get("命中评级")),
            "theme": normalize_field(f.get("主题分类")),
            "ai_relevance": normalize_field(f.get("AI关联度")),
            "pick_date": f.get("入选日期"),
        })
    return out


def normalize_field(v):
    if v is None:
        return ""
    if isinstance(v, list):
        out = []
        for item in v:
            if isinstance(item, dict):
                out.append(item.get("text", "") or item.get("name", ""))
            else:
                out.append(str(item))
        return "\n".join(out)
    if isinstance(v, dict):
        return v.get("name", "") or v.get("text", "") or json.dumps(v, ensure_ascii=False)
    return str(v)


def extract_records(items):
    out = []
    for item in items:
        f = item.get("fields", {})
        out.append({
            "name": normalize_field(f.get("股票名称")),
            "code": normalize_field(f.get("代码")),
            "market": normalize_field(f.get("市场")),
            "business": normalize_field(f.get("主营业务")),
            "industry": normalize_field(f.get("行业归类")),
            "ai_relevance": normalize_field(f.get("AI关联度")),
            "ai_logic": normalize_field(f.get("AI关联逻辑")),
            "market_cap": normalize_field(f.get("当前市值")),
            "earnings": normalize_field(f.get("最近季度业绩")),
            "conclusion": normalize_field(f.get("研究结论")),
            "risks": normalize_field(f.get("关键风险")),
            "peers": normalize_field(f.get("可比公司")),
            "rhythm": normalize_field(f.get("跟踪节奏")),
            "status": normalize_field(f.get("研究状态")),
            "source": normalize_field(f.get("数据来源")),
            "credibility": normalize_field(f.get("数据可信度")),
            "verification": normalize_field(f.get("双源验证")),
            "info_breakdown": normalize_field(f.get("信息构成")),
            "latest_price": normalize_field(f.get("最新价格")),
            "ytd_pct": f.get("YTD涨幅%"),
            "one_year_pct": f.get("一年涨幅%"),
            "one_month_pct": f.get("1月涨幅%"),
            "one_week_pct": f.get("1周涨幅%"),
            "forward_pe": f.get("远期PE"),
            "peg": f.get("PEG"),
            "earnings_growth_pct": f.get("利润增速%"),
            "yf_market_cap": normalize_field(f.get("yf市值")),
        })
    return out


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
        "tickers": ["MRVL", "AVGO", "300308", "300502", "787635"],
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
            "**KO/MCD 不是 AI 故事股**，放进 watchlist 是为了在 AI 板块回调时提供对照。"
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
<body class="bg-gradient-to-b from-slate-50 to-white">

<!-- ============ Tab 导航（sticky，所有 tab 都能看见） ============ -->
<nav id="tab-nav" class="sticky top-0 z-50 bg-white border-b border-slate-200 shadow-sm">
  <div class="max-w-7xl mx-auto px-4 flex items-center overflow-x-auto">
    <span class="text-base font-bold text-slate-900 mr-6 flex-shrink-0">📊 AI 投资</span>
    <a href="#overview" data-tab="overview" class="tab-link px-3 py-3 text-sm font-medium text-slate-700 hover:text-violet-600 border-b-2 border-transparent hover:border-violet-300 transition whitespace-nowrap">📌 概览</a>
    <a href="#portfolio" data-tab="portfolio" class="tab-link px-3 py-3 text-sm font-medium text-slate-700 hover:text-violet-600 border-b-2 border-transparent hover:border-violet-300 transition whitespace-nowrap">💼 我的持仓</a>
    <a href="#picks" data-tab="picks" class="tab-link px-3 py-3 text-sm font-medium text-slate-700 hover:text-violet-600 border-b-2 border-transparent hover:border-violet-300 transition whitespace-nowrap">⭐ 每日优选</a>
    <a href="#valuation" data-tab="valuation" class="tab-link px-3 py-3 text-sm font-medium text-slate-700 hover:text-violet-600 border-b-2 border-transparent hover:border-violet-300 transition whitespace-nowrap">📈 估值视角</a>
    <a href="#themes" data-tab="themes" class="tab-link px-3 py-3 text-sm font-medium text-slate-700 hover:text-violet-600 border-b-2 border-transparent hover:border-violet-300 transition whitespace-nowrap">🗂 主题分组</a>
    <a href="#history" data-tab="history" class="tab-link px-3 py-3 text-sm font-medium text-slate-700 hover:text-violet-600 border-b-2 border-transparent hover:border-violet-300 transition whitespace-nowrap">📅 历史</a>
    <a href="#professional" data-tab="professional" class="tab-link px-3 py-3 text-sm font-medium text-slate-700 hover:text-violet-600 border-b-2 border-transparent hover:border-violet-300 transition whitespace-nowrap">📊 专业分析</a>
    <span class="ml-auto text-xs text-slate-500 flex-shrink-0">{UPDATE_TIME}</span>
  </div>
</nav>

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

<!-- ============ 我的核心观点 ============ -->
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

<!-- ============ 打分规则说明 ============ -->
<section id="scoring-rules" class="max-w-7xl mx-auto px-6 py-10">
  <details class="bg-white rounded-2xl shadow-sm border border-slate-200 overflow-hidden">
    <summary class="px-6 py-4 hover:bg-slate-50 cursor-pointer">
      <div class="flex items-center justify-between gap-3">
        <div>
          <h2 class="text-2xl font-bold text-slate-900 flex items-center gap-3">
            <span class="text-3xl">📐</span>
            每日优选 · 打分规则（透明）
          </h2>
          <p class="text-sm text-slate-600 mt-1 ml-12">点击展开 — 了解我是按什么标准筛选「每日优选」的</p>
        </div>
        <span class="arrow text-slate-400"></span>
      </div>
    </summary>

    <div class="px-6 pb-6">
      <!-- 总览公式 -->
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

      <!-- 4 维度卡片 -->
      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3 mb-6">
        <!-- AI 关联度 -->
        <div class="bg-rose-50 border-2 border-rose-200 rounded-xl p-4">
          <div class="flex items-center justify-between mb-3">
            <h3 class="font-bold text-rose-800">🎯 AI 关联度</h3>
            <span class="text-2xl font-mono font-bold text-rose-600">35</span>
          </div>
          <div class="text-xs text-slate-700 space-y-1.5">
            <div class="flex justify-between border-b border-rose-100 pb-1"><span>极强（核心标的）</span><span class="font-mono font-bold">35</span></div>
            <div class="flex justify-between border-b border-rose-100 pb-1"><span>强（直接受益）</span><span class="font-mono font-bold">28</span></div>
            <div class="flex justify-between border-b border-rose-100 pb-1"><span>中（间接受益）</span><span class="font-mono">18</span></div>
            <div class="flex justify-between border-b border-rose-100 pb-1"><span>弱（沾边）</span><span class="font-mono">8</span></div>
            <div class="flex justify-between"><span>无</span><span class="font-mono">0</span></div>
          </div>
          <div class="text-xs text-rose-700 mt-3 pt-2 border-t border-rose-200">
            <strong>权重最高（35%）</strong>：这是 AI 主题投资，AI 关联度是首要标准
          </div>
        </div>

        <!-- 估值 -->
        <div class="bg-emerald-50 border-2 border-emerald-200 rounded-xl p-4">
          <div class="flex items-center justify-between mb-3">
            <h3 class="font-bold text-emerald-800">💰 估值（PEG/PE）</h3>
            <span class="text-2xl font-mono font-bold text-emerald-600">25</span>
          </div>
          <div class="text-xs text-slate-700 space-y-1.5">
            <div class="text-slate-500 mb-1 italic">优先看 PEG（PE÷增速）：</div>
            <div class="flex justify-between border-b border-emerald-100 pb-1"><span>PEG &lt; 1（便宜）</span><span class="font-mono font-bold">25</span></div>
            <div class="flex justify-between border-b border-emerald-100 pb-1"><span>PEG 1-2（合理）</span><span class="font-mono font-bold">18</span></div>
            <div class="flex justify-between border-b border-emerald-100 pb-1"><span>PEG 2-3（偏贵）</span><span class="font-mono">10</span></div>
            <div class="flex justify-between border-b border-emerald-100 pb-1"><span>PEG &gt; 3（贵）</span><span class="font-mono">4</span></div>
            <div class="flex justify-between"><span>PEG 缺失，PE &lt; 25</span><span class="font-mono">15</span></div>
          </div>
          <div class="text-xs text-emerald-700 mt-3 pt-2 border-t border-emerald-200">
            <strong>看的是相对增速的估值</strong>，不是绝对 PE 数字
          </div>
        </div>

        <!-- 趋势 -->
        <div class="bg-cyan-50 border-2 border-cyan-200 rounded-xl p-4">
          <div class="flex items-center justify-between mb-3">
            <h3 class="font-bold text-cyan-800">📈 趋势（1Y+1W）</h3>
            <span class="text-2xl font-mono font-bold text-cyan-600">25</span>
          </div>
          <div class="text-xs text-slate-700 space-y-1.5">
            <div class="text-slate-500 mb-1 italic">1 年涨幅基础分：</div>
            <div class="flex justify-between border-b border-cyan-100 pb-1"><span>涨 50%-200%（健康）</span><span class="font-mono font-bold">20</span></div>
            <div class="flex justify-between border-b border-cyan-100 pb-1"><span>涨 0%-50%（稳健）</span><span class="font-mono">15</span></div>
            <div class="flex justify-between border-b border-cyan-100 pb-1"><span>涨 &gt; 200%（追高）</span><span class="font-mono">12</span></div>
            <div class="flex justify-between border-b border-cyan-100 pb-1"><span>跌（逆势）</span><span class="font-mono">8</span></div>
            <div class="flex justify-between"><span class="text-slate-500">+ 1 周涨 → 加</span><span class="font-mono">+5</span></div>
          </div>
          <div class="text-xs text-cyan-700 mt-3 pt-2 border-t border-cyan-200">
            <strong>涨太多反而扣分</strong>（追高风险），跌不一定差（可能错杀）
          </div>
        </div>

        <!-- 数据可信度 -->
        <div class="bg-violet-50 border-2 border-violet-200 rounded-xl p-4">
          <div class="flex items-center justify-between mb-3">
            <h3 class="font-bold text-violet-800">🔍 数据可信度</h3>
            <span class="text-2xl font-mono font-bold text-violet-600">15</span>
          </div>
          <div class="text-xs text-slate-700 space-y-1.5">
            <div class="flex justify-between border-b border-violet-100 pb-1"><span>🟢 高（官方+多源）</span><span class="font-mono font-bold">15</span></div>
            <div class="flex justify-between border-b border-violet-100 pb-1"><span>🟡 中（权威媒体单源）</span><span class="font-mono">10</span></div>
            <div class="flex justify-between border-b border-violet-100 pb-1"><span>🔴 低（二手/推断）</span><span class="font-mono">5</span></div>
            <div class="flex justify-between"><span>未填</span><span class="font-mono">3</span></div>
          </div>
          <div class="text-xs text-violet-700 mt-3 pt-2 border-t border-violet-200">
            <strong>数据来源越多越权威，分数越高</strong>，避免单一来源被误导
          </div>
        </div>
      </div>

      <!-- 评级阈值 -->
      <div class="bg-gradient-to-r from-amber-50 to-orange-50 rounded-xl p-5 mb-4">
        <h3 class="font-bold text-slate-900 mb-3">📊 评级阈值</h3>
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
          <li><strong>不考虑宏观/政策风险</strong>：地缘冲突、关税、监管这些黑天鹅打分没法量化</li>
          <li><strong>PEG 依赖分析师预测</strong>：增速预测错了 PEG 就错了</li>
          <li><strong>只用 watchlist 内 37 只</strong>：不是从全市场万只里挑，覆盖范围有限</li>
          <li><strong>评分高 ≠ 现在买</strong>：技术面、市场情绪、个人仓位都没考虑</li>
        </ul>
      </div>
    </div>
  </details>
</section>

<!-- ============ 每日优选回顾 ============ -->
<section id="picks-review" class="max-w-7xl mx-auto px-6 py-10 bg-gradient-to-br from-amber-50 to-orange-50 rounded-2xl my-6">
  <div class="flex items-center justify-between mb-4">
    <div>
      <div class="flex items-center gap-3 mb-1">
        <span class="text-3xl">⭐</span>
        <h2 class="text-2xl font-bold text-slate-900">每日优选 · 历史回顾</h2>
      </div>
      <p class="text-slate-700">每天自动选股，长期跟踪表现 · <strong>检验我的选股策略是否有效</strong></p>
    </div>
    <div id="picks-summary" class="text-right"></div>
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

<!-- ============ 全局分布图表 ============ -->
<section id="distribution" class="max-w-7xl mx-auto px-6 py-10">
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

<!-- ============ 主题分组卡片 ============ -->
<section id="theme-groups" class="max-w-7xl mx-auto px-6 py-10">
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
      <button onclick="loadPlanA()" class="bg-amber-500 hover:bg-amber-600 text-white px-4 py-2 rounded-lg text-sm font-medium">📌 一键加载方案 A</button>
      <button onclick="addHolding()" class="bg-violet-600 hover:bg-violet-700 text-white px-4 py-2 rounded-lg text-sm font-medium">+ 添加持仓</button>
    </div>
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
  <div id="portfolio-summary" class="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4"></div>

  <!-- 三层警戒线进度条 -->
  <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4 mb-4">
    <h3 class="text-sm font-semibold text-slate-700 mb-3">⚠️ 风险警戒线（基于本金 50 万）</h3>
    <div id="alert-line" class="space-y-2"></div>
  </div>

  <!-- 持仓列表 -->
  <div class="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
    <table class="w-full text-sm">
      <thead class="bg-slate-100">
        <tr>
          <th class="px-3 py-2 text-left">股票</th>
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
        <tr><td colspan="10" class="text-center text-slate-500 py-8">暂无持仓 · 点击「+ 添加持仓」开始记录</td></tr>
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
          <label class="text-xs font-medium text-slate-600">股票（选择 watchlist 标的）</label>
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

<!-- ============ 📅 历史 Tab ============ -->
<section id="history" class="max-w-7xl mx-auto px-6 py-10" style="display:none">
  <h2 class="text-2xl font-bold text-slate-900 mb-2">📅 历史走势</h2>
  <p class="text-sm text-slate-600 mb-6">从 yfinance 拉取过去 90 天的价格走势 · 数据来自浏览器侧异步请求 · 累积久了 DuckDB 也会有自己的快照库</p>

  <!-- 选股 + 价格走势图 -->
  <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4 mb-4">
    <div class="flex items-center gap-3 mb-3">
      <h3 class="text-sm font-semibold text-slate-700">🎯 单只股票走势对比</h3>
      <select id="history-codes" multiple size="6" class="px-3 py-1 border rounded text-sm flex-1 max-w-md"></select>
      <button onclick="loadHistoryCharts()" class="bg-violet-600 hover:bg-violet-700 text-white px-3 py-1 rounded text-sm">📊 加载</button>
      <span class="text-xs text-slate-500">按住 Ctrl/Cmd 多选（最多 5 只）</span>
    </div>
    <div id="chart-history" style="height:420px"></div>
  </div>

  <!-- DuckDB 快照统计 -->
  <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
    <h3 class="text-sm font-semibold text-slate-700 mb-2">📦 DuckDB 本地快照库（用于长期回溯）</h3>
    <p class="text-xs text-slate-600">每天 daily_refresh 自动写入。当前仅累积当天，运行几天后会有真实历史可看。</p>
    <p class="text-xs text-slate-500 mt-1 font-mono">/Users/yanli/.hermes/scripts/stock_history.duckdb</p>
  </div>
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
    <div class="bg-amber-50 border border-amber-200 rounded-xl p-4 mb-4">
      <p class="text-sm text-amber-900">⚠️ 当前数据是 yfinance 快照（仅 Top 10 机构持仓）。看不到「Bridgewater 加仓 50%」这种季度变动信号。专业版需 SEC EDGAR 13F-HR 解析。</p>
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

<!-- ============ Footer ============ -->
<footer class="bg-slate-900 text-slate-300 py-8 mt-12">
  <div class="max-w-7xl mx-auto px-6 text-sm">
    <p class="mb-2"><strong class="text-white">免责声明</strong>：本看板由 Claude AI 基于公开信息生成，仅作研究学习参考，<strong class="text-rose-300">绝不构成任何投资建议</strong>。</p>
    <p class="mb-2">投资有风险，所有交易决策需自行判断、自负盈亏。本看板的数据可能滞后、错误或解读偏差。</p>
    <p class="text-slate-500">数据源：飞书「股票研究 Watchlist」表 · WebSearch 抓取的最新公司财报 · 数据更新时间 {UPDATE_TIME}</p>
  </div>
</footer>

<script>
const RECORDS = {RECORDS_JSON};
const PICKS = {PICKS_JSON};
const SIMULATION = {SIMULATION_JSON};
const RISK_METRICS = {RISK_METRICS_JSON};
const TRACK_13F = {TRACK_13F_JSON};
const OPTIMIZATION = {OPTIMIZATION_JSON};

// ============ Tab 切换框架 ============
const TAB_SECTIONS = {
  overview: ["hero", "thesis", "evolution", "scarce", "events", "hundred-x"],
  portfolio: ["portfolio"],
  picks: ["scoring-rules", "picks-review"],
  valuation: ["valuation"],
  themes: ["distribution", "theme-groups"],
  history: ["history"],
  professional: ["professional"],
};

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
  if (tab === "portfolio") setTimeout(renderPortfolio, 50);
  if (tab === "history") setTimeout(initHistorySelect, 50);
  if (tab === "professional") setTimeout(renderProfessional, 50);
}

function getTabFromHash() {
  const h = location.hash.replace("#", "");
  return TAB_SECTIONS[h] ? h : "overview";
}
window.addEventListener("hashchange", () => switchTab(getTabFromHash()));
window.addEventListener("DOMContentLoaded", () => switchTab(getTabFromHash()));

// ============ 持仓管理（localStorage） ============
const STORAGE_KEY = "ai_portfolio_holdings_v1";
const TOTAL_CAPITAL = 500000;
const STOPLOSS_LINE = 300000;
const WARNING_LINE = 400000;
const TARGET_LINE = 550000;

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

function loadHoldings() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]"); }
  catch { return []; }
}

function saveHoldings(holdings) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(holdings));
  renderPortfolio();
}

let editingIdx = -1;

function addHolding() { editingIdx = -1; openModal(); }
function editHolding(idx) {
  editingIdx = idx;
  const h = loadHoldings()[idx];
  document.getElementById("form-code").value = h.code;
  document.getElementById("form-price").value = h.entry_price;
  document.getElementById("form-shares").value = h.shares;
  document.getElementById("form-date").value = h.date;
  openModal();
}
function deleteHolding(idx) {
  if (!confirm("确定删除？")) return;
  const arr = loadHoldings();
  arr.splice(idx, 1);
  saveHoldings(arr);
}
function openModal() {
  // 填充股票下拉
  const sel = document.getElementById("form-code");
  sel.innerHTML = RECORDS.map(r => `<option value="${r.code}">${r.name} (${r.code})</option>`).join("");
  if (editingIdx === -1) {
    document.getElementById("form-price").value = "";
    document.getElementById("form-shares").value = "";
    document.getElementById("form-date").value = new Date().toISOString().split("T")[0];
  }
  document.getElementById("holding-modal").classList.remove("hidden");
}
function closeModal() { document.getElementById("holding-modal").classList.add("hidden"); }
function saveHolding() {
  const code = document.getElementById("form-code").value;
  const entry_price = parseFloat(document.getElementById("form-price").value);
  const shares = parseFloat(document.getElementById("form-shares").value);
  const date = document.getElementById("form-date").value;
  if (!code || !entry_price || !shares) { alert("请填完整"); return; }
  const arr = loadHoldings();
  const h = { code, entry_price, shares, date };
  if (editingIdx >= 0) arr[editingIdx] = h; else arr.push(h);
  saveHoldings(arr);
  closeModal();
}

function renderPortfolio() {
  const holdings = loadHoldings();
  const tbody = document.getElementById("holdings-table");
  if (!tbody) return;

  if (holdings.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" class="text-center text-slate-500 py-8">暂无持仓 · 点击「+ 添加持仓」开始记录</td></tr>';
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
    const cur = getCurrentPriceRMB(h.code);
    if (!cur) {
      return `<tr class="border-t border-slate-100">
        <td class="px-3 py-2">${name}</td>
        <td class="px-3 py-2 text-right">${h.entry_price}</td>
        <td class="px-3 py-2 text-right">${h.shares}</td>
        <td class="px-3 py-2 text-right">-</td>
        <td class="px-3 py-2 text-right text-slate-400" colspan="5">无价格数据</td>
        <td class="px-3 py-2 text-center">
          <button onclick="editHolding(${idx})" class="text-violet-600 text-xs">编辑</button>
          <button onclick="deleteHolding(${idx})" class="text-rose-500 text-xs ml-2">删除</button>
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
      <td class="px-3 py-2 font-medium">${name}<br><span class="text-xs text-slate-500 font-mono">${h.code}</span></td>
      <td class="px-3 py-2 text-right font-mono">${h.entry_price.toFixed(2)} ${cur.currency}</td>
      <td class="px-3 py-2 text-right font-mono">${h.shares}</td>
      <td class="px-3 py-2 text-right font-mono">${cost_rmb.toFixed(0)}</td>
      <td class="px-3 py-2 text-right font-mono">${cur.raw_price.toFixed(2)}</td>
      <td class="px-3 py-2 text-right font-mono">${value_rmb.toFixed(0)}</td>
      <td class="px-3 py-2 text-right font-mono ${pnlColor}">${pnl_rmb >= 0 ? '+' : ''}${pnl_rmb.toFixed(0)}</td>
      <td class="px-3 py-2 text-right font-mono ${pnlColor}">${pnl_pct >= 0 ? '+' : ''}${pnl_pct.toFixed(2)}%</td>
      <td class="px-3 py-2 text-right font-mono">${(value_rmb / TOTAL_CAPITAL * 100).toFixed(1)}%</td>
      <td class="px-3 py-2 text-center">
        <button onclick="editHolding(${idx})" class="text-violet-600 text-xs">编辑</button>
        <button onclick="deleteHolding(${idx})" class="text-rose-500 text-xs ml-2">删除</button>
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
    <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200">
      <div class="text-2xl font-bold text-slate-900">${portfolio_value.toLocaleString(undefined, {maximumFractionDigits:0})}</div>
      <div class="text-xs text-slate-500 mt-1">组合总值 RMB（含现金）</div>
    </div>
    <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200">
      <div class="text-2xl font-bold ${stockColor}">${total_pnl >= 0 ? '+' : ''}${total_pnl.toLocaleString(undefined, {maximumFractionDigits:0})}</div>
      <div class="text-xs text-slate-500 mt-1">股票仓盈亏</div>
    </div>
    <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200">
      <div class="text-2xl font-bold ${stockColor}">${total_pnl_pct >= 0 ? '+' : ''}${total_pnl_pct.toFixed(2)}%</div>
      <div class="text-xs text-slate-500 mt-1">股票仓收益率</div>
    </div>
    <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200">
      <div class="text-2xl font-bold text-slate-900">${cash.toLocaleString(undefined, {maximumFractionDigits:0})}</div>
      <div class="text-xs text-slate-500 mt-1">现金 RMB</div>
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
function clearHoldings() {
  if (!confirm("确定清空所有持仓？")) return;
  localStorage.removeItem(STORAGE_KEY);
  renderPortfolio();
}

// ============ 一键加载方案 A（用模拟数据中的现价填入持仓） ============
function loadPlanA() {
  if (!SIMULATION || !SIMULATION.stock_stats) {
    alert("还没有方案 A 模拟数据，请先跑：python3 simulate_portfolio.py");
    return;
  }
  if (loadHoldings().length > 0) {
    if (!confirm("当前已有持仓数据，加载方案 A 会覆盖。继续？")) return;
  }
  const today = new Date().toISOString().split("T")[0];
  const holdings = [];
  Object.values(SIMULATION.stock_stats).forEach(s => {
    holdings.push({
      code: s.ticker,
      entry_price: s.last_price,
      shares: s.shares,
      date: today,
      _plan_a: true,
    });
  });
  saveHoldings(holdings);
  alert(`✅ 已加载方案 A 持仓（12 只 + 25,000 RMB 现金）\n按今天的真实收盘价计算\n\n几天后再看「我的持仓」即可看到真实进度。`);
}

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
        { yAxis: 500000, label: { formatter: "本金 50 万" } },
        { yAxis: 400000, label: { formatter: "预警 40 万" } },
        { yAxis: 300000, label: { formatter: "止损 30 万" } },
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
    tooltip: { trigger: "axis", formatter: p => `${p[0].name}: ${(p[0].data || 0).toLocaleString()} RMB<br>${(((p[0].data || 0) - 500000) / 500000 * 100).toFixed(2)}%` },
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
          { yAxis: 500000, label: { formatter: "起点 50 万" } },
          { yAxis: 300000, label: { formatter: "止损 30 万" } },
        ]},
      }],
    });
  }
}

function render13FPane() {
  if (!TRACK_13F || !TRACK_13F.tickers) {
    document.getElementById("track-13f-content").innerHTML = '<div class="text-slate-500">暂无数据，请先跑：python3 track_13f.py</div>';
    return;
  }
  const tickers = TRACK_13F.tickers;
  const html = Object.entries(tickers).map(([code, t]) => {
    const inst = (t.institutional || []).slice(0, 5);
    const mf = (t.mutual_fund || []).slice(0, 5);
    const instHtml = inst.map((h, i) => {
      const pct = h.pctHeld != null ? (h.pctHeld * 100).toFixed(2) : (h["% Out"] || 0).toFixed(2);
      const value = h.Value ? `$${(h.Value/1e9).toFixed(2)}B` : "";
      return `<tr class="border-t border-slate-100"><td class="px-2 py-1">${i+1}</td><td class="px-2 py-1 truncate" style="max-width:200px">${h.Holder || "?"}</td><td class="px-2 py-1 text-right font-mono">${(h.Shares || 0).toLocaleString()}</td><td class="px-2 py-1 text-right font-mono">${pct}%</td><td class="px-2 py-1 text-right font-mono">${value}</td></tr>`;
    }).join("");
    const mfHtml = mf.map((h, i) => {
      const pct = h.pctHeld != null ? (h.pctHeld * 100).toFixed(2) : (h["% Out"] || 0).toFixed(2);
      return `<tr class="border-t border-slate-100"><td class="px-2 py-1">${i+1}</td><td class="px-2 py-1 truncate" style="max-width:200px">${h.Holder || "?"}</td><td class="px-2 py-1 text-right font-mono">${(h.Shares || 0).toLocaleString()}</td><td class="px-2 py-1 text-right font-mono">${pct}%</td></tr>`;
    }).join("");
    return `<div class="bg-white rounded-xl border border-slate-200 overflow-hidden">
      <div class="bg-slate-100 px-4 py-2 font-semibold flex items-center gap-2">
        <span>${t.name}</span><span class="text-xs text-slate-500 font-mono">${code}</span>
      </div>
      <div class="p-4 grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
        <div>
          <h5 class="font-semibold text-slate-700 mb-2">🏛 Top 5 机构持仓</h5>
          <table class="w-full">
            <thead class="bg-slate-50 text-slate-600">
              <tr><th class="px-2 py-1 text-left">#</th><th class="px-2 py-1 text-left">机构</th><th class="px-2 py-1 text-right">股数</th><th class="px-2 py-1 text-right">占比</th><th class="px-2 py-1 text-right">市值</th></tr>
            </thead>
            <tbody>${instHtml || '<tr><td colspan="5" class="text-center text-slate-400 py-2">无</td></tr>'}</tbody>
          </table>
        </div>
        <div>
          <h5 class="font-semibold text-slate-700 mb-2">📊 Top 5 共同基金持仓</h5>
          <table class="w-full">
            <thead class="bg-slate-50 text-slate-600">
              <tr><th class="px-2 py-1 text-left">#</th><th class="px-2 py-1 text-left">基金</th><th class="px-2 py-1 text-right">股数</th><th class="px-2 py-1 text-right">占比</th></tr>
            </thead>
            <tbody>${mfHtml || '<tr><td colspan="4" class="text-center text-slate-400 py-2">无</td></tr>'}</tbody>
          </table>
        </div>
      </div>
    </div>`;
  }).join("");
  document.getElementById("track-13f-content").innerHTML = html;
}

function renderOptPane() {
  if (!OPTIMIZATION || !OPTIMIZATION.current_plan) {
    document.getElementById("opt-comparison").innerHTML = '<div class="text-slate-500 col-span-4">暂无数据，请先跑：python3 optimize_portfolio.py</div>';
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

// ============ 历史 Tab ============
let historyInited = false;
function initHistorySelect() {
  if (historyInited) return;
  const sel = document.getElementById("history-codes");
  if (!sel) return;
  sel.innerHTML = RECORDS
    .filter(r => r.code && (r.market || "").indexOf("美股") >= 0)
    .map(r => `<option value="${r.code}">${r.name} (${r.code})</option>`).join("");
  historyInited = true;
}

async function loadHistoryCharts() {
  const sel = document.getElementById("history-codes");
  const codes = Array.from(sel.selectedOptions).slice(0, 5).map(o => o.value);
  if (codes.length === 0) { alert("请至少选 1 只"); return; }

  document.getElementById("chart-history").innerHTML = '<div class="text-center text-slate-500 py-12">加载中...（用浏览器拉 Yahoo Finance，可能需要几秒）</div>';

  // 用 Yahoo Finance API（无需 key）
  const series = [];
  for (const code of codes) {
    try {
      const url = `https://query1.finance.yahoo.com/v8/finance/chart/${code}?range=3mo&interval=1d`;
      const resp = await fetch(url);
      const j = await resp.json();
      const result = j?.chart?.result?.[0];
      if (!result) continue;
      const ts = result.timestamp.map(t => new Date(t * 1000).toISOString().split("T")[0]);
      const closes = result.indicators?.quote?.[0]?.close || [];
      // 归一化到 100（便于多只对比）
      const first = closes.find(c => c != null);
      const norm = closes.map(c => c == null ? null : (c / first * 100));
      const r = RECORDS.find(x => x.code === code);
      series.push({ name: r ? r.name : code, type: "line", smooth: true, data: ts.map((t, i) => [t, norm[i]]) });
    } catch (e) { console.error("history fetch", code, e); }
  }

  if (series.length === 0) {
    document.getElementById("chart-history").innerHTML = '<div class="text-center text-rose-500 py-12">数据拉取失败（CORS 或网络问题）</div>';
    return;
  }

  echarts.init(document.getElementById("chart-history")).setOption({
    tooltip: { trigger: "axis" },
    legend: { top: 0 },
    grid: { left: 50, right: 30, top: 40, bottom: 40 },
    xAxis: { type: "time" },
    yAxis: { type: "value", name: "归一化（起点=100）" },
    series,
  });
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

// ============ 每日优选回顾 ============
const validPicks = PICKS.filter(p => p.pct != null && p.pct !== "");
const totalPicks = PICKS.length;
const validCount = validPicks.length;
const avgPct = validCount > 0 ? validPicks.reduce((s, p) => s + parseFloat(p.pct), 0) / validCount : 0;
const winCount = validPicks.filter(p => parseFloat(p.pct) > 5).length;
const flatCount = validPicks.filter(p => { const v = parseFloat(p.pct); return v >= -5 && v <= 5; }).length;
const lossCount = validPicks.filter(p => parseFloat(p.pct) < -5).length;
const winRate = validCount > 0 ? (winCount / validCount * 100) : 0;

document.getElementById("picks-summary").innerHTML = totalPicks > 0
  ? `<div class="text-xs text-slate-500">最近 30 天累计 <strong class="text-amber-700">${totalPicks}</strong> 次入选</div>`
  : "";

document.getElementById("picks-stats").innerHTML = totalPicks > 0 ? `
  <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200">
    <div class="text-2xl font-bold text-slate-900">${totalPicks}</div>
    <div class="text-xs text-slate-500 mt-1">累计入选次数</div>
  </div>
  <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200">
    <div class="text-2xl font-bold ${avgPct > 0 ? 'text-emerald-600' : 'text-rose-600'}">${avgPct > 0 ? '+' : ''}${avgPct.toFixed(2)}%</div>
    <div class="text-xs text-slate-500 mt-1">平均涨跌</div>
  </div>
  <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200">
    <div class="text-2xl font-bold text-emerald-600">${winRate.toFixed(0)}%</div>
    <div class="text-xs text-slate-500 mt-1">命中率（>+5%）</div>
  </div>
  <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200">
    <div class="text-lg font-bold text-emerald-600">${winCount} 命中</div>
    <div class="text-xs text-slate-500 mt-1"><span class="text-amber-600">${flatCount} 跟随</span> · <span class="text-rose-600">${lossCount} 失败</span></div>
  </div>
  <div class="bg-white rounded-lg p-3 shadow-sm border border-slate-200">
    <div class="text-2xl font-bold text-slate-900">${validCount}</div>
    <div class="text-xs text-slate-500 mt-1">已可回顾的（有持有天数）</div>
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

// Top 5 / Bottom 5
const sortedPicks = [...validPicks].sort((a, b) => parseFloat(b.pct) - parseFloat(a.pct));
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


def build():
    print("[1/3] 拉取飞书数据...")
    token = feishu_token()
    items = fetch_records(token)
    records = extract_records(items)
    print(f"  共 {len(records)} 条 watchlist")
    pick_items = fetch_records(token, PICKS_URL)
    picks = extract_picks(pick_items)
    print(f"  共 {len(picks)} 条每日优选")

    # 读取模拟结果（如果存在）
    sim_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "simulation_plan_a.json")
    simulation = {}
    if os.path.exists(sim_file):
        with open(sim_file, encoding="utf-8") as f:
            simulation = json.load(f)
        print(f"  共 {len(simulation.get('stock_stats', {}))} 条模拟数据")

    # 读取专业分析数据
    def _load_json(name):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        return {}
    risk_metrics = _load_json("risk_metrics.json")
    track_13f = _load_json("track_13f.json")
    optimization = _load_json("optimization_result.json")
    if risk_metrics:
        print(f"  风险指标已加载 (Sharpe={risk_metrics.get('sharpe', 'N/A')})")
    if track_13f:
        print(f"  13F 数据已加载 ({len(track_13f.get('tickers', {}))} 只美股)")
    if optimization:
        print(f"  优化结果已加载 ({len(optimization.get('current_plan', []))} 只)")

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

    html = html.replace("{RECORDS_JSON}", json.dumps(records, ensure_ascii=False))
    html = html.replace("{PICKS_JSON}", json.dumps(picks, ensure_ascii=False))
    html = html.replace("{SIMULATION_JSON}", json.dumps(simulation, ensure_ascii=False))
    html = html.replace("{RISK_METRICS_JSON}", json.dumps(risk_metrics, ensure_ascii=False))
    html = html.replace("{TRACK_13F_JSON}", json.dumps(track_13f, ensure_ascii=False))
    html = html.replace("{OPTIMIZATION_JSON}", json.dumps(optimization, ensure_ascii=False))

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[3/3] 已生成：{OUTPUT}")
    print(f"\n用浏览器打开：file://{OUTPUT}")


if __name__ == "__main__":
    build()
