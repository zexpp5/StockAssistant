# 自审查后续 TODO（按性价比排序）

来源：[`docs/2026-05-10_self_review.md`](2026-05-10_self_review.md) 实测出来的 gap，按"改起来便宜 → 影响明显"排序。每项给改动位置、判断标准和大致时间。

## P1 · Dashboard 加「压力测试」卡 ★

**为什么做**：自审查里维度 3「诚实暴露」目前只有 70/100。docs 里有 STRESS_TEST_REPORT，但 [stock_dashboard.html](../stock_dashboard.html) 主入口 0 处展示压力测试结果。普通访客看不到这套系统在崩盘期会跑输 SPY 9.77%。

**改在哪**：

- 数据：[docs/STRESS_TEST_REPORT.md](STRESS_TEST_REPORT.md) 含 4 个 regime 的 alpha；可以把它结构化进 DuckDB（写一次性脚本），或直接内联到 `build_stock_dashboard_html.py`
- 渲染：在「📌 概览」tab 或新建「💀 压力测试」tab，渲染一张 4 列卡 + drawdown alpha 数字 + 一句"⚠️ 1/4 抗跌"

**判断标准**：
- 浏览器打开 dashboard 不需要点任何 tab，**首屏就能看到「1/4 抗跌 / 平均 -9.77% drawdown alpha」**
- 数字可点击跳转到 STRESS_TEST_REPORT.md

**估计**：20–30 分钟（结构化数据 + 1 张卡 + 测试）

**完成后维度 3 评分**：70 → 90+

---

## P2 · README 加「最小可重现路径」 ★

**为什么做**：自审查维度 4「可重现」85/100。新人 clone 后不知道哪条路最短跑通。

**改在哪**：[README.md](../README.md) 在「核心能力」之后、「架构」之前加一节：

```markdown
## 快速跑通（不需要飞书凭证）

只看分析（推荐路径）：
  pip install -r requirements.txt
  streamlit run streamlit_app.py
  # 浏览器打开 http://localhost:8501

完整流水线（需自己配 .env）：
  cp .env.example .env
  # 填入 FEISHU_APP_ID / FEISHU_APP_SECRET / FINNHUB_API_KEY
  bash daily_refresh.sh
```

**判断标准**：新机器 clone + 装依赖 + 跑 streamlit 这条路必须 5 分钟内能看到页面

**估计**：5 分钟

**完成后维度 4 评分**：85 → 95

---

## P3 · 删 / 实现 monthly_letter

**为什么做**：MODEL_CARD 列了「v6.1 monthly_letter 月度信件」但 doc 自己也承认"框架就绪，未真正生成第一封"。要么生成第一封，要么从声称里删除。**保留半成品在自评里 = 自欺**。

**两种做法**：

A. **删除**：从 `MODEL_CARD.md` / `2026-05-09_v1到v8演进日志_24到97分.md` 里去掉对 monthly_letter 的引用。 - 估计：5 分钟

B. **生成第一封**：跑 `python3 -m stock_research.jobs.monthly_letter --month 2026-05`，把输出 commit 到 `docs/letters/`。- 估计：依赖脚本能跑通，可能要 fix 几行

**建议**：本月底之前选 B（写一封 5-10 月报，长度 1-2 页就够）；如果到 5 月底还没写出来，就走 A 删除。

---

## P4 · README 提到的 daily_refresh 8 步是过时的

**为什么做**：[README.md](../README.md) 第 25 行写 "daily_refresh.sh # 每日 cron 编排（8 步）"——实际今天已经改到 20 步、并且是 launchd 而不是 cron 了。这是另一处 doc / code 偏离。

**改在哪**：README.md 第 25 行 `（8 步）→（20 步，launchd）`，或者直接写「每日 launchd 自动编排（具体步数见脚本头）」永久避免再过期。

**估计**：1 分钟

---

## P5 · 学术引用清单瘦身

自审查里发现 doc 列了 26 篇但代码 grep 不到 6 个作者：DeBondt / Carhart / Almgren / Asness / Pardo / Mitchell。

**两种做法**：

A. **如果代码确实参照了，给注释加引用**：
```python
# core/neutralization.py 顶部加一行：
# 参照 Asness, Moskowitz & Pedersen (2013) JF "Quality Minus Junk"
```

B. **如果代码确实没用，从 doc 引用清单删除**：把 [`2026-05-09_v1到v8演进日志_24到97分.md`](2026-05-09_v1到v8演进日志_24到97分.md) 第 522-526 行那 6 篇删掉，从「26 篇」变「20 篇」。

**估计**：6 篇逐个核 → 30 分钟，需要读源码判断

**完成后维度 1 评分**：90 → 95

---

## P6 · tushare 已弃用？

代码里 tushare 只在 1 个文件出现，doc 里没声称用 tushare。如果确认弃用：
- 从 `requirements.txt` 删掉（如有）
- 从 `MODEL_CARD.md` 第 3 章「数据来源」移除
- 在 README 数据源列表确认无残留

**估计**：5 分钟

---

## 表格汇总

| # | 任务 | 时间 | 维度 | 优先级 |
|---|---|---|---|---|
| P1 | Dashboard 压力测试卡 | 20–30 min | 维度 3 70→90 | ★★★ |
| P2 | README 最小可重现路径 | 5 min | 维度 4 85→95 | ★★ |
| P3 | monthly_letter（删 / 写） | 5 min – 30 min | 维度 4 / 维度 3 | ★★ |
| P4 | README daily_refresh 步数过时 | 1 min | 一致性 | ★ |
| P5 | 学术引用清单瘦身 | 30 min | 维度 1 90→95 | ★ |
| P6 | tushare 弃用清理 | 5 min | 维度 2 一致性 | ★ |

**全部做完（最佳情况）**：综合分 88 → 95+

---

## 不在这份清单里、但建议长期跟踪的项

- **Forward tracking 累积**：每天 daily_refresh 自动加 1 个数据点，**1 个月后**看板「方案回测」tab 才有真实曲线（不是回测）
- **DuckDB 单一事实来源**：阶段三 cutover——把 `data/snapshots/**/*.json` 全部移除，让 `store.py` 只走 DuckDB 读路径（目前是双写双读）
- **Streamlit Cloud 上线后的反馈环**：部署后跟踪页面访问、看哪些 tab 没人用 → 砍掉冗余 tab
