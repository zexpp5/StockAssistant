"""次新股 / IPO / 解禁雷达：三市场聚合（CN / US / HK）。

被 daily_refresh.sh 调用。下游消费：
  - dashboard 「📅 IPO & 次新股」tab（含市场切换）
  - 今日决策台「📅 本周市场事件」轻量提醒卡

输出：
  data/latest/junior_stock_radar.json
  data/cache/us_ipo_dates.json  (finnhub profile2 缓存，IPO 日期不变所以可缓存很久)

数据源（不同市场不同来源 — 港股最弱，A 股最强）：

  【A 股】
    - ipo_calendar.json (上游 ipo_daily.py 已生成)            IPO 日历
    - ak.stock_xgsr_ths()                                     次新股首日表现（3800+ 条）
    - ak.stock_restricted_release_detail_em(start, end)       未来 90 天个股解禁明细

  【美股】
    - stock_research.core.nasdaq_ipo.fetch_window()           NASDAQ 公开 API,过去 24 月 priced + 未来 2 月 filed
    - yfinance.download(batch)                                IPO universe 当前价批拉
    - 美股没有便宜的 lockup 数据源（S-1 招股书里），解禁雷达暂缺
    - IPO universe 独立于 system_universe,不污染主推荐流程

  【港股】
    - 没有便宜的开源 IPO/解禁源（finnhub 免费层不含港股，akshare hk 不可靠）
    - 仅显示 placeholder + HKEX 外链；后续需付费源（Wind / Choice / HKEX 官方）激活

设计原则：
  - 不算因子、不写库、不发飞书 — 只是聚合 + 打分 + 写 JSON
  - 美股次新股复用 system_universe 已有 ticker（69 只），不全网扫
  - finnhub 调用带文件缓存（IPO 日期是常量），首日跑 ~70s，之后秒级
"""
from __future__ import annotations

import json
import logging
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CACHE_DIR = REPO / "data" / "cache"
CN_INDUSTRY_CACHE = CACHE_DIR / "cn_industry_by_code.json"
CN_INDUSTRY_TTL_DAYS = 7  # 行业基本不变；缓存 7 天可覆盖周末 + 个别拉取失败

# 美股 yfinance 行业 → 中文映射（GICS 风格）
# 用户不熟英文行业名，dashboard 展示用中文；漏掉的回退原英文 + (待译)
US_SECTOR_ZH = {
    "Basic Materials": "基础材料",
    "Communication Services": "通信服务",
    "Consumer Cyclical": "可选消费",
    "Consumer Defensive": "必需消费",
    "Energy": "能源",
    "Financial Services": "金融",
    "Healthcare": "医疗保健",
    "Industrials": "工业",
    "Real Estate": "房地产",
    "Technology": "信息技术",
    "Utilities": "公用事业",
}

US_INDUSTRY_ZH = {
    "Advertising Agencies": "广告代理",
    "Aerospace & Defense": "航空航天与国防",
    "Airlines": "航空",
    "Auto & Truck Dealerships": "汽车经销",
    "Banks - Regional": "区域性银行",
    "Beverages - Wineries & Distilleries": "酒类制造",
    "Biotechnology": "生物技术",
    "Broadcasting": "广播",
    "Building Materials": "建筑材料",
    "Building Products & Equipment": "建材与设备",
    "Capital Markets": "资本市场",
    "Computer Hardware": "计算机硬件",
    "Conglomerates": "综合企业",
    "Consulting Services": "咨询服务",
    "Credit Services": "信贷服务",
    "Diagnostics & Research": "诊断与研究",
    "Drug Manufacturers - Specialty & Generic": "特种与仿制药",
    "Education & Training Services": "教育培训",
    "Electronic Gaming & Multimedia": "电子游戏与多媒体",
    "Engineering & Construction": "工程与建筑",
    "Entertainment": "娱乐",
    "Furnishings, Fixtures & Appliances": "家居家电",
    "Gambling": "博彩",
    "Gold": "黄金",
    "Health Information Services": "医疗信息服务",
    "Household & Personal Products": "家用与个人用品",
    "Information Technology Services": "IT 服务",
    "Insurance - Diversified": "综合保险",
    "Insurance - Property & Casualty": "财产保险",
    "Insurance Brokers": "保险经纪",
    "Integrated Freight & Logistics": "综合物流",
    "Internet Content & Information": "互联网内容",
    "Leisure": "休闲娱乐",
    "Medical Care Facilities": "医疗机构",
    "Medical Devices": "医疗器械",
    "Medical Distribution": "医药分销",
    "Medical Instruments & Supplies": "医疗器械与耗材",
    "Oil & Gas E&P": "油气勘探开采",
    "Oil & Gas Equipment & Services": "油气设备与服务",
    "Oil & Gas Midstream": "油气中游",
    "Other Industrial Metals & Mining": "其他工业金属与采矿",
    "Packaged Foods": "包装食品",
    "Pollution & Treatment Controls": "环保治理设备",
    "REIT - Diversified": "综合 REIT",
    "REIT - Industrial": "工业 REIT",
    "REIT - Specialty": "特种 REIT",
    "Real Estate - Development": "房地产开发",
    "Restaurants": "餐饮",
    "Scientific & Technical Instruments": "科学仪器",
    "Semiconductors": "半导体",
    "Shell Companies": "壳公司",
    "Software - Application": "应用软件",
    "Software - Infrastructure": "基础软件",
    "Solar": "太阳能",
    "Specialty Business Services": "专业商业服务",
    "Specialty Industrial Machinery": "专业工业机械",
    "Specialty Retail": "专业零售",
    "Steel": "钢铁",
    "Telecom Services": "电信服务",
    "Travel Services": "旅游服务",
    "Waste Management": "废物处理",
    # 常见但当前池子未出现，预填覆盖未来新股
    "Asset Management": "资产管理",
    "Auto Manufacturers": "汽车制造",
    "Auto Parts": "汽车零部件",
    "Beverages - Non-Alcoholic": "非酒精饮料",
    "Chemicals": "化工",
    "Communication Equipment": "通信设备",
    "Confectioners": "糖果食品",
    "Consumer Electronics": "消费电子",
    "Copper": "铜业",
    "Discount Stores": "折扣零售",
    "Electrical Equipment & Parts": "电气设备",
    "Electronic Components": "电子元件",
    "Electronics & Computer Distribution": "电子与计算机分销",
    "Farm & Heavy Construction Machinery": "农机与重型工程机械",
    "Farm Products": "农产品",
    "Financial Conglomerates": "综合金融",
    "Financial Data & Stock Exchanges": "金融数据与交易所",
    "Footwear & Accessories": "鞋类与配饰",
    "Grocery Stores": "杂货零售",
    "Health Care Plans": "医疗保险计划",
    "Home Improvement Retail": "家居装修零售",
    "Industrial Distribution": "工业分销",
    "Internet Retail": "互联网零售",
    "Lodging": "酒店住宿",
    "Lumber & Wood Production": "木材生产",
    "Luxury Goods": "奢侈品",
    "Marine Shipping": "海运",
    "Metal Fabrication": "金属加工",
    "Mortgage Finance": "住房抵押金融",
    "Personal Services": "个人服务",
    "Pharmaceutical Retailers": "药品零售",
    "Publishing": "出版",
    "Railroads": "铁路",
    "Rental & Leasing Services": "租赁服务",
    "Residential Construction": "住宅建筑",
    "Resorts & Casinos": "度假村与赌场",
    "Security & Protection Services": "安保服务",
    "Silver": "白银",
    "Staffing & Employment Services": "人力资源",
    "Tobacco": "烟草",
    "Tools & Accessories": "工具与配件",
    "Trucking": "卡车运输",
    "Uranium": "铀矿",
    "Utilities - Diversified": "综合公用事业",
    "Utilities - Independent Power Producers": "独立发电",
    "Utilities - Regulated Electric": "电力公用",
    "Utilities - Regulated Gas": "燃气公用",
    "Utilities - Regulated Water": "供水公用",
    "Utilities - Renewable": "可再生能源",
}


def _zh_us_sector(en: Any) -> str:
    if not en:
        return ""
    s = str(en).strip()
    return US_SECTOR_ZH.get(s, s)


def _zh_us_industry(en: Any) -> str:
    if not en:
        return ""
    s = str(en).strip()
    return US_INDUSTRY_ZH.get(s, f"{s}(待译)")


# ───────────── 通用工具 ─────────────

def _safe_float(x: Any) -> float | None:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except Exception:
        return None


def _norm6(code: Any) -> str:
    if code is None:
        return ""
    s = str(code).strip().split(".")[0]
    if s.isdigit():
        return s.zfill(6)[-6:]
    return s


def _to_iso(d: Any) -> str | None:
    if d is None:
        return None
    if isinstance(d, (date, datetime)):
        return d.isoformat() if isinstance(d, date) and not isinstance(d, datetime) else d.date().isoformat()
    s = str(d).strip()
    if not s or s in {"nan", "NaT", "None"}:
        return None
    return s[:10]


# ───────────── 红线 gate（step 1） ─────────────
# 触发任一红线即标 verdict="不碰"；其它档位（只观察/可研究/可小仓试探）由 step 3 填。
# 数据缺失（如 market_cap=None）默认不触发对应红线 —— 缺数据≠有问题。

CN_ST_PREFIXES = ("ST", "*ST", "S*ST", "SST", "S ST")


def _evaluate_cn_red_lines(item: dict, unlock_30d_map: dict[str, dict]) -> list[dict]:
    """A 股次新池红线评估。unlock_30d_map: {code: {days, pct, stress}}"""
    out: list[dict] = []
    name = (item.get("name") or "").strip()
    code = item.get("code") or ""
    # ST / *ST 壳公司
    if any(name.startswith(p) for p in CN_ST_PREFIXES) or "ST" in name[:4]:
        out.append({
            "key": "st",
            "label": "ST 壳公司",
            "detail": f"名称 {name} 被特别处理，退市风险显著",
        })
    # 30 天内大额解禁
    u = unlock_30d_map.get(code)
    if u:
        out.append({
            "key": "unlock_30d",
            "label": "30 日内大额解禁",
            "detail": f"{u['days']} 天后解禁 · 占流通 {u['pct']:.0f}% · 压力分 {u['stress']:.0f}",
        })
    return out


def _evaluate_us_red_lines(item: dict, is_spac_flag: bool) -> list[dict]:
    """美股次新池红线评估。"""
    out: list[dict] = []
    price = item.get("current_price")
    mcap_m = item.get("market_cap_m")  # 已是 M 美元
    dollar_vol = item.get("dollar_volume_30d")
    industry = item.get("industry") or ""

    if is_spac_flag:
        out.append({
            "key": "spac",
            "label": "SPAC 空壳",
            "detail": "发行价 $10 + 名称含 Acquisition / 后缀 U·WS — 业务未确定",
        })
    if price is not None and price < 1.0:
        out.append({
            "key": "penny",
            "label": "仙股 (<$1)",
            "detail": f"现价 ${price:.2f} — NASDAQ 持续 6 月 <$1 触发退市",
        })
    if mcap_m is not None and mcap_m < 50.0:
        out.append({
            "key": "micro",
            "label": "微盘 (<$50M)",
            "detail": f"市值 ${mcap_m:.0f}M — going concern 风险偏高",
        })
    if "壳公司" in industry or "Shell" in industry:
        out.append({
            "key": "shell",
            "label": "壳公司",
            "detail": f"yfinance 行业分类 = {industry}",
        })
    if dollar_vol is not None and dollar_vol < 200_000:
        out.append({
            "key": "low_liquidity",
            "label": "低流动性",
            "detail": f"30 日日均成交额 ${dollar_vol/1000:.0f}K — 卖出可能滑点严重",
        })
    # step 2: lockup ±30 日窗口（老股东首次可卖 → 集中抛压）
    days_to_lockup = item.get("days_to_est_lockup")
    if days_to_lockup is not None and -30 <= days_to_lockup <= 30:
        when = "未来" if days_to_lockup > 0 else "过去"
        out.append({
            "key": "us_lockup_30d",
            "label": "Lockup ±30 日（估算）",
            "detail": f"上市 + 180 天 ≈ lockup 到期，{when} {abs(days_to_lockup)} 天 — 老股东首次可卖",
        })
    # gap #2: 180 日内 ≥ 2 次稀释相关 SEC filings = 高频增发
    filings = item.get("dilution_filings_180d") or []
    if len(filings) >= 2:
        forms = [f.get("form") for f in filings]
        out.append({
            "key": "high_dilution",
            "label": f"高频增发 ({len(filings)} 次/180日)",
            "detail": f"近 180 日 SEC 提交 {len(filings)} 次稀释类申报 ({', '.join(forms[:3])}) — 老股东持续被摊薄",
        })
    # gap #3: 基本面软过滤
    fin = item.get("financials") or {}
    gm = fin.get("gross_margin")
    if gm is not None and gm < 0:
        out.append({
            "key": "negative_gross_margin",
            "label": f"负毛利 ({gm*100:.0f}%)",
            "detail": f"TTM 毛利率 {gm*100:.0f}% — 卖东西本身亏钱,business model 严重问题",
        })
    runway = fin.get("runway_quarters")
    if runway is not None and runway < 4 and runway > 0:
        out.append({
            "key": "cash_runway_short",
            "label": f"现金跑道 <4Q ({runway}Q)",
            "detail": f"当前现金按近 TTM 烧钱速度只够 {runway} 季度 — going concern 风险",
        })
    return out


