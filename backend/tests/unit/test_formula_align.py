"""块级解析结果（只有块内容、行内公式写成 $LaTeX$）的公式定位。"""

import pytest

from pdftr.domain.chars import Char
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import BlockType
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutSpan
from pdftr.fusion.formula_align import _content_parts, clean_formula_lines, locate_formulas, refine_block


def _chars(lines: list[str], size: float = 10.0) -> list[Char]:
    """把若干行文字铺成 PDF 字符（等宽 5pt），每行一个行号。"""
    out = []
    for n, text in enumerate(lines):
        for i, c in enumerate(text):
            out.append(Char(c=c, bbox=Rect(x0=10 + 5 * i, y0=20 * n, x1=15 + 5 * i, y1=20 * n + size),
                            font="Times", size=size, line=n))  # fmt: skip
    return out


def _text(chars, idx) -> str:
    return "".join(chars[k].c for k in idx)


def test_formula_found_between_anchor_text():
    chars = _chars(["Let f(θ) be a noisy objective."])
    (f,) = locate_formulas("Let $f(\\theta)$ be a noisy objective.", chars)
    assert _text(chars, f.chars) == "f(θ)"


def test_adjacent_formulas_separated_by_comma():
    chars = _chars(["with a, b and c."])
    found = locate_formulas("with $a$, $b$ and $c$.", chars)
    assert [_text(chars, f.chars) for f in found] == ["a", "b", "c"]


def test_formula_at_block_end_does_not_swallow_text():
    # 公式在末尾、后面没有可对齐的文字：只取未被正文占用的字符，正文不会被圈进来
    chars = _chars(["the update rule is", "θt ← θt−1 − α·mt"])
    (f,) = locate_formulas("the update rule is $\\theta_t \\leftarrow \\theta_{t-1} - \\alpha \\cdot m_t$", chars)
    assert _text(chars, f.chars).replace(" ", "") == "θt←θt−1−α·mt"


def test_implausible_span_is_dropped():
    # 识别结果漏了一大段正文：推算出的范围远多于公式本身的字符，放弃位置
    chars = _chars(["We use x here and then a lot of text the parser missed entirely."])
    (f,) = locate_formulas("We use $x$", chars)
    assert f.chars == []


def test_refine_block_builds_lines_and_formula_spans():
    chars = _chars(["Let f(θ) be a", "function of θ."])
    block = LayoutBlock(type=BlockType.TEXT, bbox=Rect(x0=0, y0=0, x1=200, y1=40), order=0,
                        content="Let $f(\\theta)$ be a function of $\\theta$.")  # fmt: skip
    refined, total, found = refine_block(block, chars)
    assert (total, found) == (2, 2)
    kinds = [[(s.kind, s.content) for s in line.spans] for line in refined.lines]
    assert kinds == [
        [("text", "Let"), ("inline_formula", "f(\\theta)"), ("text", "be a")],
        [("text", "function of"), ("inline_formula", "\\theta"), ("text", ".")],
    ]


def test_refine_block_without_chars_counts_only_nonempty_trimmed_formulas():
    box = Rect.of([0, 0, 200, 40])
    block = LayoutBlock(type=BlockType.TEXT, bbox=box, order=0, content="Let $ x_1 $ and $   $ be inputs.")
    refined, total, found = refine_block(block, [])
    (line,) = refined.lines
    (formula,) = [span for span in line.spans if span.kind == "inline_formula"]
    assert (total, found) == (1, 0)
    assert formula.content == "x_1" and not formula.located


def test_formula_box_ignores_tall_math_glyph_extents():
    # 根号等数学符号的字框按数学字体上下边界计算，高出一大截；公式框不能被它撑高，否则会盖住上下行的正文
    chars = _chars(["When x = y we stop"])
    sqrt = chars[7].model_copy(update={"c": "√", "bbox": Rect(x0=45, y0=-15, x1=50, y1=25)})  # “=”换成超高的根号
    chars[7] = sqrt
    block = LayoutBlock(type=BlockType.TEXT, bbox=Rect(x0=0, y0=-20, x1=200, y1=30), order=0,
                        content="When $x \\sqrt y$ we stop")  # fmt: skip
    refined, _, found = refine_block(block, chars)
    (formula,) = [s for line in refined.lines for s in line.spans if s.kind == "inline_formula"]
    assert found == 1 and formula.bbox.y0 == 0 and formula.bbox.y1 == 10


def test_cross_visual_line_formula_box_covers_all_formula_rows():
    chars = _chars(["Start ab", "cd End"])
    block = LayoutBlock(type=BlockType.TEXT, bbox=Rect(x0=0, y0=0, x1=200, y1=40), order=0,
                        content="Start $abcd$ End")  # fmt: skip
    refined, _, found = refine_block(block, chars)
    (formula,) = [s for line in refined.lines for s in line.spans if s.kind == "inline_formula"]
    assert found == 1 and formula.bbox.y1 > 20


def test_block_formula_cleanup_with_and_without_pdf_characters():
    source = (
        r"ReL $\mathrm{{U}}{:}500\times 300\times 100,$ then $\mathrm{If} r_t(\theta)$ use $\pi_\theta\colon$ done."
    )
    chars = _chars(["ReLU: 500×300×100, then If rt(θ) use πθ: done."])
    block = LayoutBlock(type=BlockType.TEXT, order=0, bbox=Rect.of([0, 0, 250, 20]), content=source)
    for page_chars in (chars, []):
        refined, total, found = refine_block(block, page_chars)
        spans = [s for line in refined.lines for s in line.spans]
        assert total == 3 and found == (3 if page_chars else 0)
        assert [s.content for s in spans if s.kind == "inline_formula"] == [
            r"500\times 300\times 100",
            r"r_t(\theta)",
            r"\pi_\theta",
        ]
        assert [s.content.strip() for s in spans if s.kind == "text"] == ["ReLU:", ", then If", "use", ": done."]


