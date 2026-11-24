import json
from pathlib import Path

import pytest

from pdftr.domain.chars import Char, CharPage
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType, Span
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutPage, LayoutResult, LayoutSpan
from pdftr.fusion.relocate import dedupe_page_blocks, relocate
from pdftr.parsing.mineru_normalize import normalize

FIXTURE = Path(__file__).parent.parent / "fixtures" / "mineru_layout_min.json"


def test_continuation_lines_move_to_placeholders():
    layout = normalize(json.loads(FIXTURE.read_text(encoding="utf-8")))
    moved, report = relocate(layout)
    p0, p1 = moved.pages
    head, same_page = p0.blocks[1], p0.blocks[2]
    next_page = p1.blocks[0]

    # 原块只剩自己框内的那一行
    assert head.lines[0].spans[0].content == "Let"
    assert len(head.lines) == 1
    # 跨栏的行搬到同页占位块，跨页的行搬到下一页占位块
    assert same_page.lines[0].spans[0].content == "continues in the next column"
    assert next_page.lines[0].spans[0].content == "and ends on the next page."
    assert not same_page.placeholder and not next_page.placeholder

    # 链：p0 第 1 块 → p0 第 2 块 → p1 第 0 块
    assert report.links == {(0, 2): (0, 1), (1, 0): (0, 2)}
    assert (report.moved_lines, report.unmatched_lines) == (2, 0)
    # 不修改输入
    assert len(layout.pages[0].blocks[1].lines) == 3


def test_continuations_use_true_neighbor_even_when_pages_are_reversed():
    layout = normalize(json.loads(FIXTURE.read_text(encoding="utf-8")))
    layout.pages.reverse()
    moved, report = relocate(layout)
    assert [page.index for page in moved.pages] == [0, 1]
    assert [page.index for page in layout.pages] == [1, 0]
    assert report.links == {(0, 2): (0, 1), (1, 0): (0, 2)}
    assert report.moved_lines == 2


def test_continuation_does_not_cross_missing_page():
    layout = normalize(json.loads(FIXTURE.read_text(encoding="utf-8")))
    layout.pages[1].index = 2
    moved, report = relocate(layout)
    assert report.links == {(0, 2): (0, 1)}
    assert report.unmatched_lines == 1
    assert moved.pages[1].blocks[0].placeholder


def _line(x: int, y: int, text: str) -> LayoutLine:
    box = Rect.of([x, y, x + 100, y + 10])
    return LayoutLine(bbox=box, spans=[LayoutSpan(kind="text", content=text, bbox=box)])


def _native_chars(xs: list[int], y: int = 10) -> list[Char]:
    return [Char(c="a", bbox=Rect.of([x, y, x + 1, y + 10]), font="Roman", size=10, line=0) for x in xs]


def test_relocated_chain_owns_shared_native_line_but_preserves_distinct_lines() -> None:
    head, shared, extra = _line(10, 10, "Left column"), _line(150, 10, "Right column"), _line(150, 30, "Another row")
    blocks = [
        LayoutBlock(order=0, type=BlockType.TEXT, bbox=head.bbox, lines=[head, shared]),
        LayoutBlock(order=1, type=BlockType.TEXT, bbox=shared.bbox, lines=[shared]),
        LayoutBlock(order=2, type=BlockType.TEXT, bbox=Rect.of([150, 10, 250, 40]), lines=[shared, extra]),
        LayoutBlock(order=3, type=BlockType.TEXT, bbox=shared.bbox, placeholder=True),
    ]
    page = LayoutPage(index=0, width=300, height=200, blocks=blocks)
    cp = CharPage(index=0, width=300, height=200, chars=_native_chars([15, 155, 175, 195]))
    result, report = relocate(LayoutResult(parser="synthetic", pages=[page]), [cp])
    assert [b.order for b in result.pages[0].blocks] == [0, 2, 3]
    assert [b.lines for b in result.pages[0].blocks] == [[head], [extra], [shared]]
    assert report.links == {(0, 3): (0, 0)}
    assert (report.deduped_lines, report.deduped_blocks) == (2, 1)


def test_distinct_pages_positions_and_scan_text_are_preserved() -> None:
    rows = [_line(10, 10, "Repeated text"), _line(10, 30, "Repeated text"), _line(10, 10, "Distinct content")]
    rows.extend([_line(10, 10, "REPEATED TEXT"), _line(30, 30, "Repeated text")])
    blocks = [LayoutBlock(order=i, type=BlockType.TEXT, bbox=line.bbox, lines=[line]) for i, line in enumerate(rows)]
    pages = [LayoutPage(index=i, width=300, height=200, blocks=group) for i, group in enumerate([blocks, blocks[:1]])]
    result, report = relocate(LayoutResult(parser="synthetic", pages=pages))
    assert [[b.order for b in pg.blocks] for pg in result.pages] == [[0, 1, 2, 4], [0]]
    assert (report.deduped_lines, report.deduped_blocks) == (1, 1)


