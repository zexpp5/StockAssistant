"""AI 主题雷达数据聚合 — 行业理解层，不是新推荐池。

定位见 docs/V2/AI主题雷达_产品定位.md。本模块严格只读：
  - 不写 watchlist / 真实持仓 / 任何用户态表
  - 不新增候选范围 — 输入是 recommendation_picks 最新一批（system_tech_universe）
  - 输出结构化 dict 供 dashboard 渲染，无副作用

四标签体系（对应文档 §三）：
  AI 价值链层级    ← chain_metadata.chain + chain_tier
  受益路径         ← chain_metadata.chain_role + layman_intro
  证据置信度       ← chain_metadata.source
  AI 关联强度      ← 本模块按 chain 反推（强/中/弱/无）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


# ─────────────── AI 关联强度映射 ───────────────
# 规则（首版）：按 chain 反推；后续可升级为独立字段或 LLM 评分
#   强 — 业务直接受益于 AI 资本开支或 AI 需求扩张
#   中 — 通过自身业务 AI 化或服务 AI 客户间接受益
#   弱 — 长期 AI 叙事但短期无明确兑现路径
#   无 — 与 AI 无直接业务关联
AI_STRENGTH_BY_CHAIN: dict[str, str] = {
    "AI 算力": "强",
    "数据中心电力": "强",
    "机器人/自动化": "强",
    "互联网/云": "中",
    "核能 / 铀": "中",
    "量子计算": "弱",
    "光伏储能": "弱",
    "创新药": "无",
    "新能源车": "无",
    "军工/国防": "无",
}

AI_STRENGTH_RANK = {"强": 3, "中": 2, "弱": 1, "无": 0, None: -1}


def derive_ai_strength(chain: str | None) -> str | None:
    """按 chain 反推 AI 关联强度；未分类返回 None（前端显示『未分类』）。"""
    if not chain:
        return None
    return AI_STRENGTH_BY_CHAIN.get(chain, "无")


# ─────────────── 覆盖率审计阈值 ───────────────
# 系统打分高但缺 chain 标签的票 — 进入审计卡片，提示运维补 chain_metadata
COVERAGE_AUDIT_SCORE_THRESHOLD = 70.0


# ─────────────── 主线轮动判断 ───────────────
# 按 chain 算 (今日均分 - 7 天前均分)，标记主线 / 稳定 / 冷却
MAINLINE_RISE_DELTA = 2.0
MAINLINE_FALL_DELTA = -2.0


def classify_mainline(delta: float | None) -> str:
    if delta is None:
        return "数据不足"
    if delta >= MAINLINE_RISE_DELTA:
        return "📈 主线发酵中"
    if delta <= MAINLINE_FALL_DELTA:
        return "📉 冷却中"
    return "➡️ 持稳"


# ─────────────── SQL ───────────────

# 取每市场最新 run 的 picks + chain_metadata LEFT JOIN
_SQL_LATEST_PICKS = """
WITH latest_run AS (
    SELECT rp.market, MAX(rr.generated_at) AS latest_at
    FROM recommendation_runs rr
    JOIN recommendation_picks rp ON rp.run_id = rr.run_id
    WHERE rr.universe_scope = 'system_tech_universe'
    GROUP BY rp.market
)
SELECT
    rp.market, rp.symbol, rp.name, rp.rank, rp.total_score, rp.rating,
    cm.chain, cm.chain_tier, cm.chain_role, cm.layman_intro, cm.source AS chain_source,
    rr.run_id, rr.generated_at
FROM recommendation_runs rr
JOIN recommendation_picks rp ON rp.run_id = rr.run_id
JOIN latest_run l ON l.market = rp.market AND l.latest_at = rr.generated_at
LEFT JOIN chain_metadata cm ON cm.market = rp.market AND cm.symbol = rp.symbol
ORDER BY rp.market, rp.rank
"""

# 取过去 7 天每条 chain 的均分（用 chain_metadata JOIN 历史 picks）
_SQL_CHAIN_TREND = """
WITH recent_picks AS (
    SELECT
        rp.market, rp.symbol, rp.total_score,
        DATE(rr.generated_at) AS pick_date,
        cm.chain
    FROM recommendation_runs rr
    JOIN recommendation_picks rp ON rp.run_id = rr.run_id
    LEFT JOIN chain_metadata cm ON cm.market = rp.market AND cm.symbol = rp.symbol
    WHERE rr.universe_scope = 'system_tech_universe'
      AND rr.generated_at >= CURRENT_TIMESTAMP - INTERVAL '8 days'
      AND cm.chain IS NOT NULL
)
SELECT
    chain,
    pick_date,
    AVG(total_score) AS avg_score,
    COUNT(*) AS n
FROM recent_picks
GROUP BY chain, pick_date
ORDER BY chain, pick_date
"""


# ─────────────── 自选股集合（只读） ───────────────

_SQL_WATCHLIST = """
SELECT market, symbol FROM manual_watchlist
"""


# ─────────────── 5 主题宏观证据卡数据（V2 ai_theme_* 表，文档 §七）───────────────

# 主题 ID → 中文显示名 / 受益逻辑一句话
THEME_DISPLAY: dict[str, tuple[str, str]] = {
    "liquid_cooling": ("水冷 / 液冷", "AI 服务器功耗暴涨 → 数据中心散热需求"),
    "rare_earths":    ("稀土",       "永磁电机/AI 硬件供应链关键原料"),
    "uranium":        ("铀",         "核电重启 + AI 数据中心电力 → 铀价/长合同"),
    "smr":            ("SMR / 先进核能", "小型模块化反应堆 + 微堆，多在监管/示范阶段"),
    "ai_data":        ("AI 数据",    "可授权内容 / 训练数据 / 标注 / 模型评测"),
}

# 主题展示顺序（按文档 §七.2 排）
THEME_ORDER = ["liquid_cooling", "rare_earths", "uranium", "smr", "ai_data"]

_SQL_THEME_SOURCES_HEALTH = """
SELECT
    m.theme,
    s.source_id,
    s.source_name,
    s.source_tier,
    s.source_type,
    s.source_url,
    s.last_check_status,
    s.last_check_http,
    s.last_checked_at,
    m.note
FROM ai_theme_source_mapping m
JOIN ai_theme_evidence_sources s ON s.source_id = m.source_id AND s.active = TRUE
ORDER BY m.theme, s.source_tier, s.source_id
"""

_SQL_THEME_EVIDENCE_COUNTS = """
SELECT theme, evidence_status, COUNT(*) AS n
FROM ai_theme_company_tags
GROUP BY theme, evidence_status
"""

_SQL_THEME_METRICS_COUNT = """
SELECT theme, COUNT(*) AS n_metrics, MAX(metric_date) AS latest
FROM ai_theme_topic_metrics
GROUP BY theme
"""

# 通过 theme → chain mapping 拉到关联 picks（最新 system_tech_universe run）
_SQL_THEME_CHAIN_PICKS = """
WITH latest_run AS (
    SELECT rp.market, MAX(rr.generated_at) AS latest_at
    FROM recommendation_runs rr
    JOIN recommendation_picks rp ON rp.run_id = rr.run_id
    WHERE rr.universe_scope = 'system_tech_universe'
    GROUP BY rp.market
)
SELECT
    tcm.theme,
    tcm.chain,
    tcm.relevance,
    cm.market, cm.symbol,
    rp.name, rp.total_score, cm.chain_role
