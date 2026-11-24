"""任务接口：创建、详情、删除、进度推送（SSE）、阅读器数据、文件、导出、重试。

不提供任务列表：任务 ID 是随机的，只有上传者知道；“我的任务”由前端保存在浏览器里。
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import ExitStack
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import ValidationError

from pdftr.api.client_ip import resolve_client_ip
from pdftr.api.deps import container, pipeline, resolve_existing_job_secrets, resolve_upload_secrets
from pdftr.api.schemas import DocumentOut, ExportPrepareOut, JobOut, RetranslateManyIn, RevisionOut, SegmentOut
from pdftr.domain.job import JobOptions, JobStatus
from pdftr.domain.submissions import NOTICE_VERSION, SubmissionContext
from pdftr.pipeline import usecases
from pdftr.pipeline.runner import ExportResult, Pipeline
from pdftr.pipeline.secrets import Secrets
from pdftr.pipeline.workers import run_thread

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

PipelineDep = Annotated[Pipeline, Depends(pipeline)]
UploadSecretsDep = Annotated[Secrets, Depends(resolve_upload_secrets)]


def _maintenance_state(request: Request) -> tuple[bool, str]:
    c = container(request)
    if c.runtime_config is None:
        return False, ""
    sys_cfg = c.runtime_config.snapshot().data.system
    return sys_cfg.maintenance_mode, sys_cfg.site_notice


def _build_limits(request: Request, sec: Secrets) -> usecases.Limits:
    c = container(request)
    settings = c.settings
    snap = c.runtime_config.snapshot() if c.runtime_config is not None else None
    paddle_public_enabled = snap.data.parser.paddle_public_enabled if snap is not None else False
    # 免费模式或自备模式下未传入个人 X-Paddle-Key 时，均视为使用站点 PaddleOCR 能力，须受公开开关约束
    user_has_paddle_key = sec.channel_mode == "byok" and bool((request.headers.get("x-paddle-key") or "").strip())
    using_site_paddle_token = not user_has_paddle_key
    if snap is not None and snap.revision > 0:
        sys_cfg = snap.data.system
        return usecases.Limits(
            max_upload_mb=sys_cfg.max_upload_mb,
            max_pages=sys_cfg.max_pages,
            max_active_jobs=sys_cfg.max_concurrent_jobs + sys_cfg.max_queued_jobs,
            free_max_pages=snap.data.free_translation.max_pages,
            free_max_active_jobs=sys_cfg.free_max_concurrent_translations + sys_cfg.free_max_queued_jobs,
            min_free_disk_mb=sys_cfg.min_free_disk_mb,
            retention_hours=sys_cfg.retention_hours,
            maintenance_mode=sys_cfg.maintenance_mode,
            site_notice=sys_cfg.site_notice,
            paddle_public_enabled=paddle_public_enabled,
            using_site_paddle_token=using_site_paddle_token,
        )
    sys_cfg = snap.data.system if snap is not None else None
    return usecases.Limits(
        max_upload_mb=settings.max_upload_mb,
        max_pages=settings.max_pages,
        max_active_jobs=settings.max_concurrent_jobs + settings.max_queued_jobs,
        free_max_pages=snap.data.free_translation.max_pages if snap is not None else None,
        free_max_active_jobs=(
            sys_cfg.free_max_concurrent_translations + sys_cfg.free_max_queued_jobs if sys_cfg is not None else 7
        ),
        min_free_disk_mb=settings.min_free_disk_mb,
        retention_hours=settings.retention_hours,
        maintenance_mode=sys_cfg.maintenance_mode if sys_cfg is not None else False,
        site_notice=sys_cfg.site_notice if sys_cfg is not None else "",
        paddle_public_enabled=paddle_public_enabled,
        using_site_paddle_token=using_site_paddle_token,
    )


@router.post("", response_model=JobOut, status_code=202)
async def create_job(
    request: Request,
    p: PipelineDep,
    sec: UploadSecretsDep,
    file: Annotated[UploadFile, File()],
    options: Annotated[str, Form()] = "{}",
) -> JobOut:
    c = container(request)
    settings = c.settings
    try:
        opts = JobOptions.model_validate_json(options)
    except ValidationError as e:
        raise usecases.UserError("任务选项格式错误") from e

    if sec.channel_mode == "free":
        # 按本次实际选择的解析器（mineru-cloud / paddleocr-vl）精确校验站点解析凭据
        sec = c.free_secrets(require_parser=True, parser=opts.parser)

    limits = _build_limits(request, sec)

    user_agent = request.headers.get("user-agent")
    if settings.submission_audit_enabled:
        client_ip = resolve_client_ip(request, settings)
        context = SubmissionContext(
            submitter_ip=client_ip.address,
            ip_source=client_ip.source,
            notice_version=NOTICE_VERSION,
            user_agent=user_agent,
            channel_mode=sec.channel_mode,
            model_name=sec.llm_model,
        )
    else:
        context = SubmissionContext(
            user_agent=user_agent,
            channel_mode=sec.channel_mode,
            model_name=sec.llm_model,
        )
    already_reserved = getattr(request.state, "upload_reserved_mode", None) == sec.channel_mode
    job = await usecases.receive_upload(
        p,
        file.file,
        file.filename or "document.pdf",
        opts,
        sec,
        limits,
        context,
        already_reserved=already_reserved,
    )
    return JobOut.of(job)


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, p: PipelineDep) -> JobOut:
    return JobOut.of(usecases.get_job(p, job_id))


@router.delete("/{job_id}", status_code=204)
def delete_job(job_id: str, p: PipelineDep) -> Response:
    usecases.delete_job(p, job_id)
    return Response(status_code=204)


@router.get("/{job_id}/events")
async def job_events(job_id: str, request: Request, p: PipelineDep) -> StreamingResponse:
    usecases.get_job(p, job_id)  # 响应开始前校验，未知任务仍返回正常的 404。

    async def stream() -> AsyncIterator[str]:
        q = p.deps.events.subscribe(job_id)
        try:
            # 先订阅再读取快照，避免任务在响应建立与订阅之间完成，导致漏掉唯一的完成事件。
            current = usecases.get_job(p, job_id)
            while True:
                yield f"data: {JobOut.of(current).model_dump_json()}\n\n"
                if current.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
                    return
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        current = await asyncio.wait_for(q.get(), timeout=15)
                        break
                    except TimeoutError:
                        # 数据心跳既保持连接，也纠正漏掉的事件，不能永久停留在旧进度。
                        current = usecases.get_job(p, job_id)
                        break
        finally:
            p.deps.events.unsubscribe(job_id, q)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


@router.get("/{job_id}/document", response_model=DocumentOut)
def get_document(job_id: str, p: PipelineDep) -> DocumentOut:
    view = usecases.document_view(p, job_id)
    return DocumentOut.of(view.job, view.document, view.segments, view.links, view.outline)


@router.get("/{job_id}/files/{name}")
def get_file(job_id: str, name: Literal["original.pdf", "background.pdf"], p: PipelineDep) -> FileResponse:
    usecases.get_job(p, job_id)
    path = p.deps.artifacts.path(job_id, name)
    if not path.exists():
        raise usecases.NotFound("文件尚未生成")
    return FileResponse(path, media_type="application/pdf", headers={"Cache-Control": "private, max-age=3600"})


EXPORT_TIMEOUT_S = 300


async def _download_access() -> AsyncIterator[ExitStack]:
    # 默认请求作用域覆盖文件响应的发送阶段，响应结束或断开后才释放工件保护。
    with ExitStack() as access:
        yield access


async def _export_with_timeout(
    p: Pipeline, job_id: str, mode: Literal["mono", "dual"], side: Literal["left", "right"]
) -> ExportResult:
    try:
        async with asyncio.timeout(EXPORT_TIMEOUT_S):
            return await usecases.prepare_export(p, job_id, mode, side)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="PDF 准备超过 5 分钟，请稍后重试") from exc


@router.get("/{job_id}/export")
async def export_job(
    job_id: str,
    p: PipelineDep,
    access: Annotated[ExitStack, Depends(_download_access)],
    mode: Literal["mono", "dual"] = "mono",
    side: Literal["left", "right"] = "right",  # 双语对照时译文放在左边还是右边
    snapshot: str | None = Query(default=None, pattern=r"^[0-9a-f]{16}$"),
) -> Response:
    result = await _export_with_timeout(p, job_id, mode, side) if snapshot is None else None
    if not access.enter_context(p.export_access(job_id)):
        raise usecases.NotFound("导出文件尚未生成")
    if snapshot is not None:
        result = await run_thread(usecases.published_export, p, job_id, mode, side, snapshot)
    if result is None or not result.path.is_file():
        raise usecases.NotFound("导出文件尚未生成")
    disposition = f"attachment; filename=\"translated.pdf\"; filename*=UTF-8''{quote(result.filename)}"
    # 文件响应支持 HTTP Range：浏览器下载中断后可以续传
    return FileResponse(result.path, media_type="application/pdf", headers={"Content-Disposition": disposition})


@router.post("/{job_id}/export/prepare", response_model=ExportPrepareOut)
async def prepare_export(
    job_id: str,
    p: PipelineDep,
    mode: Literal["mono", "dual"] = "mono",
    side: Literal["left", "right"] = "right",
) -> ExportPrepareOut:
    """先生成或命中导出缓存，再让前端启动可见的浏览器下载。"""
    result = await _export_with_timeout(p, job_id, mode, side)
    query = f"mode={mode}&side={side}&snapshot={result.digest}"
    return ExportPrepareOut(
        filename=result.filename,
        download_url=f"/api/jobs/{job_id}/export?{query}",
        size_bytes=result.size_bytes,
        overflow_pages=list(result.overflow_pages),
    )


@router.get("/{job_id}/terms", response_model=list[usecases.TermUse])
def job_terms(job_id: str, p: PipelineDep) -> list[usecases.TermUse]:
    """这篇论文用到的术语：来源与出现在哪几段。"""
    return usecases.job_terms(p, job_id)


@router.post("/{job_id}/retranslate", response_model=list[SegmentOut])
async def retranslate_many(
    job_id: str,
    body: RetranslateManyIn,
    request: Request,
    p: PipelineDep,
) -> list[SegmentOut]:
    """一次重翻多段（最多 20 段），如改了术语译法后重翻用到它的段落。"""
    maintenance_mode, site_notice = _maintenance_state(request)
    job = usecases.get_job(p, job_id)
    sec = resolve_existing_job_secrets(request, job, for_retranslate=True)
    segs = await usecases.retranslate_segments(
        p,
        job_id,
        body.segment_ids,
        body.quality,
        sec,
        body.user_terms,
        maintenance_mode=maintenance_mode,
        site_notice=site_notice,
    )
    return [SegmentOut.of(s) for s in segs]


@router.get("/{job_id}/revisions", response_model=list[RevisionOut])
def list_revisions(job_id: str, p: PipelineDep) -> list[RevisionOut]:
    """手动修改记录（保存修改、重翻、精修、撤回），按时间先后。"""
    return [RevisionOut.of(r) for r in usecases.revisions(p, job_id)]


@router.post("/{job_id}/cancel", response_model=JobOut, status_code=202)
async def cancel_job(job_id: str, p: PipelineDep) -> JobOut:
    """取消排队中或进行中的任务（异步生效：状态随后变为 cancelled）。"""
    return JobOut.of(usecases.cancel_job(p, job_id))


@router.post("/{job_id}/retry", response_model=JobOut, status_code=202)
async def retry_job(job_id: str, request: Request, p: PipelineDep) -> JobOut:
    # 必须是 async：同步路由在线程池里执行，那里没有事件循环，无法创建后台任务
    job = usecases.get_job(p, job_id)
    sec = resolve_existing_job_secrets(request, job)
    limits = _build_limits(request, sec)
    return JobOut.of(
        usecases.retry_job(
            p,
            job_id,
            sec,
            limits=limits,
            maintenance_mode=limits.maintenance_mode,
            site_notice=limits.site_notice,
        )
    )
