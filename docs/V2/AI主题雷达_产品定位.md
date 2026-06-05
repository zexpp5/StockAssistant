# AI 主题雷达产品定位

更新时间：2026-05-29

## 一、定位

`AI 主题雷达` 是 AI 助手下的行业理解层，不是新的推荐池。

它回答：
- 这只股票为什么和 AI 有关。
- 它在 AI 价值链哪一层。
- 当前 AI 主线可能轮动到哪一层。
- 系统推荐分数高，是来自 AI 关联、基本面、动量，还是估值/质量等其他因子。

它不回答：
- 今天应该买哪只股票。
- 应该买多少仓位。
- 哪些股票应该自动进入自选股。

买什么仍由 `AI 推荐`、`AI 组合方案`、`买前审查`共同决定；`AI 主题雷达`只负责解释 AI 行业结构和受益路径。

## 二、和现有模块的边界

### 和 AI 推荐的边界

`AI 推荐` 是模型建议层，候选范围来自系统科技/AI universe，输出股票排序、评分和推荐信号。

`AI 主题雷达` 是理解层，读取同一批系统科技/AI universe 和推荐评分，但按 AI 价值链聚合展示，不新增候选范围，不单独产出买入清单。

### 和自选股·AI 优选的边界

`自选股·AI 优选` 只在用户手动维护的 `watchlist` 内排序。

`AI 主题雷达` 不以 `watchlist` 为候选范围，也不把系统池股票写入 `watchlist`。如果同一只股票已经在自选股中，只能显示“已在自选”状态。

### 和产业链地图的边界

`产业链地图` 是深度研究下的公司位置解释工具，适合查看单只股票或某条产业链。

`AI 主题雷达` 是 AI 助手下的主题总览页，适合横向比较 AI 价值链各层的热度、覆盖率和系统评分。

## 三、四标签体系

每只股票用四类标签描述 AI 关系，避免把“是不是 AI 公司”做成简单二分。

| 标签 | 含义 | 初版来源 |
| --- | --- | --- |
| AI 价值链层级 | 股票处于算力、网络、存储、电力、云平台、应用、机器人、医疗 AI、稀缺资源等哪一层 | `chain_metadata.chain` + `chain_metadata.chain_tier` |
| 受益路径 | 通过什么业务受益于 AI，例如 AI GPU、HBM、数据中心电力、企业 AI 应用 | `chain_metadata.chain_role` + `layman_intro` |
| 证据置信度 | 标签来自人工确认、规则分类，还是后续 LLM 辅助 | `chain_metadata.source` |
| AI 关联强度 | 强、中、弱、无 | 初版按 `chain` 和人工 override 反推，后续可升级为独立字段 |

初版不追求一次性解决“AI 公司”的标准定义，只要求每个标签可解释、可审计、可覆盖。

### 3.1 两个辅助解释字段

为了避免用户只看到“AI 相关”而误以为可以买，页面增加两个辅助解释字段。它们只帮助决定“先研究谁”，不参与组合下单，不写入 `watchlist`，也不替代估值和买前审查。

| 字段 | 回答的问题 | 初版来源 | 展示口径 |
| --- | --- | --- | --- |
| 瓶颈强度 | 这家公司是不是未来 12 个月 AI 扩张绕不开的供给环节 | `chain`、`chain_role`、`layman_intro` 规则反推 | 高 / 中 / 低 / 未知 + 一句话理由 |
| 预期拥挤度 | 市场是否已经比较充分关注这个好消息 | 系统分、ETF 共识、链条近 7 天趋势、研究优先级 | 高 / 中 / 低 / 未知 + 一句话理由 |

解释边界：
- `瓶颈强度=高` 只代表它处在更可能卡住 AI 扩张的环节，例如 HBM、光模块、数据中心电力、液冷、核电/铀、稀土等；不代表估值便宜。
- `预期拥挤度=高` 只代表系统分、ETF 共识或链趋势已经较集中；不代表必须卖出，只提示后续必须看估值、回撤和财务反证。
- 两个字段都不是买入信号。买什么仍由 `AI 推荐`、`AI 组合方案`、`买前审查`共同决定。

## 四、初版页面形态

