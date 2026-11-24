import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from pdftr.api.schemas import EditIn
from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.domain.chars import Char, CharPage
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType, Document, Page, Segment, SegmentStatus, Span
from pdftr.domain.job import Job, JobStatus
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutPage, LayoutResult, LayoutSpan
from pdftr.domain.llm import ChatOptions, ChatResult
from pdftr.fusion.build import fuse
from pdftr.fusion.formula_align import merge_torn_formulas
from pdftr.fusion.text_fix import choose, recover_dropped_chars
from pdftr.glossary.extract import Extraction
from pdftr.glossary.matcher import TermMatcher, TermSelection
from pdftr.glossary.normalize import normalize_terms
from pdftr.glossary.sources import clean_zh
from pdftr.pipeline.usecases import UserError, edit_segment
from pdftr.storage.cache import SqliteTranslationCache
from pdftr.storage.sqlite import SqliteStore
from pdftr.translation.prompt import DocContext
from pdftr.translation.segmenter import bad_source, front_matter_ids
from pdftr.translation.service import TranslateOptions, cache_key, translate


def _ch(c: str, x: float, y: float = 100.0, *, font: str = "Synthetic-Roman", italic: bool = False) -> Char:
    return Char(
        c=c,
        bbox=Rect.of([x, y, x + 5, y + 10]),
        font=font,
        size=10.0,
        origin_y=y + 8,
        italic=italic,
        line=0,
    )


def test_control_char_span_does_not_disable_whole_prose_block():
    good_box = Rect.of([50, 100, 260, 112])
    ctrl_box = Rect.of([262, 100, 270, 112])
    bad_box = Rect.of([50, 140, 120, 152])
    lb_good = LayoutBlock(
        type=BlockType.TEXT,
        bbox=Rect.of([50, 100, 270, 112]),
        order=0,
        lines=[
            LayoutLine(
                bbox=Rect.of([50, 100, 270, 112]),
                spans=[
                    LayoutSpan(kind="text", content="Metric measures distance between sets.", bbox=good_box),
                    LayoutSpan(kind="text", content="\x00", bbox=ctrl_box),
                ],
            )
        ],
    )
    lb_bad = LayoutBlock(
        type=BlockType.TEXT,
        bbox=bad_box,
        order=1,
        lines=[LayoutLine(bbox=bad_box, spans=[LayoutSpan(kind="text", content="\ufffd\ufffd\ufffd", bbox=bad_box)])],
    )
    chars = [_ch(c, 50 + i * 5) for i, c in enumerate("Metric measures distance between sets.")] + [
        _ch("\x00", 264, font="CMEX10"),
        _ch("\ufffd", 55, 140),
        _ch("\ufffd", 60, 140),
        _ch("\ufffd", 65, 140),
    ]
    doc, _ = fuse(
        "synthetic",
        LayoutResult(parser="synthetic", pages=[LayoutPage(index=0, width=600, height=800, blocks=[lb_good, lb_bad])]),
        [CharPage(index=0, width=600, height=800, chars=chars)],
        used_cloud=False,
    )
    good, bad = doc.blocks()
    assert good.translatable is True
    assert [s.text for s in good.spans] == ["Metric measures distance between sets."]
    assert bad.translatable is False


def test_choose_repairs_broken_script_payloads_and_recover_allows_trailing_comma():
    repaired, changed = choose(
        "the nodes \ufffd<sup>\ufffd</sup> in the tree",
        "the nodes Nk in the tree",
    )
    assert changed is True and "\ufffd" not in repaired
    assert repaired in {"the nodes N<sup>k</sup> in the tree", "the nodes Nk in the tree"}

    repaired2, changed2 = choose(
        "produces values (\ufffd\ufffd\ufffd\ufffd\ufffd \u2208 R<sup>\ufffd</sup>) here",
        "produces values (Label \u2208 Rk) here",
    )
    assert changed2 is True and "\ufffd" not in repaired2 and "Label" in repaired2

    assert (
        recover_dropped_chars(
            "At each time ste the solver is em lo ed to com ute",
            "At each time step, the solver is employed to compute",
        )
        == "At each time step, the solver is employed to compute"
    )
    # 拒绝词内插逗号或数学符号
    assert recover_dropped_chars("We compare colr value.", "We compare co,lr value.") == "We compare colr value."
    assert recover_dropped_chars("We compare colr value.", "We compare colr +value.") == "We compare colr value."


