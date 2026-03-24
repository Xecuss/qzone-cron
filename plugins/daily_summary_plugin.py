"""
daily_summary_plugin — 空间简报插件

汇总好友说说（不含自己发布的），在每个配置的时间点通过 OpenAI 兼容接口生成摘要，
并通过 Telegram Bot 发送简报。

时间触发机制：
    每次 cron 运行时记录本次检测时间。若上一次检测时间早于某个推送时间点，
    而本次检测时间晚于该时间点，则说明刚刚越过了这个时间点，立即触发推送。
    发送失败时不更新检测时间，下次运行会自动重试。

配置示例 (config.toml):

    [plugins.daily_summary_plugin]
    enabled = true
    summary_times = ["08:00", "20:00"]  # 每天推送时间点列表（本地时间，HH:MM）
    vip_uins = [12345678]               # 特别关注的 QQ 号列表（摘要中优先展示）

    [plugins.daily_summary_plugin.openai]
    api_key = "sk-..."
    base_url = "https://api.openai.com/v1"  # 兼容各类 OpenAI 接口
    model = "gpt-4o-mini"
    # system_prompt = "..."   # 可选，覆盖默认系统提示词

    [plugins.daily_summary_plugin.telegram]
    bot_token = "123456:ABC-..."
    chat_id = "-1001234567890"
"""
from __future__ import annotations

import html
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

PLUGIN_NAME = "daily_summary_plugin"
ENABLED = True

logger = logging.getLogger(PLUGIN_NAME)

_DEFAULT_SYSTEM_PROMPT = (
    "你是一位 QQ 空间简报助手。"
    "你将收到用户好友今日在 QQ 空间发布的说说原始数据，请生成一份简洁易读的每日简报。\n\n"
    "要求：\n"
    "1. 用中文撰写，风格轻松活泼；\n"
    "2. 开头用 1-2 句话概括今日动态整体氛围；\n"
    "3. 若数据中有【特别关注】标记的好友动态，在简报中单独分组并重点介绍；"
    "若无【特别关注】条目则不要提及该分组，不得自行脑补；\n"
    "4. 对其余好友动态进行整体归纳，选取有趣或有代表性的内容介绍，无需逐条列举；\n"
    "5. 若某条说说是转发，介绍时需同时说明转发了谁的内容；\n"
    "6. 末尾用一句温馨的话收尾。"
)


# ─── State helpers ────────────────────────────────────────────────────────────


def _load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {"last_check_time": None, "pending_feeds": []}
    try:
        with open(state_file, encoding="utf-8") as f:
            data = json.load(f)
        # 兼容旧版本状态（last_summary_date → last_check_time）
        if "last_summary_date" in data and "last_check_time" not in data:
            data["last_check_time"] = None
            del data["last_summary_date"]
        data.setdefault("last_check_time", None)
        data.setdefault("pending_feeds", [])
        return data
    except Exception:
        return {"last_check_time": None, "pending_feeds": []}


