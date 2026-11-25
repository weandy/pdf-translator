"""接口的请求/响应结构（DTO），与 IR 解耦；前端类型由 OpenAPI 生成（frontend/src/shared/api/schema.d.ts）。"""

import re
import unicodedata
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from pdftr.domain.expansion import ExpansionOp, ExpansionStage, ExpansionStatus
from pdftr.domain.ir import Document, OutlineItem, Revision, Segment
from pdftr.domain.job import Job, JobOptions, JobStatus
from pdftr.domain.runtime_config import LlmPreset
from pdftr.rendering.links import PdfLink


class HealthOut(BaseModel):
    status: str
    version: str
    data_dir_writable: bool


class FreeChannelPublicOut(BaseModel):
    enabled: bool = False
    model_display: str = ""
    max_pages: int | None = None
    notice: str = ""


class ConfigOut(BaseModel):
    """前端需要的公开配置（不含任何 Key）。"""

    version: str
    default_base_url: str
    default_model: str
    server_has_llm_key: bool
    default_protocol: Literal["openai", "anthropic"]
    default_mineru_base_url: str = "https://mineru.net"
    server_has_parser_token: bool
    server_has_paddle_token: bool
    max_upload_mb: int
    max_pages: int
    retention_hours: int
    admin_path: str
    submission_audit_enabled: bool = False
    attributions: list[str]
    free_channel: FreeChannelPublicOut = Field(default_factory=FreeChannelPublicOut)
    llm_presets: list[LlmPreset] = Field(default_factory=list)
    paddle_public_enabled: bool = False
    maintenance_mode: bool = False
    site_notice: str = ""


class ExpandPagesIn(BaseModel):
    pages: list[int] | None = None


class ExpansionOut(BaseModel):
    id: str
    job_id: str
    status: ExpansionStatus
    stage: ExpansionStage
    progress: float
    message: str
    error: str
    requested_pages: list[int]
    merged_pages: list[int]
    base_version: int
    target_version: int
    cost_cny: float | None
    fallback_used: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, op: ExpansionOp) -> "ExpansionOut":
        return cls(**op.model_dump(exclude={"cost_applied"}))


class JobOut(BaseModel):
    id: str
    filename: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    stage: str
    progress: float
    message: str
    error: str
    page_count: int
    content_version: int = 1
    published_pages: list[int] | None = None
    active_expansion: ExpansionOut | None = None
    options: JobOptions
    model: str
    cost_cny: float | None
    channel_mode: Literal["byok", "free"] = "byok"
    fallback_used: bool = False
    created_at: datetime
    parse_queue_elapsed_s: float | None = None
    stage_started_at: datetime | None
    expires_at: datetime | None  # 到期后服务器自动删除；None 表示不自动删除

    @classmethod
    def of(cls, job: Job, active_expansion: ExpansionOp | None = None) -> "JobOut":
        started = job.parse_queue_started_at
        elapsed = (
            round(max(0.0, (datetime.now(UTC) - started).total_seconds()), 1)
            if job.status is JobStatus.RUNNING and job.stage == "parse" and started is not None
            else None
        )
        return cls(
            **job.model_dump(exclude={"updated_at", "channel_provider_id", "parse_queue_started_at"}),
            parse_queue_elapsed_s=elapsed,
            active_expansion=ExpansionOut.of(active_expansion) if active_expansion is not None else None,
        )


class ExportPrepareOut(BaseModel):
    """PDF 已生成或命中缓存后，供前端启动浏览器下载的信息。"""

    filename: str
    download_url: str
    size_bytes: int
    overflow_pages: list[int] = Field(default_factory=list)


class ExportStatusOut(BaseModel):
    status: Literal["preparing", "ready", "failed", "expired"]
    snapshot: str
    filename: str | None = None
    download_url: str | None = None
    size_bytes: int | None = None
    overflow_pages: list[int] = Field(default_factory=list)
    detail: str = ""


class BundleItemIn(BaseModel):
    job_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    mode: Literal["mono", "dual"] = "mono"
    side: Literal["left", "right"] = "right"
    snapshot: str = Field(pattern=r"^[0-9a-f]{16}$")


class ExportBundleIn(BaseModel):
    items: list[BundleItemIn] = Field(min_length=1, max_length=20)


class ExportBundleOut(BaseModel):
    download_url: str
    filename: str
    size_bytes: int
    item_count: int
    expires_in_s: int


class ExportBundleStaleOut(BaseModel):
    detail: str
    stale_indexes: list[int]


class RectOut(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class BlockOut(BaseModel):
    id: str
    page: int
    type: str
    level: int | None = None
    bbox: RectOut
    font_size: float
    bold: bool
    italic: bool
    serif: bool
    color: int
    translatable: bool
    list_item: bool = False


class PageOut(BaseModel):
    index: int
    width: float
    height: float
    kind: str
    missing_layout: bool = False
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
    translation_attempts: int = 0
    failure_reason: str = ""

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
            translation_attempts=s.translation_attempts, failure_reason=s.failure_reason,
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
        active_expansion: ExpansionOp | None = None,
    ) -> "DocumentOut":
        pages = [
            PageOut(
                index=p.index, width=p.width, height=p.height, kind=p.kind.value, missing_layout=p.missing_layout,
                blocks=[
                    BlockOut(id=b.id, page=b.page, type=b.type.value, level=b.level,
                             bbox=RectOut(**b.bbox.model_dump()),
                             font_size=b.style.font_size, bold=b.style.bold, italic=b.style.italic,
                             serif=b.style.serif, color=b.style.color, translatable=b.translatable,
                             list_item=b.list_item)
                    for b in p.blocks
                ],
            )
            for p in doc.pages
        ]  # fmt: skip
        return cls(
            job=JobOut.of(job, active_expansion),
            title=doc.meta.title,
            pages=pages,
            segments=[SegmentOut.of(s) for s in segments],
            links=[LinkOut.of(x) for x in links or []],
            outline=[OutlineOut(**x.model_dump()) for x in outline or []],
        )


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

    @field_validator("target")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("译文不能为空")
        return cleaned


class RetranslateSegmentOut(SegmentOut):
    applied: bool = True
    notice: str = ""


class RetranslateIn(BaseModel):
    quality: Literal["default", "careful"] = "careful"
    user_terms: dict[str, str] | None = Field(default=None, max_length=5000)  # 浏览器里最新的用户术语库

    instruction: str = Field(default="", max_length=200)

    @field_validator("instruction")
    @classmethod
    def _clean_instruction(cls, value: str) -> str:
        clean = "".join(c for c in value if not unicodedata.category(c).startswith("C") or c.isspace())
        clean = " ".join(clean.split())
        if re.search(r"</?translations\b", clean, re.IGNORECASE):
            raise ValueError("修改要求不能包含翻译协议标签")
        return clean


class RetranslateManyIn(BaseModel):
    segment_ids: list[str] = Field(min_length=1, max_length=20)
    quality: Literal["default", "careful"] = "default"
    user_terms: dict[str, str] | None = Field(default=None, max_length=5000)
