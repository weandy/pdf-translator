import json
from pathlib import Path

from pdftr.domain.ir import BlockType
from pdftr.parsing.mineru_normalize import normalize

FIXTURE = Path(__file__).parent.parent / "fixtures" / "mineru_layout_min.json"


def _layout():
    return normalize(json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_pages_and_version():
    lay = _layout()
    assert lay.parser == "mineru"
    assert lay.parser_version == "hybrid/3.4.4"
    assert [p.index for p in lay.pages] == [0, 1]
    assert (lay.pages[0].width, lay.pages[0].height) == (600, 800)


def test_block_types_and_children_are_flattened():
    types = [b.type for b in _layout().pages[0].blocks]
    assert types == [
        BlockType.TITLE,
        BlockType.TEXT,
        BlockType.TEXT,  # 续行占位块
        BlockType.FORMULA,
        BlockType.FIGURE,  # image_body
        BlockType.CAPTION,  # image_caption
        BlockType.REFERENCE,
        BlockType.HEADER,
        BlockType.FOOTER,
        BlockType.FOOTNOTE,
    ]


def test_spans_formula_and_placeholder():
    blocks = _layout().pages[0].blocks
    para = blocks[1]
    kinds = [s.kind for s in para.lines[0].spans]
    assert kinds == ["text", "inline_formula", "text"]
    assert para.lines[0].spans[1].content == "f(x)"
    assert blocks[2].placeholder is True
    assert blocks[3].latex == "E = mc^2"


def test_discarded_but_footnote_kept():
    blocks = {b.source_type: b for b in _layout().pages[0].blocks}
    assert blocks["header"].discarded is True
    assert blocks["page_number"].discarded is True
    assert blocks["page_footnote"].discarded is False
