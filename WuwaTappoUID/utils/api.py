"""异步 HTTP 与脚本使用的 Engine.IO v4 点击协议。"""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

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
    Side,
    SynthesisResult,
    UserInfo,
)

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")
JSON = TypeAdapter(JsonValue)
ITEMS = TypeAdapter(list[Item])
RETRY_DELAYS = (1, 3, 5)
RESOURCE_TIMEOUT = aiohttp.ClientTimeout(total=25, connect=8, sock_read=12)


async def retry_network(operation: Callable[[], Awaitable[R]], label: str) -> R:
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return await operation()
        except NetworkError as error:
            if attempt == len(RETRY_DELAYS):
                raise NetworkError(f"{error}（已重试 3 次）") from None
            delay = RETRY_DELAYS[attempt]
            logger.warning(f"[WuwaTappoUID] {label}网络失败，{delay} 秒后重试（{attempt + 1}/3）。")
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
    except ValidationError:
        raise DataError("游戏接口缺少必要字段或字段格式已变化，请管理员检查接口。") from None


class WuwaAPI:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={"Accept": "application/json", "User-Agent": UA, "Origin": ORIGIN, "Referer": ORIGIN + "/"},
            timeout=aiohttp.ClientTimeout(total=25),
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
    ) -> JsonValue:
        async def request() -> JsonValue:
            return await self._fetch_once(url, method, payload, token)

        return await retry_network(request, "接口请求") if retry else await request()

    async def _fetch_once(self, url: str, method: str, payload: object, token: str) -> JsonValue:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with self.client().request(method, url, json=payload, headers=headers) as response:
                if response.status in (401, 403):
                    raise ApiError(10002)
                if response.status >= 500 or response.status in (408, 429):
                    raise NetworkError(f"服务端暂不可用（HTTP {response.status}），稍后再查询。")
                if response.status >= 400:
                    raise DataError(f"请求被拒绝（HTTP {response.status}），请管理员检查接口。")
                return JSON.validate_json(await response.text())
        except (aiohttp.ClientError, TimeoutError):
            raise NetworkError("网络请求失败或超时，暂时无法确认服务端结果。") from None
        except ValidationError:
            raise DataError("服务端没有返回有效 JSON 数据。") from None

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
                    raise NetworkError(f"资源服务暂不可用（HTTP {response.status}）。")
                if response.status >= 400:
                    raise DataError(f"资源下载被拒绝（HTTP {response.status}）。")
                if response.content_length is not None and response.content_length > limit:
                    raise DataError("资源文件过大，已跳过。")
                # read(n) 可能只返回已到达的首段；read() 才会等完整响应。
                body = await response.read()
                if len(body) > limit:
                    raise DataError("资源文件过大，已跳过。")
                return Download(body, response.headers.get("ETag", ""), response.headers.get("Last-Modified", ""))
        except (aiohttp.ClientError, TimeoutError):
            raise NetworkError("资源下载失败或超时。") from None

    async def fetch_bytes(self, url: str, limit: int = 4_000_000) -> bytes:
        return (await self.download(url, limit)).body

    async def request(
        self, path: str, method: str = "POST", payload: object = None, token: str = "", *, retry: bool = True
    ) -> JsonValue:
        result = parse(Envelope, await self.fetch(BASE + path, method, payload, token, retry=retry))
        if result.code not in CODE_OK:
            raise ApiError(result.code)
        return result.data

    async def login(self, cuid: str, domestic: int, player_id: int | str = 0, server_id: str = "0") -> str:
        """字段形状对齐官方客户端；accessToken 一次性随机生成，插件不持有任何真实凭据。"""
        payload = {
            "cuid": cuid,
            "puid": cuid,
            "channelId": 18,
            "userid": cuid,
            "cname": f"U{cuid}A",
            "accessToken": str(uuid.uuid4()),
            "source": "sdk",
            "playerId": player_id,
            "serverId": server_id,
            "is_domestic": domestic,
        }
        result = parse(LoginData, await self.request("/api/auth/login", payload=payload))
        if not result.token:
            raise DataError("登录接口未返回有效登录态。")
        return result.token

    async def roles(self, cuid: str, domestic: int) -> list[RoleInfo]:
        """不需要登录的角色查询；只读，可以随网络重试。"""
        data = await self.request("/api/auth/player/query", payload={"cuid": cuid, "is_domestic": domestic})
        return parse(RoleQuery, data).items

    async def me(self, token: str) -> UserInfo:
        return parse(UserInfo, await self.request("/api/frontend/user/me", "GET", token=token))

    async def bags(self, token: str) -> list[Item]:
        data = await self.request("/api/frontend/bags/list", payload={}, token=token)
        if isinstance(data, dict):
            for key in ("list", "items", "appearances"):
                if key in data and isinstance(data[key], list):
                    data = data[key]
                    break
        try:
            return ITEMS.validate_python(data)
        except ValidationError:
            raise DataError("背包响应格式已变化，无法将其当作空背包。") from None

    async def atlas(self) -> list[Item]:
        data = await self.request("/api/common/atlas", "GET")
        try:
            return ITEMS.validate_python(data)
        except ValidationError:
            raise DataError("图鉴响应格式已变化，无法确定各品类的最高合成阶级。") from None

    async def synthesize(self, token: str, asset_ids: list[int], same_level: bool) -> SynthesisResult:
        path = "/api/frontend/assets/synthesis" + ("/same-level" if same_level else "")
        # 材料会被消耗，超时或响应异常时不能盲目重发。
        data = await self.request(path, payload={"asset_ids": asset_ids}, token=token, retry=False)
        return parse(SynthesisResult, data)

    async def open(self, token: str, side: Side, total: int) -> OpenResult:
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
            if attempted and (await self.me(token)).total_click_count >= total:
                return 0
            if expires_at is not None and time.time() >= expires_at:
                raise Expired
            attempted = True
            return await self._set_clicks_once(token, total, expires_at)

        return await retry_network(submit, "点击连接")

    async def _set_clicks_once(self, token: str, total: int, expires_at: float | None) -> int:
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
                                raise ApiError(code)
                            return code
        except (aiohttp.ClientError, TimeoutError):
            raise NetworkError("点击提交失败或超时，需重新查询实际点击数。") from None
        except ValidationError:
            raise DataError("点击确认帧不是有效 JSON。") from None
        raise NetworkError("点击连接已关闭，未收到服务端确认。")