def test_merge_torn_formulas_accepts_italic_variable_bridge():
    left_box = Rect.of([50, 100, 90, 110])
    mid_box = Rect.of([92, 100, 120, 110])
    right_box = Rect.of([122, 100, 160, 110])
    line = LayoutLine(
        bbox=Rect.of([50, 100, 160, 110]),
        spans=[
            LayoutSpan(kind="inline_formula", content=r"( a _ { i }", bbox=left_box, located=True),
            LayoutSpan(kind="text", content=", var", bbox=mid_box),
            LayoutSpan(kind="inline_formula", content=r"b _ { j } )", bbox=right_box, located=True),
        ],
    )
    chars = (
        [_ch(c, 52 + i * 6) for i, c in enumerate("(ai")]
        + [_ch(c, 93 + i * 5, font="BodySerif-Italic", italic=True) for i, c in enumerate(",var")]
        + [_ch(c, 124 + i * 6) for i, c in enumerate("bj)")]
    )
    merged = merge_torn_formulas(line, chars)
    assert len(merged.spans) == 1
    assert merged.spans[0].kind == "inline_formula"
    assert ", var" in merged.spans[0].content


def test_glossary_clean_normalize_and_matcher_precision():
    assert clean_zh("正则化（L1/L2）；规则化") == "正则化"
    assert clean_zh("模型(Transformer/BERT)/网络") == "模型"

    body = (
        "2. Recall\nRecall measures sensitivity in retrieval. High recall improves coverage. "
        "We apply policy-gradient updates with ADAM and Adam."
    )
    norm = normalize_terms(
        {"Recall": "召回率", "Introduction": "引言", "policy gradient": "策略梯度", "ADAM": "ADAM"},
        body,
        headings=["1. Introduction", "2. Recall"],
    )
    assert norm["Recall"] == "召回率"
    assert "Introduction" not in norm
    assert "policy-gradient" in norm and norm["policy-gradient"] == "策略梯度"
    assert norm["ADAM"] == "ADAM"

    matcher = TermMatcher(
        {"policy gradient": "策略梯度", "box": "框", "SVM": "支持向量机", "attention": "注意力"},
        min_length=3,
    )
    assert matcher.match("Check the policy. Gradient rises. Use self-attention.") == {}
    assert matcher.match("Apply policy-gradient on boxes with SVMs.") == {
        "policy gradient": "策略梯度",
        "box": "框",
        "SVM": "支持向量机",
    }
    assert matcher.match("lowercase svms are ignored") == {}

    found = Extraction(domain="优化", terms={"policy gradient": "策略梯度"})
    sel = TermSelection(found, {"policy gradients": "政策梯度"}, lambda: None)
    assert sel.paper == {"policy gradient": "政策梯度"}


def test_bad_source_allows_chemical_formulas_and_blocks_legacy_word_scripts():
    chem = Segment(
        id="s0",
        job_id="j1",
        block_ids=["p0-b0"],
        source="Samples Fe<sub>x</sub>Co, Li<sub>x</sub>Mn, and Na<sub>x</sub>Ni show high stability.",
    )
    assert bad_source([chem]) is False
    legacy = Segment(
        id="s1",
        job_id="j1",
        block_ids=["p0-b1"],
        source="The de<sub>m</sub>and of pro<sub>p</sub>osed sc<sup>h</sup>eduling grows.",
    )
    assert bad_source([legacy]) is True


def test_front_matter_preserves_structured_abstract_and_long_prose():
    def blk(idx: int, btype: BlockType, text: str) -> Block:
        box = Rect.of([50, 50 + idx * 30, 500, 75 + idx * 30])
        return Block(
            id=f"p0-b{idx}",
            page=0,
            type=btype,
            bbox=box,
            order=idx,
            spans=[Span(text=text, bbox=box, size=10.0)],
            style=BlockStyle(font_size=14.0 if btype is BlockType.TITLE else 10.0),
            translatable=True,
        )

    doc = Document(
        job_id="j1",
        pages=[
            Page(
                index=0,
                width=600,
                height=800,
                blocks=[
                    blk(0, BlockType.TITLE, "A Clinical Evaluation"),
                    blk(1, BlockType.TEXT, "A. Author, B. Writer, Department of Medicine, demo@example.edu"),
                    blk(2, BlockType.TEXT, "Objective: Evaluate the diagnostic accuracy of the proposed protocol."),
                    blk(3, BlockType.TEXT, "Methods: We enrolled two hundred participants across four cohorts."),
                    blk(4, BlockType.TITLE, "1. Introduction"),
                ],
            )
        ],
    )
    assert front_matter_ids(doc) == {"p0-b1"}


