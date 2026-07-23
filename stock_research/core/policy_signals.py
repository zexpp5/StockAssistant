"""政策大事雷达 — 纯打分函数（无 IO，可单测）。

回答一个问题：这条新闻是不是「国家级、可能影响市场方向的大动作」？

起因（2026-07-21 用户）：7-13 国务院批复消费"十五五"规划、7-17 习出席AI大会、
7-19 国新500亿增持——全是公开新闻，市场 7-21 才全面爆发，中间 1-3 天关注窗口
系统本该扫到并提醒。方案 docs/V2/2026-07-21_政策大事雷达_方案.md。

设计红线：宁可漏报不滥报。三层关键词打分，高门槛（主体+动作缺一不可）：
  主体(谁说的,2分)   —— 中共中央/国务院/央行/证监会/国家队…（级别不够不算）
  动作(干了什么,2分) —— 批复/规划/降准/增持/万亿…（表态不算,要有真动作）
  方向(利好谁,1分/个)—— 映射到可交易的受益方向
score >= 4 且至少命中一个方向 → 信号。

诚实边界：这是「第一时间提醒」不是「提前知道」；新闻→涨跌的映射不保证对；
提醒关注 ≠ 建议买入（advisory，真钱动作永远用户拍板）。
"""
from __future__ import annotations

import re
from typing import Any

# ── 主体：谁说的（国家级才算，2 分）─────────────────────────────────────────
ACTORS: dict[str, str] = {
    "中共中央": "中央", "国务院": "国务院", "中央政治局": "政治局",
    "习近平": "最高层", "李强": "总理",
    "中国人民银行": "央行", "央行": "央行", "证监会": "证监会",
    "财政部": "财政部", "发改委": "发改委", "国家发展改革委": "发改委",
    "中央汇金": "国家队", "中国国新": "国家队", "中国诚通": "国家队",
    "国资委": "国资委", "全国人大": "人大", "中央经济工作会议": "中央会议",
    "中央金融工作会议": "中央会议",
}

# ── 动作：干了什么（要真动作不要表态，2 分）─────────────────────────────────
ACTION_PATTERNS: list[tuple[str, str]] = [
    (r"批复|印发|正式发布|出台", "文件落地"),
    (r"十五五|五年规划|专项规划|行动方案|行动计划", "规划"),
    (r"降准|降息|下调.{0,8}(利率|准备金率)|存款准备金率|LPR", "货币宽松"),
    (r"再贷款|互换便利|平准基金", "稳市工具"),
    (r"增持|回购", "真金白银买入"),
    (r"专项债|特别国债|贴息", "财政资金"),
    (r"([一二三四五六七八九十\d千百]+)万亿|[五六七八九十百千\d]+00亿|超\d+亿", "大额资金"),
    (r"试点|扩围|放开|放宽|取消.{0,8}限制", "松绑/试点"),
    # 领导人出席+讲话：会议全名可以很长(如"2026世界人工智能大会暨…高级别会议开幕式")
    (r"出席.{0,50}(发表.{0,10}讲话|主旨讲话|致辞)", "领导人站台"),
]

# ── 方向：利好谁（映射到可交易受益方向，1 分/个）────────────────────────────
THEME_MAP: dict[str, str] = {
    r"人工智能|AI|大模型|算力|智能经济": "AI/算力（科创50、半导体链）",
    r"消费|以旧换新|内需|服务业|文旅|免税": "消费（白酒/家电/免税，消费ETF）",
    r"半导体|集成电路|芯片|光刻": "半导体自主（设备/材料）",
    r"新能源|光伏|风电|储能|电动汽车|动力电池": "新能源链",
    r"机器人|智能制造|工业母机": "机器人/高端制造",
    r"低空经济|商业航天|卫星": "低空/航天",
    r"资本市场|股市|股票市场|上市公司|A股|投资者": "大盘/券商（宽基ETF）",
    r"数字经济|数据要素|信创": "数字经济/信创",
    r"房地产|楼市|保障房|城中村": "地产链",
    r"生育|养老|银发|医保|创新药|医药|健康|卫生|医疗": "医药/银发经济",
    r"军工|国防": "军工",
    r"央企|国企改革|中国特色估值": "央企（央企ETF）",
}

SIGNAL_THRESHOLD = 4

# 排除：不是"政策动作"的常见假信号（回放 7-13~7-20 实测校准）
#   统计发布 — GDP/增加值是"报数"不是"出政策"
#   宣传专栏 — 《新思想引领新征程》等系列稿,是回顾成就不是新动作
#   礼仪外事 — 会见/致电/出访,与市场方向无关
EXCLUDE_TITLE_PATTERNS = [
    r"GDP|同比增长|增加值|统计局|创新高|数据显示",
    r"新思想引领新征程|奋进|礼赞|巡礼|谱写|新篇章",
    r"会见|会晤|致电|致贺|吊唁|出访|访问|抵达|欢迎宴|通电话",
    r"座谈会$",   # 座谈会=听意见,还没到动作;若内容含真金白银会被快讯另行报道
    r"积极评价|引发热议|反响热烈|媒体.{0,4}评价|央视快评|国际.{0,4}评价",  # 评论/反应≠政策动作
]


def is_excluded(title: str) -> bool:
    t = str(title or "")
    return any(re.search(p, t) for p in EXCLUDE_TITLE_PATTERNS)


def score_news(title: str, content: str = "") -> dict[str, Any]:
    """给一条新闻打「国家级大动作」分。标题权重高：主体/动作在标题命中才拿满分。

    返回 {score, actors, actions, themes, benefit_directions, is_signal}
    """
    title = str(title or "")
    content = str(content or "")
    if is_excluded(title):
        return {"score": 0, "actors": [], "actions": [], "themes": [],
                "is_signal": False, "excluded": True}
    head = title + " " + content[:400]     # 主体/动作只看标题+导语，防长文误命中
    full = title + " " + content

    actors = sorted({label for kw, label in ACTORS.items() if kw in head})
    actions = sorted({label for pat, label in ACTION_PATTERNS if re.search(pat, head)})
    themes = sorted({direction for pat, direction in THEME_MAP.items()
                     if re.search(pat, full, re.IGNORECASE)})
    # 货币宽松/稳市工具/国家队买入 = 全市场流动性事件,本身无板块但直接利好大盘。
    # 没匹配到具体板块时兜底给「大盘」方向,避免降准这种最大信号因"无方向"被漏。
    if not themes and ({"货币宽松", "稳市工具", "真金白银买入"} & set(actions)):
        themes = ["大盘/全市场（宽基ETF）"]

    score = 0
    if actors:
        score += 2
    if actions:
        score += 2
    score += min(len(themes), 2)   # 方向最多计 2 分,防长文堆方向刷分

    return {
        "score": score,
        "actors": actors,
        "actions": actions,
        "themes": themes,
        "is_signal": bool(score >= SIGNAL_THRESHOLD and actors and actions and themes),
    }


def signal_line(title: str, verdict: dict[str, Any]) -> str:
    """一行人话：谁·干了什么·利好方向。"""
    who = "/".join(verdict["actors"][:2]) or "?"
    what = "/".join(verdict["actions"][:2]) or "?"
    to = "；".join(verdict["themes"][:2]) or "?"
    return f"【{who}·{what}】{title[:40]} → 关注方向: {to}"
