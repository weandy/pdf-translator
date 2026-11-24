"""翻译用例（docs/03-mvp-design.md §3.5）。

查缓存 → 相同原文去重 → 按 token 预算分批 → 并发调用模型 → 校验编号与占位符
→ 不合格的段落单独重试 → 写缓存、汇总用量与费用。
鉴权失败、余额不足直接抛出；连接类临时错误纳入有次数和时限的单段重试。
"""

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from itertools import pairwise
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from pdftr.domain.ir import BlockType, Segment, SegmentStatus
from pdftr.domain.llm import ChatError, ChatModel, ChatOptions, ChatUsage
from pdftr.domain.text import RICH_TAG, normalize_term_key
from pdftr.translation.batching import (
    BATCH_TOKENS,
    VALIDATION_VERSION,
    NumberedResponseError,
    bold_problems,
    citation_problems,
    estimate_tokens,
    italic_problems,
    make_batches,
    normalize_cjk_spacing,
    normalize_formula_placeholders,
    parse_numbered,
    placeholder_problems,
    prose_problems,
    rich_text_problems,
    script_problems,
    script_value_problems,
)
from pdftr.translation.cost import add_cost
from pdftr.translation.port import TranslationCache
from pdftr.translation.prompt import (
    PROMPT_VERSION,
    DocContext,
    SegmentPromptMeta,
    expand_formulas,
    system_prompt,
    user_prompt,
)
from pdftr.translation.segmenter import BAD_SOURCE_MESSAGE, bad_source

Quality = Literal["default", "careful"]
TermLookup = Callable[[str], dict[str, str]]
TermHitsLookup = Callable[[str], tuple[dict[str, str], dict[str, str], dict[str, str]]]
Progress = Callable[[int, int, str], None]  # （已完成, 总数, 补充说明；平常为空）
log = logging.getLogger(__name__)


def default_concurrency(base_url: str) -> int:
    """按服务商选默认并发数：DeepSeek 官方不限并发，用 4 路；本机模型（Ollama 等）通常只有一块显卡，
    并发反而更慢，用 1 路；其他服务商限额差别大，保守用 2 路。部署者可用 PDFTR_LLM_CONCURRENCY 覆盖。"""
    host = (urlparse(base_url).hostname or "").lower()
    if host == "deepseek.com" or host.endswith(".deepseek.com"):
        return 4
    if host in ("localhost", "127.0.0.1", "::1", "host.docker.internal") or host.endswith(".local"):
        return 1
    return 2


class TranslateOptions(BaseModel):
    quality: Quality = "default"  # careful = 精翻（思考·low）
    concurrency: int = Field(default=4, ge=1)
    batch_tokens: int = Field(default=BATCH_TOKENS, ge=1)
    max_attempts: int = Field(default=10, ge=1, le=10)  # 包含首次批量翻译，避免 SDK 与业务重试相乘
    request_timeout_s: float = Field(default=30, gt=0)
    retry_timeout_s: float = Field(default=120, gt=0)  # 首批返回后的全部补救轮次共用，不逐段重新计时
    total_timeout_s: float | None = Field(default=None, gt=0)  # 同步重翻还要覆盖首批和排队，早于网关超时返回
    use_cache: bool = True  # 单段“重翻”时为 False：用户要的是另一个译文


class TranslateReport(BaseModel):
    total: int = 0
    translated: int = 0
    cached: int = 0
    failed: int = 0
    requests: int = 0
    usage: ChatUsage = Field(default_factory=ChatUsage)
    cost_cny: float | None = None
    failures: dict[str, str] = Field(default_factory=dict)
    attempts: dict[str, int] = Field(default_factory=dict)


