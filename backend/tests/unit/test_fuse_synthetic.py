"""端到端融合：按夹具里的坐标生成 PDF，再与夹具版面结果融合。"""

import json
from pathlib import Path

import pymupdf
import pytest

from pdftr.domain.chars import CharPage
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import BlockType, PageKind, SegmentStatus
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutPage, LayoutResult, LayoutSpan
from pdftr.extraction.chars import extract
from pdftr.fusion.build import fuse
from pdftr.parsing.mineru_normalize import normalize
from pdftr.rendering.background import make_background
from pdftr.rendering.export import _formula_images, _safe_bottom, export_pdf_to_path
from pdftr.translation.prompt import PROMPT_VERSION, expand_formulas
from pdftr.translation.segmenter import segment

FIXTURE = Path(__file__).parent.parent / "fixtures" / "mineru_layout_min.json"


def layout_block(order: int, kind: BlockType, coords: list[float], text: str = "") -> LayoutBlock:
    box = Rect.of(coords)
    lines = [LayoutLine(bbox=box, spans=[LayoutSpan(kind="text", content=text, bbox=box)])] if text else []
    return LayoutBlock(order=order, type=kind, bbox=box, lines=lines)


def single_page_layout(*blocks: LayoutBlock) -> LayoutResult:
    return LayoutResult(parser="synthetic", pages=[LayoutPage(index=0, width=600, height=800, blocks=list(blocks))])


# (页, x, 基线 y, 文字)：与夹具中各行的框对应
TEXTS = [
    (0, 52, 56, "A Synthetic Title"),
    (0, 52, 609, "Let"),
    (0, 124, 609, "f(x)"),
    (0, 154, 609, "be a function that"),
    (0, 312, 109, "continues in the next column"),
    (0, 52, 556, "Figure 1: A caption."),
    (0, 312, 712, "[1] Some reference."),
    (0, 52, 18, "Running header"),
    (0, 295, 788, "1"),
    (0, 52, 755, "1 A footnote."),
    (0, 52, 250, "This orphan line was missed by the parser"),  # 不在任何块里
    (1, 52, 69, "and ends on the next page."),
    (1, 52, 109, "A normal paragraph."),
]


@pytest.fixture()
def pdf(tmp_path: Path) -> Path:
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page(width=600, height=800)
    for page, x, y, text in TEXTS:
        doc[page].insert_text((x, y), text, fontsize=9, fontname="tiro")
    path = tmp_path / "synthetic.pdf"
    doc.save(path)
    return path


def test_fuse(pdf: Path):
    layout = normalize(json.loads(FIXTURE.read_text(encoding="utf-8")))
    doc, report = fuse("0123456789abcdef", layout, extract(pdf), used_cloud=True)

    assert report.pages == 2 and report.continuations == 2 and report.unmatched_lines == 0
    assert report.orphan_blocks == 1
    assert doc.meta.title == "A Synthetic Title"
    assert doc.meta.used_cloud and doc.meta.parser == "mineru"

    para = doc.block("p0-b1")
    assert para is not None
    assert [(s.kind, s.text or s.formula) for s in para.spans] == [
        ("text", "Let"),
        ("formula", "m1"),
        ("text", "be a function that"),
    ]
    assert para.formulas[0].latex == "f(x)"
    # 续行链：p0-b1 → p0-b2（下一栏）→ p1-b0（下一页）
    assert doc.block("p0-b2").continues_from == "p0-b1"  # type: ignore[union-attr]
    assert doc.block("p1-b0").continues_from == "p0-b2"  # type: ignore[union-attr]
    assert doc.block("p1-b0").spans[0].text == "and ends on the next page."  # type: ignore[union-attr]

    by_type = {b.type: b for b in doc.pages[0].blocks if not b.orphan}
    assert by_type[BlockType.FORMULA].latex == "E = mc^2"
    assert not by_type[BlockType.FORMULA].translatable
    assert by_type[BlockType.CAPTION].translatable
    assert not by_type[BlockType.REFERENCE].translatable  # 是否翻译参考文献由任务选项决定
    assert not by_type[BlockType.HEADER].translatable
    assert by_type[BlockType.FOOTNOTE].translatable  # 正文脚注要翻译

    orphan = next(b for b in doc.pages[0].blocks if b.orphan)
    assert orphan.spans[0].text == "This orphan line was missed by the parser"
    assert orphan.translatable
    assert orphan.style.font_size == pytest.approx(9, abs=0.1)


