"""公式占位标签守卫正则边界测试，以及 FallbackChat 容灾跳过主模型缓存的端到端测试。"""

import asyncio
import re
import time

import pytest

from pdftr.domain.ir import Segment, SegmentStatus
from pdftr.domain.llm import ChatError, ChatOptions, ChatResult, ChatUsage
from pdftr.glossary.extract import extract_terms
from pdftr.translation.cost import Price, cost_cny
from pdftr.translation.openai_compat import FallbackChat
from pdftr.translation.prompt import DocContext
from pdftr.translation.service import TranslateOptions, _protocol_tag_problems, translate


@pytest.mark.parametrize(
    ("source", "target"),
    [
        # 1-2: 合法自闭合占位符
        ("Let <m1/> be given.", "设<m1/>为给定值。"),
        ("Combine <m1/> and <m12/>.", "合并<m1/>与<m12/>。"),
        # 3-5: 数学不等式与尖括号表达不误杀
        ("If a < m1 + m2 > b holds.", "若 a < m1 + m2 > b 成立。"),
        ("When x < m1 and m2 > y.", "当 x < m1 且 m2 > y 时。"),
        ("The pair <m1, m2> denotes a tuple.", "序偶 <m1, m2> 表示一个二元组。"),
        # 6-7: 富文本标签与非公式 m 开头标签不误杀
        (
            "An <b>important</b> <i>term</i> with x<sup>2</sup><sub>1</sub>.",
            "一个<b>重要</b>的<i>术语</i>，含x<sup>2</sup><sub>1</sub>。",
        ),
        ("Use <math>x</math> and <meta> tags.", "使用 <math>x</math> 和 <meta> 标签。"),
        # 8-9: 原文已包含同类片段时短路放行
        ("Literal <m1> tag in source.", "原文自带 <m1> 标签。"),
        ("Literal </m2> tag in source.", "原文自带 </m2> 标签。"),
        # 10-19: 标签名边界（含 :、. 与字母数字后缀）与自然语言/数学逻辑比较绝不误杀
        ("Use <m1custom/> element.", "使用<m1custom/>元素。"),
        ("Close </m1custom> element.", "关闭</m1custom>元素。"),
        ("Use <m1_foo/> and <m1-bar>.", "使用<m1_foo/>与<m1-bar>。"),
        ("Use <m1:custom/> element.", "使用<m1:custom/>元素。"),
        ("Use <m1.custom/> element.", "使用<m1.custom/>元素。"),
        ("Use <m10x/> element.", "使用<m10x/>元素。"),
        ("Check if a<m1 and b>0 holds.", "若 a<m1 and b>0 成立。"),
        ("Check if a<m1 and b>0 holds.", "若 a<m1 且 b>0 成立。"),
        ("Check if a < m1 and b > 0 holds.", "若 a < m1 and b > 0 成立。"),
        ("Check if a < m1 or b > 0 holds.", "若 a < m1 or b > 0 成立。"),
    ],
)
def test_formula_tag_guard_accepts_valid_and_non_malformed_cases(source: str, target: str) -> None:
    assert _protocol_tag_problems(source, target) == []


