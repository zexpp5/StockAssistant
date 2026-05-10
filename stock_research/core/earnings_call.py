"""电话会议（Earnings Call）情绪分析（B 路线 Phase 3）

学术依据：
  Larcker & Zakolyukina (2012) JAR
  "Detecting Deceptive Discussions in Conference Calls"
  - 通过语言学特征（规避词、负面情绪词、第一人称）识别管理层"修饰性陈述"
  - 实证：管理层在欺诈季度的"hedging language"频率比正常季度高 2-3x

  Loughran & McDonald (2011) JF
  "When Is a Liability Not a Liability? Textual Analysis, Dictionaries, and 10-Ks"
  - 金融文本专用情绪词典（避免通用词典如 Harvard IV 的误判）

设计：
  1. fetch_transcript(ticker, year, quarter) → 全文 + 分管理层/Q&A
  2. score_hedging() → 规避词频率（管理层 vs Q&A 对比）
  3. score_sentiment() → Loughran-McDonald 词典负面/不确定/诉讼词频
  4. quarterly_trend(ticker, n=8) → 8 季度时序对比，识别"语气恶化"信号

数据源：FMP earning-call-transcript（Premium 限速，免费档每月 1-2 次）
"""
from __future__ import annotations
import logging
import re
import time
from typing import Any

from . import fmp_client

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────
# Larcker 规避词典（基于 Larcker & Zakolyukina 2012 + 经验扩展）
# ────────────────────────────────────────────────────────
HEDGING_WORDS = {
    # 模糊量化
    "approximately", "approximate", "about", "around", "roughly", "almost",
    "nearly", "essentially", "substantially", "fairly", "relatively",
    # 不确定情态
    "may", "might", "could", "would", "should", "perhaps", "presumably",
    "probably", "possibly", "likely", "potentially",
    # 弱化谓词
    "appears", "appear", "seem", "seems", "tend", "tends", "indicate",
    "indicates", "suggest", "suggests", "anticipate", "anticipates",
    "expect", "expects", "believe", "believes", "estimate", "estimates",
    # 推卸归因
    "challenging", "headwind", "uncertain", "uncertainty", "softness",
    "soft", "muted", "modest", "lumpy", "transient", "transitory",
}

# Loughran-McDonald 2011 金融负面词（节选最高频 50 个）
LM_NEGATIVE = {
    "loss", "losses", "decline", "declined", "declines", "decrease",
    "decreased", "decreases", "deficit", "delay", "delayed", "delays",
    "deteriorate", "deteriorated", "deterioration", "difficult", "difficulty",
    "drop", "dropped", "fail", "failed", "failure", "fell", "halt",
    "impair", "impaired", "impairment", "litigation", "lose", "loss",
    "negative", "negatives", "negatively", "obstacle", "outage", "penalize",
    "penalty", "poor", "problem", "problems", "recall", "reduce", "reduced",
    "reductions", "restate", "restated", "restatement", "shortfall",
    "slowdown", "stagnation", "termination", "underperform", "weak",
    "weakened", "weakness", "writeoff", "write-off",
}

# Loughran-McDonald 不确定性词（识别管理层"看不清未来"）
LM_UNCERTAINTY = {
    "approximate", "approximately", "assumed", "assumption", "believe",
    "contingency", "depend", "depending", "depends", "fluctuate",
    "fluctuation", "imprecise", "indefinite", "intangible", "may", "might",
    "perhaps", "possibility", "possible", "predict", "preliminary",
    "presume", "probabilistic", "probability", "probable", "probably",
    "random", "reconsider", "risk", "speculate", "speculation",
    "speculative", "tentative", "uncertain", "uncertainty", "unclear",
    "undetermined", "unknown", "unproven", "unsettled", "vague", "variable",
    "volatile", "volatility",
}


# ────────────────────────────────────────────────────────
# 拉 transcript
# ────────────────────────────────────────────────────────

def fetch_transcript(ticker: str, year: int, quarter: int) -> dict[str, Any] | None:
    """从 FMP 拉电话会议逐字稿。

    返回 {ticker, date, year, quarter, content, source}
    """
    if not fmp_client.is_available():
        return None
    raw = fmp_client._get("/earning-call-transcript",
                          {"symbol": ticker, "year": year, "quarter": quarter})
    if not raw or not isinstance(raw, list) or not raw:
        return None
    r = raw[0]
    return {
        "ticker": ticker,
        "date": r.get("date"),
        "year": year,
        "quarter": quarter,
        "content": r.get("content") or "",
        "source": "FMP/earning-call-transcript",
    }


