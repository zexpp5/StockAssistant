"""飞书 tenant_access_token 获取（环境变量驱动）。

替代旧的 douyin_to_feishu.py — 后者含硬编码 secret，不适合公开发布。

环境变量：
  FEISHU_APP_ID       必填
  FEISHU_APP_SECRET   必填
  FEISHU_APP_TOKEN    可选（多维表 base 的 app_token；某些脚本会读这个）

提示：从 https://open.feishu.cn 创建自建应用，"凭证与基础信息"页能拿到 App ID/Secret。
"""
from __future__ import annotations
import os
import time
import requests

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_APP_TOKEN = os.environ.get("FEISHU_APP_TOKEN", "")

_token_cache: dict = {"value": None, "expire_at": 0.0}


def feishu_token() -> str:
    """获取 tenant_access_token，带 1 小时缓存（飞书 token 有效期 2 小时）。"""
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        raise RuntimeError(
            "FEISHU_APP_ID / FEISHU_APP_SECRET 未设置；"
            "复制 .env.example 为 .env 并填入凭证，或 export 到 shell。"
        )
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expire_at"]:
        return _token_cache["value"]
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    r = requests.post(url, json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=15)
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书认证失败: {data}")
    token = data["tenant_access_token"]
    _token_cache["value"] = token
    _token_cache["expire_at"] = now + 3600
    return token
