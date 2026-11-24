import asyncio
import re

import pytest

from pdftr.domain.ir import BlockType, Segment, SegmentStatus
from pdftr.domain.llm import ChatOptions, ChatResult
from pdftr.translation.batching import (
    VALIDATION_VERSION,
    NumberedResponseError,
    bold_problems,
    citation_problems,
    italic_problems,
    make_batches,
    parse_numbered,
    rich_text_problems,
    script_problems,
    script_value_problems,
)
from pdftr.translation.prompt import DocContext, SegmentPromptMeta, system_prompt, user_prompt
from pdftr.translation.service import TranslateOptions, cache_key, translate


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("We use ResNet [12].", "我们使用ResNet[21]。"),
        ("As shown in [12].", "如[21]所示。"),
        ("Proposed in [3, 4].", "在[3, 5]中提出。"),
        ("See [12] and compare with [12].", "见[12]。"),
        ("See [12].", "见[12]和[34]。"),
        ("See [1–31].", "见[1–30]。"),
        ("See [1–32].", "见[1–31, 32]。"),
        ("See [999].", "见[998]。"),
        ("See Ref. [2020].", "见参考文献［2021］。"),
        ("See Citation [9999].", "见文献[9998]。"),
        ("See [7]. Matrix [1, 2].", "见[7]及[8]。矩阵[1, 2]。"),
        ("See [7].", "见[7]和[8]，数组[2]。"),
    ],
)
def test_citation_anchor_rejects_renumbering_loss_or_addition(source, target):
    assert citation_problems(source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("See [1, 2].", "见[1][2]。"),
        ("See [<b>1</b>][2].", "见［１，２］。"),
        ("See [2; 3, 4].", "见［４、３；２］。"),
        ("See [００１].", "见[1]。"),
        ("See [7, 1–3].", "见［1、2、3］和[7]。"),
        ("See [1–31].", "见[1][2–31]。"),
        ("See [1–32].", "见［1—32］。"),
        ("See Refs. [1000–1002].", "参考文献［1000，1001，1002］。"),
        ("We use ResNet [12].", "我们使用ResNet [12]。"),
        ("A[12] and [12] appear.", "A[12]与[12]均出现。"),
        ("Range [0, 2] and [2, 5].", "范围[0, 2]改写，见[2, 5]。"),
        ("See https://example.test/[12] and www.example.test/[14].", "请访问网页。"),
        ("See [12](https://example.test) and [13] (http://example.test).", "见两个链接。"),
        ("See <m1/> and [2<m2/>3].", "请参阅<m1/>和<m2/>。"),
        ("Years [1900] and [2099].", "年份。"),
        ("See [1000] and [9999].", "见相关条目。"),
        ("See [-2] and [2.5] and [7–5].", "对应数值。"),
        ("See [" + " " * 59 + "1].", "见[1]。"),
        ("See [" + " " * 60 + "1].", "见相关条目。"),
        ("See [2].", "见[2]，区间［1, 3］，范围［1, 4］，向量［1, 5］，矩阵［1, 6］。"),
        ("See [2].", "见[2]及网址https://example.test/[3]"),
        ("See [2].", "见[2]和混合括号［3]。"),
    ],
)
def test_citation_anchor_accepts_equivalent_or_unconfirmed_brackets(source, target):
    assert citation_problems(source, target) == []


@pytest.mark.parametrize(
    "index",
    [
        "arr", "vec", "mat", "seq", "idx", "dim", "buf", "mem", "ptr", "reg", "tab", "row", "col",
        "node", "edge", "tensor", "layer", "A", "A ", "x_i", "v2", "f(x)",
    ],
)  # fmt: skip
def test_adjacent_index_is_excluded_even_with_confirmed_citations(index):
    assert citation_problems(f"Use {index}[7][8]. See [12].", "改写索引，见[12]。") == []


@pytest.mark.parametrize(
    "prefix",
    ["= ", "+ ", "- ", "* ", "/ ", "∈ ", "⊂ ", "⊆ ", "<m1/> + ", "interval ", "range ", "vector ", "list ",
     "array ", "matrix ", "tuple ", "shape ", "size ", "dims ", "kernel ", "stride ", "x in ", "<m1/> in ",
     "values in "],
)  # fmt: skip
def test_citation_math_and_container_context_is_excluded(prefix):
    assert citation_problems(f"Use {prefix}[1, 2]. See [12].", "做计算，见[12]。") == []


