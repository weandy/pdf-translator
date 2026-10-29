"""扫描页识别：带隐形 OCR 层的扫描页不能当成原生页；扫描页的字号按块估算。"""

import pymupdf

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import BlockType, PageKind
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutSpan
from pdftr.extraction.chars import _page as extract_page
from pdftr.fusion.build import _size_for_scan


def _make(hidden: bool, image: bool) -> pymupdf.Page:
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    if image:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30), False)
        pix.clear_with(230)
        page.insert_image(page.rect, pixmap=pix)
    page.insert_text((40, 60), "Some recognised words on a scanned page " * 3, fontsize=9,
                     render_mode=3 if hidden else 0)  # fmt: skip
    return page


def test_hidden_ocr_layer_over_a_page_image_is_scanned_and_its_chars_dropped():
    scanned = extract_page(_make(hidden=True, image=True))
    assert scanned.kind is PageKind.SCANNED and scanned.chars == []
    assert extract_page(_make(hidden=False, image=True)).kind is PageKind.NATIVE  # 可见文字照常是原生页
    assert extract_page(_make(hidden=True, image=False)).kind is PageKind.NATIVE  # 没有整页图：不是扫描件


def test_scan_font_size_from_block_shape():
    def block(text: str, height: float) -> LayoutBlock:
        box = Rect(x0=0, y0=0, x1=240, y1=height)
        line = LayoutLine(bbox=box, spans=[LayoutSpan(kind="text", content=text, bbox=box)])
        return LayoutBlock(type=BlockType.TEXT, bbox=box, order=0, lines=[line])

    two_lines = block("The specific heats at constant pressure and constant volume are defined by", 24)
    assert 9 <= _size_for_scan(two_lines) <= 10.5
    assert 9 <= _size_for_scan(block("It can be shown that", 12)) <= 10.5


def test_body_text_on_scanned_pages_gets_one_size():
    from pdftr.domain.ir import Block, BlockStyle, Page
    from pdftr.fusion.build import _even_scan_sizes

    def blk(i: int, size: float, kind: BlockType = BlockType.TEXT) -> Block:
        box = Rect(x0=0, y0=i * 20, x1=100, y1=i * 20 + 10)
        return Block(id=f"p0-b{i}", page=0, type=kind, bbox=box, order=i, style=BlockStyle(font_size=size),
                     translatable=True)  # fmt: skip

    page = Page(index=0, width=600, height=800, kind=PageKind.SCANNED,
                blocks=[blk(0, 9.6), blk(1, 10.4), blk(2, 8.1), blk(3, 9.7), blk(4, 8.0, BlockType.TITLE)])  # fmt: skip
    _even_scan_sizes([page])
    assert [b.style.font_size for b in page.blocks] == [9.65, 9.65, 9.65, 9.65, 8.0]
