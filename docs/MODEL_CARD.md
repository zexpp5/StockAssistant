# Model Card — StockAssistant v6

> 标准化模型披露（参考 Mitchell et al. 2019 "Model Cards for Model Reporting"）

## 1. 基本信息

| 字段 | 内容 |
|---|---|
| **模型名称** | StockAssistant v6 — AI 主题股票推荐系统 |
| **版本** | v6 (5 因子选股 + Markowitz 优化 + 中性化 + ADV 限流 + 成本扣减) |
| **发布日期** | 2026-05-09 |
| **维护者** | yanli (lance7in@gmail.com) |
| **代码库** | `/Users/yanli/我的代码_新/线性视界/StockAssistant/` |

## 2. 用途与边界

### 2.1 设计用途
- **个人投资研究系统**：辅助 AI 主题（GPU/HBM/光通信/电力链/稀缺资源）股票筛选
- **多源数据交叉验证**：剔除二手聚合（如抖音截图）的污染
- **决策可追溯**：每条推荐有因子分解、学术依据、历史回测背景

### 2.2 不适用场景（绝不可用于）
- ❌ **直接买卖决策**（系统不给买卖建议，只做"上涨空间分析"）
- ❌ **高频/日内交易**（信号频率为日级，最快月度调仓）
- ❌ **未上市股票/私募**（仅覆盖 SEC EDGAR / 港交所披露易公开数据）
- ❌ **衍生品/期权策略**（v6 不含期权数据维度）
- ❌ **替代专业投资顾问**（无合规牌照，无客户尽调）

### 2.3 适用范围
- 美股 / 港股 / A 股 / 韩股 / 日股 / 澳股 / 哈萨克股（76 只 watchlist）
- AI 算力链 / 电力链 / 稀缺资源 / 中国 AI / 海外 AI 生态等 10 个主题
- 月度调仓时间尺度

## 3. 数据来源（按权威等级）

参考 `verify_data_sources.py sources` 的四档分级：

### 🟢 第一档（一手权威）
| 源 | 用途 |
|---|---|
| SEC EDGAR (sec.gov) | 13F-HR 持仓 / 10-K/Q 财报 |
| 港交所披露易 (hkexnews.hk) | 港股财报 / 公告 |
| 巨潮资讯 (cninfo.com.cn) | A 股财报 |

### 🔵 第二档（专业聚合）
| 源 | 用途 |
|---|---|
| Yahoo Finance (yfinance) | 历史价格 / 实时报价 |
| akshare (东方财富) | A 股 / 港股市场数据 |
| Finnhub | 美股新闻 / 内部人交易 / 分析师评级 |
| Google Trends (pytrends) | 搜索热度（情绪面） |

### ⛔ 禁用源（已强制剔除）
- 抖音截图 / 小红书截图 / 营销号"图解财经"
- 原因：2026-05-09 误读事故（"减持 22.6%"实为仓位占比，导致 8 条记录全部错误）

## 4. 模型架构

### 4.1 选股层（5 因子模型）
| 因子 | 学术来源 | 权重 |
|---|---|---|
| Piotroski F-Score | Piotroski 2000 (Stanford) | 等权 1/5 |
| 12-1 月动量 | Jegadeesh & Titman 1993 (JF) | 等权 1/5 |
| 1 月反转 | De Bondt & Thaler 1985 (JF) | 等权 1/5 |
| PEAD 业绩加速度 | Ball & Brown 1968 (JAR) | 等权 1/5 |
| 分析师评级 | yfinance upgrades_downgrades | 等权 1/5 |

### 4.2 优化层（Markowitz + 三个质量门）
1. **Markowitz Max Sharpe**（Markowitz 1952，蒙特卡洛 20000 次）
2. **行业+市值中性化**（Fama-French 1992 / Rosenberg-Marathe 1976）
3. **ADV 限流**（Almgren-Chriss 2001，单日 ≤ 5% × ADV）
4. **交易成本扣减**（Frazzini-Israel-Moskowitz 2018，5 bps 佣金 + 2 bps/% ADV 冲击）
5. **行业敞口约束**（≤ 25% / 行业，避免一边倒）

### 4.3 反向审查层（双维度）
- **时间维度**：reverse_validate_v6.py — 6 个 regime 历史回测
- **横截面维度**：jobs/audit_picks.py — 主题集中度 + 13F 一致性 + 估值理性 + 相关性矩阵
- **因子治理**：jobs/audit_ic.py — 每月 Spearman IC 衰减检测

