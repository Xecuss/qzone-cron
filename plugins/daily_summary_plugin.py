"""
daily_summary_plugin — 空间简报插件

汇总好友说说（不含自己发布的），在每个配置的时间点通过 OpenAI 兼容接口生成摘要，
并通过全局 send_notice（Telegram）发送简报。

时间触发机制：
    每次 cron 运行时记录本次检测时间。若上一次检测时间早于某个推送时间点，
    而本次检测时间晚于该时间点，则说明刚刚越过了这个时间点，立即触发推送。
    发送失败时不更新检测时间，下次运行会自动重试。

配置示例 (config.toml):

    [telegram]
    bot_token = "123456:ABC-..."
    chat_id = "-1001234567890"

    [plugins.daily_summary_plugin]
    enabled = true
    summary_times = ["08:00", "20:00"]  # 每天推送时间点列表（本地时间，HH:MM）
    vip_uins = [12345678]               # 特别关注的 QQ 号列表（摘要中优先展示）

    [plugins.daily_summary_plugin.openai]
    api_key = "sk-..."
    base_url = "https://api.openai.com/v1"  # 兼容各类 OpenAI 接口
    model = "gpt-4o-mini"
    # system_prompt = "..."   # 可选，覆盖默认系统提示词
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
    "你将收到用户好友近期在 QQ 空间发布的说说原始数据，请生成一份简洁易读的空间简报。\n\n"
    "要求：\n"
    "1. 用中文撰写，风格轻松活泼；\n"
    "2. 开头用 1-2 句话概括本期动态整体氛围；\n"
    "3. 若数据中有【特别关注】标记的好友动态，在简报中单独分组并重点介绍；"
    "若无【特别关注】条目则不要提及该分组，不得自行脑补；\n"
    "4. 对其余好友动态进行整体归纳，选取有趣或有代表性的内容介绍，无需逐条列举；\n"
    "5. 若某条说说是转发，介绍时需同时说明转发了谁的内容；\n"
    "6. 末尾用一句温馨的话收尾。"
)


# ─── State helpers ────────────────────────────────────────────────────────────


def _load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {"last_check_time": None, "pending_feed_ids": [], "image_desc_cache": {}}
    try:
        with open(state_file, encoding="utf-8") as f:
            data = json.load(f)
        # 兼容旧版本状态（last_summary_date → last_check_time）
        if "last_summary_date" in data and "last_check_time" not in data:
            data["last_check_time"] = None
            del data["last_summary_date"]
        data.setdefault("last_check_time", None)
        # 迁移旧版 pending_feeds 格式 → pending_feed_ids + image_desc_cache
        if "pending_feeds" in data and "pending_feed_ids" not in data:
            old_feeds = data.pop("pending_feeds", [])
            data["pending_feed_ids"] = [f["fid"] for f in old_feeds if "fid" in f]
            cache: dict = {}
            legacy: dict = {}
            for f in old_feeds:
                if "fid" not in f:
                    continue
                fid = f["fid"]
                if f.get("image_description"):
                    cache[fid] = f["image_description"]
                orig = f.get("original")
                if orig and not orig.get("deleted") and orig.get("image_description"):
                    cache[f"{fid}:orig"] = orig["image_description"]
                legacy[fid] = f
            data["image_desc_cache"] = cache
            data["_legacy_feed_data"] = legacy
            logger.info(
                "已迁移旧版 pending_feeds 至新格式，%d 条待处理动态。",
                len(data["pending_feed_ids"]),
            )
        data.setdefault("pending_feed_ids", [])
        data.setdefault("image_desc_cache", {})
        return data
    except Exception:
        return {"last_check_time": None, "pending_feed_ids": [], "image_desc_cache": {}}


def _save_state(state_file: Path, state: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ─── Feed ingestion ───────────────────────────────────────────────────────────


async def _describe_images(
    openai_cfg: dict, image_urls: list[str], context_text: str, llm_chat: Any
) -> str | None:
    """调用视觉模型对图片内容进行预描述，失败时静默返回 None。

    可通过 openai.describe_images = false 关闭；
    可通过 openai.vision_model 指定不同于摘要生成的模型。
    """
    if not image_urls or not openai_cfg.get("api_key") or llm_chat is None:
        return None
    if not openai_cfg.get("describe_images", True):
        return None

    vision_model: str = openai_cfg.get("vision_model") or openai_cfg.get("model", "gpt-4o-mini")
    vision_cfg = {**openai_cfg, "model": vision_model}

    prompt_text = "请用1-2句话简洁描述这些图片的内容"
    if context_text.strip():
        prompt_text += f"，可结合说说文字「{context_text.strip()}」理解语境"
    prompt_text += "。"

    content_parts: list[dict] = [{"type": "text", "text": prompt_text}]
    for url in image_urls[:4]:  # 最多处理 4 张，避免 token 消耗过大
        content_parts.append({"type": "image_url", "image_url": {"url": url}})

    try:
        return await llm_chat(
            vision_cfg,
            [{"role": "user", "content": content_parts}],
            timeout=30.0,
            max_tokens=150,
        )
    except Exception as e:
        logger.warning("图片描述生成失败（%s），将仅记录「含图片」。", e)
        return None


async def _compute_image_descs(
    fid: str, feed: Any, openai_cfg: dict, cache: dict, llm_chat: Any
) -> None:
    """计算 feed（及其转发原文）的图片描述并写入 cache，已缓存则跳过。"""
    # 主帖图片
    if fid not in cache:
        has_images = bool(feed.pic and getattr(feed.pic, "picdata", None))
        if has_images:
            content = (feed.summary.summary if feed.summary else "") or ""
            image_urls: list[str] = []
            for pic in feed.pic.picdata:
                try:
                    image_urls.append(str(pic.photourl.largest.url))
                except Exception:
                    pass
            if image_urls:
                desc = await _describe_images(openai_cfg, image_urls, content, llm_chat)
                if desc:
                    cache[fid] = desc

    # 转发原文图片
    orig_key = f"{fid}:orig"
    if orig_key not in cache and feed.original is not None:
        from aioqzone.model.api.feed import FeedOriginal
        if isinstance(feed.original, FeedOriginal):
            orig_has_images = bool(
                feed.original.pic and getattr(feed.original.pic, "picdata", None)
            )
            if orig_has_images:
                orig_content = (
                    feed.original.summary.summary if feed.original.summary else ""
                ) or ""
                orig_urls: list[str] = []
                for pic in feed.original.pic.picdata:
                    try:
                        orig_urls.append(str(pic.photourl.largest.url))
                    except Exception:
                        pass
                if orig_urls:
                    desc = await _describe_images(openai_cfg, orig_urls, orig_content, llm_chat)
                    if desc:
                        cache[orig_key] = desc


def _build_record_from_store(
    fid: str, feed_data: dict, image_desc_cache: dict, vip_uins: set[int]
) -> dict:
    """从 feed_store 数据 + image_desc_cache 构建供 _build_prompt 使用的 record。"""
    record = dict(feed_data)
    record["is_vip"] = feed_data["uin"] in vip_uins
    record["image_description"] = image_desc_cache.get(fid)
    if record.get("original") and not record["original"].get("deleted"):
        orig = dict(record["original"])
        orig["image_description"] = image_desc_cache.get(f"{fid}:orig")
        record["original"] = orig
    return record


# ─── Prompt building ──────────────────────────────────────────────────────────


def _build_prompt(pending_feeds: list[dict], now: datetime) -> str:
    if not pending_feeds:
        return "当前没有好友发布任何说说。"

    now_str = now.strftime("%Y年%m月%d日 %H:%M")
    has_vip = any(item.get("is_vip") for item in pending_feeds)
    lines: list[str] = [f"当前时间：{now_str}\n", "以下是本期收集到的好友 QQ 空间动态（按时间顺序排列）：\n"]
    if has_vip:
        lines.append("（其中标有【特别关注】的条目请在简报中重点介绍）\n")

    for item in sorted(pending_feeds, key=lambda x: x["time"]):
        ts = datetime.fromtimestamp(item["time"]).strftime("%H:%M")
        vip_tag = "【特别关注】" if item.get("is_vip") else ""
        content = item["content"].strip()

        # 分享卡片信息
        share_str = ""
        share_title = item.get("share_title", "").strip()
        share_summary_text = item.get("share_summary", "").strip()
        if share_title or share_summary_text:
            share_parts = []
            if share_title:
                share_parts.append(share_title)
            if share_summary_text:
                share_parts.append(share_summary_text)
            share_str = f"\n    🔗 分享：{' — '.join(share_parts)}"

        if not content:
            content = "（无文字内容）"

        # 媒体标注
        media_parts: list[str] = []
        if item.get("has_images") and not item.get("image_description"):
            media_parts.append("含图片")
        if item.get("has_video"):
            media_parts.append("含视频")
        media_str = f" [{','.join(media_parts)}]" if media_parts else ""

        # 图片描述（有描述时替代「含图片」标注）
        image_desc_str = ""
        if item.get("image_description"):
            image_desc_str = f"\n    📷 {item['image_description']}"

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
                if original.get("has_images") and not original.get("image_description"):
                    orig_media.append("含图片")
                if original.get("has_video"):
                    orig_media.append("含视频")
                orig_media_str = f" [{','.join(orig_media)}]" if orig_media else ""
                orig_image_desc_str = ""
                if original.get("image_description"):
                    orig_image_desc_str = f"\n        📷 {original['image_description']}"
                forward_str = (
                    f"\n    └ 转发自 {original['nickname']}({original['uin']}): "
                    f"{orig_content}{orig_media_str}"
                    f"{orig_image_desc_str}"
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
            f"{image_desc_str}"
            f"{share_str}"
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


async def _call_openai(cfg: dict, user_prompt: str, llm_chat: Any) -> str:
    system_prompt: str = cfg.get("system_prompt", "") or _DEFAULT_SYSTEM_PROMPT
    return await llm_chat(
        cfg,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        timeout=60.0,
        temperature=0.7,
    )


# ─── Shared send logic ────────────────────────────────────────────────────────


async def _do_send(
    state: dict,
    state_file: Path,
    feed_store: dict,
    openai_cfg: dict,
    vip_uins: set[int],
    summary_times: list[str],
    send_notice: Any,
    llm_chat: Any,
    *,
    force: bool = False,
) -> None:
    """生成摘要并通过全局 send_notice 发送，成功后清空队列并更新 last_check_time。

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

    pending_ids: list[str] = state["pending_feed_ids"]
    if not pending_ids:
        logger.info("待摘要队列为空，%s摘要生成。", "强制跳过" if force else "跳过")
        if not force:
            state["last_check_time"] = now.isoformat()
        _save_state(state_file, state)
        return

    image_desc_cache: dict = state["image_desc_cache"]
    legacy: dict = state.get("_legacy_feed_data", {})

    # 从 feed_store 中获取完整 feed 数据（迁移期间允许回退到 legacy）
    pending_records: list[dict] = []
    missing = 0
    for fid in pending_ids:
        feed_data = feed_store.get(fid) or legacy.get(fid)
        if not feed_data:
            missing += 1
            continue
        pending_records.append(_build_record_from_store(fid, feed_data, image_desc_cache, vip_uins))
    if missing:
        logger.warning("有 %d 条动态 fid 在 feed_store 中未找到，已跳过。", missing)

    if not pending_records:
        logger.info("所有队列条目均无法解析，%s摘要生成。", "强制跳过" if force else "跳过")
        if not force:
            state["last_check_time"] = now.isoformat()
        _save_state(state_file, state)
        return

    logger.info("开始生成空间简报，共 %d 条动态…", len(pending_records))
    user_prompt = _build_prompt(pending_records, now)

    try:
        summary = await _call_openai(openai_cfg, user_prompt, llm_chat)
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
        await send_notice(message)
        logger.info("空间简报已成功发送到 Telegram。")
    except Exception as e:
        logger.error("发送 Telegram 消息失败：%s，队列保留，下次推送时间点自动重试。", e)
        # 不更新 last_check_time，下次运行仍会触发
        _save_state(state_file, state)
        return

    # 清空队列，清理本期 image_desc_cache，移除迁移遗留数据
    for fid in pending_ids:
        image_desc_cache.pop(fid, None)
        image_desc_cache.pop(f"{fid}:orig", None)
    state["pending_feed_ids"] = []
    state.pop("_legacy_feed_data", None)
    state["last_check_time"] = now.isoformat()
    logger.info("待摘要队列已清空。")
    _save_state(state_file, state)


