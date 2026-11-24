import asyncio
import re

import pytest

from pdftr.domain.ir import BlockType, Segment, SegmentStatus
from pdftr.domain.llm import ChatError, ChatOptions, ChatResult, ChatUsage
from pdftr.translation.batching import citation_problems, normalize_cjk_spacing
from pdftr.translation.prompt import DocContext
from pdftr.translation.service import TranslateOptions, _target_problems, cache_key, translate


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
    assert report.requests == 3  # 两章各翻一批，另有一次单段补救
    first_user = chat.calls[0][0]
    assert "<m1>f(x)</m1>" in first_user  # 模型能看到公式内容
    assert "第1段章节：2 Method" in first_user and "3 Results" not in first_user
    assert "本批所在章节：3 Results" in chat.calls[1][0]
    assert first_user.count("A repeated sentence here.") == 1
    assert report.cost_cny is not None and report.usage.prompt_tokens == 300

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


def test_persistent_italic_loss_is_retried_with_explicit_reason():
    class DropItalic(FakeChat):
        async def complete(self, system, user, opts):
            result = await super().complete(system, user, opts)
            return ChatResult(text=result.text.replace("<i>", "").replace("</i>", ""), usage=result.usage)

    chat = DropItalic()
    (out,), report = asyncio.run(
        translate(
            [_seg(0, "An <i>important</i> detail.")],
            chat=chat,
            cache=DictCache(),
            ctx=DocContext(),
            opts=TranslateOptions(max_attempts=2),
        )
    )
    assert out.status is SegmentStatus.FAILED and out.translation_attempts == 2
    assert report.requests == 2 and report.failed == 1 and "斜体标记丢失" in out.failure_reason


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
                  lookup_terms=lambda s: {"Function": "其他译法", "Let": "设"},
                  opts=TranslateOptions(quality="careful"))
    )  # fmt: skip
    user, opts = chat.calls[0]
    assert opts.thinking == "low"
    assert out[0].quality == "careful"
    assert "参考词库" in user and "Let=设" in user
    assert "Function=" not in user  # 不区分大小写去掉和文档术语重复的参考词


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


@pytest.mark.parametrize("reason", ["length", "max_tokens"])
def test_truncated_batch_only_accepts_rows_before_the_last_number(reason):
    class Truncated(FakeChat):
        async def complete(self, system, user, opts):
            await super().complete(system, user, opts)
            if len(self.calls) == 1:
                return ChatResult(text="1|完整译文\n2|不完整", finish_reason=reason)
            assert all(value != "不完整" for value in cache.data.values())
            assert "完整译文" in cache.data.values()
            return ChatResult(text="1|修复后的译文")

    cache = DictCache()
    segs = [_seg(i, f"Synthetic segment {i}.") for i in range(3)]
    out, report = asyncio.run(translate(segs, chat=Truncated(), cache=cache, ctx=DocContext()))
    assert [s.target for s in out] == ["完整译文", "修复后的译文", "修复后的译文"]
    assert list(report.attempts.values()) == [1, 2, 2]


@pytest.mark.parametrize("reason", ["refusal", "content_filter"])
def test_refusal_with_numbered_text_is_terminal(reason):
    class Refusal(FakeChat):
        async def complete(self, system, user, opts):
            await super().complete(system, user, opts)
            return ChatResult(text="说明如下\n1|不能使用的文字", finish_reason=reason)

    cache, chat = DictCache(), Refusal()
    out, report = asyncio.run(translate(SEGS[1:2], chat=chat, cache=cache, ctx=DocContext()))
    assert report.requests == report.failed == 1 and report.translated == 0
    assert out[0].status is SegmentStatus.FAILED and out[0].target is None
    assert reason in out[0].failure_reason and not cache.data