def _save_state(state_file: Path, state: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ─── Feed ingestion ───────────────────────────────────────────────────────────


def _feed_to_record(feed: Any, vip_uins: set[int]) -> dict:
    uin: int = feed.userinfo.uin
    nickname: str = feed.userinfo.nickname or str(uin)
    post_time: int = int(feed.common.time)
    content: str = (feed.summary.summary if feed.summary else "") or ""
    has_images = bool(feed.pic and getattr(feed.pic, "picdata", None))
    has_video = bool(feed.video)
    fid: str = getattr(feed, "fid", f"{uin}_{post_time}")

    # 点赞数 / 评论数
    like_count: int = feed.like.likeNum if feed.like else 0
    comment_count: int = feed.comment.num if feed.comment else 0

    # 前几条评论内容（最多取3条）
    top_comments: list[dict] = []
    if feed.comment and feed.comment.comments:
        for c in feed.comment.comments[:3]:
            top_comments.append({
                "user": c.user.nickname or str(c.user.uin),
                "content": c.content,
            })

    # 转发原文
    original: dict | None = None
    if feed.original is not None:
        from aioqzone.model.api.feed import FeedOriginal
        if isinstance(feed.original, FeedOriginal):
            orig_content = (
                feed.original.summary.summary if feed.original.summary else ""
            ) or ""
            original = {
                "deleted": False,
                "uin": feed.original.userinfo.uin,
                "nickname": feed.original.userinfo.nickname or str(feed.original.userinfo.uin),
                "content": orig_content,
                "has_images": bool(
                    feed.original.pic and getattr(feed.original.pic, "picdata", None)
                ),
                "has_video": bool(feed.original.video),
            }
        else:
            # Share 对象：原文已删除或无法获取
            original = {"deleted": True}

    return {
        "fid": fid,
        "uin": uin,
        "nickname": nickname,
        "time": post_time,
        "content": content,
        "has_images": has_images,
        "has_video": has_video,
        "is_vip": uin in vip_uins,
        "like_count": like_count,
        "comment_count": comment_count,
        "top_comments": top_comments,
        "original": original,
    }


# ─── Prompt building ──────────────────────────────────────────────────────────


def _build_prompt(pending_feeds: list[dict]) -> str:
    if not pending_feeds:
        return "今天没有好友发布任何说说。"

    has_vip = any(item.get("is_vip") for item in pending_feeds)
    lines: list[str] = ["以下是今日好友的 QQ 空间动态（按时间顺序排列）：\n"]
    if has_vip:
        lines.append("（其中标有【特别关注】的条目请在简报中重点介绍）\n")

    for item in sorted(pending_feeds, key=lambda x: x["time"]):
        ts = datetime.fromtimestamp(item["time"]).strftime("%H:%M")
        vip_tag = "【特别关注】" if item.get("is_vip") else ""
        content = item["content"].strip() or "（无文字内容）"

        # 媒体标注
        media_parts: list[str] = []
        if item.get("has_images"):
            media_parts.append("含图片")
        if item.get("has_video"):
            media_parts.append("含视频")
        media_str = f" [{','.join(media_parts)}]" if media_parts else ""

        # 互动数据
        like_count = item.get("like_count", 0)
        comment_count = item.get("comment_count", 0)
        interaction_parts: list[str] = []
        if like_count:
            interaction_parts.append(f"👍{like_count}")
        if comment_count:
            interaction_parts.append(f"💬{comment_count}")
        interaction_str = f" ({' '.join(interaction_parts)})" if interaction_parts else ""

        # 转发原文
        original = item.get("original")
        forward_str = ""
        if original:
            if original.get("deleted"):
                forward_str = "\n    └ 转发了已删除/不可见的原文"
            else:
                orig_content = original.get("content", "").strip() or "（无文字内容）"
                orig_media: list[str] = []
                if original.get("has_images"):
                    orig_media.append("含图片")
                if original.get("has_video"):
                    orig_media.append("含视频")
                orig_media_str = f" [{','.join(orig_media)}]" if orig_media else ""
                forward_str = (
                    f"\n    └ 转发自 {original['nickname']}({original['uin']}): "
                    f"{orig_content}{orig_media_str}"
                )

        # 代表性评论（最多2条）
        comments_str = ""
        top_comments = item.get("top_comments", [])
        if top_comments:
            comment_lines = [
                f"\n    💬 {c['user']}: {c['content']}" for c in top_comments[:2]
            ]
            comments_str = "".join(comment_lines)

        lines.append(
            f"- {ts} {vip_tag}{item['nickname']}({item['uin']}): "
            f"{content}{media_str}{interaction_str}"
            f"{forward_str}{comments_str}"
        )
    return "\n".join(lines)


# ─── Time trigger ────────────────────────────────────────────────────────────


def _check_trigger(state: dict, summary_times: list[str], now: datetime) -> bool:
    """检查自上次运行以来是否越过了某个推送时间点。

    算法：若 last_check_time < trigger_dt <= now，则判定刚刚越过该时间点，返回 True。
    首次运行（last_check_time 为 None）时不触发，只记录基准时间。
    """
    last_check_str: str | None = state.get("last_check_time")
    if not last_check_str:
        return False
    try:
        last_check_dt = datetime.fromisoformat(last_check_str)
    except ValueError:
        return False

    for time_str in summary_times:
        try:
            hour, minute = map(int, time_str.split(":"))
        except (ValueError, AttributeError):
            logger.warning("无效的推送时间格式：%s，已跳过。", time_str)
            continue
        trigger_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if last_check_dt < trigger_dt <= now:
            logger.info("已越过推送时间点 %s，触发简报生成。", time_str)
            return True

    return False


# ─── OpenAI compatible API call ───────────────────────────────────────────────


async def _call_openai(cfg: dict, user_prompt: str) -> str:
    import httpx

    api_key: str = cfg.get("api_key", "")
    base_url: str = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    model: str = cfg.get("model", "gpt-4o-mini")
    system_prompt: str = cfg.get("system_prompt", "") or _DEFAULT_SYSTEM_PROMPT

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{base_url}/chat/completions", headers=headers, json=payload
        )
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


