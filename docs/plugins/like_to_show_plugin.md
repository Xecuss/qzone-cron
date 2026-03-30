# like_to_show_plugin — 达到条件后公布第二段内容

检测**自己**发布的说说中的嵌入指令，在满足条件（点赞阈值 或 延迟时间）后，自动将预先提供的"第二段内容"以指定方式发布出去。

本插件提供两条指令：

- `/like_to_show`：当说说点赞数达到阈值时触发
- `/delay_to_show`：当距说说登记时起经过指定时间后触发

> 依赖全局 `[telegram]` 配置（用于发送记录通知和接收 reply）。

---

## 指令一：`/like_to_show`

```
/like_to_show <like_count> [edit_method]
```

- `like_count`：整数，点赞阈值
- `edit_method`：触发后的发布方式（可选，缺省使用配置文件中的 `default_edit_method`）

### 示例

```
今天吃了一顿特别好吃的饭 /like_to_show 10
```

当该说说获得 10 个点赞后，以默认方式（`comment`）发布第二段内容。也可在指令中直接指定方式：

```
神秘公告 /like_to_show 50 append
```

达到 50 赞后，删除原说说，将原内容（去除指令）与第二段内容合并后发布新说说。

---

## 指令二：`/delay_to_show`

```
/delay_to_show <delay_time> [edit_method]
```

- `delay_time`：延迟时长，从说说被登记时起计算，到期后触发
- `edit_method`：与 `like_to_show` 完全相同

`delay_time` 支持以下格式：

| 写法 | 含义 |
|---|---|
| `30m` / `30min` | 30 分钟 |
| `2h` / `2hr` / `2hours` | 2 小时 |
| `1d` / `1day` | 1 天 |
| 纯数字（如 `3`）| 默认为小时 |

### 示例

```
留言给 48 小时后的自己 /delay_to_show 48h
```

48 小时后，以默认方式发布第二段内容。

```
明天揭晓答案 /delay_to_show 1d delete
```

1 天后，删除原说说，将第二段内容单独发布为新说说。

---

## 公共说明

### `edit_method` 取值

| `edit_method` | 行为 |
|---|---|
| `comment` | 在原说说评论区发布第二段内容（原说说保留）|
| `new` | 将第二段内容发布为新说说（原说说保留）|
| `append` | 删除原说说，将去除指令的原始内容与第二段内容合并后发新说说 |
| `delete` | 删除原说说，仅将第二段内容作为新说说单独发布 |

### 工作流程

1. 扫描到含指令的自己的说说后，通过 Telegram Bot 发送通知（含说说预览和触发条件）
2. 用户 **reply** 该 Telegram 消息来提供"第二段内容"，插件自动匹配并记录
3. 每次 cron 运行时检查触发条件：
   - 条件满足 + 已收到第二段内容 → 执行发布
   - 条件满足 + 尚未收到第二段内容 → 发送 Telegram 警告（仅一次），等待内容
   - 条件未满足 → 继续等待

状态分别持久化于 `like_to_show_state.json` 和 `delay_to_show_state.json`。

---

## 配置示例

```toml
[plugins.like_to_show_plugin]
enabled = true
# 默认发布方式（可在说说指令中逐条覆盖），适用于两条指令
# 可选值：comment | new | append | delete（默认 comment）
default_edit_method = "comment"
```
