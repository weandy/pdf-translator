import asyncio
import re

import pytest

from pdftr.domain.ir import Segment, SegmentStatus
from pdftr.domain.llm import ChatError, ChatOptions, ChatResult, ChatUsage
from pdftr.translation.prompt import DocContext
from pdftr.translation.service import TranslateOptions, translate


class FakeChat:
    """把每行原文变成“译:原文”，公式写回 <mN/>。可指定某些原文第一次故意丢占位符。"""

    model = "deepseek-flash"

    def __init__(self, break_once: set[str] | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, ChatOptions]] = []
        self.break_once = set(break_once or ())
        self.error = error

    async def list_models(self) -> list[str]:
        return [self.model]

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        if self.error:
            raise self.error
        self.calls.append((user, opts))
        out = []
        for line in user.splitlines():
            m = re.match(r"^(\d+)\|(.*)$", line)
            if not m:
                continue
            text = re.sub(r"<(m\d+)>.*?</\1>", r"<\1/>", m.group(2))
            if text in self.break_once:
                self.break_once.discard(text)
                text = re.sub(r"<m\d+/>", "", text)
            out.append(f"{m.group(1)}|译:{text}")
        return ChatResult(text="\n".join(out), usage=ChatUsage(prompt_tokens=100, completion_tokens=50))


class DictCache:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def put(self, key: str, target: str) -> None:
        self.data[key] = target

    def purge_older_than(self, hours: int) -> int:
        return 0


def _seg(i: int, source: str, formulas: dict[str, str] | None = None, section: str = "") -> Segment:
    return Segment(id=f"j-s{i}", job_id="j", block_ids=[f"p0-b{i}"], source=source, formulas=formulas or {},
                   section=section)  # fmt: skip


SEGS = [
    _seg(0, "Let <m1/> be a function.", {"m1": "f(x)"}, "2 Method"),
    _seg(1, "A repeated sentence here."),
    _seg(2, "A repeated sentence here."),
    _seg(3, "Some other text.", section="3 Results"),
]


def test_translate_dedup_retry_and_cache():
    chat, cache = FakeChat(break_once={"Let <m1/> be a function."}), DictCache()
    out, report = asyncio.run(translate(SEGS, chat=chat, cache=cache, ctx=DocContext(title="T")))

    assert [s.status for s in out] == [SegmentStatus.DONE] * 4
    assert out[0].target == "译:Let <m1/> be a function."  # 第一次丢了占位符，重试后通过
    assert out[1].target == out[2].target  # 相同原文只翻一次
    assert report.translated == 4 and report.failed == 0 and report.cached == 0
    assert report.requests == 2  # 一批 + 一次单段重试
    first_user = chat.calls[0][0]
    assert "<m1>f(x)</m1>" in first_user  # 模型能看到公式内容
    assert "本批段落所在章节：2 Method；3 Results" in first_user
    assert first_user.count("A repeated sentence here.") == 1
    assert report.cost_cny is not None and report.usage.prompt_tokens == 200

    # 第二次：全部命中缓存，不再调用模型
    chat2 = FakeChat()
    _, report2 = asyncio.run(translate(SEGS, chat=chat2, cache=cache, ctx=DocContext(title="T")))
    assert report2.cached == 4 and chat2.calls == []


def test_missing_bold_is_retried_and_bad_cached_format_is_not_reused():
    emphasized = _seg(0, "An <b>important</b> detail.")
    cache = DictCache()
    good_chat = FakeChat()
    asyncio.run(translate([emphasized], chat=good_chat, cache=cache, ctx=DocContext()))
    assert len(cache.data) == 1
    for key in cache.data:
        cache.data[key] = "一个重要细节。"

    class DropOnce(FakeChat):
        async def complete(self, system, user, opts):
            result = await super().complete(system, user, opts)
            if len(self.calls) == 1:
                return ChatResult(text=result.text.replace("<b>", "").replace("</b>", ""), usage=result.usage)
            return result

    chat = DropOnce()
    (out,), report = asyncio.run(translate([emphasized], chat=chat, cache=cache, ctx=DocContext()))
    assert out.status is SegmentStatus.DONE and "<b>important</b>" in (out.target or "")
    assert report.cached == 0 and report.requests == 2 and out.translation_attempts == 2
    assert not out.failure_reason
    assert report.usage.prompt_tokens == 200


