"""FastAPI 应用工厂。

启动：uvicorn pdftr.api.app:create_app --factory
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from pdftr import __version__
from pdftr.api import routes_jobs, routes_meta, routes_segments
from pdftr.container import Container, build_container
from pdftr.domain.llm import ChatError
from pdftr.pipeline.usecases import NotFound, UserError

# 接口只返回 JSON / 文件，不需要加载任何脚本或样式
_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def create_app(container: Container | None = None) -> FastAPI:
    c = container or build_container()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        c.pipeline.recover_interrupted()
        yield

    app = FastAPI(title="pdf-translator", version=__version__, lifespan=lifespan)
    app.state.container = c

    if c.settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=c.settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next) -> Response:
        response = await call_next(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    @app.exception_handler(UserError)
    async def user_error(_: Request, e: UserError) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.exception_handler(NotFound)
    async def not_found(_: Request, e: NotFound) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=404)

    @app.exception_handler(ChatError)
    async def chat_error(_: Request, e: ChatError) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=502)

    app.include_router(routes_meta.router)
    app.include_router(routes_jobs.router)
    app.include_router(routes_segments.router)
    return app
