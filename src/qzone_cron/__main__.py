from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import click

from .config import load_config
from .fetcher import feed_to_dict, fetch_feeds, setup_login
from .llm import chat_completion
from .notifier import make_poll_updates, make_qr_sender, make_send_message, make_send_notice, poll_updates_raw, send_message_raw
from .plugin_loader import load_plugins, run_plugins
from .state import State

# 默认配置文件路径
DEFAULT_CONFIG = Path("config.toml")

# 默认插件目录（相对于当前工作目录）
DEFAULT_PLUGINS_DIR = Path("plugins")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


@click.group()
def cli() -> None:
    """qzone-cron — 定时抓取 QQ 空间说说并通过插件处理。"""


@cli.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    default=str(DEFAULT_CONFIG),
    show_default=True,
    type=click.Path(exists=True, path_type=Path),
    help="配置文件路径（TOML 格式）。",
)
@click.option("-v", "--verbose", is_flag=True, help="输出详细日志。")
def setup(config_path: Path, verbose: bool) -> None:
    """交互式二维码登录并保存 Cookie。

    首次使用或 Cookie 过期后执行此命令。
    """
    _setup_logging(verbose)
    asyncio.run(_setup(config_path))


async def _setup(config_path: Path) -> None:
    logger = logging.getLogger(__name__)
    config = load_config(config_path)
    qr_sender = make_qr_sender(config.telegram)

    logger.info("开始 QQ 空间登录流程（UIN: %d）…", config.auth.uin)
    if qr_sender:
        logger.info("登录二维码将通过 Telegram 发送。")
    try:
        await setup_login(config.auth.uin, config.storage.cookie_file, qr_sender=qr_sender)
    except Exception as e:
        logger.error("登录失败：%s", e)
        sys.exit(1)


def _resolve_tg_chat_id(config_chat_id: str, state_chat_id: str) -> str:
    """按优先级解析有效的 Telegram chat_id：setup-tg 绑定 → 配置文件 → 空字符串。"""
    return state_chat_id or config_chat_id


@cli.command("setup-tg")
@click.option(
    "-c",
    "--config",
    "config_path",
    default=str(DEFAULT_CONFIG),
    show_default=True,
    type=click.Path(exists=True, path_type=Path),
    help="配置文件路径（TOML 格式）。",
)
@click.option(
    "--timeout",
    "wait_timeout",
    default=300,
    show_default=True,
    type=int,
    help="等待用户回复的最长时间（秒）。",
)
@click.option("-v", "--verbose", is_flag=True, help="输出详细日志。")
def setup_tg(config_path: Path, wait_timeout: int, verbose: bool) -> None:
    """绑定 Telegram 会话：生成 PIN 码，等待用户发送 /qz <PIN> 完成授权。

    \b
    流程：
      1. 生成随机 6 位 PIN 码并打印到终端。
      2. 轮询 Telegram 消息，直到收到 /qz <PIN>。
      3. 将对应会话 ID 保存到状态文件，后续通知优先使用该 ID。
    """
    _setup_logging(verbose)
    asyncio.run(_setup_tg(config_path, wait_timeout))