FROM ai_theme_chain_mapping tcm
JOIN chain_metadata cm ON cm.chain = tcm.chain
JOIN recommendation_runs rr ON rr.universe_scope = 'system_tech_universe'
JOIN recommendation_picks rp ON rp.run_id = rr.run_id AND rp.market = cm.market AND rp.symbol = cm.symbol
JOIN latest_run l ON l.market = rp.market AND l.latest_at = rr.generated_at
ORDER BY tcm.theme, rp.total_score DESC
"""

# ETF 共识 — 拉 8 个 ETF 持仓 + universe 命中 + 最新 picks 分数
_SQL_ETF_CONSENSUS = """
WITH latest_run AS (
    SELECT rp.market, MAX(rr.generated_at) AS latest_at
    FROM recommendation_runs rr
    JOIN recommendation_picks rp ON rp.run_id = rr.run_id
    WHERE rr.universe_scope = 'system_tech_universe'
    GROUP BY rp.market
),
latest_picks AS (
    SELECT rp.market, rp.symbol, rp.total_score
    FROM recommendation_runs rr
    JOIN recommendation_picks rp ON rp.run_id = rr.run_id
    JOIN latest_run l ON l.market = rp.market AND l.latest_at = rr.generated_at
)
SELECT
    u.etf_ticker, u.etf_name, u.issuer, u.theme_label, u.theme_id,
    u.holdings_url, u.last_fetched_at,
    h.rank, h.raw_ticker, h.company_name, h.weight,
    h.market_inferred, h.universe_match,
    su.market AS universe_market,
    lp.total_score AS pick_score
