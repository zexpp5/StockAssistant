# 2026-05-26 · catalyst 系统 roadmap（18 项）

**起点**：今天上线了「📰 一句话解释 + 🆕 / 连 N 日」标记（commit `961864c` + 并行 `73944e2`）。
这份文档把后续 18 项 gap 全部列出来，按主题分组，**不带先后**——按 todo 逐项推进。

**今天已上线**：
- 港股事件日历 collector（yfinance 财报 + EPS 超预期）→ [event_calendar_hk_daily.py](../stock_research/jobs/event_calendar_hk_daily.py)
- morning_brief `_build_catalyst` helper → 三市场 ticker 行下方加 📰
- dashboard 后端 `_build_appearance_index` + `_build_catalyst_index` → 前端 🆕 / 连 N 日 badge + 推荐依据 📰 prepend
- trade_delta 输出后归档 snapshot
- daily_refresh 加 19a/25 港股事件 step

---

## A. 港股事件日历 — HKEX 披露易 爬虫范畴

需要新建一个 collector：`stock_research/jobs/event_calendar_hk_hkex.py`，POST 表单到 `https://www1.hkexnews.hk/search/titlesearch.xhtml`，按公告类型分流。

| # | 事件类别 | 谁会影响 | 复杂度 | 实现路径 |
|---|---|---|---|---|
| 1 | **业绩预告 / 盈警** | 所有港股 | 加 1 模块 + 配解析规则 | 披露易"业绩预告"分类抓 → 写 `event_type="earnings_preview"` |
| 2 | **停牌 / 复牌公告** | 所有港股 | 加 1 模块 | 披露易"暂停 / 复牌"明确分类，最简单 |
| 3 | **股东减持 / 增持** | 所有港股 | 加 1 模块 | 披露易"权益披露"分类。A 股已做（[event_calendar.py:183-230](../stock_research/core/event_calendar.py#L183-L230)）参考 |
| 4 | **回购公告** | 腾讯 / 阿里 / 美团 等 | 加 1 函数 | 披露易常规分类，腾讯几乎每天回购 |
| 5 | **重大订单 / 客户合同** | 中兴 / 联想 / 舜宇 / 半导体设备 | 加模块 + NLP | 披露易归"自愿公告"，要 NLP 识别金额 / 客户 |
| 6 | **并购 / 私有化 / 借壳** | 中小盘为主 | 加 1 函数 | 披露易有专门类别 |

## B. 港股政策 / 监管事件 — 走 policy_events 不走 HKEX

| # | 事件类别 | 复杂度 | 路径 |
|---|---|---|---|
| 7 | **港股政策 / 监管** | 跨模块 | 扩 [policy_scan_daily.py](../stock_research/jobs/policy_scan_daily.py)，加港股通新规 / 反垄断 / 行业监管源 |

## C. 美股事件日历 — yfinance 已能拿，只是没接

今天 collector 只写了港股，**美股完全没做**——但 yfinance 同样能拿。

| # | 事件类别 | 数据源 | 复杂度 |
|---|---|---|---|
| 8 | **美股财报日 + EPS 超预期** | yfinance | 复制 1 文件改 2 行（universe 改成 US） |
| 9 | **美股 SEC 8-K / 13G / 13D** | SEC EDGAR | 加 1 模块（已有 13F 框架，同源） |
| 10 | **美股内部人交易（Form 4）** | SEC EDGAR / openinsider.com | 加 1 模块 |

## D. 推荐异动标签 — 今天只做了 🆕 + 连 N 日

| # | 标签 | 触发条件 | 显示位置 |
|---|---|---|---|
| 11 | **📈 排名跃升** | composite_z 相比上批次 +0.3 或 rank 进 5 位 | morning_brief 📰 同行 + dashboard ticker 列 |
| 12 | **📉 跌出 Top** | 上批次在 Top10、本批次跌出 | morning_brief 单独 section + dashboard 顶部 banner |

## E. catalyst 系统本身的收尾

| # | 项目 | 说明 |
|---|---|---|
| 13 | **catalyst helper 去重** | [_build_catalyst](../stock_research/jobs/morning_brief.py) 和 [_build_catalyst_index](../scripts/pipeline/build_stock_dashboard_html.py) 两份代码做同一件事；提到 `stock_research/core/catalyst.py` 共享（违反 [feedback-single-source-no-double-engine]） |
| 14 | **美股 catalyst** | `_build_catalyst` 美股分支当前 return None；等 #8 上线后接通 |
| 15 | **catalyst 来源 audit** | 加 `source_health.json` 一项：今天港股事件日历 hit 率、漏哪些 ticker、跟昨天对比 |

## F. 验证 / 收尾（今天临时通过的盲点）

| # | 项目 | 说明 |
|---|---|---|
| 16 | **历史 sub-tab 🆕 验证** | 今天 runs=2 状态下我没亲眼刷新看；按代码应该出"首次出现 🆕"；前端 bug 排查 |
| 17 | **「连 N 日」分叉 case 验证** | 等出现"跌出又回来"的票时（5/27+ 可能），确认 tooltip "累计 N 次推荐，期间曾跌出又重新入选" 文案能正确触发 |
| 18 | **event_calendar_hk_daily 接 acceptance** | 漏抓 / 命中率 < 80% / 文件过期 24h 时报警，跟 A 股 event_calendar 待遇一致 |

---

## 决策依据

不按 P0/P1/P2 排——用户明确说"不是让你先后"。但有几点客观取舍要在做的时候记得：

- **#5 #6 命中率低**：港股推荐池都是大盘龙头，订单 / 并购公告概率小，做完容易冷藏
- **#7 是另一个模块**（policy_events），不属于 HKEX 爬虫范畴
- **#13 是重构**，按 [feedback-polish-before-refactor]，等 #14 上线再一起抽
- **#16 / #17 是验证类**，需要人在浏览器里看，不能 Claude 单方面 close

## 跟进位置

todo 跟着这份文档逐项推进。完成的项打勾，新发现的 gap 追加到末尾。

---

## 2026-05-26 收尾追加 5 项 polish（全部完成）

| 项目 | 状态 | 说明 |
|---|---|---|
| **B7b-noise** HKMA 噪音过滤 | ✅ | `_is_hkma_noise()` 黑名单过滤 Scam alert / Tender Results / Renminbi Bills 等日常运营公告，9 条 → 0 条有效（今天 HKMA 无政策信号） |
| **A5-threshold** 金额阈值 | ✅ | `_amount_to_cny()` 把订单金额估算成 CNY，`< 1 亿 CNY` 的 major_order 降为 priority 4 弱信号；阈值 const `MAJOR_ORDER_CNY_THRESHOLD = 1e8` |
| **C9-item** 8-K Item 解析 | ✅ | `_fetch_8k_items()` 拉每个 8-K HTML 用正则 `Item N.NN` 提取，写到 event `items[]` + `item_label` + `item_priority`；catalyst 用 item_priority 覆盖 form-level；80% 命中（130 个 8-K → 104 个解析出 item）；最常见 Item 9.01/2.02/7.01/5.02/5.07/1.01 |
| **A5-ner** 客户名 NER | ✅ | 白名单规则匹配 50+ 主要客户（三大运营商/华为/比亚迪/腾讯/阿里/特斯拉/苹果/微软等）；major_order 句子优先「客户 X · 金额 Y」格式 |
| **B7b-sfc** SFC News API | ✅ | 逆向找到 SFC SPA 真正 backend `POST /edistributionWeb/api/news/search` + payload `{"lang":"EN","category":"all","year":Y,"pageNo":1,"pageSize":N,"sort":{"field":"issueDate","order":"desc"}}`；返回 newsType=GN/EF 含 5250+ 历史新闻 |

## 真正还能 polish 的（下一轮，非阻塞） · 2026-05-26 收尾追加 (全部完成 + hold 1)

| 项目 | 状态 | 说明 |
|---|---|---|
| **SFC speech 噪音过滤** | ✅ | `_is_sfc_noise()` 黑名单 17 项（speech / Keynote / Remarks / Education Award / IOSCO 等）；SFC 60 天 30 条 → 21 条真信号（监管审查/执法/罚款/和解） |
| **客户名扩词** | ✅ | 50 → **203** 词条；新增运营商 / 互联网 / 新能源车企 / 央企 / 海外巨头 / 半导体设备 / 全球车厂等 7 大类；简繁英三套 |
| **8-K Item 多元组合标签** | ✅ | `_8k_best_item_label()` 合并 `[1.01, 5.02, 9.01]` → 「📜 重大协议 + 👤 高管变动」；priority ≤ 主标+1 且 < 4 的合并显示；财报附件等噪音不参与合并 |
| **A5 PDF 上下文段落** | ✅ | `_fetch_pdf_summary()` pypdf 拉首页 → 跳过免责声明 → 前 400 字摘要；major_order 时调用；如果 title 没抽到金额/客户从摘要二次抽；附 `self_name` 排除自家公司名误识别（联想公告里"聯想"不再被当客户） |
| **HKEX news SPA** | ⏸️ HOLD | 实测页面 server-rendered 但 news 列表通过第三方 hanweb widget 异步加载,逆向工程化高;且 HKEX「news release」主要是市场规则变更（≈ SFC/HKMA 已覆盖的宏观层），跟个股催化关联弱。HKEX **公司公告（A0-A6）已通过 titleSearchServlet 拿到** — 那才是核心信号源 |

## 当前完成度

催化系统 18+5+5+1 = **29 项**全部上线。剩 HKEX news SPA 一个 hold 项（非阻塞 & 边际价值低）。

下一轮真正能做的：
- 8-K item 详情中 textual 段落抽取（拿 Item 5.02 具体高管名 / 离职原因）
- HKEX 公告 PDF 数据库化（每个 PDF 摘要存 DuckDB，避免下游每天重拉）
- 客户名扩词从静态白名单 → 词频驱动自动扩词
- catalyst 信号回测验证（盈警发布后 30 日股价 vs 净买入金额相关性）
