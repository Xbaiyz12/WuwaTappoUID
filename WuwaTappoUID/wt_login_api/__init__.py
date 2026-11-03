"""插件共享 HTTP 服务上的手机号验证码登录页与一次性认证结果。"""

from pathlib import Path

from fastapi import Request
from gsuid_core.webconsole.app_app import app
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import FileResponse, JSONResponse

from ..runtime import login, service
from ..utils.login_config import SDK_CONFIG
from ..utils.schema import SdkLogin, WuwaError

ASSETS = Path(__file__).parent.parent / "assets"
PAGE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class StatusInput(BaseModel):
    state: str = Field(min_length=1, max_length=128, repr=False)


class CompleteInput(StatusInput):
    login: SdkLogin | None = None


@app.get("/wuwatappouid/login", include_in_schema=False)
async def wuwatappouid_login_page():
    return FileResponse(ASSETS / "login" / "login.html", headers=PAGE_HEADERS)


@app.get("/wuwatappouid/assets/{name}", include_in_schema=False)
async def wuwatappouid_login_asset(name: str):
    files = {
        "logo.webp": ASSETS / "tappo" / "logo.webp",
        "bg.jpg": ASSETS / "tappo" / "bg.jpg",
        "tappo.woff2": ASSETS / "tappo" / "tappo.woff2",
        "misans.woff2": ASSETS / "login" / "misans.woff2",
    }
    if name not in files:
        return JSONResponse({"status": 1, "msg": "素材不存在", "data": {}}, status_code=404)
    return FileResponse(files[name], headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/wuwatappouid/login/sdk-config", include_in_schema=False)
async def wuwatappouid_sdk_config():
    return JSONResponse({"status": 0, "msg": "", "data": SDK_CONFIG}, headers=PAGE_HEADERS)


async def login_request(request: Request, *, complete: bool) -> JSONResponse:
    try:
        body = await request.json()
        payload = CompleteInput.model_validate(body) if complete else StatusInput.model_validate(body)
    except (ValueError, ValidationError):
        # 不使用默认 422 响应，避免回显 SDK token 和原始输入。
        return JSONResponse(
            {"status": 1, "msg": "登录请求参数不正确，请重新打开登录链接。", "data": {}},
            headers=PAGE_HEADERS,
        )
    try:
        if isinstance(payload, CompleteInput):
            result = await login.complete(payload.state, payload.login)
        else:
            result = await login.status(payload.state)
    except WuwaError as error:
        result = {"status": 1, "msg": str(error), "data": {}}
    except SQLAlchemyError as error:
        await service.disable_database(error)
        result = {"status": 1, "msg": service.database_error, "data": {}}
    return JSONResponse(result, headers=PAGE_HEADERS)


@app.post("/api/wuwatappouid/login/status", include_in_schema=False)
async def wuwatappouid_login_status(request: Request):
    return await login_request(request, complete=False)


@app.post("/api/wuwatappouid/login/complete", include_in_schema=False)
async def wuwatappouid_login_complete(request: Request):
    return await login_request(request, complete=True)
