# daily_summary_plugin — 每日空间简报

每次 `run` 时，将好友（非自己）的最新说说追加到本地待摘要队列；每当 cron 运行越过 `summary_times` 中配置的某个时间点后，调用 OpenAI 兼容接口生成中文简报，并通过 Telegram Bot 发送。

## 主要特性

- 每条说说记录文字内容、互动数据（点赞/评论数及前几条评论）以及媒体类型
- **图片预描述**：说说入队时即调用视觉模型（OpenAI Vision 兼容接口）对图片内容进行 1-2 句描述并存入队列；生成简报时大模型可读到图片的实际内容，而不仅是"含图片"标注，使简报更真实。可通过 `describe_images = false` 关闭，或通过 `vision_model` 指定独立的视觉模型
- 转发说说同时附上原文作者、内容及原文图片描述
- 通过 `summary_times` 配置多个推送时间点，采用"越过时间点触发"机制，即使 cron 频率浮动也不会漏推
- 简报以 HTML 格式发送，支持超长内容自动分段（Telegram 单条 4096 字限制）
- 发送成功后才清空队列，发送失败则保留数据供下次重试

## 配置示例

### 基础配置（推荐）

在顶层全局 `[openai]` 配置 LLM 参数，插件会自动使用：

```toml
[openai]
api_key = "sk-..."
base_url = "https://api.openai.com/v1"
model = "gpt-4o-mini"

[telegram]
bot_token = "123456:ABC-..."
chat_id = "-1001234567890"

[plugins.daily_summary_plugin]
enabled = true
summary_times = ["08:00", "20:00"]  # 每天推送时间点，可配置多个
vip_uins = [12345678]               # 特别关注的 QQ 号列表
```

### 进阶配置（插件级覆盖）

若需与其他插件使用不同的模型或独立配置，可在插件级配置中覆盖特定字段：

```toml
[plugins.daily_summary_plugin]
enabled = true
summary_times = ["08:00", "20:00"]
vip_uins = [12345678]

# 可选：覆盖全局 [openai] 的字段
[plugins.daily_summary_plugin.openai]
api_key = "sk-..."  # 若需使用不同的 API Key
model = "gpt-4"    # 只覆盖模型，base_url 仍沿用全局配置
# describe_images = true    # 入队时自动调用视觉模型预描述图片（默认开启）
# vision_model = "gpt-4o"  # 可选，单独指定视觉模型；不填则复用 model
# system_prompt = "..."    # 可选，覆盖内置系统提示词
```

**配置优先级**（从高到低）：
1. `[plugins.daily_summary_plugin.openai]` 中显式配置的字段
2. `[openai]` 全局配置
3. 编码默认值

> **必需项**：需配置全局 `[openai]` 或插件级 `[plugins.daily_summary_plugin.openai]` 中至少一个 `api_key`；同时需配置全局 `[telegram]` 方可发送简报。

## 测试简报发送

```bash
# 先抓取一批说说进队列
uv run qzone-cron run

# 不等到 summary_hour，立即生成并发送
uv run qzone-cron send-summary
```
