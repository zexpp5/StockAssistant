"""
反向数据验证工具 — 用权威源交叉核对今天落表的关键数字。

设计目标：每次写新数据前/后跑一次，把 Claude 自己的"二手聚合数据"和真实
权威源（SEC 13F-HR、港交所披露易、Yahoo Finance）做对照，发现误读/误传。

【为什么需要】
2026-05-09 案例：抖音截图（东方财富图解财经）把 13F 的"占组合比例"
画得像"持仓变动幅度"，导致一次性写入飞书 8 条记录全部误读：
  - 巴菲特 AAPL "减 22.6%" 实为占比；真实变动 -4.3%
  - 段永平 NVDA "增 7.72%" 实为占比；真实变动 +1100%+
  - 高瓴 BABA "增 25.65%" 实为占比；真实**减仓** -$334M
误差量级在 100x-1000x，能反向得出错误结论。

【三类权威源】
  1. 美股 13F      → SEC EDGAR / Dataroma / 13f.info / Valuesider
  2. 港股财报      → 港交所披露易 hkexnews.hk / 雪球 xueqiu.com
  3. 实时股价      → Yahoo Finance / yfinance Python 库

用法：
  python3 verify_data_sources.py 13f BRK         # 列巴菲特最新 13F
  python3 verify_data_sources.py 13f H&H         # 列段永平最新 13F
  python3 verify_data_sources.py 13f Hillhouse   # 列高瓴最新 13F
  python3 verify_data_sources.py price 9992.HK   # 拉港股实时数据
  python3 verify_data_sources.py price NVDA      # 拉美股实时数据
"""
import sys

# 三大 13F 大佬的 SEC CIK + 公开数据源 URL
GURUS = {
    "BRK": {
        "name": "巴菲特 / Berkshire Hathaway",
        "cik": "0001067983",
        "sec_edgar": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001067983&type=13F-HR",
        "dataroma": "https://www.dataroma.com/m/holdings.php?m=BRK",
        "13f_info": "https://13f.info/manager/0001067983-berkshire-hathaway-inc",
        "whalewisdom": "https://whalewisdom.com/filer/berkshire-hathaway-inc",
    },
    "H&H": {
        "name": "段永平 / H&H International Investment",
        "cik": "0001225171",
        "sec_edgar": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001225171&type=13F-HR",
        "valuesider": "https://valuesider.com/guru/duan-yongping-h-h-international-investment/portfolio",
        "13radar": "https://www.13radar.com/guru/duan-yongping",
        "gurufocus": "https://www.gurufocus.com/guru/duan+yongping/summary",
    },
    "Hillhouse": {
        "name": "高瓴 / HHLR Advisors",
        "cik": "0001762304",
        "sec_edgar": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001762304&type=13F-HR",
        "13f_info": "https://13f.info/manager/0001762304-hhlr-advisors-ltd",
        "whalewisdom": "https://whalewisdom.com/filer/hillhouse-capital-advisors-ltd",
        "stockzoa": "https://stockzoa.com/fund/hillhouse-capital-advisors-ltd/",
    },
}

# 港股财报权威源（披露易）
HK_FINANCIAL_SOURCES = {
    "披露易": "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh",
    "雪球港股": "https://xueqiu.com/S/{code}",
    "AAStocks": "https://www.aastocks.com/sc/stocks/analysis/company-fundamental/financial-statements/{code}",
    "投资先觉": "https://www.investing.com/equities/{slug}",
}

# 实时价格源
PRICE_SOURCES = {
    "Yahoo Finance": "https://finance.yahoo.com/quote/{code}",
    "雪球": "https://xueqiu.com/S/{code}",
    "Investing.com": "https://www.investing.com/equities/",
    "yfinance Python 库": "import yfinance; yfinance.Ticker('{code}').info",
}


def show_13f(guru_key):
    """列出某位大佬的 13F 数据源 URL，方便手动 / Claude 核对。"""
    guru_key = guru_key.upper()
    matches = [k for k in GURUS if k.upper() == guru_key]
    if not matches:
        # 模糊匹配
        matches = [k for k in GURUS if guru_key.lower() in k.lower()
                   or guru_key.lower() in GURUS[k]["name"].lower()]
    if not matches:
        print(f"未找到大佬：{guru_key}")
        print(f"可选：{', '.join(GURUS.keys())}")
        return

    for k in matches:
        g = GURUS[k]
        print(f"\n=== {g['name']} ===")
        print(f"SEC CIK：{g['cik']}")
        print("\n📊 数据源 URL（按权威度排序）：")
        for label, url in g.items():
            if label in ("name", "cik"):
                continue
            print(f"  {label:14s}: {url}")
        print("\n💡 验证流程：")
        print("  1. 优先看 SEC EDGAR 13F-HR 原文（权威但 XML 难解析）")
        print("  2. Dataroma / 13f.info / Valuesider 是聚合站，直观看变动幅度")
        print("  3. 关键检查项：")
        print("     - 报告期是否最新 Q4'25（filing date 通常在季度末后 45 天）")
        print("     - %（百分比）是占比还是变动幅度（大坑！）")
        print("     - 变动方向（Add/Reduce/Buy/Sell/New/Liquidated）")


