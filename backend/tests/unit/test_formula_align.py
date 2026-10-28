"""块级解析结果（只有块内容、行内公式写成 $LaTeX$）的公式定位。"""

from pdftr.domain.chars import Char
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import BlockType
from pdftr.domain.layout import LayoutBlock
from pdftr.fusion.formula_align import locate_formulas, refine_block


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
