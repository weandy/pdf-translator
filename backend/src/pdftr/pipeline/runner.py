"""流水线：按阶段执行任务（docs/03-mvp-design.md §3.4）。

每个阶段读工件、写工件；已完成的阶段在重跑时跳过（MinerU 结果、译文都已落盘，重跑不重复花钱）。
解析服务与大模型的错误（ParseError / ChatError）面向用户，原样显示；其他异常记录日志并显示“内部错误”。
"""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pdftr.domain.ir import BlockType, Document, PageKind, SegmentStatus
from pdftr.domain.job import STAGES, Job, JobStatus
from pdftr.domain.layout import LayoutResult
from pdftr.domain.llm import ChatError, ChatModel
from pdftr.extraction.chars import extract
from pdftr.fusion.build import fuse
from pdftr.glossary.extract import extract_terms
from pdftr.glossary.matcher import TermMatcher
from pdftr.glossary.normalize import normalize_terms
from pdftr.parsing.port import LayoutParser, ParseError
from pdftr.pipeline.events import EventBus
from pdftr.pipeline.secrets import Secrets, SecretStore
from pdftr.rendering.background import make_background
from pdftr.storage.port import ArtifactStore, JobStore
from pdftr.translation.port import TranslationCache
from pdftr.translation.prompt import DocContext
from pdftr.translation.segmenter import segment
from pdftr.translation.service import TranslateOptions, default_concurrency, translate

log = logging.getLogger(__name__)

ParserFactory = Callable[[Secrets], LayoutParser]
ChatFactory = Callable[[Secrets], ChatModel]
MatcherLoader = Callable[[], TermMatcher | None]


@dataclass(frozen=True)
class PipelineDeps:
    artifacts: ArtifactStore
    store: JobStore
    cache: TranslationCache
    events: EventBus
    secrets: SecretStore
    parser_factory: ParserFactory
    chat_factory: ChatFactory
    term_matcher: MatcherLoader
    max_concurrent_jobs: int = 2
    font_dir: Path | None = None  # 导出 PDF 用的字体目录
    llm_concurrency: int = 0  # 0 = 按服务商自动选择（translation.service.default_concurrency）


# 各阶段开始时显示的说明；阶段内有更具体的进度时会覆盖
STAGE_MESSAGES = {
    "parse": "正在准备上传",
    "fuse": "正在对齐版面与文字",
    "segment": "正在切分段落",
    "glossary": "正在抽取术语",
    "translate": "正在翻译",
    "render": "正在生成背景页",
}


class StageError(RuntimeError):
    """面向用户的阶段失败原因。"""


def load_document(deps: PipelineDeps, job_id: str) -> Document:
    return Document.model_validate_json(deps.artifacts.path(job_id, "ir.json").read_text(encoding="utf-8"))


def load_context(deps: PipelineDeps, job_id: str) -> DocContext:
    doc = load_document(deps, job_id)
    glossary_file = deps.artifacts.path(job_id, "glossary.json")
    g = json.loads(glossary_file.read_text(encoding="utf-8")) if glossary_file.exists() else {}
    return DocContext(title=doc.meta.title, abstract=doc.meta.abstract, domain=g.get("domain", ""),
                      terms=g.get("terms", {}))  # fmt: skip