## 5. 性能指标（2026-05-09 实测）

### 5.1 选股层因子 IC（Spearman，6 个 regime）
| 因子 | mean IC | IR | 命中率 | 状态 |
|---|---|---|---|---|
| Reversal | +0.157 | +0.60 | 83% | 🟢 strong（IR > 0.5 优秀） |
| Momentum | +0.095 | +0.26 | 83% | 🟢 strong（边际偏弱） |

⚠️ **已知失效场景**：
- Momentum 在 2018 贸易战熊市 IC = **-0.615**（强反向 alpha）
- Reversal 在 2024 震荡期 IC = **-0.274**（反向）
- 解读：单因子有"regime-dependent"失效，等权组合略有缓冲但非绝对

### 5.2 组合层（v6 流水线 2026-05-09 实跑）
| 指标 | $500K 组合 | $50M 组合 |
|---|---|---|
| 年化 Sharpe | 1.97 | - |
| 年化 Gross Alpha | +61% | +56% |
| 总成本 (bps) | 4.8 | 6.1 |
| 年化 Net Alpha | +60.9% | +55.9% |
| 单边换手率 | 47.5% | 46.2% |
| ADV 限流触发 | 0 只 | 1 只 (SDGR) |

### 5.3 历史回测（reverse_validate_v6 跨 regime alpha vs SPY）
| Regime | 模型 alpha |
|---|---|
| 2018-Q4 贸易战 | 待回测验证 |
| 2020-Q2 疫情反弹 | 待回测验证 |
| 2022-Q1 加息熊市 | 待回测验证 |
| 2023-Q2 AI 崛起 | 待回测验证 |
| 2024-Q3 震荡 | 待回测验证 |
| 2025-Q4 至今 | walk_forward_v6 报告中：召回 ?% / 准确 ?% |

## 6. 已知缺陷

### 🔴 严重（影响判断准确性）
1. **Survivorship bias**：回测样本只覆盖"今天还在的股票"，漏掉退市/被收购的标的
2. **Look-ahead bias 残留**：分析师评级用当前快照（无历史可获取性），不能严格 walk-forward
3. **数据滞后**：SEC 13F 滞后 45 天，看到时已是 1.5 个月前持仓
4. **A 股财报数据缺**：yfinance 对 A 股财报覆盖差，Piotroski 在 A 股标的上仅靠近似

### 🟡 中等
5. **行业归类粗**：用 daily_picks.THEME_MAPPING（手工维护 76 只），不是标准 GICS 4 级
6. **协方差用历史数据**：Markowitz 假设过去 252 天协方差代表未来（不一定）
7. **ADV 仅用 30 天均值**：未考虑流动性突变（如停牌/财报日）
8. **小盘股因子噪声大**：≤$1B 市值股票因子分数稳定性差

### 🟢 已修复（透明披露）
- ✅ 抖音截图数据污染（2026-05-09 v5 → v6 强制剔除）
- ✅ 行业 beta 主导（v6 加中性化，已验证 Top 5 从 4/5 半导体变成跨 4 个行业）
- ✅ 单一行业过度集中（v6 加 ≤ 25% 约束）

## 7. 边界声明

本模型是**研究辅助工具**，不构成投资建议。
- 用户必须独立做最终投资决策
- 模型输出的"⭐⭐⭐ 强烈推荐"仅表示模型框架内的相对排名
- **用户接受**：模型可能出错（漏报、误报、数据错误、回测过拟合）
- **绝对边界**：系统不提供买卖时点、价格目标、仓位上限以外的具体操作

## 8. 维护节奏

| 频率 | 动作 |
|---|---|
| 每日（cron 7:30）| 11 步 daily_refresh.sh 全自动 |
| 每周 | weekly_review.py 历史回顾 |
| 每月 | factor IC 监测 + 模型有效性审查 |
| 每季度 | 13F 持仓更新（SEC 公布后 45 天）|
| 每半年 | walk-forward 完整回测 + Model Card 修订 |
| 每年 | 因子组合调整（替换衰减因子）|

## 9. 联系与反馈

- 维护者：yanli
- 邮箱：lance7in@gmail.com
- 飞书 watchlist：https://w5scrwkn9y.feishu.cn/base/CuiybJoOMafb9HsZbu2cfVhfnZg

---

*本 Model Card 遵循 Mitchell et al. (2019) "Model Cards for Model Reporting" 规范。*
*所有数据 / 因子 / 约束的学术引用见 docs/METHODOLOGY.md。*
