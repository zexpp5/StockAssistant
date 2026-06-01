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
AI_RADAR_VISIBLE_STRENGTHS = {"强", "中", "弱"}


def derive_ai_strength(chain: str | None) -> str | None:
    """按 chain 反推 AI 关联强度；未分类返回 None（前端显示『未分类』）。"""
    if not chain:
        return None
    return AI_STRENGTH_BY_CHAIN.get(chain, "无")


# ─────────────── 覆盖率审计阈值 ───────────────
# 系统打分高但缺 chain 标签的票 — 进入审计卡片，提示运维补 chain_metadata
COVERAGE_AUDIT_SCORE_THRESHOLD = 70.0


# ─────────────── AI 雷达视野白名单（单一来源）───────────────
# 用于 audit 去噪：只把"AI 相关"的高分票当成"缺口"
# 排除非 AI 行业（检测/零售/化工/纺织/金融 等），避免污染 AI 雷达视野
# coverage_audit.py 必须 import 此常量，禁止双引擎
#
# 2026-06-01 收窄：去掉 C35/C38/C39/C40 这些 A 股 GICS 大类
#   原因：大类太宽，把"湖南裕能(C39 锂电)/阿特斯(C38 光伏)/健帆生物(C35 血液净化)"
#         这种非 AI 主线票拉进 audit，制造假阳性
#   保留：I63/I64/I65（电信/互联网/软件信息技术 — 这些粒度足够窄）
#         M73（研发，主要是科技 R&D 公司）
#   非 AI 高分 A 股票现在靠 chain_classifier name 规则识别（已加海康/浪潮/紫光 等具体名）
AI_RELEVANT_THEME_KEYWORDS = (
    # 英文 theme/industry
    "ai", "semiconductor", "cloud", "saas", "software", "internet",
    "cooling", "power", "grid", "electrical", "nuclear", "uranium",
    "rare earth", "robot", "data", "quantum",
    # 中文
    "互联网", "半导体", "软件", "机器人", "云",
    # A 股 GICS 行业代码 — 只留窄类，去 C35/C38/C39/C40 大类避免假阳性
    "I65",  # 软件信息技术
    "I64",  # 互联网相关
    "I63",  # 电信
    "M73",  # 研发
)


def is_ai_relevant_universe(theme: str | None, industry: str | None) -> bool:
    """判断 system_universe 票是否属于 AI 雷达视野（基于 theme/industry 关键词）。"""
    hay = (theme or "").lower() + " " + (industry or "").lower()
    return any(kw.lower() in hay for kw in AI_RELEVANT_THEME_KEYWORDS)


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
    su.theme, su.industry,
    rr.run_id, rr.generated_at
FROM recommendation_runs rr
JOIN recommendation_picks rp ON rp.run_id = rr.run_id
JOIN latest_run l ON l.market = rp.market AND l.latest_at = rr.generated_at
LEFT JOIN chain_metadata cm ON cm.market = rp.market AND cm.symbol = rp.symbol
LEFT JOIN system_universe su ON su.market = rp.market AND su.symbol = rp.symbol
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


