"""补翻准入、候选构建与发布。父任务只在结算和发布事务中写入。"""

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pymupdf

from pdftr.domain.expansion import ExpansionOp, ExpansionStage, ExpansionStatus
from pdftr.domain.ir import Document, PageKind, Segment, SegmentStatus
from pdftr.domain.job import Job, JobStatus, effective_published_pages
from pdftr.domain.layout import LayoutResult
from pdftr.domain.llm import ChatError
from pdftr.extraction.chars import extract
from pdftr.fusion.build import InvalidLayoutPages
from pdftr.fusion.incremental import fuse_incremental
from pdftr.parsing.port import ParseError
from pdftr.pipeline.memory import release_memory
from pdftr.pipeline.runner import (
    PROGRESS_MIN_INTERVAL_S,
    STAGE_MESSAGES,
    TRANSLATED_KINDS,
    Pipeline,
    StageError,
    _write_json,
    chat_session,
    load_context,
    load_document,
)
from pdftr.pipeline.secrets import Secrets
from pdftr.pipeline.usecases import Limits, UserError, _check_admission, _check_maintenance, get_job
from pdftr.pipeline.versions import candidate_dir, discard_candidate, version_artifact
from pdftr.pipeline.workers import run_thread
from pdftr.rendering.background import make_background
from pdftr.translation.segmenter import segment
from pdftr.translation.service import TranslateOptions, translate

log = logging.getLogger(__name__)


def start_expansion(
    pipeline: Pipeline,
    job_id: str,
    pages: list[int] | None,
    limits: Limits,
    resolve_secrets: Callable[[Job, bool], Secrets],
) -> ExpansionOp:
    with pipeline.expansion_admission(job_id) as allowed:
        if not allowed:
            raise UserError("任务已有操作正在进行，请稍后再试")
        job = get_job(pipeline, job_id)
        _check_maintenance(limits.maintenance_mode, limits.site_notice)
        if job.status is not JobStatus.DONE:
            raise UserError("任务完成后才能补翻")
        all_pages = set(range(job.page_count))
        published = set(effective_published_pages(job))
        if not published < all_pages:
            raise UserError("文档已全部翻译")
        if pages is not None and (not pages or not set(pages) <= all_pages):
            raise UserError("请选择文档范围内的页码")
        delta = sorted((all_pages if pages is None else set(pages)) - published)
        if not delta:
            raise UserError("所选页面已全部翻译")
        if job.channel_mode == "free":
            limit = (
                min(limits.max_pages, limits.free_max_pages) if limits.free_max_pages is not None else limits.max_pages
            )
            if job.page_count > limit:
                raise UserError(f"免费体验上限为 {limit} 页")
        _check_admission(pipeline, limits, job.channel_mode)
        base_layout = _load_layout(pipeline, job.id, job.content_version)
        existing_returned = {page.index for page in base_layout.pages}
        needs_parser = bool(set(delta) - existing_returned)
        secrets = resolve_secrets(job, needs_parser)
        op = ExpansionOp(
            id=uuid4().hex,
            job_id=job.id,
            base_version=job.content_version,
            target_version=job.content_version + 1,
            requested_pages=delta,
            merged_pages=sorted(published | set(delta)),
        )
        try:
            pipeline.launch_expansion(job, op, secrets)
        except sqlite3.IntegrityError as exc:
            raise UserError("任务已有补翻正在进行，请稍后再试") from exc
        return op


def _load_layout(pipeline: Pipeline, job_id: str, version: int) -> LayoutResult:
    return LayoutResult.model_validate_json(
        version_artifact(pipeline.deps.artifacts, job_id, version, "layout.json").read_bytes()
    )


def _merge_layout(base: LayoutResult, parsed: LayoutResult, need: set[int], page_count: int) -> LayoutResult:
    indexes = [page.index for page in parsed.pages]
    if len(set(indexes)) != len(indexes) or any(not 0 <= index < page_count for index in indexes):
        raise InvalidLayoutPages("补翻解析结果存在重复或越界页号")
    merged = sorted([*base.pages, *(page for page in parsed.pages if page.index in need)], key=lambda p: p.index)
    attempted = base.attempted_pages
    if attempted is None:
        attempted = base.requested_pages if base.requested_pages is not None else list(range(page_count))
    return base.model_copy(
        update={
            "pages": merged,
            "attempted_pages": sorted(set(attempted) | need | {page.index for page in merged}),
        }
    )


