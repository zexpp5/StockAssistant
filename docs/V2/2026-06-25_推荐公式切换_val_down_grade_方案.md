# 推荐公式切换方案：现公式 → val_down_grade（降估值 + 加评级）

> 2026-06-25 草拟 · v2（2026-06-25 评审补丁）· 状态：**待评审，未实施** · 决策人：用户（真钱相关，改打分公式必须用户拍板）
> 关联：[[docs/V2/2026-06-09_AI与科技成长推荐规则.md]] §17-19、记忆 `project_weight_variant_shadow_pipeline` / `project_ai_strategy_unvalidated_and_concentration`

---

> ## ⚠️ 性质定性（评审硬规则，写在最前面）
> **本次只切美股主排序，属于「试运行版」改善主排序——不是策略已被完全证明。**
> - 影子整体判定仍是 **BLOCKED**，样本薄：美股 **1D n=70 / 5D n=30**。
> - **回测是近似代理**（省略了 f_score / data_usability 两个常数因子），只验证「降估值+评级」**方向**，≠ 完整新公式历史收益。
> - **最终胜负看前向对照**：每天记新公式 vs 老公式 1D/5D alpha，**连续转差立即回滚**。
> - 老公式不删，降级为影子 `legacy_baseline` 永久对照。

---

## 1. 一句话

把美股生产打分公式从「估值主导」换成 **val_down_grade（估值砍半 + 加入评级因子）**，老公式降级为影子 baseline 继续对照。港/A 是否切，见 §6 待决项。

## 2. 为什么切（证据，按"前向优先"排序）

> 方法论纪律：**先看前向真金，再用回测解释原因**，不被回测数字带跑。

| 证据 | 现公式 | val_down_grade | 结论 |
|---|---|---|---|
| **前向真金**（shadow top10，样本外） | 美股 1日 -0.20% / 5日 +1.87% | 美股 1日 **+0.26%** / 5日 **+3.14%** | 两个周期都赢生产 ✅ |
| 因子 IC | 估值在 AI 池为负/反向；评级 IC +0.042 t=2.25（五个半年切片全正） | — | 降估值 + 加评级方向硬 ✅ |
| 回测代理（2 年） | 净 +98%（vs QQQ +61pp，但 2026H1 alpha≈+0.05 已塌） | 净 +117% | 不差，但**别拿回测当主证据** |
| 反例 | rev_grade_5050 回测 +384% **但前向哑火**（1日-0.09%） | — | 印证回测幸存者偏差，**不选回测冠军** |

> 🚨 **回测口径必读**：`backtest_formula_proxy.py` 为历史可得性，**省略了 f_score 和 data_usability 两个常数因子**（见其 docstring L13-14：「data_usability/f_score 为常数项已略去，排名等价」）。所以上表 val_down_grade「+117%」**只反映 动量/估值/反转/评级 四项**，f_score 那 0.20 权重没进回测。**回测只能证明「降估值+加评级」这个方向，不等于完整新公式的历史收益。** 完整公式的真实表现，只能靠切换后的前向对照来判。

**双轨重排实测**（2026-06-25 最新美股批，候选 vs 现规则）：候选把"质量好但账面贵"的票提前——VRT ↑12、GEV ↑10、AMZN ↑7、AVGO ↑5、TSM ↑5；把 NVDA ↓7、META ↓7、VST ↓9 降级。经济含义清楚：**不再因为"贵"就惩罚成长/质量龙头**。

## 3. 公式对比

```
现公式（strategy_version = tech_ai_v2_usable_data_gate）:
  total = 0.15·动量 + 0.50·估值 + 0.15·反转 + 0.20·数据质量
  · F分(f_score)：算了但不进总分（仅透传）
  · 评级(grade)：生产不计算
  · reversal 子权重由 calibrated_factor_weights.json 读，IC 失效自动归零

候选 val_down_grade:
  total = 0.15·动量 + 0.25·估值 + 0.20·反转 + 0.20·F分 + 0.20·评级
  · 估值 0.50 → 0.25（砍半，去减分项）
  · 新增 评级 0.20（IC 已 PASS 的唯一强因子）
  · F分 0 → 0.20（原本就算，只是没入总分）
```

## 4. 改动清单（中等 · 半天级 · 可回滚）

