"""段落接口：手动修改、单段重翻、公式裁剪图。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from pdftr.api.deps import container, pipeline, resolve_existing_job_secrets
from pdftr.api.schemas import EditIn, RetranslateIn, SegmentOut
from pdftr.pipeline import usecases
from pdftr.pipeline.runner import Pipeline

router = APIRouter(prefix="/api/segments", tags=["segments"])

PipelineDep = Annotated[Pipeline, Depends(pipeline)]


@router.patch("/{segment_id}", response_model=SegmentOut)
def edit_segment(segment_id: str, body: EditIn, p: PipelineDep) -> SegmentOut:
    return SegmentOut.of(usecases.edit_segment(p, segment_id, body.target))


@router.post("/{segment_id}/retranslate", response_model=SegmentOut)
async def retranslate(segment_id: str, body: RetranslateIn, request: Request, p: PipelineDep) -> SegmentOut:
    c = container(request)
    sys_cfg = c.runtime_config.snapshot().data.system if c.runtime_config is not None else None
    maintenance_mode = sys_cfg.maintenance_mode if sys_cfg is not None else False
    site_notice = sys_cfg.site_notice if sys_cfg is not None else ""
    seg = p.deps.store.segment(segment_id)
    if seg is None:
        raise usecases.NotFound("段落不存在")
    job = usecases.get_job(p, seg.job_id)
    sec = resolve_existing_job_secrets(request, job, for_retranslate=True)
    return SegmentOut.of(
        await usecases.retranslate_segment(
            p,
            segment_id,
            body.quality,
            sec,
            body.user_terms,
            maintenance_mode=maintenance_mode,
            site_notice=site_notice,
        )
    )


@router.post("/revisions/{revision_id}/revert", response_model=SegmentOut)
def revert(revision_id: int, p: PipelineDep) -> SegmentOut:
    """撤回某一步：这一段恢复到那一步之前，之后对这一段的修改一并作废。"""
    return SegmentOut.of(usecases.revert_revision(p, revision_id))


@router.get("/{segment_id}/formulas/{key}.png")
def formula_png(segment_id: str, key: str, p: PipelineDep) -> Response:
    png = usecases.formula_crop(p, segment_id, key)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})
