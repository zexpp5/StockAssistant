"""瓶颈信号注册表 + 季度复查记录 · 单一来源。

7 个 AI 叙事领先信号（bottleneck 组 GEV/VRT/MU + capex 组 MSFT/GOOGL/AMZN/META）
的注册表（GROUPS）原先住在 jobs/bottleneck_earnings_reminder.py，2026-06-12 迁到
这里成为单一来源；提醒 job / 财报体检 job / API / dashboard 全部从本模块取。

复查记录是闭环里缺的那一环：
  财报日提醒卡(bottleneck_earnings_reminder) → 次日 AI 体检卡(earnings_signal_analyzer)
  → 用户在 dashboard「催化信号验证」页回填结论(本模块存) → 红绿灯/趋势/聚合判定。

记录存 data/bottleneck_signal_reviews.json，按 (ticker, quarter) 去重 upsert。
聚合判定规则与提醒卡文案一致（提醒卡是给人看的版本，这里是给机器执行的版本）：
  bottleneck 组：任一转弱=停止加仓提示；三个同季转弱=缺货叙事退潮。
  capex 组：一家下调=记一笔先不动作；两家以上同季下调=capex 消化期开始。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[2]
REVIEWS_FILE = _REPO / "data" / "bottleneck_signal_reviews.json"

CONCLUSIONS = ("转强", "持平", "转弱")
# 证据档位（借鉴 chokepoint-atlas evidence ladder）：
#   A=财报原文/公司一手披露  B=管理层措辞/电话会转述  C=媒体或研报转述
EVIDENCE_TIERS = ("A", "B", "C")
_QUARTER_RE = re.compile(r"^20\d{2}Q[1-4]$")

# 结论 → 红绿灯（前端只渲染,不重算）
CONCLUSION_LIGHT = {"转强": "🟢", "持平": "⚪", "转弱": "🔴"}

# 两组领先信号 → 各自的"财报里看什么"复查清单（白话，新手能照着看）
GROUPS: dict[str, dict] = {
    "bottleneck": {
        "title": "🔬 瓶颈信号复查提醒",
        "headline": "AI 瓶颈龙头财报窗口到了",
        "intro": ("财报发布后花十分钟，对着下面的清单核对一遍——"
                  "这是「AI 基建还缺不缺货」最早的体温计。"),
        "meaning": ("**信号亮了怎么办**：任一指标转弱 ≠ 清仓，含义是**停止给瓶颈类个股加仓**，"
                    "等下一季财报确认方向。三个信号同时转弱才说明整条「缺货叙事」在退潮。"),
        "tickers": {
            "GEV": {
                "name": "GE Vernova（燃机/电力设备）",
                "signal": "燃机订单还抢手吗",
                "checks": [
                    "燃机槽位/新订单：预订增速比上季度回落了吗？",
                    "有没有客户「转售槽位 / 折价」的字眼？出现 = 抢产能的人开始撤了",
                    "订单积压（backlog）还在创新高吗？",
                ],
            },
            "VRT": {
                "name": "Vertiv（数据中心电力/散热）",
                "signal": "book-to-bill 还 ≥1.2 吗",
                "checks": [
                    "book-to-bill（新签订单 ÷ 当期出货）：≥1.2 = 订单仍供不应求",
                    "跌破 1.2 = 出货追上了订单，是数据中心建设热度见顶的领先信号",
                    "管理层对明年订单管线（pipeline）的措辞有没有变保守？",
                ],
            },
            "MU": {
                "name": "美光（HBM 存储）",
                "signal": "HBM 还在涨价、还售罄吗",
                "checks": [
                    "HBM 合约价：环比还在涨吗？环比转负 = 存储瓶颈退潮",
                    "HBM 产能是否仍「提前售罄」（sold out）？措辞从售罄变「供需平衡」要警惕",
                    "注意它是周期股：利润最好的时候往往就是周期顶",
                ],
            },
        },
    },
    "capex": {
        "title": "☁️ 云大厂 capex 指引复查",
        "headline": "AI 供应链「总阀门」财报窗口到了",
        "intro": ("整条 AI 供应链（英伟达/台积电/电力链/光模块）的收入，本质上就是这四家的资本开支。"
                  "财报后只盯一个问题：**capex 指引是上调、维持，还是下调？**"),
        "meaning": ("**信号怎么读**：一家下调 = 记一笔，先不动作；**两家以上同季下调 = "
                    "「capex 消化期」开始的强信号**——停止 AI 基建/算力类个股加仓，底仓定投照旧。"
                    "这是本轮牛熊机制里最重要的领先指标，比股价早一到两个季度。"),
        "tickers": {
            "MSFT": {
                "name": "微软（Azure）",
                "signal": "capex 指引方向 + 产能措辞",
                "checks": [
                    "下季度/全财年 capex 指引：上调、维持还是下调？",
                    "「产能受限/供不应求」（capacity constrained）的措辞还在吗？消失 = 需求降温早期信号",
                    "Azure 增速有没有掉档？",
                ],
            },
            "GOOGL": {
                "name": "谷歌（GCP/TPU）",
                "signal": "全年 capex 数字变没变",
                "checks": [
                    "全年 capex 指引金额：比上次说的数字高了还是低了？",
                    "Cloud 增速与利润率方向",
                    "管理层对「算力供不应求」的表述是否退坡？",
                ],
            },
            "AMZN": {
                "name": "亚马逊（AWS）",
                "signal": "capex（大头给 AWS）还在加吗",
                "checks": [
                    "capex 同比增速方向（绝大部分投给 AWS/AI）",
                    "AWS 增速有没有掉档？",
                    "管理层对 AI 需求的措辞：仍然「需求远超供给」吗？",
                ],
            },
            "META": {
                "name": "Meta（纯自用烧钱方）",
                "signal": "capex 指引区间上调还是下调",
                "checks": [
                    "全年 capex 指引区间：上调还是下调？",
                    "⚠️ 解读相反：META 下调对**它自己**股价常是利好，但对**整条 AI 供应链**是需求转弱的坏信号",
                    "对「AI 投入回报」的措辞有没有从进攻转防守？",
                ],
            },
        },
    },
}

TICKER_GROUP = {t: g for g, spec in GROUPS.items() for t in spec["tickers"]}
ALL_TICKERS = tuple(TICKER_GROUP)

# 最新一条复查超过这个天数视为过期（≈一个财报季 + 缓冲），聚合判定不再采信
STALE_AFTER_DAYS = 150


# ─────────────── 读写 ───────────────

def quarter_of(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def load_reviews() -> list[dict]:
    if not REVIEWS_FILE.exists():
        return []
    try:
        doc = json.loads(REVIEWS_FILE.read_text(encoding="utf-8"))
        return list(doc.get("reviews") or [])
    except Exception as exc:
        logger.warning("读复查记录失败：%s", exc)
        return []


def save_review(ticker: str, quarter: str, conclusion: str,
                evidence_tier: str = "", url: str = "", note: str = "") -> dict:
    """upsert 一条复查记录（按 ticker+quarter 去重）。返回写入的记录。

    校验失败抛 ValueError（API 层转 400）。
    """
    ticker = str(ticker).upper().strip()
    quarter = str(quarter).upper().strip()
    evidence_tier = str(evidence_tier).upper().strip()
    if ticker not in TICKER_GROUP:
        raise ValueError(f"未知信号股 {ticker}，必须是 {'/'.join(ALL_TICKERS)}")
    if not _QUARTER_RE.match(quarter):
        raise ValueError(f"季度格式应为 2026Q2，收到 {quarter!r}")
    if conclusion not in CONCLUSIONS:
        raise ValueError(f"结论必须是 {'/'.join(CONCLUSIONS)}，收到 {conclusion!r}")
    if evidence_tier and evidence_tier not in EVIDENCE_TIERS:
        raise ValueError(f"证据档位必须是 A/B/C 或留空，收到 {evidence_tier!r}")

    record = {
        "ticker": ticker,
        "group": TICKER_GROUP[ticker],
        "quarter": quarter,
        "conclusion": conclusion,
        "evidence_tier": evidence_tier,
        "url": str(url or "").strip(),
        "note": str(note or "").strip(),
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
    }
    reviews = [r for r in load_reviews()
               if not (r.get("ticker") == ticker and r.get("quarter") == quarter)]
    reviews.append(record)
    reviews.sort(key=lambda r: (r.get("quarter", ""), r.get("ticker", "")))
    REVIEWS_FILE.parent.mkdir(parents=True, exist_ok=True)
    REVIEWS_FILE.write_text(
        json.dumps({"reviews": reviews}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return record


def latest_review(ticker: str, reviews: list[dict] | None = None) -> dict | None:
    """该票最新一季的复查记录（按 quarter 字符串排序即时间序）。"""
    pool = [r for r in (reviews if reviews is not None else load_reviews())
            if r.get("ticker") == str(ticker).upper()]
    return max(pool, key=lambda r: r.get("quarter", "")) if pool else None


def _is_stale(record: dict, as_of: date) -> bool:
    try:
        reviewed = date.fromisoformat(str(record.get("reviewed_at", ""))[:10])
    except Exception:
        return True
    return (as_of - reviewed).days > STALE_AFTER_DAYS


# ─────────────── 聚合判定 ───────────────

def aggregate_group(group_key: str, reviews: list[dict] | None = None,
                    as_of: date | None = None) -> dict:
    """一组信号的聚合判定。level: ok / caution / alert / pending。"""
    as_of = as_of or date.today()
    reviews = reviews if reviews is not None else load_reviews()
    tickers = list(GROUPS[group_key]["tickers"])
    latest = {t: latest_review(t, reviews) for t in tickers}
    fresh = {t: r for t, r in latest.items() if r and not _is_stale(r, as_of)}
    weak = [t for t, r in fresh.items() if r["conclusion"] == "转弱"]
    n, n_reviewed, n_weak = len(tickers), len(fresh), len(weak)

    if n_reviewed == 0:
        level, text = "pending", "未复查：等财报窗口到了回填即可"
    elif group_key == "bottleneck":
        if n_weak >= 3:
            level, text = "alert", "退潮中：三个瓶颈信号同季转弱，缺货叙事在退潮"
        elif n_weak >= 1:
            level, text = ("caution",
                           f"部分转弱（{'、'.join(weak)}）：停止给瓶颈类个股加仓，等下一季确认")
        else:
            level, text = "ok", "健在：已复查信号未见转弱，缺货叙事仍成立"
    else:  # capex
        if n_weak >= 2:
            level, text = ("alert",
                           f"消化期信号（{'、'.join(weak)}）：两家以上下调，停止 AI 基建/算力类加仓")
        elif n_weak == 1:
            level, text = "caution", f"记一笔（{weak[0]} 下调）：先不动作，盯下一家财报"
        else:
            level, text = "ok", "需求阀门开着：已复查家数未见下调"

    return {"level": level, "text": text, "n_total": n,
            "n_reviewed": n_reviewed, "n_weak": n_weak, "weak_tickers": weak}


def build_payload(as_of: date | None = None) -> dict:
    """dashboard / API 的完整数据包——前端只渲染，不重算规则。"""
    as_of = as_of or date.today()
    reviews = load_reviews()
    groups = []
    for key, spec in GROUPS.items():
        signals = []
        for ticker, meta in spec["tickers"].items():
            history = sorted((r for r in reviews if r.get("ticker") == ticker),
                             key=lambda r: r.get("quarter", ""), reverse=True)[:4]
            latest = history[0] if history else None
            signals.append({
                "ticker": ticker,
                "name": meta["name"],
                "signal": meta["signal"],
                "checks": meta["checks"],
                "latest": latest,
                "stale": bool(latest and _is_stale(latest, as_of)),
                "history": history,
            })
        groups.append({
            "key": key,
            "title": spec["title"],
            "meaning": spec["meaning"],
            "verdict": aggregate_group(key, reviews, as_of),
            "signals": signals,
        })
    return {
        "available": True,
        "as_of": as_of.isoformat(),
        "current_quarter": quarter_of(as_of),
        "conclusions": list(CONCLUSIONS),
        "evidence_tiers": list(EVIDENCE_TIERS),
        "groups": groups,
        "n_reviews": len(reviews),
    }