async def _setup_tg(config_path: Path, wait_timeout: int) -> None:
    import secrets
    import time as _time

    logger = logging.getLogger(__name__)
    config = load_config(config_path)

    if not config.telegram.bot_token:
        logger.error("未配置 bot_token，请先在配置文件 [telegram] 中填写 bot_token。")
        sys.exit(1)

    bot_token = config.telegram.bot_token
    state = State(config.storage.state_file)

    # 生成随机 6 位数字 PIN
    pin = str(secrets.randbelow(900000) + 100000)

    click.echo(f"\n请在 Telegram 中向 Bot 发送以下消息完成绑定：\n\n    /qz {pin}\n")
    logger.info("等待 Telegram 验证（超时：%d 秒）…", wait_timeout)

    deadline = _time.monotonic() + wait_timeout
    offset = state.tg_update_offset
    long_poll_secs = 30

    while _time.monotonic() < deadline:
        remaining = deadline - _time.monotonic()
        poll_timeout = min(long_poll_secs, max(1, int(remaining)))
        try:
            updates, offset = await poll_updates_raw(bot_token, offset, poll_timeout)
        except Exception as e:
            logger.error("拉取 Telegram 消息失败：%s", e)
            await asyncio.sleep(3)
            continue

        for upd in updates:
            msg = upd.get("message") or upd.get("edited_message") or {}
            text: str = (msg.get("text") or "").strip()
            parts = text.split(maxsplit=1)
            if parts and parts[0].lower() in ("/qz", f"/qz@{bot_token.split(':')[1] if ':' in bot_token else ''}"):
                provided_pin = parts[1].strip() if len(parts) > 1 else ""
                chat = msg.get("chat", {})
                chat_id = str(chat.get("id", ""))
                sender = msg.get("from", {})
                sender_name = sender.get("username") or sender.get("first_name") or str(sender.get("id", "?"))

                if provided_pin == pin:
                    state.tg_chat_id = chat_id
                    state.tg_update_offset = offset
                    state.save()
                    reply = f"✅ 绑定成功！后续通知将发送至此会话（chat_id: <code>{chat_id}</code>）。"
                    try:
                        await send_message_raw(bot_token, chat_id, reply)
                    except Exception as e:
                        logger.warning("发送确认消息失败：%s", e)
                    logger.info("Telegram 会话绑定成功：chat_id=%s，来自用户 %s。", chat_id, sender_name)
                    click.echo(f"\n绑定成功！chat_id = {chat_id}")
                    return
                else:
                    logger.warning(
                        "用户 %s（chat_id=%s）发送了错误 PIN：%s", sender_name, chat_id, provided_pin
                    )
                    try:
                        await send_message_raw(bot_token, chat_id, "❌ PIN 码错误，请重试。")
                    except Exception:
                        pass

        # 更新 offset 但不保存（避免影响正常 run 流程）
        # offset 已由 poll_updates_raw 更新

    logger.error("等待超时（%d 秒），未收到有效验证消息。", wait_timeout)
    click.echo("\n绑定失败：超时。请重新运行 qzone-cron setup-tg。")
    sys.exit(1)


@cli.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    default=str(DEFAULT_CONFIG),
    show_default=True,
    type=click.Path(exists=True, path_type=Path),
    help="配置文件路径（TOML 格式）。",
)
@click.option(
    "-p",
    "--plugins-dir",
    "plugins_dir",
    default=str(DEFAULT_PLUGINS_DIR),
    show_default=True,
    type=click.Path(path_type=Path),
    help="插件目录路径。",
)
@click.option("-v", "--verbose", is_flag=True, help="输出详细日志。")
def run(config_path: Path, plugins_dir: Path, verbose: bool) -> None:
    """抓取 QQ 空间新说说并分发给各插件处理。

    通常由 crontab 定期调用，例如每 15 分钟执行一次：

    \b
    */15 * * * * cd /path/to/project && uv run qzone-cron run
    """
    _setup_logging(verbose)
    asyncio.run(_run(config_path, plugins_dir))


def _is_setup_in_progress(lock_file: Path) -> bool:
    """检查锁文件中记录的 setup 进程是否仍在运行。"""
    if not lock_file.exists():
        return False
    try:
        pid = int(lock_file.read_text().strip())
        os.kill(pid, 0)  # 信号 0 仅检查进程是否存在，不实际发送信号
        return True
    except (ValueError, OSError):
        lock_file.unlink(missing_ok=True)  # 进程已结束，清理过期锁
        return False


def _acquire_setup_lock(lock_file: Path) -> bool:
    """尝试获取 setup 锁，成功返回 True，已有进程在运行则返回 False。"""
    if _is_setup_in_progress(lock_file):
        return False
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(str(os.getpid()))
    return True


def _release_setup_lock(lock_file: Path) -> None:
    lock_file.unlink(missing_ok=True)