class Pipeline:
    def __init__(self, deps: PipelineDeps) -> None:
        self.deps = deps
        self._sem = asyncio.Semaphore(deps.max_concurrent_jobs)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # ---- 调度 ----
    def submit(self, job: Job, secrets: Secrets) -> None:
        self.deps.secrets.put(job.id, secrets)
        task = asyncio.create_task(self._run(job.id))
        self._tasks[job.id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job.id, None))

    def is_running(self, job_id: str) -> bool:
        return job_id in self._tasks

    def active_count(self) -> int:
        """排队中与执行中的任务数。"""
        return len(self._tasks)

    def recover_interrupted(self) -> None:
        """服务重启时，进行中的任务已没有密钥可用，标记失败，用户重新提供 Key 后可从断点重试。"""
        for status in (JobStatus.RUNNING, JobStatus.QUEUED):
            for job in self.deps.store.jobs_with_status(status):
                job.status, job.error = JobStatus.FAILED, "服务重启，任务中断，请点击重试"
                self.deps.store.save_job(job)

    async def wait_all(self) -> None:
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    # ---- 执行 ----
    def _update(self, job: Job, **changes: object) -> None:
        for k, v in changes.items():
            setattr(job, k, v)
        self.deps.store.save_job(job)
        self.deps.events.publish(job)

    async def _run(self, job_id: str) -> None:
        job = self.deps.store.get_job(job_id)
        secrets = self.deps.secrets.get(job_id)
        if job is None or secrets is None:
            return
        try:
            async with self._sem:
                self._update(job, status=JobStatus.RUNNING, error="")
                start = STAGES.index(job.stage) if job.stage in STAGES else len(STAGES)
                for stage in STAGES[start:]:
                    # 生成背景页只要一两秒，保留翻译阶段的总结（“已翻译 N 段”），完成后显示的就是它
                    message = job.message if stage == "render" else STAGE_MESSAGES[stage]
                    self._update(job, stage=stage, progress=0.0, message=message,
                                 stage_started_at=datetime.now(UTC))  # fmt: skip
                    await getattr(self, f"_{stage}")(job, secrets)
                self._update(job, status=JobStatus.DONE, stage="done", progress=1.0)
        except (ParseError, ChatError, StageError) as e:
            self._update(job, status=JobStatus.FAILED, error=str(e))
        except Exception:
            log.exception("任务 %s 在阶段 %s 失败", job_id, job.stage)
            self._update(job, status=JobStatus.FAILED, error="内部错误，请查看服务日志")
        finally:
            self.deps.secrets.drop(job_id)

    async def _parse(self, job: Job, secrets: Secrets) -> None:
        out = self.deps.artifacts.path(job.id, "layout.json")
        if out.exists():
            return
        parser = self.deps.parser_factory(secrets)
        raw_dir = self.deps.artifacts.job_dir(job.id) / "parser"

        def progress(message: str, fraction: float | None = None) -> None:
            self._update(job, message=message, progress=job.progress if fraction is None else fraction)

        pdf = self.deps.artifacts.path(job.id, "original.pdf")
        result = await parser.parse(pdf, raw_dir=raw_dir, on_progress=progress)
        out.write_text(result.model_dump_json(), encoding="utf-8")

    async def _fuse(self, job: Job, _: Secrets) -> None:
        out = self.deps.artifacts.path(job.id, "ir.json")
        if out.exists():
            return
        layout = LayoutResult.model_validate_json(self.deps.artifacts.path(job.id, "layout.json").read_text("utf-8"))
        pdf = self.deps.artifacts.path(job.id, "original.pdf")
        chars = await asyncio.to_thread(extract, pdf)
        doc, report = await asyncio.to_thread(fuse, job.id, layout, chars, used_cloud=True)
        out.write_text(doc.model_dump_json(), encoding="utf-8")
        skipped = [p.index + 1 for p in doc.pages if p.kind is not PageKind.NATIVE]
        note = f"第 {', '.join(map(str, skipped))} 页是扫描页或无法提取文字，当前版本暂不翻译" if skipped else ""
        self._update(job, page_count=report.pages, message=note)

    async def _segment(self, job: Job, _: Secrets) -> None:
        if self.deps.store.segments(job.id):
            return  # 已切分过（重试时保留已有译文和用户修改）
        doc = load_document(self.deps, job.id)
        native = {p.index for p in doc.pages if p.kind is PageKind.NATIVE}
        pages = native if job.options.pages is None else native & set(job.options.pages)
        segs = segment(doc, include_references=job.options.include_references,
                       include_figures=job.options.translate_figures, pages=pages)  # fmt: skip
        if not segs:
            raise StageError("没有找到可翻译的文字")
        self.deps.store.replace_segments(job.id, segs)

    async def _glossary(self, job: Job, secrets: Secrets) -> None:
        out = self.deps.artifacts.path(job.id, "glossary.json")
        if out.exists():
            return
        doc = load_document(self.deps, job.id)
        segs = self.deps.store.segments(job.id)
        headings = [s.source for s in segs if (b := doc.block(s.block_ids[0])) and b.type is BlockType.TITLE]
        body = " ".join(s.source for s in segs)
        intro = next((s.source for s in segs if len(s.source.split()) >= 30 and s.source != doc.meta.abstract), "")
        found = await extract_terms(self.deps.chat_factory(secrets), title=doc.meta.title, abstract=doc.meta.abstract,
                                    intro=intro, headings=headings)  # fmt: skip
        terms = normalize_terms(found.terms, body, headings)
        out.write_text(json.dumps({"domain": found.domain, "terms": terms}, ensure_ascii=False), encoding="utf-8")

    async def _translate(self, job: Job, secrets: Secrets) -> None:
        segs = self.deps.store.segments(job.id)
        chat = self.deps.chat_factory(secrets)
        matcher = await asyncio.to_thread(self.deps.term_matcher)

        def progress(done: int, total: int) -> None:
            self._update(job, progress=done / total if total else 1.0, message=f"翻译中 {done}/{total}")

        out, report = await translate(
            segs, chat=chat, cache=self.deps.cache, ctx=load_context(self.deps, job.id),
            lookup_terms=matcher.match if matcher else None,
            opts=TranslateOptions(quality=job.options.quality, concurrency=self._concurrency(secrets)),
            on_progress=progress,
        )  # fmt: skip
        self.deps.store.save_segments(out)
        cost = report.cost_cny
        total = None if cost is None and job.cost_cny is None else (job.cost_cny or 0) + (cost or 0)
        failed = f"，{report.failed} 段未通过校验，可在阅读器中重试" if report.failed else ""
        self._update(job, model=chat.model, cost_cny=total, message=f"已翻译 {report.translated} 段{failed}")

    def _concurrency(self, secrets: Secrets) -> int:
        return self.deps.llm_concurrency or default_concurrency(secrets.llm_base_url)

    async def _render(self, job: Job, _: Secrets) -> None:
        doc = load_document(self.deps, job.id)
        ids = {bid for s in self.deps.store.segments(job.id) for bid in s.block_ids}
        blocks = [b for b in doc.blocks() if b.id in ids]
        await asyncio.to_thread(make_background, self.deps.artifacts.path(job.id, "original.pdf"), blocks,
                                self.deps.artifacts.path(job.id, "background.pdf"))  # fmt: skip


def segment_counts(store: JobStore, job_id: str) -> dict[str, int]:
    return {s.value: store.count_segments(job_id, s) for s in SegmentStatus}
