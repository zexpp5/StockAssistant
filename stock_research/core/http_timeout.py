"""全局 HTTP 超时兜底（2026-06-01 事故修复）。

事故根因：5-29 21:00 的 research run 在 realtime_defense / ipo_daily /
junior_stock_walk / SEC EDGAR / event_calendar 等网络密集 job 上挂死，整轮拖到
6-01 才结束（~83h），霸占 PID 锁导致 5-30/5-31/6-01 三天没出新批。这些 job 底层
都走 requests（yfinance / akshare / SEC EDGAR 均是），而裸 requests 调用不传
timeout 时默认是【无限等待】——网络变慢或对端 hang 住时整个进程就永久阻塞。

兜底策略：monkeypatch requests.Session.request，仅在调用方【未显式传 timeout】时
注入一个默认 (connect, read) 超时。已显式传 timeout 的调用（如 claude_client）完全
不受影响。

刻意【不】用 socket.setdefaulttimeout：baostock 用持久 TCP 长连接，全局 socket 超时
会误伤它的长查询（呼应 baostock 单连接限制的已知坑）。requests 这一层已覆盖绝大多数
网络源，针对性更强、副作用更小。

可用环境变量覆盖：
  STOCK_HTTP_CONNECT_TIMEOUT（默认 15s）
  STOCK_HTTP_READ_TIMEOUT   （默认 120s）
"""
import os

_PATCHED = False


def install_default_timeout() -> None:
    """幂等：给 requests.Session.request 装默认超时兜底。多次调用只生效一次。"""
    global _PATCHED
    if _PATCHED:
        return

    try:
        import requests
    except ImportError:
        # 没装 requests 的环境（理论上不会）直接跳过，不阻断 import
        return

    try:
        connect_t = float(os.environ.get("STOCK_HTTP_CONNECT_TIMEOUT", "15"))
        read_t = float(os.environ.get("STOCK_HTTP_READ_TIMEOUT", "120"))
    except ValueError:
        connect_t, read_t = 15.0, 120.0

    default_timeout = (connect_t, read_t)

    _orig_request = requests.Session.request

    def _request_with_timeout(self, method, url, **kwargs):
        # 只在调用方没传 timeout（或显式传了 None=无限）时才注入兜底
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = default_timeout
        return _orig_request(self, method, url, **kwargs)

    requests.Session.request = _request_with_timeout
    _PATCHED = True