建议新增 AI 助手子页面：`AI 主题雷达`。

首版只读，不写库。页面结构：

```text
AI 主题雷达
  当前 AI 主线判断
  AI 价值链分组
    AI 算力：数量、平均系统分、强关联数量、代表股票
    数据中心电力：数量、平均系统分、强关联数量、代表股票
    云平台 / 应用 / 机器人 / 稀缺资源 ...
  展开行
    ticker、名称、chain_role、layman_intro、系统分、瓶颈强度、预期拥挤度、AI 关联强度、是否已在自选
  覆盖率审计
    系统高分但缺少 chain 标签的股票数量和列表
```

## 五、硬规则

- 不创建新的股票池。
- 不写 `watchlist`。
- 不写真实持仓。
- 不替代 `AI 推荐` 或 `AI 组合方案`。
- 不把“AI 关联强”直接等同于“可以买”。
- 不把手动自选股混入系统已拉取股票池。
- 覆盖率不足时必须显式提示，不能把未分类股票隐藏成不存在。
- `瓶颈强度` 和 `预期拥挤度` 必须显示为解释字段，不能被文案包装成买入/卖出/加仓/减仓信号。

## 六、首版验收口径

首版完成时至少验证：

- `AI 主题雷达` 展示来源为系统科技/AI universe，而不是 `watchlist`。
- 打开页面前后，`watchlist` 和真实持仓计数不变化。
- 每只展示股票至少能看到 `chain`、`chain_role` 或未分类原因。
- 每只展示股票能看到 `瓶颈强度` 和 `预期拥挤度`，并能理解这两个字段只是研究优先级解释，不是买入建议。
- 系统评分高但缺少产业链标签的股票会进入覆盖率审计。
- 页面文案不暗示“AI 主题雷达”是买入清单。

## 七、稀缺资源数据源落地需求

本节用于把页面中的“水冷、稀土、铀、SMR、AI 数据”从主题观点升级为可追溯、可审计、可回滚的数据证据系统。

目标不是新增推荐池，而是回答：

- 这个主题为什么值得关注。
- 哪些上市公司与主题有关。
- 每家公司与主题的关系有没有公开证据。
- 证据来自哪里、何时更新、可信度如何。
- 哪些公司只是待审计线索，不能被当成已确认标的。

### 7.1 数据原则

禁止流程：

```text
先列一批概念股 -> 再找理由解释为什么它们相关
```

正确流程：

```text
官方/公司/监管数据 -> 抽取主题证据 -> 映射到上市公司 -> 打证据分 -> 进入 AI 主题雷达解释层
```

数据源分层：

| 等级 | 数据源类型 | 能做什么 | 不能做什么 |
| --- | --- | --- | --- |
| A | 政府、监管、交易所、公司公告、公司财报 | 作为事实证据 | 不能直接推出股价结论 |
| B | ETF 官方持仓、行业协会、专业研究机构 | 作为候选发现和交叉验证 | 不能单独确认公司受益 |
| C | 新闻、博客、社交媒体、二手文章 | 只作为线索 | 不能进入确认标签 |

两源确认规则：

- 一家公司要被标为某主题 `confirmed`，必须至少有 1 个 A 类来源证明主题存在或行业需求存在。
- 必须至少有 1 个 A 类或 B 类来源证明公司与该主题有业务关系。
- 最近 180 天内必须至少有 1 条新证据；超过 180 天降级为 `stale`。
- 只有新闻或 ETF 成分时，状态只能是 `candidate` 或 `needs_review`。

### 7.2 五条主题线

#### 7.2.1 水冷 / 液冷

主题定义：
- AI 服务器功耗提升后，数据中心需要更高密度冷却方案，包括 direct-to-chip liquid cooling、cold plate、CDU、immersion cooling、liquid cooling rack、thermal management。

主要数据源：

