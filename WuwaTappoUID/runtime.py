"""插件生命周期；导入本模块不会调用游戏接口。"""

import asyncio
import time

from gsuid_core.aps import scheduler
from gsuid_core.data_store import get_res_path
from gsuid_core.gss import gss
from gsuid_core.logger import logger
from gsuid_core.server import on_core_shutdown, on_core_start, on_core_start_before
from pydantic import TypeAdapter, ValidationError

from .utils.api import WuwaAPI
from .utils.database import Account, AccountLink, Binding, Store, migrate_notice_columns
from .utils.resources import Resources
from .utils.schema import DataError, SessionImport, token_expiry
from .utils.service import Service, account_id, identity

ROOT = get_res_path("WuwaTappoUID")
api = WuwaAPI()
resources = Resources(api, ROOT)
service = Service(api, resources)
NOTICE_JOB = "WuwaTappoUID:notice"
RESOURCE_JOB = "WuwaTappoUID:resources"


async def import_sessions() -> None:
    path = ROOT / "session-import.json"
    if not path.exists():
        return
    try:
        entries = TypeAdapter(list[SessionImport]).validate_json(await asyncio.to_thread(path.read_text, "utf-8"))
    except (OSError, ValidationError):
        raise DataError("登录态导入文件无法读取或格式错误；请按模板填写，文件已保留。") from None
    # 先验证全部登录态；任一无效时不写入任何绑定。
    verified: list[Account] = []
    for entry in entries:
        info = await api.me(entry.token)
        if str(info.cuid) != entry.cuid:
            raise DataError("导入的登录态与 CUID 不一致；文件已保留。")
        verified.append(
            Account(
                id=account_id(entry.cuid, entry.domestic),
                cuid=entry.cuid,
                domestic=entry.domestic,
                token=entry.token,
                info=info.model_dump_json(),
                verified_at=time.time(),
                token_expires_at=token_expiry(entry.expire_time),
            )
        )
    for entry, account in zip(entries, verified, strict=True):
        owner = identity(entry.platform, entry.user_id)
        old_account = await Store.account(account.id)
        if old_account is not None:
            account.subscribed = old_account.subscribed
        await Store.save(
            account,
            Binding(id=owner, platform=entry.platform, user_id=entry.user_id, account_id=account.id),
            AccountLink(owner_id=owner, account_id=account.id),
        )
    # 成功后移除明文导入文件；token 只保存在 GS 数据库中。
    await asyncio.to_thread(path.unlink)
    logger.info(f"[WuwaTappoUID] 已导入 {len(entries)} 个登录态，导入文件已移除。")


@on_core_start_before(priority=0)
async def wt_initialize() -> None:
    # GS 的建表钩子优先级 -90，先补旧表列并迁移账号关联，再恢复任务。
    await migrate_notice_columns()
    await api.start()
    await resources.load()
    await import_sessions()


async def wt_update_resources() -> None:
    # 单次请求有重试；整轮仍失败或图片未下全时，再按间隔补齐。
    for delay in (0, 30, 120, 300):
        if delay:
            await asyncio.sleep(delay)
        if service.closing:
            return
        logger.info(f"[WuwaTappoUID] {await resources.update()}")
        if not resources.incomplete or service.closing:
            return
    logger.warning("[WuwaTappoUID] 资源仍未全部更新，已有资源继续可用；可发送 wt更新资源 补齐。")


@on_core_start
async def wt_start() -> None:
    await service.restore()
    scheduler.add_job(
        service.maintain,
        "interval",
        seconds=60,
        id=NOTICE_JOB,
        name="WuwaTappo 待发通知与任务巡检",
        replace_existing=True,
    )
    scheduler.add_job(
        wt_update_resources,
        "date",
        id=RESOURCE_JOB,
        name="WuwaTappo 名称与图库增量更新",
        replace_existing=True,
        misfire_grace_time=None,
    )


@gss.on_bot_connect
async def wt_bot_connected() -> None:
    await service.flush_notices()


@on_core_shutdown(priority=-10)
async def wt_shutdown() -> None:
    service.closing = True
    for job in scheduler.get_jobs():
        if job.id.startswith("WuwaTappoUID:"):
            job.remove()
    await resources.close()
    await api.close()
