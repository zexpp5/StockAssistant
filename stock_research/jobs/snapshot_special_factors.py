"""特色因子每日快照 · 港A股锦标赛「埋种子」步骤（2026-07-07 上线）。

背景（docs/V2/2026-07-06_港A股影子组合锦标赛_方案.md 第 3 问）：
  A 股龙虎榜 / PEAD / 政策题材、港股南向资金这几个因子，虽然在
  a_share_picks / hk_picks 里每天算过，但**从来没有按日归档**，
  所以 IC 校准 (calibrate_a_share_factor_weights) 只能给它们 0 权重。

本 job 只做一件事：把这些「今天算出来的信号值」按 (run_date, market,
  symbol, factor_name) 落进 factor_signal_snapshot，供 ~2 个月后
  与 pick_outcomes 收盘价 outcome 做 IC 验证。

刻意的边界：
  - **只读现有产物 + 只写新表**，不碰打分、不碰推荐、不碰 watchlist/holdings。
  - CN 因子直接读 data/a_share_picks.json（当天已算好），过期不写（防 PIT 污染）。
  - HK 南向资金现算一次（fetch_components_snapshot 带 7 天 cache，很便宜）。
  - 表是长格式（一行一个因子值），加新因子不改表结构。

用法：
  python3 -m stock_research.jobs.snapshot_special_factors            # 快照今天
  python3 -m stock_research.jobs.snapshot_special_factors --date 2026-07-07
  python3 -m stock_research.jobs.snapshot_special_factors --allow-stale-cn   # 调试用
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import DB_PATH, get_db  # noqa: E402

logger = logging.getLogger(__name__)

def ensure_table(conn: Any) -> None:
    """长格式特色因子快照表。加新因子只需多插行，不改结构。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS factor_signal_snapshot (
            run_date DATE NOT NULL,
            market VARCHAR NOT NULL,
            symbol VARCHAR NOT NULL,
            factor_name VARCHAR NOT NULL,
            factor_value DOUBLE,
            raw_value DOUBLE,
            source VARCHAR,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (run_date, market, symbol, factor_name)
        )
        """
    )


def _cn_bare_to_symbol(code: str) -> str | None:
    """A 股裸 6 位代码 → price_daily/factor_snapshot_universe 同款后缀格式。

    60/68/69/90 开头 → .SS（上交所+科创+B股）
    83/87/88/43/92   → .BJ（北交所）
    其余（0/3 开头）  → .SZ（深交所+创业板）
    与 a_share_universe 落库口径一致，保证未来 IC join 对得上。
    """
    raw = str(code or "").strip().split(".")[0]
    if not (raw.isdigit() and len(raw) == 6):
        return None
    if raw.startswith(("60", "68", "69", "90")):
        return f"{raw}.SS"
    if raw.startswith(("83", "87", "88", "43", "92")):
        return f"{raw}.BJ"
    return f"{raw}.SZ"


def collect_cn_rows(run_date: date) -> tuple[list[tuple], str]:
    """**现算** A 股稀疏特色因子（龙虎榜 / PEAD / 政策题材），落 PIT 快照。

    为什么不读 a_share_picks.json：生产 IC 校准把权重收敛成 reversal=1.0，
    这几个因子不在 active_factors → 生产**根本不算** → JSON 里永远是中性 0.5。
    埋种子的意义正是：抛开生产权重，把这些因子每天算出来存起来，
    等 ~2 个月后攒够非中性样本再做 IC 验证。

    刻意**不含 north_flow**：北向个股持股 2024-08 监管停发（hk_hold 全空），
    该因子结构性失活，存了也永远中性，不占坑。
    """
    from stock_research.core.a_share_universe import fetch_a_share_tech_universe
    from stock_research.core.a_share_filters import _strip_code
    from stock_research.core.lhb_signals import compute_lhb_factors
    from stock_research.core.event_calendar import build_calendar, pead_factor
    from stock_research.core.policy_events import themes_under_policy_tailwind

    universe = fetch_a_share_tech_universe()
    if not universe:
        return [], "A 股 universe 为空 — 跳过 CN"
    # {norm_code: (symbol, theme_text)}
    meta: dict[str, tuple[str, str]] = {}
    for item in universe:
        raw = str(item.get("raw_ticker") or item.get("ticker") or "").split(".")[0]
        norm = _strip_code(raw)
        symbol = _cn_bare_to_symbol(raw)
        if not symbol:
            continue
        theme_text = f"{item.get('sector') or ''} {item.get('name') or ''} {item.get('industry') or ''}"
        meta[norm] = (symbol, theme_text)

    codes = list(meta.keys())
    notes: list[str] = []

    # ── 龙虎榜（横截面归一，多数股 0.5，上榜股偏离）
    lhb_factors: dict = {}
    try:
        lhb_factors = compute_lhb_factors(codes, lookback_days=5)
    except Exception as e:
        notes.append(f"lhb失败:{e}")

    # ── PEAD（真实公告日窗口）
    cal = None
    try:
        cal = build_calendar(horizon_unlock_days=90, horizon_insider_days=60, include_earnings=True)
    except Exception as e:
        notes.append(f"calendar失败:{e}")

    # ── 政策题材受益
    tailwind: dict = {}
    try:
        tailwind = themes_under_policy_tailwind(days=14, min_count=2)
    except Exception as e:
        notes.append(f"policy失败:{e}")

    rows: list[tuple] = []
    nonneutral = {"lhb": 0, "pead": 0, "policy_theme": 0}
    for norm, (symbol, theme_text) in meta.items():
        # lhb
        lf = lhb_factors.get(norm)
        lhb_val = float(lf.score) if lf is not None else 0.5
        rows.append((run_date, "CN", symbol, "lhb", lhb_val, None, "lhb_signals"))
        if lhb_val != 0.5:
            nonneutral["lhb"] += 1
        # pead
        pead_val = 0.5
        if cal is not None:
            try:
                pead_val = float(pead_factor(norm, cal, today=run_date).get("score", 0.5))
            except Exception:
                pead_val = 0.5
        rows.append((run_date, "CN", symbol, "pead", pead_val, None, "event_calendar"))
        if pead_val != 0.5:
            nonneutral["pead"] += 1
        # policy_theme
        policy_val = 0.0
        for theme, count in tailwind.items():
            if theme and theme in theme_text:
                policy_val = max(policy_val, min(0.30, count * 0.05))
        rows.append((run_date, "CN", symbol, "policy_theme", policy_val, None, "policy_events"))
        if policy_val != 0.0:
            nonneutral["policy_theme"] += 1

    note = (f"CN {len(meta)} 只 → {len(rows)} 行 · 非中性 "
            f"lhb={nonneutral['lhb']} pead={nonneutral['pead']} policy={nonneutral['policy_theme']}")
    if notes:
        note += " · ⚠️" + ";".join(notes)
    return rows, note


def collect_hk_rows(run_date: date) -> tuple[list[tuple], str]:
    """现算全港股宇宙南向资金信号。返回 (rows, note)。

    刻意**只存截面信号**（individual_rank + individual_pct），不拉 aggregate：
      - aggregate（整体南向流向）是当日全池共享的常数，对**截面** IC 零贡献，
        存了也无法区分个股；
      - fetch_aggregate_south_flow 走 akshare 实时口，已知 ~8 分钟 / 易挂，
        不值得为一个截面上的常数把日更 job 拖死。
    截面 rank 只需 fetch_components_snapshot（带 7 天 cache，秒级）。
    """
    from stock_research.core.hk_universe import fetch_hk_tech_universe
    from stock_research.core.south_flow_signals import (
        _norm_hk_code,
        fetch_components_snapshot,
    )

    try:
        components = fetch_components_snapshot()
    except Exception as e:
        return [], f"南向 components 拉取失败: {e} — 跳过 HK"
    if not components:
        return [], "南向 components 为空（cache miss 且实时口失效）— 跳过 HK，跑 prefetch_south_flow.py 生成 cache"

    pcts = sorted(components.values())
    n = len(pcts)
    universe = fetch_hk_tech_universe()
    rows: list[tuple] = []
    covered = 0
    for item in universe:
        code = item["ticker"]  # e.g. 0700.HK
        pct = components.get(_norm_hk_code(code))
        if pct is None:
            # 非港股通标的：无南向数据，rank 记中性 0.5，raw 留空
            rank = 0.5
        else:
            below = sum(1 for x in pcts if x < pct)
            eq = sum(1 for x in pcts if x == pct)
            rank = round((below + 0.5 * eq) / n, 4) if n >= 4 else 0.5
            covered += 1
        rows.append((run_date, "HK", code, "south_flow", float(rank),
                     float(pct) if pct is not None else None, "south_flow_signals"))
    return rows, f"HK {len(universe)} 只 → {len(rows)} 行（南向持股覆盖 {covered}）"


def write_rows(conn: Any, rows: list[tuple]) -> int:
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT OR REPLACE INTO factor_signal_snapshot
        (run_date, market, symbol, factor_name, factor_value, raw_value, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        rows,
    )
    return len(rows)


def run(run_date: date, *, dry_run: bool) -> dict[str, Any]:
    cn_rows, cn_note = collect_cn_rows(run_date)
    hk_rows, hk_note = collect_hk_rows(run_date)
    all_rows = cn_rows + hk_rows

    result: dict[str, Any] = {
        "run_date": run_date.isoformat(),
        "cn_note": cn_note,
        "hk_note": hk_note,
        "cn_rows": len(cn_rows),
        "hk_rows": len(hk_rows),
        "dry_run": dry_run,
    }

    if dry_run:
        result["written"] = 0
        return result

    conn = get_db(DB_PATH)
    try:
        ensure_table(conn)
        written = write_rows(conn, all_rows)
        # 写后自检：读回本 run_date 的行数/因子分布
        verify = conn.execute(
            """
            SELECT market, factor_name, COUNT(*)
            FROM factor_signal_snapshot WHERE run_date = ?
            GROUP BY market, factor_name ORDER BY market, factor_name
            """,
            [run_date],
        ).fetchall()
    finally:
        conn.close()

    result["written"] = written
    result["verify"] = {f"{m}/{f}": int(n) for m, f, n in verify}
    return result


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="特色因子每日快照（港A股锦标赛埋种子）")
    ap.add_argument("--date", default="", help="run_date YYYY-MM-DD，默认今天")
    ap.add_argument("--dry-run", action="store_true", help="只算不写库")
    args = ap.parse_args()

    run_date = date.fromisoformat(args.date) if args.date else datetime.now().date()
    res = run(run_date, dry_run=args.dry_run)

    print(f"📸 特色因子快照 · run_date={res['run_date']}")
    print(f"  CN: {res['cn_note']}")
    print(f"  HK: {res['hk_note']}")
    if res["dry_run"]:
        print(f"  (dry-run，未写库；预计 {res['cn_rows'] + res['hk_rows']} 行)")
    else:
        print(f"  ✅ 写入 {res['written']} 行 → factor_signal_snapshot")
        print(f"  自检: {res.get('verify')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
