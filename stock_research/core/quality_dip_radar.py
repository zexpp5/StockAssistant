"""好公司低价雷达 — 质量大盘股跌出折扣时才亮灯。

起因 2026-07-20:苹果 6-25 最低 $274,系统推荐池里它只排 58 名(估值/动量/反转
三因子全平庸),严选/早报从未出现,用户完整错过 $285→$334 这波。根因是打分公式
偏"便宜+催化"的 AI 链,天生抓不住靠回购/生态慢涨的质量大盘股 → 属于策略盲区,
不改打分公式(改公式需回测,见 2026-07-06 MU 反验教训),用独立雷达补盲区。

规则(预注册,别偷偷改):
- 白名单固定 8 只质量大盘(用户拍板"闭眼买的好公司"档),不搞动态入选;
- 52 周高点 = 近 252 交易日最高收盘(本地 price_daily,不依赖网络);
- 提醒线 = 52周高点 × 0.85(苹果案例回测:334×0.85≈284,正好逮住 6 月低点);
- 档位: 🟢深折(≥20%) / 🟡打折(10~20%) / ⚪贴近高点(<10%);
- 出口=dashboard 卡片(常驻) + 触线才进早报/飞书(平时零打扰,治"一天几十条")。

单一来源:compute_quality_dips();dashboard 构建期注入,job 落 JSON 兜底。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OUT_JSON = _REPO_ROOT / "data" / "latest" / "quality_dip_radar.json"

# (代码, 中文名, 一句话定位) — 用户"好公司"档;调名单要用户拍板,别自动进出
QUALITY_WATCHLIST: list[tuple[str, str, str]] = [
    ("AAPL", "苹果", "消费生态+回购机器"),
    ("MSFT", "微软", "云+办公 AI 化"),
    ("GOOGL", "谷歌", "搜索广告+云"),
    ("META", "META", "社交广告现金牛"),
    ("AMZN", "亚马逊", "电商+AWS 云"),
    ("NVDA", "英伟达", "AI 芯片龙头"),
    ("TSM", "台积电", "AI 芯片代工 90%"),
    ("AVGO", "博通", "定制 AI 芯片+网络"),
]

ALERT_DISCOUNT = 0.15  # 提醒线 = 高点打 85 折
DEEP_DIP = 0.20        # 🟢 深折
DIP = 0.10             # 🡪 🟡 打折


def _tier(discount: float) -> str:
    if discount >= DEEP_DIP:
        return "deep"
    if discount >= DIP:
        return "dip"
    return "near_high"


def compute_quality_dips(conn) -> list[dict]:
    """按本地日线算 8 只质量股的折扣档位,按折扣从深到浅排序。

    conn: 可读的 DuckDB 连接(调用方负责只读打开与关闭)。
    单票查询失败跳过该票,不让整个雷达挂掉。
    """
    rows: list[dict] = []
    for symbol, name_zh, note in QUALITY_WATCHLIST:
        try:
            r = conn.execute(
                """
                WITH recent AS (
                    SELECT trade_date, close FROM price_daily
                    WHERE symbol = ? AND close IS NOT NULL AND isfinite(close)
                    ORDER BY trade_date DESC LIMIT 252
                )
                SELECT max(close), (SELECT close FROM recent ORDER BY trade_date DESC LIMIT 1),
                       (SELECT max(trade_date) FROM recent), count(*)
                FROM recent
                """,
                [symbol],
            ).fetchone()
        except Exception:
            continue
        if not r or not r[0] or not r[1] or (r[3] or 0) < 60:
            continue
        high_252, last_close, last_date, _n = float(r[0]), float(r[1]), r[2], r[3]
        discount = 1.0 - last_close / high_252
        alert_line = high_252 * (1.0 - ALERT_DISCOUNT)
        rows.append({
            "symbol": symbol,
            "name_zh": name_zh,
            "note": note,
            "last_close": round(last_close, 2),
            "high_252": round(high_252, 2),
            "discount_pct": round(discount * 100, 1),
            "alert_line": round(alert_line, 2),
            "triggered": last_close <= alert_line,
            "tier": _tier(discount),
            "as_of": str(last_date),
        })
    rows.sort(key=lambda x: -x["discount_pct"])
    return rows


def triggered_rows(rows: list[dict]) -> list[dict]:
    """触线(跌破 85 折提醒线)的票 — 早报/推送只关心这些,平时零打扰。"""
    return [r for r in rows if r.get("triggered")]


def write_json(rows: list[dict]) -> Path:
    _OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"), "rows": rows}
    _OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return _OUT_JSON


def main() -> int:
    from stock_research.core.buy_zone import _open_conn
    conn, ok = _open_conn()
    if not ok or conn is None:
        print("⚠️ quality_dip_radar: DB 不可读(可能撞写锁),本次跳过")
        return 1
    try:
        rows = compute_quality_dips(conn)
    finally:
        conn.close()
    if not rows:
        print("⚠️ quality_dip_radar: 0 行,不落盘(保留旧 JSON)")
        return 1
    path = write_json(rows)
    trig = triggered_rows(rows)
    print(f"✅ quality_dip_radar: {len(rows)} 只 → {path.name} · 触线 {len(trig)} 只"
          + (f" ({', '.join(t['symbol'] for t in trig)})" if trig else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
