from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import click

from .config import load_config
from .fetcher import feed_to_dict, feed_to_rich_dict, fetch_feeds, fetch_self_feeds, iter_self_feeds, setup_login
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


@cli.command("dump")
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
    "-o",
    "--output",
    "output_path",
    default=None,
    type=click.Path(path_type=Path),
    help="输出文件路径（JSON Lines 格式）。默认为 <data_dir>/my_feeds.jsonl。",
)
@click.option(
    "--since",
    "since_time",
    default=0.0,
    type=float,
    help="仅抓取晚于此 Unix 时间戳的说说。0 表示抓取全部（默认）。",
)
@click.option(
    "--max-pages",
    "max_pages",
    default=0,
    type=int,
    show_default=True,
    help="最大翻页数。0 表示不限制（抓取全部）。",
)
@click.option(
    "--continue", "resume",
    is_flag=True,
    help="断点续传：读取已有输出文件中的 fid，跳过已写条目，将新条目追加到文件末尾。",
)
@click.option("-v", "--verbose", is_flag=True, help="输出详细日志。")
def dump(config_path: Path, output_path: Path | None, since_time: float, max_pages: int, resume: bool, verbose: bool) -> None:
    """抓取自己所有的说说并保存到文件，用于离线分析。

    输出格式为 JSON Lines（每行一条说说），包含说说的全量信息：
    正文、图片、点赞数、全部评论（含回复）、访问次数等。
    每吸取一页就实时写入文件，中断后可用 --continue 继续。

    \b
    示例：
      uv run qzone-cron dump                       # 抓取全部，保存到 data/my_feeds.jsonl
      uv run qzone-cron dump -o out.jsonl          # 指定输出路径
      uv run qzone-cron dump --max-pages 5         # 仅抓取前 5 页
      uv run qzone-cron dump --continue            # 断点续传
    """
    _setup_logging(verbose)
    asyncio.run(_dump(config_path, output_path, since_time, max_pages, resume))


async def _dump(config_path: Path, output_path: Path | None, since_time: float, max_pages: int, resume: bool) -> None:
    import json as _json

    logger = logging.getLogger(__name__)
    config = load_config(config_path)

    if output_path is None:
        output_path = config.storage.data_path / "my_feeds.jsonl"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 断点续传：加载已有的 fid 集合（仅 resume 模式）
    resume_fids: set[str] = set()  # 来自已有文件，用于 --continue 时跳过
    seen_fids: set[str] = set()    # 本次运行内去重，避免 API 跨页返回重复条目
    if resume and output_path.exists():
        with open(output_path, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line:
                    try:
                        _fid = _json.loads(_line).get("fid")
                        if _fid:
                            resume_fids.add(_fid)
                    except Exception:
                        pass
        click.echo(f"已加载 {len(resume_fids)} 条已有说说，将跳过重复项并追加新内容。")
    elif resume:
        click.echo("输出文件不存在，以全量模式启动。")

    file_mode = "a" if resume and output_path.exists() else "w"
    total_written = 0
    total_skipped = 0

    logger.info(
        "开始抓取 UIN=%d 的全部说说（max_pages=%s，since=%s，resume=%s）…",
        config.auth.uin,
        max_pages if max_pages > 0 else "不限",
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since_time)) if since_time > 0 else "不限",
        resume,
    )

    try:
        with open(output_path, file_mode, encoding="utf-8") as f:
            async for page_num, page_feeds in iter_self_feeds(
                uin=config.auth.uin,
                cookie_file=config.storage.cookie_file,
                since_time=since_time,
                max_pages=max_pages,
            ):
                for feed in page_feeds:
                    fid = getattr(feed, "fid", None)
                    # --continue 模式：跳过已写入文件的条目
                    if fid and fid in resume_fids:
                        total_skipped += 1
                        continue
                    # 本次运行内去重（API 可能跨页返回重复条目）
                    if fid and fid in seen_fids:
                        continue
                    try:
                        record = feed_to_rich_dict(feed)
                    except Exception as exc:
                        logger.warning("序列化说说 %s 失败，降级为基础格式：%s", fid or "?", exc)
                        try:
                            record = feed_to_dict(feed)
                        except Exception:
                            continue
                    f.write(_json.dumps(record, ensure_ascii=False) + "\n")
                    f.flush()
                    total_written += 1
                    if fid:
                        seen_fids.add(fid)

                click.echo(
                    f"\r  第 {page_num + 1} 页，已写入 {total_written} 条"
                    + (f"（跳过 {total_skipped} 条）" if total_skipped else "")
                    + "…",
                    nl=False,
                )
    except RuntimeError as e:
        click.echo()  # 换行
        logger.error("%s", e)
        sys.exit(1)

    click.echo()  # 换行
    summary = f"完成：共写入 {total_written} 条说说至 {output_path}"
    if total_skipped:
        summary += f"（跳过已有 {total_skipped} 条）"
    click.echo(summary)


