# Agent Instructions

Before changing this repository, read [docs/V2/产品基线.md](docs/V2/产品基线.md) and [docs/V2/产品测试验收文档.md](docs/V2/产品测试验收文档.md).

Critical product boundary:
- `今日决策台` is the default daily entry. It only aggregates AI recommendations, AI portfolio, buy-before-review, run status, and holdings deltas; it must not create a new stock pool or write `watchlist`/real holdings.
- `watchlist` is the user's manually maintained self-selected pool.
- The user-facing manual pool entry is `自选股配置`, not `初始化配置`.
- `自选股·AI 优选` can only select from `watchlist`.
- `AI 推荐`, `AI 组合方案`, and `已拉取股票池` belong to the separate AI assistant flow based on the system-fetched tech/AI stock universe.
- `AI 组合方案` is a model target portfolio and rebalance suggestion generated from AI recommendations; it is not real holdings and must not automatically write holdings.
- `策略验证` belongs under AI assistant. It evaluates historical AI recommendations and portfolio plans using point-in-time snapshots; it must not output today's buy list or silently change strategy versions.
- `已拉取股票池` must not include manual-only watchlist rows. A ticker may appear in both flows, but the source identity must stay separate.
- `深度研究` is a pre-buy explanation/review workspace (`个股研究`, `产业链地图`, `买前审查`); it must not become a recommendation list, holdings editor, or system health page.
- `运行状态` belongs under Management and shows pipeline/data health by market (US/A-share/HK); it must not be mixed with investment recommendations.
- New database rebuilds must start from an empty database. Do not migrate old DuckDB tables or old `data/latest`/cache/report/dashboard artifacts into the new production data model.
- Before declaring a fix complete, run the relevant checks from `docs/V2/产品测试验收文档.md` and `scripts/tools/production_acceptance_check.py` when the change affects production data, recommendations, or dashboard status.
