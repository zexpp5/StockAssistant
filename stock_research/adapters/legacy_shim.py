"""DuckDB 旧 dict-shape 兼容 shim (2026-05-11 PM 第二轮)

⚠️ 飞书 Bitable 已 100% 退役 — 旧文件名 adapters/feishu.py 改名为 legacy_shim.py.
   本模块底层 100% 读 DuckDB,写操作 no-op,只为兼容 9 个 job 的旧
   `[{record_id, fields, normalized}]` dict shape.

未来可彻底改造各 job 直接 import stock_db,然后删除本模块.

提供的 API:
- fetch_watchlist()   → 从 DuckDB watchlist 读,返回兼容旧 shape 的 list[dict]
- fetch_picks()       → 从 DuckDB reviews JOIN picks 读
- update_record()     → no-op + warning (分析结果应直接落 DuckDB,不再回写飞书)
- batch_update()      → no-op + warning
- get_token()         → 已废,raise RuntimeError
- ts_today_ms / ts_now_ms → 保留 (无副作用的工具函数)
"""
from __future__ import annotations
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# scripts/lib (stock_db) 路径注入
_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts" / "lib"))
from stock_db import fetch_all_watchlist, fetch_picks_view  # noqa: E402

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────
# 时间工具(无副作用,保留)
# ────────────────────────────────────────────────────────

def ts_today_ms() -> int:
    return int(datetime.strptime(datetime.now().strftime("%Y-%m-%d"), "%Y-%m-%d").timestamp() * 1000)


def ts_now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


# ────────────────────────────────────────────────────────
# Token / URL — 已废
# ────────────────────────────────────────────────────────

def get_token() -> str:
    raise RuntimeError(
        "feishu.get_token() 已废 — 飞书 Bitable 在 2026-05-11 PM 第二轮已 100% 退役。"
        " 改用 stock_db 直接读 DuckDB。"
    )


# ────────────────────────────────────────────────────────
# 读取 — 透传 DuckDB,封装成兼容旧调用方的 dict shape
# ────────────────────────────────────────────────────────

def fetch_watchlist(token: str | None = None) -> list[dict[str, Any]]:
    """从 DuckDB watchlist 读。返回 [{record_id, fields, normalized}, ...].

    旧 shape 兼容:
    - record_id     固定 "" (DuckDB 主键是 code,update_record 已 no-op,不再依赖)
    - fields        原飞书字段 dict,这里给空 dict
    - normalized    name/code/market/ai_level/industry,与旧实现一致
    """
    rows = fetch_all_watchlist()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "record_id": "",
            "fields": {},
            "normalized": {
                "name": r.get("name") or "",
                "code": r.get("code") or "",
                "market": r.get("market") or "",
                "ai_level": r.get("ai_relevance") or "",
                "industry": r.get("industry") or "",
            },
        })
    logger.info("fetched %d watchlist rows [DuckDB shim]", len(out))
    return out


def _date_to_ms(d: Any) -> int | None:
    """date / datetime → 飞书 Bitable 兼容的 ms timestamp (调用方按 int 处理)."""
    if d is None:
        return None
    if isinstance(d, datetime):
        return int(d.timestamp() * 1000)
    if hasattr(d, "year"):  # datetime.date (但不是 datetime)
        return int(datetime.combine(d, datetime.min.time()).timestamp() * 1000)
    return None


def fetch_picks(token: str | None = None) -> list[dict[str, Any]]:
    """从 DuckDB reviews JOIN picks 读最新 review_date 的 picks 视图.

    兼容旧 shape:
    - record_id     固定 "" (DuckDB 主键是 pick_date+code)
    - fields        空 dict
    - normalized    name/code/rating/score/theme/ai_level/pick_date(ms ts 兼容旧调用)
                    /peg_at_pick/pe_at_pick/y1_at_pick/cum_pct/days_held
    """
    rows = fetch_picks_view()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "record_id": "",
            "fields": {},
            "normalized": {
                "name": r.get("name") or "",
                "code": r.get("code") or "",
                "rating": r.get("rating") or "",
                "score": r.get("score"),
                "theme": r.get("theme") or "",
                "ai_level": r.get("ai_relevance") or "",
                "pick_date": _date_to_ms(r.get("pick_date")),  # ms ts 保持兼容
                "peg_at_pick": None,  # DuckDB picks 表存了但 view 没 select,暂留 None
                "pe_at_pick": None,
                "y1_at_pick": None,
                "cum_pct": r.get("pct"),
                "days_held": r.get("days_held"),
            },
        })
    logger.info("fetched %d picks rows [DuckDB shim]", len(out))
    return out


# ────────────────────────────────────────────────────────
# 写入 — no-op + warning
# ────────────────────────────────────────────────────────

def update_record(record_id: str, fields: dict[str, Any], table_id: str | None = None,
                  token: str | None = None) -> dict[str, Any]:
    """no-op (2026-05-11 PM):分析结果应直接 upsert 到 DuckDB,不再回写飞书。"""
    logger.warning(
        "feishu.update_record() 已 no-op — fields=%s 被丢弃。请把这些数据改写到 DuckDB。",
        list(fields.keys()),
    )
    return {"code": 0, "msg": "no-op (feishu retired)"}


def batch_update(updates: list[dict[str, Any]], table_id: str | None = None,
                 token: str | None = None, sleep_sec: float = 0.15) -> dict[str, int]:
    """no-op (2026-05-11 PM):同 update_record."""
    if updates:
        sample_fields = list(updates[0].get("fields", {}).keys())
        logger.warning(
            "feishu.batch_update() 已 no-op — %d 条更新被丢弃 (sample fields: %s)。"
            "请把这些数据改写到 DuckDB。",
            len(updates), sample_fields,
        )
    return {"success": 0, "failed": 0, "_skipped": "feishu retired"}


# ────────────────────────────────────────────────────────
# 元数据 — 已废
# ────────────────────────────────────────────────────────

def list_fields(table_id: str | None = None, token: str | None = None) -> list[dict[str, Any]]:
    logger.warning("feishu.list_fields() 已 no-op。")
    return []


def ensure_text_field(field_name: str, table_id: str | None = None,
                      token: str | None = None) -> bool:
    logger.warning("feishu.ensure_text_field(%s) 已 no-op。", field_name)
    return False
