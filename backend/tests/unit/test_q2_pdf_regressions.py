"""合成 PDF 验证 Q2 的版面、裁图与导出路径。"""

from pathlib import Path

import pymupdf
import pytest

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import BlockType, FormulaRef, Segment, SegmentStatus
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutPage, LayoutResult, LayoutSpan
from pdftr.extraction.chars import extract
from pdftr.fusion.build import _supplement_continuations, fuse
from pdftr.rendering.background import crop_png, make_background
from pdftr.rendering.export import _formula_images, export_pdf_to_path
from pdftr.translation.segmenter import segment

BlockSpec = tuple[BlockType, float, float, str]
TEXT, TITLE = BlockType.TEXT, BlockType.TITLE


def _synthetic(path: Path, specs: list[list[BlockSpec]]) -> LayoutResult:
    pages = []
    with pymupdf.open() as pdf:
        for index, specs_on_page in enumerate(specs):
            page = pdf.new_page(width=600, height=800)
            blocks = []
            for order, (kind, x, y, text) in enumerate(specs_on_page):
                box = Rect.of([x, y - 11, x + 220, y + 3])
                if text:
                    page.insert_text((x, y), text, fontsize=10)
                spans = [LayoutSpan(kind="text", content=text, bbox=box)] if text else []
                blocks.append(LayoutBlock(type=kind, order=order, bbox=box, lines=[LayoutLine(bbox=box, spans=spans)]))
            pages.append(LayoutPage(index=index, width=600, height=800, blocks=blocks))
        pdf.save(path)
    return LayoutResult(parser="synthetic", pages=pages)


def test_elsevier_first_page_labels_and_caption_codes(tmp_path):
    original = tmp_path / "first.pdf"
    abstract = "This study uses reproducible experiments."
    highlights = ["First result.", "Second result.", "Third result.", "Fourth result."]
    layout = _synthetic(original, [[
        (TITLE, 50, 40, "A Four-Part Synthetic Study"),
        (TEXT, 50, 65, "Jane Example, Local Institute"),
        (TITLE, 50, 95, "H I G H L I G H T S"),
        *[(TEXT, 50, 115 + n * 20, text) for n, text in enumerate(highlights)],
        (TITLE, 330, 95, "A B S T R A C T"),
        (TEXT, 330, 115, abstract),
        (TITLE, 50, 205, "A R T I C L E I N F O"),
        (TITLE, 50, 225, "D A T A  S E T"),
        (TEXT, 50, 245, "Available online"),
        (BlockType.CAPTION, 50, 270, "PPO-HHD1 PPO-HHD3 PPO-HHD5PPO-HHP2"),
        (BlockType.CAPTION, 50, 290, "FIG. 2 PPO-HHD1"),
    ]])  # fmt: skip
    doc, _ = fuse("synthetic", layout, extract(original), used_cloud=False)
    assert doc.meta.abstract == abstract
    assert [s.source for s in segment(doc)] == [
        "A Four-Part Synthetic Study", "HIGHLIGHTS", *highlights, "ABSTRACT", abstract,
        "ARTICLE INFO", "DATA SET", "Available online", "FIG. 2 PPO-HHD1",
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("between", "linked"),
    [
        (BlockType.FIGURE, True),
        (BlockType.TABLE, True),
        (BlockType.CAPTION, True),
        (BlockType.FORMULA, True),
        (BlockType.CODE, True),
        (BlockType.HEADER, True),
        (BlockType.FOOTER, True),
        (BlockType.TITLE, False),
        (BlockType.FOOTNOTE, False),
    ],
)
def test_cross_column_join_respects_structural_barriers(tmp_path, between, linked):
    original = tmp_path / "columns.pdf"
    layout = _synthetic(original, [[
        (TEXT, 50, 650, "We consider both the"), (between, 50, 685, ""),
        (TEXT, 330, 80, "above sub-schedules."),
    ]])  # fmt: skip
    doc, report = fuse("synthetic", layout, extract(original), used_cloud=False)
    assert doc.pages[0].blocks[2].continues_from == ("p0-b0" if linked else None)
    assert report.continuations == int(linked)


@pytest.mark.parametrize(
    ("tail", "head", "linked"),
    [
        ("We operate under nonidentical", "TOU tariffs.", True),
        ("We operate at different times.", "independent paragraph.", False),
        ("We operate at different times.”", "independent paragraph.", False),
        ("We operate under nonidentical", "Entirely new paragraph starts with many more unrelated words.", False),
    ],
)
def test_cross_page_join_over_figure(tmp_path, tail, head, linked):
    original = tmp_path / "pages.pdf"
    layout = _synthetic(
        original,
        [
            [(TEXT, 330, 640, tail), (BlockType.FIGURE, 330, 680, "")],
            [(BlockType.FIGURE, 50, 30, ""), (TEXT, 50, 125, head)],
        ],
    )
    doc, _ = fuse("synthetic", layout, extract(original), used_cloud=False)
    assert doc.pages[1].blocks[1].continues_from == ("p0-b0" if linked else None)


