"""块级解析结果（只有块内容、行内公式写成 $LaTeX$）的公式定位。"""

import pytest

from pdftr.domain.chars import Char
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import BlockType
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutSpan
from pdftr.fusion.formula_align import (
    _content_parts,
    _rich_slice,
    clean_formula_lines,
    exclude_formula_chars,
    locate_formulas,
    merge_torn_formulas,
    promote_native_math_spans,
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


def _row_block(spans: list[LayoutSpan]) -> LayoutBlock:
    box = Rect(
        x0=min(s.bbox.x0 for s in spans),
        y0=min(s.bbox.y0 for s in spans),
        x1=max(s.bbox.x1 for s in spans),
        y1=max(s.bbox.y1 for s in spans),
    )
    return LayoutBlock(type=BlockType.TEXT, bbox=box, order=0, lines=[LayoutLine(bbox=box, spans=spans)])


@pytest.mark.parametrize("cut", [4, 6])
def test_math_word_recovers_prefix_without_clipping_descenders(cut: int) -> None:
    chars = _chars(["A macℎinejn", "next row example"])
    for c in chars:
        if c.line == 1:
            c.bbox = Rect.of([c.bbox.x0, 12.5, c.bbox.x1, 18.0])
            c.origin_y = 18.5
        else:
            c.origin_y = 8
    for c in chars[2:9]:
        c.font = "Times" if c.c == "ℎ" else "STIXMath-Italic"
    for c in chars[9:11]:
        c.size, c.origin_y = 7.0, 10.0
        c.bbox = Rect.of([c.bbox.x0, 6.0, c.bbox.x1, 12.0])
    word = "machine"
    start = chars[2 + cut].bbox.x0
    block = _row_block(
        [
            LayoutSpan(kind="text", content="A " + word[:cut], bbox=Rect.of([10, 0, start, 10])),
            LayoutSpan(kind="inline_formula", content=word[cut:] + "_{jn}", bbox=Rect.of([start, 0, 65, 8])),
        ]
    )
    text, formula = clean_formula_lines(block, chars).lines[0].spans
    assert text.content.strip() == "A"
    assert text.bbox.x1 == formula.bbox.x0 == chars[2].bbox.x0
    assert formula.content == "machine_{jn}"
    assert chars[9].bbox.y1 == formula.bbox.y1 < chars[11].bbox.y0


def test_adjacent_formulas_reclaim_only_their_math_fragments() -> None:
    chars = _chars(["pq, macℎine"])
    for c in chars:
        c.font = "CMMI10" if c.c.isalpha() and c.c != "ℎ" else "Times"
    block = _row_block(
        [
            LayoutSpan(kind="inline_formula", content="pq", bbox=chars[0].bbox),
            LayoutSpan(kind="text", content="q, <i>machin</i>", bbox=Rect.of([15, 0, 60, 10])),
            LayoutSpan(kind="inline_formula", content="e_i", bbox=chars[10].bbox),
        ]
    )
    left, text, right = clean_formula_lines(block, chars).lines[0].spans
    assert (left.content, text.content.strip(), right.content) == ("pq", ",", "machine_i")
    assert (left.bbox.x1, text.bbox.x0) == (chars[1].bbox.x1,) * 2
    assert (text.bbox.x1, right.bbox.x0) == (chars[4].bbox.x0,) * 2


@pytest.mark.parametrize(
    ("word", "cut", "script"),
    [("machine", 4, "j,m"), ("RPT", 0, "j,m"), *(("x", 0, f"i{mark}1") for mark in "+-−*/")],
)
def test_formula_recovers_internal_script_separator_only(word: str, cut: int, script: str) -> None:
    chars = _chars([word + script + ", next"])
    for c in chars[: len(word)]:
        c.font = "CMMI10"
    for c in chars[len(word) : len(word) + len(script)]:
        c.size, c.origin_y = 7, 11
        c.bbox = Rect.of([c.bbox.x0, 6, c.bbox.x1, 13])
    # 中间标点可能只有字框符合下标，字体报告的字号和基线仍与正文相同。
    mark = chars[len(word) + 1]
    mark.size, mark.origin_y = 10, 8
    for c in chars:
        if c.origin_y is None:
            c.origin_y = 8
    start = chars[cut].bbox.x0
    edge = chars[len(word)].bbox.x1
    spans = [LayoutSpan(kind="text", content=word[:cut], bbox=Rect.of([10, 0, start, 10]))] if cut else []
    spans += [
        LayoutSpan(kind="inline_formula", content=word[cut:] + "_{" + script + "}", bbox=Rect.of([start, 0, edge, 13])),
        LayoutSpan(kind="text", content=script[1:] + ", next", bbox=Rect.of([edge, 0, chars[-1].bbox.x1, 13])),
    ]
    formula, text = clean_formula_lines(_row_block(spans), chars).lines[0].spans
    assert (formula.content, text.content) == (word + "_{" + script + "}", ", next")
    assert formula.bbox == Rect.of([10, 0, chars[len(word) + 2].bbox.x1, 13])
    assert text.bbox.x0 >= formula.bbox.x1


def test_script_separator_recovers_left_prescript_and_requires_adjacent_glyphs() -> None:
    chars = _chars(["j,mQ next"])
    for c in chars[:3]:
        c.size = 7
        c.bbox = Rect.of([c.bbox.x0, 6, c.bbox.x1, 13])
    left = LayoutSpan(kind="text", content="j,", bbox=Rect.of([10, 6, 20, 13]))
    formula = LayoutSpan(kind="inline_formula", content="{}_{j,m}Q", bbox=Rect.of([20, 0, 30, 13]))
    block = _row_block([left, formula])
    (whole,) = clean_formula_lines(block, chars).lines[0].spans
    assert whole.bbox == Rect.of([10, 0, 30, 13])
    chars[0].bbox = Rect.of([4, 6, 9, 13])
    assert clean_formula_lines(block, chars).lines[0].spans == [left, formula]


@pytest.mark.parametrize("content", ["<i>jm </i>next", "<b><i>jm </i></b>next"])
def test_rich_slice_preserves_space_without_empty_emphasis(content: str) -> None:
    assert _rich_slice(content, 2, 7) == " next"


@pytest.mark.parametrize("overlap", [False, True])
@pytest.mark.parametrize("native_prefix", ["", "uv"])
def test_internal_script_copy_requires_native_right_context(overlap: bool, native_prefix: str) -> None:
    chars = _chars(["Kuv=(A+B)" + native_prefix + "; next"])
    for c in chars:
        c.origin_y = 8
    for c in chars[1:3]:
        c.size, c.origin_y = 7, 11
    left = chars[1 if overlap else 9].bbox.x0
    block = _row_block(
        [
            LayoutSpan(kind="inline_formula", content="K_{uv}=(A+B)", bbox=Rect.of([10, 0, chars[8].bbox.x1, 12])),
            LayoutSpan(
                kind="text", content=" <i><sub>uv</sub></i>; next", bbox=Rect.of([left, 0, chars[-1].bbox.x1, 12])
            ),
        ]
    )
    formula, text = clean_formula_lines(block, chars).lines[0].spans
    assert formula.content == "K_{uv}=(A+B)"
    assert text.content.strip() == ("<i><sub>uv</sub></i>" if native_prefix else "") + "; next"


@pytest.mark.parametrize("mark", [",", "."])
@pytest.mark.parametrize(("mark_size", "mark_y", "keep"), [(7.97, 8, False), (7.97, 11, True), (5.5, 8, True)])
def test_mixed_row_tail_uses_prose_size_and_baseline(mark: str, mark_size: float, mark_y: float, keep: bool) -> None:
    chars = _chars(["see (x,y)" + mark])
    for c in chars:
        c.size, c.origin_y = 7.97, 8
    for c in chars[4:9]:
        c.size, c.font = 9.96, "CMSY10"
    chars[-1].size, chars[-1].origin_y = mark_size, mark_y
    block = _row_block(
        [
            LayoutSpan(kind="text", content="see ", bbox=Rect.of([10, 0, 30, 10])),
            LayoutSpan(kind="inline_formula", content="(x,y)", bbox=Rect.of([30, 0, chars[-1].bbox.x1, 12])),
        ]
    )
    spans = clean_formula_lines(block, chars).lines[0].spans
    assert spans[1].bbox.contains_point(*chars[6].bbox.center)
    assert spans[1].bbox.contains_point(*chars[-1].bbox.center) == keep
    assert [s.content for s in spans[2:]] == ([] if keep else [mark])


def test_tall_main_symbol_still_clamps_to_both_midpoints() -> None:
    chars = _chars(["aaaa", "a∈aa", "aaaa"])
    for c in chars:
        y = 100 + 12 * c.line
        c.bbox = Rect.of([c.bbox.x0, y, c.bbox.x1, y + 10])
        c.origin_y = y + 8
    symbol = chars[5]
    symbol.font = "CMSY10"
    symbol.bbox = Rect.of([15, 107, 20, 124.29])
    block = _row_block([LayoutSpan(kind="inline_formula", content=r"\in", bbox=symbol.bbox)])
    (formula,) = clean_formula_lines(block, chars).lines[0].spans
    assert (formula.bbox.y0, formula.bbox.y1) == (111, 123)


def test_formula_owns_attached_scripts_once_and_keeps_neighbor_text() -> None:
    chars = _chars(["RPTjm end"])
    for i, c in enumerate(chars[3:5]):
        c.size, c.origin_y = 7, 11
        c.bbox = Rect.of([24 + 2 * i, 7, 26 + 2 * i, 13])
    raw_box = Rect.of([10, 0, 26, 8])
    block = _row_block(
        [
            LayoutSpan(kind="inline_formula", content="RPT_{jm}", bbox=raw_box),
            LayoutSpan(kind="text", content="<i>jm</i> end", bbox=Rect.of([24, 6, 55, 13])),
        ]
    )
    formula, right = clean_formula_lines(block, chars).lines[0].spans
    assert (formula.bbox.x1, formula.bbox.y1) == (28, 13)
    assert right.content.strip() == "end"
    assert exclude_formula_chars(chars, [raw_box], 10, 8) == chars[5:]
    assert exclude_formula_chars(chars, [formula.bbox], 10) == chars[5:]
    shallow = raw_box.model_copy(update={"y1": 4})
    assert exclude_formula_chars(chars[3:5], [shallow], 10, 8) == []
    assert exclude_formula_chars(chars[3:5], [shallow], 10) == chars[3:5]
    neighbor = chars[3].model_copy(update={"bbox": Rect.of([25, 17, 27, 23]), "origin_y": 22})
    regular = chars[3].model_copy(update={"size": 10, "origin_y": 8})
    assert exclude_formula_chars([neighbor, regular], [raw_box], 10, 8) == [neighbor, regular]


@pytest.mark.parametrize("punct", [",", ";", "."])
@pytest.mark.parametrize("already_in_text", [False, True])
def test_formula_returns_trailing_prose_mark_once(punct: str, already_in_text: bool) -> None:
    chars = _chars(["f(x)" + punct + " next"])
    comma = chars[4]
    comma.bbox = comma.bbox.model_copy(update={"x0": comma.bbox.x0 + 0.4})
    block = _row_block(
        [
            LayoutSpan(kind="inline_formula", content="f(x)", bbox=Rect.of([10, 0, 35, 10])),
            LayoutSpan(
                kind="text", content=(punct if already_in_text else "") + " next", bbox=Rect.of([35, 0, 60, 10])
            ),
        ]
    )
    formula, text = clean_formula_lines(block, chars).lines[0].spans
    assert formula.bbox.x1 < chars[4].bbox.center[0] and text.bbox.contains_point(*chars[4].bbox.center)
    assert text.content == punct + " next"
    assert formula.bbox.x1 + 0.3 <= comma.bbox.x0


@pytest.mark.parametrize(
    ("native", "latex", "font"),
    [("f(x,y)", "f(x,y)", "Times"), ("f(x,", "f", "Times"), ("x,", "x", "CMSY10"), ("a.", "a", "Times")],
)
def test_formula_keeps_internal_or_mathematical_marks(native: str, latex: str, font: str) -> None:
    chars = _chars([native])
    chars[-1].font = font
    box = Rect.of([10, 0, chars[-1].bbox.x1, 10])
    block = _row_block([LayoutSpan(kind="inline_formula", content=latex, bbox=box)])
    assert clean_formula_lines(block, chars).lines == block.lines


@pytest.mark.parametrize(("letter", "font"), [("R", "STIXMathBlackboard"), ("ℝ", "Times"), ("𝔸", "Times")])
def test_promote_blackboard_space_preserves_outer_text(letter: str, font: str) -> None:
    raw = "(Var ∈ " + letter + "o)"
    chars = _chars([raw])
    chars[7].font = font
    chars[8].size, chars[8].origin_y = 7, 6
    chars[8].bbox = Rect.of([50, -2, 55, 5])
    for c in chars:
        if c.origin_y is None:
            c.origin_y = 8
    block = _row_block([LayoutSpan(kind="text", content=raw, bbox=Rect.of([10, 0, 60, 10]))])
    result = promote_native_math_spans(block, chars)
    left, formula, right = result.lines[0].spans
    name = "A" if letter == "𝔸" else "R"
    assert (left.content, formula.content, right.content) == ("(", rf"Var \in \mathbb{{{name}}}^{{o}}", ")")
    assert formula.bbox == Rect.of([15, -2, 55, 10]) and formula.located
    assert clean_formula_lines(result, chars).lines == result.lines


@pytest.mark.parametrize("raw", ["Ro", "R", "R∈R", "ℝ", "ℝo"])
def test_promote_requires_blackboard_and_math_context(raw: str) -> None:
    chars = _chars([raw])
    block = _row_block([LayoutSpan(kind="text", content=raw, bbox=Rect.of([10, 0, chars[-1].bbox.x1, 10]))])
    assert promote_native_math_spans(block, chars) is block


def test_mixed_line_promotes_text_and_preserves_existing_formula() -> None:
    raw = "(Var ∈ ℝo)"
    chars = _chars(["xi " + raw])
    chars[-2].size = 7
    chars[-2].bbox = Rect.of([chars[-2].bbox.x0, -2, chars[-2].bbox.x1, 5])
    existing = LayoutSpan(kind="inline_formula", content="x_i", bbox=Rect.of([10, 0, 20, 10]))
    text = LayoutSpan(kind="text", content=raw, bbox=Rect.of([25, 0, chars[-1].bbox.center[0] - 0.4, 10]))
    result = promote_native_math_spans(_row_block([existing, text]), chars)
    old, left, promoted, right = result.lines[0].spans
    assert old == existing
    assert (left.content, promoted.content, right.content) == ("(", r"Var \in \mathbb{R}^{o}", ")")
    assert (promoted.bbox.x0, promoted.bbox.y0, promoted.bbox.x1) == (chars[4].bbox.x0, -2, chars[-2].bbox.x1)
    assert promote_native_math_spans(result, chars) is result


def test_overlapping_text_does_not_reuse_glyphs_of_existing_formula() -> None:
    chars = _chars(["ℝo Ro"])
    chars[1].size, chars[1].origin_y = 7, 5
    for c in chars:
        if c.origin_y is None:
            c.origin_y = 8
    existing = LayoutSpan(kind="inline_formula", content=r"\mathbb{R}^o", bbox=Rect.of([10, 0, 20, 10]))
    text = LayoutSpan(kind="text", content="Ro", bbox=Rect.of([10, 0, 35, 10]))
    block = _row_block([existing, text])
    assert promote_native_math_spans(block, chars) is block


def test_blackboard_codomain_reclaims_geometric_exponent() -> None:
    chars = _chars(["X→ℝo×v end"])
    for c in chars[3:6]:
        c.size = 7
        c.bbox = Rect.of([c.bbox.x0, -2, c.bbox.x1, 5])
    block = _row_block(
        [
            LayoutSpan(kind="inline_formula", content="X", bbox=Rect.of([10, 0, 20, 10])),
            LayoutSpan(kind="text", content="R<sup>o×v</sup> end", bbox=Rect.of([20, -2, 60, 10])),
        ]
    )
    formula, text = clean_formula_lines(block, chars).lines[0].spans
    assert formula.content == r"X \to \mathbb{R}^{o\times v}"
    assert (formula.bbox.x1, text.bbox.x0, text.content.strip()) == (40, 40, "end")


@pytest.mark.parametrize(
    ("raw", "size", "origin", "expected"),
    [
        ("ℝo", 8.5, 6.5, r"\mathbb{R}^{o}"),
        ("ℝo", 8.5, 7.0, ""),
        ("ℝo", 9.0, 5.0, ""),
        ("R∈ℕ", 10.0, 8.0, r"R \in \mathbb{N}"),
        ("R↦ℍo", 7.0, 10.0, r"R \mapsto \mathbb{H}_{o}"),
    ],
)
def test_native_space_requires_a_relation_or_raised_exponent(
    raw: str, size: float, origin: float, expected: str
) -> None:
    chars = _chars(["(" + raw + ")"])
    for c in chars:
        c.origin_y = 8
        if c.c == "o":
            c.size, c.origin_y = size, origin
    block = _row_block([LayoutSpan(kind="text", content="(" + raw + ")", bbox=Rect.of([10, 0, 50, 10]))])
    spans = promote_native_math_spans(block, chars).lines[0].spans
    assert [s.content for s in spans if s.kind == "inline_formula"] == ([expected] if expected else [])
