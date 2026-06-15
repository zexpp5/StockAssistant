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

# ───── 盘前闸门战绩渲染（详情页 + 历史二级页共用，避免两份口径漂移）─────
_PM_DOT = {"CRITICAL": "🔴", "HIGH": "🟠", "LOW": "🟡", "NONE": "🟢"}
_PM_CN_COLOR = {"CRITICAL": "红色预警", "HIGH": "橙色预警", "LOW": "黄色提醒", "NONE": "绿色（没警）"}
_PM_OC = {"TRUE_POSITIVE": ("✅", "真预警"), "FALSE_ALARM": ("🟡", "虚惊"),
          "MISS": ("❌", "漏报"), "TRUE_NEGATIVE": ("·", "正常")}


def _pm_esc(s) -> str:
    return str("" if s is None else s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pm_rec_time(r: dict) -> str:
    """报到当前档位的那次扫描时间（缺则取最后一次）。"""
    scans = r.get("scans") or []
    color = r.get("color")
    for s in scans:
        if s.get("color") == color:
            return s.get("at", "")
    return scans[-1].get("at", "") if scans else ""


def _pm_us_close_beijing(as_of_iso: str):
    """as_of 那个美股交易日的收盘(16:00 ET)对应的北京时间（naive）。"""
    try:
        from zoneinfo import ZoneInfo
        from datetime import date as _date, datetime as _dt
        d = _date.fromisoformat(as_of_iso)
        c = _dt(d.year, d.month, d.day, 16, 0, tzinfo=ZoneInfo("America/New_York"))
        return c.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    except Exception:
        return None


def _pm_is_stale(doc: dict, now=None) -> bool:
    """时效保险丝：预警只对当场美股交易日有效，过了该日收盘即过期。

    无 as_of / 非交易日 / 算不出收盘时间 → 视为过期(宁可保守)。
    """
    from datetime import date as _date, datetime as _dt
    from stock_research.core import premarket_gate as _pg
    now = now or _dt.now()
    as_of = doc.get("as_of")
    if not as_of:
        return True
    try:
        d = _date.fromisoformat(str(as_of))
    except Exception:
        return True
    if not _pg.is_us_trading_day(d):
        return True
    close_bj = _pm_us_close_beijing(as_of)
    if close_bj is None:
        return True
    return now > close_bj


def _pm_load_history(settle: bool = True) -> list[dict]:
    """Load premarket history and opportunistically settle expired rows."""
    from stock_research.core import premarket_gate as _pg

    p = _REPO_ROOT / "data" / "premarket_gate_history.json"
    records: list[dict] = []
    if p.exists():
        try:
            records = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            records = []
    if settle:
        try:
            records, changed = _pg.settle_history_records(records)
            if changed:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("premarket history settle failed: %s", e)
    return records


def _pm_evidence_html(doc: dict, wrap: bool = True) -> str:
    """Render the full premarket signal snapshot behind the headline."""
    families = [f for f in (doc.get("families") or []) if isinstance(f, dict)]
    if not families:
        return ""

    def tone(stress, available=True) -> tuple[str, str, str]:
        if not available:
            return "⚪", "#94a3b8", "缺数据"
        try:
            s = float(stress or 0)
        except Exception:
            s = 0.0
        if s >= 2.0:
            return "🔴", "#b91c1c", f"{s:.1f}/3"
        if s >= 1.0:
            return "🟠", "#c2410c", f"{s:.1f}/3"
        if s > 0:
            return "🟡", "#a16207", f"{s:.1f}/3"
        return "🟢", "#16a34a", "0/3"

    rows = []
    for f in families:
        dot, color, score = tone(f.get("stress"), bool(f.get("available", True)))
        plain = str(f.get("plain") or "")
        tags = set(f.get("tags") or [])
        data = f.get("data") if isinstance(f.get("data"), dict) else {}
        pcr = data.get("pcr_volume")
        if f.get("key") == "vol" and "pcr_bearish" in tags and "PCR" not in plain and pcr is not None:
            try:
                pcr_txt = f"{float(pcr):.2f}"
            except Exception:
                pcr_txt = str(pcr)
            if plain.endswith(("。", "！", "？")):
                plain = plain[:-1]
            plain += f"；SPY 期权 Put/Call 比 PCR {pcr_txt} 偏防守，说明有资金在买保护，作为轻微留意。"
        rows.append(
            '<div class="ev-row">'
            f'<div class="ev-label" style="color:{color}">{dot} {_pm_esc(f.get("label") or f.get("key") or "")}'
            f'<span>{_pm_esc(score)}</span></div>'
            '<div>'
            f'<div class="ev-head">{_pm_esc(f.get("headline") or "—")}</div>'
            f'<div class="ev-plain">{_pm_esc(plain)}</div>'
            '</div></div>'
        )
    comp = doc.get("composite", 0)
    cov = doc.get("coverage", 0)
    try:
        comp_txt = f"{float(comp):.3g}/3"
    except Exception:
        comp_txt = f"{_pm_esc(comp)}/3"
    try:
        cov_txt = f"{float(cov) * 100:.0f}%"
    except Exception:
        cov_txt = _pm_esc(cov)
    inner = (
        '<div class="pm-section"><h3>依据快照 '
        '<span class="muted" style="font-weight:400;font-size:13px">— 8 类信号实际读数</span></h3>'
        f'<div class="ev-meta">综合压力 {comp_txt} · 数据覆盖率 {cov_txt}</div>'
        + "".join(rows)
        + '</div>'
    )
    return f'<div class="card">{inner}</div>' if wrap else inner


def _pm_summary_html(summ: dict) -> str:
    """战绩汇总卡：统计格 + 样本量提示 + 颜色分档 + 基准对照。"""
    sd = summ.get("settled_days", 0)
    enough = summ.get("enough_sample", False)
    pc = lambda v: f"{v}%" if v is not None else "—"
    if sd <= 0:
        return ('<div class="muted" style="font-size:13px">还没有可验证的历史——从今天起每天记一笔，'
                '第二天用当天真实涨跌核对，攒几天就能看命中率了。</div>')
    note = ""
    if not enough:
        note = ('<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;'
                'padding:8px 10px;font-size:12.5px;color:#92400e;margin-bottom:10px">'
                f'⚠️ 样本不足（{sd}/{summ.get("min_sample", 20)} 个交易日），以下数字仅供观察、'
                '还不具统计意义——攒够再看才靠谱。</div>')
    stat = (
        f'<div class="stat"><b>{sd}</b><span>已验证</span></div>'
        f'<div class="stat"><b>{summ.get("warnings_issued", 0)}</b><span>发过警报</span></div>'
        f'<div class="stat"><b>{pc(summ.get("precision_pct"))}</b><span>警报命中</span></div>'
        f'<div class="stat"><b style="color:#dc2626">{summ.get("miss", 0)}</b><span>漏报</span></div>'
    )
    cb = summ.get("color_buckets") or {}
    rows_b = ""
    for k in ("CRITICAL", "HIGH", "LOW", "NONE"):
        b = cb.get(k) or {}
        if not b.get("n"):
            continue
        avg = b.get("avg_return")
        br = b.get("bad_rate")
        avg_txt = ("—" if avg is None else (f"跌 {abs(avg):.1f}%" if avg < 0 else f"涨 {avg:.1f}%"))
        rows_b += ('<div style="display:flex;justify-content:space-between;font-size:12.5px;'
                   'padding:4px 0;border-bottom:1px solid #f5f7fa">'
                   f'<span>{_PM_DOT.get(k, "")} {_PM_CN_COLOR.get(k, k)} <span class="muted">({b["n"]}天)</span></span>'
                   f'<span>事后平均<b>{avg_txt}</b>'
                   f'{f"，其中真跌 {br}%" if br is not None else ""}</span></div>')
    buckets = ('<div style="margin-top:12px"><div style="font-size:13px;font-weight:600;margin-bottom:4px">'
               '① 不同颜色，事后真有差别吗？<span class="muted" style="font-weight:400">'
               '（闸门有用→红色那天该比绿色那天跌得多）</span></div>'
               + rows_b + '</div>') if rows_b else ""

    # ② 跟「偷懒办法」PK —— 样本不够就只讲它以后要干嘛，不摆看不懂的百分比
    pk_head = ('<div style="margin-top:12px"><div style="font-size:13px;font-weight:600;margin-bottom:4px">'
               '② 它比「偷懒办法」强吗？<span class="muted" style="font-weight:400">'
               '（同样的日子里比，看谁更会提前喊对「要跌」）</span></div>')
    if enough:
        bv = summ.get("baseline_vix_only")
        bn = summ.get("baseline_never_warn") or {}

        def _bl_row(name, p, r, show_p=True):
            right = (f"喊对 {pc(p)} · " if show_p else "") + f"真跌抓到 {pc(r)}"
            return ('<div style="display:flex;justify-content:space-between;font-size:12.5px;'
                    f'padding:4px 0;border-bottom:1px solid #f5f7fa"><span>{name}</span><span>{right}</span></div>')

        bl = ('<div class="muted" style="font-size:11.5px;margin-bottom:4px">'
              '（「喊对」=喊要跌、结果真跌的比例；「真跌抓到」=真正跌的日子有几成被提前喊到）</div>')
        bl += _bl_row('🚦 <b>本闸门</b>（综合看 7 样）', summ.get("precision_pct"), summ.get("recall_pct"))
        if bv:
            bl += _bl_row('😴 只看一个 VIX', bv.get("precision_pct"), bv.get("recall_pct"))
        bl += _bl_row('🙈 从不预警（鸵鸟）', None, bn.get("recall_pct"), show_p=False)
        baseline = pk_head + bl + '</div>'
    else:
        baseline = pk_head + (
            '<div style="font-size:12.5px;color:#475569;line-height:1.8">'
            '等攒够交易日，这里会让三种办法在<b>同样的日子</b>比一比：<br>'
            '• 🚦 <b>本闸门</b>：综合看 7 样东西（期货 / 利率 / 恐慌指数 / 巨头 / 板块 / 海外 / 宏观）<br>'
            '• 😴 <b>只看一个恐慌指数 VIX</b>：最省事的笨办法<br>'
            '• 🙈 <b>从不预警</b>：假装没事的鸵鸟<br>'
            '看本闸门是不是<b>明显更会提前喊对「要跌」、又少瞎喊</b>。现在样本太少，先不下结论。</div>'
        ) + '</div>'

    # ③ 顺风准不准：说「顺风」的那些天，事后真涨了吗（与防守对称）
    tw = summ.get("tailwind") or {}
    tw_head = ('<div style="margin-top:12px"><div style="font-size:13px;font-weight:600;margin-bottom:4px">'
               '③ 「顺风」准不准？<span class="muted" style="font-weight:400">'
               '（说顺风的那些天，事后真涨了吗）</span></div>')
    if tw.get("n"):
        avg = tw.get("avg_return")
        avg_txt = ("—" if avg is None else (f"涨 {avg:.1f}%" if avg >= 0 else f"跌 {abs(avg):.1f}%"))
        back = tw.get("backfired", 0)
        tailwind_html = tw_head + (
            '<div style="font-size:12.5px;color:#475569;padding:2px 0">'
            f'🟢 顺风 <b>{tw["n"]}</b> 天：事后平均<b>{avg_txt}</b>，其中 {tw.get("rose", 0)} 天真涨'
            + (f'；<span style="color:#dc2626">⚠️ {back} 天反而大跌（打脸）</span>' if back else '')
            + '</div></div>'
        )
    else:
        tailwind_html = tw_head + ('<div class="muted" style="font-size:12.5px">还没碰上「顺风」日，'
                                   '碰上了会单独记账——看它说"顺风"的时候是不是真涨。</div></div>')

    dim = 'opacity:.55' if not enough else ''
    return note + f'<div style="{dim}"><div class="stats">{stat}</div>{buckets}{baseline}{tailwind_html}</div>'


def _pm_history_list_html(records: list) -> str:
    """历史台账可展开列表：每条 = 日期·时间 | 预警 | 结果 | 事后涨跌，点开看当晚理由。"""
    if not records:
        return '<div class="muted" style="font-size:13px;padding:10px 0">暂无历史记录。</div>'
    head = ('<div class="hl-head"><span>日期 · 时间</span><span>预警</span>'
            '<span>结果</span><span>事后涨跌</span></div>')
    rows = ""
    for r in records:
        d = _pm_esc(r.get("date", ""))
        tm = _pm_esc(_pm_rec_time(r))
        color = r.get("color", "NONE")
        oc = r.get("outcome")
        act = r.get("actual", {}) or {}
        spv, nqv = act.get("spy_pct"), act.get("nq_pct")
        move = " ".join(t for t in [f'标普{spv:+.1f}%' if spv is not None else "",
                                    f'纳指{nqv:+.1f}%' if nqv is not None else ""] if t) or "—"
        if oc:
            ic, name = _PM_OC.get(oc, ("", oc))
            res = f'{ic} {name}'
        else:
            res = '<span class="muted">待验证</span>'
        said = _pm_esc(r.get("headline_plain") or r.get("can_buy", ""))
        top = _pm_esc(r.get("top_alarm", ""))
        reasons = r.get("reasons_plain") or []
        rli = "".join(f'<li>{_pm_esc(x)}</li>' for x in reasons)
        detail = (f'<div class="hl-said">当晚结论：{said}</div>'
                  + (f'<div class="hl-top">{top}</div>' if top else '')
                  + (f'<ul class="rlist">{rli}</ul>' if rli else ''))
        rows += (
            '<details class="hl"><summary class="hl-row">'
            f'<span>{_PM_DOT.get(color, "")} <b>{d}</b> <span class="htime">{tm}</span></span>'
            f'<span>{_pm_esc(_PM_CN_COLOR.get(color, color))}</span><span>{res}</span><span class="hl-move">{move}</span>'
            '</summary>'
            f'<div class="hl-detail">{detail}</div></details>'
        )
    return head + rows


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
    @app.get("/")
    def home_dashboard():
        """系统首页 = dashboard。也用 http 提供一份，让盘前各页能「返回首页」
        （http 页面无法直接跳本地 file://，统一到同一 http 源即可互相跳转）。"""
        from fastapi.responses import HTMLResponse
        p = _REPO_ROOT / "stock_dashboard.html"
        if not p.exists():
            return HTMLResponse('<div style="font-family:sans-serif;padding:40px">'
                                '<h2>📊 首页 dashboard 尚未生成</h2>'
                                '<p>跑 build_stock_dashboard_html.py 生成后即可。</p>'
                                '<p><a href="/premarket">→ 先看盘前预警</a></p></div>')
        return HTMLResponse(p.read_text(encoding="utf-8"))

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
        """读 V2 manual_watchlist 全表（用户在 dashboard 手动加的自选股）。

        2026-06-11: 合并 fetch_manual_watchlist_enriched 的价格/动量字段
        （latest_price/ytd_pct/one_month_pct/…）——池外票（ETF/非科技）不在
        RECORDS 里，自选页此前对它们只能显示「—」。
        """
        import stock_db
        rows = stock_db.fetch_manual_watchlist()
        price_keys = (
            "latest_price", "ytd_pct", "one_year_pct", "one_month_pct",
            "one_week_pct", "forward_pe", "peg", "currency", "market_cap",
        )
        try:
            enriched = {
                str(e.get("code") or "").upper(): e
                for e in stock_db.fetch_manual_watchlist_enriched()
            }
        except Exception:
            enriched = {}
        for r in rows:
            e = enriched.get(str(r.get("symbol") or "").upper()) or {}
            for k in price_keys:
                if r.get(k) is None and e.get(k) is not None:
                    r[k] = e[k]
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

    @app.get("/api/short-crowding/{symbol}")
    def get_short_crowding(symbol: str) -> dict[str, Any]:
        """空头拥挤度提示灯（display-only · 绝不进打分）。单只查询，缓存优先（20h TTL），
        只对美股打网络；港股/A 股返回 not_applicable。供个股详情页(AI 推荐/买前研究都跳这里)懒加载。"""
        try:
            import short_interest
            out = short_interest.resolve_short_crowding([symbol])
            return out.get(symbol.upper()) or out.get(symbol) or {
                "level": "未知", "note": "无数据", "reasons": [],
            }
        except Exception as e:
            return {"level": "未知", "note": f"短仓数据获取失败：{str(e)[:80]}", "reasons": []}

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
        # 没有 active 纪律计划的仓位按固定规则补一份模板草稿（保本锁公式三线），
        # 保证新仓位从第一天起就有人盯线；已有计划则跳过。fail-soft。
        draft_info = _ensure_discipline_draft_safe(res.get("holding_id"))
        # 新建仓后用缓存行情立即重算持仓体检，让新标的当场进入「今日持仓体检」列表，
        # 不必等下一次拉行情/定时任务（与 /add /close /undo 行为对齐）。fail-soft。
        _refresh_holding_review_safe()
        new_h = stock_db.fetch_real_holding_by_id(res["holding_id"]) if res.get("holding_id") else None
        return _json_any({"status": "ok", "holding": new_h, "sync": sync_info,
                          "discipline_draft": draft_info, **res})

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

    def _ensure_discipline_draft_safe(holding_id) -> dict[str, Any] | None:
        """买入/加仓后给无计划的持仓补模板草稿纪律计划。草稿生成失败不阻断交易记录。"""
        import stock_db
        if not holding_id:
            return None
        try:
            plan = stock_db.ensure_discipline_template_draft(int(holding_id))
        except Exception as e:
            logger.warning("discipline template draft failed (non-fatal): %s", e)
            return None
        if not plan:
            return None
        return {"plan_id": plan.get("plan_id"), "source_type": plan.get("source_type"),
                "validation_status": plan.get("validation_status")}

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
        draft_info = _ensure_discipline_draft_safe(res.get("holding_id") or holding_id)
        _refresh_holding_review_safe()
        new_h = stock_db.fetch_real_holding_by_id(res.get("holding_id") or holding_id)
        return _json_any({"status": "ok", "holding": new_h, "discipline_draft": draft_info, **res})

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
        # 账本持仓：数量由交易流水决定。可改 名称/备注；改 ticker 则连交易流水一起 rename；
        # 录错的成本价可在「单一买入、未卖出」时纠正（改那唯一一笔买入成交后 rebuild）。
        if h.get("close_status"):
            renamed = None
            new_sym = (item.get("symbol") or item.get("code") or "").strip().upper()
            if new_sym and new_sym != (h.get("symbol") or "").upper():
                try:
                    renamed = stock_db.rename_real_holding_symbol(holding_id, new_sym)
                except stock_db.LedgerError as e:
                    raise HTTPException(409, str(e))
            price_corrected = None
            new_price = item.get("entry_price")
            if new_price is not None:
                try:
                    cur_price = float(h.get("entry_price") or 0)
                    np = float(new_price)
                except (TypeError, ValueError):
                    np, cur_price = None, 0.0
                if np is not None and abs(np - cur_price) > 1e-9:
                    try:
                        price_corrected = stock_db.correct_real_holding_buy_price(holding_id, np)
                    except stock_db.LedgerError as e:
                        raise HTTPException(409, str(e))
            n = stock_db.update_real_holding_meta(holding_id, name=item.get("name"), notes=item.get("notes"))
            return {"status": "ok", "id": holding_id, "rows_affected": n, "ledger_managed": True,
                    "renamed": renamed, "price_corrected": price_corrected,
                    "note": "账本持仓的数量由交易流水决定（改数量请用「加仓 / 卖出」）；代码、名称、未卖出的成本价可改。"}
        n = stock_db.update_real_holding(holding_id, item)
        return {"status": "ok", "id": holding_id, "rows_affected": n}

    @app.delete("/api/real-holdings/{holding_id}")
    def delete_real_holding_one(holding_id: int) -> dict[str, Any]:
        import stock_db
        n = stock_db.delete_real_holding(holding_id)
        if n == 0:
            raise HTTPException(404, f"real holding id not found: {holding_id}")
        return {"status": "ok", "id": holding_id, "rows_deleted": n}

    @app.get("/api/bottleneck-reviews")
    def bottleneck_reviews() -> dict[str, Any]:
        """瓶颈信号红绿灯 — 7 个领先信号的季度复查记录 + 聚合判定。

        dashboard「催化信号验证」页读这个单一源；判定规则在
        core/bottleneck_signals.aggregate_group 算好，前端只渲染
        (feedback_single_source_no_double_engine)。
        """
        from stock_research.core import bottleneck_signals as _bs

        try:
            return _bs.build_payload()
        except Exception as e:
            return {"available": False, "reason": f"读取失败: {e}"}

    @app.post("/api/bottleneck-review")
    def bottleneck_review_save(item: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """回填一条瓶颈信号复查结论（按 ticker+quarter upsert）。

        body: {ticker, quarter: "2026Q2", conclusion: 转强/持平/转弱,
               evidence_tier?: A/B/C, url?, note?}
        """
        from stock_research.core import bottleneck_signals as _bs

        try:
            record = _bs.save_review(
                ticker=str(item.get("ticker", "")),
                quarter=str(item.get("quarter", "")),
                conclusion=str(item.get("conclusion", "")),
                evidence_tier=str(item.get("evidence_tier", "") or ""),
                url=str(item.get("url", "") or ""),
                note=str(item.get("note", "") or ""),
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"status": "ok", "record": record, "payload": _bs.build_payload()}

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
            # 时效保险丝：过了当场美股交易日收盘即过期，前端据此不再当有效红灯
            doc["stale"] = _pm_is_stale(doc)
            vu = _pm_us_close_beijing(doc.get("as_of", ""))
            doc["valid_until"] = vu.isoformat(timespec="minutes") if vu else None
            return doc
        except Exception as e:
            return {"available": False, "color": "NONE", "reason": f"读取失败: {e}"}

    @app.get("/api/premarket-gate/history")
    def premarket_gate_history() -> dict[str, Any]:
        """盘前闸门历史台账 + 战绩汇总 — 回溯"预警准不准"的自我分析。

        每天一条(color/composite/原因),第二天用真实涨跌结算对错。
        前端「战绩回溯」读这个,验证这套预警的命中率/漏报率。
        """
        from stock_research.core import premarket_gate as _pg

        records = _pm_load_history(settle=True)
        records = sorted(records, key=lambda r: r.get("date", ""), reverse=True)
        return {
            "records": records,
            "summary": _pg.summarize_history(records),
            "outcome_cn": _pg.OUTCOME_CN,
        }

    @app.get("/premarket")
    def premarket_gate_page():
        """美股盘前风险闸门 — 独立网页（白底干净，手机也能看）。

        服务端直接读 data/latest/premarket_gate.json 渲染，5 分钟自动刷新。
        独立页面是过渡方案:dashboard 主生成器正被并行会话改写,先用这个让界面
        能看到;等那边落地再把横幅嵌进今日决策台顶部。
        """
        from fastapi.responses import HTMLResponse
        from stock_research.core import premarket_gate as _pg

        p = _REPO_ROOT / "data" / "latest" / "premarket_gate.json"
        doc: dict = {}
        if p.exists():
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                doc = {}

        # 历史台账 + 战绩
        hist = _pm_load_history(settle=True)
        hist_sorted = sorted(hist, key=lambda r: r.get("date", ""), reverse=True)
        summ = _pg.summarize_history(hist)

        THEME = {
            "CRITICAL": ("#dc2626", "#fef2f2", "#fecaca"),
            "HIGH":     ("#ea580c", "#fff7ed", "#fed7aa"),
            "LOW":      ("#ca8a04", "#fefce8", "#fde68a"),
            "NONE":     ("#16a34a", "#f0fdf4", "#bbf7d0"),
        }

        def esc(s: str) -> str:
            return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

        if not doc or not doc.get("families"):
            body = (
                '<div class="card"><div class="muted" style="padding:40px 0;text-align:center">'
                '盘前闸门今日尚未生成 —— 它在美股开盘前（北京约 20:10 / 20:45 / 21:15）才跑。'
                '<br>周末 / 美股假日休市不跑。</div></div>'
            )
            color = "NONE"
            foot_html = ('<div class="foot">📖 这是「美股开盘前的看天气」：开盘前帮你看今晚适不适合买。'
                         '🟢正常买 🟡小仓试 🟠先别开新仓 🔴别买只看好已有 · ⚠️ 仅供参考，不是投资建议</div>')
        else:
            stale = _pm_is_stale(doc)
            if stale:
                gen = esc(doc.get("generated_at", ""))[:16].replace("T", " ")
                scan = esc(doc.get("scan_label", ""))
                when_html = (
                    f'<div class="when">上一场时间 {gen}' + (f" · {scan}" if scan else "") + "</div>"
                    if gen else ""
                )
                body = f"""
                <div style="background:#f1f5f9;border:1px solid #cbd5e1;border-radius:10px;
                  padding:10px 14px;margin-bottom:12px;font-size:13px;color:#475569">
                  ⏸ 这是<b>上一场预警，已过期</b>（美股那场已收盘）。上一场已归入历史记录；
                  今晚新预警生成前，这里不再展示旧红灯或旧判断。
                </div>
                <div class="card">
                  <h3>当前没有有效盘前预警</h3>
                  {when_html}
                  <div class="muted" style="font-size:14px;line-height:1.8">
                    等今晚盘前扫描生成后，会自动替换为新的当场预警。上一场记录请从下方历史查看。
                  </div>
                </div>
                """
                foot_html = (
                    '<div class="foot">📖 这是「美股开盘前的看天气」：开盘前帮你看一眼今晚适不适合买。'
                    '🟢正常买 🟡小仓试 🟠先别开新仓 🔴别买只看好已有 · ⚠️ 仅供参考，不是投资建议</div>'
                )
            else:
                color = doc.get("color", "NONE")
                accent, bg, border = THEME.get(color, THEME["NONE"])
                head = esc(doc.get("headline_plain", ""))
                can_buy = esc(doc.get("can_buy", ""))
                top = doc.get("top_alarm", "")
                top_html = (f'<div class="alarm">{esc(top)}</div>') if top else ""
                reasons = doc.get("reasons_plain", [])
                reasons_html = "".join(f'<li>{esc(r)}</li>' for r in reasons) or '<li class="muted">各项平稳</li>'
                evidence_html = _pm_evidence_html(doc, wrap=False)
                hold = doc.get("holdings_impact", [])
                hold_html = ""
                if hold:
                    items = "".join(
                        f'<li><b>{esc(h.get("symbol",""))}</b>：{esc(h.get("reason",""))}</li>'
                        for h in hold
                    )
                    hold_html = (
                        '<div class="card"><h3>💼 对你持仓的影响 '
                        '<span class="muted" style="font-weight:400;font-size:13px">'
                        '（只是提醒，不是叫你一定买卖）</span></h3>'
                        f'<ul>{items}</ul></div>'
                    )
                srcs = "、".join(doc.get("pressure_sources", []))
                srcs_html = f'<div class="srcs">压力源：{esc(srcs)}</div>' if srcs else ""
                comp = doc.get("composite", 0)
                gen = esc(doc.get("generated_at", ""))[:16].replace("T", " ")
                scan = esc(doc.get("scan_label", ""))
                cov = doc.get("coverage", 1)
                when_html = (f'<div class="when">⏱ 预警时间 {gen} · {scan}</div>'
                             if gen else "")
                body = f"""
                <div class="card pm-current">
                  <div class="hero pm-hero" style="background:{bg};border-color:{border}">
                    <div class="verdict" style="color:{accent}">{head}</div>
                    {when_html}
                    <div class="cb"><b>该怎么做：</b>{can_buy}</div>
                    {srcs_html}
                  </div>
                  {top_html}
                  <div class="pm-section"><h3>为什么这么判断</h3><ul class="reasons">{reasons_html}</ul></div>
                  {evidence_html}
                </div>
                {hold_html}
                """
                foot_html = (
                    '<div class="foot">📖 这是「美股开盘前的看天气」：开盘前帮你看一眼今晚适不适合买。'
                    '🟢正常买 🟡小仓试 🟠先别开新仓 🔴别买只看好已有 · '
                    f'风险打分 {comp:.1f}/3（越高越危险）· 覆盖率 {int(cov*100)}% · '
                    f'生成 {gen}（{scan}）· ⚠️ 仅供参考，不是投资建议</div>'
                )

        # ── 战绩回溯板块：汇总卡 + 跳「全部历史」二级列表页的按钮 ──
        history_html = (
            '<div class="card"><h3>📊 战绩回溯 '
            '<span class="muted" style="font-weight:400;font-size:13px">— 这套预警事后到底准不准</span></h3>'
            + _pm_summary_html(summ)
            + '<a class="hist-btn" href="/premarket/history">📜 查看全部历史记录（列表）›</a>'
            + '</div>'
        )

        html = f"""<!DOCTYPE html><html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>美股盘前 · 今晚能不能买</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f8fafc;color:#0f172a;
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
  line-height:1.6;padding:16px}}
.wrap{{max-width:680px;margin:0 auto}}
.title{{font-size:15px;color:#64748b;margin:0 0 12px;display:flex;justify-content:space-between;align-items:center}}
.hero{{border:1px solid;border-radius:16px;padding:20px 22px;margin-bottom:14px}}
.verdict{{font-size:22px;font-weight:800;letter-spacing:.3px}}
.when{{margin-top:6px;font-size:12.5px;color:#94a3b8}}
.cb{{margin-top:10px;font-size:15px}}
.srcs{{margin-top:8px;font-size:13px;color:#475569}}
.alarm{{background:#fff;border:2px solid #dc2626;border-radius:14px;padding:14px 16px;
  margin-bottom:14px;font-size:15.5px;font-weight:700;color:#b91c1c}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:16px 18px;margin-bottom:14px}}
.card h3{{margin:0 0 10px;font-size:15px}}
.pm-current{{padding:14px}}
.pm-current .hero{{margin-bottom:0}}
.pm-current .alarm{{margin-top:14px;margin-bottom:0}}
.pm-section{{border-top:1px solid #e8eef5;margin-top:14px;padding-top:14px}}
.pm-section:first-child{{border-top:none;margin-top:0;padding-top:0}}
ul{{margin:0;padding-left:4px;list-style:none}}
.reasons li,.card li{{padding:7px 0;border-bottom:1px solid #f1f5f9;font-size:14.5px}}
.reasons li:last-child,.card li:last-child{{border-bottom:none}}
.muted{{color:#94a3b8}}
.foot{{font-size:12px;color:#94a3b8;padding:4px 4px 30px;line-height:1.7}}
b{{font-weight:700}}
.stats{{display:flex;gap:10px;margin-bottom:12px}}
.stat{{flex:1;background:#f8fafc;border-radius:10px;padding:10px 6px;text-align:center}}
.stat b{{display:block;font-size:20px;color:#0f172a}}
.stat span{{font-size:11px;color:#94a3b8}}
.hitem{{border:1px solid #e8edf3;border-radius:10px;padding:10px 12px;margin-top:10px;background:#fff}}
.hrow{{display:flex;justify-content:space-between;align-items:center;font-size:14px;margin-bottom:4px}}
.htime{{font-size:11.5px;color:#94a3b8;font-weight:400}}
.hist-btn{{display:block;margin-top:14px;text-align:center;background:#f1f5f9;color:#334155;
  text-decoration:none;padding:10px;border-radius:10px;font-size:13.5px;font-weight:600}}
.hist-btn:hover{{background:#e2e8f0}}
.hsaid{{font-size:13px;color:#334155}}
.htop{{font-size:12.5px;color:#b91c1c;margin-top:3px}}
.hact{{font-size:12.5px;color:#475569;margin-top:3px}}
.hitem details{{margin-top:6px}}
.hitem summary{{font-size:12.5px;color:#7c3aed;cursor:pointer}}
.rlist{{margin:6px 0 2px;padding-left:2px}}
.rlist li{{font-size:12.5px;color:#475569;padding:4px 0;border-bottom:1px solid #f5f7fa;list-style:none}}
.ev-meta{{font-size:12.5px;color:#64748b;margin-bottom:8px}}
.ev-row{{display:grid;grid-template-columns:118px 1fr;gap:10px;padding:8px 0;border-bottom:1px solid #f1f5f9}}
.ev-row:last-child{{border-bottom:none}}
.ev-label{{font-size:13px;font-weight:700}}
.ev-label span{{display:block;font-size:11px;color:#94a3b8;font-weight:500;margin-top:1px}}
.ev-head{{font-size:13.5px;font-weight:650;color:#0f172a}}
.ev-plain{{font-size:12.5px;color:#64748b;margin-top:2px;line-height:1.55}}
@media (max-width:480px){{.ev-row{{grid-template-columns:1fr}}}}
</style></head><body><div class="wrap">
<div class="title"><span>🚦 美股开盘前 · 今晚能不能买</span><span class="muted">每5分钟自动刷新</span></div>
{body}
{history_html}
{foot_html}
</div></body></html>"""
        return HTMLResponse(content=html)

    @app.get("/premarket/history")
    def premarket_gate_history_page():
        """盘前预警「全部历史」二级页 —— 列表形式，数据多也清楚（详情页/抽屉按钮跳来）。"""
        from fastapi.responses import HTMLResponse

        hist = _pm_load_history(settle=True)
        hist_sorted = sorted(hist, key=lambda r: (r.get("date", ""), _pm_rec_time(r)), reverse=True)
        from stock_research.core import premarket_gate as _pg
        summ = _pg.summarize_history(hist)

        body = (
            '<a class="back" href="/">‹ 返回首页</a>'
            '<a class="back" href="/premarket" style="margin-left:14px">今晚预警 ›</a>'
            '<h2>📜 盘前预警 · 全部历史记录</h2>'
            f'<div class="card">{_pm_summary_html(summ)}</div>'
            f'<div class="card listcard">{_pm_history_list_html(hist_sorted)}</div>'
            '<div class="foot">每行点开看「当晚报了哪些理由」，对照「事后涨跌」即可核对准不准 · '
            '🟢正常买 🟡小仓试 🟠先别开新仓 🔴别买只看好已有 · ⚠️ 仅供参考</div>'
        )
        html = f"""<!DOCTYPE html><html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>盘前预警 · 全部历史</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f8fafc;color:#0f172a;
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
  line-height:1.6;padding:16px}}
.wrap{{max-width:760px;margin:0 auto}}
.back{{font-size:13px;color:#7c3aed;text-decoration:none}}
h2{{font-size:18px;margin:8px 0 14px}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:16px 18px;margin-bottom:14px}}
.listcard{{padding:6px 10px}}
.stats{{display:flex;gap:10px;margin-bottom:12px}}
.stat{{flex:1;background:#f8fafc;border-radius:10px;padding:10px 6px;text-align:center}}
.stat b{{display:block;font-size:20px;color:#0f172a}}
.stat span{{font-size:11px;color:#94a3b8}}
.muted{{color:#94a3b8}}
b{{font-weight:700}}
.hl-head,.hl-row{{display:grid;grid-template-columns:1.5fr .9fr 1fr 1.3fr;gap:8px;align-items:center}}
.hl-head{{font-size:11.5px;color:#94a3b8;padding:8px 6px;border-bottom:1px solid #e2e8f0}}
.hl{{border-bottom:1px solid #f1f5f9}}
.hl-row{{padding:10px 6px;font-size:13.5px;cursor:pointer;list-style:none}}
.hl-row::-webkit-details-marker{{display:none}}
.hl[open] .hl-row{{background:#f8fafc}}
.htime{{font-size:11.5px;color:#94a3b8;font-weight:400}}
.hl-move{{font-size:12px;color:#475569}}
.hl-detail{{padding:4px 10px 14px;background:#f8fafc}}
.hl-said{{font-size:13px;color:#334155;margin-bottom:4px}}
.hl-top{{font-size:12.5px;color:#b91c1c;margin-bottom:6px}}
.rlist{{margin:0;padding-left:2px;list-style:none}}
.rlist li{{font-size:12.5px;color:#475569;padding:4px 0;border-bottom:1px solid #eef2f6}}
.foot{{font-size:12px;color:#94a3b8;padding:8px 4px 30px;line-height:1.7}}
</style></head><body><div class="wrap">{body}</div></body></html>"""
        return HTMLResponse(content=html)

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

    _V2_REFRESH_LOCK = _REPO_ROOT / "data" / "latest" / "v2_recommendation_refresh.lock"
    _V2_REFRESH_LOG = _REPO_ROOT / "data" / "logs" / "v2_recommendation_refresh.log"

    def _v2_refresh_status() -> dict[str, Any]:
        import json as _json, os as _os, time as _time
        if not _V2_REFRESH_LOCK.exists():
            return {"running": False}
        try:
            info = _json.loads(_V2_REFRESH_LOCK.read_text(encoding="utf-8"))
        except Exception:
            _V2_REFRESH_LOCK.unlink(missing_ok=True)
            return {"running": False, "stale_cleared": True}
        pid = info.get("pid")
        alive = False
        if pid:
            try:
                _os.kill(int(pid), 0)
                alive = True
            except OSError:
                alive = False
        if not alive:
            _V2_REFRESH_LOCK.unlink(missing_ok=True)
            return {"running": False, "stale_cleared": True, "last_pid": pid}
        age_s = _time.time() - float(info.get("started_at") or _time.time())
        return {
            "running": True,
            "pid": pid,
            "age_s": round(age_s, 1),
            "trigger": info.get("trigger"),
            "code": info.get("code"),
            "log": str(_V2_REFRESH_LOG),
        }

    def _spawn_v2_recommendation_refresh(*, trigger: str, code: str | None = None) -> dict[str, Any]:
        """后台刷新系统池行情并重算 AI 推荐。

        只更新 system_tech_universe 的行情/推荐/组合产物，不写 watchlist 或真实持仓。
        """
        import json as _json, re as _re, subprocess as _sub, time as _time
        existing = _v2_refresh_status()
        if existing.get("running"):
            return {"status": "already_running", **existing}

        clean_code = str(code or "").strip().upper()
        if clean_code and not _re.match(r"^[A-Z0-9][A-Z0-9.\-]{0,24}$", clean_code):
            raise HTTPException(400, "code contains unsupported characters")

        fetch_cmd = [
            sys.executable,
            str(_REPO_ROOT / "scripts" / "pipeline" / "fetch_stock_prices.py"),
            "--source",
            "tech-universe",
            "--db-schema",
            "v2",
            "--refresh-fundamentals",
            "--workers",
            "1",
        ]
        if not clean_code:
            fetch_cmd.append("--fast-repair")
        if clean_code:
            fetch_cmd.extend(["--code", clean_code])
        steps = [
            {"name": "fetch_stock_prices", "cmd": fetch_cmd},
            {
                "name": "build_v2_recommendations",
                "cmd": [sys.executable, str(_REPO_ROOT / "scripts" / "tools" / "build_v2_recommendations.py")],
            },
            {
                "name": "optimize_portfolio",
                "cmd": [sys.executable, "-m", "stock_research.jobs.optimize_portfolio"],
            },
            {
                "name": "recommendation_evidence_report",
                "cmd": [sys.executable, str(_REPO_ROOT / "scripts" / "tools" / "recommendation_evidence_report.py")],
            },
            {
                "name": "build_stock_dashboard_html",
                "cmd": [sys.executable, str(_REPO_ROOT / "scripts" / "pipeline" / "build_stock_dashboard_html.py")],
            },
            {
                "name": "production_acceptance_check",
                "cmd": [sys.executable, str(_REPO_ROOT / "scripts" / "tools" / "production_acceptance_check.py")],
            },
        ]

        wrapper_code = r"""
import datetime as _dt
import json as _json
import subprocess as _sub
import sys as _sys
from pathlib import Path as _Path

steps = _json.loads(_sys.argv[1])
log_path = _Path(_sys.argv[2])
lock_path = _Path(_sys.argv[3])
repo_root = _sys.argv[4]
trigger = _sys.argv[5].replace("\n", " ")[:240]
code = _sys.argv[6].replace("\n", " ")[:80]

log_path.parent.mkdir(parents=True, exist_ok=True)
rc = 0
try:
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"[v2-refresh] start trigger={trigger} code={code or '-'} at {_dt.datetime.now().isoformat(timespec='seconds')}\n")
        for step in steps:
            name = step.get("name")
            cmd = step.get("cmd") or []
            log.write(f"[v2-refresh] step {name}: {' '.join(cmd)}\n")
            log.flush()
            env = None
            if name == "fetch_stock_prices":
                import os as _os
                env = dict(_os.environ)
                env.setdefault("PYTHONUNBUFFERED", "1")
                env.setdefault("STOCK_ASSISTANT_YF_WORKERS", "1")
                env.setdefault("STOCK_ASSISTANT_YF_BATCH_SIZE", "40")
            rc = _sub.call(cmd, cwd=repo_root, stdout=log, stderr=_sub.STDOUT, env=env)
            log.write(f"[v2-refresh] step {name} exit={rc} at {_dt.datetime.now().isoformat(timespec='seconds')}\n")
            log.flush()
            if rc:
                break
        log.write(f"[v2-refresh] done exit={rc} at {_dt.datetime.now().isoformat(timespec='seconds')}\n")
finally:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
_sys.exit(rc)
"""
        _V2_REFRESH_LOCK.parent.mkdir(parents=True, exist_ok=True)
        proc = _sub.Popen(
            [
                sys.executable,
                "-c",
                wrapper_code,
                _json.dumps(steps, ensure_ascii=False),
                str(_V2_REFRESH_LOG),
                str(_V2_REFRESH_LOCK),
                str(_REPO_ROOT),
                str(trigger or "manual"),
                clean_code,
            ],
            cwd=str(_REPO_ROOT),
            stdout=_sub.DEVNULL,
            stderr=_sub.DEVNULL,
            start_new_session=True,
        )
        _V2_REFRESH_LOCK.write_text(_json.dumps({
            "pid": proc.pid,
            "started_at": _time.time(),
            "trigger": trigger,
            "code": clean_code or None,
            "steps": [s["name"] for s in steps],
            "log": str(_V2_REFRESH_LOG),
        }, ensure_ascii=False))
        return {"status": "started", "pid": proc.pid, "job": "refresh_v2_recommendations", "code": clean_code or None}

    @app.get("/api/jobs/refresh-v2-recommendations/status")
    def refresh_v2_recommendations_status() -> dict[str, Any]:
        return _v2_refresh_status()

    @app.post("/api/jobs/refresh-v2-recommendations")
    def trigger_refresh_v2_recommendations(item: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """刷新系统池行情/估值并重算 AI 推荐、组合和看板（异步）。"""
        return _spawn_v2_recommendation_refresh(
            trigger=str(item.get("trigger") or "manual"),
            code=item.get("code"),
        )

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