@pytest.mark.parametrize("reason", ["refusal", "content_filter"])
def test_refused_batch_retries_each_segment_once_when_one_is_refused(reason):
    class Refusal(FakeChat):
        async def complete(self, system, user, opts):
            answer = await super().complete(system, user, opts)
            if blocked.source in user:
                return ChatResult(text="1|拒绝翻译", finish_reason=reason)
            return answer

    blocked, valid = SEGS[2:]
    chat, cache = Refusal(), DictCache()
    out, report = asyncio.run(translate([blocked, valid], chat=chat, cache=cache, ctx=DocContext()))
    assert f"1|{blocked.source}\n2|{valid.source}" in chat.calls[0][0]
    assert report.requests == 3 and report.attempts == {blocked.id: 2, valid.id: 2}
    assert report.failed == report.translated == 1
    assert out[0].status is SegmentStatus.FAILED and reason in out[0].failure_reason
    assert out[1].status is SegmentStatus.DONE and out[1].target == f"译:{valid.source}"
    assert list(cache.data.values()) == [out[1].target]


@pytest.mark.parametrize("text", ["1|甲\n1|乙", "1|甲\n3|乙", "2|乙\n1|甲"])
def test_invalid_numbering_retries_entire_batch_without_caching(text):
    class WrongBatch(FakeChat):
        async def complete(self, system, user, opts):
            answer = await super().complete(system, user, opts)
            if len(self.calls) == 1:
                return ChatResult(text=text)
            assert all(value.startswith("译:") for value in cache.data.values())
            return answer

    cache = DictCache()
    out, report = asyncio.run(translate(SEGS[2:], chat=WrongBatch(), cache=cache, ctx=DocContext()))
    assert report.requests == 3
    assert [s.target for s in out] == [f"译:{s.source}" for s in SEGS[2:]]


PROSE = (
    "When the air cools, the instrument records the change over time so that every measured "
    "variation can be compared with the values obtained before the trial began."
)
PROSE_TRANSLATION = "当空气冷却时，仪器记录随时间发生的变化，以便将每项观测变化与试验开始前获得的数值进行比较。"


@pytest.mark.parametrize(
    ("source", "bad", "good", "reason"),
    [
        (
            "Use X<sub>steady_state_1</sub> and y<sup>2</sup>.",
            "使用 X 和 y。",
            "使用 X<sub>steady_state_1</sub> 和 y<sup>2</sup>。",
            "上下标",
        ),
        ("Use X<sub>1</sub>.", "使用 X<sub> </sub>。", "使用 X<sub>1</sub>。", "没有内容"),
        ("Use <b>X<sub>1</sub></b>.", "使用 <b>X<sub>1</b></sub>。", "使用 <b>X<sub>1</sub></b>。", "嵌套"),
        (
            PROSE + " [12] (Author et al., 2020) https://example.test/test /tmp/example",
            PROSE + " [12]",
            "仪器记录降温过程的变化，以便与试验前的测量值比较[12]。",
            "照抄",
        ),
        (PROSE, "温度。", "记录降温变化并与试验前的测量值比较。", "缩水"),
        (PROSE, "温度变化。" * 150, "记录降温变化并与试验前的测量值比较。", "暴涨"),
        (PROSE, "在本文中，" + PROSE, PROSE_TRANSLATION, "照抄"),
        (
            "We evaluate our method across several tasks with the same settings as in previous experiments.",
            "实验表明：We evaluate our method across several tasks with the same settings as in previous experiments.",
            "我们沿用以往实验的设置，在多个任务上评估此方法。",
            "照抄",
        ),
        (
            "The Chinese token 猫 is segmented into its own unit when the parser reads the sentence.",
            "The Chinese token 猫 is segmented into its own unit when the parser reads the sentence.",
            "解析器读取句子时，将汉字‘猫’切分为独立的单元。",
            "照抄",
        ),
        (PROSE, " ".join([PROSE_TRANSLATION] * 4), PROSE_TRANSLATION, "复读"),
        (
            "We compare precision/recall and encoder/decoder with the original model.",
            "We compare precision/recall and encoder/decoder with the original model.",
            "我们将精确率、召回率和编解码器与原始模型作比较。",
            "照抄",
        ),
    ],
)
def test_quality_faults_reject_cache_and_model_output(source, bad, good, reason):
    seg = _seg(0, source)
    chat, cache, ctx = RepeatedFailures(1, f"1|{bad}"), DictCache(), DocContext()
    out, report = asyncio.run(translate([seg], chat=chat, cache=cache, ctx=ctx, opts=TranslateOptions(max_attempts=1)))
    assert report.failed == 1 and reason in out[0].failure_reason and not cache.data

    key = cache_key(seg, chat.model, {}, "default")
    cache.data[key] = bad
    chat = RepeatedFailures(1, f"1|{good}")
    out, report = asyncio.run(translate([seg], chat=chat, cache=cache, ctx=ctx))
    assert report.cached == 0 and report.requests == 1 and out[0].target == normalize_cjk_spacing(good)
    assert cache.data == {key: normalize_cjk_spacing(good)}


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("Transformer (big)", "Transformer (big)"),
        ("Analysis of Results for the Proposed Models", "Analysis of Results for the Proposed Models"),
        ("The Effect of Learning Rate on Model Performance", "The Effect of Learning Rate on Model Performance"),
        ("BLEU 28.3 / ROUGE 82.0", "BLEU 28.3 / ROUGE 82.0"),
        ("Let <m1/> be the input and <m2/> the output.", "令<m1/>为输入，<m2/>为输出。"),
        (
            "本文使用合成数据来检验计算过程并记录全部对照实验的观测指标。",
            "本文使用合成数据来检验计算过程并记录全部对照实验的观测指标。",
        ),
        (PROSE, "记录冷却曲线并与试验前对比。"),
        (PROSE, PROSE_TRANSLATION),
        (
            "本文使用 Transformer to compare image and text embeddings with the original model。",
            "本文使用 Transformer to compare image and text embeddings with the original model。",
        ),
    ],
)
def test_quality_checks_allow_short_technical_and_compact_translations(source, target):
    seg = _seg(0, source, {"m1": "x", "m2": "y"} if "<m1/>" in source else None)
    out, report = asyncio.run(
        translate([seg], chat=RepeatedFailures(1, f"1|{target}"), cache=DictCache(), ctx=DocContext())
    )
    assert report.requests == 1 and out[0].target == normalize_cjk_spacing(target)


