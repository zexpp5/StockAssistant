# StockAssistant

多源、可信、可部署的股票研究系统 — 用经典金融理论（13F / Risk Parity / Markowitz / PEAD）+ 跨源审计取代抖音/小红书等不可靠数据。

> ⚠️ **不构成投资建议**。研究学习用，请先看下面「已知弱点」再决定是否使用。

## ⚠️ 已知弱点（请先看这个再判断）

这套系统在自评里给自己打 97/100，但下面这些事实是必须先知道的：

- **崩盘期跑输 SPY**：在 4 个历史崩盘 regime（2008 雷曼 / 2018 贸易战 / 2020 新冠 / 2022 加息熊）实测，**只有 1/4 抗跌**，平均 drawdown alpha = **-9.77%**（详见 [docs/STRESS_TEST_REPORT.md](docs/STRESS_TEST_REPORT.md)）。
- **Survivorship bias**：回测样本只覆盖"今天还在的股票"，漏掉退市/被收购的标的（详见 [docs/MODEL_CARD.md](docs/MODEL_CARD.md) 第 6 章）。
- **熊市保守估计跑输 SPY 5–15%**（walk-forward 实测，不是承诺）。
- **正向回测意义有限**：v6 因子选股本身就是基于过去 2 年回报选的，对它做 2 年回测属于套娃；2026-05-10 起改用 forward tracking（锁定日往后跟踪真实表现）。
- **A 股覆盖薄**：北方稀土 / 中际旭创 / 海光信息这些 v6 模型不进入（财报字段对接不全），需手动评估。

## 核心能力

- **SEC EDGAR 13F 持仓监控**：10 家著名机构（Berkshire / Bridgewater / Soros 等）季度持仓变动直拉
- **多源数据交叉验证**：yfinance + akshare + Finnhub + SEC 自动比对，标 HIGH/MEDIUM/LOW/CONFLICT
- **每日选股 + 反向审查**：6 个审查器（主题集中度 / 13F 一致性 / 评分校准 / 估值理性 / 数据新鲜度 / 相关性矩阵）
- **飞书 Bitable 落地**：所有数据写到飞书表 + 本地 HTML 仪表盘
- **可部署架构**：core/adapters/jobs/api 分层，FastAPI 即开即用

## 架构

```
StockAssistant/
├── feishu_auth.py             # 飞书认证（env-var 驱动）
├── fetch_stock_prices.py      # yfinance 价格抓取 → 飞书
├── daily_picks.py             # 多维打分每日选股
├── stock_to_feishu.py         # 单条 upsert 工具
├── build_stock_dashboard_html.py  # 本地 HTML 仪表盘
├── weekly_review.py           # 历史回顾
├── reverse_validate_v6.py     # 时间维度回测（5 因子 + PEAD）
├── stock_db.py                # DuckDB 存储
├── daily_refresh.sh           # 每日 cron 编排（8 步）
└── stock_research/            # 核心包（Web 部署友好分层）
    ├── config.py
    ├── core/                  # 纯数据层
    │   ├── edgar.py           # SEC EDGAR 13F
    │   ├── akshare_client.py  # A 股 / 港股
    │   ├── trends.py          # Google Trends
    │   ├── finnhub_client.py  # Finnhub
    │   ├── audit.py           # 跨源数据审计
    │   └── picks_audit.py     # picks 横截面审查
    ├── adapters/              # I/O 层
    │   ├── feishu.py
    │   └── store.py
    ├── jobs/                  # CLI / cron
    │   ├── refresh_13f.py
    │   ├── enrich_watchlist.py
    │   ├── daily_audit.py
    │   └── audit_picks.py
    └── api/                   # FastAPI 服务
        └── main.py
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置凭证
cp .env.example .env
# 编辑 .env 填入飞书 APP_ID/SECRET、可选 FINNHUB_API_KEY

# 3. 加载到 shell
set -a && source .env && set +a

# 4. 跑一次完整刷新
bash daily_refresh.sh
```

## CLI 入口

```bash
# 13F 全量刷新 + 与 watchlist 交叉
python3 -m stock_research.jobs.refresh_13f

# 多源 enrichment
python3 -m stock_research.jobs.enrich_watchlist
python3 -m stock_research.jobs.enrich_watchlist --code NVDA

# 跨源可信度审计
python3 -m stock_research.jobs.daily_audit

# picks 反向审查
python3 -m stock_research.jobs.audit_picks --fast

# 时间维度回测
python3 reverse_validate_v6.py
```

## 作为库使用

```python
from stock_research.core import edgar, audit

# 拉某机构 13F 真实变动
snap = edgar.get_investor_changes("Berkshire Hathaway", "0001067983")
print(snap["changes"])

# 跨源审计
result = audit.audit_stock(
    yf_data={"price": 1500.0, "market_cap": 4e12},
    sec_signals=[...],
    ticker="NVDA",
)
print(result["credibility"])  # HIGH / MEDIUM / LOW / CONFLICT
```

## Web 部署

```bash
pip install fastapi uvicorn
uvicorn stock_research.api.main:app --host 0.0.0.0 --port 8000
```

## 设计原则

1. **凭证全走环境变量** — 仓库零硬编码 secret
2. **core 层无 I/O 副作用** — 纯函数返回 JSON 可序列化 dict
3. **graceful degrade** — 任一数据源失败/限流时跳过
4. **数据来源可追溯** — 每条记录标 URL + 抓取时间
5. **不给买卖建议** — 仅做上涨空间分析 / 风险标记

## 跟踪的 11 家 13F 机构

Berkshire Hathaway (Buffett) · Bridgewater (Dalio) · Norges Bank · Pershing Square (Ackman) · Tiger Global · Renaissance Technologies · Coatue · Soros Fund · Third Point (Loeb) · Greenlight Capital (Einhorn) · Scion (Burry)

## License

MIT
