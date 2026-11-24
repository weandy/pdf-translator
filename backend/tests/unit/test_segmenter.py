import pytest

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType, Document, InlineFormula, Page, Span
from pdftr.translation.segmenter import _join, front_matter_ids, segment, textual_formula

BOX = Rect.of([0, 0, 100, 10])
STYLE = BlockStyle(font_size=10)


def _text(t: str, line: int = 0) -> Span:
    return Span(text=t, line=line, bbox=BOX, size=10)


def _formula(key: str, line: int = 0) -> Span:
    return Span(kind="formula", formula=key, line=line, bbox=BOX, size=10)


def _block(bid: str, page: int, btype: BlockType, spans: list[Span], **kw) -> Block:
    return Block(
        id=bid, page=page, type=btype, bbox=BOX, order=int(bid.split("-b")[1]), spans=spans, style=STYLE,
        translatable=kw.pop("translatable", btype in (BlockType.TEXT, BlockType.TITLE)), **kw,
    )  # fmt: skip


@pytest.mark.parametrize("heading", ["I. INTRODUCTION", "1. INTRODUCTION", "H I G H L I G H T S"])
def test_front_matter_stops_at_numbered_heading_or_elsevier_highlights(heading):
    blocks = [
        _block("p0-b0", 0, BlockType.TITLE, [_text("A Synthetic Article")]),
        _block("p0-b1", 0, BlockType.TEXT, [_text("A. Testwriter, Example Institute")]),
        _block("p0-b2", 0, BlockType.TITLE, [_text(heading)]),
        _block("p0-b3", 0, BlockType.TEXT, [_text("A paragraph that should be translated.")]),
    ]
    doc = Document(job_id="synthetic", pages=[Page(index=0, width=600, height=800, blocks=blocks)])
    assert front_matter_ids(doc) == {"p0-b1"}


def test_unfinished_reference_fragment_is_text_and_math_group_is_preserved():
    assert textual_formula(r"\left(\operatorname{Eq}.\right.") == "(Eq."
    assert textual_formula(r"\left(\operatorname{rank}.\right.") is None


def _doc() -> Document:
    p0 = [
        _block("p0-b0", 0, BlockType.TITLE, [_text("1 Introduction")]),
        _block(
            "p0-b1",
            0,
            BlockType.TEXT,
            [_text("Let"), _formula("m1"), _text("be a representa-"), _text("tion that is", line=1)],
            formulas=[InlineFormula(key="m1", latex="f(x)", bbox=BOX)],
        ),
        _block("p0-b2", 0, BlockType.TEXT, [_text("42")]),  # 纯数字：不翻
        _block("p0-b3", 0, BlockType.REFERENCE, [_text("[1] A. Author. Some paper.")], translatable=False),
    ]
    p1 = [
        _block(
            "p1-b0",
            1,
            BlockType.TEXT,
            [_text("left-to-"), _text("right with", line=1), _formula("m1", line=1)],
            formulas=[InlineFormula(key="m1", latex="g(y)", bbox=BOX)],
            continues_from="p0-b1",
        ),
        _block("p1-b1", 1, BlockType.TEXT, [_text("Another short paragraph.")]),
    ]
    return Document(job_id="0123456789abcdef", pages=[Page(index=0, width=600, height=800, blocks=p0),
                                                      Page(index=1, width=600, height=800, blocks=p1)])  # fmt: skip


def test_duplicate_block_id_has_one_segment_part_across_continuation_chains() -> None:
    left = _block("p0-b0", 0, BlockType.TEXT, [_text("Left head.")])
    right = _block("p0-b1", 0, BlockType.TEXT, [_text("Right head.")])
    tail = _block("p0-b2", 0, BlockType.TEXT, [_text("Shared tail.")], continues_from=left.id)
    repeated = tail.model_copy(update={"continues_from": right.id})
    sibling = _block("p0-b3", 0, BlockType.TEXT, [_text("Distinct branch.")], continues_from=right.id)
    blocks = [left, right, tail, sibling, repeated, left]
    doc = Document(job_id="synthetic", pages=[Page(index=0, width=600, height=800, blocks=blocks)])
    segments = segment(doc)
    part_ids = [part.block_id for seg in segments for part in seg.parts]
    assert sorted(part_ids) == sorted({b.id for b in blocks})
    assert all(seg.block_ids == [part.block_id for part in seg.parts] for seg in segments)
    assert sum(s.source.count("Shared tail.") for s in segments) == 1


def test_join_handles_line_end_hyphens():
    assert _join("a representa-", "tion") == "a representation"
    assert _join("left-to-", "right") == "left-to-right"  # 复合词保留连字符
    assert _join("pre-", "Training") == "pre- Training"  # 下一行大写开头：不是断词
    assert _join("", "x") == "x"