def test_edit_rejects_whitespace_only_target(tmp_path: Path):
    with pytest.raises(ValidationError):
        EditIn(target="   ")
    pipeline = build_container(Settings(data_dir=tmp_path, glossary_sources="")).pipeline
    try:
        job = Job(id="0123456789abcdef", filename="synthetic.pdf", status=JobStatus.DONE, stage="done")
        seg = Segment(
            id="s0",
            job_id=job.id,
            block_ids=["p0-b0"],
            source="Hello world.",
            target="你好世界。",
            status=SegmentStatus.DONE,
        )
        pipeline.deps.store.save_job(job)
        pipeline.deps.store.replace_segments(job.id, [seg])
        with pytest.raises(UserError):
            edit_segment(pipeline, "s0", "   ")
    finally:
        assert isinstance(pipeline.deps.store, SqliteStore)
        assert isinstance(pipeline.deps.cache, SqliteTranslationCache)
        pipeline.deps.store.close()
        pipeline.deps.cache.close()


class _MemCache:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def put(self, key: str, target: str) -> None:
        self.data[key] = target

    def purge_older_than(self, hours: int) -> int:
        return 0


def test_cache_isolation_no_write_on_retranslate_and_retry_feedback():
    s_rl = Segment(id="s0", job_id="j1", block_ids=["p0-b0"], source="Return", section="Reinforcement Learning")
    s_cc = Segment(id="s1", job_id="j1", block_ids=["p0-b1"], source="Return", section="Compiler Control Flow")
    assert cache_key(s_rl, "m", {}, "default", domain="RL", section=s_rl.section) != cache_key(
        s_cc, "m", {}, "default", domain="RL", section=s_cc.section
    )
    assert cache_key(s_rl, "m", {}, "default", domain="RL", provider="openai:https://a") != cache_key(
        s_rl, "m", {}, "default", domain="RL", provider="openai:https://b"
    )

    class _ContextChat:
        model = "fake"
        provider_id = "openai:https://example.invalid"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            self.calls += 1
            if "Compiler" in user:
                return ChatResult(text="1|返回")
            return ChatResult(text="1|回报")

        async def list_models(self) -> list[str]:
            return []

    cache = _MemCache()
    out, _ = asyncio.run(
        translate(
            [s_rl, s_cc],
            chat=_ContextChat(),
            cache=cache,
            ctx=DocContext(domain="计算机"),
            opts=TranslateOptions(batch_tokens=1, concurrency=1),
        )
    )
    assert [s.target for s in out] == ["回报", "返回"]
    before = dict(cache.data)

    # use_cache=False 重翻不覆写共享缓存
    class _AltChat(_ContextChat):
        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            return ChatResult(text="1|收益")

    re_out, _ = asyncio.run(
        translate(
            [s_rl.model_copy(update={"status": SegmentStatus.PENDING})],
            chat=_AltChat(),
            cache=cache,
            ctx=DocContext(domain="计算机"),
            opts=TranslateOptions(use_cache=False, concurrency=1),
        )
    )
    assert re_out[0].target == "收益"
    assert cache.data == before

    # 校验失败重试时将具体错误诊断喂回模型
    class _FeedbackChat:
        model = "fake"
        provider_id = "openai:https://example.invalid"

        def __init__(self) -> None:
            self.users: list[str] = []

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            self.users.append(user)
            if len(self.users) == 1:
                return ChatResult(text="1|样本权重为<m1/>。")  # 故意漏掉 <m2/>
            return ChatResult(text="1|样本<m2/>的权重为<m1/>。")

        async def list_models(self) -> list[str]:
            return []

    fb_chat = _FeedbackChat()
    seg_f = Segment(
        id="sf",
        job_id="j2",
        block_ids=["p0-b0"],
        source="The weight <m1/> of sample <m2/>.",
        formulas={"m1": "w", "m2": "s"},
    )
    fb_out, _ = asyncio.run(
        translate([seg_f], chat=fb_chat, cache=_MemCache(), ctx=DocContext(), opts=TranslateOptions(concurrency=1))
    )
    assert fb_out[0].status is SegmentStatus.DONE
    assert len(fb_chat.users) == 2
    assert "上次翻译未通过校验" in fb_chat.users[1] and "<m2/>" in fb_chat.users[1]
