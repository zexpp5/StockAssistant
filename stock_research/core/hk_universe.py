"""港股科技龙头白名单 · 与 a_share_universe.py 对称。

为什么是白名单（不是接口）：
  - akshare 港股通成分股接口被东财 proxy 拦
  - 中证指数公司不发布港股指数（HSTECH 是恒生指数公司，无公开 API）
  - baostock 不含港股
  - 港股科技龙头变化极慢（恒生科技指数一年调 2 次，每次换 1-2 只），
    手动维护成本 < 1 小时/年，可接受

四大类（与 a_share_universe.py 关键词覆盖对齐）:
  - 互联网平台 / AI 应用层 (15)  → 对应 A 股「互联网 / 传媒 / 软件」
  - 半导体 / 硬科技 (8)          → 对应 A 股「半导体 / 电子设备 / 通信」
  - 新能源车 (4)                  → 对应 A 股「电气机械 / 电池」
  - 创新药 / 生物医药 (6)        → 对应 A 股「医药 / 生物」

更新约定:
  半年回看一次 https://www.hsi.com.hk 恒生科技指数 quarterly factsheet,
  把新调入的加进去、删除的标 # DELISTED + 注释保留，方便溯源。
"""
from __future__ import annotations


# ────────────────────────────────────────────────────────
# 白名单 (yfinance 4 位 + .HK 格式)
# 最后更新: 2026-05-11
# 数据源: 恒生科技指数成分股 + 港股通常见科技龙头
# ────────────────────────────────────────────────────────

