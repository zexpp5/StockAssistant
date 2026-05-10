"""Claude API 客户端（B 路线 Phase 2B）

设计：
  - 直接走 HTTP，不依赖 anthropic SDK（保持 portable）
  - 默认模型: claude-sonnet-4-6（性价比最优）
  - 支持 prompt caching：10-K 全文等大 prompt 缓存 5min，可省 90% 成本
  - 缺 ANTHROPIC_API_KEY 时所有调用返回 None（graceful degrade）

成本基准（截至 2025-12 公开定价）:
  Sonnet 4.6: $3 / 1M input, $15 / 1M output
  Cache write: 1.25× input  /  Cache read: 0.1× input
  → 200K-token 10-K 缓存：第一次 $0.75，之后每次 $0.06（94% 折扣）

公开 API:
  is_available() -> bool
  ChatClient(model=...).complete(system, user, cache_system=True) -> str | None
  estimate_cost(input_tokens, output_tokens, cache_hit=False) -> float
"""
from __future__ import annotations
import json
import logging
import os
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# 模型定价（USD per 1M tokens，2025-12）
MODEL_PRICING = {
    "claude-opus-4-7":   {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.5},
    "claude-sonnet-4-6": {"input": 3.0,  "output": 15.0, "cache_write": 3.75,  "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 0.8,  "output": 4.0,  "cache_write": 1.0,   "cache_read": 0.08},
}

DEFAULT_MODEL = "claude-sonnet-4-6"


def is_available() -> bool:
    return bool(ANTHROPIC_API_KEY)


def estimate_cost(input_tokens: int, output_tokens: int,
                  cache_write_tokens: int = 0,
                  cache_read_tokens: int = 0,
                  model: str = DEFAULT_MODEL) -> float:
    """估算单次调用成本（USD）。"""
    p = MODEL_PRICING.get(model, MODEL_PRICING[DEFAULT_MODEL])
    return (
        input_tokens * p["input"] / 1e6 +
        output_tokens * p["output"] / 1e6 +
        cache_write_tokens * p["cache_write"] / 1e6 +
        cache_read_tokens * p["cache_read"] / 1e6
    )


class ChatClient:
    """简洁的 Claude HTTP 客户端，专为单股研报场景。"""

    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = 4096,
                 temperature: float = 0.3, timeout: int = 120):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout

    def complete(self,
                 system: str | list[dict[str, Any]],
                 user: str | list[dict[str, Any]],
                 cache_system: bool = False,
                 max_retries: int = 2) -> dict[str, Any] | None:
        """单轮对话。

        cache_system=True 时把 system 标记为 ephemeral cache（5min TTL，省钱）。
        系统提示 > 1024 tokens（约 4000 chars）才值得 cache，否则 caching 反而贵。

        返回 {text, usage, model, latency_s}，失败返回 None。
        """
        if not ANTHROPIC_API_KEY:
            logger.warning("ANTHROPIC_API_KEY not set — returning None")
            return None

        # 构造 system: str 时转成 [{"type": "text", "text": ...}]
        if isinstance(system, str):
            sys_blocks = [{"type": "text", "text": system}]
        else:
            sys_blocks = list(system)

        if cache_system and sys_blocks:
            # 把最后一块标 ephemeral（cache 边界）
            sys_blocks[-1] = {**sys_blocks[-1], "cache_control": {"type": "ephemeral"}}

        # user: str → [{"type": "text", "text": ...}]
        if isinstance(user, str):
            user_blocks = [{"type": "text", "text": user}]
        else:
            user_blocks = list(user)

        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": sys_blocks,
            "messages": [{"role": "user", "content": user_blocks}],
        }

        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        for attempt in range(max_retries + 1):
            t0 = time.time()
            try:
                r = requests.post(ANTHROPIC_API_URL, headers=headers,
                                  data=json.dumps(body), timeout=self.timeout)
                latency = time.time() - t0
                if r.status_code == 200:
                    resp = r.json()
                    # 取 text content（messages API 标准结构）
                    text_parts = [b["text"] for b in resp.get("content", [])
                                  if b.get("type") == "text"]
                    return {
                        "text": "".join(text_parts),
                        "usage": resp.get("usage", {}),
                        "model": resp.get("model"),
                        "latency_s": round(latency, 2),
                        "stop_reason": resp.get("stop_reason"),
                    }

                # 错误：429/529 重试
                if r.status_code in (429, 503, 529) and attempt < max_retries:
                    wait = 2 ** attempt * 2
                    logger.warning("Anthropic %d, retry in %ds: %s",
                                   r.status_code, wait, r.text[:200])
                    time.sleep(wait)
                    continue

                logger.error("Anthropic API failed %d: %s",
                             r.status_code, r.text[:300])
                return None
            except Exception as e:
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                    continue
                logger.error("Anthropic call exception: %s", e)
                return None
        return None


# ────────────────────────────────────────────────────────
# CLI（健康检查）
# ────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Claude API 健康检查")
    parser.add_argument("--prompt", default="用一句话介绍 Anthropic")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        choices=list(MODEL_PRICING.keys()))
    args = parser.parse_args()

    if not is_available():
        print("❌ ANTHROPIC_API_KEY 未配置")
        return 1

    client = ChatClient(model=args.model, max_tokens=200)
    r = client.complete(
        system="你是一位简洁的助手，用中文回答。",
        user=args.prompt,
    )
    if not r:
        print("❌ API 调用失败")
        return 1

    print(f"✅ 模型: {r['model']}")
    print(f"⏱  耗时: {r['latency_s']}s")
    print(f"🔢 token: in={r['usage'].get('input_tokens')} "
          f"out={r['usage'].get('output_tokens')}")
    cost = estimate_cost(
        r['usage'].get('input_tokens', 0),
        r['usage'].get('output_tokens', 0),
        model=args.model,
    )
    print(f"💵 估算成本: ${cost:.4f}")
    print(f"\n--- 输出 ---\n{r['text']}\n")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
