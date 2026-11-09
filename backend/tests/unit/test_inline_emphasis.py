"""局部粗体从字符提取、融合到翻译输入保持对应，使用合成 PDF。"""

import re

import pymupdf
import pytest

from pdftr.domain.chars import Char
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType, Document, InlineFormula, Page, Span
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutPage, LayoutResult, LayoutSpan
from pdftr.extraction.chars import extract
from pdftr.fusion.build import fuse
from pdftr.fusion.emphasis import bold_ranges
from pdftr.fusion.style import block_style
from pdftr.translation.segmenter import _join, block_source, segment

BOX = Rect.of([20, 20, 560, 40])


def chars(text: str, emphasized: str) -> list[Char]:
    start = text.find(emphasized)
    return [
        Char(c=c, bbox=Rect.of([20 + i * 5, 20, 25 + i * 5, 30]), font="Times", size=10,
             line=0, bold=bool(emphasized) and start <= i < start + len(emphasized))
        for i, c in enumerate(text)
    ]  # fmt: skip


def block(spans: list[Span], *, bid: str = "p0-b0", **kwargs) -> Block:
    return Block(id=bid, page=0, type=kwargs.pop("type", BlockType.TEXT), bbox=BOX, order=0,
                 style=kwargs.pop("style", BlockStyle(font_size=10)), spans=spans,
                 translatable=True, **kwargs)  # fmt: skip


def span(text: str, **kwargs) -> Span:
    return Span(text=text, bbox=BOX, size=10, **kwargs)


def test_exact_normalized_mapping_preserves_parser_text_and_internal_word():
    text = "prefixfix and ordinary text"
    bold, ranges = bold_ranges(text, chars(text, "fixfix"))
    assert not bold and ranges == [(3, 9)]
    item = span(text, bold=bold, bold_ranges=ranges)
    assert item.text == text
    assert block_source(block([item]), {}) == "pre<b>fixfix</b> and ordinary text"


def test_ligature_mapping_and_whitespace_use_original_parser_indices():
    bold, ranges = bold_ranges("fine    text", chars("ﬁne text", "ﬁ"))
    assert not bold and ranges == [(0, 2)]
    assert block_source(block([span("fine    text", bold_ranges=ranges)]), {}) == "<b>fi</b>ne text"
    assert bold_ranges("some  key   words remain", chars("some key words remain", "key words")) == (False, [(6, 17)])


@pytest.mark.parametrize("parser", ["Other paper content", "We lose words.", "SAMPLE TEXT", ""])
def test_mismatch_never_guesses_or_changes_parser_content(parser):
    assert bold_ranges(parser, chars("Sample text", "Sample")) == (False, [])
    assert block_source(block([span(parser)]), {}) == parser


def test_full_span_bold_keeps_local_tag_but_heading_does_not_duplicate_it():
    bold, ranges = bold_ranges("Important", chars("Important", "Important"))
    assert bold and ranges == []
    item = span("Important", bold=bold)
    assert block_source(block([item]), {}) == "<b>Important</b>"
    assert block_source(block([item], style=BlockStyle(font_size=10, bold=True)), {}) == "Important"
    assert block_source(block([item], type=BlockType.TITLE), {}) == "Important"


def test_majority_bold_body_does_not_overwrite_its_regular_suffix():
    text = "Important emphasized statement then normal"
    emphasized = "Important emphasized statement"
    source_chars = chars(text, emphasized)
    style = block_style(source_chars)
    bold, ranges = bold_ranges(text, source_chars)
    assert len(emphasized) > len(text) / 2 and not style.bold
    assert block_source(block([span(text, bold=bold, bold_ranges=ranges)], style=style), {}) == (
        "<b>Important emphasized statement</b> then normal"
    )
    assert block_style(chars(text, text)).bold


def test_line_end_hyphens_and_bold_tags_remain_balanced():
    assert _join("a <b>representa-</b>", "<b>tion</b> survives") == "a <b>representation</b> survives"
    assert _join("<b>left-to-</b>", "<b>right</b>") == "<b>left-to-right</b>"
    assert _join("<b>pre-</b>", "Training") == "<b>pre-</b> Training"
    assert _join("<b>pre-</b>", "fix") == "<b>pre</b>fix"


def test_cross_page_chain_formulas_and_numeric_only_still_work():
    first = block([span("A representa-", bold_ranges=[(2, 13)])], bid="p0-b0")
    second = block(
        [span("tion", bold=True), Span(kind="formula", formula="m1", bbox=BOX, size=10), span("is useful.")],
        bid="p1-b0",
        continues_from="p0-b0",
        formulas=[InlineFormula(key="m1", latex="x", bbox=BOX)],
    )
    second.page = 1
    numeric = block([span("42", bold=True)], bid="p1-b1")
    doc = Document(
        job_id="synthetic",
        pages=[
            Page(index=0, width=600, height=800, blocks=[first]),
            Page(index=1, width=600, height=800, blocks=[second, numeric]),
        ],
    )
    (seg,) = segment(doc)
    assert seg.source == "A <b>representation</b> <m1/> is useful."
    assert seg.formulas == {"m1": "x"}
    assert seg.source.count("<b>") == seg.source.count("</b>") == 1
    assert sum(p.chars for p in seg.parts) == sum(
        len(block_source(b, {"m1": "m1"}).replace("<b>", "").replace("</b>", "")) for b in [first, second]
    )


def test_extract_fuse_segment_keeps_mixed_bold_without_modifying_body(tmp_path):
    path = tmp_path / "synthetic-mixed-weight.pdf"
    parts = [("A normal paragraph highlights ", "tiro"), ("key words", "tibo"), (" among ordinary text.", "tiro")]
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=800)
        x = 30.0
        for text, font in parts:
            page.insert_text((x, 100), text, fontname=font, fontsize=12)
            x += pymupdf.get_text_length(text, fontname=font, fontsize=12)
        pdf.save(path)
    pages = extract(path)
    page = pages[0]
    assert any(c.bold for c in page.chars)
    assert any(not c.bold and not c.c.isspace() for c in page.chars)
    box = Rect(x0=min(c.bbox.x0 for c in page.chars), y0=min(c.bbox.y0 for c in page.chars),
               x1=max(c.bbox.x1 for c in page.chars), y1=max(c.bbox.y1 for c in page.chars))  # fmt: skip
    body = "".join(text for text, _ in parts)
    layout = LayoutResult(parser="synthetic", pages=[LayoutPage(index=0, width=600, height=800, blocks=[
        LayoutBlock(type=BlockType.TEXT, bbox=box, order=0, lines=[
            LayoutLine(bbox=box, spans=[LayoutSpan(kind="text", content=body, bbox=box)])
        ])
    ])])  # fmt: skip
    doc, _ = fuse("synthetic", layout, pages, used_cloud=False)
    (fused,) = doc.blocks()
    assert not fused.style.bold
    assert fused.spans[0].text == body
    assert fused.spans[0].bold_ranges
    (seg,) = segment(doc)
    assert seg.source == "A normal paragraph highlights <b>key words</b> among ordinary text."
    assert re.sub(r"</?b>", "", seg.source) == body
    # 解析器保留了另一种正文表述时，不能为了转移字重改回 PDF 文字或猜偏移。
    parser_body = body.replace("key words", "important terms")
    layout.pages[0].blocks[0].lines[0].spans[0].content = parser_body
    different, _ = fuse("synthetic", layout, pages, used_cloud=False)
    assert different.blocks()[0].spans[0].text == parser_body
    assert not different.blocks()[0].spans[0].bold_ranges
    assert segment(different)[0].source == parser_body