# ─── Telegram ────────────────────────────────────────────────────────────────


async def _send_telegram(cfg: dict, text: str) -> None:
    import httpx

    bot_token: str = cfg.get("bot_token", "")
    chat_id: str = str(cfg.get("chat_id", ""))

    if not bot_token or not chat_id:
        logger.warning("Telegram 配置不完整（bot_token 或 chat_id 缺失），跳过发送。")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    # Telegram 单条消息最长 4096 字符
    _MAX = 4096
    chunks = [text[i : i + _MAX] for i in range(0, len(text), _MAX)]

    async with httpx.AsyncClient(timeout=30.0) as client:
        for chunk in chunks:
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
            }
            resp = await client.post(url, json=payload)
            resp.raise_for_status()


# ─── Shared send logic ────────────────────────────────────────────────────────


async def _do_send(
    state: dict,
    state_file: Path,
    openai_cfg: dict,
    telegram_cfg: dict,
    vip_uins: set[int],
    summary_times: list[str],
    *,
    force: bool = False,
) -> None:
    """生成摘要并发送到 Telegram，成功后清空队列并更新 last_check_time。

    force=True 时跳过时间检查（用于测试）。
    发送失败时不更新 last_check_time，下次运行会自动重试。
    """
    now = datetime.now()

    if not force:
        triggered = _check_trigger(state, summary_times, now)
        if not triggered:
            state["last_check_time"] = now.isoformat()
            _save_state(state_file, state)
            return

    if not state["pending_feeds"]:
        logger.info("待摘要队列为空，%s摘要生成。", "强制跳过" if force else "跳过")
        if not force:
            state["last_check_time"] = now.isoformat()
        _save_state(state_file, state)
        return

    pending = state["pending_feeds"]
    logger.info("开始生成空间简报，共 %d 条动态…", len(pending))
    user_prompt = _build_prompt(pending)

    try:
        summary = await _call_openai(openai_cfg, user_prompt)
    except Exception as e:
        logger.error("调用 OpenAI API 失败：%s，队列保留，下次推送时间点自动重试。", e)
        # 不更新 last_check_time，下次运行仍会触发
        _save_state(state_file, state)
        return

    date_str = now.strftime("%Y年%m月%d日 %H:%M")
    vip_note = ""
    if vip_uins:
        vip_names = "、".join(str(u) for u in sorted(vip_uins))
        vip_note = f"\n<i>特别关注：{vip_names}</i>"

    message = (
        f"<b>📰 {html.escape(date_str)} QQ 空间简报</b>{vip_note}\n\n"
        f"{html.escape(summary)}"
    )

    try:
        await _send_telegram(telegram_cfg, message)
        logger.info("空间简报已成功发送到 Telegram。")
    except Exception as e:
        logger.error("发送 Telegram 消息失败：%s，队列保留，下次推送时间点自动重试。", e)
        # 不更新 last_check_time，下次运行仍会触发
        _save_state(state_file, state)
        return

    state["pending_feeds"] = []
    state["last_check_time"] = now.isoformat()
    logger.info("待摘要队列已清空。")
    _save_state(state_file, state)


