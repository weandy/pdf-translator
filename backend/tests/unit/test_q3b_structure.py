from pathlib import Path

import pymupdf
import pytest

from pdftr.api.schemas import DocumentOut
from pdftr.domain.chars import Char, CharPage
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import BlockType, PageKind
from pdftr.domain.job import Job
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutPage, LayoutResult, LayoutSpan
from pdftr.extraction.chars import extract
from pdftr.fusion.build import fuse
from pdftr.pipeline.usecases import build_outline
from pdftr.rendering.background import make_background
from pdftr.rendering.typeset import wants_indent
from pdftr.translation.segmenter import block_source, segment


def _line(text: str, y: float, x: float = 70, width: float = 150) -> LayoutLine:
    box = Rect.of([x, y - 11, x + width, y + 3])
    return LayoutLine(bbox=box, spans=[LayoutSpan(kind="text", bbox=box, content=text)])


def _block(*lines: LayoutLine, order: int = 0, kind: BlockType = BlockType.TEXT) -> LayoutBlock:
    return LayoutBlock(type=kind, bbox=Rect.of([
        min(line.bbox.x0 for line in lines), min(line.bbox.y0 for line in lines),
        max(line.bbox.x1 for line in lines), max(line.bbox.y1 for line in lines),
    ]), order=order, lines=list(lines))  # fmt: skip


def _page(index: int, *blocks: LayoutBlock) -> LayoutPage:
    return LayoutPage(index=index, width=600, height=800, blocks=list(blocks))


def _layout(*pages: LayoutPage) -> LayoutResult:
    return LayoutResult(parser="synthetic", pages=list(pages))


def _chars(text: str, x: float, y: float, line: int, size: float, *, bold: bool = False, italic: bool = False):
    return [
        Char(c=c, bbox=Rect.of([x + i * 4, y - 8, x + (i + 1) * 4, y + 2]), font="Synthetic Serif",
             size=size, origin_y=y, line=line, bold=bold, italic=italic)
        for i, c in enumerate(text)
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("first", "second"),
    [("IV. Overview", "A. Samples"), ("IV. Overview", "C. Notes"), ("1. Setup", "1.1. Samples")],
)
def test_merged_numbered_headings_match_geometry_styles_and_outline(tmp_path: Path, first, second):
    a, b = _line(first, 100), _line(second, 120, 75, 92)
    chars = _chars(first, 70, 100, 0, 14, bold=True) + _chars(second, 75, 120, 1, 10, italic=True)
    page = CharPage(index=0, width=600, height=800, chars=chars)
    doc, _ = fuse("synthetic", _layout(_page(0, _block(a, b, kind=BlockType.TITLE))), [page], used_cloud=False)
    lead, child = doc.blocks()
    assert [(t.level, t.order, t.bbox) for t in (lead, child)] == [(1, 0, a.bbox), (2, 1, b.bbox)]
    assert lead.style.bold and not lead.style.italic and lead.style.font_size == 14
    assert child.style.italic and not child.style.bold and child.style.font_size == 10
    assert lead.spans[0].text == first and child.spans[0].text == second
    assert lead.spans[0].line == child.spans[0].line == 0
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(path)
    assert [(i.title, i.level) for i in build_outline(path, doc, segment(doc))] == [(first, 1), (second, 2)]


@pytest.mark.parametrize(
    ("start", "end"),
    [("1. Introduction to Long", "Topic Name"), ("IV. SYNTHETIC OVERVIEW", "FOR LOCAL SAMPLES")],
)
def test_wrapped_heading_stays_one_block(start, end):
    a, b = _line(start, 100), _line(end, 118)
    page = CharPage(index=0, width=600, height=800, kind=PageKind.SCANNED)
    doc, _ = fuse("synthetic", _layout(_page(0, _block(a, b, kind=BlockType.TITLE))), [page], used_cloud=False)
    (title,) = doc.blocks()
    assert block_source(title, {}) == f"{start} {end}"
    assert title.level is None


def test_publication_dates_split_and_keep_wrapped_date_in_front_matter():
    header = _block(_line("About Layered Samples", 50), kind=BlockType.TITLE)
    date_lines = [
        _line("<b>Received</b>: 1 May", 106, 340),
        _line("2025", 121, 340),
        _line("<i>Revised</i>： 4 May 2025", 136, 340),
        _line("accepted: 8 May 2025", 151, 340),
        _line("Available online: 12 May 2025", 166, 340),
    ]
    intro = _block(_line("Abstract: A plain sample.", 210), order=3)
    body = _block(_line("Received: text embedded in a wide paragraph", 300, width=400), order=4)
    left = _block(_line("The group near the", 105), order=1)
    layout = _layout(_page(0, header, left, _block(*date_lines, order=2), intro, body))
    page = CharPage(index=0, width=600, height=800, kind=PageKind.SCANNED)
    doc, _ = fuse("synthetic", layout, [page], used_cloud=False)
    items = [b for b in doc.blocks() if b.list_item]
    assert len(items) == 4
    assert [len(b.spans) for b in items] == [2, 1, 1, 1]
    assert [b.bbox.y0 for b in items] == [date_lines[i].bbox.y0 for i in (0, 2, 3, 4)]
    assert all(b.continues_from is None for b in items)
    segs = segment(doc)
    by_block = {s.block_ids[0]: s for s in segs}
    assert "1 May 2025" in by_block[items[0].id].source
    assert all(not wants_indent(b, by_block[b.id], True) for b in items)
    assert not doc.blocks()[-1].list_item
    assert by_block[doc.blocks()[-1].id].source.startswith("Received:")


