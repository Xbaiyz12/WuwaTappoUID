"""wt 命令；显式请求才执行，不注册 AI 工具。"""

import asyncio
import re

from gsuid_core.bot import Bot
from gsuid_core.help.utils import register_help
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import MessageSegment
from gsuid_core.sv import SV, get_plugin_available_prefix
from gsuid_core.utils.database.base_models import DatabaseWriteTimeout
from gsuid_core.utils.database.write_gate import WriteGateTimeout
from gsuid_core.utils.image.convert import convert_img
from sqlalchemy.exc import SQLAlchemyError

from ..runtime import resources, service
from ..utils.help_card import help_text, render
from ..utils.schema import AuthenticationError, WuwaError

sv = SV("WuwaTappo账号与宝箱", pm=6)
register_help("WuwaTappoUID", f"{get_plugin_available_prefix('WuwaTappoUID')}帮助")

# 帮助图按前缀缓存：内容固定，管理员改了插件前缀才需要重画。
_help_images: dict[str, bytes] = {}


async def help_image() -> bytes:
    prefix = get_plugin_available_prefix("WuwaTappoUID")
    if prefix not in _help_images:
        _help_images[prefix] = await convert_img(await asyncio.to_thread(render, prefix))
    return _help_images[prefix]


COMMANDS = (
    "添加token",
    "绑定",
    "查看",
    "删除",
    "切换",
    "订阅",
    "取消订阅",
    "信息",
    "背包",
    "图鉴",
    "合成",
    "增加次数",
    "次数",
    "宝箱",
    "暂停",
    "恢复",
    "停止",
    "状态",
    "强制更新资源",
    "更新资源",
)


@sv.on_command((*COMMANDS, "帮助"), block=True)
async def wt_command(bot: Bot, event: Event) -> None:
    # GS 会把命令本身放在 command，把剥离命令后的参数放在 text。
    command = event.command.removeprefix("wt").strip()
    argument = event.text.strip()
    if command == "帮助":
        await bot.send(await help_image())
        return
    try:
        service.require_database()
        if command == "添加token":
            match = re.fullmatch(r"(\S+)(?:\s+(国服|海外))?", argument)
            if match is None:
                raise WuwaError("用法：wt添加token TOKEN，可在末尾加国服或海外；token 前的空格可省。")
            domestic = None if match[2] is None else 0 if match[2] == "海外" else 1
            replies = await service.bind_token(event, match[1], domestic)
        elif command == "绑定":
            match = re.fullmatch(r"([0-9]+)(?:\s+(国服|海外))?", argument)
            if match is None:
                raise WuwaError("用法：wt绑定 CUID，或 wt绑定 CUID 海外。CUID 须为纯数字。")
            replies = await service.bind(event, match[1], 0 if match[2] == "海外" else 1)
        elif command == "切换":
            if argument:
                match = re.fullmatch(r"([0-9]+|\[[0-9]+\])(?:\s+(国服|海外))?", argument)
                if match is None:
                    raise WuwaError("用法：wt切换 [编号] 或 wt切换 CUID；不带参数则切换到下一个账号。")
                domestic = None if match[2] is None else 0 if match[2] == "海外" else 1
                replies = [await service.switch(event, match[1], domestic)]
            else:
                replies = [await service.switch(event)]
        elif command == "删除":
            if argument:
                match = re.fullmatch(r"([0-9]+|\[[0-9]+\])(?:\s+(国服|海外))?", argument)
                if match is None:
                    raise WuwaError("用法：wt删除 [编号] 或 wt删除 CUID；不带参数删除当前绑定。")
                domestic = None if match[2] is None else 0 if match[2] == "海外" else 1
                replies = [await service.delete_account(event, match[1], domestic)]
            else:
                replies = [await service.delete_account(event)]
        elif command == "增加次数":
            if re.fullmatch(r"[0-9]+", argument) is None or int(argument) <= 0:
                raise WuwaError("请输入正整数，例如 wt增加次数1000 或 wt增加次数 1000。")
            replies = await service.increase(event, int(argument))
        elif command == "合成":
            if argument not in ("", "同阶", "升阶"):
                raise WuwaError("用法：wt合成（默认升阶）、wt合成 升阶，或 wt合成 同阶。")
            mode = argument or "升阶"
            await bot.send(f"开始{mode}合成多余物品：每种保留 1 件，本轮只进行{mode}合成。")
            replies = await service.synthesize(event, same_level=mode == "同阶")
        elif command in ("背包", "图鉴"):
            kinds = {"角色": "character", "配件": "accessory", "表情": "emote"}
            if argument and argument not in kinds:
                raise WuwaError(f"用法：wt{command} [角色/配件/表情]；不带参数查看全部分类。")
            kind = kinds[argument] if argument else None
            replies = (
                await service.query(event, bag=True, kind=kind)
                if command == "背包"
                else await service.atlas(event, kind)
            )
        elif argument:
            raise WuwaError("这条命令不需要参数，可发送 wt帮助 查看用法。")
        elif command == "查看":
            replies = [await service.view_accounts(event)]
        elif command in ("订阅", "取消订阅"):
            replies = [await service.subscribe(event, command == "订阅")]
        elif command == "信息":
            replies = await service.info(event)
        elif command == "次数":
            replies = await service.query(event)
        elif command == "宝箱":
            replies = await service.start_or_renew(event)
        elif command == "暂停":
            replies = [await service.set_paused(event, True)]
        elif command == "恢复":
            replies = [await service.set_paused(event, False)]
        elif command == "停止":
            replies = [await service.stop(event)]
        elif command == "状态":
            replies = [await service.status(event)]
        elif command in ("更新资源", "强制更新资源"):
            force = command == "强制更新资源"
            if force and event.user_pm > 1:
                raise WuwaError("强制更新资源仅限 Bot 管理员使用。")
            await bot.send("开始更新中文名称与物品图库，完成后会回复统计结果。")
            replies = [await resources.update(force=force)]
        else:
            replies = [help_text(get_plugin_available_prefix("WuwaTappoUID"))]
    except AuthenticationError as error:
        await service.pause_after_auth_failure(event)
        replies = [str(error)]
    except WuwaError as error:
        replies = [str(error)]
    except SQLAlchemyError as error:
        service.disable_database(error)
        replies = [service.database_error]
    except (DatabaseWriteTimeout, WriteGateTimeout):
        logger.exception("[WuwaTappoUID] 命令的数据库操作失败")
        replies = ["数据库未能完成本次操作。请稍后用 wt次数 或 wt宝箱 确认实际状态后再操作。"]
    segments = [
        MessageSegment.image(reply) if isinstance(reply, bytes) else MessageSegment.text(reply) for reply in replies
    ]
    messages = [MessageSegment.node(segments)] if sum(isinstance(reply, bytes) for reply in replies) > 1 else segments
    if event.user_type == "group" and command not in ("背包", "图鉴", "查看"):
        messages = [MessageSegment.at(event.user_id), MessageSegment.text("\n"), *messages]
    await bot.send(messages)
