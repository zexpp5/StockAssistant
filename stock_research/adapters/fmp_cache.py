"""FMP API 24h 缓存层。

为什么需要：
  单份基本面研报（fundamental_report）现在调用 FMP 30+ 次：
  profile / dcf / income / balance / cash（年报）×几个模块各一遍 + 季报 8 季 ×3 表 = 24
  + peers profile loop 6-8 次 = 总 30+。FMP 免费档 250 calls/day → 跑 8 只就爆。

  研报数据 24h 内不会变（财报季内除外，财报日次日缓存自动失效），
  所以加 24h 文件缓存能把单只研报的"边际成本"从 30 calls 降到 0。

设计：
  - 文件缓存（不引入 SQLite/Redis 依赖）
  - 路径: data/cache/fmp/<sha256_24>.json
  - key = hash(path + sorted_params_excluding_apikey)
  - 只缓存成功响应；429/null/Error Message 不缓存（透明传递，下次重试）
  - mtime 超过 TTL 自动失效
  - 不修改 fmp_client API，调用方零侵入

使用：fmp_client._get 内部自动调用，无需手动管理。
手动清缓存：rm -rf data/cache/fmp/
"""
from __future__ import annotations
import hashlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 项目根 = adapters/ 的上 2 级
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = _PROJECT_ROOT / "data" / "cache" / "fmp"

DEFAULT_TTL_SEC = 24 * 3600  # 24h


def _make_key(path: str, params: dict | None) -> str:
    """构造稳定 cache key — path + 排序后的 params（不含 apikey）。"""
    p = {k: v for k, v in (params or {}).items() if k != "apikey"}
    canonical = path + "?" + json.dumps(p, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def _cache_file(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def get(path: str, params: dict | None, ttl_sec: int = DEFAULT_TTL_SEC) -> Any | None:
    """命中返回缓存 data；未命中或过期返回 None。"""
    f = _cache_file(_make_key(path, params))
    if not f.exists():
        return None
    age = time.time() - f.stat().st_mtime
    if age > ttl_sec:
        logger.debug("cache expired (%.0fs > %ds): %s %s", age, ttl_sec, path, params)
        return None
    try:
        payload = json.loads(f.read_text())
        return payload.get("data")
    except Exception as e:
        logger.warning("cache read failed for %s: %s", f, e)
        return None


def save(path: str, params: dict | None, data: Any) -> None:
    """缓存成功响应。data 为 None 时跳过（不缓存错误/限流）。"""
    if data is None:
        return
    # FMP 错误对象不缓存
    if isinstance(data, dict) and "Error Message" in data:
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = _cache_file(_make_key(path, params))
    p_clean = {k: v for k, v in (params or {}).items() if k != "apikey"}
    payload = {
        "path": path,
        "params": p_clean,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "data": data,
    }
    try:
        f.write_text(json.dumps(payload, ensure_ascii=False, default=str))
    except Exception as e:
        logger.warning("cache write failed for %s: %s", f, e)


def stats() -> dict[str, Any]:
    """缓存统计 — entries / 大小 / 最旧/最新。"""
    if not CACHE_DIR.exists():
        return {"entries": 0, "size_kb": 0, "dir": str(CACHE_DIR)}
    files = list(CACHE_DIR.glob("*.json"))
    sizes = [f.stat().st_size for f in files]
    mtimes = [f.stat().st_mtime for f in files]
    return {
        "entries": len(files),
        "size_kb": round(sum(sizes) / 1024, 1),
        "oldest_age_h": round((time.time() - min(mtimes)) / 3600, 1) if mtimes else None,
        "newest_age_h": round((time.time() - max(mtimes)) / 3600, 1) if mtimes else None,
        "dir": str(CACHE_DIR),
    }


def clear(older_than_h: float | None = None) -> int:
    """清缓存。older_than_h=None → 全清；指定数字 → 只清比该小时数老的。"""
    if not CACHE_DIR.exists():
        return 0
    cleared = 0
    cutoff = time.time() - older_than_h * 3600 if older_than_h else None
    for f in CACHE_DIR.glob("*.json"):
        if cutoff is None or f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                cleared += 1
            except Exception:
                pass
    return cleared


# ────────────────────────────────────────────────────────
# CLI（缓存管理）
# ────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="FMP 缓存管理")
    parser.add_argument("action", choices=["stats", "clear"],
                        help="stats: 看缓存状态 / clear: 清缓存")
    parser.add_argument("--older-than-h", type=float,
                        help="只清比该小时数老的（默认全清）")
    args = parser.parse_args()

    if args.action == "stats":
        s = stats()
        print(f"📦 FMP 缓存目录: {s['dir']}")
        print(f"   条目: {s['entries']}")
        print(f"   大小: {s['size_kb']} KB")
        if s['entries']:
            print(f"   最老条目: {s['oldest_age_h']} 小时前")
            print(f"   最新条目: {s['newest_age_h']} 小时前")
    elif args.action == "clear":
        n = clear(older_than_h=args.older_than_h)
        if args.older_than_h:
            print(f"🗑  清掉 {n} 个 > {args.older_than_h}h 的缓存条目")
        else:
            print(f"🗑  清掉所有 {n} 个缓存条目")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
