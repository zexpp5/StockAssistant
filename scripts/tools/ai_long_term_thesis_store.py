"""AI 长期主线 · 逻辑层（纯文件/字典操作，不接 DB、不接页面、不写真实持仓）。

对齐 docs/V2/2026-06-16_AI长期主线与推荐准确性数据评估.md 的可实现需求：
  build_candidate_drafts   P0a 候选草稿生成（去重 + 来源标注 + 偏倚标注，绝不自动确认）
  confirm_and_freeze       P0b→P1 人工确认后冻结：先过护栏，再 append-only 归档名单 + universe 快照
  diff_snapshots           P4 “为什么变了”：版本间 added / removed / status / thesis 漂移
  compute_portfolio_overlay §九.4 持仓 overlay：名单因子叠加真实持仓的合并暴露（只读）
  build_daily_state        P2 日变状态：每票研究状态（只用 4 个允许值，绝无交易动作词）

刻意不做（需真实数据环境 / 人工 / 前端）：
  - 从 DuckDB 读 ai_theme_company_evidence / system_universe 的适配器（薄层，留给真实环境）
  - 人工确认正式名单（文档第一原则，是用户的判断）
  - 页面渲染（P3，前端）、前向验证（P5，post-MVP）
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

try:
    from scripts.tools.validate_ai_long_term_thesis import validate
except ImportError:  # 兜底：直接按文件名导入
    from validate_ai_long_term_thesis import validate


# ── 异常 ────────────────────────────────────────────────────

class ThesisValidationError(Exception):
    """确认即冻结前护栏未通过——绝不冻结一份不合规的名单。"""

    def __init__(self, violations):
        self.violations = violations
        super().__init__(
            "MVP 护栏未通过（%d 条）：%s"
            % (len(violations), "；".join(f"[{v.test}] {v.message}" for v in violations)))


class SnapshotExistsError(Exception):
    """快照 append-only：同 version / snapshot_id 已存在，复核必须用新号。"""


# ── P0a 候选草稿生成 ────────────────────────────────────────

# 来源 → 人能看懂的说明（草稿必须暴露“怎么进来的”，人工确认这道闸才有效）
PROVENANCE_NOTE = {
    "theme_evidence": "AI 主题证据 confirmed/candidate",
    "bottleneck_signal": "瓶颈信号注册表 7 只",
    "etf_consensus": "ETF 共识命中且在系统 universe 内",
    "recent_recommendation": "近期 AI 推荐多次命中",
    "manual": "人工提出",
}
# 动量/共识种子来源：天然和近期涨幅相关，确认人要能看到这是不是“只是近期赢家”
_BIAS_SEEDED_SOURCES = {"recent_recommendation", "etf_consensus"}


def build_candidate_drafts(sources: dict[str, list[str]], *,
                           universe_symbols: list[str] | None = None) -> list[dict]:
    """把多来源候选去重成草稿。

    sources: {来源key: [symbol, ...]}。每个 symbol 合并所有命中来源。
    草稿一律 status='candidate_draft'、auto_confirmable=False——绝不直接进正式名单。
    bias_seeded 标出动量/共识种子，universe_eligible 标出是否在可交易 universe 内。
    """
    drafts: dict[str, dict] = {}
    for source, symbols in sources.items():
        for sym in symbols:
            d = drafts.setdefault(sym, {
                "symbol": sym,
                "status": "candidate_draft",
                "auto_confirmable": False,
                "provenance": [],
            })
            if source not in {p["source"] for p in d["provenance"]}:
                d["provenance"].append({"source": source,
                                        "note": PROVENANCE_NOTE.get(source, source)})

    universe = set(universe_symbols) if universe_symbols is not None else None
    for sym, d in drafts.items():
        d["bias_seeded"] = any(p["source"] in _BIAS_SEEDED_SOURCES for p in d["provenance"])
        d["universe_eligible"] = universe is None or sym in universe
    return sorted(drafts.values(), key=lambda d: d["symbol"])


# ── P0b→P1 确认即冻结（append-only）────────────────────────

def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def confirm_and_freeze(thesis: dict, universe_snapshot: dict, *, store_dir: str | Path,
                       outcome_pipeline_live: bool = False) -> dict:
    """人工确认后调用：先过护栏（fail-loud），再 append-only 归档名单 + universe 快照。

    护栏不过 → ThesisValidationError，绝不写盘。
    同 version / snapshot_id 已存在 → SnapshotExistsError（复核必须用新号，不原地覆盖）。
    成功后同时刷新 live 指针 ai_long_term_thesis.json。
    """
    viols = validate(thesis, universe_snapshot=universe_snapshot,
                     outcome_pipeline_live=outcome_pipeline_live)
    if viols:
        raise ThesisValidationError(viols)

    store = Path(store_dir)
    version = thesis["version"]
    usnap_id = universe_snapshot["snapshot_id"]
    tpath = store / "ai_long_term_thesis_snapshots" / f"{version}.json"
    upath = store / "ai_long_term_universe_snapshots" / f"{usnap_id}.json"

    if tpath.exists():
        raise SnapshotExistsError(f"名单版本快照已存在：{tpath}（复核请用新 version）")
    if upath.exists():
        raise SnapshotExistsError(f"universe 快照已存在：{upath}（请用新 snapshot_id）")

    _write_json(tpath, thesis)
    _write_json(upath, universe_snapshot)
    _write_json(store / "ai_long_term_thesis.json", thesis)  # live 指针
    return {"thesis_snapshot": str(tpath), "universe_snapshot": str(upath)}


# ── P4 “为什么变了”：版本间 diff ───────────────────────────

def diff_snapshots(old: dict, new: dict) -> dict:
    """两版长期名单的差分，喂“为什么变了”。每条变化带 change_reason（来自新版成员）。"""
    om = {m["symbol"]: m for m in (old.get("members") or [])}
    nm = {m["symbol"]: m for m in (new.get("members") or [])}

    added = [{"symbol": s, "change_reason": nm[s].get("change_reason")}
             for s in nm.keys() - om.keys()]
    removed = [{"symbol": s, "last_status": om[s].get("thesis_status"),
                "change_reason": nm.get(s, {}).get("change_reason") or om[s].get("change_reason")}
               for s in om.keys() - nm.keys()]
    status_changed, thesis_changed = [], []
    for s in nm.keys() & om.keys():
        if nm[s].get("thesis_status") != om[s].get("thesis_status"):
            status_changed.append({"symbol": s, "from": om[s].get("thesis_status"),
                                   "to": nm[s].get("thesis_status"),
                                   "change_reason": nm[s].get("change_reason")})
        if nm[s].get("thesis_1y_2y") != om[s].get("thesis_1y_2y"):
            thesis_changed.append({"symbol": s, "change_reason": nm[s].get("change_reason")})

    key = lambda x: x["symbol"]
    return {"added": sorted(added, key=key), "removed": sorted(removed, key=key),
            "status_changed": sorted(status_changed, key=key),
            "thesis_changed": sorted(thesis_changed, key=key)}


# ── §九.4 持仓 overlay（只读）──────────────────────────────

def compute_portfolio_overlay(thesis: dict, holdings: list[dict], *,
                              as_of_date: str | None = None,
                              warn_threshold_pct: float = 50.0) -> dict:
    """名单因子暴露叠加真实持仓的合并暴露（回答“分散还是给已有赌注盖章”）。

    holdings: [{"symbol","weight_pct","risk_factor"}]，只读，不写回。
    """
    combined: Counter = Counter()
    for h in holdings:
        combined[h.get("risk_factor")] += float(h.get("weight_pct") or 0)

    list_factors = {m.get("risk_factor") for m in (thesis.get("members") or [])}
    member_syms = {m["symbol"] for m in (thesis.get("members") or [])}
    hold_syms = {h["symbol"] for h in holdings}

    warnings = []
    for factor, pct in combined.items():
        if factor in list_factors and pct >= warn_threshold_pct:
            warnings.append(
                f"你的真实持仓在 {factor} 上已 {pct:.0f}%，与长期主线名单同一风险因子——"
                f"加该名单是加码不是分散")

    return {
        "read_only": True,
        "as_of_date": as_of_date,
        "source": "real_holding_review_items",
        "combined_risk_exposure": {k: round(v, 1) for k, v in combined.items()},
        "shared_factors": sorted(f for f in (list_factors & set(combined)) if f is not None),
        "top_overlaps": sorted(member_syms & hold_syms),
        "warnings": warnings,
        "writes_real_holdings": False,
    }


# ── P2 日变状态（只用 4 个允许值，绝无交易动作词）─────────

ALLOWED_ACTION_STATES = ("research_only", "observe_only",
                         "evidence_review_needed", "pause_new_research")


def build_daily_state(thesis: dict, *,
                      recommendation_hits: list[str] | None = None,
                      price_state_by_symbol: dict[str, str] | None = None,
                      freshness_by_symbol: dict[str, str] | None = None,
                      event_window_by_symbol: dict[str, str] | None = None) -> list[dict]:
    """每个成员的日变叠加。current_action_state 只取 ALLOWED_ACTION_STATES，永不输出买卖词。"""
    hits = set(recommendation_hits or [])
    price_by = price_state_by_symbol or {}
    fresh_by = freshness_by_symbol or {}
    event_by = event_window_by_symbol or {}

    out = []
    for m in (thesis.get("members") or []):
        sym = m["symbol"]
        fresh = fresh_by.get(sym, m.get("counter_signal_freshness", "missing"))
        price = price_by.get(sym, "正常")
        event = event_by.get(sym, "无重大事件")

        if fresh in ("stale", "missing"):
            state = "evidence_review_needed"
        elif price == "过热" or event in ("财报前", "宏观前"):
            state = "pause_new_research"
        elif m.get("thesis_status") == "active":
            state = "research_only"
        else:
            state = "observe_only"

        out.append({
            "symbol": sym,
            "current_action_state": state,
            "price_state": price,
            "evidence_freshness": fresh,
            "today_recommendation_hit": sym in hits,
            "event_window": event,
        })
    return out