def build_research_shortlist(con, top_n: int = 5) -> dict[str, Any]:
    """把雷达多源信号汇成"值得研究 N 只"清单（不是买入清单）。

    打分维度（5 个，max 100）:
      系统打分     30 picks.total_score × 0.3（最近一批 picks 命中）
      ETF 共识     25 出现在几个主题 ETF + 持仓权重总和
      公司证据     20 confirmed=20 / candidate=10 / stale=3 / 无=0
      AI 关联强度  15 强=15 / 中=10 / 弱=5
      趋势         10 chain 7 天 delta：发酵=10 / 持稳=5 / 冷却=0

    候选范围（防止引入未追踪票）:
      - 必须在 system_universe + active
      - chain_metadata 必须有 chain
      - ai_strength 必须是 强/中/弱（非"无"）

    每只票输出 "why_now" 多源理由 chip，可追溯到具体来源。

    硬规则:
      - 输出文案绝不含"买入/推荐买入"
      - 标"值得研究"，定位为"买前研究" tab 的 funnel
    """
    # 1) 拉候选池：universe active + 有 chain + ai_strength 非"无"
    pool_rows = con.execute("""
        SELECT cm.market, cm.symbol, cm.chain, cm.chain_role, cm.layman_intro,
               su.name AS universe_name
        FROM chain_metadata cm
        JOIN system_universe su ON su.market = cm.market AND su.symbol = cm.symbol AND su.active = TRUE
        WHERE cm.chain IS NOT NULL
    """).fetchall()

    pool: dict[tuple, dict] = {}
    for m, s, chain, role, intro, name in pool_rows:
        strength = derive_ai_strength(chain)
        if strength == "无" or strength is None:
            continue
        pool[(m, s)] = {
            "market": m, "symbol": s, "chain": chain,
            "chain_role": role, "layman_intro": intro, "universe_name": name,
            "ai_strength": strength,
        }

    if not pool:
        return {"items": [], "n_candidates": 0, "generated_at": datetime.now().isoformat(timespec="seconds")}

    # 2) 系统打分（最近一批 picks）
    pick_rows = con.execute("""
        WITH latest_run AS (
            SELECT rp.market, MAX(rr.generated_at) AS latest_at
            FROM recommendation_runs rr
            JOIN recommendation_picks rp ON rp.run_id = rr.run_id
            WHERE rr.universe_scope = 'system_tech_universe'
            GROUP BY rp.market
        )
        SELECT rp.market, rp.symbol, rp.total_score, rp.name
        FROM recommendation_runs rr
        JOIN recommendation_picks rp ON rp.run_id = rr.run_id
        JOIN latest_run l ON l.market = rp.market AND l.latest_at = rr.generated_at
    """).fetchall()
    pick_score: dict[tuple, float] = {(m, s): float(score) for m, s, score, _ in pick_rows}
    pick_name: dict[tuple, str] = {(m, s): n for m, s, _, n in pick_rows}

    # 3) ETF 共识（universe_match 反查）
    etf_rows = con.execute("""
        SELECT h.universe_match AS symbol, h.weight, u.etf_ticker, u.theme_label
        FROM ai_theme_etf_holdings h
        JOIN ai_theme_etf_universe u ON u.etf_ticker = h.etf_ticker
        WHERE h.universe_match IS NOT NULL
    """).fetchall()
    etf_info: dict[str, dict] = {}
    for sym, w, etf, label in etf_rows:
        if sym not in etf_info:
            etf_info[sym] = {"etfs": [], "weight_sum": 0.0}
        etf_info[sym]["etfs"].append({"etf": etf, "label": label, "weight": float(w or 0)})
        etf_info[sym]["weight_sum"] += float(w or 0)

    # 4) 公司证据（按 symbol → 最高状态）
    evidence_rows = con.execute("""
        SELECT symbol, evidence_status, theme
        FROM ai_theme_company_tags
    """).fetchall()
    evi_status_rank = {"confirmed": 4, "candidate": 3, "needs_review": 2, "stale": 1, "rejected": 0}
    evi_info: dict[str, dict] = {}
    for sym, status, theme in evidence_rows:
        cur = evi_info.get(sym, {"status": None, "themes": []})
        if cur["status"] is None or evi_status_rank.get(status, 0) > evi_status_rank.get(cur["status"], 0):
            cur["status"] = status
        cur["themes"].append(theme)
        evi_info[sym] = cur

    # 5) chain 7 天 delta
    delta_rows = con.execute("""
        WITH series AS (
            SELECT cm.chain, DATE(rr.generated_at) AS dt, AVG(rp.total_score) AS avg_score,
                   ROW_NUMBER() OVER (PARTITION BY cm.chain ORDER BY DATE(rr.generated_at)) AS rn_asc,
                   ROW_NUMBER() OVER (PARTITION BY cm.chain ORDER BY DATE(rr.generated_at) DESC) AS rn_desc
            FROM recommendation_runs rr
            JOIN recommendation_picks rp ON rp.run_id = rr.run_id
            JOIN chain_metadata cm ON cm.market = rp.market AND cm.symbol = rp.symbol
            WHERE rr.generated_at >= CURRENT_TIMESTAMP - INTERVAL '8 days'
              AND cm.chain IS NOT NULL
            GROUP BY cm.chain, DATE(rr.generated_at)
        )
        SELECT chain,
               MAX(CASE WHEN rn_desc = 1 THEN avg_score END) -
               MAX(CASE WHEN rn_asc = 1 THEN avg_score END) AS delta
        FROM series GROUP BY chain
    """).fetchall()
    chain_delta = {chain: float(d) if d is not None else 0.0 for chain, d in delta_rows}

    # 6) 打分 + why_now 拼接
    AI_STRENGTH_POINTS = {"强": 15, "中": 10, "弱": 5}
    EVI_POINTS = {"confirmed": 20, "candidate": 10, "needs_review": 5, "stale": 3}

    scored: list[dict] = []
    for (m, s), info in pool.items():
        # 系统打分
        sys_score = pick_score.get((m, s), 0.0)
        sys_pts = min(sys_score * 0.3, 30) if sys_score > 0 else 0

        # ETF 共识
        e = etf_info.get(s) or {}
        n_etfs = len(e.get("etfs") or [])
        etf_weight = float(e.get("weight_sum") or 0)
        etf_pts = min(n_etfs * 5 + etf_weight * 0.5, 25)

        # 公司证据
        evi = evi_info.get(s) or {}
        evi_status = evi.get("status")
        evi_pts = EVI_POINTS.get(evi_status, 0)

        # AI 关联强度
        strength_pts = AI_STRENGTH_POINTS.get(info["ai_strength"], 0)

        # 趋势
        delta = chain_delta.get(info["chain"], 0.0)
        if delta >= MAINLINE_RISE_DELTA:
            trend_pts = 10; trend_label = "发酵中"
        elif delta <= MAINLINE_FALL_DELTA:
            trend_pts = 0; trend_label = "冷却中"
        else:
            trend_pts = 5; trend_label = "持稳"

        total = round(sys_pts + etf_pts + evi_pts + strength_pts + trend_pts, 1)

        # why_now 多源理由
        why_chips = []
        if sys_score > 0:
            why_chips.append(f"系统打分 {sys_score:.1f}")
        if n_etfs > 0:
            etf_codes = ",".join(x["etf"] for x in e["etfs"][:3])
            why_chips.append(f"ETF 共识 {n_etfs} 个 ({etf_codes})")
        if evi_status:
            why_chips.append(f"证据 {evi_status}")
        why_chips.append(f"AI 关联 {info['ai_strength']}")
        why_chips.append(f"chain「{info['chain']}」{trend_label}")

        scored.append({
            "market": m, "symbol": s,
            "name": pick_name.get((m, s)) or info["universe_name"] or info["chain_role"],
            "chain": info["chain"],
            "chain_role": info["chain_role"],
            "layman_intro": info["layman_intro"],
            "ai_strength": info["ai_strength"],
            "research_score": total,
            "components": {
                "system_score": round(sys_pts, 1),
                "etf_consensus": round(etf_pts, 1),
                "evidence": round(evi_pts, 1),
                "ai_strength": round(strength_pts, 1),
                "trend": round(trend_pts, 1),
            },
            "evidence_status": evi_status,
            "etf_count": n_etfs,
            "etf_weight_sum": round(etf_weight, 2),
            "chain_delta_7d": round(delta, 2),
            "trend_label": trend_label,
            "raw_system_score": round(sys_score, 1),
            "why_chips": why_chips,
        })

    scored.sort(key=lambda x: -x["research_score"])

    return {
        "items": scored[:top_n],
        "n_candidates": len(scored),
        "top_n": top_n,
        "scoring_doc": (
            "research_score 满分 100 = 系统打分 30 + ETF 共识 25 + "
            "公司证据 20 + AI 关联强度 15 + 趋势 10"
        ),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def build_freshness_panel(con) -> dict[str, Any]:
    """构建数据新鲜度面板（评审 P3 #9）。

    返回 5 个子系统的最近刷新时间 + 各自 stale 阈值判断:
      sources_checked  数据源 URL HEAD check  (7 天内 fresh)
      etf_fetched      ETF 持仓快照          (14 天内 fresh)
      evidence_captured SEC/公司证据         (30 天内 fresh)
      tags_aggregated  tags 聚合             (7 天内 fresh)
      metrics_captured 宏观指标               (90 天内 fresh)
    超过阈值标 stale，前端用 amber 提示。
    """
    from datetime import datetime, timedelta
    now = datetime.now()
    items = [
        ("sources_checked", "数据源健康", 7,
         "SELECT MAX(last_checked_at) FROM ai_theme_evidence_sources"),
        ("etf_fetched", "ETF 持仓快照", 14,
         "SELECT MAX(last_fetched_at) FROM ai_theme_etf_universe"),
        ("evidence_captured", "公司证据", 30,
         "SELECT MAX(captured_at) FROM ai_theme_company_evidence"),
        ("tags_aggregated", "公司标签聚合", 7,
         "SELECT MAX(updated_at) FROM ai_theme_company_tags"),
        ("metrics_captured", "宏观指标", 90,
         "SELECT MAX(captured_at) FROM ai_theme_topic_metrics"),
    ]

    out_items: list[dict[str, Any]] = []
    for key, label, stale_days, sql in items:
        try:
            ts = con.execute(sql).fetchone()[0]
        except Exception:
            ts = None
        if ts is None:
            status = "missing"
            age_days = None
        else:
            age = now - ts if hasattr(ts, "__sub__") else None
            age_days = age.days if age else None
            if age_days is None:
                status = "missing"
            elif age_days > stale_days:
                status = "stale"
            elif age_days > stale_days // 2:
                status = "aging"
            else:
                status = "fresh"
        out_items.append({
            "key": key,
            "label": label,
            "last_at": ts.isoformat() if hasattr(ts, "isoformat") else ts,
            "age_days": age_days,
            "stale_days": stale_days,
            "status": status,
        })

    n_stale = sum(1 for x in out_items if x["status"] in ("stale", "missing"))
    return {
        "items": out_items,
        "n_stale": n_stale,
        "checked_at": now.isoformat(timespec="seconds"),
    }


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

    # 拉主题↔chain 映射 — 独立于 picks，避免"chain 未覆盖"误报
    # 之前 bug：MP 在 chain_metadata（稀缺资源）但今天不在 picks，
    # 导致 rare_earths 主题误报"chain 体系暂未覆盖"，让用户以为系统没建稀土链
    theme_chain_relevance: dict[str, dict[str, str]] = {}
    try:
        for theme, chain, rel in con.execute(
            "SELECT theme, chain, relevance FROM ai_theme_chain_mapping"
        ).fetchall():
            theme_chain_relevance.setdefault(theme, {})[chain] = rel
    except Exception:
        pass

    # 拉主题关联 chain → 当前 picks（用于在主题下显示具体票）
    theme_chain_picks: dict[str, list[dict]] = {}
    for theme, chain, rel, market, symbol, name, score, role in con.execute(_SQL_THEME_CHAIN_PICKS).fetchall():
        theme_chain_picks.setdefault(theme, []).append({
            "chain": chain,
            "market": market,
            "symbol": symbol,
            "name": name,
            "score": float(score),
            "chain_role": role,
        })

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

    # Phase 1 三档化（修复之前的过度乐观 ✅）：
    #   未启动 = evidence 表为空
    #   PoC    = 有 evidence 条目但全部 candidate，或 confirmed 主题覆盖率 < 80%
    #   完成   = ≥ 80% 主题有 confirmed 公司证据
    n_evi_total = con.execute(
        "SELECT COUNT(*) FROM ai_theme_company_evidence"
    ).fetchone()[0]
    n_confirmed = con.execute(
        "SELECT COUNT(*) FROM ai_theme_company_evidence WHERE evidence_status = 'confirmed'"
    ).fetchone()[0]
    themes_with_confirmed = con.execute(
        "SELECT COUNT(DISTINCT theme) FROM ai_theme_company_evidence WHERE evidence_status = 'confirmed'"
    ).fetchone()[0]
    n_themes_total = len(THEME_ORDER)
    confirmed_coverage = themes_with_confirmed / n_themes_total if n_themes_total else 0.0

    if n_evi_total == 0:
        phase_1_level = "not_started"
    elif confirmed_coverage >= 0.8:
        phase_1_level = "done"
    else:
        phase_1_level = "poc"

    return {
        "themes": themes,
        "phase_status": {
            "phase_0_sources_seeded": all(t["sources_total"] > 0 for t in themes),
            "phase_1_level": phase_1_level,
            "phase_1_n_evidence": n_evi_total,
            "phase_1_n_confirmed": n_confirmed,
            "phase_1_themes_with_confirmed": themes_with_confirmed,
            "phase_1_themes_total": n_themes_total,
            "phase_2_dashboard_integrated": True,  # 这一刀就是 Phase 2 雏形
            # 保留旧字段兼容（其它读 phase_status 的地方）
            "phase_1_evidence_scanned": phase_1_level == "done",
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
            "theme", "industry",
            "run_id", "generated_at"]

    picks = [dict(zip(cols, r)) for r in rows]

    # 自选股集合 — 用 symbol 匹配（不靠 market）
    # 原因：manual_watchlist.market 字段存的是中文（"美股" / "A股·沪交所"），
    #      recommendation_picks.market 是英文短码（"US" / "CN" / "HK"），
    #      用 (market, symbol) 直接比会让"美股的 DELL"匹配不上"US 的 DELL"。
    # symbol 在跨市场冲突极小（ticker 体系一般独立），按 symbol 比足够鲁棒。
    watchlist_rows = con.execute(_SQL_WATCHLIST).fetchall()
    watchlist_symbols = {s for _m, s in watchlist_rows if s}

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
    filtered_non_ai_chains: list[dict[str, Any]] = []
    for chain, items in chain_buckets.items():
        if chain is None:
            continue  # 未分类的进覆盖率审计卡片，不进 chain 列表
        ai_strength = derive_ai_strength(chain)
        if ai_strength not in AI_RADAR_VISIBLE_STRENGTHS:
            # 非 AI 链 — 不进主视图但保留到独立"系统池其它高分板块"区，避免完全丢弃
            filtered_non_ai_chains.append({
                "chain": chain,
                "n_stocks": len(items),
                "avg_score": round(sum(p["total_score"] for p in items) / len(items), 1),
                "ai_strength": ai_strength,
                "top_picks": [
                    _format_pick(p, watchlist_symbols)
                    for p in sorted(items, key=lambda x: -x["total_score"])[:5]
                ],
            })
            continue
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
            "ai_strength": ai_strength,
            "delta_7d": round(delta, 2) if delta is not None else None,
            "mainline_status": classify_mainline(delta),
            "top_picks": [
                _format_pick(p, watchlist_symbols)
                for p in sorted(items, key=lambda x: -x["total_score"])[:5]
            ],
            "mapped_themes": chain_to_themes.get(chain, []),
        })

    # 按 AI 关联强度排序（强 → 中 → 弱），同档按平均分降序
    chains.sort(key=lambda c: (
        -AI_STRENGTH_RANK.get(c["ai_strength"], -1),
        -c["avg_score"],
    ))

    # 覆盖率审计：高分但 chain 为空 — 仅审计"AI 雷达视野内"的票
    # 避免华测检测/万辰集团/贝泰妮这种非 AI 高分票污染 AI 雷达
    uncovered = [
        p for p in picks
        if not p["chain"]
        and p["total_score"] >= COVERAGE_AUDIT_SCORE_THRESHOLD
        and is_ai_relevant_universe(p.get("theme"), p.get("industry"))
    ]
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
        "n_picks_with_chain": sum(c["n_stocks"] for c in chains),
        "n_picks_with_any_chain": sum(1 for p in picks if p["chain"]),
        "chains": chains,
        "filtered_non_ai_chains": sorted(
            filtered_non_ai_chains,
            key=lambda c: (-c["n_stocks"], c["chain"]),
        ),
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


