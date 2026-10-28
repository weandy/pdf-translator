"""接口层调用的用例：创建任务、查询、段落编辑与重翻、费用预估、公式裁剪图。

接口层（api）只做 HTTP 与这些函数之间的转换，不含业务规则。
"""

import logging
import shutil
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from pdftr.domain.ids import new_job_id
from pdftr.domain.ir import Document, Quality, Revision, RevisionAction, Segment, SegmentStatus
from pdftr.domain.job import STAGES, Job, JobOptions, JobStatus
from pdftr.domain.llm import ChatError, ChatOptions
from pdftr.extraction.chars import page_count
from pdftr.glossary.matcher import combine
from pdftr.glossary.user_import import ParsedGlossary, parse_upload
from pdftr.pipeline.runner import Pipeline, load_context, load_document
from pdftr.pipeline.secrets import Secrets
from pdftr.rendering.background import crop_png
from pdftr.rendering.export import ExportMode, Side, export_pdf
from pdftr.translation.cost import PRICES, is_deepseek_peak
from pdftr.translation.service import TranslateOptions, translate

log = logging.getLogger(__name__)


class UserError(ValueError):
    """请求本身有问题（参数、文件、缺少 Key 等），对应 4xx。"""


class NotFound(LookupError):
    pass


class ServiceBusy(RuntimeError):
    """服务器暂时无法接收新任务（排队已满、磁盘空间不足），对应 503。"""


class Limits(BaseModel):
    max_upload_mb: int
    max_pages: int
    max_active_jobs: int = 1_000_000
    min_free_disk_mb: int = 0
    data_dir: Path = Path(".")


PARSER_NAMES = {"mineru-cloud": "MinerU", "paddleocr-vl": "PaddleOCR"}


def _require_secrets(secrets: Secrets, *, parser: str | None) -> None:
    """parser 为本次要用的解析服务；不需要解析（已解析过）时为 None。"""
    if not secrets.llm_api_key:
        raise UserError("请先在设置中填写翻译服务的 API Key")
    if parser and not secrets.token_for(parser):
        raise UserError(f"请先在设置中填写 {PARSER_NAMES.get(parser, parser)} Token")


def create_job(
    pipeline: Pipeline, data: bytes, filename: str, options: JobOptions, secrets: Secrets, limits: Limits
) -> Job:
    if len(data) > limits.max_upload_mb * 1024 * 1024:
        raise UserError(f"文件超过 {limits.max_upload_mb} MB")
    if not data.startswith(b"%PDF"):
        raise UserError("只支持 PDF 文件")
    if not options.cloud_consent:  # 两个解析服务都在云端
        raise UserError(f"使用云端解析前需要确认文件会上传到 {PARSER_NAMES.get(options.parser, options.parser)}")
    _require_secrets(secrets, parser=options.parser)
    if pipeline.active_count() >= limits.max_active_jobs:
        raise ServiceBusy("服务器正忙，排队的任务太多，请稍后再试")
    if shutil.disk_usage(limits.data_dir).free < limits.min_free_disk_mb * 1024 * 1024:
        raise ServiceBusy("服务器存储空间不足，请稍后再试")
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(data)
        tmp.flush()
        try:
            pages = page_count(Path(tmp.name))
        except ValueError as e:
            raise UserError(str(e)) from e
    if pages > limits.max_pages:
        raise UserError(f"PDF 共 {pages} 页，超过上限 {limits.max_pages} 页")

    deps = pipeline.deps
    job = Job(id=new_job_id(), filename=Path(filename).name[:200] or "document.pdf", page_count=pages,
              options=options, model=secrets.llm_model)  # fmt: skip
    deps.artifacts.path(job.id, "original.pdf").write_bytes(data)
    deps.store.save_job(job)
    pipeline.submit(job, secrets)
    return job


def get_job(pipeline: Pipeline, job_id: str) -> Job:
    job = pipeline.deps.store.get_job(job_id)
    if job is None:
        raise NotFound("任务不存在")
    return job


def delete_job(pipeline: Pipeline, job_id: str) -> None:
    """删除任务及其全部文件。进行中的任务要等结束后才能删（后台线程可能还在写文件）。"""
    get_job(pipeline, job_id)
    if pipeline.is_running(job_id):
        raise UserError("任务进行中，结束后才能删除")
    pipeline.deps.store.delete_job(job_id)
    pipeline.deps.artifacts.remove(job_id)


def purge_expired(pipeline: Pipeline, retention_days: int, now: datetime | None = None) -> int:
    """删除超过保留期的任务（跳过进行中的）与译文缓存，返回删除的任务数。"""
    if retention_days <= 0:
        return 0
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    removed = 0
    for job_id in pipeline.deps.store.jobs_created_before(cutoff):
        if pipeline.is_running(job_id):
            continue
        pipeline.deps.store.delete_job(job_id)
        pipeline.deps.artifacts.remove(job_id)
        removed += 1
    cached = pipeline.deps.cache.purge_older_than(retention_days)
    if removed or cached:
        log.info("已清理过期任务 %d 个、译文缓存 %d 条", removed, cached)
    return removed


