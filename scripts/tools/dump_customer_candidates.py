"""N3: 扫所有 HKEX major_order 标题里的高频客户名候选，dump 给人工 review。

规则识别"与/向/獲得/簽訂/中標 + X + 协议/合同/订单"模式 X 部分:
  - 4-12 字中文实体 + 公司后缀（集团/公司/控股/科技/电子等）
  - 或 3-8 字英文实体 (HKEX 公告里偶有英文公司名)
未在现有 _CUSTOMER_KEYWORDS 白名单的，按出现频次降序 dump 到
data/latest/customer_candidates.json，人工 review 后手动追加到白名单。

注：选择「人工 review 入库」而非「自动 union」是出于质量考虑：
  · 自动 union 会让噪音（如"特许经营"、"控股公司"）混入白名单
  · 客户名作为 catalyst 强信号字段，宁可漏不可错
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from stock_research.jobs.event_calendar_hk_hkex_daily import _CUSTOMER_KEYWORDS

# 公司名后缀（含简繁）
_CN_SUFFIXES = [
    "集團", "集团", "控股", "公司", "有限公司",
    "股份", "科技", "電子", "电子", "半導體", "半导体", "電器", "电器",
    "互聯網", "互联网", "通訊", "通讯",
    "實業", "实业", "國際", "国际", "工業", "工业",
    "證券", "证券", "銀行", "银行", "保險", "保险",
    "資源", "资源", "能源", "新能源",
    "貿易", "贸易", "投資", "投资",
]

# 客户名抽取触发动词 + 后跟实体
_TRIGGER_PATTERNS = [
    r"(?:与|與|向|為|为|获得|獲得|代表|向其|和|及)\s*([一-龥A-Z][一-龥A-Za-z0-9]{1,15}(?:" + "|".join(_CN_SUFFIXES) + r"))",
    r"([A-Z][a-zA-Z]{2,15}(?:\s+(?:Inc|Corp|Group|Ltd|LLC|GmbH|AG|Co))\.?)",
]


def find_candidates(text: str) -> list[str]:
    out = []
    for pat in _TRIGGER_PATTERNS:
        for m in re.finditer(pat, text):
            ent = m.group(1).strip()
            if ent and len(ent) >= 3:
                out.append(ent)
    return out


def main() -> int:
    d_hkex = json.loads((REPO / "data" / "event_calendar_hk_hkex.json").read_text(encoding="utf-8") or "{}")
    events = d_hkex.get("events") or []
    print(f"扫描 {len(events)} 条 HKEX 公告...")

    counter: Counter = Counter()
    seen_in_titles: dict[str, set[str]] = {}  # candidate → set of ticker
    for e in events:
        title = e.get("title", "")
        long_text = e.get("long_text", "")
        full = f"{title} {long_text}"
        cands = find_candidates(full)
        for c in cands:
            counter[c] += 1
            seen_in_titles.setdefault(c, set()).add(e.get("ticker", ""))

    # 过滤已经在白名单里的
    new_candidates: list[dict] = []
    for c, n in counter.most_common():
        if any(kw in c or c in kw for kw in _CUSTOMER_KEYWORDS):
            continue
        # 过滤明显的噪音（自我引用、动作短语、stop words）
        if c.startswith(("本公司", "我公司", "該公司", "该公司", "其公司", "目標", "目标")):
            continue
        # 动作短语黑名单：购回/回购/发行/认购/重选/修订 等动词开头不是公司名
        if c.startswith(("購回", "购回", "回購", "回购", "發行", "发行", "認購", "认购",
                          "重選", "重选", "修訂", "修订", "建議", "建议", "採納", "采纳",
                          "授權", "授权", "通過", "通过", "批准",
                          "出售", "購買", "购买")):
            continue
        # 必须含明显公司后缀的至少 1 个（避免误命中"股份"这种泛词）
        strong_suffixes = ["集團", "集团", "控股", "公司", "有限公司",
                           "科技", "電子", "电子", "半導體", "半导体",
                           "互聯網", "互联网", "通訊", "通讯",
                           "Group", "Corp", "Inc", "Ltd"]
        if not any(s in c for s in strong_suffixes):
            continue
        if len(c) >= 3:
            new_candidates.append({
                "name": c,
                "freq": n,
                "tickers": sorted(seen_in_titles.get(c) or [])[:5],
            })
        if len(new_candidates) >= 50:
            break

    out_path = REPO / "data" / "latest" / "customer_candidates.json"
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "n_events_scanned": len(events),
        "n_candidates": len(new_candidates),
        "whitelist_size": len(_CUSTOMER_KEYWORDS),
        "instruction": (
            "人工 review：把信号强的候选追加到 event_calendar_hk_hkex_daily._CUSTOMER_KEYWORDS。"
            "宁缺勿滥 — 客户名误识别会让 catalyst 句子误导。"
        ),
        "candidates": new_candidates,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✅ 写入 {out_path}")
    print(f"  候选数: {len(new_candidates)}")
    print()
    print("Top 10 候选（频次降序）:")
    for c in new_candidates[:10]:
        tk_label = "/".join(c["tickers"][:3])
        print(f"  {c['freq']:>3}× {c['name']:<30} (ticker: {tk_label})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