FROM ai_theme_etf_universe u
JOIN ai_theme_etf_holdings h ON h.etf_ticker = u.etf_ticker
LEFT JOIN system_universe su ON su.symbol = h.universe_match AND su.active = TRUE
LEFT JOIN latest_picks lp ON lp.market = su.market AND lp.symbol = h.universe_match
WHERE u.active = TRUE
ORDER BY u.etf_ticker, h.rank
"""

# 拉 topic_metrics 每主题的展示行（最近 90 天内的最新一条 per metric_name）
_SQL_THEME_METRICS_LATEST = """
WITH ranked AS (
    SELECT
        theme, metric_name, metric_value, metric_unit, metric_date,
        source_id, source_url,
        ROW_NUMBER() OVER (PARTITION BY theme, metric_name ORDER BY metric_date DESC) AS rn
    FROM ai_theme_topic_metrics
    WHERE metric_date >= CURRENT_DATE - INTERVAL '365 days'
)
SELECT theme, metric_name, metric_value, metric_unit, metric_date, source_id, source_url
FROM ranked WHERE rn = 1
ORDER BY theme, metric_date DESC
"""


def build_etf_consensus_panel(con) -> dict[str, Any]:
    """构建 ETF 共识热门主题面板数据。

    输入：只读 DuckDB 连接
    输出：每个 ETF 一条记录，含 metadata + holdings list（带 universe/picks 状态标记）
    """
    rows = con.execute(_SQL_ETF_CONSENSUS).fetchall()
    if not rows:
        return {"etfs": []}

    etf_buckets: dict[str, dict] = {}
    for (etf, name, issuer, theme_label, theme_id, url, last_fetched,
         rank, raw_ticker, company_name, weight, mkt_inferred, uni_match,
         uni_market, pick_score) in rows:
        if etf not in etf_buckets:
            etf_buckets[etf] = {
                "etf_ticker": etf,
                "etf_name": name,
                "issuer": issuer,
                "theme_label": theme_label,
                "theme_id": theme_id,
                "holdings_url": url,
                "last_fetched_at": last_fetched.isoformat() if hasattr(last_fetched, "isoformat") else last_fetched,
                "holdings": [],
                "n_total": 0,
                "n_in_universe": 0,
                "n_in_picks": 0,
                "weight_in_universe": 0.0,
                "weight_in_picks": 0.0,
            }
        b = etf_buckets[etf]
        in_uni = bool(uni_match)
        in_picks = pick_score is not None
        b["holdings"].append({
            "rank": rank,
            "raw_ticker": raw_ticker,
            "company_name": company_name,
            "weight": float(weight) if weight else 0.0,
            "market_inferred": mkt_inferred,
            "universe_match": uni_match,
            "universe_market": uni_market,
            "in_universe": in_uni,
            "in_picks": in_picks,
            "pick_score": float(pick_score) if pick_score is not None else None,
        })
        b["n_total"] += 1
        if in_uni:
            b["n_in_universe"] += 1
            b["weight_in_universe"] += float(weight) if weight else 0.0
        if in_picks:
            b["n_in_picks"] += 1
            b["weight_in_picks"] += float(weight) if weight else 0.0

    etfs = list(etf_buckets.values())
    # 排序：universe 覆盖率高的靠前（说明系统跟主流共识对齐）
    etfs.sort(key=lambda e: -e["n_in_universe"] / max(e["n_total"], 1))

    return {
        "etfs": etfs,
        "summary": {
            "n_etfs": len(etfs),
            "n_holdings_total": sum(e["n_total"] for e in etfs),
            "n_holdings_in_universe": sum(e["n_in_universe"] for e in etfs),
            "n_holdings_in_picks": sum(e["n_in_picks"] for e in etfs),
        },
    }


def build_theme_evidence_panel(con) -> dict[str, Any]:
    """构建 5 条主题宏观证据卡的数据（文档 §十.3）。

    输入只读 DuckDB 连接，输出每条主题：
      - 关联 sources 数 / ok / degraded
      - confirmed / candidate / stale / needs_review 公司数（首版全 0）
      - 已录入的 topic_metrics 条数（首版全 0）
      - phase 进度状态（数据基建是否完成）
    """
    # 拉源健康状态
    rows = con.execute(_SQL_THEME_SOURCES_HEALTH).fetchall()
    by_theme: dict[str, list[dict]] = {}
    for theme, sid, name, tier, stype, url, status, http, checked_at, note in rows:
        by_theme.setdefault(theme, []).append({
            "source_id": sid,
            "source_name": name,
            "source_tier": tier,
            "source_type": stype,
            "source_url": url,
            "status": status,
            "http": http,
            "checked_at": checked_at.isoformat() if hasattr(checked_at, "isoformat") else checked_at,
            "note": note,
        })

    # 拉公司证据状态计数
    evi_counts: dict[str, dict[str, int]] = {}
    for theme, status, n in con.execute(_SQL_THEME_EVIDENCE_COUNTS).fetchall():
        evi_counts.setdefault(theme, {})[status] = n

    # 拉宏观指标计数
    metric_info: dict[str, dict] = {}
    for theme, n, latest in con.execute(_SQL_THEME_METRICS_COUNT).fetchall():
        metric_info[theme] = {
            "n_metrics": n,
            "latest_metric_date": latest.isoformat() if hasattr(latest, "isoformat") else latest,
        }

    # 拉主题关联 chain → picks
    theme_chain_picks: dict[str, list[dict]] = {}
    theme_chain_relevance: dict[str, dict[str, str]] = {}
    for theme, chain, rel, market, symbol, name, score, role in con.execute(_SQL_THEME_CHAIN_PICKS).fetchall():
        theme_chain_picks.setdefault(theme, []).append({
            "chain": chain,
            "market": market,
            "symbol": symbol,
            "name": name,
            "score": float(score),
            "chain_role": role,
        })
        theme_chain_relevance.setdefault(theme, {})[chain] = rel

    # 拉主题最近宏观指标
    theme_latest_metrics: dict[str, list[dict]] = {}
    for theme, name, val, unit, dt, sid, url in con.execute(_SQL_THEME_METRICS_LATEST).fetchall():
        theme_latest_metrics.setdefault(theme, []).append({
            "metric_name": name,
            "metric_value": float(val) if val is not None else None,
            "metric_unit": unit,
            "metric_date": dt.isoformat() if hasattr(dt, "isoformat") else dt,
            "source_id": sid,
            "source_url": url,
        })

    themes: list[dict[str, Any]] = []
    for tid in THEME_ORDER:
        sources = by_theme.get(tid, [])
        n_total = len(sources)
        n_ok = sum(1 for s in sources if s["status"] == "ok")
        n_a = sum(1 for s in sources if s["source_tier"] == "A")
        n_b = sum(1 for s in sources if s["source_tier"] == "B")
        ec = evi_counts.get(tid, {})

        display_name, why = THEME_DISPLAY.get(tid, (tid, ""))
        picks_assoc = theme_chain_picks.get(tid, [])
        themes.append({
            "theme_id": tid,
            "display_name": display_name,
            "why": why,
            "sources_total": n_total,
            "sources_ok": n_ok,
            "sources_a": n_a,
            "sources_b": n_b,
            "sources_detail": sources,
            "evidence_confirmed": ec.get("confirmed", 0),
            "evidence_candidate": ec.get("candidate", 0),
            "evidence_stale": ec.get("stale", 0),
            "evidence_needs_review": ec.get("needs_review", 0),
            "metrics_count": (metric_info.get(tid) or {}).get("n_metrics", 0),
            "latest_metric_date": (metric_info.get(tid) or {}).get("latest_metric_date"),
            "picks_assoc": picks_assoc[:5],   # 顶部 5 只
            "n_picks_assoc": len(picks_assoc),
            "chains_mapped": list(theme_chain_relevance.get(tid, {}).keys()),
            "chain_relevance": dict(theme_chain_relevance.get(tid, {})),
            "latest_metrics": theme_latest_metrics.get(tid, []),
        })

    # Phase 1 判断要看 evidence 原始表 — tags 是聚合结果（尚未写聚合 job）
    n_evi_total = con.execute(
        "SELECT COUNT(*) FROM ai_theme_company_evidence"
    ).fetchone()[0]

    return {
        "themes": themes,
        "phase_status": {
            "phase_0_sources_seeded": all(t["sources_total"] > 0 for t in themes),
            "phase_1_evidence_scanned": n_evi_total > 0,
            "phase_2_dashboard_integrated": True,  # 这一刀就是 Phase 2 雏形
        },
    }


# ─────────────── 主聚合函数 ───────────────

def build_ai_radar_payload(con) -> dict[str, Any]:
    """构建 AI 主题雷达页面的渲染数据。

    Args:
        con: DuckDB 只读连接

    Returns:
        见 docs/V2/AI主题雷达_产品定位.md §四。所有数据从 system_tech_universe
        最新一批 picks 派生；watchlist 仅用于显示「已在自选」标记。
    """
    rows = con.execute(_SQL_LATEST_PICKS).fetchall()
    cols = ["market", "symbol", "name", "rank", "total_score", "rating",
            "chain", "chain_tier", "chain_role", "layman_intro", "chain_source",
            "run_id", "generated_at"]

    picks = [dict(zip(cols, r)) for r in rows]

    # 自选股集合（只读，仅用于打标）
    watchlist_rows = con.execute(_SQL_WATCHLIST).fetchall()
    watchlist_set = {(m, s) for m, s in watchlist_rows}

    # chain 趋势 → 主线判断
    trend_rows = con.execute(_SQL_CHAIN_TREND).fetchall()
    chain_delta = _compute_chain_delta(trend_rows)

    # 按 chain 聚合
    chain_buckets: dict[str | None, list[dict]] = {}
    for p in picks:
        chain_buckets.setdefault(p["chain"], []).append(p)

    # 反向映射：chain → 该 chain 关联了哪些前瞻主题（用于在 chain 卡上打标签）
    chain_to_themes: dict[str, list[dict]] = {}
    try:
        for theme, chain, rel, _ in con.execute(
            "SELECT theme, chain, relevance, note FROM ai_theme_chain_mapping"
        ).fetchall():
            display = THEME_DISPLAY.get(theme, (theme, ""))[0]
            chain_to_themes.setdefault(chain, []).append({
                "theme_id": theme,
                "display": display,
                "relevance": rel,
            })
    except Exception:
        pass  # 表可能不存在（极早期 DB）

    chains: list[dict[str, Any]] = []
    for chain, items in chain_buckets.items():
        if chain is None:
            continue  # 未分类的进覆盖率审计卡片，不进 chain 列表
        avg_score = sum(p["total_score"] for p in items) / len(items)
        strong_count = sum(
            1 for p in items
            if derive_ai_strength(p["chain"]) == "强"
        )
        delta = chain_delta.get(chain)
        chains.append({
            "chain": chain,
            "n_stocks": len(items),
            "avg_score": round(avg_score, 1),
            "strong_count": strong_count,
            "ai_strength": derive_ai_strength(chain),
            "delta_7d": round(delta, 2) if delta is not None else None,
            "mainline_status": classify_mainline(delta),
            "top_picks": [
                _format_pick(p, watchlist_set)
                for p in sorted(items, key=lambda x: -x["total_score"])[:5]
            ],
            "mapped_themes": chain_to_themes.get(chain, []),
        })

    # 按 AI 关联强度排序（强 → 中 → 弱），同档按平均分降序
    chains.sort(key=lambda c: (
        -AI_STRENGTH_RANK.get(c["ai_strength"], -1),
        -c["avg_score"],
    ))

    # 覆盖率审计：高分但 chain 为空
    uncovered = [p for p in picks if not p["chain"] and p["total_score"] >= COVERAGE_AUDIT_SCORE_THRESHOLD]
    uncovered.sort(key=lambda p: -p["total_score"])

    # run 元信息
    run_id = picks[0]["run_id"] if picks else None
    generated_at = picks[0]["generated_at"] if picks else None

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_run_id": run_id,
        "data_generated_at": generated_at.isoformat() if hasattr(generated_at, "isoformat") else generated_at,
        "data_universe": "system_tech_universe",
        "n_picks_total": len(picks),
        "n_picks_with_chain": sum(1 for p in picks if p["chain"]),
        "chains": chains,
        "coverage_audit": {
            "threshold_score": COVERAGE_AUDIT_SCORE_THRESHOLD,
            "n_uncovered": len(uncovered),
            "items": [
                {
                    "market": p["market"],
                    "symbol": p["symbol"],
                    "name": p["name"],
                    "score": round(p["total_score"], 1),
                }
                for p in uncovered
            ],
        },
        # 硬规则：本模块不写 watchlist，watchlist 数据仅用于打标
        "watchlist_readonly": True,
    }


def _format_pick(p: dict, watchlist_set: set) -> dict[str, Any]:
    """单只票的展示字段（注意：不包含任何推荐/买入文案）。"""
    return {
        "market": p["market"],
        "symbol": p["symbol"],
        "name": p["name"],
        "score": round(p["total_score"], 1),
        "chain_tier": p["chain_tier"],
        "chain_role": p["chain_role"],
        "layman_intro": p["layman_intro"],
        "chain_source": p["chain_source"],
        "ai_strength": derive_ai_strength(p["chain"]),
        "in_watchlist": (p["market"], p["symbol"]) in watchlist_set,
    }


def _compute_chain_delta(trend_rows: list) -> dict[str, float]:
    """从过去 8 天的 (chain, date, avg_score, n) 行计算 (今日 - 最早) 的均分变化。"""
    by_chain: dict[str, list[tuple]] = {}
    for chain, dt, avg, n in trend_rows:
        by_chain.setdefault(chain, []).append((dt, float(avg), int(n)))

    out: dict[str, float] = {}
    for chain, series in by_chain.items():
        series.sort(key=lambda x: x[0])
        if len(series) < 2:
            continue
        earliest_score = series[0][1]
        latest_score = series[-1][1]
        out[chain] = latest_score - earliest_score
    return out


# ─────────────── 渲染（HTML）───────────────

def _esc(s: Any) -> str:
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _strength_badge(strength: str | None) -> str:
    color = {
        "强": "bg-violet-100 text-violet-800 ring-violet-300",
        "中": "bg-sky-100 text-sky-800 ring-sky-300",
        "弱": "bg-amber-100 text-amber-800 ring-amber-300",
        "无": "bg-slate-100 text-slate-600 ring-slate-300",
    }.get(strength or "", "bg-slate-50 text-slate-400 ring-slate-200")
    label = strength if strength else "未分类"
    return f'<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[11px] font-medium ring-1 {color}">{label}</span>'


def _source_badge(source: str | None) -> str:
    if not source:
        return ""
    label = {
        "manual_override": "人工确认",
        "rule_classify": "规则分类",
    }.get(source, source)
    return f'<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] bg-slate-50 text-slate-500 ring-1 ring-slate-200">{label}</span>'


def _market_label(m: str) -> str:
    return {"US": "🇺🇸", "HK": "🇭🇰", "CN": "🇨🇳"}.get(m, m)


def _watchlist_badge(in_wl: bool) -> str:
    if not in_wl:
        return ""
    return ('<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] '
            'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200">✓ 已在自选</span>')


def _render_etf_consensus_panel(panel: dict[str, Any]) -> str:
    """ETF 共识热门主题 — 客观市场资金共识，避免 hardcode 主题清单。"""
    if not panel or not panel.get("etfs"):
        return ""

    summary = panel.get("summary") or {}
    n_etfs = summary.get("n_etfs", 0)
    n_uni = summary.get("n_holdings_in_universe", 0)
    n_pick = summary.get("n_holdings_in_picks", 0)
    n_total = summary.get("n_holdings_total", 0)

    etf_cards = []
    for e in panel["etfs"]:
        cov_pct = (e["n_in_universe"] / max(e["n_total"], 1)) * 100
        cov_color = "text-emerald-700" if cov_pct >= 70 else "text-amber-700" if cov_pct >= 30 else "text-rose-700"

        # 持仓行
        rows = []
        for h in e["holdings"]:
            # 状态 badge
            if h["in_picks"]:
                badge = (f'<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] '
                         f'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200">✓ picks {h["pick_score"]:.0f}</span>')
            elif h["in_universe"]:
                badge = ('<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] '
                         'bg-sky-50 text-sky-700 ring-1 ring-sky-200">○ universe</span>')
            else:
                badge = ('<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] '
                         'bg-amber-50 text-amber-700 ring-1 ring-amber-200">⚠️ 未收</span>')
            mkt_inferred = h.get("market_inferred") or "?"
            uni_match = h.get("universe_match") or h["raw_ticker"]
            rows.append(f"""
