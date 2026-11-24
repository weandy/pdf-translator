"""块级解析结果（只有块内容、行内公式写成 $LaTeX$）的公式定位。"""

import pytest

from pdftr.domain.chars import Char
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import BlockType
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutSpan
from pdftr.fusion.formula_align import (
    _content_parts,
    clean_formula_lines,
    locate_formulas,
    merge_torn_formulas,
    refine_block,
)


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


@pytest.mark.parametrize(
    ("latex", "expected"),
    [
        ("x=1; y=2", ["", "x=1", "; ", "y=2", ""]),
        (r"f(x; \theta)", ["", r"f(x; \theta)", ""]),
        (r"\{x; x>0\}", ["", r"\{x; x>0\}", ""]),
        (r"x\;y", ["", r"x\;y", ""]),
        (r"\left|x; y\right|", ["", r"\left|x; y\right|", ""]),
        (r"\begin{matrix}x;y\end{matrix}", ["", r"\begin{matrix}x;y\end{matrix}", ""]),
        ("x; {^2}", ["", "x", "; <sup>2</sup>"]),
        ("x; ^{3}", ["", "x", "; <sup>3</sup>"]),
        ("x; (4)", ["", "x", "; (4)"]),
        ("x_{;}", ["", "x", ";"]),
        ("x_{;a}", ["", "x_{;a}", ""]),
        ("x's", ["", "x", "'s"]),
        ("x{' }s", ["", "x", "'s"]),
        ("x'+s", ["", "x'+s", ""]),
        (r"\mathrm{i.e.,} x", ["i.e., ", "x", ""]),
        (r"\mathrm{e.g.,} x", ["e.g., ", "x", ""]),
        (r"\operatorname{Eq.}(2), x", ["Eq. (2), ", "x", ""]),
        (r"\mathrm{e.g.}x", ["", r"\mathrm{e.g.}x", ""]),
        (r"N \mathrm{widgets}", ["", "N", " widgets"]),
        (r"N_{\mathrm{insp}}", ["", r"N_{\mathrm{insp}}", ""]),
        (r"x\mathrm{mol}", ["", r"x\mathrm{mol}", ""]),
        (r"x+\mathrm{sin}", ["", r"x+\mathrm{sin}", ""]),
        ("(x+1", ["(", "x+1", ""]),
        ("x+1)", ["", "x+1", ")"]),
        (r"\left(x+1", ["(", "x+1", ""]),
        (r"x+1\right)", ["", "x+1", ")"]),
        (r"D D T = 1 . (", ["", "D D T = 1 .", "("]),
        (r"\mathrm { i . e . , } O _ { 1 , 2 }", ["i.e., ", r"O _ { 1 , 2 }", ""]),
        (r"\operatorname { E q . } \left( 4 \right) , h _ { x }", ["Eq. (4), ", r"h _ { x }", ""]),
        (r"n _ { i } \mathrm { w o r d s }", ["", r"n _ { i }", " words"]),
        (r"M = ( A , B ) _ { ; }", ["", "M = ( A , B )", ";"]),
        (r"X _ { 2 } { ' } s", ["", r"X _ { 2 }", "'s"]),
        ("[0,1)", ["", "[0,1)", ""]),
        ("(0,1]", ["", "(0,1]", ""]),
    ],
)
def test_formula_boundaries_and_mathematical_negative_examples(latex, expected):
    assert _content_parts(f"$ {latex} $") == expected


def test_clean_formula_line_keeps_scripts_and_an_untouched_formula_box():
    chars = _chars(["Use x; 2 with z and H2O."])
    exact = Rect.of([78, 1, 83, 9])
    bbox = Rect.of([10, 0, 135, 10])
    spans = [
        LayoutSpan(kind="text", content="Use", bbox=bbox),
        LayoutSpan(kind="inline_formula", content="x; {^2}", bbox=Rect.of([30, 0, 55, 10])),
        LayoutSpan(kind="text", content="with", bbox=bbox),
        LayoutSpan(kind="inline_formula", content="z", bbox=exact),
        LayoutSpan(kind="text", content="and H<sub>2</sub>O.", bbox=bbox),
    ]
    source = LayoutBlock(type=BlockType.TEXT, bbox=bbox, order=0, lines=[LayoutLine(bbox=bbox, spans=spans)])
    for row in (chars, []):
        result = clean_formula_lines(source, row)
        result_spans = result.lines[0].spans
        formulas = [s for s in result_spans if s.kind == "inline_formula"]
        text = " ".join(s.content for s in result_spans if s.kind == "text")
        assert [s.content for s in formulas] == ["x", "z"]
        assert "<sup>2</sup>" in text and "H<sub>2</sub>O" in text
        assert formulas[1].bbox == exact
        if row:
            assert formulas[0].bbox == chars[4].bbox


