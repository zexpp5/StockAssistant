"""飞书 Bitable 适配器：读取 watchlist、批量更新字段。

凭证优先级：环境变量 FEISHU_APP_ID/SECRET → 回退到旧的 douyin_to_feishu.feishu_token()。
所有方法都是无状态的纯 I/O，便于将来从 Web 服务调用。
"""
from __future__ import annotations
import os
import sys
import time
import logging
import requests
from datetime import datetime
from typing import Any

from .. import config

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────
# 凭证
# ────────────────────────────────────────────────────────

def _legacy_token() -> str:
    """fallback：从旧 douyin_to_feishu.py 拿 tenant_access_token。"""
    sys.path.insert(0, str(config.BASE_DIR))
    from douyin_to_feishu import feishu_token  # type: ignore
    return feishu_token()


def get_token() -> str:
    """获取 tenant_access_token。优先环境变量，否则走旧实现。"""
    app_id = config.FEISHU_APP_ID
    app_secret = config.FEISHU_APP_SECRET
    if app_id and app_secret:
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        r = requests.post(url, json={"app_id": app_id, "app_secret": app_secret}, timeout=15)
        return r.json()["tenant_access_token"]
    return _legacy_token()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _table_url(table_id: str) -> str:
    return f"{config.FEISHU_BITABLE_API}/apps/{config.FEISHU_BASE_TOKEN}/tables/{table_id}"


# ────────────────────────────────────────────────────────
# 工具
# ────────────────────────────────────────────────────────

def _normalize(v: Any) -> str:
    """飞书字段值标准化为字符串。"""
    if v is None:
        return ""
    if isinstance(v, list) and v:
        first = v[0]
        if isinstance(first, dict):
            return first.get("text") or first.get("name") or ""
        return str(first)
    if isinstance(v, dict):
        return v.get("text") or v.get("name") or ""
    return str(v)


def ts_today_ms() -> int:
    return int(datetime.strptime(datetime.now().strftime("%Y-%m-%d"), "%Y-%m-%d").timestamp() * 1000)


def ts_now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


# ────────────────────────────────────────────────────────
# Watchlist 读取
# ────────────────────────────────────────────────────────

def fetch_watchlist(token: str | None = None) -> list[dict[str, Any]]:
    """读取 watchlist 全表。返回 [{'record_id': ..., 'fields': {...}, 'normalized': {...}}]。

    `normalized` 把列表/对象类型字段拍平成字符串，便于业务逻辑直接用。
    """
    token = token or get_token()
    out: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(f"{_table_url(config.WATCHLIST_TABLE_ID)}/records",
                         headers=_headers(token), params=params, timeout=30)
        d = r.json().get("data", {}) or {}
        for it in d.get("items", []):
            f = it.get("fields", {}) or {}
            out.append({
                "record_id": it["record_id"],
                "fields": f,
                "normalized": {
                    "name": _normalize(f.get(config.Fields.NAME)),
                    "code": _normalize(f.get(config.Fields.CODE)),
                    "market": _normalize(f.get(config.Fields.MARKET)),
                    "ai_level": _normalize(f.get(config.Fields.AI_LEVEL)),
                    "industry": _normalize(f.get(config.Fields.INDUSTRY)),
                },
            })
        if not d.get("has_more"):
            break
        page_token = d.get("page_token")
        if not page_token:
            break
    logger.info("fetched %d watchlist rows", len(out))
    return out


def fetch_picks(token: str | None = None) -> list[dict[str, Any]]:
    """读取 daily picks 表全量。返回 [{'record_id', 'fields', 'normalized'}]。

    `normalized` 把 picks 关键字段拍平成可直接消费的 dict。
    """
    token = token or get_token()
    out: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(f"{_table_url(config.DAILY_PICKS_TABLE_ID)}/records",
                         headers=_headers(token), params=params, timeout=30)
        d = r.json().get("data", {}) or {}
        for it in d.get("items", []):
            f = it.get("fields", {}) or {}
            out.append({
                "record_id": it["record_id"],
                "fields": f,
                "normalized": {
                    "name": _normalize(f.get("股票名称")),
                    "code": _normalize(f.get("代码")),
                    "rating": _normalize(f.get("入选评分")),
                    "score": f.get("综合得分"),
                    "theme": _normalize(f.get("主题分类")),
                    "ai_level": _normalize(f.get("AI关联度")),
                    "pick_date": f.get("入选日期"),
                    "peg_at_pick": f.get("入选时PEG"),
                    "pe_at_pick": f.get("入选时远期PE"),
                    "y1_at_pick": f.get("入选时1Y%"),
                    "cum_pct": f.get("累计涨跌%"),
                    "days_held": f.get("持有天数"),
                },
            })
        if not d.get("has_more"):
            break
        page_token = d.get("page_token")
        if not page_token:
            break
    logger.info("fetched %d picks rows", len(out))
    return out


# ────────────────────────────────────────────────────────
# 单条 / 批量更新
# ────────────────────────────────────────────────────────

def update_record(record_id: str, fields: dict[str, Any], table_id: str | None = None,
                  token: str | None = None) -> dict[str, Any]:
    """更新单条记录的指定字段。"""
    token = token or get_token()
    table_id = table_id or config.WATCHLIST_TABLE_ID
    url = f"{_table_url(table_id)}/records/{record_id}"
    # 过滤空值（None/空字符串），避免覆盖已有数据
    clean = {k: v for k, v in fields.items() if v not in (None, "")}
    if not clean:
        return {"code": 0, "msg": "no fields to update"}
    r = requests.put(url, headers=_headers(token), json={"fields": clean}, timeout=30)
    return r.json()


def batch_update(updates: list[dict[str, Any]], table_id: str | None = None,
                 token: str | None = None, sleep_sec: float = 0.15) -> dict[str, int]:
    """批量更新；updates = [{'record_id': ..., 'fields': {...}}, ...]。

    返回 {'success': n, 'failed': n}。
    """
    token = token or get_token()
    success = 0
    failed = 0
    for u in updates:
        resp = update_record(u["record_id"], u["fields"], table_id=table_id, token=token)
        if resp.get("code") == 0:
            success += 1
        else:
            failed += 1
            logger.warning("update failed for %s: %s", u["record_id"], resp.get("msg"))
        time.sleep(sleep_sec)
    return {"success": success, "failed": failed}


# ────────────────────────────────────────────────────────
# 字段元数据（运行时检查 schema 是否存在某字段）
# ────────────────────────────────────────────────────────

def list_fields(table_id: str | None = None, token: str | None = None) -> list[dict[str, Any]]:
    token = token or get_token()
    table_id = table_id or config.WATCHLIST_TABLE_ID
    url = f"{_table_url(table_id)}/fields"
    r = requests.get(url, headers=_headers(token), timeout=30)
    return r.json().get("data", {}).get("items", []) or []


def ensure_text_field(field_name: str, table_id: str | None = None,
                      token: str | None = None) -> bool:
    """如果字段不存在则创建文本字段；返回是否新建。"""
    token = token or get_token()
    table_id = table_id or config.WATCHLIST_TABLE_ID
    fields = list_fields(table_id, token)
    if any(f.get("field_name") == field_name for f in fields):
        return False
    url = f"{_table_url(table_id)}/fields"
    r = requests.post(url, headers=_headers(token),
                      json={"field_name": field_name, "type": 1}, timeout=30)
    ok = r.json().get("code") == 0
    if ok:
        logger.info("created text field: %s", field_name)
    return ok