@pytest.mark.parametrize(
    ("source", "target"),
    [
        # 10-14: 各类闭标签
        ("See <m1/>.", "参见</m1>。"),
        ("See <m1/>.", "参见</ m1>。"),
        ("See <m1/>.", "参见</m1 >。"),
        ("See <m1/>.", "参见</m1 extra>。"),
        ("See <m3/>.", "参见</m3/>。"),
        # 15-19: 未闭合开标签与成对标签
        ("See <m1/>.", "参见<m1>。"),
        ("See <m1/>.", "参见< m1>。"),
        ("See <m1/>.", "参见<m1 >。"),
        ("See <m1/>.", "a<m1>"),
        ("See <m1/>.", "参见<m1>x</m1>。"),
        # 20-26: 自闭合空格变形、大小写、双斜杠与超长自闭合
        ("See <m1/>.", "参见< m1/>。"),
        ("See <m1/>.", "参见<m1 />。"),
        ("See <m1/>.", "参见<m1/ >。"),
        ("See <m1/>.", "参见<M1/>。"),
        ("See <m1/>.", "参见<m1//>。"),
        ("See <m1/>.", "参见<m1 attr/>。"),
        ("See <m1/>.", f'参见<m1 a="{"x" * 65}"/>。'),
        # 27-35: 带键值对属性与 HTML 布尔属性的开标签/自闭合标签
        ("See <m1/>.", '参见<m1 id="1"/>。'),
        ("See <m1/>.", "参见<m1 id='1'/>。"),
        ("See <m1/>.", "参见<m1 id=1/>。"),
        ("See <m1/>.", '参见<m1 id="1">。'),
        ("See <m1/>.", '参见<m1 data-id="1" role=formula>。'),
        ("See <m1/>.", "参见<m1 disabled>。"),
        ("See <m1/>.", "测试 <m1 disabled>"),
        ("See <m1/>.", "A<m1 disabled>B"),
        ("See <m1/>.", "参见<m1 disabled readonly>。"),
        ("See <m1/>.", '参见<m1 id="1" disabled>。'),
    ],
)
def test_formula_tag_guard_rejects_malformed_formula_tags(source: str, target: str) -> None:
    problems = _protocol_tag_problems(source, target)
    assert problems and any("公式标签格式错误" in p for p in problems)


@pytest.mark.parametrize("tag", ["</translations", "<translations", "</TRANSLATIONS", "<translations>"])
def test_protocol_tag_guard_rejects_complete_and_truncated_tags(tag: str) -> None:
    problems = _protocol_tag_problems("A plain sentence.", f"一句译文。{tag}")
    assert len(problems) == 1 and "协议标签" in problems[0]
    assert _protocol_tag_problems(f"The literal fragment is {tag}", f"字面片段为 {tag}") == []


@pytest.mark.parametrize(
    "tag",
    [
        "<mark/>",
        "<sampling pitch/>",
        "<aperture width/>",
        "<foo_bar />",
        "</term>",
        "</sampling pitch>",
        "<widget>",
        "<x-node>内容</x-node>",
        '<node title="a > b">',
        "<node hidden data-kind=label>",
        "<b/>",
    ],
)
def test_unexpected_tag_guard_requires_exact_source_literal(tag: str) -> None:
    problems = _protocol_tag_problems("A plain sentence.", f"一句{tag}译文。")
    assert len(problems) == 1 and "伪造标签" in problems[0] and "移除" in problems[0]
    assert tag.split(">")[0] in problems[0]
    assert _protocol_tag_problems(f"Literal markup: {tag}", f"字面标记为{tag}。") == []


@pytest.mark.parametrize(
    "expression",
    ["a < b > c", "x < y and z > 0", "P(X < k)", "<m1, m2>", "a<b>c", "Vec<Item>", "<https://example.org/>"],
)
def test_unexpected_tag_guard_preserves_angle_bracket_notation(expression: str) -> None:
    assert _protocol_tag_problems("Evaluate the expression.", f"计算 {expression}。") == []


@pytest.mark.parametrize(
    ("target", "valid"),
    [
        ("<m1 " + "attr " * 2000 + "unclosed", True),
        ("<widget " + "attr " * 20000 + "unclosed", True),
        ("<widget " + 'attr="value" ' * 20000 + "unclosed", True),
        ("<m1 " + "attr " * 2000 + "unclosed!>", True),
        ("<widget " + " " * 20000 + "hidden>", True),
        ("<widget " + "attr " * 2000 + "unclosed" + "><mark/>", False),
    ],
)
def test_formula_tag_guard_no_catastrophic_backtracking(target: str, valid: bool) -> None:
    started = time.perf_counter()
    assert (not _protocol_tag_problems("plain", target)) is valid
    assert (time.perf_counter() - started) < 0.5


class _DictCache:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def put(self, key: str, target: str) -> None:
        self.data[key] = target

    def purge_older_than(self, hours: int) -> int:
        return 0


