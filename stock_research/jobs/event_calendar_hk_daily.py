"""港股事件日历刷新：财报日 + EPS 超预期幅度 → data/event_calendar_hk.json

为什么独立文件而不是写进 event_calendar.json：
  原 event_calendar 模块 (_norm6) 是 A 股 6 位代码专属，港股 ticker 走不通。
  HK 事件下游消费者目前只有 morning_brief 的 why-now 解释器，
  独立文件 + 独立 schema 最简洁，A 股流程零侵入。

数据源：
  - yfinance.Ticker(ticker).earnings_dates  历史 + 未来财报日 + EPS 估/实/超预期
  - yfinance.Ticker(ticker).calendar         下次财报日（earnings_dates 缺失时兜底）

覆盖范围：
  - hk_universe.HK_TECH_UNIVERSE  33 只科技龙头白名单（用户 5/12 维护）
  - 当前 trade_delta_hk.json 里 buys + sells + holds 出现的所有 ticker
  - 当前 plan_v6 / hk_picks 里的 HK ticker（历史推荐过都补上）

输出 schema:
  {
    "generated_at": "...",
    "n_tickers": 38,
    "n_events": 280,
    "coverage": {"hit": 35, "miss": 3, "miss_tickers": [...]},
    "events": [
      {
        "ticker": "0992.HK",
        "name": "联想集团",
        "event_date": "2026-05-21",      # ISO 日期
        "event_type": "earnings",         # "earnings" / "earnings_upcoming"
        "eps_estimate": 0.03,
        "eps_actual": 0.04,                # 未来事件为 null
        "surprise_pct": 58.02,             # (actual-estimate)/estimate * 100，未来为 null
        "description": "5/21 EPS 0.04 / 估 0.03 超预期 +58.0%",
        "source": "yfinance/earnings_dates"
      }
    ]
  }
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from stock_research.core.hk_universe import HK_TECH_UNIVERSE

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _gather_universe() -> dict[str, str]:
    """ticker → name 映射，合并白名单 + 当前推荐池。"""
    out: dict[str, str] = {}
    for x in HK_TECH_UNIVERSE:
        out[x["ticker"]] = x.get("name", "")

    # trade_delta_hk 当前快照
    td_path = REPO / "data" / "latest" / "trade_delta_hk.json"
    if td_path.exists():
        try:
            d = json.loads(td_path.read_text(encoding="utf-8"))
            for bucket in ("buys", "sells", "holds"):
                for item in (d.get(bucket) or []):
                    t = item.get("ticker", "")
                    if t and t.endswith(".HK"):
                        out.setdefault(t, item.get("name", ""))
        except Exception as e:
            logger.warning("trade_delta_hk 解析失败: %s", e)

    # plan_v6 latest snapshot
    plan_path = REPO / "data" / "latest" / "plan_v6.json"
    if plan_path.exists():
        try:
            d = json.loads(plan_path.read_text(encoding="utf-8"))
            plan = d.get("plan_v6") or d.get("plan") or []
            for item in plan:
                t = item.get("ticker", "")
                if t and t.endswith(".HK"):
                    out.setdefault(t, "")
        except Exception as e:
            logger.warning("plan_v6 解析失败: %s", e)

    return out


def _fetch_yf_earnings(ticker: str, name: str) -> tuple[list[dict], str]:
    """返回 (events_list, status)；status ∈ {"ok", "no_data", "error"}。"""
    try:
        import yfinance as yf
    except ImportError:
        return [], "error"

    out: list[dict] = []
    try:
        t = yf.Ticker(ticker)
        ed = t.earnings_dates
        if ed is None or ed.empty:
            # 兜底：试 calendar 拿下次财报日
            cal = t.calendar or {}
            nxt = cal.get("Earnings Date")
            if nxt:
                d_ = nxt[0] if isinstance(nxt, list) else nxt
                if isinstance(d_, date):
                    out.append({
                        "ticker": ticker,
                        "name": name,
                        "event_date": d_.isoformat(),
                        "event_type": "earnings_upcoming",
                        "eps_estimate": cal.get("Earnings Average"),
                        "eps_actual": None,
                        "surprise_pct": None,
                        "description": f"{name or ticker} 下次财报预计 {d_.strftime('%m-%d')}",
                        "source": "yfinance/calendar",
                    })
                    return out, "ok"
            return [], "no_data"

        # earnings_dates DataFrame: index=Earnings Date (tz aware), cols=EPS Estimate / Reported EPS / Surprise(%)
        today = date.today()
        for idx, row in ed.iterrows():
            d_ = idx.date() if hasattr(idx, "date") else idx
            if not isinstance(d_, date):
                continue
            est = _safe_float(row.get("EPS Estimate"))
            act = _safe_float(row.get("Reported EPS"))
            surp = _safe_float(row.get("Surprise(%)"))

            if act is not None:
                # 已发布财报
                if surp is None and est not in (None, 0):
                    surp = (act - est) / abs(est) * 100
                surp_label = f"{surp:+.1f}%" if surp is not None else "n/a"
                desc = f"{name or ticker} {d_.strftime('%m-%d')} EPS {act:.2f} / 估 {est:.2f} 超预期 {surp_label}" if est is not None \
                    else f"{name or ticker} {d_.strftime('%m-%d')} EPS {act:.2f}（无估计）"
                out.append({
                    "ticker": ticker,
                    "name": name,
                    "event_date": d_.isoformat(),
                    "event_type": "earnings",
                    "eps_estimate": est,
                    "eps_actual": act,
                    "surprise_pct": round(surp, 2) if surp is not None else None,
                    "description": desc,
                    "source": "yfinance/earnings_dates",
                })
            elif d_ >= today:
                # 未来财报
                out.append({
                    "ticker": ticker,
                    "name": name,
                    "event_date": d_.isoformat(),
                    "event_type": "earnings_upcoming",
                    "eps_estimate": est,
                    "eps_actual": None,
                    "surprise_pct": None,
                    "description": f"{name or ticker} 下次财报 {d_.strftime('%m-%d')}（EPS 估 {est:.2f}）" if est is not None
                        else f"{name or ticker} 下次财报 {d_.strftime('%m-%d')}",
                    "source": "yfinance/earnings_dates",
                })
        return (out, "ok") if out else ([], "no_data")
    except Exception as e:
        logger.warning("yfinance fetch %s failed: %s", ticker, e)
        return [], "error"


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:
            return None
        return f
    except (TypeError, ValueError):
        return None


def main() -> int:
    universe = _gather_universe()
    logger.info("覆盖 %d 只港股 ticker", len(universe))

    all_events: list[dict] = []
    hit, miss, errored = 0, [], []
    for ticker, name in sorted(universe.items()):
        evs, status = _fetch_yf_earnings(ticker, name)
        if status == "ok":
            all_events.extend(evs)
            hit += 1
        elif status == "no_data":
            miss.append(ticker)
        else:
            errored.append(ticker)

    # 按日期排序便于人读
    all_events.sort(key=lambda e: e["event_date"], reverse=True)

    payload = {
        "generated_at": datetime.now().isoformat(),
        "n_tickers": len(universe),
        "n_events": len(all_events),
        "coverage": {
            "hit": hit,
            "miss": len(miss),
            "errored": len(errored),
            "miss_tickers": miss[:20],
            "errored_tickers": errored[:20],
        },
        "events": all_events,
    }

    out = REPO / "data" / "event_calendar_hk.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✅ 港股事件日历已写入 {out}")
    print(f"   tickers: {len(universe)} (hit {hit} / miss {len(miss)} / err {len(errored)})")
    print(f"   events:  {len(all_events)}")
    if miss:
        print(f"   miss 样本: {miss[:5]}")
    if errored:
        print(f"   error 样本: {errored[:5]}")
    return 0 if hit > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
