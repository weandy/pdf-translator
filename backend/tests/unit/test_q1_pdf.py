"""用合成双栏 PDF 验证跨页段落、链接和最小字号的页面边界。"""

from pathlib import Path

import pymupdf
import pytest

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType, Document, InlineFormula, Page, Span
from pdftr.rendering.background import make_background
from pdftr.rendering.export import ExportMode, Side, export_pdf
from pdftr.translation.segmenter import segment


def _block(page: int, order: int, box: list[float], text: str, prev: str | None = None) -> Block:
    bbox = Rect.of(box)
    return Block(
        id=f"p{page}-b{order}", page=page, order=order, bbox=bbox, type=BlockType.TEXT,
        style=BlockStyle(font_size=10), spans=[Span(bbox=bbox, text=text, size=10)],
        continues_from=prev, translatable=True,
    )  # fmt: skip


def test_background_preserves_tall_glyphs_touching_body_from_above(tmp_path: Path) -> None:
    original, out = tmp_path / "tight.pdf", tmp_path / "erased.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=160, height=100)
        page.insert_text((24, 40), "A||B", fontsize=14)
        end = 24 + pymupdf.get_text_length("A||B", fontsize=14)
        page.insert_text((end, 44), "k", fontsize=7)
        page.insert_text((24, 51), "sample", fontsize=10)
        page.insert_text((24, 63), "again", fontsize=10)
        keep = page.search_for("A||B")[0]
        body = page.search_for("sample")[0] | page.search_for("again")[0]
        assert keep.y0 < body.y0 + 0.5 < keep.y1
        pdf.save(original)
    make_background(original, [_block(0, 0, list(body), "sample again")], out)
    with pymupdf.open(out) as pdf:
        assert "".join(pdf[0].get_textpage().extractText().split()) == "A||Bk"
        assert pdf[0].search_for("A||B")[0] == keep


@pytest.fixture
def columns(tmp_path: Path) -> tuple[Path, Document]:
    original = tmp_path / "two-columns.pdf"
    a = _block(0, 0, [20, 180, 150, 195], "Opening on first page.")
    b = _block(0, 1, [180, 180, 310, 195], "Continued across columns.", a.id)
    c = _block(1, 0, [20, 25, 150, 40], "Closing on second page.", b.id)
    d = _block(1, 1, [180, 25, 310, 40], "Other column.")
    with pymupdf.open() as pdf:
        pdf.new_page(width=330, height=220)
        pdf.new_page(width=460, height=220)
        for block in (a, b, c, d):
            pdf[block.page].insert_text((block.bbox.x0, block.bbox.y1 - 4), block.spans[0].text, fontsize=10)
        pdf[0].insert_link({
            "kind": pymupdf.LINK_GOTO,
            "from": pdf[0].search_for("across columns")[0],
            "page": 1, "to": pymupdf.Point(30, 80),
        })  # fmt: skip
        pdf.save(original)
    document = Document(
        job_id="columns", pages=[
            Page(index=0, width=330, height=220, blocks=[a, b]),
            Page(index=1, width=460, height=220, blocks=[c, d]),
        ],
    )  # fmt: skip
    return original, document


@pytest.mark.parametrize("selected", [0, 1])
def test_page_filter_keeps_other_page_untouched(tmp_path: Path, columns: tuple[Path, Document], selected: int):
    original, doc = columns
    segs = segment(doc, pages=[selected])
    assert {bid for s in segs for bid in s.block_ids} == {b.id for b in doc.pages[selected].blocks}
    background = tmp_path / "selected.pdf"
    chosen = {p.block_id for s in segs for p in s.parts}
    make_background(original, (b for b in doc.blocks() if b.id in chosen), background)
    with pymupdf.open(background) as pdf, pymupdf.open(original) as before:
        assert not str(pdf[selected].get_text()).strip()
        assert pdf[1 - selected].get_text() == before[1 - selected].get_text()


