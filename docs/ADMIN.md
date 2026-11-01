# 管理说明

`wt状态`、`wt强制更新资源` 仅限 Bot 主人或超级用户使用。

## 导入已有登录态

一般直接私聊发送 `wt添加token TOKEN` 即可。批量导入时：

1. 首次启动插件后停止 GsCore。
2. 将 [session-import.example.json](session-import.example.json) 复制到插件资源目录，改名为 `session-import.json`。默认目录是 GsCore 下的 `data/WuwaTappoUID/`。
3. 替换全部占位内容：`platform` 为适配器的 `Event.bot_id`，`user_id` 为聊天用户 ID，`cuid` 为库洛账号标识，`domestic` 为 `1` 国服或 `0` 海外，`token` 不带 `Bearer `。
4. 启动 GsCore。插件验证账号后保存绑定，并删除已成功导入的文件；验证失败会保留文件并报错。

导入不会自动开启宝箱任务。登录态保存在 GsCore 数据库中，不要将真实 token 或数据库提交到仓库。

## 数据与恢复

- 数据库和插件资源目录由 GsCore 管理；备份需包含数据库与资源目录。
- 重启会恢复原任务，手动暂停任务继续冻结计时。通过 `wt恢复` 恢复，不直接修改数据库状态。
- 通知发送失败会保留并补发；不要重放开箱请求来补通知。
- `wt更新资源` 补齐素材；`wt强制更新资源` 覆盖更新素材，不清空账号或任务。