def test_persistent_bold_loss_reaches_the_existing_bound_with_explicit_reason():
    class DropBold(FakeChat):
        async def complete(self, system, user, opts):
            result = await super().complete(system, user, opts)
            return ChatResult(text=result.text.replace("<b>", "").replace("</b>", ""), usage=result.usage)

    chat = DropBold()
    (out,), report = asyncio.run(
        translate([_seg(0, "An <b>important</b> detail.")], chat=chat, cache=DictCache(), ctx=DocContext())
    )
    assert out.status is SegmentStatus.FAILED and out.translation_attempts == 10
    assert report.requests == 10 and report.failed == 1 and "粗体标记丢失" in out.failure_reason
    assert report.cost_cny is not None and report.usage.prompt_tokens == 1000


def test_persistent_failure_and_edited_untouched():
    chat = FakeChat(break_once={"Some other text."})
    chat.break_once = set()  # 下面用一个总是丢占位符的段落
    bad = _seg(9, "Always <m1/> broken.", {"m1": "z"})

    class AlwaysBreak(FakeChat):
        async def complete(self, system, user, opts):
            r = await super().complete(system, user, opts)
            return ChatResult(text=re.sub(r"<m1/>", "", r.text), usage=r.usage)

    edited = _seg(5, "User edited.").model_copy(update={"target": "用户的译文", "status": SegmentStatus.EDITED})
    out, report = asyncio.run(
        translate([bad, edited], chat=AlwaysBreak(), cache=DictCache(), ctx=DocContext(),
                  opts=TranslateOptions(max_attempts=3))
    )  # fmt: skip
    assert out[0].status is SegmentStatus.FAILED and out[0].target is None
    assert out[1].target == "用户的译文" and out[1].status is SegmentStatus.EDITED
    assert report.failed == 1 and report.requests == 3  # 一次批量 + 两次重试


def test_retries_are_individual_and_third_attempt_thinks():
    class BreakTwice(FakeChat):
        """前两轮都丢占位符，第三轮（开了思考）才答对。"""

        async def complete(self, system, user, opts):
            r = await super().complete(system, user, opts)
            if opts.thinking == "off":
                return ChatResult(text=re.sub(r"<m\d+/>", "", r.text), usage=r.usage)
            return r

    segs = [_seg(i, f"Sentence {i} with <m1/> here.", {"m1": "x"}) for i in range(5)]
    chat = BreakTwice()
    out, report = asyncio.run(translate(segs, chat=chat, cache=DictCache(), ctx=DocContext()))
    assert all(s.status is SegmentStatus.DONE for s in out)
    # 首轮 1 批；失败后每段单独重试；第三次起开启低强度思考。
    assert report.requests == 11
    assert [o.thinking for _, o in chat.calls] == ["off"] * 6 + ["low"] * 5
    assert all(o.transport_retries == 0 for _, o in chat.calls)


def test_careful_quality_turns_on_thinking_and_terms_are_looked_up():
    chat = FakeChat()
    out, _ = asyncio.run(
        translate(SEGS[:1], chat=chat, cache=DictCache(), ctx=DocContext(terms={"function": "函数"}),
                  lookup_terms=lambda s: {"function": "函数", "Let": "设"},
                  opts=TranslateOptions(quality="careful"))
    )  # fmt: skip
    user, opts = chat.calls[0]
    assert opts.thinking == "low"
    assert out[0].quality == "careful"
    assert "本批术语（推荐译法）：Let=设" in user  # 已在文档级术语表里的不重复放


def test_chat_error_propagates():
    with pytest.raises(ChatError):
        asyncio.run(
            translate(SEGS, chat=FakeChat(error=ChatError("API Key 无效")), cache=DictCache(), ctx=DocContext())
        )


def test_default_concurrency_by_provider():
    from pdftr.translation.service import default_concurrency

    assert default_concurrency("https://api.deepseek.com") == 4
    assert default_concurrency("https://api.deepseek.com/v1") == 4
    assert default_concurrency("http://localhost:11434/v1") == 1  # Ollama
    assert default_concurrency("https://dashscope.aliyuncs.com/compatible-mode/v1") == 2
    assert default_concurrency("https://notdeepseek.com.example.org") == 2


def test_add_cost_prices_each_request_at_its_own_time(monkeypatch):
    from pdftr.translation import cost

    usage = ChatUsage(prompt_tokens=1_000_000, completion_tokens=0)
    monkeypatch.setattr(cost, "is_deepseek_peak", lambda: False)
    total = cost.add_cost(None, "deepseek-flash", usage)
    monkeypatch.setattr(cost, "is_deepseek_peak", lambda: True)  # 任务中途进入高峰时段
    total = cost.add_cost(total, "deepseek-flash", usage)
    assert total == 1.0 + 2.0
    assert cost.add_cost(None, "unknown-model", usage) is None