def _format_pick(p: dict, watchlist_symbols: set) -> dict[str, Any]:
    """单只票的展示字段（注意：不包含任何推荐/买入文案）。

    in_watchlist 按 symbol 匹配 — manual_watchlist 与 picks 的 market 字段表示不一致
    （"美股" vs "US"），symbol 几乎唯一更鲁棒。
    """
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
        "in_watchlist": p["symbol"] in watchlist_symbols,
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


def _render_research_shortlist(sl: dict[str, Any]) -> str:
    """渲染紧凑版"研究优先级"。避免视觉上变成推荐/买入卡片。"""
    if not sl or not sl.get("items"):
        return ""

    rows = []
    for i, it in enumerate(sl["items"], 1):
        c = it["components"]
        trend_label = it["trend_label"]
        trend_color = "text-emerald-700" if trend_label == "发酵中" else "text-rose-700" if trend_label == "冷却中" else "text-slate-500"
        evidence = it.get("evidence_status") or "待补公司证据"
        evidence_color = "text-emerald-700" if evidence == "confirmed" else "text-slate-500"
        chips = " · ".join(it.get("why_chips") or [])
        rows.append(f"""
<tr class="border-t border-slate-100">
  <td class="py-2 pr-2 align-top text-[12px] text-slate-400 font-mono">#{i}</td>
  <td class="py-2 pr-2 align-top whitespace-nowrap">
    <div class="text-[13px] font-bold text-slate-900">{_market_label(it["market"])} <span class="font-mono">{_esc(it["symbol"])}</span></div>
    <div class="text-[11px] text-slate-500 truncate max-w-[140px]">{_esc(it["name"] or "")}</div>
  </td>
  <td class="py-2 pr-2 align-top">
    <div class="text-[12px] text-slate-800">{_esc(it["chain"])} · {_esc(it["chain_role"] or "")}</div>
    <div class="text-[11px] text-slate-500 truncate max-w-[360px] md:max-w-[520px]">{_esc(chips)}</div>
  </td>
  <td class="py-2 pr-2 align-top text-[12px] whitespace-nowrap">{_strength_badge(it["ai_strength"])}</td>
  <td class="py-2 pr-2 align-top text-[12px] whitespace-nowrap"><span class="{trend_color}">{_esc(trend_label)}</span></td>
  <td class="py-2 pr-2 align-top text-[12px] whitespace-nowrap"><span class="{evidence_color}">{_esc(evidence)}</span></td>
  <td class="py-2 pl-2 align-top text-right whitespace-nowrap">
    <div class="text-sm font-semibold text-slate-800">{it["research_score"]:.0f}</div>
    <div class="text-[10px] text-slate-400">研究优先级</div>
  </td>
</tr>
""")

    n_cand = sl.get("n_candidates", 0)
    return f"""
<div class="bg-white rounded-xl ring-1 ring-slate-200 p-4 mb-4">
  <div class="flex items-center justify-between flex-wrap gap-2 mb-3">
    <div>
      <div class="text-sm font-bold text-slate-900">研究优先级 Top {len(sl["items"])}</div>
      <div class="text-[11px] text-slate-500 mt-0.5">用于决定先研究谁；高分表示多源信号集中，不等于买入。</div>
    </div>
    <span class="text-[11px] text-slate-500">候选池 {n_cand} 只 · 取 top {len(sl["items"])}</span>
  </div>
  <div class="overflow-x-auto">
    <table class="w-full min-w-[760px]">
      <thead>
        <tr class="text-[10px] text-slate-400 uppercase tracking-wide">
          <th class="py-1 pr-2 text-left font-normal">序号</th>
          <th class="py-1 pr-2 text-left font-normal">标的</th>
          <th class="py-1 pr-2 text-left font-normal">为什么现在看</th>
          <th class="py-1 pr-2 text-left font-normal">AI 关联</th>
          <th class="py-1 pr-2 text-left font-normal">链趋势</th>
          <th class="py-1 pr-2 text-left font-normal">公司证据</th>
          <th class="py-1 pl-2 text-right font-normal">优先级</th>
        </tr>
      </thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
  </div>
  <div class="text-[10px] text-slate-500 mt-2 leading-relaxed">
    研究优先级 = 系统打分 30 + ETF 共识 25 + 公司证据 20 + AI 关联强度 15 + 趋势 10。下一步仍需到
    <a href="#buy-research" class="text-violet-700 hover:underline">买前研究</a> 做估值、财务和风险反证。
  </div>
</div>
"""