def test_accepted_model_output_is_normalized_before_cache_and_persistence():
    class SpacedChat(FakeChat):
        async def complete(self, system, user, opts):
            return ChatResult(text="1|模型 <b>ResNet 50</b> 的 2 层含 <m1/> 公式。")

    seg = _seg(0, "The <b>ResNet 50</b> model with <m1/>.", {"m1": "x"})
    cache = DictCache()
    expected = "模型<b>ResNet 50</b>的2层含<m1/>公式。"
    out, _ = asyncio.run(translate([seg], chat=SpacedChat(), cache=cache, ctx=DocContext()))
    assert out[0].target == expected
    key = cache_key(seg, SpacedChat.model, {}, "default")
    assert cache.get(key) == expected
    cached, report = asyncio.run(translate([seg], chat=FakeChat(), cache=cache, ctx=DocContext()))
    assert cached[0].target == expected and report.cached == 1


_PREVIOUS = "NovaGrid recorded 47 samples during the initial calibration."
_ELABORATION = (
    "这次测量持续了整个校准过程，研究人员逐项记录每轮读数，并独立核对设备状态，"
    "随后对照所有观测结果调整了采样方式，最终将这些数据汇总到同一份报告中。"
)


@pytest.mark.parametrize("source", ["This worked.", "Calibration succeeded."])
def test_context_leakage_rejects_cache_and_retries_model_output(source: str) -> None:
    previous = _seg(0, _PREVIOUS, section="Calibration")
    current = _seg(1, source, section="Calibration")
    previous.role = current.role = BlockType.TEXT
    bad = "NovaGrid记录了47个样本。" + _ELABORATION
    chat, cache = RepeatedFailures(1, "1|" + bad), DictCache()
    key = cache_key(
        current, chat.model, {}, "default", section=current.section, role=BlockType.TEXT.value, prev_context=_PREVIOUS
    )
    cache.put(key, bad)
    (out,), report = asyncio.run(
        translate(
            [current],
            context_segments=[previous, current],
            chat=chat,
            cache=cache,
            ctx=DocContext(),
            opts=TranslateOptions(max_attempts=2),
        )
    )
    assert out.status is SegmentStatus.DONE and report.cached == 0
    assert report.requests == out.translation_attempts == 2
    assert (_PREVIOUS in chat.calls[0][0]) == source.startswith("This")
    assert "吸纳了参考上文" in chat.calls[1][0]
    assert cache.get(key) == out.target and out.target != bad


