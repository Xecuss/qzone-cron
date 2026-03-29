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
│       ├── notifier.py     # 全局通知（Telegram send_notice）
│       ├── plugin_loader.py # 插件加载与分发
│       └── state.py        # 运行状态持久化
└── plugins/
    ├── __init__.py
    ├── print_plugin.py          # 示例插件：打印说说内容
    ├── auto_delete_plugin.py    # 自动删除说说插件
    ├── auto_like_plugin.py      # 自动点赞插件
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

若已配置全局 `[telegram]`，二维码图片也会同时发送到 Telegram，方便远程扫码。

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

`send-summary` — 忽略 `summary_times` 时间限制，立即用队列中已有的说说生成简报并发送，主要用于测试。若队列为空，可先执行一次 `qzone-cron run` 抓取说说再调用。

## 全局配置

`config.toml` 中有四个顶层配置块，影响主流程行为。

### `[auth]` — 账号与登录

```toml
[auth]
uin = 123456789           # 你的 QQ 号
auto_relogin = false      # Cookie 失效时自动发起重登并推送二维码到 Telegram
```

### `[storage]` — 数据存储

```toml
[storage]
data_dir = "~/.local/share/qzone-cron"   # Cookie / 状态文件存放目录（支持 ~ 路径）
```

### `[fetch]` — 抓取行为与全量更新

```toml
[fetch]
max_pages = 10                      # 每次最多抓取的页数（每页约 10-20 条）
time_window_hours = 24.0            # 首次运行（无状态文件）时的回溯时间窗口（小时）
fetch_interval_minutes = 5          # 实际抓取间隔（分钟）；crontab 可高频触发，两次抓取之间只执行插件维护任务
stats_refresh_interval_minutes = 60 # 全量 stats 刷新间隔：每隔此时间重新拉取近期说说以更新点赞/评论数
stats_refresh_window_hours = 6.0    # 全量刷新覆盖的时间范围（小时）
feed_retention_hours = 48.0         # feed 详情在内存中的保留时长（小时），须 >= stats_refresh_window_hours
```

**全量 stats 刷新**：普通增量抓取只能拿到新发布的说说，已有说说的点赞/评论数不会自动更新。每隔 `stats_refresh_interval_minutes` 分钟，主流程会重新拉取过去 `stats_refresh_window_hours` 小时内的所有说说并刷新点赞、评论等互动数据，再回调各插件的 `process()`，使插件能感知到数据变化（如评论数增加）。

### `[telegram]` — 全局 Telegram 通知

配置后，主流程（如检测到登录失效、`auto_relogin` 推送二维码）与各插件均可通过 `context["send_notice"]` 发送通知消息。

```toml
[telegram]
bot_token = "123456:ABC-..."      # 通过 @BotFather 创建的 Bot Token
chat_id = "-1001234567890"        # 发送目标的 Chat ID（个人、群组或频道均可）
```

> 插件通过 `context["send_notice"]` 调用，未配置时该函数为 `None`，插件应做判空处理。

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

每次 `run` 时，将好友（非自己）的最新说说追加到本地待摘要队列；每当 cron 运行越过 `summary_times` 中配置的某个时间点后，调用 OpenAI 兼容接口生成中文简报，并通过 Telegram Bot 发送。

**主要特性：**

- 每条说说记录文字内容、互动数据（点赞/评论数及前几条评论）以及媒体类型
- **图片预描述**：说说入队时即调用视觉模型（OpenAI Vision 兼容接口）对图片内容进行 1-2 句描述并存入队列；生成简报时大模型可读到图片的实际内容，而不仅是"含图片"标注，使简报更真实。可通过 `describe_images = false` 关闭，或通过 `vision_model` 指定独立的视觉模型
- 转发说说同时附上原文作者、内容及原文图片描述
- 通过 `summary_times` 配置多个推送时间点，采用"越过时间点触发"机制，即使 cron 频率浮动也不会漏推
- 简报以 HTML 格式发送，支持超长内容自动分段（Telegram 单条 4096 字限制）
- 发送成功后才清空队列，发送失败则保留数据供下次重试

**配置示例（`config.toml`）：**

```toml
[plugins.daily_summary_plugin]
enabled = true
summary_times = ["08:00", "20:00"]  # 每天推送时间点，可配置多个
vip_uins = [12345678]               # 特别关注的 QQ 号列表

[plugins.daily_summary_plugin.openai]
api_key = "sk-..."
base_url = "https://api.openai.com/v1"  # 可替换为任意 OpenAI 兼容接口
model = "gpt-4o-mini"                   # 用于生成简报
# describe_images = true    # 入队时自动调用视觉模型预描述图片（默认开启）
# vision_model = "gpt-4o"  # 可选，单独指定视觉模型；不填则复用 model
# system_prompt = "..."    # 可选，覆盖内置系统提示词
```

**测试简报发送：**

```bash
# 先抓取一批说说进队列
uv run qzone-cron run

# 不等到 summary_hour，立即生成并发送
uv run qzone-cron send-summary
```

### `auto_like_plugin` — 自动点赞

模拟真实用户点赞习惯：通过大模型过滤说说（排除情绪低落、自我攻击类内容）后入队，随机延迟一段时间后批量点赞，并自动回避凌晨等不适宜操作的时段。

**工作流程：**

1. 每次 `run` 抓取到好友说说后，批量发给大模型判断是否应该点赞
2. 通过筛选的说说入待点赞队列，并预计算一个随机激活时间
3. cron 运行到激活时间后，每次取若干条点赞（条数可配置），每两条之间随机等待 5-10 秒
4. 队列清空后重新预计算下一次激活时间
5. 激活时间若落在禁止时段（如凌晨）内，自动顺延到允许的小时