def _apply_cn_red_lines(junior_pool: list[dict], unlock_radar: list[dict]) -> None:
    """inplace 给 cn junior_pool 挂 red_lines / verdict。需要先调用此函数再 sort。

    30 天大额解禁口径：days_to_unlock ≤ 30 且 (占流通 ≥ 30% 或 stress_score ≥ 65)。
    同一只可能有多条解禁记录，取最紧迫（days 最小）的那条。
    """
    # 构造 30 天内紧迫解禁 map
    unlock_map: dict[str, dict] = {}
    for u in unlock_radar:
        if u.get("days_to_unlock", 999) > 30:
            continue
        if u.get("pct_of_float", 0) < 30 and u.get("stress_score", 0) < 65:
            continue
        code = u.get("code")
        if not code:
            continue
        prev = unlock_map.get(code)
        if prev is None or u["days_to_unlock"] < prev["days"]:
            unlock_map[code] = {
                "days": u["days_to_unlock"],
                "pct": u.get("pct_of_float", 0),
                "stress": u.get("stress_score", 0),
            }
    n_bad = 0
    for it in junior_pool:
        rls = _evaluate_cn_red_lines(it, unlock_map)
        it["red_lines"] = rls
        it["verdict"] = "不碰" if rls else None
        if rls:
            n_bad += 1
    # 重新排序：不碰沉底，同档内按分数降序
    junior_pool.sort(key=lambda x: (1 if x.get("verdict") == "不碰" else 0, -x.get("score", 0)))
    # percentile + 4 档状态分档
    _attach_percentile(junior_pool)
    _assign_tier(junior_pool, market="cn")
    tier_counts = {t: sum(1 for x in junior_pool if x.get("tier") == t)
                   for t in ("可小仓试探", "可研究", "只观察", "不碰")}
    logger.info("[CN] 红线 gate: %d / %d 标 \"不碰\"; 分档: %s",
                n_bad, len(junior_pool),
                " · ".join(f"{k}={v}" for k, v in tier_counts.items()))


def _build_unlock_60d_map(unlock_radar: list[dict]) -> dict[str, dict]:
    """构造 60 天内解禁 map (含 30 天红线那部分)。同一只票取最大占比那条。

    返回 {code: {"pct": float, "date": "YYYY-MM-DD", "days": int}}
    """
    m: dict[str, dict] = {}
    for u in unlock_radar:
        days = u.get("days_to_unlock", 999)
        if days < 0 or days > 60:
            continue
        code = u.get("code")
        if not code:
            continue
        pct = u.get("pct_of_float", 0)
        prev = m.get(code)
        if prev is None or pct > prev["pct"]:
            m[code] = {
                "pct": pct,
                "date": u.get("unlock_date"),
                "days": days,
            }
    return m


def _enrich_cn_ownership_and_unlock_60d(junior_pool: list[dict],
                                          unlock_60d_map: dict[str, dict]) -> None:
    """inplace 给 cn junior_pool 挂股权穿透 + 60d 解禁字段。advisory only — 不进 verdict/tier。

    挂载字段：
      controller_nature / controller_name / controller_confidence / top5_concentration_pct
      unlock_60d_pct / unlock_60d_date
    """
    try:
        from stock_research.core.ownership_lookthrough import bulk_fetch
    except Exception as e:
        logger.warning("ownership_lookthrough 加载失败,跳过股权穿透: %s", e)
        bulk_fetch = None

    codes = [it.get("code") for it in junior_pool if it.get("code")]
    own_map: dict[str, dict] = {}
    if bulk_fetch and codes:
        try:
            logger.info("[CN] 拉股权穿透 (%d 只, 命中缓存的不限频)...", len(codes))
            own_map = bulk_fetch(codes, sleep_sec=1.0)
        except Exception as e:
            logger.warning("ownership bulk_fetch failed: %s", e)
            own_map = {}

    nature_counts: dict[str, int] = {}
    u60_count = 0
    for it in junior_pool:
        code = it.get("code")
        own = own_map.get(code) if code else None
        if own:
            nat = own.get("controller_nature", "unknown")
            it["controller_nature"] = nat
            it["controller_name"] = own.get("controller_name")
            it["controller_confidence"] = own.get("controller_confidence", "heuristic")
            it["top5_concentration_pct"] = own.get("top5_concentration_pct", 0.0)
            nature_counts[nat] = nature_counts.get(nat, 0) + 1
        else:
            it["controller_nature"] = "unknown"
            it["controller_name"] = None
            it["controller_confidence"] = "heuristic"
            it["top5_concentration_pct"] = 0.0
            nature_counts["unknown"] = nature_counts.get("unknown", 0) + 1

        u60 = unlock_60d_map.get(code) if code else None
        if u60:
            it["unlock_60d_pct"] = u60["pct"]
            it["unlock_60d_date"] = u60["date"]
            u60_count += 1
        else:
            it["unlock_60d_pct"] = None
            it["unlock_60d_date"] = None

    logger.info("[CN] 股权穿透: %s · 60d 解禁覆盖: %d / %d",
                " · ".join(f"{k}={v}" for k, v in nature_counts.items()),
                u60_count, len(junior_pool))

    # 重建 audit_card（_assign_tier 那次没看到 60d 字段，需重算让 whats_missing 包含）
    for it in junior_pool:
        it["audit_card"] = _build_audit_card(it, market="cn")


def _board_of_cn(code: str) -> str:
    c = _norm6(code)
    if not c:
        return "other"
    if c.startswith("688"):
        return "star"
    if c.startswith("300"):
        return "chinext"
    if c.startswith(("600", "601", "603", "605")):
        return "main"
    if c.startswith(("000", "001", "002", "003")):
        return "main"
    if c.startswith(("8", "9")):
        return "bse"
    return "other"


# ───────────── 持仓 / 自选股集合 ─────────────

def _load_pool_symbols() -> dict[str, dict[str, set[str]]]:
    """读 real_holdings + manual_watchlist，按市场返回代码集合。

    返回 {'cn': {holdings, watchlist}, 'us': {...}, 'hk': {...}}
    cn 用 6 位代码；us/hk 用原始 symbol（如 AAPL / 0700.HK）。
    """
    out = {m: {"holdings": set(), "watchlist": set()} for m in ("cn", "us", "hk")}
    try:
        import duckdb
        db_path = REPO / "stock_history_v2.duckdb"
        if not db_path.exists():
            return out
        con = duckdb.connect(str(db_path), read_only=True)
        for market, symbol in con.execute("SELECT market, symbol FROM real_holdings").fetchall():
            m = (market or "").lower()
            if m == "cn":
                out["cn"]["holdings"].add(_norm6(symbol))
            elif m == "us":
                out["us"]["holdings"].add(str(symbol).upper())
            elif m == "hk":
                out["hk"]["holdings"].add(str(symbol).upper())
        for market, symbol in con.execute("SELECT market, symbol FROM manual_watchlist").fetchall():
            m = (market or "").lower()
            if "a股" in m or m == "cn":
                out["cn"]["watchlist"].add(_norm6(symbol))
            elif "美股" in m or "us" in m:
                out["us"]["watchlist"].add(str(symbol).upper())
            elif "港股" in m or "hk" in m:
                out["hk"]["watchlist"].add(str(symbol).upper())
        con.close()
    except Exception as e:
        logger.warning("read holdings/watchlist failed: %s", e)
    return out


def _load_ipo_calendar() -> dict:
    p = REPO / "data" / "ipo_calendar.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("read ipo_calendar.json failed: %s", e)
        return {}


def _import_ak():
    try:
        import akshare as ak
        return ak
    except ImportError:
        logger.error("akshare not installed; pip install akshare")
        return None


# ═════════════════════════════════════════════════════
#  A 股
# ═════════════════════════════════════════════════════

def _cn_junior_summary(
    months_listed: float,
    vs_issue_pct: float,
    vs_first_close_pct: float | None,
    first_chg_pct: float | None,
) -> str:
    """把次新股池打分维度翻译成"人话"摘要。

    朴实事实描述，不做未来预测；不做"建议买/不建议买"判断 —
    用户自行做基本面/行业/技术面三重判断。
    """
    m = int(round(months_listed))
    # 月数阶段
    if months_listed < 9:
        stage = f"上市 {m} 月刚解禁初期"
    elif months_listed < 12:
        stage = f"上市 {m} 月接近首发解禁窗口"
    elif months_listed <= 18:
        stage = f"上市 {m} 月正处首发解禁窗口"
    elif months_listed <= 21:
        stage = f"上市 {m} 月度过解禁压力期"
    else:
        stage = f"上市 {m} 月次新尾段"

    # vs 发行价
    if vs_issue_pct < -30:
        price_phrase = f"深度破发 {abs(vs_issue_pct):.0f}%"
    elif vs_issue_pct < 0:
        price_phrase = f"已破发 {abs(vs_issue_pct):.0f}%"
    elif vs_issue_pct < 50:
        price_phrase = f"较发行价 +{vs_issue_pct:.0f}%"
    elif vs_issue_pct < 100:
        price_phrase = f"较发行价 +{vs_issue_pct:.0f}% 偏强"
    else:
        price_phrase = f"较发行价 +{vs_issue_pct:.0f}% 主力强势"

    parts = [stage, price_phrase]

    # vs 首日收盘
    if vs_first_close_pct is not None:
        if vs_first_close_pct < -70:
            parts.append(f"较首日已跌 {abs(vs_first_close_pct):.0f}%（接近底部）")
        elif vs_first_close_pct < -50:
            parts.append(f"较首日跌 {abs(vs_first_close_pct):.0f}%（过半）")
        elif vs_first_close_pct < -20:
            parts.append(f"较首日跌 {abs(vs_first_close_pct):.0f}%")
        elif vs_first_close_pct < 0:
            parts.append(f"较首日小跌 {abs(vs_first_close_pct):.0f}%")
        elif vs_first_close_pct > 20:
            parts.append(f"较首日 +{vs_first_close_pct:.0f}%")
        # -20~+20 之间不啰嗦

    return " · ".join(parts)


def _attach_percentile(items: list[dict], score_key: str = "score") -> None:
    """给每个 verdict != "不碰" 的 item inplace 加 percentile 字段
    (= 在 *合格* 池子内的"前 X%")。"不碰"档不参与排名，percentile=None。

    两个市场的打分公式不同(A 股 4 维 vs 美股 5 维),绝对分数不可横向比较；
    percentile 把"位置"暴露出来 — "前 0.6%" vs "前 50%" 跨市场可读。
    """
    eligible = [x for x in items if x.get("verdict") != "不碰"]
    by_score = sorted(eligible, key=lambda x: -(x.get(score_key) or 0))
    total = len(by_score)
    if total > 0:
        for i, x in enumerate(by_score):
            x["percentile"] = round((i + 1) / total * 100, 1)
    for x in items:
        if x.get("verdict") == "不碰":
            x["percentile"] = None


# step 5b + gap #4: 日间 diff — 按日期归档,保留 N 天,加载最近一日做 prev
JUNIOR_SNAPSHOT_DIR = REPO / "data" / "snapshots" / "junior_radar"
JUNIOR_SNAPSHOT_KEEP_DAYS = 60  # 保留 60 天，超过的自动清理
TIER_RANK = {"不碰": 0, "只观察": 1, "可研究": 2, "可小仓试探": 3}


def _snapshot_path(d: date) -> Path:
    return JUNIOR_SNAPSHOT_DIR / f"junior_radar_{d.isoformat()}.json"


def _list_snapshots() -> list[tuple[date, Path]]:
    """返回所有已存归档,按日期降序。"""
    if not JUNIOR_SNAPSHOT_DIR.exists():
        return []
    out = []
    for p in JUNIOR_SNAPSHOT_DIR.glob("junior_radar_*.json"):
        try:
            d = datetime.fromisoformat(p.stem.replace("junior_radar_", "")).date()
            out.append((d, p))
        except Exception:
            continue
    out.sort(key=lambda x: -x[0].toordinal())
    return out


def _load_prev_snapshot() -> tuple[dict, str | None]:
    """加载最近一日 (早于今日) 的 snapshot 作为 diff 基准。
    返回 (key→item dict, snapshot_date_iso)。无可用返回 ({}, None)。
    """
    today = date.today()
    for d, p in _list_snapshots():
        if d >= today:
            continue  # 今日的不作 prev (避免同日重跑 diff 自己)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            idx = {}
            for it in data.get("items") or []:
                mk = it.get("market")
                code = it.get("code")
                if mk and code:
                    idx[f"{mk}:{code}"] = it
            return idx, data.get("snapshot_date")
        except Exception as exc:
            logger.warning("read snapshot %s failed: %s", p.name, exc)
            continue
    return {}, None


def _prune_old_snapshots() -> int:
    """清理 keep_days 之前的归档。返回清理个数。"""
    cutoff = date.today() - timedelta(days=JUNIOR_SNAPSHOT_KEEP_DAYS)
    n = 0
    for d, p in _list_snapshots():
        if d < cutoff:
            try:
                p.unlink()
                n += 1
            except Exception:
                pass
    return n


