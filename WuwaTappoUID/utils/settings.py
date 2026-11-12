"""GS 控制台中的插件配置。"""

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.gs_config import StringConfig
from gsuid_core.utils.plugins_config.models import GsBoolConfig, GsIntConfig, GsStrConfig

from .schema import WuwaError

settings = StringConfig(
    "WuwaTappoUID",
    get_res_path("WuwaTappoUID") / "config.json",
    {
        "bot_name": GsStrConfig(
            title="图片页脚 Bot 名称",
            desc="所有图片页脚中 Bot：后显示的名称，保存后新生成的图片立即生效。",
            data="飞行雪绒",
        ),
        "max_click_increment": GsIntConfig(
            title="单次增加次数上限",
            desc="wt增加次数单次允许增加的最大值，须为正整数；保存后生效。自动补次数仍为每次10000。",
            data=1_000_000,
        ),
        "chest_task_hours": GsIntConfig(
            title="宝箱任务时长（小时）",
            desc="wt宝箱新建任务及每次续期增加的小时数，须为正整数；已有任务到期时间不变。",
            data=24,
        ),
        "record_retention_days": GsIntConfig(
            title="原始记录保留天数",
            desc=(
                "正整数，默认7天，含今天。过期成功开箱归档后删除明细，已完成通知和合成流水同步清理；"
                "保留近30天按日统计及长期累计。保存后下一轮生效，已归档明细不会恢复。"
            ),
            data=7,
        ),
        "login_public_url": GsStrConfig(
            title="登录页面地址",
            desc=(
                "浏览器可访问的地址，可省略协议（默认 https，纯 http 服务请显式写 http://）；"
                "本地模式留空使用 GsCore HOST/PORT，也可填写反代或隧道地址；外置模式必填 ww-login 地址。"
            ),
            data="",
        ),
        "login_use_local": GsBoolConfig(
            title="使用本地登录页面",
            desc="默认开启，使用 GsCore 的登录页；关闭后通过 ww-login 创建会话并主动轮询，无需公网开放 GsCore。",
            data=True,
        ),
        "login_send_qrcode": GsBoolConfig(
            title="发送登录二维码图片",
            desc="默认关闭，发送文字登录链接；开启后发送二维码图片，使用浏览器扫描。本地和外置登录均生效。",
            data=False,
        ),
        "retry_limit": GsIntConfig(
            title="后台连续异常阈值",
            desc="持续后台异常达到此次数后停止重复告警；开启主人告警时发送私聊，不封禁用户命令。",
            data=3,
            max_value=10,
        ),
        "master_alerts_enabled": GsBoolConfig(
            title="发送主人后台异常告警",
            desc="默认开启。关闭后不发送主人私聊；后台异常计数、处理暂停和日志仍按原规则执行。",
            data=True,
        ),
    },
)


def positive_int(key: str) -> int:
    config = settings.get_config(key)
    if config.data < 1:
        raise WuwaError(f"后台插件配置“{config.title}”须为正整数，请主人修改后重试。")
    return config.data


def bot_name() -> str:
    return settings.get_config("bot_name").data


def max_click_increment() -> int:
    return positive_int("max_click_increment")


def chest_task_hours() -> int:
    return positive_int("chest_task_hours")


def record_retention_days() -> int:
    return positive_int("record_retention_days")


def retry_limit() -> int:
    return max(1, settings.get_config("retry_limit").data)


def master_alerts_enabled() -> bool:
    return settings.get_config("master_alerts_enabled").data