async def _run(config_path: Path, plugins_dir: Path) -> None:
    logger = logging.getLogger(__name__)

    config = load_config(config_path)
    state = State(config.storage.state_file)

    plugins = load_plugins(plugins_dir, config.plugins)
    if not plugins:
        logger.warning("未加载任何插件，退出。")
        return

    effective_chat_id = _resolve_tg_chat_id(config.telegram.chat_id, state.tg_chat_id)
    tg_cfg = config.telegram.model_copy(update={"chat_id": effective_chat_id})
    send_notice = make_send_notice(tg_cfg)
    poll_updates = make_poll_updates(tg_cfg)
    send_message = make_send_message(tg_cfg)

    plugin_context = {
        "uin": config.auth.uin,
        "cookie_file": config.storage.cookie_file,
        "data_dir": config.storage.data_path,
        "plugins_config": config.plugins,
        "global_openai_cfg": config.openai.as_dict(),
        "llm_chat": chat_completion,
        "send_notice": send_notice,
        "tg_send_message": send_message,
        "feed_store": state.feed_store,  # 与 state.feed_store 同一对象引用，后续更新自动可见
    }

    now = time.time()
    interval_secs = config.fetch.fetch_interval_minutes * 60
    refresh_interval_secs = config.fetch.stats_refresh_interval_minutes * 60
    elapsed = now - state.last_fetched_at

    should_fetch = elapsed >= interval_secs
    do_full_refresh = now - state.last_full_refresh_at >= refresh_interval_secs

    feeds: list = []
    updated_feeds: list[dict] = []

    if not should_fetch and not do_full_refresh:
        logger.info(
            "距上次抓取仅 %ds，未到抓取间隔（%dmin），跳过抓取，仅执行插件维护任务。",
            int(elapsed),
            config.fetch.fetch_interval_minutes,
        )
    else:
        # 两者都需要时合并为一次请求，取更大的时间窗口避免重复请求
        since_full = now - config.fetch.stats_refresh_window_hours * 3600
        if should_fetch and do_full_refresh:
            since_time = min(state.last_fetch_time, since_full) if state.last_fetch_time > 0 else since_full
            logger.info(
                "开始合并抓取（新说说 + 全量 stats 刷新，UIN: %d，时间窗口: %.1f 小时）…",
                config.auth.uin,
                config.fetch.stats_refresh_window_hours,
            )
        elif should_fetch:
            since_time = state.last_fetch_time
            logger.info(
                "开始抓取说说（UIN: %d，上次抓取时间: %s）…",
                config.auth.uin,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state.last_fetch_time))
                if state.last_fetch_time > 0
                else "从未",
            )
        else:  # only do_full_refresh
            since_time = since_full
            logger.info(
                "开始全量 stats 刷新（过去 %.1f 小时）…",
                config.fetch.stats_refresh_window_hours,
            )

        raw_feeds: list = []
        fetch_ok = False
        try:
            raw_feeds = await fetch_feeds(
                uin=config.auth.uin,
                cookie_file=config.storage.cookie_file,
                since_time=since_time,
                max_pages=config.fetch.max_pages,
            )
            fetch_ok = True
        except RuntimeError as e:
            if should_fetch:
                logger.error("%s", e)
                if config.auth.auto_relogin:
                    lock_file = config.storage.data_path / "setup.lock"
                    if not _acquire_setup_lock(lock_file):
                        pid = lock_file.read_text().strip() if lock_file.exists() else "?"
                        logger.info(
                            "登录失效，setup 进程（PID %s）已在运行，等待扫码中，本次跳过。", pid
                        )
                        return
                    logger.info("登录失效，自动触发重新登录（二维码将发送至 Telegram）…")
                    try:
                        qr_sender = make_qr_sender(config.telegram)
                        await setup_login(
                            config.auth.uin,
                            config.storage.cookie_file,
                            qr_sender=qr_sender,
                        )
                        logger.info("自动重新登录成功，下次 cron 将恢复正常抓取。")
                    except Exception as setup_err:
                        logger.error("自动重新登录失败：%s", setup_err)
                        if send_notice:
                            import html as _html
                            await send_notice(
                                f"\u26a0\ufe0f <b>qzone-cron 自动重新登录失败</b>\n"
                                f"{_html.escape(str(setup_err))}\n\n"
                                "请手动运行 <code>qzone-cron setup</code>。"
                            )
                    finally:
                        _release_setup_lock(lock_file)
                    return
                else:
                    if send_notice:
                        import html as _html
                        await send_notice(
                            f"\u26a0\ufe0f <b>qzone-cron 登录失效</b>\n"
                            f"{_html.escape(str(e))}\n\n"
                            "请运行 <code>qzone-cron setup</code> 重新扫码登录。"
                        )
                    sys.exit(1)
            else:
                logger.warning("全量 stats 刷新失败：%s，跳过。", e)
                state.last_full_refresh_at = now  # 避免失败后每次 cron 都重试
                state.save()

        if fetch_ok:
            store = state.feed_store
            known_fids = set(store.keys())  # 抓取前的快照，用于判断是否为新说说

            for feed in raw_feeds:
                fid = getattr(feed, "fid", None)
                if not fid:
                    continue
                refreshed = feed_to_dict(feed)
                if fid not in known_fids:
                    # 新说说（仅在 should_fetch 时加入通知列表）
                    if should_fetch:
                        feeds.append(feed)
                    store[fid] = refreshed
                else:
                    # 已知说说，检查内容或互动数据是否变化
                    if do_full_refresh:
                        old = store[fid]
                        if (
                            refreshed["like_count"] != old.get("like_count")
                            or refreshed["comment_count"] != old.get("comment_count")
                            or refreshed["content"] != old.get("content")
                            or refreshed["top_comments"] != old.get("top_comments")
                            or refreshed["image_urls"] != old.get("image_urls")
                        ):
                            updated_feeds.append(refreshed)
                    store[fid] = refreshed

            if should_fetch:
                state.last_fetched_at = now
                if feeds:
                    newest_time = max(f.common.time for f in feeds)
                    state.last_fetch_time = float(newest_time)
                    logger.info(
                        "抓取到 %d 条新说说，更新上次抓取时间为 %s。",
                        len(feeds),
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state.last_fetch_time)),
                    )
                else:
                    logger.info("没有新说说。")

            if do_full_refresh:
                expired = state.expire_feeds(config.fetch.feed_retention_hours)
                if expired:
                    logger.info("已清理 %d 条过期 feed。", expired)
                state.last_full_refresh_at = now
                logger.info(
                    "全量 stats 刷新完成，%d 条 feed 有数据更新。", len(updated_feeds)
                )

            state.save()

    # 拉取 Telegram 更新，供插件拪取用户指令（如 reply 消息）
    tg_updates: list[dict] = []
    if poll_updates:
        tg_updates, new_tg_offset = await poll_updates(state.tg_update_offset)
        if new_tg_offset != state.tg_update_offset:
            state.tg_update_offset = new_tg_offset
            state.save()
    plugin_context["tg_updates"] = tg_updates

    await run_plugins(plugins, feeds, updated_feeds=updated_feeds, context=plugin_context)


