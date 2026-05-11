"""StockAssistant Web App — Streamlit 版（替代静态 HTML 仪表盘）。

把 v7.5 系统的核心数据用 streamlit 包装成可交互 Web App。

部署：
  本地运行： streamlit run streamlit_app.py
  公网部署： 推到 GitHub → 在 share.streamlit.io 一键部署（免费）
            或 streamlit deploy（需 Streamlit Cloud 账号）

Web App 包含 6 个 Tab：
  📌 概览           - 系统状态 + 当前实盘防御警报
  ⭐ 每日推荐      - v6 ⭐⭐⭐ 推荐 + 评分分解
  🛡 反向审查      - 主题集中度 + 13F + 估值 + 相关性
  📊 因子治理      - alphalens IC tear sheet
  🌐 OpenBB 情报   - 宏观 + 行业 + 商品 + PCR + 内部人
  💀 Stress Test  - 4 崩盘 × 3 防御对比

不构成投资建议；维护者: yanli
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

_REPO_ROOT = Path(__file__).resolve().parents[2]  # repo root (was parent before 5.11 move)
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))  # 2026-05-11 lib 迁移


# ─────────── 页面配置 ───────────

st.set_page_config(
    page_title="StockAssistant",
    page_icon="📊",
    layout="wide",
)


# ─────────── 工具：读快照（DuckDB 优先，文件 fallback）───────────
#
# 部署到 Streamlit Cloud 时，data/snapshots/ 因 .gitignore 不可用；
# DuckDB 文件在仓库里（plan a 入库），所以走 DB 读路径。

@st.cache_data(ttl=300)
def load_latest_snapshot(name_prefix: str, dir_name: str = "audit") -> dict | None:
    """读最新快照——优先 DuckDB snapshots 表，失败/缺数据再 fallback 到本地文件。"""
    db_path = _REPO_ROOT / "stock_history.duckdb"
    if db_path.exists():
        try:
            import duckdb
            con = duckdb.connect(str(db_path), read_only=True)
            row = con.execute(
                "SELECT payload FROM snapshots "
                "WHERE category=? AND name=? "
                "ORDER BY taken_at DESC LIMIT 1",
                [dir_name, name_prefix],
            ).fetchone()
            con.close()
            if row:
                payload = row[0]
                return json.loads(payload) if isinstance(payload, str) else payload
        except Exception:
            pass
    # fallback：本地 data/snapshots/<dir>/<prefix>_*.json
    snap_dir = _REPO_ROOT / "data" / "snapshots" / dir_name
    if not snap_dir.exists():
        return None
    files = sorted([f for f in snap_dir.glob(f"{name_prefix}_*.json")], reverse=True)
    if not files:
        return None
    try:
        return json.loads(files[0].read_text(encoding="utf-8"))
    except Exception:
        return None


@st.cache_data(ttl=300)
def load_picks_csv(date_str: str) -> pd.DataFrame | None:
    """读某天的归档 picks.csv。"""
    f = _REPO_ROOT / "archive" / date_str / "picks.csv"
    if not f.exists():
        return None
    return pd.read_csv(f)


# ─────────── 顶部 ───────────

st.title("📊 StockAssistant")
st.caption(
    "AI 主线投资研究系统 · 学术因子 + Markowitz + 反向审查 + 实盘防御 · "
    "**不构成投资建议**"
)

# 关键状态卡片（顶部）
col1, col2, col3, col4 = st.columns(4)
defense_snap = load_latest_snapshot("realtime_defense")
audit_snap = load_latest_snapshot("picks_audit")
intel_snap = load_latest_snapshot("openbb_intel")
factor_ic_snap = load_latest_snapshot("factor_ic")

with col1:
    sev = defense_snap.get("severity", "?") if defense_snap else "?"
    color = {"NONE": "🟢", "LOW": "🟡", "HIGH": "🟠", "CRITICAL": "🔴"}.get(sev, "❓")
    st.metric("实盘防御", f"{color} {sev}",
              defense_snap.get("summary", "") if defense_snap else "")

with col2:
    if audit_snap:
        n_picks = audit_snap.get("picks_today_count", 0)
        st.metric("⭐⭐⭐ 当日推荐数", n_picks)
    else:
        st.metric("⭐⭐⭐ 当日推荐数", "—")

with col3:
    if intel_snap:
        macro = intel_snap.get("macro", {})
        regime = macro.get("regime", "—")
        st.metric("宏观 Regime", regime,
                  f"Fed={macro.get('fed_rate', '?')}%" if macro.get("fed_rate") else "")
    else:
        st.metric("宏观 Regime", "—")

with col4:
    if factor_ic_snap:
        n_factors = len(factor_ic_snap.get("factors", {}))
        st.metric("活跃因子", n_factors)
    else:
        st.metric("活跃因子", "—")

st.divider()


# ─────────── Tabs ───────────

(tab_overview, tab_picks, tab_charts, tab_watchlist, tab_audit, tab_factors,
 tab_intel, tab_stress, tab_a_share, tab_ipo) = st.tabs([
    "📌 概览",
    "⭐ 每日推荐",
    "📈 K 线图表",
    "🔭 Watchlist",
    "🛡 反向审查",
    "📊 因子治理",
    "🌐 OpenBB 情报",
    "💀 Stress Test",
    "🇨🇳 A 股优选",
    "📈 IPO 打新",
])


# ─────────── 工具：读 A 股闭环输出 ───────────

@st.cache_data(ttl=300)
def _load_json(rel: str) -> dict | None:
    f = _REPO_ROOT / rel
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


# ───── Tab 1: 概览 ─────
with tab_overview:
    # 顶部：实盘防御警报（实时数据，永远放最上面）
    if defense_snap:
        st.subheader(f"🛡 实盘防御警报（{defense_snap.get('generated_at', '')[:16]}）")
        alerts = defense_snap.get("alerts", [])
        if not alerts:
            st.success("🟢 当前无任何防御警报")
        else:
            for a in alerts:
                sev = a.get("severity", "")
                icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "⚪"}.get(sev, "❓")
                with st.expander(f"{icon} [{sev}] {a.get('type')}"):
                    st.write(a.get("suggested_action", a.get("trigger", "")))

    st.divider()

    # 中部 + 底部：从 docs/关于.md 读「系统能力 + Roadmap」
    # 单一信息源：以后更新文档即可，UI 自动跟上
    about_md_path = _REPO_ROOT / "docs" / "关于.md"
    if about_md_path.exists():
        about_md = about_md_path.read_text(encoding="utf-8")
        # 按章节标题切两半
        SEP = "# 二、接下来要做的 (roadmap)"
        if SEP in about_md:
            capabilities_md, roadmap_md = about_md.split(SEP, 1)
            roadmap_md = SEP + roadmap_md
        else:
            capabilities_md, roadmap_md = about_md, ""

        # Roadmap 默认展开 —「接下来要做什么」是高频问题
        if roadmap_md:
            with st.expander("🗺 接下来要做什么（Roadmap）", expanded=True):
                st.markdown(roadmap_md)

        # 系统能力默认折叠 —「系统能做什么」点开再看
        with st.expander("📖 系统能力概览（关于.md §1）", expanded=False):
            st.markdown(capabilities_md)

        st.caption(f"📄 内容源：[docs/关于.md](docs/关于.md) — 改文档即更新本页")
    else:
        st.warning("找不到 docs/关于.md — 请先创建该文件")


# ───── Tab 2: 每日推荐 ─────
with tab_picks:
    st.header("⭐⭐⭐ 当日推荐")
    today_str = datetime.now().strftime("%Y-%m-%d")
    df = load_picks_csv(today_str)
    if df is None:
        # 找最近的归档
        archive_dir = _REPO_ROOT / "archive"
        if archive_dir.exists():
            days = sorted([d.name for d in archive_dir.iterdir() if d.is_dir()], reverse=True)
            if days:
                df = load_picks_csv(days[0])
                st.caption(f"最近归档：{days[0]}")
    if df is not None and len(df) > 0:
        # 只看 ⭐⭐⭐
        if "入选评分" in df.columns:
            strong = df[df["入选评分"].astype(str).str.contains("⭐⭐⭐", na=False)]
            st.subheader(f"⭐⭐⭐ 强烈推荐（{len(strong)} 只）")
            st.dataframe(strong[["股票名称", "代码", "综合得分", "AI关联度", "主题分类",
                                  "入选时1Y%", "入选理由"]] if all(c in strong.columns for c in
                ["股票名称", "代码", "综合得分", "AI关联度", "主题分类", "入选时1Y%", "入选理由"])
                else strong, use_container_width=True)

        with st.expander("查看全部推荐"):
            st.dataframe(df, use_container_width=True)
    else:
        st.warning("无 picks 归档；先跑 daily_picks_v5.py + jobs.archive_picks")


# ───── Tab 3: K 线图表 + 因子叠加（TV 替代位）─────

@st.cache_data(ttl=300)
def load_history_data() -> dict:
    """读 history_data.json 的 tickers map，缺则返回空 dict。"""
    p = _REPO_ROOT / "data" / "latest" / "history_data.json"
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d.get("tickers", {}) if isinstance(d, dict) else {}
    except Exception:
        return {}


@st.cache_data(ttl=300)
def load_plan_v5() -> dict | None:
    """读 plan_a_v5_constrained 优先，回退 plan_a_v5。"""
    for fn in ("plan_a_v5_constrained.json", "plan_a_v5.json"):
        f = _REPO_ROOT / fn
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                pass
    return None


with tab_charts:
    st.header("📈 K 线图表 + 因子叠加")
    st.caption("替代 TradingView 的最简形态——价格 + 移动均线，叠加你自己系统的观点（权重 / F-Score / regime）")

    hist = load_history_data()
    plan = load_plan_v5()

    if not hist:
        st.warning("无 history_data.json — 先跑 `python3 fetch_stock_prices.py` 拉历史价格")
    else:
        plan_v5 = (plan.get("plan_v5") or []) if plan else []
        plan_meta = {e.get("ticker"): e for e in plan_v5}

        col_l, col_m, col_r = st.columns([2, 1, 2])
        with col_l:
            all_tickers = sorted(hist.keys())
            held = [t for t in all_tickers if t in plan_meta]
            others = [t for t in all_tickers if t not in plan_meta]
            options = held + others
            default_idx = 0 if options else None
            selected = st.selectbox(
                "选股票（⭐ 标记 = 当前建议组合）",
                options=options,
                index=default_idx,
                format_func=lambda t: f"⭐ {t}" if t in plan_meta else t,
            )
        with col_m:
            window = st.selectbox("时间窗口", [30, 60, 120, 250, 500], index=2)
        with col_r:
            show_ma = st.multiselect("移动均线", ["MA20", "MA60", "MA200"],
                                      default=["MA20", "MA60"])

        ticker_data = hist.get(selected, {})
        ts = ticker_data.get("ts", [])
        closes = ticker_data.get("close", [])
        if not ts or not closes or len(ts) < 5:
            st.warning(f"{selected} 数据不足（{len(closes)} 天）")
        else:
            df = pd.DataFrame({"date": pd.to_datetime(ts), "close": closes}).set_index("date")
            # 移动均线先在全量上算，再切窗口（避免边界 NaN）
            if "MA20" in show_ma:
                df["MA20"] = df["close"].rolling(20).mean()
            if "MA60" in show_ma:
                df["MA60"] = df["close"].rolling(60).mean()
            if "MA200" in show_ma:
                df["MA200"] = df["close"].rolling(200).mean()
            df_win = df.tail(window)

            # KPI 行
            start_p = float(df_win["close"].iloc[0])
            end_p = float(df_win["close"].iloc[-1])
            ret_pct = ((end_p - start_p) / start_p * 100) if start_p else 0.0
            high_p = float(df_win["close"].max())
            low_p = float(df_win["close"].min())
            maxdd_pct = float(((df_win["close"] / df_win["close"].cummax() - 1) * 100).min())

            k1, k2, k3, k4, k5 = st.columns(5)
            k1.metric(f"{window}d 涨跌", f"{ret_pct:+.1f}%", f"${end_p:.2f}")
            k2.metric(f"{window}d 高", f"${high_p:.2f}")
            k3.metric(f"{window}d 低", f"${low_p:.2f}")
            k4.metric(f"{window}d 最大回撤", f"{maxdd_pct:.1f}%")
            meta = plan_meta.get(selected, {})
            if meta:
                w = (meta.get("v5_weight") or meta.get("weight") or 0) * 100
                k5.metric("⭐ 系统权重", f"{w:.1f}%", f"F-Score {meta.get('f_score', '?')}")
            else:
                k5.metric("系统权重", "—", "未在建议组合")

            # 主图（price + MA）
            st.line_chart(df_win, use_container_width=True, height=420)

            # 系统观点叠加（针对持仓股）
            st.divider()
            if meta:
                st.subheader(f"🤖 系统对 {selected} 的观点")
                c1, c2 = st.columns(2)
                with c1:
                    st.markdown(
                        f"- **当前建议权重**: {(meta.get('v5_weight') or meta.get('weight') or 0)*100:.2f}%\n"
                        f"- **F-Score**: {meta.get('f_score', '?')} / 9\n"
                        f"- **综合 Z 分**: {meta.get('composite_z', meta.get('composite', 0)):+.2f}\n"
                        f"- **行业**: {meta.get('sector', '—')}"
                    )
                with c2:
                    fpe = meta.get("forward_pe")
                    peg = meta.get("peg_ratio")
                    st.markdown(
                        f"- **Forward P/E**: {fpe if fpe else '—'}\n"
                        f"- **PEG**: {peg if peg else '—'}\n"
                        f"- **1Y 涨跌**: {meta.get('one_year_pct', meta.get('y1', '?'))}%\n"
                        f"- **1M 涨跌**: {meta.get('one_month_pct', meta.get('m1', '?'))}%"
                    )
            else:
                st.info(f"ℹ️ **{selected}** 不在当前建议组合 — 仅展示价格走势，无系统观点")


# ───── Tab 4: Watchlist 概览（编辑走飞书 base，本视图只浏览）─────

_SPARK_BARS = "▁▂▃▄▅▆▇█"


def _sparkline_str(values: list[float], length: int = 10) -> str:
    if not values or len(values) < 2:
        return "—"
    n = len(values)
    if n > length:
        step = n / length
        sampled = [values[min(n - 1, int(i * step))] for i in range(length)]
    else:
        sampled = list(values)
    lo, hi = min(sampled), max(sampled)
    if hi == lo:
        return _SPARK_BARS[3] * len(sampled)
    span = hi - lo
    return "".join(_SPARK_BARS[min(7, int((v - lo) / span * 7))] for v in sampled)


with tab_watchlist:
    st.header("🔭 Watchlist 概览")
    st.caption(
        "全部已抓价的关注股 + 60d sparkline。"
        "**编辑**（增删）请去飞书 watchlist base 表 — 此处只读浏览，避免双轨制。"
    )

    hist = load_history_data()
    plan = load_plan_v5()

    if not hist:
        st.warning("无 history_data.json — 先跑 `python3 fetch_stock_prices.py`")
    else:
        plan_v5 = (plan.get("plan_v5") or []) if plan else []
        plan_meta = {e.get("ticker"): e for e in plan_v5}

        rows = []
        for tkr, td in hist.items():
            closes = td.get("close") or []
            if len(closes) < 2:
                continue
            recent = closes[-60:]
            pct60 = ((recent[-1] - recent[0]) / recent[0] * 100) if recent[0] else None
            pct20 = None
            if len(closes) >= 20:
                v20 = closes[-20:]
                pct20 = ((v20[-1] - v20[0]) / v20[0] * 100) if v20[0] else None
            meta = plan_meta.get(tkr, {})
            weight = (meta.get("v5_weight") or meta.get("weight") or 0) * 100
            rows.append({
                "ticker": tkr,
                "name": td.get("name", ""),
                "market": td.get("market", "") or ("US" if not any(td.get("yf_ticker","").endswith(s) for s in (".SS",".SZ",".BJ",".HK",".KS")) else td.get("yf_ticker","").split(".")[-1]),
                "sparkline 60d": _sparkline_str(recent, length=12),
                "60d %": round(pct60, 1) if pct60 is not None else None,
                "20d %": round(pct20, 1) if pct20 is not None else None,
                "在建议组合": "⭐" if tkr in plan_meta else "",
                "权重 %": round(weight, 2) if weight else None,
                "F-Score": meta.get("f_score") if meta else None,
                "数据天数": len(closes),
                "最新价": round(float(closes[-1]), 2),
            })

        if not rows:
            st.warning("history_data 里没找到可用 ticker")
        else:
            df = pd.DataFrame(rows)
            # KPI
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("关注总数", len(df))
            c2.metric("在建议组合", int((df["在建议组合"] == "⭐").sum()))
            c3.metric("60d 上涨", int((df["60d %"].dropna() > 0).sum()))
            c4.metric("60d 下跌", int((df["60d %"].dropna() < 0).sum()))

            # 过滤
            col_f1, col_f2, col_f3 = st.columns([1, 1, 2])
            with col_f1:
                only_held = st.checkbox("仅看建议组合", value=False)
            with col_f2:
                market_filter = st.multiselect(
                    "市场",
                    options=sorted(df["market"].dropna().unique().tolist()),
                    default=[],
                )
            with col_f3:
                search = st.text_input("ticker 搜索", placeholder="例：NVDA / 600 / 0700")

            df_view = df.copy()
            if only_held:
                df_view = df_view[df_view["在建议组合"] == "⭐"]
            if market_filter:
                df_view = df_view[df_view["market"].isin(market_filter)]
            if search:
                m = df_view["ticker"].str.contains(search, case=False, na=False) | \
                    df_view["name"].astype(str).str.contains(search, case=False, na=False)
                df_view = df_view[m]

            st.dataframe(
                df_view.sort_values("60d %", ascending=False, na_position="last"),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "60d %": st.column_config.NumberColumn(format="%.1f%%"),
                    "20d %": st.column_config.NumberColumn(format="%.1f%%"),
                    "权重 %": st.column_config.NumberColumn(format="%.2f%%"),
                    "最新价": st.column_config.NumberColumn(format="$%.2f"),
                },
            )

            st.caption(
                f"显示 {len(df_view)} / 总 {len(df)} 只 · "
                "增删股票走 dashboard 的 ⚙️ Watchlist 编辑(直接 UPDATE DuckDB),保存即生效"
            )


# ───── Tab 5: 反向审查 ─────
with tab_audit:
    st.header("🛡 反向审查")
    if audit_snap:
        st.caption(f"快照: {audit_snap.get('ts', '')}")

        col_l, col_r = st.columns(2)
        with col_l:
            st.subheader("主题集中度（Risk Parity）")
            tc = audit_snap.get("theme_concentration", {})
            if tc.get("status") == "ok":
                st.markdown(f"**{tc['verdict']}**")
                dist = pd.DataFrame(tc.get("distribution", []))
                if len(dist) > 0:
                    st.bar_chart(dist.set_index("theme")["pct"])
            else:
                st.info("跳过：" + tc.get("reason", ""))

        with col_r:
            st.subheader("估值警告")
            vs = audit_snap.get("valuation_sanity", {})
            if vs.get("warn_count", 0) == 0:
                st.success("🟢 当日 ⭐⭐⭐ 估值均合理")
            else:
                for w in vs.get("warnings", []):
                    flags = " / ".join(w["flags"])
                    st.warning(f"**{w['name']}** ({w['code']}): {flags}")

        st.subheader("相关性矩阵（Markowitz 伪分散对）")
        cr = audit_snap.get("correlation", {})
        if cr.get("status") == "ok":
            pairs = cr.get("high_corr_pairs", [])
            if pairs:
                df_c = pd.DataFrame(pairs)
                st.dataframe(df_c, use_container_width=True)
            else:
                st.success(f"🟢 无相关 > {cr.get('threshold', 0.75)} 的对")
        else:
            st.info("跳过：" + cr.get("reason", ""))
    else:
        st.warning("无审查快照；先跑 jobs.audit_picks")


# ───── Tab 4: 因子治理 ─────
with tab_factors:
    st.header("📊 因子治理（IC + Quintile Tear Sheet）")
    if factor_ic_snap:
        factors = factor_ic_snap.get("factors", {})
        if factors:
            for fname, info in factors.items():
                with st.expander(f"📌 {fname}"):
                    s = info.get("summary", {})
                    a = info.get("alert", {})
                    cols = st.columns(4)
                    cols[0].metric("Mean IC", f"{s.get('mean_ic', 0):+.3f}")
                    cols[1].metric("IC IR", f"{s.get('ic_ir', 0):+.2f}")
                    cols[2].metric("Hit Rate", f"{s.get('hit_rate', 0)*100:.0f}%")
                    cols[3].metric("Status", a.get("status", ""))
                    st.caption(a.get("verdict", ""))
        else:
            st.info("先跑 jobs.audit_ic")

    # alphalens tear sheet 快照（优先 DuckDB，缺则本地文件）
    tearsheets = []
    db_path = _REPO_ROOT / "stock_history.duckdb"
    if db_path.exists():
        try:
            import duckdb
            con = duckdb.connect(str(db_path), read_only=True)
            rows = con.execute(
                "SELECT payload FROM snapshots WHERE category='tearsheet' "
                "AND name LIKE 'factor_tearsheet%' ORDER BY taken_at DESC LIMIT 3"
            ).fetchall()
            con.close()
            for r in rows:
                p = r[0]
                tearsheets.append(json.loads(p) if isinstance(p, str) else p)
        except Exception:
            pass
    if not tearsheets:
        snap_dir = _REPO_ROOT / "data" / "snapshots" / "tearsheet"
        if snap_dir.exists():
            for f in sorted(snap_dir.glob("factor_tearsheet_*.json"), reverse=True)[:3]:
                try:
                    tearsheets.append(json.loads(f.read_text(encoding="utf-8")))
                except Exception:
                    pass
    if tearsheets:
        st.subheader("Alphalens-style Tear Sheet")
        for ts in tearsheets:
            with st.expander(f"📁 {ts.get('factor', '')} ({ts.get('generated_at', '')[:10]})"):
                summary = ts.get("ic_summary", {})
                df_ic = pd.DataFrame(summary).T
                st.dataframe(df_ic, use_container_width=True)


# ───── Tab 5: OpenBB 情报 ─────
with tab_intel:
    st.header("🌐 OpenBB 综合情报")
    if intel_snap:
        st.caption(f"快照: {intel_snap.get('generated_at', '')}")

        # 宏观
        macro = intel_snap.get("macro", {})
        if macro:
            st.subheader("🌐 宏观 Regime")
            cols = st.columns(4)
            cols[0].metric("Fed Funds", f"{macro.get('fed_rate', '?')}%")
            cols[1].metric("10Y Treasury", f"{macro.get('ten_year_yield', '?')}%")
            cols[2].metric("收益率曲线", f"{macro.get('yield_curve', '?')}%")
            cols[3].metric("Regime", macro.get("regime", "?"))
            for a in macro.get("alerts", []):
                st.warning(f"[{a['severity']}] {a['msg']}")

        # 行业轮动
        st.subheader("🔄 GICS 11 行业轮动（60 天）")
        rot = intel_snap.get("sector_rotation", {})
        if rot.get("all_rankings"):
            df_rot = pd.DataFrame(rot["all_rankings"])
            st.bar_chart(df_rot.set_index("ticker")["return_pct"])

        # 商品
        st.subheader("🛢 5 大商品趋势")
        com = intel_snap.get("commodities", {})
        if com.get("rankings"):
            df_com = pd.DataFrame(com["rankings"])
            st.bar_chart(df_com.set_index("ticker")["cum_return_pct"])

        # 内部人
        ins = intel_snap.get("insider", {})
        if ins and "error" not in ins:
            col_a, col_b = st.columns(2)
            with col_a:
                st.subheader("🟢 强买入")
                for r in ins.get("strong_buy", []):
                    st.success(f"**{r['name']}** ({r['ticker']}) "
                               f"+${r['net_dollars']/1e6:.1f}M · {r['unique_buyers']}人")
            with col_b:
                st.subheader("🔴 强卖出")
                for r in ins.get("strong_sell", [])[:5]:
                    st.error(f"**{r['name']}** ({r['ticker']}) "
                             f"${r['net_dollars']/1e6:.1f}M · {r['unique_sellers']}人")
    else:
        st.warning("无 OpenBB 情报；先跑 jobs.openbb_intelligence")


# ───── Tab 6: Stress Test ─────
with tab_stress:
    st.header("💀 Stress Test (4 崩盘 × 3 防御)")
    stress_snap = load_latest_snapshot("stress_test")
    if stress_snap:
        # 3 路对比表
        st.subheader("A/B/C 防御机制对比")
        results = stress_snap.get("results", [])
        results_b = stress_snap.get("results_filtered", [])
        results_c = stress_snap.get("results_combined", [])
        idx_b = {r["regime"]: r for r in results_b}
        idx_c = {r["regime"]: r for r in results_c}
        rows = []
        for r in results:
            b = idx_b.get(r["regime"])
            c = idx_c.get(r["regime"])
            rows.append({
                "Regime": r["regime"],
                "A 原版 DD": r["portfolio_max_drawdown_pct"],
                "B 200MA DD": b["portfolio_max_drawdown_pct"] if b else None,
                "C 终极版 DD": c["portfolio_max_drawdown_pct"] if c else None,
                "C-A 改善": (c["portfolio_max_drawdown_pct"] - r["portfolio_max_drawdown_pct"]) if c else None,
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True)

        col_a, col_b = st.columns(2)
        with col_a:
            st.metric("B 200MA 平均改善",
                      f"{stress_snap.get('filter_b_avg_improvement_pct', 0):+.2f}%",
                      f"{stress_snap.get('filter_b_n_improved', 0)}/{len(results)} regime")
        with col_b:
            st.metric("C 终极版平均改善",
                      f"{stress_snap.get('filter_c_avg_improvement_pct', 0):+.2f}%",
                      f"{stress_snap.get('filter_c_n_improved', 0)}/{len(results)} regime")

        st.info("📚 学术依据：Faber 2007 (200MA) + Whaley 2009 (VIX) + O'Neil 2002 (-15% 止损)")
    else:
        st.warning("无 stress test 快照；先跑 jobs.stress_test")


# ───── Tab 7: A 股优选（v9.0 6 因子闭环）─────
with tab_a_share:
    st.header("🇨🇳 A 股每日优选（6 因子闭环 v9.0）")
    a_pick = _load_json("data/a_share_picks.json")
    if a_pick is None:
        st.warning("无 a_share_picks.json — 先跑 `python3 -m stock_research.jobs.a_share_picks`")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("总扫描", a_pick.get("n_total", 0))
        c2.metric("可买", a_pick.get("n_tradable", 0))
        c3.metric("入选", a_pick.get("n_recommended", 0))
        c4.metric("cutoff", f"{a_pick.get('cutoff', 0):.3f}")

        st.caption(f"生成时间: {a_pick.get('generated_at', '?')[:16]} · "
                   f"模式: {a_pick.get('mode', '?')}")

        weights = a_pick.get("factor_weights", {})
        if weights:
            st.subheader("📊 因子权重")
            st.bar_chart(pd.Series(weights))

        tailwind = a_pick.get("policy_tailwind", {})
        if tailwind:
            st.subheader("🏛 当前政策受益主题（最近 14 天命中 ≥ 2 次）")
            st.write(", ".join(f"**{t}** ({c} 次)"
                                for t, c in sorted(tailwind.items(), key=lambda x: -x[1])))

        selected = a_pick.get("selected", [])
        if selected:
            st.subheader(f"⭐ 入选 {len(selected)} 只")
            rows = []
            for e in selected:
                f_norm = e.get("f_score_norm")
                rows.append({
                    "代码": e.get("code"),
                    "名称": e.get("name"),
                    "F-Score": f"{f_norm * 9:.0f}/9" if f_norm is not None else "?",
                    "动量分位": f"{e.get('momentum_norm', 0):.2f}",
                    "龙虎榜": f"{e.get('lhb_score', 0.5):.2f}",
                    "北向": f"{e.get('north_score', 0.5):.2f}",
                    "PEAD": f"{e.get('pead_score', 0.5):.2f}",
                    "政策": f"+{e.get('policy_boost', 0):.2f}",
                    "风险×": f"{e.get('event_risk_score', 1.0):.2f}",
                    "综合": f"{e.get('composite', 0):.3f}",
                    "可买": "✅" if e.get("tradable") else "❌",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        all_entries = a_pick.get("all_entries", [])
        blocked = [e for e in all_entries if not e.get("tradable")]
        if blocked:
            with st.expander(f"❌ 被拦截的 {len(blocked)} 只（ST/涨停/停牌等）"):
                rows = [{
                    "代码": e.get("code"), "名称": e.get("name"),
                    "综合分": f"{e.get('composite', 0):.3f}",
                    "拦截原因": "; ".join(e.get("block_reasons") or []),
                } for e in blocked]
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        st.info(
            "📚 因子说明：综合分 = "
            "Piotroski(0.25) + 动量(0.15) + 反转(0.10) + 龙虎榜(0.15) + "
            "北向(0.15) + PEAD(0.10) + 政策(0.10)，再 × 事件风险加权 (0-1)"
        )


# ───── Tab 8: IPO 打新 ─────
with tab_ipo:
    st.header("📈 IPO 打新日历")
    ipo = _load_json("data/ipo_calendar.json")
    if ipo is None:
        st.warning("无 ipo_calendar.json — 先跑 `python3 -m stock_research.jobs.ipo_daily`")
    else:
        ups = ipo.get("upcoming_subscription", [])
        await_l = ipo.get("awaiting_listing", [])
        recent = ipo.get("recently_listed", [])

        c1, c2, c3 = st.columns(3)
        c1.metric("即将申购", len(ups))
        c2.metric("已申购未上市", len(await_l))
        c3.metric("近 30 日上市", len(recent))

        st.caption(f"生成时间: {ipo.get('fetched_at', '?')[:16]}")

        ai_only = st.checkbox("仅显示 AI 相关 (相关性 ≥ 2)", value=False)

        def _to_table(entries):
            rows = []
            for e in entries:
                if ai_only and e.get("ai_relevance", 0) < 2:
                    continue
                ai_flag = "🟢" if e.get("ai_relevance", 0) >= 2 else \
                          "🟡" if e.get("ai_relevance", 0) == 1 else "⚪"
                rows.append({
                    "代码": e.get("code"),
                    "申购代码": e.get("subscribe_code") or "-",
                    "名称": e.get("name"),
                    "板块": e.get("board"),
                    "申购日": e.get("subscribe_date") or "-",
                    "上市日": e.get("listing_date") or "-",
                    "发行价": f"¥{e.get('issue_price'):.2f}" if e.get("issue_price") else "-",
                    "PE": f"{e.get('pe_ratio'):.1f}" if e.get("pe_ratio") else "-",
                    "AI": f"{ai_flag} {e.get('ai_relevance', 0)}",
                    "主题": e.get("theme") or "-",
                    "业务": (e.get("business_desc") or e.get("industry") or "")[:40],
                })
            return pd.DataFrame(rows)

        if ups:
            st.subheader(f"🚀 即将申购 ({len(ups)})")
            st.dataframe(_to_table(ups), use_container_width=True, hide_index=True)
        if await_l:
            st.subheader(f"⏳ 已申购未上市 ({len(await_l)})")
            st.dataframe(_to_table(await_l), use_container_width=True, hide_index=True)
        if recent:
            st.subheader(f"📊 近 30 日上市 ({len(recent)})")
            st.dataframe(_to_table(recent), use_container_width=True, hide_index=True)


# ─────────── 底部 ───────────

st.divider()
st.caption(
    "维护: yanli · 数据源: SEC EDGAR / 港交所 / Finnhub / OpenBB / yfinance · "
    f"最后刷新: {datetime.now().strftime('%Y-%m-%d %H:%M')} · "
    "**不构成投资建议**"
)