def list_available_transcripts(ticker: str, max_periods: int = 8) -> list[dict[str, Any]]:
    """列出某股票最近 N 个季度有 transcript 的列表（不下载内容）。"""
    if not fmp_client.is_available():
        return []
    raw = fmp_client._get("/earning-call-transcript", {"symbol": ticker})
    if not raw or not isinstance(raw, list):
        return []
    out = []
    for r in raw[:max_periods]:
        out.append({
            "date": r.get("date"),
            "year": r.get("year"),
            "quarter": r.get("quarter"),
            "has_content": bool(r.get("content")),
        })
    return out


# ────────────────────────────────────────────────────────
# 分管理层 prepared remarks vs Q&A
# ────────────────────────────────────────────────────────

QA_TRIGGERS = [
    r"q\s*&\s*a",
    r"question[- ]and[- ]answer",
    r"questions and answers",
    r"q\s*-\s*and\s*-\s*a",
    r"operator.*we will now (begin|open).*question",
    r"operator.*first question",
]


def split_prepared_vs_qa(content: str) -> dict[str, str]:
    """把 transcript 分成"管理层准备发言 / Q&A"两段。

    简单启发：找 "Q&A" 或 "we will now begin the question-and-answer" 标志，
    之前是 prepared remarks，之后是 Q&A。找不到时全文当 prepared。
    """
    text_lower = content.lower()
    split_pos = None
    for pat in QA_TRIGGERS:
        m = re.search(pat, text_lower)
        if m:
            if split_pos is None or m.start() < split_pos:
                split_pos = m.start()

    if split_pos is None:
        return {"prepared": content, "qa": "", "split_at": -1}
    return {
        "prepared": content[:split_pos],
        "qa": content[split_pos:],
        "split_at": split_pos,
    }


# ────────────────────────────────────────────────────────
# 词频统计（语料化）
# ────────────────────────────────────────────────────────

_WORD_RE = re.compile(r"[a-z][a-z\-']+")


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def word_freq(text: str, vocab: set[str]) -> dict[str, Any]:
    """统计 text 中 vocab 集合词的命中率。"""
    tokens = _tokenize(text)
    n_total = len(tokens)
    if n_total == 0:
        return {"total_words": 0, "matches": 0, "ratio_per_1k": None, "top_words": {}}

    counter = {}
    for t in tokens:
        if t in vocab:
            counter[t] = counter.get(t, 0) + 1
    matches = sum(counter.values())
    top = dict(sorted(counter.items(), key=lambda x: -x[1])[:10])
    return {
        "total_words": n_total,
        "matches": matches,
        "ratio_per_1k": round(matches / n_total * 1000, 2),
        "top_words": top,
    }


# ────────────────────────────────────────────────────────
# 单次 transcript 综合打分
# ────────────────────────────────────────────────────────

def analyze_transcript(ticker: str, year: int, quarter: int) -> dict[str, Any]:
    """单期电话会议综合分析。"""
    t = fetch_transcript(ticker, year, quarter)
    if not t:
        return {"error": "transcript not available", "ticker": ticker,
                "year": year, "quarter": quarter}

    parts = split_prepared_vs_qa(t["content"])

    return {
        "ticker": ticker,
        "year": year,
        "quarter": quarter,
        "date": t.get("date"),
        "n_chars": len(t["content"]),
        "split_found": parts["split_at"] > 0,
        "prepared": {
            "n_chars": len(parts["prepared"]),
            "hedging": word_freq(parts["prepared"], HEDGING_WORDS),
            "negative": word_freq(parts["prepared"], LM_NEGATIVE),
            "uncertainty": word_freq(parts["prepared"], LM_UNCERTAINTY),
        },
        "qa": {
            "n_chars": len(parts["qa"]),
            "hedging": word_freq(parts["qa"], HEDGING_WORDS),
            "negative": word_freq(parts["qa"], LM_NEGATIVE),
            "uncertainty": word_freq(parts["qa"], LM_UNCERTAINTY),
        },
        "source": "FMP/earning-call-transcript + Larcker 2012 + Loughran-McDonald 2011",
    }


