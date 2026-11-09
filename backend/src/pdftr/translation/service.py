"""翻译用例（docs/03-mvp-design.md §3.5）。

查缓存 → 相同原文去重 → 按 token 预算分批 → 并发调用模型 → 校验编号与占位符
→ 不合格的段落单独重试 → 写缓存、汇总用量与费用。
鉴权失败、余额不足直接抛出；连接类临时错误纳入有次数和时限的单段重试。
"""

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from pdftr.domain.ir import Segment, SegmentStatus
from pdftr.domain.llm import ChatError, ChatModel, ChatOptions, ChatUsage
from pdftr.translation.batching import (
    BATCH_TOKENS,
    bold_problems,
    estimate_tokens,
    make_batches,
    parse_numbered,
    placeholder_problems,
    placeholders_match,
)
from pdftr.translation.cost import add_cost
from pdftr.translation.port import TranslationCache
from pdftr.translation.prompt import PROMPT_VERSION, DocContext, expand_formulas, system_prompt, user_prompt

Quality = Literal["default", "careful"]
TermLookup = Callable[[str], dict[str, str]]
Progress = Callable[[int, int, str], None]  # （已完成, 总数, 补充说明；平常为空）


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


def cache_key(seg: Segment, model: str, terms: dict[str, str], quality: Quality) -> str:
    payload = json.dumps(
        [seg.source, seg.formulas, model, PROMPT_VERSION, sorted(terms.items()), quality],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class _Unit(BaseModel):
    """去重后的翻译单元：同一原文的多个段落共享一次翻译。"""

    source: str
    formulas: dict[str, str]
    sections: list[str]
    seg_indexes: list[int]
    key: str

    @property
    def expanded(self) -> str:
        return expand_formulas(self.source, self.formulas)


async def translate(
    segments: Sequence[Segment],
    *,
    chat: ChatModel,
    cache: TranslationCache,
    ctx: DocContext,
    lookup_terms: TermLookup | None = None,
    opts: TranslateOptions | None = None,
    on_progress: Progress | None = None,
) -> tuple[list[Segment], TranslateReport]:
    o = opts or TranslateOptions()
    loop = asyncio.get_running_loop()
    total_deadline = loop.time() + o.total_timeout_s if o.total_timeout_s is not None else None
    out = [s.model_copy() for s in segments]
    report = TranslateReport(total=len(out), attempts={s.id: 0 for s in out})

    units: dict[str, _Unit] = {}
    for i, seg in enumerate(out):
        if seg.status in (SegmentStatus.DONE, SegmentStatus.EDITED):
            continue
        seg.quality = o.quality
        seg.translation_attempts = 0
        seg.failure_reason = ""
        # 本段命中的词库与用户术语也算进缓存键：用户改了术语后不会拿到旧译文
        hits = lookup_terms(seg.source) if lookup_terms else {}
        key = cache_key(seg, chat.model, ctx.terms | hits, o.quality)
        if (
            o.use_cache
            and (hit := cache.get(key))
            and placeholders_match(seg.source, hit)
            and not bold_problems(seg.source, hit)
        ):
            seg.target, seg.status = hit, SegmentStatus.DONE
            report.cached += 1
            continue
        unit = units.setdefault(
            key, _Unit(source=seg.source, formulas=seg.formulas, sections=[], seg_indexes=[], key=key)
        )
        unit.seg_indexes.append(i)
        if seg.section and seg.section not in unit.sections:
            unit.sections.append(seg.section)

    pending = list(units.values())
    done = 0
    total_units = len(pending)
    system = system_prompt(ctx)
    chat_opts = ChatOptions(thinking="low" if o.quality == "careful" else "off", transport_retries=0)
    sem = asyncio.Semaphore(o.concurrency)
    note = f"首次翻译（第 1/{o.max_attempts} 次）"
    reasons: dict[str, str] = {}
    expired_deadlines: set[float] = set()

    def deadline_for(retry_deadline: float | None) -> tuple[float | None, str]:
        if total_deadline is not None and (retry_deadline is None or total_deadline <= retry_deadline):
            return total_deadline, "本次翻译总时限耗尽"
        return retry_deadline, "补救总时限耗尽"

    def fail(batch: list[_Unit], reason: str) -> list[_Unit]:
        for u in batch:
            reasons[u.key] = reason
        if on_progress:
            on_progress(done, total_units, note)
        return batch

    async def run(batch: list[_Unit], opts_: ChatOptions, retry_deadline: float | None = None) -> list[_Unit]:
        """翻译一批，返回不合格、需要重试的单元。"""
        nonlocal done
        deadline, exhausted = deadline_for(retry_deadline)
        if deadline is not None and (deadline in expired_deadlines or deadline <= loop.time()):
            return fail(batch, exhausted)
        sections = list(dict.fromkeys(s for u in batch for s in u.sections))
        extra: dict[str, str] = {}
        if lookup_terms:
            for u in batch:
                extra |= {k: v for k, v in lookup_terms(u.source).items() if k not in ctx.terms}
        user = user_prompt([(n + 1, u.expanded) for n, u in enumerate(batch)], sections, extra)
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
            remaining = deadline - loop.time() if deadline is not None else o.request_timeout_s
            if remaining <= 0 or deadline in expired_deadlines:
                return fail(batch, exhausted)
            for u in batch:
                for i in u.seg_indexes:
                    report.attempts[out[i].id] += 1
            report.requests += 1
            try:
                result = await asyncio.wait_for(chat.complete(system, user, opts_), min(o.request_timeout_s, remaining))
            except TimeoutError:
                if deadline is not None and remaining <= o.request_timeout_s:
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
        got = parse_numbered(result.text)
        retry = []
        for n, u in enumerate(batch, start=1):
            target = got.get(n, "")
            formula_errors = placeholder_problems(u.source, target)
            bold_errors = bold_problems(u.source, target)
            if target and not formula_errors and not bold_errors:
                cache.put(u.key, target)
                for i in u.seg_indexes:
                    out[i].target, out[i].status = target, SegmentStatus.DONE
                done += 1
            else:
                errors = []
                if formula_errors:
                    errors.append("公式占位符不一致：" + "；".join(formula_errors))
                if bold_errors:
                    errors.append("粗体标记不一致：" + "；".join(bold_errors))
                reasons[u.key] = "；".join(errors) if target else "返回编号缺失或译文为空"
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
    batches = make_batches(pending, [estimate_tokens(u.expanded) for u in pending], o.batch_tokens)
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
        # 首次失败后即拆开，避免同批段落编号错位；第三次起使用已有的低强度思考策略。
        retry_opts = chat_opts if attempt == 2 else ChatOptions(thinking="low", transport_retries=0)
        failed = await run_all([[u] for u in failed], retry_opts, retry_deadline)
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