def test_native_pua_uses_exact_original_crop_and_ambiguous_pua_keeps_the_block(tmp_path):
    path = tmp_path / "synthetic-symbol.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page(width=600, height=800).insert_text((50, 100), "Read Z and continue.", fontsize=10)
        pdf.save(path)
    (page,) = extract(path)
    (glyph,) = [c for c in page.chars if c.c == "Z"]
    glyph.c = "\ue246"
    block = layout_block(0, BlockType.TEXT, [48, 87, 145, 105], "Read \ue246 and continue.")
    doc, _ = fuse("synthetic", single_page_layout(block), [page], used_cloud=False)
    (seg,) = segment(doc)
    assert seg.source == "Read <m1/> and continue."
    assert seg.formula_refs["m1"].bbox == glyph.bbox and seg.formulas == {"m1": ""}
    assert expand_formulas(seg.source, seg.formulas) == "Read <m1>[symbol]</m1> and continue."
    assert expand_formulas("<m9/>", {}) == "<m9></m9>" and PROMPT_VERSION == "v3"
    with pymupdf.open(path) as pdf:
        assert pymupdf.Pixmap(_formula_images(pdf, seg)["m1"][0]).alpha
    assert '"origin_y"' not in doc.model_dump_json()

    page.chars.append(glyph.model_copy())
    ambiguous, _ = fuse("synthetic", single_page_layout(block), [page], used_cloud=False)
    assert not ambiguous.blocks()[0].translatable and not ambiguous.blocks()[0].formulas

    # 同一 span 内出现两次相同 PUA 且数量与字形一一对应时，按顺序分别裁图。
    path_dup = tmp_path / "synthetic-symbol-dup.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page(width=600, height=800).insert_text((50, 100), "Read Z then Z end.", fontsize=10)
        pdf.save(path_dup)
    (page_dup,) = extract(path_dup)
    z_glyphs = [c for c in page_dup.chars if c.c == "Z"]
    for g in z_glyphs:
        g.c = "\ue246"
    dup_block = layout_block(0, BlockType.TEXT, [48, 87, 165, 105], "Read \ue246 then \ue246 end.")
    dup_doc, _ = fuse("synthetic", single_page_layout(dup_block), [page_dup], used_cloud=False)
    (dup_seg,) = segment(dup_doc)
    assert dup_seg.source == "Read <m1/> then <m2/> end."
    assert dup_seg.formula_refs["m1"].bbox == z_glyphs[0].bbox
    assert dup_seg.formula_refs["m2"].bbox == z_glyphs[1].bbox


def test_missed_display_formula_protects_background_and_ordinary_orphans_still_translate(tmp_path):
    path = tmp_path / "synthetic-orphans.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=800)
        page.insert_text((50, 100), "A translated paragraph ends here.", fontsize=10)
        page.insert_text((105, 140), "z  = (z  , z  )", fontsize=10)
        page.insert_text((110, 136), "top", fontsize=7)
        page.insert_text((137, 136), "top", fontsize=7)
        page.insert_text((155, 136), "top", fontsize=7)
        page.insert_text((105, 158), "z  = max(z  , f (x))   z  = min(z  , f (x))", fontsize=10)
        page.insert_text((110, 154), "top", fontsize=7)
        page.insert_text((110, 161), "1", fontsize=7)
        page.insert_text((146, 154), "top", fontsize=7)
        page.insert_text((146, 161), "1", fontsize=7)
        page.insert_text((196, 154), "top", fontsize=7)
        page.insert_text((196, 161), "2", fontsize=7)
        page.insert_text((232, 154), "top", fontsize=7)
        page.insert_text((232, 161), "2", fontsize=7)
        page.insert_text((340, 140), "(17)", fontsize=10)
        page.insert_text((52, 195), "Levels of test inputs.", fontsize=10)
        pdf.save(path)
    layout = single_page_layout(
        layout_block(0, BlockType.TEXT, [49, 89, 240, 104], "A translated paragraph ends here.")
    )
    doc, _ = fuse("synthetic", layout, extract(path), used_cloud=False)
    blocks = doc.blocks()
    para = blocks[0]
    formulas = [b for b in blocks if b.type is BlockType.FORMULA]
    orphan_texts = [b for b in blocks if b.type is BlockType.TEXT and b.orphan]
    assert len(formulas) >= 2 and all(not f.translatable and f.latex is None for f in formulas)
    assert any(f.bbox.x1 > 340 for f in formulas)
    assert [b.spans[0].text for b in orphan_texts] == ["Levels of test inputs."]
    out = tmp_path / "synthetic-orphans-background.pdf"
    make_background(path, [b for b in blocks if b.translatable], out)
    with pymupdf.open(out) as pdf:
        assert "(17)" in pdf[0].get_text()
        assert "max(" in pdf[0].get_text()
        top_formula = min(formulas, key=lambda f: f.bbox.y0)
        assert _safe_bottom(pdf[0], pymupdf.Rect(49, 89, 240, 104), 240, para, blocks) == top_formula.bbox.y0