def _stage_fuse(
    job: Job, op: ExpansionOp, pdf: Path, work: Path, base_doc: Document, merged: LayoutResult, old: list[Segment]
) -> tuple[Document, set[int], set[int]]:
    block_pages = {block.id: block.page for block in base_doc.blocks()}
    frozen = set(effective_published_pages(job)) | {block_pages[bid] for seg in old for bid in seg.block_ids}
    delta = set(op.requested_pages) - frozen
    doc = fuse_incremental(base_doc, merged, extract(pdf), frozen, delta)
    _write_json(work / "ir.json", doc.model_dump_json())
    return doc, frozen, delta


def _stage_segment(job: Job, doc: Document, delta: set[int], old: list[Segment]) -> list[Segment]:
    occupied = {bid for seg in old for bid in seg.block_ids}
    next_id = max((int(seg.id.rsplit("-s", 1)[1]) for seg in old), default=-1) + 1
    return segment(
        doc,
        include_references=job.options.include_references,
        include_figures=job.options.translate_figures,
        pages={page.index for page in doc.pages if page.kind in TRANSLATED_KINDS and page.index in delta},
        exclude_block_ids=occupied,
        id_start=next_id,
    )


def _ordered_segments(doc: Document, segments: list[Segment]) -> list[Segment]:
    order = {block.id: idx for idx, block in enumerate(doc.blocks())}
    return sorted(segments, key=lambda seg: order[seg.block_ids[0]])


def _stage_render(pdf: Path, work: Path, doc: Document, segments: list[Segment]) -> None:
    ids = {bid for seg in segments for bid in seg.block_ids}
    blocks = [block for block in doc.blocks() if block.id in ids]
    scanned = {page.index for page in doc.pages if page.kind is PageKind.SCANNED}
    make_background(pdf, blocks, work / "background.pdf", scanned)


def _validate_candidate(
    job: Job, work: Path, base_doc: Document, doc: Document, old: list[Segment], new: list[Segment], frozen: set[int]
) -> None:
    old_blocks = {block.id: block for block in base_doc.blocks()}
    candidate_blocks = {block.id: block for block in doc.blocks()}
    occupied = {bid for seg in old for bid in seg.block_ids}
    if any(bid not in old_blocks or candidate_blocks.get(bid) != old_blocks[bid] for bid in occupied):
        raise StageError("补翻校验失败，已发布的段落位置或内容有变化")
    old_pages = {page.index: page for page in base_doc.pages}
    pages = {page.index: page for page in doc.pages}
    if any(pages.get(index) != old_pages.get(index) for index in frozen):
        raise StageError("补翻校验失败，已发布的页面有变化")
    if any(
        ref.page not in pages or not 0 <= ref.page < job.page_count for seg in old for ref in seg.formula_refs.values()
    ):
        raise StageError("补翻校验失败，已发布的公式页号无效")
    new_blocks = [bid for seg in new for bid in seg.block_ids]
    if (
        len(new_blocks) != len(set(new_blocks))
        or occupied.intersection(new_blocks)
        or not set(new_blocks) <= candidate_blocks.keys()
    ):
        raise StageError("补翻校验失败，新段引用重复或无效的文字块")
    new_ids = {seg.id for seg in new}
    if len(new_ids) != len(new) or any(seg.id in new_ids for seg in old):
        raise StageError("补翻校验失败，段落 ID 冲突")
    frozen_ids = {block.id for block in old_blocks.values() if block.page in frozen}
    if any(block.page not in frozen and block.continues_from in frozen_ids for block in candidate_blocks.values()):
        raise StageError("补翻校验失败，仍有跨版本续段")
    if (
        not LayoutResult.model_validate_json((work / "layout.json").read_bytes()).pages
        or Document.model_validate_json((work / "ir.json").read_bytes()) != doc
        or len(doc.pages) != job.page_count
    ):
        raise StageError("补翻校验失败，版面或阅读数据不完整")
    with pymupdf.open(work / "background.pdf") as background:
        if not background.is_pdf or background.page_count != job.page_count:
            raise StageError("补翻校验失败，背景 PDF 页数错误")