def _resolve_context(context: dict) -> tuple[dict, int | None, Path | None, dict, list[str], set[int]]:
    plugins_config: dict = context.get("plugins_config", {})
    cfg: dict = plugins_config.get("daily_summary_plugin", {})
    owner_uin: int | None = context.get("uin")
    data_dir: Path | None = context.get("data_dir")
    global_openai_cfg: dict = context.get("global_openai_cfg") or {}
    openai_cfg: dict = {**global_openai_cfg, **cfg.get("openai", {})}
    summary_times: list[str] = cfg.get("summary_times", ["08:00"])
    vip_uins: set[int] = set(cfg.get("vip_uins", []))
    return cfg, owner_uin, data_dir, openai_cfg, summary_times, vip_uins


def _check_required(openai_cfg: dict, send_notice: Any) -> bool:
    if not openai_cfg.get("api_key"):
        logger.warning("daily_summary_plugin: 未配置 openai.api_key，跳过。")
        return False
    if send_notice is None:
        logger.warning("daily_summary_plugin: 未配置全局 [telegram]，无法发送简报，跳过。")
        return False
    return True


# ─── Main process ─────────────────────────────────────────────────────────────


async def process(
    feeds: list[Any],
    updated_feeds: list[dict] | None = None,
    context: dict | None = None,
) -> None:
    if context is None:
        return

    send_notice = context.get("send_notice")
    llm_chat: Any = context.get("llm_chat")
    cfg, owner_uin, data_dir, openai_cfg, summary_times, vip_uins = (
        _resolve_context(context)
    )
    if not owner_uin:
        return
    if not _check_required(openai_cfg, send_notice):
        return

    state_file = (
        data_dir / "daily_summary_state.json"
        if data_dir
        else Path("data/daily_summary_state.json")
    )
    state = _load_state(state_file)
    feed_store: dict = context.get("feed_store", {})

    # ── 步骤 1：将新抓取的非自己说说的 fid 写入待摘要队列 ────────────────────
    existing_fids: set[str] = set(state["pending_feed_ids"])
    added = 0
    for feed in feeds:
        if feed.userinfo.uin == owner_uin:
            continue
        fid: str = getattr(feed, "fid", f"{feed.userinfo.uin}_{feed.common.time}")
        if fid in existing_fids:
            continue
        # 趁有原始对象时计算图片描述（已缓存则自动跳过）
        await _compute_image_descs(fid, feed, openai_cfg, state["image_desc_cache"], llm_chat)
        state["pending_feed_ids"].append(fid)
        existing_fids.add(fid)
        added += 1

    if added:
        logger.info(
            "已记录 %d 条新动态，待摘要队列共 %d 条。",
            added,
            len(state["pending_feed_ids"]),
        )

    if updated_feeds:
        logger.info(
            "收到 %d 条 stats 更新动态（将在生成简报时使用最新数据）。",
            len(updated_feeds),
        )

    # ── 步骤 2：按时间决定是否发送 ───────────────────────────────────────────
    await _do_send(
        state, state_file, feed_store, openai_cfg, vip_uins, summary_times, send_notice, llm_chat
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

    send_notice = context.get("send_notice")
    llm_chat: Any = context.get("llm_chat")
    cfg, owner_uin, data_dir, openai_cfg, summary_times, vip_uins = (
        _resolve_context(context)
    )
    if not owner_uin:
        logger.error("force_send: 未提供 owner_uin。")
        return
    if not _check_required(openai_cfg, send_notice):
        return

    state_file = (
        data_dir / "daily_summary_state.json"
        if data_dir
        else Path("data/daily_summary_state.json")
    )
    state = _load_state(state_file)
    feed_store: dict = context.get("feed_store", {})

    logger.info(
        "force_send: 队列中有 %d 条动态，立即生成简报…", len(state["pending_feed_ids"])
    )
    await _do_send(
        state, state_file, feed_store, openai_cfg, vip_uins, summary_times, send_notice, llm_chat, force=True
    )
