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
│       ├── llm.py          # LLM 调用统一接口
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

### setup — 交互式登录

```bash
uv run qzone-cron setup [-c config.toml] [-v]
```

首次使用或 Cookie 过期时执行。扫描二维码完成登录，Cookie 自动保存至数据目录。

### setup-tg — Telegram 会话绑定

```bash
uv run qzone-cron setup-tg [-c config.toml] [--timeout 300] [-v]
```

绑定 Telegram 会话以接收通知。流程：
1. 生成随机 6 位 PIN 码并显示在终端
2. 向 Bot 发送消息：`/qz <PIN>`
3. 会话 ID 自动保存，后续通知优先使用此会话

参数：
- `--timeout` — 等待验证的超时时间（秒），默认 300 秒

### run — 抓取说说并处理

```bash
uv run qzone-cron run [-c config.toml] [-p plugins/] [-v]
```

抓取 QQ 空间新说说并分发给各插件处理。通常由 crontab 定期调用：

```bash
*/15 * * * * cd /path/to/qzone-cron && uv run qzone-cron run >> /var/log/qzone-cron.log 2>&1
```

### send-summary — 立即发送简报

```bash
uv run qzone-cron send-summary [-c config.toml] [-p plugins/] [-v]
```

强制立即生成并发送 `daily_summary_plugin` 的简报（测试用）。忽略 `summary_hour` 时间限制，使用队列中已有的说说生成摘要。若队列为空，可先执行一次 `run` 命令抓取说说。

### dump — 导出全部说说

```bash
uv run qzone-cron dump [-c config.toml] [-o out.jsonl] [--since <timestamp>] [--max-pages N] [--continue] [-v]
```

抓取自己的全部说说并保存到 JSON Lines 格式文件（每行一条说说），包含正文、图片、点赞数、全部评论等。每抓取一页就实时写入文件，中断后可用 `--continue` 续传。

参数：
- `-o` / `--output` — 输出文件路径，默认为 `<data_dir>/my_feeds.jsonl`
- `--since` — 仅抓取晚于此 Unix 时间戳的说说，0 表示全部（默认）
- `--max-pages` — 最大翻页数，0 表示不限制（默认）
- `--continue` — 断点续传，读取已有输出文件中的 fid，跳过已写条目

示例：
```bash
uv run qzone-cron dump                       # 抓取全部，保存到 data/my_feeds.jsonl
uv run qzone-cron dump -o out.jsonl          # 指定输出路径
uv run qzone-cron dump --max-pages 5         # 仅抓取前 5 页
uv run qzone-cron dump --continue            # 断点续传
```

### to-markdown — 转换为 Markdown 格式

```bash
uv run qzone-cron to-markdown [-c config.toml] [-i input.jsonl] [-o output.md] [--no-vision]
```

将已导出的说说 JSONL 文件转换为 Markdown 格式，便于发给大模型分析或离线阅读。对包含图片的说说，使用多模态模型自动描述图片内容（需配置 `[openai]`）。

参数：
- `-i` / `--input` — 输入 JSONL 文件路径，默认为 `<data_dir>/my_feeds.jsonl`
- `-o` / `--output` — 输出 Markdown 文件路径，默认为 `<data_dir>/my_feeds.md`
- `--cache` — 图片描述缓存文件路径（JSON），默认为 `<data_dir>/img_desc_cache.json`
- `--vision-model` — 用于描述图片的多模态模型名称（覆盖配置文件中的 `openai.model`）
- `--no-vision` — 跳过图片描述，仅输出文字内容和图片 URL

特性：
- 自动按时间升序排列说说
- 支持图片描述缓存，重新运行时自动跳过已处理条目
- 包含说说的正文、转发、分享链接、评论等完整内容
- 输出的 Markdown 包含统计信息（说说数、导出时间等）

示例：
```bash
uv run qzone-cron to-markdown                          # 使用默认路径，自动描述图片
uv run qzone-cron to-markdown --no-vision              # 跳过图片描述
uv run qzone-cron to-markdown --vision-model gpt-4o    # 指定视觉模型
uv run qzone-cron to-markdown -i data/feeds.jsonl -o output.md
```

## 全局配置

`config.toml` 中有四个顶层配置块，影响主流程行为。

