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
        conn = duckdb.connect(stock_db.DB_PATH, read_only=True)
        try:
            tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
            is_v2 = "system_universe" in tables and "price_daily" in tables
            enrichment_by_code: dict[str, dict[str, Any]] = {}
            if is_v2:
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
            else:
                pool_meta = _tech_pool_meta()
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
                picks_by_code = {}
                for r in latest_picks_q:
                    row = dict(zip(pick_cols, r))
                    code = str(row.get("code") or "")
                    if code:
                        picks_by_code[code] = row
                        picks_by_code.setdefault(code.split(".")[0], row)

                earnings_fetched = {
                    code: dt for code, dt in conn.execute(
                        "SELECT code, MAX(fetched_at) FROM earnings_history GROUP BY code"
                    ).fetchall()
                }

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
                    merged["pick_val_score"] = fs.get("valuation")
                    merged["pick_trend_score"] = fs.get("momentum")
                    merged["pick_cred_score"] = fs.get("data_quality")
                    merged["pick_coverage_score"] = fs.get("coverage")
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
            # V2 兜底：当 V1 表都空时（clean v2 后只有 system_universe / price_daily / recommendation_picks），
            # 把 V2 数据按 V1 字段名映射回来，保持响应 shape 不变。
            if not prices:
                v2_prices = _rows(
                    "SELECT market, symbol AS code, trade_date AS date, close AS price, "
                    "prev_close, currency, market_cap, forward_pe, trailing_pe, peg_ratio, "
                    "ytd_pct, one_week_pct, one_month_pct, one_year_pct, source, fetched_at "
                    "FROM price_daily WHERE symbol = ? ORDER BY trade_date DESC, fetched_at DESC"
                )
                prices = v2_prices
            if not picks:
                v2_picks = _rows(
                    "SELECT rp.symbol AS code, rp.name, rp.rank, rp.rating, rp.signal, "
                    "rp.total_score, rp.factor_scores_json, rp.recommendation_reason, "
                    "rp.entry_price, rp.entry_currency, rp.universe_scope, "
                    "rr.run_date AS pick_date, rr.generated_at "
                    "FROM recommendation_picks rp JOIN recommendation_runs rr ON rp.run_id = rr.run_id "
                    "WHERE rp.symbol = ? ORDER BY rr.generated_at DESC"
                )
                picks = v2_picks
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
