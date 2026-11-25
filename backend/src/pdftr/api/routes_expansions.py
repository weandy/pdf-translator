"""发起、查看和取消已完成任务的补翻操作。"""

from fastapi import APIRouter, Request

from pdftr.api.deps import _build_limits, resolve_expansion_secrets
from pdftr.api.routes_jobs import PipelineDep
from pdftr.api.schemas import ExpandPagesIn, ExpansionOut
from pdftr.pipeline import usecases
from pdftr.pipeline.expansion import start_expansion

router = APIRouter(prefix="/api/jobs", tags=["expansions"])


@router.post("/{job_id}/expand-pages", status_code=202, response_model=ExpansionOut)
async def expand_pages(job_id: str, body: ExpandPagesIn, request: Request, p: PipelineDep) -> ExpansionOut:
    job = usecases.get_job(p, job_id)
    limits = _build_limits(request, job.channel_mode)
    op = start_expansion(
        p,
        job_id,
        body.pages,
        limits,
        lambda current, needs_parser: resolve_expansion_secrets(request, current, needs_parser=needs_parser),
    )
    return ExpansionOut.of(op)


@router.get("/{job_id}/expansions/{op_id}", response_model=ExpansionOut)
def expansion_status(job_id: str, op_id: str, p: PipelineDep) -> ExpansionOut:
    op = p.deps.store.expansion(job_id, op_id)
    if op is None:
        raise usecases.NotFound("补翻操作不存在")
    return ExpansionOut.of(op)


@router.post("/{job_id}/expansions/{op_id}/cancel", status_code=202, response_model=ExpansionOut)
async def cancel_expansion(job_id: str, op_id: str, p: PipelineDep) -> ExpansionOut:
    op = p.deps.store.expansion(job_id, op_id)
    if op is None:
        raise usecases.NotFound("补翻操作不存在")
    p.cancel_expansion(op)
    return ExpansionOut.of(op)