def test_formula_cleanup_preserves_math_dots_and_variables():
    content = r"Start $x_{.}$, $x^{.}$, $3.14$, $a\cdots$, $a\ldots$, $x\colon y$, $x _ { : }$, $n,$ and $If x$."
    block = LayoutBlock(type=BlockType.TEXT, order=0, bbox=Rect.of([0, 0, 250, 20]), content=content)
    refined, total, _ = refine_block(block, [])
    assert total == 9
    assert [s.content for s in refined.lines[0].spans if s.kind == "inline_formula"] == [
        "x_{.}",
        "x^{.}",
        "3.14",
        r"a\cdots",
        r"a\ldots",
        r"x\colon y",
        "x _ { : }",
        "n",
        "x",
    ]


def test_formula_box_is_clamped_to_both_adjacent_visual_rows():
    chars = _chars(["previous paragraph", "Let fx be an input", "next paragraph"])
    for i, char in enumerate(chars):
        top = char.line * 9
        chars[i] = char.model_copy(update={"bbox": Rect.of([char.bbox.x0, top, char.bbox.x1, top + 14])})
    block = LayoutBlock(
        type=BlockType.TEXT,
        order=0,
        bbox=Rect.of([0, 0, 250, 35]),
        content="previous paragraph Let $fx$ be an input next paragraph",
    )
    refined, _, found = refine_block(block, chars)
    (formula,) = [s for line in refined.lines for s in line.spans if s.kind == "inline_formula"]
    assert found == 1
    assert (formula.bbox.y0, formula.bbox.y1) == (11.5, 20.5)


@pytest.mark.parametrize(
    ("content", "parts"),
    [
        (
            r"Value Network v: ReL $\mathrm { { U } } { : } 5 0 0 \times 3 0 0 \times 1 0 0 ,$ number",
            ["Value Network v: ReLU: ", r"5 0 0 \times 3 0 0 \times 1 0 0", ", number"],
        ),
        (r"$\mathrm { I f } r _ { t } ( \theta )$ deviates", ["If ", r"r _ { t } ( \theta )", " deviates"]),
        *[
            (rf"$\mathrm {{ D }} _ {{ 1 }} {colon}$ A new solution", ["", r"\mathrm { D } _ { 1 }", ": A new solution"])
            for colon in (r"\mathrm { : }", r"\text { : }", "{ : }", r"\colon")
        ],
    ],
)
def test_content_parts_clean_spaced_mineru_formula_boundaries(content, parts):
    assert _content_parts(content) == parts


def test_formula_box_keeps_elsevier_glyphs_until_adjacent_row_midpoints():
    chars = _chars(["Previous line", "Let st+1 be the result", "Next line"], size=8.0)
    for i, char in enumerate(chars):
        center = 10 + char.line * 10.5
        height, size = 13.1, 8.0
        if char.line == 1 and 35 <= char.bbox.x0 <= 45:  # t、+、1
            center += 2
            height = 5.0 if char.c == "+" else 9.1
            size = 5.6
        chars[i] = char.model_copy(update={
            "bbox": Rect.of([char.bbox.x0, center - height / 2, char.bbox.x1, center + height / 2]), "size": size,
        })  # fmt: skip
    block = LayoutBlock(
        type=BlockType.TEXT, order=0, bbox=Rect.of([0, 0, 250, 40]),
        content="Previous line Let $s_{t+1}$ be the result Next line",
    )  # fmt: skip
    refined, total, found = refine_block(block, chars)
    (formula,) = [s for line in refined.lines for s in line.spans if s.kind == "inline_formula"]
    assert (total, found) == (1, 1)
    assert (formula.bbox.y0, formula.bbox.y1) == pytest.approx((15.25, 25.75))
    assert formula.bbox.height == pytest.approx(10.5)


def test_clean_formula_lines_preserves_literal_dollar_in_text_span():
    box = Rect.of([0, 0, 200, 10])
    spans = [
        LayoutSpan(kind="text", content="It costs $50 when", bbox=box),
        LayoutSpan(kind="inline_formula", content="x,", bbox=box),
        LayoutSpan(kind="inline_formula", content=r"\mathrm { I f } y", bbox=box),
        LayoutSpan(kind="text", content="is positive.", bbox=box),
    ]
    block = LayoutBlock(type=BlockType.TEXT, order=0, bbox=box, lines=[LayoutLine(bbox=box, spans=spans)])
    assert clean_formula_lines(block, []).lines == block.lines


def test_clean_formula_lines_locates_characters_just_outside_mineru_line_box():
    # 字符中心比 MinerU 行框顶边高 0.5pt，仍应走原生字符定位。
    box = Rect.of([0, 5.5, 200, 10])
    spans = [
        LayoutSpan(kind="inline_formula", content=r"\mathrm { I f } x", bbox=box),
        LayoutSpan(kind="text", content="changes", bbox=box),
    ]
    block = LayoutBlock(type=BlockType.TEXT, order=0, bbox=box, lines=[LayoutLine(bbox=box, spans=spans)])
    (line,) = clean_formula_lines(block, _chars(["If x changes"])).lines
    assert [(span.kind, span.content) for span in line.spans] == [
        ("text", "If"), ("inline_formula", "x"), ("text", "changes"),
    ]  # fmt: skip
    assert line.spans[1].located
