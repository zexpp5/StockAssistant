#!/usr/bin/env python3
"""方案 3 探针：中际旭创 300308 在 4 个历史时点的 D+A（+参考 C）信号。

仅研究用途，不写库。输出 JSON + 终端表。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from factor_model_china import momentum_a_share, piotroski_a_share
from stock_research.core.lhb_signals import (
    STRONG_INST_BUY_YUAN,
    ULTRA_INST_BUY_YUAN,
    fetch_lhb_inst_window,
    _pick_col,
    _safe_float,
    _norm6,
)

CODE = "300308"
NAME = "中际旭创"
SLICES = [
    ("2024-02-29", "启动前约3M"),
    ("2024-05-31", "启动月"),
    ("2024-08-30", "启动后约3M"),
    ("2024-11-29", "启动后约6M"),
]

# D 亮灯：12-1 动量处于「趋势已建立」区间（单票探针，非横截面分位）
D_MOM_STRONG = 15.0   # % 12-1 momentum
D_MOM_WEAK = 5.0

# A 亮灯：与 lhb_signals / north_flow_signals 生产阈值对齐
A_LHB_SCORE = 0.60
A_NORTH_SCORE = 0.60


def _price_context(as_of: str) -> dict:
    import akshare as ak

    sina = "sz300308"
    target = pd.Timestamp(as_of)
    start = (target - pd.Timedelta(days=500)).strftime("%Y%m%d")
    end = (target + pd.Timedelta(days=400)).strftime("%Y%m%d")
    df = ak.stock_zh_a_daily(
        symbol=sina, start_date=start, end_date=end, adjust="qfq"
    )
    if df is None or len(df) < 30:
        return {"error": "no price"}
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    hist = df[df["date"] <= target]
    if hist.empty:
        return {"error": "no hist at as_of"}
    close = hist["close"].astype(float)
    p0 = float(close.iloc[-1])

    def _ret(days_ahead: int) -> float | None:
        future = df[df["date"] > target]
        if future.empty:
            return None
        idx = min(len(future) - 1, days_ahead)
        p1 = float(future.iloc[idx]["close"])
        return round((p1 / p0 - 1) * 100, 2)

    y1 = None
    if len(close) >= 253:
        y1 = round((float(close.iloc[-22]) / float(close.iloc[-253]) - 1) * 100, 2)

    return {
        "close_at_as_of": round(p0, 2),
        "y1_pct_at_as_of": y1,
        "fwd_63d_pct": _ret(63),
        "fwd_126d_pct": _ret(126),
    }


def _lhb_at_as_of(as_of: str, lookback_days: int = 20) -> dict:
    target = pd.Timestamp(as_of)
    start = (target - pd.Timedelta(days=lookback_days + 15)).strftime("%Y%m%d")
    end = target.strftime("%Y%m%d")
    inst_df = fetch_lhb_inst_window(start, end)
    c6 = _norm6(CODE)
    if inst_df is None or inst_df.empty:
        return {
            "score": 0.5,
            "inst_net_buy_yuan": 0.0,
            "lhb_appearances": 0,
            "has_strong_inst_buy": False,
            "lit": False,
            "notes": ["LHB 窗口无数据"],
        }

    code_col = _pick_col(inst_df, ["代码", "股票代码", "证券代码"])
    net_col = _pick_col(inst_df, ["机构净买额", "净买入金额", "机构净买入"])
    buy_col = _pick_col(inst_df, ["机构买入总额", "买入金额", "机构买入"])
    sell_col = _pick_col(inst_df, ["机构卖出总额", "卖出金额", "机构卖出"])
    date_col = _pick_col(inst_df, ["上榜日期", "日期"])

    if code_col is None:
        return {"score": 0.5, "lit": False, "notes": ["LHB 列名异常"]}

    net = 0.0
    appearances = 0
    max_daily_net = 0.0
    for _, row in inst_df.iterrows():
        if str(row.get(code_col, "")).strip() != c6:
            continue
        if date_col:
            d = pd.to_datetime(str(row.get(date_col, "")), errors="coerce")
            if pd.isna(d) or d > target:
                continue
        b = _safe_float(row.get(buy_col)) if buy_col else 0.0
        s = _safe_float(row.get(sell_col)) if sell_col else 0.0
        n = _safe_float(row.get(net_col)) if net_col else (b - s)
        net += n or 0.0
        appearances += 1
        max_daily_net = max(max_daily_net, n or 0.0)

    if appearances == 0:
        return {
            "score": 0.5,
            "inst_net_buy_yuan": 0.0,
            "lhb_appearances": 0,
            "has_strong_inst_buy": False,
            "lit": False,
            "notes": [f"近 {lookback_days} 日未上龙虎榜"],
        }

    score = 0.5 + max(-0.5, min(0.5, net / ULTRA_INST_BUY_YUAN * 0.5))
    notes = [f"机构净买 ¥{net/1e4:.0f}万 · 上榜 {appearances} 次"]
    if net > ULTRA_INST_BUY_YUAN:
        notes.append("极强机构净买")
    elif net > STRONG_INST_BUY_YUAN:
        notes.append("强机构净买")
    lit = score >= A_LHB_SCORE or max_daily_net >= STRONG_INST_BUY_YUAN
    return {
        "score": round(score, 4),
        "inst_net_buy_yuan": net,
        "lhb_appearances": appearances,
        "has_strong_inst_buy": max_daily_net >= STRONG_INST_BUY_YUAN,
        "lit": lit,
        "notes": notes,
    }


def _north_at_as_of(as_of: str, lookback_days: int = 20) -> dict:
    from stock_research.core.north_flow_signals import fetch_individual_history, _pick_col

    df = fetch_individual_history(CODE)
    if df is None or df.empty:
        return {"score": 0.5, "lit": False, "notes": ["无北向数据"]}

    date_col = _pick_col(df, ["持股日期", "日期"])
    pct_col = _pick_col(df, ["持股数量占发行股百分比", "持股占发行股本比例", "持股比例"])
    if date_col is None or pct_col is None:
        return {"score": 0.5, "lit": False, "notes": ["北向字段异常"]}

    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col]).sort_values(date_col)
    target = pd.Timestamp(as_of)
    df = df[df[date_col] <= target]
    if len(df) < 6:
        return {"score": 0.5, "lit": False, "notes": ["北向时序不足"]}

    recent = df.tail(lookback_days * 2)
    pcts = recent[pct_col].astype(float).tolist()
    pct_change_5d = pcts[-1] - pcts[-6] if len(pcts) >= 6 else None
    in_streak = 0
    out_streak = 0
    for i in range(len(pcts) - 1, 0, -1):
        delta = pcts[i] - pcts[i - 1]
        if delta > 0 and out_streak == 0:
            in_streak += 1
        elif delta < 0 and in_streak == 0:
            out_streak += 1
        else:
            break
    is_strong_inflow = in_streak >= 5 and (pct_change_5d or 0) > 0.3
    score = 0.5 + min(0.3, in_streak * 0.05) - min(0.3, out_streak * 0.05)
    if is_strong_inflow:
        score += 0.15
    score = round(max(0.0, min(1.0, score)), 4)
    lit = score >= A_NORTH_SCORE or is_strong_inflow
    notes = [
        f"连续加仓 {in_streak} 日 · 5d 占比变化 {pct_change_5d:+.2f}pp"
        if pct_change_5d is not None
        else f"连续加仓 {in_streak} 日"
    ]
    if is_strong_inflow:
        notes.append("强加仓")
    return {
        "score": score,
        "consecutive_inflow_days": in_streak,
        "pct_change_5d_pp": round(pct_change_5d, 3) if pct_change_5d is not None else None,
        "is_strong_inflow": is_strong_inflow,
        "lit": lit,
        "notes": notes,
    }


def _d_at_as_of(as_of: str) -> dict:
    mom = momentum_a_share(CODE, as_of=as_of)
    m = mom.get("momentum_12_1")
    r = mom.get("reversal_1m")
    err = mom.get("error")
    lit = False
    reason = []
    if m is not None:
        if m >= D_MOM_STRONG:
            lit = True
            reason.append(f"12-1动量 {m:+.1f}% ≥ {D_MOM_STRONG}%（强趋势）")
        elif m >= D_MOM_WEAK:
            reason.append(f"12-1动量 {m:+.1f}% 偏弱正（未达强阈值）")
        else:
            reason.append(f"12-1动量 {m:+.1f}% 不足")
    else:
        reason.append(f"动量不可用: {err}")
    return {
        "momentum_12_1_pct": m,
        "reversal_1m_pct": r,
        "lit": lit,
        "notes": reason,
        "error": err,
    }


def _c_ref_as_of(as_of: str) -> dict:
    pit = piotroski_a_share(CODE, as_of=as_of)
    fs = pit.get("f_score")
    return {
        "f_score": fs,
        "data_quality": pit.get("data_quality"),
        "error": pit.get("error"),
        "note": "C 参考：PIT 用报告日+120d，非真实披露日",
        "lit_ref": fs is not None and fs >= 7,
    }


def run_probe() -> dict:
    rows = []
    for as_of, label in SLICES:
        print(f"\n--- {as_of} ({label}) ---")
        price = _price_context(as_of)
        d = _d_at_as_of(as_of)
        lhb = _lhb_at_as_of(as_of)
        north = _north_at_as_of(as_of)
        c = _c_ref_as_of(as_of)
        a_lit = lhb["lit"] or north["lit"]
        da_lit = d["lit"] or a_lit
        row = {
            "as_of": as_of,
            "label": label,
            "price": price,
            "D": d,
            "A_lhb": lhb,
            "A_north": north,
            "A_lit": a_lit,
            "D_lit": d["lit"],
            "DA_lit": da_lit,
            "C_ref": c,
        }
        rows.append(row)
        print(
            f"  价 {price.get('close_at_as_of')} · 1Y@当时 {price.get('y1_pct_at_as_of')}% "
            f"· 后3M {price.get('fwd_63d_pct')}% · 后6M {price.get('fwd_126d_pct')}%"
        )
        print(f"  D: mom={d.get('momentum_12_1_pct')}% rev={d.get('reversal_1m_pct')}% → {'亮' if d['lit'] else '不亮'}")
        print(f"  A-LHB score={lhb['score']} net={lhb.get('inst_net_buy_yuan',0)/1e4:.0f}万 → {'亮' if lhb['lit'] else '不亮'}")
        print(f"  A-北向 score={north['score']} → {'亮' if north['lit'] else '不亮'}")
        print(f"  D+A: {'亮' if da_lit else '不亮'} | C F={c.get('f_score')} (参考)")

    out = {
        "code": CODE,
        "name": NAME,
        "probed_at": datetime.now().isoformat(timespec="seconds"),
        "thresholds": {
            "D_momentum_12_1_strong_pct": D_MOM_STRONG,
            "D_momentum_12_1_weak_pct": D_MOM_WEAK,
            "A_lhb_score": A_LHB_SCORE,
            "A_north_score": A_NORTH_SCORE,
        },
        "slices": rows,
    }
    out_path = REPO / "data" / "probe_300308_plan3.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 已写入 {out_path}")
    return out


if __name__ == "__main__":
    run_probe()