@cli.command("to-markdown")
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
    "-i",
    "--input",
    "input_path",
    default=None,
    type=click.Path(path_type=Path),
    help="输入 JSONL 文件路径。默认为 <data_dir>/my_feeds.jsonl。",
)
@click.option(
    "-o",
    "--output",
    "output_path",
    default=None,
    type=click.Path(path_type=Path),
    help="输出 Markdown 文件路径。默认为 <data_dir>/my_feeds.md。",
)
@click.option(
    "--cache",
    "cache_path",
    default=None,
    type=click.Path(path_type=Path),
    help="图片描述缓存文件路径（JSON）。默认为 <data_dir>/img_desc_cache.json。",
)
@click.option(
    "--vision-model",
    "vision_model",
    default=None,
    help="用于描述图片的多模态模型名称（覆盖配置文件中的 openai.model）。",
)
@click.option(
    "--no-vision",
    "no_vision",
    is_flag=True,
    help="跳过图片描述，仅输出文字内容和图片 URL。",
)
@click.option("-v", "--verbose", is_flag=True, help="输出详细日志。")
def to_markdown(
    config_path: Path,
    input_path: Path | None,
    output_path: Path | None,
    cache_path: Path | None,
    vision_model: str | None,
    no_vision: bool,
    verbose: bool,
) -> None:
    """将已爬取的说说 JSONL 转换为 Markdown 格式，便于发给大模型分析。

    对包含图片的说说，使用多模态模型自动描述图片内容（需配置 [openai]）。
    图片描述结果会缓存到本地，重新运行时自动跳过已处理的条目。

    \b
    示例：
      uv run qzone-cron to-markdown                          # 使用默认路径
      uv run qzone-cron to-markdown --no-vision              # 跳过图片描述
      uv run qzone-cron to-markdown --vision-model gpt-4o    # 指定视觉模型
    """
    _setup_logging(verbose)
    asyncio.run(_to_markdown(config_path, input_path, output_path, cache_path, vision_model, no_vision))


async def _to_markdown(
    config_path: Path,
    input_path: Path | None,
    output_path: Path | None,
    cache_path: Path | None,
    vision_model: str | None,
    no_vision: bool,
) -> None:
    import json as _json

    logger = logging.getLogger(__name__)
    config = load_config(config_path)

    if input_path is None:
        input_path = config.storage.data_path / "my_feeds.jsonl"
    if output_path is None:
        output_path = config.storage.data_path / "my_feeds.md"
    if cache_path is None:
        cache_path = config.storage.data_path / "img_desc_cache.json"

    if not input_path.exists():
        logger.error("输入文件不存在：%s", input_path)
        sys.exit(1)

    # 加载全部说说
    entries: list[dict] = []
    with open(input_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(_json.loads(line))
                except Exception:
                    pass
    click.echo(f"已加载 {len(entries)} 条说说。")

    # 按时间升序排列（从最早到最新）
    entries.sort(key=lambda e: e.get("time", 0))

    # 加载图片描述缓存
    img_cache: dict[str, str] = {}
    if cache_path.exists():
        try:
            raw_cache = _json.loads(cache_path.read_text(encoding="utf-8"))
            # 兼容旧格式（list[str]）：合并为单字符串
            img_cache = {
                k: (" ".join(v).strip() if isinstance(v, list) else v)
                for k, v in raw_cache.items()
            }
        except Exception:
            img_cache = {}
        click.echo(f"已加载图片描述缓存（{len(img_cache)} 条）。")

    # 构建 LLM 配置（视觉模型）
    openai_cfg = config.openai.as_dict()
    if vision_model:
        openai_cfg["model"] = vision_model

    # 处理图片描述
    if not no_vision:
        need_vision = [e for e in entries if e.get("has_images") and e.get("image_urls") and e["fid"] not in img_cache]
        if need_vision:
            click.echo(f"需要描述图片的说说：{len(need_vision)} 条，开始处理…")
        for idx, entry in enumerate(need_vision, 1):
            fid = entry["fid"]
            image_urls: list[str] = entry.get("image_urls", [])
            content_text: str = entry.get("content", "")
            click.echo(f"\r  [{idx}/{len(need_vision)}] 描述图片中…", nl=False)
            try:
                description = await _describe_images(openai_cfg, content_text, image_urls)
                img_cache[fid] = description
            except Exception as exc:
                logger.warning("描述图片失败（fid=%s）：%s", fid, exc)
                img_cache[fid] = f"[图片描述失败：{exc}]"
            # 每条处理完后立即保存缓存
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(_json.dumps(img_cache, ensure_ascii=False, indent=2), encoding="utf-8")
        if need_vision:
            click.echo()  # 换行

    # 渲染 Markdown
    lines: list[str] = []
    lines.append("# 我的 QQ 空间说说\n")
    lines.append(f"> 共 {len(entries)} 条，导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append("---\n")

    for entry in entries:
        lines.append(_entry_to_markdown(entry, img_cache, no_vision))

    md_text = "\n".join(lines)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(md_text, encoding="utf-8")
    click.echo(f"已生成 Markdown 文件：{output_path}（{len(entries)} 条说说）")


async def _fetch_image_as_data_uri(url: str) -> str:
    """下载图片并转换为 base64 data URI，失败时返回原 URL。"""
    import base64 as _b64
    import httpx

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"Referer": "https://user.qzone.qq.com/"})
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            if not content_type.startswith("image/"):
                content_type = "image/jpeg"
            b64 = _b64.b64encode(resp.content).decode()
            return f"data:{content_type};base64,{b64}"
    except Exception:
        return url  # 降级：直接用 URL


