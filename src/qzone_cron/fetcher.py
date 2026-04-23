from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any, AsyncGenerator

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


def feed_to_rich_dict(feed: Any) -> dict:
    """将 aioqzone feed 对象序列化为包含尽可能完整信息的字典，用于离线分析。

    与 feed_to_dict 的区别：
      - 包含 ALL 评论（不限于前 3 条），含评论时间、点赞数、回复数
      - 包含评论回复内容
      - 包含访问/查看次数（visitor / view_count）
      - 包含 curkey / orgkey / ugckey / subid 等 feed 标识字段
    """
    base = feed_to_dict(feed)

    # ── 完整评论列表 ──────────────────────────────────────────────
    all_comments: list[dict] = []
    if feed.comment and feed.comment.comments:
        for c in feed.comment.comments:
            replies: list[dict] = []
            if getattr(c, "replys", None):
                for r in c.replys:
                    replies.append({
                        "uin": r.user.uin if hasattr(r, "user") else None,
                        "nickname": (r.user.nickname or str(r.user.uin)) if hasattr(r, "user") else None,
                        "content": getattr(r, "content", ""),
                        "date": getattr(r, "date", None),
                    })
            all_comments.append({
                "commentid": c.commentid,
                "uin": c.user.uin,
                "nickname": c.user.nickname or str(c.user.uin),
                "content": c.content,
                "date": c.date,
                "like_count": c.likeNum,
                "is_liked": c.isliked,
                "reply_count": c.replynum,
                "is_deleted": c.isDeleted,
                "is_private": c.isPrivate,
                "replies": replies,
            })
    base["all_comments"] = all_comments

    # ── 访客 / 浏览次数 ───────────────────────────────────────────
    visitor_count: int | None = None
    view_count: int | None = None
    if feed.visitor is not None:
        visitor_count = getattr(feed.visitor, "visitor_count", None)
        view_count = getattr(feed.visitor, "view_count", None)
    base["visitor_count"] = visitor_count
    base["view_count"] = view_count

    # ── Feed 标识键（用于后续互动 API 调用）────────────────────────
    # curkey / orgkey / ugckey 可能是 pydantic HttpUrl 对象，需转为字符串
    def _to_str(v: Any) -> str | None:
        return str(v) if v is not None else None

    base["curkey"] = _to_str(feed.common.curkey) if feed.common else None
    base["orgkey"] = _to_str(feed.common.orgkey) if feed.common else None
    base["ugckey"] = _to_str(feed.common.ugckey) if feed.common else None
    base["subid"] = feed.common.subid if feed.common else None

    # 覆盖 top_comments，改用 all_comments 中的前 3 条摘要（保持向后兼容）
    base["top_comments"] = [
        {"user": c["nickname"], "content": c["content"]} for c in all_comments[:3]
    ]

    return base


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


async def iter_self_feeds(
    uin: int,
    cookie_file: Path,
    since_time: float = 0.0,
    max_pages: int = 0,
) -> AsyncGenerator[tuple[int, list[Any]], None]:
    """异步生成器：每翻一页就 yield (page_num, feeds_in_this_page)，
    供调用方逐页处理（如实时写文件），无需等待全部抓取完成。

    :param uin: 自己的 QQ 号。
    :param cookie_file: 已保存 Cookie 的路径。
    :param since_time: 遇到不晚于此时间戳的说说时停止翻页；0 表示不限制。
    :param max_pages: 最大翻页数；0 表示不限制。
    """
    from aioqzone.api import QzoneH5API
    from aioqzone.api.login import ConstLoginMan
    from qqqr.utils.net import ClientAdapter, use_mobile_ua

    cookies = load_cookies(cookie_file)
    if not cookies:
        raise RuntimeError(
            "未找到 Cookie 文件，请先执行 'qzone-cron setup' 进行登录。"
        )

    async with ClientAdapter() as client:
        use_mobile_ua(client)
        login_man = ConstLoginMan(uin=uin, cookie=cookies)
        api = QzoneH5API(client, login_man)

        attach_info: str | None = None
        page = 0

        while True:
            try:
                resp = await api.get_feeds(hostuin=uin, attach_info=attach_info)
            except Exception as e:
                from tenacity import RetryError
                cause = e.last_attempt.exception() if isinstance(e, RetryError) else e
                from aioqzone.exception import QzoneError
                if isinstance(cause, QzoneError) and cause.code == -3000:
                    raise RuntimeError(
                        "QQ空间返回「系统繁忙」（code=-3000），Cookie 已失效，需要重新登录。"
                    ) from e
                raise

            page_feeds: list[Any] = []
            stop = False
            for feed in resp.vFeeds:
                feed_time: int = feed.common.time
                if since_time > 0 and feed_time <= since_time:
                    stop = True
                    break
                page_feeds.append(feed)

            yield page, page_feeds

            has_more: bool = resp.hasmore
            attach_info = resp.attachinfo
            page += 1

            if stop or not has_more:
                break
            if max_pages > 0 and page >= max_pages:
                logger.warning("已达到最大翻页数（%d），停止抓取。", max_pages)
                break

            # 每翻一页随机等待 1~3 秒，降低被频控的风险
            delay = random.uniform(1.0, 3.0)
            logger.debug("翻页间隔 %.1f 秒…", delay)
            await asyncio.sleep(delay)


async def fetch_self_feeds(
    uin: int,
    cookie_file: Path,
    since_time: float = 0.0,
    max_pages: int = 0,
    progress_cb: Any = None,
) -> list[Any]:
    """抓取自己空间主页的全部说说（使用 profile/get_feeds 接口）。

    :param uin: 自己的 QQ 号。
    :param cookie_file: 已保存 Cookie 的路径。
    :param since_time: 仅返回晚于此时间戳的说说；0 表示不限制（抓取全部）。
    :param max_pages: 最大翻页数；0 表示不限制（抓取全部）。
    :param progress_cb: 可选回调 ``(page, fetched_count)``，每翻一页调用一次。
    :return: 按时间倒序排列的 feed 对象列表（最新的在前）。
    """
    feeds: list[Any] = []
    async for page, page_feeds in iter_self_feeds(uin, cookie_file, since_time, max_pages):
        feeds.extend(page_feeds)
        if progress_cb is not None:
            progress_cb(page, len(feeds))
    logger.info("共抓取到自己的 %d 条说说。", len(feeds))
    return feeds

