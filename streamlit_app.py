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

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT))


# ─────────── 页面配置 ───────────

st.set_page_config(
    page_title="StockAssistant v7.5",
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

st.title("📊 StockAssistant v7.5")
st.caption(
    "AI 主线投资研究系统 · 学术因子 + Markowitz + 反向审查 + 实盘防御 + OpenBB 增强 · "
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

(tab_overview, tab_picks, tab_audit, tab_factors, tab_intel, tab_stress,
 tab_a_share, tab_ipo) = st.tabs([
    "📌 概览",
    "⭐ 每日推荐",
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
    st.header("当前系统状态")
    st.markdown(
        """
        **v7.5 系统覆盖（11 步流水线 + 16 步流水线 daily_refresh）：**
        1. 多源数据：SEC EDGAR + akshare + Finnhub + yfinance + OpenBB FRED
        2. 5 因子学术模型：Piotroski + 12-1 动量 + 1月反转 + PEAD + 分析师
        3. 5 种组合优化：max_sharpe / min_vol / HRP / Black-Litterman / min_CVaR
        4. 双维度反向审查：时间维度（v6 walk-forward）+ 横截面（picks_audit）
        5. v7 实盘防御：VIX + 200MA + 单股 -15% 止损 + 宏观 + PCR
        6. OpenBB 增强：行业轮动 + 商品 vs 股票相关性 + 内部人交易
        7. Stress Test：4 历史崩盘 × 3 防御机制 A/B/C
        """
    )

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


# ───── Tab 3: 反向审查 ─────
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