def test_caption_scope_algorithm_bounds_and_caption_size(tmp_path):
    path = tmp_path / "synthetic-layout.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=800)
        page.insert_text((30, 280), "Figure 1. Results.", fontsize=7.25)
        pdf.save(path)
    layout = single_page_layout(
        layout_block(0, BlockType.FIGURE, [30, 80, 310, 240]),
        layout_block(1, BlockType.CAPTION, [40, 220, 150, 260], "(a) Inside chart"),
        layout_block(2, BlockType.CAPTION, [155, 242, 250, 250], "(b) Outside chart"),
        layout_block(3, BlockType.CAPTION, [28, 270, 160, 285], "Figure 1. Results."),
        layout_block(4, BlockType.CODE, [30, 320, 270, 380]),
        layout_block(5, BlockType.TEXT, [40, 308, 220, 324], "<b>Algorithm 2</b> Sample procedure"),
        layout_block(6, BlockType.TEXT, [40, 292, 220, 307], "Algorithm 3 Reference outside"),
        layout_block(7, BlockType.TEXT, [15, 307, 220, 319], "Algorithm 4 Partially aligned"),
    )
    doc, _ = fuse("synthetic", layout, extract(path), used_cloud=False)
    blocks = doc.blocks()
    assert [b.translatable for b in blocks] == [False, False, True, True, False, False, True, True]
    assert blocks[2].style.font_size == blocks[2].spans[0].size == blocks[3].style.font_size == 7.25
    assert any(blocks[2].id in s.block_ids for s in segment(doc))


def test_fusion_recovers_inline_abstract_for_metadata_and_preserves_its_segment():
    layout = single_page_layout(
        layout_block(0, BlockType.TITLE, [20, 20, 330, 40], "A Synthetic Article"),
        layout_block(1, BlockType.TEXT, [20, 42, 320, 52], "A. Testwriter, Example Institute"),
        layout_block(2, BlockType.TEXT, [20, 69, 320, 80], "<i><b>Abstract</b></i>—We use a synthetic example."),
    )
    pages = [CharPage(index=0, width=600, height=800, kind=PageKind.SCANNED)]
    doc, _ = fuse("synthetic", layout, pages, used_cloud=False)
    assert doc.meta.abstract == "We use a synthetic example."
    assert [s.block_ids for s in segment(doc)] == [["p0-b0"], ["p0-b2"]]


def test_block_without_chars_estimates_size_from_line_height(tmp_path: Path):
    """图表里用路径画出的标题提取不到字符：字号按行高估计，而不是统一当作 10pt。"""
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page(width=600, height=800)
    for page, x, y, text in TEXTS:
        if text != "Figure 1: A caption.":  # 这一块不写入文字，模拟路径绘制
            doc[page].insert_text((x, y), text, fontsize=9, fontname="tiro")
    path = tmp_path / "no_caption_chars.pdf"
    doc.save(path)

    layout = normalize(json.loads(FIXTURE.read_text(encoding="utf-8")))
    lb = next(b for b in layout.pages[0].blocks if b.type is BlockType.CAPTION)
    line = lb.lines[0]
    line.bbox = line.bbox.model_copy(update={"y1": line.bbox.y0 + 7})  # 紧贴字形的 7pt 高的行，约为 7pt 的字
    result, _ = fuse("0123456789abcdef", layout, extract(path), used_cloud=True)
    caption = next(b for b in result.blocks() if b.type is BlockType.CAPTION)
    assert caption.style.font_size == pytest.approx(7.0, abs=0.01)


def test_figure_text_becomes_labels(tmp_path: Path):
    """图里的横排文字拆成可翻译的标签；纯数字刻度、竖排文字保留原样；同一行相距很远的文字拆开。"""
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page(width=600, height=800)
    for page, x, y, text in TEXTS:
        doc[page].insert_text((x, y), text, fontsize=9, fontname="tiro")
    fig = doc[0]  # 夹具里图片主体的范围是 (50, 400)–(290, 540)
    fig.insert_text((60, 430), "Training loss", fontsize=7, fontname="helv")
    fig.insert_text((200, 430), "Validation", fontsize=7, fontname="helv")  # 同一行，间隔很大
    fig.insert_text((60, 520), "0.5", fontsize=7, fontname="helv")
    fig.insert_text((280, 500), "Pass rate", fontsize=7, fontname="helv", rotate=90)
    path = tmp_path / "figure.pdf"
    doc.save(path)

    result, report = fuse("0123456789abcdef", normalize(json.loads(FIXTURE.read_text(encoding="utf-8"))),
                          extract(path), used_cloud=True)  # fmt: skip
    labels = [b for b in result.blocks() if b.type is BlockType.FIGURE_TEXT]
    assert sorted(" ".join(s.text for s in b.spans) for b in labels) == ["Training loss", "Validation"]
    assert report.figure_labels == 2 and all(b.translatable for b in labels)
    assert all(b.bbox.x0 >= 50 and b.bbox.x1 <= 290 and b.bbox.y0 >= 400 for b in labels)


