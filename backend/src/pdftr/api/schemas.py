"""接口的请求/响应结构（DTO），与 IR 解耦；前端类型由 OpenAPI 生成（frontend/src/shared/api/schema.d.ts）。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from pdftr.domain.ir import Document, Segment
from pdftr.domain.job import Job, JobOptions


class HealthOut(BaseModel):
    status: str
    version: str
    data_dir_writable: bool


class ConfigOut(BaseModel):
    """前端需要的公开配置（不含任何 Key）。"""

    version: str
    default_base_url: str
    default_model: str
    server_has_llm_key: bool
    server_has_parser_token: bool
    max_upload_mb: int
    max_pages: int
    attributions: list[str]


class JobOut(BaseModel):
    id: str
    filename: str
    status: Literal["queued", "running", "done", "failed"]
    stage: str
    progress: float
    message: str
    error: str
    page_count: int
    options: JobOptions
    model: str
    cost_cny: float | None
    created_at: datetime

    @classmethod
    def of(cls, job: Job) -> "JobOut":
        return cls(**job.model_dump(exclude={"updated_at"}))


class RectOut(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class BlockOut(BaseModel):
    id: str
    page: int
    type: str
    bbox: RectOut
    font_size: float
    bold: bool
    italic: bool
    serif: bool
    color: int
    translatable: bool


class PageOut(BaseModel):
    index: int
    width: float
    height: float
    kind: str
    blocks: list[BlockOut]


class PartOut(BaseModel):
    block_id: str
    chars: int


class FormulaOut(BaseModel):
    latex: str
    page: int
    bbox: RectOut


class SegmentOut(BaseModel):
    id: str
    block_ids: list[str]
    parts: list[PartOut]
    source: str
    target: str | None
    status: Literal["pending", "done", "failed", "edited"]
    quality: Literal["default", "careful"]
    formulas: dict[str, FormulaOut]

    @classmethod
    def of(cls, s: Segment) -> "SegmentOut":
        formulas = {
            k: FormulaOut(latex=latex, page=s.formula_refs[k].page, bbox=RectOut(**s.formula_refs[k].bbox.model_dump()))
            for k, latex in s.formulas.items()
            if k in s.formula_refs
        }
        return cls(
            id=s.id, block_ids=s.block_ids, parts=[PartOut(**p.model_dump()) for p in s.parts], source=s.source,
            target=s.target, status=s.status.value, quality=s.quality, formulas=formulas,
        )  # fmt: skip


class DocumentOut(BaseModel):
    job: JobOut
    title: str
    pages: list[PageOut]
    segments: list[SegmentOut]

    @classmethod
    def of(cls, job: Job, doc: Document, segments: list[Segment]) -> "DocumentOut":
        pages = [
            PageOut(
                index=p.index, width=p.width, height=p.height, kind=p.kind.value,
                blocks=[
                    BlockOut(id=b.id, page=b.page, type=b.type.value, bbox=RectOut(**b.bbox.model_dump()),
                             font_size=b.style.font_size, bold=b.style.bold, italic=b.style.italic,
                             serif=b.style.serif, color=b.style.color, translatable=b.translatable)
                    for b in p.blocks
                ],
            )
            for p in doc.pages
        ]  # fmt: skip
        return cls(job=JobOut.of(job), title=doc.meta.title, pages=pages, segments=[SegmentOut.of(s) for s in segments])


class EditIn(BaseModel):
    target: str = Field(min_length=1, max_length=20000)


class RetranslateIn(BaseModel):
    quality: Literal["default", "careful"] = "careful"
