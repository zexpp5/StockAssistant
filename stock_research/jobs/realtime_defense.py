"""实盘防御 job — 每日跑一次，把 stress test 验证过的 C 终极版集成到实盘。

流程：
  1. 拉飞书 picks 表（含累计涨跌%）
  2. 检查市场层（VIX + 200MA）+ 个股层（-15% 止损）
  3. 输出警报：
     - 控制台报告
     - 写入飞书 picks 表的「风险提示」字段（在原内容前插入警报）
     - macOS 通知（如果有 CRITICAL 警报）
     - JSON 快照到 data/snapshots/audit/

边界（必须遵守）：
  - 不直接执行交易；只输出建议
  - 用户保留最终决策权
  - 系统输出"建议减仓"，用户决定何时实际减仓

CLI:
  python3 -m stock_research.jobs.realtime_defense
  python3 -m stock_research.jobs.realtime_defense --no-feishu  # 仅控制台
  python3 -m stock_research.jobs.realtime_defense --no-notify  # 不弹 macOS 通知
"""
from __future__ import annotations
import argparse
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from .. import config
from ..adapters import legacy_shim as feishu, store
from ..core import defense_signals

logger = logging.getLogger("stock_research.jobs.realtime_defense")


# ─────────── 工具 ───────────

def _macos_notify(title: str, msg: str) -> None:
    """macOS 桌面通知（osascript）。失败静默。"""
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
            timeout=5, capture_output=True,
        )
    except Exception:
        pass


# ─────────── 主流程 ───────────
# 2026-05-11 PM 第二轮:_write_alerts_to_feishu 已删 — 飞书 Bitable 100% 退役.
# alerts 已通过 3 个渠道留存:webhook 推送(defense_watcher) + JSON 快照
# (AUDIT_DIR/realtime_defense.json) + DuckDB snapshots(category='audit').

def run(notify: bool = True, **_legacy_kwargs) -> dict:
    print(f"\n{'='*80}")
    print(f"  🛡 实盘防御检查 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*80}\n")

    # 1. 拉今日 picks (DuckDB)
    print("[1/2] 拉 picks [DuckDB]...")
    picks_raw = feishu.fetch_picks()  # shim 内部走 DuckDB
    print(f"  共 {len(picks_raw)} 条")

    # 2. 综合诊断
    print("\n[2/2] 检查市场层（VIX + 200MA）+ 个股层（-15% 止损）...")
    result = defense_signals.diagnose_all(picks_raw)

    severity = result["severity"]
    icon_map = {"NONE": "🟢", "LOW": "🟡", "HIGH": "🟠", "CRITICAL": "🔴"}
    icon = icon_map.get(severity, "?")

    print(f"\n  {icon} 总体严重度: {severity}")
    print(f"     {result['summary']}")

    # 市场层警报
    if result["market_alerts"]:
        print(f"\n  ─── 市场层警报 ({len(result['market_alerts'])}) ───")
        for a in result["market_alerts"]:
            print(f"  · [{a['severity']}] {a['type']}: {a['trigger']}")
            print(f"      {a['suggested_action']}")

    # 个股止损警报
    if result["stop_loss_alerts"]:
        print(f"\n  ─── 个股止损警报 ({len(result['stop_loss_alerts'])}) ───")
        for a in result["stop_loss_alerts"]:
            print(f"  · [{a['severity']}] {a['name']} ({a['ticker']}) "
                  f"持有 {int(a['days_held'])} 天 · 跌幅 {a['current_drop_pct']:+.1f}%")
            print(f"      {a['suggested_action']}")

    if not result["alerts"]:
        print(f"\n  🟢 没有触发任何防御信号 — 市场和持仓都健康")

    if notify and severity in ("HIGH", "CRITICAL"):
        n = len(result["alerts"])
        _macos_notify(f"{icon} 实盘防御 · {severity}",
                      f"{n} 条警报 · {result['summary']}")

    # 落 JSON 快照
    snap = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "severity": severity,
        "summary": result["summary"],
        "alerts": result["alerts"],
        "n_market_alerts": len(result["market_alerts"]),
        "n_stop_loss_alerts": len(result["stop_loss_alerts"]),
    }
    store.save_json(snap, config.AUDIT_DIR, "realtime_defense")
    print(f"\n  📁 快照已保存\n{'='*80}\n")
    return snap


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    p = argparse.ArgumentParser(description="实盘防御检查（C 终极版）")
    p.add_argument("--no-feishu", action="store_true",
                   help="(deprecated 2026-05-11 PM,飞书已退役,留参数兼容旧 cron)")
    p.add_argument("--no-notify", action="store_true", help="不弹 macOS 通知")
    args = p.parse_args()
    run(notify=not args.no_notify)
    return 0


if __name__ == "__main__":
    sys.exit(main())
