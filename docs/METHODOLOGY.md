# 方法论白皮书 — StockAssistant v6

> 系统化披露 StockAssistant 投资研究系统的理论基础、数据架构、决策流程与风险管理。

## 摘要

StockAssistant 是一个**多源数据驱动 + 学术因子模型 + 经典优化论文约束**的个人股票研究系统。

**核心论断（2026-05-09）**：AI 主线已从「算力 → 网络 → 电力 → 内存 → 存储」轮动到「下一波稀缺资源」拐点。最值得潜伏：水（Xylem）、稀土（MP）、铀（CCJ）、SMR（BWXT）、AI 训练数据（Reddit）。

本系统每日跑 11 步流水线，输出 ⭐⭐⭐/⭐⭐/⭐ 三档推荐 + 双维度反向审查 + Markowitz 仓位优化。**不给买卖建议，仅作研究辅助**。

---

## 第一章 理论基础

### 1.1 百倍股的 5 个共同条件（自创框架）

参考 SK Hynix（一年 +920%）和 SanDisk（一年多 +1200%）真实路径：

| # | 条件 | 释义 |
|---|---|---|
| 1 | ❄️ 冷门到极点 | 市场归为夕阳/周期股，估值低、关注度低 |
| 2 | 🚧 结构性短缺 | 寡头供给（3-5 家）+ 长 Capex 周期（18-36 月）|
| 3 | 📋 真实订单兑现 | 客户开始签 5-20 年长期合同锁产能 |
| 4 | 🔄 认知反转 | 从"还有用吗"到"AI 必需品" |
| 5 | 💰 市值起点低 | 拆分、被忽视、小盘 |

**用法**：用这 5 条筛选"下一个百倍股"候选方向。

### 1.2 AI 主线时间轴（2023-2027）

| 阶段 | 主题 | 赢家 | 状态 |
|---|---|---|---|
| 2023 | GPU/算力 | NVDA | 已涨过（10x+）|
| 2024 H1 | 芯片代工/HBM | TSM, SK Hynix | 已涨过（3-9x）|
| 2024 H2 | 网络/光通信 | COHR, LITE, 中际旭创 | 5/7 刚回调 |
| 2025 H1 | 电力发电 | VST, CEG | 已涨过（5-10x）|
| 2025 H2 | 电力配电 | GEV, ETN | 仍在兑现 |
| 2025 H2-2026 | HBM 内存 | SK Hynix +920%, MU +90% | 已涨过 |
| **2026 Q1-Q2** | NAND/HDD 存储 | SanDisk +1200%, WDC +176% | **进行中** |
| **2026 H2 (?)** | 水/稀土/SMR/AI 数据 | XYL, MP, BWXT, RDDT | **潜伏期** |
| 2027+ (?) | AR 眼镜/量子/聚变 | Meta? Apple? | 早期 |

### 1.3 5 大稀缺资源（重点关注区）

| 主题 | 代表标的 | 核心逻辑 |
|---|---|---|
| 💧 数据中心冷却水 | XYL | AI + 半导体 + 电力三重水需求叠加 |
| 🪨 稀土国产化 | MP | Pentagon 10 年合同 + Apple 长合 |
| ☢️ 铀矿（核能燃料） | CCJ | MSFT/AMZN/GOOG 全签 SMR 长期 PPA |
| ⚛️ SMR | BWXT | 唯一规模化 TRISO 燃料生产商 |
| 📊 AI 训练数据 | RDDT | OpenAI/Google licensing；唯一上市标的 |

---

## 第二章 选股层（5 因子学术模型）

### 2.1 Piotroski F-Score（0-9）

**出处**：Piotroski (2000) "Value Investing: The Use of Historical Financial Statement Information to Separate Winners from Losers", *Journal of Accounting Research*