def test_each_request_is_time_boxed_and_reported():
    class StuckWhenThinking(FakeChat):
        """前几轮丢占位符；最后一轮（开思考）一直不返回。"""

        async def complete(self, system, user, opts):
            if opts.thinking != "off":
                await asyncio.sleep(10)
            r = await super().complete(system, user, opts)
            return ChatResult(text=re.sub(r"<m\d+/>", "", r.text), usage=r.usage)

    notes: list[str] = []
    segs = [_seg(0, "Hard <m1/> one.", {"m1": "x"}), _seg(1, "Easy sentence.")]
    out, report = asyncio.run(
        translate(segs, chat=StuckWhenThinking(), cache=DictCache(), ctx=DocContext(),
                  opts=TranslateOptions(max_attempts=3, request_timeout_s=0.05),
                  on_progress=lambda d, t, n: notes.append(n))
    )  # fmt: skip
    assert out[0].status is SegmentStatus.FAILED and out[1].status is SegmentStatus.DONE  # 超时的段落标灰，其余照常
    assert report.failed == 1 and "还有 1 段待完成，正在单段重试（第 3/3 次）" in notes
    assert report.attempts[out[0].id] == 3
    assert "翻译请求超时" in report.failures[out[0].id]


class RepeatedFailures(FakeChat):
    def __init__(self, failures: int, text: str = "没有编号的合成响应") -> None:
        super().__init__()
        self.failures = failures
        self.text = text

    async def complete(self, system, user, opts):
        result = await super().complete(system, user, opts)
        if len(self.calls) <= self.failures:
            return ChatResult(text=self.text, usage=result.usage)
        return result


@pytest.mark.parametrize(
    "failures,expected_calls,success", [(0, 1, True), (1, 2, True), (9, 10, True), (10, 10, False)]
)
def test_tenth_attempt_can_succeed_and_no_eleventh_request(failures, expected_calls, success):
    chat = RepeatedFailures(failures)
    out, report = asyncio.run(translate(SEGS[:1], chat=chat, cache=DictCache(), ctx=DocContext()))
    assert len(chat.calls) == expected_calls == report.requests
    assert report.attempts[out[0].id] == expected_calls == out[0].translation_attempts
    assert out[0].status is (SegmentStatus.DONE if success else SegmentStatus.FAILED)
    assert bool(out[0].failure_reason) is not success
    assert report.usage.prompt_tokens == expected_calls * 100
    assert report.usage.completion_tokens == expected_calls * 50
    assert report.cost_cny is not None
    if not success:
        assert "已尝试 10/10 次" in out[0].failure_reason
        assert "返回编号缺失或译文为空" in out[0].failure_reason


@pytest.mark.parametrize("target", ["1|合成译文", "1|合成 <m1/><m1/>", "1|合成 <m1/><m2/>"])
def test_incomplete_or_repeated_formulas_never_count_as_success(target):
    out, report = asyncio.run(
        translate(SEGS[:1], chat=RepeatedFailures(10, target), cache=DictCache(), ctx=DocContext())
    )
    assert report.failed == 1 and report.translated == 0 and report.requests == 10
    assert out[0].target is None
    assert "公式占位符不一致" in out[0].failure_reason


def test_valid_cached_translation_needs_no_attempt_but_bad_cache_is_retranslated():
    chat, cache = FakeChat(), DictCache()
    asyncio.run(translate(SEGS[:1], chat=chat, cache=cache, ctx=DocContext()))
    key = next(iter(cache.data))
    cache.data[key] = "缓存缺少公式"
    chat2 = FakeChat()
    out, report = asyncio.run(translate(SEGS[:1], chat=chat2, cache=cache, ctx=DocContext()))
    assert report.cached == 0 and report.attempts[out[0].id] == 1
    assert out[0].target == "译:Let <m1/> be a function."
    cached, report2 = asyncio.run(translate(SEGS[:1], chat=FakeChat(), cache=cache, ctx=DocContext()))
    assert report2.cached == 1 and cached[0].translation_attempts == 0 and cached[0].failure_reason == ""


def test_only_failed_segments_are_retried_and_deduplicated_attempts_match():
    chat = FakeChat(break_once={"Let <m1/> be a function."})
    segs = [SEGS[0], SEGS[0].model_copy(update={"id": "duplicate"}), SEGS[1]]
    out, report = asyncio.run(translate(segs, chat=chat, cache=DictCache(), ctx=DocContext()))
    assert len(chat.calls) == 2
    assert "A repeated sentence here." not in chat.calls[1][0]
    assert report.attempts == {segs[0].id: 2, "duplicate": 2, segs[2].id: 1}
    assert all(s.status is SegmentStatus.DONE for s in out)


