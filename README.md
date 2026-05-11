# StockAssistant

多源、可信、可部署的股票研究系统 — 用经典金融理论（13F / Risk Parity / Markowitz / PEAD）+ 跨源审计取代抖音/小红书等不可靠数据。

> ⚠️ **不构成投资建议**。研究学习用，请先看下面「已知弱点」再决定是否使用。

## 🌅 每天看哪里？

**只看一份：[morning_brief.md](morning_brief.md)** — 5 section、5 分钟读完：
1. 今天能不能动手（regime gate）
2. 系统当前优选组合（A 股 + 美股）
3. AI alpha 跟踪（forward NAV vs SPY）
4. 红旗（异常 / 风险）
5. 今天必须做的动作

每天 08:30 由 `daily_refresh.sh` 末尾的 step 26 自动生成；不想等的话手动跑：
```bash
python3 -m stock_research.jobs.morning_brief
```

**飞书推送 — 双通道**（让 brief 主动到达，同事也能看）：

| 通道 | 接收方 | 版本 | 配置项 |
|---|---|---|---|
| P2P 私聊（hermes lark-cli + bot） | 你本人 | **完整版**（含 section 5 个人调仓 ¥ 明细）| `FEISHU_BRIEF_USER_ID=ou_xxx` |
| 群 webhook（自定义机器人） | "📊 股票看板"群里所有人 | **共享版**（section 5 已脱敏，无 ¥ 仓位）| `FEISHU_BRIEF_WEBHOOK=https://.../bot/v2/hook/...` |

两个 env 都写到 `.env` 即可。每天 08:30 `daily_refresh.sh` 跑完，双通道自动推送。
要让群通道工作，群里需要先添加一个"自定义机器人"（飞书：群设置 → 群机器人 → 添加 → 自定义机器人；安全设置选"自定义关键词" `早安`）。

**为什么 P2P 用 lark-cli、群用 webhook？**
群是 external（跨租户）时 lark-cli bot 无法被邀请进群（API 错误 232033）；自定义机器人 webhook 不受此限。

**为什么 share_mode 默认 True 进群？**
brief section 5 含具体调仓 + ¥ 金额，给同事看会引起跟单 / 隐私问题。脚本在 [morning_brief.py:build_brief()](stock_research/jobs/morning_brief.py) 里通过 `share_mode=True` 把 section 5 替换成一行提示。

其他面板（streamlit / HTML 仪表盘 / Bitable 表）= **调试 / 数据落地**用，不是日常入口。

## ⚠️ 已知弱点（请先看这个再判断）

**2026-05-11 起**：自评分体系改成 `overall = min(quant, deep, data, risk)` 一票否决。
**当前 overall = 30 / 100**，因风控维度（stress test mean DD α = -9.77%）触发 veto。
跑 `python3 -m stock_research.jobs.self_score` 重算。

以下事实是必须先知道的：

- **崩盘期跑输 SPY**：在 4 个历史崩盘 regime（2008 雷曼 / 2018 贸易战 / 2020 新冠 / 2022 加息熊）实测，**只有 1/4 抗跌**，平均 drawdown alpha = **-9.77%**（详见 [docs/STRESS_TEST_REPORT.md](docs/STRESS_TEST_REPORT.md)）。
- **Survivorship bias**：回测样本只覆盖"今天还在的股票"，漏掉退市/被收购的标的（详见 [docs/MODEL_CARD.md](docs/MODEL_CARD.md) 第 6 章）。
- **熊市保守估计跑输 SPY 5–15%**（walk-forward 实测，不是承诺）。
- **正向回测意义有限**：v6 因子选股本身就是基于过去 2 年回报选的，对它做 2 年回测属于套娃；2026-05-10 起改用 forward tracking（锁定日往后跟踪真实表现）。
- **A 股覆盖薄**：北方稀土 / 中际旭创 / 海光信息这些 v6 模型不进入（财报字段对接不全），需手动评估。
- **跨市场汇率敞口未对冲**：组合横跨美股 + A 股，USDCNY 年波动 ~5-8% 会蚕食 USD sleeve 收益。跑 `python3 -m stock_research.jobs.cross_market_risk` 看当前敞口。

### 给新手（如果你是第一次看这个系统）

1. **系统打分 ≠ 买入信号**。即便完美，本质是"研究效率工具"，不是"按红绿灯买"。
2. **崩盘期 3/4 跑输 SPY** 是已知的结构性问题。建议把 SPY 60% + 系统 picks 40% 当作现实基线，纯跟单系统的回撤会让人受不了。
3. **A 股 + 美股两个市场都做**，外汇风险默认未对冲 — 跑 `cross_market_risk` 查实际敞口；CNY 一年波动 5-8% 足以吃掉一个 sleeve 的 α。

### 2026-05-11 起的硬闸门

- **因子 IC 闸门**：`daily_picks_v5` / `a_share_picks` 启动时跑 `factor_ic_gate`，IC < 0.03 或 |IR| < 0.30 全失效 → 强制 dry-run。`--bypass-ic-gate` 可强制通过（风险自担）。
- **动态 gross exposure**：`build_plan_a_v5` 启动时跑 `regime_filter.get_dynamic_gross_exposure`，VIX/SPY-200MA/yield curve 三信号决定 cash_pct（20%-100% 五档）。
- **A 股 ADV cap 收紧**：主板 1.5% / 创业科创北交 1.0%（Almgren-Chriss 2001），涨跌停拦截写 `followup_pending_a_share.json` 明日复评。
- **13F 仅作 conviction booster**：单凭 13F（45 天滞后）不再独立把 credibility 推到 HIGH。

## 快速跑通（不需要飞书凭证）

**只看分析（推荐 5 分钟跑通）**：

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
# 浏览器打开 http://localhost:8501
```

**完整流水线**（需配 `.env`）：

```bash
cp .env.example .env
# 编辑 .env，填入 FEISHU_APP_ID / FEISHU_APP_SECRET / FINNHUB_API_KEY
bash daily_refresh.sh
```

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
├── daily_refresh.sh           # 每日 launchd 编排（25 步，具体见脚本头）
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