<tr class="border-t border-slate-100">
  <td class="py-1.5 pr-2 align-top text-[11px] text-slate-400 text-right whitespace-nowrap">#{h["rank"]}</td>
  <td class="py-1.5 pr-2 align-top text-[12px] whitespace-nowrap font-mono text-slate-700">{_esc(h["raw_ticker"])}</td>
  <td class="py-1.5 pr-2 align-top text-[12px]">{_esc(h.get("company_name") or "")}</td>
  <td class="py-1.5 pr-2 align-top text-[12px] font-semibold text-slate-800 text-right whitespace-nowrap">{h["weight"]:.2f}%</td>
  <td class="py-1.5 pl-2 align-top text-[11px] whitespace-nowrap">{badge}</td>
</tr>
""")

        etf_cards.append(f"""
<details class="bg-white ring-1 ring-slate-200 rounded-xl p-4 mb-3 group">
  <summary class="cursor-pointer list-none flex items-start justify-between gap-2">
    <div class="flex-1 min-w-0">
      <div class="flex items-center gap-2 flex-wrap">
        <span class="text-base font-bold text-slate-900 font-mono">{_esc(e["etf_ticker"])}</span>
        <span class="text-sm font-semibold text-slate-700">{_esc(e["theme_label"])}</span>
        <span class="text-[11px] text-slate-400">· {_esc(e["issuer"])}</span>
      </div>
      <div class="text-[11px] text-slate-500 mt-0.5">{_esc(e["etf_name"])}</div>
      <div class="mt-1 text-[11px]">
        <span class="text-slate-500">universe 命中 </span>
        <span class="font-semibold {cov_color}">{e["n_in_universe"]}/{e["n_total"]}</span>
        <span class="text-slate-400"> (权重 {e["weight_in_universe"]:.0f}%) · </span>
        <span class="text-slate-500">picks 命中 </span>
        <span class="font-semibold text-emerald-700">{e["n_in_picks"]}/{e["n_total"]}</span>
      </div>
    </div>
    <span class="text-slate-400 text-[12px] group-open:rotate-180 transition select-none">▾</span>
  </summary>
  <div class="mt-3 pt-3 border-t border-slate-100">
    <table class="w-full text-[12px]">
      <thead>
        <tr class="text-[10px] text-slate-400 uppercase tracking-wide">
          <th class="py-1 pr-2 text-right font-normal">排名</th>
          <th class="py-1 pr-2 text-left font-normal">ETF Ticker</th>
          <th class="py-1 pr-2 text-left font-normal">公司</th>
          <th class="py-1 pr-2 text-right font-normal">权重</th>
          <th class="py-1 pl-2 text-left font-normal">系统状态</th>
        </tr>
      </thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    <div class="mt-2 text-[10px] text-slate-400">
      <a href="{_esc(e["holdings_url"])}" target="_blank" class="hover:text-violet-600 hover:underline">→ {_esc(e["issuer"])} 官方持仓页</a>
    </div>
  </div>
</details>
""")

    return f"""
<div class="mb-4">
  <div class="flex items-center justify-between flex-wrap gap-2 mb-2">
    <div>
      <div class="text-sm font-bold text-slate-900">📈 ETF 共识热门主题</div>
      <div class="text-[11px] text-slate-500">客观市场共识：抄 {n_etfs} 个主流主题 ETF 的持仓 Top — 不依赖任何 hardcode 主题清单</div>
    </div>
    <div class="text-[11px] text-slate-500">
      universe 命中 <span class="font-semibold text-emerald-700">{n_uni}/{n_total}</span> ·
      picks 命中 <span class="font-semibold">{n_pick}/{n_total}</span>
    </div>
  </div>
  {"".join(etf_cards)}
  <div class="text-[10px] text-slate-400 mt-1 leading-relaxed">
    ✓ picks = 在系统最新一批 picks 命中 · ○ universe = 在我们 universe 但未入 picks · ⚠️ 未收 = universe 缺口（扩张候选）
  </div>
</div>
"""


def _render_theme_evidence_panel_compact(panel: dict[str, Any], chain_to_themes_set: set[str]) -> str:
    """简化版主题面板：
       - 已映射到 chain 的主题 → 只显示一行 mini 摘要（数据源数 + 指向链卡）
       - chain 体系未覆盖的主题（如 AI 数据） → 单独成卡，因为没地方放
    """
    if not panel or not panel.get("themes"):
        return ""

    mini_rows = []
    standalone_cards = []

    for t in panel["themes"]:
        tid = t["theme_id"]
        is_mapped = tid in chain_to_themes_set
        ok_color = "text-emerald-700" if t["sources_ok"] == t["sources_total"] else "text-amber-700"
        ok_badge = f'<span class="font-semibold {ok_color}">{t["sources_ok"]}/{t["sources_total"]}</span>'

        if is_mapped:
            # 该主题已经映射到某条 chain，详细信息在那个 chain 卡里展示
            # 这里只给一行 mini 摘要，引导用户去链卡
            n_metrics = t.get("metrics_count", 0)
            metric_hint = f"宏观指标 {n_metrics} 条" if n_metrics else "宏观指标暂未录入"
            mini_rows.append(f"""