def test_export_balances_heading_and_caption_lines_only(tmp_path: Path):
    original = tmp_path / "synthetic.pdf"
    heading = _block(0, 0, [20, 20, 70, 60], "Sample heading").model_copy(update={"type": BlockType.TITLE})
    caption = _block(0, 1, [20, 70, 70, 110], "Figure 1: Sample.").model_copy(update={"type": BlockType.CAPTION})
    footnote = _block(0, 2, [20, 120, 70, 160], "1 Footnote text.").model_copy(update={"type": BlockType.FOOTNOTE})
    doc = Document(
        job_id="synthetic", pages=[Page(index=0, width=150, height=200, blocks=[heading, caption, footnote])]
    )
    with pymupdf.open() as pdf:
        pdf.new_page(width=150, height=200)
        pdf.save(original)
    segments = segment(doc)
    for seg in segments:
        seg.target = "甲乙丙丁戊己"
    output = export_pdf(original, original, doc, segments, "mono")
    with pymupdf.open(stream=output, filetype="pdf") as pdf:
        assert str(pdf[0].get_text()).splitlines() == ["甲乙丙", "丁戊己", "甲乙丙", "丁戊己", "甲乙丙丁戊", "己"]


@pytest.mark.parametrize(("mode", "side"), [("mono", "right"), ("dual", "right"), ("dual", "left")])
def test_cross_page_link_uses_relocated_block_page(
    tmp_path: Path, columns: tuple[Path, Document], mode: ExportMode, side: Side
):
    original, doc = columns
    segs = segment(doc)
    segs[0].target = "First part remains on this page. Next part ends here. Go across columns on the page."
    background = tmp_path / "background.pdf"
    make_background(original, doc.blocks(), background)
    with pymupdf.open(stream=export_pdf(original, background, doc, segs, mode, side=side), filetype="pdf") as pdf:
        assert len(pdf) == 2
        (translated,) = pdf[1].get_links()
        offset = 460 if mode == "dual" and side == "right" else 0
        assert offset + 20 < translated["from"].x0 < offset + 150
        assert translated["page"] == 1 and translated["to"].x == pytest.approx(offset + 30)
        if mode == "dual":
            (original_link,) = pdf[0].get_links()
            assert original_link["from"].x0 > (330 if side == "left" else 0)
            assert original_link["to"].x == pytest.approx((460 if side == "left" else 0) + 30)
        else:
            assert pdf[0].get_links() == []


@pytest.mark.parametrize("fits_gap", [True, False])
def test_minimum_scale_uses_only_safe_space_and_logs_remaining_overflow(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, fits_gap: bool
):
    original = tmp_path / "overflow.pdf"
    start = _block(0, 0, [20, 30, 105, 43], "First block.")
    right = _block(0, 1, [130, 45, 215, 58], "Right column.")
    below = _block(0, 2, [20, 93, 105, 107], "Below.")
    doc = Document(job_id="overflow", pages=[Page(index=0, width=250, height=120, blocks=[start, right, below])])
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=250, height=120)
        page.insert_text((130, 54), "Right column.", fontsize=10)
        page.insert_text((20, 103), "Below.", fontsize=10)
        pdf.save(original)
    (seg,) = segment(doc, pages=[0])[:1]
    seg.target = "正文" * (20 if fits_gap else 220) + "结尾"
    with pymupdf.open(stream=export_pdf(original, original, doc, [seg], "mono"), filetype="pdf") as pdf:
        assert len(pdf) == 1
        words = pdf[0].get_text("words")
        drawn = [word for word in words if "正" in word[4] or "结" in word[4]]
        assert drawn and max(float(word[3]) for word in drawn) > start.bbox.y1
        assert max(float(word[3]) for word in drawn) <= below.bbox.y0
        assert "Right column." in str(pdf[0].get_text())
        if fits_gap:
            assert "结尾" in str(pdf[0].get_text())
            assert "block_id=p0-b0" not in caplog.text
        else:
            assert "page=1 block_id=p0-b0" in caplog.text and "pt" in caplog.text