def _age_days_from_iso(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        return max((now.date() - dt.date()).days, 0)
    except Exception:
        return None


def _render_ai_radar_focus(payload: dict[str, Any],
                           shortlist: dict[str, Any] | None,
                           freshness_panel: dict[str, Any] | None,
                           theme_panel: dict[str, Any] | None) -> str:
    """第一屏摘要：只回答用户今天打开后先看哪三件事。"""
    chains = payload.get("chains") or []
    rising = sorted(
        [c for c in chains if (c.get("delta_7d") or 0) >= MAINLINE_RISE_DELTA],
        key=lambda c: -(c.get("delta_7d") or 0),
    )
    cooling = sorted(
        [c for c in chains if (c.get("delta_7d") or 0) <= MAINLINE_FALL_DELTA],
        key=lambda c: c.get("delta_7d") or 0,
    )
    if rising:
        mainline = "、".join(f'{_esc(c["chain"])} {c["delta_7d"]:+.1f}' for c in rising[:2])
        mainline_sub = "当前系统打分关注度上移"
        mainline_color = "text-emerald-700"
    else:
        mainline = "暂无明显发酵链"
        mainline_sub = "先看冷却和覆盖率，不急着扩池"
        mainline_color = "text-slate-700"

    cooling_text = "、".join(f'{_esc(c["chain"])} {c["delta_7d"]:+.1f}' for c in cooling[:2]) or "无明显冷却"

    top_items = (shortlist or {}).get("items") or []
    top_chips = []
    for it in top_items[:3]:
        top_chips.append(
            f'<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-slate-50 ring-1 ring-slate-200 text-[11px]">'
            f'<span class="font-mono text-slate-800">{_esc(it["symbol"])}</span>'
            f'<span class="text-slate-500">{_esc(it["chain"])}</span>'
            f'<span class="font-semibold text-slate-700">{it["research_score"]:.0f}</span>'
            f'</span>'
        )
    top_html = "".join(top_chips) or '<span class="text-slate-400 text-[12px]">暂无研究优先级</span>'

    data_age = _age_days_from_iso(payload.get("data_generated_at"))
    if data_age is None:
        rec_status = "推荐分时间未知"
        rec_color = "text-slate-500"
    elif data_age == 0:
        rec_status = "推荐分今天生成"
        rec_color = "text-emerald-700"
    elif data_age == 1:
        rec_status = "推荐分 1 天前"
        rec_color = "text-amber-700"
    else:
        rec_status = f"推荐分已滞后 {data_age} 天"
        rec_color = "text-rose-700"

    n_stale = (freshness_panel or {}).get("n_stale", 0)
    phase = (theme_panel or {}).get("phase_status") or {}
    phase_level = phase.get("phase_1_level")
    n_confirmed = phase.get("phase_1_n_confirmed", 0)
    n_theme_done = phase.get("phase_1_themes_with_confirmed", 0)
    n_theme_total = phase.get("phase_1_themes_total", 5)
    if n_stale:
        evidence_status = f"证据系统 {n_stale} 项滞后"
        evidence_color = "text-amber-700"
    elif phase_level == "done":
        evidence_status = f"公司证据完成：{n_theme_done}/{n_theme_total} 主题 confirmed"
        evidence_color = "text-emerald-700"
    elif phase_level == "poc":
        evidence_status = f"公司证据 PoC：{n_confirmed} confirmed，{n_theme_done}/{n_theme_total} 主题覆盖"
        evidence_color = "text-amber-700"
    else:
        evidence_status = "公司证据未启动"
        evidence_color = "text-rose-700"

    # 第 4 卡：数据缺口 — 用户优先级 #4 "哪些数据还缺，缺了会影响什么"
    gap_lines: list[tuple[str, str]] = []  # (color_class, text)
    # 缺口 1: 公司证据 confirmed 主题数
    if n_theme_done < n_theme_total:
        gap_lines.append((
            "text-rose-700",
            f"{n_theme_total - n_theme_done}/{n_theme_total} 主题无 confirmed 公司证据"
        ))
    # 缺口 2: chain 覆盖率审计
    n_uncovered = int((payload.get("coverage_audit") or {}).get("n_uncovered") or 0)
    if n_uncovered:
        gap_lines.append((
            "text-amber-700",
            f"{n_uncovered} 只 AI 高分票缺 chain（运维补规则）"
        ))
    # 缺口 3: 数据源 stale/degraded
    if n_stale:
        gap_lines.append((
            "text-amber-700",
            f"{n_stale} 个子系统 stale / degraded"
        ))
    # 缺口 4: 推荐分时间滞后
    if data_age is not None and data_age > 1:
        gap_lines.append((
            "text-rose-700",
            f"推荐分滞后 {data_age} 天（影响今天能否参考）"
        ))

    if not gap_lines:
        gap_lines.append(("text-emerald-700", "✓ 当前无关键数据缺口"))

    gap_impact = (
        '<div class="text-[10px] text-slate-400 mt-1">'
        '缺 confirmed → research_score 公司证据维度=0；缺 chain → 高分票被研究漏过；缺新鲜度 → 今日推荐不可信'
        '</div>'
    )

    gap_html = "".join(
        f'<div class="text-[12px] font-medium {color} {("mb-0.5" if i < len(gap_lines)-1 else "")}">{_esc(text)}</div>'
        for i, (color, text) in enumerate(gap_lines)
    )

    return f"""
<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3 mb-4">
  <div class="bg-white ring-1 ring-slate-200 rounded-xl p-4">
    <div class="text-[11px] text-slate-500 mb-1">1. 当前主线</div>
    <div class="text-base font-bold {_esc(mainline_color)}">{mainline}</div>
    <div class="text-[11px] text-slate-500 mt-1">{_esc(mainline_sub)}；冷却：{cooling_text}</div>
  </div>
  <div class="bg-white ring-1 ring-slate-200 rounded-xl p-4">
    <div class="text-[11px] text-slate-500 mb-1">2. 先研究谁</div>
    <div class="flex flex-wrap gap-1.5">{top_html}</div>
    <div class="text-[11px] text-slate-500 mt-2">只是研究排序，不是买入清单。</div>
  </div>
  <div class="bg-white ring-1 ring-slate-200 rounded-xl p-4">
    <div class="text-[11px] text-slate-500 mb-1">3. 数据可信度</div>
    <div class="text-sm font-bold {rec_color}">{_esc(rec_status)}</div>
    <div class="text-[11px] font-medium {evidence_color} mt-1">{_esc(evidence_status)}</div>
    <div class="text-[10px] text-slate-400 mt-1">生产验收失败时，以今日决策台/运行状态为准。</div>
  </div>
  <div class="bg-white ring-1 ring-slate-200 rounded-xl p-4">
    <div class="text-[11px] text-slate-500 mb-1">4. 数据缺口</div>
    {gap_html}
    {gap_impact}
  </div>
</div>
"""


def _status_text(panel: dict[str, Any] | None) -> str | None:
    if not panel:
        return None
    status = panel.get("status")
    return str(status).upper() if status else None


def _render_ai_radar_trust_gate(payload: dict[str, Any],
                                freshness_panel: dict[str, Any] | None,
                                theme_panel: dict[str, Any] | None,
                                production_panel: dict[str, Any] | None,
                                quality_panel: dict[str, Any] | None) -> str:
    """顶部结论灯：把生产状态、推荐新鲜度、证据成熟度压成一句可读结论。"""
    data_age = _age_days_from_iso(payload.get("data_generated_at"))
    prod_status = _status_text(production_panel)
    quality_status = _status_text(quality_panel)
    n_picks = int(payload.get("n_picks_total") or 0)
    n_uncovered = int((payload.get("coverage_audit") or {}).get("n_uncovered") or 0)
    n_stale = int((freshness_panel or {}).get("n_stale") or 0)
    phase = (theme_panel or {}).get("phase_status") or {}
    phase_level = phase.get("phase_1_level")
    n_confirmed = int(phase.get("phase_1_n_confirmed") or 0)
    themes_done = int(phase.get("phase_1_themes_with_confirmed") or 0)
    themes_total = int(phase.get("phase_1_themes_total") or 5)

    fail_reasons: list[str] = []
    observe_reasons: list[str] = []

    if n_picks <= 0:
        fail_reasons.append("最新系统 picks 为空")
    if quality_status == "FAIL":
        fail_reasons.append("推荐质量闸门 FAIL")
    if prod_status == "FAIL":
        observe_reasons.append("生产验收 FAIL")
    if data_age is None:
        observe_reasons.append("推荐分时间未知")
    elif data_age > 1:
        observe_reasons.append(f"推荐分滞后 {data_age} 天")
    if n_stale:
        observe_reasons.append(f"证据子系统 {n_stale} 项滞后")
    if phase_level != "done":
        observe_reasons.append(f"公司证据未完成：{n_confirmed} confirmed，{themes_done}/{themes_total} 主题覆盖")
    if n_uncovered:
        observe_reasons.append(f"{n_uncovered} 只高分票缺 chain 标签")

    if fail_reasons:
        label = "不可参考"
        headline = "本页今天不应作为判断依据"
        tone = "bg-rose-50 ring-rose-200 text-rose-900"
        badge = "bg-rose-600 text-white"
        reasons = fail_reasons + observe_reasons
        action = "先修推荐生成和质量闸门，再看 AI 主题雷达。"
    elif observe_reasons:
        label = "仅观察"
        headline = "本页只用于理解 AI 主线，不用于今日交易或调仓"
        tone = "bg-amber-50 ring-amber-200 text-amber-950"
        badge = "bg-amber-500 text-white"
        reasons = observe_reasons
        action = "先看主线变化和研究优先级；下单仍以 AI 推荐、AI 配仓、买前研究和生产验收为准。"
    else:
        label = "可参考"
        headline = "本页可作为今日 AI 主题研究入口"
        tone = "bg-emerald-50 ring-emerald-200 text-emerald-950"
        badge = "bg-emerald-600 text-white"
        reasons = ["生产验收 PASS", "推荐分新鲜", "公司证据覆盖达标"]
        action = "仍需在买前研究里核对估值、财报和风险反证。"

    issue_rows = "".join(
        f'<span class="inline-flex items-center px-2 py-0.5 rounded bg-white/70 ring-1 ring-black/5 text-[11px]">{_esc(r)}</span>'
        for r in reasons[:5]
    )

    prod_txt = prod_status or "未知"
    quality_txt = quality_status or "未知"
    data_txt = "未知" if data_age is None else ("今天" if data_age == 0 else f"{data_age} 天前")

    return f"""
<div class="rounded-xl ring-1 {tone} p-4 mb-4">
  <div class="flex items-start justify-between gap-3 flex-wrap">
    <div class="flex-1 min-w-[260px]">
      <div class="flex items-center gap-2 flex-wrap mb-1">
        <span class="inline-flex items-center px-2 py-0.5 rounded text-[12px] font-bold {badge}">{_esc(label)}</span>
        <span class="text-base font-bold">{_esc(headline)}</span>
      </div>
      <div class="text-[12px] leading-relaxed">{_esc(action)}</div>
      <div class="flex flex-wrap gap-1.5 mt-2">{issue_rows}</div>
    </div>
    <div class="grid grid-cols-3 gap-2 text-center text-[11px] min-w-[260px]">
      <div class="bg-white/70 rounded-lg ring-1 ring-black/5 px-2 py-1.5">
        <div class="text-slate-500">生产验收</div>
        <div class="font-bold">{_esc(prod_txt)}</div>
      </div>
      <div class="bg-white/70 rounded-lg ring-1 ring-black/5 px-2 py-1.5">
        <div class="text-slate-500">质量闸门</div>
        <div class="font-bold">{_esc(quality_txt)}</div>
      </div>
      <div class="bg-white/70 rounded-lg ring-1 ring-black/5 px-2 py-1.5">
        <div class="text-slate-500">推荐分</div>
        <div class="font-bold">{_esc(data_txt)}</div>
      </div>
    </div>
  </div>
</div>
"""


def _render_ai_radar_reader_guide(payload: dict[str, Any],
                                  freshness_panel: dict[str, Any] | None,
                                  theme_panel: dict[str, Any] | None,
                                  production_panel: dict[str, Any] | None,
                                  quality_panel: dict[str, Any] | None) -> str:
    """把技术状态翻译成用户能直接执行的读法和补数动作。"""
    data_age = _age_days_from_iso(payload.get("data_generated_at"))
    prod_status = _status_text(production_panel)
    quality_status = _status_text(quality_panel)
    n_uncovered = int((payload.get("coverage_audit") or {}).get("n_uncovered") or 0)
    n_stale = int((freshness_panel or {}).get("n_stale") or 0)
    phase = (theme_panel or {}).get("phase_status") or {}
    phase_level = phase.get("phase_1_level")
    n_confirmed = int(phase.get("phase_1_n_confirmed") or 0)
    themes_done = int(phase.get("phase_1_themes_with_confirmed") or 0)
    themes_total = int(phase.get("phase_1_themes_total") or 5)

    read_rows = [
        ("先看结论灯", "黄灯表示只能观察主线，不能拿来交易；红灯表示先别用。"),
        ("再看当前主线", "它告诉你资金和系统打分最近偏向哪条 AI 价值链。"),
        ("最后看研究优先级", "它只是研究顺序，不是买入清单；下单前还要去买前研究。"),
    ]
    read_html = "".join(
        f"""
<div class="py-2 border-b border-slate-100 last:border-0">
  <div class="text-[12px] font-semibold text-slate-900">{_esc(title)}</div>
  <div class="text-[11px] text-slate-600 mt-0.5 leading-relaxed">{_esc(body)}</div>
</div>
"""
        for title, body in read_rows
    )

    fix_rows: list[tuple[str, str, str]] = []
    if prod_status == "FAIL":
        fix_rows.append((
            "生产验收失败",
            "今天的推荐链路还没完全跑顺，本页只能当行业观察。",
            "先修运行状态里的失败步骤，再跑 production_acceptance_check.py。"
        ))
    if quality_status == "FAIL":
        fix_rows.append((
            "推荐质量闸门失败",
            "系统推荐本身不合格，研究优先级也会失真。",
            "先重跑推荐质量闸门，确认 recommendation_quality_gate.json 变 PASS。"
        ))
    if data_age is None or data_age > 1:
        age_text = "时间未知" if data_age is None else f"已滞后 {data_age} 天"
        fix_rows.append((
            f"推荐分{age_text}",
            "主线升温/冷却可能不是今天的状态。",
            "重跑行情抓取、V2 推荐生成、推荐质量闸门，再重建 dashboard。"
        ))
    if n_stale:
        fix_rows.append((
            "证据子系统滞后",
            "ETF、公司证据或宏观指标可能不是最新。",
            "重跑 AI 主题雷达证据刷新，必要时带 --refresh-etf 和 --scan-sec。"
        ))
    if phase_level != "done":
        fix_rows.append((
            "公司证据不足",
            f"现在只有 {n_confirmed} 个 confirmed，{themes_done}/{themes_total} 个主题有 confirmed 公司证据。",
            "给水冷、稀土、铀、SMR、AI 数据补公司级证据，写入 ai_theme_company_evidence 后聚合 tags。"
        ))
    if n_uncovered:
        fix_rows.append((
            "高分票缺价值链标签",
            f"{n_uncovered} 只高分票还不知道属于哪条 AI 价值链。",
            "补 chain_metadata 规则或人工 override，然后重跑 classify_chain_v2.py。"
        ))

    if not fix_rows:
        fix_rows.append((
            "暂无关键缺口",
            "数据链路和证据成熟度都达标。",
            "继续按日常刷新；新增公司时再补 chain 和公司证据。"
        ))

    fix_html = "".join(
        f"""
<tr class="border-t border-slate-100">
  <td class="py-2 pr-3 align-top text-[12px] font-semibold text-slate-900 whitespace-nowrap">{_esc(title)}</td>
  <td class="py-2 pr-3 align-top text-[12px] text-slate-600 leading-relaxed">{_esc(impact)}</td>
  <td class="py-2 align-top text-[12px] text-slate-700 leading-relaxed">{_esc(action)}</td>
</tr>
"""
        for title, impact, action in fix_rows
    )

    return f"""
<div class="bg-white ring-1 ring-slate-200 rounded-xl p-4 mb-4">
  <div class="flex items-start justify-between gap-3 flex-wrap mb-3">
    <div>
      <div class="text-sm font-bold text-slate-900">这页到底怎么看</div>
      <div class="text-[11px] text-slate-500 mt-0.5">先判断能不能参考，再看主线，最后才看股票。</div>
    </div>
    <div class="text-[11px] text-slate-400">缺数据会在下方直接写出补法</div>
  </div>
  <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
    <div class="lg:col-span-1 bg-slate-50 rounded-lg px-3 py-2">
      {read_html}
    </div>
    <div class="lg:col-span-2 overflow-x-auto">
      <table class="w-full min-w-[760px]">
        <thead>
          <tr class="text-[10px] text-slate-400 uppercase tracking-wide">
            <th class="py-1 pr-3 text-left font-normal">缺口</th>
            <th class="py-1 pr-3 text-left font-normal">影响</th>
            <th class="py-1 text-left font-normal">怎么补</th>
          </tr>
        </thead>
        <tbody>{fix_html}</tbody>
      </table>
    </div>
  </div>
</div>
"""


def _render_freshness_panel(panel: dict[str, Any]) -> str:
    """渲染数据新鲜度小卡。stale 红，aging 琥珀，fresh 绿。"""
    if not panel or not panel.get("items"):
        return ""

    chips = []
    for it in panel["items"]:
        status = it["status"]
        color = {
            "fresh":   "bg-emerald-50 text-emerald-700 ring-emerald-200",
            "aging":   "bg-amber-50  text-amber-700  ring-amber-200",
            "stale":   "bg-rose-50   text-rose-700   ring-rose-200",
            "missing": "bg-slate-50  text-slate-500  ring-slate-200",
        }.get(status, "bg-slate-50 text-slate-500 ring-slate-200")
        icon = {"fresh": "✅", "aging": "🟡", "stale": "🟠", "missing": "❌"}.get(status, "·")

        if it["age_days"] is None:
            age_txt = "未跑"
        elif it["age_days"] == 0:
            age_txt = "今天"
        elif it["age_days"] == 1:
            age_txt = "1 天前"
        else:
            age_txt = f"{it['age_days']} 天前"

        last_at = (it.get("last_at") or "")[:19].replace("T", " ")
        title = f"上次刷新: {last_at} · stale 阈值 {it['stale_days']} 天"
        chips.append(
            f'<span class="inline-flex items-center px-2 py-0.5 rounded ring-1 text-[11px] {color}" title="{_esc(title)}">'
            f'{icon} {_esc(it["label"])} · {_esc(age_txt)}</span>'
        )

    summary = ""
    if panel["n_stale"]:
        summary = (
            f'<span class="text-[11px] text-rose-700">'
            f'⚠️ {panel["n_stale"]} 个子系统 stale / missing — 建议跑 '
            f'<code class="text-[10px] bg-rose-100 px-1 rounded">python -m stock_research.jobs.ai_theme_evidence_refresh</code>'
            f'</span>'
        )
    else:
        summary = '<span class="text-[11px] text-emerald-700">✓ 全部子系统新鲜</span>'

    return f"""
<div class="bg-white ring-1 ring-slate-200 rounded-xl p-4 mb-3">
  <div class="flex items-center justify-between flex-wrap gap-2 mb-2">
    <div>
      <div class="text-sm font-bold text-slate-900">⏱ 数据新鲜度</div>
      <div class="text-[11px] text-slate-500">证据系统 5 个子系统的最近刷新时间 — 鼠标悬停看具体时间戳。</div>
    </div>
    {summary}
  </div>
  <div class="flex flex-wrap gap-1.5">{"".join(chips)}</div>
</div>
"""


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
            # 该主题已映射到 chain — 具体公司列表在 chain 卡看
            # 这里 collapsible 展开能看到该主题的宏观指标 + 数据源详情（无具体 ticker，避免视觉混淆）
            n_metrics = t.get("metrics_count", 0)
            metric_hint = f"宏观指标 {n_metrics} 条" if n_metrics else "宏观指标暂未录入"

            # 展开内容：metric_lines + 数据源清单
            metric_lines = []
            for m in (t.get("latest_metrics") or [])[:6]:
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
            metric_html = "".join(metric_lines) or '<div class="text-[11px] text-slate-400">宏观指标暂未录入</div>'

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

            mini_rows.append(f"""
<details class="border-b border-slate-100 last:border-0">
  <summary class="cursor-pointer list-none flex items-center justify-between py-1.5 text-[12px]">
    <div class="flex items-center gap-2">
      <span class="font-semibold text-slate-800">{_esc(t["display_name"])}</span>
      <span class="text-[11px] text-slate-500">数据源 {ok_badge} · {metric_hint}</span>
    </div>
    <span class="text-[11px] text-violet-700">具体公司见下方链卡 ↓ · 点开看宏观指标 ▾</span>
  </summary>
  <div class="pl-3 py-2 mt-1 border-l-2 border-violet-200">
    <div class="text-[10px] text-slate-500 mb-1">受益逻辑：{_esc(t["why"])}</div>
    <div class="text-[10px] text-slate-500 mb-1">最新宏观指标：</div>
    <div class="mb-2">{metric_html}</div>
    <div class="text-[10px] text-slate-500 mb-1">数据源清单：</div>
    <ul class="space-y-0.5 pl-1">{"".join(src_items)}</ul>
  </div>
</details>
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
        <span class="text-[11px] text-amber-700">⚠️ 暂无公司清单</span>
      </div>
      <div class="text-[12px] text-slate-600 mt-0.5">{_esc(t["why"])}</div>
      <div class="text-[11px] text-amber-700 mt-1">意思是：主题资料已经有了，但系统还没把它接成可展示的股票列表。现在只能看数据源和宏观指标；要显示公司，需要补公司证据和价值链标签。</div>
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
    # Phase 1 三档：未启动 ⏳ / PoC 🟡 / 完成 ✅
    p1_level = phase.get("phase_1_level") or ("done" if phase.get("phase_1_evidence_scanned") else "not_started")
    p1_n_evi = phase.get("phase_1_n_evidence", 0)
    p1_n_conf = phase.get("phase_1_n_confirmed", 0)
    p1_themes_done = phase.get("phase_1_themes_with_confirmed", 0)
    p1_themes_total = phase.get("phase_1_themes_total", 5)
    if p1_level == "done":
        p1 = "✅"
        p1_detail = f"{p1_themes_done}/{p1_themes_total} 主题有 confirmed"
    elif p1_level == "poc":
        p1 = "🟡 PoC"
        p1_detail = f"{p1_n_evi} 条证据 · {p1_n_conf} confirmed · {p1_themes_done}/{p1_themes_total} 主题已有 confirmed"
    else:
        p1 = "⏳"
        p1_detail = "未启动"
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
    <div class="text-[11px] text-slate-500 text-right">
      <div>Phase 0 数据源 {p0} · Phase 1 公司证据 {p1} · Phase 2 看板集成 {p2}</div>
      <div class="text-[10px] text-slate-400 mt-0.5">Phase 1 状态: {_esc(p1_detail)}</div>
    </div>
  </div>
  {"".join(mini_rows)}
</div>
"""

    standalone_html = ""
    if standalone_cards:
        standalone_html = f"""
<div class="mb-3">
  <div class="text-sm font-bold text-slate-900 mb-2">🔭 前瞻主题 · 暂无公司清单</div>
  <div class="text-[11px] text-slate-500 mb-2">这些主题已有数据源和宏观指标，但还没有接成可展示的股票列表。补完公司证据和价值链标签后，才会在 AI 价值链明细里出现公司。</div>
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
                '⚠️ 暂无公司清单 — 需要补公司证据或新增价值链类目'
                '</div>'
            )
        elif n_assoc == 0:
            picks_assoc_html = (
                f'<div class="text-[11px] text-slate-500 mt-2">'
                f'关联 chain：{_esc("、".join(chains_mapped))} · 该 chain 在最新 picks 内 0 只命中'
                f'</div>'
            )
        elif has_direct:
            # direct 映射：chain 内容跟主题语义重合，可展示具体票
            pick_chips = []
            for p in t["picks_assoc"]:
                pick_chips.append(
                    f'<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-slate-50 ring-1 ring-slate-200 text-[11px]">'
                    f'{_market_label(p["market"])} <span class="font-mono">{_esc(p["symbol"])}</span> '
                    f'<span class="text-slate-500">{_esc((p["name"] or "")[:10])}</span> '
                    f'<span class="font-semibold text-slate-700">{p["score"]:.0f}</span>'
                    f'</span>'
                )
            picks_assoc_html = (
                f'<div class="mt-2 pt-2 border-t border-slate-100">'
                f'<div class="text-[11px] text-slate-500 mb-1">'
                f'通过 chain「{_esc("、".join(chains_mapped))}」(direct) 关联的 picks（{n_assoc} 只，前 {len(t["picks_assoc"])}）：'
                f'</div>'
                f'<div class="flex flex-wrap gap-1.5">{"".join(pick_chips)}</div>'
                f'</div>'
            )
        else:
            # partial 映射：chain 是粗分类（如"数据中心电力"包电力+液冷），不能直接展示具体票
            # 否则会把 VST/GEV/NRG（电力供给）误标为"水冷"主题。
            # 等公司级证据 confirmed/candidate 之后才在主题下显示具体公司。
            picks_assoc_html = (
                f'<div class="mt-2 pt-2 border-t border-slate-100">'
                f'<div class="text-[11px] text-amber-700 mb-1">'
                f'⚠️ <strong>粗映射</strong>：暂只能 partial 关联到 chain「{_esc("、".join(chains_mapped))}」 · '
                f'该 chain 内有 {n_assoc} 只系统 picks，但 chain 是粗分类，'
                f'每只票主营是否真属于此主题需要公司级证据（SEC filings 等）确认。'
                f'</div>'
                f'<div class="text-[10px] text-slate-500">'
                f'本主题下不展示具体公司 — 待 ai_theme_company_evidence 表写入 confirmed 公司证据后才呈现。'
                f'你可以到下方「{_esc("、".join(chains_mapped))}」链卡看 chain 全量。'
                f'</div>'
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
                '⚠️ 暂无公司清单'
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
    # Phase 1 三档：未启动 ⏳ / PoC 🟡 / 完成 ✅
    p1_level = phase.get("phase_1_level") or ("done" if phase.get("phase_1_evidence_scanned") else "not_started")
    p1_n_evi = phase.get("phase_1_n_evidence", 0)
    p1_n_conf = phase.get("phase_1_n_confirmed", 0)
    p1_themes_done = phase.get("phase_1_themes_with_confirmed", 0)
    p1_themes_total = phase.get("phase_1_themes_total", 5)
    if p1_level == "done":
        p1 = "✅"
        p1_detail = f"{p1_themes_done}/{p1_themes_total} 主题有 confirmed"
    elif p1_level == "poc":
        p1 = "🟡 PoC"
        p1_detail = f"{p1_n_evi} 条证据 · {p1_n_conf} confirmed · {p1_themes_done}/{p1_themes_total} 主题已有 confirmed"
    else:
        p1 = "⏳"
        p1_detail = "未启动"
    p2 = "✅" if phase.get("phase_2_dashboard_integrated") else "⏳"

    return f"""
<div class="bg-white ring-1 ring-slate-200 rounded-xl p-4 mb-4">
  <div class="flex items-center justify-between flex-wrap gap-2 mb-2">
    <div>
      <div class="text-sm font-bold text-slate-900">📑 5 主题数据基建状态</div>
      <div class="text-[11px] text-slate-500">行业理解层的"证据底座"：每条主题先有公开数据源，再谈公司映射。文档 §七。</div>
    </div>
    <div class="text-[11px] text-slate-500 text-right">
      <div>Phase 0 数据源 {p0} · Phase 1 公司证据 {p1} · Phase 2 看板集成 {p2}</div>
      <div class="text-[10px] text-slate-400 mt-0.5">Phase 1 状态: {_esc(p1_detail)}</div>
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
                            etf_panel: dict[str, Any] | None = None,
                            freshness_panel: dict[str, Any] | None = None,
                            shortlist: dict[str, Any] | None = None,
                            production_panel: dict[str, Any] | None = None,
                            quality_panel: dict[str, Any] | None = None) -> str:
    """渲染 AI 主题雷达 section（独立 tab 内容）。

    硬规则（对应文档 §五）：
      - 不写 watchlist / 真实持仓
      - 不出现"买入 / 建仓 / 配置 / 推荐 X"等指示性文案
      - 覆盖率不足显式呈现，不隐藏未分类股票
    """
    n_total = payload["n_picks_total"]
    n_chain = payload["n_picks_with_chain"]
    n_any_chain = payload.get("n_picks_with_any_chain") or n_chain
    n_filtered_non_ai = max(n_any_chain - n_chain, 0)
    coverage_pct = round(n_chain / n_total * 100, 1) if n_total else 0
    data_at = (payload.get("data_generated_at") or "")[:19].replace("T", " ")

    headline = my_view_headline or ""
    summary = my_view_summary or ""

    head_html = f"""
<div class="bg-slate-50 rounded-xl ring-1 ring-slate-200 p-4 mb-4">
  <div class="text-[11px] text-slate-500 mb-1">作者主线假设</div>
  <div class="text-base font-bold text-slate-900 mb-1">{_esc(headline)}</div>
  <div class="text-[12px] text-slate-600 leading-relaxed">{_esc(summary)}</div>
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
    freshness_html = _render_freshness_panel(freshness_panel) if freshness_panel else ""
    shortlist_html = _render_research_shortlist(shortlist) if shortlist else ""
    focus_html = _render_ai_radar_focus(payload, shortlist, freshness_panel, theme_panel)
    trust_gate_html = _render_ai_radar_trust_gate(
        payload, freshness_panel, theme_panel, production_panel, quality_panel
    )
    reader_guide_html = _render_ai_radar_reader_guide(
        payload, freshness_panel, theme_panel, production_panel, quality_panel
    )

    rise_chains = [c for c in payload["chains"] if (c.get("delta_7d") or 0) >= MAINLINE_RISE_DELTA]
    fall_chains = [c for c in payload["chains"] if (c.get("delta_7d") or 0) <= MAINLINE_FALL_DELTA]
    rise_html = "、".join(f'<span class="font-semibold text-emerald-700">{_esc(c["chain"])}</span> (+{c["delta_7d"]})' for c in rise_chains) or '<span class="text-slate-400">无</span>'
    fall_html = "、".join(f'<span class="font-semibold text-rose-700">{_esc(c["chain"])}</span> ({c["delta_7d"]})' for c in fall_chains) or '<span class="text-slate-400">无</span>'

    trend_html = f"""
<div class="bg-white ring-1 ring-slate-200 rounded-xl p-4 mb-4">
  <div class="text-xs text-slate-500 mb-1">📊 数据驱动的 AI 相关链趋势 · 系统打分近 7 天变化</div>
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
    <div class="text-[11px] text-slate-500">AI 相关链</div>
    <div class="text-lg font-semibold text-slate-800">{n_chain} <span class="text-xs text-slate-500">({coverage_pct}%)</span></div>
    <div class="text-[10px] text-slate-400 mt-0.5">已过滤非 AI 链 {n_filtered_non_ai} 只</div>
  </div>
  <div class="bg-white ring-1 ring-slate-200 rounded-lg p-3">
    <div class="text-[11px] text-slate-500">AI 链层数</div>
    <div class="text-lg font-semibold text-slate-800">{len(payload["chains"])}</div>
    <div class="text-[10px] text-slate-400 mt-0.5">已命中 picks 的 AI 链</div>
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

        # 该 chain 对应哪些前瞻主题
        # 设计选择：chain 卡只挂"对应主题"小徽章，不嵌入主题宏观证据。
        # 嵌入证据会造成视觉上"chain 卡 = 主题"的错觉（让 VST 看起来像水冷股）。
        # 主题宏观证据统一在顶部 5 主题数据基建 panel 里看。
        mapped_themes = c.get("mapped_themes") or []
        theme_badges = []
        has_partial = False
        for mt in mapped_themes:
            rel = mt.get("relevance") or "direct"
            if rel != "direct":
                has_partial = True
            rel_marker = "" if rel == "direct" else " <span class=\"text-[10px] text-amber-600\">(粗映射)</span>"
            theme_badges.append(
                f'<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] '
                f'bg-violet-50 text-violet-800 ring-1 ring-violet-200">📑 {_esc(mt["display"])}</span>{rel_marker}'
            )

        themes_header_html = ""
        if theme_badges:
            disclaimer = ""
            if has_partial:
                disclaimer = (
                    '<div class="text-[10px] text-amber-700 mt-0.5">'
                    '⚠️ 粗映射：chain 范围大于上述主题（如本 chain 包电力+液冷+电网），'
                    'chain 内具体票主营是否属于该主题需自行核实。主题宏观证据请见顶部「5 前瞻主题数据基建」。'
                    '</div>'
                )
            themes_header_html = (
                f'<div class="mt-1">'
                f'<div class="flex items-center gap-1.5 flex-wrap">'
                f'<span class="text-[10px] text-slate-500">对应前瞻主题：</span>'
                f'{" ".join(theme_badges)}</div>'
                f'{disclaimer}</div>'
            )
        theme_evidence_html = ""  # 移除嵌入；主题证据统一在主题 panel

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

    # ─── 系统池其它高分板块：AI 关联="无" 的链（创新药/新能源车/军工等）
    # 不在主视图但仍 surface，避免用户以为 AI 雷达漏看市场，同时明确"这不属于 AI 主线"
    other_chains = payload.get("filtered_non_ai_chains") or []
    if other_chains:
        other_rows = []
        for c in other_chains:
            pick_chips = []
            for p in c.get("top_picks") or []:
                pick_chips.append(
                    f'<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-white ring-1 ring-slate-200 text-[11px]">'
                    f'{_market_label(p["market"])} <span class="font-mono">{_esc(p["symbol"])}</span> '
                    f'<span class="text-slate-500">{_esc((p["name"] or "")[:10])}</span> '
                    f'<span class="font-semibold text-slate-700">{p["score"]:.0f}</span>'
                    f'</span>'
                )
            other_rows.append(f"""
<details class="py-2 border-b border-slate-100 last:border-0">
  <summary class="cursor-pointer list-none flex items-center justify-between gap-2 text-[12px]">
    <span class="font-semibold text-slate-700">{_esc(c["chain"])}</span>
    <span class="text-[11px] text-slate-500">{c["n_stocks"]} 只 · 均分 {c["avg_score"]:.1f}</span>
  </summary>
  <div class="mt-2 flex flex-wrap gap-1.5">{"".join(pick_chips)}</div>
</details>
""")
        other_chains_html = f"""
<details class="bg-slate-50 ring-1 ring-slate-200 rounded-xl p-4 mb-3">
  <summary class="cursor-pointer list-none flex items-center justify-between gap-2">
    <div>
      <div class="text-sm font-bold text-slate-700">📊 系统池其它高分板块（非 AI 主线）</div>
      <div class="text-[11px] text-slate-500 mt-0.5">这些链系统打分也高，但 AI 关联强度为"无"，不在 AI 主题雷达主视图。点开看具体票。</div>
    </div>
    <span class="text-[11px] text-slate-500">{len(other_chains)} 条链</span>
  </summary>
  <div class="mt-3 pt-3 border-t border-slate-200">{"".join(other_rows)}</div>
</details>
"""
    else:
        other_chains_html = ""

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
  创新药、新能源车、军工等 AI 关联强度为"无"的链条已从本页主视图过滤，仍保留在 AI 推荐和产业链地图中。
  实际下单仍由「AI 推荐 / AI 配仓 / 买前研究」共同决定，本页不写自选股、不写真实持仓。
</div>
"""

    evidence_details_html = f"""
<details class="group mb-3">
  <summary class="cursor-pointer select-none list-none flex items-center justify-between gap-3 px-4 py-3 rounded-xl bg-white ring-1 ring-slate-200 hover:bg-slate-50 transition">
    <div class="flex items-center gap-3 min-w-0">
      <span class="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-100 text-slate-500 text-base leading-none transition-transform group-open:rotate-90">›</span>
      <div class="min-w-0">
        <div class="text-sm font-bold text-slate-900">数据健康与证据底座</div>
        <div class="text-[11px] text-slate-500 mt-0.5">数据源、主题证据、覆盖率审计</div>
      </div>
    </div>
    <span class="text-[11px] text-slate-400 whitespace-nowrap">点击展开</span>
  </summary>
  <div class="mt-3">
    {freshness_html}
    {theme_panel_html}
    {audit_html}
  </div>
</details>
"""

    etf_details_html = f"""
<details class="group mb-3">
  <summary class="cursor-pointer select-none list-none flex items-center justify-between gap-3 px-4 py-3 rounded-xl bg-white ring-1 ring-slate-200 hover:bg-slate-50 transition">
    <div class="flex items-center gap-3 min-w-0">
      <span class="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-100 text-slate-500 text-base leading-none transition-transform group-open:rotate-90">›</span>
      <div class="min-w-0">
        <div class="text-sm font-bold text-slate-900">ETF 共识</div>
        <div class="text-[11px] text-slate-500 mt-0.5">主题 ETF 持仓与系统 universe 命中</div>
      </div>
    </div>
    <span class="text-[11px] text-slate-400 whitespace-nowrap">点击展开</span>
  </summary>
  <div class="mt-3">{etf_panel_html}</div>
</details>
""" if etf_panel_html else ""

    chain_details_html = f"""
<details class="group mb-4">
  <summary class="cursor-pointer select-none list-none flex items-center justify-between gap-3 px-4 py-3 rounded-xl bg-white ring-1 ring-slate-200 hover:bg-slate-50 transition">
    <div class="flex items-center gap-3 min-w-0">
      <span class="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-100 text-slate-500 text-base leading-none transition-transform group-open:rotate-90">›</span>
      <div class="min-w-0">
        <div class="text-sm font-bold text-slate-900">AI 价值链明细</div>
        <div class="text-[11px] text-slate-500 mt-0.5">每条链、近 7 天趋势、链内股票</div>
      </div>
    </div>
    <span class="text-[11px] text-slate-400 whitespace-nowrap">点击展开</span>
  </summary>
  <div class="mt-3">
    {trend_html}
    {kpi_html}
    {chains_html}
    {other_chains_html}
  </div>
</details>
"""

    return f"""
<section id="ai-radar" class="max-w-7xl mx-auto px-6 py-10" style="display:none">
  <div class="mb-4">
    <h1 class="text-2xl font-bold text-slate-900 flex items-center gap-2">📡 AI 主题雷达</h1>
    <p class="text-sm text-slate-600 mt-1">AI 价值链全景 · 行业理解层 · 不构成买入建议</p>
  </div>
  {trust_gate_html}
  {reader_guide_html}
  {focus_html}
  {head_html}
  {shortlist_html}
  {evidence_details_html}
  {etf_details_html}
  {chain_details_html}
  {footer_html}
</section>
"""
