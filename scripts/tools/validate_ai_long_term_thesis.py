"""AI 长期主线 MVP 护栏校验器 · 单一来源。

把 docs/V2/2026-06-16_AI长期主线与推荐准确性数据评估.md §九.5 的 22 条 MVP 校验
落成可执行规则，并实现 §九.3 的反证新鲜度计算（日历 + 事件相对，worst 取最差）。

设计原则：
  - 构建期 fail-loud：校验返回非空 = 长期主线页不得上线；调用方应在 build 阶段
    硬阻断，绝不在渲染期 try/except 把整段静默隐藏（踩过 DuckDB 锁静默丢段的坑）。
  - 只做校验：不接页面、不写 watchlist、不写真实持仓、不改生产决策逻辑。

入参分四层，给什么校验什么（没给的层跳过）：
  thesis            —— ai_long_term_thesis.json 内容（顶层 + members）
  universe_snapshot —— {"symbols": [...]}，校验 members ⊆ universe
  page_payload      —— 页面动态 payload：禁用词、portfolio_overlay、业绩文案
  build_context     —— 构建行为标志：是否归档快照、是否误写 watchlist/持仓
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date

# ── 常量（口径全部对齐文档 §九）─────────────────────────────

# §九.1 禁用交易暗示词
TRADE_WORDS = ("可买", "买入", "卖出", "低吸", "补仓", "加仓", "建仓",
               "等回调", "目标价", "合理价", "到位")

# capex 组：需求阀门，反证整条供应链，不是自身反证（§三点五.5）
CAPEX_TICKERS = frozenset({"MSFT", "GOOGL", "AMZN", "META"})

# §九.3 反证新鲜度严重度顺序：越靠后越差
FRESHNESS_ORDER = ("fresh", "aging", "stale", "missing")

# §九.2 / §九.4：月级 outcome 链路上线前只能这两个
MVP_STATUS = frozenset({"insufficient_sample", "tracking"})
GATED_STATUS = frozenset({"passed_primary_test", "failed_primary_test"})

# §九.4 前向收益合法口径
VALID_PRICE_BASIS = "same_pull_adjusted_close_by_trade_date"

# §九.4 枚举
ENUMS = {
    "market": {"US", "ADR"},
    "thesis_status": {"active", "watch", "retired"},
    "evidence_basis": {"system_evidence", "manual_thesis", "mixed"},
    "platform_evidence_level": {None, "manual_thesis", "mixed",
                                "financial_proxy_verified", "ai_attribution_verified"},
    "counter_signal_review_mode": {"structured_quarterly_review", "manual_ad_hoc"},
    "counter_signal_freshness": {"fresh", "aging", "stale", "missing"},
    "counter_signal_source": {"self_supply", "demand_valve", "manual_thesis"},
    "performance_validation_status": {"insufficient_sample", "tracking",
                                      "passed_primary_test", "failed_primary_test"},
}

# §九.4 顶层 schema 必填
TOP_REQUIRED = ("version", "as_of_date", "confirmed_at", "reviewed_by",
                "source_policy", "change_summary", "price_basis",
                "entry_trade_date_policy", "performance_validation_status",
                "primary_test", "risk_exposure_summary", "members")

# 成员“恒必填”——counter_signals / risk_factor / change_reason 走专门的 active_* 校验，
# 不重复进这里，保证每个触发点只点亮一个预期测试名。
MEMBER_REQUIRED = ("symbol", "market", "routes", "thesis_status", "thesis_1y_2y",
                   "evidence_basis", "evidence_vector", "counter_signal_source",
                   "last_reviewed_at")

# §九.5 no_trade_words_free_text：要扫的人工自由文本字段
MEMBER_FREE_TEXT = ("thesis_1y_2y", "discipline_principle", "change_reason")
TOP_FREE_TEXT = ("source_policy", "change_summary")

# 样本不足时业绩区不得出现的措辞
OVERCLAIM_WORDS = ("跑赢", "有效", "alpha 为正", "alpha为正", "已验证 alpha", "已验证alpha")


@dataclass(frozen=True)
class Violation:
    """一条 MVP 校验失败。test = §九.5 的测试名；message = 人能看懂的原因。"""
    test: str
    message: str


def _trade_word(text) -> str | None:
    if not isinstance(text, str):
        return None
    return next((w for w in TRADE_WORDS if w in text), None)


def _worst(*freshness: str) -> str:
    """取最差的新鲜度（fresh < aging < stale < missing）。"""
    valid = [f for f in freshness if f in FRESHNESS_ORDER]
    return max(valid, key=FRESHNESS_ORDER.index) if valid else "missing"


# ── §九.3 反证新鲜度：日历规则 + 事件相对规则，最终取 worst ──────────

def compute_freshness(last_reviewed_at: str | None,
                      as_of: date,
                      latest_earnings_date: str | None = None,
                      trading_days_since_earnings: int | None = None) -> tuple[str, str]:
    """返回 (counter_signal_freshness, freshness_reason)。

    事件规则只能把状态往更差降级，绝不把 stale/missing 升成 aging/fresh
    （文档 §九.3 优先级规则；对齐 bottleneck_signals.STALE_AFTER_DAYS=150）。
    """
    if not last_reviewed_at:
        return ("missing", "missing_review")

    reviewed = date.fromisoformat(last_reviewed_at)
    days = (as_of - reviewed).days

    # 基础日历规则
    if days <= 95:
        cal = ("fresh", "review_confirmed_recently")
    elif days <= 150:
        cal = ("aging", "calendar_aging")
    else:
        cal = ("stale", "calendar_stale")

    # 事件相对规则（财报晚于复查 = 财报后未复查）
    evt: tuple[str, str] | None = None
    if latest_earnings_date:
        earnings = date.fromisoformat(latest_earnings_date)
        if earnings > reviewed:
            # “>5 个交易日”——给了交易日数用之，否则用 >7 自然日近似
            overdue = (trading_days_since_earnings is not None and trading_days_since_earnings > 5) \
                or (trading_days_since_earnings is None and (as_of - earnings).days > 7)
            evt = ("stale", "post_earnings_unreviewed_overdue") if overdue \
                else ("aging", "post_earnings_unreviewed")
    else:
        cal = (cal[0], "calendar_only")

    if evt is None:
        return cal
    # worst：谁更差用谁；同档时优先标事件原因（更可解释）
    final = _worst(cal[0], evt[0])
    reason = evt[1] if FRESHNESS_ORDER.index(evt[0]) >= FRESHNESS_ORDER.index(cal[0]) else cal[1]
    return (final, reason)


# ── §九.5 主校验 ───────────────────────────────────────────

def validate(thesis: dict, *,
             universe_snapshot: dict | None = None,
             page_payload: dict | None = None,
             build_context: dict | None = None,
             outcome_pipeline_live: bool = False) -> list[Violation]:
    """跑 §九.5 全部可校验规则。返回空列表 = 通过；非空 = 不得上线。"""
    v: list[Violation] = []
    members = thesis.get("members")
    if members is None:
        members = []

    # —— 顶层 schema_required_fields ——
    for f in TOP_REQUIRED:
        if f == "members":
            if "members" not in thesis:
                v.append(Violation("schema_required_fields", "顶层缺 members"))
        elif thesis.get(f) in (None, ""):
            v.append(Violation("schema_required_fields", f"顶层缺必填字段 {f}"))

    # —— adjusted_close_same_basis（A）——
    if thesis.get("price_basis") != VALID_PRICE_BASIS:
        v.append(Violation("adjusted_close_same_basis",
                           f"price_basis 必须为 {VALID_PRICE_BASIS}，不得存数值化旧 entry_adj_close"))
    for m in members:
        if "entry_adj_close" in m:
            v.append(Violation("adjusted_close_same_basis",
                               f"{m.get('symbol')} 冻结了数值化 entry_adj_close；应只存 entry_trade_date"))

    # —— validation_status_source（E）——
    status = thesis.get("performance_validation_status")
    if status is not None and status not in ENUMS["performance_validation_status"]:
        v.append(Violation("schema_required_fields", f"performance_validation_status 非法值 {status}"))
    if not outcome_pipeline_live and status in GATED_STATUS:
        v.append(Violation("validation_status_source",
                           f"月级 outcome 链路未上线，status 不得为 {status}（只能 {' / '.join(sorted(MVP_STATUS))}）"))

    # —— 顶层自由文本禁用词（F）——
    for f in TOP_FREE_TEXT:
        w = _trade_word(thesis.get(f))
        if w:
            v.append(Violation("no_trade_words_free_text", f"顶层 {f} 出现禁用词「{w}」"))

    # —— 逐成员 ——
    for m in members:
        sym = m.get("symbol", "?")
        routes = m.get("routes") or []
        active = m.get("thesis_status") == "active"

        for f in MEMBER_REQUIRED:
            if m.get(f) in (None, ""):
                v.append(Violation("schema_required_fields", f"成员 {sym} 缺必填字段 {f}"))

        # 枚举
        for field, allowed in (("market", ENUMS["market"]),
                               ("thesis_status", ENUMS["thesis_status"]),
                               ("evidence_basis", ENUMS["evidence_basis"]),
                               ("counter_signal_source", ENUMS["counter_signal_source"])):
            if field in m and m.get(field) not in allowed:
                v.append(Violation("schema_required_fields", f"成员 {sym} 的 {field} 非法值 {m.get(field)!r}"))
        if m.get("platform_evidence_level") not in ENUMS["platform_evidence_level"]:
            v.append(Violation("schema_required_fields",
                               f"成员 {sym} 的 platform_evidence_level 非法值 {m.get('platform_evidence_level')!r}"))

        # 自由文本禁用词（F）
        for f in MEMBER_FREE_TEXT:
            w = _trade_word(m.get(f))
            if w:
                v.append(Violation("no_trade_words_free_text", f"成员 {sym} 的 {f} 出现禁用词「{w}」"))
        for cs in (m.get("counter_signals") or []):
            w = _trade_word(cs)
            if w:
                v.append(Violation("no_trade_words_free_text", f"成员 {sym} 的 counter_signals 出现禁用词「{w}」"))

        # active 专项
        if active:
            if not m.get("counter_signals"):
                v.append(Violation("active_requires_counter_signals", f"active 成员 {sym} 缺 counter_signals"))
            if not m.get("risk_factor"):
                v.append(Violation("active_requires_risk_factor", f"active 成员 {sym} 缺 risk_factor"))
            if not m.get("change_reason"):
                v.append(Violation("active_requires_change_reason", f"active 成员 {sym} 缺 change_reason"))

        # platform 专项
        if "platform" in routes:
            if not m.get("platform_evidence_level"):
                v.append(Violation("platform_level_required", f"platform 成员 {sym} 缺 platform_evidence_level"))
            if m.get("platform_evidence_level") == "ai_attribution_verified" \
                    and not m.get("ai_attribution_disclosure"):
                v.append(Violation("no_fake_ai_attribution",
                                   f"{sym} 标 ai_attribution_verified 但无明确 AI 归因披露"))

        # bottleneck 专项
        if "bottleneck" in routes:
            if not m.get("counter_signal_review_mode") or not m.get("counter_signal_freshness"):
                v.append(Violation("bottleneck_review_required",
                                   f"bottleneck 成员 {sym} 缺 counter_signal_review_mode 或 counter_signal_freshness"))

        # capex 不得自反证（承重纠正）
        if sym in CAPEX_TICKERS and m.get("counter_signal_source") == "self_supply":
            v.append(Violation("capex_not_self_supply",
                               f"{sym} 是 capex 需求阀门，counter_signal_source 不得为 self_supply"))
        # META 方向特判
        if sym == "META" and (m.get("counter_signal_source") == "self_supply"
                              or m.get("meta_capex_direction") == "self"):
            v.append(Violation("meta_demand_valve_direction",
                               "META 的 capex 信号必须按“对供应链”方向，不得当作 META 自身利空"))

        # members ⊆ universe（D）
        if universe_snapshot is not None and m.get("thesis_status") in ("active", "watch"):
            if sym not in set(universe_snapshot.get("symbols") or []):
                v.append(Violation("member_in_universe_snapshot",
                                   f"成员 {sym} 不在本次 universe 快照中（分子有、分母无）"))

        # freshness worst 一致性（C）：成员若带了日历/事件两个输入，最终不得优于 worst
        cal_f, evt_f, final_f = m.get("calendar_freshness"), m.get("event_freshness"), m.get("counter_signal_freshness")
        if cal_f in FRESHNESS_ORDER and evt_f in FRESHNESS_ORDER and final_f in FRESHNESS_ORDER:
            if FRESHNESS_ORDER.index(final_f) < FRESHNESS_ORDER.index(_worst(cal_f, evt_f)):
                v.append(Violation("freshness_worst_rule",
                                   f"成员 {sym} 最终 freshness={final_f} 优于 worst(日历={cal_f},事件={evt_f})"))

    # —— factor_concentration_warning（名单内部单因子）——
    actives = [m for m in members if m.get("thesis_status") == "active"]
    if actives:
        summary = thesis.get("risk_exposure_summary") or {}
        threshold = summary.get("single_factor_threshold_pct", 50)
        warned = bool(summary.get("concentration_warning"))
        factor, n = Counter(m.get("risk_factor") for m in actives).most_common(1)[0]
        share = 100.0 * n / len(actives)
        if share > threshold and not warned:
            v.append(Violation("factor_concentration_warning",
                               f"单因子 {factor} 占 {share:.0f}% 超 {threshold}% 却未标 concentration_warning"))

    # —— 页面 payload 层 ——
    if page_payload is not None:
        w = _trade_word(page_payload.get("text", ""))
        if w:
            v.append(Violation("no_trade_words", f"页面文案出现禁用词「{w}」"))
        if not page_payload.get("portfolio_overlay"):
            v.append(Violation("portfolio_overlay_required", "页面 payload 缺只读 portfolio_overlay"))
        if status == "insufficient_sample":
            ptext = str(page_payload.get("performance_text", ""))
            bad = next((b for b in OVERCLAIM_WORDS if b in ptext), None)
            if bad:
                v.append(Violation("performance_not_overclaimed", f"样本不足却在业绩区显示「{bad}」"))

    # —— 构建行为层 ——
    if build_context is not None:
        if not build_context.get("frozen_snapshot_archived"):
            v.append(Violation("frozen_snapshot_required", "第一版没有归档长期名单冻结快照"))
        if not build_context.get("universe_snapshot_archived"):
            v.append(Violation("universe_snapshot_required", "第一版没有归档同一 universe 快照"))
        if build_context.get("wrote_watchlist"):
            v.append(Violation("no_watchlist_write", "构建/打开页面写入了 watchlist"))
        if build_context.get("wrote_real_holdings"):
            v.append(Violation("no_real_holding_write", "构建/打开页面写入了真实持仓"))

    return v


# ── 构建期 fail-loud CLI ────────────────────────────────────

def _load_json(path: str) -> dict:
    import json
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv: list[str] | None = None) -> int:
    """命令行入口：校验不过返回 1（构建期应据此硬阻断），通过返回 0。"""
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        description="AI 长期主线 MVP 护栏校验（构建期 fail-loud；不过即不得上线）")
    ap.add_argument("thesis", help="ai_long_term_thesis.json 路径")
    ap.add_argument("--universe", help="universe 快照 JSON（含 symbols）")
    ap.add_argument("--page-payload", help="页面动态 payload JSON")
    ap.add_argument("--build-context", help="构建行为标志 JSON")
    ap.add_argument("--outcome-pipeline-live", action="store_true",
                    help="月级 outcome 链路已上线（放开 passed/failed 状态）")
    args = ap.parse_args(argv)

    viols = validate(
        _load_json(args.thesis),
        universe_snapshot=_load_json(args.universe) if args.universe else None,
        page_payload=_load_json(args.page_payload) if args.page_payload else None,
        build_context=_load_json(args.build_context) if args.build_context else None,
        outcome_pipeline_live=args.outcome_pipeline_live,
    )
    if viols:
        print(f"❌ MVP 护栏未通过（{len(viols)} 条），长期主线页不得上线：", file=sys.stderr)
        for x in viols:
            print(f"  - [{x.test}] {x.message}", file=sys.stderr)
        return 1
    print(f"✅ MVP 护栏通过：{args.thesis}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
