"""GS 控制台中的重试上限。"""

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.gs_config import StringConfig
from gsuid_core.utils.plugins_config.models import GsIntConfig

settings = StringConfig(
    "WuwaTappoUID",
    get_res_path("WuwaTappoUID") / "config.json",
    {
        "retry_limit": GsIntConfig(
            title="连续失败次数上限",
            desc="后台检查、token续期、结果核对、通知和主人告警连续失败达到此次数后停止自动重试。",
            data=3,
            max_value=10,
        ),
    },
)


def retry_limit() -> int:
    return max(1, settings.get_config("retry_limit").data)