@pytest.mark.parametrize("at_top", [True, False])
def test_hanging_items_use_baselines_and_leave_original_markers_in_background(tmp_path: Path, at_top):
    texts = ["first sample", "details on this line", "second sample", "more details"]
    ys = [100, 115, 130, 145]
    path, background = tmp_path / "synthetic.pdf", tmp_path / "background.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=800)
        for y, text in zip(ys, texts, strict=True):
            page.insert_text((70, y), text, fontsize=10)
        if at_top:
            page.insert_text((45, ys[0]), "(3)", fontsize=10)
        page.insert_text((45, ys[2]), "(4)", fontsize=10)
        pdf.save(path)
    lines = [_line(text, y) for text, y in zip(texts, ys, strict=True)]
    doc, _ = fuse("synthetic", _layout(_page(0, _block(*lines))), extract(path), used_cloud=False)
    first, second = doc.blocks()
    assert [first.list_item, second.list_item] == [at_top, True]
    assert [first.bbox.y0, second.bbox.y0] == [lines[0].bbox.y0, lines[2].bbox.y0]
    assert first.bbox.x0 == second.bbox.x0 == 70
    assert all(not b.orphan and b.continues_from is None for b in (first, second))
    first_seg, second_seg = segment(doc)
    assert first_seg.source == "first sample details on this line"
    assert second_seg.source == "second sample more details"
    assert wants_indent(first, first_seg, True) is not at_top
    assert not wants_indent(second, second_seg, True)
    payload = DocumentOut.of(Job(id=doc.job_id, filename=path.name), doc, [first_seg, second_seg])
    assert [b.list_item for b in payload.pages[0].blocks] == [at_top, True]
    make_background(path, doc.blocks(), background)
    with pymupdf.open(background) as pdf:
        remaining = str(pdf[0].get_text()).split()
    assert remaining == (["(3)", "(4)"] if at_top else ["(4)"])


def test_single_top_hanging_item_blocks_supplemental_cross_page_join():
    layout = _layout(
        _page(0, _block(_line("the preceding paragraph ends with", 680))),
        _page(1, _block(_line("another sample on a new page", 100))),
    )
    prev = CharPage(index=0, width=600, height=800, chars=_chars("the preceding paragraph ends with", 70, 680, 0, 10))
    body = _chars("another sample on a new page", 70, 100, 0, 10)
    marker = _chars("b)", 45, 100, 1, 10)
    next_page = CharPage(index=1, width=600, height=800, chars=body + marker)
    doc, _ = fuse("synthetic", layout, [prev, next_page], used_cloud=False)
    assert doc.pages[1].blocks[0].list_item and doc.pages[1].blocks[0].continues_from is None
    plain, _ = fuse("synthetic", layout, [prev, next_page.model_copy(update={"chars": body})], used_cloud=False)
    assert plain.pages[1].blocks[0].continues_from == plain.pages[0].blocks[0].id


@pytest.mark.parametrize("step", [1, 5])
def test_review_line_number_track_is_excluded_but_independent_track_can_split(step):
    lines = [_line("sample text", 100 + i * 15) for i in range(8)]
    chars = []
    for i in range(8):
        y = 100 + i * 15
        chars += _chars("sample text", 70, y, i, 10)
        if i < 5:
            number = str(8 + i * step)
            chars += _chars(number, 48 - len(number) * 4, y, i + 20, 10)
        elif i == 6:
            # 点号标记恰好落在已有行号的右对齐轨上，也应排除。
            chars += _chars("13.", 36, y, i + 20, 10)
    page = CharPage(index=0, width=600, height=800, chars=chars)
    layout = _layout(_page(0, _block(*lines)))
    doc, _ = fuse("synthetic", layout, [page], used_cloud=False)
    (text,) = doc.blocks()
    assert not text.list_item and len(text.spans) == 8
    marker = _chars("a)", 55, 205, 30, 10)
    with_marker, _ = fuse("synthetic", layout, [page.model_copy(update={"chars": chars + marker})], used_cloud=False)
    assert [b.list_item for b in with_marker.blocks()] == [False, True]


def test_relocated_continuation_keeps_split_tail_as_its_predecessor():
    a, b, continued = _line("first sample", 630), _line("next sample", 647), _line("continues here", 104, x=350)
    merged = _block(a, b).model_copy(update={"lines": [a, b, continued]})
    placeholder = LayoutBlock(type=BlockType.TEXT, bbox=continued.bbox, order=2, placeholder=True)
    chars = (
        _chars("first sample", 70, 630, 0, 10)
        + _chars("next sample", 70, 647, 1, 10)
        + _chars("(2)", 45, 647, 3, 10)
        + _chars("continues here", 350, 104, 4, 10)
    )
    page = CharPage(index=0, width=600, height=800, chars=chars)
    doc, report = fuse("synthetic", _layout(_page(0, merged, placeholder)), [page], used_cloud=False)
    first, second, tail = doc.blocks()
    assert report.continuations == 1
    assert first.continues_from is second.continues_from is None
    assert second.list_item and tail.continues_from == second.id
    assert [s.block_ids for s in segment(doc)] == [[first.id], [second.id, tail.id]]
