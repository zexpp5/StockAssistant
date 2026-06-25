# 推荐公式切换方案：现公式 → val_down_grade（降估值 + 加评级）

> 2026-06-25 草拟 · v3（2026-06-25 执行后修订）· 状态：**影子能力已实施，生产默认未激活** · 决策人：用户（真钱相关，改打分公式必须用户拍板）
> 关联：[[docs/V2/2026-06-09_AI与科技成长推荐规则.md]] §17-19、记忆 `project_weight_variant_shadow_pipeline` / `project_ai_strategy_unvalidated_and_concentration`

---

> ## ✅ 2026-06-25 执行结论（最新）
> **代码能力已经就位，但今天不默认切生产。**
> - 新公式 `val_down_grade` 已能在生产脚本里计算；但默认只作为 **shadow / dual-track 候选**。
> - 生产默认仍是老公式 `tech_ai_v2_usable_data_gate`；只有显式设置 `US_VAL_DOWN_GRADE_ACTIVE=1` 才会激活美股新公式并写 `tech_ai_v3_us_val_down_grade`。
> - 老公式继续作为 `legacy_baseline`，新旧公式对照改成从 `factor_snapshot_universe` 全量合格候选池各自选 Top20。
> - 修正后的共同资格闸口径下，最新双轨结果显示新公式暂未胜出：
>   - 1D：新公式 -0.06% vs 老公式 +0.05%，新-旧 **-0.10pp**
>   - 5D：新公式 +0.76% vs 老公式 +1.03%，新-旧 **-0.27pp**
> - 因此本次执行选择：**不让夜间生产任务自动切到一套尚未赢过老公式的规则**；继续前向观察，等新公式按全池合格口径连续胜出后再激活。

---

> ## ⚠️ 性质定性（评审硬规则，写在最前面）
> **本次只切美股主排序，属于「试运行版」改善主排序——不是策略已被完全证明。**
> - 影子整体判定仍是 **BLOCKED**，样本薄：美股 **1D n=70 / 5D n=30**。
> - **回测是近似代理**（省略了 f_score / data_usability 两个常数因子），只验证「降估值+评级」**方向**，≠ 完整新公式历史收益。
> - **最终胜负看前向对照**：每天记新公式 vs 老公式 1D/5D alpha，**连续转差立即回滚**。
> - 老公式不删，降级为影子 `legacy_baseline` 永久对照。

---

## 🅿️0 前置硬约束：新旧公式必须在「全量候选池」同时打分（不补这条不许开工）

**问题**：现双轨脚本 `build_dual_track_ranking.py`(L49) 走 `replay.load_picks` → 读 `recommendation_picks`（**生产 Top20**）再重排。一旦生产主规则切成 val_down_grade，`recommendation_picks` 里**已经是新公式预筛过的票**，老公式在这批里重排 = **「在新公式筛过的结果里比老公式」**，对照失真——你以为在比新旧公式，其实在比"新公式选剩的老公式"。前向 alpha 对照同理会被污染。

**硬要求**：
1. 新公式、老公式（legacy_baseline）**必须基于同一批「全量合格候选池」分别独立打分、各自产出 Top20**，绝不在任一方的 Top20 内做影子重排。
2. 全量池数据源 = **`factor_snapshot_universe`**（build_v2 截断前的全宇宙快照，~370 只/天；实测表已存在、今日 6-25 有数）。**不是 `recommendation_picks`。**
3. 影响两处实现，实施时一并改：
   - `build_dual_track_ranking.py`：取数从 `load_picks`(recommendation_picks) 改为读 `factor_snapshot_universe` 当日全量，对每只算 新/老 两套分 → 各自排名 → 对照。
   - `alpha_trend_logger` 双轨（§4 #5b）：新旧 alpha 必须来自各自在全量池选出的 Top-N，而非共享同一批 picks。
4. 验证口径一句话：**「同池、各选、再比」**。

---

## 1. 一句话

