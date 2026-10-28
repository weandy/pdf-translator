"""翻译用例（docs/03-mvp-design.md §3.5）。

查缓存 → 相同原文去重 → 按 token 预算分批 → 并发调用模型 → 校验编号与占位符
→ 不合格的段落单独重试 → 写缓存、汇总用量与费用。
鉴权失败、余额不足等 ChatError 直接抛出，由流水线标记任务失败；单段格式问题只让该段失败。
"""

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from pdftr.domain.ir import Segment, SegmentStatus
from pdftr.domain.llm import ChatModel, ChatOptions, ChatUsage
from pdftr.translation.batching import BATCH_TOKENS, estimate_tokens, make_batches, parse_numbered, placeholders_match
from pdftr.translation.cost import cost_cny, is_deepseek_peak
from pdftr.translation.port import TranslationCache
from pdftr.translation.prompt import PROMPT_VERSION, DocContext, expand_formulas, system_prompt, user_prompt

Quality = Literal["default", "careful"]
TermLookup = Callable[[str], dict[str, str]]
Progress = Callable[[int, int], None]


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
    concurrency: int = 4
    batch_tokens: int = BATCH_TOKENS
    max_retries: int = 2
    use_cache: bool = True  # 单段“重翻”时为 False：用户要的是另一个译文


class TranslateReport(BaseModel):
    total: int = 0
    translated: int = 0
    cached: int = 0
    failed: int = 0
    requests: int = 0
    usage: ChatUsage = Field(default_factory=ChatUsage)
    cost_cny: float | None = None


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
    out = [s.model_copy() for s in segments]
    report = TranslateReport(total=len(out))

    units: dict[str, _Unit] = {}
    for i, seg in enumerate(out):
        if seg.status in (SegmentStatus.DONE, SegmentStatus.EDITED):
            continue
        seg.quality = o.quality
        key = cache_key(seg, chat.model, ctx.terms, o.quality)
        if o.use_cache and (hit := cache.get(key)) is not None:
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
    chat_opts = ChatOptions(thinking="low" if o.quality == "careful" else "off")
    sem = asyncio.Semaphore(o.concurrency)

    async def run(batch: list[_Unit], opts_: ChatOptions) -> list[_Unit]:
        """翻译一批，返回不合格、需要重试的单元。"""
        nonlocal done
        sections = list(dict.fromkeys(s for u in batch for s in u.sections))
        extra: dict[str, str] = {}
        if lookup_terms:
            for u in batch:
                extra |= {k: v for k, v in lookup_terms(u.source).items() if k not in ctx.terms}
        user = user_prompt([(n + 1, u.expanded) for n, u in enumerate(batch)], sections, extra)
        async with sem:
            result = await chat.complete(system, user, opts_)
        report.requests += 1
        report.usage = report.usage + result.usage
        got = parse_numbered(result.text)
        retry = []
        for n, u in enumerate(batch, start=1):
            target = got.get(n, "")
            if target and placeholders_match(u.source, target):
                cache.put(u.key, target)
                for i in u.seg_indexes:
                    out[i].target, out[i].status = target, SegmentStatus.DONE
                done += 1
            else:
                retry.append(u)
        if on_progress:
            on_progress(done, total_units)
        return retry

    async def run_all(units: list[_Unit], opts_: ChatOptions) -> list[_Unit]:
        batches = make_batches(units, [estimate_tokens(u.expanded) for u in units], o.batch_tokens)
        return [u for r in await asyncio.gather(*(run(b, opts_) for b in batches)) for u in r]

    failed = await run_all(pending, chat_opts)
    # 重试策略（M3 在 Adam 上实测）：
    # - 前几轮成批重试：每个请求都带约 1000 token 的固定提示，逐段重试太贵；
    # - 最后一轮逐段发送并开低强度思考：剩下的多是公式密集段，几段挤在一批里容易编号错位，
    #   单独发送加思考就能通过（S8 中思考也能修正公式对应关系错位）；此时只剩少数段落，额外请求不多。
    for attempt in range(o.max_retries):
        if not failed:
            break
        if attempt < o.max_retries - 1:
            failed = await run_all(failed, chat_opts)
        else:
            thinking = ChatOptions(thinking="low")
            failed = [u for r in await asyncio.gather(*(run([u], thinking) for u in failed)) for u in r]
    for u in failed:
        for i in u.seg_indexes:
            out[i].status = SegmentStatus.FAILED

    report.translated = sum(len(u.seg_indexes) for u in pending) - sum(len(u.seg_indexes) for u in failed)
    report.failed = sum(len(u.seg_indexes) for u in failed)
    report.cost_cny = cost_cny(chat.model, report.usage, peak=is_deepseek_peak())
    return out, report
