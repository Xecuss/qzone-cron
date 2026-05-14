"""
like_to_show_plugin — 达到条件后公布第二段内容

本插件提供两条嵌入指令：

──────────────────────────────────────────
指令一：/like_to_show <like_count> [edit_method]

- like_count  ：点赞阈值，达到后触发
- edit_method ：触发后的发布方式（默认 comment，可通过配置文件修改默认值）
  - append ：删除原说说，将去除指令的原始内容与第二段内容合并后发布新说说
  - delete ：删除原说说，仅将第二段内容作为新说说单独发布
  - new    ：发布第二段内容为新说说，原说说保留
  - comment：在原说说的评论区发布第二段内容（原说说保留）

──────────────────────────────────────────
指令二：/delay_to_show <delay_time> [edit_method]

- delay_time  ：延迟时间，从说说被登记时起计算，到期后触发
  支持格式：30m / 2h / 1d（分钟 / 小时 / 天），纯数字默认为小时
- edit_method ：与 like_to_show 完全相同

──────────────────────────────────────────
两条指令的公共工作流程：
1. 检测到含指令的自己说说
   → 登记到状态文件
   → 发送 TG 通知，提示用户 reply 该消息来提供第二段内容
2. 每次 cron 扫描 tg_updates（由主流程统一拉取，存于 context）
   → 匹配 reply_to_message_id 找到对应任务，写入 second_content
3. 每次 cron 检查触发条件：
   [like_to_show]  从 feed_store 读取当前点赞数 ≥ 阈值
   [delay_to_show] 当前时间 ≥ 登记时间 + 延迟秒数
   → 条件满足 且 second_content 已填写 → 执行
   → 条件满足 但 second_content 缺失   → 发送 TG 警告（仅警告一次）
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

_VALID_METHODS = ("append", "delete", "comment", "new")
_DEFAULT_EDIT_METHOD = "comment"
_MAX_RETRIES = 5

# 匹配 /like_to_show 10 或 /like_to_show 10 append 等写法
_CMD_RE = re.compile(
    r"/like_to_show\s+(\d+)(?:\s+(append|delete|comment|new)\b)?",
    re.IGNORECASE,
)

_DELAY_STATE_FILENAME = "delay_to_show_state.json"

# 匹配 /delay_to_show 30m 或 /delay_to_show 2h append 等写法
# delay_time 支持：30m / 30min / 2h / 2hr / 2hours / 1d / 1day，纯数字默认小时
_DELAY_CMD_RE = re.compile(
    r"/delay_to_show\s+([\d.]+\s*(?:m(?:in)?|h(?:r|ours?)?|d(?:ays?)?)?)(?:\s+(append|delete|comment|new)\b)?",
    re.IGNORECASE,
)


# ─── 工具函数 ──────────────────────────────────────────────────────────────────

def _parse_edit_method(value: str | None, default: str = _DEFAULT_EDIT_METHOD) -> str:
    """解析 edit_method 参数，不合法或缺省时返回 default。"""
    if not value:
        return default
    v = value.lower()
    return v if v in _VALID_METHODS else default


def _parse_delay_seconds(value: str) -> int | None:
    """解析延迟时间字符串，返回秒数。不合法时返回 None。
    支持：30m / 30min / 2h / 2hr / 2hours / 1d / 1day，纯数字默认为小时。
    """
    m = re.fullmatch(r"([\d.]+)\s*(m(?:in)?|h(?:r|ours?)?|d(?:ays?)?)?", value.strip(), re.IGNORECASE)
    if not m:
        return None
    try:
        amount = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "h").lower()
    if unit.startswith("m"):
        return max(1, int(amount * 60))
    elif unit.startswith("h"):
        return max(1, int(amount * 3600))
    elif unit.startswith("d"):
        return max(1, int(amount * 86400))
    return max(1, int(amount * 3600))


def _format_delay(seconds: int) -> str:
    """将秒数格式化为人类可读的字符串，用于通知消息。"""
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    elif seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h} 小时{f' {m} 分钟' if m else ''}"
    else:
        d = seconds // 86400
        h = (seconds % 86400) // 3600
        return f"{d} 天{f' {h} 小时' if h else ''}"


def _strip_directive(content: str) -> str:
    """从原始内容中移除 /like_to_show 或 /delay_to_show 指令，返回清理后的文本。"""
    content = _CMD_RE.sub("", content)
    content = _DELAY_CMD_RE.sub("", content)
    return content.strip()


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


async def _execute_item(
    item: dict,
    owner_uin: int,
    cookie_file: Path,
    send_notice,
    plugin_label: str,
    trigger_desc: str,
) -> bool:
    """执行已触发的 item，按 edit_method 发布第二段内容。
    返回 True 表示完成（移出队列），False 表示操作失败需下轮重试。
    """
    fid: str = item["fid"]
    first_content: str = item.get("first_content", "")
    second_content: str = item["second_content"]
    edit_method: str = item.get("edit_method", "comment")

    if edit_method == "comment":
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
            return False
        logger.info("%s 已执行（comment）：fid=%s。", plugin_label, fid)
        if send_notice:
            await send_notice(
                f"🎉 <b>{plugin_label}</b> 已触发！\n"
                f"{trigger_desc}\n"
                f"已在原说说评论区发布第二段内容。"
            )
    elif edit_method == "new":
        published = await _publish_feed(content=second_content, uin=owner_uin, cookie_file=cookie_file)
        if not published:
            logger.error("发布新说说失败（fid=%s），稍后重试。", fid)
            return False
        logger.info("%s 已执行（new）：fid=%s。", plugin_label, fid)
        if send_notice:
            await send_notice(
                f"🎉 <b>{plugin_label}</b> 已触发！\n"
                f"{trigger_desc}\n"
                f"已将第二段内容作为新说说发布（原说说保留）。"
            )
    else:
        # append / delete：先删除原说说再发新说说
        if edit_method == "append":
            stripped_first = _strip_directive(first_content)
            new_content = f"{stripped_first}\n\n{second_content}" if stripped_first else second_content
        else:  # delete
            new_content = second_content

        deleted = await _delete_feed(fid=fid, appid=item["appid"], uin=owner_uin, cookie_file=cookie_file)
        if not deleted:
            logger.error("删除原始说说 %s 失败，稍后重试。", fid)
            return False

        published = await _publish_feed(content=new_content, uin=owner_uin, cookie_file=cookie_file)
        if not published:
            # 原说说已删但新说说发布失败 → 通知用户手动补发，不再重试
            logger.error("发布新说说失败（fid=%s），原说说已删除，内容：%s", fid, new_content[:100])
            if send_notice:
                await send_notice(
                    f"❌ <b>{plugin_label}</b> 发布新说说失败！\n"
                    f"原说说（fid: {_html.escape(fid)}）已被删除，请手动发布以下内容：\n\n"
                    f"{_html.escape(new_content)}"
                )
        else:
            method_label = "已合并内容重新发布" if edit_method == "append" else "已删除原说说并发布新内容"
            logger.info("%s 已执行（%s）：fid=%s。", plugin_label, edit_method, fid)
            if send_notice:
                await send_notice(
                    f"🎉 <b>{plugin_label}</b> 已触发！\n"
                    f"{trigger_desc}\n"
                    f"{method_label}"
                )

    return True


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
    delay_state_file = (
        data_dir / _DELAY_STATE_FILENAME
        if data_dir
        else Path("data") / _DELAY_STATE_FILENAME
    )

    pending: list[dict] = _load_state(state_file)
    pending_fids = {item["fid"] for item in pending}

    delay_pending: list[dict] = _load_state(delay_state_file)
    delay_pending_fids = {item["fid"] for item in delay_pending}

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
            "retry_count": 0,
        })
        pending_fids.add(fid)
        logger.info(
            "已登记 like_to_show 说说 %s，阈值=%d，edit_method=%s。",
            fid, like_threshold, edit_method,
        )

    # ── 步骤 1b：扫描新说说，检测 /delay_to_show 指令 ────────────────────────
    for feed in feeds:
        if feed.userinfo.uin != owner_uin:
            continue

        content: str = (feed.summary.summary if feed.summary else "") or ""
        match = _DELAY_CMD_RE.search(content)
        if not match:
            continue

        fid: str = feed.fid
        if fid in delay_pending_fids:
            logger.debug("说说 %s 已在 delay_to_show 队列中，跳过。", fid)
            continue

        delay_raw: str = match.group(1).strip()
        delay_seconds = _parse_delay_seconds(delay_raw)
        if delay_seconds is None:
            logger.warning("说说 %s 中 delay_to_show 的延迟时间 '%s' 无法解析，跳过。", fid, delay_raw)
            continue

        edit_method = _parse_edit_method(match.group(2), default=cfg_default_method)
        trigger_at = now + delay_seconds

        notify_msg_id: int | None = None
        if tg_send_message:
            stripped_preview = _strip_directive(content)
            preview = stripped_preview[:80] + ("…" if len(stripped_preview) > 80 else "")
            notify_msg_id = await tg_send_message(
                f"⏰ <b>delay_to_show 已登记</b>\n"
                f"说说预览：{_html.escape(preview)}\n"
                f"延迟时间：<b>{_format_delay(delay_seconds)}</b> | 方式：<b>{edit_method}</b>\n\n"
                f"请 <b>reply 本消息</b> 来提供延迟到期后要公布的第二段内容。\n"
                f"<i>（fid: {_html.escape(fid)}）</i>"
            )
        else:
            logger.warning(
                "Telegram 未配置，delay_to_show 已登记但无法接收第二段内容（fid=%s）。", fid
            )

        delay_pending.append({
            "fid": fid,
            "appid": int(feed.common.appid),
            "delay_seconds": delay_seconds,
            "trigger_at": trigger_at,
            "edit_method": edit_method,
            "notify_msg_id": notify_msg_id,
            "first_content": content,
            "second_content": None,
            "registered_at": now,
            "warned_no_content": False,
            "retry_count": 0,
        })
        delay_pending_fids.add(fid)
        logger.info(
            "已登记 delay_to_show 说说 %s，延迟=%s，edit_method=%s。",
            fid, _format_delay(delay_seconds), edit_method,
        )

    # ── 步骤 2：扫描 TG 更新，匹配 reply 以收取第二段内容 ──────────────────
    if tg_updates:
        # 建立 notify_msg_id → pending item 的映射（like_to_show 与 delay_to_show 合并）
        waiting: dict[int, dict] = {
            item["notify_msg_id"]: item
            for items in (pending, delay_pending)
            for item in items
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
        trigger_desc = f"点赞数：{current_likes} / 阈值：{threshold}"
        done = await _execute_item(item, owner_uin, cookie_file, send_notice, "like_to_show", trigger_desc)
        if not done:
            item["retry_count"] = item.get("retry_count", 0) + 1
            if item["retry_count"] >= _MAX_RETRIES:
                logger.error(
                    "like_to_show 说说 %s 执行失败已达 %d 次，放弃重试。",
                    fid, _MAX_RETRIES,
                )
                if send_notice:
                    await send_notice(
                        f"❌ <b>like_to_show</b> 执行失败 {_MAX_RETRIES} 次，已放弃！\n"
                        f"<i>fid: {_html.escape(fid)}</i>\n"
                        f"请检查说说是否已被删除或 API 异常。"
                    )
            else:
                still_pending.append(item)
            continue

        executed_count += 1

    if executed_count:
        logger.info("本轮执行了 %d 条 like_to_show 任务。", executed_count)

    _save_state(state_file, still_pending)

    # ── 步骤 3b：检查 delay_to_show 延迟到期，触发执行 ──────────────────────
    still_delay_pending: list[dict] = []
    delay_executed_count = 0

    for item in delay_pending:
        fid = item["fid"]
        trigger_at: float = item["trigger_at"]
        second_content: str | None = item.get("second_content")
        delay_seconds = item.get("delay_seconds", 0)

        if now < trigger_at:
            still_delay_pending.append(item)
            continue

        # 已到期，但尚未收到第二段内容 → 警告一次后继续等待
        if not second_content:
            if not item.get("warned_no_content"):
                logger.warning(
                    "说说 %s delay_to_show 已到期（延迟 %s），但尚未收到第二段内容，请尽快 reply TG 通知消息。",
                    fid, _format_delay(delay_seconds),
                )
                if send_notice:
                    await send_notice(
                        f"⚠️ <b>delay_to_show</b> 延迟已到期但未收到第二段内容！\n"
                        f"延迟：{_format_delay(delay_seconds)}\n"
                        f"<i>fid: {_html.escape(fid)}</i>\n\n"
                        f"请尽快 reply 之前的 TG 通知消息来提供第二段内容。"
                    )
                item["warned_no_content"] = True
            still_delay_pending.append(item)
            continue

        # 已到期 且 已收到第二段内容 → 执行
        trigger_desc = f"延迟：{_format_delay(delay_seconds)}"
        done = await _execute_item(item, owner_uin, cookie_file, send_notice, "delay_to_show", trigger_desc)
        if not done:
            item["retry_count"] = item.get("retry_count", 0) + 1
            if item["retry_count"] >= _MAX_RETRIES:
                logger.error(
                    "delay_to_show 说说 %s 执行失败已达 %d 次，放弃重试。",
                    fid, _MAX_RETRIES,
                )
                if send_notice:
                    await send_notice(
                        f"❌ <b>delay_to_show</b> 执行失败 {_MAX_RETRIES} 次，已放弃！\n"
                        f"<i>fid: {_html.escape(fid)}</i>\n"
                        f"请检查说说是否已被删除或 API 异常。"
                    )
            else:
                still_delay_pending.append(item)
            continue

        delay_executed_count += 1

    if delay_executed_count:
        logger.info("本轮执行了 %d 条 delay_to_show 任务。", delay_executed_count)

    _save_state(delay_state_file, still_delay_pending)
