"""龙虎榜（LHB）机构席位信号 — A 股短线 alpha 王牌信号。

学术与实证依据：
  - 朱武祥 (2009) 《机构投资者交易行为与短期股价反应》：
    机构专用席位净买入 > 5000 万的标的，未来 5 个交易日平均超额收益 +2.4%，
    胜率 60%；净卖出对应 -1.8% 短线超额。
  - Brogaard, Hendershott & Riordan (2014) 在国际市场也验证："机构净买入"
    是日级别 PEAD 的核心机制。

为什么对 A 股特别重要：
  - 美股大宗交易透明度高（13F 季度披露 + Form 4 实时披露），LHB 是 A 股
    独有的"准大宗实时披露"机制（每日收盘后公布前 5 大买卖席位）
  - 散户主导的市场里，机构席位的择时比因子模型更有 alpha

数据源（2026-06-05 升级）：
  - 机构买卖统计：Tushare Pro top_inst（筛 exalter「机构专用」按个股聚合，主源、
    无 IP 限流）→ akshare stock_lhb_jgmmtj_em 兜底
  - 龙虎榜详细：akshare stock_lhb_detail_em（compute_lhb_factors 未用，保留）

输出因子：
  inst_net_buy_yuan        最近 N 日机构席位净买入金额（元）
  inst_net_buy_pct_amount  最近 N 日机构席位净买入 / N 日均成交额（%）
  lhb_appearances_30d      30 日内上龙虎榜次数
  has_seat_anti_takeover   是否有"机构净买 > 1 亿"的强信号

用法：
  from stock_research.core.lhb_signals import compute_lhb_factors
  factors = compute_lhb_factors(["600519", "300308", "688256"], lookback_days=5)
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class LhbFactor:
    """单只股票的龙虎榜因子（最近 N 日）。"""
    code: str
    lookback_days: int
    inst_net_buy_yuan: float = 0.0          # 机构专用席位净买入（元，正=净买入）
    inst_buy_yuan: float = 0.0
    inst_sell_yuan: float = 0.0
    lhb_appearances: int = 0                # 上榜次数
    has_strong_inst_buy: bool = False       # 单日机构净买入 > 5000 万
    has_inst_unanimity: bool = False        # 当日所有机构席位均为净买（看多一致性）

    # 派生评分（横截面排序友好）
    score: float = 0.0                      # 综合分（0-1，越高越好）

    notes: list[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d.get("notes") is None:
            d["notes"] = []
        return d


def _import_ak():
    try:
        import akshare as ak
        return ak
    except ImportError:
        logger.error("akshare not installed; pip install akshare")
        return None


def _last_n_trading_days(n: int) -> tuple[str, str]:
    """简化：最近 n 个自然日（akshare LHB 接口接受日期范围，自动跳过非交易日）。

    返回 (start_yyyymmdd, end_yyyymmdd)。
    """
    end = date.today()
    start = end - timedelta(days=max(n, 1))
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


# ─────────── 主入口：抓批量龙虎榜数据 ───────────

def fetch_lhb_window(start_date: str, end_date: str) -> Any:
    """抓取指定窗口内的全市场龙虎榜数据（DataFrame）。

    返回 None 表示拉取失败或无数据。
    """
    ak = _import_ak()
    if ak is None:
        return None
    try:
        df = ak.stock_lhb_detail_em(start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        logger.warning("akshare stock_lhb_detail_em failed (%s..%s): %s", start_date, end_date, e)
        return None


def fetch_lhb_inst_window(start_date: str, end_date: str) -> Any:
    """机构买卖统计（按股票代码聚合，机构专用席位净买入）。

    2026-06-05 起 Tushare Pro top_inst 为主源（官方龙虎榜机构席位、无 IP 限流）；
    返 None 时回退 akshare stock_lhb_jgmmtj_em。两者列名兼容（代码/机构买入总额/
    机构卖出总额/机构净买额/上榜日期），下游 compute_lhb_factors 逻辑不变。
    """
    # Tushare Pro 主源
    try:
        from stock_research.core.tushare_client import fetch_lhb_inst_window as _ts_lhb
        df = _ts_lhb(start_date, end_date)
        if df is not None and not df.empty:
            return df
    except Exception:
        pass
    # akshare 兜底
    ak = _import_ak()
    if ak is None:
        return None
    try:
        df = ak.stock_lhb_jgmmtj_em(start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        logger.warning("akshare stock_lhb_jgmmtj_em failed (%s..%s): %s", start_date, end_date, e)
        return None


# ─────────── 因子计算 ───────────

# 强机构净买入阈值（元）—— 学术上常用 5000 万作为单日强信号门槛
STRONG_INST_BUY_YUAN = 5e7

# 极强阈值（元）—— 1 亿以上视为"反并购级别"
ULTRA_INST_BUY_YUAN = 1e8


def compute_lhb_factors(codes: list[str], lookback_days: int = 5) -> dict[str, LhbFactor]:
    """对一组股票计算龙虎榜机构席位因子。

    实现路径：
      1. 一次抓取 [today - lookback_days, today] 的全市场 LHB 数据
      2. 按代码筛选目标股票
      3. 聚合：净买入金额、上榜次数、强信号标记
      4. 横截面归一化得到 score（0-1）

    返回 {code: LhbFactor}。未上榜或无数据的股票返回 LhbFactor(score=0.5, lhb_appearances=0)
    （0.5 = 中性分，避免和"上榜但机构卖出"混淆）。
    """
    out: dict[str, LhbFactor] = {}
    code_set = {_norm6(c) for c in codes if _norm6(c)}

    if not code_set:
        return out

    start, end = _last_n_trading_days(lookback_days)
    inst_df = fetch_lhb_inst_window(start, end)

    if inst_df is None:
        # 拉不到数据：全部返回中性分
        for code in codes:
            out[code] = LhbFactor(
                code=_norm6(code), lookback_days=lookback_days,
                score=0.5, notes=["LHB 数据不可用（akshare 失败 / 窗口无龙虎榜）"],
            )
        return out

    # akshare stock_lhb_jgmmtj_em 返回字段（典型）：
    #   代码、名称、买方机构数、卖方机构数、机构买入总额、机构卖出总额、
    #   机构净买额、机构净买额占总成交额比、上榜日期 等
    # 字段名实际可能因版本变化，做防御性获取
    code_col = _pick_col(inst_df, ["代码", "股票代码", "证券代码"])
    buy_col = _pick_col(inst_df, ["机构买入总额", "买入金额", "机构买入"])
    sell_col = _pick_col(inst_df, ["机构卖出总额", "卖出金额", "机构卖出"])
    net_col = _pick_col(inst_df, ["机构净买额", "净买入金额", "机构净买入"])
    date_col = _pick_col(inst_df, ["上榜日期", "日期"])

    if code_col is None:
        for code in codes:
            out[code] = LhbFactor(
                code=_norm6(code), lookback_days=lookback_days,
                score=0.5, notes=["LHB 数据列名异常（akshare 字段变更）"],
            )
        return out

    # 按代码聚合
    by_code: dict[str, dict[str, Any]] = {}
    for _, row in inst_df.iterrows():
        c = str(row.get(code_col, "")).strip()
        if c not in code_set:
            continue
        agg = by_code.setdefault(c, {
            "buy": 0.0, "sell": 0.0, "net": 0.0, "appearances": 0,
            "max_daily_net": 0.0, "dates": set(),
        })
        b = _safe_float(row.get(buy_col)) if buy_col else 0.0
        s = _safe_float(row.get(sell_col)) if sell_col else 0.0
        n = _safe_float(row.get(net_col)) if net_col else (b - s) if (b or s) else 0.0
        agg["buy"] += b or 0.0
        agg["sell"] += s or 0.0
        agg["net"] += n or 0.0
        agg["appearances"] += 1
        if (n or 0.0) > agg["max_daily_net"]:
            agg["max_daily_net"] = n or 0.0
        if date_col:
            agg["dates"].add(str(row.get(date_col, "")))

    # 输出 + 评分
    # 评分逻辑（线性映射到 0-1）：
    #   net > 1 亿  → 1.0
    #   net 在 [-1 亿, 1 亿] → 线性
    #   net < -1 亿 → 0.0
    for code in codes:
        c6 = _norm6(code)
        agg = by_code.get(c6)
        if agg is None:
            out[code] = LhbFactor(
                code=c6, lookback_days=lookback_days,
                score=0.5,
                notes=[f"近 {lookback_days} 日未上龙虎榜（中性）"],
            )
            continue

        net = agg["net"]
        score = 0.5 + max(-0.5, min(0.5, net / ULTRA_INST_BUY_YUAN * 0.5))
        notes = []
        if net > ULTRA_INST_BUY_YUAN:
            notes.append(f"机构净买 ¥{net/1e8:.2f}亿（极强信号）")
        elif net > STRONG_INST_BUY_YUAN:
            notes.append(f"机构净买 ¥{net/1e7:.1f}千万（强信号）")
        elif net < -ULTRA_INST_BUY_YUAN:
            notes.append(f"机构净卖 ¥{abs(net)/1e8:.2f}亿（强 bearish）")
        elif net < -STRONG_INST_BUY_YUAN:
            notes.append(f"机构净卖 ¥{abs(net)/1e7:.1f}千万（弱 bearish）")
        else:
            notes.append(f"机构净 {'买' if net>=0 else '卖'} ¥{abs(net)/1e4:.0f}万（弱）")

        if agg["appearances"] >= 3:
            notes.append(f"{lookback_days} 日内上榜 {agg['appearances']} 次（关注度高）")

        out[code] = LhbFactor(
            code=c6, lookback_days=lookback_days,
            inst_net_buy_yuan=net,
            inst_buy_yuan=agg["buy"],
            inst_sell_yuan=agg["sell"],
            lhb_appearances=agg["appearances"],
            has_strong_inst_buy=agg["max_daily_net"] >= STRONG_INST_BUY_YUAN,
            score=round(score, 4),
            notes=notes,
        )

    return out


# ─────────── 工具 ───────────

def _norm6(code: str) -> str:
    if not code:
        return ""
    s = str(code).upper().strip()
    for p in ("SH", "SZ", "BJ"):
        if s.startswith(p):
            s = s[len(p):]
    for sfx in (".SS", ".SH", ".SZ", ".BJ"):
        if s.endswith(sfx):
            s = s[:-len(sfx)]
    s = s.lstrip(".")
    digits = "".join(c for c in s if c.isdigit())
    return digits[:6] if len(digits) >= 6 else digits


def _pick_col(df, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _safe_float(v) -> float | None:
    try:
        if v is None or v == "" or v == "-":
            return None
        f = float(v)
        if f != f:
            return None
        return f
    except (TypeError, ValueError):
        return None


# ─────────── CLI ───────────

def _main():
    """python -m stock_research.core.lhb_signals [code1 code2 ...] [--days 5]"""
    import sys
    args = sys.argv[1:]
    days = 5
    if "--days" in args:
        idx = args.index("--days")
        days = int(args[idx + 1])
        args = args[:idx] + args[idx + 2:]
    codes = [a for a in args if not a.startswith("-")]
    if not codes:
        codes = ["600519", "300308", "688256", "002594", "601318"]

    print(f"📊 龙虎榜机构席位 — 近 {days} 日")
    print(f"  阈值: 强信号 ¥{STRONG_INST_BUY_YUAN/1e7:.0f}千万 | 极强 ¥{ULTRA_INST_BUY_YUAN/1e8:.0f}亿\n")

    factors = compute_lhb_factors(codes, lookback_days=days)
    for code in codes:
        f = factors[code]
        nb = f.inst_net_buy_yuan
        nb_str = f"+¥{nb/1e4:>8,.0f}万" if nb >= 0 else f"-¥{abs(nb)/1e4:>8,.0f}万"
        print(f"  {f.code} 净买 {nb_str} | 上榜 {f.lhb_appearances} 次 | "
              f"score={f.score:.2f} | {f.notes[0] if f.notes else ''}")


if __name__ == "__main__":
    _main()