def show_price(code):
    """显示某只股票的实时价格数据源。"""
    print(f"\n=== {code} 实时价格数据源 ===\n")
    is_hk = code.endswith(".HK")
    is_a = "." not in code or code.endswith((".SS", ".SZ", ".BJ"))
    is_us = not is_hk and not is_a and "." not in code

    print("📈 实时价格 / 走势：")
    print(f"  Yahoo Finance: https://finance.yahoo.com/quote/{code}")
    print(f"  雪球:          https://xueqiu.com/S/{code}")
    if is_hk:
        print(f"  AAStocks:      https://www.aastocks.com/sc/stocks/analysis/company-fundamental/financial-statements/{code.replace('.HK','')}")
        print("\n📋 港股财报：")
        print("  港交所披露易：https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh")
        print(f"  公司年报 PDF（搜索代码 {code.replace('.HK','')}）")
    elif is_us:
        print(f"\n📋 美股 SEC 文件：")
        print(f"  EDGAR:         https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={code}&type=10-K&dateb=&owner=include&count=40")
        print(f"  10-K / 10-Q / 8-K 是关键披露文件")
    elif is_a:
        print(f"\n📋 A 股财报：")
        print(f"  巨潮资讯：https://www.cninfo.com.cn/")
        print(f"  东方财富：https://emweb.eastmoney.com/PC_HSF10/NewFinanceAnalysis/index?type=web&code={code}")

    print("\n💡 Python 一行验证：")
    print(f"  python3 -c \"import yfinance as yf; print(yf.Ticker('{code}').info)\"")


def show_source_tiers():
    """打印数据源权威等级表 - 写关键数字前先确认你用的是哪一档。"""
    print("""
╔══════════════════════════════════════════════════════════════╗
║              数据源权威等级表（按可信度分四档）              ║
╚══════════════════════════════════════════════════════════════╝

🟢 第一档：原始一手权威源（最可信，关键数字必须用）
─────────────────────────────────────────────────────
  美股 SEC EDGAR        sec.gov              13F-HR/10-K/10-Q 法定披露
  港交所披露易          hkexnews.hk          港股财报/公告法定披露
  巨潮资讯网            cninfo.com.cn        A 股法定披露
  公司官网投资者关系    各公司 IR 页         一手公告
  Yahoo Finance API     yfinance Python 库   实时股价（直接拉接口）

🔵 第二档：专业聚合站（可信，数据从一手源同步，但仍是二手）
─────────────────────────────────────────────────────
  达塔罗马              dataroma.com         价投大佬持仓追踪（业内公认）
  13F 信息站            13f.info             SEC 13F 数据归档（免费权威）
  巨鲸智慧              whalewisdom.com      13F 行业最权威平台（付费）
  价值投资聚合          valuesider.com       国际投资大佬持仓
  晨星                  morningstar.com      国际公认基本面评级
  对冲基金跟踪          hedgefollow.com      对冲基金动向
  大师聚焦              gurufocus.com        投资大师持仓+研究
  股票聚合站            stockzoa.com         基金持仓
  持仓频道              holdingschannel.com  13F 数据
  金融端                fintel.io            综合金融数据

🟡 第三档：国内权威财经媒体（可信但偏分析，不是原始数据）
─────────────────────────────────────────────────────
  华尔街见闻            wallstreetcn.com     国内深度财经（推荐）
  证券时报              stcn.com             证监会主管的官方报刊
  21 世纪经济报道       21jingji.com         南方报业财经
  澎湃新闻              thepaper.cn          上海报业权威媒体
  新浪财经              finance.sina.com.cn  综合财经门户
  虎嗅                  huxiu.com            科技商业深度
  财经网                caijing.com.cn       老牌财经
  东方财富网            dfcfw.com            国内最大财经平台（PDF 研报）
  雪球                  xueqiu.com           投资者社区+大佬发文（段永平）
  浦银国际              spdbi.com            投行研报（一手分析）
  交银国际              tdt.bocomgroup.com   投行研报（一手分析）
  第一上海证券          mystockhk.com        港股投行研报
  报告查                reportify.cn         财报追踪

🔴 第四档：要打折扣的源（不能作为关键判断依据）
─────────────────────────────────────────────────────
  Phemex 新闻           phemex.com/news      加密交易所旗下，编辑质量一般
  富途新闻              futunn.com           经纪商，立场可能有偏向
  13F 雷达              13radar.com          较新聚合站，权威度待考
  AInvest               ainvest.com          质量参差
  寻找阿尔法            seekingalpha.com     社区作者水平差异大
  TipRanks              tipranks.com         机器化分析师评级
  TIKR 博客             tikr.com/blog        投资数据博客
  TrustFinance 博客     trustfinance.com     二手分析
  知乎专栏              zhuanlan.zhihu.com   自媒体
  东财财富号            caifuhao.eastmoney   自媒体（不是东财官方研究）
  腾讯/网易新闻         news.qq.com/163.com  转载为主，需追溯原始源

⛔ 不能作为数据源（仅作灵感/趋势参考）
─────────────────────────────────────────────────────
  抖音截图              ❌ 信息图视觉语言会丢失上下文（22.6% 占比 vs 变动）
  小红书截图            ❌ 同上
  微信公众号自媒体      ❌ 除非追溯到原始权威源
  营销账号"投顾"        ❌ 业绩+低价+机构扫货话术=付费投教导流模板

📋 推荐使用顺序
─────────────────────────────────────────────────────
  关键数字（百分比/估值/财务）：
    第一档（必须）→ 第二档（交叉验证）→ 第三档（背景分析）

  趋势性判断（行业方向/赛道分析）：
    第二档 + 第三档 + 一手投行研报

  绝对禁止：仅依赖第四档 / 抖音截图就写入飞书
""")


