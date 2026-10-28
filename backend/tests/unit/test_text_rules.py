from pdftr.domain.chars import Char
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import PageKind
from pdftr.extraction.classify import classify
from pdftr.fusion.align import align
from pdftr.fusion.style import block_style, is_serif
from pdftr.fusion.text_fix import choose, text_of


def _ch(
    c: str, x: float, y: float = 100, line: int = 0, font: str = "Times-Roman", size: float = 10, bold=False
) -> Char:
    return Char(c=c, bbox=Rect.of([x, y, x + 5, y + 10]), font=font, size=size, bold=bold, line=line)


def test_align_prefers_smallest_box_and_reports_orphans():
    outer, inner = Rect.of([0, 0, 200, 200]), Rect.of([10, 90, 60, 120])
    chars = [_ch("a", 20), _ch("b", 150), _ch("z", 500)]
    per, orphans = align(chars, [outer, inner])
    assert [c.c for c in per[0]] == ["b"]
    assert [c.c for c in per[1]] == ["a"]
    assert [c.c for c in orphans] == ["z"]


def test_text_of_orders_and_spaces():
    chars = [_ch("b", 6), _ch("a", 0), _ch("c", 30), _ch("ﬁ", 0, y=120, line=1)]
    assert text_of(chars) == "ab c fi"


def test_choose_only_fixes_broken_text():
    # 正常文字一律保留解析器结果，即使 PDF 文字不同
    assert choose("L weight decay", "L2 weight decay") == ("L weight decay", False)
    assert choose("BERT<sub>BASE</sub>", "BERTBASE") == ("BERT<sub>BASE</sub>", False)
    # 含控制字符的才改用 PDF 文字
    assert choose("dominated by the \x0f in", "dominated by the ε in") == ("dominated by the ε in", True)
    # PDF 文字差异太大时仍保留
    assert choose("dominated by the \x0f in", "totally different text here") == ("dominated by the \x0f in", False)
    # 解析器没给文字时用 PDF 文字
    assert choose("", "hello") == ("hello", True)


def test_block_style():
    chars = [_ch("a", 0, size=10, bold=True), _ch("b", 6, size=10, bold=True), _ch("c", 12, size=12)]
    style = block_style(chars)
    assert style.font_size == 10 and style.bold and style.serif
    assert not is_serif("Helvetica-Bold")
    assert block_style([]).font_size == 10.0


def test_classify():
    text = [_ch(chr(97 + i % 26), i * 6) for i in range(40)]
    assert classify(text, image_coverage=0.0) is PageKind.NATIVE
    assert classify([], image_coverage=0.95) is PageKind.SCANNED
    assert classify([], image_coverage=0.0) is PageKind.NATIVE  # 空白页
    garbled = [_ch("", i * 6) for i in range(30)] + text[:10]
    assert classify(garbled, image_coverage=0.0) is PageKind.GARBLED
