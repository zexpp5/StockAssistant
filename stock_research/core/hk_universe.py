"""港股科技/医疗/新能源白名单 · 与 a_share_universe.py 对称。

为什么是白名单（不是接口）：
  - akshare 港股通成分股接口被东财 proxy 拦
  - 中证指数公司不发布港股指数（HSTECH 是恒生指数公司，无公开 API）
  - baostock 不含港股
  - 港股龙头名单变化极慢（恒生科技指数一年调 2 次，每次换 1-2 只），
    手动维护成本 < 1 小时/年，可接受

四大类（与 a_share_universe.py 关键词覆盖对齐）:
  - 互联网平台 / AI 应用 / 软件 (32)  → 对应 A 股「互联网 / 传媒 / 软件」
  - 半导体 / 硬件 / 通信 (24)         → 对应 A 股「半导体 / 电子设备 / 通信」
  - 新能源车 / 光伏 / 风电 / 锂电 (19) → 对应 A 股「电气机械 / 电池」
  - 创新药 / CXO / 医疗器械 (25)      → 对应 A 股「医药 / 生物」

2026-07-07 扩池（33 → 100，用户拍板）:
  锦标赛方案第 4 问："33 只太少，配方比赛分不出胜负"。
  扩池依据 = 恒生综合指数 资讯科技 / 医疗保健 / 新能源与汽车相关板块的流动性龙头，
  原 33 只全保留；新增 67 只标 tier="extended"（原名单 tier="core"）。

更新约定:
  半年回看一次 https://www.hsi.com.hk 恒生综合/恒生科技指数 quarterly factsheet,
  把新调入的加进去、删除的标 # DELISTED + 注释保留，方便溯源。
"""
from __future__ import annotations


# ────────────────────────────────────────────────────────
# 白名单 (yfinance 4 位 + .HK 格式)
# 最后更新: 2026-07-07（扩池 33 → 100）
# 数据源: 恒生科技指数成分股 + 恒生综合指数科技/医疗/新能源板块龙头
# ────────────────────────────────────────────────────────

