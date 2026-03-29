"""llm — OpenAI 兼容接口的统一调用封装。

提供单一入口 :func:`chat_completion`，负责构建 headers / payload 并通过
httpx.AsyncClient 发起请求，消除各插件中重复的样板代码。

用法示例::

    from qzone_cron.llm import chat_completion

    text = await chat_completion(
        cfg=openai_cfg,           # 含 api_key / base_url / model 的字典
        messages=[
            {"role": "system", "content": "你是..."},
            {"role": "user",   "content": "..."},
        ],
        timeout=30.0,
        max_tokens=500,
        temperature=0.7,
    )

``cfg`` 支持的字段：
- ``api_key``  : str — Bearer Token
- ``base_url`` : str — 接口根地址（默认 https://api.openai.com/v1）
- ``model``    : str — 模型名称（默认 gpt-4o-mini）

``**extra_payload`` 中的字段（如 ``temperature``、``max_tokens``、
``response_format``）会直接合并到请求 body。
"""
from __future__ import annotations

from typing import Any


async def chat_completion(
    cfg: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    timeout: float = 30.0,
    **extra_payload: Any,
) -> str:
    """调用 OpenAI 兼容的 /chat/completions，返回第一条回复的文本内容。

    :param cfg: 包含 ``api_key`` / ``base_url`` / ``model`` 的配置字典。
    :param messages: 标准 OpenAI messages 列表。
    :param timeout: HTTP 请求超时（秒）。
    :param extra_payload: 直接合并到请求 body 的额外字段。
    :raises httpx.HTTPStatusError: 接口返回非 2xx 状态码时抛出。
    """
    import httpx

    api_key: str = cfg.get("api_key", "")
    base_url: str = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    model: str = cfg.get("model", "gpt-4o-mini")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        **extra_payload,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{base_url}/chat/completions", headers=headers, json=payload
        )
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"].strip()
