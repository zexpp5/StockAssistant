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
from ..adapters import feishu, store
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


def _write_alerts_to_feishu(stop_alerts: list[dict[str, Any]]) -> dict[str, int]:
    """把单股止损警报写入 picks 表的「风险提示」字段（追加，不覆盖）。

    使用 picks 表的 record_id 直接 update。
    """
    if not stop_alerts:
        return {"updated": 0, "failed": 0}

    picks = feishu.fetch_picks()
    by_code = {(p["normalized"].get("code") or "").upper(): p for p in picks}

    updated = 0
    failed = 0
    today = datetime.now().strftime("%Y-%m-%d")
    for a in stop_alerts:
        code = (a.get("ticker") or "").upper()
        if not code:
            continue
        # 找到 picks 表里最新的（持有天数最大的）那条记录
        candidates = [p for c, p in by_code.items() if c == code]
        if not candidates:
            continue
        # 简化：取第一条（实际可以选 days_held 最大那条）
        rec = candidates[0]

        # 拼"风险提示"新文本
        old_risk = ""
        f = rec.get("fields", {})
        v = f.get("风险提示", "")
        if isinstance(v, list) and v:
            old_risk = v[0].get("text", "") if isinstance(v[0], dict) else str(v[0])
        elif isinstance(v, str):
            old_risk = v

        alert_line = (f"🚨 [{today}] STOP-LOSS 触发：{a['current_drop_pct']:+.1f}% ≤ "
                      f"{a['threshold_pct']:+.0f}% · {a['suggested_action']}")
        # 避免重复追加（如果今天已经写过）
        if alert_line.split("·")[0] in old_risk:
            continue
        new_risk = (alert_line + "\n\n" + old_risk) if old_risk else alert_line

        try:
            resp = feishu.update_record(
                rec["record_id"],
                {"风险提示": new_risk},
                table_id=config.DAILY_PICKS_TABLE_ID,
            )
            if resp.get("code") == 0:
                updated += 1
            else:
                failed += 1
                logger.warning("更新失败 %s: %s", code, resp.get("msg"))
        except Exception as e:
            failed += 1
            logger.warning("更新异常 %s: %s", code, e)

    return {"updated": updated, "failed": failed}


# ─────────── 主流程 ───────────

def run(write_feishu: bool = True, notify: bool = True) -> dict:
    print(f"\n{'='*80}")
    print(f"  🛡 实盘防御检查 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*80}\n")

    # 1. 拉今日 picks
    print("[1/3] 拉飞书 picks 表...")
    picks_raw = feishu.fetch_picks()
    print(f"  共 {len(picks_raw)} 条")

    # 2. 综合诊断
    print("\n[2/3] 检查市场层（VIX + 200MA）+ 个股层（-15% 止损）...")
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

    # 3. 写飞书 + 通知
    if write_feishu and result["stop_loss_alerts"]:
        print(f"\n[3/3] 写入飞书 picks 表「风险提示」字段...")
        wr = _write_alerts_to_feishu(result["stop_loss_alerts"])
        print(f"  更新 {wr['updated']} / 失败 {wr['failed']}")
    elif not write_feishu:
        print(f"\n[3/3] 跳过飞书写入（--no-feishu）")
    else:
        print(f"\n[3/3] 无止损警报，跳过飞书写入")

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
    p.add_argument("--no-feishu", action="store_true", help="不写飞书")
    p.add_argument("--no-notify", action="store_true", help="不弹 macOS 通知")
    args = p.parse_args()
    run(write_feishu=not args.no_feishu, notify=not args.no_notify)
    return 0


if __name__ == "__main__":
    sys.exit(main())
