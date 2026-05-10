"""个股基本面深度报告（B 路线 Phase 1C — 端到端整合）

把 Phase 1A (fundamental_deep) + Phase 1B (peer_compare) + 公司基本信息
整合成单股研究报告 JSON / Markdown，作为 Phase 2 LLM 研报的"结构化输入"。

输出：
  - JSON: 机器可读，喂给 Claude API 生成自然语言研报
  - Markdown: 人类可读，直接看
  - 终端打印: 快速查看

CLI:
  python3 -m stock_research.jobs.fundamental_report NVDA
  python3 -m stock_research.jobs.fundamental_report NVDA --md NVDA_report.md
  python3 -m stock_research.jobs.fundamental_report NVDA --peers AMD AVGO TSM
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from stock_research.core import (
    fmp_client,
    fundamental_deep,
    peer_compare,
    forward_valuation,
    quarterly_trends,
    dcf_scenarios,
)


def build_report(ticker: str, peers: list[str] | None = None,
                 max_peers: int = 6) -> dict[str, Any]:
    """构建结构化基本面报告（不调 LLM，纯数据 + 公式分析）。"""
    if not fmp_client.is_available():
        return {"error": "FMP_API_KEY not set", "ticker": ticker}

    profile = fmp_client.fetch_company_profile(ticker)
    dcf = fmp_client.fetch_dcf(ticker)
    estimates = fmp_client.fetch_analyst_estimates(ticker)
    earnings = fmp_client.fetch_earnings_calendar(ticker)
    deep = fundamental_deep.analyze_fundamentals(ticker)
    peer = peer_compare.compare_with_peers(ticker, peers=peers, max_peers=max_peers)
    forward = forward_valuation.forward_multiples(ticker)
    trends = quarterly_trends.quarterly_trends(ticker, n_quarters=8)
    dcf_scen = dcf_scenarios.three_scenario_dcf(ticker)

    return {
        "ticker": ticker,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "profile": profile,
        "valuation": {
            "dcf_fmp_blackbox": dcf,             # 保留 FMP 黑盒 DCF 作参考
            "dcf_self_built": dcf_scen,          # 自建 DCF 三档 + 敏感度（主结论用这个）
            "analyst_estimates": estimates,
            "forward_multiples": forward,
        },
        "quarterly_trends": trends,
        "earnings_history": earnings,
        "fundamentals_deep": deep,
        "peer_compare": peer,
        "schema_version": "fundamental_report.v2",
    }


# ────────────────────────────────────────────────────────
# Markdown 渲染（给人看，也供 Phase 2 prompt 拼接）
# ────────────────────────────────────────────────────────

def to_markdown(r: dict[str, Any]) -> str:
    if r.get("error"):
        return f"# {r.get('ticker', '?')} 报告生成失败\n\n{r['error']}\n"

    p = r.get("profile") or {}
    val = r.get("valuation") or {}
    dcf = val.get("dcf_fmp_blackbox") or val.get("dcf") or {}
    dcf_self = val.get("dcf_self_built") or {}
    forward = val.get("forward_multiples") or {}
    trends = r.get("quarterly_trends") or {}
    deep = r.get("fundamentals_deep") or {}
    pc = r.get("peer_compare") or {}

    lines = []
    lines.append(f"# {r['ticker']} — 基本面深度报告")
    lines.append("")
    lines.append(f"> 生成时间: {r.get('generated_at')} · 数据源: FMP · "
                 "**仅供研究，不构成投资建议**")
    lines.append("")
    lines.append("## 一、公司概况")
    lines.append("")
    if p:
        lines.append(f"- **公司**: {p.get('company_name')}")
        lines.append(f"- **行业**: {p.get('sector')} / {p.get('industry')}")
        lines.append(f"- **国家/交易所**: {p.get('country')} / {p.get('exchange')}")
        lines.append(f"- **市值**: ${(p.get('market_cap') or 0)/1e9:.1f}B")
        lines.append(f"- **CEO**: {p.get('ceo')} · 员工: {p.get('employees')}")
        if p.get("description"):
            desc = (p["description"] or "")[:500]
            lines.append(f"- **业务**: {desc}{'...' if len(p['description']) > 500 else ''}")
    lines.append("")

    # 估值（重构 — 主结论用自建 DCF + Forward 倍数，FMP 黑盒 DCF 仅作参考）
    lines.append("## 二、估值")
    lines.append("")

    # ── 2.1 Forward 估值倍数（取代 trailing P/E 误导）──
    if forward and not forward.get("error"):
        lines.append("### 2.1 Forward 估值倍数（基于分析师一致预期）")
        lines.append("")
        lines.append("| 期 | FY 末 | EPS 预期 | 营收预期 | Fwd P/E | Fwd EV/Sales | 分析师数 |")
        lines.append("|---|---|---|---|---|---|---|")
        for label, key in [("FY1", "fy1"), ("FY2", "fy2"), ("FY3", "fy3")]:
            fy = forward.get(key)
            if not fy:
                continue
            rev_b = (fy.get("revenue_fwd") or 0) / 1e9
            lines.append(f"| {label} | {fy.get('date', '?')} | "
                         f"${fy.get('eps_fwd', '—')} | ${rev_b:.1f}B | "
                         f"{fy.get('pe_fwd') or '—'} | {fy.get('ev_sales_fwd') or '—'} | "
                         f"{fy.get('n_analysts_eps') or '—'} |")
        lines.append("")
        if forward.get("eps_cagr_2y_implied"):
            lines.append(f"- **隐含 EPS 2 年 CAGR**: {forward['eps_cagr_2y_implied']}%")
        if forward.get("peg_fy1"):
            lines.append(f"- **PEG (FY1)**: {forward['peg_fy1']} — {forward.get('verdict')}")
        lines.append(f"- ⓘ {forward.get('note', '')}")
        lines.append("")

    # ── 2.2 自建 DCF 三档场景 ──
    if dcf_self and not dcf_self.get("error"):
        lines.append("### 2.2 自建 DCF — 三档场景")
        lines.append("")
        bl = dcf_self.get("baseline") or {}
        norm_margin = bl.get('fcf_margin_normalized_pct')
        norm_n = bl.get('fcf_margin_normalized_n_years')
        latest_margin = bl.get('fcf_margin_latest_pct')
        # 显示单年 vs normalized — 让读者直接看到周期偏离程度
        margin_line = f"FCF margin **normalized {norm_margin}% ({norm_n}y avg)**"
        if latest_margin is not None and norm_margin is not None and abs(latest_margin - norm_margin) > 5:
            margin_line += f" ⚠️ 单年 {latest_margin}% 偏离 {abs(latest_margin - norm_margin):.1f}pp（周期/拐点信号）"
        else:
            margin_line += f"（单年 {latest_margin}%）"
        lines.append(f"基准: 现价 ${bl.get('current_price')} · 当前营收 ${bl.get('revenue_latest_b')}B · "
                     f"FCF ${bl.get('fcf_latest_b')}B · {margin_line} · "
                     f"分析师覆盖 {bl.get('analyst_coverage_years')} 年")
        lines.append("")
        lines.append("| 场景 | 增速 offset | 终值 FCF margin | WACC | TGR | Fair Value | vs 现价 |")
        lines.append("|---|---|---|---|---|---|---|")
        for label, key in [("🐻 保守", "bear"), ("🐂 基准", "base"), ("🚀 乐观", "bull")]:
            s = dcf_self["scenarios"].get(key) or {}
            if s.get("error"):
                lines.append(f"| {label} | ⚠️ {s['error']} | | | | | |")
                continue
            a = s["assumptions"]
            up = s.get("upside_pct")
            up_s = f"**{up:+.1f}%**" if up is not None else "—"
            lines.append(f"| {label} | {a['growth_offset_pp']:+}pp | "
                         f"{a['terminal_fcf_margin_pct']}% | {a['wacc_pct']}% | "
                         f"{a['tgr_pct']}% | ${s.get('fair_value_per_share')} | {up_s} |")
        lines.append("")

        # ── 2.3 敏感度矩阵 ──
        sens = dcf_self.get("sensitivity") or {}
        if sens.get("fair_value_grid"):
            lines.append("### 2.3 敏感度矩阵（base case，每格 = Fair Value $/股）")
            lines.append("")
            tgrs = sens["tgr_axis_pct"]
            waccs = sens["wacc_axis_pct"]
            cur = sens.get("current_price") or 0
            header = "| WACC \\ TGR | " + " | ".join(f"{t}%" for t in tgrs) + " |"
            lines.append(header)
            lines.append("|" + "|".join(["---"] * (len(tgrs) + 1)) + "|")
            for i, w in enumerate(waccs):
                cells = []
                for v in sens["fair_value_grid"][i]:
                    if v is None:
                        cells.append("—")
                    else:
                        marker = "🟢" if v > cur * 1.1 else ("🔴" if v < cur * 0.9 else "🟡")
                        cells.append(f"{marker}${v:.0f}")
                lines.append(f"| **{w}%** | " + " | ".join(cells) + " |")
            lines.append("")
            lines.append(f"- 🟢 fair value > 现价 +10% · 🟡 ±10% 内 · 🔴 < 现价 -10%")
            lines.append("")

    # ── 2.4 FMP 黑盒 DCF（仅作参考，结论别用这个）──
    if dcf and not dcf.get("error"):
        lines.append("### 2.4 FMP 黑盒 DCF（参考用，假设不透明）")
        lines.append("")
        lines.append(f"- DCF: ${dcf.get('dcf_intrinsic_value')} vs 现价 ${dcf.get('current_price')} "
                     f"→ {dcf.get('upside_pct')}% upside · {dcf.get('verdict')}")
        lines.append("- ⚠️ FMP 不公开 WACC/TGR/FCF 假设。结论建议看 2.2/2.3。")
        lines.append("")

    # ── 2.5 分析师一致预期（年度）──
    estimates = (val.get("analyst_estimates") or {}).get("estimates") or []
    if estimates:
        lines.append("### 2.5 分析师一致预期（前 4 年）")
        lines.append("")
        lines.append("| 年 | 营收均值 | EPS 均值 | 分析师数 |")
        lines.append("|---|---|---|---|")
        for est in estimates[:4]:
            rev_b = (est.get("revenue_avg") or 0) / 1e9
            lines.append(f"| {est.get('date', '?')[:4]} | ${rev_b:.1f}B | "
                         f"${est.get('eps_avg', '?')} | {est.get('analysts_eps') or '—'} |")
        lines.append("")

    # 杜邦（年度 + TTM 双视角）
    d = deep.get("dupont") or {}
    d_ttm = deep.get("dupont_ttm") or {}
    lines.append("## 三、杜邦五因子分解")
    lines.append("")

    # 3.1 年度（FY vs FY-1）
    lines.append("### 3.1 年度视角（FY vs FY-1）")
    if d.get("error"):
        lines.append(f"⚠️ {d['error']}")
    else:
        lines.append(f"- **ROE 当期**: {d.get('roe_cur')}% · 上期: {d.get('roe_prev')}% · "
                     f"变动: **{d.get('roe_change_pp')}pp**")
        lines.append(f"- **判定**: `{d.get('verdict')}`")
        attr = d.get("attribution_pp") or {}
        if attr:
            lines.append("")
            lines.append("**ROE 变动归因（百分点）**：")
            for k, v in sorted(attr.items(), key=lambda x: -abs(x[1])):
                arrow = "↑" if v > 0 else "↓"
                lines.append(f"- {k}: {arrow} {v}pp")
    lines.append("")

    # 3.2 TTM（4Q vs 同比 4Q）— 拐点更敏感
    lines.append("### 3.2 TTM 视角（trailing 4Q vs 同比 4Q）")
    if d_ttm.get("error"):
        lines.append(f"⚠️ {d_ttm['error']}")
    else:
        lines.append(f"- **TTM 截止**: {d_ttm.get('period_cur')} · "
                     f"对照 {d_ttm.get('period_prev')}")
        lines.append(f"- **ROE TTM 当期**: {d_ttm.get('roe_cur_ttm_pct')}% · "
                     f"同比 TTM: {d_ttm.get('roe_prev_ttm_pct')}% · "
                     f"变动: **{d_ttm.get('roe_change_pp')}pp**")
        lines.append(f"- **判定**: `{d_ttm.get('verdict')}`")
        # 拐点信号：TTM 变动 vs 年度变动差异
        if (d.get("roe_change_pp") is not None
                and d_ttm.get("roe_change_pp") is not None):
            diff = d_ttm["roe_change_pp"] - d["roe_change_pp"]
            if abs(diff) > 5:
                arrow = "↑" if diff > 0 else "↓"
                lines.append(f"- ⚠️ **拐点信号**: TTM 变动比年度{arrow}{abs(diff):.1f}pp，"
                             f"动量{'加速' if diff > 0 else '减速'}（年报后已发生变化）")
        attr = d_ttm.get("attribution_pp") or {}
        if attr:
            lines.append("")
            lines.append("**TTM ROE 变动归因（百分点）**：")
            for k, v in sorted(attr.items(), key=lambda x: -abs(x[1])):
                arrow = "↑" if v > 0 else "↓"
                lines.append(f"- {k}: {arrow} {v}pp")
    lines.append("")

    # Beneish
    b = deep.get("beneish") or {}
    lines.append("## 四、Beneish M-Score（财务造假识别）")
    lines.append("")
    if b.get("error"):
        lines.append(f"⚠️ {b['error']}")
    else:
        lines.append(f"- **M-Score (raw)**: {b.get('m_score')}")
        if b.get("high_growth_caveat"):
            lines.append(f"- **M-Score (growth-adjusted)**: **{b.get('m_score_adjusted')}** "
                         f"→ 风险等级 **{(b.get('risk_level') or '?').upper()}**")
            lines.append(f"- ⚠️ 高增长公司（SGI > 1.5）原始 M-Score 有假阳性倾向；"
                         f"下游使用 `m_score_adjusted` + `risk_level` 字段，不要用 raw M")
        else:
            lines.append(f"- **风险等级**: **{(b.get('risk_level') or '?').upper()}**")
        lines.append(f"- **判定**: {b.get('verdict')}")
        v = b.get("variables") or {}
        lines.append(f"- 8 变量: DSRI={v.get('DSRI')} · GMI={v.get('GMI')} · "
                     f"SGI={v.get('SGI')} · TATA={v.get('TATA')}")
    lines.append("")

    # Altman
    a = deep.get("altman") or {}
    lines.append("## 五、Altman Z-Score（破产预警）")
    lines.append("")
    if a.get("error"):
        lines.append(f"⚠️ {a['error']}")
    else:
        lines.append(f"- **Z-Score**: {a.get('z_score')} → {a.get('verdict')}")
    lines.append("")

    # 盈利质量
    q = deep.get("quality") or {}
    lines.append("## 六、盈利质量 8 项")
    lines.append("")
    if q.get("error"):
        lines.append(f"⚠️ {q['error']}")
    else:
        lines.append(f"- **综合质量分**: **{q.get('quality_score')}/100**")
        lines.append("")
        for m in q.get("metrics", []):
            v = (m.get("value_pp") if "value_pp" in m else
                 m.get("value_pct") if "value_pct" in m else
                 m.get("value", "—"))
            lines.append(f"- {m['verdict']} **{m['name']}**: {v} — *{m.get('note', '')}*")
    lines.append("")

    # 季度财务 Trend（动态视角 — 静态 cur vs prev 看不出方向）
    if trends and not trends.get("error"):
        lines.append("## 七、季度财务 Trend（8 季）")
        lines.append("")
        lines.append(f"对齐 {trends.get('n_quarters')} 季：{trends.get('periods', [])[-1]} → {trends.get('periods', [])[0]}")
        lines.append("")
        emoji_map = {"improving": "🟢↑", "deteriorating": "🔴↓",
                     "stable": "🟡→", "insufficient_data": "⚪—"}
        # 表头：指标 | 趋势 | 季度列
        periods = trends.get("periods") or []
        headers = ["指标", "趋势", "最新"] + [p[5:] for p in periods[1:]]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join(["---"] * len(headers)) + "|")
        labels = {
            "gross_margin_pct": "毛利率%",
            "operating_margin_pct": "营业利润率%",
            "net_margin_pct": "净利率%",
            "revenue_yoy_pct": "营收 YoY%",
            "revenue_qoq_pct": "营收 QoQ%",
            "dso_days": "应收周转天数",
            "dio_days": "存货周转天数",
            "accruals_to_ta_pct": "应计/资产% (Sloan)",
            "cfo_to_ni": "CFO/NI",
            "sbc_pct_revenue": "SBC/营收%",
        }
        for key, label in labels.items():
            m = (trends.get("metrics") or {}).get(key) or {}
            vals = m.get("values") or []
            trend_emoji = emoji_map.get(m.get("trend"), "?")
            cells = [str(v) if v is not None else "—" for v in vals]
            row = [label, trend_emoji] + cells
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
        lines.append("- 趋势判定：序列前一半均值 vs 后一半均值，相对变化 >5% 标改善/恶化")
        lines.append("- 反向指标（应收/存货周转天数、应计/资产、SBC/营收）改善 = 数值下降")
        lines.append("")

    # 同业对标
    lines.append("## 八、同业横向对标")
    lines.append("")
    if pc.get("error"):
        lines.append(f"⚠️ {pc['error']}")
    else:
        scope = pc.get("peer_scope") or "?"
        # 三级回退口径告警：industry 真同业最可信；sector_fallback 次之；fmp_default_marketcap 仅市值匹配，不可全信
        if scope.startswith("same_industry"):
            scope_marker = "🟢 真同业（industry 匹配）"
        elif "sector_fallback" in scope:
            scope_marker = "🟡 行业内同业不足，已用 sector 回退凑数"
        elif "fmp_default" in scope:
            scope_marker = "🔴 仅按市值匹配，未必是业务可比公司 — 分位结论参考即可"
        else:
            scope_marker = scope
        lines.append(f"- **对标同业** ({pc.get('n_peers')} 家): "
                     f"{', '.join(pc.get('peers') or [])}")
        lines.append(f"- **同业口径**: {scope_marker} `(scope={scope})`")
        lines.append(f"- **综合分位**: **{pc.get('composite_percentile')}%** → "
                     f"{pc.get('verdict')}")
        lines.append("")
        lines.append("| 指标 | 本公司 | 同业中位 | 分位 |")
        lines.append("|---|---|---|---|")
        for metric, label in peer_compare.METRIC_LABELS.items():
            rk = (pc.get("rankings") or {}).get(metric) or {}
            tv = rk.get("target_value")
            med = rk.get("peer_median")
            pct = rk.get("percentile_better")
            tv_s = f"{tv:.2f}" if tv is not None else "—"
            med_s = f"{med:.2f}" if med is not None else "—"
            pct_s = f"{pct}%" if pct is not None else "—"
            lines.append(f"| {label} | {tv_s} | {med_s} | {pct_s} |")
    lines.append("")

    # 财报历史
    eh = r.get("earnings_history") or []
    if eh:
        lines.append("## 九、近 5 季财报回顾")
        lines.append("")
        lines.append("| 财报日 | EPS 实际 | EPS 预期 | EPS 超预期 |")
        lines.append("|---|---|---|---|")
        for e in eh[:5]:
            surprise = e.get("surprise")
            srp = f"{surprise:.2f}" if surprise is not None else "—"
            lines.append(f"| {e.get('date')} | {e.get('eps_actual')} | "
                         f"{e.get('eps_estimated')} | {srp} |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*Generated by StockAssistant fundamental_report v2 "
                 f"(forward valuation + 8Q trend + self-built DCF) · "
                 f"{r.get('generated_at')}*")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="个股基本面深度报告（端到端）")
    parser.add_argument("ticker", help="股票代码 e.g. NVDA")
    parser.add_argument("--peers", nargs="+", help="自定义同业列表")
    parser.add_argument("--max-peers", type=int, default=6)
    parser.add_argument("--json", help="保存 JSON 路径")
    parser.add_argument("--md", help="保存 Markdown 路径")
    parser.add_argument("--quiet", action="store_true", help="不打印到终端")
    args = parser.parse_args()

    print(f"📥 拉取 {args.ticker} 数据中...", flush=True)
    r = build_report(args.ticker, peers=args.peers, max_peers=args.max_peers)
    if r.get("error"):
        print(f"❌ {r['error']}")
        return 1

    md = to_markdown(r)
    if not args.quiet:
        print(md)

    if args.json:
        Path(args.json).write_text(json.dumps(r, indent=2, ensure_ascii=False))
        print(f"\n💾 JSON 已保存: {args.json}")
    if args.md:
        Path(args.md).write_text(md)
        print(f"💾 Markdown 已保存: {args.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