_PROTOCOL_TAG = re.compile(r"</?translations\b[^>]*>", re.I)
_MALFORMED_FORMULA_TAG = re.compile(r"</m\d+\s*>|<m\d+(?:\s[^<>]*?)?(?<!/)>|<m\d+\s+[^<>/\s][^<>]*/>", re.I)
_FORMULA_FOLLOWED_BY_EQ = re.compile(r"<(m\d+)/>\s*[=＝]")
_HIGH_FORMULA_THRESHOLD = 5
_HIGH_FORMULA_MAX_SEGMENTS = 4
_HIGH_FORMULA_MAX_FORMULAS = 18
_THINKING_MIN_TIMEOUT_S = 60.0
_THINKING_TIMEOUT_SCALE_THRESHOLD_S = 5.0


def _latex_has_top_level_equals(latex: str) -> bool:
    """判断 LaTeX 外层（非花括号内部，如 \\sum_{i=1} 下标）是否已包含等号。"""
    depth = 0
    i = 0
    n = len(latex)
    while i < n:
        ch = latex[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
        elif ch == "=" and depth == 0:
            return True
        i += 1
    return False


def _duplicate_formula_equals_problems(source: str, target: str, formulas: Mapping[str, str] | None) -> list[str]:
    if not formulas:
        return []
    src_counts: dict[str, int] = {}
    for m in _FORMULA_FOLLOWED_BY_EQ.finditer(source):
        key = m.group(1)
        src_counts[key] = src_counts.get(key, 0) + 1
    tgt_counts: dict[str, int] = {}
    for m in _FORMULA_FOLLOWED_BY_EQ.finditer(target):
        key = m.group(1)
        tgt_counts[key] = tgt_counts.get(key, 0) + 1
    problems: list[str] = []
    for key, count in tgt_counts.items():
        if count <= src_counts.get(key, 0):
            continue
        latex = formulas.get(key, "")
        if latex and _latex_has_top_level_equals(latex):
            problems.append(f"公式 <{key}/> 已包含等号，请勿在占位符后重复写出等号和数值")
    return problems


def _protocol_tag_problems(source: str, target: str) -> list[str]:
    problems: list[str] = []
    if any(m[0] not in source for m in _PROTOCOL_TAG.finditer(target)):
        problems.append("译文包含协议标签（如 </translations>），请移除后重新输出")
    bad_formulas = [m[0] for m in _MALFORMED_FORMULA_TAG.finditer(target) if m[0] not in source]
    if bad_formulas:
        sample = "、".join(dict.fromkeys(bad_formulas))
        problems.append(f"公式标签格式错误（{sample}），请使用自闭合占位符如 <m1/>")
    return problems


def _target_problems(
    source: str,
    target: str,
    *,
    translated_terms: dict[str, str] | None = None,
    formulas: Mapping[str, str] | None = None,
) -> list[str]:
    if problems := _protocol_tag_problems(source, target):
        return problems
    if problems := placeholder_problems(source, target):
        return ["公式占位符不一致：" + "；".join(problems)]
    if problems := _duplicate_formula_equals_problems(source, target, formulas):
        return problems
    errors: list[str] = []
    for label, check in (
        ("粗体标记", bold_problems),
        ("斜体标记", italic_problems),
        ("上下标标记", script_problems),
    ):
        if problems := check(source, target):
            errors.append(f"{label}不一致：" + "；".join(problems))
    if structure := errors or rich_text_problems(target):
        return structure
    anchors = citation_problems(source, target) + script_value_problems(source, target)
    return anchors or prose_problems(source, target, translated_terms=translated_terms)


_BLOCK_PAGE = re.compile(r"p([0-9]{1,12})-b[0-9]+")
_BACK_REFERENCE = re.compile(
    r"^(?:This|These|That|Those|It|Its|They|Their|Such|"
    r"The\s+(?:former|latter|above|aforementioned|same|following)|However,\s+(?:this|it|they))\b",
    re.I,
)
_CONTEXT_FORMULA = re.compile(r"<m\d+/>")
_SENTENCE_MARK = re.compile(
    r"\b(?:e\.g\.|i\.e\.|et al\.|Fig\.|Eq\.|Sec\.|No\.|vs\.|Dr\.)(?=\s|$)|(?<=\d)\.(?=\d)|"
    r"""(?P<end>[.!?]+["'”’）)」』]*(?:\s+|$)|[。！？]+["'”’）)」』]*\s*)""",
    re.I,
)


def _last_sentence(source: str) -> str:
    plain = RICH_TAG.sub("", _CONTEXT_FORMULA.sub("[公式]", source))
    text = " ".join(plain.split())
    start = 0
    for mark in _SENTENCE_MARK.finditer(text):
        if mark["end"] and mark.end() < len(text):
            start = mark.end()
    sentence = text[start:]
    return f"…{sentence[-239:]}" if len(sentence) > 240 else sentence


def _previous_context(segments: Sequence[Segment]) -> dict[str, str]:
    contexts: dict[str, str] = {}
    for previous, current in pairwise(segments):
        if (
            previous.role is not BlockType.TEXT
            or current.role is not BlockType.TEXT
            or not current.section
            or current.section != previous.section
            or not previous.block_ids
            or not current.block_ids
            or not _BACK_REFERENCE.match(RICH_TAG.sub("", current.source).lstrip())
        ):
            continue
        before = _BLOCK_PAGE.fullmatch(previous.block_ids[-1])
        after = _BLOCK_PAGE.fullmatch(current.block_ids[0])
        if before and after and 0 <= int(after[1]) - int(before[1]) <= 1:
            contexts[current.id] = _last_sentence(previous.source)
    return contexts


def cache_key(
    seg: Segment,
    model: str,
    terms: dict[str, str],
    quality: Quality,
    *,
    reference_terms: dict[str, str] | None = None,
    domain: str = "",
    section: str = "",
    provider: str = "",
    validation_version: int = VALIDATION_VERSION,
    title: str = "",
    abstract: str = "",
    role: str = "",
    prev_context: str = "",
    user_terms: dict[str, str] | None = None,
) -> str:
    payload = json.dumps(
        [
            seg.source,
            seg.formulas,
            model,
            PROMPT_VERSION,
            sorted(terms.items()),
            quality,
            sorted((reference_terms or {}).items()),
            domain,
            section,
            provider,
            validation_version,
            title,
            abstract,
            role,
            prev_context,
            sorted((user_terms or {}).items()),
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class _Unit(BaseModel):
    """去重后的翻译单元：同一原文的多个段落共享一次翻译。"""

    source: str
    formulas: dict[str, str]
    section: str
    role: BlockType | None
    prev_context: str
    seg_indexes: list[int]
    key: str
    user_terms: dict[str, str]
    reference_terms: dict[str, str]

    @property
    def expanded(self) -> str:
        return expand_formulas(self.source, self.formulas)

    def meta(self, number: int) -> SegmentPromptMeta:
        return SegmentPromptMeta(
            number, self.section, self.role, self.prev_context, self.user_terms, self.reference_terms
        )


def _is_high_formula(unit: _Unit) -> bool:
    return len(unit.formulas) >= _HIGH_FORMULA_THRESHOLD


def _request_timeout(base_timeout_s: float, thinking: str) -> float:
    """思考模式放宽单次请求上限至至少 60s；极小超时（如单测）按 2 倍放宽以不拖慢验证。"""
    if thinking == "off":
        return base_timeout_s
    if base_timeout_s >= _THINKING_TIMEOUT_SCALE_THRESHOLD_S:
        return max(base_timeout_s, _THINKING_MIN_TIMEOUT_S)
    return base_timeout_s * 2


def _chunk_high_formula(units: list[_Unit]) -> list[list[_Unit]]:
    """对高公式密度段施加独立小批上限：每批最多 4 段且累计公式数 <= 18。"""
    chunks: list[list[_Unit]] = []
    cur: list[_Unit] = []
    formulas = 0
    for unit in units:
        count = len(unit.formulas)
        if cur and (len(cur) >= _HIGH_FORMULA_MAX_SEGMENTS or formulas + count > _HIGH_FORMULA_MAX_FORMULAS):
            chunks.append(cur)
            cur, formulas = [], 0
        cur.append(unit)
        formulas += count
    if cur:
        chunks.append(cur)
    return chunks


def _split_high_formula_batches(batches: list[list[_Unit]]) -> list[list[_Unit]]:
    """将普通段与高公式密度段拆开，并对高公式批次施加段数与公式数上限。"""
    result: list[list[_Unit]] = []
    for batch in batches:
        high = [u for u in batch if _is_high_formula(u)]
        if not high:
            result.append(batch)
            continue
        plain = [u for u in batch if not _is_high_formula(u)]
        if plain:
            result.append(plain)
        result.extend(_chunk_high_formula(high))
    return result


async def translate(
    segments: Sequence[Segment],
    *,
    chat: ChatModel,
    cache: TranslationCache,
    ctx: DocContext,
    lookup_terms: TermLookup | None = None,
    lookup_user_terms: TermLookup | None = None,
    lookup_term_hits: TermHitsLookup | None = None,
    context_segments: Sequence[Segment] | None = None,
    opts: TranslateOptions | None = None,
    on_progress: Progress | None = None,
) -> tuple[list[Segment], TranslateReport]:
    if bad_source(segments):
        raise ChatError(BAD_SOURCE_MESSAGE)
    o = opts or TranslateOptions()
    loop = asyncio.get_running_loop()
    total_deadline = loop.time() + o.total_timeout_s if o.total_timeout_s is not None else None
    out = [s.model_copy() for s in segments]
    report = TranslateReport(total=len(out), attempts={s.id: 0 for s in out})

    previous_context = _previous_context(context_segments if context_segments is not None else segments)
    doc_terms = {(normalize_term_key(k), v) for k, v in ctx.terms.items()}
    legacy_doc_keys = {k.casefold() for k in ctx.terms}
    provider = str(getattr(chat, "provider_id", ""))
    units: dict[str, _Unit] = {}
    for i, seg in enumerate(out):
        if seg.status in (SegmentStatus.DONE, SegmentStatus.EDITED):
            continue
        seg.quality = o.quality
        seg.translation_attempts = 0
        seg.failure_reason = ""
        if lookup_term_hits is not None:
            mine, _, reference_hits = lookup_term_hits(seg.source)
        else:
            mine = lookup_user_terms(seg.source) if lookup_user_terms is not None else {}
            references = lookup_terms(seg.source) if lookup_terms is not None else {}
            locked = legacy_doc_keys | {k.casefold() for k in mine}
            reference_hits = {k: v for k, v in references.items() if k.casefold() not in locked}
        user_hits = {k: v for k, v in mine.items() if (normalize_term_key(k), v) not in doc_terms}
        prev_context = previous_context.get(seg.id, "")
        # 本文术语与额外用户术语的优先级不同，缓存指纹保留两套映射。
        key = cache_key(
            seg,
            chat.model,
            ctx.terms,
            o.quality,
            reference_terms=reference_hits,
            domain=ctx.domain,
            section=seg.section,
            provider=provider,
            title=ctx.title,
            abstract=ctx.abstract,
            role=seg.role.value if seg.role is not None else "",
            prev_context=prev_context,
            user_terms=user_hits,
        )
        active_terms = ctx.terms | user_hits if user_hits else ctx.terms
        if (
            o.use_cache
            and (hit := cache.get(key))
            and not _target_problems(seg.source, hit, translated_terms=active_terms, formulas=seg.formulas)
        ):
            seg.target, seg.status = normalize_cjk_spacing(hit), SegmentStatus.DONE
            report.cached += 1
            continue
        unit = units.setdefault(
            key,
            _Unit(
                source=seg.source,
                formulas=seg.formulas,
                section=seg.section,
                role=seg.role,
                prev_context=prev_context,
                seg_indexes=[],
                key=key,
                user_terms=user_hits,
                reference_terms=reference_hits,
            ),
        )
        unit.seg_indexes.append(i)

    pending = list(units.values())
    done = 0
    total_units = len(pending)
    system = system_prompt(ctx)
    chat_opts = ChatOptions(thinking="low" if o.quality == "careful" else "off", transport_retries=0)
    sem = asyncio.Semaphore(o.concurrency)
    note = f"首次翻译（第 1/{o.max_attempts} 次）"
    reasons: dict[str, str] = {}
    feedback: dict[str, str] = {}
    refused: list[_Unit] = []
    expired_deadlines: set[float] = set()

    def deadline_for(retry_deadline: float | None) -> tuple[float | None, str]:
        if total_deadline is not None and (retry_deadline is None or total_deadline <= retry_deadline):
            return total_deadline, "本次翻译总时限耗尽"
        return retry_deadline, "补救总时限耗尽"

    def fail(batch: list[_Unit], reason: str, *, diagnostic: bool = False) -> list[_Unit]:
        for u in batch:
            reasons[u.key] = reason
            if diagnostic:
                feedback[u.key] = reason
        if on_progress:
            on_progress(done, total_units, note)
        return batch

    async def run(batch: list[_Unit], opts_: ChatOptions, retry_deadline: float | None = None) -> list[_Unit]:
        """翻译一批，返回不合格、需要重试的单元。"""
        nonlocal done
        deadline, exhausted = deadline_for(retry_deadline)
        if deadline is not None and (deadline in expired_deadlines or deadline <= loop.time()):
            return fail(batch, exhausted)
        hint = feedback.get(batch[0].key, "") if len(batch) == 1 else ""
        numbered = list(enumerate(batch, start=1))
        user = user_prompt(
            [(n, u.expanded) for n, u in numbered],
            [],
            {},
            retry_feedback=hint,
            scoped_meta=[u.meta(n) for n, u in numbered],
        )
        # 排队不计为模型尝试；有整次时限时，等待并发名额也必须受限。
        try:
            if deadline is None:
                await sem.acquire()
            else:
                await asyncio.wait_for(sem.acquire(), max(0, deadline - loop.time()))
        except TimeoutError:
            if deadline is not None:
                expired_deadlines.add(deadline)
            return fail(batch, exhausted)
        try:
            call_opts = (
                opts_.model_copy(update={"thinking": "low"})
                if opts_.thinking == "off" and any(_is_high_formula(u) for u in batch)
                else opts_
            )
            req_timeout = _request_timeout(o.request_timeout_s, call_opts.thinking)
            remaining = deadline - loop.time() if deadline is not None else req_timeout
            if remaining <= 0 or deadline in expired_deadlines:
                return fail(batch, exhausted)
            for u in batch:
                for i in u.seg_indexes:
                    report.attempts[out[i].id] += 1
            report.requests += 1
            log.debug("翻译请求段数=%d，估算输入 token=%d", len(batch), system_tokens + estimate_tokens(user))
            try:
                result = await asyncio.wait_for(chat.complete(system, user, call_opts), min(req_timeout, remaining))
            except TimeoutError:
                if deadline is not None and remaining <= req_timeout:
                    # 事件循环的计时器可能略早于单调时钟触发，不再用几毫秒余量另起一轮。
                    expired_deadlines.add(deadline)
                    return fail(batch, exhausted)
                return fail(batch, "翻译请求超时")
            except ChatError as e:
                if not e.retryable:
                    raise
                return fail(batch, str(e))
        finally:
            sem.release()
        report.usage = report.usage + result.usage
        report.cost_cny = add_cost(report.cost_cny, chat.model, result.usage)
        if result.finish_reason in ("refusal", "content_filter"):
            failed_batch = fail(batch, f"模型拒绝翻译（{result.finish_reason}）")
            if len(batch) > 1:
                return failed_batch  # 批次拒绝无法归因到某一段，拆开后再确认。
            refused.extend(failed_batch)
            return []
        try:
            got = parse_numbered(result.text, batch_size=len(batch), sources=[u.source for u in batch])
        except NumberedResponseError as e:
            return fail(batch, str(e), diagnostic=True)
        truncated = result.finish_reason in ("length", "max_tokens")
        if truncated and got:
            # 最后一项可能在段内或紧贴下一项编号中断，仅信任有后续完整编号分隔的前缀。
            del got[next(reversed(got))]
        retry = []
        for n, u in enumerate(batch, start=1):
            raw_target = got.get(n, "")
            target = normalize_formula_placeholders(raw_target) if raw_target else ""
            active_terms = ctx.terms | u.user_terms if u.user_terms else ctx.terms
            errors = (
                _target_problems(u.source, target, translated_terms=active_terms, formulas=u.formulas) if target else []
            )
            if target and not errors:
                target = normalize_cjk_spacing(target)
                if o.use_cache and not result.compatibility_fallback:
                    cache.put(u.key, target)
                for i in u.seg_indexes:
                    out[i].target, out[i].status = target, SegmentStatus.DONE
                done += 1
            else:
                missing = "模型输出截断" if truncated else "返回编号缺失或译文为空"
                reason = "；".join(errors) if target else missing
                reasons[u.key] = reason
                if target or not truncated:
                    feedback[u.key] = reason
                retry.append(u)
        if on_progress:
            on_progress(done, total_units, note)
        return retry

    async def run_all(
        batches: list[list[_Unit]], opts_: ChatOptions, retry_deadline: float | None = None
    ) -> list[_Unit]:
        tasks = [asyncio.create_task(run(b, opts_, retry_deadline)) for b in batches]
        try:
            return [u for r in await asyncio.gather(*tasks) for u in r]
        except BaseException:
            # 鉴权失败或取消时一起结束同轮请求，避免孤儿任务继续消耗模型额度。
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    if on_progress:
        on_progress(done, total_units, note)
    system_tokens = estimate_tokens(system)
    sizes = [
        estimate_tokens(user_prompt([(n, u.expanded)], [], {}, scoped_meta=[u.meta(n)]))
        for n, u in enumerate(pending, start=1)
    ]
    batches = make_batches(
        pending, sizes, o.batch_tokens, sections=[u.section for u in pending], roles=[u.role for u in pending]
    )
    batches = _split_high_formula_batches(batches)
    failed = await run_all(batches, chat_opts)
    retry_deadline = loop.time() + o.retry_timeout_s
    for attempt in range(2, o.max_attempts + 1):
        if not failed:
            break
        deadline, exhausted = deadline_for(retry_deadline)
        if deadline is not None and (deadline in expired_deadlines or deadline <= loop.time()):
            fail(failed, exhausted)
            break
        note = f"还有 {len(failed)} 段待完成，正在单段重试（第 {attempt}/{o.max_attempts} 次）"
        if on_progress:
            on_progress(done, total_units, note)
        # 首次失败后即拆开，避免同批段落编号错位；按已尝试次数升序排列，优先保障未补救段落。
        failed.sort(key=lambda u: min(report.attempts[out[i].id] for i in u.seg_indexes))
        retry_opts = chat_opts if attempt == 2 else ChatOptions(thinking="low", transport_retries=0)
        failed = await run_all([[u] for u in failed], retry_opts, retry_deadline)
    failed.extend(refused)
    for u in failed:
        for i in u.seg_indexes:
            out[i].status = SegmentStatus.FAILED
            reason = reasons.get(u.key, "未取得可用译文")
            count = report.attempts[out[i].id]
            report.failures[out[i].id] = f"已尝试 {count}/{o.max_attempts} 次：{reason}"

    report.translated = sum(len(u.seg_indexes) for u in pending) - sum(len(u.seg_indexes) for u in failed)
    report.failed = sum(len(u.seg_indexes) for u in failed)
    for unit in pending:
        for i in unit.seg_indexes:
            out[i].translation_attempts = report.attempts[out[i].id]
            out[i].failure_reason = report.failures.get(out[i].id, "")
    return out, report