def test_torn_formula_requires_unique_math_bridge_and_a_single_visual_row():
    chars = _chars(["f(x, k)"])
    for c in chars:
        c.font = "CMMI10"
    boxes = [Rect.of([10, 0, 25, 10]), Rect.of([25, 0, 40, 10]), Rect.of([40, 0, 45, 10])]
    spans = [
        LayoutSpan(kind=kind, content=text, bbox=box)
        for kind, text, box in zip(
            ("inline_formula", "text", "inline_formula"), ("f(x", ", k", ")"), boxes, strict=True
        )
    ]
    line = LayoutLine(bbox=Rect.of([10, 0, 45, 10]), spans=spans)
    (merged,) = merge_torn_formulas(line, chars).spans
    assert merged.content == "f(x , k )" and merged.bbox == line.bbox
    chars[5].font = "Times"
    assert merge_torn_formulas(line, chars).spans == spans
    chars[5].font = "CMMI10"
    spans[1].bbox = Rect.of([20, 0, 40, 10])
    assert merge_torn_formulas(line, chars).spans == spans


def _torn_line(left, bridge, right, *, font="Times", italic=False, bold=False, gap=0):
    chars = _chars(["x" + bridge + "y"])
    for i, glyph in enumerate(chars):
        shift = 0 if i == 0 else gap if i <= len(bridge) else 2 * gap
        glyph.bbox = Rect.of([glyph.bbox.x0 + shift, 0, glyph.bbox.x1 + shift, 10])
        if 0 < i <= len(bridge):
            glyph.font, glyph.italic, glyph.bold = font, italic, bold
    middle_box = Rect.of([chars[1].bbox.x0, 0, chars[-2].bbox.x1, 10])
    spans = [
        LayoutSpan(kind="inline_formula", content=left, bbox=chars[0].bbox),
        LayoutSpan(kind="text", content=bridge, bbox=middle_box),
        LayoutSpan(kind="inline_formula", content=right, bbox=chars[-1].bbox),
    ]
    return LayoutLine(bbox=Rect.of([10, 0, chars[-1].bbox.x1, 10]), spans=spans), chars


@pytest.mark.parametrize(
    ("left", "right"),
    [(r"\left(x", r"y\right)"), ("[x", "y]"), (r"\{x", r"y\}"), (r"\langle x", r"y\rangle")],
)
def test_torn_formula_restores_complementary_outer_delimiters(left, right):
    line, chars = _torn_line(left, ", k", right, italic=True)
    (merged,) = merge_torn_formulas(line, chars).spans
    assert merged.content == f"{left} , k {right}"


@pytest.mark.parametrize(
    ("bridge", "expected", "style"),
    [
        (", x", r", \mathbf{x}", {"bold": True}),
        ("; sin", "; sin", {}),
        ("-1.2,x≤2", r"-1.2,x\le 2", {"font": "CambriaMath"}),
        ("−1×2·3≥", r"-1\times 2\cdot 3\ge", {}),
        ("=", "=", {}),
    ],
)
def test_torn_formula_math_items_and_glyph_styles(bridge, expected, style):
    line, chars = _torn_line("(x", bridge, "y)", **style)
    (merged,) = merge_torn_formulas(line, chars).spans
    assert merged.content == f"(x {expected} y)"


@pytest.mark.parametrize(
    ("bridge", "style"),
    [
        (", AND", {"italic": True, "bold": True}),
        ("where", {"font": "CMMI10"}),
        (", density", {}),
        (", m", {}),
        ("x", {"bold": True}),
        ("sin", {}),
        ("x,y,z,w", {"italic": True}),
        ("123456789,1234567", {}),
        ("x 2", {"italic": True}),
        ("x_1", {"italic": True}),
        (r"x,\alpha", {"italic": True}),
    ],
)
def test_torn_formula_rejects_prose_and_bridges_without_evidence(bridge, style):
    line, chars = _torn_line("(x", bridge, "y)", **style)
    assert merge_torn_formulas(line, chars).spans == line.spans


@pytest.mark.parametrize(("gap", "merged"), [(-4, False), (-2, True), (15, True), (16, False)])
def test_torn_formula_uses_local_median_gap_and_overlap(gap, merged):
    line, chars = _torn_line("(x", ", abc", "y)", italic=True, gap=gap)
    assert (len(merge_torn_formulas(line, chars).spans) == 1) is merged


@pytest.mark.parametrize(("left", "right"), [("(x", "y]"), ("[(x", "y)]"), ("{x", "y}")])
def test_torn_formula_rejects_mismatched_or_nested_unpaired_delimiters(left, right):
    line, chars = _torn_line(left, ", k", right, italic=True)
    assert merge_torn_formulas(line, chars).spans == line.spans


def test_torn_formula_rejects_mismatched_glyph_text_and_multiple_visual_rows():
    line, chars = _torn_line("(x", ", k", "y)", italic=True)
    chars[3].c = "n"
    assert merge_torn_formulas(line, chars).spans == line.spans
    chars[3].c = "k"
    chars[-1].line = 1
    right = chars[-1].bbox
    chars[-1].bbox = Rect.of([right.x0, 20, right.x1, 30])
    line.spans[2].bbox = chars[-1].bbox
    line.bbox = Rect.of([10, 0, right.x1, 30])
    assert merge_torn_formulas(line, chars).spans == line.spans


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