def test_citation_followed_by_assignment_or_call_is_excluded():
    assert citation_problems("[1] = 2. [3](x). See [12].", "计算后见[12]。") == []


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("10<sup>−6</sup>", "10<sup>6</sup>"),
        ("x<sup>+2</sup>", "x<sup>+3</sup>"),
        ("x<sup>−6</sup>", "x<sup>6</sup>"),
        ("10<sup>6</sup>", "10<sup>7</sup>"),
        ("m<sup>2</sup>", "m<sup>3</sup>"),
        ("R<sup>2</sup>", "R<sup>3</sup>"),
        ("H<sub>2</sub>O", "H<sub>3</sub>O"),
        ("H<sub>2</sub>O", "HO<sub>2</sub>"),
        ("x<sub>1</sub>, y<sub>2</sub>", "x<sub>2</sub>和y<sub>1</sub>"),
        ("x<sub>1</sub>, x<sub>1</sub>", "x<sub>1</sub>和x<sub>i</sub>"),
    ],
)
def test_script_anchors_reject_value_and_base_changes(source, target):
    assert script_problems(source, target) == []
    assert script_value_problems(source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("10<sup><b>−６</b></sup>", "10<sup><b>－6</b></sup>"),
        ("<i>m</i><sup>+２</sup>", "<i>m</i><sup>2</sup>"),
        ("x<sup>−2.5</sup>", "x<sup>–２.５</sup>"),
        ("x<sub>1</sub>, y<sub>2</sub>", "y<sub>2</sub>和x<sub>1</sub>"),
        ("Fe<sub>2</sub>, Li<sub>3</sub>, CO<sub>4</sub>", "CO<sub>４</sub>、Li<sub>3</sub>和Fe<sub>2</sub>"),
        ("Layer<sub>1</sub>", "层<sub>１</sub>"),
        ("Smith<sup>1</sup>, 2020<sup>2</sup>", "史密斯<sup>2</sup>，2020<sup>3</sup>"),
        ("Doe<sup>∗</sup> and n<sup>2</sup>", "作者<sup>*</sup>和n<sup>3</sup>"),
        ("Use x<sub>steady_state</sub> and x<sub>i,j</sub>.", "用x<sub>稳态</sub>和x<sub>j,i</sub>。"),
        ("Two with a ratio of 0.05", "2个，比例为5%"),
    ],
)
def test_script_anchors_accept_normalization_reordering_and_unlocked_footnotes(source, target):
    assert script_problems(source, target) == []
    assert script_value_problems(source, target) == []


@pytest.mark.parametrize(
    "base", ["m", "cm", "mm", "km", "s", "ms", "ns", "Hz", "kHz", "MHz", "GHz", "kg", "g", "mg", "K", "Pa", "R"]
)
def test_script_anchors_cover_every_unit_base(base):
    assert script_value_problems(f"{base}<sup>2</sup>", f"{base}<sup>3</sup>")


@pytest.mark.parametrize("tag", ["b", "i"])
def test_emphasis_allows_split_and_merge_but_guards_coverage_and_structure(tag):
    check = bold_problems if tag == "b" else italic_problems

    def tag_text(text):
        return f"<{tag}>{text}</{tag}>"

    original = f"Two external words {tag_text('part one')}."
    merged = f"部分{tag_text('一与二')}。"
    assert check(original, f"外部{tag_text('一')}及{tag_text('二')}。") == []
    assert check(f"Two external words {tag_text('one')} and {tag_text('two')}.", merged) == []
    assert "覆盖了整段" in check(original, tag_text("全包") + "。")[0]
    assert "覆盖了整段" in check(original, f"（{tag_text('甲')}，{tag_text('乙')}）。")[0]
    assert "覆盖了整段" in check(f"<m1/> <m2/> {tag_text('x')}", tag_text("甲<m1/><m2/>"))[0]
    assert "自我嵌套" in check(original, tag_text("甲" + tag_text("乙")))[0]
    assert "没有内容" in check(original, tag_text(" \n "))[0]
    assert "丢失" in check(original, "段落。")[0]
    assert "配对" in check(original, f"<{tag}>段落")[0]
    assert "新增" in check("A plain paragraph.", tag_text("段落"))[0]
    assert check(f"An {tag_text('important')} note.", tag_text("重要提示") + "。") == []
    assert check(original, f"{tag_text('局部')}<m1/>。") == []
    assert rich_text_problems(tag_text(tag_text("编辑内容"))) == []