def _publish(
    pipeline: Pipeline,
    job: Job,
    op: ExpansionOp,
    work: Path,
    old: list[Segment],
    new: list[Segment],
    ordered_ids: list[str],
) -> Job:
    target = version_artifact(pipeline.deps.artifacts, job.id, op.target_version, "ir.json").parent
    # 拷贝以外的操作不必在事件循环外执行，目录重命名与事务之间没有取消点。
    (work / "candidate_segments.json").unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    with pipeline.content_access():
        current = get_job(pipeline, job.id)
        if current.content_version != op.base_version or pipeline.expansion_cancel_requested(op.id):
            raise StageError("任务版本已变化或补翻已取消")
        discard_candidate(target)
        work.rename(target)
        try:
            target.touch()
            return pipeline.deps.store.publish_expansion(
                job.id,
                op.id,
                {seg.id for seg in old},
                new,
                ordered_ids,
            )
        except BaseException:
            # 即使连接在提交结果返回时失败，也只回收仍未发布的版本。
            current = get_job(pipeline, job.id)
            if current.content_version < op.target_version:
                discard_candidate(target)
            raise


class _Run:
    def __init__(self, pipeline: Pipeline, job: Job, op: ExpansionOp, secrets: Secrets) -> None:
        self.pipeline, self.job, self.op, self.secrets = pipeline, job, op, secrets
        self.deps = pipeline.deps
        self.work = candidate_dir(self.deps.artifacts, job.id, op.id)
        self.pdf = self.deps.artifacts.path(job.id, "original.pdf")
        self.last_progress = 0.0

    def update(self) -> None:
        self.op.updated_at = datetime.now(UTC)
        self.deps.store.save_expansion(self.op)
        self.deps.events.publish(self.job)

    def stage(self, stage: ExpansionStage) -> None:
        self.op.stage, self.op.progress = stage, 0.0
        self.op.message = STAGE_MESSAGES.get(stage, "正在发布新版本")
        self.last_progress = 0.0
        self.update()

    def progress(self, message: str, fraction: float | None = None) -> None:
        now = time.monotonic()
        if now - self.last_progress < PROGRESS_MIN_INTERVAL_S and fraction != 1.0 and "重试" not in message:
            return
        self.op.message = message
        if fraction is not None:
            self.op.progress = fraction
        self.last_progress = now
        self.update()

    async def parse(self) -> LayoutResult:
        self.stage(ExpansionStage.PARSE)
        base = await run_thread(_load_layout, self.pipeline, self.job.id, self.op.base_version)
        returned = {page.index for page in base.pages}
        need = sorted(set(self.op.requested_pages) - returned)
        if need:
            async with self.pipeline.hold_stages(("parse",), self.job, lambda _, msg: self.progress(msg)):
                raw = self.work / "parser"
                parsed = await self.pipeline.parse_layout(
                    self.job, self.secrets, need, raw, self.progress, require_cached_pages=True
                )
            layout = _merge_layout(base, parsed, set(need), self.job.page_count)
            await run_thread(discard_candidate, raw)
        else:
            layout = base
        await run_thread(_write_json, self.work / "layout.json", layout.model_dump_json())
        return layout

    def usage(self, cost: float | None, fallback: bool) -> None:
        self.op.cost_cny, self.op.fallback_used = cost, fallback
        self.update()

    async def translate(self, doc: Document, old: list[Segment], new: list[Segment]) -> list[Segment]:
        self.stage(ExpansionStage.TRANSLATE)
        context, selection = await run_thread(load_context, self.deps, self.job.id, self.job.options.user_terms, doc)
        prices = self.deps.price_overrides_loader() if self.deps.price_overrides_loader else None
        async with (
            self.pipeline.hold_stages(("translate",), self.job, lambda _, msg: self.progress(msg)),
            chat_session(self.deps.chat_factory(self.secrets)) as chat,
        ):
            out, report = await translate(
                new,
                chat=chat,
                cache=self.deps.cache,
                ctx=context,
                lookup_term_hits=selection.match_segment,
                context_segments=_ordered_segments(doc, [*old, *new]),
                opts=TranslateOptions(
                    quality=self.job.options.quality,
                    concurrency=self.pipeline.llm_concurrency(self.secrets),
                    price_overrides=prices,
                ),
                on_progress=lambda done, total, note: self.progress(
                    f"补翻中 {done}/{total}" + (f"，{note}" if note else ""), done / total if total else 1.0
                ),
                on_usage=self.usage,
            )
        self.usage(report.cost_cny, report.used_fallback)
        if report.failed or any(seg.status is not SegmentStatus.DONE for seg in out):
            raise StageError(f"还有 {report.failed} 段未完成补翻，请重试")
        return out

    async def execute(self) -> Job:
        self.work.mkdir(parents=True, exist_ok=True)
        self.op.status = ExpansionStatus.RUNNING
        merged = await self.parse()
        self.stage(ExpansionStage.FUSE)
        async with self.pipeline.hold_stages(("fuse",), self.job, lambda _, msg: self.progress(msg)):
            old_doc = await run_thread(load_document, self.deps, self.job.id, self.op.base_version)
            old = self.deps.store.segments(self.job.id)
            doc, frozen, delta = await run_thread(
                _stage_fuse, self.job, self.op, self.pdf, self.work, old_doc, merged, old
            )
            self.stage(ExpansionStage.SEGMENT)
            new = await run_thread(_stage_segment, self.job, doc, delta, old)
        if new:
            new = await self.translate(doc, old, new)
        ordered = _ordered_segments(doc, [*old, *new])
        payload = "[" + ",".join(seg.model_dump_json() for seg in new) + "]"
        await run_thread(_write_json, self.work / "candidate_segments.json", payload)
        self.stage(ExpansionStage.RENDER)
        async with self.pipeline.hold_stages(("render",), self.job, lambda _, msg: self.progress(msg)):
            await run_thread(_stage_render, self.pdf, self.work, doc, ordered)
            await run_thread(_validate_candidate, self.job, self.work, old_doc, doc, old, new, frozen)
        self.stage(ExpansionStage.PUBLISH)
        # 发布前保留一个取消点，事务一旦开始即完整执行。
        await asyncio.sleep(0)
        return _publish(self.pipeline, self.job, self.op, self.work, old, new, [seg.id for seg in ordered])


