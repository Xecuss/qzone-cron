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
│       ├── __main__.py     # CLI 入口（setup / run / send-summary 命令）
│       ├── config.py       # 配置加载（TOML + Pydantic）
│       ├── fetcher.py      # QQ 空间说说抓取
│       ├── plugin_loader.py # 插件加载与分发
│       └── state.py        # 运行状态持久化
└── plugins/
    ├── __init__.py
    ├── print_plugin.py          # 示例插件：打印说说内容
    ├── auto_delete_plugin.py    # 自动删除说说插件
    └── daily_summary_plugin.py  # 每日空间简报插件
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
# 编辑 config.toml，填入你的 QQ 号及各插件配置
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
qzone-cron setup        [-c config.toml] [-v]
qzone-cron run          [-c config.toml] [-p plugins/] [-v]
qzone-cron send-summary [-c config.toml] [-p plugins/] [-v]
```

`send-summary` — 忽略 `summary_hour` 时间限制，立即用队列中已有的说说生成简报并发送，主要用于测试。若队列为空，可先执行一次 `qzone-cron run` 抓取说说再调用。

## 内置插件

### `auto_delete_plugin` — 自动删除说说

检测**自己**发布的说说内容中的 `/autodelete` 指令，到期后自动通过 API 删除该说说。

支持以下写法：

```
/autodelete          # 下次检测周期时立即删除
/autodelete 5min     # 5 分钟后删除
/autodelete 5h       # 5 小时后删除
```

无需额外配置，插件默认启用。如需禁用：

```toml
[plugins.auto_delete_plugin]
enabled = false
```

### `daily_summary_plugin` — 每日空间简报

每次 `run` 时，将好友（非自己）的最新说说追加到本地待摘要队列；每天到达 `summary_hour` 指定的小时后，调用 OpenAI 兼容接口生成中文简报，并通过 Telegram Bot 发送。

**主要特性：**

- 每条说说记录文字内容、媒体类型（图片/视频）、点赞/评论数及前几条评论
- 转发说说同时附上原文作者和内容
- 通过 `vip_uins` 配置特别关注名单，对应用户的动态在简报中单独分组高亮
- 简报以 HTML 格式发送，支持超长内容自动分段（Telegram 单条 4096 字限制）
- 发送成功后才清空队列，发送失败则保留数据供下次重试

**配置示例（`config.toml`）：**

```toml
[plugins.daily_summary_plugin]
enabled = true
summary_hour = 8          # 每天几点发送摘要（0-23，本地时间）
vip_uins = [12345678]     # 特别关注的 QQ 号列表

[plugins.daily_summary_plugin.openai]
api_key = "sk-..."
base_url = "https://api.openai.com/v1"  # 可替换为任意 OpenAI 兼容接口
model = "gpt-4o-mini"
# system_prompt = "..."   # 可选，覆盖内置系统提示词

[plugins.daily_summary_plugin.telegram]
bot_token = "123456:ABC-..."
chat_id = "-1001234567890"
```

**测试简报发送：**

```bash
# 先抓取一批说说进队列
uv run qzone-cron run

# 不等到 summary_hour，立即生成并发送
uv run qzone-cron send-summary
```

## 编写插件

在 `plugins/` 目录中新建一个 `.py` 文件，定义 `process()` 异步函数：

```python
# plugins/my_plugin.py

PLUGIN_NAME = "my_plugin"   # 可选，用于日志显示
ENABLED = True               # 可选，False 则禁用

async def process(feeds: list, context: dict | None = None) -> None:
    """
    feeds:   本次新抓取到的说说列表（aioqzone FeedData 对象）
    context: 运行上下文，包含以下键：
               uin           — 账号 QQ 号
               cookie_file   — Cookie 文件路径
               data_dir      — 数据目录（Path）
               plugins_config — 插件配置字典

    常用 feed 字段：
      feed.userinfo.uin         — 发布者 QQ 号
      feed.userinfo.nickname    — 发布者昵称
      feed.common.time          — 发布时间（Unix 时间戳）
      feed.summary.summary      — 说说文字内容
      feed.pic                  — 图片信息（可能为 None）
      feed.video                — 视频信息（可能为 None）
      feed.like.likeNum         — 点赞数（可能为 None）
      feed.comment.num          — 评论数（可能为 None）
      feed.comment.comments     — 评论列表（可能为 None）
      feed.original             — 转发原文（可能为 None）
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