def test_export_overflow_at_page_bottom_retains_only_in_page_lines(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    original = tmp_path / "bottom.pdf"
    block = _block(0, 0, [20, 65, 90, 78], "Last paragraph.")
    doc = Document(job_id="bottom", pages=[Page(index=0, width=220, height=120, blocks=[block])])
    with pymupdf.open() as pdf:
        pdf.new_page(width=220, height=120)
        pdf.save(original)
    (seg,) = segment(doc)
    seg.target = "正文" * 100 + "结尾"
    with pymupdf.open(stream=export_pdf(original, original, doc, [seg], "mono"), filetype="pdf") as pdf:
        assert len(pdf) == 1
        words = pdf[0].get_text("words")
        assert words and block.bbox.y1 < max(float(word[3]) for word in words) <= 116
        assert "结尾" not in str(pdf[0].get_text())
    assert "page=1 block_id=p0-b0" in caplog.text


@pytest.mark.parametrize(
    ("block_type", "size", "target"),
    [(BlockType.TITLE, 12, "原字号"), (BlockType.TEXT, 12, "原字号"), (BlockType.CAPTION, 9, "Full-size caption")],
)
def test_single_line_export_keeps_original_font_size(tmp_path: Path, block_type: BlockType, size: int, target: str):
    original = tmp_path / "single-line.pdf"
    block = _block(0, 0, [20, 30, 180, 30 + size], "Synthetic text.")
    block.type, block.style.font_size = block_type, size
    doc = Document(job_id="single-line", pages=[Page(index=0, width=220, height=120, blocks=[block])])
    with pymupdf.open() as pdf:
        pdf.new_page(width=220, height=120)
        pdf.save(original)
    (seg,) = segment(doc)
    seg.target = target
    with pymupdf.open(stream=export_pdf(original, original, doc, [seg], "mono"), filetype="pdf") as pdf:
        (span,) = [
            span
            for text_block in pdf[0].get_textpage().extractDICT()["blocks"]
            for line in text_block["lines"]
            for span in line["spans"]
        ]
        assert span["text"] == seg.target and span["size"] == pytest.approx(size)


def test_export_squeezes_first_line_into_clipped_box_next_to_another_block(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    original = tmp_path / "first-line.pdf"
    block = _block(0, 0, [20, -5.65, 180, -0.65], "Synthetic text.")
    block.style.font_size = 15
    below = _block(0, 1, [20, 6.1, 180, 19], "Keep this.")
    below.translatable = False
    doc = Document(job_id="clipped", pages=[Page(index=0, width=220, height=120, blocks=[block, below])])
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=220, height=120)
        page.insert_text((20, 16), "Keep this.", fontsize=9)
        pdf.save(original)
    (seg,) = segment(doc)
    seg.target = "首行"
    with pymupdf.open(stream=export_pdf(original, original, doc, [seg], "mono"), filetype="pdf") as pdf:
        (first_line,) = pdf[0].search_for("首行")
        assert first_line.y1 <= below.bbox.y0
        assert pdf[0].search_for("Keep this.")
    assert "page=1 block_id=p0-b0" in caplog.text


def test_unlocated_formula_does_not_shift_citation_link(tmp_path: Path):
    original, background = tmp_path / "citation.pdf", tmp_path / "citation-background.pdf"
    block = _block(0, 0, [20, 20, 205, 70], "Synthetic text.")
    block.spans = [
        Span(bbox=block.bbox, text="We use", size=10),
        Span(bbox=block.bbox, kind="formula", formula="m1", size=10),
        Span(bbox=block.bbox, text="as shown in [1].", size=10),
    ]
    block.formulas = [InlineFormula(key="m1", latex="x_1", bbox=block.bbox, located=False)]
    doc = Document(job_id="citation", pages=[Page(index=0, width=220, height=120, blocks=[block])])
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=220, height=120)
        page.insert_text((20, 38), "We use x_1 as shown in [1].", fontsize=10)
        page.insert_link({"kind": pymupdf.LINK_GOTO, "from": page.search_for("[1]")[0], "page": 0})
        pdf.save(original)
    (seg,) = segment(doc)
    assert seg.formulas == {"m1": "x_1"} and not seg.formula_refs
    seg.target = "使用<m1/>，参见[1]。"
    make_background(original, [block], background)
    with pymupdf.open(stream=export_pdf(original, background, doc, [seg], "mono"), filetype="pdf") as pdf:
        page = pdf[0]
        (formula,) = page.search_for("$x_1$")
        (citation,) = page.search_for("[1]")
        (link,) = page.get_links()
        assert not link["from"].intersects(formula)
        assert citation.x0 < link["from"].x0 < link["from"].x1 < citation.x1
        assert link["from"].intersects(citation)
