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


def test_small_caps_roman_section_is_promoted_to_title():
    layout = {
        "_backend": "hybrid",
        "_version_name": "3.4.4",
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [612, 792],
                "para_blocks": [
                    {
                        "type": "text",
                        "index": 0,
                        "bbox": [100, 400, 260, 412],
                        "lines": [
                            {
                                "bbox": [100, 400, 260, 412],
                                "spans": [
                                    {
                                        "type": "text",
                                        "bbox": [100, 400, 260, 412],
                                        "content": "I. INTRODUCTION",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    block = normalize(layout).pages[0].blocks[0]
    assert block.type is BlockType.TITLE
    assert block.source_type == "text"


def _untyped_section(text: str, *, spans: list[str] | None = None, lines: list[str] | None = None) -> dict:
    line_texts = lines or [text]
    line_data = []
    for line in line_texts:
        pieces = spans if len(line_texts) == 1 and spans is not None else [line]
        line_data.append(
            {
                "bbox": [100, 400, 400, 412],
                "spans": [{"type": "text", "bbox": [100, 400, 400, 412], "content": piece} for piece in pieces],
            }
        )
    return {"type": "text", "index": 0, "bbox": [100, 400, 400, 430], "lines": line_data}


def test_numbered_title_case_and_split_spans_are_promoted():
    layout = {
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [612, 792],
                "para_blocks": [
                    _untyped_section("1. Introduction", spans=["1.", "Introduction"]),
                    _untyped_section("2.1 Background"),
                    _untyped_section("A. Background"),
                ],
            }
        ]
    }
    assert [b.type for b in normalize(layout).pages[0].blocks] == [
        BlockType.TITLE,
        BlockType.TITLE,
        BlockType.TITLE,
    ]


def test_two_line_section_title_is_promoted_but_sentence_is_not():
    layout = {
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [612, 792],
                "para_blocks": [
                    _untyped_section("1. A Long Section Title", lines=["1. A Long Section", "Title"]),
                    _untyped_section("1. this is an ordinary sentence."),
                    _untyped_section("2. This sentence ends with a colon:"),
                ],
            }
        ]
    }
    assert [b.type for b in normalize(layout).pages[0].blocks] == [
        BlockType.TITLE,
        BlockType.TEXT,
        BlockType.TEXT,
    ]


def test_chinese_numbered_section_is_promoted():
    layout = {
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [612, 792],
                "para_blocks": [_untyped_section("一、研究方法"), _untyped_section("第 2 章 实验结果")],
            }
        ]
    }
    assert [b.type for b in normalize(layout).pages[0].blocks] == [BlockType.TITLE, BlockType.TITLE]
