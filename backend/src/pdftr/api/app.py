"""FastAPI 应用工厂。

启动：uvicorn pdftr.api.app:create_app --factory
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from pdftr import __version__
from pdftr.api import (
    routes_admin,
    routes_admin_config,
    routes_admin_submissions,
    routes_jobs,
    routes_meta,
    routes_segments,
    routes_stats,
)
from pdftr.api.admin_deps import AdminSecurityMiddleware
from pdftr.container import Container, build_container
from pdftr.domain.llm import ChatError
from pdftr.pipeline.usecases import NotFound, ServiceBusy, UserError, purge_expired
from pdftr.pipeline.workers import run_thread
from pdftr.storage.runtime_config import RevisionConflictError

log = logging.getLogger(__name__)

# 接口只返回 JSON / 文件，不需要加载任何脚本或样式
_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# 管理接口 422 仅允许透出下列非敏感字段名，严禁回显 input、密钥、Token 或代理值
_ADMIN_ALLOWED_422_FIELDS = frozenset(
    {
        "body",
        "password",
        "title_query",
        "ip",
        "date_from",
        "date_to",
        "page",
        "page_size",
        "channel_mode",
        "status",
        "client_device",
        "expected_revision",
        "target_revision",
        "enabled",
        "max_pages",
        "notice",
        "base_url",
        "model",
        "fallback_model",
        "timeout_s",
        "protocol",
        "mineru_base_url",
        "paddle_public_enabled",
        "max_upload_mb",
        "max_concurrent_jobs",
        "max_concurrent_translations",
        "free_max_concurrent_translations",
        "max_queued_jobs",
        "free_max_queued_jobs",
        "retention_hours",
        "min_free_disk_mb",
        "maintenance_mode",
        "site_notice",
        "sources",
        "providers",
        "presets",
        "models",
        "cache_hit",
        "cache_miss",
        "output",
        "peak_factor",
    }
)


def _preflight_reserve_upload(c: Container, request: Request) -> tuple[str | None, Literal["byok", "free"] | None]:
    """在 FastAPI 解析 multipart 之前检查维护模式并原子预留通道名额，避免并发大文件穿透临时落盘。"""
    snap = c.runtime_config.snapshot() if c.runtime_config is not None else None
    sys_cfg = snap.data.system if snap is not None else None
    if sys_cfg is not None and sys_cfg.maintenance_mode:
        return sys_cfg.site_notice.strip() or "系统维护中，暂不接收新任务，已完成任务仍可阅读与下载", None
    raw_mode = (request.headers.get("x-translation-mode") or "byok").strip().lower()
    if raw_mode not in ("byok", "free"):
        return None, None
    mode: Literal["byok", "free"] = "free" if raw_mode == "free" else "byok"
    if snap is not None and snap.revision > 0 and sys_cfg is not None:
        max_byok = sys_cfg.max_concurrent_jobs + sys_cfg.max_queued_jobs
        max_free = sys_cfg.free_max_concurrent_translations + sys_cfg.free_max_queued_jobs
    else:
        max_byok = c.settings.max_concurrent_jobs + c.settings.max_queued_jobs
        max_free = sys_cfg.free_max_concurrent_translations + sys_cfg.free_max_queued_jobs if sys_cfg is not None else 7
    limit = max_free if mode == "free" else max_byok
    if not c.pipeline.try_reserve_upload(mode, limit):
        if mode == "free":
            return "免费体验通道正忙，排队的任务较多，请稍后再试或切换自备 API Key", None
        return "服务器正忙，排队的任务太多，请稍后再试", None
    return None, mode


async def _run_purge_once(c: Container) -> None:
    try:
        snap = c.runtime_config.snapshot() if c.runtime_config is not None else None
        retention = (
            snap.data.system.retention_hours if snap is not None and snap.revision > 0 else c.settings.retention_hours
        )
        await run_thread(purge_expired, c.pipeline, retention)
    except Exception:
        log.exception("清理过期任务失败")
    if c.admin is not None:
        try:
            await asyncio.to_thread(c.admin.purge)
        except Exception:
            log.exception("清理过期管理会话失败")


async def _purge_periodically(
    c: Container,
    interval_s: float = 600,
    *,
    stop_event: asyncio.Event | None = None,
    active_purge: list[asyncio.Task[None]] | None = None,
) -> None:
    """启动时和之后每 10 分钟清理一次到期任务（每轮读取最新热配置保留期）；停机时可追踪在途清理线程。"""
    stop = stop_event or asyncio.Event()
    tracked = active_purge if active_purge is not None else []
    while not stop.is_set():
        task = asyncio.create_task(_run_purge_once(c))
        tracked[:] = [task]
        try:
            await asyncio.shield(task)
        finally:
            if tracked and tracked[0] is task and task.done():
                tracked.clear()
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            continue


def create_app(container: Container | None = None) -> FastAPI:
    owns_container = container is None
    c = container or build_container()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if c.runtime_config is not None:
            c.runtime_config.bind_loop(asyncio.get_running_loop())
        c.pipeline.recover_interrupted()
        stop_housekeeping = asyncio.Event()
        active_purge: list[asyncio.Task[None]] = []
        housekeeping = asyncio.create_task(
            _purge_periodically(c, stop_event=stop_housekeeping, active_purge=active_purge)
        )
        # 在后台预先下载并加载术语库，第一个任务不用等
        warmup = asyncio.create_task(asyncio.to_thread(c.pipeline.deps.term_matcher))
        try:
            yield
        finally:
            stop_housekeeping.set()
            housekeeping.cancel()
            with suppress(asyncio.CancelledError):
                await housekeeping
            warmup.cancel()
            with suppress(asyncio.CancelledError):
                await warmup
            deadline = time.monotonic() + max(0.0, float(c.settings.shutdown_grace_s))
            purge_drained = True
            running_purges = [t for t in active_purge if not t.done()]
            if running_purges:
                rem = max(0.0, deadline - time.monotonic())
                _done_purges, pending_purges = await asyncio.wait(running_purges, timeout=rem)
                purge_drained = not pending_purges
            # 部署新版本时先让进行中的任务跑完，避免用户的翻译被中途打断，也绝不先关库后写库
            pending = c.pipeline.pending_work_count()
            pipeline_drained = True
            if pending:
                rem = max(0.0, deadline - time.monotonic())
                log.info("等待 %d 项翻译与导出工作完成（最多 %.1f 秒）", pending, rem)
                pipeline_drained = await c.pipeline.wait_all(rem)
                if not pipeline_drained:
                    log.warning("仍有任务未完成，服务停止后可从断点重试")
            if owns_container:
                if purge_drained and pipeline_drained:
                    await run_thread(c.close)
                else:
                    log.warning("停机宽限期耗尽且仍有后台任务或清理线程未退出，跳过强制关闭数据库连接以避免写库异常")
            elif purge_drained and pipeline_drained and c.pipeline.deps.layout_cache is not None:
                await run_thread(c.pipeline.deps.layout_cache.close)

    app = FastAPI(title="pdf-translator", version=__version__, lifespan=lifespan)
    app.state.container = c

    if c.settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=c.settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(AdminSecurityMiddleware, auth=c.admin)

    @app.exception_handler(RequestValidationError)
    async def request_validation(request: Request, e: RequestValidationError) -> Response:
        if request.url.path == "/api/admin" or request.url.path.startswith("/api/admin/"):
            # FastAPI 默认会回显 input；密码、密钥、标题及 IP 筛选都不能出现在错误正文里。
            detail = [
                {
                    "type": error.get("type", "value_error"),
                    "loc": [part for part in error.get("loc", ()) if part in _ADMIN_ALLOWED_422_FIELDS] or ["body"],
                    "msg": "管理请求参数无效",
                }
                for error in e.errors()
            ]
            return JSONResponse({"detail": detail}, status_code=422)
        return await request_validation_exception_handler(request, e)

    @app.middleware("http")
    async def security_headers(request: Request, call_next) -> Response:
        reserved_mode: Literal["byok", "free"] | None = None
        if request.method == "POST" and request.url.path == "/api/jobs":
            busy_detail, reserved_mode = _preflight_reserve_upload(c, request)
            if busy_detail is not None:
                return JSONResponse(
                    {"detail": busy_detail},
                    status_code=503,
                    headers={"Retry-After": "300", **_SECURITY_HEADERS},
                )
            request.state.upload_reserved_mode = reserved_mode
        try:
            response = await call_next(request)
        finally:
            if reserved_mode is not None:
                c.pipeline.release_upload(reserved_mode)
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    @app.exception_handler(UserError)
    async def user_error(_: Request, e: UserError) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.exception_handler(NotFound)
    async def not_found(_: Request, e: NotFound) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=404)

    @app.exception_handler(RevisionConflictError)
    async def revision_conflict(_: Request, __: RevisionConflictError) -> JSONResponse:
        return JSONResponse({"detail": "配置已被其他窗口更新，请刷新后重试"}, status_code=409)

    @app.exception_handler(ServiceBusy)
    async def service_busy(_: Request, e: ServiceBusy) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=503, headers={"Retry-After": "300"})

    @app.exception_handler(ChatError)
    async def chat_error(_: Request, e: ChatError) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=502)

    app.include_router(routes_meta.router)
    app.include_router(routes_jobs.router)
    app.include_router(routes_segments.router)
    app.include_router(routes_stats.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_admin_config.router)
    app.include_router(routes_admin_submissions.router)
    return app