| 数据源 | 类型 | 用途 | 链接 |
| --- | --- | --- | --- |
| DOE / LBNL 2024 U.S. Data Center Energy Usage Report | A | 判断数据中心和 AI 负载增长的宏观需求 | https://buildings.lbl.gov/publications/2024-lbnl-data-center-energy-usage-report |
| DOE 数据中心电力需求说明 | A | 解释 AI 数据中心电力/能耗增长背景 | https://www.energy.gov/articles/doe-releases-new-report-evaluating-increase-electricity-demand-data-centers |
| SEC EDGAR 公司 filings | A | 抽取公司是否披露 liquid cooling、data center cooling、AI rack 等业务证据 | https://www.sec.gov/edgar/sec-api-documentation |
| 公司 IR / 10-K / 10-Q / earnings call | A | 验证订单、收入、backlog、客户方向 | 公司官网或 SEC filing |
| Uptime Institute cooling survey | B | 行业采用率、技术趋势参考 | https://uptimeinstitute.com/ |

关键词：

```text
liquid cooling
direct-to-chip
cold plate
CDU
coolant distribution unit
immersion cooling
thermal management
AI rack
high density rack
data center cooling
```

红线：
- 只写了 `data center` 但没有冷却、热管理或高密度机柜证据，不能标为水冷。
- 只卖普通 HVAC，不能自动归为 AI 水冷。

#### 7.2.2 稀土

主题定义：
- 主要关注 AI/电气化/机器人链条可能使用的磁材相关稀土，特别是 NdPr、Dy、Tb、permanent magnets、rare earth separation、rare earth oxide。

主要数据源：

| 数据源 | 类型 | 用途 | 链接 |
| --- | --- | --- | --- |
| USGS Mineral Commodity Summaries 2026 | A | 稀土产量、储量、进口依赖、供应结构 | https://pubs.usgs.gov/publication/mcs2026 |
| USGS 2026 MCS data release | A | 结构化原始数据 | https://data.usgs.gov/datacatalog/data/USGS%3A69837e43b66b01367d7ec7c7 |
| IEA Critical Minerals Data Explorer | A/B | 稀土需求情景、关键矿物供需 | https://www.iea.org/data-and-statistics/data-tools/critical-minerals-data-explorer |
| 公司年报 / 技术报告 / 矿山公告 | A | 验证公司是否生产、分离、加工或销售稀土 | 公司官网 / 交易所公告 / SEC |

关键词：

```text
rare earth
rare earth oxide
REO
NdPr
neodymium
praseodymium
dysprosium
terbium
permanent magnet
magnet materials
separation
refining
offtake agreement
```

公司分类：

| 分类 | 说明 |
| --- | --- |
| mining | 稀土矿开采 |
| processing | 分离、冶炼、氧化物生产 |
| magnet | 磁材或磁体制造 |
| downstream | 下游使用稀土磁材，但不是核心稀土公司 |

红线：
- 只因为名字里有“资源”“材料”不能归为稀土。
- 只持有早期探矿权、没有经济性披露的公司必须降权。
- 中国 A 股若只有概念描述，必须用公告或主营业务验证。

#### 7.2.3 铀

主题定义：
- 核电重启、长期合同、供需缺口、矿山复产、铀价、实物铀基金、核燃料周期带来的投资主题。

主要数据源：

| 数据源 | 类型 | 用途 | 链接 |
| --- | --- | --- | --- |
| EIA Nuclear & Uranium Data | A | 美国核电、铀采购、铀价、核燃料数据 | https://www.eia.gov/nuclear/data/ |
| EIA Uranium Marketing Annual Report | A | 美国反应堆运营商采购量和加权平均价格 | https://www.eia.gov/uranium/marketing/ |
| World Nuclear Association - Supply of Uranium | B | 全球铀供需、库存、资源解释 | https://world-nuclear.org/information-library/nuclear-fuel-cycle/uranium-resources/supply-of-uranium |
| World Nuclear Association - Uranium Markets | B | 铀市场结构、长期合同与现货市场说明 | https://world-nuclear.org/information-library/nuclear-fuel-cycle/uranium-resources/uranium-markets |
| 公司 filings / 技术报告 | A | 产量、成本、复产计划、长期合同、资源量 | 公司公告 / SEC / SEDAR |

关键词：

```text
uranium
U3O8
yellowcake
ISR
in-situ recovery
long-term contract
production guidance
restart
licensed capacity
resource estimate
```

公司分类：

