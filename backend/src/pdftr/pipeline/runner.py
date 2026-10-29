"""流水线：按阶段执行任务（docs/03-mvp-design.md §3.4）。

每个阶段读工件、写工件；已完成的阶段在重跑时跳过（MinerU 结果、译文都已落盘，重跑不重复花钱）。
解析服务与大模型的错误（ParseError / ChatError）面向用户，原样显示；其他异常记录日志并显示“内部错误”。
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
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
from pdftr.glossary.matcher import TermMatcher, combine, override
from pdftr.glossary.normalize import normalize_terms
from pdftr.parsing.port import LayoutParser, ParseError
from pdftr.pipeline.events import EventBus
from pdftr.pipeline.memory import release_memory
from pdftr.pipeline.secrets import Secrets, SecretStore
from pdftr.rendering.background import make_background
from pdftr.storage.port import ArtifactStore, JobStore
from pdftr.translation.cost import add_cost
from pdftr.translation.port import TranslationCache
from pdftr.translation.prompt import DocContext
from pdftr.translation.segmenter import segment
from pdftr.translation.service import TranslateOptions, default_concurrency, translate

log = logging.getLogger(__name__)

ParserFactory = Callable[[Secrets, str], LayoutParser]  # （密钥, 解析服务名）→ 解析器
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
    max_concurrent_jobs: int = 4  # 同时上传、解析的任务数（主要在等云端，不占本机资源）
    max_concurrent_translations: int = 1  # 同时翻译的任务数：一篇接一篇，每篇内部并发请求翻译服务
    parse_retry_delays: tuple[float, ...] = (15, 45)  # 解析遇到网络类错误时自动重试的等待秒数
    font_dir: Path | None = None  # 导出 PDF 用的字体目录
    llm_concurrency: int = 0  # 0 = 按服务商自动选择（translation.service.default_concurrency）


# 阶段的中文名（错误说明里用，与前端 STAGE_LABELS 一致）
STAGE_NAMES = {"parse": "版面解析", "fuse": "对齐文字", "segment": "切分段落", "glossary": "抽取术语",
               "translate": "翻译", "render": "生成背景页"}  # fmt: skip

# 会翻译的页：原生页，以及用解析服务识别结果的扫描页（乱码页跳过）
TRANSLATED_KINDS = frozenset({PageKind.NATIVE, PageKind.SCANNED})

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


def load_context(deps: PipelineDeps, job_id: str, user_terms: dict[str, str] | None = None) -> DocContext:
    """user_terms：用户术语库，覆盖自动抽取的同名术语。"""
    doc = load_document(deps, job_id)
    glossary_file = deps.artifacts.path(job_id, "glossary.json")
    g = json.loads(glossary_file.read_text(encoding="utf-8")) if glossary_file.exists() else {}
    return DocContext(title=doc.meta.title, abstract=doc.meta.abstract, domain=g.get("domain", ""),
                      terms=override(g.get("terms", {}), user_terms or {}))  # fmt: skip


class Slots:
    """有名额限制的一类阶段：先到先得；排队的任务显示自己排在第几位，队伍前进时更新。"""

    def __init__(self, size: int, waiting: Callable[[int], str]) -> None:
        self._sem = asyncio.Semaphore(size)
        self._queue: list[Job] = []
        self._waiting = waiting  # 排在第 n 位时显示的说明

    @asynccontextmanager
    async def hold(self, job: Job, notify: Callable[[Job, str], None]) -> AsyncIterator[None]:
        if self._sem.locked():
            self._queue.append(job)
            self._refresh(notify)
            try:
                await self._sem.acquire()
            finally:
                self._queue.remove(job)
                self._refresh(notify)
        else:
            await self._sem.acquire()
        try:
            yield
        finally:
            self._sem.release()

    def _refresh(self, notify: Callable[[Job, str], None]) -> None:
        for n, job in enumerate(self._queue, start=1):
            notify(job, self._waiting(n))


class Pipeline:
    def __init__(self, deps: PipelineDeps) -> None:
        self.deps = deps
        # 解析可以几篇同时进行；对齐与生成背景页用 PyMuPDF，耗内存，一次一篇（每篇只要一两秒）；
        # 术语抽取与翻译一篇接一篇，每篇内部并发请求翻译服务（DeepSeek 默认 4 路）
        parse = Slots(deps.max_concurrent_jobs, lambda n: f"排队等待解析，排在第 {n} 位")
        local = Slots(1, lambda n: f"排队等待处理，前面还有 {n} 篇")
        llm = Slots(deps.max_concurrent_translations, lambda n: f"等待翻译，前面还有 {n} 篇")
        self._slots = {"parse": parse, "fuse": local, "segment": local, "glossary": llm, "translate": llm,
                       "render": local}  # fmt: skip
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancelling: set[str] = set()  # 用户要求取消的任务（与服务停止时的取消区分开）

    # ---- 调度 ----
    def submit(self, job: Job, secrets: Secrets) -> None:
        self.deps.secrets.put(job.id, secrets)
        task = asyncio.create_task(self._run(job.id))
        self._tasks[job.id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job.id, None))

    def is_running(self, job_id: str) -> bool:
        return job_id in self._tasks

    def cancel(self, job_id: str) -> bool:
        """取消排队中或进行中的任务；返回是否有任务可取消。已完成的阶段与译文缓存保留，重试时从中断处继续。"""
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        self._cancelling.add(job_id)
        task.cancel()
        return True

    def active_count(self) -> int:
        """排队中与执行中的任务数。"""
        return len(self._tasks)

    def recover_interrupted(self) -> None:
        """服务重启时，进行中的任务已没有密钥可用，标记失败，用户重新提供 Key 后可从断点重试。"""
        for status in (JobStatus.RUNNING, JobStatus.QUEUED):
            for job in self.deps.store.jobs_with_status(status):
                job.status, job.error = JobStatus.FAILED, "服务重启，任务中断，请点击重试"
                self.deps.store.save_job(job)

    async def wait_all(self, timeout: float | None = None) -> bool:
        """等待进行中的任务完成；超时返回 False（剩下的任务在下次启动时标记为中断，可从断点重试）。"""
        if not self._tasks:
            return True
        _, pending = await asyncio.wait(list(self._tasks.values()), timeout=timeout)
        return not pending

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
            start = STAGES.index(job.stage) if job.stage in STAGES else len(STAGES)
            todo = list(STAGES[start:])
            while todo:
                # 连续几个阶段属于同一类名额时一次拿下（术语抽取与翻译之间不让别的任务插队）
                slots = self._slots[todo[0]]
                n = next((i for i, s in enumerate(todo) if self._slots[s] is not slots), len(todo))
                group, todo = todo[:n], todo[n:]
                summary = job.message  # 排队说明会覆盖消息，先记下翻译阶段的总结
                self._update(job, stage=group[0], progress=0.0)
                async with slots.hold(job, lambda j, msg: self._update(j, message=msg)):
                    if job.status is not JobStatus.RUNNING:
                        self._update(job, status=JobStatus.RUNNING, error="")
                    for stage in group:
                        # 生成背景页只要一两秒，保留翻译阶段的总结（“已翻译 N 段”），完成后显示的就是它
                        message = summary if stage == "render" else STAGE_MESSAGES[stage]
                        self._update(job, stage=stage, progress=0.0, message=message,
                                     stage_started_at=datetime.now(UTC))  # fmt: skip
                        await getattr(self, f"_{stage}")(job, secrets)
            self._update(job, status=JobStatus.DONE, stage="done", progress=1.0)
        except asyncio.CancelledError:
            if job_id not in self._cancelling:
                raise  # 服务停止：交给启动时的中断恢复处理
            self._update(job, status=JobStatus.CANCELLED, error="", message="已取消")
        except (ParseError, ChatError, StageError) as e:
            self._update(job, status=JobStatus.FAILED, error=str(e))
        except Exception:
            log.exception("任务 %s 在阶段 %s 失败", job_id, job.stage)
            stage = STAGE_NAMES.get(job.stage, job.stage)
            hint = "可以点“重试”从中断处继续；多次失败请反馈给部署者"
            self._update(job, status=JobStatus.FAILED, error=f"处理时出现意外错误（{stage}）。{hint}")
        finally:
            self._cancelling.discard(job_id)
            self.deps.secrets.drop(job_id)
            release_memory()

    async def _parse(self, job: Job, secrets: Secrets) -> None:
        out = self.deps.artifacts.path(job.id, "layout.json")
        if out.exists():
            return
        parser = self.deps.parser_factory(secrets, job.options.parser)
        raw_dir = self.deps.artifacts.job_dir(job.id) / "parser"

        def progress(message: str, fraction: float | None = None) -> None:
            self._update(job, message=message, progress=job.progress if fraction is None else fraction)

        pdf = self.deps.artifacts.path(job.id, "original.pdf")
        result = await self._parse_with_retry(job, lambda: parser.parse(pdf, raw_dir=raw_dir, on_progress=progress))
        out.write_text(result.model_dump_json(), encoding="utf-8")

    async def _parse_with_retry(self, job: Job, attempt_parse: Callable[[], Awaitable[LayoutResult]]) -> LayoutResult:
        """网络类错误（超时、连不上、上传停滞、服务端 5xx）过一会儿常常就好：自动重试，仍失败才交给用户。"""
        delays = self.deps.parse_retry_delays
        for attempt, delay in enumerate(delays, start=1):
            try:
                return await attempt_parse()
            except ParseError as e:
                if not e.retryable:
                    raise
                log.warning("任务 %s 解析失败（%s），%.0f 秒后第 %d 次重试", job.id, e, delay, attempt)
                self._update(job, message=f"网络不稳定，{delay:.0f} 秒后自动重试（第 {attempt}/{len(delays)} 次）：{e}")
                await asyncio.sleep(delay)
        return await attempt_parse()

    async def _fuse(self, job: Job, _: Secrets) -> None:
        out = self.deps.artifacts.path(job.id, "ir.json")
        if out.exists():
            return
        layout = LayoutResult.model_validate_json(self.deps.artifacts.path(job.id, "layout.json").read_text("utf-8"))
        pdf = self.deps.artifacts.path(job.id, "original.pdf")
        chars = await asyncio.to_thread(extract, pdf)
        doc, report = await asyncio.to_thread(fuse, job.id, layout, chars, used_cloud=True)
        out.write_text(doc.model_dump_json(), encoding="utf-8")
        skipped = [p.index + 1 for p in doc.pages if p.kind not in TRANSLATED_KINDS]
        note = f"第 {', '.join(map(str, skipped))} 页的文字无法提取（乱码），暂不翻译" if skipped else ""
        self._update(job, page_count=report.pages, message=note)

    async def _segment(self, job: Job, _: Secrets) -> None:
        if self.deps.store.segments(job.id):
            return  # 已切分过（重试时保留已有译文和用户修改）
        doc = load_document(self.deps, job.id)
        usable = {p.index for p in doc.pages if p.kind in TRANSLATED_KINDS}
        pages = usable if job.options.pages is None else usable & set(job.options.pages)
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
        chat = self.deps.chat_factory(secrets)
        if (cost := add_cost(None, chat.model, found.usage)) is not None:
            self._update(job, cost_cny=(job.cost_cny or 0) + cost)  # 术语抽取也计入任务花费
        out.write_text(json.dumps({"domain": found.domain, "terms": terms}, ensure_ascii=False), encoding="utf-8")

    async def _translate(self, job: Job, secrets: Secrets) -> None:
        segs = self.deps.store.segments(job.id)
        chat = self.deps.chat_factory(secrets)
        matcher = await asyncio.to_thread(self.deps.term_matcher)

        def progress(done: int, total: int, note: str = "") -> None:
            message = f"翻译中 {done}/{total}" + (f"，{note}" if note else "")
            self._update(job, progress=done / total if total else 1.0, message=message)

        out, report = await translate(
            segs, chat=chat, cache=self.deps.cache, ctx=load_context(self.deps, job.id, job.options.user_terms),
            lookup_terms=combine(job.options.user_terms, matcher),
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
        scanned = {p.index for p in doc.pages if p.kind is PageKind.SCANNED}
        await asyncio.to_thread(make_background, self.deps.artifacts.path(job.id, "original.pdf"), blocks,
                                self.deps.artifacts.path(job.id, "background.pdf"), scanned)  # fmt: skip


def segment_counts(store: JobStore, job_id: str) -> dict[str, int]:
    return {s.value: store.count_segments(job_id, s) for s in SegmentStatus}
