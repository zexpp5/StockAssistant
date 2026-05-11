"""A 股扫描池 · 方案 B：hs300 科技子集 + 科创 50 + 创业板指 ≈ 200 只。

为什么是这个组合（基于行业调研 + 用户 AI 主题偏好）：
  - 沪深 300 全集 = 主要是金融/消费/能源蓝筹，AI 主题密度只 ~20%
  - 单看沪深 300 科技子集（计算机/电子/通信等行业）→ 拿到 hs300 里的 AI 相关大盘
  - 科创 50 = 上海科创板硬科技核心（半导体/AI/生物医药）
  - 创业板指 = 深圳创业板成长股龙头
  三者合并后 ~200 只，AI 主题密度高，命中率好

数据源：
  - 沪深 300：baostock query_hs300_stocks（官方接口）
  - 沪深 300 行业映射：baostock query_stock_industry（用于过滤科技子集）
  - 科创 50：akshare index_stock_cons_csindex(symbol='000688')
  - 创业板指：akshare index_stock_cons_sina(symbol='399006')

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
    from . import baostock_client
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    seen: dict[str, dict] = {}  # bs_code → record

    # 行业表（hs300 子集过滤要用；其他来源若没匹配也会留默认行业）
    industry_map: dict[str, dict[str, str]] = {}
    if baostock_client._ensure_login():
        industry_map = baostock_client._industry_table()

    # ── 1. baostock 沪深 300 → 按行业过滤科技子集
    if baostock_client._ensure_login():
        try:
            import baostock as bs
            rs = bs.query_hs300_stocks(date=date)
            while rs.error_code == "0" and rs.next():
                r = rs.get_row_data()  # [updateDate, code, code_name]
                bs_code = r[1]
                info = industry_map.get(bs_code, {})
                industry = info.get("industry") or ""
                if _is_tech(industry):
                    seen[bs_code] = {
                        "bs_code": bs_code,
                        "name": r[2],
                        "industry": industry,
                        "source": "hs300_tech",
                    }
        except Exception as e:
            logger.warning("hs300 拉取失败: %s", e)

    # ── 2. akshare 科创 50（中证指数官方）
    try:
        import akshare as ak
        df = ak.index_stock_cons_csindex(symbol="000688")
        for _, row in df.iterrows():
            code = str(row.get("成分券代码") or "").strip()
            if not (code.isdigit() and len(code) == 6):
                continue
            bs_code = "sh." + code  # 科创板都在沪市
            if bs_code in seen:
                continue
            info = industry_map.get(bs_code, {})
            seen[bs_code] = {
                "bs_code": bs_code,
                "name": row.get("成分券名称") or "",
                "industry": info.get("industry") or "科创板",
                "source": "kechuang50",
            }
    except Exception as e:
        logger.warning("科创 50 拉取失败: %s", e)

    # ── 3. akshare 创业板指（新浪源）
    try:
        import akshare as ak
        df = ak.index_stock_cons_sina(symbol="399006")
        for _, row in df.iterrows():
            code = str(row.get("code") or "").strip()
            if not (code.isdigit() and len(code) == 6):
                continue
            bs_code = "sz." + code  # 创业板都在深市
            if bs_code in seen:
                continue
            info = industry_map.get(bs_code, {})
            seen[bs_code] = {
                "bs_code": bs_code,
                "name": row.get("name") or "",
                "industry": info.get("industry") or "创业板",
                "source": "chuangyeban",
            }
    except Exception as e:
        logger.warning("创业板指拉取失败: %s", e)

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
