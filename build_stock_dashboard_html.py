"""
AI 投资研究 Dashboard - 专业研究报告风格

设计目标：让一个完全没看过这些数据的同伴，30 秒看懂全局，3 分钟看懂任何一只股票。

输出：stock_dashboard.html（脚本所在目录）
"""
import sys
import os
import json
import requests
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from feishu_auth import feishu_token, FEISHU_APP_TOKEN  # noqa: E402

# 表 ID 走 .env，缺失时回退到本地默认（避免 .env 不全时仪表盘跑不起来）
TABLE_ID = os.environ.get("FEISHU_WATCHLIST_TABLE_ID") or "tblaEuCPOlXBlSvP"
PICKS_TABLE_ID = os.environ.get("FEISHU_PICKS_TABLE_ID") or "tbl7K88JZ0ZMqPIE"
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
    <a href="#audit" data-tab="audit" class="tab-link px-3 py-3 text-sm font-medium text-slate-700 hover:text-violet-600 border-b-2 border-transparent hover:border-violet-300 transition whitespace-nowrap">🛡 反向审查</a>
    <a href="#valuation" data-tab="valuation" class="tab-link px-3 py-3 text-sm font-medium text-slate-700 hover:text-violet-600 border-b-2 border-transparent hover:border-violet-300 transition whitespace-nowrap">📈 估值视角</a>
    <a href="#themes" data-tab="themes" class="tab-link px-3 py-3 text-sm font-medium text-slate-700 hover:text-violet-600 border-b-2 border-transparent hover:border-violet-300 transition whitespace-nowrap">🗂 主题分组</a>
    <a href="#history" data-tab="history" class="tab-link px-3 py-3 text-sm font-medium text-slate-700 hover:text-violet-600 border-b-2 border-transparent hover:border-violet-300 transition whitespace-nowrap">📅 历史</a>
    <a href="#professional" data-tab="professional" class="tab-link px-3 py-3 text-sm font-medium text-slate-700 hover:text-violet-600 border-b-2 border-transparent hover:border-violet-300 transition whitespace-nowrap">📊 专业分析</a>
    <a href="#upgrade" data-tab="upgrade" class="tab-link px-3 py-3 text-sm font-medium text-slate-700 hover:text-violet-600 border-b-2 border-transparent hover:border-violet-300 transition whitespace-nowrap">💰 升级建议</a>
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

<!-- ============ 打分规则说明（动态由 scoring_rules_panel_html 渲染） ============ -->
{SCORING_RULES_PANEL}

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

