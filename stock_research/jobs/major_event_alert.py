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
    # 只认百分比数字（避免把 167.00/447.00 这种价格线误当跌幅）。
    pct_nums = [float(x) for x in re.findall(r"(-?\d+(?:\.\d+)?)%", fp)]
    worst = min(pct_nums) if pct_nums else 0.0
    is_portfolio = "PORTFOLIO" in fp.upper() or "组合" in fp
    discipline_hit = ("纪律线" in fp and "触发" in fp) or ("止损" in fp)
    crit = (discipline_hit
            or (is_portfolio and worst <= HOLDING_PORTFOLIO_CRITICAL)
            or (not is_portfolio and worst <= HOLDING_SINGLE_CRITICAL))
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


def _rank_industry_lookup() -> dict:
    """{ticker: {rank, industry}} —— 来自 discovery_candidates（系统当前推荐榜）。

    行业优先用 candidate.sector，缺失退赛道分类（classify_theme，中文标签）。
    不在榜里的（自选/未入榜）→ rank=None。
    """
    out: dict[str, dict] = {}
    try:
        from stock_research.core.monthly_actions import classify_theme
    except Exception:
        classify_theme = lambda tk, raw: ""  # noqa: E731

    # 行业兜底源：全宇宙 symbol→industry（含 US/HK/A股，比 top20 全得多）。
    industry_by_tk: dict[str, str] = {}
    audit = _read_json(_REPO / "data" / "latest" / "recommendation_data_usability_audit.json")
    for bucket in ("selected", "attention", "blocked", "review_gated"):
        for it in (audit.get(bucket) or []):
            tk = str(it.get("symbol") or it.get("ticker") or it.get("code") or "").upper()
            ind = it.get("industry") or it.get("sector")
            if tk and ind and tk not in industry_by_tk:
                industry_by_tk[tk] = str(ind)

    # 排名 + 行业（行业优先 candidate.sector）。
    d = _read_json(_REPO / "data" / "discovery_candidates.json")
    for c in (d.get("candidates") or d.get("items") or []):
        tk = str(c.get("ticker") or c.get("code") or "").upper()
        if not tk:
            continue
        sector = c.get("sector") or c.get("industry") or c.get("theme")
        industry = sector or industry_by_tk.get(tk) or classify_theme(tk, str(c.get("name") or ""))
        out[tk] = {"rank": c.get("rank"), "industry": industry}

    # 不在 top20 但有行业的，也补进来（rank=None）。
    for tk, ind in industry_by_tk.items():
        if tk not in out:
            out[tk] = {"rank": None, "industry": ind}
    return out


def collect_opportunities() -> list[dict]:
    """🟢 机会：盘前 buy_signals 里"便宜"(跌进可买区)的票。按代码去重(新便宜才提醒)。

    每只附带系统当前排名 + 行业（用户要求：知道它现在排第几、属哪个行业）。
    """
    from stock_research.core.monthly_actions import classify_theme
    opps: list[dict] = []
    pg = _read_json(LATEST_DIR / "premarket_gate.json")
    green = ((pg.get("buy_signals") or {}).get("green")) or []
    ri = _rank_industry_lookup()
    for g in green:
        sym = str(g.get("symbol") or "").upper()
        if not sym:
            continue
        low, high, cur = g.get("low"), g.get("high"), g.get("current")
        disc = g.get("discount_pct")
        src = "/".join(g.get("sources") or []) or "票池"
        zone = f"${low:g}~${high:g}" if (low is not None and high is not None) else ""
        curtxt = f"现价${cur:g}" if cur is not None else ""
        disctxt = f"，比目标低{abs(disc):g}%" if isinstance(disc, (int, float)) else ""
        meta = ri.get(sym) or {}
        rank = meta.get("rank")
        industry = meta.get("industry") or classify_theme(sym, "")
        rank_txt = f"系统排名#{rank}" if rank is not None else "未进今日Top20"
        ind_txt = f" · 行业 {industry}" if industry else ""

        # 2026-07-24: 源头已算好接飞刀/目标价过期警示,此前被卡片丢弃 →
        # 把 flags 带上;带警示的降级为"⚠️谨慎"(不再🟢绿),排序沉底,防把自由落体当便宜货。
        flags = list(g.get("flags") or [])
        is_knife = bool(g.get("falling_knife"))
        is_stale = bool(g.get("target_stale"))
        cautioned = is_knife or is_stale or bool(flags)
        icon = "⚠️" if cautioned else "🟢"
        head = f"{sym}（{rank_txt}{ind_txt}） 跌进可买区 {zone}（{curtxt}{disctxt}）".replace("（）", "")
        if flags:
            head += "  " + " ".join(flags)
        opps.append({
            "source": f"机会·{src}",
            "headline": head,
            "icon": icon,
            "cautioned": cautioned,
            # 排序键:干净的在前(0),带警示的在后(1);同组内折价大的在前
            "sort_key": (1 if cautioned else 0, -(abs(disc) if isinstance(disc, (int, float)) else 0)),
            "key": f"opp:{sym}",
        })
    opps.sort(key=lambda o: o["sort_key"])
    return opps


def _build_card(result: dict) -> dict:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    majors = result.get("major_events", [])
    opps = result.get("opportunities", [])
    if result.get("recovered"):
        content = "**已恢复常态**，风险与机会均已解除。\n之前的红警/机会不再活跃。"
        template = "green"
        title = f"🟢 已恢复常态 · {now_str}"
    else:
        lines = ["**统一重大事件提醒（只在真·重大时响，平时不打扰）**"]
        if majors:
            lines.append("\n**🔴 风险**")
            for ev in majors[:8]:
                lines.append(f"• 🔴 **{ev['source']}**：{ev['headline']}")
        if opps:
            clean = [o for o in opps if not o.get("cautioned")]
            caut = [o for o in opps if o.get("cautioned")]
            lines.append("\n**💡 机会**（跌进可买区，研究参考非买入信号）")
            if clean:
                for o in clean[:6]:
                    lines.append(f"• {o.get('icon', '🟢')} {o['headline']}")
            else:
                lines.append("_今日无干净回调标的（下面都是接飞刀/目标价偏旧，先查为什么跌）_")
            if caut:
                lines.append("\n**⚠️ 谨慎**（系统标了接飞刀/目标价偏旧，别当便宜货）")
                for o in caut[:5]:
                    lines.append(f"• ⚠️ {o['headline']}")
        others = [s for s in result.get("all_signals", [])
                  if core._order(s["severity"]) < core._order(result.get("threshold", "CRITICAL"))
                  and s["severity"] != "NONE"]
        if others:
            lines.append("\n_次级（未达红线，仅参考）_：" +
                         "；".join(f"{o['source']}{core.ICON.get(o['severity'],'')}" for o in others[:5]))
        content = "\n".join(lines)
        template = "red" if majors else "turquoise"
        title = f"{result.get('headline', '重大事件')} · {now_str}"
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
    opportunities = collect_opportunities()
    result = core.aggregate_major_alert(signals, prev_state, threshold=threshold,
                                        opportunities=opportunities)
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
