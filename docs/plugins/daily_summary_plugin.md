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

## 测试简报发送

```bash
# 先抓取一批说说进队列
uv run qzone-cron run

# 不等到 summary_hour，立即生成并发送
uv run qzone-cron send-summary
```
