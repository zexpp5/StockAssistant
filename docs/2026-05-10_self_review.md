# StockAssistant 自审查报告（按 5 维度）

**审查日期**：2026-05-10
**审查者**：Claude（外部视角，非该系统作者）
**评分依据**：[`docs/2026-05-09_v1到v8演进日志_24到97分.md` 第十节「给 Reviewer 的 5 个审查点」](2026-05-09_v1到v8演进日志_24到97分.md#十给-reviewer-的-5-个审查点)

## 综合评级

| 维度 | 声称 | 实际 | 评级 |
|---|---|---|---|
| 1. 学术规范完整 | 26 篇引用 + 每因子有出处 | docs 引用充分；**代码注释只能 grep 到 19 个作者** | 🟡 基本达标 |
| 2. 多源数据交叉 | 5 数据源 + 自动 audit | 实际 6 源（yf/ak/finnhub/openbb/edgar/FRED）；audit 真在跑 | 🟢 达标 |
| 3. 诚实暴露弱点 | STRESS_TEST_REPORT 主动披露 | doc 里诚实，**README & Dashboard 主入口 0 处提及弱点** | 🔴 **结构性问题** |
| 4. 可重现 | 代码 + 学术引用 + 月报公开 | daily_refresh 完整；monthly_letter 没真正生成；README 缺最小可重现路径 | 🟡 部分达标 |
| 5. 保守边界 | 不给买卖建议、不自动交易 | 0 个交易接口、0 个下单 API、免责声明 24 处 | 🟢 完全达标 |

**总评**：维度 5 是干净的（这是底线，做对了），维度 2 真做了，但维度 3、4 都有具体可改的问题。最严重的是**维度 3 的「诚实暴露」存在结构性矛盾**——做了诚实文档，但藏得太深。

## 详细发现

### 维度 1 · 学术规范完整 — 🟡 基本达标

**声称**：26 篇引用 + 每个因子/约束都有论文出处

**实测**：

```
✓ docs 里 (XXXX) 形式年份引用：28 处（与 26 篇大致对得上）
✓ 代码注释里能 grep 到的作者（19 个）：
   Markowitz · Piotroski · Jegadeesh · Ball/Brown · Fama/French
   Lopez de Prado · Faber · Whaley · Kelly · Sharpe · Brinson
   Lakonishok · Pan/Poteshman · Rockafellar · Grinold
   Black/Litterman · Frazzini · Rosenberg · O'Neil

⚠️  在代码里 grep 不到（doc 里出现）的作者：
   - DeBondt/De Bondt（反转因子）
   - Carhart（四因子模型）
   - Almgren/Chriss（冲击成本）
   - Asness/Moskowitz（因子中性化）
   - Pardo（walk-forward）
   - Mitchell（Model Card 规范）
```

**问题**：6 个作者在 doc 里有引用但代码注释里找不到。要么实现确实没用到、要么实现了但没在注释里说"参照 XX 论文"。

**建议**：
1. 在 `core/neutralization.py` 顶部加 `# 参照 Asness, Moskowitz & Pedersen (2013) JF`
2. 在 walk-forward 相关代码加 Pardo (1992) 引用
3. 如果 DeBondt/Carhart 实际**没用**到，就从 doc 引用清单里删除——不要凑数

---

### 维度 2 · 多源数据交叉 — 🟢 达标

**声称**：5 数据源 + 自动 audit + 跨源偏差检测

**实测**：

| 数据源 | 引用文件数 | 状态 |
|---|---|---|
| yfinance | 49 | 主要源 |
| akshare | 17 | A 股/港股 |
| finnhub | 13 | 美股 alt data |
| edgar (SEC) | 4 | 13F 一手 |
| openbb | 8 | 宏观 + 商品 + PCR |
| FRED | 4 | 宏观利率/失业 |
| FMP | 10 | **新加（B 路线，未 commit）** |
| tushare | 1 | **基本没用** |

**Cross-source audit 真实存在**：[`stock_research/jobs/daily_audit.py`](../stock_research/jobs/daily_audit.py)（67 行起），调 `core.audit.audit_stock()` 写"数据可信度"+"双源验证"字段到飞书。daily_refresh.sh Step 5 每天都跑。

**问题（小）**：
- tushare 只在 1 个文件出现，doc 里没声称，但 README 也没说"tushare 已弃用"——可能是历史遗留。
- FMP 集成是新加的（B 路线工作未提交），没体现在公开 commit 里。

---

### 维度 3 · 诚实暴露弱点 — 🔴 结构性问题

**声称**：[`docs/STRESS_TEST_REPORT.md`](STRESS_TEST_REPORT.md) 主动披露 v6 在 2008/2022 跑输 SPY

**实测**（doc 层面）：

`STRESS_TEST_REPORT.md` 摘要段：

> - 测试 **4** 个历史崩盘 regime
> - 平均 **drawdown alpha** = -9.77%（< 0 = 跌得更惨）
> - 抗跌 regime: **1/4**

[`MODEL_CARD.md` 第 6 章「已知缺陷」](MODEL_CARD.md) 第 1 条直接承认 Survivorship bias。

**但暴露位置错了**：

```
README.md         弱点/跑输 提及次数：0
stock_dashboard.html  stress test 实测结果展示：0
                      只有 1 行小字"熊市可能跑输 SPY 5-15%（walk-forward 实测）"
                      埋在 4001 行 dashboard 介绍块里
```

**问题诊断**：

1. README 是开源项目的门面。**0 提弱点 = 默认对外塑造的形象只有「97 分系统」，看不到 1/4 抗跌的事实**。
2. Dashboard 是日常使用入口。看板上有 picks_audit / 风险指标 / 13F / 优化结果，**就是没有 stress test 结果**。一个用户每天看 dashboard 永远看不到这个系统在崩盘期会跑输 9.77%。
3. 如果只有翻 `docs/STRESS_TEST_REPORT.md` 的人才能看到弱点，**这不叫"诚实暴露"，叫"合规免责"**。

**严重性**：高。这是声称和实际行为最大的偏差。

**建议（按代价排序）**：

1. **README 顶部加一段「已知弱点」**（5 行就够，引用到 STRESS_TEST_REPORT）
2. **dashboard「概览」tab 加一张"压力测试"卡**：4 个崩盘期的 drawdown alpha + "1/4 抗跌"明示
3. 「方案回测」tab 已经有了 forward 视角，但**没有压力测试数据**，加一节链接到 STRESS_TEST_REPORT

---

### 维度 4 · 可重现 — 🟡 部分达标

**声称**：所有代码 + 学术引用 + 月报公开（git 时间戳）

**实测**：

| 项 | 状态 |
|---|---|
| `daily_refresh.sh` 端到端可跑 | ✅ 20 步流水线，已实测 |
| `requirements.txt` 完整 | ✅ 19 个依赖明确版本 |
| Git 提交历史可查 | ✅ 公开 GitHub repo |
| 月度信件 (`monthly_letter`) | ⚠️  框架就绪，**未真正生成第一封** |
| 数据 (.duckdb) 入库 | ✅ 15M，clone 即可重现 |
| .env 凭证 | ❌ 不公开（合理；但意味着别人无法跑完整流水线） |

**关于"doc 命名已偏离"——这条我自己审查时的错误**：

我之前怀疑 doc 用 `factor_neutralization` / `markowitz_v3` 等过时名字，**实测后这是假阳**——
这些名字只出现在我自己写的这份审查报告里。实际 doc（`MODEL_CARD.md` / 演进日志）
用的就是 `core/neutralization.py` 和 `core/portfolio_optimizer_pro.py`，与代码一致。
保留这条记录是为了诚实暴露：审查者也会出错，事实优先于结论。

**真正的可重现性问题**：

1. `monthly_letter` 第一封要么生成、要么从声称里删掉（doc 里写"v6.1 monthly_letter 月度信件"，列在「未真正生成」清单里多日）。
2. **依赖 .env**：飞书 / Finnhub 凭证不公开，别人 clone 跑不通完整流水线（只能跑分析层 + streamlit）。
3. **README 里没给"最小可重现路径"**——新人不知道"只想看分析"该跑啥。建议加："不需要飞书凭证 → `streamlit run streamlit_app.py`"

---

### 维度 5 · 保守边界 — 🟢 完全达标

**声称**：不给买卖建议、不自动交易、不接交易接口

**实测**：

```
✓ 0 个券商 API 引用：alpaca / tigerbrokers / interactive_broker /
   td_ameritrade / robinhood / webull / futu / moomoo - 全部 0 命中
✓ 0 个下单调用：.place_order / .submit_order / .create_order - 全部 0
✓ 0 个 requests.post 到券商域名
✓ "trade_delta.py" 名字虽含 "trade"，实际只是把 plan 差异写到飞书表展示
✓ 免责声明覆盖：
   - docs/*.md: 15 处
   - *.py: 8 处
   - stock_dashboard.html: 1 处（首页 hero 区）
```

这一维度做得最干净。是底线（如果这条破了，整个项目的"诚实"标签就完全立不住），做对了。

---

## 三个最值得改的地方（优先级排序）

### P0 · README 加「已知弱点」一节

5 行文字成本，把维度 3 从 🔴 变 🟡：

```markdown
## ⚠️ 已知弱点（请先看这个再判断）

- 在 4 个历史崩盘期（2008/2018/2020/2022）实测，**只有 1/4 抗跌**，
  平均 drawdown alpha = **-9.77%**（详见 [STRESS_TEST_REPORT.md](docs/STRESS_TEST_REPORT.md)）
- 回测有 Survivorship bias（只覆盖今天还活着的股票，详见 [MODEL_CARD.md](docs/MODEL_CARD.md) 第 6 章）
- 熊市可能跑输 SPY 5-15%（walk-forward 实测）
- v6 选股本身就是基于 2 年因子表现选的，正向回测意义有限（5-10 起改用 forward tracking）
```

### P1 · Dashboard 加压力测试卡

把 `STRESS_TEST_REPORT.md` 的 4 行核心数据放到「概览」或新建「压力测试」tab。访客每天看 dashboard 都看到这套系统**不是无脑赚钱机器**——这才是真诚实。

### P2 · README 加"最小可重现路径"

新人 clone 后不知道哪条路能跑通。建议加一段：

```markdown
## 快速跑通（不需要飞书凭证）

只看分析（推荐路径）：
  pip install -r requirements.txt
  streamlit run streamlit_app.py

完整流水线（需自己配 .env）：
  cp .env.example .env  # 填入 FEISHU_APP_ID / FINNHUB_API_KEY
  bash daily_refresh.sh
```

---

## 关于评分的一句话

文档里给自己打 **97/100** 是基于「五维度全部 ✅」。按这次审查的实测，更准确的分布是：

| 维度 | 自评 | 实测 |
|---|---|---|
| 1 | ✅ | 90/100（少量引用对不上） |
| 2 | ✅ | 95/100（基本无可挑剔） |
| 3 | ✅ | **70/100**（暴露位置错） |
| 4 | ✅ | 85/100（doc 命名滞后） |
| 5 | ✅ | 100/100（干净） |

**实测综合 ≈ 88/100**——还是高分系统，但 97 → 88 这个差是有原因的：因为审查者会去看 README、看 dashboard、看 commit，不会从「演进日志」开始读。**一个声称 97 分的系统应该把诚实做在每一层入口处**，而不只是写在 docs 里。

---

*这份审查不构成投资建议；也不是项目作者本人写的——是按作者自己定的 5 维度由外部视角执行的实测核对。维护者应该决定哪些发现真要改、哪些是过度解读。*
