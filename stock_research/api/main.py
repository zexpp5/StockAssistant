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
        from .. import config as _c
        import stock_db  # 与其他 endpoint 一致：local import 避免启动时初始化 db
        wl_n = len(stock_db.fetch_all_watchlist())
        return {
            "status": "ok",
            "investors_tracked": len(_c.INVESTORS_13F),
            "watchlist_rows": wl_n,
            "data_source": "DuckDB (飞书 Bitable 已 100% 退役)",
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

    # ────────── DB 全库浏览（深度研究 → DB 全库 tab） ──────────
    @app.get("/api/db/all-stocks")
    def db_all_stocks() -> dict[str, Any]:
        """按市场分组返回 DB 内全部股票 + 最新行情 + 最新 picks 评级。

        返回结构：
          {
            "as_of": {"prices_date": "2026-05-12", "picks_date": "2026-05-12"},
            "counts": {"美股": 82, "A股": 12, "港股": 6, "其他": 8, "total": 108},
            "groups": {"美股": [row, ...], "A股": [...], "港股": [...], "其他": [...]},
          }
        每行包含 watchlist 全 25 列 + price_* 字段 + pick_* 字段。
        """
        import stock_db
        conn = stock_db.get_db()
        try:
            wl_rows = stock_db.fetch_all_watchlist(conn=conn)

            latest_prices_q = conn.execute(
                """
                SELECT * FROM prices
                WHERE (code, date) IN (SELECT code, MAX(date) FROM prices GROUP BY code)
                """
            ).fetchall()
            price_cols = [d[0] for d in conn.description]
            prices_by_code = {r[price_cols.index("code")]: dict(zip(price_cols, r)) for r in latest_prices_q}

            latest_picks_q = conn.execute(
                """
                SELECT * FROM picks
                WHERE (code, pick_date) IN (SELECT code, MAX(pick_date) FROM picks GROUP BY code)
                """
            ).fetchall()
            pick_cols = [d[0] for d in conn.description]
            picks_by_code = {r[pick_cols.index("code")]: dict(zip(pick_cols, r)) for r in latest_picks_q}

            prices_date = conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
            picks_date = conn.execute("SELECT MAX(pick_date) FROM picks").fetchone()[0]
        finally:
            conn.close()

        def _classify(market: str | None) -> str:
            m = market or ""
            if "美股" in m:
                return "美股"
            if "A股" in m:
                return "A股"
            if "港股" in m:
                return "港股"
            return "其他"

        def _jsonable(v):
            if v is None:
                return None
            if hasattr(v, "isoformat"):
                # 去掉 ISO 8601 的 T 分隔符 + 截到秒（2026-05-12 08:32:00）
                if hasattr(v, "hour"):
                    return v.isoformat(sep=" ", timespec="seconds")
                return v.isoformat()
            return v

        groups: dict[str, list[dict[str, Any]]] = {"美股": [], "A股": [], "港股": [], "其他": []}
        for w in wl_rows:
            code = w.get("code")
            merged: dict[str, Any] = {}
            for k, v in w.items():
                merged[k] = _jsonable(v)
            p = prices_by_code.get(code) or {}
            for k, v in p.items():
                if k in ("code", "name"):
                    continue
                merged[f"price_{k}"] = _jsonable(v)
            pk = picks_by_code.get(code) or {}
            for k, v in pk.items():
                if k in ("code", "name", "market"):
                    continue
                merged[f"pick_{k}"] = _jsonable(v)
            groups[_classify(merged.get("market"))].append(merged)

        return {
            "as_of": {
                "prices_date": _jsonable(prices_date),
                "picks_date": _jsonable(picks_date),
            },
            "counts": {k: len(v) for k, v in groups.items()} | {"total": sum(len(v) for v in groups.values())},
            "groups": groups,
        }

    @app.get("/api/db/stock-history/{code}")
    def db_stock_history(code: str) -> dict[str, Any]:
        """单只股票全历史快照 — 4 张表全表过滤后按时间倒序。

        返回：
          {
            "code": "NVDA",
            "watchlist": {...},          # 当前 watchlist 一行（含 25 列元数据）
            "prices": [...],             # 多日行情时间序列（含 fetched_at）
            "picks": [...],              # 历次 picks 入选 + 评分
            "reviews": [...],            # picks 跟踪记录（含 days_held / pct）
            "discovery": [...],          # discovery_history 历次推荐
          }
        """
        import stock_db
        conn = stock_db.get_db()
        try:
            wl_row = stock_db.get_watchlist_item(code, conn=conn)

            def _rows(q: str) -> list[dict[str, Any]]:
                cur = conn.execute(q, [code])
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]

            prices = _rows("SELECT * FROM prices WHERE code = ? ORDER BY date DESC, fetched_at DESC")
            picks = _rows("SELECT * FROM picks WHERE code = ? ORDER BY pick_date DESC")
            reviews = _rows("SELECT * FROM reviews WHERE code = ? ORDER BY review_date DESC")
            discovery = _rows("SELECT * FROM discovery_history WHERE ticker = ? ORDER BY generated_date DESC")
            earnings_history = _rows("SELECT * FROM earnings_history WHERE code = ? ORDER BY fiscal_period DESC")
        finally:
            conn.close()

        def _jsonify(v):
            if v is None:
                return None
            if hasattr(v, "isoformat"):
                return v.isoformat(sep=" ", timespec="seconds") if hasattr(v, "hour") else v.isoformat()
            return v

        def _walk(obj):
            if isinstance(obj, list):
                return [_walk(x) for x in obj]
            if isinstance(obj, dict):
                return {k: _walk(v) for k, v in obj.items()}
            return _jsonify(obj)

        if not wl_row and not prices and not picks and not reviews and not discovery and not earnings_history:
            raise HTTPException(404, f"code not found in any table: {code}")

        return {
            "code": code,
            "watchlist": _walk(wl_row) if wl_row else None,
            "prices": _walk(prices),
            "picks": _walk(picks),
            "reviews": _walk(reviews),
            "discovery": _walk(discovery),
            "earnings_history": _walk(earnings_history),
        }

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

    # ────────── 持仓 holdings（2026-05-12 从 dashboard localStorage 迁过来） ──────────
    @app.get("/api/holdings")
    def list_holdings() -> list[dict[str, Any]]:
        """读全部持仓，按 entry_date 倒序。"""
        import stock_db
        rows = stock_db.fetch_all_holdings()
        for r in rows:
            if r.get("entry_date"):
                r["entry_date"] = r["entry_date"].isoformat()
            for k in ("created_at", "updated_at"):
                if r.get(k):
                    r[k] = r[k].isoformat()
        return rows

    @app.post("/api/holdings")
    def create_holding(item: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """新增持仓一条。body: {code, entry_price, shares, date, source?, notes?}"""
        import stock_db
        if not item.get("code"):
            raise HTTPException(400, "code is required")
        new_id = stock_db.insert_holding(item)
        return {"status": "ok", "id": new_id}

    @app.put("/api/holdings/{holding_id}")
    def update_holding_one(holding_id: int, item: dict[str, Any] = Body(...)) -> dict[str, Any]:
        import stock_db
        n = stock_db.update_holding(holding_id, item)
        if n == 0:
            raise HTTPException(404, f"holding id not found: {holding_id}")
        return {"status": "ok", "id": holding_id, "rows_affected": n}

    @app.delete("/api/holdings/{holding_id}")
    def delete_holding_one(holding_id: int) -> dict[str, Any]:
        import stock_db
        n = stock_db.delete_holding(holding_id)
        if n == 0:
            raise HTTPException(404, f"holding id not found: {holding_id}")
        return {"status": "ok", "id": holding_id, "rows_deleted": n}

    @app.post("/api/holdings/bulk-replace")
    def bulk_replace_holdings_endpoint(items: list[dict[str, Any]] = Body(...)) -> dict[str, Any]:
        """整批替换持仓（清空 + 重插）。用于从 localStorage 一次性迁移。"""
        import stock_db
        n = stock_db.bulk_replace_holdings(items)
        return {"status": "ok", "rows_inserted": n}

    # ────────── 投资方案配置（DuckDB user_config 表 · 2026-05-11 PM 起） ──────────
    @app.get("/api/config")
    def get_user_config() -> dict[str, Any]:
        """读全部配置；缺失 key 自动用 USER_CONFIG_DEFAULTS 补齐。"""
        import stock_db
        return stock_db.get_all_config()

    @app.put("/api/config")
    def update_user_config(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """批量 upsert 配置。

        Body: {"total_capital": 600000, "stoploss_line": 350000}
        校验：total_capital 与 stoploss_line 必须为正数；后者 < 前者。
        """
        import stock_db
        # 抽出已知数值类 key（其它 key 直接透传）
        tc = payload.get("total_capital")
        sl = payload.get("stoploss_line")
        if tc is not None:
            try:
                tc = int(tc)
            except (TypeError, ValueError):
                raise HTTPException(400, "total_capital must be a number")
            if tc <= 0:
                raise HTTPException(400, "total_capital must be positive")
        if sl is not None:
            try:
                sl = int(sl)
            except (TypeError, ValueError):
                raise HTTPException(400, "stoploss_line must be a number")
            if sl <= 0:
                raise HTTPException(400, "stoploss_line must be positive")
        if tc is not None and sl is not None and sl >= tc:
            raise HTTPException(400, "stoploss_line must be less than total_capital")
        # 写入
        for k, v in payload.items():
            if k == "total_capital" and tc is not None:
                stock_db.set_config(k, tc)
            elif k == "stoploss_line" and sl is not None:
                stock_db.set_config(k, sl)
            else:
                stock_db.set_config(k, v)
        return {"status": "ok", "config": stock_db.get_all_config()}

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
