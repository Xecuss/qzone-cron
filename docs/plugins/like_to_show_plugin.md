# like_to_show_plugin — 达到点赞阈值后公布第二段内容

检测**自己**发布的说说中的 `/like_to_show` 指令，当该说说点赞数超过阈值后，自动将预先提供的"第二段内容"以指定方式发布出去。

> 依赖全局 `[telegram]` 配置（用于发送记录通知和接收 reply）。

## 指令格式

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

## 工作流程

1. 扫描到含 `/like_to_show` 指令的自己的说说后，通过 Telegram Bot 发送通知（含说说预览和点赞阈值）
2. 用户 **reply** 该 Telegram 消息来提供"第二段内容"，插件自动匹配并记录
3. 每次 cron 运行时检查点赞数是否达到阈值：
   - 达到阈值 + 已收到第二段内容 → 执行发布
   - 达到阈值 + 尚未收到第二段内容 → 发送 Telegram 警告（仅一次），等待内容
   - 未达到阈值 → 继续等待

## 配置示例

```toml
[plugins.like_to_show_plugin]
enabled = true
# 默认发布方式（可在说说指令中逐条覆盖）
# 可选值：comment | new | append | delete（默认 comment）
default_edit_method = "comment"
```

## 示例说说

```
今天吃了一顿特别好吃的饭 /like_to_show 10
```

当该说说获得 10 个点赞后，插件会将你提前 reply 到 TG 通知的内容以 `comment` 方式（或配置的默认方式）发布出去。也可在指令中直接指定方式：

```
神秘公告 /like_to_show 50 append
```

达到 50 赞后，删除原说说，将原内容（去除指令）与第二段内容合并后发布新说说。
