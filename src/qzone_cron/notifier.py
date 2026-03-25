"""notifier — 全局通知工具。

目前支持通过 Telegram Bot 发送消息。
提供 make_send_notice() 工厂函数，根据全局 TelegramConfig 返回一个绑定好配置的
async send_notice(text) 协程函数，供主流程和各插件通过 context["send_notice"] 使用。

用法（主流程）:
    from .notifier import make_send_notice
    context["send_notice"] = make_send_notice(config.telegram)

用法（插件内）:
    send_notice = context.get("send_notice")
    if send_notice:
        await send_notice("发现登录失效，请重新扫码登录。")
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import TelegramConfig

logger = logging.getLogger(__name__)

# Telegram 单条消息最长 4096 字符
_TG_MAX_LEN = 4096

SendNotice = Callable[[str], Awaitable[None]]


async def _send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    """向指定 Telegram 会话发送消息（自动按 4096 字分块）。

    text 支持 HTML 格式（parse_mode="HTML"）。
    """
    import httpx

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    chunks = [text[i : i + _TG_MAX_LEN] for i in range(0, len(text), _TG_MAX_LEN)]

    async with httpx.AsyncClient(timeout=30.0) as client:
        for chunk in chunks:
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
            }
            resp = await client.post(url, json=payload)
            resp.raise_for_status()


def make_send_notice(cfg: "TelegramConfig") -> SendNotice | None:
    """根据全局 TelegramConfig 返回一个绑定好参数的 send_notice 函数。

    若 Telegram 未配置（bot_token 或 chat_id 为空），返回 None。
    """
    if not cfg.enabled:
        return None

    bot_token = cfg.bot_token
    chat_id = cfg.chat_id

    async def send_notice(text: str) -> None:
        """发送全局通知到 Telegram。"""
        try:
            await _send_telegram(bot_token, chat_id, text)
        except Exception as e:
            logger.error("发送 Telegram 通知失败：%s", e)

    return send_notice
