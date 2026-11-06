"""一次性 KrSDK 网页登录；没有后台登录和 SDK 凭据续期。"""

import asyncio
import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from gsuid_core.aps import scheduler
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.subscribe import gs_subscribe
from sqlalchemy.exc import SQLAlchemyError

from . import login_remote
from .database import Account, AccountLink, Binding, LoginSession, Notice, Store
from .formatting import mask_cuid
from .login_card import render_login
from .render_cache import render_in_thread
from .schema import CN_SERVER_ID, ApiError, DataError, SdkLogin, WuwaError, token_expiry
from .service import Service, account_id, identity
from .settings import settings

LOGIN_TTL = 300


def session_key(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


class LoginManager:
    def __init__(self, service: Service) -> None:
        self.service = service
        self._remote_retries: dict[str, tuple[int, float]] = {}

    async def create(self, event: Event) -> list[str | bytes]:
        self.service.require_database()
        base = settings.get_config("login_public_url").data.strip().rstrip("/")
        address = urlsplit(base)
        if address.scheme != "https" or not address.netloc or address.query or address.fragment:
            raise WuwaError("主人尚未配置有效的 HTTPS 登录页面公网地址，请暂用 wt添加token。")
        owner = identity(event.bot_id, event.user_id)
        async with self.service.user_lock(owner):
            opened = await Store.open_logins(owner)
            if any(row.status == "processing" and row.expires_at > time.time() for row in opened):
                raise WuwaError("登录正在处理，请等待结果。")
            state = secrets.token_urlsafe(32)
            remote = None
            if not settings.get_config("login_use_local").data:
                remote = await login_remote.create(self.service.api, base, event.bot_id, event.user_id)
                state = remote.state
            key = session_key(state)
            await gs_subscribe.add_subscribe("single", "WuwaTappo登录", event, uid=key)
            subscriptions = await gs_subscribe.get_subscribe("WuwaTappo登录", uid=key)
            if not subscriptions:
                raise DataError("无法保存登录消息会话，请稍后再试。")
            row = LoginSession(
                id=key,
                owner_id=owner,
                platform=event.bot_id,
                user_id=event.user_id,
                subscription_id=subscriptions[0].id,
                expires_at=time.time() + LOGIN_TTL,
            )
            if remote is not None:
                row.expires_at = min(row.expires_at, remote.expires_at / 1000)
                row.external_url = base
                row.external_state = state
                row.external_poll_token = remote.poll_token
            for old in opened:
                old.status = "revoked"
                old.message = "该链接已被新的登录链接替代，请使用最新链接。"
            await Store.save(*opened, row)
        url = base + "/wuwatappouid/login#state=" + state
        try:
            card = await render_in_thread(render_login, url)
        except Exception:
            logger.exception("[WuwaTappoUID] 登录二维码绘制失败，发送文字链接")
            return ["请在5分钟内打开链接登录本人国服账号，请勿转发。\n" + url]
        return [card, url]

    async def get(self, state: str) -> LoginSession:
        return await self.get_key(session_key(state))

    async def get_key(self, key: str) -> LoginSession:
        self.service.require_database()
        row = await Store.login_session(key)
        if row is None:
            raise WuwaError("登录链接无效，请重新发送 wt登录。")
        if row.expires_at <= time.time() and row.status in ("pending", "verifying"):
            row.status = "expired"
            row.message = "登录链接已过期，请重新发送 wt登录。"
            await Store.save(row)
        return row

    @staticmethod
    def response(row: LoginSession) -> dict:
        return {
            "status": 0 if row.status in ("pending", "processing", "verifying", "success") else 1,
            "msg": row.message,
            "data": {
                "state": row.status,
                "expires_at": int(row.expires_at * 1000),
                "binding_target": f"{row.platform} · {mask_cuid(row.user_id)}",
                **json.loads(row.result),
            },
        }

    async def status(self, state: str) -> dict:
        return self.response(await self.get(state))

    async def complete(self, state: str, sdk: SdkLogin | None) -> dict:
        row = await self.get(state)
        if row.external_url:
            raise WuwaError("请在外置登录页面完成登录。")
        return await self.complete_key(row.id, sdk)

    async def complete_key(self, key: str, sdk: SdkLogin | None) -> dict:
        row = await self.get_key(key)
        if row.status not in ("pending", "verifying"):
            return self.response(row)
        async with self.service.user_lock(row.owner_id):
            row = await self.get_key(key)
            if row.status not in ("pending", "verifying"):
                return self.response(row)
            try:
                if row.status == "pending":
                    if sdk is None:
                        raise WuwaError("请先填写手机号和验证码后登录。")
                    row.status = "processing"
                    await Store.save(row)
                    roles = await self.service.api.login_roles(sdk)
                    role = next(
                        (
                            item
                            for item in roles.items
                            if str(item.playerId).isdigit() and int(item.playerId) > 0 and item.serverId == CN_SERVER_ID
                        ),
                        None,
                    )
                    target = account_id(sdk.cuid, 1)
                else:
                    target = row.account_id
                async with self.service.account_lock(target):
                    if row.status == "processing":
                        # 角色查询和等锁期间链接可能已到期；发认证请求前重新检查。
                        if row.expires_at <= time.time():
                            raise WuwaError("登录链接已过期，请重新发送 wt登录。")
                        account = await Store.account(target)
                        if account is None:
                            account = Account(id=target, cuid=sdk.cuid, domestic=1)
                        token = await self.service.api.exchange_sdk_token(sdk, role, roles.channelId)
                        account.token = token.token
                        account.token_state = "pending"
                        account.token_expires_at = 0
                        account.token_renew_after = 0
                        row.status = "verifying"
                        row.account_id = target
                        row.token_digest = session_key(token.token)
                        await Store.save(account, row)
                        # 先落盘新token；期限解析失败也不得丢弃它或重新交换。
                        account.token_expires_at = token_expiry(token.expire_time)
                        await Store.save(account)
                    else:
                        account = await Store.account(target)
                        if account is None or session_key(account.token) != row.token_digest:
                            row.status = "failed"
                            raise WuwaError("本次登录凭据已被更新，请重新发送 wt登录。")
                    try:
                        info = await self.service.api.me(account.token, fresh=True)
                    except ApiError as error:
                        if error.is_auth:
                            # 仍持有账号锁，不能用旧快照覆盖并发更新的 token。
                            await self.service.expire_account(account)
                            row.status = "failed"
                        raise
                    if (
                        str(info.cuid) != account.cuid
                        or (info.is_domestic is not None and info.is_domestic != 1)
                        or str(info.server_id) not in ("0", CN_SERVER_ID)
                    ):
                        row.status = "failed"
                        raise DataError("登录结果的账号或区服不一致，本次未建立绑定。")
                    account.info = info.model_dump_json()
                    account.verified_at = time.time()
                    account.token_state = "ready"
                    await Store.save(
                        account,
                        Binding(id=row.owner_id, platform=row.platform, user_id=row.user_id, account_id=target),
                        AccountLink(owner_id=row.owner_id, account_id=target),
                    )
                    await self.service.failures.success("renew:" + target)
                    try:
                        note = await self.service.resume_after_token(account, info)
                    except SQLAlchemyError:
                        raise
                    except Exception as error:
                        logger.error(f"[WuwaTappoUID] 登录成功但任务恢复失败：{self.service.error_detail(error)}")
                        note = f"登录成功，但任务尚未恢复：{self.service.error_detail(error)}；可稍后用 wt恢复。"
                    row.status = "success"
                    row.message = "登录成功，已绑定为当前账号。"
                    row.result = json.dumps(
                        {
                            "cuid": mask_cuid(account.cuid),
                            "region": "国服",
                            "player_name": info.display_name,
                            "task_note": note,
                        },
                        ensure_ascii=False,
                    )
                    await Store.save(
                        row,
                        Notice(
                            subscription_id=row.subscription_id,
                            account_id=target,
                            text=row.message + "\n" + note,
                        ),
                    )
                    self.wake_notices()
            except WuwaError as error:
                if row.status == "success":
                    raise
                # 新token取得后只允许继续验证；不会再次提交SDK认证。
                if row.status != "verifying":
                    row.status = "failed"
                row.message = str(error)
                await Store.save(row)
            except SQLAlchemyError as error:
                await self.service.disable_database(error)
                raise WuwaError(self.service.database_error) from None
            except Exception:
                logger.exception("[WuwaTappoUID] 网页登录处理异常")
                row.status = "failed"
                row.message = "登录处理出现异常，已记录，请重新发送 wt登录。"
                await Store.save(row)
        return self.response(row)

    def wake_notices(self) -> None:
        job = scheduler.get_job("WuwaTappoUID:notice")
        if job is not None:
            job.modify(next_run_time=datetime.now(timezone.utc))

    async def poll_external(self) -> None:
        if not self.service.database_ready or self.service.closing:
            return
        try:
            rows = await Store.external_logins()
            await asyncio.gather(*(self.poll_one(row) for row in rows))
        except SQLAlchemyError as error:
            await self.service.disable_database(error)

    async def poll_one(self, snapshot: LoginSession) -> None:
        # 本地回调或另一轮认证持有用户锁时，不启动第二次认证。
        if self.service.user_lock(snapshot.owner_id).locked():
            return
        attempts, retry_after = self._remote_retries.get(snapshot.id, (0, 0))
        if snapshot.expires_at > time.time() and retry_after > time.time():
            return
        try:
            row = await self.get_key(snapshot.id)
            if row.expires_at <= time.time():
                # 链接过期停止通信；已经成功的绑定和保存的 token 保留。
                row.external_synced = True
                row.external_poll_token = ""
                await Store.save(row)
                self._remote_retries.pop(row.id, None)
                return
            if row.status == "processing":
                return
            if row.status in ("pending", "verifying"):
                data = await login_remote.poll(self.service.api, row)
                if not data.submitted:
                    self._remote_retries.pop(row.id, None)
                    return
                result = await self.complete_key(row.id, data.login)
                row = await Store.login_session(row.id)
            else:
                result = self.response(row)
            await login_remote.confirm(self.service.api, row, result)
            # verifying 会话等待用户在页面点击继续，只验证已保存的 token。
            if row.status not in ("pending", "verifying"):
                row.external_synced = True
                row.external_poll_token = ""
                await Store.save(row)
            self._remote_retries.pop(row.id, None)
        except SQLAlchemyError:
            raise
        except Exception as error:
            row = await Store.login_session(snapshot.id)
            if row.expires_at <= time.time():
                # 正常到期不触发后台故障告警，也不撤销已完成的账号绑定。
                row.external_synced = True
                row.external_poll_token = ""
                await Store.save(row)
                self._remote_retries.pop(row.id, None)
                return
            detail = self.service.error_detail(error)
            if attempts == 0:
                if isinstance(error, WuwaError):
                    logger.warning(f"[WuwaTappoUID] 外置登录通信失败，将在链接有效期内退避重试：{detail}")
                else:
                    logger.exception("[WuwaTappoUID] 外置登录轮询出现程序异常，将在链接有效期内退避重试")
            delay = (3, 6, 12, 24, 30)[min(attempts, 4)]
            self._remote_retries[row.id] = (attempts + 1, time.time() + delay)