{AUDIT_PANEL}

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
      <button onclick="loadPlanAv6()" class="bg-emerald-600 hover:bg-emerald-700 text-white px-4 py-2 rounded-lg text-sm font-medium">🎯 一键加载方案 A v6（学术因子）</button>
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

  <!-- 📚 v6 学术指标（加载方案 A v6 后显示）-->
  <div id="v6-metrics-card" class="bg-gradient-to-r from-emerald-50 to-teal-50 border border-emerald-300 rounded-xl p-4 mb-4" style="display:none">
    <div class="flex items-center justify-between mb-3">
      <h3 class="text-sm font-bold text-emerald-900">📚 当前持仓的 v6 学术指标（基于过去 252 天 yfinance 真实日 K）</h3>
      <span class="text-xs text-emerald-700 bg-emerald-100 px-2 py-1 rounded">⚠️ backtest，非未来预测</span>
    </div>
    <div class="grid grid-cols-2 md:grid-cols-4 gap-3" id="v6-metrics-content"></div>
    <p class="text-xs text-emerald-800 mt-3"><strong>夏普 2.95 解读</strong>：远超巴菲特长期 0.76 / 标普 500 长期 0.4 — 是<strong>过去 1 年 AI 大涨</strong>叠加 Markowitz 在历史窗口内"完美选股"的结果，实际未来很难复现。仅用于<strong>不同方案对比</strong>。</p>
  </div>

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
  <h2 class="text-2xl font-bold text-slate-900 mb-2">📅 历史走势对比</h2>
  <p class="text-sm text-slate-600 mb-4">从 yfinance 实时拉历史价格 · 多股归一化对比（起点 = 100）· 自动算涨跌幅排行 + 相关性矩阵</p>

  <!-- 主题快捷按钮 -->
  <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4 mb-4">
    <h3 class="text-sm font-semibold text-slate-700 mb-3">🎨 快捷主题（一键加载同主题股）</h3>
    <div class="flex flex-wrap gap-2">
      <button onclick="loadHistoryByTheme('AI 算力核心', ['NVDA','TSM','AMD','AVGO'])" class="bg-violet-100 hover:bg-violet-200 text-violet-800 px-3 py-1.5 rounded text-xs font-medium">🔥 AI 算力核心 (4)</button>
      <button onclick="loadHistoryByTheme('AI 电力链', ['VRT','ETN','GEV','MTZ','PWR','VST'])" class="bg-amber-100 hover:bg-amber-200 text-amber-800 px-3 py-1.5 rounded text-xs font-medium">⚡ AI 电力链 (6)</button>
      <button onclick="loadHistoryByTheme('下一波稀缺资源', ['XYL','MP','CCJ','BWXT','RDDT'])" class="bg-emerald-100 hover:bg-emerald-200 text-emerald-800 px-3 py-1.5 rounded text-xs font-medium">💎 下一波稀缺资源 (5)</button>
      <button onclick="loadHistoryByTheme('数据中心承载层', ['EQIX','ORCL','LRCX'])" class="bg-blue-100 hover:bg-blue-200 text-blue-800 px-3 py-1.5 rounded text-xs font-medium">🏢 数据中心 (3)</button>
      <button onclick="loadHistoryByTheme('AI 应用层', ['GOOGL','NET','CDNS','CRWD'])" class="bg-cyan-100 hover:bg-cyan-200 text-cyan-800 px-3 py-1.5 rounded text-xs font-medium">📱 AI 应用层 (4)</button>
      <button onclick="loadHistoryByTheme('物理 AI', ['SYM','TSLA'])" class="bg-rose-100 hover:bg-rose-200 text-rose-800 px-3 py-1.5 rounded text-xs font-medium">🤖 物理 AI (2)</button>
      <button onclick="loadHistoryByTheme('SMR 核能', ['BWXT','OKLO','SMR','NNE','LEU','UUUU'])" class="bg-orange-100 hover:bg-orange-200 text-orange-800 px-3 py-1.5 rounded text-xs font-medium">☢️ 核能/SMR (6)</button>
      <button onclick="loadHistoryByTheme('防御对照', ['KO','MCD'])" class="bg-slate-100 hover:bg-slate-200 text-slate-800 px-3 py-1.5 rounded text-xs font-medium">🛡 防御对照 (2)</button>
      <button onclick="loadHistoryByTheme('AI 光通信链', ['MRVL','300308','300502','AVGO'])" class="bg-pink-100 hover:bg-pink-200 text-pink-800 px-3 py-1.5 rounded text-xs font-medium">🔗 AI 光通信链 (4)</button>
      <button onclick="loadHistoryByTheme('A 股 AI 核心', ['300308','300502','002230','688256','688041','688111'])" class="bg-red-100 hover:bg-red-200 text-red-800 px-3 py-1.5 rounded text-xs font-medium">🇨🇳 A 股 AI 核心 (6)</button>
      <button onclick="loadHistoryByTheme('港股 AI 平台', ['3690','9988','0700','0020'])" class="bg-yellow-100 hover:bg-yellow-200 text-yellow-800 px-3 py-1.5 rounded text-xs font-medium">🇭🇰 港股 AI (4)</button>
      <button onclick="loadHistoryByTheme('基准对照', ['SPY','QQQ'])" class="bg-indigo-100 hover:bg-indigo-200 text-indigo-800 px-3 py-1.5 rounded text-xs font-medium">📐 基准 SPY/QQQ (2)</button>
    </div>
  </div>

  <!-- 时间窗口 + 自定义选股 -->
  <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4 mb-4">
    <div class="flex flex-wrap items-center gap-3 mb-3">
      <h3 class="text-sm font-semibold text-slate-700">🎯 自定义对比</h3>
      <span class="text-xs text-slate-500 ml-auto mr-2">时间窗口:</span>
      <div class="flex gap-1" id="period-buttons">
        <button onclick="setPeriod('1mo')" data-period="1mo" class="period-btn px-2.5 py-1 rounded text-xs border border-slate-300 hover:bg-slate-100">30 天</button>
        <button onclick="setPeriod('3mo')" data-period="3mo" class="period-btn px-2.5 py-1 rounded text-xs border border-slate-300 hover:bg-slate-100 bg-violet-600 text-white border-violet-600">90 天</button>
        <button onclick="setPeriod('6mo')" data-period="6mo" class="period-btn px-2.5 py-1 rounded text-xs border border-slate-300 hover:bg-slate-100">180 天</button>
        <button onclick="setPeriod('1y')" data-period="1y" class="period-btn px-2.5 py-1 rounded text-xs border border-slate-300 hover:bg-slate-100">1 年</button>
        <button onclick="setPeriod('2y')" data-period="2y" class="period-btn px-2.5 py-1 rounded text-xs border border-slate-300 hover:bg-slate-100">2 年</button>
      </div>
    </div>
    <div class="flex items-center gap-3">
      <select id="history-codes" multiple size="5" class="px-3 py-1 border rounded text-sm flex-1 max-w-md"></select>
      <button onclick="loadHistoryCharts()" class="bg-violet-600 hover:bg-violet-700 text-white px-4 py-1.5 rounded text-sm">📊 加载所选</button>
      <span class="text-xs text-slate-500">按住 Cmd 多选 (最多 8 只)</span>
    </div>
  </div>

  <!-- 走势图 -->
  <div id="history-chart-card" class="bg-white rounded-xl shadow-sm border border-slate-200 p-4 mb-4" style="display:none">
    <h3 class="text-sm font-semibold text-slate-700 mb-3" id="history-chart-title">📈 归一化走势对比</h3>
    <div id="chart-history" style="height:420px"></div>
  </div>

  <!-- 涨跌幅排行表 -->
  <div id="history-ranking-card" class="bg-white rounded-xl shadow-sm border border-slate-200 p-4 mb-4" style="display:none">
    <h3 class="text-sm font-semibold text-slate-700 mb-3">🏆 涨跌幅排行</h3>
    <div id="history-ranking" class="overflow-x-auto"></div>
  </div>

  <!-- 相关性矩阵 -->
  <div id="history-corr-card" class="bg-white rounded-xl shadow-sm border border-slate-200 p-4 mb-4" style="display:none">
    <h3 class="text-sm font-semibold text-slate-700 mb-1">🔗 相关性矩阵 (Pearson)</h3>
    <p class="text-xs text-slate-500 mb-3">基于日收益率 · 1.0 = 完全同向 · 0 = 无关 · -1 = 完全反向。<strong>组合优化关心：相关性低的股票一起持仓能降低组合波动</strong></p>
    <div id="history-corr" class="overflow-x-auto"></div>
  </div>

  <!-- DuckDB 快照统计 -->
  <div class="bg-slate-50 rounded-xl border border-slate-200 p-4">
    <h3 class="text-sm font-semibold text-slate-700 mb-2">📦 DuckDB 本地快照库（长期回溯用）</h3>
    <p class="text-xs text-slate-600">每天 daily_refresh 自动写入。累积越久 = 你自己的历史数据库（不依赖 yfinance），未来可做严肃回测。</p>
    <p class="text-xs text-slate-500 mt-1 font-mono">stock_history.duckdb</p>
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

