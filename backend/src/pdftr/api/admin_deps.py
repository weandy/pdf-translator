"""管理接口复用的认证、CSRF、限流和有界 HTTP 请求边界。"""

import hmac
import json

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pdftr.api.deps import container
from pdftr.domain.admin import SESSION_COOKIE, AdminIdentity
from pdftr.pipeline.admin import AdminAuth, AdminRateLimited, InvalidAdminCredentials

MAX_ADMIN_BODY_BYTES = 8192


def admin_auth(request: Request) -> AdminAuth:
    auth = container(request).admin
    if auth is None:
        raise HTTPException(404, "本站未开启管理功能")
    return auth


def require_admin(request: Request) -> AdminIdentity:
    try:
        return admin_auth(request).authenticate(request.cookies.get(SESSION_COOKIE))
    except InvalidAdminCredentials as exc:
        raise HTTPException(401, "管理员会话无效或已到期") from exc


def enforce_origin_json(request: Request) -> None:
    auth = admin_auth(request)
    if request.headers.getlist("origin") != [auth.origin]:
        raise HTTPException(403, "管理请求来源无效")
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "管理请求必须使用 JSON")


def enforce_csrf(request: Request, identity: AdminIdentity) -> None:
    enforce_origin_json(request)
    supplied = request.headers.getlist("x-csrf-token")
    if len(supplied) != 1 or not hmac.compare_digest(supplied[0].encode("utf-8"), identity.csrf_token.encode("ascii")):
        raise HTTPException(403, "管理请求 CSRF 校验失败")


def rate_error(exc: AdminRateLimited) -> HTTPException:
    return HTTPException(429, "管理请求过于频繁，请稍后重试", headers={"Retry-After": str(exc.retry_after)})


def enforce_query_rate(request: Request, identity: AdminIdentity) -> None:
    try:
        admin_auth(request).enforce_query_rate(identity)
    except AdminRateLimited as exc:
        raise rate_error(exc) from exc


class AdminSecurityMiddleware:
    """先检查原始请求，再交给 Pydantic；管理响应包括错误均禁止缓存和凭据跨域。"""

    def __init__(self, app: ASGIApp, auth: AdminAuth | None) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/api/admin" or path.startswith("/api/admin/")):
            await self.app(scope, receive, send)
            return
        response_started = False

        async def protected_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control" and not key.lower().startswith(b"access-control-")
                ] + [(b"cache-control", b"no-store")]
            await send(message)

        async def reject(status: int, detail: str) -> None:
            await JSONResponse({"detail": detail}, status_code=status)(scope, receive, protected_send)

        async def dispatch(request_receive: Receive) -> None:
            try:
                await self.app(scope, request_receive, protected_send)
            except Exception:
                # 已发响应不能重发；只屏蔽尚未输出的管理异常，取消信号继续向上传播。
                if response_started:
                    raise
                await reject(500, "管理请求处理失败，请稍后重试")

        if self.auth is None:
            await reject(404, "本站未开启管理功能")
            return
        if scope["method"] not in {"POST", "DELETE", "PUT", "PATCH"}:
            await dispatch(receive)
            return
        request = Request(scope)
        if request.headers.getlist("origin") != [self.auth.origin]:
            await reject(403, "管理请求来源无效")
            return
        content_types = request.headers.getlist("content-type")
        if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
            await reject(415, "管理请求必须使用 JSON")
            return
        lengths = request.headers.getlist("content-length")
        if lengths:
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                await reject(400, "管理请求长度无效")
                return
            if len(lengths[0]) > 8 or int(lengths[0]) > MAX_ADMIN_BODY_BYTES:
                await reject(413, "管理请求正文过大")
                return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await reject(400, "管理请求正文不完整")
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_ADMIN_BODY_BYTES:
                await reject(413, "管理请求正文过大")
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        try:
            json.loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            await reject(422, "管理请求正文必须是有效 JSON")
            return
        delivered = False

        async def buffered_receive() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await dispatch(buffered_receive)
