"""集中配置：路径、表 ID、API 端点、追踪机构列表。

凭证一律走环境变量；缺失时回退到本地默认（仅本地开发）。
所有路径相对 BASE_DIR，便于将来打包成容器或 Web 服务时整体迁移。
"""
from __future__ import annotations
import os
from pathlib import Path

# ─────────── 路径 ───────────
BASE_DIR = Path(os.environ.get("STOCK_RESEARCH_BASE", os.path.expanduser("~/.hermes/scripts")))
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
SEC_13F_DIR = SNAPSHOT_DIR / "13f"
ENRICH_DIR = SNAPSHOT_DIR / "enrich"
AUDIT_DIR = SNAPSHOT_DIR / "audit"
DUCKDB_PATH = BASE_DIR / "stock_history.duckdb"

for d in (DATA_DIR, SNAPSHOT_DIR, SEC_13F_DIR, ENRICH_DIR, AUDIT_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ─────────── 飞书 ───────────
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET")
FEISHU_BASE_TOKEN = os.environ.get("FEISHU_BASE_TOKEN", "")

# 表 ID 走环境变量；首次部署时，从飞书表 URL 里取 ?table=tbl... 之后的部分填到 .env
WATCHLIST_TABLE_ID = os.environ.get("FEISHU_WATCHLIST_TABLE_ID", "")
DAILY_PICKS_TABLE_ID = os.environ.get("FEISHU_PICKS_TABLE_ID", "")
EVENTS_TABLE_ID = os.environ.get("FEISHU_EVENTS_TABLE_ID", "")
PEERS_TABLE_ID = os.environ.get("FEISHU_PEERS_TABLE_ID", "")

FEISHU_BITABLE_API = "https://open.feishu.cn/open-apis/bitable/v1"

# ─────────── 第三方 API ───────────
SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "stock-research-toolkit your-email@example.com",  # 部署时改成自己的联系方式
)
SEC_RATE_LIMIT_DELAY = 0.12  # SEC 限流 10 req/sec

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
FINNHUB_BASE = "https://finnhub.io/api/v1"

# ─────────── 跟踪机构（13F 监控）───────────
INVESTORS_13F = {
    "Berkshire Hathaway (Buffett)":   "0001067983",
    "Bridgewater Associates (Dalio)": "0001350694",
    "Norges Bank (挪威主权基金)":      "0001262992",
    "Pershing Square (Ackman)":        "0001336528",
    "Tiger Global Management":         "0001167483",
    "Renaissance Technologies":        "0001037389",
    "Coatue Management":               "0001135730",
    "Soros Fund Management":           "0001029160",
    "Third Point (Loeb)":              "0001040273",
    "Greenlight Capital (Einhorn)":    "0001079114",
    "Scion Asset Mgmt (Burry)":        "0001649339",
}

# ─────────── CUSIP → Ticker 高频映射 ───────────
CUSIP_TO_TICKER = {
    "67066G104": "NVDA",
    "037833100": "AAPL",
    "02079K305": "GOOGL",
    "02079K107": "GOOG",
    "023135106": "AMZN",
    "30303M102": "META",
    "594918104": "MSFT",
    "11135F101": "AVGO",
    "874039100": "TSM",
    "88160R101": "TSLA",
    "92826C839": "VST",
    "G393261078": "VRT",
    "26210C104": "EQIX",
    "65339F101": "NET",
    "14149Y108": "CCJ",
    "98419M100": "XYL",
    "55301A109": "MP",
    "05538Y101": "BWXT",
    "75691K104": "RDDT",
    "68389X105": "ORCL",
    "191216100": "KO",
    "580135101": "MCD",
}

ISSUER_TO_TICKER_KEYWORDS = [
    ("nvidia", "NVDA"),
    ("apple inc", "AAPL"),
    ("alphabet inc cl a", "GOOGL"),
    ("alphabet inc cl c", "GOOG"),
    ("alphabet inc", "GOOGL"),
    ("amazon.com", "AMZN"),
    ("meta platforms", "META"),
    ("microsoft", "MSFT"),
    ("broadcom", "AVGO"),
    ("taiwan semi", "TSM"),
    ("tesla", "TSLA"),
    ("vistra", "VST"),
    ("vertiv", "VRT"),
    ("equinix", "EQIX"),
    ("cloudflare", "NET"),
    ("cameco", "CCJ"),
    ("xylem", "XYL"),
    ("mp materials", "MP"),
    ("bwx tech", "BWXT"),
    ("reddit", "RDDT"),
    ("oracle", "ORCL"),
]

# ─────────── 数据可信度等级 ───────────
CREDIBILITY_LEVELS = {
    "HIGH": "🟢 高（多权威源一致）",
    "MEDIUM": "🟡 中（权威媒体单源）",
    "LOW": "🔴 低（仅二手聚合）",
    "CONFLICT": "⚠️ 冲突（多源数据不一致）",
}

# ─────────── 飞书表字段名 ───────────
class Fields:
    NAME = "股票名称"
    CODE = "代码"
    MARKET = "市场"
    BUSINESS = "主营业务"
    INDUSTRY = "行业归类"
    AI_LEVEL = "AI关联度"
    AI_LOGIC = "AI关联逻辑"
    MARKET_CAP = "当前市值"
    QUARTERLY = "最近季度业绩"
    CONCLUSION = "研究结论"
    RISK = "关键风险"
    PEERS = "可比公司"
    CADENCE = "跟踪节奏"
    STATUS = "研究状态"
    DATA_SOURCE = "数据来源"
    CREATED = "录入日期"
    UPDATED = "最近更新"
    SNAPSHOT_DATE = "数据快照时间"
    CREDIBILITY = "数据可信度"
    INFO_COMPOSITION = "信息构成"
    DUAL_SOURCE = "双源验证"
    PRICE = "最新价格"
    YTD_PCT = "YTD涨幅%"
    ONE_YEAR_PCT = "一年涨幅%"
    FORWARD_PE = "远期PE"
    YF_MARKET_CAP = "yf市值"
    PRICE_UPDATED = "价格更新时间"
    ONE_WEEK_PCT = "1周涨幅%"
    ONE_MONTH_PCT = "1月涨幅%"
    PEG = "PEG"
    EARNINGS_GROWTH_PCT = "利润增速%"
    INSTITUTIONAL_13F = "13F机构信号"
