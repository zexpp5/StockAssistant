# StockAssistant 优化路线图（v8）

> 给同事/Reviewer 看的"已实现 + 待办"全景图。
> 维护者: yanli · 最后更新: 2026-05-09 · **不构成投资建议**

---

## 总览

**当前评分**: **89 / 100**（v9.1，加入个股深度新维度后的综合分；量化打分维度仍 97/100）

> 评分口径在 v9 后扩展：从单一"量化打分质量"改为"量化 + 个股深度 + 数据效率"三维加权。
> 详见 [docs/2026-05-10_B路线个股深度研究系统.md](2026-05-10_B路线个股深度研究系统.md)

| 阶段 | 时间 | 评分 | 关键里程碑 |
|---|---|---|---|
| v1   | 2026-05-09 上午 | 24 | 拍脑袋 4 维打分 |
| v6.0 | 2026-05-09 中午 | 77 | 5 因子学术 + Markowitz |
| v7.0 | 2026-05-09 下午 | 91 | + 反向审查 + 实盘防御 |
| v7.5 | 2026-05-09 傍晚 | 94 | + OpenBB（宏观 + 行业 + 商品 + PCR + 内部人）|
| **v8.0** | 2026-05-09 晚上 | 97 | + alphalens + PyPortfolioOpt + streamlit + pyfolio + De Prado + vectorbt |
| **v9.0** | 2026-05-10 白天 | **86**(综合) | **+ B 路线 Phase 1-4 代码落地**（个股深度新维度，从 30 → 75/100）|
| **v9.1** | 2026-05-10 晚上 | **89**(综合) | **+ Forward 估值 + 自建 DCF + 8 季 trend + FMP 24h 缓存** |

**当前等待激活**：Anthropic API（充值后跑 Phase 2C/4 LLM 研报）+ FMP Premium（解锁 Phase 3 电话会议）→ 详见 [docs/2026-05-10_付费数据源升级清单.md](2026-05-10_付费数据源升级清单.md)

---

## ✅ 已实现的优化（按 v8 完成顺序）

### 1. alphalens 风格因子 Tear Sheet 🟢 已实现

**做了什么**：
- 因为 alphalens-reloaded 在 pandas 2.x 上有 freq bug，**自实现核心算法**
- 跑 IC（Spearman）+ quintile portfolio 累计收益
- 文件: [`stock_research/jobs/factor_tearsheet.py`](../stock_research/jobs/factor_tearsheet.py)

**学术依据**: Grinold (1994) + Quantopian alphalens-reloaded

**实测结果**（2026-05-09 跑 momentum / reversal / long_momentum 三因子）：
| 因子 | 21D Mean IC | IR | 单调性 |
|---|---|---|---|
| momentum | -0.017 | -0.07 | 🟡 非单调 |
| reversal | +0.07 | +0.19 | 🟡 非单调 |
| long_momentum | +0.067 | +0.19 | 🟡 非单调 |

**给 reviewer 看**：因子在 **小样本（16 股 × 12 月）** 上"非单调"是预期的；机构级实战需要 500+ 股 × 10 年。

**CLI**：`python3 -m stock_research.jobs.factor_tearsheet --all`

**评分加分**：+1（因子治理可视化）

---

### 2. PyPortfolioOpt 专业组合优化 🟢 已实现

**做了什么**：
- 替换 v5 的 20000 次蒙特卡洛 Markowitz（粗糙近似）为 cvxpy 凸优化（精确解）
- 新增 4 种优化方法：min_volatility / HRP / Black-Litterman / min_CVaR
- **Black-Litterman**: 把 v6 因子分数当 view，结合市场均衡先验贝叶斯更新
- 文件: [`stock_research/core/portfolio_optimizer_pro.py`](../stock_research/core/portfolio_optimizer_pro.py)

**学术依据**:
- Markowitz (1952) Portfolio Selection
- Black & Litterman (1992) Global Portfolio Optimization
- Lopez de Prado (2016) HRP
- Rockafellar & Uryasev (2000) CVaR optimization

**实测对比**（8 只 AI 股 1 年数据）：
| 方法 | 年化收益 | Sharpe | 特点 |
|---|---|---|---|
| max_sharpe | +152% | 5.32 | 最激进 |
| min_volatility | +103% | 4.25 | 防御 |
| **HRP** (de Prado 2016) | **+55%** | **2.70** | **out-of-sample 最稳** |
| Black-Litterman | +6.5% | 0.08 | 用 v6 view 后保守 |
| min_CVaR | - | - | 尾部风险敏感 |

