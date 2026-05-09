"""OpenBB 综合情报 — 每日产出宏观 + 行业轮动 + 商品 + PCR + 内部人 五合一简报。

由 daily_refresh.sh 触发，输出：
  1. 控制台报告
  2. data/snapshots/audit/openbb_intel_<date>.json
  3. docs/letters/intel_<date>.md（可对外发布的每日简报）

集成的 OpenBB 数据：
  - macro_data.macro_regime()         FRED + yf 宏观 regime
  - sector_etf.get_sector_rotation_signal() 11 GICS ETF 轮动
  - commodity_signals.fetch_commodity_prices() + signal_summary() 5 大商品
  - options_signals.diagnose()        SPY put/call ratio
  - insider_signals.aggregate_watchlist() 内部人交易汇总

CLI:
  python3 -m stock_research.jobs.openbb_intelligence
  python3 -m stock_research.jobs.openbb_intelligence --quick  # 跳过较慢的（PCR）
"""
from __future__ import annotations
import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from .. import config
from ..adapters import store
from ..core import (
    macro_data, sector_etf, commodity_signals,
    options_signals, insider_signals,
)

logger = logging.getLogger("stock_research.jobs.openbb_intelligence")


def run(quick: bool = False) -> dict:
    print(f"\n{'='*80}")
    print(f"  📡 OpenBB 综合情报 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*80}\n")

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date": datetime.now().strftime("%Y-%m-%d"),
    }

    # 1. 宏观
    print("[1/5] 宏观经济 regime（FRED + yf fallback）...")
    try:
        macro = macro_data.macro_regime()
        summary["macro"] = macro
        if macro.get("fed_rate"):
            print(f"  Fed Funds: {macro['fed_rate']:.2f}% · "
                  f"10Y: {macro.get('ten_year_yield', '?')}% · "
                  f"曲线: {macro.get('yield_curve', '?')}% · "
                  f"regime: {macro['regime']}")
        for a in macro.get("alerts", []):
            print(f"  [{a['severity']}] {a['msg'][:120]}")
    except Exception as e:
        print(f"  ❌ {e}")

    # 2. 行业轮动
    print("\n[2/5] GICS 11 行业轮动（过去 60 天）...")
    try:
        rotation = sector_etf.get_sector_rotation_signal(lookback_days=60)
        summary["sector_rotation"] = rotation
        print(f"  🏆 Top 3:")
        for x in rotation["leaders"]:
            print(f"    {x['ticker']:<6} {x['name']:<35} {x['return_pct']:+7.2f}%")
        print(f"  📉 Bottom 3:")
        for x in rotation["laggards"]:
            print(f"    {x['ticker']:<6} {x['name']:<35} {x['return_pct']:+7.2f}%")
    except Exception as e:
        print(f"  ❌ {e}")

    # 3. 商品
    print("\n[3/5] 5 大商品趋势（过去 90 天）...")
    try:
        commod = commodity_signals.fetch_commodity_prices(lookback_days=90)
        com_summary = commodity_signals.signal_summary(commod)
        summary["commodities"] = com_summary
        for r in com_summary["rankings"]:
            arrow = "🟢" if r["cum_return_pct"] > 5 else ("🔴" if r["cum_return_pct"] < -5 else "🟡")
            print(f"  {arrow} {r['ticker']:<5} {r['name']:<35} {r['cum_return_pct']:+7.2f}%")
    except Exception as e:
        print(f"  ❌ {e}")

    # 4. SPY put/call ratio
    if not quick:
        print("\n[4/5] SPY 期权 put/call ratio...")
        try:
            opt = options_signals.diagnose()
            summary["options"] = opt
            sig_vol = opt.get("signal_volume", {})
            sig_oi = opt.get("signal_oi", {})
            print(f"  PCR (volume) = {opt.get('pcr_volume')} · {sig_vol.get('label', '')}")
            print(f"  PCR (OI)     = {opt.get('pcr_oi')} · {sig_oi.get('label', '')}")
            if sig_vol.get("severity") in ("HIGH", "CRITICAL"):
                print(f"  → 行动建议: {sig_vol.get('action')}")
        except Exception as e:
            print(f"  ❌ {e}")
    else:
        print("\n[4/5] SPY put/call ratio: 跳过（--quick 模式）")
        summary["options"] = {"skipped": True}

    # 5. 内部人交易（用已有 enrich snapshot）
    print("\n[5/5] 内部人交易信号汇总（过去 90 天）...")
    try:
        agg = insider_signals.aggregate_watchlist([])
        summary["insider"] = agg
        if "error" in agg:
            print(f"  ⚠️ {agg['error']}")
        else:
            n_buy = len(agg.get("strong_buy", []))
            n_sell = len(agg.get("strong_sell", []))
            print(f"  🟢 强买入 {n_buy} 只 · 🔴 强卖出 {n_sell} 只 · 总样本 {agg['n_total']}")
            if n_buy > 0:
                print(f"  Top 强买入:")
                for r in agg["strong_buy"][:3]:
                    print(f"    + {r['name']} ({r['ticker']}) +${r['net_dollars']/1e6:.1f}M")
            if n_sell > 0:
                print(f"  Top 强卖出:")
                for r in agg["strong_sell"][:3]:
                    print(f"    - {r['name']} ({r['ticker']}) ${r['net_dollars']/1e6:.1f}M")
    except Exception as e:
        print(f"  ❌ {e}")

    # 写快照
    snap = store.save_json(summary, config.AUDIT_DIR, "openbb_intel")
    print(f"\n  📁 JSON 快照: {snap}")

    # 写 markdown 简报
    md = _to_markdown(summary)
    docs_dir = _REPO_ROOT / "docs" / "letters"
    docs_dir.mkdir(parents=True, exist_ok=True)
    md_path = docs_dir / f"intel_{summary['date']}.md"
    md_path.write_text(md, encoding="utf-8")
    print(f"  📁 Markdown 简报: {md_path}")
    print(f"\n{'='*80}\n")
    return summary