@pytest.mark.parametrize("tag", ["sup", "sub"])
def test_script_markup_keeps_strict_pair_count(tag):
    original = f"x<{tag}>i</{tag}>"
    assert script_problems(original, "x")
    assert script_problems(original, original * 2)
    assert script_problems(original, f"x<{tag}> </{tag}>")
    assert script_problems(original, f"x<{tag}>i</sup>" if tag == "sub" else f"x<{tag}>i</sub>")


def test_numbered_marker_stops_complete_and_partial_responses_and_can_be_omitted():
    assert parse_numbered("1|甲\n2|乙\n </translations> \n2|尾注", batch_size=2) == {1: "甲", 2: "乙"}
    assert parse_numbered("1|甲\n</translations>\n2|尾注", batch_size=2) == {1: "甲"}
    assert parse_numbered("</translations>\n1|尾注", batch_size=2) == {}
    assert parse_numbered("1|甲\n</translations>\n1|尾注") == {1: "甲"}
    assert parse_numbered("1|甲\n2｜乙", batch_size=2) == {1: "甲", 2: "乙"}


def test_spaced_pipe_continuation_needs_a_previous_row_and_source_evidence():
    assert parse_numbered("1|算式\n3 | 条件\n2|结果", batch_size=2, sources=["x | y", "z"]) == {
        1: "算式3 | 条件",
        2: "结果",
    }
    assert parse_numbered("1|A\n1 ｜ B\n0 | C", batch_size=1, sources=["x｜y"]) == {1: "A 1 ｜ B 0 | C"}
    assert parse_numbered("1|甲\n3 | 第三段", batch_size=3, sources=["x | y", "z", "u"]) == {1: "甲", 3: "第三段"}
    assert parse_numbered("1|甲\n2|3", batch_size=2, sources=["x | y", "z"]) == {1: "甲", 2: "3"}
    assert parse_numbered("2|乙\n1 | 条件", batch_size=2, sources=["x", "x | y"]) == {2: "乙1 | 条件"}


@pytest.mark.parametrize(
    ("response", "sources", "reason"),
    [
        ("3 | 条件", ["x | y", "z"], "越界"),
        ("1|甲\n3 | 条件", None, "越界"),
        ("1|甲\n3 | 条件", ["x", "x | y"], "越界"),
        ("1|甲\n1|乙", ["x | y", "z"], "重复"),
        ("1|甲\n1| 乙", ["x | y", "z"], "重复"),
        ("1|甲\n1 |乙", ["x | y", "z"], "重复"),
        ("2|乙\n1 | 甲", ["x | y", "z"], "乱序"),
        ("1|甲\n1234567890123 | 条件", ["x | y", "z"], "越界"),
    ],
)
def test_spaced_pipe_does_not_weaken_other_number_errors(response, sources, reason):
    with pytest.raises(NumberedResponseError, match=reason):
        parse_numbered(response, batch_size=2, sources=sources)


def test_chapter_split_starts_at_title_and_combines_title_with_body():
    items = list("abcdefgh")
    sections = ["第一章", "", "第二章", "", "第三章", "第三章", "第三章", "第三章"]
    roles = [
        BlockType.TEXT,
        BlockType.TITLE,
        BlockType.TEXT,
        None,
        None,
        BlockType.TEXT,
        BlockType.TEXT,
        BlockType.TEXT,
    ]
    assert make_batches(items, [2, 1, 2, 1, 1, 9, 1, 1], 5, sections=sections, roles=roles) == [
        ["a"], ["b", "c", "d"], ["e"], ["f"], ["g", "h"],
    ]  # fmt: skip


def test_prompt_v5_and_scopes_include_only_their_own_paragraph_numbers():
    system = system_prompt(DocContext(terms={"model": "模型"}))
    assert "用户术语 > 本文术语表 > 参考词库" in system
    assert "因此自注意力可以大幅提高并行度。" in system
    assert "样本<m2/>的权重<m1/>固定。" in system
    assert system.splitlines()[system.splitlines().index("8|样本<m2/>的权重<m1/>固定。") + 1] == "</translations>"
    assert "model=模型" in system
    prompt = user_prompt(
        [(1, "标题"), (2, "文本"), (3, "图例")],
        [],
        {},
        scoped_meta=[
            SegmentPromptMeta(1, "A", BlockType.TITLE, user_terms={"token": "词元"}),
            SegmentPromptMeta(2, "A", BlockType.TEXT, "A nearby sentence.", reference_terms={"model": "模型"}),
            SegmentPromptMeta(3, "B", BlockType.FIGURE_TEXT, user_terms={"token": "词元", "net": "网络"}),
        ],
    )
    assert prompt.splitlines() == [
        "第1段章节：A",
        "第2段章节：A",
        "第3段章节：B",
        "段落类型：第1段=标题；第3段=图中标签",
        "第2段前文（仅供理解指代，勿翻译）：A nearby sentence.",
        "第1、3段用户术语（统一使用）：token=词元",
        "第3段用户术语（统一使用）：net=网络",
        "第2段参考词库（仅在语境与论文领域吻合时参考）：model=模型",
        "1|标题",
        "2|文本",
        "3|图例",
    ]