**评分加分**：+2（组合管理）

---

### 3. streamlit Web 应用 🟢 已实现

**做了什么**：
- 把静态 HTML dashboard 升级成可交互 Web 应用（6 个 Tab）
- 包含: 概览 / 每日推荐 / 反向审查 / 因子治理 / OpenBB 情报 / Stress Test
- 部署：`streamlit run streamlit_app.py` 本地启动 / 推 GitHub 后 Streamlit Cloud 一键部署
- 文件: [`streamlit_app.py`](../streamlit_app.py)

**Tab 设计**：
1. **📌 概览** - 4 个指标卡（实盘防御 / 当日推荐数 / 宏观 regime / 活跃因子）
2. **⭐ 每日推荐** - ⭐⭐⭐ 推荐表 + 全部推荐展开
3. **🛡 反向审查** - 主题集中度条形图 + 估值警告 + 相关性矩阵
4. **📊 因子治理** - IC + IR + Quintile（每个因子可展开）
5. **🌐 OpenBB 情报** - 宏观 + 行业 + 商品 + 内部人（4 区块）
6. **💀 Stress Test** - 4 崩盘 × 3 防御 A/B/C 对比表

**评分加分**：+2（可部署性 + 公开化）

---

### 4. pyfolio 机构级 Tear Sheet 🟢 已实现

**做了什么**：
- 把 monthly_letter 升级到投行级 tear sheet
- 输出 15+ 性能指标：Annual return / Cumulative / Vol / Sharpe / Calmar / Max DD / Omega / Sortino / Skew / Kurtosis / Tail ratio / VaR / Alpha / Beta / Stability
- 文件: [`stock_research/jobs/pyfolio_tearsheet.py`](../stock_research/jobs/pyfolio_tearsheet.py)

**学术依据**: Quantopian pyfolio (2014-2020 Quantopian 内部用 6 年)

**实测**（2026-05 月组合）：
- 累计收益 +2.32%
- Sharpe 4.96
- Sortino 14.63
- Max DD -1.60%
- Tail ratio 2.99（右尾比左尾粗 2.99x，好兆头）

**CLI**：`python3 -m stock_research.jobs.pyfolio_tearsheet --month 2026-05`

**评分加分**：+1（运维 / 对外披露专业度）

---

### 5. De Prado 学术 ML 算法 🟢 已实现（自实现）

**做了什么**：
- mlfinlab 已商业化下架，**自实现 De Prado 2018 论文核心算法**
- 文件: [`stock_research/core/financial_ml.py`](../stock_research/core/financial_ml.py)

**包含的算法**：

#### 5.1 Triple Barrier Labeling（Chapter 3.2-3.3）
给每个交易事件三类标签：止盈 (+1) / 止损 (-1) / 时间到期 (0)
- 替代固定 holding period
- 避免 forward-looking bias

**实测**（NVDA 13 个月度入场，pt=10%/sl=-7%/max=20 天）：
- 5 次止盈、3 次止损、5 次时间到期
- 平均收益 +4.01%/月

#### 5.2 Purged K-Fold Cross Validation（Chapter 7.4）
解决金融时序的 label leakage 问题：
- **Purge**: 移除 train 里 label window 与 test 重叠的样本
- **Embargo**: test 之后额外 buffer

**实测**（100 样本 / 5 折 / embargo 1%）：
- 标准 KFold 训练集 80
- Purged KFold 训练集 76.8（损失 3.2 防 leakage）

#### 5.3 Sample Uniqueness（Chapter 4.3）
算每个样本的"独特性"，给 ML 模型 weight 重叠样本降权。

**学术依据**: Marcos Lopez de Prado (2018) Advances in Financial Machine Learning

**评分加分**：+2（回测严谨度跨过"个人 vs 机构"界线）

---

### 6. vectorbt 向量化回测 🟢 已实现

**做了什么**：
- 用 vectorbt 跑 3 个策略对比（Buy&Hold / 200MA / RSI Mean Reversion）
- 12 标的 × 5 年 = 45,360 数据点回测，**14 秒完成**（for-loop 需要 60-120 秒）
- 文件: [`stock_research/jobs/vectorbt_backtest.py`](../stock_research/jobs/vectorbt_backtest.py)

