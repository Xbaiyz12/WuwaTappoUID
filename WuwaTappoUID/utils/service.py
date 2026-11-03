"""账号锁、24 小时任务、开箱流水与通知提交。"""

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone

from gsuid_core.aps import scheduler
from gsuid_core.gss import gss
from gsuid_core.logger import logger
from gsuid_core.models import Event, Message
from gsuid_core.segment import MessageSegment
from gsuid_core.subscribe import gs_subscribe
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from .api import WuwaAPI, with_read_cache
from .cards import Cards, ResultContext
from .database import Account, AccountLink, Binding, ChestRecord, ChestTask, Notice, Store, SynthesisRecord, new_id
from .failures import Failures
from .formatting import date_text, mask_cuid, task_text
from .resources import Resources
from .schema import (
    AUTO_CLICKS,
    NET_SERVER_IDS,
    RENEW_WINDOW,
    SETTLE_SECONDS,
    SIDE_NAMES,
    TASK_SECONDS,
    TOKEN_LIFETIME,
    TOKEN_RENEW_RETRY,
    TOKEN_RENEW_WINDOW,
    ApiError,
    AuthenticationError,
    DatabaseSchemaError,
    DataError,
    Expired,
    Item,
    LoginData,
    NetworkError,
    RoleInfo,
    Side,
    SynthesisResult,
    UserInfo,
    WuwaError,
    token_expiry,
)
from .settings import retry_limit
from .synthesis import highest_tiers, next_recipe

NOTICE_CACHE_BYTES = 32 * 1024 * 1024


@dataclass
class CachedNoticeImage:
    revision: int
    body: bytes


def identity(platform: str, user_id: str) -> str:
    return hashlib.sha256(json.dumps([platform, user_id], ensure_ascii=False).encode()).hexdigest()


def account_id(cuid: str, domestic: int) -> str:
    return f"{domestic}:{cuid}"


