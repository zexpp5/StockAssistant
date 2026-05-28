"""股权穿透：A 股次新股的「第一大股东类型」推断 + 前 N 大股东集中度。

⚠️ 重要：这个模块拿到的是「第一大股东」(剔除托管/公募/汇金等被动账户),
   不是「多层穿透的实际控制人」。
   - 自然人直接持股 (创始人控股的次新股) → 两者一致, proxy 准确
   - 多层 SPV / 国资集团多级控股 → 不一致, 需付费源 (天眼查/企查查) 才能穿透
   字段名沿用 controller_nature / controller_name 但语义是「第一大股东」, UI 文案已明示。

为什么需要：
  次新股最容易死在「解禁砸盘」和「大股东减持」，但触底分公式只看价格，看不见股权结构。
  本模块只做 advisory（不进 verdict / tier / readiness），用于审查卡和表格列。

数据源：
  主源：ak.stock_gdfx_top_10_em(symbol=sh688256, date=20260331)  东方财富，前 10 大股东（含限售）
        返回列: 名次/股东名称/股份类型/持股数/占总股本持股比例/增减/变动比率
        覆盖率高（含小盘次新股），按季报期，要带市场前缀
  备源：ak.stock_main_stock_holder(stock=code)  新浪 vip 前 5 大股东
        对很多次新股返回 "No tables found"，覆盖差，仅作 fallback

识别策略（粗到细，针对「第一大股东」而非穿透实控人）：
  1. 取第一大「非被动股东」（剔除托管/公募/证金/汇金等被动账户）
  2. 若前 3 大里出现自然人 → 民营 (创始人持股，proxy = 实控人)
  3. 若关键词匹配国资关键词集 → 国资背景 (但可能是国资集团的子公司层级，不是国资委本身)
  4. 若关键词匹配外资特征 → 外资背景
  5. 第一大股东 < 20% 且前 5 合计 < 40% → 股权分散
  6. 其余 → unknown

口径限制：
  - 自动推断 confidence = "heuristic"，会有误判（如名字像国资的民营）
  - 手动 override 走 data/overrides/ownership_overrides.json
  - 持股截至日期通常季报口径，会有 1-3 月滞后
"""
from __future__ import annotations
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
CACHE_PATH = REPO / "data" / "cache" / "ownership_lookthrough.json"
OVERRIDE_PATH = REPO / "data" / "overrides" / "ownership_overrides.json"
CACHE_TTL_DAYS = 7  # 季报口径不会日变，7 天足够

# 被动账户：托管 / 公募 / 证金 / 汇金 / 社保 / 养老 — 不算实控人候选
PASSIVE_HOLDER_KEYWORDS = (
    "香港中央结算",         # 港股通托管
    "证券金融股份",         # 证金公司
    "中央汇金",             # 汇金 (被动持有四大行等)
    "汇金资产管理",
    "社保基金",             # 全国社保
    "全国社会保障",
    "养老保险",             # 各类养老金
    "基本养老保险",
    "证券投资基金",         # 公募基金
    "指数证券投资",
    "ETF",
    "交易型开放式指数",
    "联接基金",
)

# 国资关键词
SOE_KEYWORDS = (
    "国资委", "国有资产", "国资", "国投", "国控",
    "国家开发投资", "国新", "国机",
    "中央汇金",
    # 央企前缀（按 SASAC 名录粗筛）
    "中国石油", "中国石化", "中国海洋石油", "中海油",
    "中国电信", "中国移动", "中国联通", "中国铁通",
    "中国华能", "中国大唐", "中国华电", "中国国电", "国家电网",
    "中国南方电网", "中国核工业", "中核",
    "中国中铁", "中国铁建", "中国交建", "中国建筑", "中国建材",
    "中国中车", "中国船舶", "中航", "中船", "中粮",
    "华润", "保利", "招商局", "中信", "光大",
    "国家开发银行", "进出口银行", "农业发展银行",
    "工商银行", "农业银行", "中国银行", "建设银行", "交通银行", "邮政储蓄",
    # 地方国资关键词（弱信号，需配合"省/市"）
    "省国有", "市国有", "省国资", "市国资", "省投资", "市投资",
    "省属", "市属",
    "省财政", "市财政",
)

# 外资特征
FOREIGN_KEYWORDS = (
    "(BVI)", "BVI",
    "HOLDINGS", "LIMITED", "Co.,Ltd",
    "Inc.", "Corp.",
    "（开曼）", "(开曼)",
    "Cayman",
    "境外", "海外",
)

