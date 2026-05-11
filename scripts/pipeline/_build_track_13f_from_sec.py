"""把 SEC EDGAR 真实 13F 数据转成 dashboard 期望的 track_13f.json 格式。

替代旧的 track_13f.py（yfinance Top 10 静态快照）— 现在是 SEC 季度变动信号。
"""
import json
import sys
import os
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))  # 2026-05-11 lib 迁移
from pathlib import Path

from stock_research import config
from stock_research.core import edgar
from stock_research.adapters import store


def main():
    # 1. 加载所有 SEC 13F 快照
    snaps = []
    for cik_dir in config.SEC_13F_DIR.iterdir():
        if cik_dir.is_dir():
            s = store.load_latest_json(cik_dir, "snapshot")
            if s:
                snaps.append(s)
    print(f"Loaded {len(snaps)} SEC 13F snapshots from {len(list(config.SEC_13F_DIR.iterdir()))} CIK dirs")

    # 2. 按 ticker 聚合所有信号
    by_ticker = edgar.aggregate_signals_by_ticker(snaps)
    print(f"{len(by_ticker)} tickers have institutional signals")

    # 3. 转成 dashboard 期望的 schema
    output = {
        "generated_at": snaps[0]["fetched_at"] if snaps else None,
        "data_source": "SEC EDGAR 13F-HR (真实季度持仓变动)",
        "report_quarter": snaps[0].get("latest_filing", {}).get("report_date") if snaps else None,
        "investors_tracked": [s["investor"] for s in snaps],
        "tickers": {},
    }
    for ticker, signals in by_ticker.items():
        # 聚合统计
        adds = sum(1 for s in signals if "加仓" in s.get("action", "") or "新建仓" in s.get("action", ""))
        cuts = sum(1 for s in signals if "减仓" in s.get("action", "") or "清仓" in s.get("action", ""))

        output["tickers"][ticker] = {
            "name": ticker,
            "summary": {
                "total_signals": len(signals),
                "investors_adding": adds,
                "investors_cutting": cuts,
                "net_direction": ("买入主导" if adds > cuts else ("卖出主导" if cuts > adds else "持平")),
            },
            "institutional_signals": signals,
            # 兼容旧 dashboard 字段（避免前端 broken）
            "top_holders": [
                {
                    "name": s["investor"],
                    "shares": s.get("shares_curr", 0),
                    "change_pct": s.get("shares_change_pct"),
                    "action": s.get("action"),
                    "report_date": s.get("report_date"),
                    "value_usd": (s.get("value_curr_kusd", 0) * 1000) if s.get("value_curr_kusd") else None,
                }
                for s in signals[:10]
            ],
            "major_funds": [],
        }

    # 4. 写到根目录
    out_path = Path(_REPO) / "data" / "latest" / "track_13f.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"✅ Wrote {out_path}: {out_path.stat().st_size:,} bytes")
    print(f"   Tickers covered: {len(output['tickers'])}")
    print(f"   Sample: {list(output['tickers'].keys())[:8]}")


if __name__ == "__main__":
    main()
