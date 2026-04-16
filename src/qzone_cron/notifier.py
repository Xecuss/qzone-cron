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
SendMessage = Callable[[str], Awaitable["int | None"]]
PollUpdates = Callable[[int], Awaitable["tuple[list[dict], int]"]]


async def poll_updates_raw(bot_token: str, offset: int, long_poll_timeout: int = 30) -> tuple[list[dict], int]:
    """公开的长轮询接口，供 setup-tg 等命令直接调用。"""
    return await _poll_telegram_updates(bot_token, offset, long_poll_timeout)


async def send_message_raw(bot_token: str, chat_id: str, text: str) -> None:
    """向指定 chat_id 发送消息，供 setup-tg 等命令直接调用。"""
    await _send_telegram(bot_token, chat_id, text)


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


async def _send_telegram_single(bot_token: str, chat_id: str, text: str) -> int:
    """发送单条 Telegram 消息，返回 message_id。文本超长时截断到 4096 字符。"""
    import httpx

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text[:_TG_MAX_LEN],
        "parse_mode": "HTML",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return int(resp.json()["result"]["message_id"])


async def _poll_telegram_updates(
    bot_token: str, offset: int, long_poll_timeout: int = 0
) -> tuple[list[dict], int]:
    """调用 getUpdates 拉取从 offset 开始的新消息，返回 (updates, new_offset)。

    long_poll_timeout > 0 时启用长轮询（秒），HTTP 超时自动加 5 秒余量。
    """
    import httpx

    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params: dict = {"offset": offset, "timeout": long_poll_timeout, "allowed_updates": ["message"]}
    http_timeout = max(30.0, long_poll_timeout + 5.0)
    async with httpx.AsyncClient(timeout=http_timeout) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    updates: list[dict] = data.get("result", [])
    new_offset = updates[-1]["update_id"] + 1 if updates else offset
    return updates, new_offset


def make_send_message(cfg: "TelegramConfig") -> SendMessage | None:
    """返回一个发送单条 Telegram 消息并返回 message_id 的异步函数。

    若 Telegram 未配置，返回 None。
    """
    if not cfg.enabled:
        return None

    bot_token = cfg.bot_token
    chat_id = cfg.chat_id

    async def send_message(text: str) -> int | None:
        try:
            return await _send_telegram_single(bot_token, chat_id, text)
        except Exception as e:
            logger.error("发送 Telegram 消息失败：%s", e)
            return None

    return send_message


def make_poll_updates(cfg: "TelegramConfig") -> PollUpdates | None:
    """返回一个通过 getUpdates 拉取 Telegram 消息的异步函数。

    若 Telegram 未配置，返回 None。
    返回的函数签名：async (offset: int) -> (updates: list[dict], new_offset: int)
    """
    if not cfg.enabled:
        return None

    bot_token = cfg.bot_token

    async def poll_updates(offset: int) -> tuple[list[dict], int]:
        try:
            return await _poll_telegram_updates(bot_token, offset)
        except Exception as e:
            logger.error("拉取 Telegram 更新失败：%s", e)
            return [], offset

    return poll_updates


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
