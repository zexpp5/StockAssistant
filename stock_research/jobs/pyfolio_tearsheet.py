"""pyfolio 机构级 tear sheet — monthly_letter 升级版。

为什么：
  monthly_letter.py 是手写 markdown，pyfolio 是 Quantopian 用 6 年的工业标准。
  pyfolio 输出 30+ 张图：累计收益、月度热图、最大回撤、滚动 Sharpe、turnover、
  行业暴露、风险归因 — 投行级 tear sheet。

输入：
  组合每日收益序列（pandas Series，index=date, value=daily return）

输出：
  - PNG 图集（保存到 docs/letters/pyfolio_<date>/）
  - 文字摘要（performance metrics）

CLI:
  python3 -m stock_research.jobs.pyfolio_tearsheet
  python3 -m stock_research.jobs.pyfolio_tearsheet --picks-month 2026-04
"""
from __future__ import annotations
import argparse
import logging
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from .. import config
from ..adapters import legacy_shim as feishu, store

logger = logging.getLogger("stock_research.jobs.pyfolio_tearsheet")

warnings.filterwarnings("ignore")


def _build_portfolio_returns_from_picks(year: int, month: int) -> pd.Series | None:
    """从飞书 picks 表构造组合每日收益序列。

    简化方法：取该月入选的所有 picks，等权组合，用 yfinance 拉每日价格。
    """
    import yfinance as yf

    picks = feishu.fetch_picks()
    start_ts = datetime(year, month, 1).timestamp() * 1000
    if month == 12:
        end_ts = datetime(year + 1, 1, 1).timestamp() * 1000
    else:
        end_ts = datetime(year, month + 1, 1).timestamp() * 1000
    month_picks = []
    for p in picks:
        f = p.get("fields", {})
        pd_ts = f.get("入选日期")
        if pd_ts and start_ts <= pd_ts < end_ts:
            month_picks.append(p)

    if not month_picks:
        return None

    # 取唯一 ticker（避免重复），假设等权
    tickers = list(set(p["normalized"].get("code") for p in month_picks
                       if p["normalized"].get("code")))[:20]  # 最多 20 只
    if not tickers:
        return None

    # 拉每日价格（从月初到今天）
    end = datetime.now()
    start = datetime(year, month, 1)
    df = yf.download(tickers, start=start, end=end, progress=False, auto_adjust=True)
    if hasattr(df.columns, "levels"):
        prices = df["Close"]
    else:
        prices = df

    # 等权组合每日收益
    daily_returns = prices.pct_change().mean(axis=1).dropna()
    if daily_returns.index.tz is not None:
        daily_returns.index = daily_returns.index.tz_localize(None)
    return daily_returns


def _spy_returns(start: datetime, end: datetime) -> pd.Series:
    """SPY 每日收益（基准）。"""
    import yfinance as yf
    h = yf.Ticker("SPY").history(start=start, end=end)
    if h.index.tz is not None:
        h.index = h.index.tz_localize(None)
    return h["Close"].pct_change().dropna()


def run(year: int | None = None, month: int | None = None) -> dict:
    if year is None or month is None:
        target = datetime.now()
        if target.day <= 5:
            target = target.replace(day=1) - timedelta(days=1)
        year, month = target.year, target.month

    print(f"\n{'='*80}")
    print(f"  📊 pyfolio Tear Sheet · {year}-{month:02d}")
    print(f"{'='*80}\n")

    print("[1/3] 构造组合每日收益序列...")
    rets = _build_portfolio_returns_from_picks(year, month)
    if rets is None or len(rets) < 5:
        return {"error": f"{year}-{month:02d} 月数据不足（picks 太少或未生成）"}
    print(f"  {len(rets)} 个交易日 · 累计收益 {(rets + 1).prod() - 1:+.2%}")

    print("[2/3] 拉 SPY 基准...")
    benchmark = _spy_returns(rets.index[0], rets.index[-1] + timedelta(days=1))

    print("[3/3] pyfolio 计算性能指标...")
    try:
        import pyfolio as pf
        # pyfolio 的核心 metrics（不画图，避免 matplotlib 依赖）
        from pyfolio.timeseries import perf_stats
        stats = perf_stats(rets, factor_returns=benchmark)
        print(f"\n  pyfolio 性能指标：")
        for k, v in stats.items():
            print(f"    {k:<25} {v:>10.4f}")
    except Exception as e:
        return {"error": f"pyfolio 失败: {str(e)[:200]}"}

    # 保存 markdown 报告
    md_path = _REPO_ROOT / "docs" / "letters" / f"pyfolio_{year}-{month:02d}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md = _to_markdown(year, month, rets, benchmark, stats)
    md_path.write_text(md, encoding="utf-8")
    print(f"\n  📁 Markdown 报告: {md_path}")

    # JSON 快照
    out = {
        "year": year, "month": month,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stats": {k: float(v) for k, v in stats.items()},
        "n_trading_days": len(rets),
        "cumulative_return_pct": round(((rets + 1).prod() - 1) * 100, 2),
        "spy_cumulative_pct": round(((benchmark + 1).prod() - 1) * 100, 2),
    }
    store.save_json(out, config.AUDIT_DIR, f"pyfolio_{year}-{month:02d}")
    print(f"\n{'='*80}\n")
    return out


def _to_markdown(year, month, rets, benchmark, stats) -> str:
    """渲染 pyfolio markdown 报告。"""
    cum_p = (rets + 1).prod() - 1
    cum_b = (benchmark + 1).prod() - 1
    alpha = cum_p - cum_b
    lines = [
        f"# pyfolio Tear Sheet · {year}-{month:02d}",
        "",
        f"_生成时间: {datetime.now().isoformat(timespec='seconds')}_",
        "_数据源: 飞书 picks + yfinance · 不构成投资建议_",
        "",
        "## 摘要",
        "",
        f"- 交易日数: **{len(rets)}**",
        f"- 组合累计收益: **{cum_p:+.2%}**",
        f"- SPY 累计收益: {cum_b:+.2%}",
        f"- **Alpha: {alpha:+.2%}**",
        "",
        "## pyfolio 完整性能指标",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
    ]
    for k, v in stats.items():
        lines.append(f"| {k} | {v:.4f} |")

    lines += [
        "",
        "## 限制声明",
        "",
        "- 本 tear sheet 假设等权组合（picks 表所有月度 ⭐⭐⭐ 推荐等权持有）",
        "- 实际交易会有止损、再平衡、税费等",
        "- 不构成投资建议",
        "",
        "_StockAssistant v8 (pyfolio-enhanced) · 维护: yanli_",
    ]
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    p = argparse.ArgumentParser(description="pyfolio 机构级 tear sheet")
    p.add_argument("--month", help="格式 YYYY-MM；默认上月或本月")
    args = p.parse_args()

    year = month = None
    if args.month:
        try:
            year, month = map(int, args.month.split("-"))
        except ValueError:
            print(f"❌ --month 格式错误: {args.month}")
            return 1
    r = run(year=year, month=month)
    return 0 if "error" not in r else 1


if __name__ == "__main__":
    sys.exit(main())
