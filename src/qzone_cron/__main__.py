from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

import click

from .config import load_config
from .fetcher import fetch_feeds, setup_login
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
    logger = logging.getLogger(__name__)

    config = load_config(config_path)
    cookie_file = config.storage.cookie_file

    logger.info("开始 QQ 空间登录流程（UIN: %d）…", config.auth.uin)
    try:
        asyncio.run(setup_login(config.auth.uin, cookie_file))
    except Exception as e:
        logger.error("登录失败：%s", e)
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


async def _run(config_path: Path, plugins_dir: Path) -> None:
    logger = logging.getLogger(__name__)

    config = load_config(config_path)
    state = State(config.storage.state_file)

    plugins = load_plugins(plugins_dir, config.plugins)
    if not plugins:
        logger.warning("未加载任何插件，退出。")
        return

    plugin_context = {
        "uin": config.auth.uin,
        "cookie_file": config.storage.cookie_file,
        "data_dir": config.storage.data_path,
        "plugins_config": config.plugins,
    }

    now = time.time()
    interval_secs = config.fetch.fetch_interval_minutes * 60
    elapsed = now - state.last_fetched_at
    should_fetch = elapsed >= interval_secs

    feeds: list = []

    if should_fetch:
        logger.info(
            "开始抓取说说（UIN: %d，上次抓取时间: %s）…",
            config.auth.uin,
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state.last_fetch_time))
            if state.last_fetch_time > 0
            else "从未",
        )
        try:
            feeds = await fetch_feeds(
                uin=config.auth.uin,
                cookie_file=config.storage.cookie_file,
                since_time=state.last_fetch_time,
                max_pages=config.fetch.max_pages,
            )
        except RuntimeError as e:
            logger.error("%s", e)
            sys.exit(1)

        state.last_fetched_at = now
        if feeds:
            newest_time = max(f.common.time for f in feeds)
            state.last_fetch_time = float(newest_time)
        state.save()

        if feeds:
            logger.info(
                "抓取到 %d 条新说说，更新上次抓取时间为 %s。",
                len(feeds),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state.last_fetch_time)),
            )
        else:
            logger.info("没有新说说。")
    else:
        remaining = int(interval_secs - elapsed)
        logger.info(
            "距上次抓取仅 %ds，未到抓取间隔（%dmin），跳过抓取，仅执行插件维护任务。",
            int(elapsed),
            config.fetch.fetch_interval_minutes,
        )

    await run_plugins(plugins, feeds, context=plugin_context)


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

    plugin_context = {
        "uin": config.auth.uin,
        "cookie_file": config.storage.cookie_file,
        "data_dir": config.storage.data_path,
        "plugins_config": config.plugins,
    }
    await mod.force_send(context=plugin_context)


if __name__ == "__main__":
    cli()