async def _describe_images(cfg: dict, content_text: str, image_urls: list[str]) -> str:
    """下载图片后以 base64 data URI 发给多模态 LLM，返回整体描述字符串。"""
    content_parts: list[dict] = []

    prompt = "请描述以下图片的内容，稍微详细一些。"
    if content_text:
        prompt += f"\n\n说说正文（供参考上下文）：{content_text}"

    content_parts.append({"type": "text", "text": prompt})

    # 并发下载所有图片
    data_uris = await asyncio.gather(*[_fetch_image_as_data_uri(u) for u in image_urls])
    for data_uri in data_uris:
        content_parts.append({"type": "image_url", "image_url": {"url": data_uri}})

    response = await chat_completion(
        cfg=cfg,
        messages=[{"role": "user", "content": content_parts}],
        timeout=60.0,
        max_tokens=1000,
    )
    return response.strip()


def _entry_to_markdown(entry: dict, img_cache: dict[str, str], no_vision: bool) -> str:
    """将单条说说转换为 Markdown 字符串。"""
    import datetime as _dt

    fid = entry.get("fid", "")
    ts = entry.get("time", 0)
    dt_str = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "未知时间"
    content = (entry.get("content") or "").strip()
    image_urls: list[str] = entry.get("image_urls", [])
    like_count: int = entry.get("like_count", 0)
    comment_count: int = entry.get("comment_count", 0)
    view_count: int = entry.get("view_count", 0)
    all_comments: list[dict] = entry.get("all_comments", [])
    original: dict | None = entry.get("original")
    share_title: str = entry.get("share_title", "") or ""
    share_summary: str = entry.get("share_summary", "") or ""

    parts: list[str] = []
    parts.append(f"## {dt_str}\n")

    if content:
        parts.append(content + "\n")

    # 转发原文
    if original:
        if original.get("deleted"):
            parts.append("> *（转发内容已删除）*\n")
        else:
            orig_content = (original.get("content") or "").strip()
            orig_nick = original.get("nickname") or "未知用户"
            if orig_content:
                parts.append(f"> **转发自 {orig_nick}**：{orig_content}\n")
            elif orig_nick:
                parts.append(f"> **转发自 {orig_nick}**\n")

    # 分享链接摘要
    if share_title:
        parts.append(f"> 🔗 **{share_title}**")
        if share_summary:
            parts.append(f"> {share_summary}")
        parts.append("")

    # 图片
    if image_urls:
        desc: str = img_cache.get(fid, "") if not no_vision else ""
        parts.append(f"**图片**（共 {len(image_urls)} 张）：\n")
        if desc:
            parts.append(desc)
        else:
            for i, url in enumerate(image_urls):
                parts.append(f"- 图片 {i+1}：{url}")
        parts.append("")

    # 互动数据
    stats_parts = []
    if like_count:
        stats_parts.append(f"👍 {like_count}")
    if comment_count:
        stats_parts.append(f"💬 {comment_count}")
    if view_count:
        stats_parts.append(f"👁 {view_count}")
    if stats_parts:
        parts.append("**互动**：" + "  ".join(stats_parts) + "\n")

    # 评论
    if all_comments:
        parts.append("**评论**：\n")
        for c in all_comments:
            nick = c.get("nickname") or "匿名"
            c_content = (c.get("content") or "").strip()
            c_ts = c.get("date", 0)
            c_dt = _dt.datetime.fromtimestamp(c_ts).strftime("%m-%d %H:%M") if c_ts else ""
            date_str = f" *{c_dt}*" if c_dt else ""
            parts.append(f"- **{nick}**{date_str}：{c_content}")
            replies: list[dict] = c.get("replies", [])
            for r in replies:
                r_nick = r.get("nickname") or "匿名"
                r_content = (r.get("content") or "").strip()
                r_ts = r.get("date", 0)
                r_dt = _dt.datetime.fromtimestamp(r_ts).strftime("%m-%d %H:%M") if r_ts else ""
                r_date_str = f" *{r_dt}*" if r_dt else ""
                if r_content:
                    parts.append(f"  - **{r_nick}**{r_date_str}：{r_content}")
        parts.append("")

    parts.append("---\n")
    return "\n".join(parts)


if __name__ == "__main__":
    cli()