class _Cache:
    def __init__(self):
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def put(self, key: str, target: str) -> None:
        self.data[key] = target

    def purge_older_than(self, hours: int) -> int:
        return 0


class _Chat:
    model = "test-model"

    def __init__(self, *results: ChatResult, provider: str = ""):
        self.calls: list[tuple[str, str]] = []
        self.results = results
        self.provider_id = provider

    async def list_models(self) -> list[str]:
        return [self.model]

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        self.calls.append((system, user))
        index = len(self.calls) - 1
        if index < len(self.results):
            return self.results[index]
        rows = re.findall(r"^(\d+)\|", user, re.M)
        return ChatResult(text="\n".join(f"{number}|译文{number}。" for number in rows))


def _segment(
    index: int, source: str, *, section: str = "Method", role: BlockType | None = BlockType.TEXT, page: int = 0
) -> Segment:
    return Segment(
        id=f"s{index}", job_id="job", block_ids=[f"p{page}-b{index}"], source=source, section=section, role=role
    )


def test_context_uses_full_document_and_is_stable_across_retries_batches_and_cache_hits():
    other = _segment(0, "A nearby observation.")
    before = _segment(1, "Earlier sentence. <b>Dr.</b> Kim used\nEq. 3.14 and <m1/>.", page=1).model_copy(
        update={"status": SegmentStatus.EDITED, "block_ids": ["p0-b1", "p1-b1"]}
    )
    current = _segment(2, "<i>This</i> uses [12].", page=2)
    answer = ChatResult(text="1|<i>这</i>使用[12]。")
    chat = _Chat(ChatResult(text="1|另一段。\n2|<i>这</i>使用[21]。"), answer)
    cache = _Cache()
    segments, report = asyncio.run(
        translate([other, current], chat=chat, cache=cache, ctx=DocContext(), context_segments=[other, before, current])
    )
    assert report.requests == 2 and segments[1].target == "<i>这</i>使用[12]。"
    for number, (_, user) in zip((2, 1), chat.calls, strict=True):
        assert f"第{number}段前文（仅供理解指代，勿翻译）：Dr. Kim used Eq. 3.14 and [公式]." in user
        assert "Earlier sentence" not in user
    retry = chat.calls[1][1]
    assert "缺少引文 [12]" in retry and "多出引文 [21]" in retry

    next_seg = _segment(3, "Here is another paragraph.", page=2)
    next_chat = _Chat()
    _, report = asyncio.run(
        translate([current, next_seg], chat=next_chat, cache=cache, ctx=DocContext(),
                  context_segments=[before, current, next_seg])
    )  # fmt: skip
    assert report.cached == 1 and report.requests == 1
    assert "1|Here is another paragraph." in next_chat.calls[0][1] and current.source not in next_chat.calls[0][1]


@pytest.mark.parametrize(
    ("before_update", "current_update"),
    [
        ({"role": BlockType.TITLE}, {}),
        ({"role": BlockType.CAPTION}, {}),
        ({}, {"role": None}),
        ({"section": ""}, {}),
        ({}, {"section": "Other"}),
        ({"block_ids": []}, {}),
        ({}, {"block_ids": ["invalid"]}),
        ({}, {"block_ids": ["p2-b2"]}),
        ({"block_ids": ["p1-b1"]}, {}),
        ({}, {"source": "We use this procedure."}),
    ],
)
def test_context_requires_both_roles_section_adjacent_pages_and_clear_intro(before_update, current_update):
    before = _segment(1, "A previous observation.").model_copy(update=before_update)
    current = _segment(2, "These are our results.").model_copy(update=current_update)
    chat = _Chat()
    asyncio.run(translate([current], chat=chat, cache=_Cache(), ctx=DocContext(), context_segments=[before, current]))
    assert "前文" not in chat.calls[0][1]