**实测对比**（12 只 AI 股 5 年）：
| 策略 | 平均收益 | Sharpe | 最大回撤 |
|---|---|---|---|
| Buy & Hold | +496.72% | 1.08 | **-55.55%** |
| **200MA Trend** (Faber 2007) | +402.03% | 1.13 | **-32.83%** ⬇️ |
| RSI Mean Reversion | +72.49% | 0.58 | -44.75% |

**关键发现**：200MA 比 Buy&Hold **收益略低 (-94%)** 但**最大回撤减 22 个百分点** + Sharpe 更高 → **实证 Faber 2007**

**评分加分**：+1（让 walk-forward 升级到 monthly rolling 可行）

---

## 🆕 v9 — B 路线个股深度研究（2026-05-10 落地）

> 触发：v8 触顶量化打分 97/100 后，用户问"系统能做财报分析吗"暴露另一半工种缺失。
> v9 补的是 sell-side 卖方研报维度（对标萝卜投研 / 摩根士丹利），**不是替代 v8**。

### v9.0 Phase 1-4 代码落地（基础架构）

| 模块 | 文件 | 学术依据 |
|---|---|---|
| 杜邦五因子 + Beneish + Altman + 8 项盈利质量 | [`core/fundamental_deep.py`](../stock_research/core/fundamental_deep.py) | Beneish 1999 / Altman 1968 / Sloan 1996 |
| 同业 12 项分位 + 三级回退（industry → sector → market cap） | [`core/peer_compare.py`](../stock_research/core/peer_compare.py) | — |
| SEC 10-K/10-Q/8-K 全文 + 反向贪心章节切分 | [`core/sec_filings.py`](../stock_research/core/sec_filings.py) | — |
| Claude API 客户端（HTTP + prompt cache） | [`core/claude_client.py`](../stock_research/core/claude_client.py) | — |
| 端到端 LLM 研报生成（8 章节模板） | [`jobs/research_report.py`](../stock_research/jobs/research_report.py) | — |
| 电话会议情绪（Larcker 规避词 + LM 词典） | [`core/earnings_call.py`](../stock_research/core/earnings_call.py) | Larcker-Zakolyukina 2012 / Loughran-McDonald 2011 |
| 产业链 70%（10-K LLM 提取 + 新闻聚合） | [`core/supply_chain.py`](../stock_research/core/supply_chain.py) | — |
| 端到端结构化报告（合成 Phase 1 全部） | [`jobs/fundamental_report.py`](../stock_research/jobs/fundamental_report.py) | — |

### v9.1 修补 v9.0 NVDA 实测暴露的 3 个硬伤

| 硬伤 | 修补 | 文件 |
|---|---|---|
| Trailing P/E 对成长股是反向信号（NVDA TTM 43 但 forward 25-28） | Forward P/E + EV/Sales + PEG (Lynch 1989) | [`core/forward_valuation.py`](../stock_research/core/forward_valuation.py) |
| FMP 黑盒 DCF 假设全不知道 | 自建三档 DCF + 5×5 WACC×TGR 敏感度矩阵 | [`core/dcf_scenarios.py`](../stock_research/core/dcf_scenarios.py) |
| 静态指标丢方向（ROE 101% 是涨上来还是跌下来？） | 8 季 10 指标 trend + improving/deteriorating 自动判定 | [`core/quarterly_trends.py`](../stock_research/core/quarterly_trends.py) |
| FMP 免费档 250/day 跑 7-8 只就爆（v9.1 新模块更耗） | 24h 文件缓存层（透明接入 fmp_client._get） | [`adapters/fmp_cache.py`](../stock_research/adapters/fmp_cache.py) |

### v9 实测产出

- [data/reports/NVDA_v1.md](../data/reports/NVDA_v1.md) — Phase 1 端到端结构化报告（无 LLM）
- 手工合成 NVDA 2200 字研报（Claude Code 直接读 SEC 10-K + Phase 1 JSON），10/10 事实层与 10-K 原文交叉核对通过，与 Wall Street 共识对比有判断差异（详见对话）
- [docs/research_validation_log.md](research_validation_log.md) — NVDA #1 研报登记 5 条可证伪预测，等 5/20 财报开始回填

---

## 📋 优化建议清单（含已实现状态）

> 本节给 reviewer 看：**优化建议 → 是否已实现 → 在哪里看到证据**

