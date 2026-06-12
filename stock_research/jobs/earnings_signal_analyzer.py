"""财报信号自动体检 — 财报发布次日，AI 替用户读财报，结论直接推飞书。

闭环的最后一环（用户看不懂财报，也不知道哪天有财报）：
  财报日傍晚  → bottleneck_earnings_reminder 推「财报窗口到了」提醒卡
  次日 08:30  → 本 job 自动跑：headless claude 联网搜该公司财报新闻稿/电话会要点，
               对照同一份清单逐条给 ✅/⚠️/❌ 白话体检，包成飞书卡推送。
  分析失败    → 推兜底卡（"自动分析失败，打开 Claude 说『帮我看 X 财报』"），绝不静默。

监控名单与清单复用 bottleneck_earnings_reminder.GROUPS（单一来源）：
  bottleneck 组 GEV/VRT/MU + capex 组 MSFT/GOOGL/AMZN/META。

窗口：北京日期 ∈ [财报日+1, 财报日+3]（盘后发布的财报北京次日凌晨才出，+1 起算最稳），
     按 (ticker, 年-月) 去重 = 每家每季分析一次。
依赖：/opt/homebrew/bin/claude（headless -p 模式，只放行 WebSearch/WebFetch）。
产出：data/earnings_analyzer_state.json（防重复）。

CLI：
  python3 -m stock_research.jobs.earnings_signal_analyzer                 # 正常跑
  python3 -m stock_research.jobs.earnings_signal_analyzer --dry-run       # 只列在窗事件，不分析不推
  python3 -m stock_research.jobs.earnings_signal_analyzer --as-of 2026-06-25 --dry-run
  python3 -m stock_research.jobs.earnings_signal_analyzer --test MU       # 拿该票最近一次已发财报实测分析，打印不推送
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from stock_research.core import bottleneck_signals as bs  # noqa: E402
from stock_research.jobs.bottleneck_earnings_reminder import (  # noqa: E402
    CALENDAR_JSON, GROUPS,
)

logger = logging.getLogger(__name__)

STATE_FILE = _REPO / "data" / "earnings_analyzer_state.json"
CLAUDE_BIN = "/opt/homebrew/bin/claude"
LLM_TIMEOUT_S = 900  # 单家上限 15 分钟（联网搜索+读财报）

# 财报日 +1 起分析（盘后财报北京次日凌晨才出），+3 截止（周末/改期缓冲）
ANALYZE_FROM_DAYS = 1
ANALYZE_TO_DAYS = 3

_TICKER_GROUP = {t: g for g, spec in GROUPS.items() for t in spec["tickers"]}


def _load_calendar_events() -> list[dict]:
    if not CALENDAR_JSON.exists():
        logger.warning("事件日历不存在：%s", CALENDAR_JSON.name)
        return []
    try:
        doc = json.loads(CALENDAR_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("事件日历读取失败：%s", exc)
        return []
    out = []
    for ev in doc.get("events") or []:
        sym = str(ev.get("ticker") or ev.get("symbol") or "").upper()
        if sym not in _TICKER_GROUP:
            continue
        if ev.get("event_type") not in ("earnings", "earnings_upcoming"):
            continue
        try:
            ed = date.fromisoformat(str(ev.get("event_date") or "")[:10])
        except Exception:
            continue
        out.append({"ticker": sym, "event_date": ed, "group": _TICKER_GROUP[sym]})
    return out


def _due_events(as_of: date) -> list[dict]:
    due: dict[str, dict] = {}
    for ev in sorted(_load_calendar_events(), key=lambda x: x["event_date"]):
        lo = ev["event_date"] + timedelta(days=ANALYZE_FROM_DAYS)
        hi = ev["event_date"] + timedelta(days=ANALYZE_TO_DAYS)
        if lo <= as_of <= hi:
            due[ev["ticker"]] = ev
    return list(due.values())


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("读 state 失败: %s", exc)
    return {}


def _dedup_key(ev: dict) -> str:
    return f"{ev['ticker']}:{ev['event_date'].isoformat()[:7]}"


def _build_prompt(ev: dict, as_of: date) -> str:
    meta = GROUPS[ev["group"]]["tickers"][ev["ticker"]]
    checks = "\n".join(f"{i}. {c}" for i, c in enumerate(meta["checks"], 1))
    return f"""你是股票研究助手。今天是 {as_of.isoformat()}。{meta['name']}（美股代码 {ev['ticker']}）在 {ev['event_date'].isoformat()} 前后发布了最新季度财报。

