"""V2 产业链分类入库 — 把 system_universe 跑过 chain_classifier，灌入 chain_metadata 表。

每天 daily_refresh 调一次。先全表 upsert（chain 规则可能升级），再打印覆盖率。

V1 替代：classify_watchlist_chains.py（写 V1 watchlist.chain 列）已于 2026-05-21 删除。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_research.core.chain_classifier import classify_universe  # noqa: E402
from stock_db import get_db  # noqa: E402  # type: ignore


def main() -> int:
    conn = get_db()
    # 先确认 chain_metadata 表存在（生产 init_stock_db_v2.py 已建；这里加 IF NOT EXISTS 兜底）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chain_metadata (
            market       VARCHAR NOT NULL,
            symbol       VARCHAR NOT NULL,
            chain        VARCHAR,
            chain_tier   VARCHAR,
            chain_role   VARCHAR,
            layman_intro VARCHAR,
            source       VARCHAR DEFAULT 'rule_classify',
            classified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (market, symbol)
        )
    """)

    rows = conn.execute("""
        SELECT market, symbol, name, theme, industry
        FROM system_universe
        WHERE active = true
    """).fetchall()
    print(f"待分类 universe: {len(rows)} 只")

    classified = classify_universe(rows)
    n_chain = sum(1 for c in classified if c["chain"])
    n_role = sum(1 for c in classified if c["chain_role"])
    n_override = sum(1 for c in classified if c["source"] == "manual_override")
    print(f"命中规则: {n_chain}/{len(rows)} ({n_chain/max(1,len(rows))*100:.1f}%) chain")
    print(f"细分到角色: {n_role}/{len(rows)} ({n_role/max(1,len(rows))*100:.1f}%) chain_role")
    print(f"手工 override: {n_override} 条")

    # upsert
    conn.execute("BEGIN")
    try:
        for c in classified:
            conn.execute("""
                INSERT INTO chain_metadata (market, symbol, chain, chain_tier, chain_role, layman_intro, source, classified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT (market, symbol) DO UPDATE SET
                    chain=excluded.chain,
                    chain_tier=excluded.chain_tier,
                    chain_role=excluded.chain_role,
                    layman_intro=excluded.layman_intro,
                    source=excluded.source,
                    classified_at=excluded.classified_at
            """, [c["market"], c["symbol"], c["chain"], c["chain_tier"], c["chain_role"], c["layman_intro"], c["source"]])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    # 抽样打印分布
    print("\n── chain 分布（前 10）──")
    for chain, n in conn.execute("""
        SELECT chain, COUNT(*) FROM chain_metadata
        WHERE chain IS NOT NULL GROUP BY chain ORDER BY 2 DESC LIMIT 10
    """).fetchall():
        print(f"  {chain:>20}  {n}")

    n_total = conn.execute("SELECT COUNT(*) FROM chain_metadata").fetchone()[0]
    n_assigned = conn.execute("SELECT COUNT(*) FROM chain_metadata WHERE chain IS NOT NULL").fetchone()[0]
    print(f"\nchain_metadata 总行 {n_total} · 已分类 {n_assigned} ({n_assigned/max(1,n_total)*100:.1f}%)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