def test_native_overlap_requires_shared_glyphs() -> None:
    lines = [_line(10, 10, "Repeated text"), _line(30, 10, "Repeated text")]
    blocks = [LayoutBlock(order=i, type=BlockType.TEXT, bbox=line.bbox, lines=[line]) for i, line in enumerate(lines)]
    page = LayoutPage(index=0, width=300, height=200, blocks=blocks)
    cp = CharPage(index=0, width=300, height=200, chars=_native_chars([15, 20, 75, 118, 124]))
    _, report = relocate(LayoutResult(parser="synthetic", pages=[page]), [cp])
    assert report.deduped_lines == 0


def test_empty_and_discarded_rows_do_not_claim_native_glyphs() -> None:
    row = _line(10, 10, "Keep this row")
    blank = _line(10, 10, " ")
    blocks = [
        LayoutBlock(order=0, type=BlockType.TEXT, bbox=row.bbox, lines=[blank, blank]),
        LayoutBlock(order=1, type=BlockType.TEXT, bbox=row.bbox, lines=[row], discarded=True),
        LayoutBlock(order=2, type=BlockType.TEXT, bbox=row.bbox, lines=[row]),
    ]
    page = LayoutPage(index=0, width=300, height=200, blocks=blocks)
    cp = CharPage(index=0, width=300, height=200, chars=_native_chars([15, 20, 25]))
    result, report = relocate(LayoutResult(parser="synthetic", pages=[page]), [cp])
    assert result.pages[0].blocks == blocks
    assert report.deduped_lines == 0


def test_removed_chain_head_leaves_no_dangling_relocation_link() -> None:
    head, first, second = _line(10, 10, "Same head"), _line(150, 10, "First tail"), _line(150, 40, "Second tail")
    blocks = [
        LayoutBlock(order=0, type=BlockType.TEXT, bbox=head.bbox, lines=[head, first]),
        LayoutBlock(order=1, type=BlockType.TEXT, bbox=head.bbox, lines=[head, second]),
        LayoutBlock(order=2, type=BlockType.TEXT, bbox=first.bbox, placeholder=True),
        LayoutBlock(order=3, type=BlockType.TEXT, bbox=second.bbox, placeholder=True),
    ]
    page = LayoutPage(index=0, width=300, height=200, blocks=blocks)
    result, report = relocate(LayoutResult(parser="synthetic", pages=[page]))
    assert [b.order for b in result.pages[0].blocks] == [0, 2, 3]
    assert report.links == {(0, 2): (0, 0)}


@pytest.mark.parametrize("native", [False, True])
def test_page_blocks_drop_duplicate_orphan_and_keep_text_in_empty_space(native: bool) -> None:
    box = Rect.of([10, 10, 110, 20])
    gap = Rect.of([10, 30, 110, 40])
    span = Span(text="Synthetic row", bbox=box, size=10)
    body = Block(
        id="p0-b2",
        order=2,
        page=0,
        type=BlockType.TEXT,
        bbox=Rect.of([10, 10, 110, 40]),
        spans=[span],
        style=BlockStyle(font_size=10),
        translatable=True,
        continues_from="p0-b0",
    )
    duplicate = body.model_copy(update={"id": "p0-b1", "order": 1, "continues_from": None})
    orphan = duplicate.model_copy(update={"id": "p0-b3", "bbox": box, "orphan": True})
    in_gap = orphan.model_copy(update={"id": "p0-b4", "bbox": gap, "spans": [span.model_copy(update={"bbox": gap})]})
    other_page = orphan.model_copy(update={"id": "p1-b0", "page": 1})
    separate_box = Rect.of([160, 10, 260, 20])
    separate = orphan.model_copy(
        update={
            "id": "p0-b5",
            "bbox": separate_box,
            "spans": [span.model_copy(update={"bbox": separate_box})],
        }
    )
    chars = _native_chars([15, 40]) + _native_chars([15, 40], y=30) if native else None
    assert dedupe_page_blocks([orphan, duplicate, body, in_gap, separate], chars) == [body, in_gap, separate]
    assert dedupe_page_blocks([body, other_page]) == [body, other_page]


@pytest.mark.parametrize("native", [True, False])
def test_orphan_lines_can_belong_to_two_distinct_blocks(native: bool) -> None:
    def paragraph(bid: str, lines: list[int], *, orphan: bool = False) -> Block:
        spans = [
            Span(text=f"Row {n}", bbox=Rect.of([10, n * 20, 110, n * 20 + 10]), size=10, line=i)
            for i, n in enumerate(lines)
        ]
        return Block(
            id=bid,
            page=0,
            order=lines[0],
            type=BlockType.TEXT,
            orphan=orphan,
            translatable=True,
            style=BlockStyle(font_size=10),
            spans=spans,
            bbox=Rect.of([10, spans[0].bbox.y0, 110, spans[-1].bbox.y1]),
        )

    first, second = paragraph("a", [1, 2]), paragraph("b", [3, 4])
    duplicate = paragraph("orphan", [2, 3], orphan=True)
    chars = [c for n in range(1, 5) for c in _native_chars([15, 40], y=n * 20)] if native else None
    assert dedupe_page_blocks([duplicate, first, second], chars) == [first, second]