| 分类 | 说明 |
| --- | --- |
| producer | 已有商业生产 |
| developer | 有资源量/项目但未稳定生产 |
| physical_trust | 实物铀持有工具 |
| fuel_cycle | 转化、浓缩、燃料服务 |
| utility | 核电运营商，通常不是铀价格弹性标的 |

红线：
- 铀价上涨不等于所有核电公司受益。
- 早期矿权公司不能和生产商同权重。
- 长期合同、产量指导、复产计划必须来自公司公告或监管文件。

#### 7.2.4 SMR / advanced nuclear

主题定义：
- 小型模块化反应堆、先进反应堆、微堆、相关核岛设备、燃料、工程服务、监管许可和示范项目。

主要数据源：

| 数据源 | 类型 | 用途 | 链接 |
| --- | --- | --- | --- |
| DOE Advanced SMRs | A | SMR 技术和政策背景 | https://www.energy.gov/ne/nuclear-reactor-technologies/small-modular-nuclear-reactors |
| DOE Advanced Reactor Demonstration Projects | A | TerraPower、X-energy 等示范项目 | https://www.energy.gov/ne/advanced-reactor-demonstration-projects |
| NRC Advanced Reactors | A | 监管许可、申请和项目状态 | https://www.nrc.gov/reactors/new-reactors/advanced |
| NRC Advanced Reactor Highlights | A | 最新监管进展 | https://www.nrc.gov/reactors/new-reactors/advanced/highlights/2026 |
| 公司 filings / 项目公告 | A | 公司是否直接参与项目、设备供应或许可申请 | 公司公告 / SEC |

关键词：

```text
SMR
small modular reactor
advanced reactor
microreactor
BWRX-300
Natrium
Xe-100
AP300
reactor vessel
nuclear island
HALEU
fuel fabrication
NRC application
construction permit
licensing
```

公司分类：

| 分类 | 说明 |
| --- | --- |
| reactor_vendor | 反应堆设计/开发商 |
| component_supplier | 反应堆压力容器、核岛设备、控制系统等 |
| fuel_supplier | HALEU、燃料制造、转化/浓缩 |
| utility_partner | 项目业主或电力购买方 |
| engineering | 工程建设和项目管理 |

红线：
- 私有公司不能直接作为可交易标的，只能映射到公开合作方或供应链公司。
- 只有备忘录或意向书，不能标为 `confirmed`。
- 监管阶段必须区分 `pre-application`、`application submitted`、`accepted_for_review`、`approved`、`under_construction`。

#### 7.2.5 AI 数据

主题定义：
- 训练数据、标注数据、内容授权、数据许可、模型评测、RLHF、人类反馈、行业专有数据、检索/知识库等可被 AI 模型商业化使用的数据资产。

主要数据源：

| 数据源 | 类型 | 用途 | 链接 |
| --- | --- | --- | --- |
| SEC EDGAR 公司 filings | A | 抽取 data licensing、AI data、content licensing、model evaluation 等披露 | https://www.sec.gov/edgar/sec-api-documentation |
| 公司 8-K / press release / investor presentation | A/B | 验证是否有 AI 数据授权合同或 AI 数据服务收入 | 公司官网 / SEC |
| Common Crawl | A/B | 开放网络训练数据背景，不映射上市公司 | https://commoncrawl.org/ |
| Common Crawl Get Started | A/B | 技术数据源访问方式 | https://commoncrawl.org/get-started |

关键词：

```text
data licensing
content licensing
AI training data
training data
model evaluation
RLHF
human feedback
data annotation
synthetic data
knowledge graph
retrieval augmented generation
RAG
large language model
```

公司分类：

| 分类 | 说明 |
| --- | --- |
| licensed_content | 拥有可授权内容或社区数据 |
| data_annotation | 数据标注、RLHF、模型评测 |
| vertical_data | 法律、金融、科研、医疗等专有数据 |
| open_corpus | 开放数据生态，仅做宏观背景 |

红线：
- Common Crawl 是开放语料背景，不是可投资公司，不能映射 ticker。
- “公司有很多用户数据”不能自动等于 AI 数据受益。
- 必须有授权、收入、合同、产品或服务证据。

## 八、证据数据模型

