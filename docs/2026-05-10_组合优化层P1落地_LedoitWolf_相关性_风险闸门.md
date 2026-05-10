# 组合优化层 P1 落地 — Ledoit-Wolf · 相关性剪枝 · 风险闸门

> 维护: yanli (lance7in@gmail.com) · 日期: 2026-05-10 · **不构成投资建议**

## 起源

之前对 [stock_research/core/portfolio_optimizer_pro.py](../stock_research/core/portfolio_optimizer_pro.py)
做完 review，识别出 6 项 P1 改造（按 ROI 排）。本次会话推进其中相关性最强的三项 — 都集中在
组合优化层，可一次会话整体改完，不互相干扰。

| # | 项 | 估时 | 障碍 |
|---|---|---|---|
| 2 | Markowitz 协方差换 Ledoit-Wolf shrinkage | 半天 | 需读 `portfolio_optimizer_pro.py`，可能涉及 cvxpy 重构 |
| 3 | 风险指标反馈到 Markowitz 优化（不只是事后报告）| 1 天 | 同上 |
| 4 | 选股 Pairwise correlation < 0.7 约束 | 半天 | 需历史价格计算相关性矩阵 |

未做（留下次）：① factor_model_china 复权检查；⑤ 多套并行 picks 系统加 production/legacy
头部标记；⑥ 跑 IC 校准把 `data/calibrated_factor_weights.json` 落地。

## 改动落地

### Item 2 · 协方差估计统一接口（默认 Ledoit-Wolf）

[stock_research/core/portfolio_optimizer_pro.py](../stock_research/core/portfolio_optimizer_pro.py)
新增 `_build_cov(returns_df, method=...)` 三档：

- `ledoit_wolf`（默认）— Ledoit & Wolf 2003 收缩估计，收缩目标 `constant_variance`，
  对 N>>T 鲁棒；smoke 实测 Ledoit-Wolf 条件数 1.15 vs 样本协方差 2.13（10 股 × 252 天）
- `sample` — 历史样本协方差（兼容 / 对照用）
- `exp` — 指数加权协方差（近期权重高）

`optimize_max_sharpe / optimize_min_volatility / optimize_black_litterman / optimize_hrp`
全部改用 `cov_method` 参数（默认 `"ledoit_wolf"`）。HRP 内部虽然用 corr 做层次聚类，
仍把 shrinkage cov 一并传入让聚类更稳。

### Item 4 · 选股层相关性剪枝（三档退化）

新增 `prune_correlated(returns_df, ranked_tickers, max_corr=0.7, ...)`：按 ranked 顺序
贪心保留，后来者若与已保留任一股 |ρ| > max_corr 则丢弃；返回 `(kept, dropped)`，dropped
含 `vs / rho / method / n_history` 用于审计。

按样本量分三档退化：

| 历史 | 行为 |
|---|---|
| ≥ 126 天 | 正常 corr 剪枝 |
| 30 ≤ N < 126 | 仍用 corr，但 logger.warning 提示噪音；建议传 industries 启用 fallback |
| < 30 天 | 退化到行业 cap（每行业最多 `industry_cap` 只）；无 industries 则全保留 |

复权依赖在 docstring 里硬性标出 — A 股需前复权 qfq、美股需 adjusted close，
否则分红 / 拆股事件会让协方差和相关性失真（典型案例：银行股相关性虚高）。

### Item 3 · 风险闸门反馈到优化（多级降级）

新增 `risk_aware_optimize(returns_df, ranked_tickers, ...)`，把候选权重的样本内
风险指标当作优化器**内部**闸门 — 与原 [risk_metrics.py](../risk_metrics.py)
的「事后报告」根本区别：

- 事后报告：组合已成形 → 跑出 vol/DD/CVaR → 出问题只能调仓
- 风险闸门：优化器**输出权重前**先做 in-sample 评估 → 直接换更稳的方案

四级降级（任一指标破限就进下一级）：

| Stage | 方法 | max_w | cash 建议 |
|---|---|---|---|
| 0 | max_sharpe (Ledoit-Wolf) | `max_weight` | 5% |
| 1 | max_sharpe（收紧）| `max(0.05, max_w × 0.6, 1.05/N)` | 15% |
| 2 | min_cvar（尾部风险敏感）| 同 stage 1 | 25% |
| 3 | min_volatility 兜底 | 同 stage 1 | 30%（上限）|

默认风险闸门是「中等风险偏好」基线（成长 + 价值混合）：

| 指标 | 中等（默认）| 保守（防御 / 退休账户）| 激进（成长 / 高 Beta）|
|---|---|---|---|
| max_drawdown | -0.25 | -0.15 | -0.40 |
| annual_vol | 0.30 | 0.20 | 0.40 |
| cvar_95_daily | -0.04 | -0.025 | -0.06 |

调用方传 `risk_limits=...` 覆盖即可。

### 接入 v6 主流水线

[stock_research/jobs/optimize_portfolio.py](../stock_research/jobs/optimize_portfolio.py)
默认走 `risk_aware_optimize`：

- `--legacy-mc` 切回 20000 次蒙特卡洛 Markowitz（保留 review 兼容）
- `--max-corr`（默认 0.7）控制 Item 4 剪枝阈值
- 全级 infeasible 时自动 fallback 到 legacy MC（双保险）
- `risk_aware_meta` 写进结果 JSON：stage / stage_label / pruned_dropped /
  effective_cash_pct / warning，留审计

## 关键修补（review 时发现 / 用户增量改进）

工作过程中暴露并修掉的问题：