class _StubChat:
    def __init__(
        self,
        model: str,
        *,
        provider_id: str = "",
        error: ChatError | None = None,
        fail_after: int | None = None,
        break_first_once: bool = False,
    ) -> None:
        self.model = model
        self.provider_id = provider_id or f"openai:nemotron:{model}"
        self.error = error
        self.fail_after = fail_after
        self.break_first_once = break_first_once
        self.calls = 0

    async def list_models(self) -> list[str]:
        return [self.model]

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        del system, opts
        self.calls += 1
        if self.error is not None and (self.fail_after is None or self.calls > self.fail_after):
            raise self.error
        lines: list[str] = []
        for line in user.splitlines():
            m = re.match(r"^(\d+)\|(.*)$", line)
            if not m:
                continue
            text = re.sub(r"<(m\d+)>.*?</\1>", r"<\1/>", m.group(2))
            if self.break_first_once:
                self.break_first_once = False
                text = re.sub(r"<m\d+/>", "", text)
            lines.append(f"{m.group(1)}|译:{text}")
        return ChatResult(
            text="\n".join(lines),
            usage=ChatUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000),
        )


def _seg(idx: int, source: str, formulas: dict[str, str] | None = None) -> Segment:
    return Segment(id=f"job-s{idx}", job_id="job", block_ids=[f"p0-b{idx}"], source=source, formulas=formulas or {})


@pytest.mark.parametrize(
    ("suffix", "hint"),
    [("</translations", "协议标签"), ("<translations", "协议标签"), ("<sampling pitch/>", "<sampling pitch/>")],
)
def test_translate_retries_only_the_segment_with_a_leaked_tag(suffix: str, hint: str) -> None:
    class _LeakingChat(_StubChat):
        users: list[str]

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            del system, opts
            self.users.append(user)
            if len(self.users) == 1:
                return ChatResult(text=f"1|记录<m1/>处的读数。{suffix}\n2|保持仪表水平。\n</translations>")
            return ChatResult(text="1|记录<m1/>处的读数。\n</translations>")

    chat = _LeakingChat("synthetic-model")
    chat.users = []
    cache = _DictCache()
    segments = [_seg(0, "Record the reading at <m1/>.", {"m1": "x"}), _seg(1, "Keep the gauge level.")]
    out, report = asyncio.run(
        translate(
            segments, chat=chat, cache=cache, ctx=DocContext(title="Paper"), opts=TranslateOptions(use_cache=True)
        )
    )
    assert [s.status for s in out] == [SegmentStatus.DONE, SegmentStatus.DONE]
    assert [s.target for s in out] == ["记录<m1/>处的读数。", "保持仪表水平。"]
    assert report.requests == 2 and report.attempts == {"job-s0": 2, "job-s1": 1}
    assert "2|Keep the gauge level." in chat.users[0]
    assert "Keep the gauge level." not in chat.users[1]
    assert "上次翻译未通过校验" in chat.users[1] and hint in chat.users[1]
    assert sorted(cache.data.values()) == ["保持仪表水平。", "记录<m1/>处的读数。"]


def test_translate_fallback_skips_primary_cache_and_records_actual_model_cost() -> None:
    primary = _StubChat(
        "nvidia/nemotron-3-ultra-550b",
        error=ChatError("请求受限", retryable=True, status_code=429, error_code="rate_limit_exceeded"),
    )
    backup = _StubChat("nvidia/nemotron-3-super-120b")
    chat = FallbackChat(primary, [backup])
    cache = _DictCache()
    segments = [
        _seg(0, "Let <m1/> be a matrix.", {"m1": "A"}),
        _seg(1, "We evaluate the method."),
    ]
    prices = {
        "nvidia/nemotron-3-super-120b": Price(cache_hit=0.0, cache_miss=0.5, output=1.5, peak_factor=1.0),
    }

    out, report = asyncio.run(
        translate(
            segments,
            chat=chat,
            cache=cache,
            ctx=DocContext(title="Paper"),
            opts=TranslateOptions(use_cache=True, price_overrides=prices),
        )
    )

    assert [s.status for s in out] == [SegmentStatus.DONE, SegmentStatus.DONE]
    assert report.used_fallback is True
    assert report.fallback_models == ["nvidia/nemotron-3-super-120b"]
    # 备选通道出字绝不污染主模型缓存
    assert cache.data == {}
    # 按 actual_model（备选模型）热配置单价计费：0.5 + 1.5 = 2.0
    assert report.cost_cny == pytest.approx(2.0)


