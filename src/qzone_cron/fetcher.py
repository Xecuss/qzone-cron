from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def feed_to_dict(feed: Any) -> dict:
    """将 aioqzone feed 对象序列化为可 JSON 存储的字典（不含 AI 生成内容）。"""
    uin: int = feed.userinfo.uin
    nickname: str = feed.userinfo.nickname or str(uin)
    post_time: int = int(feed.common.time)
    appid: int = feed.common.appid
    content: str = (feed.summary.summary if feed.summary else "") or ""
    fid: str = getattr(feed, "fid", f"{uin}_{post_time}")

    like_count: int = feed.like.likeNum if feed.like else 0
    comment_count: int = feed.comment.num if feed.comment else 0

    top_comments: list[dict] = []
    if feed.comment and feed.comment.comments:
        for c in feed.comment.comments[:3]:
            top_comments.append({
                "user": c.user.nickname or str(c.user.uin),
                "content": c.content,
            })

    has_images = bool(feed.pic and getattr(feed.pic, "picdata", None))
    has_video = bool(feed.video)
    image_urls: list[str] = []
    if has_images:
        for pic in feed.pic.picdata:
            try:
                image_urls.append(str(pic.photourl.largest.url))
            except Exception:
                pass

    share_title: str = ""
    share_summary: str = ""
    if feed.operation and feed.operation.share_info:
        _s = feed.operation.share_info.summary or ""
        if "来自QQ空间" not in _s:
            share_title = feed.operation.share_info.title or ""
            share_summary = _s

    original: dict | None = None
    if feed.original is not None:
        from aioqzone.model.api.feed import FeedOriginal
        if isinstance(feed.original, FeedOriginal):
            orig_content = (feed.original.summary.summary if feed.original.summary else "") or ""
            orig_has_images = bool(feed.original.pic and getattr(feed.original.pic, "picdata", None))
            orig_image_urls: list[str] = []
            if orig_has_images and feed.original.pic:
                for pic in feed.original.pic.picdata:
                    try:
                        orig_image_urls.append(str(pic.photourl.largest.url))
                    except Exception:
                        pass
            original = {
                "deleted": False,
                "uin": feed.original.userinfo.uin,
                "nickname": feed.original.userinfo.nickname or str(feed.original.userinfo.uin),
                "content": orig_content,
                "has_images": orig_has_images,
                "has_video": bool(feed.original.video),
                "image_urls": orig_image_urls,
            }
        else:
            original = {"deleted": True}

    return {
        "fid": fid,
        "uin": uin,
        "nickname": nickname,
        "time": post_time,
        "appid": appid,
        "content": content,
        "has_images": has_images,
        "has_video": has_video,
        "image_urls": image_urls,
        "like_count": like_count,
        "comment_count": comment_count,
        "top_comments": top_comments,
        "original": original,
        "share_title": share_title,
        "share_summary": share_summary,
    }


def load_cookies(cookie_file: Path) -> dict[str, str] | None:
    if not cookie_file.exists():
        return None
    with open(cookie_file) as f:
        return json.load(f)


def save_cookies(cookie_file: Path, cookies: dict[str, str]) -> None:
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cookie_file, "w") as f:
        json.dump(cookies, f, indent=2)


def _detect_module_count(size: int) -> int:
    """
    从图像边长自动探测 QR 二维码的总模块数（含安静区）。
    QR v1-v40 含 4 模块安静区的总格数依次为 29, 33, 37, 41, ... (+4 递增)。
    返回能整除 size 且最小的合法总格数；若无匹配则退化为 1（逐像素）。
    """
    for total in range(29, 200, 4):
        if size % total == 0:
            return total
    return 1


def _print_qr_terminal(png: bytes) -> None:
    """
    将 QR 二维码 PNG 渲染为终端半块字符并打印到 stdout。

    终端字符格宽:高约为 1:2，利用上/下半块字符（▀ ▄ █ 空格）可将每格
    竖切为两个近似正方形的"像素"。因此将 QR 缩放至 module_count × module_count
    的正方形像素图，半块渲染后输出 module_count 字符宽 × module_count/2 字符高，
    乘以字符 1:2 比例，视觉上即为正方形。
    """
    import io as _io

    from PIL import Image

    img = Image.open(_io.BytesIO(png)).convert("1")
    w, h = img.size

    # 缩放至正方形模块网格，每个像素对应一个 QR 模块
    module_count = _detect_module_count(w)
    small = img.resize((module_count, module_count), Image.Resampling.NEAREST)
    sw, sh = small.size

    print()
    for y in range(0, sh, 2):
        row = ""
        for x in range(sw):
            top = small.getpixel((x, y)) == 0
            bot = small.getpixel((x, y + 1)) == 0 if y + 1 < sh else False
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


async def setup_login(uin: int, cookie_file: Path, qr_sender=None) -> None:
    """交互式二维码登录，将 cookie 保存至文件。

    qr_sender: 可选的二维码回调（如 QrSender 实例），接收 PNG bytes 并负责发送/更新；
               为 None 时仅在终端渲染。
    """
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
            # 通过 Telegram 发送/更新（若已配置）
            if qr_sender is not None:
                await qr_sender(png)

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
            try:
                resp = await api.get_active_feeds(attach_info=attach_info)
            except Exception as e:
                # tenacity.RetryError 包裹真正的异常，需要解包
                from tenacity import RetryError
                cause = e.last_attempt.exception() if isinstance(e, RetryError) else e
                from aioqzone.exception import QzoneError
                if isinstance(cause, QzoneError) and cause.code == -3000:
                    raise RuntimeError(
                        f"QQ空间返回「系统繁忙」（code=-3000），Cookie 已失效，需要重新登录。"
                    ) from e
                raise

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
