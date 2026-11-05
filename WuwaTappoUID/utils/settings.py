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
        "login_public_url": GsStrConfig(
            title="登录页面公网地址",
            desc="HTTPS 登录入口，如 https://login.example.com；本地模式反代 GsCore，外置模式填写 ww-login 地址。",
            data="",
        ),
        "login_use_local": GsBoolConfig(
            title="使用本地登录页面",
            desc="默认开启，使用 GsCore 的登录页；关闭后通过 ww-login 创建会话并主动轮询，无需公网开放 GsCore。",
            data=True,
        ),
        "retry_limit": GsIntConfig(
            title="后台连续异常阈值",
            desc="持续后台异常达到此次数后提醒主人并暂停异常处理；不封禁用户或账号命令。",
            data=3,
            max_value=10,
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


def retry_limit() -> int:
    return max(1, settings.get_config("retry_limit").data)
