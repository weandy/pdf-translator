"""单一管理员密码建立 Cookie 会话；不复用翻译凭据依赖。"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from pdftr.api.admin_deps import admin_auth, enforce_csrf, rate_error, require_admin
from pdftr.api.client_ip import resolve_client_ip
from pdftr.api.deps import container
from pdftr.domain.admin import SESSION_COOKIE, SESSION_SECONDS
from pdftr.pipeline.admin import AdminRateLimited, InvalidAdminCredentials

router = APIRouter(prefix="/api/admin", tags=["admin"])


class AdminLoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=256, repr=False)

    @field_validator("password")
    @classmethod
    def check_password_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 256:
            raise ValueError("密码不得超过 256 个 UTF-8 字节")
        return value


class AdminSessionOut(BaseModel):
    csrf_token: str
    expires_at: datetime


@router.post("/session", response_model=AdminSessionOut)
async def login(body: AdminLoginIn, request: Request, response: Response) -> AdminSessionOut:
    auth = admin_auth(request)
    client = resolve_client_ip(request, container(request).settings)
    try:
        token, identity = await auth.login(body.password, client.address)
    except InvalidAdminCredentials as exc:
        raise HTTPException(401, "管理密码错误") from exc
    except AdminRateLimited as exc:
        raise rate_error(exc) from exc
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_SECONDS,
        expires=identity.expires_at,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return AdminSessionOut(csrf_token=identity.csrf_token, expires_at=identity.expires_at)


@router.get("/session", response_model=AdminSessionOut)
def current_session(request: Request) -> AdminSessionOut:
    identity = require_admin(request)
    return AdminSessionOut(csrf_token=identity.csrf_token, expires_at=identity.expires_at)


@router.delete("/session", status_code=204)
def logout(request: Request) -> Response:
    identity = require_admin(request)
    enforce_csrf(request, identity)
    admin_auth(request).logout(identity)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="strict", path="/")
    return response