# 自然人识别：2-4 汉字、不含组织后缀
ORG_SUFFIXES = (
    "公司", "集团", "中心", "合伙", "企业", "厂",
    "基金", "银行", "保险", "信托", "投资", "控股",
    "证券", "资产", "管理", "实业", "科技", "股份",
)


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("ownership_lookthrough cache 损坏: %s", e)
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_overrides() -> dict:
    """手动 override 文件：{"600519": {"controller_nature": "国资", "controller_name": "..."}}"""
    if not OVERRIDE_PATH.exists():
        return {}
    try:
        return json.loads(OVERRIDE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("ownership overrides 损坏: %s", e)
        return {}


def _is_fresh(entry: dict, ttl_days: int = CACHE_TTL_DAYS) -> bool:
    ts = entry.get("fetched_at")
    if not ts:
        return False
    try:
        fetched = datetime.fromisoformat(ts)
    except Exception:
        return False
    return (datetime.now() - fetched) < timedelta(days=ttl_days)


def _is_passive(name: str) -> bool:
    return any(kw in name for kw in PASSIVE_HOLDER_KEYWORDS)


def _is_natural_person(name: str) -> bool:
    """2-4 汉字、不含组织后缀 → 自然人。"""
    if not name or len(name) < 2 or len(name) > 4:
        return False
    if any(suf in name for suf in ORG_SUFFIXES):
        return False
    # 全是汉字
    return all("一" <= ch <= "鿿" for ch in name)


def _match_soe(name: str) -> bool:
    """国资识别。地方国资必须含明确国资词，避免"杭州XX控股"民企误判。"""
    if any(kw in name for kw in SOE_KEYWORDS):
        return True
    # 地方国资强信号：必须含明确国资词
    strong_local = (
        "国有资产", "国有资本", "国资委",
        "城投", "建投", "水投", "交投", "产投", "金投",
        "国有控股", "国有投资", "国有股权",
    )
    if any(kw in name for kw in strong_local):
        return True
    # 央企弱信号: "中国XX" 开头 + 是有限/股份/集团 (民企取名「中国XX」会被工商驳回)
    if name.startswith("中国") and any(suf in name for suf in ("公司", "集团", "股份", "有限")):
        return True
    return False


def _match_foreign(name: str) -> bool:
    upper = name.upper()
    if any(kw.upper() in upper for kw in FOREIGN_KEYWORDS):
        return True
    return False


def _pick_primary_holder(holders: list[dict]) -> dict | None:
    """从前 N 大股东里挑出第一大『主要股东』（剔除被动账户）。"""
    for h in holders:
        name = h.get("name", "")
        if _is_passive(name):
            continue
        v = h.get("pct")
        if v is None or v != v or v <= 0:  # NaN-safe
            continue
        return h
    return None


def _infer_nature(holders: list[dict]) -> tuple[str, str | None, float]:
    """返回 (controller_nature, controller_name, top5_concentration_pct)。

    nature ∈ {国资, 民营, 外资, 无实控人, unknown}
    """
    # top5 集中度（含被动账户也算，反映流通盘有多分散）
    # 注意：akshare 部分股东 pct 是 NaN，需过滤
    def _safe_pct(h):
        v = h.get("pct")
        if v is None:
            return 0.0
        try:
            if v != v:  # NaN check
                return 0.0
            return float(v)
        except (ValueError, TypeError):
            return 0.0
    top5_concentration = round(sum(_safe_pct(h) for h in holders[:5]), 2)

    primary = _pick_primary_holder(holders)
    if not primary:
        return ("unknown", None, top5_concentration)

    name = primary["name"]
    pct = _safe_pct(primary)

    # 前 3 大股东里只要出现自然人 → 民营（次新股创始人常被一致行动人摊分到 20-30%）
    for h in holders[:3]:
        nm = h.get("name", "")
        if not _is_passive(nm) and _is_natural_person(nm):
            return ("民营", nm, top5_concentration)

    # 第一大股东是法人
    if _is_natural_person(name):  # 兜底
        return ("民营", name, top5_concentration)
    if _match_soe(name):
        return ("国资", name, top5_concentration)
    if _match_foreign(name):
        return ("外资", name, top5_concentration)

    # 极度分散 → 无实控人（仅对法人型第一大股东）
    if pct < 20 and top5_concentration < 40:
        return ("无实控人", name, top5_concentration)

    # 是法人但识别不出来 → unknown（建议手动 override）
    return ("unknown", name, top5_concentration)


def _parse_holders_df(df: Any) -> list[dict]:
    """akshare DataFrame → 标准化 list[dict]。

    兼容两种 schema：
      新浪: 股东名称 / 持股比例 / 股本性质
      东财: 股东名称 / 占总股本持股比例 / 股份类型
    """
    if df is None or df.empty:
        return []
    cols = list(df.columns)
    if "占总股本持股比例" in cols:
        pct_col, type_col = "占总股本持股比例", "股份类型"
    else:
        pct_col, type_col = "持股比例", "股本性质"

    out = []
    for _, row in df.iterrows():
        name = str(row.get("股东名称", "")).strip()
        if not name or name == "nan":
            continue
        pct_raw = row.get(pct_col)
        try:
            pct = float(pct_raw) if pct_raw is not None else None
        except (ValueError, TypeError):
            pct = None
        out.append({
            "name": name,
            "pct": pct,
            "share_type": str(row.get(type_col, "") or ""),
        })
    # 去重（同名股东偶尔重复出现）
    seen = set()
    dedup = []
    for h in out:
        key = h["name"]
        if key in seen:
            continue
        seen.add(key)
        dedup.append(h)
    return dedup


def _market_prefix(code: str) -> str:
    """6 位 A 股代码 → 东财格式 (sh/sz/bj 前缀)。"""
    if not code:
        return ""
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("0", "2", "3")):
        return "sz" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sh" + code  # 默认


