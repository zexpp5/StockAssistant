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
        """新增 watchlist 一条；code 必填；如已存在则 upsert 更新。

        入库成功后**异步触发** daily_picks_v5 评级（不阻塞响应）。
        """
        import stock_db
        if not item.get("code"):
            raise HTTPException(400, "code is required")
        n = stock_db.upsert_watchlist([item])
        rerun_info: dict[str, Any] = {}
        try:
            rerun_info = _spawn_picks_rerun(trigger=f"watchlist:add:{item['code']}")
        except Exception as e:  # 不让评级失败拖垮添加流程
            rerun_info = {"status": "error", "error": str(e)}
        return {
            "status": "ok",
            "code": item["code"],
            "rows_affected": n,
            "rerun": rerun_info,
        }

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
            wl_codes = {r["code"] for r in wl_rows}

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

            # earnings_history.fetched_at 最新值 — 每只股票"财报最近一次实际从 yfinance 抓取的时间"
            # 跟 watchlist.updated_at（分析时间）不同：updated_at 还可能被 enrich/用户编辑等触发
            earnings_fetched = {
                code: dt for code, dt in conn.execute(
                    "SELECT code, MAX(fetched_at) FROM earnings_history GROUP BY code"
                ).fetchall()
            }

            # 找 picks 表里不在 watchlist 的标的（如 hk_picks 的 hk_universe.py 白名单股、a_share_picks 额外评的）
            # signal='buy' 过滤：avoid/watch 档不算"额外候选"，避免把 daily_picks_v5 的负向 picks 当推荐展示。
            picks_only_q = conn.execute(
                """
                SELECT DISTINCT code, FIRST(name) AS name, FIRST(market) AS market FROM picks
                WHERE code NOT IN (SELECT code FROM watchlist)
                  AND COALESCE(signal, 'buy') = 'buy'
                GROUP BY code
                """
            ).fetchall()
            picks_only = [{"code": r[0], "name": r[1], "market": r[2]} for r in picks_only_q]

            # 找 discovery_history 表里不在 watchlist 的标的（美股 AI ETF 扫出来的候选）
            disc_only_q = conn.execute(
                """
                SELECT DISTINCT ticker AS code, FIRST(name) AS name, FIRST(sector) AS sector,
                       FIRST(market) AS market FROM discovery_history
                WHERE ticker NOT IN (SELECT code FROM watchlist)
                  AND ticker NOT IN (SELECT code FROM picks)
                GROUP BY ticker
                """
            ).fetchall()
            disc_only = [{"code": r[0], "name": r[1], "industry": r[2], "market": r[3]} for r in disc_only_q]

            prices_date = conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
            picks_date = conn.execute("SELECT MAX(pick_date) FROM picks").fetchone()[0]
        finally:
            conn.close()

        def _infer_market_from_code(code: str) -> str | None:
            """从 code 后缀/前缀推断 market（用于 picks/discovery 外标的）"""
            if not code:
                return None
            if code.endswith(".HK"): return "港股"
            if code.endswith(".SS"): return "A股·上交所"
            if code.endswith(".SZ"): return "A股·深交所"
            if code.endswith(".BJ"): return "A股·北交所"
            if code.endswith(".T"):  return "日股"
            if code.endswith(".KS"): return "韩股"
            if code.endswith(".TW"): return "台股"
            if code.endswith(".AX"): return "澳股·ASX"
            if code.endswith(".IL"): return "英股"
            if code.isdigit() and len(code) == 6:
                if code.startswith("6"): return "A股·上交所"
                if code.startswith(("0", "3")): return "A股·深交所"
                if code.startswith(("4", "8")): return "A股·北交所"
            if code.isalpha() and 1 <= len(code) <= 5: return "美股"
            return None

        def _classify(market: str | None) -> str:
            m = (market or "")
            m_low = m.lower()
            if "美股" in m or "united states" in m_low or "nasdaq" in m_low or "nyse" in m_low:
                return "美股"
            if "A股" in m or "上交" in m or "深交" in m or "北交" in m or "china" in m_low and "hong" not in m_low:
                return "A股"
            if "港股" in m or "hong kong" in m_low or m.endswith(".HK") or m_low == "hk":
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

        # 把 picks_only / disc_only 也合并进 wl_rows（伪 watchlist 行，标记 _source_origin）
        for w in wl_rows:
            w["_source_origin"] = "watchlist"
        for r in picks_only:
            r["_source_origin"] = "picks_only"
            r["market"] = r.get("market") or _infer_market_from_code(r["code"])
            wl_rows.append(r)
        for r in disc_only:
            r["_source_origin"] = "discovery_only"
            r["market"] = r.get("market") or _infer_market_from_code(r["code"])
            wl_rows.append(r)

        groups: dict[str, list[dict[str, Any]]] = {"美股": [], "A股": [], "港股": [], "其他": []}
        for w in wl_rows:
            code = w.get("code")
            # 每行带上 earnings 真实抓取时间（earnings_history.fetched_at 的 max）
            w["earnings_fetched_at"] = earnings_fetched.get(code)
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

    @app.get("/api/db/tables-overview")
    def db_tables_overview() -> dict[str, Any]:
        """DB 各表行数 + 一句话说明，给前端"数据总览"用。"""
        import stock_db
        conn = stock_db.get_db()
        try:
            descs = {
                "watchlist":          "自选股清单（每只 1 行元数据，25 列）",
                "prices":             "行情/估值快照（每只每天 1 行，含 PE/PEG/涨幅/市值）",
                "picks":              "AI 评级历史（每只每次评级 1 行）",
                "reviews":            "picks 跟踪记录（每只每天 1 行，含累计%/持仓天）",
                "earnings_history":   "季报历史归档（每只每季 1 行，含 YoY）",
                "discovery_history":  "AI 推荐池快照（每只每次推荐 1 行）",
                "discovery_tracking": "推荐准确度跟踪（1d/5d/20d/60d alpha）",
                "holdings":           "实际持仓（每只 1 行，含入场价/份数）",
                "snapshots":          "pipeline JSON 归档（13F / audit / optimize）",
                "user_config":        "用户配置（total_capital / stoploss_line）",
            }
            tbls = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name"
            ).fetchall()
            out = []
            for (t,) in tbls:
                n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                out.append({"name": t, "rows": n, "desc": descs.get(t, "")})
            return {"tables": out, "total_rows": sum(t["rows"] for t in out)}
        finally:
            conn.close()

    @app.get("/api/db/table/{name}")
    def db_table_dump(name: str, limit: int = 2000) -> dict[str, Any]:
        """直接返回某张表的全部行（按主键/日期倒序）。给「DB 数据总览」点击卡片展开用。"""
        ALLOWED = {
            "watchlist":          "ORDER BY updated_at DESC",
            "prices":             "ORDER BY date DESC, fetched_at DESC",
            "picks":              "ORDER BY pick_date DESC, total_score DESC",
            "reviews":            "ORDER BY review_date DESC, days_held DESC",
            "earnings_history":   "ORDER BY fiscal_period DESC, code",
            "discovery_history":  "ORDER BY generated_date DESC, rank",
            "discovery_tracking": "ORDER BY generated_date DESC",
            "holdings":           "ORDER BY entry_date DESC",
            "snapshots":          "ORDER BY captured_at DESC",
            "user_config":        "ORDER BY key",
        }
        if name not in ALLOWED:
            raise HTTPException(404, f"table not found: {name}")
        import stock_db
        conn = stock_db.get_db()
        try:
            order = ALLOWED[name]
            cur = conn.execute(f"SELECT * FROM {name} {order} LIMIT ?", [limit])
            cols = [d[0] for d in cur.description]
            rows_raw = cur.fetchall()
            total_count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        finally:
            conn.close()

        def _jsonify(v):
            if v is None: return None
            if hasattr(v, "isoformat"):
                return v.isoformat(sep=" ", timespec="seconds") if hasattr(v, "hour") else v.isoformat()
            return v

        rows = [{k: _jsonify(v) for k, v in zip(cols, r)} for r in rows_raw]
        return {
            "table": name,
            "columns": cols,
            "rows": rows,
            "returned": len(rows),
            "total_in_db": total_count,
            "limited": len(rows) < total_count,
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

    # ────────── picks 自动评级（加股后异步触发） ──────────
    # lock 文件：被自动评级或日批 daily_picks_v5 占用时存在；写入 JSON {pid, started_at, trigger}
    _PICKS_LOCK = Path("/tmp/picks_rerun.lock")
    _PICKS_LOG = Path("/tmp/picks_rerun.log")

    def _rerun_status() -> dict[str, Any]:
        """读 lock 文件，判断是否在跑；自动清理 stale lock（pid 已死）。"""
        import json as _json, os as _os, time as _time
        if not _PICKS_LOCK.exists():
            return {"running": False}
        try:
            info = _json.loads(_PICKS_LOCK.read_text())
        except Exception:
            _PICKS_LOCK.unlink(missing_ok=True)
            return {"running": False, "stale_cleared": True}
        pid = info.get("pid")
        alive = False
        if pid:
            try:
                _os.kill(pid, 0)  # signal 0 = 探活
                alive = True
            except OSError:
                alive = False
        if not alive:
            _PICKS_LOCK.unlink(missing_ok=True)
            return {"running": False, "stale_cleared": True, "last_pid": pid}
        age_s = _time.time() - info.get("started_at", _time.time())
        return {
            "running": True,
            "pid": pid,
            "started_at": info.get("started_at"),
            "age_s": round(age_s, 1),
            "trigger": info.get("trigger"),
        }

    def _spawn_picks_rerun(
        trigger: str,
        *,
        force_refresh: bool = False,
        bypass_ic_gate: bool = False,
        bypass_audit_gate: bool = False,
        bypass_reason: str | None = None,
    ) -> dict[str, Any]:
        """启动 daily_picks_v5 子进程（detached），写 lock。已在跑则 noop。

        force_refresh=True 时删 factor cache，保证新加的股被评（否则 cache 命中跳过拉因子）。
        watchlist 触发的 rerun 总是 force_refresh，因为 watchlist 才刚变化。
        """
        import json as _json, os as _os, time as _time, subprocess as _sub
        existing = _rerun_status()
        if existing.get("running"):
            return {"status": "already_running", **existing}

        repo_root = str(_REPO_ROOT)
        # watchlist 变化触发的 rerun 强制重拉因子；手动触发可省时间复用 cache
        if force_refresh or trigger.startswith("watchlist:"):
            cache_file = _REPO_ROOT / "data" / "latest" / "factor_scores_today.json"
            try:
                cache_file.unlink(missing_ok=True)
            except Exception:
                pass

        cmd = [
            sys.executable,
            str(_REPO_ROOT / "scripts" / "pipeline" / "daily_picks_v5.py"),
        ]
        bypasses: list[str] = []
        if bypass_ic_gate:
            cmd.append("--bypass-ic-gate")
            bypasses.append("ic_gate")
        if bypass_audit_gate:
            cmd.append("--bypass-audit-gate")
            bypasses.append("audit_gate")

        # 不走 shell，避免 trigger/body 注入；wrapper 只负责落 log 和结束时清 lock。
        wrapper_code = r"""
import datetime as _dt
import json as _json
import subprocess as _sub
import sys as _sys
from pathlib import Path as _Path

cmd = _json.loads(_sys.argv[1])
log_path = _Path(_sys.argv[2])
lock_path = _Path(_sys.argv[3])
repo_root = _sys.argv[4]
trigger = _sys.argv[5].replace("\n", " ")[:240]
bypass_info = _sys.argv[6].replace("\n", " ")[:240]
reason = _sys.argv[7].replace("\n", " ")[:240]

log_path.parent.mkdir(parents=True, exist_ok=True)
rc = 1
try:
    with log_path.open("a", encoding="utf-8") as log:
        ts = _dt.datetime.now().isoformat(timespec="seconds")
        log.write(f"[picks-rerun] start trigger={trigger} bypass={bypass_info or '-'} reason={reason or '-'} at {ts}\n")
        log.flush()
        rc = _sub.call(cmd, cwd=repo_root, stdout=log, stderr=_sub.STDOUT)
        ts = _dt.datetime.now().isoformat(timespec="seconds")
        log.write(f"[picks-rerun] done exit={rc} at {ts}\n")
finally:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
_sys.exit(rc)
"""
        proc = _sub.Popen(
            [
                sys.executable,
                "-c",
                wrapper_code,
                _json.dumps(cmd, ensure_ascii=False),
                str(_PICKS_LOG),
                str(_PICKS_LOCK),
                repo_root,
                str(trigger or "manual"),
                ",".join(bypasses),
                str(bypass_reason or ""),
            ],
            cwd=repo_root,
            stdout=_sub.DEVNULL,
            stderr=_sub.DEVNULL,
            start_new_session=True,  # detach
        )
        _PICKS_LOCK.write_text(_json.dumps({
            "pid": proc.pid,
            "started_at": _time.time(),
            "trigger": trigger,
            "force_refresh": force_refresh or trigger.startswith("watchlist:"),
            "bypasses": bypasses,
            "bypass_reason": bypass_reason or "",
            "cmd": cmd,
        }, ensure_ascii=False))
        return {
            "status": "started",
            "pid": proc.pid,
            "trigger": trigger,
            "bypasses": bypasses,
        }

    @app.get("/api/picks/rerun-status")
    def picks_rerun_status() -> dict[str, Any]:
        """前端轮询用：picks 评级 job 是否还在跑。"""
        return _rerun_status()

    @app.post("/api/picks/rerun")
    def picks_rerun(item: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """手动触发 daily_picks_v5 重跑。

        body 可选 {trigger, force_refresh}；force_refresh=true 时删 factor cache 强制重拉。
        默认不绕过 IC / audit 闸门。若确需紧急绕过，必须同时传：
        {allow_gate_bypass: true, bypass_ic_gate/bypass_audit_gate: true, bypass_reason: "..."}。
        """
        bypass_ic = bool(item.get("bypass_ic_gate", False))
        bypass_audit = bool(item.get("bypass_audit_gate", False))
        if (bypass_ic or bypass_audit) and not bool(item.get("allow_gate_bypass", False)):
            raise HTTPException(
                400,
                "gate bypass is disabled by default; pass allow_gate_bypass=true "
                "and bypass_reason to make the override explicit",
            )
        return _spawn_picks_rerun(
            item.get("trigger") or "manual",
            force_refresh=bool(item.get("force_refresh", False)),
            bypass_ic_gate=bypass_ic,
            bypass_audit_gate=bypass_audit,
            bypass_reason=str(item.get("bypass_reason") or ""),
        )

    @app.get("/api/picks/latest-summary")
    def picks_latest_summary() -> dict[str, Any]:
        """返回 {code: {pick_date, rating, total_score, ai_score, theme}}，
        每只股取它**自己**最新 pick_date 的一行（不是全表最新 pick_date）。

        前端用这个实时刷新 AI 评级列。
        """
        import stock_db
        conn = stock_db.get_db()
        try:
            rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT
                        code, pick_date, rating, total_score, ai_score, theme,
                        ROW_NUMBER() OVER (PARTITION BY code ORDER BY pick_date DESC) AS rn
                    FROM picks
                )
                SELECT code, pick_date, rating, total_score, ai_score, theme
                FROM ranked
                WHERE rn = 1
                """,
            ).fetchall()
        finally:
            conn.close()
        return {
            r[0]: {
                "pick_date": r[1].isoformat() if r[1] else None,
                "rating": r[2],
                "total_score": r[3],
                "ai_score": r[4],
                "theme": r[5],
            }
            for r in rows
        }

    @app.get("/api/picks/by-code/{code}")
    def picks_by_code(code: str) -> dict[str, Any]:
        """查这只股的最新评级（最大 pick_date）。返回 {found, pick_date, rating, total_score} 或 {found: false}。"""
        import stock_db
        conn = stock_db.get_db()
        try:
            row = conn.execute(
                """
                SELECT pick_date, rating, total_score, ai_score, theme, model_source
                FROM picks
                WHERE code = ?
                ORDER BY pick_date DESC
                LIMIT 1
                """,
                [code],
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {"found": False, "code": code}
        return {
            "found": True,
            "code": code,
            "pick_date": row[0].isoformat() if row[0] else None,
            "rating": row[1],
            "total_score": row[2],
            "ai_score": row[3],
            "theme": row[4],
            "model_source": row[5],
        }

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