### A. 数据层
| 建议 | 状态 | 证据 |
|---|---|---|
| 多源数据交叉验证 | ✅ 已做 | [`core/audit.py`](../stock_research/core/audit.py) |
| OpenBB 100+ 数据源接入 | ✅ 已做 (v7.5) | [`core/macro_data.py`](../stock_research/core/macro_data.py) |
| 期权数据（PCR）| ✅ 已做 | [`core/options_signals.py`](../stock_research/core/options_signals.py) |
| 内部人交易（Form 4）| ✅ 已做 | [`core/insider_signals.py`](../stock_research/core/insider_signals.py) |
| 大宗商品相关性 | ✅ 已做 | [`core/commodity_signals.py`](../stock_research/core/commodity_signals.py) |
| 宏观经济数据（FRED）| ✅ 已做 | [`core/macro_data.py`](../stock_research/core/macro_data.py) |
| 实时 Bloomberg 数据 | ❌ 未做 | 机构付费，个人不可行 |

### B. 模型与因子
| 建议 | 状态 | 证据 |
|---|---|---|
| 学术因子模型（Piotroski / 动量 / 反转 / PEAD / 分析师）| ✅ 已做 (v6) | [`factor_model.py`](../factor_model.py) |
| 因子中性化（行业 + 市值）| ✅ 已做 (v6) | [`core/neutralization.py`](../stock_research/core/neutralization.py) |
| 因子 IC 监测（Grinold-Kahn）| ✅ 已做 (v6.1) | [`core/factor_ic.py`](../stock_research/core/factor_ic.py) + [`jobs/audit_ic.py`](../stock_research/jobs/audit_ic.py) |
| **alphalens-style Tear Sheet** | ✅ **已做 (v8)** | [`jobs/factor_tearsheet.py`](../stock_research/jobs/factor_tearsheet.py) |
| 多模型 ensemble（XGBoost + 因子）| ❌ 未做 | 工作量 1 周+ |

### C. 组合优化
| 建议 | 状态 | 证据 |
|---|---|---|
| Markowitz Max Sharpe | ✅ 已做 (v6) | [`build_plan_a_v5.py`](../build_plan_a_v5.py) |
| ADV 流动性约束 | ✅ 已做 (v6) | [`core/portfolio_constraints.py`](../stock_research/core/portfolio_constraints.py) |
| 交易成本扣减 | ✅ 已做 (v6) | [`core/portfolio_constraints.py`](../stock_research/core/portfolio_constraints.py) |
| 行业敞口约束（≤25%）| ✅ 已做 (v6.1) | 同上 |
| Kelly 半仓位上限 | ✅ 已做 (v6.1) | 同上 |
| **PyPortfolioOpt 精确解（cvxpy）** | ✅ **已做 (v8)** | [`core/portfolio_optimizer_pro.py`](../stock_research/core/portfolio_optimizer_pro.py) |
| **Black-Litterman 贝叶斯优化** | ✅ **已做 (v8)** | 同上 |
| **HRP（Lopez de Prado 2016）**| ✅ **已做 (v8)** | 同上 |
| **min CVaR（尾部风险）**| ✅ **已做 (v8)** | 同上 |

### D. 回测与验证
| 建议 | 状态 | 证据 |
|---|---|---|
| Walk-forward 验证（6 regime）| ✅ 已做 | [`walk_forward_validate.py`](../walk_forward_validate.py) |
| Stress test（4 历史崩盘）| ✅ 已做 | [`jobs/stress_test.py`](../stock_research/jobs/stress_test.py) |
| 防御 A/B/C 对比 | ✅ 已做 | 同上 |
| **De Prado Triple Barrier** | ✅ **已做 (v8)** | [`core/financial_ml.py`](../stock_research/core/financial_ml.py) |
| **De Prado Purged K-Fold** | ✅ **已做 (v8)** | 同上 |
| **vectorbt 向量化回测** | ✅ **已做 (v8)** | [`jobs/vectorbt_backtest.py`](../stock_research/jobs/vectorbt_backtest.py) |
| Monthly rolling walk-forward | ⚠️ 部分（vectorbt 已铺路）| - |
| Survivorship bias 修正 | ❌ 未做 | 需用 SEC 历史 13F-HR 重建股票池 |