建议新增以下 V2 表。若首版先做轻量版，也可以生成 JSON 快照，但字段语义必须一致。

### ai_theme_evidence_sources

```sql
CREATE TABLE IF NOT EXISTS ai_theme_evidence_sources (
    source_id        VARCHAR PRIMARY KEY,
    source_name      VARCHAR NOT NULL,
    source_tier      VARCHAR NOT NULL, -- A | B | C
    source_type      VARCHAR NOT NULL, -- government | regulator | company_filing | company_ir | industry | etf | news
    source_url       VARCHAR NOT NULL,
    update_cadence   VARCHAR,
    license_note     VARCHAR,
    last_checked_at  TIMESTAMP,
    active           BOOLEAN DEFAULT TRUE
);
```

### ai_theme_company_evidence

```sql
CREATE TABLE IF NOT EXISTS ai_theme_company_evidence (
    evidence_id      VARCHAR PRIMARY KEY,
    theme            VARCHAR NOT NULL, -- liquid_cooling | rare_earths | uranium | smr | ai_data
    market           VARCHAR,
    symbol           VARCHAR,
    company_name     VARCHAR,
    evidence_status  VARCHAR NOT NULL, -- candidate | confirmed | stale | rejected | needs_review
    source_id        VARCHAR NOT NULL,
    source_tier      VARCHAR NOT NULL,
    source_url       VARCHAR NOT NULL,
    source_title     VARCHAR,
    source_date      DATE,
    captured_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    evidence_text    VARCHAR, -- 最短可复核摘录/摘要，不放长文
    evidence_kind    VARCHAR, -- keyword_hit | filing_metric | contract | project_status | macro_metric | holdings_seed
    metric_json      VARCHAR,
    confidence_score DOUBLE,
    expires_at       DATE,
    reviewer_note    VARCHAR
);
```

### ai_theme_company_tags

```sql
CREATE TABLE IF NOT EXISTS ai_theme_company_tags (
    theme              VARCHAR NOT NULL,
    market             VARCHAR NOT NULL,
    symbol             VARCHAR NOT NULL,
    company_name       VARCHAR,
    theme_role         VARCHAR,
    ai_strength        VARCHAR, -- 强 | 中 | 弱 | 无
    evidence_status    VARCHAR, -- confirmed | candidate | stale | needs_review
    evidence_score     DOUBLE,
    source_count_a     INTEGER,
    source_count_b     INTEGER,
    source_count_c     INTEGER,
    latest_source_date DATE,
    rationale          VARCHAR,
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (theme, market, symbol)
);
```

### ai_theme_topic_metrics

```sql
CREATE TABLE IF NOT EXISTS ai_theme_topic_metrics (
    theme             VARCHAR NOT NULL,
    metric_date       DATE NOT NULL,
    metric_name       VARCHAR NOT NULL,
    metric_value      DOUBLE,
    metric_unit       VARCHAR,
    source_id         VARCHAR NOT NULL,
    source_url        VARCHAR,
    captured_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (theme, metric_date, metric_name, source_id)
);
```

## 九、证据评分和状态

公司主题得分满分 100，只用于解释和排序，不是买入分。

```text
evidence_score =
  source_quality_score      0-25
  + theme_directness_score  0-25
  + business_materiality    0-20
  + recency_score           0-15
  + cross_validation_score  0-15
  - risk_penalty            0-30
```

source_quality_score：

| 条件 | 分数 |
| --- | --- |
| 公司 filing / 政府 / 监管 | 25 |
| 公司 IR / 交易所公告 | 20 |
| ETF 官方持仓 / 行业协会 | 10 |
| 新闻 / 二手文章 | 3 |

theme_directness_score：

| 条件 | 分数 |
| --- | --- |
| 公司主营产品直接属于该主题 | 25 |
| 关键产品或订单明显暴露于该主题 | 18 |
| 下游客户或项目间接受益 | 10 |
| 只有概念或关键词弱相关 | 3 |

evidence_status：

| 状态 | 条件 |
| --- | --- |
| confirmed | 满足两源确认，且最近 180 天有 A/B 类证据 |
| candidate | 有线索但未满足两源确认 |
| stale | 曾 confirmed，但最新有效证据超过 180 天 |
| needs_review | 数据冲突、ticker 映射不确定、主题路径不清楚 |
| rejected | 人工确认不相关，或证据被后续材料否定 |