def _resolve_context(context: dict) -> tuple[dict, int | None, Path | None, dict, dict, list[str], set[int]]:
    plugins_config: dict = context.get("plugins_config", {})
    cfg: dict = plugins_config.get("daily_summary_plugin", {})
    owner_uin: int | None = context.get("uin")
    data_dir: Path | None = context.get("data_dir")
    openai_cfg: dict = cfg.get("openai", {})
    telegram_cfg: dict = cfg.get("telegram", {})
    summary_times: list[str] = cfg.get("summary_times", ["08:00"])
    vip_uins: set[int] = set(cfg.get("vip_uins", []))
    return cfg, owner_uin, data_dir, openai_cfg, telegram_cfg, summary_times, vip_uins


def _check_required(openai_cfg: dict, telegram_cfg: dict) -> bool:
    if not openai_cfg.get("api_key"):
        logger.warning("daily_summary_plugin: 未配置 openai.api_key，跳过。")
        return False
    if not telegram_cfg.get("bot_token") or not telegram_cfg.get("chat_id"):
        logger.warning("daily_summary_plugin: 未配置 telegram.bot_token 或 chat_id，跳过。")
        return False
    return True


# ─── Main process ─────────────────────────────────────────────────────────────


async def process(feeds: list[Any], context: dict | None = None) -> None:
    if context is None:
        return

    cfg, owner_uin, data_dir, openai_cfg, telegram_cfg, summary_times, vip_uins = (
        _resolve_context(context)
    )
    if not owner_uin:
        return
    if not _check_required(openai_cfg, telegram_cfg):
        return

    state_file = (
        data_dir / "daily_summary_state.json"
        if data_dir
        else Path("data/daily_summary_state.json")
    )
    state = _load_state(state_file)

    # ── 步骤 1：将新抓取的非自己说说写入待摘要队列 ────────────────────────────
    existing_fids: set[str] = {item["fid"] for item in state["pending_feeds"]}
    added = 0
    for feed in feeds:
        if feed.userinfo.uin == owner_uin:
            continue
        record = _feed_to_record(feed, vip_uins)
        if record["fid"] in existing_fids:
            continue
        state["pending_feeds"].append(record)
        existing_fids.add(record["fid"])
        added += 1

    if added:
        logger.info(
            "已记录 %d 条新动态，待摘要队列共 %d 条。",
            added,
            len(state["pending_feeds"]),
        )

    # ── 步骤 2：按时间决定是否发送 ───────────────────────────────────────────
    await _do_send(
        state, state_file, openai_cfg, telegram_cfg, vip_uins, summary_times
    )


# ─── Force send (for testing) ─────────────────────────────────────────────────


async def force_send(context: dict | None = None) -> None:
    """跳过时间限制，立即用队列中已有的说说生成简报并发送（测试用）。

    调用方式：
        uv run qzone-cron send-summary
    """
    if context is None:
        logger.error("force_send: context 为 None，无法执行。")
        return

    cfg, owner_uin, data_dir, openai_cfg, telegram_cfg, summary_times, vip_uins = (
        _resolve_context(context)
    )
    if not owner_uin:
        logger.error("force_send: 未提供 owner_uin。")
        return
    if not _check_required(openai_cfg, telegram_cfg):
        return

    state_file = (
        data_dir / "daily_summary_state.json"
        if data_dir
        else Path("data/daily_summary_state.json")
    )
    state = _load_state(state_file)

    logger.info(
        "force_send: 队列中有 %d 条动态，立即生成简报…", len(state["pending_feeds"])
    )
    await _do_send(
        state, state_file, openai_cfg, telegram_cfg, vip_uins, summary_times, force=True
    )
