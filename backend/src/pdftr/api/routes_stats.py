"""仅暴露匿名汇总；请求轮询、SSE、健康检查不会产生访问计数。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from pdftr.api.deps import pipeline
from pdftr.domain.stats import StatsOut
from pdftr.pipeline.runner import Pipeline

router = APIRouter(prefix="/api/stats", tags=["stats"])
PipelineDep = Annotated[Pipeline, Depends(pipeline)]


class StatsVisitIn(BaseModel):
    event_id: UUID
    visitor_id: UUID


@router.get("", response_model=StatsOut)
def stats(p: PipelineDep, response: Response, days: Annotated[int, Query(ge=1, le=90)] = 30) -> StatsOut:
    response.headers["Cache-Control"] = "no-store"
    return p.deps.store.stats(days)


@router.post("/visits", status_code=204)
def visit(body: StatsVisitIn, p: PipelineDep) -> Response:
    p.deps.store.record_visit(str(body.event_id), str(body.visitor_id))
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
