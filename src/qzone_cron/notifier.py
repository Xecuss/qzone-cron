"""notifier — 全局通知工具。

目前支持通过 Telegram Bot 发送消息和二维码图片。
提供两个工厂函数：
  make_send_notice(cfg) — 返回 async send_notice(text) 文字通知函数
  make_qr_sender(cfg)   — 返回 QrSender 实例，用于登录二维码的发送与原地更新

用法（主流程）:
    from .notifier import make_send_notice, make_qr_sender
    context["send_notice"] = make_send_notice(config.telegram)
    qr_sender = make_qr_sender(config.telegram)
    await setup_login(uin, cookie_file, qr_sender=qr_sender)

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


# ─── QR code sender ───────────────────────────────────────────────────────────


async def _send_photo_telegram(
    bot_token: str, chat_id: str, png: bytes, caption: str = ""
) -> int:
    """发送图片消息，返回 message_id。"""
    import httpx

    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            data={"chat_id": chat_id, "caption": caption},
            files={"photo": ("qrcode.png", png, "image/png")},
        )
        resp.raise_for_status()
        return int(resp.json()["result"]["message_id"])


async def _edit_photo_telegram(
    bot_token: str, chat_id: str, message_id: int, png: bytes
) -> None:
    """使用 editMessageMedia 原地替换已有消息的图片。"""
    import json as _json

    import httpx

    url = f"https://api.telegram.org/bot{bot_token}/editMessageMedia"
    media = _json.dumps({"type": "photo", "media": "attach://photo"})
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            data={"chat_id": chat_id, "message_id": str(message_id), "media": media},
            files={"photo": ("qrcode.png", png, "image/png")},
        )
        resp.raise_for_status()


class QrSender:
    """有状态的 Telegram 二维码发送器。

    首次调用 sendPhoto 发送新消息并记录 message_id；
    后续调用 editMessageMedia 原地更新同一条消息，避免刷新时刷屏。
    """

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._message_id: int | None = None

    async def __call__(self, png: bytes) -> None:
        try:
            if self._message_id is None:
                self._message_id = await _send_photo_telegram(
                    self._bot_token,
                    self._chat_id,
                    png,
                    caption="📱 请扫描二维码登录 QQ 空间（二维码过期时会自动刷新）",
                )
                logger.info("QQ 登录二维码已发送至 Telegram（message_id=%d）。", self._message_id)
            else:
                await _edit_photo_telegram(
                    self._bot_token, self._chat_id, self._message_id, png
                )
                logger.info("Telegram 中的登录二维码已更新。")
        except Exception as e:
            logger.error("Telegram 二维码发送/更新失败：%s", e)


def make_qr_sender(cfg: "TelegramConfig") -> QrSender | None:
    """根据全局 TelegramConfig 返回 QrSender 实例。

    若 Telegram 未配置，返回 None（二维码仅在终端显示）。
    """
    if not cfg.enabled:
        return None
    return QrSender(cfg.bot_token, cfg.chat_id)
