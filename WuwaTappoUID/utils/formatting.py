"""纯文字消息。"""

import math
import time
from datetime import datetime, timedelta, timezone

from .database import ChestTask
from .resources import Resources
from .schema import SIDE_NAMES, Item, OpenResult, Side, UserInfo

CHINA = timezone(timedelta(hours=8))
PAUSE_REASONS = {"manual": "手动暂停", "auth": "登录态失效", "failure": "连续失败"}


def mask_cuid(cuid: str | int) -> str:
    value = str(cuid)
    if len(value) <= 2:
        return "*" * len(value)
    visible = 3 if len(value) > 6 else 1
    return value[:visible] + "*" * (len(value) - visible * 2) + value[-visible:]


def date_text(timestamp: float) -> str:
    if timestamp <= 0:
        return "已可开启"
    return datetime.fromtimestamp(timestamp, CHINA).strftime("%m-%d %H:%M:%S（北京时间）")


def remaining_text(seconds: float) -> str:
    seconds = max(0, math.ceil(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}小时{minutes}分{seconds}秒"


def counts(info: UserInfo) -> str:
    return f"累计点击：{info.total_click_count}\n已消耗点击：{info.total_click_consume}\n剩余可用点击：{info.remaining}"


def account_text(info: UserInfo, domestic: int) -> str:
    now = time.time()
    cooldowns = []
    for side in ("left", "right"):
        stamp = info.cooldown(side)
        cooldowns.append(f"{SIDE_NAMES[side]}宝箱：" + (date_text(stamp) if stamp > now else "已可开启"))
    region = f"CUID：{mask_cuid(info.cuid)}（{'国服' if domestic else '海外'}）"
    uid = [f"UID：{info.player_id}"] if str(info.player_id) not in ("", "0") else []
    return "\n".join([f"账号昵称：{info.display_name}", *uid, region, counts(info), *cooldowns])


def task_text(task: ChestTask | None) -> str:
    now = time.time()
    if task is None or task.status not in ("active", "paused"):
        return "自动开箱：未开启"
    if task.remaining(now) <= 0:
        return "自动开箱：本轮已到期，发送 wt宝箱 开启新一轮"
    state = "运行中" if task.status == "active" else f"已暂停（{PAUSE_REASONS.get(task.pause_reason, '已暂停')}）"
    deadline = "恢复后重新计算" if task.timer_paused else date_text(task.expires_at)
    return (
        f"自动开箱：{state}\n剩余：{remaining_text(task.remaining(now))}\n"
        f"到期：{deadline}\n本轮成功：{task.successes} 个"
    )


def chest_text(side: Side, result: OpenResult, info: UserInfo, resources: Resources, supplement: str) -> str:
    return (
        f"{SIDE_NAMES[side]}宝箱开启成功\n"
        f"物品：{resources.name(result.asset_name, result.asset_id)}\n"
        f"稀有度：{result.rarity_type}\n"
        f"消耗点击：{result.click_consume}\n"
        f"剩余点击：{info.remaining}\n"
        f"下次可开：{date_text(info.cooldown(side))}" + (f"\n{supplement}" if supplement else "")
    )


def split_lines(lines: list[str], limit: int = 1400) -> list[str]:
    parts: list[str] = []
    current = ""
    for line in lines:
        if current and len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        parts.append(current)
    return parts


def bag_text(items: list[Item], resources: Resources) -> list[str]:
    if not items:
        return ["背包为空。"]
    groups: dict[str, list[Item]] = {}
    names = {"character_skin": "角色皮肤", "accessory": "配件", "emote": "表情"}
    for item in items:
        groups.setdefault(item.asset_type, []).append(item)
    lines = [f"背包：共 {len(items)} 项"]
    for kind, entries in groups.items():
        lines.append(f"【{names.get(kind, kind)}】")
        for item in entries:
            rarity = item.rarity if item.rarity is not None else "未知"
            lines.append(f"{resources.name(item.asset_name, item.asset_id)} ×{item.quantity}［稀有度：{rarity}］")
    return split_lines(lines)