def common_pitfalls():
    """打印「数据误读」常见陷阱清单 - 给未来的自己看。"""
    print("""
╔══════════════════════════════════════════════════════════════╗
║         数据误读常见陷阱（每次写关键数字前必读）             ║
╚══════════════════════════════════════════════════════════════╝

🔴 陷阱 1：13F 图表上的 "%" 是【仓位占比】还是【变动幅度】？
   - 抖音/小红书/东方财富图解类的可视化经常把这两个混在一起
   - 解读时务必去 Dataroma / 13f.info 看具体的"Activity"列
     → 应是 "Add 4.32%" / "Reduce 7.09%" / "New" / "Sold Out"
   - 真实案例：「巴菲特减持 22.6%」 实际是「持仓占比 22.6%，减幅 4.3%」

🔴 陷阱 2：港股代码 ≠ 美股 ADR 代码
   - 9988.HK（阿里港股）和 BABA（NYSE ADR）经济权益等价但是不同实体
   - 13F 只披露美股；段永平/高瓴持仓里的"阿里"是 BABA 不是 9988.HK
   - 要标清楚：「13F 持仓为 BABA ADR，与 9988.HK 经济权益等价」

🔴 陷阱 3：13F 数据滞后 45 天
   - Q4'25 的 13F 在 2026/2/14 前才公布
   - 看到时已是 1.5 个月前持仓，可能早卖了
   - 永远标 "Q4'25 数据，公布于 2026/2 月" 而不是当前持仓

🔴 陷阱 4：财报"营收增长"基数效应
   - SanDisk "+1200%" 是因为分拆初期低基数
   - 高新行业 "+200%" 是因为去年微利/亏损
   - 看绝对值 + 行业平均，不要只看百分比

🔴 陷阱 5：营销账号"业绩+低价+机构扫货"话术
   - 这是付费投教导流的标准模板（如「博众证券投教」）
   - 三者共振、不可能三角、脱胎换骨 = 反向指标

🔴 陷阱 6：抖音/小红书的产业链图
   - 标的归类通常正确（哪些公司属于 CPO / PCB 等）
   - 但「未来主线预测」是猜测；标的有效，预测无效

🟢 反向验证 SOP：
  1. 看到关键数字 → 反问「这是占比还是变动？」
  2. 二手聚合源 → 必去原始权威源（SEC / 港交所 / 公司官网）
  3. 写到飞书前 → 用 verify_data_sources.py 拉对应权威源对照
  4. 异常大数字（>100%）→ 一定追问数据源
""")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        common_pitfalls()
        return

    cmd = sys.argv[1].lower()
    if cmd == "13f":
        if len(sys.argv) < 3:
            print("用法：verify_data_sources.py 13f <BRK|H&H|Hillhouse>")
            return
        show_13f(sys.argv[2])
    elif cmd == "price":
        if len(sys.argv) < 3:
            print("用法：verify_data_sources.py price <ticker>")
            return
        show_price(sys.argv[2])
    elif cmd in ("pitfalls", "trap", "warn"):
        common_pitfalls()
    else:
        print(f"未知命令：{cmd}")
        print("可选：13f <guru> | price <ticker> | pitfalls")


if __name__ == "__main__":
    main()
