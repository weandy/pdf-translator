"""目录：以 PDF 书签为准，没有书签时用标题块按编号定层级，并配上译文。"""

from pathlib import Path

import pymupdf

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType, DocMeta, Document, Page, Segment, SegmentStatus, Span
from pdftr.extraction.outline import pdf_outline
from pdftr.pipeline.usecases import build_outline


def _pdf(tmp_path: Path, toc: list | None = None) -> Path:
    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page(width=600, height=800)
    if toc:
        doc.set_toc(toc)
    path = tmp_path / "x.pdf"
    doc.save(path)
    return path


def _doc(titles: list[tuple[int, str]]) -> tuple[Document, list[Segment]]:
    blocks, segs = [], []
    for i, (page, text) in enumerate(titles):
        box = Rect(x0=50, y0=100 + i * 20, x1=300, y1=112 + i * 20)
        span = Span(text=text, bbox=box, size=10)
        b = Block(id=f"p{page}-b{i}", page=page, type=BlockType.TITLE, bbox=box, order=i, spans=[span],
                  style=BlockStyle(font_size=10), translatable=True)  # fmt: skip
        blocks.append(b)
        segs.append(Segment(id=f"s{i}", job_id="0123456789abcdef", block_ids=[b.id], source=text,
                            target=f"译:{text}", status=SegmentStatus.DONE))  # fmt: skip
    pages = [Page(index=n, width=600, height=800, blocks=[b for b in blocks if b.page == n]) for n in range(3)]
    return Document(job_id="0123456789abcdef", pages=pages, meta=DocMeta(title="Paper Title")), segs


def test_bookmarks_win_and_get_translations(tmp_path: Path):
    pdf = _pdf(tmp_path, [[1, "Introduction", 1], [2, "Encoder Stacks", 2]])
    assert [(i.title, i.level, i.page) for i in pdf_outline(pdf)] == [("Introduction", 1, 0), ("Encoder Stacks", 2, 1)]
    doc, segs = _doc([(0, "Paper Title"), (0, "1 Introduction"), (1, "3.1 Encoder Stacks")])
    items = build_outline(pdf, doc, segs)
    assert [(i.title, i.translated, i.segment_id) for i in items] == [
        ("Introduction", "译:1 Introduction", "s1"),
        ("Encoder Stacks", "译:3.1 Encoder Stacks", "s2"),
    ]


def test_without_bookmarks_titles_are_levelled_by_numbering(tmp_path: Path):
    doc, segs = _doc([(0, "Paper Title"), (0, "Abstract"), (1, "3. Method"), (1, "3.1. Model"), (2, "A.2 Proofs")])
    items = build_outline(_pdf(tmp_path), doc, segs)
    # 论文标题不进目录；“3.” 一级、“3.1.” 二级、“A.2” 二级
    assert [(i.title, i.level, i.page) for i in items] == [
        ("Abstract", 1, 0), ("3. Method", 1, 1), ("3.1. Model", 2, 1), ("A.2 Proofs", 2, 2)
    ]  # fmt: skip
    assert items[1].top == 140 and items[1].translated == "译:3. Method"
