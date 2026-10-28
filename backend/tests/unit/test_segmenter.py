from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType, Document, InlineFormula, Page, Span
from pdftr.translation.segmenter import _join, segment

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


def test_join_handles_line_end_hyphens():
    assert _join("a representa-", "tion") == "a representation"
    assert _join("left-to-", "right") == "left-to-right"  # 复合词保留连字符
    assert _join("pre-", "Training") == "pre- Training"  # 下一行大写开头：不是断词
    assert _join("", "x") == "x"


def test_chain_merge_and_formula_renumbering():
    segs = segment(_doc())
    assert [s.block_ids for s in segs] == [["p0-b0"], ["p0-b1", "p1-b0"], ["p1-b1"]]
    chain = segs[1]
    assert chain.source == "Let <m1/> be a representation that is left-to-right with <m2/>"
    assert chain.formulas == {"m1": "f(x)", "m2": "g(y)"}
    assert [(p.block_id, p.chars > 0) for p in chain.parts] == [("p0-b1", True), ("p1-b0", True)]
    assert {k: r.page for k, r in chain.formula_refs.items()} == {"m1": 0, "m2": 1}
    assert chain.section == "1 Introduction"
    assert segs[0].section == ""  # 标题本身不带章节上下文
    assert segs[1].id == "0123456789abcdef-s1"


def test_references_and_page_filter():
    with_refs = segment(_doc(), include_references=True)
    assert any(s.block_ids == ["p0-b3"] for s in with_refs)
    only_page1 = segment(_doc(), pages=[1])
    assert [s.block_ids for s in only_page1] == [["p1-b1"]]  # 续接块随链头所在页处理


def test_ordinal_formulas_become_text():
    from pdftr.translation.segmenter import textual_formula

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
    doc = Document(job_id="0123456789abcdef", pages=[Page(index=0, width=600, height=800, blocks=blocks)])
    assert [s.block_ids for s in segment(doc)] == [["p0-b0"], ["p0-b2"], ["p0-b3"]]


def test_figure_labels_follow_option():
    label = _block("p0-b1", 0, BlockType.FIGURE_TEXT, [_text("Training loss")], translatable=True)
    doc = Document(job_id="0123456789abcdef", pages=[Page(index=0, width=600, height=800, blocks=[label])])
    assert [s.source for s in segment(doc)] == ["Training loss"]
    assert segment(doc, include_figures=False) == []
