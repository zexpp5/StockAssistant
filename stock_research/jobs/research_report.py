"""个股 LLM 研报生成（B 路线 Phase 2C — 端到端）

把 Phase 1（结构化基本面）+ Phase 2A（10-K 章节）+ Phase 2B（Claude）整合，
产出 8-12 页机构级 Markdown 研报。

流程：
  1. 拉结构化基本面 JSON（杜邦/M-Score/Z-Score/质量/同业/估值）
  2. 拉最新 10-K 关键章节（Business / Risk Factors / MD&A）
  3. 构造 prompt（system 缓存：研报骨架 + 10-K 全文）
  4. 调 Claude API → Markdown 研报
  5. 保存到 data/reports/

成本：Sonnet 4.6 单份研报约 $0.5-1.5（10-K 200K input + 4K output）
      命中 cache 后续重跑 ≈ $0.05

CLI:
  python3 -m stock_research.jobs.research_report NVDA
  python3 -m stock_research.jobs.research_report NVDA --peers AMD AVGO TSM
  python3 -m stock_research.jobs.research_report NVDA --no-10k    # 省 token
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from stock_research.core import claude_client, sec_filings
from stock_research.jobs import fundamental_report

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────
# Prompt 模板
# ────────────────────────────────────────────────────────

SYSTEM_PROMPT_HEADER = """你是一位资深 sell-side 卖方研究员，曾任职高盛/摩根士丹利覆盖 TMT 行业。
请根据下方提供的 SEC 10-K 全文 + 结构化基本面数据，生成一份**机构级中文 Markdown 研报**。

# 研报结构（必须按此章节顺序输出）

## 1. 投资要点（Executive Summary）
- 3-5 条 bullet，每条 1-2 句
- 必须包含：核心论点 + 估值锚 + 主要风险

## 2. 公司业务剖析
- 基于 10-K Item 1 Business 章节
- 业务分部 + 收入构成 + 关键客户/供应商集中度（如有披露）
- 护城河来源（技术/网络/规模/品牌/转换成本）

## 3. 财务深度解读
- 解读杜邦五因子归因：ROE 变动主要由哪个因子驱动？是经营改善还是加杠杆？
- 解读盈利质量 8 项：哪几项警示？哪几项亮眼？是否有"账面利润 vs 真实赚钱"的差距？
- 解读 Beneish M-Score / Altman Z-Score：财务稳健性如何？

## 4. 估值分析
- DCF 上行空间 vs 同业 PE/EV-EBITDA 分位
- 给出至少 2 种估值方法的区间（保守/中性/乐观）
- 当前股价处于哪个区间，隐含什么市场预期

## 5. 关键风险揭示
- 必须从 10-K Item 1A Risk Factors 提取**最严重的 3-5 条**（不要全列）
- 每条风险给"发生概率（高/中/低）+ 量化影响"
- 重点关注：客户集中度、监管、技术替代、宏观敏感性

## 6. 经营趋势（基于 MD&A）
- 从 10-K Item 7 MD&A 提取管理层口径的核心叙事
- 分部表现：哪个分部加速、哪个减速、为什么
- 资本配置动作：研发 / 并购 / 回购 / 分红的优先级

## 7. 催化剂与时间表
- 列出未来 6-12 月可能催化股价的 3-5 个事件（产品发布 / 财报 / 政策）
- 每个事件给预计时点 + 影响方向

## 8. 投资判定
- 不给"买/卖/持有"评级（合规边界）
- 给"是否值得纳入研究/跟踪"的判断 + 触发条件
- 给"风险预警阈值"（什么数据/事件出现需立即重新评估）

# 写作风格要求

1. **数据必须援引**：每个论断后括号注明数据出处，如"(10-K MD&A)" "(杜邦归因)" "(同业分位)"
2. **不堆砌信息**：宁可少写一段也要每段有信息密度，避免"营收增长 X%，主要由 Y 业务驱动"这种重述
3. **直面坏消息**：盈利质量警示、风险因素的严重项必须放在显眼位置，不"美化"
4. **避免空话**：禁用"行业领先"、"显著优势"、"前景广阔"等模板化措辞，必须用数字和事实替代
5. **长度 1500-2500 字**：太短不够深度，太长稀释信号