def _save_snapshot(cn_pool: list[dict], us_pool: list[dict]) -> None:
    items = []
    for x in cn_pool:
        if not x.get("tier"):
            continue
        items.append({
            "market": "cn",
            "code": x.get("code"),
            "name": x.get("name"),
            "tier": x.get("tier"),
            "percentile": x.get("percentile"),
            "bottom_score": x.get("score"),
            "readiness_score": x.get("readiness_score"),
        })
    for x in us_pool:
        if not x.get("tier"):
            continue
        items.append({
            "market": "us",
            "code": x.get("symbol"),
            "name": x.get("name"),
            "tier": x.get("tier"),
            "percentile": x.get("percentile"),
            "bottom_score": x.get("bottom_score") or x.get("score"),
            "readiness_score": x.get("readiness_score"),
        })
    today = date.today()
    payload = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot_date": today.isoformat(),
        "items": items,
    }
    JUNIOR_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    _snapshot_path(today).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    pruned = _prune_old_snapshots()
    if pruned:
        logger.info("[snapshot] pruned %d old snapshots beyond %d days",
                    pruned, JUNIOR_SNAPSHOT_KEEP_DAYS)


def _apply_daily_diff(cn_pool: list[dict], us_pool: list[dict],
                       prev_idx: dict, prev_date: str | None) -> dict:
    """inplace 给 cn_pool / us_pool 每个 item 加 diff 字段：
      diff_flag: "new" | "upgraded" | "downgraded" | "jumped" | "slipped" | None
      prev_tier, prev_percentile (None if 首次出现)
    返回统计 dict 便于 logging。
    """
    counts = {"new": 0, "upgraded": 0, "downgraded": 0, "jumped": 0, "slipped": 0, "exited": 0}
    seen_keys = set()
    for market, pool in [("cn", cn_pool), ("us", us_pool)]:
        for x in pool:
            code = x.get("code") or x.get("symbol")
            if not code:
                continue
            k = f"{market}:{code}"
            seen_keys.add(k)
            prev = prev_idx.get(k)
            if prev is None:
                # 首次出现 — 只对 actionable (可研究 / 可小仓试探) 才标 🆕 (避免淹没)
                if x.get("tier") in ("可研究", "可小仓试探"):
                    x["diff_flag"] = "new"
                    counts["new"] += 1
                else:
                    x["diff_flag"] = None
                x["prev_tier"] = None
                x["prev_percentile"] = None
                continue
            x["prev_tier"] = prev.get("tier")
            x["prev_percentile"] = prev.get("percentile")
            today_rank = TIER_RANK.get(x.get("tier"), 0)
            prev_rank = TIER_RANK.get(prev.get("tier"), 0)
            if today_rank > prev_rank:
                x["diff_flag"] = "upgraded"
                counts["upgraded"] += 1
            elif today_rank < prev_rank:
                x["diff_flag"] = "downgraded"
                counts["downgraded"] += 1
            else:
                today_pct = x.get("percentile")
                prev_pct = prev.get("percentile")
                if today_pct is not None and prev_pct is not None:
                    if today_pct < prev_pct - 5:
                        x["diff_flag"] = "jumped"
                        counts["jumped"] += 1
                    elif today_pct > prev_pct + 5:
                        x["diff_flag"] = "slipped"
                        counts["slipped"] += 1
                    else:
                        x["diff_flag"] = None
                else:
                    x["diff_flag"] = None
    # 掉出统计 (prev 里有今天没有)
    counts["exited"] = sum(1 for k in prev_idx.keys() if k not in seen_keys)
    return counts


def _build_audit_card(item: dict, market: str) -> dict | None:
    """step 5a: 给非"不碰"项生成买前审查卡 — why_bottom_like / whats_missing / stop_loss_hint。

    only 🟢 可小仓试探 / 🟡 可研究 才挂卡片（只观察的不细化研究价值）。
    """
    tier = item.get("tier")
    if tier in (None, "不碰", "只观察"):
        return None

    # why like-bottom: 把触底分的主因翻译成人话
    why = []
    vs_issue = item.get("vs_issue_pct")
    if vs_issue is not None:
        if vs_issue < -50:
            why.append(f"深度破发 {abs(vs_issue):.0f}%")
        elif vs_issue < -20:
            why.append(f"破发 {abs(vs_issue):.0f}%")
        elif vs_issue < 0:
            why.append(f"已破发 {abs(vs_issue):.0f}%")
    pct = item.get("percentile")
    if pct is not None:
        why.append(f"触底分前 {pct}%")
    months = item.get("months_listed")
    if market == "us":
        dtl = item.get("days_to_est_lockup")
        if dtl is not None and dtl < -30:
            why.append(f"过 lockup 估算 {abs(dtl)} 天")
        dd = item.get("drawdown_pct")
        if dd is not None and dd < -50:
            why.append(f"较 IPO 高点 {dd:.0f}%")
    else:
        if months and months >= 18:
            why.append(f"上市 {months:.0f} 月过解禁压力期")

    # what's missing: 距升档差什么 (统一 — gap #1 后 CN/US 都有 readiness)
    missing = []
    r = item.get("readiness_score") or 0
    if tier == "可研究":
        if r < 60:
            missing.append(f"准备度 {r:.0f} (需 60+，差 {60 - r:.0f})")
    ma20_diff = item.get("price_vs_ma20_pct")
    if ma20_diff is not None and ma20_diff < 0:
        missing.append(f"现价低于 MA20 {abs(ma20_diff):.0f}% (等突破)")
    low_hold = item.get("low_not_new_pct")
    if low_hold is not None and low_hold < 3:
        missing.append("低点未抬升 (仍在或紧贴全期最低)")
    vol_ratio = item.get("vol_ratio_30d")
    if vol_ratio is not None and vol_ratio < 1.2:
        missing.append(f"无放量 (近 30d 量 = 前 30d {vol_ratio}x)")
    if market == "cn" and months and months < 18:
        missing.append(f"仍在解禁窗口 (上市 {months:.0f} 月,等 ≥18 月)")
    # 60d 解禁早期预警（>=15% 才提，避免噪音；30d 已是红线则只观察那部分）
    u60 = item.get("unlock_60d_pct")
    if u60 is not None and u60 >= 15:
        u60_date = item.get("unlock_60d_date")
        date_part = f" ({u60_date})" if u60_date else ""
        missing.append(f"60 日解禁压力 {u60:.0f}% (30 日红线阈值 30%){date_part}")

    # stop loss hint: 跌破哪个位置代表"像底"假设被推翻
    stop = []
    cur_sym = "$" if market == "us" else "¥"
    low_30d = item.get("low_30d")
    low_since = item.get("low_since_ipo")
    if low_30d is not None:
        stop.append(f"近 30 日最低 {cur_sym}{low_30d:.2f}")
    if low_since is not None:
        stop.append(f"全期最低 {cur_sym}{low_since:.2f} (跌破推翻假设)")
    if not stop and market == "cn":
        first_close = item.get("first_close")
        if first_close:
            stop.append(f"首日收盘 ¥{first_close} 是上市初熔断价")

    return {
        "why_bottom_like": " · ".join(why) if why else None,
        "whats_missing": " · ".join(missing) if missing else "三层信号齐全,可正式进入买前研究",
        "stop_loss_hint": " · ".join(stop) if stop else None,
    }


def _assign_tier(items: list[dict], market: str) -> None:
    """给 items inplace 加 tier 字段。前提：red_lines + verdict + percentile 已就绪。

    四档：不碰 / 只观察 / 可研究 / 可小仓试探

    升档条件（step 4: 改用 readiness_score 替代 rebound_pct）：
      - 不碰         : verdict == "不碰" (红线触发)
      - 可小仓试探   : 触底分 percentile <= 10 AND 过解禁窗口 AND 买入准备度 >= 60
      - 可研究       : 触底分 percentile <= 30 AND 过解禁窗口
      - 只观察       : 其它通过红线的

    "过解禁窗口"：
      A 股 - months_listed >= 18
      美股 - days_to_est_lockup < -30
    "买入准备度"：
      美股 - readiness_score (流动性+反弹+均线+低点抬升+放量+池子) 已算好
      A 股 - 暂无日 K 数据,readiness_score 为 None,不达 60 阈值 (step 4.5 接 akshare K 线后开放)
    """
    for x in items:
        if x.get("verdict") == "不碰":
            x["tier"] = "不碰"
            continue
        pct = x.get("percentile")
        if pct is None:
            x["tier"] = "只观察"
            continue

        if market == "cn":
            past_window = (x.get("months_listed") or 0) >= 18
            r = x.get("readiness_score")
            readiness_ok = (r is not None and r >= 60)  # gap #1 后 CN 也有 readiness
        else:
            dtl = x.get("days_to_est_lockup")
            past_window = (dtl is not None and dtl < -30)
            r = x.get("readiness_score")
            readiness_ok = (r is not None and r >= 60)

        if pct <= 10 and past_window and readiness_ok:
            x["tier"] = "可小仓试探"
        elif pct <= 30 and past_window:
            x["tier"] = "可研究"
        else:
            x["tier"] = "只观察"
        # step 5a: 买前审查卡 (只给 🟢/🟡 挂)
        x["audit_card"] = _build_audit_card(x, market)


def _load_cn_industry_cache() -> dict[str, str]:
    """读 code → industry 缓存。整体 TTL 7 天，过期就丢全部重拉。"""
    if not CN_INDUSTRY_CACHE.exists():
        return {}
    try:
        payload = json.loads(CN_INDUSTRY_CACHE.read_text(encoding="utf-8"))
        saved_at = datetime.fromisoformat(payload.get("saved_at", "1970-01-01"))
        if datetime.now() - saved_at > timedelta(days=CN_INDUSTRY_TTL_DAYS):
            return {}
        entries = payload.get("entries") or {}
        return {str(k): str(v) for k, v in entries.items() if v}
    except Exception:
        return {}


