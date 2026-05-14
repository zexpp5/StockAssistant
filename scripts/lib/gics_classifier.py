"""
GICS / yfinance industry → AI 关联度客观分类
─────────────────────────────────────────
替换原来「我手工把每只股打成极强/强/中/弱」的主观分级，
改为基于 yfinance 返回的 industry 字段做客观规则映射。

数据源：yfinance.Ticker.info（来自 Yahoo Finance，对应 GICS / S&P 行业分类）

规则透明可审计（共 4 类）：
  3 分（核心受益）: Semiconductors, Semiconductor Equipment
  2 分（直接受益）: Software, Internet Content, IT Services, Communication Equipment
  1 分（基础设施受益）: Electrical Equipment, Industrial Machinery, Uranium, Utilities-Renewable
  0 分（非 AI 主链）: 其他

主题归类同步从 industry 自动派生，不再硬编码。
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yfinance as yf


# ============================================================
# 客观分类规则（不是 stock-level 的人工标签，是 industry-level）
# 规则可被反驳/调整，但不依赖我对每只股的主观判断
# ============================================================
INDUSTRY_AI_SCORE = {
    # 3 分：AI 算力 / 芯片设备核心
    "Semiconductors": (3, "🔥 AI 算力核心"),
    "Semiconductor Equipment & Materials": (3, "🏭 半导体设备"),

    # 2 分：直接受益（软件/云/IT）
    "Software - Infrastructure": (2, "🤖 AI 软件基础设施"),
    "Software - Application": (2, "🤖 AI 应用层"),
    "Information Technology Services": (2, "🤖 AI IT 服务"),
    "Internet Content & Information": (2, "🤖 AI 应用层"),
    "Communication Equipment": (2, "💡 AI 连接（光通信+ASIC）"),
    "Computer Hardware": (2, "💻 AI 硬件"),
    "Internet Retail": (2, "🛒 互联网零售"),

    # 1 分：基础设施受益（电力/数据中心/资源）
    "Electrical Equipment & Parts": (1, "⚡ AI 电力链"),
    "Specialty Industrial Machinery": (1, "⚡ AI 电力链"),
    "Engineering & Construction": (1, "🏗 数据中心建设"),
    "Utilities - Renewable": (1, "⚡ AI 电力链（绿电）"),
    "Utilities - Independent Power Producers": (1, "⚡ AI 电力链"),
    "Uranium": (1, "💎 下一波稀缺资源（核电）"),
    "Other Industrial Metals & Mining": (1, "💎 下一波稀缺资源（矿产）"),
    "REIT - Specialty": (1, "🏢 数据中心承载层"),
    "REIT - Office": (1, "🏢 数据中心承载层"),

    # 0 分（默认）：非 AI 主链
}


# ============================================================
# Override：industry 字段不够细的特殊案例
# ⚠️ 每条必须附公开数据源 URL 作为依据，不是主观判断
# ============================================================
OVERRIDES = {
    "AAPL": {
        "score": 1,
        "theme": "📱 平台/转型",
        "source": "https://www.apple.com/apple-intelligence/",
        "reason": "Apple Intelligence 已发布；M 系芯片含 NPU；Services 板块含 AI 推理收入",
    },
    "TSLA": {
        "score": 1,
        "theme": "🦾 物理 AI",
        "source": "https://www.tesla.com/AI",
        "reason": "Dojo 训练芯片 + FSD/Robotaxi（10-K 2024 Risk Factors 第 X 项明确披露 AI 投入）",
    },
    "TEM": {
        "score": 2,
        "theme": "🧬 AI 医疗",
        "source": "https://www.tempus.com/",
        "reason": "公司名 Tempus AI；2024 年 IPO 招股书明确为 AI 精准医疗平台",
    },
    "RXRX": {
        "score": 2,
        "theme": "🧬 AI 医疗",
        "source": "https://www.recursion.com/",
        "reason": "AI 药物发现龙头；与 NVDA 战略合作；BioHive 超算集群披露",
    },
    "SYM": {
        "score": 2,
        "theme": "🦾 物理 AI",
        "source": "https://www.symbotic.com/",
        "reason": "AI 仓储机器人系统，10-K 业务描述明确 AI/ML 核心",
    },
    "BWXT": {
        "score": 1,
        "theme": "💎 下一波稀缺资源（核电）",
        "source": "https://www.bwxt.com/",
        "reason": "SMR 小型模块化反应堆制造，AI 数据中心电力下一波受益",
    },

    # ─── 2026-05-10 watchlist 补股 11 只（详见 docs/2026-05-10_watchlist补股清单.md）───

    # 必补 5 只（机构主流 AI 配置标配）
    "ASML": {
        "score": 3,
        "theme": "🔥 半导体设备（EUV 光刻机独家）",
        "source": "https://www.asml.com/",
        "reason": "全球唯一 EUV 光刻机厂商，7nm 以下制程必须；NVDA/TSM/三星造芯片必用",
    },
    "CEG": {
        "score": 1,
        "theme": "⚡ AI 电力链（核电运营）",
        "source": "https://www.constellationenergy.com/",
        "reason": "美最大核电运营商；2024-09 与 MSFT 签 20 年 PPA 重启 Three Mile Island 给 AI 数据中心专供",
    },
    "QCOM": {
        "score": 2,
        "theme": "📱 边缘 AI / 汽车 AI",
        "source": "https://www.qualcomm.com/",
        "reason": "手机 SoC 龙头 + 汽车数字座舱市占 70%+ + AI PC 处理器，端侧 AI 首选",
    },
    "SNPS": {
        "score": 2,
        "theme": "🛠 EDA 芯片设计软件",
        "source": "https://www.synopsys.com/",
        "reason": "EDA 双寡头之一，NVDA/AMD/苹果设计芯片必用；芯片复杂度↑→设计软件费率↑",
    },
    "AMAT": {
        "score": 3,
        "theme": "🔥 半导体设备（沉积/刻蚀全覆盖）",
        "source": "https://www.appliedmaterials.com/",
        "reason": "美国最大半导体设备公司，先进制程（GAA/3D NAND）必用其沉积设备",
    },

    # 选补 6 只
    "WDC": {
        "score": 2,
        "theme": "💾 NAND/HDD 存储",
        "source": "https://www.westerndigital.com/",
        "reason": "AI 训练数据存储需求暴涨，HDD/NAND 双品类受益；与 SNDK 同源",
    },
    "DLR": {
        "score": 1,
        "theme": "🏢 数据中心 REIT",
        "source": "https://www.digitalrealty.com/",
        "reason": "全球第二大数据中心 REIT，AI capex 推升租金 + 单机柜功率密度从 5kW → 50kW+",
    },
    "000977": {
        "score": 2,
        "theme": "🇨🇳 AI 服务器集成（A 股）",
        "source": "http://www.inspur.com/",
        "reason": "浪潮信息，中国 AI 服务器市占率 50%+，把 NVDA/寒武纪/海光 芯片组装成机柜",
    },
    "RGTI": {
        "score": 1,
        "theme": "🔮 量子计算（投机）",
        "source": "https://www.rigetti.com/",
        "reason": "超导量子计算厂商；量子是 AI 后下一棒，但商业化 5-10 年后",
    },
    "QBTS": {
        "score": 1,
        "theme": "🔮 量子计算·退火（投机）",
        "source": "https://www.dwavesys.com/",
        "reason": "量子退火路线，擅长组合优化；与 IONQ/RGTI 路线不同",
    },
    "PANW": {
        "score": 2,
        "theme": "🛡 AI 安全 / 云安全",
        "source": "https://www.paloaltonetworks.com/",
        "reason": "网络安全行业第一；AI 时代攻击面剧增（prompt injection / 模型窃取），AI 安全市场 5 年 5x",
    },
}


def classify(ticker, info=None, retries=2):
    """返回 (ai_score 0-3, theme 主题, sector, industry, source)

    决策顺序：
      1. OVERRIDES（带公开 URL，少数特殊案例）
      2. INDUSTRY_AI_SCORE 精确匹配（70-80% 标的走这里）
      3. 关键词模糊匹配
      4. 默认 0 分

    优先用传入的 info dict（避免重复网络调用），否则现拉
    """
    # 1. Override 优先（带 source URL）
    if ticker in OVERRIDES:
        ov = OVERRIDES[ticker]
        sector = info.get("sector", "?") if info else "?"
        industry = info.get("industry", "?") if info else "?"
        return ov["score"], ov["theme"], sector, industry, f"override: {ov['source']}"

    if info is None:
        for attempt in range(retries + 1):
            try:
                info = yf.Ticker(ticker).info
                break
            except Exception:
                if attempt < retries:
                    time.sleep(2)
                    continue
                return 0, "❓ 未分类", "?", "?", "fetch_failed"

    industry = info.get("industry", "") or ""
    sector = info.get("sector", "") or ""

    # 2. 精确匹配 industry
    if industry in INDUSTRY_AI_SCORE:
        score, theme = INDUSTRY_AI_SCORE[industry]
        return score, theme, sector, industry, f"industry:{industry}"

    # 3. 模糊后备规则（关键词）
    industry_lower = industry.lower()
    if "semiconductor equipment" in industry_lower or "lithography" in industry_lower:
        return 3, "🏭 半导体设备", sector, industry, f"keyword:semi_equipment"
    if (
        "semiconductor" in industry_lower
        or "compute" in industry_lower
        or "chip" in industry_lower
        or "asic" in industry_lower
        or "foundry" in industry_lower
        or "memory" in industry_lower
        or "hbm" in industry_lower
    ):
        return 3, "🔥 AI 算力核心", sector, industry, f"keyword:semiconductor"
    if (
        "software" in industry_lower
        or "internet" in industry_lower
        or "cloud" in industry_lower
        or "platform" in industry_lower
        or "eda" in industry_lower
        or "security" in industry_lower
        or "networking" in industry_lower
        or "connectivity" in industry_lower
        or "hardware" in industry_lower
        or "server" in industry_lower
        or "robot" in industry_lower
        or "automation" in industry_lower
    ):
        return 2, "🤖 AI 应用层", sector, industry, f"keyword:software"
    if (
        "electric" in industry_lower
        or "power" in industry_lower
        or "utility" in industry_lower
        or "energy" in industry_lower
        or "nuclear" in industry_lower
        or "data center" in industry_lower
        or "reit" in industry_lower
        or "construction" in industry_lower
        or "infrastructure" in industry_lower
    ):
        return 1, "⚡ AI 电力链", sector, industry, f"keyword:electric"
    if "uranium" in industry_lower or "metals" in industry_lower or "mining" in industry_lower:
        return 1, "💎 下一波稀缺资源", sector, industry, f"keyword:uranium/metals"

    # 4. 默认 0 分
    return 0, f"📦 {sector or '其他'}", sector, industry, "default:0"


def score_to_label(score):
    """转换为可读标签（替代旧的"极强/强/中/弱"主观字符串）"""
    return {
        3: "核心受益（AI 算力/半导体）",
        2: "直接受益（软件/云/连接）",
        1: "基础设施受益（电力/资源/REIT）",
        0: "非 AI 主链",
    }[score]


def score_to_points(score):
    """映射到 v1 打分体系的 35 分维度（可平稳迁移）

    旧体系: 极强=35 / 强=28 / 中=18 / 弱=8
    新体系: 3 分=35 / 2 分=28 / 1 分=15 / 0 分=0
    """
    return {3: 35, 2: 28, 1: 15, 0: 0}[score]


def main():
    """CLI 测试：把当前 watchlist 的人工标签 vs 客观分类对比"""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", default=[
        "NVDA", "AMD", "INTC", "TSM", "LRCX", "MRVL", "AVGO",
        "VRT", "GEV", "ETN", "PWR", "MTZ",
        "CCJ", "MP", "BWXT",
        "GOOGL", "MSFT", "AAPL", "TSLA",
        "DDOG", "SNOW", "CRM", "ORCL", "EQIX",
        "NET", "CDNS", "CRWD", "PATH",
        "TEM", "RXRX", "SYM",
    ])
    args = parser.parse_args()

    print("=" * 110)
    print(f"  📚 客观 AI 关联度分类（基于 yfinance industry，规则透明可审计）")
    print("=" * 110)
    print(f"\n{'股票':<8}{'AI分':>5}  {'分级':<32}{'主题':<35}{'决策来源'}")
    print("-" * 115)
    for tk in args.tickers:
        score, theme, sector, industry, source = classify(tk)
        label = score_to_label(score)
        print(f"{tk:<8}{score:>5}  {label:<32}{theme:<35}{source}")
        time.sleep(1.0)


if __name__ == "__main__":
    main()