def export_job(pipeline: Pipeline, job_id: str, mode: ExportMode, side: Side = "right") -> tuple[str, bytes]:
    """导出纯译文或双语对照 PDF，返回（建议的文件名, 内容）。每次按当前译文生成，包含用户的修改。"""
    job = get_job(pipeline, job_id)
    if job.status is not JobStatus.DONE:
        raise UserError("任务完成后才能下载")
    deps = pipeline.deps
    data = export_pdf(deps.artifacts.path(job_id, "original.pdf"), deps.artifacts.path(job_id, "background.pdf"),
                      load_document(deps, job_id), deps.store.segments(job_id), mode, deps.font_dir, side)  # fmt: skip
    suffix = "译文" if mode == "mono" else "双语对照"
    return f"{Path(job.filename).stem}-{suffix}.pdf", data


def retry_job(pipeline: Pipeline, job_id: str, secrets: Secrets) -> Job:
    job = get_job(pipeline, job_id)
    if pipeline.is_running(job_id):
        raise UserError("任务正在进行中")
    if job.status is JobStatus.DONE and not pipeline.deps.store.count_segments(job_id, SegmentStatus.FAILED):
        raise UserError("任务已完成")
    _require_secrets(secrets, parser=job.options.parser if job.stage == "parse" else None)
    if job.stage == "done":
        job.stage = "translate"  # 已完成但有失败段落：只重跑翻译和背景页
    job.status, job.error = JobStatus.QUEUED, ""
    pipeline.deps.store.save_job(job)
    pipeline.submit(job, secrets)
    return job


class DocumentView(BaseModel):
    job: Job
    document: Document
    segments: list[Segment]


def document_view(pipeline: Pipeline, job_id: str) -> DocumentView:
    job = get_job(pipeline, job_id)
    if STAGES.index("segment") > (STAGES.index(job.stage) if job.stage in STAGES else len(STAGES)):
        raise UserError("文档尚未解析完成")
    return DocumentView(job=job, document=load_document(pipeline.deps, job_id),
                        segments=pipeline.deps.store.segments(job_id))  # fmt: skip


def _segment(pipeline: Pipeline, segment_id: str) -> Segment:
    seg = pipeline.deps.store.segment(segment_id)
    if seg is None:
        raise NotFound("段落不存在")
    return seg


def _record(
    pipeline: Pipeline, before: Segment, after: Segment, action: RevisionAction, reverts: int | None = None
) -> None:
    pipeline.deps.store.add_revision(
        Revision(
            job_id=after.job_id, segment_id=after.id, action=action, reverts=reverts,
            before=before.target, before_status=before.status, before_quality=before.quality,
            after=after.target, after_status=after.status, after_quality=after.quality,
        )
    )  # fmt: skip


def edit_segment(pipeline: Pipeline, segment_id: str, target: str) -> Segment:
    seg = _segment(pipeline, segment_id)
    before = seg.model_copy()
    seg.target, seg.status = target.strip(), SegmentStatus.EDITED
    pipeline.deps.store.save_segments([seg])
    _record(pipeline, before, seg, "edit")
    return seg


GLOSSARY_MAX_BYTES = 2 * 1024 * 1024


async def parse_glossary(pipeline: Pipeline, secrets: Secrets, data: bytes, *, use_ai: bool) -> ParsedGlossary:
    """识别用户上传的术语表（不保存，结果交给用户确认）。use_ai 时用翻译服务读取任意格式，否则按规则识别。"""
    if len(data) > GLOSSARY_MAX_BYTES:
        raise UserError("术语表文件太大（上限 2 MB），可以拆成几个文件分别导入")
    if use_ai and not secrets.llm_api_key:
        raise UserError("AI 识别要用翻译服务，请先在设置中填写 API Key")
    return await parse_upload(data, chat=pipeline.deps.chat_factory(secrets) if use_ai else None)


class LlmCheck(BaseModel):
    ok: bool
    model: str
    latency_ms: int
    reply: str = ""
    detail: str = ""


async def llm_models(pipeline: Pipeline, secrets: Secrets) -> list[str]:
    """从翻译服务拉取可用模型（各家都提供 /models）。"""
    _require_secrets(secrets, parser=None)
    return await pipeline.deps.chat_factory(secrets).list_models()


