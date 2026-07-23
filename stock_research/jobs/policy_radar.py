"""政策大事雷达 job — 每晚扫新闻联播，国家级大动作推飞书。

用法:
  python3 -m stock_research.jobs.policy_radar                # 扫今天(新闻联播19:30后)
  python3 -m stock_research.jobs.policy_radar --date 20260713
  python3 -m stock_research.jobs.policy_radar --replay 20260713 20260719 --dry-run

源 = akshare news_cctv(新闻联播文字稿,政策信号浓度最高的公开源)。
打分 = stock_research.core.policy_signals(纯函数,单测锁规则)。
去重 = data/policy_radar_log.jsonl 按标题指纹,同一条只推一次。
推送 = FEISHU_ALERT_WEBHOOK > FEISHU_BRIEF_WEBHOOK(与红警同管道);无信号不推(宁漏勿滥)。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import requests  # noqa: E402

from stock_research.core.policy_signals import score_news, signal_line  # noqa: E402

OUT_JSON = REPO / "data" / "latest" / "policy_radar.json"
LOG_PATH = REPO / "data" / "policy_radar_log.jsonl"
logger = logging.getLogger(__name__)


def _fingerprint(title: str) -> str:
    return hashlib.md5(str(title).strip().encode("utf-8")).hexdigest()[:16]


def _seen_fingerprints() -> set[str]:
    if not LOG_PATH.exists():
        return set()
    out = set()
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        try:
            out.add(json.loads(line).get("fp"))
        except Exception:
            continue
    return out


def fetch_cctv(day: str) -> list[dict]:
    """拉某天新闻联播。akshare 偶发失败,重试一次;失败返回空(明天还会再扫)。"""
    import akshare as ak
    for _ in range(2):
        try:
            df = ak.news_cctv(date=day)
            return [{"date": day, "title": str(r["title"]), "content": str(r["content"])}
                    for _, r in df.iterrows()]
        except Exception as exc:
            logger.warning("news_cctv(%s) 失败: %s", day, exc)
    return []


def _units(item: dict) -> list[tuple[str, str]]:
    """一条新闻 → 打分单元 [(标题, 正文)]。

    「国内联播快讯」是聚合条目(真政策常埋正文、标题无信息)→ 按句拆开逐句打分,
    命中句自身当标题;普通新闻整条打分。
    """
    title, content = item["title"], item["content"]
    if "联播快讯" not in title:
        return [(title, content)]
    sentences = [s.strip() for s in content.replace("\n", "。").split("。") if len(s.strip()) >= 10]
    return [(s, "") for s in sentences]


def scan_day(day: str, seen: set[str]) -> list[dict]:
    signals = []
    for item in fetch_cctv(day):
        for unit_title, unit_content in _units(item):
            v = score_news(unit_title, unit_content)
            if not v["is_signal"]:
                continue
            fp = _fingerprint(unit_title)
            signals.append({
                "fp": fp,
                "date": day,
                "title": unit_title,
                "line": signal_line(unit_title, v),
                "score": v["score"],
                "actors": v["actors"],
                "actions": v["actions"],
                "themes": v["themes"],
                "is_new": fp not in seen,
                "summary": (unit_content or unit_title)[:160],
            })
    return signals


def _build_card(new_signals: list[dict]) -> dict:
    lines = []
    for s in new_signals[:5]:
        lines.append(f"**{s['line']}**")
        if s.get("summary"):
            lines.append(s["summary"][:100] + "…")
    body = "\n".join(lines)
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "orange",
                "title": {"tag": "plain_text",
                          "content": f"📡 政策大事雷达 · {len(new_signals)} 条国家级信号"},
            },
            "elements": [
                {"tag": "markdown", "content": body},
                {"tag": "note", "elements": [{
                    "tag": "plain_text",
                    "content": "源:新闻联播 · 第一时间提醒非内幕 · 提醒关注≠建议买入,方向映射可能出错",
                }]},
            ],
        },
    }


def _push(card: dict) -> bool:
    webhook = (os.environ.get("FEISHU_ALERT_WEBHOOK", "").strip()
               or os.environ.get("FEISHU_BRIEF_WEBHOOK", "").strip())
    if not webhook:
        logger.info("无 webhook 配置，仅打印")
        return False
    try:
        r = requests.post(webhook, json=card, timeout=15)
        return r.status_code == 200 and r.json().get("StatusCode", 0) == 0
    except Exception as exc:
        logger.warning("推送异常: %s", exc)
        return False


def run(days: list[str], *, dry_run: bool = False) -> dict:
    seen = _seen_fingerprints()
    all_signals: list[dict] = []
    for day in days:
        all_signals.extend(scan_day(day, seen))
    new_signals = [s for s in all_signals if s["is_new"]]

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scanned_days": days,
        "signals": all_signals,
        "new_count": len(new_signals),
        "pushed": False,
        "advisory": "第一时间提醒非内幕；提醒关注≠建议买入。",
    }

    for s in all_signals:
        print(("🆕 " if s["is_new"] else "   ") + s["line"])
    if not all_signals:
        print(f"扫描 {days}: 无国家级信号（高门槛，宁漏勿滥）")

    if new_signals and not dry_run:
        result["pushed"] = _push(_build_card(new_signals))
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            for s in new_signals:
                fh.write(json.dumps({"fp": s["fp"], "date": s["date"], "title": s["title"],
                                     "themes": s["themes"]}, ensure_ascii=False) + "\n")
    if not dry_run:
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", help="扫某天 YYYYMMDD(默认今天)")
    p.add_argument("--replay", nargs=2, metavar=("START", "END"), help="回放区间 YYYYMMDD")
    p.add_argument("--dry-run", action="store_true", help="只打印,不推送不落盘")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.replay:
        d0 = datetime.strptime(args.replay[0], "%Y%m%d").date()
        d1 = datetime.strptime(args.replay[1], "%Y%m%d").date()
        days = [(d0 + timedelta(days=i)).strftime("%Y%m%d") for i in range((d1 - d0).days + 1)]
    else:
        days = [args.date or date.today().strftime("%Y%m%d")]
    run(days, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
