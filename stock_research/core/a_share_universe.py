"""A 股扫描池 · 方案 B：hs300 科技子集 + 科创 50 + 创业板指 ≈ 200 只。

为什么是这个组合（基于行业调研 + 用户 AI 主题偏好）：
  - 沪深 300 全集 = 主要是金融/消费/能源蓝筹，AI 主题密度只 ~20%
  - 单看沪深 300 科技子集（计算机/电子/通信等行业）→ 拿到 hs300 里的 AI 相关大盘
  - 科创 50 = 上海科创板硬科技核心（半导体/AI/生物医药）
  - 创业板指 = 深圳创业板成长股龙头
  三者合并后 ~200 只，AI 主题密度高，命中率好

数据源（2026-06-05 升级）：
  - 指数成分名单：Tushare index_weight（沪深300 000300.SH / 科创50 000688.SH /
    创业板指 399006.SZ）—— 替代 baostock query_hs300 + akshare index_stock_cons_*
  - 行业映射：baostock query_stock_industry（国标分类，仅用于 _is_tech 过滤；
    保留以保持科技子集口径不变，不随成分源切换而改变 universe 构成）

输出：yfinance 格式 ticker（XXXXXX.SS / XXXXXX.SZ）+ 元数据
"""
from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# 科技导向行业关键词（baostock industry 字段是国标格式如 "C39计算机、通信和其他电子设备制造业"，
# 用 substring 匹配比 exact 更稳）
TECH_INDUSTRY_KEYWORDS = [
    "计算机", "通信", "电子设备", "半导体",
    "软件", "信息技术", "互联网",
    "专用设备",        # 含半导体设备
    "仪器仪表",        # 含科学仪器 / 测量设备
    "电气机械", "电池",  # 新能源 + 电力设备
    "医药", "生物",    # 创新药 / 生物医药
    "航空航天",        # 国防军工 + AI 应用
    "传媒",            # AI 应用层
]


def _is_tech(industry: str) -> bool:
    """国标行业字段（'C39计算机...'）substring 匹配科技关键词。"""
    if not industry:
        return False
    return any(kw in industry for kw in TECH_INDUSTRY_KEYWORDS)


def _con_to_bs(con_code: str) -> str:
    """Tushare con_code '600519.SH' → baostock 风格 'sh.600519'（查行业表用）。"""
    code, _, ex = str(con_code).partition(".")
    if not (code.isdigit() and len(code) == 6) or ex not in ("SH", "SZ"):
        return ""
    return f"{ex.lower()}.{code}"


def fetch_a_share_tech_universe(date: str | None = None) -> list[dict]:
    """方案 B：hs300 科技子集 + 科创 50 + 创业板指。

    Args:
      date: YYYY-MM-DD，hs300 取当日成分股。None = 今日。
            周末/节假日 baostock 可能返回空，调用方处理回退。

    Returns:
      list of {
        ticker:       "600519.SS" / "300750.SZ"  (yfinance 格式)
        raw_ticker:   "600519"                    (裸代码,跟 watchlist 匹配用)
        name:         "中际旭创"
        sector:       "电子" / "计算机" / ...     (baostock 行业)
        location:     "China/Shanghai Stock Exchange" / "China/Shenzhen Stock Exchange"
        source:       "hs300_tech" / "kechuang50" / "chuangyeban"  (追溯哪条路进的)
      }
    """
    from . import baostock_client, tushare_client
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    seen: dict[str, dict] = {}  # bs_code → record

    # 行业表（hs300 子集过滤要用；其他来源若没匹配也会留默认行业）
    industry_map: dict[str, dict[str, str]] = {}
    if baostock_client._ensure_login():
        industry_map = baostock_client._industry_table()

    # ── 1. 沪深 300（Tushare index_weight）→ 按 baostock 国标行业过滤科技子集
    #    成分名单走 Tushare（去 baostock query_hs300）；行业分类仍用 baostock 国标，
    #    以保持 _is_tech 过滤口径不变（Tushare 行业是另一套体系，换源会改 universe 构成）。
    try:
        for con in tushare_client.fetch_index_cons("000300.SH"):
            bs_code = _con_to_bs(con)
            if not bs_code:
                continue
            info = industry_map.get(bs_code, {})
            industry = info.get("industry") or ""
            if _is_tech(industry):
                seen[bs_code] = {
                    "bs_code": bs_code,
                    "name": info.get("name") or "",
                    "industry": industry,
                    "source": "hs300_tech",
                }
    except Exception as e:
        logger.warning("hs300(Tushare) 拉取失败: %s", e)

    # ── 2. 科创 50（Tushare index_weight 000688.SH，全纳入）
    try:
        for con in tushare_client.fetch_index_cons("000688.SH"):
            bs_code = _con_to_bs(con)
            if not bs_code or bs_code in seen:
                continue
            info = industry_map.get(bs_code, {})
            seen[bs_code] = {
                "bs_code": bs_code,
                "name": info.get("name") or "",
                "industry": info.get("industry") or "科创板",
                "source": "kechuang50",
            }
    except Exception as e:
        logger.warning("科创 50(Tushare) 拉取失败: %s", e)

    # ── 3. 创业板指（Tushare index_weight 399006.SZ，全纳入）
    try:
        for con in tushare_client.fetch_index_cons("399006.SZ"):
            bs_code = _con_to_bs(con)
            if not bs_code or bs_code in seen:
                continue
            info = industry_map.get(bs_code, {})
            seen[bs_code] = {
                "bs_code": bs_code,
                "name": info.get("name") or "",
                "industry": info.get("industry") or "创业板",
                "source": "chuangyeban",
            }
    except Exception as e:
        logger.warning("创业板指(Tushare) 拉取失败: %s", e)

    # 转 yfinance 格式
    out = []
    for r in seen.values():
        bsc = r["bs_code"]
        if bsc.startswith("sh."):
            yf_t = bsc.split(".")[1] + ".SS"
            location = "China/Shanghai Stock Exchange"
        elif bsc.startswith("sz."):
            yf_t = bsc.split(".")[1] + ".SZ"
            location = "China/Shenzhen Stock Exchange"
        else:
            continue
        out.append({
            "ticker": yf_t,
            "raw_ticker": yf_t.split(".")[0],
            "name": r["name"],
            "sector": r["industry"],
            "location": location,
            "source": r["source"],
        })
    return out


if __name__ == "__main__":
    # 自检：跑一次输出 universe 统计
    logging.basicConfig(level=logging.INFO)
    items = fetch_a_share_tech_universe()
    print(f"A 股 universe 总计: {len(items)} 只")
    from collections import Counter
    by_source = Counter(r["source"] for r in items)
    print("按来源:", dict(by_source))
    by_industry = Counter(r["sector"] for r in items)
    print("按行业 Top 10:")
    for ind, n in by_industry.most_common(10):
        print(f"  {ind}: {n}")
    print()
    print("Top 5 sample:")
    for r in items[:5]:
        print(f"  {r['ticker']} {r['name']} · {r['sector']} · {r['source']}")
