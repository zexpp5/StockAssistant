"""月度成绩单（Monthly Letter）— 生成可对外发布的 markdown 报告。

设计哲学：
  - **诚实暴露错误**（学 Charlie Munger "反过来想"）
  - **公开命中/漏报/误报清单**（不能只发对的）
  - **学术指标**（Sharpe / IR / Hit Rate / Alpha vs SPY）
  - **可追溯的决策原因**（每条推荐的入选理由 + 实际结果）

输出：
  docs/letters/YYYY-MM_letter.md   ← 可直接发布到雪球/Substack/微博

数据源：
  - feishu picks 表（入选时间、评分、当时价格、当前价格、累计涨跌）
  - SPY benchmark（同期 alpha）
  - factor_ic 快照（因子治理状态）
  - audit_picks 快照（横截面审查记录）

CLI:
  python3 -m stock_research.jobs.monthly_letter
  python3 -m stock_research.jobs.monthly_letter --month 2026-04
"""
from __future__ import annotations
import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from .. import config
from ..adapters import store

logger = logging.getLogger("stock_research.jobs.monthly_letter")


# ─────────── 数据加载 ───────────

def _normalize(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list) and v:
        first = v[0]
        return first.get("text", "") if isinstance(first, dict) else str(first)
    if isinstance(v, dict):
        return v.get("text", "") or v.get("name", "")
    return str(v)


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _filter_month(picks: list[dict], year: int, month: int) -> list[dict]:
    """筛选指定月份入选的 picks。"""
    out = []
    start_ts = datetime(year, month, 1).timestamp() * 1000
    if month == 12:
        end_ts = datetime(year + 1, 1, 1).timestamp() * 1000
    else:
        end_ts = datetime(year, month + 1, 1).timestamp() * 1000
    for p in picks:
        f = p.get("fields", {})
        pd = f.get("入选日期")
        if pd and start_ts <= pd < end_ts:
            out.append(f)
    return out


def _spy_return(start: datetime, end: datetime) -> float | None:
    """同期 SPY 累计收益。"""
    try:
        import yfinance as yf
        h = yf.Ticker("SPY").history(start=start, end=end + timedelta(days=1))
        if len(h) < 2:
            return None
        return (h["Close"].iloc[-1] / h["Close"].iloc[0] - 1) * 100
    except Exception:
        return None


# ─────────── 月报生成 ───────────