@pytest.mark.parametrize(
    ("source", "context", "target", "leaks"),
    [
        ("This worked.", _PREVIOUS, "该方法有效。", False),
        ("This worked.", _PREVIOUS, "NovaGrid的47次测量有效。", False),
        ("This worked.", _PREVIOUS, "NovaGrid与147次采样。" + _ELABORATION, False),
        ("This worked on 47 samples.", _PREVIOUS, "NovaGrid与47次采样。" + _ELABORATION, False),
        ("This worked.", _PREVIOUS, "NovaGrid与47次采样。" + _ELABORATION, True),
        ("This worked.", "NovaGrid and 47.", "NovaGrid与47次采样。" + _ELABORATION, False),
        ("This worked.", "According to Algorithm 2 the update took 4ms.", "算法2耗时4ms。" + _ELABORATION, True),
        ("This worked.", "Section 3 compares our data with Table 5.", "第3节对照表5。" + _ELABORATION, True),
        ("This worked.", "We recorded several values. It took longer.", "We和It的译法。" + _ELABORATION, False),
        ("This worked.", _PREVIOUS, _PREVIOUS + "这一做法有效。", True),
        (
            "This worked.",
            "The lab ran 10 and 100 trials for each setup.",
            "分别运行10次和100次。" + _ELABORATION,
            False,
        ),
    ],
)
def test_context_leakage_requires_length_and_distinct_evidence(
    source: str, context: str, target: str, leaks: bool
) -> None:
    errors = _target_problems(source, target, prev_context=context)
    assert any("吸纳了参考上文" in error for error in errors) == leaks


def test_translated_context_in_an_ordinary_length_paragraph() -> None:
    context = "The LUX-UV instrument uses LUX to collect samples and UV to check the readings."
    source = (
        "This second trial measures the speed of the LUX controller when the operator changes the sampling interval "
        "and then compares each result with a separate set of independent observations."
    )
    clean = "第二轮试验测量操作人员改变采样间隔时控制器的响应速度，再将各次测量结果与另一组独立观测数据逐一比较。"
    copied = "此前使用LUX-UV仪器采集样本，并利用UV通道复核各个测点的读数。" + _ELABORATION[:35]
    assert not _target_problems(source, clean, prev_context=context)
    assert not _target_problems(source, clean + _ELABORATION, prev_context=context)
    assert not _target_problems(source, "该LUX-UV仪器的UV通道参与测量。" + clean, prev_context=context)
    assert any("吸纳了参考上文" in s for s in _target_problems(source, copied + clean, prev_context=context))


def test_citation_rejects_lost_separator_and_reordered_group() -> None:
    source = "See prior work [29, 30]."
    errors = citation_problems(source, "见[29 30]。")
    assert all(any(term in error for error in errors) for term in ["缺少引文 [29]", "缺少引文 [30]", "分隔符"])
    assert any("应保持原文顺序" in error for error in citation_problems(source, "见[30, 29]。"))
    assert any("缺少编号之间的分隔符" in error for error in citation_problems(source, "见[30 29]。"))
    assert citation_problems(source, "见[29, 30]。") == []
    assert citation_problems(source, "见[29]、[30]。") == []


def test_citation_order_uses_only_confirmed_short_groups() -> None:
    assert citation_problems("See [4–6].", "见［６、５、４］。")
    assert citation_problems("See [4–6].", "见[4,5,6]。") == []
    assert citation_problems("See [29,30] and [30,29].", "见[30,29]和[29,30]。") == []
    assert citation_problems("Matrix [29,30]. See [7].", "矩阵[30,29]。见[7]。") == []
    assert citation_problems("See [29,30].", "见[29,30]，矩阵[29 30]。") == []
    assert citation_problems("See [2,2,3].", "见[3,2,2]。") == []
    assert citation_problems("See [1–16].", "见[16,1–15]。") == []
