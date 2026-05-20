"""A 股每日优选 — 6 因子闭环  ✅ PRODUCTION（A 股）

**这是当前 A 股选股的主流水线。** daily_refresh.sh 第 21 步调本文件，
仅在收盘后（≥16:00 工作日 或 周末）执行，避免北向 T+1 / 龙虎榜盘后才出导致的脏数据。

美股不走这个 — 美股看 daily_picks_v5.py（学术因子 + 分析师上修）。
v1 的 daily_picks.py 是 LEGACY，仅保留作对照基线。

—— 以下原 docstring ——

把 6 个新模块串成闭环。

闭环数据流（输入 → 信号合成 → 过滤 → 排序）：

  1. 输入：飞书 watchlist 中的 A 股
  2. 信号合成（横截面 0-1 评分）：
     a. Piotroski F-Score (factor_model_china)         权重 0.25
     b. 12-1 月动量      (factor_model_china)          权重 0.15
     c. 1 月反转         (factor_model_china)          权重 0.10
     d. 龙虎榜机构净买入  (lhb_signals)                  权重 0.15
     e. 北向资金信号      (north_flow_signals)           权重 0.15
     f. PEAD 真实公告日   (event_calendar.pead_factor)   权重 0.10
     g. 政策主题受益      (policy_events.themes_tailwind) 权重 0.10
  3. 风险加权：event_calendar.risk_score (×0.0~1.0)
  4. 硬过滤：a_share_filters.filter_tradable (剔除 ST/涨停/停牌)
  5. 横截面排序 → top K → 写入 飞书 + JSON

输出：
  data/a_share_picks.json   完整结果（用于看板/streamlit）
  飞书"每日优选"表（与 v5 美股版并行写入，市场字段区分）

为什么不改 daily_picks_v5.py：
  v5 写死了 yfinance + is_us_ticker，硬塞 A 股会污染原有逻辑。
  这里做并行 job，daily_refresh.sh 同时调两个，互不干扰。
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))  # 2026-05-11 lib 迁移

from stock_research import config
from stock_research.core.a_share_filters import (
    fetch_spot_snapshot, filter_tradable, _strip_code,
)
from stock_research.core.lhb_signals import compute_lhb_factors
from stock_research.core.north_flow_signals import (
    compute_north_flow_signal, double_confirm_signal,
)
from stock_research.core.event_calendar import (
    build_calendar, pead_factor, EventCalendar,
)
from stock_research.core.policy_events import themes_under_policy_tailwind

# 复用现有 A 股因子模型（Piotroski + 动量 + 反转）
from factor_model_china import fetch_factors_a_share, momentum_a_share

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ─────────── 因子权重（合计 1.00）───────────
#
# ⚠️ 当前权重是启发式值，未经 A 股市场内 IC 历史验证。生产写库前必须有
# data/calibrated_factor_weights.json，且文件需声明 market=a_share 与 validated=true。
# 无有效校准文件时本 job 会强制 dry-run；只有显式 --bypass-ic-gate 才允许写库。
#
# 任何对启发式权重的临时调整必须经过 walk-forward backtest 验证 Sharpe > Equal-Weight + 0.3，
# 否则不应进 production（防止 overfitting watchlist 历史）。

DEFAULT_FACTOR_WEIGHTS = {
    "f_score":       0.25,   # Piotroski 财务质量（基本面）
    "momentum":      0.15,   # 12-1 月动量（趋势）
    "reversal":      0.10,   # 1 月反转（短期均值回归）
    "lhb":           0.15,   # 龙虎榜机构净买入（A 股短线 alpha）
    "north_flow":    0.15,   # 北向资金信号（外资偏好）
    "pead":          0.10,   # PEAD（业绩公告漂移）
    "policy_theme":  0.10,   # 政策主题受益
}
assert abs(sum(DEFAULT_FACTOR_WEIGHTS.values()) - 1.0) < 1e-9
CALIBRATED_WEIGHTS_PATH = REPO / "data" / "calibrated_factor_weights.json"
A_SHARE_FACTOR_CACHE = REPO / "data" / "latest" / "a_share_factor_cache.json"
A_SHARE_PRICE_HISTORY_CACHE = REPO / "data" / "latest" / "a_share_price_history_cache.json"


def _load_calibrated_weights() -> tuple[dict[str, float] | None, str]:
    """读取并校验 A 股 IC 权重文件。

    返回 (weights, status)。weights 为 None 时 status 说明不可生产的原因。
    """
    calib = CALIBRATED_WEIGHTS_PATH
    if not calib.exists():
        return None, "missing"
    try:
        data = json.loads(calib.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("加载 calibrated_factor_weights.json 失败: %s", e)
        return None, "unreadable"
    if not isinstance(data, dict):
        return None, "invalid_payload"

    market = str(data.get("market") or data.get("universe") or "").strip().lower()
    if market and market not in {"a_share", "ashare", "cn", "china", "v6_cn", "a股"}:
        return None, f"wrong_market:{market}"
    validated = data.get("validated") is True or str(data.get("validation_status") or "").lower() in {
        "pass", "passed", "valid", "validated",
    }
    if not validated:
        return None, "not_validated"

    raw = data.get("weights")
    if not isinstance(raw, dict):
        return None, "missing_weights"
    unknown = sorted(k for k in raw if k not in DEFAULT_FACTOR_WEIGHTS)
    if unknown:
        return None, f"unknown_factors:{','.join(unknown)}"
    weights: dict[str, float] = {}
    try:
        for k, v in raw.items():
            fv = float(v)
            if fv < 0:
                return None, f"negative_weight:{k}"
            if fv > 0:
                weights[k] = fv
    except Exception:
        return None, "non_numeric_weight"
    total = sum(weights.values())
    if total <= 0:
        return None, "zero_weights"
    if abs(total - 1.0) > 1e-4:
        return None, f"weights_sum:{total:.6f}"
    return weights, f"ic_calibrated@{calib.name}"


def load_weights() -> tuple[dict[str, float], str]:
    """读生产校准权重；无有效文件时只返回启发式权重供 dry-run / bypass 使用。"""
    weights, source = _load_calibrated_weights()
    if weights is not None:
        return weights, source
    logger.warning("A 股校准权重不可用(%s)，回退启发式权重；生产写库会被上层闸门阻断", source)
    return DEFAULT_FACTOR_WEIGHTS, "heuristic_default"


# 兼容旧调用（外部 import FACTOR_WEIGHTS）
FACTOR_WEIGHTS = DEFAULT_FACTOR_WEIGHTS


@dataclass
class APickEntry:
    """单只 A 股优选结果。"""
    code: str
    name: str
    market: str = "A 股"
    industry: str = ""                       # 用于 sector cap

    # 子因子分（0-1）
    f_score_norm: float | None = None        # F-Score / 9
    momentum_norm: float | None = None       # 横截面分位
    reversal_norm: float | None = None       # 横截面分位
    lhb_score: float = 0.5
    north_score: float = 0.5
    pead_score: float = 0.5
    policy_boost: float = 0.0                # 政策受益主题加成（0-0.3）

    # 风险加权
    event_risk_score: float = 1.0            # 0-1，越低越避开

    # 综合
    composite: float = 0.0
    coverage_score: float = 0.0
    missing_factors: str = ""
    factor_weights_used: str = ""
    rank: int = 0
    recommended: bool = False

    # 拦截原因（可买性）
    tradable: bool = True
    block_reasons: list[str] = None

    # 软红旗（Altman Z'' / Beneish M，2026-05-12 P0-3b 接入）
    altman_z: float | None = None
    beneish_m: float | None = None
    risk_flags: list[str] = None

    # 备注
    notes: list[str] = None

    def to_dict(self):
        d = asdict(self)
        if d.get("notes") is None:
            d["notes"] = []
        if d.get("block_reasons") is None:
            d["block_reasons"] = []
        if d.get("risk_flags") is None:
            d["risk_flags"] = []
        return d


# ─────────── watchlist 读取（兼容 daily_picks.py 的接口）───────────

def fetch_a_share_watchlist() -> list[dict]:
    """从 V2 manual_watchlist 拉 A 股自选股。

    2026-05-20 V2 cutover：fetch_all_watchlist (V1) → fetch_manual_watchlist_enriched(market='CN')。
    自选股完全由用户在 dashboard 手动维护，空是合法状态（V2 spec）。
    """
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "scripts" / "lib"))
    from stock_db import fetch_manual_watchlist_enriched
    return fetch_manual_watchlist_enriched(market="CN")


def _a_share_market(raw_ticker: str) -> str:
    if raw_ticker.startswith(("00", "20", "30")):
        return "A股·深交所"
    if raw_ticker.startswith(("8", "9", "43")):
        return "A股·北交所"
    return "A股·上交所"


def _universe_item_to_record(item: dict) -> dict:
    raw = str(item.get("raw_ticker") or item.get("ticker") or "").split(".")[0]
    return {
        "code": raw,
        "name": item.get("name") or raw,
        "market": _a_share_market(raw),
        "industry": item.get("sector") or item.get("industry") or "",
        "theme": "A-share AI/tech",
        "ai_logic": f"production universe: {item.get('source') or 'a_share_universe'}",
        "source": item.get("source") or "a_share_universe",
    }


def _static_a_share_universe_records(limit: int | None = None) -> list[dict]:
    return _dynamic_a_share_universe_records(limit=limit)


def _dynamic_a_share_universe_records(limit: int | None = None) -> list[dict]:
    from stock_research.core.a_share_universe import fetch_a_share_tech_universe
    items = fetch_a_share_tech_universe()
    if limit and limit > 0:
        items = items[:limit]
    return [_universe_item_to_record(item) for item in items]


def fetch_a_share_candidate_records(
    *,
    universe: str = "auto",
    limit: int | None = None,
) -> tuple[list[dict], str]:
    """A 股生产输入池。

    watchlist 是用户自选，不再作为生产推荐的唯一输入。默认 auto：
    仅使用动态 A 股科技池；若动态池为空则保持为空。用户自选 A 股只作为
    额外覆盖/补充，不为空也不会排除生产 universe。
    """
    manual = fetch_a_share_watchlist()
    if universe == "watchlist":
        return manual, "watchlist"

    if universe == "static":
        records = _static_a_share_universe_records(limit=limit)
        source = "dynamic_universe"
    else:
        source = "dynamic_universe"
        try:
            records = _dynamic_a_share_universe_records(limit=limit)
        except Exception as e:
            if universe == "dynamic":
                raise
            logger.warning("A 股动态 universe 失败，保持空池: %s", e)
            records = []

    by_code = {r["code"]: r for r in records}
    for r in manual:
        # 用户自选覆盖 universe 的名称/行业/备注；不在 universe 中则追加。
        by_code[r["code"]] = {**by_code.get(r["code"], {}), **r}
    return list(by_code.values()), source + ("+watchlist_overlay" if manual else "")


def _load_a_share_factor_cache() -> dict:
    try:
        data = json.loads(A_SHARE_FACTOR_CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"items": {}}
    except Exception:
        return {"items": {}}


def _save_a_share_factor_cache(cache: dict) -> None:
    A_SHARE_FACTOR_CACHE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "items": cache.get("items", {}),
    }
    tmp = A_SHARE_FACTOR_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(A_SHARE_FACTOR_CACHE)


def _load_a_share_price_cache() -> dict:
    try:
        data = json.loads(A_SHARE_PRICE_HISTORY_CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"items": {}}
    except Exception:
        return {"items": {}}


def _price_factors_from_cache(code: str, as_of_today: str, price_cache: dict) -> tuple[float | None, float | None]:
    item = (price_cache.get("items") or {}).get(_strip_code(code))
    rows = item.get("rows") if isinstance(item, dict) else None
    if not isinstance(rows, list):
        return None, None
    usable = sorted(
        (r for r in rows if str(r.get("date", "")) <= as_of_today and r.get("close") is not None),
        key=lambda r: str(r.get("date", "")),
    )
    if len(usable) < 253:
        return None, None
    try:
        closes = [float(r["close"]) for r in usable]
        t_now = closes[-1]
        t_minus_21 = closes[-22]
        t_minus_252 = closes[-253]
        if min(t_now, t_minus_21, t_minus_252) <= 0:
            return None, None
        mom = (t_minus_21 / t_minus_252 - 1.0) * 100.0
        rev = -((t_now / t_minus_21 - 1.0) * 100.0)
        return round(mom, 2), round(rev, 2)
    except Exception:
        return None, None


def _cached_a_share_bundle(cache_items: dict, code: str, as_of_today: str,
                           active_factors: set[str]) -> dict | None:
    key = _strip_code(code)
    item = cache_items.get(key)
    if isinstance(item, dict) and item.get("date") == as_of_today and isinstance(item.get("bundle"), dict):
        bundle = item["bundle"]
        fetched = set(bundle.get("_fetched_factors") or DEFAULT_FACTOR_WEIGHTS)
        if active_factors.issubset(fetched):
            return bundle
    return None


def _fetch_a_share_factor_bundle(code: str, as_of_today: str,
                                 cache_items: dict,
                                 price_cache: dict,
                                 active_factors: set[str]) -> tuple[dict, dict | None, bool]:
    """Network-heavy A-share factor bundle with same-day cache.

    The tradability filter, LHB batch, event calendar, and policy scan stay
    outside the cache because they are already batch-level or cheap local reads.
    """
    norm = _strip_code(code)
    cached = _cached_a_share_bundle(cache_items, norm, as_of_today, active_factors)
    if cached is not None:
        return cached, None, True

    f_score = mom = rev = None
    if "f_score" in active_factors:
        try:
            f_data = fetch_factors_a_share(norm, as_of=as_of_today)
            f_score = (f_data.get("piotroski") or {}).get("f_score")
            mom = (f_data.get("momentum") or {}).get("momentum_12_1")
            rev = (f_data.get("momentum") or {}).get("reversal_1m")
        except Exception as e:
            logger.debug("factor_model_china failed for %s: %s", norm, e)
    elif active_factors & {"momentum", "reversal"}:
        mom, rev = _price_factors_from_cache(norm, as_of_today, price_cache)
        if mom is None or rev is None:
            try:
                m_data = momentum_a_share(norm, as_of=as_of_today)
                mom = m_data.get("momentum_12_1")
                rev = m_data.get("reversal_1m")
            except Exception as e:
                logger.debug("momentum_a_share failed for %s: %s", norm, e)

    if "north_flow" in active_factors:
        try:
            n_sig = compute_north_flow_signal(norm, lookback_days=20)
            north_score = n_sig.score
        except Exception as e:
            north_score = 0.5
            logger.debug("north flow failed for %s: %s", norm, e)
    else:
        north_score = 0.5

    run_deep_risk = os.environ.get("A_SHARE_DEEP_RISK", "0").strip().lower() in {"1", "true", "yes", "on"}
    if run_deep_risk or "f_score" in active_factors:
        try:
            from stock_research.core.a_share_fundamental_deep import (
                altman_z_double_prime_a, beneish_m_score_a, build_a_share_risk_flags,
            )
            from stock_research.core.a_share_industry import get_industry as _get_ind
            altman = altman_z_double_prime_a(norm)
            beneish = beneish_m_score_a(norm)
            ind_info = _get_ind(norm) or {}
            z_inapp = ind_info.get("z_prime_inapplicable", False)
            risk_flags = build_a_share_risk_flags(altman, beneish, z_prime_inapplicable=z_inapp)
            altman_z_val = altman.get("z_score") if not altman.get("error") else None
            beneish_m_val = beneish.get("m_score_adjusted") if not beneish.get("error") else None
        except Exception as fe:
            logger.debug("a_share_fundamental_deep failed for %s: %s", norm, fe)
            altman_z_val, beneish_m_val, risk_flags = None, None, []
    else:
        altman_z_val, beneish_m_val, risk_flags = None, None, []

    bundle = {
        "f_score": f_score,
        "mom": mom,
        "rev": rev,
        "north_score": north_score,
        "altman_z": altman_z_val,
        "beneish_m": beneish_m_val,
        "risk_flags": risk_flags,
        "_fetched_factors": sorted(active_factors),
    }
    return bundle, {"date": as_of_today, "bundle": bundle}, False


def _build_a_share_entry(
    r: dict,
    *,
    as_of_today: str,
    cache_items: dict,
    price_cache: dict,
    snapshot,
    lhb_factors: dict,
    cal: EventCalendar,
    tailwind: dict,
    theme_field: str,
    active_factors: set[str],
) -> tuple[APickEntry, tuple, dict | None, str]:
    code = r["code"]
    norm = _strip_code(code)
    name = r.get("name", code)
    bundle, cache_update, cache_hit = _fetch_a_share_factor_bundle(
        norm, as_of_today, cache_items, price_cache, active_factors,
    )

    f_score = bundle.get("f_score")
    mom = bundle.get("mom")
    rev = bundle.get("rev")

    if "pead" in active_factors:
        try:
            pead = pead_factor(norm, cal)
        except Exception as e:
            pead = {"score": 0.5, "in_event_window": False}
            logger.debug("pead failed for %s: %s", norm, e)
    else:
        pead = {"score": 0.5, "in_event_window": False}

    theme_text = (r.get(theme_field, "") or r.get("industry", "")
                  or r.get("ai_logic", "") or "")
    policy_boost = 0.0
    if "policy_theme" in active_factors:
        for theme, count in tailwind.items():
            if theme in theme_text:
                policy_boost = max(policy_boost, min(0.30, count * 0.05))

    event_risk = cal.risk_score(norm) if cal.events else 1.0

    if snapshot is not None:
        tradable_codes, blocked = filter_tradable(
            [norm], snapshot,
            allow_st=False,
            allow_limit_up=False,
            allow_suspended=False,
        )
        tradable = norm in tradable_codes
        block_reasons = blocked.get(norm, [])
    else:
        tradable, block_reasons = True, []

    lhb = lhb_factors.get(norm) or lhb_factors.get(code)
    lhb_score = lhb.score if ("lhb" in active_factors and lhb) else 0.5

    entry = APickEntry(
        code=norm, name=name,
        market=r.get("market", "A 股") or "A 股",
        industry=(r.get("industry") or r.get(theme_field) or "")[:32],
        f_score_norm=(f_score / 9.0) if isinstance(f_score, (int, float)) else None,
        momentum_norm=mom,
        reversal_norm=rev,
        lhb_score=lhb_score,
        north_score=bundle.get("north_score", 0.5),
        pead_score=pead.get("score", 0.5),
        policy_boost=policy_boost,
        event_risk_score=event_risk,
        tradable=tradable,
        block_reasons=block_reasons,
        altman_z=bundle.get("altman_z"),
        beneish_m=bundle.get("beneish_m"),
        risk_flags=bundle.get("risk_flags") or [],
        notes=[],
    )
    f_str = f"F={f_score}" if f_score is not None else "F=?"
    m_str = f"M={mom:+.0f}%" if isinstance(mom, (int, float)) else "M=?"
    flag = "✅" if tradable else "❌"
    status = (
        f"{f_str:<6}{m_str:<9} LHB={lhb_score:.2f} "
        f"N={entry.north_score:.2f} PEAD={pead.get('score', 0.5):.2f} {flag}"
        + (" cache" if cache_hit else "")
    )
    return entry, (mom, rev), cache_update, status


# ─────────── 主流程 ───────────

def run_a_share_picks(top_k: int = 12, mode: str = "tertile",
                      dry_run: bool = False, theme_field: str = "industry",
                      sector_cap_count: int = 3,
                      require_after_close: bool = False,
                      bypass_ic_gate: bool = False,
                      bypass_audit_gate: bool = False,
                      workers: int = 3,
                      universe: str = "auto",
                      universe_limit: int | None = 80):
    """主入口。

    参数：
      top_k                最多写入的优选数
      mode                 'tertile'（前 1/3）/ 'median'（前 1/2）/ 'quartile'（前 1/4）
      dry_run              仅打印不写飞书
      theme_field          watchlist 里用哪个字段做主题匹配（默认 industry）
      sector_cap_count     单 industry 在 top_k 内最多入选的股票数（默认 3，组合分散）
      require_after_close  仅在 A 股收盘后允许执行，盘前/盘中直接退出（避免脏数据）
                           触发条件：北向 T+1、龙虎榜盘后才出，未收盘信号都不可信
    """
    # 盘前/盘中守卫：北向 T+1 + LHB 盘后 + spot volume=0，跑出来全是脏数据
    if require_after_close and not _is_after_a_share_close():
        now = datetime.now()
        print(f"⛔ a_share_picks: 当前 {now:%Y-%m-%d %H:%M} 非 A 股收盘后时段（要求 ≥16:00），")
        print(f"   --require-after-close 设置下退出。北向 T+1、龙虎榜盘后发布、spot 盘前 volume=0。")
        print(f"   收盘后单跑：python -m stock_research.jobs.a_share_picks --dry-run")
        return 0

    # A 股不能复用美股 IC gate；生产权重必须来自市场内有效校准文件。
    calibrated_weights, calibration_status = _load_calibrated_weights()
    if calibrated_weights is None:
        if bypass_ic_gate:
            print(
                "\n⚠️ --bypass-ic-gate：A 股无有效市场内 IC 校准权重 "
                f"({calibration_status})，继续使用启发式权重（风险自担）\n"
            )
        else:
            print("\n🔴 A 股缺少有效市场内 IC 校准权重 → 强制 dry-run（不写 DB）")
            print("   要求：data/calibrated_factor_weights.json 含 market=a_share, validated=true, weights 合计=1")
            print("   临时研究用途可显式 --bypass-ic-gate，但不会作为默认生产路径\n")
            dry_run = True

    # 跨源 audit CONFLICT 闸门 — 同 daily_picks_v5
    from stock_research.core.audit_gate import evaluate_gate as evaluate_audit_gate
    from stock_research.core.audit_gate import format_report as format_audit_report
    audit_gate = evaluate_audit_gate()
    print(format_audit_report(audit_gate))
    if not audit_gate.passed:
        if bypass_audit_gate:
            print("\n⚠️ --bypass-audit-gate：用户强制跳过闸门，继续（风险自担）\n")
        else:
            print("\n🔴 跨源 audit 闸门 FAIL → 强制 dry-run（不写飞书 / 不写 DB）")
            print("   修复：python3 -m stock_research.jobs.daily_audit  或 --bypass-audit-gate\n")
            dry_run = True

    print(f"\n📊 A 股每日优选 — {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 70)

    # 1. 拉 A 股生产候选池（不依赖用户自选股）
    print("\n[1/7] 拉 A 股生产候选池...")
    records, records_source = fetch_a_share_candidate_records(
        universe=universe,
        limit=universe_limit,
    )
    print(f"  {len(records)} 只 A 股标的（source={records_source}）")
    if not records:
        print("  (无 A 股候选，退出)")
        return 0

    weights, weights_source = load_weights()
    active_factors = {k for k, v in weights.items() if float(v) > 0}
    print(f"  权重源={weights_source} · active_factors={','.join(sorted(active_factors)) or 'none'}")

    # 2. 抓全市场 spot 快照（一次性，用于过滤）
    print("\n[2/7] 抓 A 股全市场 spot 快照...")
    snapshot = fetch_spot_snapshot()
    if snapshot is None:
        print("  🔴 快照获取失败 — 无法过滤 ST/停牌/涨停，强制 dry-run（不写 DB）")
        dry_run = True
    else:
        print(f"  抓取 {snapshot.raw_count} 只全 A 股，分类完成")

    # 3. 抓 LHB / 北向 / 事件日历 (一次性)
    print("\n[3/7] 抓龙虎榜 (近 5 日) ...")
    codes = [r["code"] for r in records]
    if "lhb" in active_factors:
        lhb_factors = compute_lhb_factors(codes, lookback_days=5)
        print(f"  覆盖 {len(lhb_factors)} 只")
    else:
        lhb_factors = {}
        print("  跳过（当前校准权重未启用 lhb）")

    print("\n[4/7] 构建事件日历（解禁/减持/财报）...")
    cal: EventCalendar = build_calendar(
        horizon_unlock_days=90,
        horizon_insider_days=60,
        include_earnings=("pead" in active_factors),
    )
    print(f"  {len(cal.events)} 条事件")

    print("\n[5/7] 扫描政策受益主题（最近 14 天）...")
    if "policy_theme" in active_factors:
        try:
            tailwind = themes_under_policy_tailwind(days=14, min_count=2)
        except Exception as e:
            logger.warning("policy scan failed: %s", e)
            tailwind = {}
    else:
        tailwind = {}
    if tailwind:
        print(f"  受益主题：{', '.join(f'{t}({c})' for t, c in sorted(tailwind.items(), key=lambda x: -x[1])[:5])}")
    else:
        msg = "未启用 policy_theme" if "policy_theme" not in active_factors else "无明显主题受益"
        print(f"  {msg}")

    # 4. 逐股算因子 + 信号 (北向是 per-stock API，无法批量；其他都已批量)
    print(f"\n[6/7] 逐股计算因子 ({len(records)} 只)...")
    # PIT (C-5)：as_of=今日，让 factor_model_china 过滤"今天还没披露的"年报
    as_of_today = datetime.now().strftime("%Y-%m-%d")
    entries: list[APickEntry] = []
    raw_metrics = []  # 用于横截面归一化
    factor_cache = _load_a_share_factor_cache()
    cache_items = factor_cache.setdefault("items", {})
    price_cache = _load_a_share_price_cache()
    cache_dirty = False
    worker_count = max(1, min(workers, len(records)))
    print(f"  并发 workers={worker_count} · 当天 cache={A_SHARE_FACTOR_CACHE}")
    with ThreadPoolExecutor(max_workers=worker_count) as ex:
        futures = {
            ex.submit(
                _build_a_share_entry,
                r,
                as_of_today=as_of_today,
                cache_items=cache_items,
                price_cache=price_cache,
                snapshot=snapshot,
                lhb_factors=lhb_factors,
                cal=cal,
                tailwind=tailwind,
                theme_field=theme_field,
                active_factors=active_factors,
            ): (idx, r)
            for idx, r in enumerate(records, 1)
        }
        for fut in as_completed(futures):
            idx, r = futures[fut]
            code = r["code"]
            name = r.get("name", code)
            try:
                entry, raw_pair, cache_update, status = fut.result()
            except Exception as e:
                entry = APickEntry(code=_strip_code(code), name=name, tradable=False,
                                   block_reasons=["因子计算失败"], notes=[str(e)])
                raw_pair = (None, None)
                cache_update = None
                status = f"失败: {e}"
            entries.append(entry)
            raw_metrics.append(raw_pair)
            if cache_update is not None:
                cache_items[entry.code] = cache_update
                cache_dirty = True
            print(f"  [{idx:>2}/{len(records)}] {entry.code} {name[:10]:<10} {status}")
    if cache_dirty:
        _save_a_share_factor_cache(factor_cache)

    # 5. 横截面归一化 momentum / reversal — winsorize + rank
    # 原 min-max 对极值零防御（一只异常股拉爆 max，其他股全挤在 0 端）。
    # 改用 [1%, 99%] winsorize 截断尾部 → 横截面 percent rank → uniform [0,1]，机构标配。
    moms = [m for m, _ in raw_metrics]   # 含 None
    revs = [r for _, r in raw_metrics]
    mom_ranks = _winsorize_rank(moms)
    rev_ranks = _winsorize_rank(revs)

    # 6. 合成综合分
    print(f"\n[7/7] 合成综合分（{len(entries)} 只，权重源={weights_source}）...")
    for idx, e in enumerate(entries):
        e.momentum_norm = mom_ranks[idx]
        e.reversal_norm = rev_ranks[idx]

        factor_values = {
            "f_score": e.f_score_norm,
            "momentum": e.momentum_norm,
            "reversal": e.reversal_norm,
            "lhb": e.lhb_score,
            "north_flow": e.north_score,
            "pead": e.pead_score,
            "policy_theme": 0.5 + e.policy_boost,
        }
        active = {k: float(v) for k, v in weights.items() if float(v) > 0}
        total_w = sum(active.values()) or 1.0
        covered_w = sum(w for k, w in active.items() if factor_values.get(k) is not None)
        e.coverage_score = round(covered_w / total_w, 4)
        e.missing_factors = ",".join(k for k in active if factor_values.get(k) is None)
        e.factor_weights_used = json.dumps(active, sort_keys=True)
        if covered_w <= 0:
            composite = -0.25
        else:
            raw = sum(active[k] * float(factor_values[k])
                      for k in active if factor_values.get(k) is not None) / covered_w
            penalty = max(0.0, 0.50 - e.coverage_score) / 0.50 * 0.25
            composite = raw * e.coverage_score - penalty
        # 风险加权（解禁/减持等）
        composite *= e.event_risk_score
        e.composite = round(composite, 4)

    # 7. 排序 + 决策门槛 + 硬过滤
    entries.sort(key=lambda e: -e.composite)
    composites = [e.composite for e in entries if e.tradable]
    if not composites:
        print("  ⚠️ 无可买标的（全部被过滤）")
        return 0
    cutoff_map = {
        "quartile": _quantile(composites, 0.75),
        "tertile":  _quantile(composites, 2/3),
        "median":   _quantile(composites, 0.50),
    }
    cutoff = cutoff_map[mode]
    for i, e in enumerate(entries, 1):
        e.rank = i
        e.recommended = e.tradable and e.composite >= cutoff

    # 8. Sector cap：贪心选 top_k 时单 industry 不超过 sector_cap_count
    selected, sector_skipped = _select_with_sector_cap(
        entries, top_k=top_k, sector_cap_count=sector_cap_count,
    )

    print(f"\n  cutoff = {cutoff:.3f} (mode={mode})")
    print(f"  推荐 {len(selected)} / 可买 {sum(1 for e in entries if e.tradable)}"
          f" / 总 {len(entries)}")
    if sector_skipped:
        print(f"  sector cap 跳过 {len(sector_skipped)} 只（单 industry ≤ {sector_cap_count}）：")
        for e in sector_skipped[:5]:
            print(f"    · {e.code} {e.name[:8]:<8} [{e.industry}] composite={e.composite}")

    print(f"\n  {'排':<3}{'代码':<8}{'名称':<10}{'F分':>5}{'LHB':>6}{'北':>6}{'PEAD':>6}"
          f"{'政策':>6}{'风险':>6}{'综合':>7}  状态")
    print(f"  {'-'*78}")
    for e in entries[:30]:
        f_str = f"{e.f_score_norm*9:.0f}" if e.f_score_norm is not None else "?"
        flag = "✅" if e.recommended else ("❌" if not e.tradable else "  ")
        block = "/".join(e.block_reasons[:2]) if e.block_reasons else ""
        print(f"  {e.rank:<3}{e.code:<8}{e.name[:8]:<10}{f_str:>5}{e.lhb_score:>6.2f}"
              f"{e.north_score:>6.2f}{e.pead_score:>6.2f}{e.policy_boost:>6.2f}"
              f"{e.event_risk_score:>6.2f}{e.composite:>7.3f}  {flag} {block}")

    # 9. 写文件
    out = REPO / "data" / "a_share_picks.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(),
        "mode": mode,
        "cutoff": cutoff,
        "top_k": top_k,
        "sector_cap_count": sector_cap_count,
        "universe": universe,
        "universe_source": records_source,
        "a_share_production_enabled": config.A_SHARE_PRODUCTION_ENABLED,
        "factor_weights": weights,
        "factor_weights_source": weights_source,
        "calibration_status": calibration_status,
        "production_write_enabled": not dry_run,
        "n_total": len(entries),
        "n_tradable": sum(1 for e in entries if e.tradable),
        "n_recommended": len(selected),
        "n_sector_capped": len(sector_skipped),
        "selected": [e.to_dict() for e in selected],
        "all_entries": [e.to_dict() for e in entries],
        "policy_tailwind": tailwind,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n✅ JSON: {out}")

    if dry_run:
        print("  (dry-run 模式，跳过 DuckDB picks 写入)")
        return 0

    # 2026-05-21 V1 cutover：picks 表已删；只算 entry_price 不写库
    try:
        # A 股当前价：优先 snapshot.by_code（akshare 快照），缺失则 fallback 到 yfinance
        # 盘前 / 节假日 snapshot 可能拿不到价格 — yfinance 兜底
        price_map = {}
        missing = []
        if selected and snapshot is not None and hasattr(snapshot, "by_code"):
            try:
                for e in selected:
                    status = snapshot.by_code.get(e.code)
                    if status and status.price is not None:
                        try:
                            v = float(status.price)
                            price_map[e.code] = v if v == v else None
                        except Exception:
                            price_map[e.code] = None
                    else:
                        missing.append(e.code)
            except Exception as pe:
                logger.warning(f"从 snapshot.by_code 取 A 股价格失败: {pe}")
                missing = [e.code for e in selected]
        else:
            missing = [e.code for e in selected]
        # yfinance fallback（盘前 / snapshot 失败时兜底）
        if missing:
            try:
                import yfinance as yf
                for code in missing:
                    yf_code = code + ('.SS' if code.startswith(('60', '68')) else '.SZ' if code.startswith(('00', '30', '20')) else '.BJ')
                    try:
                        h = yf.Ticker(yf_code).history(period="2d")
                        if not h.empty:
                            price_map[code] = float(h["Close"].iloc[-1])
                    except Exception:
                        pass
                logger.info(f"yfinance fallback 补齐 {len([c for c in missing if price_map.get(c) is not None])}/{len(missing)} 只")
            except Exception as ye:
                logger.warning(f"yfinance fallback 失败: {ye}")
        db_rows = []
        for e in selected:
            if e.composite >= 0.70:
                grade_label = "⭐⭐⭐ 强烈推荐（综合 ≥0.70）"
            elif e.composite >= 0.55:
                grade_label = "⭐⭐ 推荐（综合 ≥0.55）"
            else:
                grade_label = "⭐ 关注"
            if e.coverage_score < 0.50:
                grade_label = f"⭐ 观察（数据覆盖 {e.coverage_score:.0%} < 50%，不进 buy）"
            risk_flags = getattr(e, "risk_flags", None) or []
            if risk_flags:
                grade_label = grade_label + " · " + "｜".join(risk_flags)
            f_score_val = int((e.f_score_norm or 0) * 9) if e.f_score_norm is not None else 0
            db_rows.append({
                "code": e.code,
                "name": e.name,
                "market": e.market or "A 股",
                "rating": grade_label,
                "total_score": round(e.composite * 100, 2),
                "ai_score": f_score_val * 10,
                "val_score": f_score_val * 3,
                "trend_score": 0,
                "cred_score": 0,
                "ai_relevance": e.industry or "—",
                "theme": e.industry or "A 股",
                "entry_price": price_map.get(e.code),
                "entry_currency": "CNY",
                "model_source": "v6_cn",
                "signal": "watch" if e.coverage_score < 0.50 else "buy",
                "coverage_score": e.coverage_score,
                "missing_factors": e.missing_factors,
                "factor_weights_used": e.factor_weights_used,
            })
        if db_rows:
            filled = sum(1 for r in db_rows if r.get("entry_price") is not None)
            print(f"  ({len(db_rows)} 行 · entry_price 已算 {filled}/{len(db_rows)} · JSON 已落 a_share_picks.json)")
    except Exception as db_e:
        print(f"  ⚠️  entry_price 计算失败: {db_e}")
    return 0


# ─────────── 工具 ───────────

def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(q * (len(s) - 1))
    return s[idx]


def _winsorize_rank(values: list[float | None],
                    lo_pct: float = 0.01, hi_pct: float = 0.99
                    ) -> list[float | None]:
    """横截面归一化：[lo_pct, hi_pct] winsorize 截尾后做 percent-rank。

    比 min-max 稳健：单只异常股不会污染整个范围，缺失值保留 None（上层补 0.5）。
    返回与输入等长的 list，每个元素 ∈ [0,1] ∪ {None}。
    样本量 < 4 时无统计意义，全返 0.5（避免 watchlist 太小时归一化失真）。
    """
    valid = sorted([v for v in values if isinstance(v, (int, float))])
    n = len(valid)
    if n < 4:
        return [0.5 if isinstance(v, (int, float)) else None for v in values]

    lo_idx = int(lo_pct * (n - 1))
    hi_idx = int(hi_pct * (n - 1))
    lo, hi = valid[lo_idx], valid[hi_idx]

    # 截尾后的 sorted 池子（用于排名）
    pool = sorted(max(lo, min(hi, v)) for v in valid)
    pool_n = len(pool)

    out: list[float | None] = []
    for v in values:
        if not isinstance(v, (int, float)):
            out.append(None)
            continue
        clipped = max(lo, min(hi, v))
        below = sum(1 for x in pool if x < clipped)
        eq = sum(1 for x in pool if x == clipped)
        out.append((below + 0.5 * eq) / pool_n)
    return out


def _select_with_sector_cap(entries: list[APickEntry], top_k: int,
                            sector_cap_count: int
                            ) -> tuple[list[APickEntry], list[APickEntry]]:
    """贪心选 top_k：按 composite 排序，单 industry 不超过 sector_cap_count。

    返回 (selected, skipped_due_to_cap)。industry 为空时归类到 "_unknown_"，
    限制条件相同（避免空 industry 集中度爆炸）。
    """
    selected: list[APickEntry] = []
    skipped: list[APickEntry] = []
    sector_counts: dict[str, int] = {}

    for e in entries:  # entries 已按 composite 降序
        if not e.recommended:
            continue
        if len(selected) >= top_k:
            break
        sec = e.industry or "_unknown_"
        if sector_counts.get(sec, 0) >= sector_cap_count:
            skipped.append(e)
            e.notes = (e.notes or []) + [f"sector_cap: {sec} 已达 {sector_cap_count} 只上限"]
            continue
        selected.append(e)
        sector_counts[sec] = sector_counts.get(sec, 0) + 1
    return selected, skipped


def _is_after_a_share_close(now: datetime | None = None) -> bool:
    """A 股是否已收盘（含周末）。

    判定：周末 → True（上一交易日数据已稳定）；工作日 → 16:00 后 → True。
    16:00 而不是 15:00 是为了等龙虎榜/北向 T+1 数据落库。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:  # 周六周日
        return True
    return now.hour >= 16