HK_TECH_UNIVERSE: list[dict] = [
    # ── 1. 互联网平台 / AI 应用 / 软件 (32)
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
    # ── 1.x 扩池新增（2026-07-07）
    {"ticker": "9626.HK", "raw_ticker": "9626", "name": "哔哩哔哩-W",     "sector": "互联网",   "source": "hk_internet", "tier": "extended"},
    {"ticker": "0780.HK", "raw_ticker": "0780", "name": "同程旅行",       "sector": "互联网",   "source": "hk_internet", "tier": "extended"},
    {"ticker": "1698.HK", "raw_ticker": "1698", "name": "腾讯音乐-SW",    "sector": "传媒",     "source": "hk_internet", "tier": "extended"},
    {"ticker": "9899.HK", "raw_ticker": "9899", "name": "云音乐",         "sector": "传媒",     "source": "hk_internet", "tier": "extended"},
    {"ticker": "2400.HK", "raw_ticker": "2400", "name": "心动公司",       "sector": "游戏",     "source": "hk_internet", "tier": "extended"},
    {"ticker": "0777.HK", "raw_ticker": "0777", "name": "网龙",           "sector": "游戏",     "source": "hk_internet", "tier": "extended"},
    {"ticker": "0799.HK", "raw_ticker": "0799", "name": "IGG",            "sector": "游戏",     "source": "hk_internet", "tier": "extended"},
    {"ticker": "1357.HK", "raw_ticker": "1357", "name": "美图公司",       "sector": "AI",       "source": "hk_internet", "tier": "extended"},
    {"ticker": "6682.HK", "raw_ticker": "6682", "name": "第四范式",       "sector": "AI",       "source": "hk_internet", "tier": "extended"},
    {"ticker": "2121.HK", "raw_ticker": "2121", "name": "创新奇智",       "sector": "AI",       "source": "hk_internet", "tier": "extended"},
    {"ticker": "0909.HK", "raw_ticker": "0909", "name": "明源云",         "sector": "软件",     "source": "hk_internet", "tier": "extended"},
    {"ticker": "9959.HK", "raw_ticker": "9959", "name": "联易融科技-W",   "sector": "软件",     "source": "hk_internet", "tier": "extended"},
    {"ticker": "6608.HK", "raw_ticker": "6608", "name": "百融云-W",       "sector": "AI",       "source": "hk_internet", "tier": "extended"},
    {"ticker": "2158.HK", "raw_ticker": "2158", "name": "医渡科技",       "sector": "AI",       "source": "hk_internet", "tier": "extended"},
    {"ticker": "0241.HK", "raw_ticker": "0241", "name": "阿里健康",       "sector": "互联网医疗", "source": "hk_internet", "tier": "extended"},
    {"ticker": "1833.HK", "raw_ticker": "1833", "name": "平安好医生",     "sector": "互联网医疗", "source": "hk_internet", "tier": "extended"},
    {"ticker": "9878.HK", "raw_ticker": "9878", "name": "汇通达网络",     "sector": "互联网",   "source": "hk_internet", "tier": "extended"},

    # ── 2. 半导体 / 硬件 / 通信 (24)
    {"ticker": "0981.HK", "raw_ticker": "0981", "name": "中芯国际",       "sector": "半导体",   "source": "hk_semi"},
    {"ticker": "1347.HK", "raw_ticker": "1347", "name": "华虹半导体",     "sector": "半导体",   "source": "hk_semi"},
    {"ticker": "2382.HK", "raw_ticker": "2382", "name": "舜宇光学科技",   "sector": "电子设备", "source": "hk_semi"},
    {"ticker": "0992.HK", "raw_ticker": "0992", "name": "联想集团",       "sector": "硬件",     "source": "hk_semi"},
    {"ticker": "0763.HK", "raw_ticker": "0763", "name": "中兴通讯",       "sector": "通信",     "source": "hk_semi"},
    {"ticker": "9698.HK", "raw_ticker": "9698", "name": "万国数据-SW",    "sector": "IDC",      "source": "hk_semi"},
    {"ticker": "9660.HK", "raw_ticker": "9660", "name": "地平线机器人-W", "sector": "AI芯片",   "source": "hk_semi"},
    {"ticker": "2016.HK", "raw_ticker": "2016", "name": "中软国际",       "sector": "软件",     "source": "hk_semi"},
    # ── 2.x 扩池新增（2026-07-07）
    {"ticker": "0522.HK", "raw_ticker": "0522", "name": "ASMPT",          "sector": "半导体设备", "source": "hk_semi", "tier": "extended"},
    {"ticker": "1385.HK", "raw_ticker": "1385", "name": "上海复旦",       "sector": "半导体",   "source": "hk_semi", "tier": "extended"},
    {"ticker": "2878.HK", "raw_ticker": "2878", "name": "晶门半导体",     "sector": "半导体",   "source": "hk_semi", "tier": "extended"},
    {"ticker": "0285.HK", "raw_ticker": "0285", "name": "比亚迪电子",     "sector": "电子设备", "source": "hk_semi", "tier": "extended"},
    {"ticker": "2018.HK", "raw_ticker": "2018", "name": "瑞声科技",       "sector": "电子设备", "source": "hk_semi", "tier": "extended"},
    {"ticker": "1478.HK", "raw_ticker": "1478", "name": "丘钛科技",       "sector": "电子设备", "source": "hk_semi", "tier": "extended"},
    {"ticker": "0710.HK", "raw_ticker": "0710", "name": "京东方精电",     "sector": "电子设备", "source": "hk_semi", "tier": "extended"},
    {"ticker": "1415.HK", "raw_ticker": "1415", "name": "高伟电子",       "sector": "电子设备", "source": "hk_semi", "tier": "extended"},
    {"ticker": "6088.HK", "raw_ticker": "6088", "name": "鸿腾精密",       "sector": "电子设备", "source": "hk_semi", "tier": "extended"},
    {"ticker": "0303.HK", "raw_ticker": "0303", "name": "伟易达",         "sector": "电子设备", "source": "hk_semi", "tier": "extended"},
    {"ticker": "6969.HK", "raw_ticker": "6969", "name": "思摩尔国际",     "sector": "电子设备", "source": "hk_semi", "tier": "extended"},
    {"ticker": "2498.HK", "raw_ticker": "2498", "name": "速腾聚创",       "sector": "激光雷达", "source": "hk_semi", "tier": "extended"},
    {"ticker": "0788.HK", "raw_ticker": "0788", "name": "中国铁塔",       "sector": "通信",     "source": "hk_semi", "tier": "extended"},
    {"ticker": "0941.HK", "raw_ticker": "0941", "name": "中国移动",       "sector": "通信",     "source": "hk_semi", "tier": "extended"},
    {"ticker": "0728.HK", "raw_ticker": "0728", "name": "中国电信",       "sector": "通信",     "source": "hk_semi", "tier": "extended"},
    {"ticker": "0762.HK", "raw_ticker": "0762", "name": "中国联通",       "sector": "通信",     "source": "hk_semi", "tier": "extended"},

    # ── 3. 新能源车 / 光伏 / 风电 / 锂电 (19)
    {"ticker": "1211.HK", "raw_ticker": "1211", "name": "比亚迪股份",     "sector": "新能源车", "source": "hk_ev"},
    {"ticker": "2015.HK", "raw_ticker": "2015", "name": "理想汽车-W",     "sector": "新能源车", "source": "hk_ev"},
    {"ticker": "9868.HK", "raw_ticker": "9868", "name": "小鹏汽车-W",     "sector": "新能源车", "source": "hk_ev"},
    {"ticker": "9866.HK", "raw_ticker": "9866", "name": "蔚来-SW",        "sector": "新能源车", "source": "hk_ev"},
    # ── 3.x 扩池新增（2026-07-07）
    {"ticker": "9863.HK", "raw_ticker": "9863", "name": "零跑汽车",       "sector": "新能源车", "source": "hk_ev", "tier": "extended"},
    {"ticker": "0175.HK", "raw_ticker": "0175", "name": "吉利汽车",       "sector": "新能源车", "source": "hk_ev", "tier": "extended"},
    {"ticker": "2333.HK", "raw_ticker": "2333", "name": "长城汽车",       "sector": "新能源车", "source": "hk_ev", "tier": "extended"},
    {"ticker": "1316.HK", "raw_ticker": "1316", "name": "耐世特",         "sector": "汽车零部件", "source": "hk_ev", "tier": "extended"},
    {"ticker": "3931.HK", "raw_ticker": "3931", "name": "中创新航",       "sector": "动力电池", "source": "hk_ev", "tier": "extended"},
    {"ticker": "0819.HK", "raw_ticker": "0819", "name": "天能动力",       "sector": "动力电池", "source": "hk_ev", "tier": "extended"},
    {"ticker": "1772.HK", "raw_ticker": "1772", "name": "赣锋锂业",       "sector": "锂电材料", "source": "hk_ev", "tier": "extended"},
    {"ticker": "3800.HK", "raw_ticker": "3800", "name": "协鑫科技",       "sector": "光伏",     "source": "hk_ev", "tier": "extended"},
    {"ticker": "0968.HK", "raw_ticker": "0968", "name": "信义光能",       "sector": "光伏",     "source": "hk_ev", "tier": "extended"},
    {"ticker": "6865.HK", "raw_ticker": "6865", "name": "福莱特玻璃",     "sector": "光伏",     "source": "hk_ev", "tier": "extended"},
    {"ticker": "1799.HK", "raw_ticker": "1799", "name": "新特能源",       "sector": "光伏",     "source": "hk_ev", "tier": "extended"},
    {"ticker": "2208.HK", "raw_ticker": "2208", "name": "金风科技",       "sector": "风电",     "source": "hk_ev", "tier": "extended"},
    {"ticker": "0916.HK", "raw_ticker": "0916", "name": "龙源电力",       "sector": "风电",     "source": "hk_ev", "tier": "extended"},
    {"ticker": "1072.HK", "raw_ticker": "1072", "name": "东方电气",       "sector": "电力设备", "source": "hk_ev", "tier": "extended"},
    {"ticker": "2727.HK", "raw_ticker": "2727", "name": "上海电气",       "sector": "电力设备", "source": "hk_ev", "tier": "extended"},

    # ── 4. 创新药 / CXO / 医疗器械 (25)
    {"ticker": "2269.HK", "raw_ticker": "2269", "name": "药明生物",       "sector": "创新药",   "source": "hk_biotech"},
    {"ticker": "6160.HK", "raw_ticker": "6160", "name": "百济神州",       "sector": "创新药",   "source": "hk_biotech"},
    {"ticker": "1801.HK", "raw_ticker": "1801", "name": "信达生物",       "sector": "创新药",   "source": "hk_biotech"},
    {"ticker": "6618.HK", "raw_ticker": "6618", "name": "京东健康",       "sector": "医疗",     "source": "hk_biotech"},
    {"ticker": "1177.HK", "raw_ticker": "1177", "name": "中国生物制药",   "sector": "创新药",   "source": "hk_biotech"},
    {"ticker": "3692.HK", "raw_ticker": "3692", "name": "翰森制药",       "sector": "创新药",   "source": "hk_biotech"},
    # ── 4.x 扩池新增（2026-07-07）
    {"ticker": "1093.HK", "raw_ticker": "1093", "name": "石药集团",       "sector": "创新药",   "source": "hk_biotech", "tier": "extended"},
    {"ticker": "2196.HK", "raw_ticker": "2196", "name": "复星医药",       "sector": "创新药",   "source": "hk_biotech", "tier": "extended"},
    {"ticker": "9926.HK", "raw_ticker": "9926", "name": "康方生物",       "sector": "创新药",   "source": "hk_biotech", "tier": "extended"},
    {"ticker": "1877.HK", "raw_ticker": "1877", "name": "君实生物",       "sector": "创新药",   "source": "hk_biotech", "tier": "extended"},
    {"ticker": "9995.HK", "raw_ticker": "9995", "name": "荣昌生物",       "sector": "创新药",   "source": "hk_biotech", "tier": "extended"},
    {"ticker": "2162.HK", "raw_ticker": "2162", "name": "康诺亚-B",       "sector": "创新药",   "source": "hk_biotech", "tier": "extended"},
    {"ticker": "2696.HK", "raw_ticker": "2696", "name": "复宏汉霖",       "sector": "创新药",   "source": "hk_biotech", "tier": "extended"},
    {"ticker": "6185.HK", "raw_ticker": "6185", "name": "康希诺生物",     "sector": "疫苗",     "source": "hk_biotech", "tier": "extended"},
    {"ticker": "2616.HK", "raw_ticker": "2616", "name": "基石药业-B",     "sector": "创新药",   "source": "hk_biotech", "tier": "extended"},
    {"ticker": "6990.HK", "raw_ticker": "6990", "name": "科伦博泰生物-B", "sector": "创新药",   "source": "hk_biotech", "tier": "extended"},
    {"ticker": "2359.HK", "raw_ticker": "2359", "name": "药明康德",       "sector": "CXO",      "source": "hk_biotech", "tier": "extended"},
    {"ticker": "3759.HK", "raw_ticker": "3759", "name": "康龙化成",       "sector": "CXO",      "source": "hk_biotech", "tier": "extended"},
    {"ticker": "6127.HK", "raw_ticker": "6127", "name": "昭衍新药",       "sector": "CXO",      "source": "hk_biotech", "tier": "extended"},
    {"ticker": "1548.HK", "raw_ticker": "1548", "name": "金斯瑞生物科技", "sector": "CXO",      "source": "hk_biotech", "tier": "extended"},
    {"ticker": "0853.HK", "raw_ticker": "0853", "name": "微创医疗",       "sector": "医疗器械", "source": "hk_biotech", "tier": "extended"},
    {"ticker": "2252.HK", "raw_ticker": "2252", "name": "微创机器人-B",   "sector": "医疗器械", "source": "hk_biotech", "tier": "extended"},
    {"ticker": "1066.HK", "raw_ticker": "1066", "name": "威高股份",       "sector": "医疗器械", "source": "hk_biotech", "tier": "extended"},
    {"ticker": "1789.HK", "raw_ticker": "1789", "name": "爱康医疗",       "sector": "医疗器械", "source": "hk_biotech", "tier": "extended"},
    {"ticker": "6078.HK", "raw_ticker": "6078", "name": "海吉亚医疗",     "sector": "医疗服务", "source": "hk_biotech", "tier": "extended"},
]


