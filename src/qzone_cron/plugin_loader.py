from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_plugins(plugins_dir: Path, plugin_configs: dict[str, Any]) -> list[Any]:
    """
    从 plugins_dir 目录加载所有插件模块。

    每个插件文件需定义：
        async def process(feeds: list) -> None: ...

    可选定义：
        PLUGIN_NAME: str  # 插件显示名称（默认使用文件名）
        ENABLED: bool     # 设置为 False 可禁用插件（默认 True）

    插件也可通过 config.toml 中 [plugins.<name>] 下的 enabled = false 被禁用。
    """
    if not plugins_dir.exists():
        logger.warning("插件目录 %s 不存在，跳过插件加载。", plugins_dir)
        return []

    plugins = []
    for plugin_file in sorted(plugins_dir.glob("*.py")):
        if plugin_file.name.startswith("_"):
            continue

        module_name = plugin_file.stem
        plugin_cfg = plugin_configs.get(module_name, {})

        # 检查是否通过配置文件禁用
        if not plugin_cfg.get("enabled", True):
            logger.info("插件 %s 已在配置中禁用，跳过。", module_name)
            continue

        spec = importlib.util.spec_from_file_location(
            f"qzone_cron_plugin.{module_name}", plugin_file
        )
        if spec is None or spec.loader is None:
            logger.warning("无法加载插件文件 %s，跳过。", plugin_file)
            continue

        module = importlib.util.module_from_spec(spec)
        sys.modules[f"qzone_cron_plugin.{module_name}"] = module

        try:
            spec.loader.exec_module(module)
        except Exception:
            logger.exception("加载插件 %s 时出错，跳过。", module_name)
            continue

        # 检查插件内部的 ENABLED 标志
        if not getattr(module, "ENABLED", True):
            logger.info("插件 %s 内部已设置 ENABLED=False，跳过。", module_name)
            continue

        if not hasattr(module, "process"):
            logger.warning("插件 %s 未定义 process() 函数，跳过。", module_name)
            continue

        display_name = getattr(module, "PLUGIN_NAME", module_name)
        logger.info("已加载插件：%s", display_name)
        plugins.append(module)

    return plugins


async def run_plugins(
    plugins: list[Any], feeds: list[Any], context: dict[str, Any] | None = None
) -> None:
    """将 feeds 列表依次传递给每个插件的 process() 函数。

    若插件的 process() 函数签名中包含 context 参数，则额外传入 context 字典。
    context 包含以下键：
        uin        - 账号 QQ 号（int）
        cookie_file - Cookie 文件路径（Path）
        data_dir    - 数据目录路径（Path）
    """
    import asyncio
    import inspect

    for module in plugins:
        display_name = getattr(module, "PLUGIN_NAME", module.__name__)
        try:
            sig = inspect.signature(module.process)
            if "context" in sig.parameters:
                result = module.process(feeds, context=context)
            else:
                result = module.process(feeds)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("插件 %s 处理说说时出错。", display_name)
