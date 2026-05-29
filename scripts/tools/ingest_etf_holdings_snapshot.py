"""ETF 持仓快照 ingest（2026-05-29 WebFetch 真实数据）。

数据采集：2026-05-29 通过 WebFetch 从各 ETF 官方/聚合页面抓取
  - BOTZ / AIQ / PAVE / URA  → globalxetfs.com
  - REMX / SOXX / NUKZ        → stockanalysis.com
  - KWEB                      → kraneshares.com

每次跑都覆盖该 ETF 全量持仓 + 触发 universe 匹配。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "scripts" / "tools"))

from stock_db import get_db  # noqa: E402  # type: ignore
from fetch_theme_etf_holdings import upsert_holdings  # noqa: E402  # type: ignore


# 2026-05-29 WebFetch 实测数据
SNAPSHOTS: dict[str, list[dict]] = {
    "BOTZ": [
        {"raw_ticker": "6954 JP", "company_name": "FANUC CORP", "weight": 9.06},
        {"raw_ticker": "ABBN SW", "company_name": "ABB LTD-REG", "weight": 8.95},
        {"raw_ticker": "6861 JP", "company_name": "KEYENCE CORP", "weight": 8.70},
        {"raw_ticker": "NVDA",    "company_name": "NVIDIA CORP", "weight": 8.37},
        {"raw_ticker": "ISRG",    "company_name": "INTUITIVE SURGICAL INC", "weight": 6.11},
        {"raw_ticker": "300124 C2", "company_name": "SHENZHEN INOVANCE TECHNOLOGY-A", "weight": 4.48},
        {"raw_ticker": "6273 JP", "company_name": "SMC CORP", "weight": 3.93},
        {"raw_ticker": "6383 JP", "company_name": "DAIFUKU CO LTD", "weight": 3.48},
        {"raw_ticker": "300757 C2", "company_name": "ROBOTECHNIK INTELLIGENT TE-A", "weight": 2.76},
        {"raw_ticker": "6506 JP", "company_name": "YASKAWA ELECTRIC CORP", "weight": 2.40},
    ],
    "AIQ": [
        {"raw_ticker": "000660 KS", "company_name": "SK HYNIX INC", "weight": 7.15},
        {"raw_ticker": "MU",       "company_name": "MICRON TECHNOLOGY INC", "weight": 5.60},
        {"raw_ticker": "AMD",      "company_name": "ADVANCED MICRO DEVICES", "weight": 4.92},
        {"raw_ticker": "005930 KS", "company_name": "SAMSUNG ELECTRONICS CO LTD", "weight": 4.64},
        {"raw_ticker": "INTC",     "company_name": "INTEL CORP", "weight": 4.50},
        {"raw_ticker": "CSCO",     "company_name": "CISCO SYSTEMS INC", "weight": 3.85},
        {"raw_ticker": "AVGO",     "company_name": "BROADCOM INC", "weight": 3.16},
        {"raw_ticker": "TSM",      "company_name": "TAIWAN SEMICONDUCTOR-SP ADR", "weight": 3.13},
        {"raw_ticker": "AAPL",     "company_name": "APPLE INC", "weight": 3.03},
        {"raw_ticker": "GOOGL",    "company_name": "ALPHABET INC-CL A", "weight": 2.84},
    ],
    "PAVE": [
        {"raw_ticker": "PWR",  "company_name": "Quanta Services Inc", "weight": 4.28},
        {"raw_ticker": "CSX",  "company_name": "CSX Corp", "weight": 3.50},
        {"raw_ticker": "ETN",  "company_name": "Eaton Corp PLC", "weight": 3.30},
        {"raw_ticker": "HWM",  "company_name": "Howmet Aerospace Inc", "weight": 3.28},
        {"raw_ticker": "TT",   "company_name": "Trane Technologies PLC", "weight": 3.18},
        {"raw_ticker": "UNP",  "company_name": "Union Pacific Corp", "weight": 3.15},
        {"raw_ticker": "NUE",  "company_name": "Nucor Corp", "weight": 3.08},
        {"raw_ticker": "ROK",  "company_name": "Rockwell Automation Inc", "weight": 2.94},
        {"raw_ticker": "NSC",  "company_name": "Norfolk Southern Corp", "weight": 2.92},
        {"raw_ticker": "SRE",  "company_name": "Sempra", "weight": 2.88},
    ],
    "URA": [
        {"raw_ticker": "CCO CN", "company_name": "Cameco Corp", "weight": 23.03},
        {"raw_ticker": "OKLO",   "company_name": "Oklo Inc", "weight": 7.59},
        {"raw_ticker": "NXE CN", "company_name": "Nexgen Energy Ltd", "weight": 6.24},
        {"raw_ticker": "UEC",    "company_name": "Uranium Energy Corp", "weight": 5.83},
        {"raw_ticker": "U-U CN", "company_name": "Sprott Physical Uranium Trust", "weight": 4.53},
        {"raw_ticker": "KAP LI", "company_name": "Nac Kazatomprom Jsc-Gdr Regs", "weight": 4.51},
        {"raw_ticker": "EFR CN", "company_name": "Energy Fuels Inc", "weight": 4.00},
        {"raw_ticker": "PDN AU", "company_name": "Paladin Energy Ltd", "weight": 3.29},
        {"raw_ticker": "047040 KS", "company_name": "Daewoo Engineering & Constr", "weight": 3.22},
        {"raw_ticker": "LEU",    "company_name": "Centrus Energy Corp-Class A", "weight": 2.91},
    ],
    "REMX": [
        {"raw_ticker": "PLS AU", "company_name": "Pilbara Minerals Limited", "weight": 8.34},
        {"raw_ticker": "ALB",    "company_name": "Albemarle Corporation", "weight": 8.14},
        {"raw_ticker": "LYC AU", "company_name": "Lynas Rare Earths Limited", "weight": 7.27},
        {"raw_ticker": "600111 C1", "company_name": "China Northern Rare Earth (Group)", "weight": 6.74},
        {"raw_ticker": "MP",     "company_name": "MP Materials Corp.", "weight": 6.32},
        {"raw_ticker": "LTR AU", "company_name": "Liontown Resources Limited", "weight": 6.27},
        {"raw_ticker": "SQM",    "company_name": "Sociedad Química y Minera de Chile", "weight": 5.06},
        {"raw_ticker": "GNENF",  "company_name": "Ganfeng Lithium Group", "weight": 4.81},
        {"raw_ticker": "601958 C1", "company_name": "Jinduicheng Molybdenum", "weight": 4.47},
        {"raw_ticker": "600549 C1", "company_name": "Xiamen Tungsten", "weight": 4.25},
        {"raw_ticker": "AII",    "company_name": "Almonty Industries Inc.", "weight": 3.91},
        {"raw_ticker": "600392 C1", "company_name": "Shenghe Resources Holding", "weight": 3.82},
        {"raw_ticker": "ILU AU", "company_name": "Iluka Resources Limited", "weight": 3.62},
        {"raw_ticker": "LAC",    "company_name": "Lithium Americas Corp.", "weight": 3.01},
        {"raw_ticker": "AMG",    "company_name": "AMG Critical Materials N.V.", "weight": 2.97},
    ],
    "SOXX": [
        {"raw_ticker": "MU",   "company_name": "Micron Technology", "weight": 11.22},
        {"raw_ticker": "AMD",  "company_name": "Advanced Micro Devices", "weight": 9.20},
        {"raw_ticker": "INTC", "company_name": "Intel Corporation", "weight": 6.65},
        {"raw_ticker": "AVGO", "company_name": "Broadcom Inc.", "weight": 6.58},
        {"raw_ticker": "MRVL", "company_name": "Marvell Technology", "weight": 6.06},
        {"raw_ticker": "NVDA", "company_name": "NVIDIA Corporation", "weight": 5.98},
        {"raw_ticker": "AMAT", "company_name": "Applied Materials", "weight": 4.47},
        {"raw_ticker": "QCOM", "company_name": "QUALCOMM Incorporated", "weight": 4.08},
        {"raw_ticker": "TXN",  "company_name": "Texas Instruments", "weight": 3.73},
        {"raw_ticker": "NXPI", "company_name": "NXP Semiconductors N.V.", "weight": 3.61},
        {"raw_ticker": "MPWR", "company_name": "Monolithic Power Systems", "weight": 3.53},
        {"raw_ticker": "LRCX", "company_name": "Lam Research Corporation", "weight": 3.40},
        {"raw_ticker": "KLAC", "company_name": "KLA Corporation", "weight": 3.20},
        {"raw_ticker": "TER",  "company_name": "Teradyne, Inc.", "weight": 2.93},
        {"raw_ticker": "ADI",  "company_name": "Analog Devices, Inc.", "weight": 2.92},
    ],
    "NUKZ": [
        {"raw_ticker": "CCJ",   "company_name": "Cameco Corporation", "weight": 8.67},
        {"raw_ticker": "GEV",   "company_name": "GE Vernova Inc.", "weight": 3.66},
        {"raw_ticker": "TLN",   "company_name": "Talen Energy Corporation", "weight": 3.26},
        {"raw_ticker": "028260 KS", "company_name": "Samsung C&T Corporation", "weight": 3.26},
        {"raw_ticker": "CEZ",   "company_name": "CEZ, a. s.", "weight": 3.05},
        {"raw_ticker": "D",     "company_name": "Dominion Energy, Inc.", "weight": 2.91},
        {"raw_ticker": "ELE",   "company_name": "Endesa, S.A.", "weight": 2.89},
        {"raw_ticker": "RR LN", "company_name": "Rolls-Royce Holdings plc", "weight": 2.82},
        {"raw_ticker": "VST",   "company_name": "Vistra Corp.", "weight": 2.81},
        {"raw_ticker": "CEG",   "company_name": "Constellation Energy Corporation", "weight": 2.72},
        {"raw_ticker": "SOLS",  "company_name": "Solstice Advanced Materials, Inc.", "weight": 2.71},
        {"raw_ticker": "FORTUM", "company_name": "Fortum Oyj", "weight": 2.68},
        {"raw_ticker": "DUK",   "company_name": "Duke Energy Corporation", "weight": 2.58},
        {"raw_ticker": "CW",    "company_name": "Curtiss-Wright Corporation", "weight": 2.49},
        {"raw_ticker": "PCG",   "company_name": "PG&E Corporation", "weight": 2.47},
    ],
    "KWEB": [
        {"raw_ticker": "9988 HK", "company_name": "Alibaba Group Holding Ltd", "weight": 9.59},
        {"raw_ticker": "700 HK",  "company_name": "Tencent Holdings Ltd", "weight": 9.31},
        {"raw_ticker": "PDD",     "company_name": "PDD Holdings Inc", "weight": 7.58},
        {"raw_ticker": "9999 HK", "company_name": "NetEase Inc", "weight": 7.02},
        {"raw_ticker": "3690 HK", "company_name": "Meituan-Class B", "weight": 6.82},
        {"raw_ticker": "9888 HK", "company_name": "Baidu Inc-Class A", "weight": 5.34},
        {"raw_ticker": "9618 HK", "company_name": "JD.Com Inc-Class A", "weight": 5.21},
        {"raw_ticker": "2423 HK", "company_name": "Ke Holdings Inc-Cl A", "weight": 5.12},
        {"raw_ticker": "YMM",     "company_name": "Full Truck Alliance -Spn Adr", "weight": 4.03},
        {"raw_ticker": "9961 HK", "company_name": "Trip.Com Group Ltd", "weight": 3.57},
        {"raw_ticker": "1024 HK", "company_name": "Kuaishou Technology", "weight": 3.48},
        {"raw_ticker": "6618 HK", "company_name": "JD Health International Inc", "weight": 3.44},
        {"raw_ticker": "BZ",      "company_name": "Kanzhun Ltd - Adr", "weight": 3.13},
        {"raw_ticker": "TAL",     "company_name": "Tal Education Group- Adr", "weight": 3.10},
        {"raw_ticker": "VIPS",    "company_name": "Vipshop Holdings Ltd - Adr", "weight": 2.62},
    ],
    "SKYY": [
        {"raw_ticker": "DOCN", "company_name": "DigitalOcean Holdings", "weight": 5.90},
        {"raw_ticker": "P",    "company_name": "Pure Storage (P)", "weight": 4.18},
        {"raw_ticker": "ORCL", "company_name": "Oracle Corporation", "weight": 4.04},
        {"raw_ticker": "AMZN", "company_name": "Amazon.com, Inc.", "weight": 3.84},
        {"raw_ticker": "GOOGL", "company_name": "Alphabet Inc.", "weight": 3.79},
        {"raw_ticker": "NTNX", "company_name": "Nutanix, Inc.", "weight": 3.70},
        {"raw_ticker": "LUMN", "company_name": "Lumen Technologies", "weight": 3.62},
        {"raw_ticker": "ANET", "company_name": "Arista Networks", "weight": 3.60},
        {"raw_ticker": "CRWV", "company_name": "CoreWeave, Inc.", "weight": 3.37},
        {"raw_ticker": "MSFT", "company_name": "Microsoft Corporation", "weight": 3.22},
        {"raw_ticker": "IBM",  "company_name": "International Business Machines", "weight": 3.17},
        {"raw_ticker": "DELL", "company_name": "Dell Technologies Inc.", "weight": 3.13},
        {"raw_ticker": "AKAM", "company_name": "Akamai Technologies", "weight": 3.05},
        {"raw_ticker": "CSCO", "company_name": "Cisco Systems, Inc.", "weight": 3.02},
        {"raw_ticker": "HPE",  "company_name": "Hewlett Packard Enterprise", "weight": 2.69},
    ],
    "HACK": [
        {"raw_ticker": "CRWD", "company_name": "CrowdStrike Holdings", "weight": 7.45},
        {"raw_ticker": "PANW", "company_name": "Palo Alto Networks", "weight": 7.38},
        {"raw_ticker": "AVGO", "company_name": "Broadcom Inc.", "weight": 7.34},
        {"raw_ticker": "CSCO", "company_name": "Cisco Systems, Inc.", "weight": 6.99},
        {"raw_ticker": "FTNT", "company_name": "Fortinet, Inc.", "weight": 6.49},
        {"raw_ticker": "FFIV", "company_name": "F5, Inc.", "weight": 4.88},
        {"raw_ticker": "NET",  "company_name": "Cloudflare, Inc.", "weight": 4.81},
        {"raw_ticker": "ZS",   "company_name": "Zscaler, Inc.", "weight": 4.31},
        {"raw_ticker": "OKTA", "company_name": "Okta, Inc.", "weight": 4.24},
        {"raw_ticker": "S",    "company_name": "SentinelOne, Inc.", "weight": 4.20},
        {"raw_ticker": "RBRK", "company_name": "Rubrik, Inc.", "weight": 4.17},
        {"raw_ticker": "GD",   "company_name": "General Dynamics Corp.", "weight": 3.98},
        {"raw_ticker": "ATEN", "company_name": "A10 Networks, Inc.", "weight": 3.94},
        {"raw_ticker": "VRNS", "company_name": "Varonis Systems, Inc.", "weight": 3.78},
        {"raw_ticker": "TENB", "company_name": "Tenable Holdings, Inc.", "weight": 3.68},
    ],
    "LIT": [
        {"raw_ticker": "RIO",      "company_name": "Rio Tinto plc-Spon ADR", "weight": 20.35},
        {"raw_ticker": "6762 JP",  "company_name": "TDK Corporation", "weight": 5.69},
        {"raw_ticker": "ALB",      "company_name": "Albemarle Corporation", "weight": 5.49},
        {"raw_ticker": "002371 C2", "company_name": "NAURA Technology Group-A", "weight": 5.37},
        {"raw_ticker": "006400 KS", "company_name": "Samsung SDI Co Ltd", "weight": 5.28},
        {"raw_ticker": "6752 JP",  "company_name": "Panasonic Holdings Corporation", "weight": 4.51},
        {"raw_ticker": "TSLA",     "company_name": "Tesla, Inc.", "weight": 4.33},
        {"raw_ticker": "373220 KS", "company_name": "LG Energy Solution", "weight": 3.68},
        {"raw_ticker": "300750 C2", "company_name": "CATL (Contemporary Amperex)-A", "weight": 3.52},
        {"raw_ticker": "PLS AU",   "company_name": "PLS Group Ltd (Pilbara)", "weight": 3.50},
    ],
    "ICLN": [
        {"raw_ticker": "BE",      "company_name": "Bloom Energy Corporation", "weight": 12.74},
        {"raw_ticker": "FSLR",    "company_name": "First Solar, Inc.", "weight": 8.68},
        {"raw_ticker": "NXT",     "company_name": "Nextracker Inc.", "weight": 7.42},
        {"raw_ticker": "ENPH",    "company_name": "Enphase Energy", "weight": 6.19},
        {"raw_ticker": "600900 C1", "company_name": "China Yangtze Power Co., Ltd.", "weight": 5.30},
        {"raw_ticker": "PLUG",    "company_name": "Plug Power Inc.", "weight": 3.88},
        {"raw_ticker": "SEDG",    "company_name": "SolarEdge Technologies", "weight": 2.95},
        {"raw_ticker": "VWS DC",  "company_name": "Vestas Wind Systems A/S", "weight": 2.70},
        {"raw_ticker": "EQTL3 BZ", "company_name": "Equatorial S.A.", "weight": 2.27},
        {"raw_ticker": "SUZLON IS", "company_name": "Suzlon Energy Limited", "weight": 2.17},
        {"raw_ticker": "EDP PL",  "company_name": "EDP, S.A.", "weight": 1.97},
        {"raw_ticker": "9502 JP", "company_name": "Chubu Electric Power", "weight": 1.96},
        {"raw_ticker": "336260 KS", "company_name": "Doosan Fuel Cell Co., Ltd.", "weight": 1.85},
        {"raw_ticker": "CWEN",    "company_name": "Clearway Energy, Inc.", "weight": 1.69},
        {"raw_ticker": "ORSTED DC", "company_name": "Ørsted A/S", "weight": 1.57},
    ],
}


def main():
    con = get_db()
    try:
        total = 0
        for etf, holdings in SNAPSHOTS.items():
            n = upsert_holdings(con, etf, holdings)
            total += n
            print(f"  {etf}: {n} 条持仓入库")

        # universe 命中统计
        match_rows = con.execute("""
            SELECT u.etf_ticker, u.theme_label,
                   COUNT(*) AS n_total,
                   SUM(CASE WHEN h.universe_match IS NOT NULL THEN 1 ELSE 0 END) AS n_match
            FROM ai_theme_etf_universe u
            JOIN ai_theme_etf_holdings h ON h.etf_ticker = u.etf_ticker
            GROUP BY u.etf_ticker, u.theme_label
            ORDER BY u.etf_ticker
        """).fetchall()
        print(f"\n✅ 共 {total} 条持仓入库 · universe 匹配统计：")
        for etf, label, n_total, n_match in match_rows:
            print(f"  {etf:<6} {label:<25} {n_match}/{n_total} 在 universe")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
