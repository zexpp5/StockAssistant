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
import signal
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from .. import config
from ..adapters import store
from ..core import defense_signals

logger = logging.getLogger("stock_research.jobs.realtime_defense")

DEFAULT_DIAGNOSE_TIMEOUT_SECONDS = 45


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


class _DiagnoseTimeout(RuntimeError):
    pass


@contextmanager
def _deadline(seconds: int):
    """Unix-only soft deadline so the production pipeline can still save a snapshot.

    The outer morning pipeline gives this step about 60 seconds.  Some yfinance
    calls (especially SPY option chains) have historically hung past that, so the
    job keeps its own shorter deadline and degrades instead of leaving no output.
    """
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)

    def _raise_timeout(_signum, _frame):
        raise _DiagnoseTimeout(f"diagnose_all exceeded {seconds}s")

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


def _degraded_result(reason: str) -> dict[str, Any]:
    return {
        "alerts": [{
            "type": "REALTIME_DEFENSE_DEGRADED",
            "severity": "LOW",
            "trigger": reason[:160],
            "suggested_action": "防御检查外部数据源降级：不要据此加仓；可稍后重跑实时防御检查确认。",
        }],
        "severity": "LOW",
        "market_alerts": [],
        "stop_loss_alerts": [],
        "summary": f"⚠️ 防御检查降级：{reason[:80]}",
        "degraded": True,
        "degrade_reason": reason[:240],
    }


def _apply_preflight_alert(result: dict[str, Any], alert: dict[str, Any] | None) -> dict[str, Any]:
    if not alert:
        return result
    result.setdefault("alerts", []).append(alert)
    result.setdefault("market_alerts", []).append(alert)
    if result.get("severity") == "NONE":
        result["severity"] = "LOW"
    old_summary = str(result.get("summary") or "")
    result["summary"] = f"{old_summary} · 防御读取降级" if old_summary else "⚠️ 防御读取降级"
    result["degraded"] = True
    result["degrade_reason"] = alert.get("trigger")
    return result


# ─────────── 主流程 ───────────
# 2026-05-11 PM 第二轮:_write_alerts_to_feishu 已删 — 飞书 Bitable 100% 退役.
# alerts 已通过 3 个渠道留存:webhook 推送(defense_watcher) + JSON 快照
# (AUDIT_DIR/realtime_defense.json) + DuckDB snapshots(category='audit').

def run(
    notify: bool = True,
    *,
    include_options: bool = False,
    diagnose_timeout_seconds: int = DEFAULT_DIAGNOSE_TIMEOUT_SECONDS,
    **_legacy_kwargs,
) -> dict:
    print(f"\n{'='*80}")
    print(f"  🛡 实盘防御检查 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*80}\n")

    # 1. 拉今日 picks (V2 recommendation_picks)
    print("[1/2] 拉 picks [DuckDB · V2]...")
    import sys as _sys
    _sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
    from stock_db import fetch_picks_normalized
    preflight_alert = None
    try:
        picks_raw = fetch_picks_normalized()
    except Exception as e:
        logger.warning("fetch picks degraded: %s", str(e)[:160])
        picks_raw = []
        preflight_alert = {
            "type": "PICKS_DATA_UNAVAILABLE",
            "severity": "LOW",
            "trigger": f"推荐池读取失败：{type(e).__name__}: {str(e)[:120]}",
            "suggested_action": "个股止损检查本轮降级：不要据此加仓；稍后 DB 写锁释放后重跑确认。",
        }
    print(f"  共 {len(picks_raw)} 条")

    # 2. 综合诊断
    print("\n[2/2] 检查市场层（VIX + 200MA）+ 个股层（-15% 止损）...")
    if not include_options:
        print("  · 生产批处理跳过 SPY 期权 PCR（避免 Yahoo 期权链超时拖垮整条流水线）")
    try:
        with _deadline(diagnose_timeout_seconds):
            result = defense_signals.diagnose_all(
                picks_raw,
                include_options=include_options,
            )
    except _DiagnoseTimeout as e:
        logger.warning("realtime defense degraded by timeout: %s", e)
        result = _degraded_result(str(e))
    except Exception as e:
        logger.exception("realtime defense degraded by exception")
        result = _degraded_result(f"{type(e).__name__}: {str(e)[:160]}")
    result = _apply_preflight_alert(result, preflight_alert)

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
        "degraded": bool(result.get("degraded")),
        "degrade_reason": result.get("degrade_reason"),
        "options_pcr": "included" if include_options else "skipped_in_production_batch",
        "diagnose_timeout_seconds": diagnose_timeout_seconds,
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
    p.add_argument("--include-options", action="store_true",
                   help="包含 SPY 期权 PCR。默认跳过，避免 yfinance 期权链慢请求拖垮生产批处理")
    p.add_argument("--diagnose-timeout-seconds", type=int,
                   default=DEFAULT_DIAGNOSE_TIMEOUT_SECONDS,
                   help="综合诊断软超时；超时会保存降级快照而不是让 pipeline 无输出")
    args = p.parse_args()
    run(
        notify=not args.no_notify,
        include_options=args.include_options,
        diagnose_timeout_seconds=args.diagnose_timeout_seconds,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
