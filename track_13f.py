"""
13F · 顶级机构持仓跟踪（简化版）
─────────────────────────────────────────
对方案 A 美股标的，从 yfinance 拉取当前 Top 10 机构持仓 + 主要基金持仓。

⚠️ 限制：
  • yfinance 给的是当前快照，不是季度变动 trend
  • 真正专业版要解析 SEC EDGAR 13F-HR XML
  • 现在这个版本：先建立 baseline，下次跑可以对比变动
"""
import sys, os, json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yfinance as yf

# 只跟踪美股部分（A 股/港股/澳股 13F 不适用）
US_TICKERS = [
    ("NVDA",       "NVDA"),
    ("TSM",        "TSM"),
    ("GOOGL",      "GOOGL"),
    ("MSFT",       "MSFT"),
    ("AMD",        "AMD"),
    ("Vertiv",     "VRT"),
    ("Cameco",     "CCJ"),
    ("Datadog",    "DDOG"),
]


def fetch_holders(ticker):
    """拉机构持仓（institutional holders）+ 基金持仓（mutualfund holders）"""
    try:
        t = yf.Ticker(ticker)
        ih = t.institutional_holders
        mf = t.mutualfund_holders
        return {
            "institutional": ih.to_dict("records") if ih is not None and len(ih) > 0 else [],
            "mutual_fund": mf.to_dict("records") if mf is not None and len(mf) > 0 else [],
        }
    except Exception as e:
        print(f"  ! {ticker}: {e}")
        return None


def main():
    print("=" * 70)
    print("  📊 方案 A 美股 · 13F 机构持仓跟踪")
    print("=" * 70)
    print(f"\n⚠️ yfinance 数据是快照，不是季度变动。要看真正的「加仓/减仓」")
    print(f"   需要去 SEC EDGAR 解析 13F-HR XML（复杂度更高）。\n")

    out = {"generated_at": datetime.now().isoformat(), "tickers": {}}

    for name, ticker in US_TICKERS:
        print(f"\n📌 {name} ({ticker})")
        print("-" * 60)
        h = fetch_holders(ticker)
        if not h:
            print("  ❌ 无数据")
            continue

        inst = h["institutional"][:5]  # Top 5 机构
        mf = h["mutual_fund"][:5]

        if inst:
            print("  Top 5 机构持仓：")
            for i, holder in enumerate(inst, 1):
                holder_name = holder.get("Holder", "?")
                shares = holder.get("Shares", 0)
                pct = holder.get("pctHeld", holder.get("% Out", 0))
                value = holder.get("Value", 0)
                if isinstance(pct, float) and pct < 1:
                    pct_str = f"{pct*100:.2f}%"
                else:
                    pct_str = f"{pct:.2f}%"
                print(f"    {i}. {holder_name[:35]:<35}股数 {shares:>14,}  占比 {pct_str:>8}  市值 ${value/1e9:.2f}B")

        if mf:
            print("  Top 5 共同基金持仓：")
            for i, holder in enumerate(mf, 1):
                holder_name = holder.get("Holder", "?")
                shares = holder.get("Shares", 0)
                pct = holder.get("pctHeld", holder.get("% Out", 0))
                if isinstance(pct, float) and pct < 1:
                    pct_str = f"{pct*100:.2f}%"
                else:
                    pct_str = f"{pct:.2f}%"
                print(f"    {i}. {holder_name[:35]:<35}股数 {shares:>14,}  占比 {pct_str:>8}")

        out["tickers"][ticker] = {
            "name": name,
            "institutional": inst,
            "mutual_fund": mf,
        }

    # 保存
    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "track_13f.json")
    # 处理 numpy / pandas 类型
    def safe_default(o):
        try:
            return float(o)
        except (TypeError, ValueError):
            return str(o)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=safe_default)
    print(f"\n✅ 完整数据：{out_file}")
    print(f"\n💡 下一步升级（专业版）：")
    print(f"  1. 用 SEC EDGAR API 解析季度 13F-HR")
    print(f"  2. 计算每个机构的季度持仓变化（加仓/减仓/新建/清仓）")
    print(f"  3. 对方案 A 标的标注「Bridgewater 加仓 X%」「Citadel 清仓」等")


if __name__ == "__main__":
    main()
