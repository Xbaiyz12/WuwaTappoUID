"""GS 控制台中的重试上限。"""

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.gs_config import StringConfig
from gsuid_core.utils.plugins_config.models import GsBoolConfig, GsIntConfig, GsStrConfig

settings = StringConfig(
    "WuwaTappoUID",
    get_res_path("WuwaTappoUID") / "config.json",
    {
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


def retry_limit() -> int:
    return max(1, settings.get_config("retry_limit").data)
