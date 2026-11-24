"""流水线：按阶段执行任务（docs/03-mvp-design.md §3.4）。

每个阶段读工件、写工件；已完成的阶段在重跑时跳过（MinerU 结果、译文都已落盘，重跑不重复花钱）。
解析服务与大模型的错误（ParseError / ChatError）面向用户，原样显示；其他异常记录日志并显示“内部错误”。
"""

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NamedTuple, Protocol, runtime_checkable

from pdftr.domain.document_name import paper_title
from pdftr.domain.ir import BlockType, Document, PageKind, Segment, SegmentStatus
from pdftr.domain.job import STAGES, Job, JobStatus
from pdftr.domain.layout import LayoutResult
from pdftr.domain.llm import ChatError, ChatModel
from pdftr.extraction.chars import extract
from pdftr.fusion.build import InvalidLayoutPages, fuse
from pdftr.glossary.extract import Extraction, extract_acronym_definitions, extract_terms, frequent_candidates
from pdftr.glossary.matcher import TermMatcher, TermSelection
from pdftr.glossary.normalize import normalize_terms
from pdftr.parsing.mineru_plan import preview_requested_pages
from pdftr.parsing.port import LayoutCache, LayoutParser, ParseError
from pdftr.pipeline.events import EventBus
from pdftr.pipeline.memory import release_memory
from pdftr.pipeline.secrets import Secrets, SecretStore
from pdftr.pipeline.workers import finish_on_cancel, run_thread
from pdftr.rendering.background import make_background
from pdftr.storage.port import ArtifactStore, JobStore
from pdftr.translation.cost import Price, add_cost
from pdftr.translation.port import TranslationCache
from pdftr.translation.prompt import DocContext
from pdftr.translation.segmenter import BAD_SOURCE_MESSAGE, bad_source, segment
from pdftr.translation.service import TranslateOptions, default_concurrency, translate

log = logging.getLogger(__name__)
PROGRESS_MIN_INTERVAL_S = 1.5
LAYOUT_CACHE_VERSION = 2


def _parse_progress_kind(message: str) -> str:
    for kind, prefixes in (
        ("upload", ("正在上传", "上传")),
        ("queue", ("MinerU 已接收", "MinerU 正在接收", "排队")),
        ("parse", ("MinerU 正在解析", "正在解析")),
        ("convert", ("MinerU 正在整理", "整理")),
        ("download", ("正在下载", "下载")),
        ("cache", ("已复用近期版面", "缓存")),
    ):
        if message.startswith(prefixes):
            return kind
    return "other"


def _pdf_sha256(pdf: Path) -> str:
    digest = hashlib.sha256()
    with pdf.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _layout_cache_key(scope: str, pdf_hash: str, config: str, pages: Sequence[int] | None) -> str:
    pages_key = "all" if pages is None else ",".join(str(p) for p in pages)
    components = [LAYOUT_CACHE_VERSION, scope, pdf_hash, "mineru-cloud", config, pages_key]
    return hashlib.sha256(json.dumps(components, separators=(",", ":")).encode("utf-8")).hexdigest()


ParserFactory = Callable[[Secrets, str], LayoutParser]  # （密钥, 解析服务名）→ 解析器
ChatFactory = Callable[[Secrets], ChatModel]
MatcherLoader = Callable[[], TermMatcher | None]
PriceOverridesLoader = Callable[[], Mapping[str, Price] | None]


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
    free_max_concurrent_translations: int = 1  # 免费通道同时翻译的任务数
    mineru_partial_pages_enabled: bool = True
    layout_cache: LayoutCache | None = None
    layout_cache_ttl_hours: int = 0
    parse_retry_delays: tuple[float, ...] = (15, 45)  # 解析遇到网络类错误时自动重试的等待秒数
    font_dir: Path | None = None  # 导出 PDF 用的字体目录
    llm_concurrency: int = 0  # 0 = 按服务商自动选择（translation.service.default_concurrency）
    price_overrides_loader: PriceOverridesLoader | None = None


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


