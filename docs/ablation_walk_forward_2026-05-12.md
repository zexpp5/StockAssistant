# Walk-Forward 仓位约束消融测试 · 2026-05-12

**回应二审第二轮**："已写函数没消融不能上生产"。本文档是**指标准入门槛门 3** 的首次实战。

**测试窗口**：2015-01 ~ 2020-12（6 年，72 月，含 2018 Q4 + 2020/03 COVID 两次崩盘）
**Universe**：12 只科技股 NVDA / TSM / GOOGL / MSFT / AAPL / AMD / AVGO / MRVL / META / AMZN / VRT / LRCX
**Benchmark**：SPY
**因子模型**：3 月动量 + 1 月反转 等权（D 系列 walk_forward 简化版）
**Top K**：每月 5 只等权

---

## 实验设计

| 配置 | enable_kelly_cap | enable_atr_stop | 现金部分 |
|---|---|---|---|
| **A baseline** | ❌ | ❌ | 0%（等权 top-5 = 100%） |
| **B +Kelly** | ✅ 单股 ≤ 7.5% | ❌ | 5 × 7.5% = 62.5% 现金 |
| **C +ATR** | ❌ | ✅ -15% 月内日级 | 仅触发日 |
| **D 双开** | ✅ | ✅ | 同 B + 触发日 |

---

## 实测结果（2026-05-12 跑完，72 个 OOS 月度，含 2018Q4 + 2020/03 COVID）

| 配置 | 总超额 % | 年化 Sharpe | 最大回撤 | vs baseline Sharpe Δ | MDD 改善 |
|---|---|---|---|---|---|
| **A baseline** | **+672.20%** | 1.77 | -18.56% | — | — |
| **B +Kelly cap** | +74.18% | **2.10** ⬆ | **-6.38%** ⬆ | **+0.33** ✅ | **+12.18pp** ✅ |
| **C +ATR stop** | +577.00% | 1.66 ⬇ | -15.63% | **-0.11** ❌ | +2.93pp ✅ |
| **D Kelly+ATR** | +62.46% | 1.99 | **-5.28%** ⬆⬆ | +0.22 ⚠️ | **+13.28pp** ✅ |
| **E +BAB defense** | +486.84% | 1.71 ⬇ | -18.82% | **-0.06** ❌ | **-0.26pp** ❌ |
| **F Kelly+BAB** | +58.35% | **2.19** ⬆⬆ | **-5.42%** ⬆⬆ | +0.42 ✅<sup>*</sup> | +13.14pp ✅ |

⬆ = 改善 / ⬇ = 恶化
<sup>*</sup> F vs A 通过；但 F **vs B (Kelly 单开)** Sharpe Δ 仅 +0.09，BAB 边际增益 < +0.3 阈值

---

## 准入门槛 4 件套判定

| 4 件套指标 | 阈值 | B Kelly | C ATR | D 双开 |
|---|---|---|---|---|
| Sharpe Δ | ≥ +0.3 | **+0.33** ✅ | -0.11 ❌ | +0.22 ❌ |
| MDD 改善 | ≥ +2pp | **+12.18pp** ✅ | +2.93pp ✅ | **+13.28pp** ✅ |
| Turnover 增加 | ≤ +50% | 同 baseline ✅ | +月内 reroute（实测每月平均 ~1 只触发 < 50%）✅ | 同 B+C ✅ |
| 错杀率 | ≤ 15% | N/A（Kelly 不止损，无错杀）| **C 是问题** — 高波动 universe 月内 -15% 触发频繁，但很多触发月当月最终翻红（错杀） | 同 C |

**判定结果**：

| 配置 | 4 件套 | 准入 |
|---|---|---|
| **B +Kelly cap 单开** | ✅ 4/4 全过 | **P0 接入** ← 唯一通过 |
| **C +ATR stop 单开** | ❌ 2/4 | **拒绝接入** — Sharpe 倒退 + 错杀率高 |
| **D 双开** | ⚠️ 3/4（Sharpe Δ 未达 +0.3）| **P1 候选** — MDD 最优但 Sharpe 优势不显著 |

---

## 关键发现

### 1. Kelly cap 是真正的胜者
- Sharpe **从 1.77 → 2.10**（+19% 风险调整后收益提升）
- MDD **从 -18.56% → -6.38%**（深度回撤压缩 66%）
- 代价：总超额从 +672% → +74%（**现金 62.5% 按 rf 4.5%/年拖累绝对收益**）
- 这是经典的"防御换风险调整"权衡，**风险调整后 dominant 优于 baseline**

### 1b. BAB 在科技股 universe 上**反向 alpha** ⚠️（三审追加）

实测：BAB 单开 Sharpe 1.71 < baseline 1.77（-0.06）/ MDD 几乎无变化。

原因：
- 学术 BAB（Frazzini-Pedersen 2014）是 **long-short + 全市场 universe**
- 我们 long-only + 集中科技股 universe（12 只全是高 β NVDA/AMD/TSM 等）
- BAB 触发期（SPY < 200MA）把高 β 减半 = **变相大幅减仓**
- 没有低 β 替代标的 → 被迫现金 → 错过反弹

**结论**：BAB defense 在**当前 universe 不适用**。要让 BAB 真正生效需要：
1. universe 扩到全市场（含金融 / 公用事业 / 必需消费等低 β 行业）
2. 或：把 BAB 改造为"在 risk_off 期切换到 KO / JNJ / TLT 等防御 ETF"

