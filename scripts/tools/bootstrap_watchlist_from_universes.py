#!/usr/bin/env python3
"""Preview or explicitly seed DuckDB watchlist from production universes.

The watchlist is user-curated state.  It must stay empty after a clean reset
until the user adds stocks manually from the dashboard.  This tool is retained
only as an explicit recovery/import utility and never writes unless --apply is
passed.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts", "lib"))

from stock_db import fetch_all_watchlist, upsert_watchlist  # noqa: E402
from stock_research.core.hk_universe import fetch_hk_tech_universe  # noqa: E402
from stock_research.core.us_universe import fetch_us_ai_tech_universe  # noqa: E402


A_SHARE_STATIC_SEED: list[dict] = [
    {"ticker": "300308.SZ", "raw_ticker": "300308", "name": "中际旭创", "sector": "光通信", "source": "cn_static_optical"},
    {"ticker": "300502.SZ", "raw_ticker": "300502", "name": "新易盛", "sector": "光通信", "source": "cn_static_optical"},
    {"ticker": "300394.SZ", "raw_ticker": "300394", "name": "天孚通信", "sector": "光通信", "source": "cn_static_optical"},
    {"ticker": "688498.SS", "raw_ticker": "688498", "name": "源杰科技", "sector": "光芯片", "source": "cn_static_optical"},
    {"ticker": "000977.SZ", "raw_ticker": "000977", "name": "浪潮信息", "sector": "AI服务器", "source": "cn_static_compute"},
    {"ticker": "601138.SS", "raw_ticker": "601138", "name": "工业富联", "sector": "AI服务器", "source": "cn_static_compute"},
    {"ticker": "603019.SS", "raw_ticker": "603019", "name": "中科曙光", "sector": "AI服务器", "source": "cn_static_compute"},
    {"ticker": "688256.SS", "raw_ticker": "688256", "name": "寒武纪", "sector": "AI芯片", "source": "cn_static_compute"},
    {"ticker": "688041.SS", "raw_ticker": "688041", "name": "海光信息", "sector": "AI芯片", "source": "cn_static_compute"},
    {"ticker": "688981.SS", "raw_ticker": "688981", "name": "中芯国际", "sector": "晶圆代工", "source": "cn_static_semi"},
    {"ticker": "688012.SS", "raw_ticker": "688012", "name": "中微公司", "sector": "半导体设备", "source": "cn_static_semi"},
    {"ticker": "002371.SZ", "raw_ticker": "002371", "name": "北方华创", "sector": "半导体设备", "source": "cn_static_semi"},
    {"ticker": "688072.SS", "raw_ticker": "688072", "name": "拓荆科技", "sector": "半导体设备", "source": "cn_static_semi"},
    {"ticker": "688120.SS", "raw_ticker": "688120", "name": "华海清科", "sector": "半导体设备", "source": "cn_static_semi"},
    {"ticker": "688008.SS", "raw_ticker": "688008", "name": "澜起科技", "sector": "存储/互连芯片", "source": "cn_static_semi"},
    {"ticker": "603986.SS", "raw_ticker": "603986", "name": "兆易创新", "sector": "存储芯片", "source": "cn_static_semi"},
    {"ticker": "600584.SS", "raw_ticker": "600584", "name": "长电科技", "sector": "半导体封测", "source": "cn_static_semi"},
    {"ticker": "603501.SS", "raw_ticker": "603501", "name": "韦尔股份", "sector": "图像传感器", "source": "cn_static_semi"},
    {"ticker": "688099.SS", "raw_ticker": "688099", "name": "晶晨股份", "sector": "SoC", "source": "cn_static_semi"},
    {"ticker": "688521.SS", "raw_ticker": "688521", "name": "芯原股份", "sector": "芯片IP", "source": "cn_static_semi"},
    {"ticker": "002463.SZ", "raw_ticker": "002463", "name": "沪电股份", "sector": "AI PCB", "source": "cn_static_hardware"},
    {"ticker": "002916.SZ", "raw_ticker": "002916", "name": "深南电路", "sector": "AI PCB", "source": "cn_static_hardware"},
    {"ticker": "300476.SZ", "raw_ticker": "300476", "name": "胜宏科技", "sector": "AI PCB", "source": "cn_static_hardware"},
    {"ticker": "002475.SZ", "raw_ticker": "002475", "name": "立讯精密", "sector": "消费电子/连接器", "source": "cn_static_hardware"},
    {"ticker": "000063.SZ", "raw_ticker": "000063", "name": "中兴通讯", "sector": "通信设备", "source": "cn_static_hardware"},
    {"ticker": "000725.SZ", "raw_ticker": "000725", "name": "京东方A", "sector": "显示面板", "source": "cn_static_hardware"},
    {"ticker": "002837.SZ", "raw_ticker": "002837", "name": "英维克", "sector": "数据中心温控", "source": "cn_static_power"},
    {"ticker": "300274.SZ", "raw_ticker": "300274", "name": "阳光电源", "sector": "电力设备", "source": "cn_static_power"},
    {"ticker": "600406.SS", "raw_ticker": "600406", "name": "国电南瑞", "sector": "电网自动化", "source": "cn_static_power"},
    {"ticker": "300750.SZ", "raw_ticker": "300750", "name": "宁德时代", "sector": "电池", "source": "cn_static_power"},
    {"ticker": "601012.SS", "raw_ticker": "601012", "name": "隆基绿能", "sector": "光伏", "source": "cn_static_power"},
    {"ticker": "600111.SS", "raw_ticker": "600111", "name": "北方稀土", "sector": "稀土", "source": "cn_static_resources"},
    {"ticker": "002230.SZ", "raw_ticker": "002230", "name": "科大讯飞", "sector": "AI应用", "source": "cn_static_software"},
    {"ticker": "688111.SS", "raw_ticker": "688111", "name": "金山办公", "sector": "AI办公软件", "source": "cn_static_software"},
    {"ticker": "688777.SS", "raw_ticker": "688777", "name": "中控技术", "sector": "工业软件", "source": "cn_static_software"},
    {"ticker": "002410.SZ", "raw_ticker": "002410", "name": "广联达", "sector": "建筑软件", "source": "cn_static_software"},
    {"ticker": "002415.SZ", "raw_ticker": "002415", "name": "海康威视", "sector": "机器视觉", "source": "cn_static_aiot"},
    {"ticker": "300124.SZ", "raw_ticker": "300124", "name": "汇川技术", "sector": "工业自动化", "source": "cn_static_aiot"},
    {"ticker": "300316.SZ", "raw_ticker": "300316", "name": "晶盛机电", "sector": "高端装备", "source": "cn_static_aiot"},
    {"ticker": "300760.SZ", "raw_ticker": "300760", "name": "迈瑞医疗", "sector": "医疗设备", "source": "cn_static_healthcare"},
    {"ticker": "600276.SS", "raw_ticker": "600276", "name": "恒瑞医药", "sector": "创新药", "source": "cn_static_healthcare"},
    {"ticker": "688271.SS", "raw_ticker": "688271", "name": "联影医疗", "sector": "医疗影像", "source": "cn_static_healthcare"},
]


def _a_share_market(raw_ticker: str) -> str:
    if raw_ticker.startswith(("00", "20", "30")):
        return "A股·深交所"
    if raw_ticker.startswith(("8", "9")):
        return "A股·北交所"
    return "A股·上交所"


def _row(
    *,
    code: str,
    name: str,
    market: str,
    industry: str,
    source: str,
    theme: str,
) -> dict:
    return {
        "code": code,
        "name": name,
        "market": market,
        "business": f"{theme} universe seed; run enrichment for full business notes.",
        "industry": industry,
        "ai_relevance": "待验证",
        "ai_logic": "自动候选池种子，后续由因子模型、审计和 enrichment 写入证据。",
        "theme": theme,
        "conclusion": "待研究（自动 universe 种子）",
        "risks": "待补充",
        "peers": "",
        "rhythm": "",
        "status": "seed",
        "source": source,
        "credibility": "中",
        "notes": f"bootstrap_watchlist_from_universes.py inserted on {date.today().isoformat()}",
        "chain": theme,
        "chain_tier": "N/A",
        "chain_role": industry,
        "layman_intro": f"{name}：{industry} 方向候选标的。",
        "earnings": "",
        "verification": "待验证",
        "info_breakdown": "",
    }


def _us_rows() -> list[dict]:
    return [
        _row(
            code=item["ticker"],
            name=item["name"],
            market="美股",
            industry=item["sector"],
            source=f"universe:{item['source']}",
            theme="US AI/tech",
        )
        for item in fetch_us_ai_tech_universe()
    ]


def _hk_rows() -> list[dict]:
    return [
        _row(
            code=item["ticker"],
            name=item["name"],
            market="港股",
            industry=item["sector"],
            source=f"universe:{item['source']}",
            theme="HK tech",
        )
        for item in fetch_hk_tech_universe()
    ]


def _cn_rows(a_share_mode: str, a_share_limit: int | None) -> list[dict]:
    try:
        from stock_research.core.a_share_universe import fetch_a_share_tech_universe

        items = fetch_a_share_tech_universe()
    except Exception as e:
        if a_share_mode == "dynamic":
            raise
        print(f"  A 股动态 universe 失败，保持为空: {e}")
        items = []

    if a_share_limit is not None and a_share_limit > 0:
        items = items[:a_share_limit]

    rows = []
    for item in items:
        raw = item.get("raw_ticker") or item["ticker"].split(".")[0]
        rows.append(
            _row(
                code=raw,
                name=item.get("name") or raw,
                market=_a_share_market(raw),
                industry=item.get("sector") or "",
                source=f"universe:{item.get('source') or 'cn_static'}",
                theme="A-share AI/tech",
            )
        )
    return rows


def _parse_markets(value: str) -> set[str]:
    aliases = {
        "us": "us",
        "美股": "us",
        "hk": "hk",
        "港股": "hk",
        "cn": "cn",
        "a": "cn",
        "a股": "cn",
    }
    out = set()
    for part in value.split(","):
        key = part.strip().lower()
        if not key:
            continue
        if key not in aliases:
            raise SystemExit(f"unknown market: {part!r}; use us,hk,cn")
        out.add(aliases[key])
    return out or {"us", "hk", "cn"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", default="us,hk,cn", help="comma list: us,hk,cn")
    parser.add_argument(
        "--a-share-mode",
        choices=["static", "auto", "dynamic"],
        default="static",
        help="static is deterministic; auto tries live A-share universe then falls back",
    )
    parser.add_argument("--a-share-limit", type=int, default=80)
    parser.add_argument("--refresh-existing", action="store_true", help="overwrite existing seed fields too")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write universe rows into watchlist; default is preview only",
    )
    parser.add_argument(
        "--confirm-watchlist-seed",
        action="store_true",
        help="required with --apply; acknowledges this writes auto universe rows into the user-curated watchlist",
    )
    args = parser.parse_args()

    markets = _parse_markets(args.markets)
    rows: list[dict] = []
    if "us" in markets:
        rows.extend(_us_rows())
    if "hk" in markets:
        rows.extend(_hk_rows())
    if "cn" in markets:
        rows.extend(_cn_rows(args.a_share_mode, args.a_share_limit))

    existing_codes = {r["code"] for r in fetch_all_watchlist()}
    if args.refresh_existing:
        to_insert = rows
    else:
        to_insert = [r for r in rows if r["code"] not in existing_codes]

    print(
        f"Universe seed rows: built={len(rows)}, existing={len(existing_codes)}, "
        f"to_insert={len(to_insert)}"
    )
    if not args.apply:
        print("Preview only: watchlist is manual; pass --apply to import these rows.")
        for row in to_insert[:20]:
            print(f"  {row['code']:>8} {row['name']} · {row['market']} · {row['industry']}")
        if len(to_insert) > 20:
            print(f"  ... {len(to_insert) - 20} more")
        return
    if not args.confirm_watchlist_seed:
        raise SystemExit(
            "Refusing to seed watchlist without --confirm-watchlist-seed. "
            "watchlist is user-curated state; use AI 推荐/discovery for broad universes."
        )

    if to_insert:
        n = upsert_watchlist(to_insert)
        print(f"DuckDB watchlist inserted/updated: {n}")
    else:
        print("DuckDB watchlist already has all requested universe rows.")


if __name__ == "__main__":
    main()
