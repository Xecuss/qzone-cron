# qzone-cron

基于 [uv](https://docs.astral.sh/uv/) 的 Python 项目，通过 crontab 定期抓取 QQ 空间说说，并将内容分发给 `plugins/` 目录下的各个插件处理。

底层使用 [aioqzone](https://github.com/aioqzone/aioqzone) 库访问 QQ 空间 API。

## 项目结构

```
qzone-cron/
├── pyproject.toml          # uv 项目配置
├── config.example.toml     # 配置文件示例
├── src/
│   └── qzone_cron/
│       ├── __main__.py     # CLI 入口（setup / run 命令）
│       ├── config.py       # 配置加载（TOML + Pydantic）
│       ├── fetcher.py      # QQ 空间说说抓取
│       ├── plugin_loader.py # 插件加载与分发
│       └── state.py        # 运行状态持久化
└── plugins/
    ├── __init__.py
    └── print_plugin.py     # 示例插件：打印说说内容
```

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

如需在 setup 时弹出二维码图片窗口：

```bash
uv sync --extra qr-display
```

### 2. 初始化配置

```bash
cp config.example.toml config.toml
# 编辑 config.toml，填入你的 QQ 号
```

### 3. 首次登录（扫码）

```bash
uv run qzone-cron setup
```

扫描终端或弹出窗口中的二维码，完成登录。Cookie 将自动保存至 `~/.local/share/qzone-cron/cookies.json`。

### 4. 手动运行一次

```bash
uv run qzone-cron run
```

### 5. 配置 crontab

```bash
crontab -e
```

添加以下内容（每 15 分钟执行一次）：

```
*/15 * * * * cd /path/to/qzone-cron && uv run qzone-cron run >> /var/log/qzone-cron.log 2>&1
```

## 命令行选项

```
qzone-cron setup [-c config.toml] [-v]
qzone-cron run   [-c config.toml] [-p plugins/] [-v]
```

## 编写插件

在 `plugins/` 目录中新建一个 `.py` 文件，定义 `process()` 异步函数：

```python
# plugins/my_plugin.py

PLUGIN_NAME = "my_plugin"   # 可选，用于日志显示
ENABLED = True               # 可选，False 则禁用

async def process(feeds: list) -> None:
    """
    feeds: 本次新抓取到的说说列表（aioqzone FeedData 对象）
    
    常用字段：
      feed.fid                  — 说说 ID
      feed.userinfo.uin         — 发布者 QQ 号
      feed.userinfo.nickname    — 发布者昵称
      feed.common.time          — 发布时间（Unix 时间戳）
      feed.common.appid         — 内容类型（311 = 普通说说）
      feed.summary.summary      — 说说文字内容
      feed.pic.photos           — 图片列表（可能为 None）
      feed.video                — 视频信息（可能为 None）
    """
    for feed in feeds:
        print(feed.summary.summary)
```

插件也可在 `config.toml` 中通过 `enabled = false` 禁用：

```toml
[plugins.my_plugin]
enabled = false
```

## Cookie 过期处理

当 Cookie 过期时，`run` 命令会报错退出。重新执行 `setup` 命令扫码登录即可。

> **提示**：`aioqzone` 文档建议每 5 分钟调用一次 `mfeeds_get_count` 可保持 Cookie 活跃。`run` 命令通过 `get_active_feeds` 访问 API，同样有保活效果。

## 许可证

本项目依赖 [aioqzone](https://github.com/aioqzone/aioqzone)（AGPL-3.0）。请阅读其[免责声明](https://aioqzone.github.io/aioqzone/disclaimers.html)后再使用。