用 WebSearch 搜索这份财报的新闻稿、业绩要点和财报电话会报道（建议用英文关键词如 "{ev['ticker']} earnings results guidance"，确认是刚发布的这一季），然后只输出下面格式的中文体检报告，不要输出任何其他内容（不要开场白、不要解释过程）：

**{meta['name']} 财报信号体检**
逐条核对（每条以 ✅/⚠️/❌ 开头 + 一句话事实，必须带你搜到的具体数字或管理层原话）：
{checks}

**结论**：一句话判断信号方向（仍在抢货 / 供给追上 / 需求转弱 / 证据不足），再给一句对应提醒。提醒必须是「建议复查/暂停加仓」式的 advisory 措辞，绝不能出现"必须买/必须卖"。
**信源**：列出你实际读到的 1-2 个来源（媒体名+标题，不用链接）。

最后单独输出一行机读行（严格此格式，机器解析用）：
DRAFT: 转强或持平或转弱或不确定 ; TIER: A或B或C ; NOTE: 不超过30字的关键数字或原话
（转强=信号仍紧俏；持平=方向没变化；转弱=出现退潮证据；证据不足时写"不确定"。
TIER 按你信源的硬度：A=公司新闻稿/财报原文，B=电话会/管理层表态的报道，C=媒体二手转述。）