<div class="flex items-center justify-between py-1.5 text-[12px] border-b border-slate-100 last:border-0">
  <div class="flex items-center gap-2">
    <span class="font-semibold text-slate-800">{_esc(t["display_name"])}</span>
    <span class="text-[11px] text-slate-500">数据源 {ok_badge} · {metric_hint}</span>
  </div>
  <span class="text-[11px] text-violet-700">详见下方链卡 ↓</span>
</div>
""")
        else:
            # chain 未覆盖的主题，独立成卡（如 AI 数据 / 当前可能也是稀土）
            # 完整渲染数据源 + 宏观指标
            src_items = []
            for s in t["sources_detail"]:
                status_emoji = "✅" if s["status"] == "ok" else "⚠️"
                tier_color = "text-violet-700" if s["source_tier"] == "A" else "text-sky-700"
                src_items.append(
                    f'<li class="text-[11px]">{status_emoji} <span class="{tier_color} font-mono">[{s["source_tier"]}]</span> '
                    f'<a href="{_esc(s["source_url"])}" target="_blank" class="text-slate-700 hover:text-violet-700 hover:underline">'
                    f'{_esc(s["source_name"])}</a>'
                    f'{" · " + _esc(s["status"]) if s["status"] != "ok" else ""}</li>'
                )
            metric_lines = []
            for m in (t.get("latest_metrics") or [])[:4]:
                val = m["metric_value"]
                val_str = f"{val:g}" if val is not None else "—"
                metric_lines.append(
                    f'<div class="text-[11px] flex items-baseline justify-between gap-2 py-0.5">'
                    f'<span class="text-slate-700">{_esc(m["metric_name"])}</span>'
                    f'<span class="font-mono text-slate-800">{val_str} '
                    f'<span class="text-slate-400 text-[10px]">{_esc(m.get("metric_unit") or "")}</span></span>'
                    f'<a href="{_esc(m["source_url"] or "")}" target="_blank" class="text-[10px] text-violet-600 hover:underline ml-2 whitespace-nowrap">'
                    f'{_esc(m["source_id"])} · {_esc(m["metric_date"])}</a>'
                    f'</div>'
                )
            metric_html = "".join(metric_lines) or '<div class="text-[11px] text-slate-400">宏观指标暂未录入</div>'

            standalone_cards.append(f"""
<details class="bg-white ring-1 ring-slate-200 rounded-xl p-4 mb-3">
  <summary class="cursor-pointer list-none flex items-start justify-between gap-2">
    <div class="flex-1 min-w-0">
      <div class="flex items-center gap-2 flex-wrap">
        <span class="text-base font-bold text-slate-900">{_esc(t["display_name"])}</span>
        <span class="text-[11px] text-amber-700">⚠️ chain 体系暂未覆盖此主题</span>
      </div>
      <div class="text-[12px] text-slate-600 mt-0.5">{_esc(t["why"])}</div>
      <div class="text-[11px] text-slate-500 mt-1">数据源 {ok_badge} · A 类 {t["sources_a"]} / B 类 {t["sources_b"]}</div>
    </div>
    <span class="text-slate-400 text-[12px] select-none">▾</span>
  </summary>
  <div class="mt-3 pt-3 border-t border-slate-100">
    <div class="text-[11px] text-slate-500 mb-1">最新宏观指标：</div>
    <div class="mb-3">{metric_html}</div>
    <div class="text-[11px] text-slate-500 mb-1">数据源清单：</div>
    <ul class="space-y-1 pl-1">{"".join(src_items)}</ul>
  </div>
</details>
""")

    phase = panel.get("phase_status") or {}
    p0 = "✅" if phase.get("phase_0_sources_seeded") else "⏳"
    p1 = "✅" if phase.get("phase_1_evidence_scanned") else "⏳"
    p2 = "✅" if phase.get("phase_2_dashboard_integrated") else "⏳"

    mini_html = ""
    if mini_rows:
        mini_html = f"""
<div class="bg-white ring-1 ring-slate-200 rounded-xl p-4 mb-3">
  <div class="flex items-center justify-between flex-wrap gap-2 mb-1">
    <div>
      <div class="text-sm font-bold text-slate-900">📑 5 前瞻主题数据基建</div>
      <div class="text-[11px] text-slate-500">下面这些主题已经映射到 AI 价值链，详细公司列表在对应链卡里。文档 §七。</div>
    </div>
    <div class="text-[11px] text-slate-500">
      Phase 0 数据源 {p0} · Phase 1 公司证据 {p1} · Phase 2 看板集成 {p2}
    </div>
  </div>
  {"".join(mini_rows)}
</div>
"""

    standalone_html = ""
    if standalone_cards:
        standalone_html = f"""
<div class="mb-3">
  <div class="text-sm font-bold text-slate-900 mb-2">🔭 前瞻主题 · chain 未覆盖</div>
  <div class="text-[11px] text-slate-500 mb-2">这些主题尚未在 AI 价值链 chain 体系中独立成类，下面只展示宏观数据源和指标，待 universe 扩张或 chain 类目新增后才会出现公司列表。</div>
  {"".join(standalone_cards)}
