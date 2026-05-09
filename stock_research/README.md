# stock_research

可拆解、Web 部署友好的股票研究系统。

## 设计原则

```
core/      纯数据获取（无 I/O 副作用，函数返回 JSON 可序列化 dict/list）
adapters/  I/O 适配层（飞书、DuckDB、文件）
jobs/      编排层（CLI + cron 入口）
config.py  集中常量（路径、表 ID、追踪机构、CUSIP→ticker）
```

未来部署成 Web 服务时，FastAPI 路由直接 `from stock_research.core.edgar import get_investor_changes`，零改造。

## 数据源

| 源 | 用途 | 凭证 | 库 |
|---|---|---|---|
| SEC EDGAR | 13F 持仓真实变动（替代抖音截图） | 无（仅 User-Agent） | requests |
| akshare | A 股 / 港股财报 + 资金流 | 无 | akshare |
| Google Trends | 搜索热度（情绪面） | 无（限流） | pytrends |
| Finnhub | 美股新闻 + 内部人交易 + 分析师评级 | `FINNHUB_API_KEY` 环境变量 | finnhub-python |
| 飞书 Bitable | 写回 watchlist 字段 | `FEISHU_APP_ID/SECRET` 或回退到旧 token | requests |

## CLI 用法

```bash
# 1. 13F 全量刷新 + 与 watchlist 交叉
python3 -m stock_research.jobs.refresh_13f
python3 -m stock_research.jobs.refresh_13f --refresh   # 仅刷新快照
python3 -m stock_research.jobs.refresh_13f --crossref  # 仅交叉

# 2. 多源 enrichment（akshare/trends/finnhub）
python3 -m stock_research.jobs.enrich_watchlist
python3 -m stock_research.jobs.enrich_watchlist --code NVDA
python3 -m stock_research.jobs.enrich_watchlist --skip-trends   # 跳过慢的

# 3. 跨源可信度审计
python3 -m stock_research.jobs.daily_audit
python3 -m stock_research.jobs.daily_audit --code NVDA
```

## 作为库使用

```python
from stock_research.core import edgar, audit
from stock_research.adapters import feishu

# 拉某机构的 13F 变动
snap = edgar.get_investor_changes("Berkshire Hathaway", "0001067983")
print(snap["changes"])

# 跨源审计
result = audit.audit_stock(
    yf_data={...},
    akshare_data={...},
    sec_signals=[...],
    ticker="NVDA",
)
```

## 环境变量

```bash
export FEISHU_APP_ID=xxx               # 飞书凭证（可选，缺则回退到旧实现）
export FEISHU_APP_SECRET=xxx
export FEISHU_BASE_TOKEN=xxx           # 默认已设
export FINNHUB_API_KEY=xxx             # 缺则 Finnhub 静默跳过
export SEC_USER_AGENT="name email"     # SEC 合规要求
export STOCK_RESEARCH_BASE=/path       # 部署到非 ~/.hermes 时设置
```

## 数据存储

```
data/
└── snapshots/
    ├── 13f/<cik>/snapshot_<report_date>_*.json
    ├── enrich/watchlist_*.json
    └── audit/audit_*.json
stock_history.duckdb (可选，需 pip install duckdb)
```

## 目录结构

```
stock_research/
├── __init__.py
├── config.py              # 路径/表 ID/凭证/CUSIP 映射
├── core/                  # 纯数据获取
│   ├── edgar.py           # SEC EDGAR 13F
│   ├── akshare_client.py  # A 股/港股
│   ├── trends.py          # Google Trends
│   ├── finnhub_client.py  # Finnhub
│   └── audit.py           # 跨源审计
├── adapters/              # I/O 适配
│   ├── feishu.py
│   └── store.py
└── jobs/                  # 编排 + CLI
    ├── refresh_13f.py
    ├── enrich_watchlist.py
    └── daily_audit.py
```

## 未来 Web 服务封装示例

```python
# api/main.py（待实现）
from fastapi import FastAPI
from stock_research.core import edgar
from stock_research.adapters import feishu

app = FastAPI()

@app.get("/api/13f/{cik}")
def get_13f(cik: str):
    return edgar.get_investor_changes("?", cik)

@app.get("/api/watchlist")
def list_watchlist():
    return feishu.fetch_watchlist()
```