def test_reverse_column_join_fills_original_block_coordinates(tmp_path):
    original = tmp_path / "reverse.pdf"
    layout = _synthetic(original, [[
        (TEXT, 330, 80, "as possible in every round."),
        (BlockType.CODE, 50, 430, ""),
        (TEXT, 50, 650, "Calculate the answer as quickly"),
    ]])  # fmt: skip
    doc, report = fuse("synthetic", layout, extract(original), used_cloud=False)
    assert report.continuations == 1
    (seg,) = segment(doc)
    assert seg.source == "Calculate the answer as quickly as possible in every round."
    assert [part.block_id for part in seg.parts] == ["p0-b2", "p0-b0"]

    background, output = tmp_path / "background.pdf", tmp_path / "translated.pdf"
    make_background(original, [doc.pages[0].blocks[0], doc.pages[0].blocks[2]], background)
    seg.target, seg.status = "Calculate first part; finish second part.", SegmentStatus.DONE
    export_pdf_to_path(original, background, doc, [seg], "mono", links=[], out=output)
    with pymupdf.open(output) as pdf:
        page = pdf[0]
        (first,) = page.search_for("Calculate first part")
        (second,) = page.search_for("finish second part")
        assert first.x0 < 100 and first.y0 > 600
        assert second.x0 > 325 and second.y0 < 100


def test_reverse_join_requires_uniqueness_and_rejects_cycles(tmp_path):
    original = tmp_path / "reverse.pdf"
    layout = _synthetic(original, [[
        (TEXT, 330, 80, "as possible in every round."),
        (TEXT, 50, 510, "This incomplete statement"),
        (TEXT, 50, 650, "Calculate the answer as quickly"),
    ]])  # fmt: skip
    doc, report = fuse("synthetic", layout, extract(original), used_cloud=False)
    first, other, last = doc.pages[0].blocks[:3]
    assert report.continuations == 0 and first.continues_from is None
    other.translatable = False
    last.continues_from = first.id
    assert _supplement_continuations(doc.pages) == 0 and first.continues_from is None


def _ink_rows(pix: pymupdf.Pixmap) -> list[bool]:
    alpha = pix.samples[3::4]
    return [any(alpha[y * pix.width : (y + 1) * pix.width]) for y in range(pix.height)]


def test_compact_formula_crop_removes_only_detached_edge_ink(tmp_path):
    original = tmp_path / "formula.pdf"
    bbox = Rect.of([40, 50, 80, 64])
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=160, height=100)
        # 模拟上下行伸入裁图首尾的笔画，以及贴近主体的分数线、下标与重音。
        for rect in [(42, 49, 43, 50.5), (58, 54, 60, 55), (55, 55, 65, 56), (57, 56, 62, 61), (74, 63.5, 75, 66)]:
            page.draw_rect(pymupdf.Rect(rect), color=None, fill=(0, 0, 0))
        pdf.save(original)

    seg = Segment(
        id="s0",
        job_id="synthetic",
        block_ids=["p0-b0"],
        source="Evaluate <m1/> here.",
        formula_refs={"m1": FormulaRef(page=0, bbox=bbox)},
    )
    with pymupdf.open(original) as pdf:
        page = pdf[0]
        raw = page.get_pixmap(matrix=pymupdf.Matrix(4, 4), clip=pymupdf.Rect(39.7, 50, 80.3, 64), alpha=True)
        png, width, height = _formula_images(pdf, seg)["m1"]
    assert _ink_rows(raw)[0] and _ink_rows(raw)[-1]
    assert crop_png(original, 0, bbox) == png
    trimmed = pymupdf.Pixmap(png)
    assert (width, height) == (trimmed.width / 4, trimmed.height / 4)
    assert not _ink_rows(trimmed)[0] and not _ink_rows(trimmed)[-1]
    assert sum(_ink_rows(trimmed)) == 7 * 4
    assert trimmed.height == raw.height - 4


def test_export_normalizes_legacy_spacing_and_keeps_link_geometry(tmp_path):
    original = tmp_path / "linked.pdf"
    layout = _synthetic(original, [[(TEXT, 50, 80, "See [1] for detail.")]])
    uri = "https://example.invalid/reference"
    with pymupdf.open(original) as pdf:
        page = pdf[0]
        (anchor,) = page.search_for("[1]")
        page.insert_link({"kind": pymupdf.LINK_URI, "from": anchor, "uri": uri})
        pdf.saveIncr()
    doc, _ = fuse("synthetic", layout, extract(original), used_cloud=False)
    (seg,) = segment(doc)
    seg.target, seg.status = "图 ResNet 50 的 2 项结果见[1]。", SegmentStatus.DONE
    background, output = tmp_path / "background.pdf", tmp_path / "translated.pdf"
    make_background(original, doc.blocks(), background)
    export_pdf_to_path(original, background, doc, [seg], "mono", out=output)
    with pymupdf.open(output) as pdf:
        page = pdf[0]
        assert "图ResNet 50的2项结果见[1]。" in page.get_text(
            flags=pymupdf.TEXTFLAGS_TEXT | pymupdf.TEXT_INHIBIT_SPACES
        )
        (link,) = page.get_links()
        assert link["uri"] == uri
        (anchor,) = page.search_for("[1]")
        assert link["from"].intersects(anchor)
