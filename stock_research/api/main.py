"""FastAPI app：把 core / adapters / jobs 直接暴露成 HTTP API。

设计原则：
  - 路由零业务逻辑，只做 HTTP ↔ 函数适配
  - 所有数据来自 core/adapters，不重复实现
  - 同步 endpoint 适合短查询；长 job 走 BackgroundTasks

现在可跑（需 fastapi + uvicorn）：
  pip install fastapi uvicorn
  uvicorn stock_research.api.main:app --reload
"""
from __future__ import annotations
import logging
from typing import Any

try:
    from fastapi import FastAPI, BackgroundTasks, HTTPException, Query, Body
    from fastapi.middleware.cors import CORSMiddleware
except ImportError:  # 允许在没装 fastapi 时仍能 import 包做单元测试
    FastAPI = None  # type: ignore

from pathlib import Path
import sys

from .. import config
from ..core import edgar, audit
from ..adapters import feishu

# 让 stock_db.py（在 repo 根）能被 import
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
# 2026-05-11 lib 迁移：6 个 lib（stock_db 等）从根目录搬到了 scripts/lib/
_LIB_DIR = str(_REPO_ROOT / "scripts" / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

logger = logging.getLogger(__name__)


def create_app():
    if FastAPI is None:
        raise RuntimeError("fastapi not installed. Run: pip install fastapi uvicorn")

    app = FastAPI(
        title="Stock Research API",
        description="基于 SEC EDGAR / akshare / Finnhub 的多源股票研究服务",
        version="0.1.0",
    )

    # CORS：dashboard 通过 file:// 或 http://localhost 打开时调本地 API
    # 本地工具开放给所有源，部署到公网时应收窄
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ────────── 健康检查 ──────────
    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "investors_tracked": len(config.INVESTORS_13F),
            "watchlist_table": config.WATCHLIST_TABLE_ID,
        }

    # ────────── 13F 查询 ──────────
    @app.get("/api/13f/investors")
    def list_investors() -> dict[str, str]:
        """返回当前跟踪的所有机构 → CIK 映射。"""
        return config.INVESTORS_13F

    @app.get("/api/13f/changes/{cik}")
    def get_13f_changes(cik: str, name: str = Query("?", description="机构名展示用")):
        """拉某机构最新 + 上期 13F 并计算变动。SEC EDGAR 直接源。"""
        snap = edgar.get_investor_changes(name, cik)
        if not snap:
            raise HTTPException(404, f"no 13F filings for CIK {cik}")
        return snap

    @app.get("/api/13f/filings/{cik}")
    def list_filings(cik: str, limit: int = 10):
        """列某 CIK 的 13F 提交历史。"""
        filings = edgar.list_13f_filings(cik)
        return filings[:limit]

    # ────────── Watchlist (DuckDB · single source of truth · 2026-05-11 起) ──────────
    @app.get("/api/watchlist")
    def list_watchlist() -> list[dict[str, Any]]:
        """读 DuckDB watchlist 全表。"""
        import stock_db  # local import 避免启动时初始化 db connection
        rows = stock_db.fetch_all_watchlist()
        # datetime → ISO string (FastAPI JSON 序列化兼容)
        for r in rows:
            for k in ("created_at", "updated_at"):
                v = r.get(k)
                if v is not None and hasattr(v, "isoformat"):
                    r[k] = v.isoformat()
        return rows

    @app.get("/api/watchlist/{code}")
    def get_watchlist_one(code: str) -> dict[str, Any]:
        import stock_db
        row = stock_db.get_watchlist_item(code)
        if not row:
            raise HTTPException(404, f"watchlist code not found: {code}")
        for k in ("created_at", "updated_at"):
            v = row.get(k)
            if v is not None and hasattr(v, "isoformat"):
                row[k] = v.isoformat()
        return row

    @app.post("/api/watchlist")
    def create_watchlist(item: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """新增 watchlist 一条；code 必填；如已存在则 upsert 更新。"""
        import stock_db
        if not item.get("code"):
            raise HTTPException(400, "code is required")
        n = stock_db.upsert_watchlist([item])
        return {"status": "ok", "code": item["code"], "rows_affected": n}

    @app.put("/api/watchlist/{code}")
    def update_watchlist(code: str, item: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """更新 watchlist 一条；body 里的 code 会被 URL code 覆盖。"""
        import stock_db
        item["code"] = code
        n = stock_db.upsert_watchlist([item])
        return {"status": "ok", "code": code, "rows_affected": n}

    @app.delete("/api/watchlist/{code}")
    def delete_watchlist(code: str) -> dict[str, Any]:
        import stock_db
        n = stock_db.delete_watchlist_item(code)
        if n == 0:
            raise HTTPException(404, f"watchlist code not found: {code}")
        return {"status": "ok", "code": code, "rows_deleted": n}

    @app.post("/api/watchlist/auto-enrich")
    def auto_enrich_watchlist(item: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """根据 code 自动补全 watchlist 全字段（不入库，只返回结果）。

        body: {"code": "NVDA", "name": "可选"}
        前端拿到后展示在表单里给用户审核 → 再调 POST /api/watchlist 入库。
        """
        from ..core.watchlist_enrich import enrich_one
        code = (item.get("code") or "").strip()
        if not code:
            raise HTTPException(400, "code is required")
        return enrich_one(code, item.get("name"))

    # ────────── 异步 job 触发 ──────────
    @app.post("/api/jobs/refresh-13f")
    def trigger_13f_refresh(background: BackgroundTasks):
        """触发 SEC EDGAR 全量刷新（异步）。"""
        from ..jobs import refresh_13f as job  # 避免循环 import
        background.add_task(job.run_refresh_all)
        return {"status": "queued", "job": "refresh_13f"}

    @app.post("/api/jobs/enrich")
    def trigger_enrich(background: BackgroundTasks, code: str | None = None):
        """触发多源 enrichment（异步）。"""
        from ..jobs import enrich_watchlist as job
        background.add_task(job.run_all, only_code=code, do_trends=False)
        return {"status": "queued", "job": "enrich_watchlist", "code": code}

    @app.post("/api/jobs/audit")
    def trigger_audit(background: BackgroundTasks, code: str | None = None):
        """触发跨源审计（异步）。"""
        from ..jobs import daily_audit as job
        background.add_task(job.run_audit, only_code=code)
        return {"status": "queued", "job": "daily_audit", "code": code}

    return app


# 模块级 app 对象供 uvicorn 加载
app = create_app() if FastAPI is not None else None
