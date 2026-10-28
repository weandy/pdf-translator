"""段落接口：手动修改、单段重翻、公式裁剪图。"""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from pdftr.api.deps import pipeline, secrets
from pdftr.api.schemas import EditIn, RetranslateIn, SegmentOut
from pdftr.pipeline import usecases
from pdftr.pipeline.runner import Pipeline
from pdftr.pipeline.secrets import Secrets

router = APIRouter(prefix="/api/segments", tags=["segments"])

PipelineDep = Annotated[Pipeline, Depends(pipeline)]


@router.patch("/{segment_id}", response_model=SegmentOut)
def edit_segment(segment_id: str, body: EditIn, p: PipelineDep) -> SegmentOut:
    return SegmentOut.of(usecases.edit_segment(p, segment_id, body.target))


@router.post("/{segment_id}/retranslate", response_model=SegmentOut)
async def retranslate(
    segment_id: str, body: RetranslateIn, p: PipelineDep, sec: Annotated[Secrets, Depends(secrets)]
) -> SegmentOut:
    return SegmentOut.of(await usecases.retranslate_segment(p, segment_id, body.quality, sec))


@router.get("/{segment_id}/formulas/{key}.png")
def formula_png(segment_id: str, key: str, p: PipelineDep) -> Response:
    png = usecases.formula_crop(p, segment_id, key)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})
