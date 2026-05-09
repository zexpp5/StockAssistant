"""编排层：组合 core + adapters，提供 CLI 入口。

每个 job 模块定义一个 main()，便于：
  - bash/cron 调用 `python3 -m stock_research.jobs.refresh_13f`
  - 未来 FastAPI 路由 import 函数直接调用
"""
