"""股票研究系统：可拆解、Web 部署友好的架构。

分层：
  core/      纯数据获取（无 I/O 副作用，返回 JSON 可序列化 dict/list）
  adapters/  I/O 适配器（飞书读写、DuckDB、文件快照）
  jobs/      编排逻辑（CLI 入口 + cron 任务）

未来可直接被 FastAPI 路由 import core 函数封装成 API。
"""

__version__ = "0.1.0"

# 全局 HTTP 超时兜底：所有 jobs 经此入口 import，给裸 requests 调用（yfinance/akshare/
# SEC EDGAR 等底层）注入默认 timeout，防止网络 hang 导致整轮 pipeline 卡死数十小时
# （2026-06-01 事故）。仅在调用方未显式传 timeout 时生效，幂等。
try:
    from .core.http_timeout import install_default_timeout as _install_http_timeout
    _install_http_timeout()
except Exception:
    # 兜底安装失败绝不能阻断系统 import
    pass
