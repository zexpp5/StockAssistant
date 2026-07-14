"""泡泡玛特(9992.HK)持仓剧本卡 · 每周一推飞书。

背景：2026-07-14 与用户一起定下"未来两个月怎么买卖"的剧本
（盈喜窗口→中报前→中报周→中报后纪律期），用户要求定时推送而不是靠他来问。
本 job 每周一 09:00 把"当前阶段 + 本周动作 + 纪律线状态"做成一张短卡推飞书。

设计约束（沿用既有偏好）：
  - 卡片要短：状态 3 行 + 本周动作 ≤3 行 + 免责 note，不放研究长文。
  - advisory 不 directive：全部用"可考虑/纪律参考"措辞，不写"必须买卖"。
  - 只读 DB（force_read_only 带锁重试），拿不到价格时降级为"价格暂缺"照样推。
  - 剧本有限期：EXPIRE_DATE 之后自动退出不再推送（launchd 可留着，job 自杀）。

触发：launchd com.linearview.stockassistant.popmart_playbook.plist 每周一 09:00。
CLI：
  /opt/homebrew/bin/python3 -m stock_research.jobs.popmart_playbook_card --dry-run
  /opt/homebrew/bin/python3 -m stock_research.jobs.popmart_playbook_card          # 真推
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

logger = logging.getLogger(__name__)

SYMBOL = "9992.HK"
NAME = "泡泡玛特"
# 持仓口径从 real_holdings 现读；这两个只做 DB 拿不到时的兜底
FALLBACK_COST = 167.0
FALLBACK_SHARES = 3000.0
# 纪律线（2026-07-14 与用户确认的剧本）
STOP_HALF_LINE = 150.0   # 跌破且放量 → 可考虑先减半仓
STOP_CLEAR_LINE = 140.0  # 再破 → 可考虑清仓观察
EST_REPORT_DATE = date(2026, 8, 18)   # 预估中报日（去年 8/19，前年 8/20；以港交所定档公告为准）
PROFIT_ALERT_DEADLINE = date(2026, 7, 24)  # 盈喜窗口截止（去年 7/15 发）
EXPIRE_DATE = date(2026, 9, 30)       # 剧本演完自动停


def _open_conn():
    try:
        lib = str(_REPO / "scripts" / "lib")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        import stock_db  # type: ignore
        return stock_db.get_db(force_read_only=True)
    except Exception as exc:
        logger.warning("只读连接失败：%s", exc)
        return None


def _load_market_state() -> dict:
    """最新收盘价 + 持仓成本/股数。任何一项拿不到都给 None，卡片降级显示。"""
    out: dict = {"close": None, "close_date": None,
                 "cost": FALLBACK_COST, "shares": FALLBACK_SHARES}
    conn = _open_conn()
    if conn is None:
        return out
    try:
        row = conn.execute(
            "SELECT trade_date, close FROM price_daily "
            "WHERE symbol = ? AND close IS NOT NULL AND isfinite(close) "
            "ORDER BY trade_date DESC LIMIT 1", [SYMBOL]).fetchone()
        if row:
            out["close_date"], out["close"] = str(row[0]), float(row[1])
    except Exception as exc:
        logger.warning("读价格失败：%s", exc)
    try:
        row = conn.execute(
            "SELECT avg_cost_local_per_share, remaining_shares FROM real_holdings "
            "WHERE symbol = ? AND (close_status IS NULL OR close_status != 'CLOSED') "
            "LIMIT 1", [SYMBOL]).fetchone()
        if row and row[0]:
            out["cost"] = float(row[0])
            out["shares"] = float(row[1] or 0)
    except Exception as exc:
        logger.warning("读持仓失败：%s", exc)
    try:
        conn.close()
    except Exception:
        pass
    return out


def _stage(as_of: date) -> tuple[str, str]:
    """返回 (阶段名, 本周动作文案)。文案全部 advisory 措辞。"""
    if as_of <= PROFIT_ALERT_DEADLINE:
        return ("① 盈喜窗口（去年 7/15 发）", (
            "**本周只观察一件事：发不发盈喜。**\n"
            "· 发了且冲高（如 +5%↑）→ 参考去年「盈喜后一周 -8%」，可考虑趁冲高减 1/3\n"
            "· 到 7/24 还没发 → 本身就是中报平淡的信号，进入「中报前减压」评估"))
    if as_of < EST_REPORT_DATE:
        return ("③ 中报前（预估 8/18 发布）", (
            "**盈喜季已过，核对：7 月有没有发过盈喜？**\n"
            "· 没发过 → 可考虑中报发布前减到半仓，别满仓扛一个已知偏弱的财报\n"
            "· 留意港交所「董事会会议通知」定档公告（约提前 2 周）"))
    if as_of <= EST_REPORT_DATE + timedelta(days=7):
        return ("④ 中报周 · 只看海外增速", (
            "🟢 海外没失速、明显超 20% 指引 → 拿着；一周内冲高可考虑兑现日附近减仓锁利，"
            "**别追加**（去年顶点=中报后第 5 天）\n"
            "🟡 增速 ~20%、海外平淡 → 维持现状，守纪律线\n"
            "🔴 海外负增长 / 整体 <15% → 逻辑破位，可考虑减一半以上"))
    return ("⑤ 中报后纪律期（至 9/30）", (
        "利好已兑现，纪律优先：\n"
        "· 冲高不追加，逢大涨日可考虑分批兑现\n"
        "· 严守下方纪律线，别抱「跌多了会回来」的执念"))


def build_card(as_of: date, ms: dict) -> dict:
    close, cost, shares = ms["close"], ms["cost"], ms["shares"]
    if close:
        pnl_pct = (close / cost - 1) * 100
        to_stop = (close / STOP_HALF_LINE - 1) * 100
        status = (f"现价 **{close:.1f}** 港元（{ms['close_date']}） · "
                  f"成本 {cost:.0f} · 浮盈亏 **{pnl_pct:+.1f}%** · {shares:.0f} 股\n"
                  f"距 150 减半线 **{to_stop:+.1f}%** · 距 140 清仓线 "
                  f"{(close / STOP_CLEAR_LINE - 1) * 100:+.1f}%")
        if close < STOP_HALF_LINE:
            status += "\n⚠️ **已跌破 150 纪律线** — 按剧本可考虑先减半仓（放量确认）"
    else:
        status = "⚠️ 价格暂缺（DB 被写锁或数据未刷新），阶段判断仍有效"
    days_to_report = (EST_REPORT_DATE - as_of).days
    stage_name, action = _stage(as_of)
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": status}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": (
            f"**当前阶段：{stage_name}**"
            + (f" · 距预估中报还有 **{days_to_report}** 天" if days_to_report >= 0 else "")
        )}},
        {"tag": "div", "text": {"tag": "lark_md", "content": action}},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": (
            "剧本定于 2026-07-14，中报日为预估值(去年8/19)以港交所定档公告为准 · "
            "每周一 09:00 推送至 9/30 · 仅供研究参考，不构成买卖指令"
        )}]},
    ]
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"🎭 {NAME}持仓剧本卡 · {as_of.isoformat()}"},
                "template": "wathet" if close and close >= STOP_HALF_LINE else "red",
            },
            "elements": elements,
        },
    }


def run(now: datetime | None = None, dry_run: bool = False) -> bool:
    as_of = (now or datetime.now()).date()
    if as_of > EXPIRE_DATE:
        logger.info("剧本已到期（%s > %s），不再推送。可卸载 launchd popmart_playbook。",
                    as_of, EXPIRE_DATE)
        return False
    ms = _load_market_state()
    if not ms["shares"]:
        logger.info("持仓已清零，剧本卡停止推送。")
        return False
    card = build_card(as_of, ms)
    if dry_run:
        import json as _json
        print(_json.dumps(card, ensure_ascii=False, indent=2))
        return True
    from stock_research.jobs.premarket_gate import _push  # 复用同一 webhook
    ok = _push(card)
    logger.info("剧本卡推送%s（%s）", "成功" if ok else "失败", as_of)
    return ok


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--as-of", help="YYYY-MM-DD 模拟日期（测试各阶段文案）")
    args = ap.parse_args()
    now = datetime.fromisoformat(args.as_of) if args.as_of else None
    ok = run(now=now, dry_run=args.dry_run)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
