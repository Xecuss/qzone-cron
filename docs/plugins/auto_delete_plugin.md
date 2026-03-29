# auto_delete_plugin — 自动删除说说

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