<!-- ============ 💰 升级建议 Tab ============ -->
<section id="upgrade" class="max-w-7xl mx-auto px-6 py-10" style="display:none">
  <div class="mb-6">
    <h2 class="text-3xl font-bold text-slate-900">💰 系统体检 + 升级建议</h2>
    <p class="text-sm text-slate-600 mt-2">先看现状（已实现 / 在做 / 缺口），再看升级方案。给同事看决策用。</p>
  </div>

  <!-- ════════ 系统响应能力 ════════ -->
  <div class="bg-gradient-to-br from-emerald-50 to-cyan-50 border-2 border-emerald-300 rounded-xl p-6 mb-8">
    <h3 class="text-2xl font-bold text-emerald-900 mb-4">📡 系统响应能力（实时性总览）</h3>
    <p class="text-sm text-slate-700 mb-4">数据从市场发生 → 落入系统的实际延迟。系统不是真"实时"，是 <strong>daily 批处理 + 盘中 30 分钟轮询</strong>。</p>

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
          <li>· 盘中异动告警（每 30 分钟）</li>
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

    <!-- 双引擎刷新 -->
    <div class="bg-white rounded-lg p-5 mb-4">
      <h4 class="font-bold text-slate-800 mb-3">🔁 双引擎自动刷新</h4>
      <table class="w-full text-sm">
        <thead><tr class="border-b-2 border-slate-200 text-left text-slate-700 bg-slate-50">
          <th class="py-2 px-2">引擎</th>
          <th class="px-2">频率</th>
          <th class="px-2">包含</th>
          <th class="px-2 text-right">耗时</th>
        </tr></thead>
        <tbody>
          <tr class="border-b">
            <td class="py-2 px-2"><span class="font-mono text-xs bg-indigo-100 px-2 py-0.5 rounded">daily_refresh.sh</span></td>
            <td class="px-2">每天 7:30 北京</td>
            <td class="px-2 text-xs">18 步全量：价格 / 13F / enrichment / v6 因子 / ML 堆叠 / v7 整合 / Markowitz / trade_delta / HTML</td>
            <td class="px-2 text-right text-slate-600">~20-40 分钟</td>
          </tr>
          <tr class="border-b">
            <td class="py-2 px-2"><span class="font-mono text-xs bg-emerald-100 px-2 py-0.5 rounded">intraday_refresh.sh</span></td>
            <td class="px-2">盘中每 30 分钟</td>
            <td class="px-2 text-xs">3 步轻量：价格 → 异动监控 → v7 重算（动量/反转/新闻/WSB → 排名 ±15 名告警）</td>
            <td class="px-2 text-right text-slate-600">~3-5 分钟</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- v7 因子组合（10 个） -->
    <div class="bg-white rounded-lg p-5 mb-4">
      <h4 class="font-bold text-slate-800 mb-3">🎯 v7 模型 — 10 个因子（5 经典 + 3 现代 + 2 可选）</h4>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-2 text-xs">
        <div class="bg-slate-50 rounded p-2"><strong>1. Piotroski F-Score</strong> · Stanford 2000</div>
        <div class="bg-slate-50 rounded p-2"><strong>2. 12-1 月动量</strong> · Jegadeesh-Titman JF 1993</div>
        <div class="bg-slate-50 rounded p-2"><strong>3. 1 月反转</strong> · Jegadeesh JF 1990</div>
        <div class="bg-slate-50 rounded p-2"><strong>4. PEAD 业绩加速度</strong> · Ball-Brown JAR 1968</div>
        <div class="bg-slate-50 rounded p-2"><strong>5. 分析师上修</strong> · Stickel JF 1991, Womack JF 1996</div>
        <div class="bg-cyan-50 rounded p-2 border border-cyan-300"><strong>6. 新闻事件评分</strong> · MacKinlay 1997（现代）</div>
        <div class="bg-cyan-50 rounded p-2 border border-cyan-300"><strong>7. WSB 社交情感</strong> · Bradley JFE 2024（现代）</div>
        <div class="bg-cyan-50 rounded p-2 border border-cyan-300"><strong>8. 业绩 surprise + EPS 预期</strong> · Bernard-Thomas 1989</div>
        <div class="bg-violet-50 rounded p-2 border border-violet-200"><strong>9. SEC 10-K LM 情感</strong>（可选）· Loughran-McDonald JF 2011</div>
        <div class="bg-violet-50 rounded p-2 border border-violet-200"><strong>10. ML 预测 alpha</strong>（可选）· Gu-Kelly-Xiu RFS 2020</div>
      </div>
      <p class="text-xs text-slate-600 mt-3">合成方法: 横截面 z-score 等权（DeMiguel 2009 RFS 论文证明 1/N 不输 Markowitz 优化权重）</p>
    </div>

    <!-- 实测验证 -->
    <div class="bg-slate-900 text-white rounded-lg p-5">
      <h4 class="text-lg font-bold mb-3">📈 全程进步实测（2025-12-31 → 2026-05-09 反向验证）</h4>
      <table class="w-full text-sm">
        <thead><tr class="border-b border-slate-700 text-left text-slate-300">
          <th class="py-2 px-2">版本</th>
          <th class="px-2">因子数</th>
          <th class="px-2">YTD 收益</th>
          <th class="px-2">Alpha vs SPY</th>
          <th class="px-2">胜率</th>
          <th class="px-2">特点</th>
        </tr></thead>
        <tbody>
          <tr class="border-b border-slate-800"><td class="py-2 px-2">v1 我编的</td><td class="px-2">4</td><td class="px-2">-</td><td class="px-2">-</td><td class="px-2">-</td><td class="px-2 text-rose-300">主观打分</td></tr>
          <tr class="border-b border-slate-800"><td class="py-2 px-2">v6 学术经典</td><td class="px-2">5</td><td class="px-2">+48.5%</td><td class="px-2 text-emerald-300">+40.1%</td><td class="px-2">72%</td><td class="px-2 text-slate-300">顶刊论文</td></tr>
          <tr class="bg-emerald-900/40"><td class="py-2 px-2 font-bold">v7 全栈整合</td><td class="px-2 font-bold">10</td><td class="px-2 font-bold text-emerald-300">+51.5%</td><td class="px-2 font-bold text-emerald-300">+43.8%</td><td class="px-2 font-bold">79%</td><td class="px-2 text-emerald-200">+ 现代另类数据</td></tr>
        </tbody>
      </table>
      <p class="text-xs text-slate-400 mt-3">⚠️ 这是 backtest 不是承诺；2026 Q1-Q2 是 AI/半导体强势期，模型天然 work；熊市可能跑输 SPY 5-15%（walk-forward 实测）</p>
    </div>
  </div>

  <!-- ════════ 系统体检报告 ════════ -->
  <div class="bg-gradient-to-br from-indigo-50 to-blue-50 border-2 border-indigo-300 rounded-xl p-6 mb-8">
    <h3 class="text-2xl font-bold text-indigo-900 mb-4">🩺 系统体检报告</h3>

    <!-- ✅ 做得好的 -->
    <div class="mb-6">
      <h4 class="text-lg font-bold text-emerald-700 mb-3">✅ 已经做得好的（6 块基础坚实）</h4>
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
          <p class="text-xs text-slate-600">仓库分层 (core/adapters/jobs/api) · 12 步 daily_refresh · 9 tab 仪表盘 · METHODOLOGY/MODEL_CARD · GitHub</p>
        </div>
      </div>
    </div>

    <!-- 🟡 在做但未完成 -->
    <div class="mb-6">
      <h4 class="text-lg font-bold text-amber-700 mb-3">🟡 在做但未完成（等数据/时间）</h4>
      <ul class="space-y-2 text-sm">
        <li class="bg-white rounded p-3 border border-amber-200"><strong>因子 IC 验证</strong> — 框架就位，需累积 30+ 天历史才有意义</li>
        <li class="bg-white rounded p-3 border border-amber-200"><strong>每日优选 hit rate 真实回测</strong> — picks 才积累 1 天，1 个月后才能验证 v6 模型准不准</li>
        <li class="bg-white rounded p-3 border border-amber-200"><strong>monthly_letter 月度信件</strong> — 框架就绪，未真正生成第一封</li>
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
          <tr class="border-b border-rose-100"><td class="py-2 px-2"><span class="bg-rose-500 text-white px-2 py-0.5 rounded text-xs">🔴 真痛点</span></td><td class="px-2 font-medium">数据缓存层（重复请求多）</td><td class="px-2 text-slate-600">SQLite 缓存（免费，1天工作量）</td></tr>
          <tr class="border-b border-rose-100"><td class="py-2 px-2"><span class="bg-rose-500 text-white px-2 py-0.5 rounded text-xs">🔴 真痛点</span></td><td class="px-2 font-medium">告警系统（只有 macOS notify）</td><td class="px-2 text-slate-600">邮件/微信推送（免费）</td></tr>
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
      <h4 class="text-lg font-bold text-indigo-900 mb-3">📅 我建议的下一步（按顺序）</h4>
      <ol class="space-y-2 text-sm">
        <li class="flex gap-3"><span class="bg-indigo-100 text-indigo-700 font-bold rounded-full w-7 h-7 flex items-center justify-center flex-shrink-0">1</span><div><strong>本周</strong>：用免费 FMP 跑 1 周，看 NVDA/AAPL/GOOGL 大盘股能不能给出新洞察</div></li>
        <li class="flex gap-3"><span class="bg-indigo-100 text-indigo-700 font-bold rounded-full w-7 h-7 flex items-center justify-center flex-shrink-0">2</span><div><strong>下周</strong>：注册 Tushare Pro（¥200）+ 加 SQLite 缓存层（免费，1 天工作量）</div></li>
        <li class="flex gap-3"><span class="bg-indigo-100 text-indigo-700 font-bold rounded-full w-7 h-7 flex items-center justify-center flex-shrink-0">3</span><div><strong>下月</strong>：积累 1 个月 picks 历史，跑真实 hit rate 回测，看 v6 模型到底准不准</div></li>
        <li class="flex gap-3"><span class="bg-indigo-100 text-indigo-700 font-bold rounded-full w-7 h-7 flex items-center justify-center flex-shrink-0">4</span><div><strong>下季度</strong>：根据回测决定要不要持续；要就上 FMP Starter / Polygon</div></li>
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
        <p class="text-sm text-slate-700 mb-3"><strong>解决 watchlist 中 30%+ A股/港股的数据脆弱问题。</strong></p>
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
        <p class="text-xs text-violet-700 mt-1"><strong>建议：</strong>watchlist 里这类股 &lt; 5 只，性价比一般</p>
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
          <td class="text-slate-400">这类股 watchlist 里 &lt; 5 只</td>
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
    <p class="text-slate-500">数据源：飞书「股票研究 Watchlist」表 · WebSearch 抓取的最新公司财报 · 数据更新时间 {UPDATE_TIME}</p>
  </div>
