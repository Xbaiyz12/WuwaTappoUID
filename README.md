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
| `wt绑定 CUID` / `wt绑定 CUID 海外` | 绑定账号 |
| `wt添加token TOKEN` | 使用已有 token 绑定账号 |
| `wt查看` / `wt切换 [1]` / `wt删除 [1]` | 多账号管理 |
| `wt信息` / `wt次数` | 查看账号、冷却、任务和点击数 |
| `wt背包` / `wt图鉴 角色` | 查看背包和图鉴；图鉴也支持配件、表情 |
| `wt宝箱` | 开启24小时任务；剩余不超过1小时可续期24小时，并提醒一次 |
| `wt暂停` / `wt恢复` / `wt停止` | 管理任务，暂停时冻结计时 |
| `wt订阅` / `wt取消订阅` | 成功开箱10次后通知 / 任务结束汇总 |
| `wt增加次数 N` | 增加游戏点击数 |
| `wt合成` / `wt合成 同阶` | 合成多余物品，每种留1件，默认升阶 |
| `wt更新资源` | 更新名称与素材 |
| `wt帮助` | 查看全部命令与说明 |

CUID 是库洛账号标识，不是鸣潮角色 UID。请仅绑定本人或已获授权的账号，token 建议通过私聊发送。任务和通知会在重启后恢复。

[帮助示例](docs/help.png) · [管理说明](docs/ADMIN.md) · [合成说明](docs/SYNTHESIS.md) · [更新记录](CHANGELOG.md)

## 致谢

感谢 [XWUIDCollection](https://github.com/Loping151/XWUIDCollection) 提供图片布局和素材参考。素材来源详见 [素材说明](WuwaTappoUID/assets/tappo/ATTRIBUTION.md)。

## 免责声明

所有与《鸣潮》相关的角色形象、素材版权均归库洛游戏（Kuro Games）所有。