@pytest.mark.parametrize("opening", ["However, it", "The former", "Their approach", "Such data"])
def test_context_keeps_last_240_codepoints_and_uses_default_segment_order(opening):
    sentence = "𠀀" * 240 + "。"
    before = _segment(1, f"Previous sentence. {sentence}").model_copy(update={"status": SegmentStatus.DONE})
    current = _segment(2, f"{opening} still holds.")
    chat = _Chat()
    asyncio.run(translate([before, current], chat=chat, cache=_Cache(), ctx=DocContext()))
    preview = next(line for line in chat.calls[0][1].splitlines() if "第1段前文" in line).partition("：")[2]
    assert preview == "…" + sentence[-239:] and len(preview) == 240


def test_lookup_scopes_remove_only_equivalent_paper_terms_and_keep_arbitrated_library_hits():
    segs = [_segment(1, "Alpha"), _segment(2, "Beta")]
    ctx = DocContext(terms={"policy-gradient": "策略梯度", "model": "模型", "ADAM": "优化器"})
    chat = _Chat()

    def hits(source):
        if source == "Alpha":
            return {"policy gradients": "策略梯度", "model": "建模", "Adam": "亚当"}, {"paper": "论文"}, {"net": "网络"}
        return {"model": "范型"}, {}, {"table": "表"}

    def unused(source):
        pytest.fail(f"旧查词接口被调用：{source}")

    asyncio.run(
        translate(segs, chat=chat, cache=_Cache(), ctx=ctx, lookup_term_hits=hits,
                  lookup_user_terms=unused, lookup_terms=unused)
    )  # fmt: skip
    system, user = chat.calls[0]
    assert all(f"{key}={value}" in system for key, value in ctx.terms.items())
    assert "policy gradients=" not in user and "paper=" not in user
    assert "本批所在章节：Method" in user
    assert "第1段用户术语（统一使用）：Adam=亚当；model=建模" in user
    assert "第2段用户术语（统一使用）：model=范型" in user
    assert "第1段参考词库（仅在语境与论文领域吻合时参考）：net=网络" in user
    assert "第2段参考词库（仅在语境与论文领域吻合时参考）：table=表" in user


def test_prompt_budget_counts_expanded_formula_scoped_terms_role_and_context(caplog):
    first = _segment(1, "A previous sentence.")
    second = _segment(2, "This uses <m1/>.").model_copy(update={"formulas": {"m1": "x" * 40}})
    third = _segment(3, "Diagram", role=BlockType.CAPTION)
    chat = _Chat(ChatResult(text="1|见<m1/>。"), ChatResult(text="1|图注。"))

    def hits(source):
        return ({"term": "译" * 40}, {}, {}) if source == second.source else ({}, {}, {})

    with caplog.at_level("DEBUG", logger="pdftr.translation.service"):
        _, report = asyncio.run(
            translate(
                [second, third],
                chat=chat,
                cache=_Cache(),
                ctx=DocContext(),
                context_segments=[first, second, third],
                lookup_term_hits=hits,
                opts=TranslateOptions(batch_tokens=70),
            )
        )
    assert report.requests == 2 and report.failed == 0
    assert "<m1>" + "x" * 40 + "</m1>" in chat.calls[0][1]
    assert "第1段前文" in chat.calls[0][1] and "第1段用户术语" in chat.calls[0][1]
    assert "段落类型：第1段=图注" in chat.calls[1][1]
    assert all(int(line.rsplit("=", 1)[1]) > 70 for line in caplog.messages if "估算输入 token" in line)
    assert sum("估算输入 token" in line for line in caplog.messages) == 2


def test_cache_key_is_deterministic_and_covers_semantic_inputs():
    seg = _segment(1, "A paragraph.")
    version = cache_key(seg, "m", {"b": "乙", "a": "甲"}, "default", reference_terms={"y": "乙", "x": "甲"})
    assert version == cache_key(seg, "m", {"a": "甲", "b": "乙"}, "default", reference_terms={"x": "甲", "y": "乙"})
    assert VALIDATION_VERSION == 4
    default = cache_key(seg, "m", {}, "default")
    keys = [
        default,
        cache_key(seg, "m", {}, "default", domain="领域"),
        cache_key(seg, "m", {}, "default", section="章"),
        cache_key(seg, "m", {}, "default", provider="openai:generic:https://test"),
        cache_key(seg, "m", {}, "default", provider="openai:deepseek:https://test"),
        cache_key(seg, "m", {}, "default", validation_version=1),
        cache_key(seg, "m", {}, "default", role="text"),
        cache_key(seg, "m", {}, "default", title="题目"),
        cache_key(seg, "m", {}, "default", abstract="摘要"),
        cache_key(seg, "m", {}, "default", prev_context="前一句。"),
        cache_key(seg, "m", {}, "default", user_terms={"a": "甲"}),
        cache_key(seg, "m", {}, "default", reference_terms={"a": "甲"}),
        cache_key(seg, "m", {"a": "甲"}, "default"),
    ]
    assert len(set(keys)) == len(keys)
    assert default != cache_key(seg.model_copy(update={"formulas": {"m1": "x"}}), "m", {}, "default")
    assert default != cache_key(seg, "m", {}, "careful")
    assert default != cache_key(seg, "different", {}, "default")


