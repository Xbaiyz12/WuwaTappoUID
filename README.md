# WuwaTappoUID

WuwaTappo（鸣潮桌宠）的 GsCore 插件，支持账号管理、背包与图鉴查询、自动开箱和合成。统一使用 `wt` 前缀。

## 安装

需要 Python 3.11+、GsCore 和已连接的 Bot 适配器。在 GsCore 项目目录执行：

```bash
git clone https://github.com/gufei233/WuwaTappoUID.git gsuid_core/plugins/WuwaTappoUID
```

保留两层同名目录，重启 GsCore 即可。依赖由 GsCore 自动安装；关闭自动安装时，请在 GsCore 环境中安装 `requirements.txt`。

## 常用命令

| 命令 | 功能 |
| --- | --- |
| `wt登录` | 浏览器登录国服，链接5分钟内有效 |
| `wt添加token TOKEN` | 仅私聊，使用已有 token 绑定账号 |
| `wt查看` / `wt切换 [1]` / `wt删除 [1]` | 多账号管理 |
| `wt信息` / `wt次数` | 查看账号、冷却、任务和点击数 |
| `wt背包 [角色/配件/表情]` / `wt图鉴 [角色/配件/表情]` | 不带参数查看全部，带参数只查看对应分类 |
| `wt宝箱` | 按后台配置的时长开启或续期，默认24小时；剩余不超过1小时可续期，并提醒一次 |
| `wt暂停` / `wt恢复` / `wt停止` | 暂停计时、恢复登录与任务、停止任务 |
| `wt订阅` / `wt取消订阅` | 成功开箱10次后通知 / 任务结束汇总 |
| `wt增加次数 N` | 增加游戏点击数，单次上限由后台配置，默认1,000,000 |
| `wt合成` / `wt合成 同阶` | 合成多余物品，每种留1件，默认升阶 |
| `wt更新资源` | 更新名称与素材 |
| `wt帮助` | 查看全部命令与说明 |

手动及自动增加次数提交前，会与同账号上次实际网络请求间隔至少30秒；遇到80013按30秒间隔最多重试3次。没有鸣潮角色的库洛账号也支持 `wt登录`，显示“漂泊者”。

`wt登录` 会保存本人SDK恢复凭据及官方返回的有效期。平台登录态失效后，SDK凭据仍可用时可发送 `wt恢复` 免验证码重新登录，并恢复原有未到期任务；无任务时只恢复账号登录。重新登录会使同账号其他客户端的旧登录态失效。SDK凭据缺少或过期时仍需 `wt登录` 或 `wt添加token`，插件不在后台自动重登。0.1.43起本地和外置登录采用WW式会话页面，提交后弹出成功提示并在5秒后跳转项目主页，绑定结果由机器人通知；外置登录需同时更新ww-login至0.3.2。

主人在插件配置中可填写 `login_public_url`（浏览器可访问的HTTP/HTTPS地址）。默认使用 GsCore 登录页，地址留空时按 GsCore HOST/PORT 生成，支持本机、局域网、VPN或隧道访问，不要求公网；关闭 `login_use_local` 后使用 [ww-login](https://github.com/gufei233/ww-login) 外置登录，此时必须填写ww-login地址，GsCore 无需公网入口。配置方式见管理说明。CUID仅用于账号标识，不再支持凭CUID登录。请仅绑定本人或已获授权的账号，token 仅通过私聊添加。本地登录页会显示脱敏的绑定对象，请确认是本人账号。token过期暂停的有效任务在登录或添加token后自动恢复；已到期任务自动收尾汇总，手动暂停仍需 `wt恢复`。任务和通知会在重启后恢复。开箱汇总每页最多40件，多页合并转发。

[帮助示例](docs/help.png) · [管理说明](docs/ADMIN.md) · [合成说明](docs/SYNTHESIS.md) · [更新记录](CHANGELOG.md)

## 致谢

登录交互参考 [XutheringWavesUID](https://github.com/Loping151/XutheringWavesUID)。感谢 [XWUIDCollection](https://github.com/Loping151/XWUIDCollection) 提供图片布局和素材参考。素材来源详见 [素材说明](WuwaTappoUID/assets/tappo/ATTRIBUTION.md)。

## 免责声明

所有与《鸣潮》相关的角色形象、素材版权均归库洛游戏（Kuro Games）所有。
