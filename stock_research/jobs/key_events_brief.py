"""聚合三层 Key Events → data/latest/key_events.json

输入（不重新抓取，只读已有 JSON）:
  L1 公司事件: data/event_calendar.json          (earnings / 解禁 / 增减持)
  L2 政策事件: data/policy_events.json           (中央 / 部委 / HKMA 政策)
  L3 行业大会: data/curated/industry_events.json (GTC / WWDC / 财报大会 手维护)

输出 schema (data/latest/key_events.json):
  {
    "generated_at": "...",
    "today": "YYYY-MM-DD",
    "horizon_days_forward": 180,
    "horizon_days_backward_policy": 14,
    "horizon_days_backward_company": 7,
    "counts": {"L1_company": N, "L2_policy": N, "L3_industry": N},
    "events": [
       {
         "layer": "L1" | "L2" | "L3",
         "date": "YYYY-MM-DD",
         "title": "...",
         "desc": "...",
         "tickers": [...],
         "importance": 1-5,
         "tense": "today" | "future" | "recent_past",
         ...layer-specific fields
       }, ...
    ]
  }

设计原则（用户 2026-06-01 定）:
  - 只聚合，不假装有未来数据
  - L1 当前 JSON 主要是过去 earnings，取最近 7 天 + 未来已有的
  - L2 政策天然是发布完才有，取最近 14 天 + 未来已有的
  - L3 取未来 180 天，半年手维护一次
  - dashboard fallback: 若本文件缺失，dashboard 直接 fallback 到 industry_events.json

下游消费:
  scripts/pipeline/build_stock_dashboard_html.py → 概览页「关键事件」section
"""
from __future__ import annotations
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

HORIZON_FORWARD_DAYS = 180
HORIZON_BACKWARD_POLICY_DAYS = 14
HORIZON_BACKWARD_COMPANY_DAYS = 7

POLICY_MIN_RELEVANCE = 4


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        logger.warning("缺数据源 %s", path.name)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("解析 %s 失败: %s", path.name, e)
        return None


def _tense(ev_date: date, today: date) -> str:
    if ev_date == today:
        return "today"
    if ev_date > today:
        return "future"
    return "recent_past"


def _collect_l1_company(today: date) -> list[dict]:
    """L1: 公司事件（earnings 等）— 取最近 7 天 + 未来 180 天，仅 importance 高的。

    当前 event_calendar.json 主要是过去 earnings，未来基本为 0。
    筛选规则：
      - earnings 仅取 magnitude 绝对值 ≥ 0.2（盈利同比 ±20%+ 的有看点）
      - unlock/insider 全保留
    """
    data = _load_json(REPO / "data" / "event_calendar.json")
    if not data:
        return []

    window_start = (today - timedelta(days=HORIZON_BACKWARD_COMPANY_DAYS)).isoformat()
    window_end = (today + timedelta(days=HORIZON_FORWARD_DAYS)).isoformat()

    out = []
    for ev in data.get("events", []):
        d = ev.get("event_date", "")
        if not (window_start <= d <= window_end):
            continue

        etype = ev.get("event_type", "")
        magnitude = ev.get("magnitude") or 0

        if etype == "earnings" and abs(magnitude) < 0.2:
            continue

        try:
            ev_date = date.fromisoformat(d)
        except Exception:
            continue

        out.append({
            "layer": "L1",
            "date": d,
            "title": ev.get("description", "")[:80] or "(无标题)",
            "desc": ev.get("description", ""),
            "tickers": [ev.get("code", "")] if ev.get("code") else [],
            "event_type": etype,
            "magnitude": magnitude,
            "market": ev.get("market", ""),
            "source": ev.get("source", ""),
            "importance": 3 if abs(magnitude) >= 0.5 else 2,
            "tense": _tense(ev_date, today),
        })
    return out