@pytest.mark.parametrize("indexes", [[2, 1, 0], [0, 2]])
def test_fuse_aligns_page_numbers_and_preserves_missing_page(tmp_path, indexes):
    from pdftr.domain.geometry import Rect
    from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutPage, LayoutSpan

    path = tmp_path / "indexed.pdf"
    with pymupdf.open() as source:
        for index, text in enumerate(("Alpha", "Bravo", "Charlie")):
            page = source.new_page(width=600 + index, height=800)
            page.insert_text((52, 70), f"{text} synthetic page with independent text.")
        source.save(path)

    def block(index: int) -> LayoutBlock:
        box = Rect(x0=47, y0=50, x1=450 + index, y1=80)
        text = ("Alpha", "Bravo", "Charlie")[index]
        line = LayoutLine(
            bbox=box, spans=[LayoutSpan(kind="text", content=f"{text} synthetic page with independent text.", bbox=box)]
        )
        return LayoutBlock(type=BlockType.TEXT, bbox=box, order=0, lines=[line])

    layout = LayoutResult(
        parser="synthetic",
        pages=[LayoutPage(index=index, width=600 + index, height=800, blocks=[block(index)]) for index in indexes],
    )
    doc, report = fuse("synthetic", layout, extract(path), used_cloud=True)
    assert [p.index for p in doc.pages] == [0, 1, 2]
    assert [p.width for p in doc.pages] == [600, 601, 602]
    assert report.pages == 3
    assert report.missing_layout_pages == ([1] if 1 not in indexes else [])
    for index, text in enumerate(("Alpha", "Bravo", "Charlie")):
        if index in indexes:
            assert doc.pages[index].blocks[0].bbox.x1 == 450 + index
            assert doc.pages[index].blocks[0].spans[0].text.startswith(text)
        else:
            assert not doc.pages[index].blocks

    if report.missing_layout_pages:
        background, dual = tmp_path / "background.pdf", tmp_path / "dual.pdf"
        segments = segment(doc)
        assert len(segments) == 2
        translated_ids = {block_id for item in segments for block_id in item.block_ids}
        assert {block.page for block in doc.blocks() if block.id in translated_ids} == {0, 2}
        for item in segments:
            item.status = SegmentStatus.DONE
            item.target = "Translated synthetic text."
        make_background(path, (block for block in doc.blocks() if block.id in translated_ids), background)
        with pymupdf.open(background) as bg:
            assert bg.page_count == 3
            assert "Bravo" in bg[1].get_text()
            assert "Alpha" not in bg[0].get_text() and "Charlie" not in bg[2].get_text()
        export_pdf_to_path(path, background, doc, segments, mode="dual", out=dual)
        with pymupdf.open(dual) as exported:
            assert [page.rect.width for page in exported] == [1200, 1202, 1204]
            unchanged_text = exported[1].get_text()
            assert isinstance(unchanged_text, str) and unchanged_text.count("Bravo") == 2
            assert "Translated synthetic text." in exported[2].get_text()


def test_missing_garbled_page_has_only_its_page_kind_warning():
    layout = LayoutResult(parser="synthetic", pages=[])
    garbled = CharPage(index=0, width=600, height=800, kind=PageKind.GARBLED, has_images=True)
    scanned = garbled.model_copy(update={"index": 1, "kind": PageKind.SCANNED})
    doc, report = fuse("synthetic", layout, [garbled, scanned], used_cloud=True)
    assert doc.pages[0].kind is PageKind.GARBLED and not doc.pages[0].missing_layout
    assert doc.pages[1].missing_layout
    assert report.non_native_pages == [0, 1]
    assert report.missing_layout_pages == [1]


@pytest.mark.parametrize(("indexes", "message"), [([0, 0], "重复"), ([0, 2], "范围"), ([-1, 1], "范围")])
def test_fuse_rejects_invalid_layout_page_numbers(pdf, indexes, message):
    layout = normalize(json.loads(FIXTURE.read_text(encoding="utf-8")))
    for page, index in zip(layout.pages, indexes, strict=True):
        page.index = index
    with pytest.raises(ValueError, match=message):
        fuse("synthetic", layout, extract(pdf), used_cloud=True)