→ **拒绝接入主路径**；BAB 模块降级为"工具箱"（[bab_defense.py](../stock_research/core/bab_defense.py) 留作未来 universe 扩张后启用）

### 1c. Kelly + BAB 组合 Sharpe 最高，但 BAB 增量未达门槛

Kelly + BAB (F)：Sharpe 2.19 / MDD -5.42% — 数值上最优。

但严格按门 3：
- F vs A baseline：Δ +0.42 ✅ 通过
- **F vs B Kelly 单开：Δ +0.09 ❌ BAB 边际增益不达 +0.3 阈值**

所以 BAB 不是 Kelly 之上的**独立增益**，是 noise。

### 2. ATR stop 在科技股 universe 上是噪音 ⚠️
- 这是**今天最重要的发现**
- 学界文献（O'Neil / Wilder）支持 -15% 止损，但在 **NVDA/AMD/TSM 等高 β 科技股**上：
  - 月内频繁触发 -15% 阈值（高波动属性）
  - 触发后强制锁定 -15% PnL → 失去当月反弹机会
  - **结果：Sharpe 1.77 → 1.66（-0.11）**
- 这印证了二审"错杀率"指标的工程意义 — 一个"看起来合理"的风控规则在错的 universe 上**反而是负 alpha**
- **生产 morning_brief.section_holdings_stoploss 用的 ATR-proxy 自适应止损（[7%, 25%] cap）比固定 -15% 应该更稳健**，但同样需要在你的实际 holdings 上消融才能确认

### 3. 双开 ≠ 简单叠加
- Sharpe 1.99 < Kelly 单开 2.10（双开把 ATR 的负 alpha 也吃进来）
- 只有 MDD 受益于"双重防御"（-5.28% < Kelly 单开 -6.38%）
- 适合**极度保守**用户（如退休账户、风险预算极紧）

---

## 生产接入建议

### 立即接入（按门 4）

**1. Kelly cap → 已接入 optimize_portfolio**（[a5012d6](#)、[c43cd1d](#)）
- 当前美股 [optimize_portfolio.py [5pre/6]](../stock_research/jobs/optimize_portfolio.py) 默认 `kelly_fraction=0.5`
- 本消融**正式批准这个默认值**：Sharpe Δ +0.33 / MDD 改善 12pp 满足准入门槛

### 拒绝接入

**2. 固定 -15% ATR stop → 不接入 walk_forward / 不作主路径默认**
- 当前 [morning_brief.section_holdings_stoploss](../stock_research/jobs/morning_brief.py) 已经在用**自适应 ATR-proxy [7%, 25%]**，不是固定 -15% — 这是更稳健的做法
- 固定 -15% 仅在 backtest 简化版作对照

### 待消融候选

**3. BAB 防御模式 [bab_defense.py](../stock_research/core/bab_defense.py)**
- 已实现但**未消融**，下一个候选
- 模拟方法：walk_forward 加 `enable_bab_defense=True`，根据当月 regime 切换高 Beta 减仓
- 预期：在 RISK_OFF regime（如 2018Q4 / 2020Q1）显著改善 MDD

**4. D 系列其他指标（ADX/CHOP/Chandelier/TSMOM）** — 同样需要走完整消融

---

## 复跑命令

```bash
# 4 档基础消融（6 年）
for cfg in baseline kelly atr both; do
    case $cfg in
        baseline) flags="";;
        kelly)    flags="--enable-kelly-cap";;
        atr)      flags="--enable-atr-stop";;
        both)     flags="--enable-kelly-cap --enable-atr-stop";;
    esac
    python3 -m stock_research.jobs.walk_forward_backtest \
        --start 2015-01 --end 2020-12 --top-k 5 $flags \
        --out data/ablation/wf_$cfg.json
done

# 调 atr-stop-pct 灵敏度（找 universe-specific 最优阈值）
for stop in 0.10 0.15 0.20 0.25; do
    python3 -m stock_research.jobs.walk_forward_backtest \
        --start 2015-01 --end 2020-12 --top-k 5 \
        --enable-atr-stop --atr-stop-pct $stop \
        --out data/ablation/wf_atr_${stop}.json
done
```

### 历史教训

参考 [系统模块优先级.md](系统模块优先级.md#七指标因子准入门槛-二审-p0-5-2026-05-12-增) 反例：
- kelly_cap 写 8 个月空转 → 这次必须消融才接
- Z-Score / M-Score 算了未用 → 同上
- Quality 因子算了未入合成 → 7bb3f30 已修正（含 vs 不含 quality 用 --include-quality False 对照可消融）

---

## 复跑命令

```bash
# 4 档 baseline / +Kelly / +ATR / 双开
for cfg in baseline kelly atr both; do
    case $cfg in
        baseline) flags="";;
        kelly)    flags="--enable-kelly-cap";;
        atr)      flags="--enable-atr-stop";;
        both)     flags="--enable-kelly-cap --enable-atr-stop";;
    esac
    python3 -m stock_research.jobs.walk_forward_backtest \
        --start 2015-01 --end 2020-12 --top-k 5 $flags \
        --out data/ablation/wf_$cfg.json
done
```

---

**复跑频率**：每季度一次，或加新仓位约束时立即跑。
