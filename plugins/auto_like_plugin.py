"""
auto_like_plugin — 自动点赞插件

模拟真实用户的点赞行为：
1. 通过大模型过滤说说（排除情绪低落、自我攻击类内容）
2. 将应点赞的说说放入队列
3. 等待随机时延后进入激活状态，按批次点赞（每批 likes_per_cycle 条，条间随机间隔）
4. 队列清空后退出激活状态，预计算下一次激活时间
5. 激活时间落在禁止时段时自动顺延至允许时段

配置示例 (config.toml)：

    [plugins.auto_like_plugin]
    enabled = true
    likes_per_cycle = 3           # 每次激活时点赞的条数
    like_interval_min = 5.0       # 两次点赞之间的最短间隔（秒）
    like_interval_max = 10.0      # 两次点赞之间的最长间隔（秒）
    activation_delay_min = 30     # 激活延迟最小值（分钟）
    activation_delay_max = 180    # 激活延迟最大值（分钟）
    forbidden_hours = [0,1,2,3,4,5,6]  # 禁止激活的小时（本地时间 0-23）

    [plugins.auto_like_plugin.openai]
    api_key = "sk-..."
    base_url = "https://api.openai.com/v1"
    model = "gpt-4o-mini"
    # system_prompt = "..."   # 可选，覆盖默认判断 prompt
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import Any

PLUGIN_NAME = "auto_like_plugin"
ENABLED = True

logger = logging.getLogger(PLUGIN_NAME)

_DEFAULT_SYSTEM_PROMPT = (
    "你是一个模拟 QQ 空间用户点赞行为的助手。\n"
    "根据说说内容，判断用户是否应该点赞。\n\n"
    "点赞规则：\n"
    "- 应该点赞：日常生活更新、好消息、出行/美食/照片/运动、有趣或有意义的分享等正常内容\n"
    "- 不应该点赞：自我攻击、情绪极度低落、抑郁倾向、自我否定、发泄强烈负面情绪的说说\n\n"
    "请返回一个 JSON 对象，格式为：{\"fids\": [\"fid1\", \"fid2\", ...]}，"
    "只包含应该点赞的说说 fid。若全部不应点赞，返回 {\"fids\": []}。\n\n"
    "注意：直接输出裸 JSON，不要使用 markdown 代码块（不要加 ```json 或 ``` 标记）。\n"
    "示例输出：{\"fids\": [\"abc123\", \"def456\"]}"
)


# ─── State helpers ────────────────────────────────────────────────────────────


def _load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {"pending_items": [], "next_activation_time": None}
    try:
        with open(state_file, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("pending_items", [])
        data.setdefault("next_activation_time", None)
        return data
    except Exception:
        logger.warning("读取状态文件 %s 失败，视为空状态。", state_file)
        return {"pending_items": [], "next_activation_time": None}


def _save_state(state_file: Path, state: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ─── Time helpers ─────────────────────────────────────────────────────────────


def _compute_next_activation(cfg: dict) -> float:
    """计算下一次激活时间（Unix 时间戳），跳过禁止时段。"""
    delay_min: float = float(cfg.get("activation_delay_min", 30))   # 分钟
    delay_max: float = float(cfg.get("activation_delay_max", 180))  # 分钟
    forbidden_hours: list[int] = cfg.get("forbidden_hours", list(range(0, 7)))

    delay_sec = random.uniform(delay_min * 60, delay_max * 60)
    target = time.time() + delay_sec

    # 若目标时间落在禁止时段，逐小时顺延
    while True:
        local_hour = time.localtime(target).tm_hour
        if local_hour not in forbidden_hours:
            break
        target += 3600

    readable = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(target))
    logger.info("下一次激活时间已预计算：%s", readable)
    return target


# ─── LLM filtering ────────────────────────────────────────────────────────────


async def _should_like_feeds(feeds: list[Any], openai_cfg: dict, llm_chat: Any) -> set[str]:
    """通过大模型判断哪些说说应该点赞，返回应点赞的 fid 集合。
    未配置 LLM 时默认全部点赞。"""
    if not openai_cfg.get("api_key") or llm_chat is None:
        logger.debug("未配置 OpenAI API Key，默认对所有说说点赞。")
        return {fid for f in feeds if (fid := getattr(f, "fid", None)) is not None}

    system_prompt: str = openai_cfg.get("system_prompt", _DEFAULT_SYSTEM_PROMPT)

    # 构建用户消息：每条说说一行
    lines = ["请判断以下 QQ 空间说说，哪些应该点赞（返回 JSON 对象 {\"fids\": [...]}）：\n"]
    for feed in feeds:
        fid = getattr(feed, "fid", None)
        if not fid:
            continue
        nickname = (feed.userinfo.nickname or str(feed.userinfo.uin)) if feed.userinfo else "未知"
        content = (feed.summary.summary if feed.summary else "") or "（无文字内容）"
        lines.append(f'- fid: "{fid}", 用户: {nickname}, 内容: {content[:200]}')

    user_message = "\n".join(lines)

    logger.debug(
        "[LLM 输入] system_prompt:\n%s\n\nuser_message:\n%s",
        system_prompt,
        user_message,
    )

    extra: dict = {"max_tokens": 500}
    # response_format 仅部分模型支持（如 gpt-4o 系列），可通过配置显式开启
    if openai_cfg.get("json_mode", False):
        extra["response_format"] = {"type": "json_object"}

    try:
        raw = await llm_chat(
            openai_cfg,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            timeout=30.0,
            **extra,
        )
        logger.debug("[LLM 输出] %s", raw)
        # 兼容部分模型在回复中包裹 markdown 代码块的情况
        stripped = raw.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[-1]
            stripped = stripped.rsplit("```", 1)[0].strip()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as e:
            logger.warning("LLM 返回内容无法解析为 JSON（%s），原始内容：%s", e, raw)
            return set()

        # 兼容返回格式：{"fids": [...]} 或其他常见键名
        if isinstance(parsed, list):
            return set(parsed)
        if isinstance(parsed, dict):
            for key in ("fids", "like", "to_like", "result", "ids"):
                if key in parsed and isinstance(parsed[key], list):
                    return set(str(x) for x in parsed[key])
        logger.warning("LLM 返回格式不符合预期：%s", raw[:300])
        return set()

    except Exception as e:
        logger.warning("LLM 过滤请求失败（%s），跳过本批说说的过滤，默认全部点赞。", e)
        # 降级为全部点赞，避免因 LLM 故障导致说说永远不被处理
        return {fid for f in feeds if (fid := getattr(f, "fid", None)) is not None}


# ─── Like API ─────────────────────────────────────────────────────────────────


async def _like_feed(item: dict, owner_uin: int, cookie_file: Path) -> bool:
    """调用 aioqzone API 点赞指定说说，返回是否成功。"""
    from aioqzone.api import QzoneH5API
    from aioqzone.api.login import ConstLoginMan
    from qqqr.utils.net import ClientAdapter, use_mobile_ua

    if not cookie_file.exists():
        logger.error("Cookie 文件 %s 不存在，无法点赞。", cookie_file)
        return False

    with open(cookie_file) as f:
        cookies = json.load(f)

    try:
        async with ClientAdapter() as client:
            use_mobile_ua(client)
            login_man = ConstLoginMan(uin=owner_uin, cookie=cookies)
            api = QzoneH5API(client, login_man)
            await api.internal_dolike_app(
                appid=item["appid"],
                unikey=item["unikey"],
                curkey=item["curkey"],
                like=True,
            )
        logger.info(
            "点赞成功：fid=%s，用户=%s(%s)",
            item["fid"],
            item.get("nickname", "?"),
            item.get("uin", "?"),
        )
        return True
    except Exception as e:
        logger.error("点赞失败：fid=%s，错误：%s", item["fid"], e)
        return False


# ─── Main process ─────────────────────────────────────────────────────────────


async def process(feeds: list[Any], context: dict | None = None) -> None:
    if context is None:
        logger.warning("未收到 context，跳过自动点赞处理。")
        return

    owner_uin: int | None = context.get("uin")
    cookie_file: Path | None = context.get("cookie_file")
    data_dir: Path | None = context.get("data_dir")
    plugin_cfg: dict = (context.get("plugins_config") or {}).get("auto_like_plugin", {})
    global_openai_cfg: dict = context.get("global_openai_cfg") or {}
    openai_cfg: dict = {**global_openai_cfg, **plugin_cfg.get("openai", {})}
    llm_chat: Any = context.get("llm_chat")

    if not owner_uin or not cookie_file:
        logger.warning("context 中缺少 uin 或 cookie_file，跳过自动点赞处理。")
        return

    state_file = (
        data_dir / "auto_like_state.json"
        if data_dir
        else Path("data/auto_like_state.json")
    )

    likes_per_cycle: int = int(plugin_cfg.get("likes_per_cycle", 3))
    like_interval_min: float = float(plugin_cfg.get("like_interval_min", 5.0))
    like_interval_max: float = float(plugin_cfg.get("like_interval_max", 10.0))

    state = _load_state(state_file)

    # ── 步骤1：过滤新说说，将应点赞的说说入队 ───────────────────────────────
    # 只处理好友说说（非自己发布的）
    friend_feeds = [f for f in feeds if f.userinfo.uin != owner_uin]

    if friend_feeds:
        should_like_fids = await _should_like_feeds(friend_feeds, openai_cfg, llm_chat)
        existing_fids = {item["fid"] for item in state["pending_items"]}

        new_items: list[dict] = []
        for feed in friend_feeds:
            fid = getattr(feed, "fid", None)
            if not fid or fid not in should_like_fids or fid in existing_fids:
                continue
            new_items.append({
                "fid": fid,
                "uin": feed.userinfo.uin,
                "nickname": feed.userinfo.nickname or str(feed.userinfo.uin),
                "appid": feed.common.appid,
                "unikey": feed.topicId,
                "curkey": str(feed.common.curkey),
            })
            existing_fids.add(fid)

        if new_items:
            # 新说说插到队列头部，保证从最新的开始点赞（类似人看到新内容先点赞）
            state["pending_items"] = new_items + state["pending_items"]
            logger.info("新增 %d 条待点赞说说，队列共 %d 条。", len(new_items), len(state["pending_items"]))

    # ── 步骤2：若队列非空且尚未预计算激活时间，则计算激活时间 ───────────────
    if state["pending_items"] and state["next_activation_time"] is None:
        state["next_activation_time"] = _compute_next_activation(plugin_cfg)

    # ── 步骤3：检查是否已到达激活时间 ──────────────────────────────────────
    now = time.time()
    logger.info(
        "[诊断] 队列长度=%d，next_activation_time=%s，now=%s，差值=%.1f 分钟",
        len(state["pending_items"]),
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state["next_activation_time"]))
        if state["next_activation_time"] else "None",
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        (state["next_activation_time"] - now) / 60 if state["next_activation_time"] else float("nan"),
    )
    if (
        state["pending_items"]
        and state["next_activation_time"] is not None
        and now >= state["next_activation_time"]
    ):
        queue_len = len(state["pending_items"])
        logger.info("进入激活状态，队列中有 %d 条待点赞说说。", queue_len)

        batch = state["pending_items"][:likes_per_cycle]
        state["pending_items"] = state["pending_items"][likes_per_cycle:]

        liked_count = 0
        for i, item in enumerate(batch):
            success = await _like_feed(item, owner_uin, cookie_file)
            if success:
                liked_count += 1
            # 每两条点赞之间随机等待（最后一条不等待）
            if i < len(batch) - 1:
                await asyncio.sleep(random.uniform(like_interval_min, like_interval_max))

        logger.info("本轮点赞完成，成功 %d / %d 条。", liked_count, len(batch))

        # ── 步骤4：队列清空后退出激活状态，预计算下一次激活时间 ──────────────
        if not state["pending_items"]:
            logger.info("点赞队列已清空，退出激活状态。")
            state["next_activation_time"] = _compute_next_activation(plugin_cfg)
        else:
            logger.info(
                "队列仍剩余 %d 条，等待下次 cron 继续处理。", len(state["pending_items"])
            )

    elif state["pending_items"] and state["next_activation_time"] is not None:
        remaining_min = (state["next_activation_time"] - now) / 60
        logger.debug(
            "待点赞队列有 %d 条，距激活还有 %.1f 分钟。",
            len(state["pending_items"]),
            remaining_min,
        )

    _save_state(state_file, state)