**9 个二元会计指标**：
- 盈利能力 (4): ROA > 0 / CFO > 0 / ΔROA > 0 / CFO > NI（盈利质量）
- 杠杆/流动性 (3): Δ长债 < 0 / Δ流动比率 > 0 / 没发新股
- 经营效率 (2): Δ毛利率 > 0 / Δ资产周转率 > 0

**论文实证**：高 F-Score（7-9）组合 1976-1996 年化超额 +7.5%，IR 1.0+

### 2.2 12-1 月动量

**出处**：Jegadeesh & Titman (1993) "Returns to Buying Winners and Selling Losers", *Journal of Finance*

**公式**：return from t-252 to t-21（剔除最近 1 月避免反转）

**论文实证**：动量因子年化超额 +12%，是 Carhart (1997) 四因子之一

**已知失效场景**：贸易战/V 型反转期（2018-Q4 IC = **-0.615**）

### 2.3 1 月反转

**出处**：De Bondt & Thaler (1985) "Does the Stock Market Overreact?", *Journal of Finance*

**公式**：-1 × return from t-21 to t（最近 1 月跌得多的反弹）

**已知失效场景**：持续震荡期（2024-Q3 IC = **-0.274**）

### 2.4 PEAD 业绩加速度

**出处**：Ball & Brown (1968) "An Empirical Evaluation of Accounting Income Numbers", *Journal of Accounting Research*

**核心**：财报后股价漂移效应（Post-Earnings Announcement Drift）—— EPS 超预期的股票，未来 3-9 个月持续跑赢

### 2.5 分析师评级

**数据源**：yfinance.upgrades_downgrades

**信号**：90 天内 ≥ 3 次目标价 Raises = 强信号

### 2.6 因子合成

**5 因子等权 z-score 合成**（Tertile cutoff，Top 1/3 入选 ⭐⭐⭐）。

⚠️ **不做动态权重**（避免 over-fitting），保留 IC 衰减时人工调整空间。

---

## 第三章 优化层（Markowitz + 三个质量门）

### 3.1 Markowitz Max Sharpe（基础）

**出处**：Markowitz (1952) "Portfolio Selection", *Journal of Finance*（诺贝尔经济学奖）

**算法**：蒙特卡洛 20000 次，找最大 Sharpe 解
**约束**：单只 max 15% / min 2% / 现金 5% / 无杠杆

### 3.2 因子中性化（剔除风格暴露）

**出处**：
- Fama & French (1992) "The Cross-Section of Expected Stock Returns"（size 因子显著）
- Rosenberg & Marathe (1976) "Common Factors in Security Returns"（Barra 风险模型）

**实施**：
- 行业 z-score：按行业分组算 z-score（避免半导体 beta 主导）
- 市值残差：因子 = α + β·log(MCap) + ε，取 ε 作为 size-neutral 因子

**实证效果（2026-05-09）**：
- 中性化前 Top 5：全部工业电力链（行业 beta 主导）
- 中性化后 Top 5：跨 4 个行业（医药/SaaS/消费/半导体）

### 3.3 ADV 限流（流动性约束）

**出处**：
- Almgren & Chriss (2001) "Optimal Execution of Portfolio Transactions"
- Frazzini, Israel & Moskowitz (2018) "Trading Costs"

**规则**：单日交易 ≤ 5% × ADV（30 日均成交额）

**实证**（2026-05-09 $50M 资金）：SDGR 目标 +4.46% ($2.23M) 超 ADV 5% = $945K，被砍到 +1.89%

### 3.4 交易成本扣减

**模型**：total_cost = commission + slippage + impact
- commission: 5 bps（双边）
- slippage: 0（限价单假设）
- impact: 2 bps × (Δ$ / ADV × 100)

**实证**：47.5% 单边换手 → 4.8 bps 总成本 → Net Alpha 几乎不影响

### 3.5 行业敞口约束

**规则**：单一行业总权重 ≤ 25%
**理由**：避免一边倒押注（如 v5 选出 LRCX/MTZ/MOD/VRT/GEV 五连击全是工业电力）