原目标是把美股生产打分公式从「估值主导」换成 **val_down_grade（估值砍半 + 加入评级因子）**，老公式降级为影子 baseline 继续对照。**实际执行后因严格双轨未胜出，改为：生产保留老公式，新公式进入完整影子对照；港/A 暂不切**（见 §6）。

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
| 3 | F分/评级**缺失兜底**：仅美股走新公式，**美股缺 F分/评级时记中性 50**；**港/A 不进入新公式、维持 legacy**（不做归一化，不切就不存在缺失问题） | build_v2 | 🟢 小 |
| 4 | 老公式注册为**影子 baseline**：`prod_recheck` 已在 replay VARIANTS；夜班 shadow 跑它对照 | shadow 配置 | 🟢 小 |
| 5 | **升 strategy_version → `tech_ai_v3_us_val_down_grade`** + 下游对齐：打分说明串(L1525-1526)、production_acceptance_check、strategy_eval 口径 | 多文件 | 🟡 中 |
| 5b | **alpha_trend_logger 改双轨**：现只记"最新版本"，需改成同时记 **新公式 vs 老公式(legacy_baseline)** 的 1D/5D alpha，喂 §6 的回滚线判断；历史曲线保留旧版本不覆盖 | alpha_trend_logger.py | 🟢 小 |
| 6 | 确保 `analyst_grade_events` 表每晚刷新（夜班 23d3c 已有，复核新鲜度） | 复核 | 🟢 小 |

## 5. 两个必须先知道的后果

1. **升版 = 验证样本清零重攒。** strategy_eval / alpha 走势 / dashboard 验证都按"最新 strategy_version"过滤。一升版，现攒的 n=220（美股1日）归零，**alpha 走势线断、从头攒**。这是诚实做法（新公式=新战绩），但要接受验证要重新积累 ~2-3 周。
2. **真钱 picks 立刻变**：升版后第一批出单即新名单（NVDA/META/VST 降、VRT/GEV/AMZN 升）。**且前向证据仍薄（n20-70）**——这是在"真实但样本不厚"的信号上行动，非铁证如山。

## 6. 拍板（评审后定稿的执行姿势）

**执行后口径：只切影子，不默认切生产；保留显式激活开关。** 具体：

- **默认生产版本号**：`tech_ai_v2_usable_data_gate`。
- **显式激活版本号**：`tech_ai_v3_us_val_down_grade`（设置 `US_VAL_DOWN_GRADE_ACTIVE=1` 后才使用；名字里带 `us_`，自带"仅美股"语义）；混合版本里 HK/A 仍记为 legacy。
- **⚠️ strategy_version 是 run 级别、不分市场**——同一批 run 里 HK/A 仍是旧公式，所以**必须在 `params_json` 里逐市场写清**，否则策略验证看见同一个 v3 会误以为三市场都换了：
  ```
  per_market_formula:
    US = val_down_grade
    HK = legacy
    A  = legacy
  ```
- **美股**：默认仍上老公式；`val_down_grade` 在 shadow/dual-track 中完整计算（含评级 + F分入总分）。若用户强制激活，则生产使用该公式。
- **港股 / A股**：**暂不切**，维持现公式。理由：评级因子本就美股专属（analyst_grade_events 只有美股），A 股记忆判定是"池子问题，权重救不了"。
- **页面标注**：AI 推荐页（美股）默认应显示「当前主规则：tech_ai_v2_usable_data_gate；候选规则：val_down_grade 影子观察」。强制激活后才显示「tech_ai_v3_us_val_down_grade · 🧪试运行」。
- **老公式**：继续作为 `prod_recheck` / `legacy_baseline` 影子对照，**永久保留不删**。
- **策略验证**：默认继续按旧生产版本计数；`alpha_trend_logger` 额外记录新旧公式全池对照。若强制激活 v3，再从新版本重新计数（n 归零重攒）。
- **每日对照 + 回滚线**：alpha_trend_logger 每天记 新公式 vs 老公式 1D/5D alpha；**连续转差（如新公式 5D alpha 连续 3 个交易日 < 老公式）立即 git revert 回滚**。

## 7. 回滚方案

- 公式纯权重 + 因子接入，`git revert` 即回老公式。
- 评级因子接入若出问题（表空/PIT 错），grade 记中性 50 → 自动退化为「降估值版」，不崩。
- 老公式同时在影子跑，切换后仍有对照，随时比对决定回不回。

## 8. 验收（实施后自测）

- [x] 新公式可在强制激活 dry-run 下生成美股新排序（同源校验）
- [x] **【P0】双轨对照基于 `factor_snapshot_universe` 全量池「同池各选再比」**，新旧公式各自独立产出 Top20，不在任一方 Top20 内重排；US 先套共同资格闸
- [x] 港/A 维持 legacy，双轨候选=基线，结果不因本次切换变化
- [x] 默认 `params_json` / dry-run 已逐市场写 per_market_formula（US=legacy / HK=legacy / A=legacy）；强制激活时 US=val_down_grade
- [x] strategy_version 默认不升，避免未验证规则污染生产；强制激活时才升 `tech_ai_v3_us_val_down_grade`
- [x] 老公式 baseline 在影子正常产出，可对照
- [x] alpha_trend_logger 已增加新旧公式全池对照
- [ ] `git revert` 演练一次确认可回滚