def test_dedup_cache_separates_identical_text_with_different_previous_sentence_or_role():
    source = "This repeats."
    previous_a = _segment(1, "Prior observation A.", page=1)
    current_a = _segment(2, source, page=1)
    previous_b = _segment(3, "Prior observation B.", page=1)
    current_b = _segment(4, source, page=1)
    current_c = _segment(5, source, role=BlockType.FOOTNOTE, page=1)
    chat, cache = _Chat(), _Cache()
    out, report = asyncio.run(
        translate([current_a, current_b, current_c], chat=chat, cache=cache, ctx=DocContext(),
                  context_segments=[previous_a, current_a, previous_b, current_b, current_c])
    )  # fmt: skip
    assert report.requests == 1 and report.translated == len(cache.data) == 3
    assert len({s.target for s in out}) == 3
    user = chat.calls[0][1]
    assert "第1段前文（仅供理解指代，勿翻译）：Prior observation A." in user
    assert "第2段前文（仅供理解指代，勿翻译）：Prior observation B." in user
    assert "第3段前文" not in user and "段落类型：第3段=脚注" in user


@pytest.mark.parametrize(("compatibility_fallback", "use_cache"), [(True, True), (False, False), (False, True)])
def test_cache_write_policy_applies_to_successful_single_segment_retry(compatibility_fallback, use_cache):
    seg = _segment(1, "Use H<sub>2</sub>O.", section="", role=None)
    cache = _Cache()
    key = cache_key(seg, _Chat.model, {}, "default")
    cache.data[key] = "使用H<sub>3</sub>O。"
    chat = _Chat(
        ChatResult(text="1|使用HO<sub>2</sub>。"),
        ChatResult(text="1|使用H<sub>2</sub>O。", compatibility_fallback=compatibility_fallback),
    )
    out, report = asyncio.run(
        translate([seg], chat=chat, cache=cache, ctx=DocContext(), opts=TranslateOptions(use_cache=use_cache))
    )
    assert report.cached == 0 and report.requests == 2 and out[0].target == "使用H<sub>2</sub>O。"
    assert "下标数值" in chat.calls[1][1]
    assert cache.data[key] == (
        "使用H<sub>2</sub>O。" if use_cache and not compatibility_fallback else "使用H<sub>3</sub>O。"
    )


def test_early_response_end_retries_only_missing_segment():
    segs = [_segment(1, "First."), _segment(2, "Second.")]
    chat = _Chat(ChatResult(text="1|甲\n</translations>\n2|尾注"), ChatResult(text="1|乙"))
    out, report = asyncio.run(translate(segs, chat=chat, cache=_Cache(), ctx=DocContext()))
    assert [seg.target for seg in out] == ["甲", "乙"]
    assert report.attempts == {"s1": 1, "s2": 2}


def test_first_batch_uses_compatible_fallback_without_writing_shared_cache():
    segs = [_segment(1, "First."), _segment(2, "Second.")]
    chat = _Chat(ChatResult(text="1|甲\n2|乙", compatibility_fallback=True))
    cache = _Cache()
    out, report = asyncio.run(translate(segs, chat=chat, cache=cache, ctx=DocContext()))
    assert [seg.target for seg in out] == ["甲", "乙"]
    assert report.requests == 1 and report.translated == 2 and not cache.data


def test_first_batch_passes_source_pipe_evidence_to_number_parser():
    segs = [_segment(1, "Conditional x | y."), _segment(2, "Second.")]
    chat = _Chat(ChatResult(text="1|条件\n3 ｜ 取值\n2|第二段\n</translations>\n1|尾注"))
    out, report = asyncio.run(translate(segs, chat=chat, cache=_Cache(), ctx=DocContext()))
    assert [seg.target for seg in out] == ["条件3 ｜ 取值", "第二段"]
    assert report.requests == 1 and report.failed == 0
