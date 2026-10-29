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

    def purge_older_than(self, days: int) -> int:
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
                  opts=TranslateOptions(max_retries=2))
    )  # fmt: skip
    assert out[0].status is SegmentStatus.FAILED and out[0].target is None
    assert out[1].target == "用户的译文" and out[1].status is SegmentStatus.EDITED
    assert report.failed == 1 and report.requests == 3  # 一次批量 + 两次重试


def test_retries_are_batched_and_last_one_thinks():
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
    # 首轮 1 批；第一轮重试成批 1 次；最后一轮逐段开思考 5 次
    assert report.requests == 7
    assert [o.thinking for _, o in chat.calls] == ["off", "off"] + ["low"] * 5


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


def test_final_round_is_time_boxed_and_reported():
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
                  opts=TranslateOptions(final_round_timeout_s=0.05), on_progress=lambda d, t, n: notes.append(n))
    )  # fmt: skip
    assert out[0].status is SegmentStatus.FAILED and out[1].status is SegmentStatus.DONE  # 超时的段落标灰，其余照常
    assert report.failed == 1 and "还有 1 段较难，正在开思考重翻" in notes
