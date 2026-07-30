"""统一回测引擎（SHADOW_RESEARCH_ONLY，只读，不碰生产）。

回答一个此前 6 个散装回测工具都答不了的问题：
「扣掉手续费、印花税、滑点之后，这套公式到底还剩多少肉？」

三块拼图（对应 2026-07-14 盘点出的三大缺件）：
  1. 交易成本模型 — 三市场分开建（A股印花税卖出单边、港股双边、美股零佣+滑点），
     全部走保守口径（宁可高估成本，不给自己吹牛空间）。
  2. 统一 PIT 宇宙 — 唯一数据源 factor_snapshot_universe（每天全宇宙因子快照，
     2026-06-12 起），当天名单就是当天真实名单，无幸存者回填。
  3. 换手统计 — 每次调仓记录换了几只，成本按换手率逐日扣，年化摩擦单列。

口径（与 strategy_eval / 锦标赛对齐）：
  - 买入价 = 收盘价（entry_price 盘中价虚高的教训，memory: validation_alpha_caliber）
  - 组合 = TopN 等权，按 hold_days 个快照日调一次仓
  - 基准 = 锦标赛同款（US=SPY+QQQ 均值 / HK=^HSI / CN=000300.SS）
  - NaN close 用 isfinite 挡（HK/CN 价源有 NaN 非 NULL，memory: 锦标赛 🐛）

已知边界（报告里如实标注）：
  - 快照 2026-06-12 才开始攒，窗口短，结论当方向参考而非定论；引擎价值随数据自然变厚。
  - 等权+按只数算换手是简化（真实组合有漂移权重），对成本是近似而非精确。
  - 不建模市场冲击（组合体量小，冲击≈0 合理）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable


NEUTRAL = 50.0


# ── 1. 三市场交易成本模型（单位：% of 成交额，单边） ─────────────────────────
#
# 依据（保守口径，2026 现行费率）：
#   CN: 印花税 0.05%（仅卖出，2023-08 减半后）+ 佣金 0.025%（双边，忽略 ¥5 起步）
#       + 过户费 0.001%（双边）+ 滑点 0.10%（小盘反转股点差偏大，保守）
#   HK: 印花税 0.10%（双边）+ 交易费/征费 ~0.008%（双边）+ 佣金 0.025%（双边）
#       + 滑点 0.15%（港股点差普遍比美股宽）
#   US: 零佣金券商 + SEC/TAF 费忽略不计 + 滑点 0.05%（大盘科技股点差窄）
@dataclass(frozen=True)
class CostModel:
    """单边成本（%）。round_trip_pct = 买单边 + 卖单边。"""

    buy_pct: float      # 买入单边总成本 %
    sell_pct: float     # 卖出单边总成本 %
    label: str = ""

    @property
    def round_trip_pct(self) -> float:
        return self.buy_pct + self.sell_pct


DEFAULT_COST_MODELS: dict[str, CostModel] = {
    "US": CostModel(buy_pct=0.05, sell_pct=0.05,
                    label="零佣金+滑点0.05%/边"),
    "HK": CostModel(buy_pct=0.10 + 0.008 + 0.025 + 0.15,
                    sell_pct=0.10 + 0.008 + 0.025 + 0.15,
                    label="印花税0.1%+费0.033%+滑点0.15%/边"),
    "CN": CostModel(buy_pct=0.025 + 0.001 + 0.10,
                    sell_pct=0.05 + 0.025 + 0.001 + 0.10,
                    label="佣金0.025%+过户0.001%+滑点0.1%/边+卖出印花税0.05%"),
}

# 免成本模型（算毛成绩用，和净成绩同一条代码路径，防两套逻辑漂移）
ZERO_COST = CostModel(buy_pct=0.0, sell_pct=0.0, label="零成本(毛)")


# ── 2. 打分（与锦标赛/回放引擎同口径：缺因子记中性 50） ──────────────────────
def score_row(scores: dict[str, Any], weights: dict[str, float]) -> float:
    total = 0.0
    for factor, weight in weights.items():
        value = scores.get(factor)
        try:
            v = float(value)
            if not math.isfinite(v):
                v = NEUTRAL
        except (TypeError, ValueError):
            v = NEUTRAL
        total += weight * v
    return total


# ── 3. 核心模拟（纯函数，可注入合成数据单测） ────────────────────────────────
@dataclass
class BacktestResult:
    market: str
    n_days: int = 0
    n_rebalances: int = 0
    gross_total_pct: float = 0.0        # 毛累计收益 %
    net_total_pct: float = 0.0          # 净累计收益 %（扣成本）
    benchmark_total_pct: float = 0.0    # 基准累计收益 %
    gross_alpha_pct: float = 0.0        # 毛超额
    net_alpha_pct: float = 0.0          # 净超额
    cost_drag_pct: float = 0.0          # 成本一共吃掉几个点（毛-净）
    total_trades: int = 0               # 总买卖笔数（买+卖各算一笔）
    avg_turnover_pct: float = 0.0       # 平均每次调仓换掉几成
    annualized_turnover_x: float = 0.0  # 年化换手倍数（全组合换几轮/年）
    avg_holdings: float = 0.0
    dates: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def simulate(
    *,
    market: str,
    frames: dict[str, list[dict[str, Any]]],   # run_date(iso) -> 当日宇宙行(含因子分)
    closes: dict[str, dict[str, float]],       # symbol -> {date: close}
    benchmark_closes: dict[str, dict[str, float]],  # 基准 symbol -> {date: close}
    weights: dict[str, float],
    top_n: int = 10,
    hold_days: int = 1,                        # 每几个快照日调一次仓
    cost_model: CostModel = ZERO_COST,
    eligibility_filter: Callable[[dict], bool] | None = None,
    regime_ma: int | None = None,              # 防御闸:基准跌破 N 日均线→空仓休息
    regime_series: dict[str, float] | None = None,  # 防御闸基准收盘序列(date->close)
) -> BacktestResult:
    """按 PIT 快照逐日推进：快照日收盘建仓/调仓，快照日之间吃 close-to-close 收益。"""
    result = BacktestResult(market=market)
    dates = sorted(frames.keys())
    if len(dates) < 2:
        result.notes.append("快照不足 2 天，无法回测")
        return result
    result.dates = dates

    def _close(symbol: str, d: str) -> float | None:
        v = closes.get(symbol, {}).get(d)
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) and f > 0 else None

    def _bench_return(d0: str, d1: str) -> float | None:
        rets = []
        for sym, series in benchmark_closes.items():
            a, b = series.get(d0), series.get(d1)
            if a and b and math.isfinite(a) and math.isfinite(b) and a > 0:
                rets.append(b / a - 1.0)
        return sum(rets) / len(rets) if rets else None

    _regime_days = sorted(regime_series.keys()) if regime_series else []

    def _regime_on(d: str) -> bool:
        """基准在 N 日均线上方=True(可持仓)。数据不足不拦(宁可漏防不误伤)。"""
        if not regime_ma or not regime_series:
            return True
        past = [k for k in _regime_days if k <= d]
        if len(past) < regime_ma:
            return True
        window = [regime_series[k] for k in past[-regime_ma:]]
        ma = sum(window) / len(window)
        return regime_series[past[-1]] >= ma

    gross, net, bench = 1.0, 1.0, 1.0
    holdings: list[str] = []
    turnovers: list[float] = []
    holding_counts: list[int] = []
    missing_price_days = 0

    for i, d in enumerate(dates[:-1]):
        next_d = dates[i + 1]

        # 调仓日（含首日）：按当日 PIT 快照重排；防御闸关(基准跌破均线)→目标=空仓
        if i % hold_days == 0:
            rows = frames.get(d) or []
            if eligibility_filter is not None:
                rows = [r for r in rows if eligibility_filter(r)]
            ranked = sorted(
                rows,
                key=lambda r: (-score_row(r, weights), str(r.get("symbol"))),
            )
            target = [str(r["symbol"]) for r in ranked[:top_n]] if _regime_on(d) else []

            if not holdings and target:
                # 建仓/从空仓回场：全部买入
                net *= 1.0 - cost_model.buy_pct / 100.0
                result.total_trades += len(target)
            elif holdings and not target:
                # 防御闸触发：清仓休息，只付卖出侧
                net *= 1.0 - cost_model.sell_pct / 100.0
                result.total_trades += len(holdings)
                turnovers.append(1.0)
            elif holdings and target:
                sold = [s for s in holdings if s not in target]
                bought = [s for s in target if s not in holdings]
                turnover = len(sold) / max(len(holdings), 1)
                turnovers.append(turnover)
                result.total_trades += len(sold) + len(bought)
                # 换掉 turnover 比例的仓位，付一来一回成本
                net *= 1.0 - turnover * cost_model.round_trip_pct / 100.0
            holdings = target
            result.n_rebalances += 1

        holding_counts.append(len(holdings))

        # 持仓期收益：d 收盘 → next_d 收盘，等权
        rets = []
        for sym in holdings:
            a, b = _close(sym, d), _close(sym, next_d)
            if a is None or b is None:
                missing_price_days += 1
                continue
            rets.append(b / a - 1.0)
        day_ret = sum(rets) / len(rets) if rets else 0.0
        gross *= 1.0 + day_ret
        net *= 1.0 + day_ret

        br = _bench_return(d, next_d)
        if br is not None:
            bench *= 1.0 + br

        result.n_days += 1

    # 期末清仓成本（如实：要拿到钱得卖掉）
    if holdings:
        net *= 1.0 - cost_model.sell_pct / 100.0
        result.total_trades += len(holdings)

    result.gross_total_pct = round((gross - 1.0) * 100.0, 4)
    result.net_total_pct = round((net - 1.0) * 100.0, 4)
    result.benchmark_total_pct = round((bench - 1.0) * 100.0, 4)
    result.gross_alpha_pct = round(result.gross_total_pct - result.benchmark_total_pct, 4)
    result.net_alpha_pct = round(result.net_total_pct - result.benchmark_total_pct, 4)
    result.cost_drag_pct = round(result.gross_total_pct - result.net_total_pct, 4)
    result.avg_turnover_pct = round(
        (sum(turnovers) / len(turnovers) * 100.0) if turnovers else 0.0, 2)
    result.avg_holdings = round(
        (sum(holding_counts) / len(holding_counts)) if holding_counts else 0.0, 1)
    # 年化换手倍数：每交易日换手率 × 252（调仓间隔已隐含在 turnovers 采样频率里）
    if result.n_days > 0 and turnovers:
        per_day = sum(turnovers) / result.n_days
        result.annualized_turnover_x = round(per_day * 252, 1)
    if missing_price_days:
        result.notes.append(f"{missing_price_days} 个「持仓×日」缺价，按 0 收益跳过")
    if result.n_days < 40:
        result.notes.append(
            f"窗口仅 {result.n_days} 个交易日，统计功效低，结论当方向参考而非定论")
    return result


# ── 4. DB 装载（与锦标赛同源：factor_snapshot_universe + price_daily） ────────
US_ELIGIBLE = {"buyable", "research_only"}

BENCHMARKS = {"US": ("SPY", "QQQ"), "HK": ("^HSI",), "CN": ("000300.SS",)}
# 防御闸基准(2026-07-27 预注册):组合是科技风格 → US 用 QQQ 而非 SPY
REGIME_BENCHMARK = {"US": "QQQ", "HK": "^HSI", "CN": "000300.SS"}


def us_eligibility_filter(row: dict[str, Any]) -> bool:
    return str(row.get("eligibility") or "") in US_ELIGIBLE


def load_frames(conn, market: str, start: str | None = None,
                end: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """factor_snapshot_universe → {run_date: rows}。列名即因子键，与 VARIANTS 对齐。"""
    cond = ["market = ?"]
    params: list[Any] = [market]
    if start:
        cond.append("run_date >= ?")
        params.append(start)
    if end:
        cond.append("run_date <= ?")
        params.append(end)
    sql = f"""
        SELECT run_date, symbol, momentum, valuation, reversal, data_usability,
               f_score, quality, grade, eligibility
        FROM factor_snapshot_universe
        WHERE {' AND '.join(cond)}
        ORDER BY run_date
    """
    cols = ["run_date", "symbol", "momentum", "valuation", "reversal",
            "data_usability", "f_score", "quality", "grade", "eligibility"]
    frames: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(sql, params).fetchall():
        rec = dict(zip(cols, row))
        d = rec.pop("run_date").isoformat()
        frames.setdefault(d, []).append(rec)
    return frames


def load_closes(conn, symbols: list[str], start: str | None = None) -> dict[str, dict[str, float]]:
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    params: list[Any] = list(symbols)
    extra = ""
    if start:
        extra = " AND trade_date >= ?"
        params.append(start)
    sql = f"""
        SELECT symbol, trade_date, close FROM price_daily
        WHERE symbol IN ({placeholders}) AND close IS NOT NULL {extra}
    """
    out: dict[str, dict[str, float]] = {}
    for sym, d, close in conn.execute(sql, params).fetchall():
        try:
            c = float(close)
        except (TypeError, ValueError):
            continue
        if math.isfinite(c) and c > 0:
            out.setdefault(sym, {})[d.isoformat()] = c
    return out


def run_market_backtest(
    conn,
    *,
    market: str,
    weights: dict[str, float],
    top_n: int = 10,
    hold_days: int = 1,
    cost_model: CostModel | None = None,
    start: str | None = None,
    end: str | None = None,
    regime_ma: int | None = None,
) -> tuple[BacktestResult, BacktestResult]:
    """返回 (毛成绩, 净成绩) —— 同一引擎两个成本模型，防双引擎漂移。"""
    frames = load_frames(conn, market, start, end)
    symbols = sorted({str(r["symbol"]) for rows in frames.values() for r in rows})
    closes = load_closes(conn, symbols, start)
    # 基准不加 start:防御闸的均线要用快照期之前的历史算
    bench = load_closes(conn, list(BENCHMARKS.get(market, ())))
    regime_series = bench.get(REGIME_BENCHMARK.get(market, "")) if regime_ma else None
    # 周末/假日也可能有快照(daily_refresh 周末照跑)但没有收盘价 → 只保留基准
    # 指数有收盘价的日子（=该市场真交易日），否则缺价日按 0 收益会稀释成绩。
    # （同一教训: memory 周末 run 永久压低覆盖率, commit 30baa9e）
    trading_days = set()
    for series in bench.values():
        trading_days.update(series.keys())
    if trading_days:
        frames = {d: rows for d, rows in frames.items() if d in trading_days}
    elig = us_eligibility_filter if market == "US" else None
    cm = cost_model or DEFAULT_COST_MODELS.get(market, ZERO_COST)

    common = dict(market=market, frames=frames, closes=closes,
                  benchmark_closes=bench, weights=weights, top_n=top_n,
                  hold_days=hold_days, eligibility_filter=elig,
                  regime_ma=regime_ma, regime_series=regime_series)
    gross_r = simulate(cost_model=ZERO_COST, **common)
    net_r = simulate(cost_model=cm, **common)
    net_r.notes.append(f"成本模型[{market}]: {cm.label} (来回 {cm.round_trip_pct:.3f}%)")
    if regime_ma:
        net_r.notes.append(f"防御闸: {REGIME_BENCHMARK.get(market)} 跌破 {regime_ma} 日均线→空仓")
    return gross_r, net_r
