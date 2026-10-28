"""FastAPI 应用工厂。

启动：uvicorn pdftr.api.app:create_app --factory
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from pdftr import __version__
from pdftr.api import routes_jobs, routes_meta, routes_segments
from pdftr.container import Container, build_container
from pdftr.domain.llm import ChatError
from pdftr.pipeline.usecases import NotFound, ServiceBusy, UserError, purge_expired

log = logging.getLogger(__name__)

# 接口只返回 JSON / 文件，不需要加载任何脚本或样式
_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


async def _purge_periodically(c: Container, interval_s: float = 3600) -> None:
    """启动时和之后每小时清理一次过期任务。"""
    while True:
        try:
            await asyncio.to_thread(purge_expired, c.pipeline, c.settings.retention_days)
        except Exception:
            log.exception("清理过期任务失败")
        await asyncio.sleep(interval_s)


def create_app(container: Container | None = None) -> FastAPI:
    c = container or build_container()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        c.pipeline.recover_interrupted()
        housekeeping = asyncio.create_task(_purge_periodically(c))
        # 在后台预先下载并加载术语库，第一个任务不用等
        warmup = asyncio.create_task(asyncio.to_thread(c.pipeline.deps.term_matcher))
        yield
        housekeeping.cancel()
        warmup.cancel()
        # 部署新版本时先让进行中的任务跑完，避免用户的翻译被中途打断
        if c.pipeline.active_count():
            log.info(
                "等待 %d 个进行中的任务完成（最多 %.0f 秒）", c.pipeline.active_count(), c.settings.shutdown_grace_s
            )
            if not await c.pipeline.wait_all(c.settings.shutdown_grace_s):
                log.warning("仍有任务未完成，服务停止后可从断点重试")

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

    @app.exception_handler(ServiceBusy)
    async def service_busy(_: Request, e: ServiceBusy) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=503, headers={"Retry-After": "300"})

    @app.exception_handler(ChatError)
    async def chat_error(_: Request, e: ChatError) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=502)

    app.include_router(routes_meta.router)
    app.include_router(routes_jobs.router)
    app.include_router(routes_segments.router)
    return app
