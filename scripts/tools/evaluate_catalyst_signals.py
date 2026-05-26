"""N4: catalyst 信号回测验证 — 各事件类型在 T+1/5/20 天的平均收益。

回答的核心问题：「这套 catalyst 系统到底有没有 alpha」。
对每个 event_type（盈警 / 内部人净卖 / 13D / 8-K Item 5.02 等），统计：
  - 样本数
  - T+N 平均 return (相对 SPY 基准 alpha，无基准时为绝对 return)
  - 命中率（return > 0 的比例）

数据源：
  - 事件：event_calendar*.json 5 个文件
  - 价格：data/latest/history_data.json（344 ticker × 2 年 daily close）
  - 基准：SPY（美股），港股 / A 股暂无,只算绝对 return

输出：
  - data/latest/catalyst_validation.json — 完整报告
  - 终端打印 top 信号摘要
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load_json(rel: str) -> dict:
    p = REPO / rel
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _market_of(ticker: str) -> str:
    t = ticker.upper()
    if t.endswith(".HK"):
        return "HK"
    if any(t.endswith(s) for s in (".SS", ".SH", ".SZ", ".BJ")):
        return "CN"
    return "US"


def load_price_map() -> dict[str, dict[str, float]]:
    """ticker → {date_str: close}"""
    d = _load_json("data/latest/history_data.json")
    out: dict[str, dict[str, float]] = {}
    for tk, info in (d.get("tickers") or {}).items():
        ts = info.get("ts") or []
        closes = info.get("close") or []
        if len(ts) != len(closes):
            continue
        out[tk.upper()] = dict(zip(ts, closes))
    return out


def load_benchmark_map() -> dict[str, dict[str, float]]:
    """benchmark symbol → {date_str: close}；从 DuckDB price_daily 拿三市场基准。"""
    out: dict[str, dict[str, float]] = {}
    db = REPO / "stock_history_v2.duckdb"
    if not db.exists():
        return out
    try:
        import duckdb
        con = duckdb.connect(str(db), read_only=True)
        for sym in ["SPY", "^HSI", "000300.SS"]:
            try:
                rows = con.execute(
                    "SELECT trade_date, close FROM price_daily WHERE symbol=? ORDER BY trade_date",
                    [sym],
                ).fetchall()
                out[sym] = {r[0].isoformat(): r[1] for r in rows if r[1]}
            except Exception:
                pass
        con.close()
    except Exception:
        pass
    return out


def benchmark_for_market(market: str) -> str:
    return {"US": "SPY", "HK": "^HSI", "CN": "000300.SS"}.get(market, "")


def next_trading_close(price_map: dict, ticker: str, base_date: date, offset_days: int) -> tuple[date, float] | None:
    """从 base_date 算起，找第 offset_days 个交易日的 close（forward fill 跳过停牌）。"""
    prices = price_map.get(ticker.upper())
    if not prices:
        return None
    # 列出该 ticker 所有有数据的日期（升序）
    sorted_dates = sorted(prices.keys())
    base_iso = base_date.isoformat()
    # 找 base_date 当天或之后第一个有 close 的日期
    base_idx = None
    for i, d_iso in enumerate(sorted_dates):
        if d_iso >= base_iso:
            base_idx = i
            break
    if base_idx is None:
        return None
    target_idx = base_idx + offset_days
    if target_idx >= len(sorted_dates):
        return None
    tdate_iso = sorted_dates[target_idx]
    return (datetime.fromisoformat(tdate_iso).date(), prices[tdate_iso])


def calc_returns(price_map: dict, ticker: str, event_date: date) -> dict[int, float] | None:
    """计算 T+1 / 5 / 20 日收益率（百分比）。无足够数据返回 None。"""
    base = next_trading_close(price_map, ticker, event_date, 0)
    if not base:
        return None
    _, base_close = base
    if not base_close:
        return None
    out: dict[int, float] = {}
    for n in (1, 5, 20):
        tn = next_trading_close(price_map, ticker, event_date, n)
        if not tn:
            continue
        _, tn_close = tn
        if tn_close > 0:
            out[n] = (tn_close - base_close) / base_close * 100  # 百分比
    return out or None


def calc_benchmark_returns(bench_map: dict, market: str, event_date: date) -> dict[int, float] | None:
    """基准 N 日收益率（US: SPY / HK: ^HSI / CN: 000300.SS）。"""
    bench_ticker = benchmark_for_market(market)
    if not bench_ticker or bench_ticker not in bench_map:
        return None
    return calc_returns(bench_map, bench_ticker, event_date)


def collect_events() -> list[dict]:
    """合并 5 个 event_calendar 文件 → 统一 schema:
      {ticker, market, event_type, event_date, source_file, extra_label}
    """
    out: list[dict] = []
    sources = [
        ("data/event_calendar.json",         "cn_general"),       # A 股 akshare
        ("data/event_calendar_hk.json",      "hk_yfinance"),      # 港股 yfinance
        ("data/event_calendar_us.json",      "us_yfinance"),      # 美股 yfinance
        ("data/event_calendar_hk_hkex.json", "hk_hkex"),          # HKEX 披露易
        ("data/event_calendar_us_sec.json",  "us_sec"),           # SEC 8-K/13G/13D
        ("data/event_calendar_us_form4.json","us_form4"),         # SEC Form 4
    ]
    for rel, src_label in sources:
        d = _load_json(rel)
        events = d.get("events") or []
        for e in events:
            ticker = (e.get("ticker") or "").upper()
            if not ticker:
                code = e.get("code")
                if code:
                    ticker = f"{code}.SH"  # A 股 event_calendar.json 用 code 字段，假设上交所（粗略）
            if not ticker:
                continue
            try:
                ed = datetime.strptime(e.get("event_date", "")[:10], "%Y-%m-%d").date()
            except Exception:
                continue
            etype = e.get("event_type", "")
            # 8-K 子类用 item_label 分细
            sub_label = ""
            if etype == "material_event" and e.get("item_label"):
                sub_label = e["item_label"]
            # earnings 按 surprise 方向细分（PEAD 信号区分正负）
            elif etype == "earnings":
                surp = e.get("surprise_pct")
                mag = e.get("magnitude")
                # HK/US (yfinance) 用 surprise_pct；A 股 (akshare) 用 magnitude (net_profit_yoy)
                signal = surp if isinstance(surp, (int, float)) else (mag * 100 if isinstance(mag, (int, float)) else None)
                if signal is not None:
                    if signal >= 10:
                        sub_label = "✅ 超预期 >+10%"
                    elif signal >= 0:
                        sub_label = "↗️ 超预期 0~+10%"
                    elif signal >= -10:
                        sub_label = "↘️ 差预期 -10%~0"
                    else:
                        sub_label = "❌ 差预期 <-10%"
            out.append({
                "ticker": ticker,
                "market": _market_of(ticker),
                "event_type": etype,
                "sub_label": sub_label,
                "event_date": ed,
                "source": src_label,
            })
    return out


def main() -> int:
    print("📊 加载 events + 价格历史 + 三市场基准...")
    events = collect_events()
    print(f"  事件总数: {len(events)}")
    price_map = load_price_map()
    bench_map = load_benchmark_map()
    print(f"  价格历史 ticker: {len(price_map)}")
    print(f"  基准数据: {list(bench_map.keys())} (US: SPY, HK: ^HSI, CN: 000300.SS)")

    # 按 (event_type, sub_label) 分组算 T+1/5/20 alpha
    from collections import defaultdict
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)

    no_price_count = 0
    for e in events:
        rets = calc_returns(price_map, e["ticker"], e["event_date"])
        if not rets:
            no_price_count += 1
            continue
        bench = calc_benchmark_returns(bench_map, e["market"], e["event_date"]) or {}
        # alpha = ticker return - benchmark return（三市场都用了基准）
        alphas = {}
        for n, r in rets.items():
            if n in bench:
                alphas[n] = r - bench[n]
            else:
                alphas[n] = r  # 无基准退化为绝对 return
        key = (e["event_type"], e["sub_label"])
        grouped[key].append({
            "ticker": e["ticker"],
            "date": e["event_date"].isoformat(),
            "market": e["market"],
            **{f"r{n}": alphas[n] for n in alphas},
        })

    print(f"  无价格数据跳过: {no_price_count} 条")
    print(f"  纳入回测: {sum(len(v) for v in grouped.values())} 条")
    print()

    # 聚合统计
    summary: dict[str, dict] = {}
    for (etype, sub), arr in grouped.items():
        key = f"{etype}::{sub}" if sub else etype
        stats = {"n": len(arr), "by_horizon": {}}
        for n in (1, 5, 20):
            vals = [a[f"r{n}"] for a in arr if f"r{n}" in a]
            if not vals:
                continue
            stats["by_horizon"][f"T+{n}"] = {
                "n": len(vals),
                "mean": round(statistics.mean(vals), 2),
                "median": round(statistics.median(vals), 2),
                "stdev": round(statistics.stdev(vals), 2) if len(vals) >= 2 else 0,
                "hit_pos": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
                "best": round(max(vals), 2),
                "worst": round(min(vals), 2),
            }
        summary[key] = stats

    # 输出报告
    out = REPO / "data" / "latest" / "catalyst_validation.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "n_events_total": len(events),
        "n_events_with_price": sum(len(v) for v in grouped.values()),
        "no_price_data": no_price_count,
        "benchmark_us": "SPY (alpha)",
        "benchmark_hk": "^HSI (alpha)",
        "benchmark_cn": "000300.SS (alpha)",
        "summary_by_type": summary,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 详细报告: {out}")

    # 控制台 top 信号
    print()
    print("=" * 78)
    print(f"{'event_type':<48} {'n':>4} {'T+5 mean%':>10} {'hit%':>6}")
    print("-" * 78)
    rows = []
    for key, stats in summary.items():
        h5 = stats["by_horizon"].get("T+5")
        if h5 and h5["n"] >= 3:
            rows.append((key, stats["n"], h5["mean"], h5["hit_pos"]))
    rows.sort(key=lambda x: x[2], reverse=True)
    for key, n, mean5, hit in rows[:15]:
        marker = "🚀" if mean5 > 3 else ("📈" if mean5 > 0 else ("📉" if mean5 < -3 else "↘️"))
        print(f"{marker} {key[:46]:<46} {n:>4} {mean5:>+9.2f}  {hit:>5.1f}")
    print()
    print(f"完整报告: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