**实施**：超出按比例缩放，溢出转入现金（不强制平摊到其他行业）

### 3.6 半 Kelly 仓位上限

**出处**：Kelly (1956) "A New Interpretation of Information Rate" + Thorp (2006) 实证半 Kelly 风险调整后收益更高

**规则**：单只仓位 ≤ max_single × 0.5（默认 7.5%）

---

## 第四章 反向审查（双维度）

### 4.1 时间维度（reverse_validate v6）

**目标**：证明模型不是 over-fit 单一 regime

**实施**：6 个 regime（贸易战 / 疫情反弹 / 加息熊 / AI 崛起 / 震荡 / 当前）依次回测

**评估指标**：
- Precision = 推荐 ∩ 涨过 SPY+5% / 推荐总数
- Recall = 推荐 ∩ 涨过 SPY+5% / 涨过 SPY+5% 总数
- Alpha vs SPY（每个 regime + 跨 regime 平均）

### 4.2 横截面维度（jobs/audit_picks）

**6 个审查器**：
1. **主题集中度**（Risk Parity）：≤ 50% 健康 / > 70% 严重失衡
2. **13F 一致性**：当日推荐 vs 巴菲特/段永平/高瓴最新动作
3. **评分校准**：⭐⭐⭐/⭐⭐/⭐ 历史平均涨跌
4. **估值理性**：PEG > 3 / PE > 100 / 1Y > 200% 警告
5. **数据新鲜度**：价格陈旧、单源依赖
6. **相关性矩阵**（Markowitz）：⭐⭐⭐ 两两相关 > 0.75 的"伪分散"对

### 4.3 因子治理（jobs/audit_ic）

**Grinold-Kahn 标准**：
- IC > 0.05 = 🟢 strong
- IC 0.02-0.05 = 🟡 marginal
- |IC| < 0.02 = 🔴 decayed
- IC < -0.02 = ⛔ inverted（反向 alpha）

每月跑一次，告警衰减因子。

---

## 第五章 数据架构（多源交叉验证）

### 5.1 数据源四档分级

详见 `verify_data_sources.py sources` 的完整分级。

### 5.2 跨源审计（jobs/daily_audit）

**规则**：
- yfinance vs akshare 价格偏差 > 1% → 标 LOW；> 5% → CONFLICT
- 市值偏差 > 5% → 标 LOW
- ≥ 3 源一致 → HIGH；2 源 → MEDIUM；1 源 → LOW

### 5.3 数据污染防御（强制规则）

❌ **禁用源**：抖音截图 / 小红书截图 / 营销号"图解财经"

**事故记录**（2026-05-09）：
- 抖音"东方财富图解财经"截图把"占组合比例"画成"持仓变动幅度"
- 误读导致一次性写入 8 条 13F 错误记录到飞书
- 误差量级 100x-1000x（"减持 22.6%"实为占比，真实变动 -4.3%）

---

## 第六章 决策流程（11 步流水线）

每日 7:30 cron 自动跑：

```
1/11  抓价格（fetch_stock_prices.py · yfinance）
2/11  SEC 13F 刷新（jobs.refresh_13f）
3/11  多源 enrichment（jobs.enrich_watchlist · akshare/Finnhub）
4/11  跨源审计（jobs.daily_audit · 数据可信度判定）
5/11  每日优选 v1（daily_picks.py · 旧 4 维体系，对照保留）
6/11  picks 反向审查（jobs.audit_picks · 6 审查器横截面）
7/11  历史回顾（weekly_review.py）
8/11  v6 学术因子选股（daily_picks_v5.py · 5 因子）
9/11  Markowitz 仓位优化（build_plan_a_v5.py）
10/11 调仓建议（write_trade_delta_to_feishu.py）
11/11 重建 HTML（build_stock_dashboard_html.py · 含审查面板）
```

---