def fetch_hk_tech_universe(*, core_only: bool = False) -> list[dict]:
    """返回港股白名单(2026-07-07 扩池后共 100 只),格式与 a_share_universe.fetch_a_share_tech_universe() 对齐。

    Args:
      core_only: True 时只返回扩池前的 33 只核心名单（tier="core"）。

    Returns:
      list of {
        ticker:       "0700.HK"          (yfinance 格式)
        raw_ticker:   "0700"             (裸代码,跟 watchlist 匹配用)
        name:         "腾讯控股"
        sector:       "互联网" / "半导体" / "新能源车" / "创新药" / ...
        location:     "Hong Kong"        (与 discover_candidates.EXCHANGE_SUFFIX 对齐)
        source:       "hk_internet" / "hk_semi" / "hk_ev" / "hk_biotech"
        tier:         "core"(原33只) / "extended"(2026-07-07 扩池新增)
      }
    """
    rows = [{**r, "location": "Hong Kong", "tier": r.get("tier", "core")} for r in HK_TECH_UNIVERSE]
    if core_only:
        rows = [r for r in rows if r["tier"] == "core"]
    return rows


if __name__ == "__main__":
    items = fetch_hk_tech_universe()
    print(f"港股白名单总计: {len(items)} 只（core={sum(1 for r in items if r['tier']=='core')} / extended={sum(1 for r in items if r['tier']=='extended')}）")
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