硬性要求：每条都基于真实搜索结果；搜不到的条目写"未披露/未搜到"，禁止编造数字；总长度不超过 16 行；全部用中文。"""


def _run_llm(prompt: str) -> str | None:
    """headless claude 联网分析。失败/超时返回 None，由调用方推兜底卡。"""
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", prompt,
             "--allowedTools", "WebSearch,WebFetch",
             "--model", "sonnet"],
            capture_output=True, text=True, timeout=LLM_TIMEOUT_S, cwd="/tmp")
        out = (r.stdout or "").strip()
        if r.returncode != 0 or len(out) < 50:
            logger.warning("claude 分析失败 rc=%s stderr=%s out_len=%d",
                           r.returncode, (r.stderr or "")[:200], len(out))
            return None
        return out
    except subprocess.TimeoutExpired:
        logger.warning("claude 分析超时（>%ds）", LLM_TIMEOUT_S)
        return None
    except Exception as exc:
        logger.warning("claude 调用异常: %s", exc)
        return None


_DRAFT_RE = re.compile(
    r"DRAFT[:：]\s*(转强|持平|转弱|不确定)\s*[;；]\s*TIER[:：]\s*([ABCabc]?)\s*[;；]\s*NOTE[:：]\s*(.*)")


def _split_draft_line(analysis: str) -> tuple[str, dict | None]:
    """从分析文本剥出最后的机读行。返回 (展示用文本, 草稿字段 dict 或 None)。

    模型没按格式输出 / 写"不确定" → 不出草稿，只展示正文（不硬编结论）。
    """
    m = _DRAFT_RE.search(analysis)
    if not m:
        return analysis, None
    display = (analysis[:m.start()] + analysis[m.end():]).strip()
    conclusion, tier, note = m.group(1), m.group(2).upper(), m.group(3).strip()
    if conclusion == "不确定":
        return display, None
    return display, {"conclusion": conclusion, "evidence_tier": tier,
                     "note": note[:60]}


def _save_draft_for(ev: dict, draft: dict) -> bool:
    """把解析出的结论写进红绿灯草稿（人工已确认的同季不覆盖）。"""
    try:
        quarter = bs.prev_quarter(bs.quarter_of(ev["event_date"]))
        saved = bs.save_draft(ev["ticker"], quarter, draft["conclusion"],
                              draft["evidence_tier"], "", draft["note"])
        return saved is not None
    except Exception as exc:
        logger.warning("写红绿灯草稿失败 %s: %s", ev["ticker"], exc)
        return False


def _build_result_card(ev: dict, analysis: str, as_of: date,
                       draft_saved: bool = False) -> dict:
    spec = GROUPS[ev["group"]]
    note = (
        "🤖 AI 自动联网读财报生成，数字可能有误——重要决定前可打开 Claude 让我复核。"
        "判读规则见前一晚的提醒卡 · 仅供研究参考，不构成买卖指令"
    )
    if draft_saved:
        note = ("✏️ 已按本卡结论写入红绿灯草稿——到 dashboard「催化信号验证」页"
                "点 ✓ 确认或修改后才正式记账。 · " + note)
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"📋 财报信号体检 · {ev['ticker']} · {as_of.isoformat()}"},
                "subtitle": {"tag": "plain_text",
                             "content": f"{spec['title']}的自动解读 · 财报日 {ev['event_date'].isoformat()}"},
                "template": "blue",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": analysis}},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": note}]},
            ],
        },
    }


def _build_failure_card(ev: dict, as_of: date) -> dict:
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"📋 财报体检未生成 · {ev['ticker']}"},
                "template": "grey",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": (
                    f"{ev['ticker']} 的财报自动分析没跑成（搜索失败或超时）。\n"
                    f"**手动兜底**：打开 Claude 说一句「帮我看 {ev['ticker']} 财报」，"
                    "我现场分析给你。"
                )}},
            ],
        },
    }


def run(now: datetime | None = None, dry_run: bool = False) -> int:
    as_of = (now or datetime.now()).date()
    due = _due_events(as_of)
    if not due:
        logger.info("财报体检：%s 无在窗事件", as_of)
        return 0

    state = _load_state()
    done: dict = state.get("analyzed") or {}
    fresh = [ev for ev in due if _dedup_key(ev) not in done]
    if not fresh:
        logger.info("财报体检：在窗事件本季均已分析（%s）",
                    "、".join(e["ticker"] for e in due))
        return 0

    if dry_run:
        for ev in fresh:
            print(f"[dry-run] 将分析 {ev['ticker']}（财报日 {ev['event_date']}，组 {ev['group']}）")
        return len(fresh)

    from stock_research.jobs.premarket_gate import _push  # 复用同一 webhook
    n_ok = 0
    for ev in fresh:
        logger.info("财报体检：开始分析 %s（财报日 %s）...", ev["ticker"], ev["event_date"])
        analysis = _run_llm(_build_prompt(ev, as_of))
        draft_saved = False
        if analysis:
            analysis, draft = _split_draft_line(analysis)
            if draft:
                draft_saved = _save_draft_for(ev, draft)
                logger.info("红绿灯草稿 %s：%s", ev["ticker"],
                            f"已写入（{draft['conclusion']}）" if draft_saved
                            else "跳过（同季已人工确认或写入失败）")
        card = (_build_result_card(ev, analysis, as_of, draft_saved) if analysis
                else _build_failure_card(ev, as_of))
        ok = _push(card)
        logger.info("财报体检：%s %s → 推送%s", ev["ticker"],
                    "分析成功" if analysis else "兜底卡", "成功" if ok else "失败")
        # 推送成功才记账；分析失败也记账（兜底卡已指引手动路径，不无限重试烧钱）
        if ok:
            done[_dedup_key(ev)] = {
                "event_date": ev["event_date"].isoformat(),
                "analyzed_at": datetime.now().isoformat(timespec="seconds"),
                "llm_ok": bool(analysis),
            }
            n_ok += 1
    if n_ok:
        state["analyzed"] = done
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    return n_ok


def _test_one(ticker: str) -> int:
    """实测：取该票最近一次已发布财报，跑完整分析，只打印不推送不记账。"""
    ticker = ticker.upper()
    past = [ev for ev in _load_calendar_events()
            if ev["ticker"] == ticker and ev["event_date"] <= date.today()]
    if not past:
        print(f"日历里没有 {ticker} 的历史财报事件")
        return 1
    ev = max(past, key=lambda x: x["event_date"])
    as_of = ev["event_date"] + timedelta(days=ANALYZE_FROM_DAYS)
    print(f"实测 {ticker}：财报日 {ev['event_date']}，模拟分析日 {as_of}，调用 claude 联网分析中（最长 {LLM_TIMEOUT_S//60} 分钟）...")
    analysis = _run_llm(_build_prompt(ev, as_of))
    if not analysis:
        print("❌ 分析失败（生产环境会推兜底卡）")
        return 1
    print("✅ 分析成功，卡片正文如下：\n")
    print(analysis)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="财报信号自动体检（AI 替用户读财报）")
    p.add_argument("--dry-run", action="store_true", help="只列在窗事件，不分析不推送")
    p.add_argument("--as-of", help="模拟日期 YYYY-MM-DD（测试用）")
    p.add_argument("--test", metavar="TICKER", help="实测指定票最近一次财报的完整分析，打印不推送")
    args = p.parse_args()
    if args.test:
        return _test_one(args.test)
    now = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now()
    n = run(now=now, dry_run=args.dry_run)
    print(f"本次处理事件数：{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
