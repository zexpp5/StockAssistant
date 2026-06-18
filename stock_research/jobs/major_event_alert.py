"""统一「重大事件」红色警报 job —— 收敛多路源，只在真·重大(🔴)推一条。

设计：本 job 不重算信号，只读各源已算好的最新状态（大盘防御 / 盘前 / 持仓日内 /
财报），交给 stock_research.core.major_event_alert 聚合判定。达到 🔴(CRITICAL) 才推
飞书，且用指纹去重（同一事件不反复轰炸）；从重大恢复平静推一条"已解除"。

出口：
  - 飞书：FEISHU_ALERT_WEBHOOK > FEISHU_BRIEF_WEBHOOK（与 defense_watcher 同 webhook）
  - 首页红条：写 data/latest/major_event_alert.json（dashboard 读它渲染顶部红条）
  - 去重状态：data/major_event_alert_state.json

用法：
  python3 -m stock_research.jobs.major_event_alert            # 正常：升档/恢复才推
  python3 -m stock_research.jobs.major_event_alert --dry-run  # 只打印不推、不写状态
  python3 -m stock_research.jobs.major_event_alert --force    # 强制推当前一次（测试）

安全边界：只读源 JSON + 推送/写产物，不读写 DB、不动持仓/推荐/策略。研究风控提示，非交易指令。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import requests

_REPO = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(_REPO / ".env")

from stock_research.core import major_event_alert as core  # noqa: E402

logger = logging.getLogger(__name__)

LATEST_DIR = _REPO / "data" / "latest"
OUT_JSON = LATEST_DIR / "major_event_alert.json"
STATE_FILE = _REPO / "data" / "major_event_alert_state.json"
TEMPLATE = {"NONE": "blue", "LOW": "yellow", "HIGH": "orange", "CRITICAL": "red"}

# 持仓日内达到这些幅度 → 升为 CRITICAL（够格进统一红警）。其余持仓异动留在 defense 橙卡。
HOLDING_PORTFOLIO_CRITICAL = -3.0
HOLDING_SINGLE_CRITICAL = -8.0


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception as exc:
        logger.warning("读 %s 失败: %s", path.name, exc)
        return {}


def _holding_signal(state: dict) -> dict | None:
    """从 defense_watcher_state 的持仓指纹判持仓异动严重度。

    组合加权 ≤ -3% 或单票 ≤ -8% → CRITICAL（进红警）；否则按 defense 原级别 HIGH。
    """
    fp = str(state.get("holding_alert_fingerprint") or "")
    if not fp or not state.get("last_holding_alert_at"):
        return None
    nums = [float(x) for x in re.findall(r"-?\d+\.?\d*", fp)]
    worst = min(nums) if nums else 0.0
    is_portfolio = "PORTFOLIO" in fp.upper() or "组合" in fp
    crit = (is_portfolio and worst <= HOLDING_PORTFOLIO_CRITICAL) or \
           (not is_portfolio and worst <= HOLDING_SINGLE_CRITICAL)
    sev = "CRITICAL" if crit else "HIGH"
    return {"source": "持仓异动", "severity": sev,
            "headline": fp, "detail": "组合加权 ≤-3% 或单票 ≤-8% 入红警",
            "key": f"holding:{fp}"}


def collect_signals() -> list[dict]:
    """读各源最新状态 → 归一化信号列表（不重算）。"""
    signals: list[dict] = []

    # 大盘防御（defense_watcher 的当前市场 severity）
    dstate = _read_json(_REPO / "data" / "defense_watcher_state.json")
    dsev = str(dstate.get("last_severity") or "NONE").upper()
    if dsev != "NONE":
        signals.append({"source": "大盘防御", "severity": dsev,
                        "headline": f"大盘防御当前 {dsev}",
                        "key": f"defense:{dsev}:{dstate.get('last_check_at','')[:10]}"})
    hsig = _holding_signal(dstate)
    if hsig:
        signals.append(hsig)

    # 盘前环境（premarket_gate 的 color）
    pg = _read_json(LATEST_DIR / "premarket_gate.json")
    psev = str(pg.get("color") or "NONE").upper()
    if psev != "NONE":
        signals.append({"source": "盘前环境", "severity": psev,
                        "headline": str(pg.get("headline_plain") or pg.get("top_alarm") or f"盘前 {psev}"),
                        "key": f"premarket:{psev}:{str(pg.get('as_of') or '')[:10]}"})

    # 财报领先信号（领先提醒，非崩盘 → 最高按 HIGH 入列做上下文，默认不触红线）
    er = _read_json(LATEST_DIR / "earnings_revision_signals.json")
    ersigs = er.get("signals") if isinstance(er.get("signals"), list) else []
    for s in ersigs:
        sev = str(s.get("severity") or "").upper()
        if sev in ("HIGH", "CRITICAL"):
            sym = s.get("symbol") or s.get("ticker") or "?"
            signals.append({"source": "财报", "severity": "HIGH" if sev == "CRITICAL" else sev,
                            "headline": f"{sym} {s.get('headline') or s.get('summary') or '财报领先信号'}",
                            "key": f"earnings:{sym}:{str(er.get('snapshot_date') or '')}"})
    return signals


def _build_card(result: dict) -> dict:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    if result.get("recovered"):
        content = "**重大警报已解除**，市场/持仓/盘前回到常态。\n之前的红色事件不再活跃。"
        template = "green"
        title = f"🟢 重大警报解除 · {now_str}"
    else:
        lines = ["**触发统一重大事件红警（只在真·重大时响）**\n"]
        for ev in result.get("major_events", [])[:8]:
            lines.append(f"• 🔴 **{ev['source']}**：{ev['headline']}")
        others = [s for s in result.get("all_signals", [])
                  if core._order(s["severity"]) < core._order(result.get("threshold", "CRITICAL"))
                  and s["severity"] != "NONE"]
        if others:
            lines.append("\n_次级（未达红线，仅参考）_：" +
                         "；".join(f"{o['source']}{core.ICON.get(o['severity'],'')}" for o in others[:5]))
        content = "\n".join(lines)
        template = "red"
        title = f"🔴 重大事件 · {len(result.get('major_events', []))} 项 · {now_str}"
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "subtitle": {"tag": "plain_text", "content": "advisory · 风控提示，不构成交易指令"},
                "template": template,
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}},
                {"tag": "note", "elements": [{
                    "tag": "plain_text",
                    "content": "收敛自 大盘防御/盘前/持仓日内/财报；只在 🔴 级别推，平时不打扰。",
                }]},
            ],
        },
    }


def _push(card: dict) -> bool:
    webhook = (os.environ.get("FEISHU_ALERT_WEBHOOK", "").strip()
               or os.environ.get("FEISHU_BRIEF_WEBHOOK", "").strip())
    if not webhook:
        logger.info("无 webhook 配置，仅打印；export FEISHU_ALERT_WEBHOOK=... 启用推送")
        return False
    try:
        r = requests.post(webhook, json=card, timeout=15)
        ok = r.status_code == 200 and r.json().get("StatusCode", 0) == 0
        if not ok:
            logger.warning("webhook 推送失败 (%s): %s", r.status_code, r.text[:200])
        return ok
    except Exception as exc:
        logger.warning("webhook 推送异常: %s", exc)
        return False


def run(*, dry_run: bool = False, force: bool = False, threshold: str = core.DEFAULT_THRESHOLD) -> dict:
    prev_state = _read_json(STATE_FILE)
    signals = collect_signals()
    result = core.aggregate_major_alert(signals, prev_state, threshold=threshold)
    result["generated_at"] = datetime.now().isoformat(timespec="seconds")

    do_push = force or result["should_push"] or result["recovered"]
    pushed = False
    if do_push:
        card = _build_card(result)
        if dry_run:
            logger.info("[dry-run] 将推送：%s", result["headline"])
        else:
            pushed = _push(card)
    result["pushed"] = pushed

    if not dry_run:
        LATEST_DIR.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        new_state = dict(result["state"])
        new_state["updated_at"] = result["generated_at"]
        STATE_FILE.write_text(json.dumps(new_state, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="统一重大事件红色警报")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--threshold", default=core.DEFAULT_THRESHOLD,
                    choices=list(core.SEVERITY_ORDER))
    args = ap.parse_args(argv)
    result = run(dry_run=args.dry_run, force=args.force, threshold=args.threshold)
    print(result["headline"], "| should_push=", result["should_push"],
          "| pushed=", result.get("pushed"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