def _latest_reported_quarter(today: datetime | None = None) -> str:
    """计算最近一个已发布的季报期 YYYYMMDD。

    披露规则（粗略）：
      Q1 (3/31) → 4/30 前披露完  →  5/1 起可用
      Q2 (6/30) → 8/31 前披露完  →  9/1 起可用
      Q3 (9/30) → 10/31 前披露完 →  11/1 起可用
      Q4 (12/31)→ 4/30 前披露完  →  5/1 起可用 (但 Q1 也已出)
    """
    today = today or datetime.now()
    y, m = today.year, today.month
    if m >= 11:
        return f"{y}0930"
    if m >= 9:
        return f"{y}0630"
    if m >= 5:
        return f"{y}0331"
    # 1-4 月 → 用去年 Q3 (Q4 年报 4 月底才出齐)
    return f"{y-1}0930"


def fetch_top_holders(code: str, *, force_refresh: bool = False) -> dict:
    """获取一只 A 股的股权穿透摘要。带 7 天缓存 + 手动 override。

    返回：
      {
        "controller_nature": "国资|民营|外资|无实控人|unknown",
        "controller_name":   str | None,
        "controller_confidence": "manual|heuristic",
        "top5_concentration_pct": float,
        "fetched_at": ISO 时间戳,
        "source": "manual_override|akshare|cache",
      }

    失败时返回 {"controller_nature": "unknown", ...}。
    """
    code = str(code).strip().zfill(6) if str(code).isdigit() else str(code).strip()

    # 1. manual override 最高优先
    overrides = _load_overrides()
    if code in overrides:
        ov = overrides[code]
        return {
            "controller_nature": ov.get("controller_nature", "unknown"),
            "controller_name": ov.get("controller_name"),
            "controller_confidence": "manual",
            "top5_concentration_pct": ov.get("top5_concentration_pct", 0.0),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "source": "manual_override",
        }

    # 2. 缓存
    cache = _load_cache()
    if not force_refresh and code in cache and _is_fresh(cache[code]):
        entry = dict(cache[code])
        entry["source"] = "cache"
        return entry

    # 3. 拉东财（主源，覆盖好），失败再 fallback 新浪
    df = None
    try:
        import akshare as ak
        sym = _market_prefix(code)
        quarter = _latest_reported_quarter()
        df = ak.stock_gdfx_top_10_em(symbol=sym, date=quarter)
    except Exception as e:
        logger.debug("ownership em fetch %s failed: %s", code, e)
    if df is None or df.empty:
        try:
            import akshare as ak
            df = ak.stock_main_stock_holder(stock=code)
        except Exception as e:
            logger.warning("ownership sina fallback %s failed: %s", code, e)
            df = None
    if df is None or df.empty:
        # 失败时如果有过期缓存仍用，标 source=stale_cache
        if code in cache:
            entry = dict(cache[code])
            entry["source"] = "stale_cache"
            return entry
        return {
            "controller_nature": "unknown",
            "controller_name": None,
            "controller_confidence": "heuristic",
            "top5_concentration_pct": 0.0,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "source": "fetch_failed",
        }

    holders = _parse_holders_df(df)
    nature, name, top5 = _infer_nature(holders)
    entry = {
        "controller_nature": nature,
        "controller_name": name,
        "controller_confidence": "heuristic",
        "top5_concentration_pct": top5,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
    cache[code] = entry
    _save_cache(cache)
    entry["source"] = "akshare"
    return entry


def bulk_fetch(codes: list[str], *, sleep_sec: float = 1.0,
               force_refresh: bool = False) -> dict[str, dict]:
    """批量拉。命中缓存的不限频；走网络的按 sleep_sec 节流。"""
    cache = _load_cache()
    overrides = _load_overrides()
    out: dict[str, dict] = {}
    network_count = 0
    for code in codes:
        c = str(code).strip().zfill(6) if str(code).isdigit() else str(code).strip()
        if c in overrides or (not force_refresh and c in cache and _is_fresh(cache[c])):
            out[c] = fetch_top_holders(c, force_refresh=False)
            continue
        if network_count > 0:
            time.sleep(sleep_sec)
        out[c] = fetch_top_holders(c, force_refresh=force_refresh)
        network_count += 1
    if network_count:
        logger.info("ownership bulk_fetch: %d codes, %d network hits", len(codes), network_count)
    return out


if __name__ == "__main__":
    # smoke test
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for code in ["600519", "688256", "300750"]:
        r = fetch_top_holders(code, force_refresh=True)
        print(f"{code}: {r}")
        time.sleep(1)
