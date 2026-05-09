"""当日 picks 横截面审查（Risk Parity + 估值理性 + Markowitz 相关性）。

纯函数，输入已 normalized 的 picks/watchlist 列表 → 返回 JSON 可序列化 dict。
无 I/O 副作用；feishu/yfinance 由 jobs 层注入。

3 个审查器（与 reverse_validate v6 时间维度回测互补）：
  1. theme_concentration  — Risk Parity 视角，单一主题占 ⭐⭐⭐ 推荐比例
  2. valuation_sanity     — PEG > 3 / PE > 100 / 1Y > 200% 警告
  3. correlation_matrix   — Markowitz 视角，⭐⭐⭐ 推荐两两相关 > 0.75 的"伪分散"对
"""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timedelta
from typing import Any


def _to_float(v: Any) -> float | None:
    """飞书数字字段有时返回字符串，统一转 float。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def filter_strong_picks(picks_today: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """过滤出 ⭐⭐⭐ 推荐。"""
    return [p for p in picks_today if "⭐⭐⭐" in (p.get("normalized", {}).get("rating", "") or "")]


# ─────────── 审查器 1：主题集中度（Risk Parity） ───────────

def theme_concentration(picks_today: list[dict[str, Any]]) -> dict[str, Any]:
    """Risk Parity 视角：⭐⭐⭐ 推荐里单一主题占比。

    阈值：> 70% 严重 / > 50% 中度 / 否则健康。
    """
    strong = filter_strong_picks(picks_today)
    if not strong:
        return {"status": "skip", "reason": "今日无 ⭐⭐⭐ 推荐"}

    counter = Counter(p["normalized"].get("theme") or "未分类" for p in strong)
    total = sum(counter.values())
    distribution = [
        {"theme": t, "n": n, "pct": round(n / total * 100, 1)}
        for t, n in counter.most_common()
    ]
    top = distribution[0]

    if top["pct"] > 70:
        level, icon = "严重", "🔴"
    elif top["pct"] > 50:
        level, icon = "中度", "⚠️"
    else:
        level, icon = "健康", "🟢"

    return {
        "status": "ok",
        "level": level,
        "icon": icon,
        "total_strong": total,
        "distribution": distribution,
        "top_theme": top["theme"],
        "top_pct": top["pct"],
        "verdict": f"{icon} {level}集中：{top['theme']} 占 ⭐⭐⭐ 推荐 {top['pct']:.0f}% ({top['n']}/{total})",
    }


# ─────────── 审查器 2：估值合理性 ───────────

def valuation_sanity(picks_today: list[dict[str, Any]],
                     watchlist: list[dict[str, Any]]) -> dict[str, Any]:
    """估值理性：⭐⭐⭐ 推荐里 PEG > 3 / PE > 100 / 1Y > 200% 的标记。"""
    by_code = {(w["normalized"].get("code") or "").upper(): w for w in watchlist}
    warnings = []

    for p in filter_strong_picks(picks_today):
        n = p["normalized"]
        code = (n.get("code") or "").upper()
        wf = by_code.get(code, {}).get("fields", {})
        peg = _to_float(n.get("peg_at_pick")) or _to_float(wf.get("PEG"))
        pe = _to_float(n.get("pe_at_pick")) or _to_float(wf.get("远期PE"))
        y1 = _to_float(n.get("y1_at_pick")) or _to_float(wf.get("一年涨幅%"))

        flags = []
        if peg and peg > 3:
            flags.append(f"PEG={peg:.1f}（>3 偏贵）")
        if pe and pe > 100:
            flags.append(f"远期PE={pe:.0f}（>100 极贵）")
        if y1 and y1 > 200:
            flags.append(f"1Y涨幅={y1:.0f}%（已涨过头）")
        if flags:
            warnings.append({
                "code": code,
                "name": n.get("name"),
                "flags": flags,
            })

    return {"status": "ok", "warn_count": len(warnings), "warnings": warnings}


# ─────────── 审查器 3：相关性矩阵（Markowitz） ───────────

def _ticker_for(code: str) -> str:
    """code → yfinance ticker。"""
    if not code:
        return ""
    if "." in code:
        return code
    if code.isdigit():
        if code.startswith(("00", "30", "20")):
            return f"{code}.SZ"
        if code.startswith(("60", "68", "78", "603")):
            return f"{code}.SS"
        if code.startswith(("8", "9")):
            return f"{code}.BJ"
    return code


def correlation_matrix(picks_today: list[dict[str, Any]],
                       lookback_days: int = 180,
                       threshold: float = 0.75) -> dict[str, Any]:
    """Markowitz 相关性矩阵：⭐⭐⭐ 推荐两两相关 > threshold 的"伪分散"对。

    需要 yfinance 拉历史价；失败则 skip。
    """
    try:
        import yfinance as yf
    except ImportError:
        return {"status": "skip", "reason": "yfinance 未安装"}

    strong = filter_strong_picks(picks_today)
    if len(strong) < 2:
        return {"status": "skip", "reason": "样本不足（<2）"}

    tickers, name_map = [], {}
    for p in strong:
        n = p["normalized"]
        t = _ticker_for(n.get("code") or "")
        if t:
            tickers.append(t)
            name_map[t] = n.get("name") or t

    if len(tickers) < 2:
        return {"status": "skip", "reason": "有效 ticker < 2"}

    end = datetime.now()
    start = end - timedelta(days=lookback_days)
    try:
        df = yf.download(
            tickers, start=start, end=end,
            progress=False, auto_adjust=True, group_by="column",
        )
        if hasattr(df.columns, "levels") and "Close" in df.columns.get_level_values(0):
            close = df["Close"]
        elif "Close" in getattr(df, "columns", []):
            close = df[["Close"]]
            close.columns = tickers[:1]
        else:
            close = df
        if close is None or len(close) == 0:
            return {"status": "skip", "reason": "yfinance 返回空"}
        close = close.dropna(axis=1, how="all")
        if close.shape[1] < 2:
            return {"status": "skip", "reason": "有效价格序列 < 2"}
        rets = close.pct_change().dropna(how="all")
        corr = rets.corr()
    except Exception as e:
        return {"status": "skip", "reason": f"yfinance 失败: {str(e)[:80]}"}

    pairs = []
    cols = list(corr.columns)
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            v = corr.iloc[i, j]
            try:
                vf = float(v)
            except (TypeError, ValueError):
                continue
            if vf == vf and vf > threshold:
                pairs.append({
                    "a": cols[i], "name_a": name_map.get(cols[i], cols[i]),
                    "b": cols[j], "name_b": name_map.get(cols[j], cols[j]),
                    "r": round(vf, 3),
                })
    pairs.sort(key=lambda x: -x["r"])

    return {
        "status": "ok",
        "n_tickers": len(cols),
        "high_corr_pairs": pairs[:20],
        "threshold": threshold,
    }
