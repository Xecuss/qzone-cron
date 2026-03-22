"""
print_plugin — 示例插件

将新抓取到的说说内容打印到标准输出。
可作为编写自定义插件的参考模板。

插件接口规范
-----------
插件文件放置于 plugins/ 目录，文件名不以 _ 开头。

必须定义：
    async def process(feeds: list) -> None

可选定义：
    PLUGIN_NAME: str   # 展示名称
    ENABLED: bool      # 设为 False 可在不删文件的情况下禁用此插件

也可在 config.toml 中通过以下方式禁用：
    [plugins.print_plugin]
    enabled = false
"""
from __future__ import annotations

import time
from typing import Any

PLUGIN_NAME = "print_plugin"
ENABLED = True


async def process(feeds: list[Any]) -> None:
    """打印每条说说的基本信息。"""
    print(f"\n[{PLUGIN_NAME}] 收到 {len(feeds)} 条新说说：")
    print("-" * 60)

    for feed in feeds:
        uin: int = feed.userinfo.uin
        nickname: str = feed.userinfo.nickname or str(uin)
        post_time: str = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(feed.common.time)
        )
        content: str = feed.summary.summary if feed.summary else ""

        print(f"  [{post_time}] {nickname} ({uin})")
        if content:
            # 截断过长内容
            preview = content[:200] + ("…" if len(content) > 200 else "")
            print(f"  内容：{preview}")

        # 图片数量
        if feed.pic and feed.pic.picdata:
            print(f"  图片：{len(feed.pic.picdata)} 张")

        # 视频
        if feed.video:
            print(f"  视频：{feed.video.url if hasattr(feed.video, 'url') else '有视频'}")

        print()

    print("-" * 60)
