"""月度 Rolling Walk-Forward 回测：用真实"训练→验证→实盘"循环模拟系统在过去如何运行。

为什么要做：
  现有 vectorbt_backtest.py 是整体回测（一把跑 5 年）。问题：
    - 回测期内"用未来知识训练因子" → 隐含 lookahead bias
    - 看不到"系统在某个 regime 下会失效多久"
    - 用户无法回答："如果 1 月底就开始用这套系统，1-12 月各月收益是多少？"

Rolling walk-forward 解决这个：
  每月 1 号：
    1. 用过去 N 个月的数据**训练/校准**因子权重（如 IC weighting）
    2. 用当月**预测/选股**生成组合
    3. 持有当月，结算月度收益
    4. 月末把这个月的真实数据并入训练集，继续滚动
  最终输出：12 个月的 OOS（out-of-sample）月度收益序列 + 整体 Sharpe / DD

学术依据：
  - Bailey & Lopez de Prado (2014) JPM "Pseudo-Mathematics and Financial Charlatanism"
    指出：单次回测的 Sharpe 严重高估，walk-forward 是减少 backtest overfit 的金标准
  - Hastie et al. (2009) "ESL" §7.10：时序数据的交叉验证必须按时间顺序滑动

实现简化：
  - 不用 ML 模型重训练，只用"过去 N 月 IC 加权" 作为月度因子组合权重
  - 选股池：固定 watchlist（避免 universe 漂移）
  - 持仓周期：1 个月，等权 Top K
  - 出口：vectorbt 算月度 PnL

CLI:
  python -m stock_research.jobs.walk_forward_backtest --start 2025-05 --end 2026-04 --top-k 5
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ───────────── 数据结构 ─────────────

@dataclass
class MonthResult:
    """单月 OOS 结果。"""
    month: str                           # "2025-05"
    selected: list[str]                  # 当月持仓 (top-k)
    factor_weights: dict[str, float]     # 因子组合权重（用过去 N 月校准）
    monthly_return: float                # 当月组合等权收益（百分比）
    benchmark_return: float              # 基准（如沪深 300 / 标普 500）
    excess_return: float                 # 超额

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WalkForwardResult:
    """整体 walk-forward 结果。"""
    start_month: str
    end_month: str
    universe: list[str]
    benchmark: str
    train_lookback_months: int
    top_k: int
    months: list[MonthResult]

    @property
    def total_excess_return(self) -> float:
        cum = 1.0
        cum_b = 1.0
        for m in self.months:
            cum *= (1 + m.monthly_return / 100)
            cum_b *= (1 + m.benchmark_return / 100)
        return (cum - cum_b) * 100

    @property
    def sharpe(self) -> float:
        rets = [m.monthly_return for m in self.months]
        if len(rets) < 2:
            return 0.0
        s = pd.Series(rets)
        if s.std() == 0:
            return 0.0
        return (s.mean() / s.std()) * (12 ** 0.5)  # 月度 → 年化

    @property
    def max_drawdown(self) -> float:
        cum = []
        v = 1.0
        for m in self.months:
            v *= (1 + m.monthly_return / 100)
            cum.append(v)
        if not cum:
            return 0.0
        peak = cum[0]
        max_dd = 0.0
        for v in cum:
            peak = max(peak, v)
            dd = (v - peak) / peak
            if dd < max_dd:
                max_dd = dd
        return max_dd * 100

    def to_dict(self) -> dict:
        return {
            "start_month": self.start_month,
            "end_month": self.end_month,
            "universe": self.universe,
            "benchmark": self.benchmark,
            "train_lookback_months": self.train_lookback_months,
            "top_k": self.top_k,
            "summary": {
                "total_excess_return_pct": round(self.total_excess_return, 2),
                "sharpe_annual": round(self.sharpe, 2),
                "max_drawdown_pct": round(self.max_drawdown, 2),
                "n_months": len(self.months),
            },
            "months": [m.to_dict() for m in self.months],
        }


# ───────────── 价格数据 ─────────────

def fetch_prices(tickers: list[str], start: date, end: date,
                 source: str = "yfinance") -> pd.DataFrame:
    """拉取价格序列（DataFrame，行=日期，列=ticker）。"""
    if source == "yfinance":
        import yfinance as yf
        df = yf.download(tickers, start=start.strftime("%Y-%m-%d"),
                         end=end.strftime("%Y-%m-%d"),
                         progress=False, auto_adjust=True)
        if hasattr(df.columns, "levels"):
            return df["Close"]
        return df
    raise ValueError(f"unsupported source: {source}")


def monthly_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """日价 → 月度收益（百分比）。"""
    monthly = prices.resample("ME").last()
    return monthly.pct_change() * 100


# ───────────── 因子校准（简化：滚动 N 月 IC 加权）─────────────

def calibrate_factor_weights_simple(
    monthly_rets: pd.DataFrame,
    lookback_months: int = 12,
    end_month: pd.Timestamp = None
) -> pd.Series:
    """简化版：用过去 N 月动量 + 反转作为唯一因子，按各自 IC 加权。

    返回 ranking-friendly 因子分数（每个 ticker 一个数）：
      score = w_mom × mom_3m_rank + w_rev × rev_1m_rank
    """
    if end_month is None:
        end_month = monthly_rets.index[-1]

    # 取过去 lookback_months 的窗口
    window = monthly_rets.loc[:end_month].tail(lookback_months + 3)
    if len(window) < 4:
        # 数据太少，等权
        latest = monthly_rets.loc[:end_month].iloc[-1] if not monthly_rets.empty else None
        if latest is None:
            return pd.Series(dtype=float)
        return pd.Series(0.5, index=latest.index)

    # 动量 (3 月) 与 反转 (1 月)
    mom_3m = window.rolling(3).sum().iloc[-1]
    rev_1m = -window.iloc[-1]   # 反转 = 上月负收益

    # 简单等权（实际可替换为基于历史 IC 的加权）
    score = 0.5 * mom_3m.rank(pct=True) + 0.5 * rev_1m.rank(pct=True)
    return score


def select_top_k(scores: pd.Series, k: int) -> list[str]:
    """从因子分数选出 top-k。"""
    if scores.empty:
        return []
    return scores.dropna().sort_values(ascending=False).head(k).index.tolist()


# ───────────── 主回测循环 ─────────────

def walk_forward(
    universe: list[str],
    start_month: str,                # "2025-05"
    end_month: str,                  # "2026-04"
    benchmark: str = "SPY",
    train_lookback_months: int = 12,
    top_k: int = 5,
    price_source: str = "yfinance",
) -> WalkForwardResult:
    """主入口。"""
    start_dt = pd.to_datetime(start_month + "-01")
    end_dt = (pd.to_datetime(end_month + "-01") + pd.offsets.MonthEnd(0)).date()

    # 拉数据：训练 lookback + 测试期
    history_start = (start_dt - pd.DateOffset(months=train_lookback_months + 2)).date()

    logger.info("拉取价格 %s ~ %s ...", history_start, end_dt)
    all_tickers = list(set(universe + [benchmark]))
    prices = fetch_prices(all_tickers, history_start, end_dt, source=price_source)
    if prices.empty:
        raise RuntimeError("价格数据为空")
    prices = prices.dropna(how="all", axis=1).ffill()

    bench_prices = prices[benchmark] if benchmark in prices.columns else None
    universe_prices = prices[[c for c in universe if c in prices.columns]]

    monthly = monthly_returns(universe_prices)
    monthly_bench = bench_prices.resample("ME").last().pct_change() * 100 if bench_prices is not None else None

    # 滚动循环：每月 1 号选股 → 持有当月
    months_in_test = pd.date_range(start_dt, end_dt, freq="MS")
    results: list[MonthResult] = []

    for month_start in months_in_test:
        month_end = (month_start + pd.offsets.MonthEnd(0))
        month_label = month_start.strftime("%Y-%m")

        # 1. 用截至上月末的数据校准因子
        prev_month_end = (month_start - pd.Timedelta(days=1)).normalize()
        scores = calibrate_factor_weights_simple(
            monthly, lookback_months=train_lookback_months,
            end_month=prev_month_end,
        )
        if scores.empty:
            logger.warning("月份 %s: 因子分为空，跳过", month_label)
            continue

        # 2. 选 top-k
        selected = select_top_k(scores, top_k)
        if not selected:
            continue

        # 3. 持有当月：等权
        try:
            this_month_ret = monthly.loc[month_end, selected].mean()
        except KeyError:
            logger.warning("月份 %s: 无法取月度收益", month_label)
            continue

        bench_ret = float(monthly_bench.loc[month_end]) if (monthly_bench is not None and month_end in monthly_bench.index) else 0.0

        results.append(MonthResult(
            month=month_label,
            selected=selected,
            factor_weights={"mom_3m": 0.5, "rev_1m": 0.5},
            monthly_return=round(float(this_month_ret), 4),
            benchmark_return=round(bench_ret, 4),
            excess_return=round(float(this_month_ret) - bench_ret, 4),
        ))

    return WalkForwardResult(
        start_month=start_month,
        end_month=end_month,
        universe=list(universe_prices.columns),
        benchmark=benchmark,
        train_lookback_months=train_lookback_months,
        top_k=top_k,
        months=results,
    )


# ───────────── 报告 ─────────────

def format_report(r: WalkForwardResult) -> str:
    lines = []
    lines.append(f"# Walk-Forward 回测 — {r.start_month} ~ {r.end_month}")
    lines.append(f"  Universe ({len(r.universe)}): {', '.join(r.universe[:8])}{'...' if len(r.universe)>8 else ''}")
    lines.append(f"  Benchmark: {r.benchmark}")
    lines.append(f"  训练 lookback: {r.train_lookback_months} 月 | top-k = {r.top_k}")
    lines.append(f"")
    lines.append(f"  📊 摘要：")
    lines.append(f"    总超额收益: {r.total_excess_return:+.2f}%")
    lines.append(f"    年化 Sharpe:  {r.sharpe:.2f}")
    lines.append(f"    最大回撤:    {r.max_drawdown:.2f}%")
    lines.append(f"    完成月份:    {len(r.months)}")
    lines.append(f"")
    lines.append(f"  📅 月度明细：")
    lines.append(f"  {'月份':<10}{'组合':>9}{'基准':>9}{'超额':>9}  持仓")
    for m in r.months:
        lines.append(f"  {m.month:<10}{m.monthly_return:>+8.2f}%{m.benchmark_return:>+8.2f}%"
                     f"{m.excess_return:>+8.2f}%  {','.join(m.selected[:6])}")
    return "\n".join(lines)


# ───────────── CLI ─────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2025-05", help="开始月份 YYYY-MM")
    parser.add_argument("--end", default="2026-04", help="结束月份 YYYY-MM")
    parser.add_argument("--top-k", type=int, default=5, help="每月持仓数")
    parser.add_argument("--lookback", type=int, default=12, help="训练 lookback 月数")
    parser.add_argument("--benchmark", default="SPY")
    parser.add_argument("--out", default=None, help="JSON 输出路径")
    parser.add_argument("--universe", nargs="*",
                        default=["NVDA", "TSM", "GOOGL", "MSFT", "AAPL", "AMD", "AVGO",
                                 "MRVL", "META", "AMZN", "VRT", "LRCX"])
    args = parser.parse_args()

    r = walk_forward(
        universe=args.universe,
        start_month=args.start,
        end_month=args.end,
        benchmark=args.benchmark,
        train_lookback_months=args.lookback,
        top_k=args.top_k,
    )

    print(format_report(r))

    out = args.out or str(REPO / "data" / f"walk_forward_{args.start}_to_{args.end}.json")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(r.to_dict(), ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print(f"\n✅ JSON 输出: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
