"""给 watchlist 中 earnings 字段为空的行补拉财报摘要。

用 stock_research.core.watchlist_enrich.fetch_earnings_summary（即 enrich_one 同款逻辑）。
只更新 earnings + updated_at，不动其他字段。

用法：
  python3 scripts/tools/backfill_earnings.py              # 跑全部 NULL
  python3 scripts/tools/backfill_earnings.py --code NVDA  # 只跑一只
  python3 scripts/tools/backfill_earnings.py --dry-run    # 只看不写
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path
from datetime import datetime

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts" / "lib"))

import stock_db
from stock_research.core.watchlist_enrich import (
    fetch_earnings_summary, fetch_earnings_quarters,
)
import yfinance as yf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", help="只跑某只 code（默认跑全部 earnings NULL 的）")
    ap.add_argument("--force", action="store_true", help="即使已有 earnings 也重抓覆盖")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = stock_db.get_db()
    where = "WHERE code = ?" if args.code else (
        "" if args.force else "WHERE earnings IS NULL OR earnings = ''"
    )
    params = [args.code] if args.code else []
    rows = conn.execute(
        f"SELECT code, name, market FROM watchlist {where} ORDER BY code", params
    ).fetchall()
    print(f"待处理：{len(rows)} 只")

    ok, fail, skip = 0, 0, 0
    quarter_rows = 0
    fail_list = []
    for i, (code, name, market) in enumerate(rows, 1):
        prefix = f"[{i}/{len(rows)}] {code:12s} {(name or '')[:20]:20s} {(market or '?'):10s}"
        try:
            ticker = yf.Ticker(code)
            info = ticker.info or {}
            quarters = fetch_earnings_quarters(info, ticker)
            if not quarters:
                print(f"{prefix} ⏭  yfinance 无季报/TTM 数据")
                skip += 1
                fail_list.append((code, "no data"))
                continue
            summary = fetch_earnings_summary(info, ticker)
            preview = (summary or "").split("\n")[0][:60]
            print(f"{prefix} ✓ {len(quarters)} 季 · {preview}")
            if not args.dry_run:
                # 1) watchlist.earnings = 最新一句摘要（看板用）
                conn.execute(
                    "UPDATE watchlist SET earnings = ?, updated_at = ? WHERE code = ?",
                    [summary, datetime.now(), code],
                )
                # 2) earnings_history = 全部季度结构化（趋势用，按 (code,fiscal_period) upsert）
                n_q = stock_db.upsert_earnings_history(code, quarters, conn=conn)
                quarter_rows += n_q
            ok += 1
        except Exception as e:
            print(f"{prefix} ✗ {e}")
            fail += 1
            fail_list.append((code, str(e)))
        time.sleep(0.3)  # 别太快

    conn.close()
    print(f"\n=== 总结 ===  成功 {ok}  失败 {fail}  无数据 {skip}  季报历史 {quarter_rows} 行 upsert")
    if fail_list:
        print("无数据 / 失败的标的：")
        for c, r in fail_list:
            print(f"  {c}: {r}")


if __name__ == "__main__":
    main()
