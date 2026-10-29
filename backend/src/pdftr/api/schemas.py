"""接口的请求/响应结构（DTO），与 IR 解耦；前端类型由 OpenAPI 生成（frontend/src/shared/api/schema.d.ts）。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from pdftr.domain.ir import Document, OutlineItem, Revision, Segment
from pdftr.domain.job import Job, JobOptions
from pdftr.rendering.links import PdfLink


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
    default_protocol: Literal["openai", "anthropic"]
    server_has_parser_token: bool
    server_has_paddle_token: bool
    max_upload_mb: int
    max_pages: int
    retention_hours: int
    attributions: list[str]


class JobOut(BaseModel):
    id: str
    filename: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    stage: str
    progress: float
    message: str
    error: str
    page_count: int
    options: JobOptions
    model: str
    cost_cny: float | None
    created_at: datetime
    stage_started_at: datetime | None
    expires_at: datetime | None  # 到期后服务器自动删除；None 表示不自动删除

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
    latex: str | None = None  # 独立公式的 LaTeX（阅读视图里用 KaTeX 显示）


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
    """page、bbox 为空：没有精确位置（扫描页），KaTeX 渲染失败时显示 LaTeX 原文而不是裁剪图。"""

    latex: str
    page: int | None
    bbox: RectOut | None


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
        refs = s.formula_refs
        formulas = {
            k: FormulaOut(latex=latex, page=refs[k].page, bbox=RectOut(**refs[k].bbox.model_dump()))
            if k in refs
            else FormulaOut(latex=latex, page=None, bbox=None)
            for k, latex in s.formulas.items()
        }
        return cls(
            id=s.id, block_ids=s.block_ids, parts=[PartOut(**p.model_dump()) for p in s.parts], source=s.source,
            target=s.target, status=s.status.value, quality=s.quality, formulas=formulas,
        )  # fmt: skip


class LinkOut(BaseModel):
    """一个链接。block_id 为空：在译文块外，原文页与译文页上都在 rect 处；否则在这一块译文里第 nth 处 text 上。"""

    page: int
    rect: RectOut
    uri: str
    dest_page: int | None
    dest_top: float  # 目标位置离页面顶部的距离（PDF 点）
    block_id: str | None
    text: str
    nth: int

    @classmethod
    def of(cls, link: PdfLink) -> "LinkOut":
        return cls(page=link.page, rect=RectOut(**link.rect.model_dump()), uri=link.uri, dest_page=link.dest_page,
                   dest_top=link.dest_top, block_id=link.block_id, text=link.text, nth=link.nth)  # fmt: skip


class OutlineOut(BaseModel):
    title: str  # 原文标题
    translated: str
    level: int
    page: int
    top: float
    segment_id: str | None


class DocumentOut(BaseModel):
    job: JobOut
    title: str
    pages: list[PageOut]
    segments: list[SegmentOut]
    links: list[LinkOut] = []
    outline: list[OutlineOut] = []

    @classmethod
    def of(
        cls,
        job: Job,
        doc: Document,
        segments: list[Segment],
        links: list[PdfLink] | None = None,
        outline: list[OutlineItem] | None = None,
    ) -> "DocumentOut":
        pages = [
            PageOut(
                index=p.index, width=p.width, height=p.height, kind=p.kind.value,
                blocks=[
                    BlockOut(id=b.id, page=b.page, type=b.type.value, bbox=RectOut(**b.bbox.model_dump()),
                             font_size=b.style.font_size, bold=b.style.bold, italic=b.style.italic,
                             serif=b.style.serif, color=b.style.color, translatable=b.translatable,
                             latex=b.latex)
                    for b in p.blocks
                ],
            )
            for p in doc.pages
        ]  # fmt: skip
        return cls(job=JobOut.of(job), title=doc.meta.title, pages=pages, segments=[SegmentOut.of(s) for s in segments],
                   links=[LinkOut.of(x) for x in links or []],
                   outline=[OutlineOut(**x.model_dump()) for x in outline or []])  # fmt: skip


class ModelsOut(BaseModel):
    models: list[str]


SegStatus = Literal["pending", "done", "failed", "edited"]


class RevisionOut(BaseModel):
    id: int
    segment_id: str
    action: Literal["edit", "retranslate", "careful", "revert"]
    before: str | None
    before_status: SegStatus
    after: str | None
    after_status: SegStatus
    reverts: int | None
    reverted_by: int | None
    created_at: datetime

    @classmethod
    def of(cls, r: Revision) -> "RevisionOut":
        return cls(**r.model_dump(exclude={"job_id", "before_quality", "after_quality"}))


class EditIn(BaseModel):
    target: str = Field(min_length=1, max_length=20000)


class RetranslateIn(BaseModel):
    quality: Literal["default", "careful"] = "careful"
    user_terms: dict[str, str] | None = Field(default=None, max_length=5000)  # 浏览器里最新的用户术语库