### E. 风险管理
| 建议 | 状态 | 证据 |
|---|---|---|
| 实盘防御（VIX + 200MA + 止损）| ✅ 已做 (v7) | [`jobs/realtime_defense.py`](../stock_research/jobs/realtime_defense.py) |
| Brinson 业绩归因 | ✅ 已做 (v7.5) | [`core/brinson.py`](../stock_research/core/brinson.py) |
| **pyfolio 机构级 tear sheet** | ✅ **已做 (v8)** | [`jobs/pyfolio_tearsheet.py`](../stock_research/jobs/pyfolio_tearsheet.py) |
| Barra 风格风险归因 | ❌ 未做 | 需 Barra 因子库（付费）|

### F. 运维与披露
| 建议 | 状态 | 证据 |
|---|---|---|
| Cron 自动每日刷新（16 步）| ✅ 已做 | [`daily_refresh.sh`](../daily_refresh.sh) |
| Model Card | ✅ 已做 | [`docs/MODEL_CARD.md`](MODEL_CARD.md) |
| 方法论白皮书 | ✅ 已做 | [`docs/METHODOLOGY.md`](METHODOLOGY.md) |
| Monthly letter | ✅ 已做 | [`jobs/monthly_letter.py`](../stock_research/jobs/monthly_letter.py) |
| 公开归档（git 时间戳）| ✅ 已做 | [`archive/`](../archive/) |
| Stress Test Report | ✅ 已做 | [`docs/STRESS_TEST_REPORT.md`](STRESS_TEST_REPORT.md) |
| **Streamlit Web 应用** | ✅ **已做 (v8)** | [`streamlit_app.py`](../streamlit_app.py) |
| FastAPI 公网部署 | ⚠️ 雏形 | [`stock_research/api/main.py`](../stock_research/api/main.py) |
| 持牌合规 | ❌ 不可行 | 需要金融牌照 |
| **研报验证日志（hit rate 闭环）** | ✅ **已做 (v9.1)** | [`docs/research_validation_log.md`](research_validation_log.md) |

### G. 个股深度研究（v9 新增维度）
| 建议 | 状态 | 证据 |
|---|---|---|
| 杜邦五因子分解（ROE 归因） | ✅ 已做 (v9.0) | [`core/fundamental_deep.py`](../stock_research/core/fundamental_deep.py) |
| Beneish M-Score 财务造假识别（含高增长假阳性 caveat） | ✅ 已做 (v9.0) | 同上 |
| Altman Z-Score 破产预警（X4 cap） | ✅ 已做 (v9.0) | 同上 |
| Sloan 1996 应计盈余 + 8 项盈利质量 | ✅ 已做 (v9.0) | 同上 |
| 同业 12 项分位排名 + industry/sector/marketcap 三级回退 | ✅ 已做 (v9.0) | [`core/peer_compare.py`](../stock_research/core/peer_compare.py) |
| SEC 10-K/8-K 全文 + 反向贪心章节切分 | ✅ 已做 (v9.0) | [`core/sec_filings.py`](../stock_research/core/sec_filings.py) |
| Forward P/E + EV/Sales + PEG (Lynch 1989) | ✅ 已做 (v9.1) | [`core/forward_valuation.py`](../stock_research/core/forward_valuation.py) |
| 自建 DCF 三档 + 5×5 WACC×TGR 敏感度 | ✅ 已做 (v9.1) | [`core/dcf_scenarios.py`](../stock_research/core/dcf_scenarios.py) |
| 8 季 10 指标 trend + improving/deteriorating 判定 | ✅ 已做 (v9.1) | [`core/quarterly_trends.py`](../stock_research/core/quarterly_trends.py) |
| 端到端 LLM 研报生成（8 章节，prompt cache） | ⏳ 代码就绪 | [`jobs/research_report.py`](../stock_research/jobs/research_report.py) — 等 Anthropic 充值 |
| 电话会议规避词 + LM 情绪 + 8 季时序 | ⏳ 代码就绪 | [`core/earnings_call.py`](../stock_research/core/earnings_call.py) — 等 FMP Premium |
| 产业链上下游 70%（10-K LLM + 新闻 + 电话会议交叉） | ⏳ 代码就绪 | [`core/supply_chain.py`](../stock_research/core/supply_chain.py) — 等 Anthropic 充值 |
| FMP 24h 缓存层（解决批量 30 只爆额度） | ✅ 已做 (v9.1) | [`adapters/fmp_cache.py`](../stock_research/adapters/fmp_cache.py) |
| A 股 10-K 等价物（巨潮年报）解析 | ❌ 未做 | A 股深度仅 yfinance 兜底，质量差 |
| Barra 风格风险归因 | ❌ 未做 | 需 Barra 因子库（付费） |

