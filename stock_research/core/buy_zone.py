"""可买入价格区间 (buy_zone) — 混合口径:估值锚定优先, 技术回撤兑底。

单一来源:后端算一次, morning_brief / dashboard / API 共用, 前端只渲染
(见 feedback_single_source_no_double_engine)。
⚠️ 研究参考, 非投资建议 —— AI 策略样本外 alpha 为负, 区间只回答"现价偏贵还是偏便宜",
不回答"该不该买"(见 project_ai_strategy_unvalidated)。

口径(用户 2026-06-16 拍板 = 混合兑底):
  估值锚定(优先): 近 TARGET_MAX_AGE_DAYS 天内有分析师目标价 →
      区间 = 目标价 × [VAL_LOW_MULT, VAL_HIGH_MULT] = [0.70, 0.85](留 15~30% 安全边际)
  技术回撤(兑底): 无目标价 → 区间 = MA50 ~ MA20 (sorted, 保证下沿≤上沿)
  现价定位: < 下沿 = 便宜 / 区间内 = 可考虑 / > 上沿 = 偏贵别追

数据现实(2026-06-16 体检):
  - forward_pe 只有当前快照、无历史序列 → 不能做"自历史 PE 分位"
  - 分析师目标价覆盖 ~33%, 收盘价历史深(500+日) → 混合保全覆盖
  - 负 PE / 数据全缺 → 返回 None, 上层显示"无法定价"
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DB_PATH = Path(os.environ.get("STOCK_DB_PATH") or (_REPO_ROOT / "stock_history_v2.duckdb"))

# 估值锚定:目标价折扣带(留安全边际)
VAL_LOW_MULT = 0.70
VAL_HIGH_MULT = 0.85
TARGET_MAX_AGE_DAYS = 120
# 技术回撤窗口
MA_SHORT = 20
MA_LONG = 50


def _open_conn():
    """优先用 stock_db 带锁重试的真只读;失败退回 raw read_only。

    返回 (conn, ok)。conn 为 None 表示打不开(库被写锁占满或不存在)。
    """
    try:
        import sys
        lib = str(_REPO_ROOT / "scripts" / "lib")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        import stock_db  # type: ignore
        return stock_db.get_db(force_read_only=True), True
    except Exception:
        try:
            import duckdb
            if not _DB_PATH.exists():
                return None, False
            return duckdb.connect(str(_DB_PATH), read_only=True), True
        except Exception:
            return None, False


def _position(current: float | None, low: float, high: float) -> str:
    if current is None:
        return "未知"
    if current < low:
        return "便宜"
    if current > high:
        return "偏贵"
    return "区间内"


def _latest_close_row(conn, symbol: str) -> tuple[float | None, str | None]:
    row = conn.execute(
        "SELECT close, trade_date FROM price_daily WHERE upper(symbol)=upper(?) AND close IS NOT NULL "
        "ORDER BY trade_date DESC LIMIT 1",
        [symbol],
    ).fetchone()
    if row and row[0] is not None:
        return float(row[0]), str(row[1]) if row[1] is not None else None
    return None, None


def _latest_close(conn, symbol: str) -> float | None:
    close, _trade_date = _latest_close_row(conn, symbol)
    return close


def _recent_target(conn, symbol: str, today: date):
    cutoff = today - timedelta(days=TARGET_MAX_AGE_DAYS)
    row = conn.execute(
        "SELECT price_target, event_date FROM analyst_grade_events "
        "WHERE upper(symbol)=upper(?) AND price_target IS NOT NULL AND price_target > 0 "
        "AND event_date >= ? ORDER BY event_date DESC LIMIT 1",
        [symbol, cutoff],
    ).fetchone()
    if row and row[0]:
        return float(row[0]), row[1]
    return None, None


def _moving_avgs(conn, symbol: str):
    rows = conn.execute(
        "SELECT close FROM price_daily WHERE upper(symbol)=upper(?) AND close IS NOT NULL "
        "ORDER BY trade_date DESC LIMIT ?",
        [symbol, MA_LONG],
    ).fetchall()
    closes = [float(r[0]) for r in rows if r[0] is not None]
    if len(closes) < MA_SHORT:
        return None, None
    ma_short = sum(closes[:MA_SHORT]) / MA_SHORT
    ma_long = sum(closes) / len(closes)  # 不足 50 日时用现有全部(>=20)
    return ma_short, ma_long


def compute_buy_zone(symbol: str, conn=None, *, today: date | None = None) -> dict[str, Any] | None:
    """单只票的可买入区间。估值锚定优先, 无目标价退技术回撤, 都没有返回 None。"""
    today = today or date.today()
    own = conn is None
    if own:
        conn, ok = _open_conn()
        if not ok or conn is None:
            return None
    try:
        current, current_trade_date = _latest_close_row(conn, symbol)
        target, tdate = _recent_target(conn, symbol, today)
        if target:
            low = round(target * VAL_LOW_MULT, 2)
            high = round(target * VAL_HIGH_MULT, 2)
            return {
                "symbol": symbol.upper(), "method": "估值",
                "low": low, "high": high, "current": current,
                "current_trade_date": current_trade_date,
                "target": target, "target_date": str(tdate) if tdate else None,
                "position": _position(current, low, high),
            }
        ma_short, ma_long = _moving_avgs(conn, symbol)
        if ma_short and ma_long:
            low = round(min(ma_short, ma_long), 2)
            high = round(max(ma_short, ma_long), 2)
            return {
                "symbol": symbol.upper(), "method": "技术",
                "low": low, "high": high, "current": current,
                "current_trade_date": current_trade_date,
                "target": None, "target_date": None,
                "position": _position(current, low, high),
            }
        return None
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def compute_buy_zones(symbols: Iterable[str], conn=None, *, today: date | None = None) -> dict[str, dict]:
    """批量:开一条只读连接复用, 返回 {SYMBOL: zone}。无数据的票直接不收录。"""
    today = today or date.today()
    own = conn is None
    if own:
        conn, ok = _open_conn()
        if not ok or conn is None:
            return {}
    try:
        out: dict[str, dict] = {}
        for s in symbols:
            if not s:
                continue
            try:
                z = compute_buy_zone(s, conn, today=today)
            except Exception:
                z = None
            if z:
                out[s.upper()] = z
        return out
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


_POS_ICON = {
    "便宜": "🟢 现价低于区间(偏便宜)",
    "区间内": "🟡 现价在区间内(可考虑)",
    "偏贵": "🔴 现价高于区间(偏贵,别追)",
    "未知": "现价未知",
}


# 紧凑版位置标(飞书卡片瘦身用)——只留 icon + 2 字结论
_POS_ICON_COMPACT = {
    "便宜": "🟢便宜",
    "区间内": "🟡区间内",
    "偏贵": "🔴偏贵别追",
}


def format_line(zone: dict | None, compact: bool = False) -> str | None:
    """渲染成早报一行(缩进 2 空格, 与现有 reason 行对齐)。研究参考措辞。

    compact=True(2026-06-24 飞书卡片瘦身)：只留区间+现价+一眼结论,去掉口径/锚说明。
    """
    if not zone:
        return None
    low, high = zone.get("low"), zone.get("high")
    if low is None or high is None:
        return None
    method = zone.get("method")
    cur = zone.get("current")
    if compact:
        pos_icon = _POS_ICON_COMPACT.get(zone.get("position"), "")
        cur_str = f"现价 ${cur:.0f} " if cur else ""
        return f"  💰 ${low:.0f}~${high:.0f} · {cur_str}{pos_icon}"
    pos_icon = _POS_ICON.get(zone.get("position"), "")
    if method == "估值" and zone.get("target"):
        anchor = f"｜锚:分析师目标价 ${zone['target']:.0f}"
    else:
        anchor = "｜锚:MA20~MA50 回撤区间"
    cur_str = f"现价 ${cur:.0f} · " if cur else ""
    return f"  💰 可买区间 ${low:.0f}~${high:.0f}（{method}）· {cur_str}{pos_icon}{anchor}"
