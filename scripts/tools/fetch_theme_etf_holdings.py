"""主题 ETF 持仓抓取 — 客观主题发现的种子数据。

为什么用 ETF 持仓：
  - ETF 厂商（Global X / VanEck / iShares 等）已经把"哪些标的属于什么主题"做成产品
  - 这就是市场客观共识，不依赖任何人脑子里"该选什么"
  - 持仓权重 = 市场资金对该标的的 conviction

数据流：
  1. 注册 ETF 元数据（手动，每个 ETF 1 行）
  2. 用 WebFetch 抓持仓 Top 10（每个 ETF 一次 fetch）
  3. 解析 raw_ticker → 推断市场 → 匹配 system_universe
  4. 落 ai_theme_etf_holdings 表
  5. dashboard 在 AI 雷达页面展示"ETF 共识"区

注意：
  - 抓取频率 weekly 即可（持仓变动慢）
  - WebFetch 失败时不删除旧持仓，只是不更新（容错）
  - 不要把 ETF 持仓自动写进 universe / watchlist —— 只显示"扩张候选"
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from stock_db import get_db  # noqa: E402  # type: ignore


# 8 个主题 ETF — 覆盖 AI 价值链 + 关联热门主题
# theme_id 若能对应到 5 主题之一就填，否则留空（独立成新主题）
ETF_SEEDS: list[dict] = [
    # 机器人 + AI（Global X 自动化与机器人 ETF）
    {
        "etf_ticker": "BOTZ",
        "etf_name": "Global X Robotics & Artificial Intelligence ETF",
        "issuer": "Global X",
        "theme_label": "机器人 + AI",
        "theme_id": None,
        "holdings_url": "https://www.globalxetfs.com/funds/botz/",
        "note": "全球机器人/工业自动化龙头，覆盖日股/瑞士股较多",
    },
    # AI + 大数据应用
    {
        "etf_ticker": "AIQ",
        "etf_name": "Global X Artificial Intelligence & Technology ETF",
        "issuer": "Global X",
        "theme_label": "AI 应用 + 大数据",
        "theme_id": "ai_data",
        "holdings_url": "https://www.globalxetfs.com/funds/aiq/",
        "note": "AI 应用层广基，含云/数据/SaaS",
    },
    # 美国基建（含数据中心电力）
    {
        "etf_ticker": "PAVE",
        "etf_name": "Global X U.S. Infrastructure Development ETF",
        "issuer": "Global X",
        "theme_label": "美国基建 / 数据中心电力",
        "theme_id": "liquid_cooling",
        "holdings_url": "https://www.globalxetfs.com/funds/pave/",
        "note": "数据中心电力受益的电网/电气装备/工程",
    },
    # 核能 + SMR
    {
        "etf_ticker": "NUKZ",
        "etf_name": "Range Nuclear Renaissance Index ETF",
        "issuer": "Range",
        "theme_label": "核能 + SMR",
        "theme_id": "smr",
        "holdings_url": "https://www.rangeetfs.com/nukz",
        "note": "覆盖核电运营商 + SMR 开发商 + 核燃料",
    },
    # 稀土
    {
        "etf_ticker": "REMX",
        "etf_name": "VanEck Rare Earth/Strategic Metals ETF",
        "issuer": "VanEck",
        "theme_label": "稀土 + 战略金属",
        "theme_id": "rare_earths",
        "holdings_url": "https://www.vaneck.com/us/en/investments/rare-earth-strategic-metals-etf-remx/holdings/",
        "note": "全球稀土+战略金属，覆盖澳/中/北美",
    },
    # 铀矿
    {
        "etf_ticker": "URA",
        "etf_name": "Global X Uranium ETF",
        "issuer": "Global X",
        "theme_label": "铀矿",
        "theme_id": "uranium",
        "holdings_url": "https://www.globalxetfs.com/funds/ura/",
        "note": "全球铀矿生产 + 核燃料供应链",
    },
    # 半导体（含光模块）
    {
        "etf_ticker": "SOXX",
        "etf_name": "iShares Semiconductor ETF",
        "issuer": "iShares",
        "theme_label": "半导体 + 光模块",
        "theme_id": None,
        "holdings_url": "https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf",
        "note": "半导体广基，含 GPU/CPU/光模块/设备",
    },
    # 中概互联网 AI
    {
        "etf_ticker": "KWEB",
        "etf_name": "KraneShares CSI China Internet ETF",
        "issuer": "KraneShares",
        "theme_label": "中概互联网 + AI",
        "theme_id": None,
        "holdings_url": "https://kraneshares.com/kweb/",
        "note": "BAT + 港股互联网龙头",
    },
    # 云计算
    {
        "etf_ticker": "SKYY",
        "etf_name": "First Trust Cloud Computing ETF",
        "issuer": "First Trust",
        "theme_label": "云计算",
        "theme_id": None,
        "holdings_url": "https://stockanalysis.com/etf/skyy/holdings/",
        "note": "云基础设施 + IaaS/PaaS 龙头",
    },
    # 网络安全（AI 安全是热点）
    {
        "etf_ticker": "HACK",
        "etf_name": "ETFMG Prime Cyber Security ETF",
        "issuer": "ETFMG",
        "theme_label": "网络安全 + AI 安全",
        "theme_id": None,
        "holdings_url": "https://stockanalysis.com/etf/hack/holdings/",
        "note": "AI 让网络安全需求暴涨",
    },
    # 锂电池 + 储能（AI 数据中心备电 / EV）
    {
        "etf_ticker": "LIT",
        "etf_name": "Global X Lithium & Battery Tech ETF",
        "issuer": "Global X",
        "theme_label": "锂电池 + 储能",
        "theme_id": None,
        "holdings_url": "https://www.globalxetfs.com/funds/lit/",
        "note": "锂矿 + 电池厂 + EV，覆盖韩日中",
    },
    # 清洁能源（数据中心绿电来源）
    {
        "etf_ticker": "ICLN",
        "etf_name": "iShares Global Clean Energy ETF",
        "issuer": "iShares",
        "theme_label": "清洁能源",
        "theme_id": None,
        "holdings_url": "https://stockanalysis.com/etf/icln/holdings/",
        "note": "光伏 + 风电 + 燃料电池，AI 数据中心绿电",
    },
]


# raw_ticker → (clean_ticker, market) 推断规则
# ETF 网站使用各种 ticker 格式，统一映射到 (symbol, market)
def parse_raw_ticker(raw: str) -> tuple[str | None, str | None]:
    """从 ETF 持仓 raw ticker 推断 (symbol, market)。

    Examples:
        '6954 JP'       → ('6954.T', 'JP')      日股 Tokyo
        'ABBN SW'       → ('ABBN.SW', 'CH')     瑞士
        '300124 C2'     → ('300124.SZ', 'CN')   A股 深圳
        'NVDA'          → ('NVDA', 'US')        美股
        '0700 HK'       → ('0700.HK', 'HK')     港股
    """
    raw = (raw or "").strip().upper()
    if not raw:
        return None, None

    parts = raw.split()
    if len(parts) == 1:
        # 纯字母 ticker → 假定美股
        return raw, "US" if raw.isalpha() else None

    sym, suffix = parts[0], parts[-1]
    suffix_map = {
        "JP": (f"{sym}.T", "JP"),     # Tokyo
        "JT": (f"{sym}.T", "JP"),
        "SW": (f"{sym}.SW", "CH"),    # Swiss
        "VX": (f"{sym}.SW", "CH"),
        "C1": (f"{sym}.SS", "CN"),    # Shanghai
        "C2": (f"{sym}.SZ", "CN"),    # Shenzhen
        "CH": (f"{sym}.SS", "CN"),    # 通用 China
        "HK": (f"{sym}.HK", "HK"),
        "LN": (f"{sym}.L", "GB"),     # London
        "FP": (f"{sym}.PA", "FR"),    # Paris
        "GR": (f"{sym}.DE", "DE"),    # Germany
        "AU": (f"{sym}.AX", "AU"),
        "CN": (f"{sym}.TO", "CA"),    # Canada (有时也用于 China，看 issuer 上下文)
    }
    if suffix in suffix_map:
        return suffix_map[suffix]
    # 未识别后缀
    return None, None


def upsert_etf_universe(con) -> int:
    n = 0
    for s in ETF_SEEDS:
        # 校验
        assert s["etf_ticker"], f"缺 etf_ticker: {s}"
        assert s["holdings_url"].startswith("http"), f"非法 holdings_url: {s}"
        con.execute("""
            INSERT INTO ai_theme_etf_universe
              (etf_ticker, etf_name, issuer, theme_label, theme_id, holdings_url, note, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, TRUE)
            ON CONFLICT (etf_ticker) DO UPDATE SET
              etf_name = excluded.etf_name,
              issuer = excluded.issuer,
              theme_label = excluded.theme_label,
              theme_id = excluded.theme_id,
              holdings_url = excluded.holdings_url,
              note = excluded.note
        """, [s["etf_ticker"], s["etf_name"], s["issuer"], s["theme_label"],
              s["theme_id"], s["holdings_url"], s.get("note")])
        n += 1
    return n


def upsert_holdings(con, etf_ticker: str, holdings: list[dict]) -> int:
    """覆盖该 ETF 全量持仓。"""
    # 先清旧持仓
    con.execute("DELETE FROM ai_theme_etf_holdings WHERE etf_ticker = ?", [etf_ticker])

    # universe 匹配查询（一次性）
    uni_rows = con.execute("SELECT symbol FROM system_universe WHERE active = TRUE").fetchall()
    uni_set = {r[0] for r in uni_rows}

    n = 0
    for i, h in enumerate(holdings, 1):
        raw = h["raw_ticker"]
        sym, market = parse_raw_ticker(raw)
        uni_match = sym if (sym and sym in uni_set) else None
        # 也尝试无后缀的纯 ticker（美股）
        if not uni_match and sym and sym in uni_set:
            uni_match = sym
        # 美股纯 ticker
        if not uni_match and raw.replace(" ", "").isalpha() and raw in uni_set:
            uni_match = raw
            market = "US"

        con.execute("""
            INSERT INTO ai_theme_etf_holdings
              (etf_ticker, rank, raw_ticker, company_name, weight, market_inferred, universe_match)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [etf_ticker, i, raw, h.get("company_name"), h.get("weight"),
              market, uni_match])
        n += 1

    # 更新 last_fetched_at
    con.execute("UPDATE ai_theme_etf_universe SET last_fetched_at = ? WHERE etf_ticker = ?",
                [datetime.now(), etf_ticker])
    return n


# ──────────────────────────────────────────────────────────────
# 已抓数据（由 WebFetch 在 fetch_holdings_via_external() 调用方填入）
# 子进程里跑 WebFetch 不方便，所以 fetcher 改为"由 driver 提供"
# 这里只暴露 ingest 接口：把 (etf_ticker, [{raw_ticker, company_name, weight}, ...]) 入库
# ──────────────────────────────────────────────────────────────


def main():
    con = get_db()
    try:
        n = upsert_etf_universe(con)
        print(f"✅ 注册 {n} 个 ETF 元数据")
        for r in con.execute(
            "SELECT etf_ticker, theme_label, issuer FROM ai_theme_etf_universe ORDER BY etf_ticker"
        ).fetchall():
            print(f"  {r[0]:<6} {r[1]:<25} ({r[2]})")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