def test_translate_retry_fallback_skips_cache_only_for_fallback_segment() -> None:
    # 首批由主模型完成（第 0 段丢公式触发单段补救，第 1 段正常入缓存）；单段补救时主模型 429 切备选
    primary = _StubChat(
        "nvidia/nemotron-3-ultra-550b",
        error=ChatError("请求受限", retryable=True, status_code=429),
        fail_after=1,
        break_first_once=True,
    )
    backup = _StubChat("nvidia/nemotron-3-super-120b")
    chat = FallbackChat(primary, [backup])
    cache = _DictCache()
    segments = [
        _seg(0, "Let <m1/> be a scalar.", {"m1": "x"}),
        _seg(1, "Another plain sentence."),
    ]

    out, report = asyncio.run(
        translate(
            segments,
            chat=chat,
            cache=cache,
            ctx=DocContext(title="Paper"),
            opts=TranslateOptions(use_cache=True),
        )
    )

    assert [s.status for s in out] == [SegmentStatus.DONE, SegmentStatus.DONE]
    assert report.used_fallback is True
    assert report.fallback_models == ["nvidia/nemotron-3-super-120b"]
    # 只有主模型成功的那 1 段写入缓存，备选补救的那 1 段跳过写缓存
    assert len(cache.data) == 1
    assert list(cache.data.values()) == ["译:Another plain sentence."]


def test_slow_primary_switches_to_fallback_within_budget_and_cancellation_releases() -> None:
    class _SlowChat:
        def __init__(self, model: str, delay_s: float) -> None:
            self.model = model
            self.provider_id = f"openai:generic:{model}"
            self.delay_s = delay_s
            self.cancelled = False
            self.calls = 0

        async def list_models(self) -> list[str]:
            return [self.model]

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            del system, opts
            self.calls += 1
            try:
                await asyncio.sleep(self.delay_s)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            lines = [
                f"{m.group(1)}|译:{m.group(2)}" for line in user.splitlines() if (m := re.match(r"^(\d+)\|(.*)$", line))
            ]
            return ChatResult(text="\n".join(lines), usage=ChatUsage(prompt_tokens=10, completion_tokens=10))

    async def run() -> None:
        # 1. 慢主模型在切分超时后自动切换快备选模型
        slow_primary = _SlowChat("slow-primary", delay_s=5.0)
        fast_backup = _SlowChat("fast-backup", delay_s=0.01)
        chat = FallbackChat(slow_primary, [fast_backup], primary_timeout_s=0.05)
        out, report = await translate(
            [_seg(0, "A short sentence.")],
            chat=chat,
            cache=_DictCache(),
            ctx=DocContext(title="Paper"),
            opts=TranslateOptions(use_cache=False, request_timeout_s=0.5, max_attempts=1),
        )
        assert out[0].status is SegmentStatus.DONE
        assert report.used_fallback is True
        assert report.fallback_models == ["fast-backup"]
        assert slow_primary.cancelled is True
        assert fast_backup.calls == 1

        # 2. 等待主模型期间外部取消任务：立刻传播 CancelledError 且绝不误切备选
        slow2 = _SlowChat("slow-primary-2", delay_s=5.0)
        backup2 = _SlowChat("fast-backup-2", delay_s=0.01)
        chat2 = FallbackChat(slow2, [backup2], primary_timeout_s=1.0)
        task = asyncio.create_task(chat2.complete("sys", "1|hi", ChatOptions()))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert slow2.cancelled is True
        assert backup2.calls == 0

    asyncio.run(run())


def test_empty_price_override_does_not_fallback_to_builtin_prices_and_extract_terms_propagates_fallback() -> None:
    usage = ChatUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert cost_cny("deepseek-flash", usage, peak=False, prices=None) is not None
    assert cost_cny("deepseek-flash", usage, peak=False, prices={}) is None

    primary = _StubChat(
        "primary-model",
        error=ChatError("服务繁忙", retryable=True, status_code=503),
    )

    class _GlossaryBackup:
        model = "backup-glossary-model"

        async def list_models(self) -> list[str]:
            return [self.model]

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            del system, user, opts
            return ChatResult(
                text="领域：测试\nAI公共词库：停用\nmatrix=矩阵",
                usage=ChatUsage(prompt_tokens=100, completion_tokens=50),
            )

    found = asyncio.run(
        extract_terms(
            FallbackChat(primary, [_GlossaryBackup()]),
            title="Test",
            abstract="Abstract",
            intro="Intro",
            headings=["Methods"],
        )
    )
    assert found.used_fallback is True
    assert found.actual_model == "backup-glossary-model"
    assert found.terms == {"matrix": "矩阵"}