@cli.command("send-summary")
@click.option(
    "-c",
    "--config",
    "config_path",
    default=str(DEFAULT_CONFIG),
    show_default=True,
    type=click.Path(exists=True, path_type=Path),
    help="配置文件路径（TOML 格式）。",
)
@click.option(
    "-p",
    "--plugins-dir",
    "plugins_dir",
    default=str(DEFAULT_PLUGINS_DIR),
    show_default=True,
    type=click.Path(path_type=Path),
    help="插件目录路径。",
)
@click.option("-v", "--verbose", is_flag=True, help="输出详细日志。")
def send_summary(config_path: Path, plugins_dir: Path, verbose: bool) -> None:  # noqa: ARG001
    """强制立即生成并发送 daily_summary_plugin 的简报（测试用）。

    忽略 summary_hour 时间限制，使用队列中已有的说说生成摘要。
    若队列为空，可先运行 `qzone-cron run` 抓取一批说说，再执行此命令。
    """
    _setup_logging(verbose)
    asyncio.run(_send_summary(config_path, plugins_dir))


async def _send_summary(config_path: Path, plugins_dir: Path) -> None:
    logger = logging.getLogger(__name__)
    config = load_config(config_path)

    # 找到 daily_summary_plugin 模块
    import importlib.util

    plugin_file = plugins_dir / "daily_summary_plugin.py"
    if not plugin_file.exists():
        logger.error("找不到 daily_summary_plugin.py（路径：%s）", plugin_file)
        sys.exit(1)

    spec = importlib.util.spec_from_file_location("daily_summary_plugin", plugin_file)
    if spec is None or spec.loader is None:
        logger.error("无法加载 daily_summary_plugin.py")
        sys.exit(1)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    if not hasattr(mod, "force_send"):
        logger.error("daily_summary_plugin 未提供 force_send 函数")
        sys.exit(1)

    send_notice = make_send_notice(config.telegram)
    state = State(config.storage.state_file)

    effective_chat_id = _resolve_tg_chat_id(config.telegram.chat_id, state.tg_chat_id)
    tg_cfg = config.telegram.model_copy(update={"chat_id": effective_chat_id})
    send_notice = make_send_notice(tg_cfg)
    plugin_context = {
        "uin": config.auth.uin,
        "cookie_file": config.storage.cookie_file,
        "data_dir": config.storage.data_path,
        "plugins_config": config.plugins,
        "global_openai_cfg": config.openai.as_dict(),
        "llm_chat": chat_completion,
        "send_notice": send_notice,
        "feed_store": state.feed_store,
    }
    await mod.force_send(context=plugin_context)


if __name__ == "__main__":
    cli()
