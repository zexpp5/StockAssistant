"""空头拥挤度提示灯（display-only · 绝不进打分模型）。

2026-06-12 设计边界（用户对齐）：
  本模块只回答一个问题——"这只票现在有没有额外的空头风险？"
  它**不**回答"该不该买"。输出是一盏低/中/高的提示灯，给持仓页和买前研究页展示用，
  绝不喂进 AI 推荐的因子打分（那必须先过 IC 记分牌，本模块刻意不参与）。

数据口径与限制：
  - 来源 = yfinance .info 的 FINRA 双月结算字段（shortPercentOfFloat / shortRatio /
    sharesShort / sharesShortPriorMonth）。这是**双月、约两周滞后**的慢数据，
    能反映结构性拥挤，但抓不住盘中突发逼空——提示灯本就不该当实时信号用。
  - 借券费 / 实时 days-to-cover：yfinance 没有，真实借券费要付费源（Ortex/S3/IBKR）。
    本模块不假装有这些字段（见上轮分析结论）。
  - 港股 / A 股：没有等价 FINRA 短仓披露，统一返回 not_applicable，不硬凑。

阈值（行业经验值，标注为启发式，非验证因子）：
  - short % float: <5% 低 / 5–10% 中 / ≥10% 高（≥20% 仍记"高"并在 note 标"极高"）
  - days to cover: 仅作升级项——≥6 把"低"抬到"中"，≥8 至少"高"
  - 环比（sharesShort vs priorMonth）：升 >+15% 加"在增加"提示，降 <−15% 加"在减少"
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
CACHE_PATH = REPO / "data" / "latest" / "short_interest_cache.json"
CACHE_TTL_HOURS = 20.0  # 双月数据，一天刷一次绰绰有余


def _as_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None  # 排除 NaN
    except (TypeError, ValueError):
        return None


def classify_short_crowding(
    short_pct_float: float | None,
    days_to_cover: float | None,
    shares_short: float | None = None,
    shares_short_prior: float | None = None,
) -> dict[str, Any]:
    """纯分类函数（无网络，便于单测）。返回展示用 dict。

    level ∈ {低, 中, 高, 未知}；level 仅描述"额外空头风险"，不含买卖含义。
    """
    spf = _as_float(short_pct_float)
    dtc = _as_float(days_to_cover)
    ss = _as_float(shares_short)
    ssp = _as_float(shares_short_prior)

    # short % float 拿不到 → 提示灯点不亮（多半是非美股或字段缺失）
    if spf is None:
        return {
            "level": "未知",
            "short_pct_float": None,
            "days_to_cover": dtc,
            "mom_change_pct": None,
            "note": "无空头披露数据（非美股或数据源未覆盖）",
            "reasons": [],
        }

    spf_pct = spf * 100.0 if spf <= 1.0 else spf  # 兼容 0.047 与 4.7 两种存法
    reasons: list[str] = []

    # 主信号：short % float
    if spf_pct >= 10.0:
        level = "高"
    elif spf_pct >= 5.0:
        level = "中"
    else:
        level = "低"
    reasons.append(f"空头占流通股 {spf_pct:.1f}%")

    # 升级项：days to cover（回补压力）
    if dtc is not None:
        reasons.append(f"回补天数 {dtc:.1f} 天")
        if dtc >= 8.0:
            if level != "高":
                level = "高"
                reasons.append("回补天数≥8 抬升至高")
        elif dtc >= 6.0 and level == "低":
            level = "中"
            reasons.append("回补天数≥6 抬升至中")

    # 环比方向
    mom_pct = None
    if ss is not None and ssp is not None and ssp > 0:
        mom_pct = (ss / ssp - 1.0) * 100.0
        if mom_pct > 15.0:
            reasons.append(f"空头环比 +{mom_pct:.0f}%（在增加）")
        elif mom_pct < -15.0:
            reasons.append(f"空头环比 {mom_pct:.0f}%（在减少）")

    # note：一句话人话总结
    if spf_pct >= 20.0:
        head = "空头极高"
    elif level == "高":
        head = "空头拥挤"
    elif level == "中":
        head = "空头中等"
    else:
        head = "空头很低"
    tail = ""
    if mom_pct is not None and mom_pct > 15.0:
        tail = "，但环比在增加，留意"
    elif mom_pct is not None and mom_pct < -15.0:
        tail = "，且环比在减少"
    note = head + tail

    return {
        "level": level,
        "short_pct_float": round(spf_pct, 2),
        "days_to_cover": round(dtc, 2) if dtc is not None else None,
        "mom_change_pct": round(mom_pct, 1) if mom_pct is not None else None,
        "note": note,
        "reasons": reasons,
    }


def _is_us_ticker(symbol: str) -> bool:
    s = str(symbol).upper()
    return "." not in s and "-" != s[:1]  # 港股 .HK / A 股 .SS/.SZ 带后缀；美股纯字母


def _read_cache() -> dict[str, Any]:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_cache(payload: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".short_interest_", suffix=".json", dir=str(CACHE_PATH.parent))
    try:
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    finally:
        Path(tmp).replace(CACHE_PATH)


def _cache_fresh(entry: dict | None, now: datetime) -> bool:
    if not entry:
        return False
    ts = entry.get("fetched_at")
    if not ts:
        return False
    try:
        age_h = (now - datetime.fromisoformat(ts)).total_seconds() / 3600.0
        return age_h < CACHE_TTL_HOURS
    except Exception:
        return False


def _fetch_short_fields(symbol: str) -> dict[str, Any]:
    """从 yfinance .info 取四个短仓字段。失败返回 {error}。"""
    import yfinance as yf  # 延迟导入，避免给纯分类单测加依赖
    info = yf.Ticker(symbol).info or {}
    return {
        "short_pct_float": info.get("shortPercentOfFloat"),
        "days_to_cover": info.get("shortRatio"),
        "shares_short": info.get("sharesShort"),
        "shares_short_prior": info.get("sharesShortPriorMonth"),
    }


def resolve_short_crowding(
    symbols: list[str],
    *,
    use_cache: bool = True,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """批量取每只票的空头拥挤度。带 TTL JSON 缓存，只对美股发网络请求。

    返回 {symbol: classification}。非美股直接 not_applicable，不打网络。
    """
    now = now or datetime.now()
    cache = _read_cache() if use_cache else {}
    out: dict[str, dict[str, Any]] = {}
    dirty = False

    for sym in symbols:
        s = str(sym).upper()
        if not _is_us_ticker(s):
            out[s] = {
                "level": "不适用",
                "short_pct_float": None,
                "days_to_cover": None,
                "mom_change_pct": None,
                "note": "非美股，无 FINRA 短仓披露",
                "reasons": [],
            }
            continue

        entry = cache.get(s)
        if use_cache and _cache_fresh(entry, now):
            fields = entry["fields"]
        else:
            try:
                fields = _fetch_short_fields(s)
                cache[s] = {"fields": fields, "fetched_at": now.isoformat(timespec="seconds")}
                dirty = True
            except Exception as e:
                # 抓取失败：能用旧缓存就用旧的（标 stale），否则给未知
                if entry and entry.get("fields"):
                    fields = entry["fields"]
                else:
                    out[s] = {
                        "level": "未知", "short_pct_float": None, "days_to_cover": None,
                        "mom_change_pct": None, "note": f"短仓数据抓取失败：{str(e)[:60]}",
                        "reasons": [],
                    }
                    continue

        out[s] = classify_short_crowding(
            fields.get("short_pct_float"),
            fields.get("days_to_cover"),
            fields.get("shares_short"),
            fields.get("shares_short_prior"),
        )

    if use_cache and dirty:
        try:
            _write_cache(cache)
        except Exception:
            pass
    return out