def _save_cn_industry_cache(entries: dict[str, str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "entries": entries,
    }
    CN_INDUSTRY_CACHE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _strip_sw_roman(s: str) -> str:
    """申万行业名有"Ⅰ/Ⅱ/Ⅲ"罗马数字后缀，对用户无意义，剥掉。"""
    return (s or "").rstrip("ⅠⅡⅢⅣⅤ").strip()


# gap #1: A 股日 K 批拉 (baostock) — 算 MA20/MA50/低点抬升/放量/反弹/趋势
CN_JUNIOR_PRICE_CACHE = CACHE_DIR / "cn_junior_prices.json"
CN_JUNIOR_PRICE_TTL_HOURS = 24


def _bs_secid(code: str) -> str | None:
    """A 股代码加 baostock 前缀。

    覆盖:
      sh — 600/601/603/605 主板 + 688 科创板
      sz — 000/001/002/003 主板 + 300/301 创业板
    不支持:
      北交所 (8xxxxx, 92xxxx, 43xxxx) — baostock 不收录,返回 None 留作数据源缺口
    """
    c = _norm6(code)
    if not c:
        return None
    if c.startswith(("600", "601", "603", "605", "688", "689")):
        return f"sh.{c}"
    if c.startswith(("000", "001", "002", "003", "300", "301")):
        return f"sz.{c}"
    # 北交所(8/92/43) baostock 不支持,跳过
    return None


def _batch_baostock_history(codes: list[str], list_dates: dict[str, str],
                             period_back_days: int = 730) -> dict[str, dict]:
    """baostock 顺序拉每只 A 股 K 线,算和美股版同名的派生指标。

    返回 {code: {price, low_since_ipo, low_date, high_since_ipo, low_30d,
                  ma20, ma50, trend_30d_pct, vol_ratio_30d, avg_volume_30d}}
    """
    if not codes:
        return {}
    try:
        import baostock as bs
        import pandas as pd
    except ImportError:
        logger.warning("baostock/pandas not installed; skip CN history")
        return {}

    today = date.today()
    start = (today - timedelta(days=period_back_days)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    lg = bs.login()
    if lg.error_code != "0":
        logger.warning("baostock login failed: %s", lg.error_msg)
        return {}

    out: dict[str, dict] = {}
    failed = 0
    try:
        for code in codes:
            sid = _bs_secid(code)
            if not sid:
                continue
            try:
                rs = bs.query_history_k_data_plus(
                    sid, "date,close,low,high,volume",
                    start_date=start, end_date=end, frequency="d", adjustflag="2",
                )
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
                if not rows:
                    out[code] = {"price": None}
                    continue
                df = pd.DataFrame(rows, columns=["date", "close", "low", "high", "volume"])
                df["close"] = pd.to_numeric(df["close"], errors="coerce")
                df["low"] = pd.to_numeric(df["low"], errors="coerce")
                df["high"] = pd.to_numeric(df["high"], errors="coerce")
                df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
                df = df.dropna(subset=["close"])
                # 过滤上市前
                ls = list_dates.get(code)
                if ls:
                    df = df[df["date"] >= ls]
                if df.empty:
                    out[code] = {"price": None}
                    continue
                close = df["close"]
                low = float(df["low"].min())
                high = float(df["high"].max())
                cur = float(close.iloc[-1])
                low_idx = df["low"].idxmin()
                low_date = df.loc[low_idx, "date"] if low_idx in df.index else None
                avg_vol = int(df["volume"].tail(30).mean()) if len(df) >= 5 else None
                ma20 = float(close.tail(20).mean()) if len(close) >= 20 else None
                ma50 = float(close.tail(50).mean()) if len(close) >= 50 else None
                low_30d = float(df["low"].tail(30).min()) if len(df) >= 5 else None
                trend_30d_pct = None
                if len(close) >= 30:
                    c30 = float(close.iloc[-30])
                    if c30 > 0:
                        trend_30d_pct = (cur - c30) / c30 * 100.0
                vol_ratio_30d = None
                if len(df) >= 60:
                    vr = float(df["volume"].tail(30).mean())
                    vp = float(df["volume"].iloc[-60:-30].mean())
                    if vp > 0:
                        vol_ratio_30d = round(vr / vp, 2)
                out[code] = {
                    "price": cur,
                    "date": df["date"].iloc[-1],
                    "low_since_ipo": low,
                    "low_date": low_date,
                    "high_since_ipo": high,
                    "avg_volume_30d": avg_vol,
                    "ma20": ma20,
                    "ma50": ma50,
                    "low_30d": low_30d,
                    "trend_30d_pct": trend_30d_pct,
                    "vol_ratio_30d": vol_ratio_30d,
                }
            except Exception as exc:
                failed += 1
                logger.debug("baostock %s failed: %s", code, exc)
                continue
    finally:
        bs.logout()
    if failed:
        logger.info("[CN history] baostock 拉取完成 (成功 %d / 失败 %d)", len(out), failed)
    return out


def _enrich_cn_history(items: list[dict]) -> None:
    """inplace 给 cn_junior_pool items 挂 K 线派生字段; 走 24h 缓存。"""
    if not items:
        return
    hist_cache = _load_cache(CN_JUNIOR_PRICE_CACHE, CN_JUNIOR_PRICE_TTL_HOURS)
    cached = hist_cache.get("entries") or {}
    codes_need = [it["code"] for it in items
                  if it.get("code") and it["code"] not in cached]
    if codes_need:
        list_dates = {it["code"]: it.get("list_date") for it in items if it.get("code")}
        logger.info("[CN history] baostock 顺序拉 %d 只 (缓存 %d)", len(codes_need), len(cached))
        fresh = _batch_baostock_history(codes_need, list_dates)
        cached.update(fresh)
        for c in codes_need:
            if c not in cached:
                cached[c] = {"price": None}
        _save_cache(CN_JUNIOR_PRICE_CACHE, cached)
    else:
        logger.info("[CN history] 缓存命中 (%d 只)", len(items))
    # 把 K 线派生挂回 items
    for it in items:
        h = cached.get(it.get("code") or "") or {}
        for k in ("ma20", "ma50", "low_30d", "trend_30d_pct", "vol_ratio_30d",
                  "avg_volume_30d", "low_since_ipo", "high_since_ipo"):
            v = h.get(k)
            if v is not None:
                it[k] = v
        # rebound_pct = 现价 vs low_since_ipo
        cur = it.get("current_price")
        low = h.get("low_since_ipo")
        if cur and low and low > 0:
            it["rebound_pct"] = round((cur - low) / low * 100.0, 1)
        # drawdown_pct = 现价 vs high_since_ipo
        high = h.get("high_since_ipo")
        if cur and high and high > 0:
            it["drawdown_pct"] = round((cur - high) / high * 100.0, 1)
        # vs MA20 / MA50
        ma20 = h.get("ma20")
        if cur and ma20 and ma20 > 0:
            it["price_vs_ma20_pct"] = round((cur - ma20) / ma20 * 100.0, 1)
        ma50 = h.get("ma50")
        if cur and ma50 and ma50 > 0:
            it["price_vs_ma50_pct"] = round((cur - ma50) / ma50 * 100.0, 1)
        # 低点抬升
        low_30d = h.get("low_30d")
        if low_30d and low and low > 0:
            it["low_not_new_pct"] = round((low_30d - low) / low * 100.0, 1)


def _compute_cn_readiness(items: list[dict], holdings: set[str], watchlist: set[str]) -> None:
    """给 A 股 items 算 readiness_score (同 US 公式 100 制)。前提:_enrich_cn_history 已挂字段。"""
    for it in items:
        code = it.get("code") or ""
        cur = it.get("current_price")
        avg_vol = it.get("avg_volume_30d")
        # 流动性 (25): A 股用日均成交额 (¥, 元为单位 → 转万)
        # avg_vol 是股数,需 * 价格 = 成交额。¥3000万+/日=满分, ¥1000万=18, ¥500万=10
        dollar_vol = (avg_vol * cur) if (avg_vol and cur) else None
        if dollar_vol and dollar_vol >= 30_000_000:
            s_liquid = 25.0
        elif dollar_vol and dollar_vol >= 10_000_000:
            s_liquid = 18.0
        elif dollar_vol and dollar_vol >= 3_000_000:
            s_liquid = 10.0
        else:
            s_liquid = 0.0
        # rebound (25)
        r = it.get("rebound_pct")
        if r is None:
            s_rebound = 0.0
        elif 20 <= r <= 60:
            s_rebound = 25.0
        elif 10 <= r < 20 or 60 < r <= 100:
            s_rebound = 15.0
        else:
            s_rebound = 0.0
        # ma20 (20)
        ma_diff = it.get("price_vs_ma20_pct")
        if ma_diff is None:
            s_ma = 0.0
        elif ma_diff >= 5:
            s_ma = 20.0
        elif ma_diff >= 0:
            s_ma = 14.0
        elif ma_diff >= -5:
            s_ma = 7.0
        else:
            s_ma = 0.0
        # low_not_new (15)
        lnn = it.get("low_not_new_pct")
        if lnn is None:
            s_low = 0.0
        elif lnn >= 20:
            s_low = 15.0
        elif lnn >= 10:
            s_low = 10.0
        elif lnn >= 3:
            s_low = 5.0
        else:
            s_low = 0.0
        # vol_ratio (10)
        vr = it.get("vol_ratio_30d")
        if vr is None:
            s_vol = 0.0
        elif vr >= 1.5:
            s_vol = 10.0
        elif vr >= 1.2:
            s_vol = 6.0
        elif vr >= 0.8:
            s_vol = 2.0
        else:
            s_vol = 0.0
        # in_your_pool (5)
        s_pool = 5.0 if (code in holdings or code in watchlist) else 0.0
        it["readiness_score"] = round(s_liquid + s_rebound + s_ma + s_low + s_vol + s_pool, 1)
        # breakdown 写到现有 score_breakdown
        sb = it.get("score_breakdown") or {}
        sb.update({
            "readiness_liquidity": round(s_liquid, 1),
            "readiness_rebound": round(s_rebound, 1),
            "readiness_ma20": round(s_ma, 1),
            "readiness_low_hold": round(s_low, 1),
            "readiness_volume": round(s_vol, 1),
            "readiness_pool": round(s_pool, 1),
        })
        it["score_breakdown"] = sb
        # dollar_volume 也存便于 dashboard 显示
        if dollar_vol is not None:
            it["dollar_volume_30d"] = int(dollar_vol)


def _enrich_cn_industry(items: list[dict]) -> None:
    """给 A 股 items inplace 补 industry 字段（申万一级 / 二级）。

    数据源：akshare stock_industry_change_cninfo（巨潮接口，含 4 套行业分类）。
    选申万分类（分类标准编码 008003），展示"门类 / 次类" = SW1 / SW2，
    例："电力设备 / 其他电源设备"、"基础化工 / 农化制品"。

    实测：~0.5s/只，159 只首次约 80s；命中 7 天缓存后秒级。
    fail-soft：单只失败不抛异常；连续 5 次失败提前终止避免拖慢主流程。
    """
    ak = _import_ak()
    if ak is None:
        return
    cache = _load_cn_industry_cache()
    # 先用缓存填
    for it in items:
        code = it.get("code")
        if code and not it.get("industry") and code in cache:
            it["industry"] = cache[code]
    # 找还缺的
    missing = [it for it in items if not it.get("industry") and it.get("code")]
    if not missing:
        logger.info("[CN industry] 全部命中缓存（%d 只）", len(items))
        return
    import time as _time
    today = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=730)).strftime("%Y%m%d")  # 回看 2 年内的分类变更
    fetched = 0
    failed = 0
    consec_fail = 0
    for idx, it in enumerate(missing):
        code = it["code"]
        try:
            df = ak.stock_industry_change_cninfo(symbol=code, start_date=start, end_date=today)
            sw = df[df["分类标准编码"] == "008003"]  # 申银万国行业分类标准
            if not sw.empty:
                r = sw.iloc[-1]
                sw1 = _strip_sw_roman(str(r.get("行业门类") or ""))
                sw2 = _strip_sw_roman(str(r.get("行业次类") or ""))
                industry = f"{sw1} / {sw2}" if sw1 and sw2 else (sw1 or sw2)
                if industry:
                    it["industry"] = industry
                    cache[code] = industry
                    fetched += 1
                    consec_fail = 0
            _time.sleep(0.2)
        except Exception:
            failed += 1
            consec_fail += 1
            if consec_fail >= 5:
                logger.warning("[CN industry] 连续失败 5 次，cninfo 可能限流，跳过剩余 %d 只", len(missing) - idx - 1)
                break
    if fetched > 0:
        _save_cn_industry_cache(cache)
    logger.info("[CN industry] 申万分类补全 %d 只 (失败 %d / 缓存累计 %d / 池子总 %d)",
                fetched, failed, len(cache), len(items))


