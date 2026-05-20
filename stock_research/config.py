"""集中配置：路径、表 ID、API 端点、追踪机构列表。

凭证一律走环境变量；缺失时回退到本地默认（仅本地开发）。
所有路径相对 BASE_DIR，便于将来打包成容器或 Web 服务时整体迁移。
"""
from __future__ import annotations
import os
from pathlib import Path
import json


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


# ─────────── 路径 ───────────
# 默认指向 stock_research 包的父目录（即 StockAssistant 项目根），
# 与 stock_db.py 的脚本相对路径保持一致，避免双 DuckDB 分裂。
_DEFAULT_BASE = Path(__file__).resolve().parent.parent
BASE_DIR = Path(os.environ.get("STOCK_RESEARCH_BASE", str(_DEFAULT_BASE)))
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
SEC_13F_DIR = SNAPSHOT_DIR / "13f"
ENRICH_DIR = SNAPSHOT_DIR / "enrich"
AUDIT_DIR = SNAPSHOT_DIR / "audit"
DUCKDB_PATH = Path(os.environ.get("STOCK_DB_PATH", str(BASE_DIR / "stock_history_v2.duckdb")))

for d in (DATA_DIR, SNAPSHOT_DIR, SEC_13F_DIR, ENRICH_DIR, AUDIT_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ─────────── 第三方 API ───────────
SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "stock-research-toolkit your-email@example.com",  # 部署时改成自己的联系方式
)
SEC_RATE_LIMIT_DELAY = 0.12  # SEC 限流 10 req/sec

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
FINNHUB_BASE = "https://finnhub.io/api/v1"

# Financial Modeling Prep — 财报 + DCF + 分析师预期
# 免费注册：https://site.financialmodelingprep.com/developer/docs（250 calls/day）
FMP_API_KEY = os.environ.get("FMP_API_KEY")
FMP_BASE = "https://financialmodelingprep.com/api/v3"

# ─────────── 生产市场开关 ───────────
# A 股当前需要市场内 IC 校准权重才允许生产推荐。
# 默认 auto：只要 data/calibrated_factor_weights.json 有效，就自动启用；
# 也可用 A_SHARE_PRODUCTION_MODE=off/on 强制关闭/开启。


def _valid_a_share_calibration(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    market = str(data.get("market") or data.get("universe") or "").strip().lower()
    if market and market not in {"a_share", "ashare", "cn", "china", "v6_cn", "a股"}:
        return False
    validated = data.get("validated") is True or str(data.get("validation_status") or "").lower() in {
        "pass", "passed", "valid", "validated",
    }
    weights = data.get("weights")
    if not validated or not isinstance(weights, dict):
        return False
    try:
        total = sum(float(v) for v in weights.values())
    except Exception:
        return False
    return abs(total - 1.0) <= 1e-4


A_SHARE_PRODUCTION_MODE = os.environ.get("A_SHARE_PRODUCTION_MODE", "auto").strip().lower()
A_SHARE_CALIBRATION_PATH = DATA_DIR / "calibrated_factor_weights.json"
if "A_SHARE_PRODUCTION_ENABLED" in os.environ:
    A_SHARE_PRODUCTION_ENABLED = _env_flag("A_SHARE_PRODUCTION_ENABLED", "0")
elif A_SHARE_PRODUCTION_MODE in {"off", "0", "false", "no"}:
    A_SHARE_PRODUCTION_ENABLED = False
elif A_SHARE_PRODUCTION_MODE in {"on", "1", "true", "yes"}:
    A_SHARE_PRODUCTION_ENABLED = True
else:
    A_SHARE_PRODUCTION_ENABLED = _valid_a_share_calibration(A_SHARE_CALIBRATION_PATH)

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

# 2026-05-11 PM 第二轮:删 Fields 类(飞书 Bitable 字段中文名映射).
# 飞书 100% 退役后,DuckDB 列名是 source of truth — code/name/market/credibility/
# verification/info_breakdown 等,直接用 SQL 列名,不再需要中文 → 英文映射.
