from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_cookies(cookie_file: Path) -> dict[str, str] | None:
    if not cookie_file.exists():
        return None
    with open(cookie_file) as f:
        return json.load(f)


def save_cookies(cookie_file: Path, cookies: dict[str, str]) -> None:
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cookie_file, "w") as f:
        json.dump(cookies, f, indent=2)


def _print_qr_terminal(png: bytes) -> None:
    """将 QR 二维码 PNG 渲染为终端半块字符并打印到 stdout。"""
    import io as _io

    from PIL import Image

    img = Image.open(_io.BytesIO(png)).convert("1")  # 转为 1-bit 黑白
    w, h = img.size
    # 每两行合并为一行（上半块 ▀ / 下半块 ▄ / 实心 █ / 空格）
    print()
    for y in range(0, h, 2):
        row = ""
        for x in range(w):
            top = img.getpixel((x, y)) == 0       # 0 = 黑色模块
            bot = img.getpixel((x, y + 1)) == 0 if y + 1 < h else False
            if top and bot:
                row += "█"
            elif top:
                row += "▀"
            elif bot:
                row += "▄"
            else:
                row += " "
        print(row)
    print()


async def setup_login(uin: int, cookie_file: Path) -> None:
    """交互式二维码登录，将 cookie 保存至文件。"""
    import asyncio

    from aioqzone.api.login import QrLoginManager
    from aioqzone.model.protocol.config import QrLoginConfig
    from qqqr.utils.net import ClientAdapter

    qr_path = Path("qrcode.png")

    async with ClientAdapter() as client:
        mgr = QrLoginManager(client, config=QrLoginConfig(uin=uin))

        async def on_qr_fetched(png: bytes | None, times: int, qr_renew: bool = False) -> None:
            if png is None:
                return
            # 无论是否有图形界面，都在终端渲染二维码
            try:
                _print_qr_terminal(png)
                logger.info("请用 QQ 扫描上方二维码（也已保存至 %s）。", qr_path.resolve())
            except Exception:
                logger.info("终端渲染失败，二维码已保存至 %s，请用 QQ 扫描。", qr_path.resolve())
            # 始终保存 PNG 以备不时之需
            qr_path.write_bytes(png)

        mgr.qr_fetched.add_impl(on_qr_fetched)

        await mgr.new_cookie()
        save_cookies(cookie_file, mgr.cookie)
        logger.info("登录成功！Cookie 已保存至 %s", cookie_file)


async def fetch_feeds(
    uin: int,
    cookie_file: Path,
    since_time: float = 0.0,
    max_pages: int = 10,
) -> list[Any]:
    """使用已保存的 cookie 抓取说说列表，仅返回 since_time 之后的新说说。"""
    from aioqzone.api import QzoneH5API
    from aioqzone.api.login import ConstLoginMan
    from qqqr.utils.net import ClientAdapter

    cookies = load_cookies(cookie_file)
    if not cookies:
        raise RuntimeError(
            "未找到 Cookie 文件，请先执行 'qzone-cron setup' 进行登录。"
        )

    async with ClientAdapter() as client:
        from qqqr.utils.net import use_mobile_ua
        use_mobile_ua(client)
        login_man = ConstLoginMan(uin=uin, cookie=cookies)
        api = QzoneH5API(client, login_man)

        feeds: list[Any] = []
        attach_info: str | None = None

        for page in range(max_pages):
            resp = await api.get_active_feeds(attach_info=attach_info)

            for feed in resp.vFeeds:
                feed_time: int = feed.common.time
                if since_time > 0 and feed_time <= since_time:
                    logger.debug(
                        "说说 %s (time=%d) 不晚于上次抓取时间，停止翻页。",
                        feed.fid,
                        feed_time,
                    )
                    return feeds
                feeds.append(feed)

            # FeedPageResp 字段：hasmore / attachinfo
            has_more: bool = resp.hasmore
            attach_info = resp.attachinfo

            if not has_more:
                break

        logger.info("共抓取到 %d 条新说说（%d 页）。", len(feeds), page + 1)
        return feeds
