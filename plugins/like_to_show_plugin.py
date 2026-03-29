"""
like_to_show_plugin — 达到点赞阈值后公布第二段内容

在自己发布的说说中嵌入指令：
    /like_to_show <like_count> [edit_method]

- like_count  ：点赞阈值，达到后触发
- edit_method ：触发后的发布方式（默认 comment，可通过配置文件修改默认值）
  - append ：删除原说说，将去除指令的原始内容与第二段内容合并后发布新说说
  - delete ：删除原说说，仅将第二段内容作为新说说单独发布
  - new    ：发布第二段内容为新说说，原说说保留
  - comment：在原说说的评论区发布第二段内容（原说说保留）

工作流程：
1. 检测到含 /like_to_show 的自己说说
   → 登记到状态文件
   → 发送 TG 通知，提示用户 reply 该消息来提供第二段内容
2. 每次 cron 扫描 tg_updates（由主流程统一拉取，存于 context）
   → 匹配 reply_to_message_id 找到对应任务，写入 second_content
3. 每次 cron 从 feed_store 读取当前点赞数
   → 点赞达到阈值 且 second_content 已填写 → 执行
   → 点赞达到阈值 但 second_content 缺失   → 发送 TG 警告（仅警告一次）
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

PLUGIN_NAME = "like_to_show_plugin"
ENABLED = True

logger = logging.getLogger(PLUGIN_NAME)

_STATE_FILENAME = "like_to_show_state.json"

# 匹配 /like_to_show 10 或 /like_to_show 10 append 等写法
_VALID_METHODS = ("append", "delete", "comment", "new")
_DEFAULT_EDIT_METHOD = "comment"
_CMD_RE = re.compile(
    r"/like_to_show\s+(\d+)(?:\s+(append|delete|comment|new))?(?:\s|$)",
    re.IGNORECASE,
)


# ─── 工具函数 ──────────────────────────────────────────────────────────────────

def _parse_edit_method(value: str | None, default: str = _DEFAULT_EDIT_METHOD) -> str:
    """解析 edit_method 参数，不合法或缺省时返回 default。"""
    if not value:
        return default
    v = value.lower()
    return v if v in _VALID_METHODS else default


def _strip_directive(content: str) -> str:
    """从原始内容中移除 /like_to_show 指令，返回清理后的文本。"""
    return _CMD_RE.sub("", content).strip()


def _load_state(state_file: Path) -> list[dict]:
    if not state_file.exists():
        return []
    try:
        with open(state_file) as f:
            data = json.load(f)
        return data.get("pending", [])
    except Exception:
        logger.warning("读取 like_to_show 状态文件 %s 失败，视为空列表。", state_file)
        return []


def _save_state(state_file: Path, pending: list[dict]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w") as f:
        json.dump({"pending": pending}, f, indent=2, ensure_ascii=False)


async def _delete_feed(fid: str, appid: int, uin: int, cookie_file: Path) -> bool:
    """调用 API 删除指定说说，返回是否成功。"""
    from aioqzone.api import QzoneH5API
    from aioqzone.api.login import ConstLoginMan
    from qqqr.utils.net import ClientAdapter, use_mobile_ua

    if not cookie_file.exists():
        logger.error("Cookie 文件不存在，无法删除说说 %s。", fid)
        return False

    with open(cookie_file) as f:
        cookies = json.load(f)

    try:
        async with ClientAdapter() as client:
            use_mobile_ua(client)
            login_man = ConstLoginMan(uin=uin, cookie=cookies)
            api = QzoneH5API(client, login_man)
            await api.delete_ugc(fid=fid, appid=appid)
        return True
    except Exception as e:
        logger.error("删除说说 %s 失败：%s", fid, e)
        return False


async def _publish_feed(content: str, uin: int, cookie_file: Path) -> bool:
    """调用 API 发布新说说，返回是否成功。"""
    from aioqzone.api import QzoneH5API
    from aioqzone.api.login import ConstLoginMan
    from qqqr.utils.net import ClientAdapter, use_mobile_ua

    if not cookie_file.exists():
        logger.error("Cookie 文件不存在，无法发布说说。")
        return False

    with open(cookie_file) as f:
        cookies = json.load(f)

    try:
        async with ClientAdapter() as client:
            use_mobile_ua(client)
            login_man = ConstLoginMan(uin=uin, cookie=cookies)
            api = QzoneH5API(client, login_man)
            await api.publish_mood(content=content)
        return True
    except Exception as e:
        logger.error("发布说说失败：%s", e)
        return False


async def _add_comment(fid: str, appid: int, hostuin: int, content: str, uin: int, cookie_file: Path) -> bool:
    """调用 API 在指定说说下发评论，返回是否成功。"""
    from aioqzone.api import QzoneH5API
    from aioqzone.api.login import ConstLoginMan
    from qqqr.utils.net import ClientAdapter, use_mobile_ua

    if not cookie_file.exists():
        logger.error("Cookie 文件不存在，无法发评论。")
        return False

    with open(cookie_file) as f:
        cookies = json.load(f)

    try:
        async with ClientAdapter() as client:
            use_mobile_ua(client)
            login_man = ConstLoginMan(uin=uin, cookie=cookies)
            api = QzoneH5API(client, login_man)
            await api.add_comment(hostuin=hostuin, fid=fid, appid=appid, content=content)
        return True
    except Exception as e:
        logger.error("发评论失败（fid=%s）：%s", fid, e)
        return False


# ─── 主入口 ────────────────────────────────────────────────────────────────────

async def process(
    feeds: list[Any],
    context: dict | None = None,
    updated_feeds: list[dict] | None = None,
) -> None:
    if context is None:
        logger.warning("未收到 context，跳过 like_to_show 处理。")
        return

    owner_uin: int | None = context.get("uin")
    cookie_file: Path | None = context.get("cookie_file")
    data_dir: Path | None = context.get("data_dir")
    send_notice = context.get("send_notice")
    tg_send_message = context.get("tg_send_message")
    tg_updates: list[dict] = context.get("tg_updates", [])
    feed_store: dict = context.get("feed_store", {})

    plugin_cfg: dict = (context.get("plugins_config") or {}).get("like_to_show_plugin", {})
    cfg_default_method = plugin_cfg.get("default_edit_method", _DEFAULT_EDIT_METHOD)
    if cfg_default_method not in _VALID_METHODS:
        logger.warning(
            "config default_edit_method '%s' 无效，使用内置默认值 '%s'。",
            cfg_default_method, _DEFAULT_EDIT_METHOD,
        )
        cfg_default_method = _DEFAULT_EDIT_METHOD

    if not owner_uin or not cookie_file:
        logger.warning("context 中缺少 uin 或 cookie_file，跳过处理。")
        return

    state_file = (
        data_dir / _STATE_FILENAME
        if data_dir
        else Path("data") / _STATE_FILENAME
    )

    pending: list[dict] = _load_state(state_file)
    pending_fids = {item["fid"] for item in pending}
    now = time.time()

    # ── 步骤 1：扫描新说说，检测 /like_to_show 指令 ─────────────────────────
    for feed in feeds:
        if feed.userinfo.uin != owner_uin:
            continue

        content: str = (feed.summary.summary if feed.summary else "") or ""
        match = _CMD_RE.search(content)
        if not match:
            continue

        fid: str = feed.fid
        if fid in pending_fids:
            logger.debug("说说 %s 已在 like_to_show 队列中，跳过。", fid)
            continue

        like_threshold = int(match.group(1))
        edit_method = _parse_edit_method(match.group(2), default=cfg_default_method)

        # 发 TG 通知，请求用户 reply 来提供第二段内容
        notify_msg_id: int | None = None
        if tg_send_message:
            stripped_preview = _strip_directive(content)
            preview = stripped_preview[:80] + ("…" if len(stripped_preview) > 80 else "")
            notify_msg_id = await tg_send_message(
                f"📌 <b>like_to_show 已登记</b>\n"
                f"说说预览：{_html.escape(preview)}\n"
                f"点赞阈值：<b>{like_threshold}</b> | 方式：<b>{edit_method}</b>\n\n"
                f"请 <b>reply 本消息</b> 来提供点赞达标后要公布的第二段内容。\n"
                f"<i>（fid: {_html.escape(fid)}）</i>"
            )
        else:
            logger.warning(
                "Telegram 未配置，like_to_show 已登记但无法接收第二段内容（fid=%s）。", fid
            )

        pending.append({
            "fid": fid,
            "appid": int(feed.common.appid),
            "like_threshold": like_threshold,
            "edit_method": edit_method,
            "notify_msg_id": notify_msg_id,
            "first_content": content,
            "second_content": None,
            "registered_at": now,
            "warned_no_content": False,
        })
        pending_fids.add(fid)
        logger.info(
            "已登记 like_to_show 说说 %s，阈值=%d，edit_method=%s。",
            fid, like_threshold, edit_method,
        )

    # ── 步骤 2：扫描 TG 更新，匹配 reply 以收取第二段内容 ──────────────────
    if tg_updates:
        # 建立 notify_msg_id → pending item 的映射（仅针对尚未填写第二段内容的任务）
        waiting: dict[int, dict] = {
            item["notify_msg_id"]: item
            for item in pending
            if item.get("notify_msg_id") is not None and item["second_content"] is None
        }
        for update in tg_updates:
            msg = update.get("message") or {}
            reply_to = msg.get("reply_to_message") or {}
            replied_id: int | None = reply_to.get("message_id")
            if replied_id not in waiting:
                continue
            text: str = msg.get("text", "").strip()
            if not text:
                continue
            item = waiting[replied_id]
            item["second_content"] = text
            logger.info("已从 TG reply 收到 fid=%s 的第二段内容。", item["fid"])
            if send_notice:
                await send_notice(
                    f"✅ <b>like_to_show</b> 第二段内容已收到\n"
                    f"<i>fid: {_html.escape(item['fid'])}</i>\n"
                    f"内容预览：{_html.escape(text[:100])}{'…' if len(text) > 100 else ''}"
                )

    # ── 步骤 3：检查点赞阈值，触发执行 ─────────────────────────────────────
    still_pending: list[dict] = []
    executed_count = 0

    for item in pending:
        fid = item["fid"]
        threshold = item["like_threshold"]
        second_content: str | None = item.get("second_content")

        # 从 feed_store 获取当前点赞数（主流程在全量刷新时会更新此数据）
        feed_dict: dict = feed_store.get(fid) or {}
        current_likes: int = feed_dict.get("like_count", 0)

        if current_likes < threshold:
            still_pending.append(item)
            continue

        # 已达到阈值，但尚未收到第二段内容 → 警告一次后继续等待
        if not second_content:
            if not item.get("warned_no_content"):
                logger.warning(
                    "说说 %s 点赞已达 %d（阈值 %d），但尚未收到第二段内容，请尽快 reply TG 通知消息。",
                    fid, current_likes, threshold,
                )
                if send_notice:
                    await send_notice(
                        f"⚠️ <b>like_to_show</b> 点赞已达阈值但未收到第二段内容！\n"
                        f"点赞数：{current_likes} / 阈值：{threshold}\n"
                        f"<i>fid: {_html.escape(fid)}</i>\n\n"
                        f"请尽快 reply 之前的 TG 通知消息来提供第二段内容。"
                    )
                item["warned_no_content"] = True
            still_pending.append(item)
            continue

        # 已达到阈值 且 已收到第二段内容 → 执行
        first_content: str = item.get("first_content", "")
        edit_method: str = item.get("edit_method", "comment")

        if edit_method == "comment":
            # 在原说说评论区发布第二段内容，原说说保留
            ok = await _add_comment(
                fid=fid,
                appid=item["appid"],
                hostuin=owner_uin,
                content=second_content,
                uin=owner_uin,
                cookie_file=cookie_file,
            )
            if not ok:
                logger.error("发评论失败（fid=%s），稍后重试。", fid)
                still_pending.append(item)
                continue
            logger.info("like_to_show 已执行（comment）：fid=%s，点赞=%d。", fid, current_likes)
            if send_notice:
                await send_notice(
                    f"🎉 <b>like_to_show</b> 已触发！\n"
                    f"点赞数：{current_likes} / 阈值：{threshold}\n"
                    f"已在原说说评论区发布第二段内容。"
                )
        elif edit_method == "new":
            # 直接发布第二段内容为新说说，原说说保留
            published = await _publish_feed(
                content=second_content, uin=owner_uin, cookie_file=cookie_file
            )
            if not published:
                logger.error("发布新说说失败（fid=%s），稍后重试。", fid)
                still_pending.append(item)
                continue
            logger.info("like_to_show 已执行（new）：fid=%s，点赞=%d。", fid, current_likes)
            if send_notice:
                await send_notice(
                    f"🎉 <b>like_to_show</b> 已触发！\n"
                    f"点赞数：{current_likes} / 阈值：{threshold}\n"
                    f"已将第二段内容作为新说说发布（原说说保留）。"
                )
        else:
            # append / delete：需要先删除原说说再发新说说
            if edit_method == "append":
                stripped_first = _strip_directive(first_content)
                new_content = f"{stripped_first}\n\n{second_content}" if stripped_first else second_content
            else:  # delete
                new_content = second_content

            deleted = await _delete_feed(
                fid=fid, appid=item["appid"], uin=owner_uin, cookie_file=cookie_file
            )
            if not deleted:
                logger.error("删除原始说说 %s 失败，稍后重试。", fid)
                still_pending.append(item)
                continue

            published = await _publish_feed(
                content=new_content, uin=owner_uin, cookie_file=cookie_file
            )
            if not published:
                # 原说说已删但新说说发布失败 → 通知用户手动补发，不再重试
                logger.error(
                    "发布新说说失败（fid=%s），原说说已删除，内容：%s", fid, new_content[:100]
                )
                if send_notice:
                    await send_notice(
                        f"❌ <b>like_to_show</b> 发布新说说失败！\n"
                        f"原说说（fid: {_html.escape(fid)}）已被删除，请手动发布以下内容：\n\n"
                        f"{_html.escape(new_content)}"
                    )
            else:
                method_label = "已合并内容重新发布" if edit_method == "append" else "已删除原说说并发布新内容"
                logger.info(
                    "like_to_show 已执行（%s）：fid=%s，点赞=%d。",
                    edit_method, fid, current_likes,
                )
                if send_notice:
                    await send_notice(
                        f"🎉 <b>like_to_show</b> 已触发！\n"
                        f"点赞数：{current_likes} / 阈值：{threshold}\n"
                        f"{method_label}"
                    )

        executed_count += 1  # 无论成功与否，移出队列（失败时已通知，不重试）

    if executed_count:
        logger.info("本轮执行了 %d 条 like_to_show 任务。", executed_count)

    _save_state(state_file, still_pending)