</div>
"""

    return mini_html + standalone_html


def _render_theme_evidence_panel(panel: dict[str, Any]) -> str:
    """渲染 5 主题宏观证据卡（旧版 — 保留兼容，未使用）。"""
    if not panel or not panel.get("themes"):
        return ""

    cards = []
    for t in panel["themes"]:
        tid = t["theme_id"]
        ok = t["sources_ok"]
        total = t["sources_total"]
        ok_color = "text-emerald-700" if ok == total else "text-amber-700" if ok else "text-rose-700"
        ok_badge = f'<span class="font-semibold {ok_color}">{ok}/{total}</span>'

        # 源详情（悬浮提示，避免主卡片冗长）
        src_items = []
        for s in t["sources_detail"]:
            status_emoji = "✅" if s["status"] == "ok" else "⚠️"
            tier_color = "text-violet-700" if s["source_tier"] == "A" else "text-sky-700"
            src_items.append(
                f'<li class="text-[11px]">{status_emoji} <span class="{tier_color} font-mono">[{s["source_tier"]}]</span> '
                f'<a href="{_esc(s["source_url"])}" target="_blank" class="text-slate-700 hover:text-violet-700 hover:underline">'
                f'{_esc(s["source_name"])}</a>'
                f'{" · " + _esc(s["status"]) if s["status"] != "ok" else ""}'
                f'<div class="text-[10px] text-slate-400 ml-5">{_esc(s.get("note") or "")}</div></li>'
            )

        # 公司证据状态
        n_conf = t["evidence_confirmed"]
        n_cand = t["evidence_candidate"]
        n_stale = t["evidence_stale"]
        if n_conf + n_cand + n_stale == 0:
            evidence_html = (
                '<span class="text-[11px] text-slate-400">'
                '公司证据：尚未启动（Phase 1 SEC 扫描待接入）</span>'
            )
        else:
            evidence_html = (
                f'<span class="text-[11px]">'
                f'<span class="text-emerald-700 font-semibold">{n_conf}</span> confirmed · '
                f'<span class="text-sky-700">{n_cand}</span> candidate · '
                f'<span class="text-amber-700">{n_stale}</span> stale</span>'
            )

        metrics_html = (
            f'<span class="text-[11px] text-slate-400">宏观指标：暂未录入</span>'
            if t["metrics_count"] == 0
            else f'<span class="text-[11px] text-slate-600">宏观指标 {t["metrics_count"]} 条 · '
                 f'最新 {_esc(t["latest_metric_date"])}</span>'
        )

        # 关联 picks（通过 chain mapping）
        # 注意：chain_metadata 是粗分类（"数据中心电力"包电力+液冷）；relevance 标
        #   direct  → chain 内全部 picks 都符合此主题，可展示具体 ticker
        #   partial → chain 内只有部分子类符合，只能给数量提示，不展示具体 ticker
        #             避免把 VST/GEV（电力）误标成水冷 picks
        n_assoc = t.get("n_picks_assoc", 0)
        chains_mapped = t.get("chains_mapped", [])
        chain_rel = t.get("chain_relevance", {})  # {chain: relevance}
        has_direct = any(chain_rel.get(c) == "direct" for c in chains_mapped)

        if not chains_mapped:
            picks_assoc_html = (
                '<div class="text-[11px] text-amber-700 mt-2">'
                '⚠️ chain 体系未覆盖此主题 — 等待 universe 扩张或新 chain 类目'
                '</div>'
            )
        elif n_assoc == 0:
            picks_assoc_html = (
                f'<div class="text-[11px] text-slate-500 mt-2">'
                f'关联 chain：{_esc("、".join(chains_mapped))} · 该 chain 在最新 picks 内 0 只命中'
                f'</div>'
            )
        else:
            # 不管 direct 还是 partial 都展示具体票 — partial 加 needs_review 警示
            # 之前 partial 隐藏具体票造成用户"找不到水冷链"的认知断层
            pick_chips = []
            for p in t["picks_assoc"]:
                # partial 票额外加 ⚠️ 标签
                ts_warn = '' if has_direct else (
                    '<span class="text-amber-600 text-[10px] ml-1" title="chain 粗映射，主营是否属于此主题需人工核实">⚠️</span>'
                )
                pick_chips.append(
                    f'<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-slate-50 ring-1 ring-slate-200 text-[11px]">'
                    f'{_market_label(p["market"])} <span class="font-mono">{_esc(p["symbol"])}</span> '
                    f'<span class="text-slate-500">{_esc((p["name"] or "")[:10])}</span> '
                    f'<span class="font-semibold text-slate-700">{p["score"]:.0f}</span>'
                    f'{ts_warn}'
                    f'</span>'
                )

            header_note = (
                f'通过 chain「{_esc("、".join(chains_mapped))}」关联的 picks（{n_assoc} 只，前 {len(t["picks_assoc"])}）：'
                if has_direct
                else f'⚠️ <strong>粗映射</strong>：通过 chain「{_esc("、".join(chains_mapped))}」关联（{n_assoc} 只）· 该 chain 是粗分类，每只票主营是否真属于此主题需人工核实'
            )
            note_color = "text-slate-500" if has_direct else "text-amber-700"

            picks_assoc_html = (
                f'<div class="mt-2 pt-2 border-t border-slate-100">'
                f'<div class="text-[11px] {note_color} mb-1">{header_note}</div>'
                f'<div class="flex flex-wrap gap-1.5">{"".join(pick_chips)}</div>'
                f'</div>'
            )

        # 宏观指标详情
        latest_metrics = t.get("latest_metrics") or []
        if latest_metrics:
            metric_rows = []
            for m in latest_metrics:
                val = m["metric_value"]
                val_str = f"{val:g}" if val is not None else "—"
                metric_rows.append(
                    f'<div class="text-[11px] flex items-baseline justify-between gap-2 py-0.5">'
                    f'<span class="text-slate-700">{_esc(m["metric_name"])}</span>'
                    f'<span class="font-mono text-slate-800">{val_str} '
                    f'<span class="text-slate-400 text-[10px]">{_esc(m.get("metric_unit") or "")}</span></span>'
                    f'<a href="{_esc(m["source_url"] or "")}" target="_blank" class="text-[10px] text-violet-600 hover:underline ml-2 whitespace-nowrap">'
                    f'{_esc(m["source_id"])} · {_esc(m["metric_date"])}</a>'
                    f'</div>'
                )
            metrics_detail_html = (
                f'<div class="mt-2 pt-2 border-t border-slate-100">'
                f'<div class="text-[11px] text-slate-500 mb-1">最新宏观指标：</div>'
                f'{"".join(metric_rows)}</div>'
            )
        else:
            metrics_detail_html = ""

        # 摘要里明确指出关联 picks 去哪条 chain 找 — 避免"下面找不到水冷链"的认知断层
        if chains_mapped and n_assoc > 0:
            rel_marker = "" if has_direct else " <span class=\"text-[10px] text-amber-600\">(粗映射)</span>"
            picks_summary = (
                f'<span class="text-[11px] text-slate-500">'
                f'关联 {n_assoc} 只 picks · 在下方「<span class="font-semibold text-slate-700">{_esc("、".join(chains_mapped))}</span>」链卡{rel_marker}'
                f'</span>'
            )
        elif chains_mapped:
            picks_summary = (
                f'<span class="text-[11px] text-slate-500">'
                f'关联 chain「{_esc("、".join(chains_mapped))}」· 该 chain 在最新 picks 内 0 只命中'
                f'</span>'
            )
        else:
            picks_summary = (
                '<span class="text-[11px] text-amber-700">'
                '⚠️ chain 体系暂未覆盖此主题'
                '</span>'
            )

        cards.append(f"""
<details class="bg-white ring-1 ring-slate-200 rounded-xl p-4 mb-3 group">
  <summary class="cursor-pointer list-none flex items-start justify-between gap-2">
    <div class="flex-1 min-w-0">
      <div class="flex items-center gap-2 flex-wrap">
        <span class="text-base font-bold text-slate-900">{_esc(t["display_name"])}</span>
        <span class="text-[11px] text-slate-500">数据源 {ok_badge} · A 类 {t["sources_a"]} / B 类 {t["sources_b"]}</span>
      </div>
      <div class="text-[12px] text-slate-600 mt-0.5">{_esc(t["why"])}</div>
      <div class="mt-1">{picks_summary}</div>
      <div class="mt-1 flex flex-wrap gap-3">{evidence_html}{metrics_html}</div>
    </div>
    <span class="text-slate-400 text-[12px] group-open:rotate-180 transition select-none">▾</span>
  </summary>
  {picks_assoc_html}
  {metrics_detail_html}
  <div class="mt-3 pt-3 border-t border-slate-100">
    <div class="text-[11px] text-slate-500 mb-1">数据源清单（点击进官方页）</div>
    <ul class="space-y-1 pl-1">{"".join(src_items)}</ul>
  </div>
</details>
""")

    # 顶部说明
    phase = panel.get("phase_status") or {}
    p0 = "✅" if phase.get("phase_0_sources_seeded") else "⏳"
    p1 = "✅" if phase.get("phase_1_evidence_scanned") else "⏳"
    p2 = "✅" if phase.get("phase_2_dashboard_integrated") else "⏳"

    return f"""
<div class="bg-white ring-1 ring-slate-200 rounded-xl p-4 mb-4">
  <div class="flex items-center justify-between flex-wrap gap-2 mb-2">
    <div>
      <div class="text-sm font-bold text-slate-900">📑 5 主题数据基建状态</div>
      <div class="text-[11px] text-slate-500">行业理解层的"证据底座"：每条主题先有公开数据源，再谈公司映射。文档 §七。</div>
    </div>
    <div class="text-[11px] text-slate-500">
      Phase 0 数据源 {p0} · Phase 1 公司证据 {p1} · Phase 2 看板集成 {p2}
    </div>
  </div>
  {"".join(cards)}
  <div class="mt-2 text-[10px] text-slate-400 leading-relaxed">
    主题证据卡只显示"该主题是否有可追溯的官方数据源"，不代表任何公司归属判断。
    A 类 = 政府/监管/公司财报；B 类 = ETF/行业协会；C 类 = 新闻。confirmed 必须 ≥1 A 类源 + 90 天内有新证据。
  </div>
