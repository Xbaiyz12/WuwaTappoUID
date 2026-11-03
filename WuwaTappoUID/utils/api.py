"""异步 HTTP 与脚本使用的 Engine.IO v4 点击协议。"""

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import ParamSpec, TypeVar

import aiohttp
from gsuid_core.logger import logger
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from .schema import (
    BASE,
    CODE_OK,
    CODE_TOKEN_INVALID,
    ORIGIN,
    UA,
    ApiError,
    DataError,
    Envelope,
    Expired,
    Item,
    LoginData,
    NetworkError,
    OpenResult,
    RoleInfo,
    RoleQuery,
    SdkLogin,
    Side,
    SynthesisResult,
    UserInfo,
)

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")
P = ParamSpec("P")
JSON = TypeAdapter(JsonValue)
ITEMS = TypeAdapter(list[Item])
RETRY_DELAYS = (1, 3, 5)
RESOURCE_TIMEOUT = aiohttp.ClientTimeout(total=25, connect=8, sock_read=12)


@dataclass
class ReadCache:
    active: bool = True
    users: dict[tuple[str, int], UserInfo] = field(default_factory=dict)
    bags: dict[tuple[str, int], list[Item]] = field(default_factory=dict)
    roles: dict[tuple[str, int], list[RoleInfo]] = field(default_factory=dict)


_read_cache: ContextVar[ReadCache | None] = ContextVar("wt_read_cache", default=None)


