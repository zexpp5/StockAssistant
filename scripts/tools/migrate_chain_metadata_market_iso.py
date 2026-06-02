#!/usr/bin/env python3
"""把 chain_metadata 里展示值 market 标签('美股' 等)规范成 ISO(US/HK/CN)，并清掉重复行。

背景
----
chain_metadata 的 PK 是 (market, symbol)。rule_classify 走 ISO 标签(US/HK/CN)，
但手动编辑路径(upsert_chain_metadata，2026-06-02 前)把前端展示值 '美股' 原样落库，
于是 NXPI/GOOGL/MSFT/AMZN 各留下两行:

    ('US',  symbol, ..., 'rule_classify')      ← 自动分类
    ('美股', symbol, ..., 'manual_override')    ← 人工分类(权威)

fetch_manual_watchlist 按 symbol LEFT JOIN 时一对多放大 → 自选股「股票池」重复行。

迁移策略(幂等、保留人工分类)
-----------------------------
1. 删掉「同时存在展示值行」的那些 symbol 的 rule_classify 行 —— 避免下一步提升 PK 撞车。
2. 把展示值 market 行规范成 ISO:'美股'→'US'、'港股'→'HK'、'A股*'→'CN'。
   人工分类(chain/role/intro)随行保留，source 仍是 manual_override。

无脏行时本脚本是空操作，可安全重复执行。

上游已堵口:upsert_chain_metadata 现在一律用 ticker 后缀推断 ISO market，
不会再产生展示值行。本脚本只清历史存量。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import stock_db  # noqa: E402

# 展示值 → ISO。键用 startswith 匹配('A股·沪交所' 等带后缀也归 CN)。
_DISPLAY_TO_ISO = (
    ("美股", "US"),
    ("港股", "HK"),
    ("A股", "CN"),
)


def migrate() -> dict[str, int]:
    conn = stock_db.get_db()
    try:
        before = conn.execute("SELECT count(*) FROM chain_metadata").fetchone()[0]
        # 展示值标签的全部取值(非 ISO)
        bad_markets = [
            m for (m,) in conn.execute(
                "SELECT DISTINCT market FROM chain_metadata WHERE market NOT IN ('US','HK','CN')"
            ).fetchall()
        ]
        deleted = 0
        updated = 0
        for disp in bad_markets:
            iso = next((v for k, v in _DISPLAY_TO_ISO if str(disp).startswith(k)), None)
            if iso is None:
                print(f"  ⚠️ 未知 market 展示值,跳过: {disp!r}")
                continue
            # 1) 删掉这些 symbol 的 rule_classify 行(它们的人工分类在展示值行里,权威)
            d = conn.execute(
                """
                DELETE FROM chain_metadata
                WHERE source = 'rule_classify'
                  AND market = ?
                  AND symbol IN (SELECT symbol FROM chain_metadata WHERE market = ?)
                """,
                [iso, disp],
            )
            deleted += getattr(d, "rowcount", 0) or 0
            # 2) 展示值行规范成 ISO
            conn.execute(
                "UPDATE chain_metadata SET market = ? WHERE market = ?",
                [iso, disp],
            )
            updated += 1
        conn.commit()
        after = conn.execute("SELECT count(*) FROM chain_metadata").fetchone()[0]
        dups = conn.execute(
            "SELECT count(*) FROM (SELECT symbol FROM chain_metadata GROUP BY symbol HAVING count(*)>1)"
        ).fetchone()[0]
        return {"before": before, "after": after, "labels_normalized": updated,
                "rule_rows_dropped": before - after, "remaining_dup_symbols": dups}
    finally:
        conn.close()


if __name__ == "__main__":
    res = migrate()
    print("chain_metadata market 规范化迁移完成:")
    for k, v in res.items():
        print(f"  {k}: {v}")
    if res["remaining_dup_symbols"]:
        print("  ⚠️ 仍有重复 symbol,请人工检查")
        sys.exit(1)
    print("  ✅ 无重复 symbol")