</div>
"""


def render_ai_radar_section(payload: dict[str, Any], *, my_view_headline: str | None = None,
                            my_view_summary: str | None = None,
                            theme_panel: dict[str, Any] | None = None,
                            etf_panel: dict[str, Any] | None = None) -> str:
    """渲染 AI 主题雷达 section（独立 tab 内容）。

    硬规则（对应文档 §五）：
      - 不写 watchlist / 真实持仓
      - 不出现"买入 / 建仓 / 配置 / 推荐 X"等指示性文案
      - 覆盖率不足显式呈现，不隐藏未分类股票
    """
    n_total = payload["n_picks_total"]
    n_chain = payload["n_picks_with_chain"]
    coverage_pct = round(n_chain / n_total * 100, 1) if n_total else 0
    data_at = (payload.get("data_generated_at") or "")[:19].replace("T", " ")

    headline = my_view_headline or ""
    summary = my_view_summary or ""

    head_html = f"""
<div class="bg-gradient-to-br from-violet-50 to-indigo-50 rounded-xl ring-1 ring-violet-200 p-5 mb-4">
  <div class="text-xs text-violet-700 mb-1">当前 AI 主线判断（作者观点）</div>
  <div class="text-lg font-bold text-slate-900 mb-2">{_esc(headline)}</div>
  <div class="text-sm text-slate-700 leading-relaxed">{_esc(summary)}</div>
</div>
"""

    # 5 主题宏观证据 — 简化版：已映射到 chain 的主题只在链卡里展示详情，
    # 这里只显示一行 mini 摘要；chain 未覆盖的主题（如 AI 数据）才独立成卡
    chain_to_themes_set = set()
    if theme_panel:
        try:
            import duckdb as _ddb
            # 直接从 payload chains.mapped_themes 反推已映射的主题
            for c in payload.get("chains") or []:
                for mt in c.get("mapped_themes") or []:
                    chain_to_themes_set.add(mt["theme_id"])
        except Exception:
            pass
    theme_panel_html = (
        _render_theme_evidence_panel_compact(theme_panel, chain_to_themes_set)
        if theme_panel else ""
    )
    etf_panel_html = _render_etf_consensus_panel(etf_panel) if etf_panel else ""

    rise_chains = [c for c in payload["chains"] if (c.get("delta_7d") or 0) >= MAINLINE_RISE_DELTA]
    fall_chains = [c for c in payload["chains"] if (c.get("delta_7d") or 0) <= MAINLINE_FALL_DELTA]
    rise_html = "、".join(f'<span class="font-semibold text-emerald-700">{_esc(c["chain"])}</span> (+{c["delta_7d"]})' for c in rise_chains) or '<span class="text-slate-400">无</span>'
    fall_html = "、".join(f'<span class="font-semibold text-rose-700">{_esc(c["chain"])}</span> ({c["delta_7d"]})' for c in fall_chains) or '<span class="text-slate-400">无</span>'

    trend_html = f"""
<div class="bg-white ring-1 ring-slate-200 rounded-xl p-4 mb-4">
  <div class="text-xs text-slate-500 mb-1">📊 数据驱动的链条趋势 · 系统打分近 7 天变化</div>
  <div class="text-sm text-slate-700 mb-1">📈 发酵中：{rise_html}</div>
  <div class="text-sm text-slate-700">📉 冷却中：{fall_html}</div>
  <div class="text-[11px] text-slate-400 mt-2">指标含义：同一条产业链最新 picks 的平均系统分 - 7 天前 picks 的平均分。非买卖信号，仅供识别"市场资金/打分关注度在哪条链上"。</div>
</div>
"""

    kpi_html = f"""
<div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
  <div class="bg-white ring-1 ring-slate-200 rounded-lg p-3">
    <div class="text-[11px] text-slate-500">数据来源</div>
    <div class="text-sm font-semibold text-slate-800">{_esc(payload.get("data_universe"))}</div>
    <div class="text-[10px] text-slate-400 mt-0.5">最新 run · {_esc(data_at)}</div>
  </div>
  <div class="bg-white ring-1 ring-slate-200 rounded-lg p-3">
    <div class="text-[11px] text-slate-500">系统 picks</div>
    <div class="text-lg font-semibold text-slate-800">{n_total}</div>
    <div class="text-[10px] text-slate-400 mt-0.5">三市场最新一批合计</div>
  </div>
  <div class="bg-white ring-1 ring-slate-200 rounded-lg p-3">
    <div class="text-[11px] text-slate-500">已分类</div>
    <div class="text-lg font-semibold text-slate-800">{n_chain} <span class="text-xs text-slate-500">({coverage_pct}%)</span></div>
    <div class="text-[10px] text-slate-400 mt-0.5">含 chain_metadata 标签</div>
  </div>
  <div class="bg-white ring-1 ring-slate-200 rounded-lg p-3">
    <div class="text-[11px] text-slate-500">价值链层数</div>
    <div class="text-lg font-semibold text-slate-800">{len(payload["chains"])}</div>
    <div class="text-[10px] text-slate-400 mt-0.5">已命中 picks 的链</div>
  </div>
</div>
"""

    chain_cards = []
    for c in payload["chains"]:
        rows = []
        for p in c["top_picks"]:
            intro = p.get("layman_intro") or ""
            role = p.get("chain_role") or "—"
            tier = p.get("chain_tier") or ""
            tier_badge = (f'<span class="text-[10px] text-slate-500">· {_esc(tier)}</span>' if tier else "")
            rows.append(f"""
<tr class="border-t border-slate-100">
  <td class="py-1.5 pr-2 align-top text-[12px] whitespace-nowrap">{_market_label(p["market"])} <span class="font-mono">{_esc(p["symbol"])}</span></td>
  <td class="py-1.5 pr-2 align-top text-[12px]">{_esc(p["name"] or "")}</td>
  <td class="py-1.5 pr-2 align-top text-[12px] font-semibold text-slate-800 text-right whitespace-nowrap">{p["score"]:.1f}</td>
  <td class="py-1.5 pr-2 align-top text-[11px] text-slate-600">{_esc(role)} {tier_badge}<div class="text-[10px] text-slate-400 mt-0.5">{_esc(intro)}</div></td>
  <td class="py-1.5 pr-2 align-top text-[11px] whitespace-nowrap">{_strength_badge(p["ai_strength"])} {_source_badge(p["chain_source"])}</td>
  <td class="py-1.5 pl-2 align-top text-[11px] whitespace-nowrap">{_watchlist_badge(p["in_watchlist"])}</td>
</tr>
""")

        delta = c.get("delta_7d")
        delta_color = "text-emerald-700" if (delta or 0) >= MAINLINE_RISE_DELTA else \
                      "text-rose-700" if (delta or 0) <= MAINLINE_FALL_DELTA else "text-slate-500"
        delta_txt = f'{delta:+.1f}' if delta is not None else 'N/A'

        # 该 chain 对应哪些前瞻主题（用于在 chain 卡顶部打主题标签 + 嵌主题宏观证据）
        mapped_themes = c.get("mapped_themes") or []
        theme_badges = []
        theme_evidence_blocks = []
        if mapped_themes and theme_panel:
            theme_idx = {tp["theme_id"]: tp for tp in theme_panel.get("themes", [])}
            for mt in mapped_themes:
                rel = mt.get("relevance") or "direct"
                rel_marker = "" if rel == "direct" else " <span class=\"text-[10px] text-amber-600\">(粗映射)</span>"
                theme_badges.append(
                    f'<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] '
                    f'bg-violet-50 text-violet-800 ring-1 ring-violet-200">📑 {_esc(mt["display"])}</span>{rel_marker}'
                )
                # 把该主题的宏观证据嵌入（折叠）
                tp = theme_idx.get(mt["theme_id"])
                if tp:
                    metric_lines = []
                    for m in (tp.get("latest_metrics") or [])[:4]:
                        val = m["metric_value"]
                        val_str = f"{val:g}" if val is not None else "—"
                        metric_lines.append(
                            f'<div class="text-[11px] flex items-baseline justify-between gap-2 py-0.5">'
                            f'<span class="text-slate-700">{_esc(m["metric_name"])}</span>'
                            f'<span class="font-mono text-slate-800">{val_str} '
                            f'<span class="text-slate-400 text-[10px]">{_esc(m.get("metric_unit") or "")}</span></span>'
                            f'<a href="{_esc(m["source_url"] or "")}" target="_blank" '
                            f'class="text-[10px] text-violet-600 hover:underline ml-2 whitespace-nowrap">'
                            f'{_esc(m["source_id"])} · {_esc(m["metric_date"])}</a>'
                            f'</div>'
                        )
                    src_items = []
                    for s in (tp.get("sources_detail") or []):
                        status_emoji = "✅" if s["status"] == "ok" else "⚠️"
                        src_items.append(
                            f'<li class="text-[11px]">{status_emoji} '
                            f'<span class="text-violet-700 font-mono">[{s["source_tier"]}]</span> '
                            f'<a href="{_esc(s["source_url"])}" target="_blank" '
                            f'class="text-slate-700 hover:underline">{_esc(s["source_name"])}</a>'
                            f'{" · " + _esc(s["status"]) if s["status"] != "ok" else ""}</li>'
                        )
                    metric_html = ("".join(metric_lines)
                                   or '<div class="text-[11px] text-slate-400">该主题暂未录入宏观指标</div>')
                    theme_evidence_blocks.append(f"""