1. **`N × max_w < 1` 导致 cvxpy infeasible**：单仓上限 0.15 + 6 只股 → 上限和 0.9 < 1.0，
   cvxpy 直接报 "infeasible"。新增 `_feasible_max_weight(returns_df, max_weight)` 强制
   `max(max_weight, 1.05/N)` 缓冲，所有 EfficientFrontier / EfficientCVaR 入口统一接入。

2. **invariant 修正：weights 必须 sum=1**（关键架构决定）：初稿 `_scale_for_cash` 隐式把
   weights 缩到 `sum=0.95`，引入三个隐患 —
   - `weights.sum() ≠ 1` → discrete_allocation 资金分配按 95% 算，留缩水现金
   - `annual_ret/vol` 在 unscaled weights 上计算，与返回的 weights 不一致
   - cash 缓冲不可见（字典里没有 `$CASH` 行）

   重写为 `_attach_cash_meta`：weights 始终 sum=1（"100% 投资"基线视角），cash_pct
   仅作元数据。调用方按 stage 实际建议的 cash 自己缩 `target_w`，
   stage_metrics 也同步乘 `(1 - cash_pct)` 才是实盘组合数字。
   v6 调用方 `optimize_portfolio.py` 显式做这件事，与 legacy MC 路径行为完全对齐
   （legacy MC 内部已缩，新路径外部缩，最终 `target_w.sum() = 1 - effective_cash`）。

3. **每级 stage 的 cash 应该渐进升级**：初稿固定 5% / 第 1 级起 +10pp（15%），其余级别没改。
   实测发现 stage 2/3 已经是「问题严重」状态，cash 应继续抬升。改为 5% / 15% / 25% / 30%
   四档，破线越多 cash 越高，30% 是上限。

4. **prune_correlated 三档退化**：初稿在样本 < 30 天时直接保留全部。但实务上
   watchlist 经常有新股（IPO < 半年）；这种数据上 corr 矩阵极不稳定，"保留全部"
   等于绕过约束。改成 < 30 天时若给了 `industries` 参数则退化到行业 cap，
   缺省 cap=1（每行业只留 ranked 顶部那只）。

5. **HRP 也走 Ledoit-Wolf**：HRP 论文（Lopez de Prado 2016）对协方差不敏感是相对的 —
   距离矩阵的稳定性仍受协方差噪音影响。一致性上传 shrinkage cov 让 HRP 聚类更稳。

## 实测验证

### Smoke test（人造数据）

`/tmp/smoke_optimizer_pro.py`（4 项 smoke）：

- ✅ `Ledoit-Wolf cond# 1.15` < `sample cov cond# 2.13`（收缩起作用）
- ✅ `prune_correlated`：克隆股 S01（与 S00 ρ=0.998）正确剔除
- ✅ `_portfolio_realized_metrics`：等权 10 股年化 vol 7.5% / max DD -4.9% / CVaR_95 -1.0%
- ✅ `risk_aware_optimize`：低波数据 Stage 0 通过；vol_scale=5 数据 Stage 0→1→2→3
  全级破限，min_volatility 兜底（说明这一篮子标的天然太抖，符合预期）

### 真实 v6 流水（top-n=6）

```bash
python3 -m stock_research.jobs.optimize_portfolio --top-n 6 --capital 500000
```

输出（[data/snapshots/optimize/plan_v6_2026-05-10_204717.json](../data/snapshots/optimize/plan_v6_2026-05-10_204717.json)）：

- 因子 Top 6: MU / LRCX / MTZ / SNDK / MOD / GEV
- **相关性剪枝**：丢 LRCX (vs MU ρ=0.713)、SNDK (vs MU ρ=0.726) — 都是半导体相关
- **风险闸门**：vol≈42% > 30% 上限触发，从 Stage 0 → 1 → 2 → 3 全级破限
- **min_volatility 兜底** + 现金提到 52.7%（高 ADV 限流后实际占比）
- 行业敞口约束二次触发：⚡ AI 电力链 62.7% → 25%

闸门正确识别这一篮子是高波动结构 → 自动放大现金缓冲。

## 当前状态

| 项 | 状态 |
|---|---|
| portfolio_optimizer_pro.py 升级 | ✅ Ledoit-Wolf / prune_correlated / risk_aware_optimize 全部上线 |
| optimize_portfolio.py 接入 | ✅ 默认走新路径，`--legacy-mc` 兼容老 review |
| Smoke + 真实 v6 端到端 | ✅ 已实测 |
| 单元测试 | ⚠️ 仅 `/tmp/smoke_optimizer_pro.py`（一次性脚本，未入库） |

## 下一步

仍剩的 P1（按 ROI 排）：

| # | 项 | 估时 | 障碍 |
|---|---|---|---|
| 1 | factor_model_china 复权检查（动量因子是否用了前复权价）| 半小时 | 模块在 repo 外，需读源码确认 |
| 5 | 多套并行 picks 系统加 production/legacy 头部标记 | 半小时 | 纯文档 |
| 6 | 实际跑 IC 校准把 `data/calibrated_factor_weights.json` 落地 | 1-2 天 | 需历史 watchlist 数据 |

短期建议：把 `/tmp/smoke_optimizer_pro.py` 整理进 `tests/`，至少 risk_aware_optimize
四级 fallback 路径需要 CI 守门，避免后续改动悄悄让 stage 1+ infeasible。

## 已知遗留

- `optimize_portfolio.py` 第一步 `feishu.fetch_watchlist()` 报
  `No module named 'douyin_to_feishu'` — 与本次 P1 无关，是飞书 adapter 的
  环境依赖问题，本次未触碰。当前会让 `skip_neutralize` 自动转为 True。

---

*StockAssistant v8 组合层 P1 落地 · 维护: yanli (lance7in@gmail.com) · 不构成投资建议*