## 十、任务设计

### 10.1 数据抓取任务

建议新增：

```text
stock_research/jobs/ai_theme_evidence_refresh.py
```

运行方式：

```bash
/opt/homebrew/bin/python3 -m stock_research.jobs.ai_theme_evidence_refresh \
  --themes liquid_cooling,rare_earths,uranium,smr,ai_data \
  --mode incremental \
  --max-age-days 30
```

职责：
- 拉取/刷新数据源元信息。
- 扫描 SEC filings、公司 IR、官方报告。
- 生成 `ai_theme_company_evidence`。
- 聚合 `ai_theme_company_tags`。
- 输出 `data/latest/ai_theme_evidence_summary.json`。

### 10.2 覆盖率审计任务

建议新增：

```text
stock_research/jobs/ai_theme_coverage_audit.py
```

审计项：
- 系统推荐 Top N 中缺少主题标签的股票。
- 主题标签只有 C 类来源的股票。
- `confirmed` 但超过 180 天无新证据的股票。
- 同一 symbol 多个 market 映射冲突。
- AI 主题雷达中出现的主题没有任何 A 类数据源。

输出：

```text
data/latest/ai_theme_coverage_audit.json
```

### 10.3 Dashboard 集成

`AI 主题雷达` 读取：

```text
chain_metadata
recommendation_picks
ai_theme_company_tags
ai_theme_company_evidence
ai_theme_topic_metrics
```

页面展示：
- 每个主题的宏观证据卡。
- 每个主题的 `confirmed / candidate / stale / needs_review` 数量。
- 每只股票的证据状态、证据分、最近来源日期。
- 覆盖率审计。

禁止：
- 在页面上显示“建议买入”“应该配置”“必买”。
- 因为主题 `confirmed` 自动写入 `watchlist`。
- 因为主题 `confirmed` 自动进入 `AI 组合方案`。

## 十一、实施步骤

### Phase 0：数据源注册

完成：
- 建立 `ai_theme_evidence_sources` 种子清单。
- Dashboard 不改推荐逻辑。

验收：
- 数据源清单全部能打开。
- 每个源有 `source_tier`、`source_type`、`update_cadence`、`license_note`。

### Phase 1：证据表和离线快照

完成：
- 新增数据表。
- 写 SEC filings 关键词扫描。
- 写官方宏观数据源人工/半自动录入接口。
- 生成 `ai_theme_evidence_summary.json`。

验收：
- 每条 evidence 都有 `source_url`、`source_tier`、`captured_at`。
- 没有 `source_url` 的证据不能入库。
- C 类来源不能让公司进入 `confirmed`。

### Phase 2：接入 AI 主题雷达

完成：
- AI 主题雷达展示主题证据卡和公司证据状态。
- 覆盖率审计显示缺证据股票。
- 主题标签不改变推荐池。

验收：

```sql
SELECT COUNT(*) FROM manual_watchlist;
SELECT COUNT(*) FROM holdings;
```

刷新 AI 主题雷达前后，两者必须不变。

### Phase 3：每周审计和人工复核

完成：
- 每周生成 coverage audit。
- 高分但缺证据股票进入待复核。
- 人工可以把标签改为 `rejected / confirmed`，但必须写 `reviewer_note`。

验收：
- `needs_review` 股票不会被隐藏。
- `stale` 股票在页面显式标记。
- `rejected` 股票 30 天内不被自动重新确认，除非出现新的 A 类证据。

## 十二、反误导规则

系统必须遵守：

- 没有来源 URL，不显示事实判断。
- 没有公司级证据，只能显示主题级宏观趋势。
- 只有 ETF 持仓，不能显示 `confirmed`。
- 只有新闻标题，不能显示 `confirmed`。
- 只有 LLM 推断，不能显示 `confirmed`。
- 证据过期，必须显示 `stale`。
- 数据源抓取失败，必须显示 `source_degraded`。
- 对私有公司，只显示“主题项目/合作方”，不生成股票标签。

页面默认文案：