@pytest.mark.parametrize(
    ("prev", "nxt", "expected"),
    [
        ("<b>repre\u00ad</b>", "<b>sentation</b>", "<b>representation</b>"),
        ("off\u00ad", "policy", "offpolicy"),
        ("https://example.invalid/real-", "time", "https://example.invalid/real- time"),
        ("term--", "end", "term-- end"),
        ("term−", "end", "term− end"),
        ("term–", "end", "term– end"),
        ("term—", "end", "term— end"),
        ("term-", "échelle", "term- échelle"),
        ("Q-", "learning", "Q-learning"),
        ("2-", "stage", "2-stage"),
        ("3D-", "printed", "3D-printed"),
        ("L1-", "norm", "L1-norm"),
        ("off-", "policy", "off-policy"),
        ("well-", "defined", "well-defined"),
        ("quasi-", "static", "quasi-static"),
        ("well-", "ness", "wellness"),
        ("self-", "ish", "selfish"),
        ("cross-", "ing", "crossing"),
        ("pseudo-", "er", "pseudoer"),
        ("non-", "linear", "nonlinear"),
        ("multi-", "ple", "multiple"),
        ("meta-", "bolic", "metabolic"),
        ("real-", "ize", "realize"),
        ("end-", "ure", "endure"),
        ("fine-", "st", "finest"),
        ("<i>model-</i>", "<m1/>", "<i>model-</i> <m1/>"),
    ],
)
def test_join_compound_and_boundary_rules(prev, nxt, expected):
    assert _join(prev, nxt) == expected


def test_join_prefers_hyphen_evidence_and_respects_joined_evidence():
    assert _join("non-", "linear", hyphenated={"non-linear"}, joined={"nonlinear"}) == "non-linear"
    assert _join("off-", "policy", joined={"offpolicy"}) == "offpolicy"
    assert _join("Q-", "learning", joined={"qlearning"}) == "Q-learning"


def test_full_document_inline_evidence_for_visual_and_block_breaks():
    head = _block("p0-b0", 0, BlockType.TEXT, [_text("Apply non-"), _text("linear and semi-", 1)])
    tail = _block(
        "p0-b1", 0, BlockType.FOOTNOTE, [_text("conductor methods")], continues_from=head.id, translatable=True
    )
    split = _block("p0-b2", 0, BlockType.TEXT, [_text("Avoid real-"), _text("time rates", 1)])
    partial = _block("p0-b3", 0, BlockType.TEXT, [_text("Use real-time-"), _text("based methods", 1)])
    evidence = _block("p1-b0", 1, BlockType.TEXT, [_text("NON-LINEAR semi-conductor nonlinear")])
    doc = Document(
        job_id="synthetic",
        pages=[
            Page(index=0, width=600, height=800, blocks=[head, tail, split, partial]),
            Page(index=1, width=600, height=800, blocks=[evidence]),
        ],
    )
    first, second, third = segment(doc, pages=[0])
    assert first.source == "Apply non-linear and semi-conductor methods"
    assert first.role is BlockType.TEXT and first.block_ids == [head.id, tail.id]
    assert second.source == "Avoid realtime rates"
    assert third.source == "Use real-time-based methods"


def test_chain_merge_and_formula_renumbering():
    segs = segment(_doc())
    assert [s.block_ids for s in segs] == [["p0-b0"], ["p0-b1", "p1-b0"], ["p1-b1"]]
    chain = segs[1]
    assert chain.source == "Let <m1/> be a representation that is left-to-right with <m2/>"
    assert chain.formulas == {"m1": "f(x)", "m2": "g(y)"}
    assert [(p.block_id, p.chars > 0) for p in chain.parts] == [("p0-b1", True), ("p1-b0", True)]
    assert {k: r.page for k, r in chain.formula_refs.items()} == {"m1": 0, "m2": 1}
    assert chain.section == "1 Introduction"
    assert segs[0].role is BlockType.TITLE and chain.role is BlockType.TEXT
    assert segs[0].section == ""  # 标题本身不带章节上下文
    assert segs[1].id == "0123456789abcdef-s1"


def test_references_and_page_filter():
    with_refs = segment(_doc(), include_references=True)
    assert any(s.block_ids == ["p0-b3"] for s in with_refs)
    only_page1 = segment(_doc(), pages=[1])
    assert [s.block_ids for s in only_page1] == [["p1-b0"], ["p1-b1"]]
    assert only_page1[0].formulas == {"m1": "g(y)"}
    assert [s.block_ids for s in segment(_doc(), pages=[0])] == [["p0-b0"], ["p0-b1"]]
    doc = _doc()
    tail = _block("p2-b0", 2, BlockType.TEXT, [_text("The last part")], continues_from="p1-b0")
    doc.pages.append(Page(index=2, width=600, height=800, blocks=[tail]))
    assert [s.block_ids for s in segment(doc, pages=[0, 2])] == [["p0-b0"], ["p0-b1"], ["p2-b0"]]


