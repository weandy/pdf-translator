"""任务接口：创建、详情、删除、进度推送（SSE）、阅读器数据、文件、导出、重试。

不提供任务列表：任务 ID 是随机的，只有上传者知道；“我的任务”由前端保存在浏览器里。
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import ValidationError

from pdftr.api.deps import pipeline, secrets
from pdftr.api.schemas import DocumentOut, JobOut, RevisionOut
from pdftr.domain.job import JobOptions, JobStatus
from pdftr.pipeline import usecases
from pdftr.pipeline.runner import Pipeline
from pdftr.pipeline.secrets import Secrets

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

PipelineDep = Annotated[Pipeline, Depends(pipeline)]
SecretsDep = Annotated[Secrets, Depends(secrets)]


@router.post("", response_model=JobOut, status_code=202)
async def create_job(
    request: Request,
    p: PipelineDep,
    sec: SecretsDep,
    file: Annotated[UploadFile, File()],
    options: Annotated[str, Form()] = "{}",
) -> JobOut:
    settings = request.app.state.container.settings
    try:
        opts = JobOptions.model_validate_json(options)
    except ValidationError as e:
        raise usecases.UserError("任务选项格式错误") from e
    data = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    limits = usecases.Limits(
        max_upload_mb=settings.max_upload_mb,
        max_pages=settings.max_pages,
        max_active_jobs=settings.max_concurrent_jobs + settings.max_queued_jobs,
        min_free_disk_mb=settings.min_free_disk_mb,
        data_dir=settings.data_dir,
        retention_hours=settings.retention_hours,
    )
    job = usecases.create_job(p, data, file.filename or "document.pdf", opts, sec, limits)
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
    job = usecases.get_job(p, job_id)

    async def stream() -> AsyncIterator[str]:
        q = p.deps.events.subscribe(job_id)
        try:
            current = job
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
                        yield ": ping\n\n"  # 保持连接，防止代理断开
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


# 导出时每份约占 200 MB 内存，同时只做一份，避免小机器内存不够
_export_slot = asyncio.Semaphore(1)


@router.get("/{job_id}/export")
async def export_job(
    job_id: str,
    p: PipelineDep,
    mode: Literal["mono", "dual"] = "mono",
    side: Literal["left", "right"] = "right",  # 双语对照时译文放在左边还是右边
) -> Response:
    async with _export_slot:
        filename, path = await asyncio.to_thread(usecases.export_job, p, job_id, mode, side)
    disposition = f"attachment; filename=\"translated.pdf\"; filename*=UTF-8''{quote(filename)}"
    # 文件响应支持 HTTP Range：浏览器下载中断后可以续传
    return FileResponse(path, media_type="application/pdf", headers={"Content-Disposition": disposition})


@router.get("/{job_id}/revisions", response_model=list[RevisionOut])
def list_revisions(job_id: str, p: PipelineDep) -> list[RevisionOut]:
    """手动修改记录（保存修改、重翻、精修、撤回），按时间先后。"""
    return [RevisionOut.of(r) for r in usecases.revisions(p, job_id)]


@router.post("/{job_id}/cancel", response_model=JobOut, status_code=202)
async def cancel_job(job_id: str, p: PipelineDep) -> JobOut:
    """取消排队中或进行中的任务（异步生效：状态随后变为 cancelled）。"""
    return JobOut.of(usecases.cancel_job(p, job_id))


@router.post("/{job_id}/retry", response_model=JobOut, status_code=202)
async def retry_job(job_id: str, p: PipelineDep, sec: SecretsDep) -> JobOut:
    # 必须是 async：同步路由在线程池里执行，那里没有事件循环，无法创建后台任务
    return JobOut.of(usecases.retry_job(p, job_id, sec))