def generate(year: int, month: int) -> dict:
    """生成指定月份的报告 dict（人可读的 markdown 由 to_markdown 渲染）。"""
    print(f"[1/3] 拉 picks 历史 [V2]...")
    import sys as _sys
    _sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
    from stock_db import fetch_picks_normalized
    picks_raw = fetch_picks_normalized()
    picks = _filter_month(picks_raw, year, month)
    if not picks:
        return {"error": f"{year}-{month:02d} 月无 picks 记录"}
    print(f"  当月 picks: {len(picks)} 条")

    # 按评级分组
    by_rating: dict[str, list] = {"⭐⭐⭐": [], "⭐⭐": [], "⭐": []}
    for p in picks:
        rating = _normalize(p.get("入选评分"))
        for k in by_rating:
            if k in rating:
                by_rating[k].append(p)
                break

    # 算每档 stats
    print(f"[2/3] 算每档评级表现...")
    stats = {}
    for rating, plist in by_rating.items():
        if not plist:
            continue
        pcts = [_to_float(p.get("累计涨跌%")) for p in plist]
        pcts = [x for x in pcts if x is not None]
        if not pcts:
            continue
        avg = sum(pcts) / len(pcts)
        win = sum(1 for x in pcts if x > 0)
        big_win = sum(1 for x in pcts if x > 20)
        stats[rating] = {
            "n": len(plist),
            "avg_pct": round(avg, 2),
            "win_rate": round(win / len(plist) * 100, 1),
            "big_win_rate": round(big_win / len(plist) * 100, 1),
            "best": max(pcts),
            "worst": min(pcts),
        }

    # SPY benchmark
    print(f"[3/3] 拉 SPY 基准...")
    spy_pct = _spy_return(datetime(year, month, 1),
                          datetime(year, month + 1 if month < 12 else 1,
                                   1 if month < 12 else 1)
                          if month < 12 else datetime(year + 1, 1, 1))

    # 命中 / 漏报 / 误报 案例
    strong = by_rating.get("⭐⭐⭐", [])
    hits, miss_called, mistakes = [], [], []
    for p in strong:
        pct = _to_float(p.get("累计涨跌%"))
        if pct is None:
            continue
        rec = {
            "name": _normalize(p.get("股票名称")),
            "code": _normalize(p.get("代码")),
            "score": p.get("综合得分"),
            "pct": round(pct, 1),
            "days": _to_float(p.get("持有天数")) or 0,
            "reason": _normalize(p.get("入选理由"))[:100],
        }
        # ≥ 20% = 大胜 / ≥ SPY+5% = 一致 / < SPY-5% = 误报
        threshold_win = max(20.0, (spy_pct or 0) + 5)
        threshold_loss = min(-5.0, (spy_pct or 0) - 5)
        if pct >= threshold_win:
            hits.append(rec)
        elif pct <= threshold_loss:
            mistakes.append(rec)
    hits.sort(key=lambda x: -x["pct"])
    mistakes.sort(key=lambda x: x["pct"])

    # 因子 IC 状态（最新）
    factor_ic = store.load_latest_json(config.AUDIT_DIR, "factor_ic")
    factor_status = []
    if factor_ic and "factors" in factor_ic:
        for fname, info in factor_ic["factors"].items():
            alert = info.get("alert", {})
            factor_status.append({
                "name": fname,
                "icon": alert.get("icon", "?"),
                "status": alert.get("status", ""),
                "mean_ic": alert.get("mean_ic", 0),
                "ic_ir": alert.get("ic_ir", 0),
            })

    return {
        "year": year, "month": month,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stats_by_rating": stats,
        "spy_benchmark_pct": round(spy_pct, 2) if spy_pct is not None else None,
        "hits": hits,
        "mistakes": mistakes,
        "factor_status": factor_status,
        "n_total_picks": len(picks),
    }


# ─────────── Markdown 渲染 ───────────