```text
本页只解释 AI 主题链条和公开证据，不构成买入建议。
主题强相关不等于可以买；实际交易仍需经过 AI 推荐、AI 配仓和买前审查。
```

## 十三、首批种子源

| source_id | source_name | tier | type | url |
| --- | --- | --- | --- | --- |
| sec_edgar_api | SEC EDGAR APIs | A | regulator | https://www.sec.gov/edgar/sec-api-documentation |
| doe_lbnl_data_center_2024 | LBNL 2024 U.S. Data Center Energy Usage Report | A | government | https://buildings.lbl.gov/publications/2024-lbnl-data-center-energy-usage-report |
| doe_data_center_demand_2024 | DOE Data Center Electricity Demand Release | A | government | https://www.energy.gov/articles/doe-releases-new-report-evaluating-increase-electricity-demand-data-centers |
| usgs_mcs_2026 | USGS Mineral Commodity Summaries 2026 | A | government | https://pubs.usgs.gov/publication/mcs2026 |
| usgs_mcs_2026_data | USGS MCS 2026 Data Release | A | government | https://data.usgs.gov/datacatalog/data/USGS%3A69837e43b66b01367d7ec7c7 |
| iea_critical_minerals_explorer | IEA Critical Minerals Data Explorer | A/B | government_agency | https://www.iea.org/data-and-statistics/data-tools/critical-minerals-data-explorer |
| eia_nuclear_data | EIA Nuclear & Uranium Data | A | government | https://www.eia.gov/nuclear/data/ |
| eia_uranium_marketing | EIA Uranium Marketing Annual Report | A | government | https://www.eia.gov/uranium/marketing/ |
| wna_uranium_supply | World Nuclear Association Supply of Uranium | B | industry | https://world-nuclear.org/information-library/nuclear-fuel-cycle/uranium-resources/supply-of-uranium |
| wna_uranium_markets | World Nuclear Association Uranium Markets | B | industry | https://world-nuclear.org/information-library/nuclear-fuel-cycle/uranium-resources/uranium-markets |
| doe_advanced_smr | DOE Advanced SMRs | A | government | https://www.energy.gov/ne/nuclear-reactor-technologies/small-modular-nuclear-reactors |
| doe_ardp | DOE Advanced Reactor Demonstration Projects | A | government | https://www.energy.gov/ne/advanced-reactor-demonstration-projects |
| nrc_advanced_reactors | NRC Advanced Reactors | A | regulator | https://www.nrc.gov/reactors/new-reactors/advanced |
| nrc_advanced_reactor_highlights | NRC Advanced Reactor Highlights | A | regulator | https://www.nrc.gov/reactors/new-reactors/advanced/highlights/2026 |
| common_crawl | Common Crawl | A/B | open_dataset | https://commoncrawl.org/ |
| common_crawl_get_started | Common Crawl Get Started | A/B | open_dataset | https://commoncrawl.org/get-started |

## 十四、增强版验收标准

产品验收：
- 用户能看到“为什么这个主题被关注”。
- 用户能看到“为什么这家公司与主题有关”。
- 用户能区分 `confirmed`、`candidate`、`stale`、`needs_review`。
- 用户不会把 AI 主题雷达误解为买入清单。

数据验收：
- 每条 `confirmed` 标签至少有 2 个来源。
- 每条 `confirmed` 标签至少有 1 个 A 类来源。
- 每条 `confirmed` 标签最近 180 天内有新证据。
- 每条证据都能追溯到 URL。
- 所有数据源失败时，页面显示“数据不足”，而不是沿用旧结论假装正常。

系统验收：

```bash
/opt/homebrew/bin/python3 -m py_compile stock_research/jobs/ai_theme_evidence_refresh.py
/opt/homebrew/bin/python3 -m py_compile stock_research/jobs/ai_theme_coverage_audit.py
/opt/homebrew/bin/python3 scripts/tools/production_acceptance_check.py --allow-a-share-disabled
/opt/homebrew/bin/python3 scripts/pipeline/build_stock_dashboard_html.py
```

如果涉及推荐、组合、dashboard 状态，还必须按 `docs/V2/产品测试验收文档.md` 跑相关检查。
