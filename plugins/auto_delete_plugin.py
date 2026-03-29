"""
auto_delete_plugin — 自动删除说说插件

检测自己发布的说说内容中的 /autodelete 指令，到期后自动删除该说说。

支持以下写法：
    /autodelete          # 即时删除（下次检测周期执行）
    /autodelete 5min     # 5 分钟后删除
    /autodelete 5h       # 5 小时后删除

工作机制：
    每次运行时：
    1. 从状态文件读取待删除列表，将已到期的说说通过 API 删除；
    2. 扫描本次抓取到的新说说，提取 /autodelete 指令并登记（或立即删除）。
    3. 将未到期的任务写回状态文件，等待下次检测。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

PLUGIN_NAME = "auto_delete_plugin"
ENABLED = True

logger = logging.getLogger(PLUGIN_NAME)

# 匹配 /autodelete 或 /autodelete 5h 或 /autodelete 5min
_AUTODELETE_RE = re.compile(
    r"/autodelete(?:\s+(\d+)\s*(h|min|s))?(?:\s|$)", re.IGNORECASE
)


def _parse_delay(match: re.Match) -> float:
    """从正则匹配结果中解析延迟秒数；无时间参数则返回 0.0（即时删除）。"""
    amount_str, unit = match.group(1), match.group(2)
    if not amount_str:
        return 0.0
    amount = int(amount_str)
    unit = unit.lower()
    if unit == "h":
        return float(amount * 3600)
    if unit == "min":
        return float(amount * 60)
    if unit == "s":
        return float(amount)
    return 0.0


def _load_pending(state_file: Path) -> list[dict]:
    if not state_file.exists():
        return []
    try:
        with open(state_file) as f:
            return json.load(f)
    except Exception:
        logger.warning("读取待删除状态文件 %s 失败，视为空列表。", state_file)
        return []


def _save_pending(state_file: Path, pending: list[dict]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(pending, f, indent=2, ensure_ascii=False)


async def _delete_feed(fid: str, appid: int, uin: int, cookie_file: Path) -> bool:
    """调用 API 删除指定说说，返回是否成功。"""
    from aioqzone.api import QzoneH5API
    from aioqzone.api.login import ConstLoginMan
    from qqqr.utils.net import ClientAdapter, use_mobile_ua

    if not cookie_file.exists():
        logger.error("Cookie 文件 %s 不存在，无法删除说说。", cookie_file)
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


async def _register_autodelete(
    fid: str,
    appid: int,
    scheduled_at: float,
    match: re.Match,
    owner_uin: int,
    cookie_file: Path,
    still_pending: list[dict],
    pending_fids: set[str],
    now: float,
    content_preview: str = "",
) -> bool:
    """登记一条自动删除任务；若已到期则立即删除。返回 True 表示新增了延迟任务。"""
    delay = _parse_delay(match)
    delete_at = scheduled_at + delay

    if delete_at <= now:
        success = await _delete_feed(fid=fid, appid=appid, uin=owner_uin, cookie_file=cookie_file)
        if success:
            logger.info("说说 %s 已即时删除。", fid)
        else:
            still_pending.append({
                "fid": fid,
                "appid": appid,
                "delete_at": now,
                "scheduled_at": scheduled_at,
                "content_preview": content_preview,
            })
        return False
    else:
        logger.info(
            "说说 %s 将于 %s 被删除（延迟：%s）。",
            fid,
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(delete_at)),
            match.group(0).strip(),
        )
        still_pending.append({
            "fid": fid,
            "appid": appid,
            "delete_at": delete_at,
            "scheduled_at": scheduled_at,
            "content_preview": content_preview,
        })
        pending_fids.add(fid)
        return True


async def process(
    feeds: list[Any],
    context: dict | None = None,
    updated_feeds: list[dict] | None = None,
) -> None:
    if context is None:
        logger.warning("未收到 context，跳过自动删除处理。")
        return

    owner_uin: int | None = context.get("uin")
    cookie_file: Path | None = context.get("cookie_file")
    data_dir: Path | None = context.get("data_dir")

    if not owner_uin or not cookie_file:
        logger.warning("context 中缺少 uin 或 cookie_file，跳过自动删除处理。")
        return

    state_file = (
        data_dir / "auto_delete_state.json"
        if data_dir
        else Path("data/auto_delete_state.json")
    )

    pending: list[dict] = _load_pending(state_file)
    now = time.time()

    # ── 步骤1：处理到期的待删除说说 ──────────────────────────────────────
    still_pending: list[dict] = []
    deleted_count = 0
    for item in pending:
        if item["delete_at"] <= now:
            success = await _delete_feed(
                fid=item["fid"],
                appid=item["appid"],
                uin=owner_uin,
                cookie_file=cookie_file,
            )
            if success:
                logger.info(
                    "已删除说说 %s（计划删除时间：%s）",
                    item["fid"],
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item["delete_at"])),
                )
                deleted_count += 1
            else:
                # 删除失败，保留到下次重试
                still_pending.append(item)
        else:
            still_pending.append(item)

    if deleted_count:
        logger.info("本轮共删除 %d 条到期说说。", deleted_count)

    # 已在待删除队列中的 fid 集合，避免重复登记
    pending_fids = {item["fid"] for item in still_pending}

    # ── 步骤2：扫描新说说，提取 /autodelete 指令 ─────────────────────────
    new_scheduled_count = 0
    for feed in feeds:
        if feed.userinfo.uin != owner_uin:
            continue  # 只处理自己发布的说说

        content: str = (feed.summary.summary if feed.summary else "") or ""
        match = _AUTODELETE_RE.search(content)
        if not match:
            continue

        fid: str = feed.fid
        appid: int = feed.common.appid
        scheduled_at = float(feed.common.time)

        if fid in pending_fids:
            logger.debug("说说 %s 已在待删除队列中，跳过。", fid)
            continue

        scheduled = await _register_autodelete(
            fid=fid,
            appid=appid,
            scheduled_at=scheduled_at,
            match=match,
            owner_uin=owner_uin,
            cookie_file=cookie_file,
            still_pending=still_pending,
            pending_fids=pending_fids,
            now=now,
            content_preview=content[:100],
        )
        if scheduled:
            new_scheduled_count += 1

    if new_scheduled_count:
        logger.info("新登记 %d 条待删除说说。", new_scheduled_count)

    # ── 步骤3：处理全量刷新中内容有变化的自己发布的说说 ──────────────────
    if updated_feeds:
        for feed_dict in updated_feeds:
            if feed_dict.get("uin") != owner_uin:
                continue

            fid_upd: str | None = feed_dict.get("fid")
            if not fid_upd:
                continue

            content_upd: str = feed_dict.get("content", "") or ""
            match_upd = _AUTODELETE_RE.search(content_upd)

            if fid_upd in pending_fids:
                # 说说已在待删除队列中，检查指令是否变化
                existing = next((item for item in still_pending if item["fid"] == fid_upd), None)
                if existing is None:
                    continue

                if not match_upd:
                    # /autodelete 指令已被移除，取消计划删除
                    still_pending = [item for item in still_pending if item["fid"] != fid_upd]
                    pending_fids.discard(fid_upd)
                    logger.info("说说 %s 已移除 /autodelete 指令，取消计划删除。", fid_upd)
                    continue

                new_delay = _parse_delay(match_upd)
                new_delete_at = existing["scheduled_at"] + new_delay
                if abs(new_delete_at - existing["delete_at"]) > 1:
                    logger.info(
                        "说说 %s 的自动删除时间已更新：%s → %s。",
                        fid_upd,
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(existing["delete_at"])),
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(new_delete_at)),
                    )
                    existing["delete_at"] = new_delete_at
                    existing["content_preview"] = content_upd[:100]
            else:
                # 说说不在队列中，检查是否新增了 /autodelete 指令
                if not match_upd:
                    continue

                appid_upd = feed_dict.get("appid")
                if not appid_upd:
                    logger.debug("说说 %s 缺少 appid，无法登记自动删除。", fid_upd)
                    continue

                await _register_autodelete(
                    fid=fid_upd,
                    appid=appid_upd,
                    scheduled_at=float(feed_dict.get("time", now)),
                    match=match_upd,
                    owner_uin=owner_uin,
                    cookie_file=cookie_file,
                    still_pending=still_pending,
                    pending_fids=pending_fids,
                    now=now,
                    content_preview=content_upd[:100],
                )

    _save_pending(state_file, still_pending)