HK_TECH_UNIVERSE: list[dict] = [
    # ── 1. 互联网平台 / AI 应用层 (15)
    {"ticker": "0700.HK", "raw_ticker": "0700", "name": "腾讯控股",       "sector": "互联网",   "source": "hk_internet"},
    {"ticker": "9988.HK", "raw_ticker": "9988", "name": "阿里巴巴-W",     "sector": "互联网",   "source": "hk_internet"},
    {"ticker": "3690.HK", "raw_ticker": "3690", "name": "美团-W",         "sector": "互联网",   "source": "hk_internet"},
    {"ticker": "9618.HK", "raw_ticker": "9618", "name": "京东集团-SW",    "sector": "互联网",   "source": "hk_internet"},
    {"ticker": "1024.HK", "raw_ticker": "1024", "name": "快手-W",         "sector": "互联网",   "source": "hk_internet"},
    {"ticker": "9999.HK", "raw_ticker": "9999", "name": "网易-S",         "sector": "互联网",   "source": "hk_internet"},
    {"ticker": "9888.HK", "raw_ticker": "9888", "name": "百度集团-SW",    "sector": "互联网",   "source": "hk_internet"},
    {"ticker": "1810.HK", "raw_ticker": "1810", "name": "小米集团-W",     "sector": "互联网",   "source": "hk_internet"},
    {"ticker": "9961.HK", "raw_ticker": "9961", "name": "携程集团-S",     "sector": "互联网",   "source": "hk_internet"},
    {"ticker": "0020.HK", "raw_ticker": "0020", "name": "商汤-W",         "sector": "AI",       "source": "hk_internet"},
    {"ticker": "0268.HK", "raw_ticker": "0268", "name": "金蝶国际",       "sector": "软件",     "source": "hk_internet"},
    {"ticker": "3888.HK", "raw_ticker": "3888", "name": "金山软件",       "sector": "软件",     "source": "hk_internet"},
    {"ticker": "2013.HK", "raw_ticker": "2013", "name": "微盟集团",       "sector": "软件",     "source": "hk_internet"},
    {"ticker": "0772.HK", "raw_ticker": "0772", "name": "阅文集团",       "sector": "传媒",     "source": "hk_internet"},
    {"ticker": "2076.HK", "raw_ticker": "2076", "name": "BOSS直聘-W",     "sector": "互联网",   "source": "hk_internet"},

    # ── 2. 半导体 / 硬科技 (8)
    {"ticker": "0981.HK", "raw_ticker": "0981", "name": "中芯国际",       "sector": "半导体",   "source": "hk_semi"},
    {"ticker": "1347.HK", "raw_ticker": "1347", "name": "华虹半导体",     "sector": "半导体",   "source": "hk_semi"},
    {"ticker": "2382.HK", "raw_ticker": "2382", "name": "舜宇光学科技",   "sector": "电子设备", "source": "hk_semi"},
    {"ticker": "0992.HK", "raw_ticker": "0992", "name": "联想集团",       "sector": "硬件",     "source": "hk_semi"},
    {"ticker": "0763.HK", "raw_ticker": "0763", "name": "中兴通讯",       "sector": "通信",     "source": "hk_semi"},
    {"ticker": "9698.HK", "raw_ticker": "9698", "name": "万国数据-SW",    "sector": "IDC",      "source": "hk_semi"},
    {"ticker": "9660.HK", "raw_ticker": "9660", "name": "地平线机器人-W", "sector": "AI芯片",   "source": "hk_semi"},
    {"ticker": "2016.HK", "raw_ticker": "2016", "name": "中软国际",       "sector": "软件",     "source": "hk_semi"},

    # ── 3. 新能源车 (4)
    {"ticker": "1211.HK", "raw_ticker": "1211", "name": "比亚迪股份",     "sector": "新能源车", "source": "hk_ev"},
    {"ticker": "2015.HK", "raw_ticker": "2015", "name": "理想汽车-W",     "sector": "新能源车", "source": "hk_ev"},
    {"ticker": "9868.HK", "raw_ticker": "9868", "name": "小鹏汽车-W",     "sector": "新能源车", "source": "hk_ev"},
    {"ticker": "9866.HK", "raw_ticker": "9866", "name": "蔚来-SW",        "sector": "新能源车", "source": "hk_ev"},

    # ── 4. 创新药 / 生物医药 (6)
    {"ticker": "2269.HK", "raw_ticker": "2269", "name": "药明生物",       "sector": "创新药",   "source": "hk_biotech"},
    {"ticker": "6160.HK", "raw_ticker": "6160", "name": "百济神州",       "sector": "创新药",   "source": "hk_biotech"},
    {"ticker": "1801.HK", "raw_ticker": "1801", "name": "信达生物",       "sector": "创新药",   "source": "hk_biotech"},
    {"ticker": "6618.HK", "raw_ticker": "6618", "name": "京东健康",       "sector": "医疗",     "source": "hk_biotech"},
    {"ticker": "1177.HK", "raw_ticker": "1177", "name": "中国生物制药",   "sector": "创新药",   "source": "hk_biotech"},
    {"ticker": "3692.HK", "raw_ticker": "3692", "name": "翰森制药",       "sector": "创新药",   "source": "hk_biotech"},
]


def fetch_hk_tech_universe() -> list[dict]:
    """返回港股科技龙头白名单(共 33 只),格式与 a_share_universe.fetch_a_share_tech_universe() 对齐。

    Returns:
      list of {
        ticker:       "0700.HK"          (yfinance 格式)
        raw_ticker:   "0700"             (裸代码,跟 watchlist 匹配用)
        name:         "腾讯控股"
        sector:       "互联网" / "半导体" / "新能源车" / "创新药" / ...
        location:     "Hong Kong"        (与 discover_candidates.EXCHANGE_SUFFIX 对齐)
        source:       "hk_internet" / "hk_semi" / "hk_ev" / "hk_biotech"
      }
    """
    return [{**r, "location": "Hong Kong"} for r in HK_TECH_UNIVERSE]


if __name__ == "__main__":
    items = fetch_hk_tech_universe()
    print(f"港股科技龙头白名单总计: {len(items)} 只")
    from collections import Counter
    by_source = Counter(r["source"] for r in items)
    print("按主题:", dict(by_source))
    by_sector = Counter(r["sector"] for r in items)
    print("按行业:")
    for s, n in by_sector.most_common():
        print(f"  {s}: {n}")
    print()
    print("Top 5 sample:")
    for r in items[:5]:
        print(f"  {r['ticker']} {r['name']} · {r['sector']} · {r['source']}")
