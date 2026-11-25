"""可恢复的 PDF 导出准备以及一次性 ZIP 下载。"""

import asyncio
import logging
from contextlib import ExitStack, aclosing
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from pdftr.api.deps import container, pipeline
from pdftr.api.schemas import ExportBundleIn, ExportBundleOut, ExportBundleStaleOut, ExportStatusOut
from pdftr.pipeline import bundle, usecases
from pdftr.pipeline.runner import ExportResult, Pipeline
from pdftr.pipeline.workers import run_thread
from pdftr.pipeline.zip_stream import pool_call, stream_zip

router = APIRouter(prefix="/api", tags=["exports"])
PipelineDep = Annotated[Pipeline, Depends(pipeline)]
_NO_STORE = {"Cache-Control": "no-store"}


class _BundleAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) == 5:
            path = record.args[2]
            if isinstance(path, str) and "/api/exports/bundle/" in path:
                record.args = (*record.args[:2], "/api/exports/bundle/[redacted]", *record.args[3:])
        return True


def protect_access_logs() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _BundleAccessFilter) for item in logger.filters):
        logger.addFilter(_BundleAccessFilter())


def _ready(job_id: str, mode: str, side: str, result: ExportResult) -> ExportStatusOut:
    return ExportStatusOut(
        status="ready",
        snapshot=result.digest,
        filename=result.filename,
        download_url=f"/api/jobs/{job_id}/export?mode={mode}&side={side}&snapshot={result.digest}",
        size_bytes=result.size_bytes,
        overflow_pages=list(result.overflow_pages),
    )


@router.post(
    "/jobs/{job_id}/export/start",
    response_model=ExportStatusOut,
    responses={202: {"model": ExportStatusOut}},
)
async def start_export(
    job_id: str,
    p: PipelineDep,
    response: Response,
    mode: Literal["mono", "dual"] = "mono",
    side: Literal["left", "right"] = "right",
) -> ExportStatusOut:
    response.headers.update(_NO_STORE)
    digest, result = await usecases.start_export(p, job_id, mode, side)
    if result is not None:
        return _ready(job_id, mode, side, result)
    response.status_code = 202
    return ExportStatusOut(status="preparing", snapshot=digest)


@router.get("/jobs/{job_id}/export/status", response_model=ExportStatusOut)
async def export_status(
    job_id: str,
    p: PipelineDep,
    response: Response,
    snapshot: Annotated[str, Query(pattern=r"^[0-9a-f]{16}$")],
    mode: Literal["mono", "dual"] = "mono",
    side: Literal["left", "right"] = "right",
) -> ExportStatusOut:
    response.headers.update(_NO_STORE)
    state, result, detail = await usecases.export_status(p, job_id, mode, side, snapshot)
    if result is not None:
        return _ready(job_id, mode, side, result)
    return ExportStatusOut(status=state, snapshot=snapshot, detail=detail)


def _stale(error: bundle.StaleBundle) -> JSONResponse:
    body = ExportBundleStaleOut(detail=str(error), stale_indexes=error.indexes)
    return JSONResponse(body.model_dump(), status_code=409, headers=_NO_STORE)


@router.post("/exports/bundle", response_model=ExportBundleOut, responses={409: {"model": ExportBundleStaleOut}})
async def create_bundle(
    body: ExportBundleIn, request: Request, p: PipelineDep, response: Response
) -> ExportBundleOut | Response:
    items = [bundle.BundleItem(item.job_id, item.mode, item.side, item.snapshot) for item in body.items]
    try:
        token, size = await run_thread(bundle.issue_bundle, p, container(request).bundles, items)
    except bundle.StaleBundle as error:
        return _stale(error)
    response.headers.update(_NO_STORE)
    return ExportBundleOut(
        download_url=f"/api/exports/bundle/{token}",
        filename=bundle.BUNDLE_FILENAME,
        size_bytes=size,
        item_count=len(items),
        expires_in_s=bundle.BUNDLE_TOKEN_TTL_S,
    )


class _BundleResponse(Response):
    def __init__(self, registry: bundle.BundleRegistry, p: Pipeline, token: str) -> None:
        super().__init__()
        self.registry = registry
        self.pipeline = p
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        with ExitStack() as stack:
            try:
                ticket = self.registry.claim(self.token)
                stack.enter_context(self.registry.stream_slot())
                entries = await pool_call(self.registry.pool, bundle.open_bundle, stack, self.pipeline, ticket)
            except usecases.NotFound as error:
                await JSONResponse({"detail": str(error)}, status_code=404, headers=_NO_STORE)(scope, receive, send)
                return
            except usecases.ServiceBusy as error:
                await JSONResponse({"detail": str(error)}, status_code=503, headers=_NO_STORE)(scope, receive, send)
                return
            except bundle.StaleBundle as error:
                await _stale(error)(scope, receive, send)
                return
            async with aclosing(stream_zip(entries, self.registry.pool)) as stream:
                response = StreamingResponse(
                    stream,
                    media_type="application/zip",
                    headers={
                        **_NO_STORE,
                        "Content-Length": str(ticket.size_bytes),
                        "Content-Disposition": (
                            f'attachment; filename="{bundle.BUNDLE_FILENAME}"; '
                            f"filename*=UTF-8''{quote(bundle.BUNDLE_FILENAME)}"
                        ),
                    },
                )
                async with asyncio.timeout(bundle.BUNDLE_STREAM_MAX_DURATION_S):
                    await response(scope, receive, send)


@router.get(
    "/exports/bundle/{token}",
    responses={
        200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}},
        409: {"model": ExportBundleStaleOut},
    },
    response_class=Response,
)
def download_bundle(token: str, request: Request, p: PipelineDep) -> Response:
    if not bundle.TOKEN_PATTERN.fullmatch(token):
        raise usecases.NotFound("下载链接已失效，请重新打包")
    return _BundleResponse(container(request).bundles, p, token)