def test_concurrency_limit_applies_to_initial_batches_and_retries():
    class ConcurrentChat(FakeChat):
        active = 0
        peak = 0

        async def complete(self, system, user, opts):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.01)
                return await super().complete(system, user, opts)
            finally:
                self.active -= 1

    segs = [_seg(i, f"Synthetic sentence {i} with <m1/>.", {"m1": "x"}) for i in range(6)]
    chat = ConcurrentChat(break_once={s.source for s in segs})
    out, report = asyncio.run(
        translate(
            segs, chat=chat, cache=DictCache(), ctx=DocContext(), opts=TranslateOptions(concurrency=2, batch_tokens=1)
        )
    )
    assert chat.peak == 2 and all(s.status is SegmentStatus.DONE for s in out)
    assert report.requests == 12


def test_shared_retry_deadline_includes_queue_and_does_not_charge_queued_attempts():
    class StuckRetry(FakeChat):
        async def complete(self, system, user, opts):
            if self.calls:
                await asyncio.sleep(10)
            result = await super().complete(system, user, opts)
            return ChatResult(text="", usage=result.usage)

    segs = [_seg(i, f"Synthetic sentence {i}.") for i in range(3)]
    out, report = asyncio.run(
        translate(segs, chat=StuckRetry(), cache=DictCache(), ctx=DocContext(),
                  opts=TranslateOptions(concurrency=1, retry_timeout_s=0.05))
    )  # fmt: skip
    assert report.requests == 2 and report.failed == 3
    assert sorted(report.attempts.values()) == [1, 1, 2]
    assert all("补救总时限耗尽" in s.failure_reason for s in out)


def test_total_deadline_covers_initial_requests_and_waiting_for_slots():
    class StuckChat(FakeChat):
        async def complete(self, system, user, opts):
            await asyncio.sleep(10)
            return await super().complete(system, user, opts)

    segs = [_seg(i, f"Synthetic sentence {i}.") for i in range(3)]
    out, report = asyncio.run(
        translate(segs, chat=StuckChat(), cache=DictCache(), ctx=DocContext(),
                  opts=TranslateOptions(concurrency=1, batch_tokens=1, total_timeout_s=0.05))
    )  # fmt: skip
    assert report.requests == 1 and report.failed == 3
    assert sorted(report.attempts.values()) == [0, 0, 1]
    assert all("本次翻译总时限耗尽" in s.failure_reason for s in out)


def test_transient_chat_error_is_retried_but_auth_error_is_not():
    class TransientChat(FakeChat):
        first = True

        async def complete(self, system, user, opts):
            if self.first:
                self.first = False
                raise ChatError("临时连接失败", retryable=True)
            return await super().complete(system, user, opts)

    out, report = asyncio.run(translate(SEGS[:1], chat=TransientChat(), cache=DictCache(), ctx=DocContext()))
    assert out[0].status is SegmentStatus.DONE and report.attempts[out[0].id] == 2
    assert report.requests == 2


def test_persistent_transient_error_stops_at_ten_and_reports_reason():
    out, report = asyncio.run(
        translate(
            SEGS[:1],
            chat=FakeChat(error=ChatError("临时连接失败", retryable=True)),
            cache=DictCache(),
            ctx=DocContext(),
        )
    )
    assert report.requests == 10 and out[0].translation_attempts == 10
    assert out[0].status is SegmentStatus.FAILED
    assert "临时连接失败" in out[0].failure_reason


def test_fatal_error_cancels_other_requests_in_same_round():
    async def scenario():
        other_started, other_cancelled = asyncio.Event(), asyncio.Event()

        class FatalChat(FakeChat):
            async def complete(self, system, user, opts):
                if "Sentence zero" in user:
                    await other_started.wait()
                    raise ChatError("API Key 无效")
                other_started.set()
                try:
                    await asyncio.sleep(10)
                finally:
                    other_cancelled.set()
                return await super().complete(system, user, opts)

        with pytest.raises(ChatError, match="API Key 无效"):
            await translate(
                [_seg(0, "Sentence zero."), _seg(1, "Sentence one.")],
                chat=FatalChat(),
                cache=DictCache(),
                ctx=DocContext(),
                opts=TranslateOptions(batch_tokens=1),
            )
        assert other_cancelled.is_set()

    asyncio.run(scenario())


def test_cancellation_propagates_and_stops_all_model_requests():
    async def scenario():
        started, cancelled = asyncio.Event(), asyncio.Event()

        class StuckChat(FakeChat):
            async def complete(self, system, user, opts):
                started.set()
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.set()
                return await super().complete(system, user, opts)

        task = asyncio.create_task(translate(SEGS, chat=StuckChat(), cache=DictCache(), ctx=DocContext()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("attempts", [0, 11])
def test_attempt_limit_cannot_exceed_ten(attempts):
    with pytest.raises(ValueError):
        TranslateOptions(max_attempts=attempts)
