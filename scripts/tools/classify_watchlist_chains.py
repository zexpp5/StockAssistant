"""一次性脚本:为 watchlist 89 条记录填充 chain / chain_tier / chain_role / layman_intro。

设计:
  • 只 UPDATE 4 个新字段,不动 business / ai_logic 等已有字段
  • chain 允许多链,逗号分隔
  • chain_tier 枚举:核心 / 一线 / 二线 / 三线 / N/A
  • chain_role 枚举:IDM / GPU / 设备 / 材料 / 封测 / EDA / 网络芯片 / 服务器 /
                  应用层 / 基础设施 / 服务 / 对照
  • layman_intro 不超过 60 字
"""
import os, sys
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))  # 2026-05-11 lib 迁移
from stock_db import get_db

CLASSIFICATIONS = [
    # ───── HBM 链条(SK 海力士所在) ─────
    ("000660.KS", "HBM,AI 算力", "核心", "IDM", "全球 HBM 内存第一,NVIDIA 主供应商,AI GPU 必须配它才跑得动模型"),
    ("MU",        "HBM,AI 算力", "核心", "IDM", "全球第三大内存厂,HBM3E 已切入 NVDA,叙事最强但份额最小"),
    ("SNDK",      "HBM,AI 算力", "二线", "IDM", "闪迪 NAND+存储,AI 存储隐性受益,弹性比 WDC 高"),
    ("WDC",       "HBM,AI 算力", "三线", "IDM", "西部数据 HDD+SSD,AI 存储隐性受益,边缘标的"),

    # ───── 半导体设备(HBM 设备链 + 通用设备) ─────
    ("ASML",      "半导体设备",   "核心", "设备", "全球唯一 EUV 光刻机厂商,7nm 以下必用,垄断高端光刻"),
    ("AMAT",      "半导体设备,HBM", "一线", "设备", "美国最大半导体设备公司,沉积/刻蚀/CMP 全覆盖,HBM 堆叠也用"),
    ("LRCX",      "半导体设备,HBM", "一线", "设备", "全球第三大半导体设备,HBM TSV 硅通孔刻蚀龙头"),
    ("8035.T",    "半导体设备,HBM", "一线", "设备", "东京电子,全球第三大半导体设备,HBM/3D 堆叠核心工艺"),
    ("6857.T",    "HBM,半导体设备", "一线", "设备", "Advantest,全球最大半导体测试设备之一,HBM 测试龙头"),

    # ───── EDA(半导体设计) ─────
    ("SNPS",      "EDA",         "核心", "EDA", "Synopsys,全球第一 EDA 软件,所有 AI 芯片设计的基础设施"),
    ("CDNS",      "EDA",         "核心", "EDA", "Cadence,全球第二 EDA,与 Synopsys 双寡头,AI 芯片设计供应商"),

    # ───── AI 算力 / GPU / ASIC ─────
    ("NVDA",      "AI 算力",      "核心", "GPU", "全球 AI GPU 第一(90%+ 份额),AI 时代最核心标的"),
    ("AMD",       "AI 算力",      "一线", "GPU", "NVDA 之外唯一可能成功的 AI GPU 选项,数据中心 Q1 +57%"),
    ("AVGO",      "AI 算力",      "核心", "网络芯片", "Broadcom,AI 定制 ASIC(Google TPU/Meta MTIA)+网络芯片"),
    ("MRVL",      "AI 算力,光通信", "一线", "网络芯片", "Marvell,光学 DSP 全球 70%+定制 ASIC(Amazon Trainium)"),
    ("ALAB",      "AI 算力",      "二线", "网络芯片", "Astera Labs,AI 数据中心连接芯片(PCIe6 + Fabric Switch)"),
    ("ARM",       "AI 算力",      "一线", "IDM", "全球 ARM 架构 IP 授权,数据中心 ARM CPU 渗透 + AI 加速器 IP"),
    ("INTC",      "AI 算力,半导体设备", "二线", "IDM", "Intel x86 CPU + Foundry 晶圆代工 + Gaudi AI 推理,转型加速中"),
    ("TSM",       "AI 算力,半导体设备", "核心", "封测", "台积电,全球晶圆代工龙头,NVDA/AMD/AAPL 芯片都靠它制造"),
    ("QCOM",      "消费 AI,AI 算力", "一线", "GPU", "高通,手机芯片+汽车 AI+边缘 AI,端侧算力第一层"),

    # ───── AI 算力国产替代(A 股) ─────
    ("688256",    "AI 算力",      "核心", "GPU", "寒武纪,A 股 AI 算力国产替代第一龙头,国产 AI 芯片设计"),
    ("688041",    "AI 算力",      "一线", "GPU", "海光信息,A 股 AI 算力国产替代第二,国产 CPU + DCU(类 GPU)"),
    ("000977",    "AI 算力",      "一线", "服务器", "浪潮信息,中国 AI 服务器集成第一(份额 50%+),组装 NVDA/寒武纪芯片"),

    # ───── 光通信(光模块 + DSP + 光纤) ─────
    ("300308",    "光通信",       "核心", "网络芯片", "中际旭创,全球高速光模块第一,800G/1.6T,深度绑定 NVDA/MSFT/Meta"),
    ("300502",    "光通信",       "一线", "网络芯片", "新易盛,A 股光模块第二,紧随中际旭创,主供北美 hyperscaler"),
    ("688635",    "光通信",       "三线", "材料",    "长进光子,科创板,光通信上游特种光纤(掺镱/光子晶体),小盘"),

    # ───── 数据中心电力 ─────
    ("VRT",       "数据中心电力,数据中心冷却", "核心", "设备", "Vertiv,数据中心电力管理+冷却双龙头,AI Capex 直接受益最大"),
    ("ETN",       "数据中心电力",  "核心", "设备", "Eaton,数据中心电力供应最大受益方,Q1 数据中心订单 +240%"),
    ("GEV",       "数据中心电力",  "核心", "设备", "GE Vernova,GE 拆出的电力业务,数据中心电气化订单 $24 亿"),
    ("CEG",       "数据中心电力,核电", "核心", "基础设施", "Constellation,美国最大核电运营商,2024 与微软签 20 年 PPA"),
    ("VST",       "数据中心电力",  "一线", "基础设施", "Vistra,独立电力商+核电,给 AI 数据中心供电"),
    ("PWR",       "数据中心建设",  "核心", "服务", "Quanta Services,美国电力基建第一,数据中心电力交付"),
    ("MTZ",       "数据中心建设",  "一线", "服务", "MasTec,美国基建,通信+清洁能源+输配电+数据中心 MEP"),

    # ───── 数据中心冷却 ─────
    ("MOD",       "数据中心冷却",  "一线", "设备", "Modine,美股数据中心冷却隐形冠军,Q3 数据中心 +78% YoY"),
    ("XYL",       "数据中心冷却",  "三线", "设备", "Xylem,水处理+数据中心水冷,间接受益"),
    ("002837",    "数据中心冷却",  "一线", "设备", "英维克,A 股液冷第一龙头,给浪潮/华为/腾讯做液冷"),

    # ───── 数据中心 REIT ─────
    ("EQIX",      "数据中心 REIT", "核心", "基础设施", "Equinix,全球数据中心互联龙头,260+ 数据中心,Q1 大单 60% 是 AI"),
    ("DLR",       "数据中心 REIT", "一线", "基础设施", "Digital Realty,全球第二大数据中心 REIT,300+ 数据中心"),

    # ───── 核电(AI 用电主线) ─────
    ("CCJ",       "核电",         "核心", "材料", "Cameco,全球第二大铀矿,持股西屋 49%,核能复兴最大受益方"),
    ("KAP.IL",    "核电",         "核心", "材料", "Kazatomprom,全球最大铀矿(哈萨克),占全球铀产量 22-25%"),
    ("BWXT",      "核电",         "一线", "设备", "美国核动力技术龙头,海军核动力+SMR+TRISO 核燃料"),
    ("LEU",       "核电",         "二线", "材料", "Centrus,美国唯一国产铀浓缩,HALEU 是 SMR 卡脖子环节"),
    ("UUUU",      "核电,稀土",     "二线", "材料", "Energy Fuels,铀+稀土双主题,美国本土铀生产"),
    ("OKLO",      "核电",         "二线", "设备", "Oklo,SMR 主题最纯种早期标的,预营收,~14 GW 客户管线"),
    ("SMR",       "核电",         "二线", "设备", "NuScale Power,SMR(NRC 已认证),首家美国 SMR 监管批准"),
    ("NNE",       "核电",         "三线", "设备", "NANO Nuclear,微反应堆开发商,5/6 与 Supermicro 签 MOU"),
    ("RYCEY",     "核电",         "二线", "设备", "Rolls-Royce,SMR+航空发动机,英国国家级 SMR 标的"),

    # ───── 稀土(永磁材料,AI/电动车/国防共用) ─────
    ("MP",        "稀土",         "核心", "材料", "MP Materials,美国唯一全产业链稀土,加州矿+德州磁铁厂"),
    ("LYC.AX",    "稀土",         "一线", "材料", "Lynas,澳洲稀土加工,西方最大稀土加工商"),
    ("600111",    "稀土",         "核心", "材料", "北方稀土,中国稀土第一龙头,轻稀土 NdPr 全球定价权"),

    # ───── AI 应用层(SaaS / Agentic / 大模型) ─────
    ("MSFT",      "AI 应用",      "核心", "应用层", "微软,Azure+OpenAI 双引擎,Azure 增速 40%,Copilot 全家桶"),
    ("GOOGL",     "AI 应用",      "核心", "应用层", "Alphabet,AI 全栈最完整(Gemini+TPU+Google Cloud+Search)"),
    ("META",      "AI 应用",      "核心", "应用层", "Meta,Facebook/Insta/WhatsApp+Llama 开源,2026 Capex $1250+ 亿"),
    ("AMZN",      "AI 应用",      "核心", "应用层", "Amazon,全球最大云(AWS)+Trainium 自研+Anthropic 大股东"),
    ("ORCL",      "AI 应用",      "一线", "应用层", "Oracle,ERP/数据库+OCI 云,主权 AI 浪潮赢家(Stargate UAE)"),
    ("CRM",       "AI 应用",      "核心", "应用层", "Salesforce,全球最大 CRM,Agentforce 是 Agentic AI 商业化最大方"),
    ("NOW",       "AI 应用",      "核心", "应用层", "ServiceNow,企业 IT 工作流 SaaS,Now Assist 是企业 AI 首选"),
    ("PLTR",      "AI 应用",      "核心", "应用层", "Palantir,美股 AI 应用层政府国防龙头,AIP 平台,商业+政府双驱动"),
    ("SNOW",      "AI 应用",      "核心", "服务", "Snowflake,数据云+AI 应用层,Cortex AI 平台"),
    ("MDB",       "AI 应用",      "一线", "服务", "MongoDB,全球最大 NoSQL+Atlas Vector Search(AI 向量)"),
    ("DDOG",      "AI 应用",      "一线", "服务", "Datadog,云原生应用监控龙头,AI 工作负载监控,Bits AI"),
    ("CFLT",      "AI 应用",      "二线", "服务", "Confluent,Kafka 商业化龙头,流数据,Agentic AI 的'神经系统'"),
    ("NET",       "AI 应用",      "一线", "服务", "Cloudflare,边缘网络+Workers AI,AI 推理从云转边缘最大受益方"),
    ("PATH",      "AI 应用",      "二线", "应用层", "UiPath,Agentic AI 平台+RPA,Maestro 编排'数字员工'"),
    ("RDDT",      "AI 应用",      "二线", "服务", "Reddit,社交媒体+AI 训练数据(Google/OpenAI 都买它数据)"),

    # ───── AI 应用(中国 - 港股 + A 股) ─────
    ("0700.HK",   "AI 应用",      "核心", "应用层", "腾讯,微信+游戏+混元大模型+元宝 AI App,2026 AI 投入翻倍"),
    ("9988.HK",   "AI 应用",      "核心", "应用层", "阿里巴巴,阿里云+通义千问 Qwen,中国 AI 应用层最重要标的"),
    ("0020.HK",   "AI 应用",      "二线", "应用层", "商汤,中国 CV 龙头,生成式 AI(日日新)+CV+智慧城市"),
    ("002230",    "AI 应用",      "一线", "应用层", "科大讯飞,中国语音 AI 国家队,星火大模型+智慧教育"),
    ("688111",    "AI 应用",      "一线", "应用层", "金山办公,WPS Office+WPS AI,中国 AI 办公国产化龙头"),
    ("3690.HK",   "AI 应用",      "三线", "应用层", "美团,生活服务超 App,本身不做 AI,领投月之暗面 $20 亿是间接受益"),

    # ───── AI 安全 ─────
    ("CRWD",      "AI 安全",      "核心", "服务", "CrowdStrike,全球网络安全龙头,Falcon AIDR AI 安全平台"),
    ("PANW",      "AI 安全",      "核心", "服务", "Palo Alto,网络安全行业第一,AI 时代攻击面剧增最大受益方"),

    # ───── AI 算力租赁 ─────
    ("CRWV",      "AI 算力租赁",   "核心", "服务", "CoreWeave,'AI 算力卖水人',租 GPU 给 OpenAI/微软,营收一年翻 2 倍"),

    # ───── AI 投资(金融) ─────
    ("9984.T",    "AI 投资",      "核心", "服务", "SoftBank,日本最大 AI 投资集团,OpenAI 主投人+Arm 大股东"),

    # ───── 端侧 AI / 消费 AI ─────
    ("AAPL",      "消费 AI",      "核心", "应用层", "苹果,iPhone+Apple Intelligence 端侧 AI,兑现节奏慢但用户基数最大"),

    # ───── AI 医疗 ─────
    ("RXRX",      "AI 医疗",      "一线", "应用层", "Recursion,AI 药物发现,与 NVDA 合作,小盘高弹性"),
    ("SDGR",      "AI 医疗",      "二线", "应用层", "Schrödinger,AI 药物发现+材料模拟"),
    ("TEM",       "AI 医疗",      "一线", "应用层", "Tempus AI,AI 医疗/精准医疗,肿瘤数据+测序+AI"),
    ("VEEV",      "AI 医疗",      "一线", "应用层", "Veeva,医疗 SaaS+垂直 AI,药企/医院都用它"),

    # ───── 物理 AI / 机器人 ─────
    ("TSLA",      "物理 AI",      "核心", "IDM", "Tesla,电动车+FSD 自动驾驶+Optimus 人形机器人"),
    ("SYM",       "物理 AI",      "一线", "应用层", "Symbotic,仓储机器人/物理 AI,Walmart/亚马逊都是客户"),

    # ───── 量子计算 ─────
    ("IONQ",      "量子计算",     "核心", "IDM", "IonQ,全球离子阱量子计算机龙头,Q1 营收 +755% YoY"),
    ("QBTS",      "量子计算",     "二线", "IDM", "D-Wave,退火量子计算,量子优化潜力(投机性)"),
    ("RGTI",      "量子计算",     "二线", "IDM", "Rigetti,超导量子计算,量子机器学习潜力(投机性)"),

    # ───── 对照组(消费/反面教材) ─────
    ("KO",        "对照组",       "N/A", "对照", "可口可乐,饮料龙头,与 AI 几乎无关联,作为消费对照"),
    ("MCD",       "对照组",       "N/A", "对照", "麦当劳,快餐连锁,AI 仅是门店运营工具,作为消费对照"),
    ("9992.HK",   "对照组",       "N/A", "对照", "泡泡玛特,潮玩 IP(LABUBU/Molly),非 AI 赛道,作为消费对照"),
    ("APX.AX",    "AI 应用",      "三线", "服务", "Appen,AI 数据标注反面教材,股价从 A$25 跌到 A$1.5,警示价值大"),
]