def _to_markdown(s: dict) -> str:
    """渲染每日情报简报。"""
    lines = [
        f"# OpenBB 综合情报 · {s['date']}",
        "",
        f"_生成时间：{s['generated_at']}_",
        "_数据：FRED + yfinance + OpenBB SEC + Finnhub_",
        "_不构成投资建议_",
        "",
    ]

    # 1. 宏观
    macro = s.get("macro", {})
    if macro:
        lines += [
            "## 🌐 宏观 Regime",
            "",
            f"- Fed Funds Rate: **{macro.get('fed_rate', '?')}%** "
            f"({macro.get('fed_rate_source', '')})",
            f"- 10Y Treasury: {macro.get('ten_year_yield', '?')}%",
            f"- 收益率曲线: **{macro.get('yield_curve', '?')}%**（< 0 = 衰退预警）",
            f"- 当前 regime: **`{macro.get('regime', '?')}`**",
            "",
        ]
        if macro.get("alerts"):
            lines.append("### 警报")
            for a in macro["alerts"]:
                lines.append(f"- [{a['severity']}] {a['msg']}")
            lines.append("")

    # 2. 行业轮动
    rot = s.get("sector_rotation", {})
    if rot:
        lines += [
            "## 🔄 GICS 11 行业轮动（过去 60 天）",
            "",
            "| 排名 | ETF | 行业 | 收益 |",
            "|---|---|---|---|",
        ]
        for x in rot.get("all_rankings", []):
            lines.append(f"| - | {x['ticker']} | {x['name']} | {x['return_pct']:+.2f}% |")
        lines.append("")

    # 3. 商品
    com = s.get("commodities", {})
    if com:
        lines += [
            "## 🛢 5 大商品趋势（过去 90 天）",
            "",
            "| 商品 | ETF | 收益 | 受益 watchlist |",
            "|---|---|---|---|",
        ]
        for x in com.get("rankings", []):
            beneficiaries = ", ".join(x.get("beneficiaries", []))
            lines.append(f"| {x['name']} | {x['ticker']} | {x['cum_return_pct']:+.2f}% | {beneficiaries} |")
        lines.append("")

    # 4. PCR
    opt = s.get("options", {})
    if opt and not opt.get("skipped"):
        sig_vol = opt.get("signal_volume", {})
        lines += [
            "## 📊 SPY put/call ratio",
            "",
            f"- PCR (volume) = **{opt.get('pcr_volume')}** · {sig_vol.get('label', '')}",
            f"- PCR (OI) = **{opt.get('pcr_oi')}**",
            f"- 行动建议: {sig_vol.get('action', '—')}",
            "",
        ]

    # 5. 内部人
    ins = s.get("insider", {})
    if ins and "error" not in ins:
        lines += [
            "## 👤 内部人交易（过去 90 天）",
            "",
            f"样本 {ins.get('n_total', 0)} 只 · "
            f"🟢 强买入 {len(ins.get('strong_buy', []))} 只 · "
            f"🔴 强卖出 {len(ins.get('strong_sell', []))} 只",
            "",
        ]
        if ins.get("strong_buy"):
            lines.append("### 🟢 高管强买入")
            for r in ins["strong_buy"]:
                lines.append(f"- **{r['name']}** ({r['ticker']}) "
                             f"+${r['net_dollars']/1e6:.1f}M · {r['unique_buyers']} 高管增持")
            lines.append("")
        if ins.get("strong_sell"):
            lines.append("### 🔴 高管强卖出")
            for r in ins["strong_sell"]:
                lines.append(f"- **{r['name']}** ({r['ticker']}) "
                             f"${r['net_dollars']/1e6:.1f}M · {r['unique_sellers']} 高管减持")
            lines.append("")

    lines += [
        "---",
        "",
        "_StockAssistant v7.5 (OpenBB-enhanced) · 不构成投资建议_",
    ]
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    p = argparse.ArgumentParser(description="OpenBB 综合情报")
    p.add_argument("--quick", action="store_true", help="跳过 PCR（较慢）")
    args = p.parse_args()
    run(quick=args.quick)
    return 0


if __name__ == "__main__":
    sys.exit(main())