@runtime_checkable
class _ClosableChat(Protocol):
    async def aclose(self) -> None: ...


@asynccontextmanager
async def chat_session(chat: ChatModel) -> AsyncIterator[ChatModel]:
    """关闭本次调用创建的客户端，兼容没有关闭方法的测试替身。"""
    try:
        yield chat
    finally:
        if isinstance(chat, _ClosableChat):
            try:
                await finish_on_cancel(asyncio.create_task(chat.aclose()))
            except Exception as e:
                log.warning("关闭翻译服务客户端失败（%s）", type(e).__name__)


def _write_json(path: Path, content: str) -> None:
    tmp = path.with_suffix(f"{path.suffix}.part")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def load_document(deps: PipelineDeps, job_id: str) -> Document:
    return Document.model_validate_json(deps.artifacts.path(job_id, "ir.json").read_text(encoding="utf-8"))


def glossary_texts(doc: Document, segments: Sequence[Segment]) -> tuple[list[str], list[str]]:
    """分开收集正文和标题，兼容还没保存段落角色的旧任务。"""
    block_types = {block.id: block.type for block in doc.blocks()}
    body_sources: list[str] = []
    headings: list[str] = []
    for seg in segments:
        role = block_types.get(seg.block_ids[0], seg.role) if seg.block_ids else seg.role
        if role is BlockType.TITLE:
            headings.append(seg.source)
        elif role is not BlockType.REFERENCE:
            body_sources.append(seg.source)
    return body_sources, headings


def load_context(deps: PipelineDeps, job_id: str, user_terms: dict[str, str]) -> tuple[DocContext, TermSelection]:
    doc = load_document(deps, job_id)
    glossary_file = deps.artifacts.path(job_id, "glossary.json")
    found = Extraction.model_validate_json(glossary_file.read_bytes()) if glossary_file.exists() else Extraction()
    selection = TermSelection(found, user_terms, deps.term_matcher)
    return DocContext(title=doc.meta.title, abstract=doc.meta.abstract, domain=found.domain,
                      terms=selection.paper), selection  # fmt: skip


class DynamicSlots:
    """支持运行时动态调限的排队名额管理器：FIFO 唤醒、取消安全、绝不泄漏名额。"""

    def __init__(self, size: int, waiting: Callable[[int], str]) -> None:
        self._limit = max(1, size)
        self._in_use = 0
        self._waiters: dict[object, tuple[asyncio.Future[None], Job | None, Callable[[Job, str], None] | None]] = {}
        self._waiting = waiting  # 排在第 n 位时显示的说明

    def set_limit(self, new_limit: int) -> None:
        self._limit = max(1, new_limit)
        self._dispatch_waiters()

    def stats(self) -> tuple[int, int, int]:
        return self._in_use, self._limit, len(self._waiters)

    @asynccontextmanager
    async def hold(
        self,
        job: Job | None = None,
        notify: Callable[[Job, str], None] | None = None,
        *,
        timeout: float | None = None,
    ) -> AsyncIterator[None]:
        acquired = False
        if self._in_use < self._limit and not self._waiters:
            self._in_use += 1
            acquired = True
        else:
            fut = asyncio.get_running_loop().create_future()
            ticket = object()
            self._waiters[ticket] = (fut, job, notify)
            self._refresh()
            try:
                if timeout is None:
                    await fut
                else:
                    await asyncio.wait_for(fut, timeout)
                acquired = True
            except BaseException:
                self._waiters.pop(ticket, None)
                if fut.done() and not fut.cancelled():
                    # 名额已在取消或超时前转交给当前协程，立即归还防止泄漏。
                    self._release_one()
                else:
                    self._refresh()
                raise
        try:
            yield
        finally:
            if acquired:
                self._release_one()

    def _release_one(self) -> None:
        self._in_use = max(0, self._in_use - 1)
        self._dispatch_waiters()

    def _dispatch_waiters(self) -> None:
        changed = False
        while self._in_use < self._limit and self._waiters:
            ticket, (fut, _, _) = next(iter(self._waiters.items()))
            del self._waiters[ticket]
            changed = True
            if fut.done():
                continue
            self._in_use += 1
            fut.set_result(None)
        if changed:
            self._refresh()

    def _refresh(self) -> None:
        for n, (_, job, notify) in enumerate(self._waiters.values(), start=1):
            if job is not None and notify is not None:
                try:
                    notify(job, self._waiting(n))
                except Exception:
                    log.exception("任务 %s 的排队进度通知失败", job.id)