def test_ordinal_formulas_become_text():
    assert textual_formula(r"1 ^ { \mathrm { s t } }") == "1st"
    assert textual_formula(r"2^{nd}") == "2nd"
    assert textual_formula("g") is None
    assert textual_formula(r"\beta_1") is None

    block = _block(
        "p0-b0",
        0,
        BlockType.TEXT,
        [_text("estimates of the"), _formula("m1"), _text("moment and"), _formula("m2")],
        formulas=[
            InlineFormula(key="m1", latex=r"1 ^ { \mathrm { s t } }", bbox=BOX),
            InlineFormula(key="m2", latex=r"\beta_1", bbox=BOX),
        ],
    )
    doc = Document(job_id="0123456789abcdef", pages=[Page(index=0, width=600, height=800, blocks=[block])])
    (seg,) = segment(doc)
    assert seg.source == "estimates of the 1st moment and <m1/>"
    assert seg.formulas == {"m1": r"\beta_1"}  # 序数不占公式编号


@pytest.mark.parametrize(
    ("latex", "expected"),
    [
        ("11^{th}", "11th"), ("112^{th}", "112th"), ("113^{th}", "113th"), ("121^{st}", "121st"),
        ("22^{nd}", "22nd"), ("23^{rd}", "23rd"), ("20^{th}", "20th"),
        ("2^{st}", None), ("1^{rd}", None), ("11^{st}", None), ("112^{nd}", None), ("113^{rd}", None),
    ],
)  # fmt: skip
def test_ordinal_english_suffix_must_agree_with_digits(latex, expected):
    assert textual_formula(latex) == expected


@pytest.mark.parametrize("operator", "=+-*/<>≤≥≈×·")
def test_ordinal_stays_math_next_to_an_operator_on_either_side(operator):
    assert textual_formula("2^{nd}", before=f"<i>x {operator}</i> ") is None
    assert textual_formula("2^{nd}", after=f" <b>{operator} x</b>") is None


def test_following_operator_preserves_formula_in_segment():
    spans = [_text("Use"), _formula("m1"), _text("<i> </i>"), _text("≤ x, then the"), _formula("m2"), _text("moment")]
    block = _block(
        "p0-b0",
        0,
        BlockType.TEXT,
        spans,
        formulas=[InlineFormula(key="m1", latex="2^{nd}", bbox=BOX), InlineFormula(key="m2", latex="3^{rd}", bbox=BOX)],
    )
    doc = Document(job_id="synthetic", pages=[Page(index=0, width=600, height=800, blocks=[block])])
    (seg,) = segment(doc)
    assert seg.source == "Use <m1/> <i> </i> ≤ x, then the 3rd moment"
    assert seg.formulas == {"m1": "2^{nd}"}


def test_front_matter_between_title_and_abstract_is_kept_original():
    def at(bid: str, btype: BlockType, text: str, y: float) -> Block:
        b = _block(bid, 0, btype, [_text(text)])
        return b.model_copy(update={"bbox": Rect.of([0, y, 100, y + 10])})

    blocks = [
        at("p0-b0", BlockType.TITLE, "Attention Is All You Need", 50),
        at("p0-b1", BlockType.TEXT, "Ashish Vaswani Google Brain avaswani@google.com", 100),
        at("p0-b2", BlockType.TITLE, "Abstract", 200),
        at("p0-b3", BlockType.TEXT, "The dominant sequence transduction models are based on networks.", 220),
    ]
    blocks[-1].continues_from = "p0-b1"
    doc = Document(job_id="0123456789abcdef", pages=[Page(index=0, width=600, height=800, blocks=blocks)])
    assert [s.block_ids for s in segment(doc)] == [["p0-b0"], ["p0-b2"], ["p0-b3"]]


def test_front_matter_uses_structural_heading_with_lowest_order():
    blocks = [
        _block("p0-b4", 0, BlockType.TITLE, [_text("Abstract")]),
        _block("p0-b3", 0, BlockType.TEXT, [_text("First highlight.")]),
        _block("p0-b0", 0, BlockType.TITLE, [_text("A Synthetic Study")]),
        _block("p0-b1", 0, BlockType.TEXT, [_text("Jane Example, Local Institute")]),
        _block("p0-b2", 0, BlockType.TITLE, [_text("H I G H L I G H T S")]),
    ]
    doc = Document(job_id="synthetic", pages=[Page(index=0, width=600, height=800, blocks=blocks)])
    assert front_matter_ids(doc) == {"p0-b1"}
    assert any(seg.source == "First highlight." for seg in segment(doc))