async def run_expansion(pipeline: Pipeline, job: Job, op: ExpansionOp, secrets: Secrets) -> None:
    store = pipeline.deps.store
    work = candidate_dir(pipeline.deps.artifacts, job.id, op.id)
    try:
        if pipeline.expansion_cancel_requested(op.id):
            raise asyncio.CancelledError
        job = await _Run(pipeline, job, op, secrets).execute()
    except asyncio.CancelledError:
        if not pipeline.expansion_cancel_requested(op.id):
            raise
        with pipeline.content_access():
            job = store.finish_expansion(job.id, op.id, ExpansionStatus.CANCELLED, "")
    except Exception as exc:
        expected = isinstance(exc, (ParseError, ChatError, InvalidLayoutPages, StageError))
        if not expected:
            log.exception("任务 %s 在补翻阶段 %s 失败", job.id, op.stage)
        error = str(exc) if expected else "补翻时出现意外错误，请稍后重试"
        with pipeline.content_access():
            job = store.finish_expansion(job.id, op.id, ExpansionStatus.FAILED, error)
    finally:
        try:
            await run_thread(discard_candidate, work)
        finally:
            release_memory()
    pipeline.deps.events.publish(job)


def recover_expansions(pipeline: Pipeline) -> None:
    store = pipeline.deps.store
    for op in store.active_expansions():
        job = store.finish_expansion(op.job_id, op.id, ExpansionStatus.FAILED, "服务重启，补翻中断，请重试")
        discard_candidate(candidate_dir(pipeline.deps.artifacts, job.id, op.id))
        if op.target_version > job.content_version:
            discard_candidate(version_artifact(pipeline.deps.artifacts, job.id, op.target_version, "ir.json").parent)