def test_single_free_provider_timeout_not_truncated_and_fallback_chain_bounded_in_translate_and_glossary() -> None:
    class _TimedChat:
        def __init__(self, model: str, delay_s: float, *, call_timeout_s: float | None = None) -> None:
            self.model = model
            self.provider_id = f"openai:generic:{model}"
            self.delay_s = delay_s
            self.call_timeout_s = call_timeout_s
            self.calls = 0

        async def list_models(self) -> list[str]:
            return [self.model]

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            del system, opts
            self.calls += 1
            await asyncio.sleep(self.delay_s)
            if "抽取需要全文统一译法的专业术语" in user:
                return ChatResult(text="领域：测试\nAI公共词库：停用\nmatrix=矩阵")
            lines = [
                f"{m.group(1)}|译:{m.group(2)}" for line in user.splitlines() if (m := re.match(r"^(\d+)\|(.*)$", line))
            ]
            return ChatResult(text="\n".join(lines))

    async def scenario() -> None:
        # 1. 单上游免费渠道配置较长 call_timeout_s（按比例模拟 65s > 默认 30s）：不被默认短 request_timeout_s 截断
        single = _TimedChat("single-free", delay_s=0.12, call_timeout_s=0.5)
        out, report = await translate(
            [_seg(0, "Single provider free sentence.")],
            chat=single,
            cache=_DictCache(),
            ctx=DocContext(title="Paper"),
            opts=TranslateOptions(use_cache=False, request_timeout_s=0.05, max_attempts=1),
        )
        assert out[0].status is SegmentStatus.DONE
        assert report.failed == 0
        assert single.calls == 1

        # 2. 多备选连续慢响应：正文翻译受整链总截止时间约束，超时后立即停止继续切换后续备选
        p_slow = _TimedChat("p-slow", delay_s=1.0, call_timeout_s=0.06)
        b1_slow = _TimedChat("b1-slow", delay_s=1.0, call_timeout_s=0.06)
        b2_never = _TimedChat("b2-never", delay_s=1.0, call_timeout_s=0.06)
        chain_translate = FallbackChat(p_slow, [b1_slow, b2_never], primary_timeout_s=0.05, total_timeout_s=0.11)
        t0 = time.monotonic()
        out2, report2 = await translate(
            [_seg(0, "Slow chain sentence.")],
            chat=chain_translate,
            cache=_DictCache(),
            ctx=DocContext(title="Paper"),
            opts=TranslateOptions(use_cache=False, request_timeout_s=0.05, max_attempts=1),
        )
        elapsed_translate = time.monotonic() - t0
        assert out2[0].status is SegmentStatus.FAILED
        assert report2.failed == 1
        assert elapsed_translate < 0.35
        assert b2_never.calls == 0

        # 3. 术语抽取同样受整链总截止时间约束，不会逐个备选无限累加等待
        gp_slow = _TimedChat("gp-slow", delay_s=1.0, call_timeout_s=0.06)
        gb1_slow = _TimedChat("gb1-slow", delay_s=1.0, call_timeout_s=0.06)
        gb2_never = _TimedChat("gb2-never", delay_s=1.0, call_timeout_s=0.06)
        chain_glossary = FallbackChat(gp_slow, [gb1_slow, gb2_never], primary_timeout_s=0.05, total_timeout_s=0.11)
        t1 = time.monotonic()
        with pytest.raises(ChatError):
            await extract_terms(
                chain_glossary,
                title="Test",
                abstract="Abstract",
                intro="Intro",
                headings=["Methods"],
            )
        elapsed_glossary = time.monotonic() - t1
        assert elapsed_glossary < 0.35
        assert gb2_never.calls == 0

    asyncio.run(scenario())
