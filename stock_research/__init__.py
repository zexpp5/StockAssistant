"""股票研究系统：可拆解、Web 部署友好的架构。

分层：
  core/      纯数据获取（无 I/O 副作用，返回 JSON 可序列化 dict/list）
  adapters/  I/O 适配器（飞书读写、DuckDB、文件快照）
  jobs/      编排逻辑（CLI 入口 + cron 任务）

未来可直接被 FastAPI 路由 import core 函数封装成 API。
"""

__version__ = "0.1.0"