下面是基础材料："""


def build_messages(ticker: str,
                   structured: dict[str, Any],
                   sec_data: dict[str, Any] | None = None,
                   include_10k_full: bool = True) -> tuple[str, str]:
    """构造 system + user prompt。"""
    parts = [SYSTEM_PROMPT_HEADER, ""]

    parts.append("# 结构化基本面数据（FMP）")
    parts.append("```json")
    # 精简：去掉冗余字段
    compact = _compact_structured(structured)
    parts.append(json.dumps(compact, indent=2, ensure_ascii=False))
    parts.append("```")
    parts.append("")

    if sec_data and not sec_data.get("error") and include_10k_full:
        f = sec_data.get("filing") or {}
        parts.append(f"# SEC 10-K 关键章节（filing date: {f.get('filing_date')}）")
        parts.append(f"原始文档: {f.get('url')}")
        parts.append("")
        secs = sec_data.get("sections") or {}
        # 优先级：Business → Risk Factors → MD&A → 其它
        for key, label in [("item_1", "## Item 1. Business"),
                           ("item_1a", "## Item 1A. Risk Factors"),
                           ("item_7", "## Item 7. Management's Discussion and Analysis"),
                           ("item_7a", "## Item 7A. Market Risk")]:
            sec = secs.get(key)
            if sec and sec.get("text"):
                parts.append(label)
                # 每章节最多 30K chars 给 Claude（防止超 context）
                txt = sec["text"][:30000]
                parts.append(txt)
                if len(sec["text"]) > 30000:
                    parts.append(f"\n[... 该章节剩余 {len(sec['text']) - 30000:,} chars 已截断 ...]")
                parts.append("")

    system = "\n".join(parts)
    user = (f"请基于以上材料，生成 {ticker} ({(structured.get('profile') or {}).get('company_name', '')}) "
            "的完整中文 Markdown 研报。严格按 8 个章节输出，确保每个论断都有数据依据。")
    return system, user


def _compact_structured(s: dict[str, Any]) -> dict[str, Any]:
    """裁剪结构化数据，去掉对 LLM 没用的字段。"""
    p = s.get("profile") or {}
    val = s.get("valuation") or {}
    deep = s.get("fundamentals_deep") or {}
    pc = s.get("peer_compare") or {}

    return {
        "ticker": s.get("ticker"),
        "company": {
            "name": p.get("company_name"),
            "sector": p.get("sector"),
            "industry": p.get("industry"),
            "market_cap_b": round((p.get("market_cap") or 0) / 1e9, 1),
            "ceo": p.get("ceo"),
            "employees": p.get("employees"),
            "ipo_date": p.get("ipo_date"),
            "description": (p.get("description") or "")[:800],
        },
        "valuation": {
            "dcf": val.get("dcf"),
            "analyst_estimates": (val.get("analyst_estimates") or {}).get("estimates", [])[:4],
        },
        "earnings_history": s.get("earnings_history", [])[:4],
        "dupont": deep.get("dupont"),
        "beneish": deep.get("beneish"),
        "altman": deep.get("altman"),
        "quality": deep.get("quality"),
        "peer_compare": {
            "peers": pc.get("peers"),
            "rankings": pc.get("rankings"),
            "composite_percentile": pc.get("composite_percentile"),
            "verdict": pc.get("verdict"),
        },
    }


# ────────────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────────────

def generate(ticker: str,
             peers: list[str] | None = None,
             include_10k: bool = True,
             model: str = claude_client.DEFAULT_MODEL,
             max_output_tokens: int = 6000) -> dict[str, Any]:
    """端到端生成研报。返回 {markdown, usage, cost, structured, sec, ...}"""
    print(f"📊 [1/4] 拉结构化基本面...", flush=True)
    structured = fundamental_report.build_report(ticker, peers=peers)
    if structured.get("error"):
        return {"error": structured["error"], "ticker": ticker}

    sec_data = None
    if include_10k:
        print(f"📜 [2/4] 拉 SEC 10-K 全文...", flush=True)
        sec_data = sec_filings.get_latest_10k_sections(
            ticker,
            sections_only=["item_1", "item_1a", "item_7", "item_7a"],
        )
        if sec_data.get("error"):
            print(f"  ⚠️ {sec_data['error']} — 继续但不带 10-K", flush=True)
            sec_data = None

    print(f"🧠 [3/4] 构造 prompt...", flush=True)
    system, user = build_messages(ticker, structured, sec_data, include_10k_full=include_10k)
    n_chars_sys = len(system)
    print(f"   system prompt: {n_chars_sys:,} chars (~{n_chars_sys // 4:,} tokens 估算)")

    if not claude_client.is_available():
        return {"error": "ANTHROPIC_API_KEY not set",
                "ticker": ticker,
                "structured": structured,
                "sec": sec_data,
                "system_preview": system[:1000]}

    print(f"💬 [4/4] Claude {model} 生成中（10-K 启用 prompt cache）...", flush=True)
    client = claude_client.ChatClient(model=model, max_tokens=max_output_tokens, temperature=0.2)
    resp = client.complete(system=system, user=user, cache_system=True)

    if not resp:
        return {"error": "Claude API 调用失败", "ticker": ticker}

    usage = resp.get("usage", {})
    cost = claude_client.estimate_cost(
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
        cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        model=model,
    )

    return {
        "ticker": ticker,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": resp.get("model"),
        "markdown": resp["text"],
        "usage": usage,
        "cost_usd": round(cost, 4),
        "latency_s": resp.get("latency_s"),
        "structured": structured,
        "sec_filing": (sec_data or {}).get("filing"),
    }


# ────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="个股 LLM 研报（端到端）")
    parser.add_argument("ticker")
    parser.add_argument("--peers", nargs="+")
    parser.add_argument("--no-10k", action="store_true", help="省 token，不拉 10-K")
    parser.add_argument("--model", default=claude_client.DEFAULT_MODEL,
                        choices=list(claude_client.MODEL_PRICING.keys()))
    parser.add_argument("--max-tokens", type=int, default=6000)
    parser.add_argument("--out-dir", default="data/reports")
    args = parser.parse_args()

    r = generate(args.ticker, peers=args.peers, include_10k=not args.no_10k,
                 model=args.model, max_output_tokens=args.max_tokens)

    if r.get("error"):
        print(f"\n❌ {r['error']}")
        if r.get("system_preview"):
            print("\n=== Prompt preview (首 1000 字) ===")
            print(r["system_preview"])
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    md_path = out_dir / f"{args.ticker}_research_{ts}.md"
    json_path = out_dir / f"{args.ticker}_research_{ts}.json"

    # Markdown 头部加 metadata
    md_full = (
        f"<!-- {args.ticker} 研报 · 生成时间 {r['generated_at']} · 模型 {r['model']} · "
        f"cost ${r['cost_usd']} · {r['latency_s']}s -->\n\n"
        + r["markdown"]
    )
    md_path.write_text(md_full)
    json_path.write_text(json.dumps(r, indent=2, ensure_ascii=False))

    print(f"\n✅ 研报已生成")
    print(f"💵 成本: ${r['cost_usd']} · ⏱ {r['latency_s']}s")
    print(f"🔢 token: in={r['usage'].get('input_tokens')} "
          f"out={r['usage'].get('output_tokens')} "
          f"cache_write={r['usage'].get('cache_creation_input_tokens', 0)} "
          f"cache_read={r['usage'].get('cache_read_input_tokens', 0)}")
    print(f"💾 Markdown: {md_path}")
    print(f"💾 JSON: {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