</footer>

<script>
const RECORDS = {RECORDS_JSON};
const PICKS = {PICKS_JSON};
const SIMULATION = {SIMULATION_JSON};
const RISK_METRICS = {RISK_METRICS_JSON};
const TRACK_13F = {TRACK_13F_JSON};
const HISTORY_DATA = {HISTORY_DATA_JSON};
const OPTIMIZATION = {OPTIMIZATION_JSON};
const PLAN_A_V6 = {PLAN_A_V6_JSON};

// ============ Tab 切换框架 ============
const TAB_SECTIONS = {
  overview: ["hero", "thesis", "evolution", "scarce", "events", "hundred-x"],
  portfolio: ["portfolio"],
  picks: ["scoring-rules", "picks-review"],
  audit: ["audit-panel"],
  valuation: ["valuation"],
  themes: ["distribution", "theme-groups"],
  history: ["history"],
  professional: ["professional"],
  upgrade: ["upgrade"],
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
        <td class="px-3 py-2">${name}</td>
        <td class="px-3 py-2 text-xs text-slate-600">${industry}</td>
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
function clearHoldings() {
  if (!confirm("确定清空所有持仓？")) return;
  localStorage.removeItem(STORAGE_KEY);
  renderPortfolio();
}

// ============ 一键加载方案 A v6（学术因子 + Markowitz 客观仓位） ============
function loadPlanAv6() {
  if (!PLAN_A_V6 || !PLAN_A_V6.plan_v5 || PLAN_A_V6.plan_v5.length === 0) {
    alert("还没有方案 A v6 数据，请先跑：python3 build_plan_a_v5.py");
    return;
  }
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
  saveHoldings(holdings);
  const metrics = PLAN_A_V6.portfolio_metrics || {};
  // 渲染持久指标卡片
  renderV6Metrics(metrics);
  alert(`✅ 已加载方案 A v6 持仓（${holdings.length} 只）\n` +
        `总金额: ¥${Math.round(totalAmount).toLocaleString()}\n\n` +
        `📚 学术因子选股 + Markowitz 优化:\n` +
        `  · 5 因子（Piotroski + 动量 + 反转 + PEAD + 分析师）\n` +
        `  · 年化 Sharpe ${metrics.annual_sharpe || '?'}\n` +
        `  · 年化收益 ${metrics.annual_return_pct || '?'}%\n` +
        `  · 年化波动 ${metrics.annual_vol_pct || '?'}%\n\n` +
        `⚠️ Sharpe 是 backtest 不是承诺；熊市可能跑输 SPY 5-15%`);
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
      <div class="text-[10px] text-slate-500">基于过去 252 天外推</div>
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

// 加载时如果已有方案 A v6 数据 + 持仓，就显示指标
window.addEventListener("DOMContentLoaded", () => {
  if (PLAN_A_V6 && PLAN_A_V6.portfolio_metrics && loadHoldings().some(h => h._plan_a_v6)) {
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

// ============ 历史 Tab ============
let historyInited = false;
let HISTORY_PERIOD = "3mo";  // 默认 90 天
const PERIOD_LABEL = { "1mo": "30 天", "3mo": "90 天", "6mo": "180 天", "1y": "1 年", "2y": "2 年" };

function initHistorySelect() {
  if (historyInited) return;
  const sel = document.getElementById("history-codes");
  if (!sel) return;
  // 用 history_data.json 里实际有数据的股票（保证选了一定能加载）
  const availableCodes = HISTORY_DATA && HISTORY_DATA.tickers ? Object.keys(HISTORY_DATA.tickers) : [];
  const opts = availableCodes.map(code => {
    const r = RECORDS.find(x => x.code === code);
    const name = (r && r.name) || (HISTORY_DATA.tickers[code].name) || code;
    return `<option value="${code}">${name} (${code})</option>`;
  }).sort();
  sel.innerHTML = opts.join("");
  historyInited = true;
}

function setPeriod(p) {
  HISTORY_PERIOD = p;
  document.querySelectorAll(".period-btn").forEach(b => {
    if (b.dataset.period === p) {
      b.classList.add("bg-violet-600", "text-white", "border-violet-600");
    } else {
      b.classList.remove("bg-violet-600", "text-white", "border-violet-600");
    }
  });
  // 如果有选中股票，自动重新加载
  const sel = document.getElementById("history-codes");
  if (sel && sel.selectedOptions.length > 0) loadHistoryCharts();
}

async function loadHistoryByTheme(themeName, codes) {
  // 在选择框里高亮选中（视觉反馈）
  const sel = document.getElementById("history-codes");
  if (sel) {
    Array.from(sel.options).forEach(o => o.selected = codes.includes(o.value));
  }
  await loadHistoryCharts(codes, themeName);
}

// 周期 → 截取最近 N 天
const PERIOD_DAYS = { "1mo": 22, "3mo": 65, "6mo": 130, "1y": 252, "2y": 504 };

async function loadHistoryCharts(presetCodes, themeName) {
  let codes = presetCodes;
  if (!codes) {
    const sel = document.getElementById("history-codes");
    codes = Array.from(sel.selectedOptions).slice(0, 8).map(o => o.value);
  }
  if (!codes || codes.length === 0) { alert("请至少选 1 只股票"); return; }

  // 显示卡片 + loading
  document.getElementById("history-chart-card").style.display = "";
  document.getElementById("history-chart-title").textContent =
    `📈 归一化走势对比 · ${themeName ? "[" + themeName + "] " : ""}${PERIOD_LABEL[HISTORY_PERIOD]} · ${codes.length} 只`;

  if (!HISTORY_DATA || !HISTORY_DATA.tickers) {
    document.getElementById("chart-history").innerHTML = '<div class="text-center text-rose-500 py-12">history_data.json 未生成 — 请先跑 <code>python3 _fetch_history_for_dashboard.py</code></div>';
    return;
  }

  // 从本地 history_data.json 读数据（无 CORS）
  const days = PERIOD_DAYS[HISTORY_PERIOD] || 65;
  const datasets = [];
  const missing = [];
  for (const code of codes) {
    const td = HISTORY_DATA.tickers[code];
    if (!td || !td.ts || td.ts.length === 0) {
      missing.push(code);
      continue;
    }
    // 取最近 N 天
    const ts = td.ts.slice(-days);
    const closes = td.close.slice(-days);
    const r = RECORDS.find(x => x.code === code);
    // 计算日收益率
    const returns = [];
    for (let i = 1; i < closes.length; i++) {
      if (closes[i] != null && closes[i-1] != null && closes[i-1] > 0) {
        returns.push((closes[i] - closes[i-1]) / closes[i-1]);
      } else {
        returns.push(null);
      }
    }
    datasets.push({ code, name: (r && r.name) || td.name || code, ts, closes, returns });
  }

  if (datasets.length === 0) {
    document.getElementById("chart-history").innerHTML =
      `<div class="text-center text-rose-500 py-12">所选股票本地都没有历史数据。<br>缺失：${missing.join(", ")}<br>请运行：<code>python3 _fetch_history_for_dashboard.py</code></div>`;
    return;
  }
  if (missing.length > 0) {
    console.warn("History data missing for:", missing);
  }

  // 1. 渲染走势图
  const series = datasets.map(d => {
    const first = d.closes.find(c => c != null);
    const norm = d.closes.map(c => c == null ? null : (c / first * 100));
    return { name: d.name, type: "line", smooth: true, data: d.ts.map((t, i) => [t, norm[i]]) };
  });
  document.getElementById("chart-history").innerHTML = "";
  echarts.init(document.getElementById("chart-history")).setOption({
    tooltip: { trigger: "axis" },
    legend: { top: 0, type: "scroll" },
    grid: { left: 50, right: 30, top: 50, bottom: 40 },
    xAxis: { type: "time" },
    yAxis: { type: "value", name: "归一化 (起点=100)" },
    series,
  });

  // 2. 渲染涨跌幅排行
  const ranking = datasets.map(d => {
    const validCloses = d.closes.filter(c => c != null);
    const first = validCloses[0];
    const last = validCloses[validCloses.length - 1];
    const pct = first && last ? (last - first) / first * 100 : null;
    // 最大回撤
    let peak = first, maxDD = 0;
    for (const c of validCloses) {
      if (c > peak) peak = c;
      const dd = (c - peak) / peak * 100;
      if (dd < maxDD) maxDD = dd;
    }
    // 年化波动率 (returns 标准差 × sqrt(252))
    const validReturns = d.returns.filter(r => r != null);
    const mean = validReturns.reduce((a,b) => a+b, 0) / validReturns.length;
    const variance = validReturns.reduce((a,b) => a + (b-mean)*(b-mean), 0) / validReturns.length;
    const annualVol = Math.sqrt(variance * 252) * 100;
    return { name: d.name, code: d.code, pct, maxDD, annualVol, first, last };
  }).sort((a,b) => (b.pct || -999) - (a.pct || -999));

  document.getElementById("history-ranking-card").style.display = "";
  document.getElementById("history-ranking").innerHTML = `
    <table class="w-full text-sm">
      <thead class="bg-slate-100 text-slate-700">
        <tr>
          <th class="px-3 py-2 text-left">#</th>
          <th class="px-3 py-2 text-left">股票</th>
          <th class="px-3 py-2 text-right">起点</th>
          <th class="px-3 py-2 text-right">终点</th>
          <th class="px-3 py-2 text-right">涨跌幅</th>
          <th class="px-3 py-2 text-right">最大回撤</th>
          <th class="px-3 py-2 text-right">年化波动率</th>
        </tr>
      </thead>
      <tbody>
        ${ranking.map((r, i) => `
          <tr class="border-t border-slate-100 hover:bg-slate-50">
            <td class="px-3 py-2">${i+1}</td>
            <td class="px-3 py-2 font-medium">${r.name} <span class="text-xs text-slate-500 font-mono">${r.code}</span></td>
            <td class="px-3 py-2 text-right font-mono">${r.first ? r.first.toFixed(2) : '-'}</td>
            <td class="px-3 py-2 text-right font-mono">${r.last ? r.last.toFixed(2) : '-'}</td>
            <td class="px-3 py-2 text-right font-mono ${r.pct >= 0 ? 'text-emerald-600' : 'text-rose-600'} font-bold">${r.pct >= 0 ? '+' : ''}${r.pct ? r.pct.toFixed(1) : '-'}%</td>
            <td class="px-3 py-2 text-right font-mono text-rose-500">${r.maxDD.toFixed(1)}%</td>
            <td class="px-3 py-2 text-right font-mono">${r.annualVol.toFixed(1)}%</td>
          </tr>`).join("")}
      </tbody>
    </table>`;

  // 3. 渲染相关性矩阵 (≥2 只时)
  if (datasets.length >= 2) {
    document.getElementById("history-corr-card").style.display = "";
    // 用最短的 returns 长度对齐
    const minLen = Math.min(...datasets.map(d => d.returns.filter(r => r != null).length));
    const aligned = datasets.map(d => d.returns.filter(r => r != null).slice(-minLen));

    function pearson(x, y) {
      const n = x.length;
      const mx = x.reduce((a,b)=>a+b,0) / n;
      const my = y.reduce((a,b)=>a+b,0) / n;
      let num = 0, dx = 0, dy = 0;
      for (let i = 0; i < n; i++) {
        num += (x[i] - mx) * (y[i] - my);
        dx += (x[i] - mx) ** 2;
        dy += (y[i] - my) ** 2;
      }
      const denom = Math.sqrt(dx * dy);
      return denom > 0 ? num / denom : 0;
    }

    const N = datasets.length;
    const matrix = [];
    for (let i = 0; i < N; i++) {
      const row = [];
      for (let j = 0; j < N; j++) {
        row.push(i === j ? 1.0 : pearson(aligned[i], aligned[j]));
      }
      matrix.push(row);
    }

    const corrColor = (v) => {
      // 1.0 → 浓绿; 0 → 白; -1 → 浓红
      if (v > 0.7) return "bg-emerald-300";
      if (v > 0.5) return "bg-emerald-200";
      if (v > 0.3) return "bg-emerald-100";
      if (v > 0) return "bg-slate-50";
      if (v > -0.3) return "bg-rose-50";
      if (v > -0.5) return "bg-rose-100";
      return "bg-rose-200";
    };

    let corrHtml = '<table class="text-xs"><thead><tr><th class="px-2 py-1"></th>';
    for (const d of datasets) corrHtml += `<th class="px-2 py-1 font-mono">${d.code}</th>`;
    corrHtml += '</tr></thead><tbody>';
    for (let i = 0; i < N; i++) {
      corrHtml += `<tr><th class="px-2 py-1 text-left font-mono">${datasets[i].code}</th>`;
      for (let j = 0; j < N; j++) {
        corrHtml += `<td class="px-2 py-1 text-center font-mono ${corrColor(matrix[i][j])}">${matrix[i][j].toFixed(2)}</td>`;
      }
      corrHtml += '</tr>';
    }
    corrHtml += '</tbody></table>';
    document.getElementById("history-corr").innerHTML = corrHtml;
  } else {
    document.getElementById("history-corr-card").style.display = "none";
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


def load_calibration_snapshot():
    """读最新的因子权重校准（stock_research.jobs.calibrate_pick_weights 写出）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
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
    """渲染「每日优选 · 打分规则」面板。

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
            每日优选 · 打分规则（透明 + IC 实证）
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
          <li><strong>只用 watchlist 内 37 只</strong>：不是从全市场万只里挑，覆盖范围有限</li>
        </ul>
      </div>
    </div>
  </details>
</section>'''


def load_audit_snapshot():
    """读最新一次 picks 反向审查快照（JSON 文件路径）。"""
    audit_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
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
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
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
    plan_a_v6 = _load_json("plan_a_v5.json")
    history_data = _load_json("history_data.json")
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

    # 反向审查面板（picks_audit 快照）—— 双数据源：JSON 文件 + DuckDB
    audit_snap_json = load_audit_snapshot()
    audit_snap_db = load_audit_snapshot_from_db()

    panel_json_inner = audit_panel_html(audit_snap_json)
    panel_db_inner = (
        audit_panel_html(audit_snap_db) if audit_snap_db else
        '<section class="max-w-7xl mx-auto px-6 py-10 bg-rose-50 rounded-2xl my-6">'
        '<p class="text-rose-700">⚠️ DuckDB <code>snapshots</code> 表中暂无 picks_audit 数据。</p>'
        '</section>'
    )
    panel_json_inner = panel_json_inner.replace('id="audit-panel"', 'id="audit-panel-json-section"', 1)
    panel_db_inner = panel_db_inner.replace('id="audit-panel"', 'id="audit-panel-db-section"', 1)

    ts_json = (audit_snap_json or {}).get("ts", "—")[:16] if audit_snap_json else "—"
    ts_db = (audit_snap_db or {}).get("ts", "—")[:16] if audit_snap_db else "—"

    audit_panel_combined = f'''
<div id="audit-panel">
  <div class="max-w-7xl mx-auto px-6 pt-6">
    <div class="flex flex-wrap items-center gap-3 bg-amber-50 border border-amber-300 rounded-lg p-3">
      <span class="text-sm font-semibold text-amber-900">🔬 数据源对比模式</span>
      <span class="text-xs text-amber-800">验证 DuckDB 迁移；下方面板可切换数据来源</span>
      <div class="ml-auto flex items-center gap-2">
        <span class="text-xs text-slate-600">当前：</span>
        <span id="audit-source-label" class="text-xs font-mono px-2 py-0.5 bg-blue-100 text-blue-800 rounded">JSON 文件 · {ts_json}</span>
        <button onclick="toggleAuditSource()" class="text-xs px-3 py-1 bg-amber-600 hover:bg-amber-700 text-white rounded font-medium shadow-sm">
          切换 ⇄
        </button>
      </div>
    </div>
  </div>
  <div id="audit-panel-json-wrap" data-source="json" data-ts="{ts_json}">{panel_json_inner}</div>
  <div id="audit-panel-db-wrap" data-source="duckdb" data-ts="{ts_db}" style="display:none">{panel_db_inner}</div>
</div>
<script>
function toggleAuditSource() {{
  const j = document.getElementById('audit-panel-json-wrap');
  const d = document.getElementById('audit-panel-db-wrap');
  const lbl = document.getElementById('audit-source-label');
  if (j.style.display === 'none') {{
    j.style.display = '';
    d.style.display = 'none';
    lbl.textContent = 'JSON 文件 · ' + (j.dataset.ts || '—');
    lbl.className = 'text-xs font-mono px-2 py-0.5 bg-blue-100 text-blue-800 rounded';
  }} else {{
    j.style.display = 'none';
    d.style.display = '';
    lbl.textContent = 'DuckDB · ' + (d.dataset.ts || '—');
    lbl.className = 'text-xs font-mono px-2 py-0.5 bg-emerald-100 text-emerald-800 rounded';
  }}
}}
</script>
'''
    html = html.replace("{AUDIT_PANEL}", audit_panel_combined)

    # 打分规则面板（动态读 factor_weights.json，缺失则 fallback 到「未实证」版本）
    calib_snap = load_calibration_snapshot()
    html = html.replace("{SCORING_RULES_PANEL}", scoring_rules_panel_html(calib_snap))
    if audit_snap_json:
        n_picks = audit_snap_json.get("picks_today_count", 0)
        print(f"  反向审查快照已加载 [JSON]（{n_picks} 只 picks @ {ts_json}）")
    if audit_snap_db:
        n_picks_db = audit_snap_db.get("picks_today_count", 0)
        print(f"  反向审查快照已加载 [DuckDB]（{n_picks_db} 只 picks @ {ts_db}）")

    html = html.replace("{RECORDS_JSON}", json.dumps(records, ensure_ascii=False))
    html = html.replace("{PICKS_JSON}", json.dumps(picks, ensure_ascii=False))
    html = html.replace("{SIMULATION_JSON}", json.dumps(simulation, ensure_ascii=False))
    html = html.replace("{RISK_METRICS_JSON}", json.dumps(risk_metrics, ensure_ascii=False))
    html = html.replace("{TRACK_13F_JSON}", json.dumps(track_13f, ensure_ascii=False))
    html = html.replace("{OPTIMIZATION_JSON}", json.dumps(optimization, ensure_ascii=False))
    html = html.replace("{PLAN_A_V6_JSON}", json.dumps(plan_a_v6, ensure_ascii=False))
    html = html.replace("{HISTORY_DATA_JSON}", json.dumps(history_data, ensure_ascii=False))

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[3/3] 已生成：{OUTPUT}")
    print(f"\n用浏览器打开：file://{OUTPUT}")


if __name__ == "__main__":
    build()