def test_nontranslatable_block_breaks_continuation_chain():
    doc = _doc()
    first, tail = doc.pages[1].blocks
    first.translatable = False
    tail.continues_from = first.id
    assert [s.block_ids for s in segment(doc, pages=[0, 1])] == [["p0-b0"], ["p0-b1"], ["p1-b1"]]


def test_figure_labels_follow_option():
    label = _block("p0-b1", 0, BlockType.FIGURE_TEXT, [_text("Training loss")], translatable=True)
    doc = Document(job_id="0123456789abcdef", pages=[Page(index=0, width=600, height=800, blocks=[label])])
    assert [s.source for s in segment(doc)] == ["Training loss"]
    assert segment(doc, include_figures=False) == []


def test_italic_runs_merge_across_lines_and_hyphenation():
    assert _join("<i>the first</i>", "<i>line</i>") == "<i>the first line</i>"
    assert _join("<b><i>represen-</i></b>", "<b><i>tation</i></b>") == "<b><i>representation</i></b>"
    assert _join("<i><b>represen-</b></i>", "<i><b>tation</b></i>") == "<i><b>representation</b></i>"

    def italic(text: str, line: int = 0) -> Span:
        return _text(text, line).model_copy(update={"italic": True})

    whole = _block("p0-b0", 0, BlockType.TEXT, [italic("The first"), italic("statement", 1)])
    whole.style = BlockStyle(font_size=10, italic=True)
    mixed = _block("p0-b1", 0, BlockType.TEXT, [_text("The"), italic("second"), italic("statement", 1)])
    title = _block("p0-b2", 0, BlockType.TITLE, [italic("A title")])
    doc = Document(job_id="synthetic", pages=[Page(index=0, width=600, height=800, blocks=[whole, mixed, title])])
    assert [s.source for s in segment(doc)] == ["The first statement", "The <i>second statement</i>", "A title"]


def test_recover_numeric_references_and_units_without_touching_math_variables():
    assert textual_formula(r"^{6,}") == "6,"
    assert textual_formula(r"^{3,}") == "3,"
    assert textual_formula(r"\operatorname{Eq.}(24)") == "Eq. (24)"
    assert textual_formula(r"\mathrm{Eq. (24)}") == "Eq. (24)"
    assert textual_formula(r"\mathrm{Fig.} 6") == "Fig. 6"
    assert textual_formula(r"\mathrm{ms}", "1.8 ") == "ms"
    assert textual_formula(r"\mathbf{s},", "1800 ") == "s,"
    assert textual_formula(r"\mathbf{s},", "the symbol ") is None
    for math in ("n,", "s,", "t,", r"\pi,", "s_t", "2^{3,}"):
        assert textual_formula(math, "1800 ") is None

    latex = [r"^{6,}", r"^{3,}", r"\operatorname{Eq.}(24)", r"\mathbf{s},", "n,", "s_t"]
    prose = ["See Fig.", "Section", "and", "for 1800", "with", "and"]
    formulas = [InlineFormula(key=f"m{i}", latex=value, bbox=BOX) for i, value in enumerate(latex, 1)]
    spans = [s for text, formula in zip(prose, formulas, strict=True) for s in (_text(text), _formula(formula.key))]
    block = _block("p0-b0", 0, BlockType.TEXT, spans, formulas=formulas)
    doc = Document(job_id="synthetic", pages=[Page(index=0, width=600, height=800, blocks=[block])])
    (seg,) = segment(doc)
    assert seg.source == "See Fig. 6, Section 3, and Eq. (24) for 1800 s, with <m1/> and <m2/>"
    assert seg.formulas == {"m1": "n,", "m2": "s_t"}


@pytest.mark.parametrize(
    ("latex", "before", "expected"),
    [
        (r"\operatorname { E q . } \left( 2 4 \right)", "defined as ", "Eq. (24)"),
        (r"{ \mathbf { s } } ,", "set to 1800 ", "s,"),
        (r"^ { 1 2 , }", "According to Fig. ", "12,"),
        (r"{ \mathbf { s } } ,", "the symbol ", None),
        *[(math, "1800 ", None) for math in ("n ,", "s ,", "t ,", r"\pi ,", "s _ { t }", "2 ^ { 3 , }")],
    ],
)
def test_textual_formula_recovers_spaced_mineru_text_only(latex, before, expected):
    assert textual_formula(latex, before) == expected
