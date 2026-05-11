"""
早期预警信号模块（领先指标）
─────────────────────────────────────────
解决反向验证暴露的问题：原打分体系只看历史涨幅和估值，
全是滞后指标——所以 Intel +238% / Marvell +100% 都漏报。

新增两个领先信号：

1. **内部人净买入**（insider_purchases）
   - 来源：yfinance.Ticker.insider_purchases
   - 含义：高管 / 董事最近 6 个月净买卖
   - 强信号：净买入 > 0 且金额 > $1M

2. **分析师上修**（upgrades_downgrades）
   - 来源：yfinance.Ticker.upgrades_downgrades
   - 含义：投行近期目标价上调 / 评级上调
   - 强信号：90 天内 ≥ 3 次目标价 Raises

支持点对点回测：传入 as_of_date 时，只取那之前的分析师事件。
（内部人 6 月窗口数据是当前快照，无法按历史日切回，仅供近似。）
"""
import sys
import os
import json
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yfinance as yf
import pandas as pd


def _safe_float(v):
    try:
        f = float(v)
        if pd.isna(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def fetch_insider_signal(ticker, retries=2):
    """拉 6 个月内部人净买入数据（当前快照，无法历史回溯）"""
    for attempt in range(retries + 1):
        try:
            t = yf.Ticker(ticker)
            ip = t.insider_purchases
            if ip is None or len(ip) == 0:
                return None
            row_label = ip.columns[0]
            buys = ip[ip[row_label] == "Purchases"]
            sells = ip[ip[row_label] == "Sales"]
            net = ip[ip[row_label] == "Net Shares Purchased (Sold)"]

            buy_shares = _safe_float(buys.iloc[0, 1]) if len(buys) else 0
            sell_shares = _safe_float(sells.iloc[0, 1]) if len(sells) else 0
            net_shares = _safe_float(net.iloc[0, 1]) if len(net) else 0
            net_trans = _safe_float(net.iloc[0, 2]) if len(net) else 0

            try:
                hist = t.history(period="5d")
                last_price = float(hist["Close"].iloc[-1]) if len(hist) > 0 else None
            except Exception:
                last_price = None
            net_value_usd = (net_shares * last_price) if (net_shares and last_price) else None

            return {
                "buy_shares_6m": buy_shares or 0,
                "sell_shares_6m": sell_shares or 0,
                "net_shares_6m": net_shares or 0,
                "net_transactions_6m": net_trans or 0,
                "net_value_usd_approx": net_value_usd,
            }
        except Exception as e:
            if attempt < retries:
                time.sleep(2 + attempt * 2)
                continue
            return {"error": str(e)}
    return None


def fetch_analyst_signal(ticker, as_of=None, lookback_days=90, retries=2):
    """拉分析师上调/下调，按 as_of 切片，支持历史回测"""
    for attempt in range(retries + 1):
        try:
            t = yf.Ticker(ticker)
            ud = t.upgrades_downgrades
            if ud is None or len(ud) == 0:
                return None

            df = ud.copy().reset_index()
            if "GradeDate" not in df.columns:
                df.rename(columns={df.columns[0]: "GradeDate"}, inplace=True)
            df["GradeDate"] = pd.to_datetime(df["GradeDate"], errors="coerce", utc=True)
            df = df.dropna(subset=["GradeDate"])

            if as_of:
                cutoff = pd.to_datetime(as_of, utc=True)
                df = df[df["GradeDate"] <= cutoff]
            else:
                cutoff = pd.Timestamp.now(tz="UTC")

            window_start = cutoff - pd.Timedelta(days=lookback_days)
            window = df[df["GradeDate"] >= window_start]

            if len(window) == 0:
                return {
                    "window_days": lookback_days,
                    "events_total": 0,
                    "raises": 0, "lowers": 0,
                    "upgrades": 0, "downgrades": 0,
                    "avg_target_raise_pct": None,
                }

            raises = lowers = 0
            target_raise_pcts = []
            for _, row in window.iterrows():
                pa = str(row.get("priceTargetAction", "") or "").strip()
                cur = _safe_float(row.get("currentPriceTarget"))
                pri = _safe_float(row.get("priorPriceTarget"))
                if pa == "Raises":
                    raises += 1
                    if cur and pri and pri > 0:
                        target_raise_pcts.append((cur / pri - 1) * 100)
                elif pa == "Lowers":
                    lowers += 1

            upgrades = (window["Action"].astype(str).str.lower() == "up").sum()
            downgrades = (window["Action"].astype(str).str.lower() == "down").sum()
            avg_pct = (sum(target_raise_pcts) / len(target_raise_pcts)) if target_raise_pcts else None

            return {
                "window_days": lookback_days,
                "events_total": int(len(window)),
                "raises": int(raises),
                "lowers": int(lowers),
                "upgrades": int(upgrades),
                "downgrades": int(downgrades),
                "avg_target_raise_pct": round(avg_pct, 2) if avg_pct else None,
            }
        except Exception as e:
            if attempt < retries:
                time.sleep(2 + attempt * 2)
                continue
            return {"error": str(e)}
    return None


def fetch_signals_for(ticker, as_of=None, lookback_days=90):
    return {
        "ticker": ticker,
        "as_of": as_of,
        "insider": fetch_insider_signal(ticker),
        "analyst": fetch_analyst_signal(ticker, as_of=as_of, lookback_days=lookback_days),
    }


def fetch_signals_batch(tickers, as_of=None, lookback_days=90, sleep_sec=1.5):
    results = []
    for tk in tickers:
        print(f"  · 拉 {tk} ...", end="", flush=True)
        sig = fetch_signals_for(tk, as_of=as_of, lookback_days=lookback_days)
        results.append(sig)
        ins_ok = sig["insider"] and "error" not in sig["insider"]
        ana_ok = sig["analyst"] and "error" not in sig["analyst"]
        print(f" 内部人={'OK' if ins_ok else 'X'} 分析师={'OK' if ana_ok else 'X'}")
        time.sleep(sleep_sec)
    return results


# ============================================================
# 信号打分（叠加在现有 100 分体系上的早期信号加成）
# ============================================================
def score_insider(ins):
    """内部人净买入：0-15 分"""
    if not ins or "error" in ins:
        return 0, "无数据"
    net = ins.get("net_shares_6m", 0) or 0
    val = ins.get("net_value_usd_approx") or 0
    if net <= 0:
        return 0, f"6m 净卖出 {abs(net):.0f} 股"
    if val and val >= 10_000_000:
        return 15, f"6m 净买入 ${val/1e6:.1f}M（强）"
    if val and val >= 1_000_000:
        return 10, f"6m 净买入 ${val/1e6:.1f}M"
    if net >= 100_000:
        return 8, f"6m 净买入 {net/1000:.0f}k 股"
    return 4, f"6m 小额净买入"


def score_analyst(ana):
    """分析师 90 天内目标价上调：0-15 分"""
    if not ana or "error" in ana:
        return 0, "无数据"
    raises = ana.get("raises", 0)
    lowers = ana.get("lowers", 0)
    avg_pct = ana.get("avg_target_raise_pct")
    net = raises - lowers
    if net <= 0:
        return 0, f"90d 上调{raises}次 / 下调{lowers}次"
    if raises >= 5 and avg_pct and avg_pct >= 20:
        return 15, f"90d 上调{raises}次 / 均幅 +{avg_pct:.1f}%（强烈共识）"
    if raises >= 3:
        avg_str = f" / 均幅 +{avg_pct:.1f}%" if avg_pct else ""
        return 10, f"90d 上调{raises}次{avg_str}"
    return 5, f"90d 上调{raises}次"


def score_signals(sig):
    ins_score, ins_reason = score_insider(sig.get("insider"))
    ana_score, ana_reason = score_analyst(sig.get("analyst"))
    return ins_score, ana_score, [ins_reason, ana_reason]


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", help="股票代码列表（默认用反向验证样本）")
    parser.add_argument("--as-of", help="历史回测日期 YYYY-MM-DD（仅影响分析师信号）")
    parser.add_argument("--lookback", type=int, default=90)
    parser.add_argument("--out", help="输出 JSON 路径")
    args = parser.parse_args()

    if not args.tickers:
        args.tickers = [
            "AMD", "INTC", "000660.KS", "DDOG", "VRT",
            "300308.SZ", "688256.SS", "LRCX", "MRVL", "AVGO",
            "AAPL", "MSFT", "CRM", "SNOW", "TSLA",
        ]

    print("=" * 70)
    print(f"  📡 早期预警信号（内部人 + 分析师）")
    print(f"  as_of = {args.as_of or '当前'} · lookback = {args.lookback}天")
    print("=" * 70)
    print(f"\n[1/2] 拉取 {len(args.tickers)} 只股票信号...")
    results = fetch_signals_batch(args.tickers, as_of=args.as_of, lookback_days=args.lookback)

    print(f"\n[2/2] 信号打分汇总：")
    print(f"\n  {'股票':<14}{'内部人':<8}{'内部人说明':<34}{'分析师':<8}{'分析师说明'}")
    print(f"  {'-'*110}")
    for sig in results:
        ins_s, ana_s, reasons = score_signals(sig)
        print(f"  {sig['ticker']:<14}{ins_s:<8}{reasons[0][:32]:<34}{ana_s:<8}{reasons[1]}")

    out_file = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "early_signals.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "as_of": args.as_of,
            "lookback_days": args.lookback,
            "results": results,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n✅ 完整数据：{out_file}")


if __name__ == "__main__":
    main()