## 第七章 风险管理与限制

### 7.1 系统级风险
- 数据延迟（yfinance 实时性差，A 股盘中数据缺）
- 模型 over-fit（虽然有 walk-forward 但样本只 6 个 regime）
- Survivorship bias（回测样本只覆盖现存股票）

### 7.2 操作级限制
- 不给买卖建议（绝对边界）
- 不给价格目标
- 不给买入时点
- 可做"上涨空间分析"（产业逻辑/估值/催化剂）

### 7.3 合规说明
- 无投资顾问牌照
- 仅个人研究辅助
- 用户独立决策
- 输出 ≠ 投资建议

---

## 第八章 路线图

### 短期（1 个月）
- [ ] 期权数据维度（Finnhub put/call ratio）
- [ ] 历史预测自动归档到公开 GitHub
- [ ] FastAPI 公网部署（开放只读 API）

### 中期（3 个月）
- [ ] 因子 IC 月度自动报告
- [ ] Brinson 业绩归因（行业/选股/择时）
- [ ] 月度 walk-forward（96 时点 vs 6 regime）

### 长期（6-12 个月）
- [ ] Survivorship bias 修正（用 SEC 历史 13F-HR 重建当时股票池）
- [ ] Stress test（2020-03 / 2022 加息 / 2008 历史回测）
- [ ] 多模型 ensemble（XGBoost + 逻辑回归 + 因子等权）

---

## 附录 A：学术引用清单

| 标号 | 文献 | 用途 |
|---|---|---|
| Markowitz (1952) | "Portfolio Selection", JF | 组合优化基础 |
| Kelly (1956) | "A New Interpretation of Information Rate" | 仓位上限 |
| Ball & Brown (1968) | "An Empirical Evaluation of Accounting Income Numbers", JAR | PEAD 因子 |
| Rosenberg & Marathe (1976) | "Common Factors in Security Returns" | Barra 风险模型 |
| De Bondt & Thaler (1985) | "Does the Stock Market Overreact?", JF | 反转因子 |
| Fama & French (1992) | "The Cross-Section of Expected Stock Returns", JF | Size 因子 + 中性化 |
| Pardo (1992) | "Design, Testing & Optimization of Trading Systems" | Walk-forward 方法 |
| Jegadeesh & Titman (1993) | "Returns to Buying Winners and Selling Losers", JF | 12-1 动量 |
| Grinold (1994) | "Alpha is Volatility Times IC Times Score" | IC 监测 |
| Carhart (1997) | "On Persistence in Mutual Fund Performance" | 四因子模型 |
| Piotroski (2000) | "Value Investing: Use of Historical Financial Statement", JAR | F-Score |
| Grinold & Kahn (2000) | *Active Portfolio Management* | IC 阈值标准 |
| Almgren & Chriss (2001) | "Optimal Execution of Portfolio Transactions" | 冲击成本 |
| Faber (2007) | "A Quantitative Approach to Tactical Asset Allocation" | 200MA 趋势过滤 |
| Frazzini, Israel & Moskowitz (2018) | "Trading Costs" | 实证成本约束 |
| Asness, Moskowitz & Pedersen (2013) | "Value and Momentum Everywhere", JF | 因子中性化标准 |
| Mitchell et al. (2019) | "Model Cards for Model Reporting" | Model Card 规范 |

---

## 附录 B：版本历史

| 版本 | 日期 | 关键变化 |
|---|---|---|
| v1 | 早期 | 4 维拍脑袋打分（AI 关联+估值+趋势+可信度）|
| v2-v5 | 迭代 | 反向验证模型从 4 维 → 学术 4 因子 → 加 反转 |
| **v6** | 2026-05-09 | 5 因子（+ PEAD）+ 中性化 + Markowitz + ADV + 成本 + IC 监测 + Model Card |

---

*维护者：yanli (lance7in@gmail.com)*
*最后更新：2026-05-09*
