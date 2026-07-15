#!/usr/bin/env python3
"""买卖闭环全链验收（方案 2026-07-15 §4，A1~A7 自动判定）。

只读检查，产出 ✅/❌ 报告；任何一项 FAIL 退出码非 0。
用法: /opt/homebrew/bin/python3 scripts/tools/acceptance_trade_loop.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

EXPECT = {  # 预注册参数(方案 §2)——验收对表,别从 exit_rules 反读(那是同义反复)
    "US": {"stop_pct": -8.0, "review_days": 5},
    "HK": {"stop_pct": -10.0, "review_days": 20},
    "CN": {"stop_pct": -6.0, "review_days": 5},
}

results: list[tuple[str, bool, str]] = []


def check(code: str, ok: bool, detail: str) -> None:
    results.append((code, ok, detail))


def main() -> int:
    multi_p = REPO / "data" / "latest" / "daily_strict_picks_multi.json"
    multi = json.loads(multi_p.read_text(encoding="utf-8")) if multi_p.exists() else {}

    # A1 美股严选 3 只带退出计划(数值=预注册)
    us = multi.get("US") or {}
    us_picks = us.get("picks") or []
    ok = len(us_picks) == 3
    for p in us_picks:
        plan = p.get("exit_plan") or {}
        entry = plan.get("entry_price") or 0
        ok = ok and plan.get("review_days") == EXPECT["US"]["review_days"]
        ok = ok and plan.get("stop_pct") == EXPECT["US"]["stop_pct"]
        ok = ok and entry > 0 and abs(plan.get("stop_price", 0) / entry - 0.92) < 0.005
    check("A1", ok, f"美股严选 {len(us_picks)} 只, 退出计划参数核对")

    # A2 港股严选 ≤3 只带退出计划(review=20)
    hk = multi.get("HK") or {}
    hk_picks = hk.get("picks") or []
    ok = 0 < len(hk_picks) <= 3
    for p in hk_picks:
        plan = p.get("exit_plan") or {}
        ok = ok and plan.get("review_days") == EXPECT["HK"]["review_days"]
        ok = ok and plan.get("stop_pct") == EXPECT["HK"]["stop_pct"]
    check("A2", ok, f"港股严选 {len(hk_picks)} 只, -10%/20日复评")

    # A3 剔贵/接飞刀闸对港股生效(排除段存在 或 名单全通过且规则声明在)
    rules = hk.get("rules") or {}
    ok = "exclude_expensive" in rules and "exclude_falling_knife" in rules
    check("A3", ok, f"港股闸门规则声明: {list(rules.keys())[:3]}, excluded={len(hk.get('excluded') or [])}")

    # A4 dashboard HTML 含三条线(渲染函数级检查,不依赖已部署文件)
    try:
        sys.path.insert(0, str(REPO / "scripts" / "pipeline"))
        import importlib
        bd = importlib.import_module("build_stock_dashboard_html")
        html = bd.strict_picks_card_html()
        ok = all(k in html for k in ("卖出三条线", "止损", "复评")) and "港股严选" in html
        check("A4", ok, "strict_picks_card_html 含 卖出三条线/止损/复评/港股严选")
    except Exception as e:  # noqa: BLE001
        check("A4", False, f"渲染失败: {e}")

    # A5 早报严选段含退出行
    try:
        from stock_research.jobs.morning_brief import _daily_strict_picks_lines
        lines = "\n".join(_daily_strict_picks_lines())
        ok = "🛑止损" in lines and "港股严选" in lines
        check("A5", ok, "早报严选段含 🛑止损 + 港股严选")
    except Exception as e:  # noqa: BLE001
        check("A5", False, f"早报生成失败: {e}")

    # A6 持仓命中严选史 → 合成退出线(函数级:造一条假持仓+假命中验证管道)
    try:
        from stock_research.jobs.real_holding_review import _strict_exit_synthetic_plan
        synth = _strict_exit_synthetic_plan(
            {"id": 1, "symbol": "TEST.HK", "avg_cost_local_per_share": 100.0, "market": "HK"},
            {"date": "2026-07-15", "market": "HK", "formula": "quality_heavy"},
        )
        trig = {t["trigger_type"]: t for t in (synth or {}).get("triggers") or []}
        ok = (synth is not None
              and synth["source_type"] == "strict_pick_auto"
              and trig.get("strict_stop_loss", {}).get("price_max") == 90.0   # -10%
              and synth["review_days"] == 20)
        check("A6", ok, "合成退出线: HK 成本100→止损90/复评20日/source=strict_pick_auto")
    except Exception as e:  # noqa: BLE001
        check("A6", False, f"合成计划失败: {e}")

    # A7 相关单测全过
    import subprocess
    r = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_exit_rules",
         "tests.test_backtest_engine", "tests.test_daily_strict_picks", "-q"],
        capture_output=True, text=True, cwd=REPO,
    )
    ok = r.returncode == 0
    tail = (r.stderr or r.stdout).strip().splitlines()[-1:]
    check("A7", ok, f"unittest: {tail}")

    print("=" * 64)
    print("买卖闭环全链验收 (docs/V2/2026-07-15_买卖闭环_可实盘化方案.md §4)")
    print("=" * 64)
    fails = 0
    for code, ok, detail in results:
        mark = "✅" if ok else "❌"
        if not ok:
            fails += 1
        print(f"  {mark} {code}: {detail}")
    print("-" * 64)
    print(f"  {len(results) - fails}/{len(results)} PASS" + ("" if fails == 0 else f" · {fails} FAIL"))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