def with_read_cache(operation: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    @wraps(operation)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        cache = ReadCache()
        token = _read_cache.set(cache)
        try:
            return await operation(*args, **kwargs)
        finally:
            cache.active = False
            _read_cache.reset(token)

    return wrapped


def network_details(path: str, error: Exception, status: int | None = None, request_id: str = "") -> str:
    if isinstance(error, aiohttp.ClientConnectorCertificateError):
        reason = str(error.certificate_error)
    elif isinstance(error, aiohttp.ClientConnectorError):
        reason = str(error.os_error)
    elif isinstance(error, TimeoutError):
        reason = "请求超时"
    else:
        reason = type(error).__name__
    return f"接口 {path}，HTTP {status}，异常 {type(error).__name__}，原因 {reason}，请求ID {request_id or '未提供'}"


async def retry_network(operation: Callable[[], Awaitable[R]], label: str) -> R:
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return await operation()
        except NetworkError as error:
            if attempt == len(RETRY_DELAYS):
                logger.warning(f"[WuwaTappoUID] {label}已重试3次仍未完成：{error.details}。")
                raise NetworkError(f"{error}（已重试 3 次）", error.details) from None
            delay = RETRY_DELAYS[attempt]
            if attempt == 0:
                logger.warning(f"[WuwaTappoUID] {label}首次网络失败，将按1/3/5秒重试：{error.details}。")
            await asyncio.sleep(delay)
    raise RuntimeError("网络重试没有产生结果")


@dataclass
class Download:
    body: bytes
    etag: str = ""
    last_modified: str = ""
    not_modified: bool = False


def parse(model: type[T], data: object) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        fields = "、".join(
            f"{'.'.join(str(part) for part in entry['loc']) or '响应'}:{entry['type']}"
            for entry in error.errors(include_input=False, include_context=False, include_url=False)
        )
        raise DataError(f"{model.__name__}响应字段格式已变化：{fields}。") from None


class WuwaAPI:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None
        self._versions: dict[str, int] = {}

    def _read_key(self, token: str) -> tuple[str, int]:
        key = hashlib.sha256(token.encode()).hexdigest()
        return key, self._versions.get(key, 0)

    def invalidate(self, token: str) -> None:
        key, version = self._read_key(token)
        self._versions[key] = version + 1

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={"Accept": "application/json", "User-Agent": UA, "Origin": ORIGIN, "Referer": ORIGIN + "/"},
            timeout=aiohttp.ClientTimeout(total=25),
            cookie_jar=aiohttp.DummyCookieJar(),
        )

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()

    def client(self) -> aiohttp.ClientSession:
        if self.session is None:
            raise RuntimeError("WuwaTappo 网络客户端尚未初始化")
        return self.session

    async def fetch(
        self, url: str, method: str = "GET", payload: object = None, token: str = "", *, retry: bool = True
    ) -> Envelope:
        async def request() -> Envelope:
            return await self._fetch_once(url, method, payload, token)

        if retry:
            return await retry_network(request, "接口请求")
        try:
            return await request()
        except NetworkError as error:
            logger.warning(f"[WuwaTappoUID] 本次接口请求结果未确认：{error.details}。")
            raise

    async def _fetch_once(self, url: str, method: str, payload: object, token: str) -> Envelope:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        path = url.removeprefix(BASE).split("?", 1)[0]
        request_id = ""
        status = None
        try:
            async with self.client().request(method, url, json=payload, headers=headers) as response:
                request_id = response.headers.get("x-request-id", "")
                status = response.status
                try:
                    envelope = Envelope.model_validate_json(await response.text())
                except ValidationError:
                    if response.status in (401, 403):
                        raise ApiError(None, http_status=response.status, path=path, request_id=request_id) from None
                    if response.status >= 500 or response.status in (408, 429):
                        raise NetworkError(
                            f"服务端暂不可用（HTTP {response.status}）。",
                            f"接口 {path}，HTTP {status}，请求ID {request_id or '未提供'}，无有效业务响应",
                        ) from None
                    raise DataError(f"接口 {path} 未返回有效业务响应（HTTP {response.status}）。") from None
                request_id = envelope.request_id or request_id
                if envelope.code not in CODE_OK:
                    raise ApiError(envelope.code, http_status=response.status, path=path, request_id=request_id)
                if response.status in (401, 403):
                    raise ApiError(None, http_status=response.status, path=path, request_id=request_id)
                if response.status >= 500 or response.status in (408, 429):
                    raise NetworkError(
                        f"服务端暂不可用（HTTP {response.status}），稍后再查询。",
                        f"接口 {path}，HTTP {status}，业务码 {envelope.code}，请求ID {request_id or '未提供'}",
                    )
                if response.status >= 400:
                    raise DataError(f"请求被拒绝（HTTP {response.status}），请管理员检查接口。")
                return envelope
        except (aiohttp.ClientError, TimeoutError) as error:
            raise NetworkError(
                "网络请求失败或超时，暂时无法确认服务端结果。", network_details(path, error, status, request_id)
            ) from None

    async def download(self, url: str, limit: int = 4_000_000, *, etag: str = "", last_modified: str = "") -> Download:
        async def request() -> Download:
            return await self._download_once(url, limit, etag, last_modified)

        return await retry_network(request, "资源下载")

    async def _download_once(self, url: str, limit: int, etag: str, last_modified: str) -> Download:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        try:
            async with self.client().get(url, headers=headers, timeout=RESOURCE_TIMEOUT) as response:
                if response.status == 304:
                    return Download(b"", etag, last_modified, not_modified=True)
                if response.status >= 500 or response.status in (408, 429):
                    raise NetworkError(
                        f"资源服务暂不可用（HTTP {response.status}）。",
                        f"资源 {url.split('?', 1)[0]}，HTTP {response.status}，"
                        f"请求ID {response.headers.get('x-request-id', '未提供')}",
                    )
                if response.status >= 400:
                    raise DataError(f"资源下载被拒绝（HTTP {response.status}）。")
                if response.content_length is not None and response.content_length > limit:
                    raise DataError("资源文件过大，已跳过。")
                # read(n) 可能只返回已到达的首段；read() 才会等完整响应。
                body = await response.read()
                if len(body) > limit:
                    raise DataError("资源文件过大，已跳过。")
                return Download(body, response.headers.get("ETag", ""), response.headers.get("Last-Modified", ""))
        except (aiohttp.ClientError, TimeoutError) as error:
            raise NetworkError("资源下载失败或超时。", network_details(url.split("?", 1)[0], error)) from None

    async def fetch_bytes(self, url: str, limit: int = 4_000_000) -> bytes:
        return (await self.download(url, limit)).body

    async def request(
        self, path: str, method: str = "POST", payload: object = None, token: str = "", *, retry: bool = True
    ) -> JsonValue:
        try:
            response = await self.fetch(BASE + path, method, payload, token, retry=retry)
        except ApiError as error:
            if token and error.is_auth:
                self.invalidate(token)
            logger.warning(f"[WuwaTappoUID] 接口拒绝请求：{error.details}。")
            raise
        return response.payload()

    async def exchange_sdk_token(self, login: SdkLogin, role: RoleInfo, channel_id: int | str) -> LoginData:
        """仅显式网页登录使用真实 KrSDK 凭据；认证请求不自动重发。"""
        payload = {
            "cuid": login.cuid,
            "puid": login.sdk_openid,
            "channelId": channel_id,
            "userid": login.sdk_openid,
            "cname": login.username,
            "accessToken": login.token,
            "source": "sdk",
            "playerId": int(role.playerId),
            "serverId": role.serverId,
            "is_domestic": 1,
        }
        result = parse(LoginData, await self.request("/api/auth/login", payload=payload, retry=False))
        return result

    async def login_roles(self, cuid: str) -> RoleQuery:
        data = await self.request("/api/auth/player/query", payload={"cuid": cuid, "is_domestic": 1})
        return parse(RoleQuery, data)

    async def renew_token(self, token: str) -> None:
        # 续期延长服务端期限，超时不盲目重复；仍保留原 token。
        result = await self.request("/api/frontend/user/renew-token", "GET", token=token, retry=False)
        if result is not True:
            raise DataError("token 续期接口未返回 true，原会话已保留。")

    async def roles(self, cuid: str, domestic: int) -> list[RoleInfo]:
        """不需要登录的角色查询；只读，可以随网络重试。"""
        cache = _read_cache.get()
        key = (cuid, domestic)
        if cache is not None and cache.active and key in cache.roles:
            return cache.roles[key]
        data = await self.request("/api/auth/player/query", payload={"cuid": cuid, "is_domestic": domestic})
        roles = parse(RoleQuery, data).items
        if cache is not None and cache.active:
            cache.roles[key] = roles
        return roles

    async def me(self, token: str, *, fresh: bool = False) -> UserInfo:
        cache = _read_cache.get()
        key = self._read_key(token)
        if not fresh and cache is not None and cache.active and key in cache.users:
            return cache.users[key]
        info = parse(UserInfo, await self.request("/api/frontend/user/me", "GET", token=token))
        if cache is not None and cache.active and key == self._read_key(token):
            cache.users[key] = info
        return info

    async def bags(self, token: str, *, fresh: bool = False) -> list[Item]:
        cache = _read_cache.get()
        cache_key = self._read_key(token)
        if not fresh and cache is not None and cache.active and cache_key in cache.bags:
            return cache.bags[cache_key]
        data = await self.request("/api/frontend/bags/list", payload={}, token=token)
        if isinstance(data, dict):
            for field_name in ("list", "items", "appearances"):
                if field_name in data and isinstance(data[field_name], list):
                    data = data[field_name]
                    break
        try:
            items = ITEMS.validate_python(data)
        except ValidationError:
            raise DataError("背包响应格式已变化，无法将其当作空背包。") from None
        if cache is not None and cache.active and cache_key == self._read_key(token):
            cache.bags[cache_key] = items
        return items

    async def atlas(self) -> list[Item]:
        data = await self.request("/api/common/atlas", "GET")
        try:
            return ITEMS.validate_python(data)
        except ValidationError:
            raise DataError("图鉴响应格式已变化，无法确定各品类的最高合成阶级。") from None

    async def synthesize(self, token: str, asset_ids: list[int], same_level: bool) -> SynthesisResult:
        self.invalidate(token)
        path = "/api/frontend/assets/synthesis" + ("/same-level" if same_level else "")
        # 材料会被消耗，超时或响应异常时不能盲目重发。
        data = await self.request(path, payload={"asset_ids": asset_ids}, token=token, retry=False)
        return parse(SynthesisResult, data)

    async def open(self, token: str, side: Side, total: int) -> OpenResult:
        self.invalidate(token)
        return parse(
            OpenResult,
            # 开箱由 Service 根据持久化流水确认结果后重试，不能在 HTTP 层直接重发。
            await self.request(
                "/api/frontend/chests/open", payload={"place": side, "click_num": total}, token=token, retry=False
            ),
        )

    async def set_clicks(self, token: str, total: int, expires_at: float | None = None) -> int:
        """重连时仍提交同一个目标累计值；回读已达到目标就不再发送。"""
        attempted = False

        async def submit() -> int:
            nonlocal attempted
            if attempted and (await self.me(token, fresh=True)).total_click_count >= total:
                return 0
            if expires_at is not None and time.time() >= expires_at:
                raise Expired
            attempted = True
            return await self._set_clicks_once(token, total, expires_at)

        return await retry_network(submit, "点击连接")

    async def _set_clicks_once(self, token: str, total: int, expires_at: float | None) -> int:
        self.invalidate(token)
        try:
            async with asyncio.timeout(15):
                async with self.client().ws_connect(
                    BASE.replace("https://", "wss://") + "/socket.io/",
                    params={"EIO": "4", "transport": "websocket", "token": token},
                    origin=ORIGIN,
                ) as ws:
                    sent_connect = sent_clicks = False
                    async for frame in ws:
                        if frame.type == aiohttp.WSMsgType.ERROR:
                            raise NetworkError("点击连接中断，需重新查询实际点击数。")
                        if frame.type != aiohttp.WSMsgType.TEXT:
                            continue
                        message = frame.data
                        if message == "2":
                            await ws.send_str("3")
                        elif message.startswith("0") and not sent_connect:
                            await ws.send_str("40")
                            sent_connect = True
                        elif message.startswith("40") and not sent_clicks:
                            if expires_at is not None and time.time() >= expires_at:
                                raise Expired
                            await ws.send_str("42" + json.dumps(["cr", {"num": total}], separators=(",", ":")) + ",1")
                            sent_clicks = True
                        elif message.startswith("42"):
                            event = JSON.validate_json(message[2:])
                            if isinstance(event, list) and len(event) >= 2 and event[0] == "fd":
                                body = event[1]
                                if isinstance(body, dict) and isinstance(body.get("c"), int):
                                    raise ApiError(body["c"], path="/socket.io:fd")
                        elif message.startswith("43") and sent_clicks:
                            # 部分服务端在 43 后携带 ACK id，正文从 [ 或 { 开始。
                            starts = [i for i in (message.find("["), message.find("{")) if i >= 0]
                            if not starts:
                                raise DataError("点击确认帧缺少正文。")
                            body = JSON.validate_json(message[min(starts) :])
                            if isinstance(body, list) and body:
                                body = body[0]
                            if not isinstance(body, dict) or not isinstance(body.get("c"), int):
                                raise DataError("点击确认帧格式已变化。")
                            code = body["c"]
                            if code in CODE_TOKEN_INVALID:
                                raise ApiError(code, path="/socket.io:cr")
                            return code
        except aiohttp.WSServerHandshakeError as error:
            request_id = error.headers.get("x-request-id", "") if error.headers is not None else ""
            if error.status in (401, 403):
                raise ApiError(
                    None, http_status=error.status, path="/socket.io:connect", request_id=request_id
                ) from None
            raise NetworkError(
                f"点击连接握手失败（HTTP {error.status}），需回读实际点击数。",
                network_details("/socket.io:connect", error, error.status, request_id),
            ) from None
        except (aiohttp.ClientError, TimeoutError) as error:
            raise NetworkError(
                "点击提交失败或超时，需重新查询实际点击数。", network_details("/socket.io:cr", error)
            ) from None
        except ValidationError:
            raise DataError("点击确认帧不是有效 JSON。") from None
        raise NetworkError("点击连接已关闭，未收到服务端确认。")