**配置示例（`config.toml`）：**

```toml
[plugins.auto_like_plugin]
enabled = true
likes_per_cycle = 3           # 每次激活处理的条数
like_interval_min = 5.0       # 两次点赞之间的最短间隔（秒）
like_interval_max = 10.0      # 两次点赞之间的最长间隔（秒）
activation_delay_min = 30     # 激活延迟最短时间（分钟）
activation_delay_max = 180    # 激活延迟最长时间（分钟）
forbidden_hours = [0,1,2,3,4,5,6]  # 禁止激活的小时（本地时间 0-23）

[plugins.auto_like_plugin.openai]
api_key = "sk-..."
base_url = "https://api.openai.com/v1"  # 可替换为任意 OpenAI 兼容接口
model = "gpt-4o-mini"
# json_mode = true    # 开启 response_format: json_object（仅 OpenAI 官方等部分模型支持）
# system_prompt = "..."  # 可选，覆盖内置的点赞判断提示词
```

> **提示**：不配置 `[plugins.auto_like_plugin.openai]` 时，所有好友说说均会进入点赞队列（无 LLM 过滤）。

### `like_to_show_plugin` — 达到点赞阈值后公布第二段内容

检测**自己**发布的说说中的 `/like_to_show` 指令，当该说说点赞数超过阈值后，自动将预先提供的"第二段内容"以指定方式发布出去。

**指令格式：**

```
/like_to_show <like_count> [edit_method]
```

- `like_count`：整数，点赞阈值
- `edit_method`：触发后的发布方式（可选，缺省使用配置文件中的 `default_edit_method`）

| `edit_method` | 行为 |
|---|---|
| `comment` | 在原说说评论区发布第二段内容（原说说保留）|
| `new` | 将第二段内容发布为新说说（原说说保留）|
| `append` | 删除原说说，将去除指令的原始内容与第二段内容合并后发新说说 |
| `delete` | 删除原说说，仅将第二段内容作为新说说单独发布 |

**工作流程：**

1. 扫描到含 `/like_to_show` 指令的自己的说说后，通过 Telegram Bot 发送通知（含说说预览和点赞阈值）
2. 用户 **reply** 该 Telegram 消息来提供"第二段内容"，插件自动匹配并记录
3. 每次 cron 运行时检查点赞数是否达到阈值：
   - 达到阈值 + 已收到第二段内容 → 执行发布
   - 达到阈值 + 尚未收到第二段内容 → 发送 Telegram 警告（仅一次），等待内容
   - 未达到阈值 → 继续等待

> 依赖全局 `[telegram]` 配置（用于发送记录通知和接收 reply）。

**配置示例（`config.toml`）：**

```toml
[plugins.like_to_show_plugin]
enabled = true
# 默认发布方式（可在说说指令中逐条覆盖）
# 可选值：comment | new | append | delete（默认 comment）
default_edit_method = "comment"
```

**示例说说：**

```
今天吃了一顿特别好吃的饭 /like_to_show 10
```

当该说说获得 10 个点赞后，插件会将你提前 reply 到 TG 通知的内容以 `comment` 方式（或配置的默认方式）发布出去。也可在指令中直接指定方式：

```
神秘公告 /like_to_show 50 append
```

达到 50 赞后，删除原说说，将原内容（去除指令）与第二段内容合并后发布新说说。

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
               uin            — 账号 QQ 号
               cookie_file    — Cookie 文件路径
               data_dir       — 数据目录（Path）
               plugins_config — 插件配置字典
               send_notice    — 全局 Telegram 通知函数（async (str) -> None）
                                未配置全局 [telegram] 时为 None

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
    send_notice = context.get("send_notice") if context else None
    for feed in feeds:
        print(feed.summary.summary)
        if send_notice:
            await send_notice(f"新说说：{feed.summary.summary}")
```

插件也可在 `config.toml` 中通过 `enabled = false` 禁用：

```toml
[plugins.my_plugin]
enabled = false
```

## Cookie 过期处理

当 Cookie 过期时，`run` 命令会检测到登录失效。有两种处理方式：

**方式一：手动重新登录**

```bash
uv run qzone-cron setup
```

**方式二：`auto_relogin` 自动重登（推荐配合 crontab 使用）**

在 `config.toml` 中开启：

```toml
[auth]
uin = 123456789
auto_relogin = true
```

开启后，当 `run` 检测到登录失效时，会自动进入登录流程并将二维码发送至 Telegram（需配置 `[telegram]`）。
二维码过期刷新时，Telegram 中的图片会**原地更新**，不会产生新消息。

**防止 crontab 重复触发：** 自动登录期间会在数据目录写入 `setup.lock` 锁文件（记录进程 PID）。
crontab 下次触发时若检测到该进程仍在运行（等待扫码），会静默退出，不会重新发送二维码。
扫码成功或登录超时后，锁文件自动删除，后续 cron 恢复正常抓取。

> **提示**：`aioqzone` 文档建议每 5 分钟调用一次 `mfeeds_get_count` 可保持 Cookie 活跃。`run` 命令通过 `get_active_feeds` 访问 API，同样有保活效果。

## 许可证

本项目依赖 [aioqzone](https://github.com/aioqzone/aioqzone)（AGPL-3.0）。请阅读其[免责声明](https://aioqzone.github.io/aioqzone/disclaimers.html)后再使用。