# ────────────────────────────────────────────────────────
# 时序趋势（8 季度对比）
# ────────────────────────────────────────────────────────

def quarterly_trend(ticker: str, n_quarters: int = 8,
                    sleep_sec: float = 0.5) -> dict[str, Any]:
    """对最近 N 季度的电话会议做时序对比。

    输出：每个季度 hedging/negative/uncertainty 的 ratio_per_1k，
    管理层(prepared) vs 分析师(Q&A) 分别给。
    """
    available = list_available_transcripts(ticker, max_periods=n_quarters)
    if not available:
        return {"error": "no transcripts available", "ticker": ticker}

    quarters = []
    for a in available:
        try:
            r = analyze_transcript(ticker, a["year"], a["quarter"])
            if r.get("error"):
                continue
            quarters.append({
                "date": r.get("date"),
                "year": r.get("year"),
                "quarter": r.get("quarter"),
                "prepared_hedging_per_1k": r["prepared"]["hedging"]["ratio_per_1k"],
                "prepared_negative_per_1k": r["prepared"]["negative"]["ratio_per_1k"],
                "prepared_uncertainty_per_1k": r["prepared"]["uncertainty"]["ratio_per_1k"],
                "qa_hedging_per_1k": r["qa"]["hedging"]["ratio_per_1k"],
                "qa_negative_per_1k": r["qa"]["negative"]["ratio_per_1k"],
                "qa_uncertainty_per_1k": r["qa"]["uncertainty"]["ratio_per_1k"],
            })
            time.sleep(sleep_sec)
        except Exception as e:
            logger.warning("transcript %s %sQ%s failed: %s",
                           ticker, a["year"], a["quarter"], e)

    if not quarters:
        return {"error": "no usable quarters", "ticker": ticker}

    # 计算趋势：最近 vs 4 季度均值
    quarters.sort(key=lambda x: (x["year"], x["quarter"]))
    latest = quarters[-1]
    baseline = quarters[:-1] if len(quarters) > 1 else quarters

    def avg(field):
        vals = [q[field] for q in baseline if q.get(field) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    deltas = {}
    for field in ["prepared_hedging_per_1k", "prepared_negative_per_1k",
                  "prepared_uncertainty_per_1k"]:
        cur = latest.get(field)
        base = avg(field)
        if cur is not None and base is not None:
            deltas[field] = {"latest": cur, "baseline_avg": base,
                             "delta_pct": round((cur - base) / base * 100, 1)
                             if base > 0 else None}

    # 警示：如果最近季度 hedging 或 uncertainty 比基线高 30%+
    warnings = []
    for field, d in deltas.items():
        if d["delta_pct"] is not None and d["delta_pct"] > 30:
            warnings.append(f"⚠️ {field} 较 {len(baseline)} 季均值高 {d['delta_pct']}%")

    return {
        "ticker": ticker,
        "n_quarters": len(quarters),
        "quarters": quarters,
        "latest_vs_baseline": deltas,
        "warnings": warnings,
        "source": "FMP transcripts + Larcker 2012",
    }


# ────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────

def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description="电话会议情绪分析")
    parser.add_argument("ticker")
    parser.add_argument("--year", type=int, help="单期分析时填")
    parser.add_argument("--quarter", type=int, help="单期分析时填")
    parser.add_argument("--trend", action="store_true",
                        help="时序对比（默认 8 季度）")
    parser.add_argument("--n", type=int, default=8, help="趋势季度数")
    parser.add_argument("--list", action="store_true", help="只列可用季度")
    parser.add_argument("--out")
    args = parser.parse_args()

    if not fmp_client.is_available():
        print("⚠️ FMP_API_KEY 未配置")
        return 1

    if args.list:
        avail = list_available_transcripts(args.ticker, args.n)
        for a in avail:
            print(f"  · {a['date']} {a['year']}Q{a['quarter']} "
                  f"{'✅' if a['has_content'] else '❌'}")
        return 0

    if args.trend or (not args.year):
        r = quarterly_trend(args.ticker, n_quarters=args.n)
    else:
        r = analyze_transcript(args.ticker, args.year, args.quarter)

    if r.get("error"):
        print(f"❌ {r['error']}")
        return 1

    print(json.dumps(r, indent=2, ensure_ascii=False))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(r, f, indent=2, ensure_ascii=False)
        print(f"\n💾 已保存: {args.out}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
