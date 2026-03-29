# auto_like_plugin — 自动点赞

模拟真实用户点赞习惯：通过大模型过滤说说（排除情绪低落、自我攻击类内容）后入队，随机延迟一段时间后批量点赞，并自动回避凌晨等不适宜操作的时段。

## 工作流程

1. 每次 `run` 抓取到好友说说后，批量发给大模型判断是否应该点赞
2. 通过筛选的说说入待点赞队列，并预计算一个随机激活时间
3. cron 运行到激活时间后，每次取若干条点赞（条数可配置），每两条之间随机等待 5-10 秒
4. 队列清空后重新预计算下一次激活时间
5. 激活时间若落在禁止时段（如凌晨）内，自动顺延到允许的小时

## 配置示例

### 基础配置（推荐）

在顶层全局 `[openai]` 配置 LLM 参数，插件会自动使用：

```toml
[openai]
api_key = "sk-..."
base_url = "https://api.openai.com/v1"
model = "gpt-4o-mini"

[plugins.auto_like_plugin]
enabled = true
likes_per_cycle = 3           # 每次激活处理的条数
like_interval_min = 5.0       # 两次点赞之间的最短间隔（秒）
like_interval_max = 10.0      # 两次点赞之间的最长间隔（秒）
activation_delay_min = 30     # 激活延迟最短时间（分钟）
activation_delay_max = 180    # 激活延迟最长时间（分钟）
forbidden_hours = [0,1,2,3,4,5,6]  # 禁止激活的小时（本地时间 0-23）
```

### 进阶配置（插件级覆盖）

若需与其他插件使用不同的模型，可在插件级配置中覆盖特定字段：

```toml
[plugins.auto_like_plugin]
enabled = true
likes_per_cycle = 3
like_interval_min = 5.0
like_interval_max = 10.0
activation_delay_min = 30
activation_delay_max = 180
forbidden_hours = [0,1,2,3,4,5,6]

# 可选：覆盖全局 [openai] 的字段
[plugins.auto_like_plugin.openai]
model = "gpt-4"  # 只覆盖模型，api_key 和 base_url 仍沿用全局配置
# json_mode = true    # 开启 response_format: json_object（仅 OpenAI 官方等部分模型支持）
# system_prompt = "..."  # 可选，覆盖内置的点赞判断提示词
```

**配置优先级**（从高到低）：
1. `[plugins.auto_like_plugin.openai]` 中显式配置的字段
2. `[openai]` 全局配置
3. 编码默认值

> **提示**：不配置任何 `[openai]` 或 `[plugins.auto_like_plugin.openai]` 时，所有好友说说均会进入点赞队列（无 LLM 过滤）。