# 保持旧名称别名兼容历史测试与调用方
Slots = DynamicSlots


class ExportResult(NamedTuple):
    filename: str
    path: Path
    size_bytes: int
    digest: str = ""
    overflow_pages: tuple[int, ...] = ()


class Pipeline:
    def __init__(self, deps: PipelineDeps) -> None:
        self.deps = deps
        # 解析可以几篇同时进行；免费任务在解析阶段额外受软上限约束，至少给自备 Key 留 1 席；
        # 对齐与生成背景页用 PyMuPDF，耗内存，一次一篇；
        # 术语抽取与翻译按通道独立排队（byok_llm / free_llm），避免免费队列卡死自备 Key
        parse = DynamicSlots(deps.max_concurrent_jobs, lambda n: f"排队等待解析，排在第 {n} 位")
        free_parse_limit = max(1, deps.max_concurrent_jobs - 1) if deps.max_concurrent_jobs >= 2 else 1
        free_parse = DynamicSlots(free_parse_limit, lambda n: f"免费通道排队等待解析，排在第 {n} 位")
        local = DynamicSlots(1, lambda n: f"排队等待处理，前面还有 {n} 篇")
        byok_llm = DynamicSlots(deps.max_concurrent_translations, lambda n: f"等待翻译，前面还有 {n} 篇")
        free_llm = DynamicSlots(deps.free_max_concurrent_translations, lambda n: f"免费通道排队翻译，前面还有 {n} 篇")
        self._local = local
        self._free_parse = free_parse
        self._byok_llm = byok_llm
        self._free_llm = free_llm
        self._slots = {"parse": parse, "fuse": local, "segment": local, "glossary": byok_llm, "translate": byok_llm,
                       "render": local}  # fmt: skip
        self.receiving_byok = 0  # 正在接收且尚未调度的自备 Key 上传，只在事件循环读写
        self.receiving_free = 0  # 正在接收且尚未调度的免费通道上传，只在事件循环读写
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._task_channels: dict[str, Literal["byok", "free"]] = {}
        self._cancelling: set[str] = set()  # 用户要求取消的任务（与服务停止时的取消区分开）
        self._mutations: set[str] = set()  # 已完成任务的编辑/重翻/撤回互斥标记
        self._mutation_guard = threading.Lock()
        self._artifact_guard = threading.RLock()
        self._admission_guard = threading.RLock()
        self._exporting: dict[str, int] = {}
        self._deleting: set[str] = set()
        self._exports: dict[Path, asyncio.Task[ExportResult]] = {}
        self._applied_limits_revision: int = -1

    @property
    def receiving(self) -> int:
        return self.receiving_byok + self.receiving_free

    @receiving.setter
    def receiving(self, value: int) -> None:
        self.receiving_byok = max(0, value)
        self.receiving_free = 0

    def receiving_for(self, channel_mode: Literal["byok", "free"]) -> int:
        return self.receiving_free if channel_mode == "free" else self.receiving_byok

    def try_reserve_upload(self, channel_mode: Literal["byok", "free"], limit: int) -> bool:
        """在读取 multipart 请求体前原子预留 1 个通道名额，避免并发请求同时穿透预检。"""
        with self._admission_guard:
            if self.active_count(channel_mode) + self.receiving_for(channel_mode) >= limit:
                return False
            if channel_mode == "free":
                self.receiving_free += 1
            else:
                self.receiving_byok += 1
            return True

    def release_upload(self, channel_mode: Literal["byok", "free"]) -> None:
        """归还在上传中间件中预留的通道名额。"""
        with self._admission_guard:
            if channel_mode == "free":
                self.receiving_free = max(0, self.receiving_free - 1)
            else:
                self.receiving_byok = max(0, self.receiving_byok - 1)

    def apply_runtime_limits(
        self,
        parse_limit: int,
        byok_limit: int,
        free_limit: int,
        *,
        revision: int | None = None,
    ) -> bool:
        """在事件循环内热更新解析与双通道翻译并发上限；忽略落后于已应用版本的迟到回调。"""
        if revision is not None:
            if revision < self._applied_limits_revision:
                return False
            self._applied_limits_revision = revision
        self._slots["parse"].set_limit(parse_limit)
        self._free_parse.set_limit(max(1, parse_limit - 1) if parse_limit >= 2 else 1)
        self._byok_llm.set_limit(byok_limit)
        self._free_llm.set_limit(free_limit)
        return True

    def queue_snapshot(self) -> dict[str, tuple[int, int, int]]:
        parse_in_use, parse_limit, parse_waiting = self._slots["parse"].stats()
        _, _, free_parse_waiting = self._free_parse.stats()
        return {
            "parse": (parse_in_use, parse_limit, parse_waiting + free_parse_waiting),
            "free": self._free_llm.stats(),
            "byok": self._byok_llm.stats(),
        }

    def parse_reserved_for_byok(self) -> bool:
        _, parse_limit, _ = self._slots["parse"].stats()
        return parse_limit >= 2

    def active_jobs_summary(self) -> list[dict[str, object]]:
        """返回在途任务的脱敏运行摘要，绝不含文件名或论文正文。"""
        items: list[dict[str, object]] = []
        with self._admission_guard:
            job_ids = list(self._tasks)
        for job_id in job_ids:
            job = self.deps.store.get_job(job_id)
            if job is None:
                continue
            items.append(
                {
                    "job_id": job.id,
                    "page_count": job.page_count,
                    "stage": job.stage,
                    "progress": job.progress,
                    "channel_mode": job.channel_mode,
                    "model": job.model,
                    "status": job.status.value,
                }
            )
        return items

    # ---- 调度 ----
    def submit(self, job: Job, secrets: Secrets) -> None:
        self.deps.secrets.put(job.id, secrets)
        task = asyncio.create_task(self._run(job.id))
        with self._admission_guard:
            self._tasks[job.id] = task
            self._task_channels[job.id] = job.channel_mode

        def _cleanup(done: asyncio.Task[None]) -> None:
            with self._admission_guard:
                if self._tasks.get(job.id) is done:
                    self._tasks.pop(job.id, None)
                    self._task_channels.pop(job.id, None)

        task.add_done_callback(_cleanup)

    def active_job_ids(self) -> set[str]:
        with self._admission_guard:
            return set(self._tasks)

    def is_running(self, job_id: str) -> bool:
        with self._admission_guard:
            return job_id in self._tasks

    def cancel(self, job_id: str) -> bool:
        """取消排队中或进行中的任务；返回是否有任务可取消。已完成的阶段与译文缓存保留，重试时从中断处继续。"""
        with self._admission_guard:
            task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        self._cancelling.add(job_id)
        task.cancel()
        return True

    @asynccontextmanager
    async def local_slot(self) -> AsyncIterator[None]:
        """导出与本机处理阶段共享同一个重任务名额。"""
        async with self._local.hold():
            yield

    @asynccontextmanager
    async def translation_slot(
        self,
        channel_mode: Literal["byok", "free"],
        *,
        timeout: float | None = None,
    ) -> AsyncIterator[None]:
        """复用对应通道的翻译并发槽，使单段与批量重翻也受通道并发上限约束并在概况可见。"""
        slot = self._free_llm if channel_mode == "free" else self._byok_llm
        async with slot.hold(timeout=timeout):
            yield

    async def export_once(self, path: Path, render: Callable[[], Coroutine[None, None, ExportResult]]) -> ExportResult:
        task = self._exports.get(path)
        if task is None or task.done():

            def finished(done: asyncio.Task[ExportResult]) -> None:
                if self._exports.get(path) is done:
                    self._exports.pop(path, None)
                if not done.cancelled():
                    done.exception()  # 超时或断开后可能已没有等待方

            task = asyncio.create_task(render())
            self._exports[path] = task
            task.add_done_callback(finished)
        return await asyncio.shield(task)

    @contextmanager
    def export_access(self, job_id: str) -> Iterator[bool]:
        with self._artifact_guard:
            allowed = job_id not in self._deleting
            if allowed:
                self._exporting[job_id] = self._exporting.get(job_id, 0) + 1
        try:
            yield allowed
        finally:
            if allowed:
                with self._artifact_guard:
                    self._exporting[job_id] -= 1
                    if self._exporting[job_id] == 0:
                        del self._exporting[job_id]

    @contextmanager
    def export_cleanup_access(self, job_id: str) -> Iterator[bool]:
        """清旧缓存时短暂独占工件，下载或快照读取中的文件留待后续清理。"""
        with self._artifact_guard:
            yield self._exporting.get(job_id) == 1

    @contextmanager
    def deletion_access(self, job_id: str) -> Iterator[bool]:
        with self._artifact_guard:
            allowed = job_id not in self._deleting and job_id not in self._exporting and not self.is_running(job_id)
            if allowed:
                self._deleting.add(job_id)
        try:
            yield allowed
        finally:
            if allowed:
                with self._artifact_guard:
                    self._deleting.remove(job_id)

    def active_count(self, channel_mode: Literal["byok", "free"] | None = None) -> int:
        """排队中与执行中的任务数，可按通道独立统计。"""
        with self._admission_guard:
            if channel_mode is None:
                return len(self._tasks)
            return sum(1 for mode in self._task_channels.values() if mode == channel_mode)

    def pending_work_count(self) -> int:
        """停机时还需等待的流水线任务与导出。"""
        return self.active_count() + sum(not task.done() for task in self._exports.values())

    def begin_mutation(self, job_id: str) -> bool:
        """占用一个任务的用户修改槽，避免两个请求同时覆盖段落或修改记录。"""
        with self._mutation_guard:
            if job_id in self._mutations:
                return False
            self._mutations.add(job_id)
            return True

    def end_mutation(self, job_id: str) -> None:
        with self._mutation_guard:
            self._mutations.discard(job_id)

    def recover_interrupted(self) -> None:
        """服务重启时，进行中的任务已没有密钥可用，标记失败，用户重新提供 Key 后可从断点重试。"""
        self.deps.artifacts.purge_parser_tmp(set())
        for status in (JobStatus.RUNNING, JobStatus.QUEUED):
            for job in self.deps.store.jobs_with_status(status):
                job.status, job.error = JobStatus.FAILED, "服务重启，任务中断，请点击重试"
                self.deps.store.save_job(job)

    async def wait_all(self, timeout: float | None = None) -> bool:
        """等待在途的翻译和导出，超时返回 False。"""
        with self._admission_guard:
            tasks = [*self._tasks.values(), *self._exports.values()]
        if not tasks:
            return True
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        return not pending

    # ---- 执行 ----
    def _update(self, job: Job, **changes: object) -> None:
        for k, v in changes.items():
            setattr(job, k, v)
        self.deps.store.save_job(job)
        self.deps.events.publish(job)

    def _slot_for(self, stage: str, channel_mode: Literal["byok", "free"]) -> DynamicSlots:
        if stage in ("glossary", "translate"):
            return self._free_llm if channel_mode == "free" else self._byok_llm
        return self._slots[stage]

    @asynccontextmanager
    async def _hold_group(
        self, group: Sequence[str], job: Job, notify: Callable[[Job, str], None]
    ) -> AsyncIterator[None]:
        slots = self._slot_for(group[0], job.channel_mode)
        if group[0] == "parse" and job.channel_mode == "free":
            async with self._free_parse.hold(job, notify), slots.hold(job, notify):
                yield
            return
        async with slots.hold(job, notify):
            yield

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
                slots = self._slot_for(todo[0], job.channel_mode)
                n = next((i for i, s in enumerate(todo) if self._slot_for(s, job.channel_mode) is not slots), len(todo))
                group, todo = todo[:n], todo[n:]
                summary = job.message  # 排队说明会覆盖消息，先记下翻译阶段的总结
                self._update(job, stage=group[0], progress=0.0)
                async with self._hold_group(group, job, lambda j, msg: self._update(j, message=msg)):
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
        raw_dir = None if job.options.parser == "mineru-cloud" else self.deps.artifacts.job_dir(job.id) / "parser"

        last_written_at = 0.0
        last: tuple[str, str, float | None] | None = None

        def progress(message: str, fraction: float | None = None) -> None:
            nonlocal last_written_at, last
            now = time.monotonic()
            current = (_parse_progress_kind(message), message, fraction)
            if (
                last is None
                or current[0] != last[0]
                or (fraction == 1.0 and last[2] != 1.0)
                or any(warning in message for warning in ("超时", "重试", "准备重新提交"))
                or (current != last and now - last_written_at >= PROGRESS_MIN_INTERVAL_S)
            ):
                self._update(job, message=message, progress=job.progress if fraction is None else fraction)
                last_written_at, last = now, current

        pdf = self.deps.artifacts.path(job.id, "original.pdf")
        pages = (
            job.options.pages
            if job.options.parser == "mineru-cloud" and self.deps.mineru_partial_pages_enabled
            else None
        )
        cache = self.deps.layout_cache
        cache_config = getattr(parser, "cache_config_sig", None)
        cache_ttl = self.deps.layout_cache_ttl_hours
        cache_identity: tuple[str, str, str] | None = None
        if (
            job.options.parser == "mineru-cloud"
            and cache is not None
            and cache_ttl > 0
            and isinstance(cache_config, str)
        ):
            started = time.monotonic()
            scope = hashlib.sha256(f"pdftr-layout-scope-v1:{secrets.parser_token}".encode()).hexdigest()
            pdf_hash = await run_thread(_pdf_sha256, pdf)
            wanted = None
            if pages is not None:
                async with self.local_slot():
                    wanted = await run_thread(preview_requested_pages, pdf, pages)
            cache_identity = (scope, pdf_hash, cache_config)
            key = _layout_cache_key(*cache_identity, wanted)
            hit = None
            try:
                hit = await run_thread(cache.get, key, job_id=job.id)
                if hit is None and wanted is not None:
                    hit = await run_thread(cache.get, _layout_cache_key(*cache_identity, None), job_id=job.id)
            except (OSError, sqlite3.Error):
                log.warning("任务 %s 读取版面缓存失败，将向 MinerU 解析", job.id)
            if hit is not None:
                log.info(
                    "版面缓存命中 job=%s pages=%s elapsed_ms=%d",
                    job.id,
                    "all" if wanted is None else len(wanted),
                    (time.monotonic() - started) * 1000,
                )
                progress("已复用近期版面解析结果（跳过 MinerU 请求）", 1.0)
                _write_json(out, hit.model_dump_json())
                return

        result = await self._parse_with_retry(
            job, lambda: parser.parse(pdf, pages=pages, raw_dir=raw_dir, on_progress=progress)
        )
        _write_json(out, result.model_dump_json())
        if cache is not None and cache_identity is not None:
            try:
                key = _layout_cache_key(*cache_identity, result.requested_pages)
                await run_thread(cache.put, key, result, job_id=job.id, ttl_seconds=cache_ttl * 3600)
            except Exception:
                log.warning("任务 %s 写入版面缓存失败", job.id)

    async def _parse_with_retry(self, job: Job, attempt_parse: Callable[[], Awaitable[LayoutResult]]) -> LayoutResult:
        """网络错误或 MinerU 云端解析超时自动重试，最多使用配置的重试机会。"""
        delays = self.deps.parse_retry_delays
        retry_limit = len(delays)
        for attempt, delay in enumerate(delays, start=1):
            try:
                return await attempt_parse()
            except ParseError as e:
                if e.max_retries is not None:
                    retry_limit = min(retry_limit, e.max_retries)
                if not e.retryable or attempt > retry_limit:
                    raise
                log.warning("任务 %s 解析失败（%s），%.0f 秒后第 %d 次重试", job.id, e, delay, attempt)
                if str(e).startswith("MinerU 解析超过"):
                    message = f"MinerU 解析超时，{delay:.0f} 秒后重新提交（第 {attempt}/{retry_limit} 次重试）"
                else:
                    message = f"网络不稳定，{delay:.0f} 秒后自动重试（第 {attempt}/{retry_limit} 次）：{e}"
                self._update(job, message=message, progress=0.0)
                await asyncio.sleep(delay)
        return await attempt_parse()

    async def _fuse(self, job: Job, _: Secrets) -> None:
        out = self.deps.artifacts.path(job.id, "ir.json")
        if out.exists():
            doc = load_document(self.deps, job.id)
            self.deps.store.update_submission_title(job.id, paper_title(doc.pages, fallback=doc.meta.title))
            return
        layout = LayoutResult.model_validate_json(self.deps.artifacts.path(job.id, "layout.json").read_text("utf-8"))
        pdf = self.deps.artifacts.path(job.id, "original.pdf")
        chars = await run_thread(extract, pdf)
        try:
            doc, report = await run_thread(
                fuse,
                job.id,
                layout,
                chars,
                used_cloud=True,
                pages_to_translate=job.options.pages,
                expected_layout_pages=layout.requested_pages,
            )
        except InvalidLayoutPages as e:
            raise StageError(str(e)) from e
        if report.missing_layout_pages:
            log.warning("任务 %s 的解析结果缺少 PDF 页号 %s", job.id, report.missing_layout_pages)
        _write_json(out, doc.model_dump_json())
        # IR 先落盘，恢复分支也会补写；标题持久化成功后才进入后续阶段。
        self.deps.store.update_submission_title(job.id, paper_title(doc.pages, fallback=doc.meta.title))
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
            raise StageError("没有找到可翻译的文字" + _missing_layout_note(doc))
        self.deps.store.replace_segments(job.id, segs)

    async def _glossary(self, job: Job, secrets: Secrets) -> None:
        out = self.deps.artifacts.path(job.id, "glossary.json")
        if out.exists():
            return
        doc = load_document(self.deps, job.id)
        segs = self.deps.store.segments(job.id)
        if bad_source(segs):
            raise StageError(BAD_SOURCE_MESSAGE)
        body_sources, headings = glossary_texts(doc, segs)
        candidates = frequent_candidates(body_sources)
        definitions = extract_acronym_definitions(body_sources, candidates)
        intro = next((s.source for s in segs if len(s.source.split()) >= 30 and s.source != doc.meta.abstract), "")
        prices = self.deps.price_overrides_loader() if self.deps.price_overrides_loader else None
        async with chat_session(self.deps.chat_factory(secrets)) as chat:
            found = await extract_terms(
                chat,
                title=doc.meta.title,
                abstract=doc.meta.abstract,
                intro=intro,
                headings=headings,
                candidates=candidates,
                definitions=definitions,
                user_terms=job.options.user_terms,
            )
            billing_model = found.actual_model or chat.model
            cost = add_cost(None, billing_model, found.usage, prices=prices)
        terms = normalize_terms(found.terms, body_sources, headings)
        updates: dict[str, object] = {}
        if cost is not None:
            updates["cost_cny"] = (job.cost_cny or 0) + cost  # 术语抽取也计入任务花费
        if found.used_fallback:
            updates["fallback_used"] = True
        if updates:
            self._update(job, **updates)
        found.terms = terms
        _write_json(out, found.model_dump_json(exclude={"usage", "actual_model", "used_fallback"}))

    async def _translate(self, job: Job, secrets: Secrets) -> None:
        segs = self.deps.store.segments(job.id)
        if bad_source(segs):
            raise StageError(BAD_SOURCE_MESSAGE)
        ctx, selection = await asyncio.to_thread(load_context, self.deps, job.id, job.options.user_terms)

        def progress(done: int, total: int, note: str = "") -> None:
            message = f"翻译中 {done}/{total}" + (f"，{note}" if note else "")
            self._update(job, progress=done / total if total else 1.0, message=message)

        prices = self.deps.price_overrides_loader() if self.deps.price_overrides_loader else None
        async with chat_session(self.deps.chat_factory(secrets)) as chat:
            out, report = await translate(
                segs, chat=chat, cache=self.deps.cache, ctx=ctx,
                lookup_term_hits=selection.match_segment, context_segments=segs,
                opts=TranslateOptions(
                    quality=job.options.quality,
                    concurrency=self.llm_concurrency(secrets),
                    price_overrides=prices,
                ),
                on_progress=progress,
            )  # fmt: skip
            model = chat.model
        self.deps.store.save_segments(out)
        cost = report.cost_cny
        total = None if cost is None and job.cost_cny is None else (job.cost_cny or 0) + (cost or 0)
        updates: dict[str, object] = {"model": model, "cost_cny": total, "message": _translation_summary(out)}
        if report.used_fallback:
            updates["fallback_used"] = True
        self._update(job, **updates)
        if report.failed:
            reasons = "；".join(list(dict.fromkeys(report.failures.values()))[:3])
            raise StageError(
                f"还有 {report.failed} 段未完成翻译，已达到尝试次数或时间上限。"
                f"{reasons}。已成功的译文已保留，点击重试只继续处理未完成段落"
            )

    def llm_concurrency(self, secrets: Secrets) -> int:
        return self.deps.llm_concurrency or default_concurrency(secrets.llm_base_url)

    async def _render(self, job: Job, _: Secrets) -> None:
        doc = load_document(self.deps, job.id)
        segments = self.deps.store.segments(job.id)
        ids = {bid for s in segments for bid in s.block_ids}
        blocks = [b for b in doc.blocks() if b.id in ids]
        scanned = {p.index for p in doc.pages if p.kind is PageKind.SCANNED}
        await run_thread(make_background, self.deps.artifacts.path(job.id, "original.pdf"), blocks,
                                self.deps.artifacts.path(job.id, "background.pdf"), scanned)  # fmt: skip
        self._update(job, message=_translation_summary(segments) + _missing_layout_note(doc))


def _translation_summary(segments: Sequence[Segment]) -> str:
    translated = sum(s.status in (SegmentStatus.DONE, SegmentStatus.EDITED) for s in segments)
    return f"已翻译 {translated}/{len(segments)} 段"


def _missing_layout_note(doc: Document) -> str:
    pages = "、".join(str(p.index + 1) for p in doc.pages if p.missing_layout)
    return f"。第 {pages} 页未返回版面解析结果，已保留原文" if pages else ""


def segment_counts(store: JobStore, job_id: str) -> dict[str, int]:
    return {s.value: store.count_segments(job_id, s) for s in SegmentStatus}
