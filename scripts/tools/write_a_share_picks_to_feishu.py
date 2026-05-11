"""把 data/a_share_picks.json 写入飞书「每日优选」表。

读取 stock_research.jobs.a_share_picks 产出的 JSON，把 6 因子优选结果写到
飞书 Bitable，方便手机查看。

字段映射（与 v6 美股写入对齐，便于 Bitable 视图过滤）：
  - 市场: 'A 股'（用于飞书视图按市场拆分）
  - 入选评分: '⭐⭐⭐ A 股强推' / '⭐⭐ A 股推荐' / '⭐ A 股关注'（按 composite 分位）
  - 入选理由: 6 因子明细 + 风险加权 + 拦截原因
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[2]  # repo root
sys.path.insert(0, str(REPO))

from feishu_auth import feishu_token, FEISHU_APP_TOKEN

PICKS_TABLE_ID = "tbl7K88JZ0ZMqPIE"
PICKS_BASE = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{PICKS_TABLE_ID}"


def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _grade_label(composite: float, cutoff: float) -> str:
    """根据综合分相对 cutoff 的位置标星级。"""
    if composite >= cutoff * 1.15:
        return "⭐⭐⭐ A 股强推"
    if composite >= cutoff * 1.05:
        return "⭐⭐ A 股推荐"
    return "⭐ A 股关注"


def _format_reasons(entry: dict, weights: dict) -> str:
    """把 6 因子明细 + 加权分 + 风险加权 排成可读文字。"""
    f_norm = entry.get("f_score_norm")
    f_score_str = f"{f_norm * 9:.0f}/9" if f_norm is not None else "?/9"
    lines = [
        f"📊 综合分 = {entry.get('composite', 0):.3f}（A 股 6 因子加权）",
        f"📚 因子明细：",
        f"  · Piotroski F-Score = {f_score_str} (权重 {weights['f_score']})",
        f"  · 12-1 月动量 (横截面分位) = {entry.get('momentum_norm', 0):.2f} (权重 {weights['momentum']})",
        f"  · 1 月反转 (横截面分位) = {entry.get('reversal_norm', 0):.2f} (权重 {weights['reversal']})",
        f"  · 龙虎榜机构净买入 = {entry.get('lhb_score', 0.5):.2f} (权重 {weights['lhb']})",
        f"  · 北向资金信号 = {entry.get('north_score', 0.5):.2f} (权重 {weights['north_flow']})",
        f"  · PEAD（真实公告日窗口）= {entry.get('pead_score', 0.5):.2f} (权重 {weights['pead']})",
        f"  · 政策主题加成 = +{entry.get('policy_boost', 0):.2f}",
        f"⚖️ 事件风险加权 = ×{entry.get('event_risk_score', 1.0):.2f} (解禁/减持降权)",
    ]
    notes = entry.get("notes") or []
    if notes:
        lines.append(f"📝 备注：{'; '.join(notes[:3])}")
    block = entry.get("block_reasons") or []
    if block:
        lines.append(f"⚠️ 拦截：{'; '.join(block)}")
    return "\n".join(lines)


def main():
    # 2026-05-11 架构调整：飞书 picks 表废弃为通知入口，DuckDB 是 single source of truth
    # 默认 no-op；FEISHU_WRITE_TABLES=1 强制启用（应急快照用）
    if os.environ.get("FEISHU_WRITE_TABLES", "0") != "1":
        print("⏭️  跳过 write_a_share_picks_to_feishu（FEISHU_WRITE_TABLES=0）")
        print("    A 股 picks 已在 data/a_share_picks.json + DuckDB（如有），dashboard 直接读")
        print("    应急写飞书：FEISHU_WRITE_TABLES=1 python3 write_a_share_picks_to_feishu.py")
        return 0

    src = REPO / "data" / "a_share_picks.json"
    if not src.exists():
        print(f"❌ 找不到 {src}，先运行 stock_research.jobs.a_share_picks")
        return 1

    payload = json.loads(src.read_text(encoding="utf-8"))
    selected = payload.get("selected", [])
    weights = payload.get("factor_weights", {})
    cutoff = payload.get("cutoff", 0.5)
    tailwind = payload.get("policy_tailwind", {})

    if not selected:
        print("⚠️ a_share_picks.json 无入选标的（可能 watchlist 无 A 股 / 全部被过滤）")
        return 0

    token = feishu_token()
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_ts = int(datetime.strptime(today_str, "%Y-%m-%d").timestamp() * 1000)

    print("=" * 90)
    print(f"  📤 写入飞书「每日优选」表（A 股 6 因子闭环 v9.0）")
    print("=" * 90)
    print(f"\n  选中 {len(selected)} 只 / 总 {payload.get('n_total', '?')} 只")
    print(f"  cutoff = {cutoff:.3f} (mode={payload.get('mode', '?')})")
    if tailwind:
        top_themes = sorted(tailwind.items(), key=lambda x: -x[1])[:3]
        print(f"  本期政策受益主题：{', '.join(f'{t}({c})' for t, c in top_themes)}")

    rows_written = 0
    for entry in selected:
        code = entry.get("code", "")
        name = entry.get("name", code)
        composite = entry.get("composite", 0)
        grade = _grade_label(composite, cutoff)

        fields = {
            "入选日期": today_ts,
            "股票名称": name,
            "代码": code,
            "市场": entry.get("market", "A 股"),
            "入选评分": grade,
            "综合得分": round(composite * 100, 1),  # 0-100 量纲，方便对比 v6 美股的 z×100
            "AI关联度": f"政策受益 +{entry.get('policy_boost', 0):.2f}" if entry.get('policy_boost', 0) > 0 else "中性",
            "主题分类": "A 股 v9.0 (6 因子闭环)",
            "入选理由": _format_reasons(entry, weights),
            "风险提示": "⚠️ A 股流动性约束已应用；真实成交受涨跌停/换手影响，建议分批",
            "跟踪状态": "🟢 在选中",
            "最近更新": int(datetime.now().timestamp() * 1000),
        }
        # 去掉 None / 空字符串字段，避免飞书 API 报错
        fields = {k: v for k, v in fields.items() if v not in (None, "")}

        r = requests.post(f"{PICKS_BASE}/records", headers=headers(token),
                          json={"fields": fields})
        d = r.json()
        if d.get("code") == 0:
            rows_written += 1
            print(f"    + {code:<8} {name:<10} composite={composite:.3f} → {grade}")
        else:
            print(f"    ! 失败 {code}: {d.get('msg')} (code={d.get('code')})")

    print(f"\n✅ 共写入 {rows_written} / {len(selected)} 条")
    print(f"  飞书表：https://w5scrwkn9y.feishu.cn/base/{FEISHU_APP_TOKEN}?table={PICKS_TABLE_ID}")
    return 0 if rows_written else 1


if __name__ == "__main__":
    sys.exit(main())
