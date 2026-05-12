"""南向资金个股持股 % prefetch — 解决 stock_hk_ggt_components_em API 列名失效问题。

为什么需要：
  五审/七审发现 ak.stock_hk_ggt_components_em() 返回列不含"持股 %"，
  导致 south_flow_signals.fetch_components_snapshot() 永远返回空 →
  hk_picks 个股南向 score 全 fallback 0.5 → 横截面 alpha 贡献为 0。

备选 API：
  ak.stock_hsgt_hold_stock_em(market='港股通沪', indicator='5日排行')
  ak.stock_hsgt_hold_stock_em(market='港股通深', indicator='5日排行')
  列名："今日持股-占流通股比" 等

  缺点：121 页 paginated，单次拉 ~8 分钟，不能在 daily 早班 cron 跑

策略（这个脚本）：
  - 独立 prefetch job：拉一次（沪+深合并）写 data/cache/south_flow_components.json
  - 跑一次 ~15 分钟，TTL 7 天
  - 写完后 south_flow_signals.fetch_components_snapshot() 优先读 cache

用法：
  # 手动跑一次（首次激活 / 每周更新）
  python3 scripts/tools/prefetch_south_flow.py

  # 强制重抓
  python3 scripts/tools/prefetch_south_flow.py --force

  # 接入 daily_refresh.sh 推荐：周日凌晨独立 cron 跑
  0 3 * * 0 python3 scripts/tools/prefetch_south_flow.py
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CACHE_PATH = REPO / "data" / "cache" / "south_flow_components.json"
TTL_DAYS = 7


def _normalize_hk_code(code: str) -> str:
    """港股代码 → 无前导 0 统一格式（与 south_flow_signals._norm_hk_code 对齐）。"""
    s = str(code).strip().upper()
    for sfx in (".HK", ".HKSE", ".HKEX"):
        if s.endswith(sfx):
            s = s[:-len(sfx)]
    s = s.lstrip("0") or "0"
    return s


def fetch_one_market(market: str, indicator: str = "5日排行") -> dict[str, float]:
    """拉 stock_hsgt_hold_stock_em 单个市场。返回 {code: pct}。"""
    print(f"  拉 market={market!r} indicator={indicator!r} ...")
    try:
        import akshare as ak
        df = ak.stock_hsgt_hold_stock_em(market=market, indicator=indicator)
    except Exception as e:
        print(f"    ❌ {type(e).__name__}: {str(e)[:100]}")
        return {}
    if df is None or df.empty:
        print(f"    ❌ 空 DataFrame")
        return {}

    # 找代码列 + 持股 % 列
    col_code = None
    for c in ["代码", "证券代码"]:
        if c in df.columns:
            col_code = c
            break
    col_pct = None
    for c in ["今日持股-占流通股比", "持股占流通股比", "持股占已发行股本百分比",
              "持股占已发行股份百分比", "持股比例"]:
        if c in df.columns:
            col_pct = c
            break

    if col_code is None or col_pct is None:
        print(f"    ❌ 找不到代码列或持股 %% 列：{list(df.columns)[:10]}")
        return {}

    out: dict[str, float] = {}
    for _, row in df.iterrows():
        code = _normalize_hk_code(row[col_code])
        try:
            pct = float(row[col_pct])
            if pct == pct:  # not NaN
                out[code] = pct
        except (TypeError, ValueError):
            continue
    print(f"    ✅ {len(out)} 条")
    return out


def main():
    p = argparse.ArgumentParser(description="南向资金个股持股 % prefetch")
    p.add_argument("--force", action="store_true", help="强制重抓，忽略 TTL")
    p.add_argument("--out", default=str(CACHE_PATH))
    args = p.parse_args()

    out_path = Path(args.out)
    if out_path.exists() and not args.force:
        try:
            cache = json.loads(out_path.read_text(encoding="utf-8"))
            ts = cache.get("fetched_at", "")
            fresh_cutoff = (datetime.now() - timedelta(days=TTL_DAYS)).isoformat()
            if ts > fresh_cutoff:
                print(f"⏭️ cache fresh（{ts}，TTL {TTL_DAYS} 天），跳过；--force 强制重抓")
                return 0
        except Exception:
            pass

    print(f"=== 拉港股通沪 + 港股通深 全量持股 % ===")
    print(f"⚠️ 121 页 paginated × 2 市场，单次约 15-20 分钟\n")

    t0 = datetime.now()
    sh = fetch_one_market("港股通沪")
    sz = fetch_one_market("港股通深")
    # 合并（同一只港股两个 market 都可能有，取较大值为该股的总持股 %）
    components: dict[str, float] = {}
    for code, pct in sh.items():
        components[code] = pct
    for code, pct in sz.items():
        if code in components:
            components[code] = max(components[code], pct)
        else:
            components[code] = pct

    elapsed = (datetime.now() - t0).total_seconds()
    print(f"\n=== 合并完成 ===")
    print(f"  沪股通 {len(sh)} + 深股通 {len(sz)} → 去重 {len(components)} 只港股")
    print(f"  耗时 {elapsed:.0f} 秒")

    if not components:
        print(f"❌ 拉取失败 — cache 未写")
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cache = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": "ak.stock_hsgt_hold_stock_em(market='港股通沪|深', indicator='5日排行')",
        "n_components": len(components),
        "ttl_days": TTL_DAYS,
        "components": {k: round(v, 4) for k, v in components.items()},
    }
    out_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n✅ cache: {out_path} ({out_path.stat().st_size / 1024:.1f} KB)")

    print(f"\n下一步：hk_picks FACTOR_WEIGHTS south_flow 可手动恢复 0.15")
    print(f"  跑过几天 morning_brief 验证 south_flow 个股 score 不全是 0.5 后再恢复")
    return 0


if __name__ == "__main__":
    sys.exit(main())