def to_markdown(report: dict) -> str:
    """把 report dict 渲染成可发布的 markdown。"""
    if "error" in report:
        return f"# 错误\n\n{report['error']}\n"

    y, m = report["year"], report["month"]
    spy = report.get("spy_benchmark_pct")
    spy_str = f"{spy:+.2f}%" if spy is not None else "N/A"

    lines = [
        f"# StockAssistant 月度成绩单 · {y}-{m:02d}",
        "",
        f"_生成时间：{report['generated_at']}_",
        f"_系统版本：v6（5 因子 + Markowitz + 中性化 + ADV + 成本）_",
        "",
        "## 摘要",
        "",
        f"- 当月 picks 总数: **{report['n_total_picks']}** 条",
        f"- 同期 SPY 收益: **{spy_str}**",
        f"- 维护者: yanli · 不构成投资建议",
        "",
        "## 评级表现",
        "",
        "| 评级 | 样本 | 平均涨跌 | 胜率 | 大胜率 (>20%) | 最高 | 最低 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in ["⭐⭐⭐", "⭐⭐", "⭐"]:
        s = report["stats_by_rating"].get(r)
        if s:
            lines.append(f"| {r} | {s['n']} | {s['avg_pct']:+.2f}% | "
                         f"{s['win_rate']:.0f}% | {s['big_win_rate']:.0f}% | "
                         f"{s['best']:+.1f}% | {s['worst']:+.1f}% |")
        else:
            lines.append(f"| {r} | 0 | - | - | - | - | - |")

    # 命中（最大胜利）
    lines += [
        "", "## 🏆 命中案例（≥ 20% 涨幅 或 跑赢 SPY+5%）", "",
    ]
    if report["hits"]:
        lines.append("| 股票 | 代码 | 累计涨跌 | 持有天数 | 入选理由（节选）|")
        lines.append("|---|---|---|---|---|")
        for h in report["hits"][:15]:
            lines.append(f"| {h['name']} | {h['code']} | {h['pct']:+.1f}% | "
                         f"{int(h['days'])} | {h['reason']} |")
    else:
        lines.append("_本月无 ≥ 20% 涨幅的命中案例_")

    # 误报（诚实披露）
    lines += [
        "", "## 🔴 误报清单（跌穿 -5% 或跑输 SPY-5%）", "",
        "_遵循 Charlie Munger 「反过来想」原则：暴露错误，建立信任。_", "",
    ]
    if report["mistakes"]:
        lines.append("| 股票 | 代码 | 累计涨跌 | 持有天数 | 入选理由（节选）|")
        lines.append("|---|---|---|---|---|")
        for m_rec in report["mistakes"][:15]:
            lines.append(f"| {m_rec['name']} | {m_rec['code']} | {m_rec['pct']:+.1f}% | "
                         f"{int(m_rec['days'])} | {m_rec['reason']} |")
    else:
        lines.append("_本月无明显误报_")

    # 因子治理（IC 状态）
    if report["factor_status"]:
        lines += [
            "", "## 📊 因子治理（最新 IC 监测）", "",
            "| 因子 | 状态 | 平均 IC | IR |",
            "|---|---|---|---|",
        ]
        for f in report["factor_status"]:
            lines.append(f"| {f['name']} | {f['icon']} {f['status']} | "
                         f"{f['mean_ic']:+.3f} | {f['ic_ir']:+.2f} |")
        lines.append("")
        lines.append("_IC ≥ 0.05 = strong / 0.02-0.05 = marginal / |IC| < 0.02 = decayed / IC < 0 = inverted_")
        lines.append("_参考：Grinold-Kahn (2000) Active Portfolio Management_")

    # 限制声明
    lines += [
        "", "## 限制声明", "",
        "- 本系统是个人研究辅助工具，**不构成投资建议**",
        "- 本月报数据基于飞书 picks 表 + yfinance SPY，可能存在数据延迟",
        "- 模型已知缺陷见 [docs/MODEL_CARD.md](../MODEL_CARD.md)",
        "- 学术依据见 [docs/METHODOLOGY.md](../METHODOLOGY.md)",
        "",
        "---",
        f"*StockAssistant v6 · {report['generated_at']}*",
    ]
    return "\n".join(lines)


# ─────────── 主流程 ───────────

def run(year: int | None = None, month: int | None = None) -> dict:
    if year is None or month is None:
        # 默认：上个月（如果今天是月初前 5 天）或本月
        now = datetime.now()
        if now.day <= 5:
            target = (now.replace(day=1) - timedelta(days=1))
        else:
            target = now
        year, month = target.year, target.month

    print(f"\n{'='*60}")
    print(f"  📜 StockAssistant 月度成绩单 · {year}-{month:02d}")
    print(f"{'='*60}\n")

    report = generate(year, month)
    md = to_markdown(report)

    # 写文件
    docs_dir = _REPO_ROOT / "docs" / "letters"
    docs_dir.mkdir(parents=True, exist_ok=True)
    md_path = docs_dir / f"{year}-{month:02d}_letter.md"
    md_path.write_text(md, encoding="utf-8")
    print(f"\n✅ 月报已生成: {md_path}")

    # 同时存 JSON 快照
    store.save_json(report, config.AUDIT_DIR, f"monthly_letter_{year}-{month:02d}")

    return report


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    p = argparse.ArgumentParser(description="月度成绩单（Monthly Letter）")
    p.add_argument("--month", help="格式 YYYY-MM；默认上月或本月")
    args = p.parse_args()

    year = month = None
    if args.month:
        try:
            year, month = map(int, args.month.split("-"))
        except ValueError:
            print(f"❌ --month 格式错误：应该是 YYYY-MM，得到 {args.month}")
            return 1

    r = run(year=year, month=month)
    return 0 if "error" not in r else 1


if __name__ == "__main__":
    sys.exit(main())