def main():
    conn = get_db()
    n_updated = 0
    n_missing = []
    for code, chain, tier, role, intro in CLASSIFICATIONS:
        cur = conn.execute(
            "UPDATE watchlist SET chain=?, chain_tier=?, chain_role=?, layman_intro=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE code = ?",
            [chain, tier, role, intro, code],
        )
        # DuckDB 的 cursor 没有标准 rowcount,用 SELECT 验证
        check = conn.execute("SELECT 1 FROM watchlist WHERE code = ?", [code]).fetchone()
        if check:
            n_updated += 1
        else:
            n_missing.append(code)

    # 验证:看有多少条已填 chain
    total = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
    filled = conn.execute("SELECT COUNT(*) FROM watchlist WHERE chain IS NOT NULL").fetchone()[0]
    unfilled_codes = [r[0] for r in conn.execute(
        "SELECT code FROM watchlist WHERE chain IS NULL ORDER BY code").fetchall()]

    conn.close()

    print(f"已尝试 UPDATE {len(CLASSIFICATIONS)} 条,实际匹配 {n_updated} 条")
    if n_missing:
        print(f"⚠️ 这些 code 在 DB 找不到: {n_missing}")
    print(f"\n数据库总览: {filled}/{total} 条已填 chain")
    if unfilled_codes:
        print(f"⚠️ 仍未填 chain 的: {unfilled_codes}")


if __name__ == "__main__":
    main()