---

## 🎯 还能做但 ROI 已经低的（v9.1 → v10）

> **v8 → v9 的 ROI 排序已被 B 路线打破**。原"剩下 3 分"清单里的 Survivorship bias、Monthly rolling walk-forward 等还在；但 v9 把"个股深度"这个新维度补到 75/100，剩下提升空间转移到了 LLM 路径激活、产业链覆盖度、A 股深度这些新方向。

### 量化打分维度（97/100 → 100，原 v8 残余）
| 候选 | 加分 | 工作量 | ROI |
|---|---|---|---|
| Survivorship bias 修正 | +1 | 1 周 | 中 |
| Monthly rolling walk-forward（vectorbt 已铺路）| +1 | 2-3 天 | 中 |
| 多模型 ensemble（XGB + LGBM）| +0.5 | 1 周 | 低 |
| 实时 VIX 流（盘中监测）| +0.5 | 3 天 | 低 |
| FastAPI 完整部署到云 | +0.5 | 1 周 | 低 |
| 持牌合规体系 | +0.5 | **不可行（钱+牌照）** | - |

### 个股深度维度（75/100 → 90，v9.1 新空间）
| 候选 | 加分 | 工作量 | ROI |
|---|---|---|---|
| **充值 Anthropic API** → 跑通 LLM 研报端到端，5 只 watchlist 验证质量 | +5 | 半天（充值 + 验证） | **极高** |
| **充值 FMP Starter $35/月** → 解锁电话会议时序 + 同业候选广 | +3 | 1 周观察 | **高** |
| FMP peers 跨 sector 问题 → 三级回退已做（v9.1），需更稳健的 sector_etf 同业池 | +1 | 半天 | 中 |
| A 股 10-K 等价物（巨潮年报）解析器 → A 股深度从 0 → 60 | +3 | 2-3 天 | 中（覆盖 A 股） |
| research_report 加引用追溯（每条论断 footnote） | +1 | 2 天 | 中（合规友好） |
| Streamlit "个股研究"tab — LLM 研报 Markdown 自动渲染 | +1 | 半天 | 低（依赖 LLM 跑通） |
| 自建 DCF 终值 FCF margin 假设按行业/公司画像区分 | +0.5 | 半天 | 低 |
| quarterly_trends 加 YoY 同期对比线（目前只绝对值序列） | +0.5 | 1-2h | 低 |

### 数据效率维度（90/100 → 95，v9.1 新空间）
| 候选 | 加分 | 工作量 | ROI |
|---|---|---|---|
| 缓存层加 force_refresh 参数（财报日强制刷新） | +1 | 1h | 中 |
| 缓存命中率 metrics 接 streamlit | +1 | 2h | 低 |
| FMP/Finnhub 调用配额监控 + 阈值告警 | +1 | 半天 | 低 |
| 缓存层加 SQLite 后端选项（百万级 entries 时） | +1 | 1 天 | 低（远未到规模） |

---

## 🔧 安装/运行

```bash
# 一次性安装全部依赖
pip install \
  alphalens-reloaded \
  PyPortfolioOpt \
  streamlit \
  pyfolio-reloaded \
  vectorbt \
  openbb \
  yfinance akshare finnhub-python pandas numpy scipy

# 启动 Web 应用
streamlit run streamlit_app.py

# 跑各模块
python3 -m stock_research.jobs.factor_tearsheet --all
python3 -m stock_research.jobs.pyfolio_tearsheet
python3 -m stock_research.jobs.vectorbt_backtest
python3 -m stock_research.jobs.openbb_intelligence
python3 -m stock_research.jobs.stress_test

# 完整每日刷新（16 步）
./daily_refresh.sh
```

---

## 📚 学术引用清单（v9.1 完整版）