### Telegram 会话绑定

如需接收通知，需执行 `setup-tg` 命令绑定 Telegram 会话：

```bash
uv run qzone-cron setup-tg
```

流程说明：
1. **生成 PIN 码**：命令会生成随机 6 位数字 PIN 码并显示在终端
2. **用户验证**：用户在 Telegram 中向 Bot 发送 `/qz <PIN>` 完成验证
3. **保存会话**：验证成功后，会话 ID（chat_id）自动保存到状态文件

验证通过后，后续所有通知（包括登录二维码、插件消息等）都会优先发送到此会话。若未绑定会话，通知会发送到 `config.toml` 中配置的 `[telegram]` 的默认 `chat_id`。

可选：修改超时时间（默认 300 秒）：
```bash
uv run qzone-cron setup-tg --timeout 600
```

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

### `[openai]` — 全局大模型配置

需要调用 LLM（如 `daily_summary_plugin`、`auto_like_plugin`）的插件会使用此处的配置。各插件也可在单独的 `[plugins.<name>.openai]` 中覆盖相应字段。

```toml
[openai]
api_key = "sk-..."                           # OpenAI 兼容接口的 API Key
base_url = "https://api.openai.com/v1"       # 接口地址（可替换为 Azure / DeepSeek / 本地 Ollama 等）
model = "gpt-4o-mini"                        # 默认使用的模型名称
```

**配置优先级**（从高到低）：
1. `[plugins.<name>.openai]` 中显式写的字段
2. `[openai]` 全局字段
3. 代码内硬编码默认值

## 内置插件

| 插件 | 说明 |
|---|---|
| [`auto_delete_plugin`](docs/plugins/auto_delete_plugin.md) | 检测说说中的 `/autodelete` 指令，到期自动删除 |
| [`daily_summary_plugin`](docs/plugins/daily_summary_plugin.md) | 汇总好友说说，定时通过 Telegram 发送 AI 简报 |
| [`auto_like_plugin`](docs/plugins/auto_like_plugin.md) | 模拟真实习惯，经 LLM 过滤后随机延迟批量点赞 |
| [`like_to_show_plugin`](docs/plugins/like_to_show_plugin.md) | 说说点赞数达到阈值后，自动发布预先准备的第二段内容 |

各插件的详细配置说明见 `docs/plugins/` 目录。

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
               uin              — 账号 QQ 号
               cookie_file      — Cookie 文件路径
               data_dir         — 数据目录（Path）
               plugins_config   — 插件配置字典
               global_openai_cfg — 全局 [openai] 配置字典（含 api_key / base_url / model）
               llm_chat         — LLM 调用函数 (async (cfg, messages, **kwargs) -> str)
               send_notice      — 全局 Telegram 通知函数（async (str) -> None）
                                  未配置全局 [telegram] 时为 None
               feed_store       — 保存最近 feed 详情的字典（用于后续查询）

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

### 在插件中调用大模型

若需调用 LLM（如用来分析或生成说说内容），可使用 `context["llm_chat"]` 函数：

```python
async def process(feeds: list, context: dict | None = None) -> None:
    if context is None:
        return
    
    llm_chat = context.get("llm_chat")
    if llm_chat is None:
        return  # 未配置 LLM
    
    # 获取插件自己的配置及全局 OpenAI 配置
    plugin_cfg = (context.get("plugins_config") or {}).get("my_plugin", {})
    global_openai_cfg = context.get("global_openai_cfg") or {}
    
    # 合并配置（插件配置优先级更高）
    openai_cfg = {**global_openai_cfg, **plugin_cfg.get("openai", {})}
    
    for feed in feeds:
        # 调用 LLM，获取分析结果
        result = await llm_chat(
            openai_cfg,
            [
                {"role": "system", "content": "你是一个说说分析助手。"},
                {"role": "user", "content": f"分析这条说说：{feed.summary.summary}"},
            ],
            timeout=30.0,
            max_tokens=100,
        )
        print(f"分析结果：{result}")
```

插件配置示例：

```toml
[plugins.my_plugin]
enabled = true

# 可选：覆盖全局 [openai] 配置
[plugins.my_plugin.openai]
model = "gpt-4"
```

### 禁用插件

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
