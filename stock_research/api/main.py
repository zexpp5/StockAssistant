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
import json
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

    # DuckDB 写锁冲突（refresh_system_universe_v2 / daily_refresh.sh 等长进程占锁）
    # 统一返回 200 + status=db_busy，让前端兜底显示"稍后重试"而不是 500 把卡片打没。
    try:
        import duckdb as _duckdb_for_handler
        from fastapi.responses import JSONResponse as _JSONResponse

        @app.exception_handler(_duckdb_for_handler.IOException)
        async def _duckdb_lock_handler(request, exc):
            msg = str(exc)
            if "lock" in msg.lower():
                return _JSONResponse(
                    status_code=200,
                    content={
                        "status": "db_busy",
                        "message": "DuckDB 写锁被其他进程占用，请稍后刷新",
                        "_path": str(getattr(request, "url", "")),
                    },
                )
            # 其他 IO 错误照常抛 500
            return _JSONResponse(status_code=500, content={"detail": msg})
    except Exception:
        pass

    # ────────── 健康检查 ──────────
    @app.get("/health")
    def health():
        """进程存活探活：始终 HTTP 200，避免 DuckDB 被流水线占锁时前端误判「API 未启动」。

        db=ok 时附带表行数；db=locked/error 时仅报告原因，不抛 500。
        """
        from .. import config as _c
        payload: dict[str, Any] = {
            "status": "ok",
            "api": "up",
            "investors_tracked": len(_c.INVESTORS_13F),
        }
        try:
            import stock_db
            mw_n = len(stock_db.fetch_manual_watchlist())
            u_n = len(stock_db.fetch_universe_for_ai_recommendations())
            payload.update({
                "db": "ok",
                "manual_watchlist_rows": mw_n,
                "system_universe_rows": u_n,
                "data_source": "V2 DuckDB",
            })
        except Exception as exc:
            msg = str(exc)
            locked = "Could not set lock" in msg or "Conflicting lock" in msg
            payload["db"] = "locked" if locked else "error"
            payload["db_detail"] = msg[:240]
        return payload

    # ────────── 汇率（单一来源，前端读这里替代两份 JS 硬编码 FX_TO_RMB） ──────────
    @app.get("/api/fx-rates")
    def get_fx_rates() -> dict[str, Any]:
        """本币→RMB 汇率单一来源。前端启动时拉一次写入 window.FX_RATES。

        升级到实时汇率时只改 scripts/lib/fx_rates.py 内部实现,接口字段不变。
        """
        import fx_rates
        payload = fx_rates.get_fx_payload()
        return {
            "rates": dict(payload.get("rates") or {}),
            "as_of": payload.get("as_of"),
            "source": payload.get("source"),
            "status": payload.get("status"),
            "refreshed_at": payload.get("refreshed_at"),
            "errors": payload.get("errors") or [],
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

    # ────────── Manual Watchlist (V2 单源真相 · 2026-05-21 V1 cutover) ──────────
    @app.get("/api/watchlist")
    def list_watchlist() -> list[dict[str, Any]]:
        """读 V2 manual_watchlist 全表（用户在 dashboard 手动加的自选股）。"""
        import stock_db
        rows = stock_db.fetch_manual_watchlist()
        for r in rows:
            for k in ("created_at", "updated_at"):
                v = r.get(k)
                if v is not None and hasattr(v, "isoformat"):
                    r[k] = v.isoformat()
        return rows

    @app.get("/api/watchlist/{code}")
    def get_watchlist_one(code: str) -> dict[str, Any]:
        """V2 manual_watchlist 单条读取（按 symbol/code 模糊匹配第一只）。"""
        import stock_db
        rows = [r for r in stock_db.fetch_manual_watchlist() if r.get("symbol") == code]
        if not rows:
            raise HTTPException(404, f"manual_watchlist code not found: {code}")
        row = rows[0]
        for k in ("created_at", "updated_at"):
            v = row.get(k)
            if v is not None and hasattr(v, "isoformat"):
                row[k] = v.isoformat()
        return row

    @app.post("/api/watchlist")
    def create_watchlist(item: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """V2 manual_watchlist 新增一条（按 (market, symbol) PK upsert）。

        入库成功后**异步触发** daily_picks_v5 评级（不阻塞响应）。
        同时把前端送来的 chain/chain_tier/chain_role/layman_intro 写入 chain_metadata
        （没传或全空则跳过；source='manual_override'）。
        """
        import stock_db
        if not item.get("code") and not item.get("symbol"):
            raise HTTPException(400, "code/symbol is required")
        n = stock_db.upsert_manual_watchlist([item])
        chain_n = stock_db.upsert_chain_metadata([item])
        rerun_info: dict[str, Any] = {}
        try:
            rerun_info = _spawn_picks_rerun(trigger=f"watchlist:add:{item.get('code') or item.get('symbol')}")
        except Exception as e:
            rerun_info = {"status": "error", "error": str(e)}
        return {
            "status": "ok",
            "code": item.get("code") or item.get("symbol"),
            "rows_affected": n,
            "chain_rows_affected": chain_n,
            "rerun": rerun_info,
        }

    @app.put("/api/watchlist/{code}")
    def update_watchlist(code: str, item: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """V2 manual_watchlist 更新一条（URL code 强制覆盖 body）。

        chain/tier/role/intro 同步写入 chain_metadata（source='manual_override'）。
        """
        import stock_db
        item["code"] = code
        n = stock_db.upsert_manual_watchlist([item])
        chain_n = stock_db.upsert_chain_metadata([item])
        return {"status": "ok", "code": code, "rows_affected": n, "chain_rows_affected": chain_n}

    @app.get("/api/chain-metadata")
    def list_chain_metadata() -> dict[str, Any]:
        """全量 chain_metadata，供前端编辑后热刷新 WATCHLIST_CHAIN_INFO。

        返回 {symbol: {chain, chain_tier, chain_role, layman_intro, source, name}} 格式,
        和 build_stock_dashboard 烘进 HTML 时的结构对齐。
        """
        import stock_db
        rows = stock_db.fetch_chain_metadata_all()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            out[r["symbol"]] = {
                "chain": r["chain"],
                "chain_tier": r["chain_tier"],
                "chain_role": r["chain_role"],
                "layman_intro": r["layman_intro"],
                "source": r["source"],
            }
        # 把 manual_watchlist.name / industry / business 也合并进去,
        # 这样 stockPill 看到 inWatchlist=true 才会渲染 badge, 行业/主营也能立刻显示。
        try:
            wl_rows = stock_db.fetch_manual_watchlist()
            for w in wl_rows:
                sym = w.get("symbol")
                if not sym:
                    continue
                out.setdefault(sym, {})
                if w.get("name"):
                    out[sym]["name"] = w["name"]
                if w.get("industry"):
                    out[sym]["industry"] = w["industry"]
                if w.get("business"):
                    out[sym]["business"] = w["business"]
        except Exception:
            pass
        return out

    @app.delete("/api/watchlist/{code}")
    def delete_watchlist(code: str) -> dict[str, Any]:
        """V2 manual_watchlist 删除（按 symbol 找市场再删）。"""
        import stock_db
        rows = [r for r in stock_db.fetch_manual_watchlist() if r.get("symbol") == code]
        if not rows:
            raise HTTPException(404, f"manual_watchlist code not found: {code}")
        n = stock_db.delete_manual_watchlist(rows[0]["market"], code)
        return {"status": "ok", "code": code, "rows_deleted": n}

    # ────────── DB 全库浏览（深度研究 → DB 全库 tab） ──────────
    @app.get("/api/db/all-stocks")
    def db_all_stocks() -> dict[str, Any]:
        """按市场分组返回系统已拉取科技/AI 股票池 + 最新行情 + 最新 picks 评级。

        返回结构：
          {
            "as_of": {"prices_date": "2026-05-12", "picks_date": "2026-05-12"},
            "counts": {"美股": 82, "A股": 12, "港股": 6, "其他": 8, "total": 108},
            "groups": {"美股": [row, ...], "A股": [...], "港股": [...], "其他": [...]},
          }
        重要边界：
          - 这里不是 watchlist，不展示用户手动自选股身份。
          - 若同一 ticker 也在 watchlist，它在本接口里仍只按系统科技池记录展示。
          - watchlist / 自选股 AI 优选 属于“我的池子”模块。
        """
        import stock_db

        def _jsonify(v):
            if v is None:
                return None
            if hasattr(v, "isoformat"):
                if hasattr(v, "hour"):
                    return v.isoformat(sep=" ", timespec="seconds")
                return v.isoformat()
            return v

        def _tech_pool_meta() -> dict[str, dict[str, Any]]:
            from stock_research.core.hk_universe import fetch_hk_tech_universe
            from stock_research.core.us_universe import fetch_us_ai_tech_universe
            from stock_research.core.a_share_universe import fetch_a_share_tech_universe

            meta: dict[str, dict[str, Any]] = {}

            def add(item: dict[str, Any], market: str, theme: str) -> None:
                ticker = str(item["ticker"])
                raw = str(item.get("raw_ticker") or ticker.split(".")[0])
                row = {
                    "code": ticker,
                    "name": item.get("name") or ticker,
                    "market": market,
                    "industry": item.get("sector") or "",
                    "theme": theme,
                    "ai_relevance": "科技/AI universe",
                    "source": f"tech_universe:{item.get('source') or ''}",
                    "_source_origin": "system_pool",
                }
                meta[ticker] = row
                meta[raw] = {**row, "code": raw}

            for item in fetch_us_ai_tech_universe():
                add(item, "美股", "US AI/tech")
            for item in fetch_hk_tech_universe():
                add(item, "港股", "HK tech")
            for item in fetch_a_share_tech_universe():
                add(item, "A股", "A-share AI/tech")
            return meta

        import duckdb
        # 2026-05-22: read_only=False 与 stock_db.get_db 保持一致,
        # 避免 API 多线程内不同 mode 的 conn 冲突
        conn = duckdb.connect(stock_db.DB_PATH, read_only=False)
        try:
            tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
            if "system_universe" not in tables or "price_daily" not in tables:
                raise HTTPException(503, "V2 tables missing — run init_stock_db_v2.py first")
            enrichment_by_code: dict[str, dict[str, Any]] = {}
            if True:  # V2-only path (V1 fallback removed 2026-05-21)
                pool_meta = {}
                market_label_map = {"US": "美股", "HK": "港股", "CN": "A股"}

                def _add_v2(item: dict[str, Any]) -> None:
                    ticker = str(item.get("symbol") or item.get("ticker") or "").strip()
                    if not ticker:
                        return
                    market_code = str(item.get("market") or "").upper()
                    market = market_label_map.get(market_code, market_code)
                    row = {
                        "code": ticker,
                        "name": item.get("name") or ticker,
                        "market": market,
                        "industry": item.get("industry") or "",
                        "theme": item.get("theme") or "",
                        "ai_relevance": "科技/AI universe",
                        "source": f"pool:{item.get('pool_id') or 'system_tech_universe'}",
                        "_source_origin": "system_pool",
                    }
                    pool_meta[ticker] = row

                for row in conn.execute(
                    "SELECT pool_id, market, symbol, raw_symbol, name, theme, industry, source FROM system_universe WHERE active = TRUE"
                ).fetchall():
                    _add_v2({
                        "pool_id": row[0],
                        "market": row[1],
                        "symbol": row[2],
                        "raw_symbol": row[3],
                        "name": row[4],
                        "theme": row[5],
                        "industry": row[6],
                        "source": row[7],
                    })

                price_rows = conn.execute(
                    """
                    SELECT market, symbol, trade_date, close, prev_close, currency, market_cap,
                           forward_pe, trailing_pe, peg_ratio, ytd_pct, one_week_pct,
                           one_month_pct, one_year_pct, source, fetched_at
                    FROM price_daily
                    QUALIFY ROW_NUMBER() OVER (
                        PARTITION BY market, symbol
                        ORDER BY trade_date DESC, fetched_at DESC
                    ) = 1
                    """
                ).fetchall()
                price_cols = [d[0] for d in conn.description]
                prices_by_code = {f"{r[price_cols.index('symbol')]}": dict(zip(price_cols, r)) for r in price_rows}

                pick_rows = []
                latest_run = conn.execute(
                    "SELECT run_id FROM recommendation_runs ORDER BY generated_at DESC LIMIT 1"
                ).fetchone()
                if latest_run:
                    pick_rows = conn.execute(
                        """
                        SELECT market, symbol, name, rating, signal, total_score,
                               factor_scores_json, recommendation_reason, risk_flags_json,
                               universe_scope, source_origin
                        FROM recommendation_picks
                        WHERE run_id = ?
                        """,
                        [latest_run[0]],
                    ).fetchall()
                pick_cols = [
                    "market", "symbol", "name", "rating", "signal", "total_score",
                    "factor_scores_json", "recommendation_reason", "risk_flags_json",
                    "universe_scope", "source_origin",
                ]
                picks_by_code = {}
                for r in pick_rows:
                    row = dict(zip(pick_cols, r))
                    code = str(row.get("symbol") or "")
                    picks_by_code[code] = row

                if "source_raw_snapshots" in tables:
                    for payload_json, fetched_at in conn.execute(
                        """
                        SELECT payload_json, fetched_at
                        FROM source_raw_snapshots
                        WHERE source = 'v2_system_enrichment'
                        ORDER BY fetched_at DESC
                        """
                    ).fetchall():
                        try:
                            payload = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
                        except Exception:
                            continue
                        if not isinstance(payload, dict):
                            continue
                        symbol = str(payload.get("symbol") or "").strip().upper()
                        raw_symbol = str(payload.get("raw_symbol") or "").strip().upper()
                        market_code = str(payload.get("market") or "").strip().upper()
                        if not symbol:
                            continue
                        fetched_ts = payload.get("fetched_at") or _jsonify(fetched_at)
                        enrich_row = {
                            "earnings": payload.get("earnings") or "",
                            "conclusion": payload.get("conclusion") or "",
                            "risks": payload.get("risks") or "",
                            "info_breakdown": payload.get("info_breakdown") or "",
                            "notes": payload.get("notes") or "",
                            "verification": payload.get("source_text") or "",
                            "updated_at": fetched_ts,
                            "earnings_fetched_at": fetched_ts,
                            "_v2_enrichment_source": "source_raw_snapshots.v2_system_enrichment",
                        }
                        keys = {symbol}
                        if raw_symbol:
                            keys.add(raw_symbol)
                        if market_code:
                            keys.add(f"{market_code}:{symbol}")
                            if raw_symbol:
                                keys.add(f"{market_code}:{raw_symbol}")
                        for key in keys:
                            enrichment_by_code.setdefault(key, enrich_row)

                earnings_fetched = {}
                prices_date = conn.execute("SELECT MAX(trade_date) FROM price_daily").fetchone()[0]
                picks_date = conn.execute("SELECT MAX(generated_at) FROM recommendation_runs").fetchone()[0]
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

        groups: dict[str, list[dict[str, Any]]] = {"美股": [], "A股": [], "港股": [], "其他": []}
        for code, meta in pool_meta.items():
            price_row = prices_by_code.get(str(code)) or prices_by_code.get(str(code).split(".")[0]) or {}
            if not meta:
                continue
            # 每行带上 earnings 真实抓取时间（earnings_history.fetched_at 的 max）
            base = {
                **meta,
                "market": meta.get("market") or _infer_market_from_code(str(code)),
                "earnings_fetched_at": earnings_fetched.get(code),
            }
            enrich_row = enrichment_by_code.get(str(code).upper()) or {}
            if enrich_row:
                base.update(enrich_row)
            merged: dict[str, Any] = {}
            for k, v in base.items():
                merged[k] = _jsonify(v)
            for k, v in price_row.items():
                if k in ("code", "name", "symbol", "market"):
                    continue
                merged[f"price_{k}"] = _jsonify(v)
            pk = picks_by_code.get(str(code)) or picks_by_code.get(str(code).split(".")[0]) or {}
            for k, v in pk.items():
                if k in ("code", "name", "market", "symbol"):
                    continue
                merged[f"pick_{k}"] = _jsonify(v)
            fs_json = pk.get("factor_scores_json") if pk else None
            if fs_json:
                try:
                    fs = json.loads(fs_json) if isinstance(fs_json, str) else fs_json
                    # V1 命名兼容层：把 V2 factor_scores keys 暴露成 V1 字段名供前端表格用
                    merged["pick_val_score"] = fs.get("valuation")
                    merged["pick_trend_score"] = fs.get("momentum")
                    merged["pick_cred_score"] = fs.get("data_quality")
                    merged["pick_coverage_score"] = fs.get("coverage")
                    merged["pick_f_score"] = fs.get("f_score")  # 2026-05-21 新增（Piotroski P5-Lite）
                    # ai_relevance 不在 V2 factor_scores keys 里，永远 None；保留旧字段是兼容前端
                    merged["pick_ai_score"] = fs.get("ai_relevance")
                except Exception:
                    pass
            if pk.get("recommendation_reason") and not merged.get("conclusion"):
                merged["conclusion"] = pk.get("recommendation_reason")
            if pk.get("risk_flags_json") and not merged.get("risks"):
                try:
                    flags = json.loads(pk.get("risk_flags_json"))
                    if flags:
                        merged["risks"] = "\n".join(f"- {flag}" for flag in flags)
                except Exception:
                    pass
            groups[_classify(merged.get("market"))].append(merged)

        return {
            "as_of": {
                "prices_date": _jsonify(prices_date),
                "picks_date": _jsonify(picks_date),
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
            tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
            # V2 manual_watchlist 单条查询（按 symbol）
            wl_rows = [r for r in stock_db.fetch_manual_watchlist(conn=conn) if r.get("symbol") == code]
            wl_row = wl_rows[0] if wl_rows else None

            def _rows(q: str) -> list[dict[str, Any]]:
                cur = conn.execute(q, [code])
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]

            # 2026-05-21 V1 cutover：纯 V2 查询（V1 表已 DROP）
            prices = _rows(
                "SELECT market, symbol AS code, trade_date AS date, close AS price, "
                "prev_close, currency, market_cap, forward_pe, trailing_pe, peg_ratio, "
                "ytd_pct, one_week_pct, one_month_pct, one_year_pct, source, fetched_at "
                "FROM price_daily WHERE symbol = ? ORDER BY trade_date DESC, fetched_at DESC"
            )
            picks = _rows(
                "SELECT rp.symbol AS code, rp.name, rp.rank, rp.rating, rp.signal, "
                "rp.total_score, rp.factor_scores_json, rp.recommendation_reason, "
                "rp.entry_price, rp.entry_currency, rp.universe_scope, "
                "rr.run_date AS pick_date, rr.generated_at "
                "FROM recommendation_picks rp JOIN recommendation_runs rr ON rp.run_id = rr.run_id "
                "WHERE rp.symbol = ? ORDER BY rr.generated_at DESC"
            )
            # 验收文档 §8: 明确要求 recommendation_runs / recommendation_picks / pick_outcomes 三段 V2 追溯
            recommendation_runs = _rows(
                "SELECT DISTINCT rr.run_id, rr.run_date, rr.strategy_version, rr.model_version, "
                "rr.universe_scope, rr.data_cutoff_at, rr.generated_at, rr.status "
                "FROM recommendation_runs rr "
                "JOIN recommendation_picks rp ON rp.run_id = rr.run_id "
                "WHERE rp.symbol = ? ORDER BY rr.generated_at DESC"
            )
            recommendation_picks = _rows(
                "SELECT rp.run_id, rp.symbol, rp.name, rp.market, rp.rank, rp.rating, rp.signal, "
                "rp.total_score, rp.factor_scores_json, rp.recommendation_reason, rp.risk_flags_json, "
                "rp.entry_price, rp.entry_currency, rp.universe_scope, rp.source_origin, "
                "rr.run_date, rr.strategy_version, rr.model_version, rr.generated_at "
                "FROM recommendation_picks rp JOIN recommendation_runs rr ON rp.run_id = rr.run_id "
                "WHERE rp.symbol = ? ORDER BY rr.generated_at DESC"
            )
            pick_outcomes: list[dict[str, Any]] = []
            if "pick_outcomes" in tables:
                pick_outcomes = _rows(
                    "SELECT po.run_id, po.market, po.symbol, po.horizon, po.outcome_date, "
                    "po.return_pct, po.benchmark_symbol, po.benchmark_pct, po.alpha_pct, "
                    "po.is_success, po.updated_at, "
                    "rr.run_date, rr.strategy_version, rr.universe_scope "
                    "FROM pick_outcomes po LEFT JOIN recommendation_runs rr ON rr.run_id = po.run_id "
                    "WHERE po.symbol = ? ORDER BY po.outcome_date DESC, po.horizon ASC"
                )
            reviews: list[dict[str, Any]] = []  # V2 评估在 pick_outcomes 已有
            discovery: list[dict[str, Any]] = []  # V2 历史走 recommendation_runs
            earnings_history: list[dict[str, Any]] = []  # V2 财报数据在 source_raw_snapshots
            if not wl_row:
                # V2: 用 system_universe 里这只股的元数据兜 V1 watchlist 形状
                v2_wl = _rows(
                    "SELECT symbol AS code, name, market, industry, theme, "
                    "source, first_seen_at AS created_at, last_seen_at AS updated_at "
                    "FROM system_universe WHERE symbol = ?"
                )
                if v2_wl:
                    wl_row = v2_wl[0]
            if wl_row and "source_raw_snapshots" in tables:
                enrich_rows = conn.execute(
                    """
                    SELECT payload_json, fetched_at
                    FROM source_raw_snapshots
                    WHERE source = 'v2_system_enrichment'
                      AND json_extract_string(payload_json, '$.symbol') = ?
                    ORDER BY fetched_at DESC
                    LIMIT 1
                    """,
                    [code],
                ).fetchall()
                if enrich_rows:
                    payload_json, fetched_at = enrich_rows[0]
                    try:
                        payload = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
                    except Exception:
                        payload = {}
                    if isinstance(payload, dict):
                        wl_row = {
                            **wl_row,
                            "earnings": payload.get("earnings") or "",
                            "conclusion": payload.get("conclusion") or "",
                            "risks": payload.get("risks") or "",
                            "info_breakdown": payload.get("info_breakdown") or "",
                            "notes": payload.get("notes") or "",
                            "verification": payload.get("source_text") or "",
                            "updated_at": payload.get("fetched_at") or fetched_at,
                            "_v2_enrichment_source": "source_raw_snapshots.v2_system_enrichment",
                        }
            if not earnings_history and "financial_statements" in tables:
                earnings_history = _rows(
                    "SELECT symbol AS code, period_end_date AS fiscal_period, source, "
                    "fetched_at, payload_json "
                    "FROM financial_statements WHERE symbol = ? "
                    "ORDER BY period_end_date DESC, fetched_at DESC"
                )
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

        if (not wl_row and not prices and not picks and not reviews and not discovery
                and not earnings_history and not recommendation_runs and not pick_outcomes):
            raise HTTPException(404, f"code not found in any table: {code}")

        return {
            "code": code,
            "watchlist": _walk(wl_row) if wl_row else None,
            "prices": _walk(prices),
            "picks": _walk(picks),
            "reviews": _walk(reviews),
            "discovery": _walk(discovery),
            "earnings_history": _walk(earnings_history),
            # V2 追溯三段（验收文档 §8）
            "recommendation_runs": _walk(recommendation_runs),
            "recommendation_picks": _walk(recommendation_picks),
            "pick_outcomes": _walk(pick_outcomes),
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
        """V2: 每只标的取最新 recommendation_runs 里的 pick。

        前端用这个实时刷新 AI 评级列。
        """
        import stock_db
        conn = stock_db.get_db()
        try:
            rows = conn.execute(
                """
                WITH latest_run AS (
                    SELECT run_id, run_date, generated_at
                    FROM recommendation_runs
                    WHERE universe_scope = 'system_tech_universe' AND status = 'generated'
                    ORDER BY generated_at DESC LIMIT 1
                )
                SELECT rp.symbol AS code, lr.run_date AS pick_date, rp.rating,
                       rp.total_score, NULL AS ai_score, NULL AS theme
                FROM recommendation_picks rp JOIN latest_run lr USING(run_id)
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
        """V2: 单只标的最新 recommendation_picks 评级。"""
        import stock_db
        conn = stock_db.get_db()
        try:
            row = conn.execute(
                """
                SELECT rr.run_date, rp.rating, rp.total_score, NULL, NULL, rp.universe_scope
                FROM recommendation_picks rp JOIN recommendation_runs rr ON rp.run_id = rr.run_id
                WHERE rp.symbol = ?
                ORDER BY rr.generated_at DESC LIMIT 1
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

    def _json_dates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for r in rows:
            for k, v in list(r.items()):
                if hasattr(v, "isoformat") and (
                    k.endswith("_date")
                    or k.endswith("_as_of")
                    or k in {"created_at", "updated_at", "generated_at"}
                ):
                    r[k] = v.isoformat()
        return rows

    def _json_any(value: Any) -> Any:
        if hasattr(value, "isoformat"):
            return value.isoformat()
        if isinstance(value, list):
            return [_json_any(v) for v in value]
        if isinstance(value, dict):
            return {k: _json_any(v) for k, v in value.items()}
        return value

    # ────────── 真实持仓 / 模型模拟仓（V2 split · 不再混用 holdings.source） ──────────
    @app.get("/api/real-holdings")
    def list_real_holdings(include_closed: bool = False) -> list[dict[str, Any]]:
        import stock_db
        rows = stock_db.fetch_all_real_holdings()
        if not include_closed:
            # 向后兼容：迁移前老行 close_status 为 NULL，仍要展示；只隐藏已清仓。
            rows = [h for h in rows if (h.get("close_status") or "open") != "closed"]
        # 附带活跃交易笔数：前端据此决定是否显示「展开批次」三角。
        counts = stock_db.fetch_active_trade_counts()
        for h in rows:
            h["trade_count"] = counts.get(
                (h.get("account"), h.get("market"), (h.get("symbol") or "").upper()), 0)
        return _json_dates(rows)

    @app.post("/api/real-holdings")
    def create_real_holding(item: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """录入真实持仓 + 自动同步到自选股(2026-05-22 方案 A)。

        持仓与 manual_watchlist 之前完全解耦,导致非科技股(MCD/IAUM/BRK.B 等)
        永远不进 daily_picks_v5 评级,verdict 永远显示 ⚪ AI 未覆盖。
        现在:录入持仓 → upsert manual_watchlist → 触发 picks rerun,
        让系统每天对持仓也评级,verdict 才能产生有意义的 7 档判断。
        """
        import stock_db
        symbol = item.get("code") or item.get("symbol")
        if not symbol:
            raise HTTPException(400, "code is required")
        insert_result = stock_db.insert_real_holding_result(item)
        new_id = int(insert_result["id"])

        # 自动同步到 manual_watchlist + 触发评级,失败不影响主流程返回。
        # 幂等命中的重复提交不再重复触发评级任务。
        sync_info: dict[str, Any] = {"status": "skipped"}
        if insert_result.get("created"):
            try:
                stock_db.upsert_manual_watchlist([{
                    "code": symbol,
                    "symbol": symbol,
                    "name": item.get("name"),
                    "notes": "auto-sync from real_holdings",
                }])
                sync_info = _spawn_picks_rerun(trigger=f"watchlist:holding:{symbol}")
                sync_info["watchlist_synced"] = True
            except Exception as e:
                sync_info = {"status": "error", "error": str(e), "watchlist_synced": False}
        elif insert_result.get("deduped"):
            sync_info = {"status": "skipped", "reason": "recent_duplicate", "watchlist_synced": False}

        return {"status": "ok", "id": new_id, "sync": sync_info, **insert_result}

    @app.post("/api/real-holdings/fetch-prices")
    def fetch_holdings_prices() -> dict[str, Any]:
        """立刻触发 intraday_refresh_holdings.py 子进程刷新真实持仓行情。

        只刷新 real_holdings 中实际持仓的标的,不依赖 watchlist,不写推荐池。
        非阻塞,返回 PID;脚本会拉价、写 price_daily,并重跑 real_holding_review。
        失败 fail-soft 返回 error 字段,不抛 HTTP 5xx。
        """
        import subprocess as _sub
        import time as _time
        refresh_script = _REPO_ROOT / "scripts" / "pipeline" / "intraday_refresh_holdings.py"
        log_dir = _REPO_ROOT / "data" / "latest"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "fetch_prices_on_demand.log"
        try:
            with open(log_path, "ab") as log_f:
                log_f.write(f"\n=== on-demand real-holdings refresh {_time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
                proc = _sub.Popen(
                    [sys.executable, str(refresh_script)], cwd=str(_REPO_ROOT),
                    stdout=log_f, stderr=_sub.STDOUT,
                    start_new_session=True,
                )
            return {
                "status": "started", "pid": proc.pid,
                "log": str(log_path),
                "hint": "约几十秒到 1 分钟后真实持仓拉价 + 体检重算完成,刷新 dashboard 现价/盈亏就更新",
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    @app.post("/api/real-holdings/sync-to-watchlist")
    def sync_holdings_to_watchlist() -> dict[str, Any]:
        """一键把现有所有 real_holdings 批量同步到 manual_watchlist + 触发一次评级。

        给 2026-05-22 方案 A 之前录入的老持仓补同步。POST endpoint 后自动同步逻辑
        只对新录入持仓生效,历史持仓走这个补救入口。批量同步只触发**一次**
        picks rerun(daily_picks_v5 启动后会评全表),而不是每只一次。
        """
        import stock_db
        holdings = stock_db.fetch_all_real_holdings()
        if not holdings:
            return {"status": "ok", "synced": 0, "rerun": {"status": "skipped", "reason": "no holdings"}}
        rows = []
        for h in holdings:
            sym = h.get("code") or h.get("symbol")
            if not sym:
                continue
            rows.append({"code": sym, "symbol": sym, "name": h.get("name"),
                         "notes": "auto-sync from real_holdings (batch)"})
        n = stock_db.upsert_manual_watchlist(rows) if rows else 0
        rerun_info: dict[str, Any] = {"status": "skipped"}
        if n > 0:
            try:
                rerun_info = _spawn_picks_rerun(trigger="watchlist:holdings-batch-sync")
            except Exception as e:
                rerun_info = {"status": "error", "error": str(e)}
        return {"status": "ok", "synced": n, "rerun": rerun_info}

    # ---- 账本 v2：交易流水（加仓 / 卖出 / 撤销 / 历史 / 收益摘要）----

    @app.post("/api/real-holdings/buy")
    def record_real_holding_buy(item: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """记录买入成交（账本口径）：新标的开仓 / 已持仓自动按加仓处理，并同步自选股。

        与旧 POST /api/real-holdings 的区别：这条走交易流水事实源（real_holding_trades），
        是 P0 之后的标准建仓入口。迁移后所有持仓均为账本口径，此入口对新老标的一致。
        """
        import stock_db
        symbol = item.get("code") or item.get("symbol")
        if not symbol:
            raise HTTPException(400, "code/symbol is required")
        try:
            res = stock_db.insert_real_holding_buy(item)
        except stock_db.LedgerError as e:
            raise HTTPException(400, str(e))
        sync_info: dict[str, Any] = {"status": "skipped"}
        if res.get("created"):
            try:
                stock_db.upsert_manual_watchlist([{
                    "code": symbol, "symbol": symbol, "name": item.get("name"),
                    "notes": "auto-sync from real_holdings",
                }])
                sync_info = _spawn_picks_rerun(trigger=f"watchlist:holding:{symbol}")
                sync_info["watchlist_synced"] = True
            except Exception as e:
                sync_info = {"status": "error", "error": str(e), "watchlist_synced": False}
        # 新建仓后用缓存行情立即重算持仓体检，让新标的当场进入「今日持仓体检」列表，
        # 不必等下一次拉行情/定时任务（与 /add /close /undo 行为对齐）。fail-soft。
        _refresh_holding_review_safe()
        new_h = stock_db.fetch_real_holding_by_id(res["holding_id"]) if res.get("holding_id") else None
        return _json_any({"status": "ok", "holding": new_h, "sync": sync_info, **res})

    def _holding_key_or_404(holding_id: int):
        import stock_db
        h = stock_db.fetch_real_holding_by_id(holding_id)
        if not h:
            raise HTTPException(404, f"real holding id not found: {holding_id}")
        return h

    def _refresh_holding_review_safe():
        """卖出/加仓/撤销后用缓存行情快速重跑持仓体检，让收益卡/曲线只含当前持仓，
        不再因为卖光的股票残留在旧体检里导致总览与曲线对不上。fail-soft。"""
        try:
            from stock_research.jobs.real_holding_review import build_real_holding_review
            build_real_holding_review(persist=True)
        except Exception as e:
            logger.warning("post-trade review refresh failed (non-fatal): %s", e)

    @app.post("/api/real-holdings/{holding_id}/add")
    def add_to_real_holding(holding_id: int, item: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """加仓：对已存在持仓追加一笔买入成交，rebuild 后返回更新后的聚合持仓。"""
        import stock_db
        h = _holding_key_or_404(holding_id)
        # 加仓必须沿用持仓本身的币种，不能按 ticker 重新推断（否则与买入 fx 口径打架）。
        payload = {**item, "account": h.get("account"), "market": h.get("market"),
                   "symbol": h.get("symbol"), "name": item.get("name") or h.get("name"),
                   "currency": item.get("currency") or h.get("currency")}
        try:
            res = stock_db.insert_real_holding_buy(payload)
        except stock_db.LedgerError as e:
            raise HTTPException(400, str(e))
        _refresh_holding_review_safe()
        new_h = stock_db.fetch_real_holding_by_id(res.get("holding_id") or holding_id)
        return _json_any({"status": "ok", "holding": new_h, **res})

    @app.post("/api/real-holdings/{holding_id}/close")
    def close_real_holding(holding_id: int, item: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """卖出 / 平仓：记录一笔卖出成交，rebuild 并返回已实现盈亏 + 剩余持仓。"""
        import stock_db
        h = _holding_key_or_404(holding_id)
        # 卖出必须沿用持仓本身的币种，不能按 ticker 重新推断（否则已实现盈亏 fx 口径错）。
        payload = {**item, "account": h.get("account"), "market": h.get("market"),
                   "symbol": h.get("symbol"), "name": item.get("name") or h.get("name"),
                   "currency": item.get("currency") or h.get("currency")}
        try:
            res = stock_db.insert_real_holding_sell(payload)
        except stock_db.LedgerConflict:
            rem = h.get("remaining_shares")
            raise HTTPException(400, f"卖出数量超过当前剩余股数（剩 {rem} 股），或与历史交易顺序冲突。")
        except stock_db.LedgerError as e:
            raise HTTPException(400, str(e))
        _refresh_holding_review_safe()
        new_h = stock_db.fetch_real_holding_by_id(res.get("holding_id") or holding_id)
        return _json_any({"status": "ok", "holding": new_h, **res})

    @app.get("/api/real-holdings/trade-history")
    def real_holdings_trade_history() -> dict[str, Any]:
        """已卖出 / 交易历史：全部 sell 成交，按卖出日期倒序。"""
        import stock_db
        return _json_any({"status": "ok", "sells": stock_db.fetch_real_holding_trade_history()})

    @app.get("/api/real-holdings/pnl-summary")
    def real_holdings_pnl_summary() -> dict[str, Any]:
        """收益摘要：已实现（成交日锁定汇率）+ 未实现（复用每日体检盯市）+ 合计。"""
        import stock_db
        base = stock_db.fetch_pnl_summary()  # realized + realized_since（权威，来自 trades）
        unrealized = None
        try:
            review = stock_db.fetch_latest_real_holding_review()
            items = (review or {}).get("items") or []
            open_pnls = [i.get("pnl_rmb") for i in items
                         if (i.get("pnl_rmb") is not None) and (i.get("close_status") in (None, "open", "partial"))]
            if items:
                unrealized = float(sum(p for p in open_pnls if p is not None))
        except Exception:
            unrealized = None
        realized = base.get("realized_pnl_rmb") or 0.0
        total = (realized + unrealized) if unrealized is not None else None
        return _json_any({"status": "ok", "realized_pnl_rmb": realized,
                          "unrealized_pnl_rmb": unrealized, "total_pnl_rmb": total,
                          "realized_since": base.get("realized_since")})

    # ---- 现金账本：入金/出金 + 账户总览(持仓市值+现金=总资产) ----

    def _holdings_market_value_rmb() -> float:
        """当前持仓市值(RMB)：复用每日体检的盯市值，只算 open/partial。"""
        import stock_db
        try:
            review = stock_db.fetch_latest_real_holding_review()
            items = (review or {}).get("items") or []
            return float(sum(float(i.get("current_value_rmb") or 0) for i in items
                             if i.get("close_status") in (None, "open", "partial")))
        except Exception:
            return 0.0

    @app.get("/api/real-holdings/account-summary")
    def real_holdings_account_summary() -> dict[str, Any]:
        """账户总览：总资产 = 持仓市值 + 现金；现金来自现金账本(含买卖自动进出)。"""
        import stock_db
        cash = stock_db.fetch_cash_summary()
        mv = _holdings_market_value_rmb()
        return _json_any({"status": "ok",
                          "holdings_value_rmb": mv,
                          "cash_rmb": cash["cash_rmb"],
                          "total_asset_rmb": mv + cash["cash_rmb"],
                          **cash})

    @app.get("/api/real-holdings/cash-flows")
    def list_cash_flows() -> dict[str, Any]:
        import stock_db
        return _json_any({"status": "ok", "flows": stock_db.fetch_cash_flows(),
                          "summary": stock_db.fetch_cash_summary()})

    @app.post("/api/real-holdings/cash-flows")
    def create_cash_flow(item: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """记一笔入金/出金。flow_type=deposit|withdraw, amount_rmb>0。"""
        import stock_db
        try:
            fid = stock_db.insert_cash_flow(item)
        except stock_db.LedgerError as e:
            raise HTTPException(400, str(e))
        return _json_any({"status": "ok", "flow_id": fid, "summary": stock_db.fetch_cash_summary()})

    @app.delete("/api/real-holdings/cash-flows/{flow_id}")
    def delete_cash_flow_one(flow_id: int) -> dict[str, Any]:
        import stock_db
        n = stock_db.delete_cash_flow(flow_id)
        if n == 0:
            raise HTTPException(404, f"cash flow not found: {flow_id}")
        return _json_any({"status": "ok", "summary": stock_db.fetch_cash_summary()})

    @app.get("/api/real-holdings/{holding_id}/records")
    def real_holding_records(holding_id: int) -> dict[str, Any]:
        """单只持仓完整买卖时间线（含被合并/清仓的历史轮次）。"""
        import stock_db
        _holding_key_or_404(holding_id)
        recs = stock_db.fetch_real_holding_records(holding_id=holding_id, include_voided=True)
        return _json_any({"status": "ok", "records": recs})

    @app.post("/api/real-holdings/trades/{trade_id}/void")
    def void_real_holding_trade_one(trade_id: int, item: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """纠错：撤销最近一笔 active trade（软删 + rebuild）。非最近一笔返回 409。"""
        import stock_db
        try:
            res = stock_db.void_real_holding_trade(trade_id, reason=(item or {}).get("reason"))
        except stock_db.LedgerNotLatest as e:
            raise HTTPException(409, str(e))
        except stock_db.LedgerError as e:
            raise HTTPException(400, str(e))
        _refresh_holding_review_safe()
        return _json_any({"status": "ok", **res})

    @app.post("/api/real-holdings/rebuild-from-trades")
    def rebuild_real_holdings() -> dict[str, Any]:
        """从交易流水重建聚合缓存（运维 / 迁移后校验用）。"""
        import stock_db
        conn = stock_db.get_db()
        try:
            remap = stock_db.rebuild_real_holdings_from_trades(conn=conn)
            return _json_any({"status": "ok", "rebuilt_keys": len(remap)})
        finally:
            conn.close()

    @app.get("/api/real-holdings/discipline")
    def list_real_holding_discipline(status: str = "active") -> dict[str, Any]:
        """真实持仓纪律计划列表。只读 real_holding_discipline_*，不写推荐池。"""
        import stock_db
        status_filter = None if status == "all" else status
        plans = stock_db.fetch_real_holding_discipline_plans(status=status_filter)
        return _json_any({"status": "ok", "plans": plans})

    @app.get("/api/real-holdings/{holding_id}/discipline")
    def get_real_holding_discipline(holding_id: int) -> dict[str, Any]:
        """查看单个真实持仓的纪律计划与近期触发历史。"""
        import stock_db
        holding = stock_db.fetch_real_holding_by_id(holding_id)
        if not holding:
            raise HTTPException(404, f"real holding id not found: {holding_id}")
        plans = stock_db.fetch_real_holding_discipline_plans(holding_id=holding_id, status=None)
        events = stock_db.fetch_real_holding_discipline_events(holding_id=holding_id, limit=50)
        return _json_any({"status": "ok", "holding": holding, "plans": plans, "events": events})

    @app.post("/api/real-holdings/{holding_id}/discipline")
    def create_real_holding_discipline(
        holding_id: int,
        item: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """为一笔真实持仓创建用户确认的纪律计划。

        这不是交易指令；后端强制 auto_trade_allowed=false。
        """
        import stock_db
        try:
            plan = stock_db.create_real_holding_discipline_plan(
                holding_id,
                item,
                replace_active=bool(item.get("replace_active")),
            )
        except stock_db.DisciplinePlanConflict as e:
            raise HTTPException(409, str(e))
        except stock_db.DisciplinePlanNotFound as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return _json_any({"status": "ok", "plan": plan})

    @app.patch("/api/real-holdings/discipline/{plan_id}")
    def update_real_holding_discipline_status(
        plan_id: str,
        item: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """暂停/归档纪律计划；不改持仓、不下单。"""
        import stock_db
        new_status = str(item.get("status") or "").strip()
        if new_status not in {"active", "paused", "archived"}:
            raise HTTPException(400, "status must be active|paused|archived")
        n = stock_db.update_real_holding_discipline_plan_status(plan_id, new_status)
        if n == 0:
            raise HTTPException(404, f"discipline plan not found: {plan_id}")
        return {"status": "ok", "plan_id": plan_id, "rows_affected": n}

    @app.put("/api/real-holdings/{holding_id}")
    def update_real_holding_one(holding_id: int, item: dict[str, Any] = Body(...)) -> dict[str, Any]:
        import stock_db
        h = stock_db.fetch_real_holding_by_id(holding_id)
        if not h:
            raise HTTPException(404, f"real holding id not found: {holding_id}")
        # 账本持仓：数量/成本由交易流水决定。可改 名称/备注；改 ticker 则连交易流水一起 rename。
        if h.get("close_status"):
            renamed = None
            new_sym = (item.get("symbol") or item.get("code") or "").strip().upper()
            if new_sym and new_sym != (h.get("symbol") or "").upper():
                try:
                    renamed = stock_db.rename_real_holding_symbol(holding_id, new_sym)
                except stock_db.LedgerError as e:
                    raise HTTPException(409, str(e))
            n = stock_db.update_real_holding_meta(holding_id, name=item.get("name"), notes=item.get("notes"))
            return {"status": "ok", "id": holding_id, "rows_affected": n, "ledger_managed": True,
                    "renamed": renamed,
                    "note": "账本持仓的数量/成本由交易流水决定（改数量请用「加仓 / 卖出」）；代码与名称可改。"}
        n = stock_db.update_real_holding(holding_id, item)
        return {"status": "ok", "id": holding_id, "rows_affected": n}

    @app.delete("/api/real-holdings/{holding_id}")
    def delete_real_holding_one(holding_id: int) -> dict[str, Any]:
        import stock_db
        n = stock_db.delete_real_holding(holding_id)
        if n == 0:
            raise HTTPException(404, f"real holding id not found: {holding_id}")
        return {"status": "ok", "id": holding_id, "rows_deleted": n}

    @app.get("/api/premarket-gate")
    def premarket_gate_latest() -> dict[str, Any]:
        """美股盘前风险闸门最新结论 — 今日决策台 / 持仓页顶部横幅读这个单一源。

        由 stock_research.jobs.premarket_gate 在美股开盘前(北京 20:10/20:45/21:15)
        写入 data/latest/premarket_gate.json。前端只渲染颜色 + can_buy + 持仓影响,
        不重算业务规则(feedback_single_source_no_double_engine)。
        缺文件 / 解析失败 → available=False,前端渲染空态(不报错)。
        """
        p = _REPO_ROOT / "data" / "latest" / "premarket_gate.json"
        if not p.exists():
            return {"available": False, "color": "NONE",
                    "reason": "盘前闸门今日尚未生成(美股开盘前才跑)"}
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            doc["available"] = True
            return doc
        except Exception as e:
            return {"available": False, "color": "NONE", "reason": f"读取失败: {e}"}

    @app.get("/api/real-holdings/daily-verdict")
    def real_holdings_daily_verdict() -> dict[str, Any]:
        """真实持仓 7 档判断单一源 — 复用 morning_brief.compute_holdings_verdict 纯函数。

        前端持仓页「💡 今日动作」卡片 + 表格「系统判断」列 + 决策台「持仓体检」小卡
        共用这一个 endpoint，避免前端重算业务规则
        （feedback_single_source_no_double_engine）。
        """
        import stock_db
        from stock_research.jobs.morning_brief import compute_holdings_verdict

        holdings = stock_db.fetch_all_real_holdings()
        # 空持仓 — 返回空结构,前端能渲染空态
        if not holdings:
            return {
                "as_of": "",
                "holdings": [],
                "summary": {"stoploss_breached": 0, "stoploss_watched": 0,
                            "model_weakened": 0, "near_event": 0,
                            "weight_off": 0, "ai_uncovered": 0, "normal": 0,
                            "coverage_ai_portfolio": 0, "coverage_picks_only": 0,
                            "coverage_tracking_only": 0, "coverage_needs_fix": 0},
            }

        # IO 全在 endpoint 内:history / picks / universe / events / target_weights
        def _load_latest_json(rel: str) -> dict:
            p = _REPO_ROOT / rel
            if not p.exists():
                return {}
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}

        history_doc = _load_latest_json("data/latest/history_data.json")
        history = (history_doc.get("tickers") if isinstance(history_doc, dict) else None) or {}

        try:
            picks = stock_db.fetch_latest_recommendation_picks()
        except Exception:
            picks = []
        try:
            universe = stock_db.fetch_universe_for_ai_recommendations()
        except Exception:
            universe = []

        events_data = _load_latest_json("data/event_calendar.json") or {}

        plan = _load_latest_json("data/latest/plan_a_v5.json") or {}
        plan_items = plan.get("plan_v5") or plan.get("plan_v6") or []
        target_weights: dict[str, float] = {}
        for it in plan_items:
            t = it.get("ticker") or it.get("code") or it.get("symbol")
            w = it.get("capped_weight") or it.get("target_weight")
            if t and w is not None:
                try:
                    target_weights[t] = float(w)
                except Exception:
                    pass

        try:
            total_capital = float(stock_db.get_config("total_capital") or 500000)
        except Exception:
            total_capital = 500000.0

        return compute_holdings_verdict(
            holdings,
            history=history,
            picks=picks,
            universe=universe,
            events_data=events_data,
            target_weights=target_weights,
            total_capital=total_capital,
        )

    @app.get("/api/real-holdings/daily-review/latest")
    def real_holdings_daily_review_latest() -> dict[str, Any]:
        """真实持仓每日体检最新结果。

        这是“我的持仓”页的评分/建议单一源。该接口只读落库快照；
        没有快照时返回 missing,不在 GET 请求里临时重算，避免刷新页面产生副作用。
        """
        import stock_db
        payload = stock_db.fetch_latest_real_holding_review()
        if payload is None:
            payload = {
                "status": "missing",
                "run": None,
                "items": [],
                "message": "今日真实持仓体检尚未生成；请等待 daily_refresh 23f 或调用 POST /api/real-holdings/daily-review/run。",
            }
        payload["transient"] = False
        return _json_any(payload)

    @app.post("/api/real-holdings/daily-review/run")
    def real_holdings_daily_review_run() -> dict[str, Any]:
        """手动重算并落库真实持仓每日体检。"""
        from stock_research.jobs.real_holding_review import build_real_holding_review
        return _json_any(build_real_holding_review(persist=True))

    @app.get("/api/real-holdings/equity-curve")
    def real_holdings_equity_curve(days: int = 90) -> dict[str, Any]:
        """账户净值曲线：每日 total_value/total_cost/pnl，按 as_of_date 升序。

        同一天有多次 review run 时取 generated_at 最晚的那次（日终快照）。
        DB 写锁被其他进程占用时返回 status=db_busy，让前端显示"30s 后重试"。
        """
        import stock_db
        import duckdb as _duckdb
        days_clamped = max(7, min(int(days), 730))
        conn = None
        try:
            conn = stock_db.get_db(read_only=True)
            rows = conn.execute(
                """
                WITH latest_run_per_day AS (
                    SELECT review_run_id, as_of_date
                    FROM (
                        SELECT review_run_id, as_of_date, generated_at,
                               ROW_NUMBER() OVER (PARTITION BY as_of_date ORDER BY generated_at DESC) AS rn
                        FROM real_holding_review_runs
                        WHERE status = 'generated'
                          AND as_of_date >= CURRENT_DATE - INTERVAL (? * 1) DAY
                    )
                    WHERE rn = 1
                )
                SELECT r.as_of_date,
                       SUM(i.current_value_rmb) AS total_value_rmb,
                       SUM(i.cost_rmb_locked)   AS total_cost_rmb,
                       COUNT(*)                 AS holding_count
                FROM real_holding_review_items i
                JOIN latest_run_per_day r ON i.review_run_id = r.review_run_id
                GROUP BY r.as_of_date
                ORDER BY r.as_of_date ASC
                """,
                [days_clamped],
            ).fetchall()
        except _duckdb.IOException as exc:
            # DuckDB 文件级写锁冲突: 不抛 500,前端可以等下次轮询自愈
            if "lock" in str(exc).lower():
                return _json_any({
                    "days": days_clamped,
                    "point_count": 0,
                    "points": [],
                    "status": "db_busy",
                    "message": "DuckDB 写锁被其他进程占用，请稍后刷新",
                })
            raise
        finally:
            if conn is not None:
                conn.close()

        points = []
        for as_of_date, total_value, total_cost, holding_count in rows:
            tv = float(total_value or 0.0)
            tc = float(total_cost or 0.0)
            pnl = tv - tc
            pnl_pct = (pnl / tc * 100.0) if tc > 0 else None
            points.append({
                "as_of_date": as_of_date.isoformat() if hasattr(as_of_date, "isoformat") else str(as_of_date),
                "total_value_rmb": round(tv, 2),
                "total_cost_rmb": round(tc, 2),
                "pnl_rmb": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 4) if pnl_pct is not None else None,
                "holding_count": int(holding_count or 0),
            })
        return _json_any({
            "days": days_clamped,
            "point_count": len(points),
            "points": points,
        })

    @app.get("/api/real-holdings/daily-review/history")
    def real_holdings_daily_review_history(
        symbol: str | None = None,
        days: int = 14,
    ) -> dict[str, Any]:
        """真实持仓体检历史轨迹 (按日期升序)。

        无 symbol → 返回近 N 日所有持仓的轨迹 dict {symbol: [snapshots...]}；
        带 symbol → 只返回这一只的轨迹。dashboard 用前者批量拉,详情页用后者。
        """
        import stock_db
        days_clamped = max(1, min(int(days), 365))
        symbols = [symbol] if symbol else None
        history = stock_db.fetch_real_holding_review_history(symbols=symbols, days=days_clamped)
        return _json_any({
            "days": days_clamped,
            "symbol": symbol,
            "history": history,
        })

    @app.get("/api/model-sim-holdings")
    def list_model_sim_holdings() -> list[dict[str, Any]]:
        import stock_db
        return _json_dates(stock_db.fetch_all_model_sim_holdings())

    @app.post("/api/model-sim-holdings/bulk-replace")
    def bulk_replace_model_sim_holdings_endpoint(items: list[dict[str, Any]] = Body(...)) -> dict[str, Any]:
        import stock_db
        n = stock_db.bulk_replace_model_sim_holdings(items)
        return {"status": "ok", "rows_inserted": n}

    @app.delete("/api/model-sim-holdings/{holding_id}")
    def delete_model_sim_holding_one(holding_id: int) -> dict[str, Any]:
        import stock_db
        n = stock_db.delete_model_sim_holding(holding_id)
        if n == 0:
            raise HTTPException(404, f"model sim holding id not found: {holding_id}")
        return {"status": "ok", "id": holding_id, "rows_deleted": n}

    # ────────── legacy holdings（V2 split 后停用，避免真实/模拟再次混表） ──────────
    @app.get("/api/holdings")
    def list_holdings() -> list[dict[str, Any]]:
        raise HTTPException(410, "deprecated: use /api/real-holdings or /api/model-sim-holdings")

    @app.post("/api/holdings")
    def create_holding(item: dict[str, Any] = Body(...)) -> dict[str, Any]:
        raise HTTPException(410, "deprecated: write real holdings to /api/real-holdings; model simulations to /api/model-sim-holdings/bulk-replace")

    @app.put("/api/holdings/{holding_id}")
    def update_holding_one(holding_id: int, item: dict[str, Any] = Body(...)) -> dict[str, Any]:
        raise HTTPException(410, "deprecated: use /api/real-holdings")

    @app.delete("/api/holdings/{holding_id}")
    def delete_holding_one(holding_id: int) -> dict[str, Any]:
        raise HTTPException(410, "deprecated: use /api/real-holdings or /api/model-sim-holdings")

    @app.post("/api/holdings/bulk-replace")
    def bulk_replace_holdings_endpoint(items: list[dict[str, Any]] = Body(...)) -> dict[str, Any]:
        raise HTTPException(410, "deprecated: use /api/model-sim-holdings/bulk-replace")

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

    @app.get("/api/ipo/radar")
    def get_ipo_radar() -> dict[str, Any]:
        """返回最新 junior_stock_radar.json（IPO 日历 + 解禁雷达 + 次新股池）。

        前端 dashboard 重新渲染 IPO tab 用：refresh-ipo 触发重算后，
        前端 fetch 这个接口拿到新数据再替换 JUNIOR_RADAR。
        """
        p = _REPO_ROOT / "data" / "latest" / "junior_stock_radar.json"
        if not p.exists():
            return {"error": "junior_stock_radar.json 不存在；请先 POST /api/jobs/refresh-ipo"}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            return {"error": f"读取失败: {e}"}

    @app.post("/api/jobs/refresh-ipo")
    def trigger_ipo_refresh(background: BackgroundTasks):
        """触发 IPO 日历 + 次新股雷达刷新（异步，串行：先 ipo_daily 后 junior_stock_watcher）。

        junior_stock_watcher 读取 ipo_daily 生成的 ipo_calendar.json，
        所以必须串行不能并行。整体 ~15s。
        """
        def _run_both():
            from ..jobs import ipo_daily, junior_stock_watcher
            try:
                ipo_daily.main()
            except Exception as e:
                logger.warning("refresh-ipo: ipo_daily 失败: %s", e)
            try:
                junior_stock_watcher.main()
            except Exception as e:
                logger.warning("refresh-ipo: junior_stock_watcher 失败: %s", e)
        background.add_task(_run_both)
        return {"status": "queued", "job": "refresh_ipo", "steps": ["ipo_daily", "junior_stock_watcher"]}

    return app


# 模块级 app 对象供 uvicorn 加载
app = create_app() if FastAPI is not None else None