| # | 改动 | 文件 | 大小 |
|---|---|---|---|
| 0 | **「只切美股」不是天然支持，是本方案最大施工点**：现 `build_v2_recommendations.py` 是**全局单一** `STRATEGY_VERSION`(L47) + **统一打分入口** `_factor_scores(row)`(L252) **无分市场分支**。直接改权重会**三市场一起变**。必须改成分市场：`if market=='US' → val_down_grade else 老公式`，并在 **params_json / 结果说明里写清「混合版本：US=val_down_grade, HK/A=legacy」**，否则策略验证会把三市场当同一公式误读 | build_v2 | 🟡 中 |
| 1 | 改权重 + F分入总分（**仅 US 分支**） | `scripts/tools/build_v2_recommendations.py` `_factor_scores`(约 L256-299) | 🟢 小 |
| 2 | **接入评级因子到生产打分**：打分时 PIT 查 `analyst_grade_events` 算窗口内净上调 → `grade_score_from_net`，写入 scores 并入总分。逻辑复用 `replay_weight_variants.inject_grade_scores` / `grade_score_from_net`，建议下沉到 `stock_research/core/`（单一来源，replay 与生产共用） | build_v2 + 新 core 模块 | 🟡 中 |
| 3 | F分/评级**缺失兜底**：F分美/港才有、评级美股专属 → 缺失记中性 50 或按市场归一化（见 §6） | build_v2 | 🟢 小 |
| 4 | 老公式注册为**影子 baseline**：`prod_recheck` 已在 replay VARIANTS；夜班 shadow 跑它对照 | shadow 配置 | 🟢 小 |
| 5 | **升 strategy_version → `tech_ai_v3_us_val_down_grade`** + 下游对齐：打分说明串(L1525-1526)、production_acceptance_check、strategy_eval 口径 | 多文件 | 🟡 中 |
| 5b | **alpha_trend_logger 改双轨**：现只记"最新版本"，需改成同时记 **新公式 vs 老公式(legacy_baseline)** 的 1D/5D alpha，喂 §6 的回滚线判断；历史曲线保留旧版本不覆盖 | alpha_trend_logger.py | 🟢 小 |
| 6 | 确保 `analyst_grade_events` 表每晚刷新（夜班 23d3c 已有，复核新鲜度） | 复核 | 🟢 小 |

## 5. 两个必须先知道的后果

1. **升版 = 验证样本清零重攒。** strategy_eval / alpha 走势 / dashboard 验证都按"最新 strategy_version"过滤。一升版，现攒的 n=220（美股1日）归零，**alpha 走势线断、从头攒**。这是诚实做法（新公式=新战绩），但要接受验证要重新积累 ~2-3 周。
2. **真钱 picks 立刻变**：升版后第一批出单即新名单（NVDA/META/VST 降、VRT/GEV/AMZN 升）。**且前向证据仍薄（n20-70）**——这是在"真实但样本不厚"的信号上行动，非铁证如山。

## 6. 拍板（评审后定稿的执行姿势）

**只切美股 AI 推荐主排序，混合版本，试运行。** 具体：

- **版本号**：`tech_ai_v3_us_val_down_grade`（名字里带 `us_`，自带"仅美股"语义）；混合版本里 HK/A 仍记为 legacy。
- **美股**：上完整 val_down_grade（含评级 + F分入总分）。
- **港股 / A股**：**暂不切**，维持现公式。理由：评级因子本就美股专属（analyst_grade_events 只有美股），A 股记忆判定是"池子问题，权重救不了"。
- **页面标注**：AI 推荐页（美股）显示「当前主规则：tech_ai_v3_us_val_down_grade · 🧪试运行」。
- **老公式**：继续作为 `prod_recheck` / `legacy_baseline` 影子对照，**永久保留不删**。
- **策略验证**：从新版本**重新计数**（n 归零重攒），但**历史页保留旧版本曲线**（别覆盖，要能对照"换公式前 vs 后"）。
- **每日对照 + 回滚线**：alpha_trend_logger 每天记 新公式 vs 老公式 1D/5D alpha；**连续转差（如新公式 5D alpha 连续 3 个交易日 < 老公式）立即 git revert 回滚**。

## 7. 回滚方案

- 公式纯权重 + 因子接入，`git revert` 即回老公式。
- 评级因子接入若出问题（表空/PIT 错），grade 记中性 50 → 自动退化为「降估值版」，不崩。
- 老公式同时在影子跑，切换后仍有对照，随时比对决定回不回。

## 8. 验收（实施后自测）

- [ ] 新公式出单的美股 top10 与双轨面板「新名次」一致（同源校验）
- [ ] 港/A 按选定方案正确处理（A：维持现公式；B：归一化无 grade）
- [ ] strategy_version 已升，alpha_trend_logger / dashboard 验证按新版本重新计数（从 0 起）
- [ ] 老公式 baseline 在影子正常产出，可对照
- [ ] `git revert` 演练一次确认可回滚