def _collect_l2_policy(today: date) -> list[dict]:
    """L2: 政策事件 — 取最近 14 天 relevance≥4 的 + 未来 180 天有的（极罕见）。"""
    data = _load_json(REPO / "data" / "policy_events.json")
    if not data:
        return []

    window_start = (today - timedelta(days=HORIZON_BACKWARD_POLICY_DAYS)).isoformat()
    window_end = (today + timedelta(days=HORIZON_FORWARD_DAYS)).isoformat()

    out = []
    for ev in data.get("events", []):
        d = ev.get("date", "")
        if not (window_start <= d <= window_end):
            continue
        if (ev.get("relevance_score") or 0) < POLICY_MIN_RELEVANCE:
            continue

        try:
            ev_date = date.fromisoformat(d)
        except Exception:
            continue

        out.append({
            "layer": "L2",
            "date": d,
            "title": ev.get("title", "(无标题)"),
            "desc": (ev.get("full_text") or "")[:200],
            "tickers": [],
            "themes": ev.get("matched_themes", []),
            "source_authority": ev.get("source_authority", ""),
            "relevance_score": ev.get("relevance_score", 0),
            "importance": min(5, ev.get("relevance_score", 0)),
            "tense": _tense(ev_date, today),
        })
    return out


def _collect_l3_industry(today: date) -> list[dict]:
    """L3: 行业大会 — curated/industry_events.json，未来 180 天 + 最近 7 天保留。"""
    data = _load_json(REPO / "data" / "curated" / "industry_events.json")
    if not data:
        logger.warning("L3 数据源缺失 — 需要先创建 data/curated/industry_events.json")
        return []

    window_start = (today - timedelta(days=HORIZON_BACKWARD_COMPANY_DAYS)).isoformat()
    window_end = (today + timedelta(days=HORIZON_FORWARD_DAYS)).isoformat()

    out = []
    for ev in data.get("events", []):
        d = ev.get("start_date", "")
        if not (window_start <= d <= window_end):
            continue

        try:
            ev_date = date.fromisoformat(d)
        except Exception:
            continue

        item = {
            "layer": "L3",
            "date": d,
            "end_date": ev.get("end_date", d),
            "title": ev.get("title", "(无标题)"),
            "desc": ev.get("desc", ""),
            "tickers": ev.get("tickers", []),
            "themes": ev.get("themes", []),
            "event_type": ev.get("event_type", "industry_conference"),
            "importance": ev.get("importance", 3),
            "source_url": ev.get("source_url", ""),
            "timezone": ev.get("timezone", ""),
            "tense": _tense(ev_date, today),
            "updated_at": ev.get("updated_at", ""),
            # 5 段分析（手维护，未来 LLM 自动补）
            "watch": ev.get("watch", ""),
            "bull": ev.get("bull", ""),
            "bear": ev.get("bear", ""),
            "prep": ev.get("prep", ""),
            "history": ev.get("history", ""),
        }
        out.append(item)
    return out


def main() -> int:
    today = date.today()
    logger.info("聚合 Key Events · today=%s · 前向 %d 天 / 后向政策 %d 天 / 后向公司 %d 天",
                today, HORIZON_FORWARD_DAYS,
                HORIZON_BACKWARD_POLICY_DAYS, HORIZON_BACKWARD_COMPANY_DAYS)

    l1 = _collect_l1_company(today)
    l2 = _collect_l2_policy(today)
    l3 = _collect_l3_industry(today)

    # 合并 + 按日期排 + 同日按 importance 降序
    all_events = l1 + l2 + l3
    all_events.sort(key=lambda x: (x["date"], -x.get("importance", 0)))

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "horizon_days_forward": HORIZON_FORWARD_DAYS,
        "horizon_days_backward_policy": HORIZON_BACKWARD_POLICY_DAYS,
        "horizon_days_backward_company": HORIZON_BACKWARD_COMPANY_DAYS,
        "policy_min_relevance": POLICY_MIN_RELEVANCE,
        "counts": {
            "L1_company": len(l1),
            "L2_policy": len(l2),
            "L3_industry": len(l3),
            "total": len(all_events),
        },
        "events": all_events,
    }

    out = REPO / "data" / "latest" / "key_events.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    logger.info("✅ %s · L1=%d  L2=%d  L3=%d  total=%d",
                out.relative_to(REPO), len(l1), len(l2), len(l3), len(all_events))
    return 0


if __name__ == "__main__":
    sys.exit(main())