<details class="mt-2 bg-violet-50/50 rounded-lg ring-1 ring-violet-200 p-3">
  <summary class="cursor-pointer text-[12px] text-violet-800 font-semibold list-none flex items-center justify-between">
    <span>📑 {_esc(tp["display_name"])} · 主题宏观证据</span>
    <span class="text-[10px] text-slate-500 group-open:hidden">点开看 ▾</span>
  </summary>
  <div class="mt-2 pt-2 border-t border-violet-200">
    <div class="text-[10px] text-slate-500 mb-1">受益逻辑：{_esc(tp["why"])}</div>
    <div class="mb-2">{metric_html}</div>
    <div class="text-[10px] text-slate-500 mb-1">数据源（{tp["sources_ok"]}/{tp["sources_total"]} ok · A 类 {tp["sources_a"]} / B 类 {tp["sources_b"]}）：</div>
    <ul class="space-y-0.5 pl-1">{"".join(src_items)}</ul>
  </div>
</details>
""")

        themes_header_html = (
            f'<div class="mt-1 flex items-center gap-1.5 flex-wrap">'
            f'<span class="text-[10px] text-slate-500">对应前瞻主题：</span>'
            f'{" ".join(theme_badges)}</div>'
            if theme_badges else ""
        )
        theme_evidence_html = "".join(theme_evidence_blocks)

        chain_cards.append(f"""
<div class="bg-white ring-1 ring-slate-200 rounded-xl p-4 mb-3">
  <div class="flex items-center justify-between flex-wrap gap-2 mb-2">
    <div class="flex-1 min-w-0">
      <div class="flex items-center gap-2 flex-wrap">
        <span class="text-base font-bold text-slate-900">{_esc(c["chain"])}</span>
        {_strength_badge(c["ai_strength"])}
        <span class="text-[12px] text-slate-500">· {c["n_stocks"]} 只 · 均分 {c["avg_score"]:.1f}</span>
      </div>
      {themes_header_html}
    </div>
    <div class="text-[12px]">
      <span class="text-slate-400">近 7 天均分变化：</span>
      <span class="font-mono font-semibold {delta_color}">{delta_txt}</span>
      <span class="ml-1 text-slate-500">{_esc(c["mainline_status"])}</span>
    </div>
  </div>
  {theme_evidence_html}
  <table class="w-full text-[12px]">
    <thead>
      <tr class="text-[10px] text-slate-400 uppercase tracking-wide">
        <th class="py-1 pr-2 text-left font-normal">市场/代码</th>
        <th class="py-1 pr-2 text-left font-normal">名称</th>
        <th class="py-1 pr-2 text-right font-normal">系统分</th>
        <th class="py-1 pr-2 text-left font-normal">受益路径</th>
        <th class="py-1 pr-2 text-left font-normal">AI 关联 · 证据</th>
        <th class="py-1 pl-2 text-left font-normal">状态</th>
      </tr>
    </thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>
""")

    chains_html = "".join(chain_cards) if chain_cards else (
        '<div class="bg-slate-50 ring-1 ring-slate-200 rounded p-4 text-slate-500 text-sm">'
        '没有可展示的链条 — 最新一批 picks 全部未匹配 chain_metadata。'
        '</div>'
    )

    ca = payload["coverage_audit"]
    if ca["n_uncovered"]:
        audit_rows = []
        for it in ca["items"]:
            audit_rows.append(f"""
<tr class="border-t border-amber-100">
  <td class="py-1 pr-2 text-[12px] whitespace-nowrap">{_market_label(it["market"])} <span class="font-mono">{_esc(it["symbol"])}</span></td>
  <td class="py-1 pr-2 text-[12px]">{_esc(it["name"] or "")}</td>
  <td class="py-1 pl-2 text-[12px] text-right font-semibold text-slate-800">{it["score"]:.1f}</td>
</tr>
""")
        audit_html = f"""
<div class="bg-amber-50 ring-1 ring-amber-200 rounded-xl p-4 mb-3">
  <div class="text-sm font-bold text-amber-900 mb-1">⚠️ 覆盖率审计 · {ca["n_uncovered"]} 只系统高分票缺 chain 标签</div>
  <div class="text-[12px] text-amber-800 mb-2">阈值：系统分 ≥ {ca["threshold_score"]:.0f}。这些票在最新一批 picks 里、但 chain_metadata 没有规则或人工 override 命中。它们不会被隐藏，只是无法归入任何一条 AI 价值链。补 chain 规则后下次刷新自动出现。</div>
  <table class="w-full">
    <thead>
      <tr class="text-[10px] text-amber-700 uppercase tracking-wide">
        <th class="py-1 pr-2 text-left font-normal">市场/代码</th>
        <th class="py-1 pr-2 text-left font-normal">名称</th>
        <th class="py-1 pl-2 text-right font-normal">系统分</th>
      </tr>
    </thead>
    <tbody>{"".join(audit_rows)}</tbody>
  </table>
</div>
"""
    else:
        audit_html = ('<div class="bg-emerald-50 ring-1 ring-emerald-200 rounded-xl p-3 text-[12px] text-emerald-800">'
                      '✓ 覆盖率审计通过：最新一批 picks 中没有"系统高分但缺 chain"的票。</div>')

    footer_html = """
<div class="mt-4 text-[11px] text-slate-500 leading-relaxed bg-slate-50 ring-1 ring-slate-200 rounded p-3">
  <strong class="text-slate-700">本页定位：</strong>
  AI 主题雷达只解释「这些票为什么和 AI 有关、当前主线在哪一层」，不是买入清单。
  系统分高与 AI 关联强都只是分类指标，不构成买入信号。
  实际下单仍由「AI 推荐 / AI 配仓 / 买前研究」共同决定，本页不写自选股、不写真实持仓。
</div>
"""

    return f"""
<section id="ai-radar" class="max-w-7xl mx-auto px-6 py-10" style="display:none">
  <div class="mb-4">
    <h1 class="text-2xl font-bold text-slate-900 flex items-center gap-2">📡 AI 主题雷达</h1>
    <p class="text-sm text-slate-600 mt-1">AI 价值链全景 · 行业理解层 · 不构成买入建议</p>
  </div>
  {head_html}
  {etf_panel_html}
  {theme_panel_html}
  {trend_html}
  {kpi_html}
  {chains_html}
  {audit_html}
  {footer_html}
</section>
"""