async def llm_check(pipeline: Pipeline, secrets: Secrets) -> LlmCheck:
    """发一个很短的请求，检查接口地址、Key、模型能否连通（费用可忽略）。"""
    started = time.monotonic()
    try:
        _require_secrets(secrets, parser=None)
        r = await pipeline.deps.chat_factory(secrets).complete(
            "只回复 OK 两个字母。", "ping", ChatOptions(max_tokens=16)
        )
    except (ChatError, UserError) as e:
        return LlmCheck(ok=False, model=secrets.llm_model, latency_ms=_ms(started), detail=str(e))
    return LlmCheck(ok=True, model=secrets.llm_model, latency_ms=_ms(started), reply=r.text.strip()[:40])


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def revisions(pipeline: Pipeline, job_id: str) -> list[Revision]:
    get_job(pipeline, job_id)
    return pipeline.deps.store.revisions(job_id)


def revert_revision(pipeline: Pipeline, revision_id: int) -> Segment:
    """把这一段恢复到这一步之前的样子；这一段在这一步之后的修改一并作废（不同段落互不影响）。"""
    store = pipeline.deps.store
    rev = store.revision(revision_id)
    if rev is None:
        raise NotFound("修改记录不存在")
    if rev.action == "revert" or rev.reverted_by is not None:
        raise UserError("这一步已经撤回过了")
    seg = _segment(pipeline, rev.segment_id)
    before = seg.model_copy()
    seg.target, seg.status, seg.quality = rev.before, rev.before_status, rev.before_quality
    store.save_segments([seg])
    undo = store.add_revision(
        Revision(
            job_id=seg.job_id, segment_id=seg.id, action="revert", reverts=rev.id,
            before=before.target, before_status=before.status, before_quality=before.quality,
            after=seg.target, after_status=seg.status, after_quality=seg.quality,
        )
    )  # fmt: skip
    later = [
        r.model_copy(update={"reverted_by": undo.id})
        for r in store.revisions(seg.job_id)
        if r.segment_id == seg.id
        and r.id >= rev.id
        and r.id != undo.id
        and r.action != "revert"
        and r.reverted_by is None
    ]
    store.save_revisions(later)
    return seg


async def retranslate_segment(
    pipeline: Pipeline, segment_id: str, quality: Quality, secrets: Secrets, user_terms: dict[str, str] | None = None
) -> Segment:
    """user_terms：浏览器里最新的用户术语库（用户纠正术语后重翻这一段时用）；不传则用建任务时的。"""
    _require_secrets(secrets, parser=None)
    seg = _segment(pipeline, segment_id)
    terms = user_terms if user_terms is not None else get_job(pipeline, seg.job_id).options.user_terms
    before = seg.model_copy()
    seg.status = SegmentStatus.PENDING
    deps = pipeline.deps
    chat = deps.chat_factory(secrets)
    matcher = deps.term_matcher()
    (out,), report = await translate(
        [seg], chat=chat, cache=deps.cache, ctx=load_context(deps, seg.job_id, terms),
        lookup_terms=combine(terms, matcher),
        opts=TranslateOptions(quality=quality, use_cache=False),
    )  # fmt: skip
    deps.store.save_segments([out])
    _record(pipeline, before, out, "careful" if quality == "careful" else "retranslate")
    if report.cost_cny is not None:  # 重翻、精修的费用也计入任务花费
        job = get_job(pipeline, seg.job_id)
        job.cost_cny = (job.cost_cny or 0) + report.cost_cny
        deps.store.save_job(job)
    return out


def formula_crop(pipeline: Pipeline, segment_id: str, key: str) -> bytes:
    seg = _segment(pipeline, segment_id)
    ref = seg.formula_refs.get(key)
    if ref is None:
        raise NotFound("公式不存在")
    return crop_png(pipeline.deps.artifacts.path(seg.job_id, "original.pdf"), ref.page, ref.bbox)


class Estimate(BaseModel):
    pages: int
    cny_low: float | None
    cny_high: float | None
    peak: bool
    minutes: float


# M3 实测：DeepSeek Flash 默认档每页约 ¥0.0023–0.0037（空闲），精翻约 4 倍；
# 耗时主要在 MinerU 排队（30 秒到 4 分钟）与翻译（每 100 段约 20 秒）
_PER_PAGE = {"default": (0.0023, 0.0040), "careful": (0.009, 0.016)}


def estimate(pages: int, quality: Quality, model: str) -> Estimate:
    peak = is_deepseek_peak()
    if model in PRICES:
        factor = PRICES[model].peak_factor if peak else 1.0
        if model != "deepseek-flash":  # 其他已知模型按输出单价折算
            factor *= PRICES[model].output / PRICES["deepseek-flash"].output
        low, high = (round(pages * v * factor, 3) for v in _PER_PAGE[quality])
    else:
        low = high = None
    return Estimate(pages=pages, cny_low=low, cny_high=high, peak=peak, minutes=round(1 + pages * 0.15, 1))