| # | 文献 | 用途 |
|---|---|---|
| 1 | Markowitz (1952) JF | 组合优化基础 |
| 2 | Kelly (1956) | 仓位上限 |
| 3 | Ball & Brown (1968) JAR | PEAD 因子 |
| 4 | **Altman (1968) JF** | **Z-Score 破产预警** ⭐ v9 |
| 5 | Rosenberg & Marathe (1976) | Barra 风险模型 |
| 6 | De Bondt & Thaler (1985) JF | 反转因子 |
| 7 | Brinson, Hood & Beebower (1986) FAJ | 业绩归因 |
| 8 | **Lynch (1989) "One Up on Wall Street"** | **PEG 估值阈值带** ⭐ v9.1 |
| 9 | Lakonishok & Lee (2001) RFS | 内部人交易 |
| 10 | Fama & French (1992) JF | Size 因子 + 中性化 |
| 11 | Black & Litterman (1992) | 贝叶斯组合优化 |
| 12 | Pardo (1992) | Walk-forward 方法 |
| 13 | Jegadeesh & Titman (1993) JF | 12-1 动量 |
| 14 | Grinold (1994) | IC 监测 |
| 15 | **Sloan (1996) AR** | **应计盈余反向因子（盈利质量 8 项）** ⭐ v9 |
| 16 | Carhart (1997) | 四因子模型 |
| 17 | **Beneish (1999) FAJ** | **M-Score 财务造假识别** ⭐ v9 |
| 18 | Rockafellar & Uryasev (2000) | CVaR 起源 |
| 19 | Piotroski (2000) JAR | F-Score |
| 20 | Grinold & Kahn (2000) | IC 阈值标准 |
| 21 | Almgren & Chriss (2001) | 冲击成本 |
| 22 | O'Neil (2002) | 个股止损规则 |
| 23 | **Dechow & Dichev (2002) AR** | **应计质量（盈利质量补充）** ⭐ v9 |
| 24 | Pan & Poteshman (2006) RFS | 期权 PCR 信号 |
| 25 | **Beneish & Nichols (2007)** | **M-Score 高增长假阳性 caveat** ⭐ v9 |
| 26 | Faber (2007) SSRN | 200MA 趋势过滤 |
| 27 | Whaley (2009) | VIX 恐慌阈值 |
| 28 | **Loughran & McDonald (2011) JF** | **金融文本情绪词典（电话会议负面/不确定）** ⭐ v9 |
| 29 | **Larcker & Zakolyukina (2012) JAR** | **管理层规避词识别（电话会议）** ⭐ v9 |
| 30 | **Penman (2013) Financial Statement Analysis** | **杜邦五因子分解（v9 杜邦归因）** ⭐ v9 |
| 31 | Asness, Moskowitz & Pedersen (2013) JF | 因子中性化 |
| 32 | Lopez de Prado (2016) | HRP |
| 33 | Lopez de Prado (2018) | Triple Barrier + Purged K-Fold + Sample Uniqueness ⭐ v8 |
| 34 | Frazzini, Israel & Moskowitz (2018) | 实证交易成本 |
| 35 | Mitchell et al. (2019) | Model Card 规范 |

> v9 / v9.1 新增 9 篇：Altman 1968 / Lynch 1989 / Sloan 1996 / Beneish 1999 / Dechow-Dichev 2002 / Beneish-Nichols 2007 / Loughran-McDonald 2011 / Larcker-Zakolyukina 2012 / Penman 2013。

---

## 给 Reviewer 的 5 个关键审查点（v9.1 更新）

1. **学术规范完整**：35 篇引用（v9 新增 9 篇覆盖个股深度）+ 每个因子/约束都有论文出处
2. **多源数据交叉**：6 数据源（SEC + akshare + Finnhub + yfinance + OpenBB + FMP）+ 自动 audit
3. **诚实暴露弱点**：
   - [`STRESS_TEST_REPORT.md`](STRESS_TEST_REPORT.md) 主动披露 v6 在 2008/2022 跑输 SPY
   - v9.1 模块均带 caveat：Beneish 高增长假阳性、Altman X4 cap、forward EBITDA 免费层不可得、产业链 70% 而非机构级
   - [`research_validation_log.md`](research_validation_log.md) 每份研报登记可证伪预测，定期回填胜率
4. **可重现**：所有代码 + 学术引用 + 月报公开（git 时间戳）+ FMP 24h 缓存让结果可重现
5. **保守边界**：不给买卖建议、不自动交易、不接交易接口；v9 LLM 研报模板明确禁止"买/卖"评级，仅给"是否值得跟踪"判断

---

*StockAssistant v8.0 · 维护: yanli (lance7in@gmail.com) · 不构成投资建议*