# ─────────── CLI ───────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["tertile", "median", "quartile"], default="tertile")
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sector-cap", type=int, default=3,
                        help="单 industry 在 top_k 内最多入选股票数（默认 3）")
    parser.add_argument("--require-after-close", action="store_true",
                        help="仅在 A 股收盘后允许执行（北向 T+1 + LHB 盘后才出）")
    parser.add_argument("--bypass-ic-gate", action="store_true",
                        help="⚠️ 强行跳过因子 IC 闸门（需自担风险）")
    parser.add_argument("--bypass-audit-gate", action="store_true",
                        help="⚠️ 强行跳过跨源 audit CONFLICT 闸门（需自担风险）")
    parser.add_argument("--workers", type=int, default=int(os.getenv("STOCK_ASSISTANT_A_WORKERS", "3")),
                        help="缓存未命中时并发拉 A 股因子的线程数（默认 3）")
    parser.add_argument("--universe", choices=["auto", "static", "dynamic", "watchlist"],
                        default=os.getenv("A_SHARE_UNIVERSE", "auto"),
                        help="A 股候选池：auto=动态失败回退静态；watchlist=仅用户自选")
    parser.add_argument("--universe-limit", type=int,
                        default=int(os.getenv("A_SHARE_UNIVERSE_LIMIT", "80")),
                        help="A 股候选池最多评估多少只（默认 80；0 表示不限）")
    args = parser.parse_args()
    return run_a_share_picks(
        top_k=args.top, mode=args.mode, dry_run=args.dry_run,
        sector_cap_count=args.sector_cap,
        require_after_close=args.require_after_close,
        bypass_ic_gate=args.bypass_ic_gate,
        bypass_audit_gate=args.bypass_audit_gate,
        workers=args.workers,
        universe=args.universe,
        universe_limit=(args.universe_limit or None),
    )


if __name__ == "__main__":
    sys.exit(main() or 0)