def fetch_cn_unlock_radar(holdings: set[str], watchlist: set[str], horizon_days: int = 90) -> list[dict]:
    """A 股未来 horizon_days 内个股解禁明细，按"解禁压力"排序。

    压力分 = 占流通市值比例(0..1) × 80 + log10(市值亿/1) × 5 + 10，封顶 100。
    """
    ak = _import_ak()
    if ak is None:
        return []
    today = date.today()
    end = today + timedelta(days=horizon_days)
    try:
        df = ak.stock_restricted_release_detail_em(
            start_date=today.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    except Exception as e:
        logger.warning("stock_restricted_release_detail_em failed: %s", e)
        return []
    if df is None or df.empty:
        return []

    out: list[dict] = []
    for _, r in df.iterrows():
        code = _norm6(r.get("股票代码"))
        if not code:
            continue
        unlock_date = _to_iso(r.get("解禁时间"))
        if not unlock_date:
            continue
        try:
            d_days = (datetime.fromisoformat(unlock_date).date() - today).days
        except Exception:
            d_days = 0
        if d_days < 0 or d_days > horizon_days:
            continue
        market_value = _safe_float(r.get("实际解禁市值")) or 0.0
        pct_float = _safe_float(r.get("占解禁前流通市值比例")) or 0.0
        if pct_float > 1.5:
            pct_float = pct_float / 100.0

        mv_yi = market_value / 1e8 if market_value > 0 else 0.0
        log_term = math.log10(max(mv_yi, 0.1)) * 5
        stress = min(100.0, max(0.0, pct_float * 80.0 + log_term + 10.0))

        out.append({
            "code": code,
            "name": str(r.get("股票简称") or ""),
            "board": _board_of_cn(code),
            "unlock_date": unlock_date,
            "days_to_unlock": d_days,
            "market_value_yi": round(mv_yi, 2),
            "pct_of_float": round(pct_float * 100.0, 2),
            "stress_score": round(stress, 1),
            "category": str(r.get("限售股类型") or ""),
            "pre_price": _safe_float(r.get("解禁前一交易日收盘价")),
            "in_holdings": code in holdings,
            "in_watchlist": code in watchlist,
        })
    out.sort(key=lambda x: (-x["stress_score"], x["days_to_unlock"]))
    return out


def fetch_cn_junior_pool(holdings: set[str], watchlist: set[str],
                          months_min: int = 6, months_max: int = 24) -> list[dict]:
    """A 股次新股底部打分（4 维：折发行价 / 时间衰减 / 首日溢价 / 较首日跌幅）。"""
    ak = _import_ak()
    if ak is None:
        return []
    try:
        df = ak.stock_xgsr_ths()
    except Exception as e:
        logger.warning("stock_xgsr_ths failed: %s", e)
        return []
    if df is None or df.empty:
        return []

    today = date.today()
    out: list[dict] = []
    for _, r in df.iterrows():
        code = _norm6(r.get("股票代码"))
        if not code:
            continue
        list_date_str = _to_iso(r.get("上市日期"))
        if not list_date_str:
            continue
        try:
            list_date = datetime.fromisoformat(list_date_str).date()
        except Exception:
            continue
        days_listed = (today - list_date).days
        months_listed = days_listed / 30.4
        if months_listed < months_min or months_listed > months_max:
            continue

        issue_price = _safe_float(r.get("发行价"))
        current_price = _safe_float(r.get("最新价"))
        first_close = _safe_float(r.get("首日收盘价"))
        first_chg = _safe_float(r.get("首日涨跌幅"))
        broken = str(r.get("是否破发") or "").strip() in {"是", "Y", "true", "True"}
        if issue_price is None or current_price is None or issue_price <= 0:
            continue

        vs_issue_pct = (current_price - issue_price) / issue_price * 100.0
        vs_first_close_pct = None
        if first_close and first_close > 0:
            vs_first_close_pct = (current_price - first_close) / first_close * 100.0

        s_discount = min(25.0, abs(vs_issue_pct) / 2.0) if vs_issue_pct <= 0 else 0.0
        if 12 <= months_listed <= 18:
            s_time = 25.0
        elif 9 <= months_listed < 12 or 18 < months_listed <= 21:
            s_time = 20.0
        elif 6 <= months_listed < 9 or 21 < months_listed <= 24:
            s_time = 15.0
        else:
            s_time = 10.0
        if first_chg is None or first_chg <= 0:
            s_first = 0.0
        elif first_chg >= 200:
            s_first = 25.0
        elif first_chg >= 100:
            s_first = 20.0
        elif first_chg >= 50:
            s_first = 15.0
        else:
            s_first = 8.0
        s_vs_first = min(25.0, abs(vs_first_close_pct) / 3.0) if (vs_first_close_pct is not None and vs_first_close_pct < 0) else 0.0

        total = round(s_discount + s_time + s_first + s_vs_first, 1)
        tags = []
        if broken or vs_issue_pct < 0:
            tags.append("已破发")
        if 12 <= months_listed <= 18:
            tags.append("首发解禁窗口")
        if first_chg and first_chg >= 100:
            tags.append("首日爆炒")
        if vs_first_close_pct is not None and vs_first_close_pct < -50:
            tags.append("较首日腰斩")

        summary = _cn_junior_summary(months_listed, vs_issue_pct, vs_first_close_pct, first_chg)

        out.append({
            "code": code,
            "name": str(r.get("股票简称") or ""),
            "board": _board_of_cn(code),
            "industry": "",  # TODO: 等 akshare 网络恢复 + 加 _enrich_cn_industry() 补
            "list_date": list_date_str,
            "months_listed": round(months_listed, 1),
            "issue_price": round(issue_price, 2),
            "current_price": round(current_price, 2),
            "vs_issue_pct": round(vs_issue_pct, 1),
            "first_day_change_pct": round(first_chg, 1) if first_chg is not None else None,
            "first_close": round(first_close, 2) if first_close else None,
            "vs_first_close_pct": round(vs_first_close_pct, 1) if vs_first_close_pct is not None else None,
            "broken_issue": broken or vs_issue_pct < 0,
            "score": total,
            "score_breakdown": {
                "discount_to_issue": round(s_discount, 1),
                "time_decay": round(s_time, 1),
                "first_day_premium": round(s_first, 1),
                "vs_first_close": round(s_vs_first, 1),
            },
            "tags": tags,
            "summary": summary,
            "in_holdings": code in holdings,
            "in_watchlist": code in watchlist,
        })
    out.sort(key=lambda x: -x["score"])
    _attach_percentile(out)
    return out


def slim_cn_ipo_calendar(raw: dict) -> dict:
    """复用 ipo_calendar.json，精简前端字段。"""
    today = date.today()

    def _e(e: dict) -> dict:
        sub_d = e.get("subscribe_date")
        try:
            d_days = (datetime.fromisoformat(str(sub_d)[:10]).date() - today).days if sub_d else None
        except Exception:
            d_days = None
        return {
            "code": e.get("code"),
            "subscribe_code": e.get("subscribe_code"),
            "name": e.get("name"),
            "board": e.get("board"),
            "subscribe_date": sub_d,
            "listing_date": e.get("listing_date"),
            "issue_price": e.get("issue_price"),
            "pe_ratio": e.get("pe_ratio"),
            "industry": e.get("industry"),
            "theme": e.get("theme"),
            "ai_relevance": e.get("ai_relevance"),
            "days_to_subscribe": d_days,
        }

    return {
        "fetched_at": raw.get("fetched_at"),
        "fetch_status": raw.get("fetch_status"),
        "upcoming_subscription": [_e(x) for x in raw.get("upcoming_subscription") or []],
        "awaiting_listing": [_e(x) for x in raw.get("awaiting_listing") or []],
        "recently_listed": [_e(x) for x in (raw.get("recently_listed") or [])[:30]],
    }


# ═════════════════════════════════════════════════════
#  美股 (NASDAQ 公开 API + yfinance 批拉价格)
# ═════════════════════════════════════════════════════

US_IPO_UNIVERSE_CACHE = CACHE_DIR / "us_ipo_universe.json"
US_IPO_PRICE_CACHE = CACHE_DIR / "us_ipo_prices.json"
US_IPO_META_CACHE = CACHE_DIR / "us_ipo_meta.json"
US_IPO_PRICE_TTL_HOURS = 24
US_IPO_META_TTL_HOURS = 168  # 7 天 — sector/industry 几乎不变,market cap 不需要每天更新


def _is_spac(name: str, symbol: str, issue_price: float | None) -> bool:
    """SPAC 启发式判断:发行价正好 $10 + (名称含 Acquisition 或 ticker 后缀 U/WS)。"""
    nm = (name or "").upper()
    sym = (symbol or "").upper()
    if issue_price is not None and abs(issue_price - 10.0) < 0.01:
        if any(kw in nm for kw in ["ACQUISITION", "ACQUISTION", "SPAC"]):
            return True
        if sym.endswith(("U", "WS")) and len(sym) >= 4:
            return True
    return False


def _batch_yf_history(symbols: list[str], ipo_dates: dict[str, str],
                       batch: int = 100, period: str = "2y") -> dict[str, dict]:
    """yfinance 批拉历史 — 同时算 current_price / low_since_ipo / high_since_ipo / avg_volume_30d。

    ipo_dates: {SYM: 'YYYY-MM-DD'} — 用于过滤"上市前"的脏数据(yfinance 偶尔回填)。
    """
    if not symbols:
        return {}
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        logger.warning("yfinance/pandas not installed; skip US history refresh")
        return {}

    out: dict[str, dict] = {}
    for i in range(0, len(symbols), batch):
        chunk = symbols[i:i + batch]
        try:
            data = yf.download(chunk, period=period, progress=False, threads=True, auto_adjust=False)
        except Exception as e:
            logger.warning("yfinance batch %d-%d failed: %s", i, i + batch, e)
            continue
        if data is None or data.empty:
            continue
        # MultiIndex (level0=Field, level1=Ticker) vs flat
        is_multi = isinstance(data.columns, pd.MultiIndex)

        for sym in chunk:
            try:
                if is_multi:
                    if ("Close", sym) not in data.columns:
                        continue
                    close = data["Close"][sym].dropna()
                    volume = data["Volume"][sym].dropna() if ("Volume", sym) in data.columns else None
                else:
                    close = data["Close"].dropna() if "Close" in data.columns else None
                    volume = data["Volume"].dropna() if "Volume" in data.columns else None
                if close is None or close.empty:
                    continue
                # 过滤上市前的脏数据
                ipo_s = ipo_dates.get(sym)
                if ipo_s:
                    try:
                        ipo_d = datetime.fromisoformat(ipo_s).date()
                        close = close[close.index.date >= ipo_d]
                        if volume is not None:
                            volume = volume[volume.index.date >= ipo_d]
                    except Exception:
                        pass
                if close.empty:
                    continue
                low = float(close.min())
                high = float(close.max())
                cur = float(close.iloc[-1])
                low_idx = close.idxmin()
                low_date = str(low_idx.date()) if hasattr(low_idx, "date") else None
                avg_vol = None
                if volume is not None and len(volume) >= 5:
                    avg_vol = int(volume.tail(30).mean())
                # step 4: 技术确认指标
                ma20 = float(close.tail(20).mean()) if len(close) >= 20 else None
                ma50 = float(close.tail(50).mean()) if len(close) >= 50 else None
                low_30d = float(close.tail(30).min()) if len(close) >= 5 else None
                # 近 30 日相对于 30 日前的总体趋势 (正 = 抬升)
                if len(close) >= 30:
                    close_30d_ago = float(close.iloc[-30])
                    trend_30d_pct = ((cur - close_30d_ago) / close_30d_ago * 100.0) if close_30d_ago > 0 else None
                else:
                    trend_30d_pct = None
                # 近 30 日成交量 vs 之前的均量比 (放量信号)
                vol_ratio_30d = None
                if volume is not None and len(volume) >= 60:
                    vol_recent = float(volume.tail(30).mean())
                    vol_prev = float(volume.iloc[-60:-30].mean())
                    if vol_prev > 0:
                        vol_ratio_30d = round(vol_recent / vol_prev, 2)
                out[sym.upper()] = {
                    "price": cur,
                    "date": str(close.index[-1].date()),
                    "low_since_ipo": low,
                    "low_date": low_date,
                    "high_since_ipo": high,
                    "avg_volume_30d": avg_vol,
                    "ma20": ma20,
                    "ma50": ma50,
                    "low_30d": low_30d,
                    "trend_30d_pct": trend_30d_pct,
                    "vol_ratio_30d": vol_ratio_30d,
                }
            except Exception as e:
                logger.debug("history parse %s failed: %s", sym, e)
                continue
    return out


def _batch_yf_info(symbols: list[str], max_workers: int = 8) -> dict[str, dict]:
    """yfinance Ticker.info 拿 sector / industry / marketCap — 并行,因为单只 ~1s。"""
    if not symbols:
        return {}
    try:
        import yfinance as yf
        from concurrent.futures import ThreadPoolExecutor, as_completed
    except ImportError:
        return {}
    out: dict[str, dict] = {}

    def _one(sym: str) -> tuple[str, dict] | None:
        try:
            t = yf.Ticker(sym)
            info = t.info or {}
            return sym.upper(), {
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "market_cap": info.get("marketCap"),  # raw USD
                "long_name": info.get("longName"),
            }
        except Exception as e:
            logger.debug("info %s failed: %s", sym, e)
            return None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for fut in as_completed([pool.submit(_one, s) for s in symbols]):
            r = fut.result()
            if r:
                out[r[0]] = r[1]
    return out


def _load_cache(path: Path, ttl_hours: int) -> dict:
    if not path.exists():
        return {"fetched_at": None, "entries": {}}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if d.get("fetched_at"):
            age_h = (datetime.now() - datetime.fromisoformat(d["fetched_at"])).total_seconds() / 3600
            if age_h > ttl_hours:
                return {"fetched_at": None, "entries": {}}
        return d
    except Exception:
        return {"fetched_at": None, "entries": {}}


def _save_cache(path: Path, entries: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"fetched_at": datetime.now().isoformat(timespec="seconds"), "entries": entries}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _enrich_with_price(items: list[dict], hist_cache: dict, meta_cache: dict) -> list[dict]:
    """给 IPO 日历条目附上 current_price / vs_issue / sector / market_cap (如果有缓存)。"""
    out = []
    for e in items:
        sym = (e.get("symbol") or "").upper()
        h = hist_cache.get(sym) or {}
        m = meta_cache.get(sym) or {}
        cur = _safe_float(h.get("price"))
        issue = _safe_float(e.get("issue_price"))
        vs_issue = None
        if cur and issue and issue > 0:
            vs_issue = round((cur - issue) / issue * 100.0, 1)
        out.append({
            **e,
            "current_price": round(cur, 2) if cur else None,
            "vs_issue_pct": vs_issue,
            "sector": _zh_us_sector(m.get("sector")),
            "industry": _zh_us_industry(m.get("industry")),
            "market_cap_m": round(m.get("market_cap") / 1e6, 1) if m.get("market_cap") else None,
        })
    return out


def build_us_ipo_calendar(nasdaq_window: dict) -> dict:
    """从 NASDAQ window 构造 dashboard 三栏:
       - upcoming_filing  (即将申购): filed 列表,近 60 天内
       - awaiting_listing (已申购未上市): priced 列表,priced_date 在过去 7 天内
       - recently_listed  (近 30 日上市): priced 列表,priced_date 在 8-30 天前
       0-7 天已在 awaiting_listing 展示；这里排除，避免同一 IPO 在两栏重复出现。
    """
    today = date.today()
    priced = nasdaq_window.get("priced") or []
    filed = nasdaq_window.get("filed") or []

    def _delta_days(s: str | None) -> int | None:
        if not s:
            return None
        try:
            return (today - datetime.fromisoformat(s).date()).days
        except Exception:
            return None

    upcoming_filing = []
    for f in filed:
        d = _delta_days(f.get("filed_date"))
        if d is None or d > 60:
            continue
        if d < 0:
            continue  # 不应出现 (filed 日期未来),但防御
        upcoming_filing.append({**f, "days_since_filed": d})
    upcoming_filing.sort(key=lambda x: x.get("days_since_filed", 999))

    awaiting = []
    recently = []
    for p in priced:
        d = _delta_days(p.get("priced_date"))
        if d is None:
            continue
        item = {**p, "days_since_priced": d}
        if 0 <= d <= 7:
            awaiting.append(item)
        if 8 <= d <= 30:
            recently.append(item)
    awaiting.sort(key=lambda x: x.get("days_since_priced", 999))
    recently.sort(key=lambda x: x.get("days_since_priced", 999))

    return {
        "upcoming_filing": upcoming_filing[:30],
        "awaiting_listing": awaiting[:30],
        "recently_listed": recently[:50],
    }


# 美股 lockup 估算 (step 2)
# SEC Investor.gov 提示 IPO lock-up 常见 180 天 — 实际范围 90-365 天，
# 真实值在 S-1 招股书 (付费源解析)。这里用 180 天默认估算，明确标 is_estimated=True。
US_LOCKUP_DEFAULT_DAYS = 180


# gap #3: 美股基本面软过滤 — yfinance financials 拿毛利率 + 现金 runway
US_FINANCIALS_CACHE = CACHE_DIR / "us_financials.json"
US_FINANCIALS_TTL_HOURS = 168  # 7 天 (季报频率)


def _fetch_yf_financials_one(symbol: str) -> dict | None:
    """单只: 取 TTM 毛利率, 现金 runway (季度), 收入 YoY。
    yfinance 数据稀疏,缺数据返回 None 单字段; 整体出错返回 None。
    """
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        info = t.info or {}
        # 毛利率 (TTM, yfinance 已算好)
        gross_margin = info.get("grossMargins")  # 0-1 区间
        # 收入增长
        rev_growth = info.get("revenueGrowth")  # YoY, 0-1 区间
        # 现金 runway 估算: 总现金 / 季度运营烧
        total_cash = info.get("totalCash")  # 美元
        # operatingCashflow 是 TTM (12 月), 季度 burn ≈ TTM / 4 (负数=烧)
        op_cf = info.get("operatingCashflow")
        runway_quarters = None
        if total_cash is not None and op_cf is not None and op_cf < 0:
            q_burn = abs(op_cf) / 4
            if q_burn > 0:
                runway_quarters = round(total_cash / q_burn, 1)
        return {
            "gross_margin": round(gross_margin, 3) if gross_margin is not None else None,
            "revenue_growth": round(rev_growth, 3) if rev_growth is not None else None,
            "total_cash_m": round(total_cash / 1e6, 1) if total_cash else None,
            "operating_cashflow_m": round(op_cf / 1e6, 1) if op_cf else None,
            "runway_quarters": runway_quarters,
        }
    except Exception as exc:
        logger.debug("[financials] %s failed: %s", symbol, exc)
        return None


def _enrich_us_financials(items: list[dict], max_workers: int = 6) -> None:
    """inplace 给 US items 挂 financials 字段。并行 + 7 天缓存。"""
    if not items:
        return
    cache = _load_cache(US_FINANCIALS_CACHE, US_FINANCIALS_TTL_HOURS)
    cached = cache.get("entries") or {}
    syms_need = [(it.get("symbol") or "").upper() for it in items
                 if it.get("symbol") and (it.get("symbol") or "").upper() not in cached]
    if syms_need:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        logger.info("[financials] yfinance 并行拉 %d 只 (缓存 %d)", len(syms_need), len(cached))
        ok = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = {pool.submit(_fetch_yf_financials_one, s): s for s in syms_need}
            for fut in as_completed(futs):
                sym = futs[fut]
                res = fut.result()
                if res is not None:
                    cached[sym] = res
                    ok += 1
                else:
                    cached[sym] = {"fetch_failed": True}
        _save_cache(US_FINANCIALS_CACHE, cached)
        logger.info("[financials] 拉取完成 (成功 %d / %d)", ok, len(syms_need))
    else:
        logger.info("[financials] 缓存命中 (%d 只)", len(items))
    for it in items:
        sym = (it.get("symbol") or "").upper()
        info = cached.get(sym) or {}
        it["financials"] = {
            "gross_margin": info.get("gross_margin"),
            "revenue_growth": info.get("revenue_growth"),
            "runway_quarters": info.get("runway_quarters"),
            "total_cash_m": info.get("total_cash_m"),
        }


# gap #2: SEC EDGAR 增发频率检测
# 数据源: https://data.sec.gov (公开,不需 key,但需 User-Agent 标识)
# Ticker → CIK 映射: https://www.sec.gov/files/company_tickers.json
# 每只 CIK 的近期 filings: https://data.sec.gov/submissions/CIK{cik:010d}.json
SEC_TICKER_CIK_CACHE = CACHE_DIR / "us_sec_ticker_cik.json"
SEC_FILINGS_CACHE = CACHE_DIR / "us_sec_filings.json"
SEC_TICKER_CIK_TTL_HOURS = 168  # 7 天 (CIK 映射几乎不变)
SEC_FILINGS_TTL_HOURS = 24
SEC_USER_AGENT = "StockAssistant Research lance7in@gmail.com"
# 增发 / 稀释相关 form type (公司发行新股)
DILUTION_FORMS = {"S-3", "S-3ASR", "S-3/A", "424B2", "424B3", "424B5", "S-1/A"}


def _fetch_sec_ticker_to_cik(force: bool = False) -> dict[str, str]:
    """返回 {ticker_upper: cik_str (10-digit zero-padded)}。缓存 7 天。"""
    if not force:
        cache = _load_cache(SEC_TICKER_CIK_CACHE, SEC_TICKER_CIK_TTL_HOURS)
        entries = cache.get("entries") or {}
        if entries:
            return entries
    try:
        import requests
        url = "https://www.sec.gov/files/company_tickers.json"
        r = requests.get(url, headers={"User-Agent": SEC_USER_AGENT}, timeout=15)
        r.raise_for_status()
        raw = r.json()
        # raw 是 {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
        out = {}
        for v in raw.values():
            t = (v.get("ticker") or "").upper()
            c = v.get("cik_str")
            if t and c is not None:
                out[t] = str(c).zfill(10)
        _save_cache(SEC_TICKER_CIK_CACHE, out)
        logger.info("[SEC] ticker→CIK 映射拉取完成 %d 只", len(out))
        return out
    except Exception as exc:
        logger.warning("[SEC] ticker→CIK 拉取失败: %s", exc)
        cache = _load_cache(SEC_TICKER_CIK_CACHE, 24 * 365)  # fall back to old cache 即使过期
        return cache.get("entries") or {}


def _fetch_sec_filings_for_symbol(symbol: str, cik: str) -> dict | None:
    """拉 SEC EDGAR 单 CIK 近期 filings, 返回精简结果。

    返回: {"dilution_filings_180d": [{"form": "S-3", "date": "2026-04-15"}, ...]}
    """
    try:
        import requests
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        r = requests.get(url, headers={"User-Agent": SEC_USER_AGENT}, timeout=15)
        if r.status_code == 404:
            return {"dilution_filings_180d": []}
        r.raise_for_status()
        d = r.json()
        recent = (d.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        cutoff = (date.today() - timedelta(days=180)).isoformat()
        out = []
        for f, dt in zip(forms, dates):
            if f in DILUTION_FORMS and dt >= cutoff:
                out.append({"form": f, "date": dt})
        return {"dilution_filings_180d": out}
    except Exception as exc:
        logger.debug("[SEC] %s (CIK %s) failed: %s", symbol, cik, exc)
        return None


def _enrich_us_sec_filings(items: list[dict]) -> None:
    """inplace 给 US junior_pool items 挂 dilution_filings_180d 字段; 24h 缓存。

    rate limit: SEC 限 10 req/s, 我们 sleep 0.12s 安全。
    """
    if not items:
        return
    ticker_to_cik = _fetch_sec_ticker_to_cik()
    if not ticker_to_cik:
        logger.warning("[SEC] ticker→CIK 映射为空,跳过 SEC 富集")
        return
    cache = _load_cache(SEC_FILINGS_CACHE, SEC_FILINGS_TTL_HOURS)
    cached = cache.get("entries") or {}
    # 找需拉的
    syms_need = []
    for it in items:
        sym = (it.get("symbol") or "").upper()
        if not sym:
            continue
        if sym in cached:
            continue
        if sym not in ticker_to_cik:
            # 不在 SEC 表 (可能是 ADR / 非美国发行) → 标 not_in_sec
            cached[sym] = {"dilution_filings_180d": [], "not_in_sec": True}
            continue
        syms_need.append((sym, ticker_to_cik[sym]))
    if syms_need:
        import time as _time
        logger.info("[SEC] 顺序拉 %d 只 dilution filings (rate≈8/s)", len(syms_need))
        ok = 0
        for sym, cik in syms_need:
            res = _fetch_sec_filings_for_symbol(sym, cik)
            if res is not None:
                cached[sym] = res
                ok += 1
            else:
                cached[sym] = {"dilution_filings_180d": [], "fetch_failed": True}
            _time.sleep(0.12)
        _save_cache(SEC_FILINGS_CACHE, cached)
        logger.info("[SEC] 拉取完成 (成功 %d / %d)", ok, len(syms_need))
    else:
        logger.info("[SEC] dilution filings 缓存命中 (%d 只)", len(items))
    # 挂回 items
    for it in items:
        sym = (it.get("symbol") or "").upper()
        info = cached.get(sym) or {}
        it["dilution_filings_180d"] = info.get("dilution_filings_180d") or []
        it["sec_not_found"] = bool(info.get("not_in_sec"))


def _us_est_lockup_end(priced_date_s: str | None) -> tuple[str | None, int | None]:
    """返回 (估算 lockup 到期日 ISO, 距今天数)。距今为负数 = 已过期。"""
    if not priced_date_s:
        return None, None
    try:
        ipo_d = datetime.fromisoformat(str(priced_date_s)[:10]).date()
    except Exception:
        return None, None
    end = ipo_d + timedelta(days=US_LOCKUP_DEFAULT_DAYS)
    return end.isoformat(), (end - date.today()).days


def fetch_us_lockup_radar(nasdaq_priced: list[dict], holdings: set[str], watchlist: set[str],
                           horizon_days: int = 90) -> list[dict]:
    """美股 lockup 雷达 — 估算 (priced + 180 天)，覆盖未来 horizon_days 内即将解禁的 IPO。

    输入是 nasdaq_priced 24 月窗口；只挑 lockup 在 ±horizon_days 范围内的。
    本质上 = 上市 (180-90) ~ (180+90) 天 ≈ 3~9 月。
    """
    today = date.today()
    out: list[dict] = []
    for e in nasdaq_priced:
        sym = e.get("symbol")
        priced_date_s = e.get("priced_date")
        if not sym or not priced_date_s:
            continue
        # SPAC 的 lockup 规则不同 (绑 business combination 而非 priced+180),
        # 180 天估算对它们不适用；junior_pool 那边已有 "SPAC 空壳" 红线覆盖。
        if _is_spac(e.get("name") or "", sym, _safe_float(e.get("issue_price"))):
            continue
        end_iso, days_to = _us_est_lockup_end(priced_date_s)
        if end_iso is None or days_to is None:
            continue
        if not (0 <= days_to <= horizon_days):
            continue  # 已过期的不进雷达 (已在 junior_pool 红线侧覆盖)
        out.append({
            "symbol": sym,
            "name": e.get("name") or "",
            "exchange": e.get("exchange") or "",
            "ipo_date": priced_date_s,
            "issue_price": e.get("issue_price"),
            "est_lockup_end": end_iso,
            "days_to_unlock": days_to,
            "is_estimated": True,
            "in_holdings": sym in holdings,
            "in_watchlist": sym in watchlist,
        })
    out.sort(key=lambda x: x["days_to_unlock"])
    return out


def fetch_us_junior_pool(holdings: set[str], watchlist: set[str],
                          nasdaq_priced: list[dict],
                          months_min: int = 6, months_max: int = 24,
                          hard_min_price_usd: float = 0.3,
                          hard_min_market_cap_usd: float = 10_000_000) -> list[dict]:
    """美股次新股底部观察池 — 基于 NASDAQ 24 月 priced 列表。

    硬过滤 (彻底剔除,极端值才动手):
      - 现价 < $0.3              → yfinance 脏数据 / 已实际退市
      - 市值 < $10M              → 几乎不可交易
      - 上市 < 6 月 / > 24 月    → 不在"次新股"窗口
      - 完全无价                 → 无法分析

    其它问题不剔除,改为红线软标记 (red_lines 字段, step 1 落地):
      - SPAC / 仙股 (<$1) / 微盘 (<$50M) / 壳公司 / 低流动性
      → 用户能看到"哪些被打回 + 为啥",而不是默默丢弃

    打分维度 (5 维,总分 100):
      - discount_to_issue (35):  跌破发行价越深越高分
      - time_decay        (25):  上市 12-18 月最高
      - liquidity         (15):  日均成交额 (注: 是金额,不是股数,避免仙股偏差)
      - rebound_bonus     (15):  从最低反弹 20-60% (转折信号)
      - in_your_pool      (10):  你的持仓/自选股加成
    """
    today = date.today()
    # 窗口过滤 + 必须有 issue_price + 必须有 symbol。SPAC 不在此剔除,改为下面红线评估。
    candidates: list[dict] = []
    for e in nasdaq_priced:
        sym = e.get("symbol")
        if not sym:
            continue
        priced_date_s = e.get("priced_date")
        if not priced_date_s:
            continue
        try:
            ipo_d = datetime.fromisoformat(priced_date_s).date()
        except Exception:
            continue
        days_listed = (today - ipo_d).days
        months_listed = days_listed / 30.4
        if months_listed < months_min or months_listed > months_max:
            continue
        issue_price = _safe_float(e.get("issue_price"))
        if issue_price is None or issue_price <= 0:
            continue
        is_spac = _is_spac(e.get("name") or "", sym, issue_price)
        candidates.append({
            **e,
            "months_listed": months_listed,
            "days_listed": days_listed,
            "_is_spac": is_spac,
        })

    if not candidates:
        return []

    # 批拉历史 (yfinance) — 24h 缓存
    ipo_dates = {c["symbol"]: c.get("priced_date") for c in candidates}
    hist_cache = _load_cache(US_IPO_PRICE_CACHE, US_IPO_PRICE_TTL_HOURS)
    cached_hist = hist_cache.get("entries") or {}
    syms_need_hist = [c["symbol"] for c in candidates if c["symbol"] not in cached_hist]
    if syms_need_hist:
        logger.info("US 次新股池: %d 只需拉历史 (yfinance batch,缓存 %d)",
                    len(syms_need_hist), len(cached_hist))
        fresh = _batch_yf_history(syms_need_hist, ipo_dates, batch=100, period="2y")
        cached_hist.update(fresh)
        for sym in syms_need_hist:
            if sym not in cached_hist:
                cached_hist[sym] = {"price": None}
        _save_cache(US_IPO_PRICE_CACHE, cached_hist)
    else:
        logger.info("US 次新股池: 历史缓存命中 (%d 只)", len(candidates))

    # info enrich (sector/marketCap) — 7d 缓存,只对有当前价的拉
    meta_cache = _load_cache(US_IPO_META_CACHE, US_IPO_META_TTL_HOURS)
    cached_meta = meta_cache.get("entries") or {}
    syms_with_price = [c["symbol"] for c in candidates if (cached_hist.get(c["symbol"]) or {}).get("price")]
    syms_need_meta = [s for s in syms_with_price if s not in cached_meta]
    if syms_need_meta:
        logger.info("US 次新股池: %d 只需拉 info (yfinance 并行,缓存 %d)",
                    len(syms_need_meta), len(cached_meta))
        fresh = _batch_yf_info(syms_need_meta, max_workers=8)
        cached_meta.update(fresh)
        for sym in syms_need_meta:
            if sym not in cached_meta:
                cached_meta[sym] = {"sector": None}
        _save_cache(US_IPO_META_CACHE, cached_meta)
    else:
        logger.info("US 次新股池: info 缓存命中 (%d 只)", len(syms_with_price))

    # gap #2: SEC dilution filings — 稀释类申报近 180 日
    # gap #3: yfinance financials — 毛利率 / 现金 runway
    fake_items = [{"symbol": c["symbol"]} for c in candidates]
    _enrich_us_sec_filings(fake_items)
    _enrich_us_financials(fake_items)
    sec_map = {x["symbol"].upper(): x.get("dilution_filings_180d") or []
               for x in fake_items if x.get("symbol")}
    fin_map = {x["symbol"].upper(): x.get("financials") or {}
               for x in fake_items if x.get("symbol")}

    # 打分
    out: list[dict] = []
    rejected_extreme_penny = 0
    rejected_extreme_micro = 0
    rejected_no_price = 0
    for c in candidates:
        sym = c["symbol"]
        issue_price = float(c["issue_price"])
        h = cached_hist.get(sym) or {}
        meta = cached_meta.get(sym) or {}
        current_price = _safe_float(h.get("price"))
        low = _safe_float(h.get("low_since_ipo"))
        high = _safe_float(h.get("high_since_ipo"))
        avg_vol = h.get("avg_volume_30d")
        market_cap = _safe_float(meta.get("market_cap"))
        sector = meta.get("sector") or ""
        industry = meta.get("industry") or ""
        months_listed = c["months_listed"]

        # ─── 硬过滤 ─── 只剔极端数据,其它走红线软标记
        if current_price is None:
            rejected_no_price += 1
            continue
        if current_price < hard_min_price_usd:
            rejected_extreme_penny += 1
            continue
        if market_cap is not None and market_cap < hard_min_market_cap_usd:
            rejected_extreme_micro += 1
            continue

        # 派生:vs_issue / rebound_from_low / drawdown_from_high
        vs_issue_pct = None
        rebound_pct = None
        drawdown_pct = None
        if current_price is not None and current_price > 0:
            vs_issue_pct = (current_price - issue_price) / issue_price * 100.0
            if low and low > 0:
                rebound_pct = (current_price - low) / low * 100.0
            if high and high > 0:
                drawdown_pct = (current_price - high) / high * 100.0

        # ─── step 4: 拆 bottom_score (像不像底) 与 readiness_score (能不能动手) ───
        # bottom_score 100 制
        # discount_to_issue (50): 跌破越深越高分
        s_discount = min(50.0, abs(vs_issue_pct) / 1.2) if (vs_issue_pct is not None and vs_issue_pct <= 0) else 0.0
        # time_decay (30): 12-18 月最高
        if 12 <= months_listed <= 18:
            s_time = 30.0
        elif 9 <= months_listed < 12 or 18 < months_listed <= 21:
            s_time = 23.0
        elif 6 <= months_listed < 9 or 21 < months_listed <= 24:
            s_time = 15.0
        else:
            s_time = 8.0
        # drawdown_from_high (20): 较 IPO 高点回撤越深越像底
        s_drawdown = min(20.0, abs(drawdown_pct) / 4.0) if (drawdown_pct is not None and drawdown_pct < 0) else 0.0
        bottom_score = round(s_discount + s_time + s_drawdown, 1)

        # readiness_score 100 制
        # liquidity (25)
        dollar_vol = (avg_vol * current_price) if (avg_vol and current_price) else None
        if dollar_vol and dollar_vol >= 5_000_000:
            s_liquid = 25.0
        elif dollar_vol and dollar_vol >= 1_000_000:
            s_liquid = 18.0
        elif dollar_vol and dollar_vol >= 200_000:
            s_liquid = 10.0
        else:
            s_liquid = 0.0
        # rebound_from_low (25): 从最低反弹 20-60% 是反转信号；过低没启动，过高已涨多
        if rebound_pct is not None:
            if 20 <= rebound_pct <= 60:
                s_rebound = 25.0
            elif 10 <= rebound_pct < 20 or 60 < rebound_pct <= 100:
                s_rebound = 15.0
            else:
                s_rebound = 0.0
        else:
            s_rebound = 0.0
        # price_vs_ma20 (20): 现价 >= MA20 = 站上短期均线 (动能)
        ma20 = _safe_float(h.get("ma20"))
        ma50 = _safe_float(h.get("ma50"))
        price_vs_ma20_pct = None
        price_vs_ma50_pct = None
        if ma20 and ma20 > 0 and current_price:
            price_vs_ma20_pct = (current_price - ma20) / ma20 * 100.0
        if ma50 and ma50 > 0 and current_price:
            price_vs_ma50_pct = (current_price - ma50) / ma50 * 100.0
        if price_vs_ma20_pct is None:
            s_ma = 0.0
        elif price_vs_ma20_pct >= 5:
            s_ma = 20.0
        elif price_vs_ma20_pct >= 0:
            s_ma = 14.0
        elif price_vs_ma20_pct >= -5:
            s_ma = 7.0
        else:
            s_ma = 0.0
        # low_not_renewed (15): 近 30 日最低 vs 全期最低,差距越大越好 (低点抬升)
        low_30d = _safe_float(h.get("low_30d"))
        low_not_new_pct = None
        if low_30d and low and low > 0:
            low_not_new_pct = (low_30d - low) / low * 100.0  # 0 = 仍在创新低
        if low_not_new_pct is None:
            s_low = 0.0
        elif low_not_new_pct >= 20:
            s_low = 15.0
        elif low_not_new_pct >= 10:
            s_low = 10.0
        elif low_not_new_pct >= 3:
            s_low = 5.0
        else:
            s_low = 0.0
        # volume_pickup (10): 近 30 日均量 / 前 30 日均量 > 1.2 = 放量
        vol_ratio = _safe_float(h.get("vol_ratio_30d"))
        if vol_ratio is None:
            s_vol = 0.0
        elif vol_ratio >= 1.5:
            s_vol = 10.0
        elif vol_ratio >= 1.2:
            s_vol = 6.0
        elif vol_ratio >= 0.8:
            s_vol = 2.0
        else:
            s_vol = 0.0
        # in_your_pool (5)
        s_pool = 5.0 if (sym in holdings or sym in watchlist) else 0.0
        readiness_score = round(s_liquid + s_rebound + s_ma + s_low + s_vol + s_pool, 1)

        # 科技股识别 (Technology + Communication Services 都算,后者含 GOOG/META 类)
        is_tech = sector in {"Technology", "Communication Services"}
        total = bottom_score  # 兼容旧字段 score

        tags = []
        if is_tech:
            tags.append("🔬 科技")
        if vs_issue_pct is not None and vs_issue_pct < 0:
            tags.append("已破发")
        if 12 <= months_listed <= 18:
            tags.append("解禁窗口")
        if vs_issue_pct is not None and vs_issue_pct < -50:
            tags.append("腰斩")
        if rebound_pct is not None and 20 <= rebound_pct <= 60:
            tags.append("从底反弹")
        if dollar_vol is not None and dollar_vol < 200_000:
            tags.append("低流动性")

        est_lockup_end, days_to_est_lockup = _us_est_lockup_end(c.get("priced_date"))
        dilution_180d = sec_map.get(sym.upper(), [])
        financials = fin_map.get(sym.upper(), {})
        item = {
            "symbol": sym,
            "name": c.get("name") or "",
            "exchange": c.get("exchange") or "",
            "sector": _zh_us_sector(sector),
            "industry": _zh_us_industry(industry),
            "ipo_date": c.get("priced_date"),
            "est_lockup_end": est_lockup_end,
            "days_to_est_lockup": days_to_est_lockup,
            "months_listed": round(months_listed, 1),
            "issue_price": round(issue_price, 2),
            "current_price": round(current_price, 2) if current_price else None,
            "low_since_ipo": round(low, 2) if low else None,
            "low_date": h.get("low_date"),
            "high_since_ipo": round(high, 2) if high else None,
            "vs_issue_pct": round(vs_issue_pct, 1) if vs_issue_pct is not None else None,
            "rebound_pct": round(rebound_pct, 1) if rebound_pct is not None else None,
            "drawdown_pct": round(drawdown_pct, 1) if drawdown_pct is not None else None,
            "market_cap_m": round(market_cap / 1e6, 1) if market_cap else None,
            "avg_volume_30d": avg_vol,
            "deal_value_usd": c.get("deal_value_usd"),
            "shares_offered": c.get("shares_offered"),
            "score": total,
            "bottom_score": bottom_score,
            "readiness_score": readiness_score,
            "is_tech": is_tech,
            "dilution_filings_180d": dilution_180d,
            "financials": financials,
            "dollar_volume_30d": int(dollar_vol) if dollar_vol else None,
            "ma20": round(ma20, 2) if ma20 else None,
            "ma50": round(ma50, 2) if ma50 else None,
            "price_vs_ma20_pct": round(price_vs_ma20_pct, 1) if price_vs_ma20_pct is not None else None,
            "price_vs_ma50_pct": round(price_vs_ma50_pct, 1) if price_vs_ma50_pct is not None else None,
            "low_30d": round(low_30d, 2) if low_30d else None,
            "low_not_new_pct": round(low_not_new_pct, 1) if low_not_new_pct is not None else None,
            "vol_ratio_30d": vol_ratio,
            "score_breakdown": {
                "bottom_discount": round(s_discount, 1),
                "bottom_time": round(s_time, 1),
                "bottom_drawdown": round(s_drawdown, 1),
                "readiness_liquidity": round(s_liquid, 1),
                "readiness_rebound": round(s_rebound, 1),
                "readiness_ma20": round(s_ma, 1),
                "readiness_low_hold": round(s_low, 1),
                "readiness_volume": round(s_vol, 1),
                "readiness_pool": round(s_pool, 1),
            },
            "tags": tags,
            "in_holdings": sym in holdings,
            "in_watchlist": sym in watchlist,
        }
        # 红线评估（step 1）
        red_lines = _evaluate_us_red_lines(item, is_spac_flag=bool(c.get("_is_spac")))
        item["red_lines"] = red_lines
        item["verdict"] = "不碰" if red_lines else None
        out.append(item)

    # 排序：先按 verdict（"不碰" 沉底），同档内按 score 降序
    out.sort(key=lambda x: (1 if x.get("verdict") == "不碰" else 0, -x["score"]))
    _attach_percentile(out)
    _assign_tier(out, market="us")
    n_bad = sum(1 for x in out if x.get("verdict") == "不碰")
    tier_counts = {t: sum(1 for x in out if x.get("tier") == t)
                   for t in ("可小仓试探", "可研究", "只观察", "不碰")}
    logger.info("US 次新股池: 候选 %d → 入池 %d (其中标 \"不碰\" %d · 极端剔除: 仙股 %d / 微盘 %d / 无价 %d); 分档: %s",
                len(candidates), len(out), n_bad,
                rejected_extreme_penny, rejected_extreme_micro, rejected_no_price,
                " · ".join(f"{k}={v}" for k, v in tier_counts.items()))
    return out


# ═════════════════════════════════════════════════════
#  港股（数据源受限的 placeholder）
# ═════════════════════════════════════════════════════

HK_DATA_NOTE = (
    "港股 IPO/解禁/次新股的开源数据源极有限：finnhub 免费层不含港股，"
    "akshare 的港股 IPO 接口（stock_ipo_hk_ths）实际返回的是 A 股数据（命名 bug）。"
    "目前推荐外部跟踪：① HKEX 官网 disclosure；② 富途/老虎 app 的「打新」入口；"
    "③ AAStocks.com 的 IPO 频道。本系统计划在接入 Wind/Choice 或 HKEX 付费 API 后补全。"
)
HK_LINKS = [
    {"label": "HKEX 新上市公司", "url": "https://www.hkexnews.hk/listedco/listconews/newlist/sehknewlist.htm"},
    {"label": "AAStocks IPO 频道", "url": "http://www.aastocks.com/sc/stocks/market/ipo/upcomingipo/companysummary"},
]


def build_hk_placeholder(holdings: set[str], watchlist: set[str]) -> dict:
    return {
        "available": False,
        "note": HK_DATA_NOTE,
        "external_links": HK_LINKS,
        "ipo_calendar": {"upcoming_subscription": [], "awaiting_listing": [], "recently_listed": []},
        "unlock_radar": [],
        "junior_pool": [],
        "your_pool_size": len(holdings) + len(watchlist),
    }


# ═════════════════════════════════════════════════════
#  主入口
# ═════════════════════════════════════════════════════

def build_radar() -> dict:
    pools = _load_pool_symbols()
    logger.info("持仓: CN %d / US %d / HK %d  ·  自选: CN %d / US %d / HK %d",
                len(pools["cn"]["holdings"]), len(pools["us"]["holdings"]), len(pools["hk"]["holdings"]),
                len(pools["cn"]["watchlist"]), len(pools["us"]["watchlist"]), len(pools["hk"]["watchlist"]))

    # —— A 股 ——
    ipo_raw = _load_ipo_calendar()
    cn_ipo = slim_cn_ipo_calendar(ipo_raw) if ipo_raw else {
        "fetched_at": None, "fetch_status": {},
        "upcoming_subscription": [], "awaiting_listing": [], "recently_listed": [],
    }
    if not ipo_raw:
        logger.warning("ipo_calendar.json 缺失 — 跑过 ipo_daily.py 了吗？")
    logger.info("[CN] 拉解禁雷达（未来 90 天）...")
    cn_unlock = fetch_cn_unlock_radar(pools["cn"]["holdings"], pools["cn"]["watchlist"], horizon_days=90)
    logger.info("[CN]   → %d 条解禁", len(cn_unlock))
    logger.info("[CN] 拉次新股池（上市 6-24 月）...")
    cn_junior = fetch_cn_junior_pool(pools["cn"]["holdings"], pools["cn"]["watchlist"], months_min=6, months_max=24)
    logger.info("[CN]   → %d 只候选", len(cn_junior))
    logger.info("[CN] 补 industry 字段（缓存 7 天 + fail-soft）...")
    _enrich_cn_industry(cn_junior)
    # gap #1: baostock 日 K → MA20 / 低点抬升 / 放量 / 反弹 → readiness_score
    logger.info("[CN] 补 K 线派生指标 (baostock, 24h 缓存)...")
    _enrich_cn_history(cn_junior)
    _compute_cn_readiness(cn_junior, pools["cn"]["holdings"], pools["cn"]["watchlist"])
    # step 1: 红线 gate — cross-ref 30 天大额解禁 + ST
    _apply_cn_red_lines(cn_junior, cn_unlock)
    # 股权穿透 + 60d 解禁早期预警（advisory only, 不进 verdict/tier）
    unlock_60d_map = _build_unlock_60d_map(cn_unlock)
    _enrich_cn_ownership_and_unlock_60d(cn_junior, unlock_60d_map)

    # —— 美股 (NASDAQ 公开 API + yfinance) ——
    logger.info("[US] 拉 NASDAQ IPO window (过去 24 月 priced + 未来 2 月 filed)...")
    try:
        from stock_research.core import nasdaq_ipo
        nasdaq_win = nasdaq_ipo.fetch_window(months_back=24, months_forward=2)
        logger.info("[US]   → priced %d · filed %d · 月份 %d",
                    len(nasdaq_win["priced"]), len(nasdaq_win["filed"]),
                    len(nasdaq_win["months_pulled"]))
    except Exception as e:
        logger.warning("[US] NASDAQ window 失败: %s", e)
        nasdaq_win = {"priced": [], "filed": [], "months_pulled": []}
    us_ipo_cal = build_us_ipo_calendar(nasdaq_win)
    logger.info("[US]   IPO 日历: 即将申报 %d · 已定价未上市 %d · 近 30 日上市 %d",
                len(us_ipo_cal["upcoming_filing"]),
                len(us_ipo_cal["awaiting_listing"]),
                len(us_ipo_cal["recently_listed"]))
    logger.info("[US] 拉次新股池 (NASDAQ priced ∩ 上市 6-24 月)...")
    us_junior = fetch_us_junior_pool(
        pools["us"]["holdings"], pools["us"]["watchlist"],
        nasdaq_priced=nasdaq_win["priced"],
        months_min=6, months_max=24,
    )
    logger.info("[US]   → %d 只候选 (有当前价: %d)",
                len(us_junior),
                sum(1 for x in us_junior if x.get("current_price")))
    # step 2: lockup 雷达 — 180 天估算, 未来 90 天内到期
    us_unlock = fetch_us_lockup_radar(
        nasdaq_win["priced"], pools["us"]["holdings"], pools["us"]["watchlist"], horizon_days=90,
    )
    logger.info("[US] lockup 雷达 (估算 priced+180d, 未来 90 天): %d 条", len(us_unlock))

    # step 5b: 日间 diff — 加载昨日 snapshot, 在 cn/us pool 每项打 diff_flag, 写新 snapshot
    prev_idx, prev_date = _load_prev_snapshot()
    diff_counts = _apply_daily_diff(cn_junior, us_junior, prev_idx, prev_date)
    _save_snapshot(cn_junior, us_junior)
    if prev_date:
        logger.info("[DIFF] vs %s: 🆕 %d / 📈 升档 %d / 📉 降档 %d / ↑ jumped %d / ↓ slipped %d / exited %d",
                    prev_date, diff_counts["new"], diff_counts["upgraded"], diff_counts["downgraded"],
                    diff_counts["jumped"], diff_counts["slipped"], diff_counts["exited"])
    else:
        logger.info("[DIFF] 首次跑(无 prev snapshot), 全部按新进入对待但只标 actionable 档")

    # 近 30 日上市 + 已定价未上市 也借同一份 yfinance 缓存补当前价
    # (fetch_us_junior_pool 已经把这些 ticker 拉过且写进缓存)
    hist_cache = (_load_cache(US_IPO_PRICE_CACHE, US_IPO_PRICE_TTL_HOURS).get("entries") or {})
    meta_cache = (_load_cache(US_IPO_META_CACHE, US_IPO_META_TTL_HOURS).get("entries") or {})
    # 但「近 30 日上市」的 ticker 可能不在 junior_pool 缓存里(<6 月),需要额外补拉
    recent_syms = [e["symbol"] for e in us_ipo_cal["recently_listed"]
                   if e.get("symbol") and e["symbol"] not in hist_cache]
    if recent_syms:
        logger.info("[US] 补拉「近 30 日上市」%d 只价格...", len(recent_syms))
        recent_dates = {e["symbol"]: e.get("priced_date") for e in us_ipo_cal["recently_listed"]}
        fresh = _batch_yf_history(recent_syms, recent_dates, batch=50, period="60d")
        hist_cache.update(fresh)
        for s in recent_syms:
            if s not in hist_cache:
                hist_cache[s] = {"price": None}
        _save_cache(US_IPO_PRICE_CACHE, hist_cache)
    us_ipo_cal["recently_listed"] = _enrich_with_price(us_ipo_cal["recently_listed"], hist_cache, meta_cache)
    us_ipo_cal["awaiting_listing"] = _enrich_with_price(us_ipo_cal["awaiting_listing"], hist_cache, meta_cache)

    # —— 港股 ——
    logger.info("[HK] 数据源受限，输出 placeholder")
    hk = build_hk_placeholder(pools["hk"]["holdings"], pools["hk"]["watchlist"])

    # —— 本周事件（A 股为主，其他市场摘要）——
    today = date.today()
    week_end = today + timedelta(days=7)

    def _in_week(date_str: str | None) -> bool:
        if not date_str:
            return False
        try:
            d = datetime.fromisoformat(str(date_str)[:10]).date()
            return today <= d <= week_end
        except Exception:
            return False

    week_cn_subs = [x for x in cn_ipo["upcoming_subscription"] if _in_week(x.get("subscribe_date"))]
    week_cn_listings = [x for x in cn_ipo["awaiting_listing"] if _in_week(x.get("listing_date"))]
    week_cn_unlocks = [x for x in cn_unlock if x["days_to_unlock"] <= 7]
    week_cn_unlocks_in_pool = [x for x in week_cn_unlocks if x["in_holdings"] or x["in_watchlist"]]
    # 美股「本周事件」= 已定价未上市 + 近 7 日定价 + 近 7 日 filed (即将申报)
    week_us_priced = [x for x in us_ipo_cal["awaiting_listing"] if x.get("days_since_priced", 99) <= 7]
    week_us_filed = [x for x in us_ipo_cal["upcoming_filing"] if x.get("days_since_filed", 99) <= 7]

    summary = {
        "cn": {
            "subscribe_count": len(week_cn_subs),
            "listing_count": len(week_cn_listings),
            "unlock_count": len(week_cn_unlocks),
            "unlock_in_pool_count": len(week_cn_unlocks_in_pool),
            "unlock_in_pool_codes": [
                {"code": x["code"], "name": x["name"], "date": x["unlock_date"], "stress": x["stress_score"]}
                for x in week_cn_unlocks_in_pool[:5]
            ],
            "junior_top3": [
                {"code": x["code"], "name": x["name"], "score": x["score"], "vs_issue_pct": x["vs_issue_pct"]}
                for x in cn_junior[:3]
            ],
        },
        "us": {
            "priced_7d_count": len(week_us_priced),
            "filed_7d_count": len(week_us_filed),
            "junior_count": len(us_junior),
            "broken_count": sum(1 for x in us_junior if (x.get("vs_issue_pct") or 0) < 0),
            "ipo_top3": [
                {"symbol": x["symbol"], "name": x["name"], "date": x.get("priced_date"),
                 "exchange": x.get("exchange"), "issue_price": x.get("issue_price")}
                for x in week_us_priced[:3]
            ],
            "junior_top3": [
                {"symbol": x["symbol"], "name": x["name"], "vs_issue_pct": x["vs_issue_pct"],
                 "score": x["score"]}
                for x in us_junior[:3]
            ],
        },
        "hk": {"available": False},
    }

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary_week": summary,
        "markets": {
            "cn": {
                "available": True,
                "ipo_calendar": cn_ipo,
                "unlock_radar": cn_unlock,
                "junior_pool": cn_junior,
            },
            "us": {
                "available": True,
                "ipo_calendar": us_ipo_cal,
                "unlock_radar": us_unlock,
                "unlock_estimated": True,
                "junior_pool": us_junior,
                "data_source": "NASDAQ public API + yfinance batch",
                "note": "美股 lockup 数据来自估算 (priced + 180 天, SEC 典型默认值)，真实 lockup 在 S-1 招股书,长度可能 90-365 天不等。关键决策需查公司 S-1 确认。",
            },
            "hk": hk,
        },
        "params": {
            "unlock_horizon_days": 90,
            "junior_months_range": [6, 24],
            "us_ipo_horizon_days": 120,
        },
    }


def main() -> int:
    radar = build_radar()
    out = REPO / "data" / "latest" / "junior_stock_radar.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(radar, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"✅ junior_stock_radar.json 已写入 {out}")
    cn = radar["markets"]["cn"]
    us = radar["markets"]["us"]
    print(f"   [CN] IPO 申购 {len(cn['ipo_calendar']['upcoming_subscription'])} · "
          f"解禁 {len(cn['unlock_radar'])} · 次新股 {len(cn['junior_pool'])}")
    print(f"   [US] 即将申报 {len(us['ipo_calendar']['upcoming_filing'])} · "
          f"已定价未上市 {len(us['ipo_calendar']['awaiting_listing'])} · "
          f"近 30 日上市 {len(us['ipo_calendar']['recently_listed'])} · "
          f"次新股 {len(us['junior_pool'])}")
    print(f"   [HK] 数据源受限：placeholder + 2 外链")
    s = radar["summary_week"]
    print(f"   本周事件: CN 申购 {s['cn']['subscribe_count']} / 上市 {s['cn']['listing_count']} / "
          f"解禁 {s['cn']['unlock_count']}（池子内 {s['cn']['unlock_in_pool_count']}）· "
          f"US 定价 {s['us']['priced_7d_count']} / 申报 {s['us']['filed_7d_count']}")
    has_data = bool(cn["junior_pool"] or cn["unlock_radar"] or us["ipo_calendar"] or us["junior_pool"])
    return 0 if has_data else 2


if __name__ == "__main__":
    sys.exit(main())