class Service:
    def __init__(self, api: WuwaAPI, resources: Resources) -> None:
        self.api = api
        self.resources = resources
        self.cards = Cards(api, resources)
        self.failures = Failures(resources.root)
        self._accounts: dict[str, asyncio.Lock] = {}
        self._users: dict[str, asyncio.Lock] = {}
        self._push_lock = asyncio.Lock()
        self._notice_images: OrderedDict[str, CachedNoticeImage] = OrderedDict()
        self._notice_image_bytes = 0
        self.closing = False
        self.database_ready = False
        self.database_error = "插件尚未完成数据库初始化，请稍后重试。"

    def require_database(self) -> None:
        if not self.database_ready:
            raise WuwaError(self.database_error)

    async def disable_database(self, error: SQLAlchemyError | DatabaseSchemaError) -> None:
        message = "插件数据库初始化或操作失败，后台任务已停止；请更新插件并重启 GsCore。"
        if self.database_error == message:
            return
        self.database_ready = False
        self.database_error = message
        for job in scheduler.get_jobs():
            if job.id.startswith("WuwaTappoUID:chest:") or job.id == "WuwaTappoUID:notice":
                job.remove()
        # 不输出 ORM SQL 或绑定参数，其中可能含 token。
        detail = (
            str(error.orig)
            if isinstance(error, DBAPIError)
            else str(error)
            if isinstance(error, DatabaseSchemaError)
            else type(error).__name__
        )
        logger.error(f"[WuwaTappoUID] {message} {type(error).__name__}：{detail}")
        await self.failures.fail("database", "数据库故障", detail, limit=1)
        await self.failures.flush_alerts()

    @staticmethod
    def error_detail(error: Exception) -> str:
        if isinstance(error, (ApiError, NetworkError)):
            return error.details
        return str(error) if isinstance(error, WuwaError) else type(error).__name__

    async def task_failure(self, task: ChestTask, error: Exception) -> None:
        already_paused = task.status == "paused"
        failure = await self.failures.fail(
            "check:" + task.account_id,
            "开箱请求重试耗尽" if already_paused else "后台开箱检查",
            self.error_detail(error),
            task.account_id,
            limit=1 if already_paused else None,
        )
        task.failures = failure.attempts
        if failure.blocked:
            if already_paused:
                # 开箱重试耗尽的通知已由 open_side 保存，只补主人告警。
                await Store.save(task)
                return
            task.status = "paused"
            task.pause_reason = "failure"
            await Store.save(
                task,
                Notice(
                    subscription_id=task.subscription_id,
                    account_id=task.account_id,
                    text=f"后台检查连续失败达到上限（{failure.attempts}次），已暂停并生成主人告警。"
                    f"\n{self.error_detail(error)}\n处理后可用 wt恢复 重置重试计数。",
                ),
            )
        else:
            await Store.save(task)

    async def command_failure(self, event: Event, command: str, error: Exception, failure_key: str) -> None:
        await self.failures.fail(
            failure_key,
            f"命令 wt{command}（用户 {event.user_id}）",
            self.error_detail(error),
        )

    def account_lock(self, key: str) -> asyncio.Lock:
        return self._accounts.setdefault(key, asyncio.Lock())

    def user_lock(self, key: str) -> asyncio.Lock:
        return self._users.setdefault(key, asyncio.Lock())

    async def bound(self, event: Event) -> tuple[Binding, Account]:
        binding = await Store.binding(identity(event.bot_id, event.user_id))
        if binding is None:
            raise WuwaError("请先发送 wt绑定 CUID 或 wt添加token TOKEN（海外账号加上海外）。")
        account = await Store.account(binding.account_id)
        if account is None:
            raise WuwaError("绑定的账号数据缺失，请重新绑定。")
        return binding, account

    async def account_notice(self, account: Account, text: str) -> None:
        task = await Store.task(account.id)
        if task is not None and task.status in ("active", "paused"):
            await Store.save(Notice(subscription_id=task.subscription_id, account_id=account.id, text=text))

    async def refresh(self, account: Account, *, fresh: bool = False) -> UserInfo:
        # 调用方进入账号锁前可能已读取绑定，锁内以数据库中最新 token 为准。
        saved = await Store.account(account.id)
        if saved is not None:
            account.token = saved.token
            account.subscribed = saved.subscribed
            account.token_expires_at = saved.token_expires_at
            account.token_renew_after = saved.token_renew_after
            account.token_state = saved.token_state
            account.info = saved.info
            account.verified_at = saved.verified_at
        pending = account.token_state == "pending"
        if account.token_state == "ready":
            await self.renew_token_if_needed(account)
        try:
            info = await self.api.me(account.token, fresh=fresh)
        except ApiError as error:
            if not error.is_auth:
                raise
            # 新取得的 token 只验证，不在同一失败周期中再次登录。
            logger.info(f"[WuwaTappoUID] CUID {mask_cuid(account.cuid)}：账号查询鉴权失败，{error.details}。")
            if account.token_state != "ready":
                raise AuthenticationError(
                    "当前会话验证未通过，已保留 token 并停止操作，不会重复登录；请稍后查询或添加有效 token。"
                ) from None
            info = await self.renew_session(account)
        if str(info.cuid) != account.cuid:
            raise DataError("服务端返回的 CUID 与绑定账号不一致，已停止本次操作。")
        account.info = info.model_dump_json()
        account.verified_at = time.time()
        account.token_state = "ready"
        await Store.save(account)
        await self.failures.success("auth:" + account.id)
        if pending:
            await self.account_notice(account, "新会话已验证，继续使用已保存的 token。")
        return info

    async def renew_token_if_needed(self, account: Account) -> None:
        now = time.time()
        failure_key = "renew:" + account.id
        if not self.failures.allows(failure_key):
            return
        if account.token_renew_after > now:
            return
        if account.token_expires_at and account.token_expires_at - now >= TOKEN_RENEW_WINDOW:
            return
        try:
            await self.api.renew_token(account.token)
        except WuwaError as error:
            account.token_renew_after = now + TOKEN_RENEW_RETRY
            await Store.save(account)
            reason = self.error_detail(error)
            failure = await self.failures.fail(failure_key, "原token续期", reason, account.id, delay=TOKEN_RENEW_RETRY)
            if failure.blocked:
                await self.account_notice(
                    account,
                    f"原token续期连续失败{failure.attempts}次，已停止自动续期并生成主人告警。"
                    "原token仍会继续查询验证；可用 wt恢复 或添加有效token重置计数。",
                )
            return
        account.token_expires_at = max(now, account.token_expires_at) + TOKEN_LIFETIME
        account.token_renew_after = 0
        await Store.save(account)
        await self.failures.success(failure_key)
        logger.info(f"[WuwaTappoUID] CUID {mask_cuid(account.cuid)}：原 token 续期成功，未重新登录。")

    async def save_login_token(self, account: Account, login: LoginData) -> None:
        # 先保存新 token，期限解析或账号验证失败也不能再恢复数据库中的旧 token。
        account.token = login.token
        account.token_state = "pending"
        account.token_expires_at = 0
        account.token_renew_after = 0
        await Store.save(account)
        await self.failures.success("renew:" + account.id)
        await self.failures.success("auth:" + account.id)
        account.token_expires_at = token_expiry(login.expire_time)
        await Store.save(account)

    async def renew_session(self, account: Account) -> UserInfo:
        # 只有账号查询已拒绝当前会话才进入本函数。
        await self.queue_results(account.id, force=True, reason="账号查询鉴权失败，汇总已开结果")
        player_id, server_id = await self.login_role(account.cuid, account.domestic)
        # 发请求前保存状态。登录结果不明时，下次只验证现有 token，不自动再发登录请求。
        account.token_state = "login_unconfirmed"
        await Store.save(account)
        try:
            login = await self.api.login(account.cuid, account.domestic, player_id, server_id)
        except (ApiError, DataError) as error:
            reason = error.details if isinstance(error, ApiError) else "响应结构不符合接口定义"
            logger.warning(f"[WuwaTappoUID] CUID {mask_cuid(account.cuid)}：登录未完成，阶段 auth/login，{reason}。")
            raise AuthenticationError("登录未完成，已停止操作，不会重复提交登录；请添加有效 token。") from None
        await self.save_login_token(account, login)
        try:
            info = await self.api.me(account.token)
        except (ApiError, DataError) as error:
            reason = error.details if isinstance(error, ApiError) else "响应结构不符合接口定义"
            logger.warning(f"[WuwaTappoUID] CUID {mask_cuid(account.cuid)}：新 token 已保存，阶段 user/me，{reason}。")
            raise AuthenticationError("新 token 已保存但验证未通过；请稍后查询，不会再次自动登录。") from None
        if str(info.cuid) != account.cuid:
            raise DataError("新 token 所属 CUID 与绑定账号不一致，已保存待核对会话并停止操作。")
        logger.info(f"[WuwaTappoUID] CUID {mask_cuid(account.cuid)}：重登成功。")
        await self.account_notice(account, "登录态已失效，已根据 CUID 重新登录。")
        return info

    async def bind(self, event: Event, cuid: str, domestic: int) -> list[str | bytes]:
        owner = identity(event.bot_id, event.user_id)
        target = account_id(cuid, domestic)
        async with self.user_lock(owner):
            async with self.account_lock(target):
                account = await Store.account(target)
                if account is None:
                    player_id, server_id = await self.login_role(cuid, domestic)
                    account = Account(id=target, cuid=cuid, domestic=domestic, token_state="login_unconfirmed")
                    await Store.save(account)
                    login = await self.api.login(cuid, domestic, player_id, server_id)
                    await self.save_login_token(account, login)
                elif account.token_state == "login_unconfirmed" and not account.token:
                    # 用户再次显式绑定无 token 的账号，允许开启一次新的登录尝试。
                    player_id, server_id = await self.login_role(cuid, domestic)
                    login = await self.api.login(cuid, domestic, player_id, server_id)
                    await self.save_login_token(account, login)
                previous_token = account.token
                await self.refresh(account)
                await Store.save(
                    Binding(id=owner, platform=event.bot_id, user_id=event.user_id, account_id=target),
                    AccountLink(owner_id=owner, account_id=target),
                )
                roles, role_note = await self.role_details(account)
                task = await Store.task(account.id)
        await self.flush_notices()
        image = await self.cards.account_summary(account, roles, role_note, task, event)
        return ["绑定成功，已切换到该账号。" + self.refresh_hint(previous_token, account), image]

    async def bind_token(self, event: Event, token: str, domestic: int | None = None) -> list[str | bytes]:
        owner = identity(event.bot_id, event.user_id)
        async with self.user_lock(owner):
            info = await self.api.me(token)
            cuid = str(info.cuid)
            if domestic is None:
                domestic = info.is_domestic
                if domestic is None:
                    domestic = 0 if str(info.server_id) in NET_SERVER_IDS else 1
            target = account_id(cuid, domestic)
            async with self.account_lock(target):
                account = await Store.account(target)
                if account is None:
                    account = Account(id=target, cuid=cuid, domestic=domestic)
                # refresh 会优先使用数据库里的旧 token；新会话验证后直接保存。
                account.token = token
                account.token_state = "ready"
                account.token_expires_at = 0
                account.token_renew_after = 0
                account.info = info.model_dump_json()
                account.verified_at = time.time()
                await Store.save(
                    account,
                    Binding(id=owner, platform=event.bot_id, user_id=event.user_id, account_id=target),
                    AccountLink(owner_id=owner, account_id=target),
                )
                await self.failures.reset_account(account.id)
                await Store.reset_notice_attempts(account.id)
                roles, role_note = await self.role_details(account)
                task = await Store.task(account.id)
        await self.flush_notices()
        image = await self.cards.account_summary(account, roles, role_note, task, event)
        return ["token 登录成功，已获取 CUID 并绑定为当前账号。", image]

    @staticmethod
    def select_account(accounts: list[Account], reference: str, domestic: int | None) -> Account:
        if reference.startswith("["):
            index = int(reference[1:-1])
            if not 1 <= index <= len(accounts):
                raise WuwaError("账号编号不存在，请发送 wt查看 查看当前编号。")
            target = accounts[index - 1]
            if domestic is not None and target.domestic != domestic:
                raise WuwaError("该编号的区服与指定区服不同，请按 wt查看 的编号操作。")
            return target
        matches = [a for a in accounts if a.cuid == reference and (domestic is None or a.domestic == domestic)]
        if len(matches) > 1:
            raise WuwaError("该 CUID 同时绑定了国服与海外，请使用编号或在末尾指定区服。")
        if matches:
            return matches[0]
        if domestic is None and 1 <= int(reference) <= len(accounts):
            return accounts[int(reference) - 1]
        raise WuwaError("尚未绑定该账号，请发送 wt查看 查看编号或 CUID。")

    async def view_accounts(self, event: Event) -> str:
        owner = identity(event.bot_id, event.user_id)
        async with self.user_lock(owner):
            accounts = await Store.linked_accounts(owner)
            if not accounts:
                raise WuwaError("请先发送 wt绑定 CUID 或 wt添加token TOKEN。")
            lines = ["已绑定的 CUID："]
            for index, account in enumerate(accounts, 1):
                lines.append(f"[{index}] {mask_cuid(account.cuid)}（{'国服' if account.domestic else '海外'}）")
        return "\n".join(lines) + "\nwt切换 [x] 切换账号；wt删除 [x] 删除绑定。"

    async def delete_account(self, event: Event, cuid: str = "", domestic: int | None = None) -> str:
        owner = identity(event.bot_id, event.user_id)
        async with self.user_lock(owner):
            binding, _ = await self.bound(event)
            accounts = await Store.linked_accounts(owner)
            if cuid:
                target = self.select_account(accounts, cuid, domestic)
            else:
                target = next(a for a in accounts if a.id == binding.account_id)
            async with self.account_lock(target.id):
                task = await Store.task(target.id)
                stopped = task is not None and task.owner_id == owner and task.status in ("active", "paused")
                if stopped:
                    await self.finish(task, "stopped", "已删除绑定，自动开箱已停止")
                remaining = [a for a in accounts if a.id != target.id]
                replacement = remaining[0].id if remaining else None
                await Store.delete_link(owner, target.id, replacement)
                if binding.account_id == target.id:
                    current = (
                        f"当前账号已切换为 CUID {mask_cuid(remaining[0].cuid)}。" if remaining else "暂无绑定账号。"
                    )
                else:
                    current = ""
        await self.flush_notices()
        return (
            f"已删除 CUID {mask_cuid(target.cuid)}（{'国服' if target.domestic else '海外'}）的绑定。"
            + ("本人开启的任务已停止并汇总。" if stopped else "")
            + current
        )

    async def status(self, event: Event) -> str:
        if event.user_pm > 1:
            raise WuwaError("wt状态仅限 Bot 管理员使用。")
        if not self.database_ready:
            return (
                f"WT 已停止\n{self.database_error}\n连续失败上限：{retry_limit()} 次"
                f"\n主人告警待处理：{len(self.failures.state.alerts)} 条"
            )
        bound_people, task_people, total_chests = await Store.statistics()
        return (
            f"WT 状态\n绑定人数：{bound_people} 人\n开启任务人数：{task_people} 人（含暂停）"
            f"\n累计成功开箱：{total_chests} 个"
            f"\n连续失败上限：{retry_limit()} 次"
            f"\n已停止重试的阶段：{sum(item.blocked for item in self.failures.state.failures.values())} 个"
            f"\n主人告警待处理：{len(self.failures.state.alerts)} 条"
        )

    async def switch(self, event: Event, cuid: str = "", domestic: int | None = None) -> str:
        owner = identity(event.bot_id, event.user_id)
        async with self.user_lock(owner):
            binding, _ = await self.bound(event)
            accounts = await Store.linked_accounts(owner)
            if cuid:
                target = self.select_account(accounts, cuid, domestic)
            else:
                current = next(i for i, a in enumerate(accounts) if a.id == binding.account_id)
                target = accounts[(current + 1) % len(accounts)]
            binding.account_id = target.id
            await Store.save(binding)
        return (
            f"已切换到 CUID {mask_cuid(target.cuid)}（{'国服' if target.domestic else '海外'}）。"
            "原账号的任务、订阅及推送位置不变。"
        )

    async def subscribe(self, event: Event, enabled: bool) -> str:
        async with self.user_lock(identity(event.bot_id, event.user_id)):
            binding, account = await self.bound(event)
            async with self.account_lock(account.id):
                account = await Store.account(account.id)
                assert account is not None
                task = await Store.task(account.id)
                if task is not None and task.status in ("active", "paused") and task.owner_id != binding.id:
                    raise WuwaError("当前账号的任务属于另一用户，请由任务所属用户修改订阅。")
                account.subscribed = enabled
                await Store.save(account)
                if enabled:
                    await self.queue_results(account.id)
        await self.flush_notices()
        return f"CUID {mask_cuid(account.cuid)}：" + (
            "已开启10次后通知；失败、重新登录、暂停或任务结束时也会补发已有结果。"
            if enabled
            else "已关闭10次后通知，任务结束后汇总发图；中途失败、重新登录或暂停时仍发送已开出的结果。"
        )

    async def queue_results(self, key: str, *, force: bool = False, reason: str = "") -> None:
        """调用方持有账号锁；结果归入通知和 notice_id 在同一事务落库。"""
        account = await Store.account(key)
        if account is None:
            return
        grouped: dict[tuple[str, int], list[ChestRecord]] = {}
        for record in await Store.pending_results(key):
            grouped.setdefault((record.run_id, record.subscription_id), []).append(record)
        info = UserInfo.model_validate_json(account.info) if account.info else None
        task = await Store.task(key)
        for (run_id, subscription_id), records in grouped.items():
            send_rest = force or task is None or task.run_id != run_id or task.status not in ("active", "paused")
            while records and (send_rest or account.subscribed and len(records) >= 10):
                # 订阅按十件推送；未订阅的待发结果完整汇总为一张图。
                size = min(len(records), 10) if account.subscribed else len(records)
                batch, records = records[:size], records[size:]
                notice = Notice(
                    subscription_id=subscription_id,
                    account_id=key,
                    text=f"{reason + ' · ' if reason else ''}本次 {len(batch)} 个 · {date_text(batch[-1].created_at)}",
                    record_ids=json.dumps([r.id for r in batch]),
                    available_clicks=info.remaining if info else None,
                )
                for record in batch:
                    record.notice_id = notice.id
                await Store.save(*batch, notice)

    async def login_role(self, cuid: str, domestic: int) -> tuple[int | str, str]:
        """登录载荷里的角色字段；查不到就沿用占位值，登录本身不依赖它。"""
        try:
            roles = await self.api.roles(cuid, domestic)
        except WuwaError:
            return 0, "0"
        for role in roles:
            if role.playerId and role.serverId:
                return role.playerId, role.serverId
        return 0, "0"

    async def role_details(self, account: Account) -> tuple[list[RoleInfo], str]:
        """角色只用于展示：查询失败不影响绑定和其他命令。"""
        region = "国服" if account.domestic else "海外"
        try:
            roles = await self.api.roles(account.cuid, account.domestic)
        except WuwaError:
            return [], "角色暂时查询不到，稍后发送 wt信息 重试"
        if roles:
            return roles[:3], ""
        # 本区服没有角色时看一眼另一个区服，提示是不是绑定时选错了。
        other = 0 if account.domestic else 1
        try:
            elsewhere = await self.api.roles(account.cuid, other)
        except WuwaError:
            elsewhere = []
        hint = f"；{'海外' if other == 0 else '国服'}有角色，可能绑定时选错了区服" if elsewhere else ""
        return [], f"{region}下没有查到游戏角色{hint}"

    async def info(self, event: Event) -> list[str | bytes]:
        async with self.user_lock(identity(event.bot_id, event.user_id)):
            _, account = await self.bound(event)
            async with self.account_lock(account.id):
                previous_token = account.token
                await self.refresh(account)
                roles, role_note = await self.role_details(account)
                task = await Store.task(account.id)
        await self.flush_notices()
        image = await self.cards.account_summary(account, roles, role_note, task, event)
        hint = self.refresh_hint(previous_token, account)
        return [hint, image] if hint else [image]

    def refresh_hint(self, previous_token: str, account: Account) -> str:
        hint = "\n登录态已更新：已根据 CUID 重新登录。" if previous_token != account.token else ""
        if self.failures.blocked("renew:" + account.id):
            hint += "\n原token续期失败达到上限，自动续期已停止并生成主人告警；wt恢复 可重置计数。"
        return hint

    async def query(self, event: Event, bag: bool = False, kind: str | None = None) -> list[str | bytes]:
        owner = identity(event.bot_id, event.user_id)
        async with self.user_lock(owner):
            _, account = await self.bound(event)
            async with self.account_lock(account.id):
                previous_token = account.token
                await self.refresh(account)
                items = await self.read_bag(account) if bag else None
        # 绘图只读取本次快照，不占用自动开箱所需的账号锁。
        if items is not None:
            answer: list[str | bytes] = list(await self.cards.backpack(account, items, event, kind))
            hint = self.refresh_hint(previous_token, account)
            if hint:
                answer.insert(0, hint)
        else:
            image = await self.cards.click_summary(account, event)
            hint = self.refresh_hint(previous_token, account)
            answer = [hint, image] if hint else [image]
        await self.flush_notices()
        return answer

    async def atlas(self, event: Event, kind: str | None = None) -> list[str | bytes]:
        owner = identity(event.bot_id, event.user_id)
        async with self.user_lock(owner):
            _, account = await self.bound(event)
            async with self.account_lock(account.id):
                previous_token = account.token
                await self.refresh(account)
                items = await self.read_bag(account)
        images: list[str | bytes] = list(await self.cards.atlas(account, items, event, kind))
        hint = self.refresh_hint(previous_token, account)
        if hint:
            images.insert(0, hint)
        await self.flush_notices()
        return images

    async def read_bag(self, account: Account, *, fresh: bool = False) -> list[Item]:
        try:
            return await self.api.bags(account.token, fresh=fresh)
        except ApiError as error:
            if not error.is_auth:
                raise
            await self.refresh(account, fresh=True)
            return await self.api.bags(account.token, fresh=True)

    async def synthesize(self, event: Event, same_level: bool = False) -> list[str | bytes]:
        results: list[SynthesisResult] = []
        upgrades = same_count = spent = 0
        mode_name = "同阶" if same_level else "升阶"
        problem = ""
        bag_current = True
        run_id = new_id()
        async with self.user_lock(identity(event.bot_id, event.user_id)):
            _, account = await self.bound(event)
            async with self.account_lock(account.id):
                failure_key = "synthesis:" + account.id
                if self.failures.blocked(failure_key):
                    raise WuwaError("合成连续失败已达到上限，处理后请用 wt恢复 重置计数。")
                await self.refresh(account)
                items = await self.read_bag(account)
                highest = {} if same_level else highest_tiers(await self.api.atlas())
                relogged = False
                while True:
                    try:
                        recipe = next_recipe(items, highest, same_level=same_level)
                    except WuwaError as error:
                        problem = str(error)
                        break
                    if recipe is None:
                        break
                    record = SynthesisRecord(
                        run_id=run_id,
                        account_id=account.id,
                        mode="same_level" if recipe.same_level else "level_up",
                        material_ids=json.dumps(recipe.asset_ids),
                        before_quantity=sum(item.quantity for item in items),
                    )
                    await Store.save(record)
                    try:
                        result = await self.api.synthesize(account.token, recipe.asset_ids, recipe.same_level)
                    except ApiError as error:
                        record.state, record.error = "rejected", str(error)
                        await Store.save(record)
                        if error.is_auth:
                            logger.info(
                                f"[WuwaTappoUID] CUID {mask_cuid(account.cuid)}：合成鉴权被拒绝，{error.details}。"
                            )
                        if error.is_auth and not relogged:
                            relogged = True
                            previous_token = account.token
                            try:
                                await self.refresh(account, fresh=True)
                                if account.token == previous_token:
                                    problem = "合成请求被拒绝，但账号查询确认原 token 可用；已保留会话并停止本轮。"
                                    break
                                items = await self.read_bag(account, fresh=True)
                            except WuwaError as login_error:
                                problem = str(login_error)
                                break
                            continue
                        problem = f"服务端拒绝本次合成：{error}，本轮已停止。"
                        break
                    except WuwaError as error:
                        record.state, record.error = "uncertain", type(error).__name__
                        bag_current = False
                        await Store.save(record)
                        problem = "最后一次合成结果未确认，已停止且不会自动重发。请先用 wt背包 检查实际结果。"
                        break
                    record.result = result.model_dump_json()
                    record.state = "success"
                    await Store.save(record)
                    results.append(result)
                    same_count += int(recipe.same_level)
                    upgrades += int(not recipe.same_level)
                    spent += len(recipe.asset_ids)
                    try:
                        items = await self.read_bag(account, fresh=True)
                    except WuwaError as error:
                        bag_current = False
                        problem = f"已保存本次合成结果，但背包回读失败：{error}。本轮已停止。"
                        break
                    record.after_quantity = sum(item.quantity for item in items)
                    await Store.save(record)
                    if record.before_quantity - record.after_quantity != len(recipe.asset_ids) - 1:
                        problem = "服务端背包数量尚未与合成消耗一致，本轮已停止；请用 wt背包 核对。"
                        break
        if problem:
            await self.failures.fail(failure_key, "合成批次", problem, account.id)
        else:
            await self.failures.success(failure_key)
        await self.flush_notices()
        summary = f"本轮已确认：升阶 {upgrades} 次，同阶 {same_count} 次。"
        summary += f"消耗多余物品 {spent} 件，获得 {len(results)} 件。"
        if not results and not problem:
            cost = 5 if same_level else 10
            return [f"没有足够的多余物品可{mode_name}合成：每种保留 1 件，需同品类同稀有度 {cost} 件。"]
        if problem:
            summary += "\n" + problem
        if not results:
            return [summary]
        try:
            images = await self.cards.synthesis(
                account, results, items if bag_current else None, event, f"升阶 {upgrades} 次 · 同阶 {same_count} 次"
            )
        except Exception:
            logger.exception("[WuwaTappoUID] 合成结果已保存，但结果图绘制失败")
            return [summary + "\n结果图绘制失败，可用 wt背包 查看。"]
        return [summary, *images]

    @staticmethod
    def check_deadline(task: ChestTask | None) -> None:
        if task is not None and task.remaining() <= 0:
            raise Expired

    async def add_clicks(
        self, account: Account, number: int, task: ChestTask | None = None
    ) -> tuple[UserInfo, int, str]:
        before = await self.refresh(account)
        self.check_deadline(task)
        problem = ""
        submission_error = None
        try:
            ack = await self.api.set_clicks(
                account.token, before.total_click_count + number, task.expires_at if task is not None else None
            )
            if ack != 0:
                problem = str(ApiError(ack))
        except ApiError as error:
            problem = str(error)
        except (NetworkError, DataError) as error:
            submission_error = error
            problem = str(error)
        # 重连只提交固定目标累计值；最终仍按服务端实际变化作答。
        after = await self.refresh(account, fresh=True)
        delta = after.total_click_count - before.total_click_count
        if task is not None and delta <= 0 and submission_error is not None:
            raise submission_error
        return after, delta, problem

    async def increase(self, event: Event, number: int) -> list[str | bytes]:
        async with self.user_lock(identity(event.bot_id, event.user_id)):
            _, account = await self.bound(event)
            async with self.account_lock(account.id):
                failure_key = "clicks:" + account.id
                if self.failures.blocked(failure_key):
                    raise WuwaError("增加次数连续失败已达到上限，处理后请用 wt恢复 重置计数。")
                previous_token = account.token
                _, delta, problem = await self.add_clicks(account, number)
                if delta <= 0:
                    await self.failures.fail(
                        failure_key, "增加游戏点击次数", problem or "回读确认累计点击数未增加", account.id
                    )
                else:
                    await self.failures.success(failure_key)
        await self.flush_notices()
        image = await self.cards.click_summary(account, event, number, delta)
        hint = f"提交提示：{problem}（已回读实际累计值）" if problem else ""
        hint += self.refresh_hint(previous_token, account)
        return [hint, image] if hint else [image]

    def job_id(self, key: str) -> str:
        return "WuwaTappoUID:chest:" + key

    def schedule(self, task: ChestTask) -> None:
        if task.timer_paused:
            self.unschedule(task.account_id)
            return
        if self.closing or not self.database_ready or task.status not in ("active", "paused"):
            return
        if task.status == "active" and (
            self.failures.blocked("check:" + task.account_id) or self.failures.blocked("save:" + task.account_id)
        ):
            return
        scheduler.add_job(
            self.tick,
            "date",
            run_date=datetime.fromtimestamp(max(time.time() + 0.1, task.next_check), timezone.utc),
            args=[task.account_id],
            id=self.job_id(task.account_id),
            name="WuwaTappo 账号宝箱检查",
            replace_existing=True,
            misfire_grace_time=None,
            # tick 在收尾时给自己排下一次；只允许 1 个实例时，本次未结束就到点的一次性任务会被跳过并丢失。
            # 允许第 2 个实例，它会在账号锁上等本次结束。
            max_instances=2,
        )

    def unschedule(self, key: str) -> None:
        job = scheduler.get_job(self.job_id(key))
        if job is not None:
            job.remove()

    def next_wakeup(self, task: ChestTask, info: UserInfo | None, retry: bool = False) -> float:
        now = time.time()
        if retry:
            # 失败后不使用已经过去的提醒/到期点，避免 0.1 秒循环。
            wakeup = max(now + 60, self.failures.retry_after("check:" + task.account_id))
            return min(wakeup, task.expires_at) if task.expires_at > now else wakeup
        candidates = [task.expires_at]
        if not task.reminded:
            # 剩余不超过 1 小时进入提醒与续期窗口。
            candidates.append(task.expires_at - RENEW_WINDOW)
        if task.status == "active":
            if retry or info is None:
                candidates.append(now + 60)
            else:
                candidates.extend(max(now + 5, info.cooldown(side)) for side in ("left", "right"))
        return max(now + 0.1, min(candidates))

    async def start_or_renew(self, event: Event) -> list[str | bytes]:
        async with self.user_lock(identity(event.bot_id, event.user_id)):
            binding, account = await self.bound(event)
            async with self.account_lock(account.id):
                previous_token = account.token
                task = await Store.task(account.id)
                now = time.time()
                if (
                    task is not None
                    and task.status in ("active", "paused")
                    and task.remaining(now) > 0
                    and task.owner_id != binding.id
                ):
                    raise WuwaError("这个账号已有其他用户开启的任务，请由任务所属用户操作。")
                if task is not None and task.status in ("active", "paused") and task.remaining(now) <= 0:
                    await self.finish(task, "expired", "本轮已到期")
                if task is not None and task.status in ("active", "paused"):
                    if task.owner_id != binding.id:
                        raise WuwaError("这个账号已有其他用户开启的任务，请由任务所属用户操作。")
                    if task.status == "paused":
                        action = "任务已暂停，可发送 wt恢复 继续本轮。"
                    else:
                        action = "任务正在运行。"
                    if 0 < task.remaining(now) <= RENEW_WINDOW:
                        task.expires_at += TASK_SECONDS
                        task.reminded = False
                        action += "已在原到期时间上续期 24 小时。"
                    await Store.save(task)
                    self.schedule(task)
                else:
                    if await Store.unresolved(account.id):
                        assert task is not None
                        info = await self.refresh(account)
                        if not await self.reconcile(task, info):
                            raise WuwaError("上轮开箱仍未能确认，已保留流水，请管理员核对后再开启。")
                    await self.refresh(account)
                    if not event.WS_BOT_ID:
                        raise WuwaError("当前会话缺少 GS WebSocket 路由，无法保存自动推送目标。")
                    run_id = new_id()
                    await gs_subscribe.add_subscribe("single", "WuwaTappo宝箱", event, uid=run_id)
                    subscriptions = await gs_subscribe.get_subscribe("WuwaTappo宝箱", uid=run_id)
                    if not subscriptions or len(subscriptions) != 1:
                        raise WuwaError("推送订阅未能唯一保存，未启动开箱。")
                    now = time.time()
                    task = ChestTask(
                        account_id=account.id,
                        run_id=run_id,
                        owner_id=binding.id,
                        subscription_id=subscriptions[0].id,
                        started_at=now,
                        expires_at=now + TASK_SECONDS,
                        next_check=now,
                    )
                    await Store.save(task)
                    self.schedule(task)
                    action = "已开启 24 小时自动开箱，将立即检查左右宝箱。"
                answer = action + self.refresh_hint(previous_token, account)
        await self.flush_notices()
        image = await self.cards.task_summary(account, task, event)
        return [answer, image]

    async def finish(self, task: ChestTask, status: str, reason: str) -> None:
        # 先持久化结束状态并撤销调度，结果核对/汇总失败不能阻止任务停止。
        task.status = status
        task.paused_at = None
        await Store.save(task)
        self.unschedule(task.account_id)
        pending = await Store.unresolved(task.account_id)
        if pending:
            info: UserInfo | None = None
            account = await Store.account(task.account_id)
            if account is not None:
                try:
                    info = await self.api.me(account.token, fresh=True)
                    if str(info.cuid) != account.cuid:
                        raise DataError("结束任务核对的CUID与账号不一致。")
                except WuwaError as error:
                    await self.failures.fail(
                        "reconcile:" + account.id,
                        "结束任务的结果核对",
                        self.error_detail(error),
                        account.id,
                        limit=1
                        if isinstance(error, DataError) or isinstance(error, ApiError) and error.is_auth
                        else None,
                    )
            if info is not None:
                await self.reconcile(task, info)
            else:
                for record in pending:
                    if record.result:
                        await self.record_success(task, record)
        unresolved_count = len(await Store.unresolved(task.account_id))
        await self.queue_results(task.account_id, force=True, reason=reason)
        await Store.save(
            task,
            Notice(
                subscription_id=task.subscription_id,
                account_id=task.account_id,
                text=f"{reason}。本轮成功开箱 {task.successes} 个。"
                + (f"另有 {unresolved_count} 个请求结果待确认，尚未计入。" if unresolved_count else ""),
            ),
        )

    async def stop_owned(self, binding: Binding, reason: str) -> str:
        task = await Store.task(binding.account_id)
        if task is None or task.status not in ("active", "paused"):
            return "当前没有运行中的自动开箱任务，账号绑定已保留。"
        if task.owner_id != binding.id:
            raise WuwaError("任务属于绑定此账号的另一用户，请由任务所属用户停止。")
        await self.finish(task, "stopped", reason)
        return f"自动开箱已停止，账号绑定已保留。本轮成功 {task.successes} 个。"

    async def stop(self, event: Event) -> str:
        async with self.user_lock(identity(event.bot_id, event.user_id)):
            binding, account = await self.bound(event)
            async with self.account_lock(account.id):
                answer = await self.stop_owned(binding, "已手动停止自动开箱")
        await self.flush_notices()
        return answer

    async def set_paused(self, event: Event, paused: bool) -> str:
        async with self.user_lock(identity(event.bot_id, event.user_id)):
            if not paused and await Store.binding(identity(event.bot_id, event.user_id)) is None:
                await self.failures.reset_commands(identity(event.bot_id, event.user_id))
                return "当前没有绑定账号，命令重试计数已重置，请先绑定账号。"
            binding, account = await self.bound(event)
            async with self.account_lock(account.id):
                task = await Store.task(account.id)
                if not paused:
                    if task is not None and task.status in ("active", "paused") and task.owner_id != binding.id:
                        raise WuwaError("任务属于另一用户，请由任务所属用户恢复。")
                    await self.failures.reset_account(account.id)
                    await Store.reset_notice_attempts(account.id)
                    if event.user_pm <= 1:
                        await self.failures.success("maintain")
                        await self.failures.success("notice_loop")
                    account.token_renew_after = 0
                    await Store.save(account)
                    if task is None or task.status not in ("active", "paused"):
                        await self.refresh(account)
                        await self.flush_notices()
                        return "当前账号的重试计数已重置，将继续核对结果和补发通知；未开启宝箱任务。"
                if task is None or task.status not in ("active", "paused"):
                    raise WuwaError("当前没有可暂停或恢复的宝箱任务，请用 wt宝箱 开启。")
                if task.owner_id != binding.id:
                    raise WuwaError("任务属于另一用户，请由任务所属用户暂停或恢复。")
                if task.remaining() <= 0:
                    await self.finish(task, "expired", "本轮已到期")
                    answer = "本轮已到期，请用 wt宝箱 开启新的 24 小时任务。"
                elif paused and task.timer_paused:
                    answer = "任务和计时已经暂停。\n" + task_text(task)
                elif not paused and task.status == "active":
                    await self.refresh(account)
                    answer = "任务正在运行，重试计数已重置。\n" + task_text(task)
                else:
                    previous_token = account.token
                    if paused:
                        task.status = "paused"
                        task.pause_reason = "manual"
                        task.paused_at = time.time()
                        action = "宝箱任务和计时已暂停，剩余有效期保留。"
                        await Store.save(task)
                        await self.queue_results(account.id, force=True, reason="任务暂停")
                    else:
                        info = await self.refresh(account)
                        if not await self.reconcile(task, info):
                            raise WuwaError("仍有结果未确认的开箱，任务保持暂停，请管理员核对流水。")
                        if task.remaining() <= 0:
                            await self.finish(task, "expired", "本轮已到期")
                            await self.flush_notices()
                            return "本轮已到期，请用 wt宝箱 开启新一轮。"
                        now = time.time()
                        if task.paused_at is not None:
                            task.expires_at += now - task.paused_at
                        task.paused_at = None
                        task.status = "active"
                        task.pause_reason = ""
                        task.failures = 0
                        action = "宝箱任务已恢复，继续剩余有效期，将立即检查左右宝箱。"
                    task.next_check = time.time()
                    await Store.save(
                        task,
                        Notice(
                            subscription_id=task.subscription_id,
                            account_id=task.account_id,
                            text=action + "\n" + task_text(task),
                        ),
                    )
                    self.schedule(task)
                    answer = action + "\n" + task_text(task) + self.refresh_hint(previous_token, account)
        await self.flush_notices()
        return answer

    async def pause_after_auth_failure(self, event: Event) -> None:
        async with self.user_lock(identity(event.bot_id, event.user_id)):
            binding = await Store.binding(identity(event.bot_id, event.user_id))
            if binding is None:
                return
            async with self.account_lock(binding.account_id):
                task = await Store.task(binding.account_id)
                if task is None or task.status != "active":
                    return
                task.status = "paused"
                task.pause_reason = "auth"
                await self.failures.fail(
                    "auth:" + binding.account_id,
                    "账号鉴权未恢复",
                    "账号查询仍未通过，宝箱任务已暂停。",
                    binding.account_id,
                    limit=1,
                )
                await self.queue_results(binding.account_id, force=True, reason="重新登录失败")
                task.next_check = self.next_wakeup(task, None)
                await Store.save(
                    task,
                    Notice(
                        subscription_id=task.subscription_id,
                        account_id=task.account_id,
                        text="登录态失效且重新登录未成功，自动开箱已暂停。处理后可用 wt恢复 继续本轮。",
                    ),
                )
                self.schedule(task)
        await self.flush_notices()

    async def record_success(self, task: ChestTask, record: ChestRecord) -> None:
        record.state = "success"
        if task.run_id == record.run_id:
            task.successes += 1
            task.failures = 0
        await Store.save(record, task)
        await self.failures.success("check:" + task.account_id)
        await self.queue_results(record.account_id)

    async def reconcile(self, task: ChestTask, info: UserInfo) -> bool:
        unresolved = await Store.unresolved(task.account_id)
        clear = True
        now = time.time()
        for record in unresolved:
            before = UserInfo.model_validate_json(record.before_info)
            side: Side = "left" if record.side == "left" else "right"
            if record.result or info.cooldown(side) > before.cooldown(side):
                await self.record_success(task, record)
            elif now - record.created_at >= SETTLE_SECONDS:
                # 成功开箱一定会推进该侧冷却；早已超时仍未推进，说明请求没有生效（包括没发出去）。
                record.state = "not_applied"
                await Store.save(
                    record,
                    Notice(
                        subscription_id=record.subscription_id,
                        account_id=record.account_id,
                        text=f"{SIDE_NAMES[side]}宝箱上次请求没有生效（该侧冷却未变化）。"
                        + ("将按重试次数限制继续处理。" if task.status == "active" else "本次仅核对结果，不重新开箱。"),
                    ),
                )
            else:
                clear = False
                if record.state != "uncertain":
                    record.state = "uncertain"
                    await Store.save(
                        record,
                        Notice(
                            subscription_id=record.subscription_id,
                            account_id=record.account_id,
                            text=f"{SIDE_NAMES[side]}宝箱结果尚未确认，暂缓开箱并继续查询，避免重复请求。",
                        ),
                    )
        if unresolved:
            # 中断后迟到的成功结果也立刻补发，不等下一次十件或到期。
            await self.queue_results(task.account_id, force=True, reason="开箱请求核对结果")
        return clear

    async def open_side(self, account: Account, task: ChestTask, side: Side, info: UserInfo) -> UserInfo:
        recent = await Store.recent_chests(account.id, task.run_id, side)
        failed = 0
        for record in recent:
            if record.state != "not_applied":
                break
            failed += 1
        if failed == 4:
            task.status = "paused"
            task.pause_reason = "failure"
            # 留下终止标记，用户恢复后才开始新的重试批次；不新增数据库列。
            recent[0].state = "retry_exhausted"
            message = f"{SIDE_NAMES[side]}宝箱首次请求及 3 次重试均未生效，已暂停；检查网络后可发送 wt恢复。"
            await Store.save(
                recent[0],
                task,
                Notice(subscription_id=task.subscription_id, account_id=account.id, text=message),
            )
            raise NetworkError(message)
        if failed:
            logger.warning(f"[WuwaTappoUID] {SIDE_NAMES[side]}宝箱已确认前次未生效，开始第 {failed}/3 次重试。")
        supplement = ""
        # 最多一次自动补充、一次重试；每次真正发请求前都检查到期时间。
        for attempt in range(2):
            self.check_deadline(task)
            record = ChestRecord(
                account_id=account.id,
                run_id=task.run_id,
                subscription_id=task.subscription_id,
                side=side,
                before_info=info.model_dump_json(),
                supplement=supplement,
            )
            await Store.save(record)
            if time.time() >= task.expires_at:
                record.state = "not_sent"
                await Store.save(record)
                raise Expired
            try:
                result = await self.api.open(account.token, side, info.total_click_count)
            except ApiError as error:
                record.state = "rejected"
                record.result = str(error)
                await Store.save(record)
                if error.code not in (70005, 70006):
                    await self.queue_results(account.id, force=True, reason="开箱失败，汇总已开结果")
                if error.code == 70005 and attempt == 0:
                    info, delta, problem = await self.add_clicks(account, AUTO_CLICKS, task)
                    supplement = f"自动补充请求 {AUTO_CLICKS} 点击，服务端累计值实际变化 {delta:+d}。"
                    if problem:
                        supplement += f"提交提示：{problem}。"
                    record.supplement = supplement
                    await Store.save(record)
                    if delta > 0:
                        continue
                    raise WuwaError(supplement + "本次未重试开箱。")
                if error.is_auth:
                    # 先验证原会话，本次不再次提交开箱。
                    previous_token = account.token
                    await self.refresh(account, fresh=True)
                    if account.token == previous_token:
                        raise WuwaError("开箱请求被拒绝，但原 token 仍可查询；会话已保留，本次停止开箱。") from None
                    raise NetworkError("开箱请求鉴权失败，账号查询确认后已恢复会话，稍后重新检查。") from None
                if supplement:
                    await self.account_notice(account, f"{SIDE_NAMES[side]}宝箱重试未成功：{error}。\n{supplement}")
                raise
            except (NetworkError, DataError):
                # 冷却推进就结算成功；120 秒后确认未生效才重试，累计最多初次加 3 次。
                await self.queue_results(account.id, force=True, reason="开箱响应中断")
                fresh = await self.refresh(account, fresh=True)
                await self.reconcile(task, fresh)
                return fresh
            record.result = result.model_dump_json()
            await Store.save(record)
            fresh = await self.refresh(account, fresh=True)
            await self.record_success(task, record)
            return fresh
        raise RuntimeError("开箱重试流程没有产生结果")

    @with_read_cache
    async def tick(self, key: str) -> None:
        if self.closing or not self.database_ready:
            return
        attempts_before = self.failures.attempts("check:" + key)
        try:
            await self._check_account(key)
            await self.flush_notices()
        except SQLAlchemyError as error:
            await self.disable_database(error)
        except Exception as error:
            if self.failures.attempts("check:" + key) > attempts_before:
                return
            if self.failures.attempts("check:" + key) == 0:
                logger.exception("[WuwaTappoUID] 账号检查未完成")
            failure = await self.failures.fail("check:" + key, "账号检查", self.error_detail(error), key)
            if failure.blocked:
                self.unschedule(key)
        finally:
            await self.failures.flush_alerts()

    async def _check_account(self, key: str) -> None:
        async with self.account_lock(key):
            if self.closing or not self.database_ready:
                return
            task = await Store.task(key)
            if task is None or task.status not in ("active", "paused"):
                return
            if task.timer_paused:
                self.unschedule(key)
                return
            info: UserInfo | None = None
            retry = False
            interrupted = False
            try:
                if task.remaining() <= 0:
                    await self.finish(task, "expired", "24 小时自动开箱已结束")
                    return
                if not task.reminded and task.expires_at - time.time() <= RENEW_WINDOW:
                    task.reminded = True
                    await Store.save(
                        task,
                        Notice(
                            subscription_id=task.subscription_id,
                            account_id=key,
                            text="自动开箱剩余不超过 1 小时，可发送 wt宝箱 在原到期时间上续期 24 小时。"
                            f"\n到期：{date_text(task.expires_at)}",
                        ),
                    )
                if task.status == "paused":
                    return
                if not self.failures.allows("check:" + key):
                    retry = True
                    return
                account = await Store.account(key)
                if account is None:
                    raise DataError("任务关联的账号数据缺失。")
                info = await self.refresh(account)
                if not await self.reconcile(task, info):
                    await self.failures.success("check:" + key)
                    task.failures = 0
                    retry = True
                    return
                for side in ("left", "right"):
                    self.check_deadline(task)
                    if info.cooldown(side) > time.time():
                        continue
                    try:
                        info = await self.open_side(account, task, side, info)
                    except ApiError as error:
                        if error.code == 70006:
                            info = await self.refresh(account)
                            retry = True
                            continue
                        raise
                    if await Store.unresolved(key):
                        retry = True
                        break
                await self.failures.success("check:" + key)
                task.failures = 0
            except Expired:
                await self.finish(task, "expired", "24 小时自动开箱已结束")
            except NetworkError as error:
                # 每轮网络请求自身有有限重试；持续失败也有后台轮次上限。
                await self.task_failure(task, error)
                retry = True
                interrupted = True
            except AuthenticationError as error:
                interrupted = True
                task.status = "paused"
                task.pause_reason = "auth"
                await self.failures.fail("auth:" + key, "账号鉴权未恢复", str(error), key, limit=1)
                await Store.save(
                    task,
                    Notice(subscription_id=task.subscription_id, account_id=key, text=f"{error} 自动开箱已暂停。"),
                )
            except ApiError as error:
                interrupted = True
                if error.is_auth:
                    task.status = "paused"
                    task.pause_reason = "auth"
                    text = f"请求鉴权未通过：{error}。自动开箱已暂停，请查询账号或添加有效 token 后发送 wt恢复。"
                    await self.failures.fail("auth:" + key, "账号鉴权未恢复", error.details, key, limit=1)
                    await Store.save(task, Notice(subscription_id=task.subscription_id, account_id=key, text=text))
                else:
                    await self.task_failure(task, error)
                retry = True
            except WuwaError as error:
                interrupted = True
                await self.task_failure(task, error)
                retry = True
            except SQLAlchemyError as error:
                await self.disable_database(error)
            except Exception as error:
                if self.failures.attempts("check:" + key) == 0:
                    logger.exception("[WuwaTappoUID] 宝箱检查出现意外错误")
                await self.task_failure(task, error)
                retry = True
                interrupted = True
            finally:
                # 先在内存里排好下次检查，数据库写入或推送再出错也不会让任务停摆。
                if self.database_ready and task.status in ("active", "paused"):
                    task.next_check = self.next_wakeup(task, info, retry)
                    self.schedule(task)
                try:
                    if self.database_ready and interrupted:
                        await self.queue_results(key, force=True, reason="任务中断，汇总已开结果")
                    if self.database_ready and task.status in ("active", "paused"):
                        await Store.save(task)
                        await self.failures.success("save:" + key)
                except SQLAlchemyError as error:
                    await self.disable_database(error)
                except Exception as error:
                    if self.failures.attempts("save:" + key) == 0:
                        logger.exception("[WuwaTappoUID] 保存检查结果失败")
                    failure = await self.failures.fail("save:" + key, "保存检查结果", self.error_detail(error), key)
                    if failure.blocked:
                        self.unschedule(key)
                    else:
                        task.next_check = max(time.time() + 60, self.failures.retry_after("save:" + key))
                        self.schedule(task)

    async def restore(self) -> None:
        for snapshot in await Store.active_tasks():
            async with self.account_lock(snapshot.account_id):
                # 启动后命令已可能进入，锁内重读，避免覆盖用户刚完成的续期/暂停。
                task = await Store.task(snapshot.account_id)
                if task is not None and task.status in ("active", "paused"):
                    if self.failures.blocked("check:" + task.account_id) or self.failures.blocked(
                        "save:" + task.account_id
                    ):
                        task.status = "paused"
                        task.pause_reason = "failure"
                    if task.timer_paused:
                        self.unschedule(task.account_id)
                        continue
                    # 不改变 started_at / expires_at；停机期间不补开历史轮次。
                    task.next_check = time.time()
                    await Store.save(task)
                    self.schedule(task)

    @with_read_cache
    async def maintain(self) -> None:
        """每分钟执行：补排丢失了下次检查的任务，再补发待提交的通知。"""
        if self.closing or not self.database_ready:
            return
        if not self.failures.allows("maintain"):
            return
        try:
            await self._maintain()
            await self.failures.success("maintain")
        except SQLAlchemyError as error:
            await self.disable_database(error)
        except Exception as error:
            if self.failures.attempts("maintain") == 0:
                logger.exception("[WuwaTappoUID] 后台巡检出现意外错误")
            await self.failures.fail("maintain", "后台巡检", self.error_detail(error))
        finally:
            await self.failures.flush_alerts()

    async def _maintain(self) -> None:
        if not self.closing:
            for task in await Store.active_tasks():
                if task.timer_paused:
                    continue
                key = task.account_id
                if self.failures.blocked("save:" + key) or (
                    task.status == "active" and self.failures.blocked("check:" + key)
                ):
                    continue
                # 正在检查的账号持有账号锁，收尾时会自己排下一次。
                if scheduler.get_job(self.job_id(key)) is None and not self.account_lock(key).locked():
                    logger.warning("[WuwaTappoUID] 发现没有排期的宝箱任务，已重新安排检查。")
                    task.next_check = time.time()
                    self.schedule(task)
            for key in await Store.unresolved_accounts():
                if self.account_lock(key).locked() or not self.failures.allows("reconcile:" + key):
                    continue
                async with self.account_lock(key):
                    task = await Store.task(key)
                    if task is None or task.status in ("active", "paused"):
                        continue
                    account = await Store.account(key)
                    if account is None:
                        continue
                    try:
                        # 结束后只查询原登录态，不重新登录、不提交点击或开箱。
                        info = await self.api.me(account.token, fresh=True)
                        if str(info.cuid) != account.cuid:
                            raise DataError("核对返回的 CUID 与账号不一致。")
                        account.info = info.model_dump_json()
                        account.verified_at = time.time()
                        await Store.save(account)
                        await self.reconcile(task, info)
                        await self.failures.success("reconcile:" + key)
                    except SQLAlchemyError:
                        raise
                    except Exception as error:
                        await self.failures.fail(
                            "reconcile:" + key,
                            "已结束任务的结果核对",
                            self.error_detail(error),
                            key,
                            limit=1
                            if isinstance(error, DataError) or isinstance(error, ApiError) and error.is_auth
                            else None,
                        )
            for key in await Store.result_accounts():
                if self.account_lock(key).locked() or not self.failures.allows("results:" + key):
                    continue
                async with self.account_lock(key):
                    try:
                        await self.queue_results(key)
                        await self.failures.success("results:" + key)
                    except SQLAlchemyError:
                        raise
                    except Exception as error:
                        await self.failures.fail("results:" + key, "开箱结果汇总", self.error_detail(error), key)
        await self.flush_notices()

    def release_notice_image(self, key: str) -> None:
        if key in self._notice_images:
            self._notice_image_bytes -= len(self._notice_images.pop(key).body)

    def cached_notice_image(self, key: str) -> bytes | None:
        if key not in self._notice_images:
            return None
        cached = self._notice_images[key]
        if cached.revision != self.resources.revision:
            self.release_notice_image(key)
            return None
        self._notice_images.move_to_end(key)
        return cached.body

    def cache_notice_image(self, key: str, revision: int, body: bytes) -> None:
        self.release_notice_image(key)
        size = len(body)
        if size > NOTICE_CACHE_BYTES:
            return
        while self._notice_images and self._notice_image_bytes + size > NOTICE_CACHE_BYTES:
            _, old = self._notice_images.popitem(last=False)
            self._notice_image_bytes -= len(old.body)
        self._notice_images[key] = CachedNoticeImage(revision, body)
        self._notice_image_bytes += size

    async def notice_messages(
        self, notice: Notice, event: Event, account: Account | None, contexts: dict[str, ResultContext]
    ) -> list[Message]:
        notice_text = notice.text.replace(account.cuid, mask_cuid(account.cuid)) if account is not None else notice.text
        if notice.record_ids:
            if account is None:
                raise DataError("开箱通知对应的账号不存在。")
            rendered = self.cached_notice_image(notice.id)
            if rendered is None:
                revision = self.resources.revision
                if account.id not in contexts:
                    contexts[account.id] = await self.cards.result_context(account, event)
                records = await Store.records(json.loads(notice.record_ids))
                rendered = await self.cards.results(records, notice.available_clicks, notice_text, contexts[account.id])
                self.cache_notice_image(notice.id, revision, rendered)
            return [MessageSegment.image(rendered)]
        label = f"CUID {mask_cuid(account.cuid)}：\n" if account is not None else ""
        messages = [MessageSegment.text(label + notice_text + "\n")]
        if notice.asset_id:
            try:
                async with asyncio.timeout(20):
                    tile = await self.resources.item_image(notice.asset_id, notice.rarity)
            except TimeoutError:
                logger.warning("[WuwaTappoUID] 物品图获取超时，本条只发文字。")
                tile = None
            if tile is not None:
                messages.append(MessageSegment.image(tile))
        return messages

    async def flush_notices(self) -> None:
        if self.closing or not self.database_ready:
            return
        if not self.failures.allows("notice_loop"):
            return
        try:
            await self._flush_notices()
            await self.failures.success("notice_loop")
        except SQLAlchemyError as error:
            await self.disable_database(error)
        except Exception as error:
            if self.failures.attempts("notice_loop") == 0:
                logger.exception("[WuwaTappoUID] 通知巡检出现意外错误")
            await self.failures.fail("notice_loop", "通知巡检", self.error_detail(error))
        finally:
            await self.failures.flush_alerts()

    async def _flush_notices(self) -> None:
        async with self._push_lock:
            if self.closing or not self.database_ready or not gss.active_bot:
                return
            # 同一订阅仍指向原任务和会话，离线积压的多张图也一起补发。
            groups: dict[int, list[Notice]] = {}
            for notice in await Store.notices():
                groups.setdefault(notice.subscription_id, []).append(notice)
            for subscription_id, batch in groups.items():
                account_id = batch[0].account_id
                send_key = f"notice:{account_id}:{subscription_id}"
                render_key = f"render:{account_id}:{subscription_id}"
                if not self.failures.allows(send_key):
                    continue
                if max(notice.attempts for notice in batch) >= retry_limit():
                    await self.failures.fail(
                        send_key,
                        "结果通知发送",
                        "保存的发送尝试次数已达到上限，通知保留；wt恢复 可重置。",
                        account_id,
                        limit=1,
                    )
                    continue
                sub = await Store.subscription(subscription_id)
                if sub is None or not sub.WS_BOT_ID:
                    await self.failures.fail(send_key, "结果通知发送", "通知订阅路由不存在", account_id)
                    continue
                event = Event(
                    bot_id=sub.bot_id,
                    user_id=sub.user_id,
                    bot_self_id=sub.bot_self_id,
                    user_type=sub.user_type,
                    group_id=sub.group_id,
                )
                contents: list[Message] = []
                accounts: dict[str, Account | None] = {}
                contexts: dict[str, ResultContext] = {}
                if not self.failures.blocked(render_key) and not self.failures.allows(render_key):
                    continue
                if not self.failures.blocked(render_key):
                    try:
                        for notice in batch:
                            key = notice.account_id
                            if key not in accounts:
                                accounts[key] = await Store.account(key)
                            contents.extend(await self.notice_messages(notice, event, accounts[key], contexts))
                        await self.failures.success(render_key)
                    except SQLAlchemyError:
                        raise
                    except Exception as error:
                        if self.failures.attempts(render_key) == 0:
                            logger.exception("[WuwaTappoUID] 通知图片绘制失败")
                        failure = await self.failures.fail(
                            render_key, "通知图片绘制", self.error_detail(error), account_id
                        )
                        if not failure.blocked:
                            continue
                if self.failures.blocked(render_key):
                    contents = [MessageSegment.text("结果图片持续绘制失败，本组改为文字通知；已保存的开箱流水保留。\n")]
                    for notice in batch:
                        cuid = notice.account_id.split(":", 1)[-1]
                        text = notice.text.replace(cuid, mask_cuid(cuid))
                        contents.append(MessageSegment.text(f"CUID {mask_cuid(cuid)}：{text}\n"))
                # 多图用一个合并转发；单图照常发送。艾特仅放在外层一次。
                messages = [MessageSegment.node(contents)] if sum(m.type == "image" for m in contents) > 1 else contents
                if sub.user_type == "group":
                    messages = [MessageSegment.at(sub.user_id), MessageSegment.text("\n"), *messages]
                for notice in batch:
                    notice.attempts += 1
                # 提交前保存次数；结果不明或重启也不会增加发送预算。
                await Store.save(*batch)
                try:
                    async with asyncio.timeout(15):
                        submitted = await sub.send(messages)
                    if submitted != 0:
                        raise NetworkError("GS 未能提交推送，Bot可能离线。")
                except SQLAlchemyError:
                    raise
                except Exception as error:
                    for notice in batch:
                        notice.last_error = f"推送异常：{type(error).__name__}"
                    await Store.save(*batch)
                    await self.failures.fail(send_key, "结果通知发送", self.error_detail(error), account_id)
                    continue
                submitted_at = time.time()
                for notice in batch:
                    notice.submitted = True
                    notice.submitted_at = submitted_at
                    notice.last_error = ""
                # 整组合并消息共同标记提交，不会只标记其中一页。
                await Store.save(*batch)
                await self.failures.success(send_key)
                for notice in batch:
                    self.release_notice_image(notice.id)
