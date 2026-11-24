"""导出用的译文拆分与前端 shared/text/split.ts 行为一致（用例与 split.test.ts 相同）。"""

import json
import re
from pathlib import Path

import pymupdf
import pytest

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType, Document, FormulaRef, Page, Segment, SegmentPart
from pdftr.rendering.export import (
    _balance_rich_parts,
    _translated_rects,
    _typeset_all,
    block_pieces,
    place_links,
    split_by_parts,
)
from pdftr.rendering.links import PdfLink


@pytest.mark.parametrize(
    "vector",
    json.loads((Path(__file__).parents[3] / "frontend/src/shared/text/split.fixture.json").read_text(encoding="utf-8")),
)
def test_shared_cross_engine_split_vectors(vector):
    assert split_by_parts(vector["text"], vector["weights"]) == vector["expected"]


def test_single_part_not_split():
    assert split_by_parts("全部译文", [10]) == ["全部译文"]


def test_long_integer_stays_splittable_next_to_decimal():
    digits = "7" * 12000
    assert split_by_parts(digits + " 3.14", [1, 1, 1]) == [digits[:4002], digits[4002:8003], digits[8003:] + " 3.14"]


def test_split_at_punctuation_by_source_ratio():
    text = "第一部分的内容比较长，一直写到这里。第二部分短。"
    parts = split_by_parts(text, [30, 10])
    assert len(parts) == 2 and "".join(parts) == text
    assert parts[0].endswith(("。", "，"))


def test_placeholder_never_split():
    text = "甲" * 9 + "<m12/>" + "乙" * 9
    for parts in (split_by_parts(text, [10, 10]), split_by_parts(text, [11, 9])):
        assert "".join(parts) == text
        assert all(not re.search(r"<m\d*$", p) for p in parts)


def test_three_parts_all_non_empty():
    text = "一，二，三，四，五，六，七，八，九。"
    parts = split_by_parts(text, [6, 6, 6])
    assert len(parts) == 3 and "".join(parts) == text and all(parts)


def test_bold_scope_continues_across_blocks_without_losing_text_or_formula():
    text = "<b>" + "甲" * 9 + "<m12/>" + "乙" * 9 + "</b>普通"
    parts = split_by_parts(text, [10, 10])
    assert parts[0].startswith("<b>") and parts[0].endswith("</b>")
    assert parts[1].startswith("<b>") and parts[1].endswith("</b>普通")
    assert "".join(re.sub(r"</?b>", "", p) for p in parts) == re.sub(r"</?b>", "", text)
    assert "".join(parts).count("<m12/>") == 1


def test_nested_style_scopes_are_balanced_per_part():
    assert _balance_rich_parts(["<b>甲<i>乙", "丙</i>丁</b>戊"]) == ["<b>甲<i>乙</i></b>", "<b><i>丙</i>丁</b>戊"]
    assert _balance_rich_parts(["<sup>2", "3</sup>普通"]) == ["<sup>2</sup>", "<sup>3</sup>普通"]
    assert _balance_rich_parts(["<sub>i", "j</sub>普通"]) == ["<sub>i</sub>", "<sub>j</sub>普通"]


def test_empty_parts_do_not_create_empty_styles_and_unknown_tags_remain_plain():
    assert _balance_rich_parts(["<b>", "", "甲", "", "</b>", "乙"]) == ["", "", "<b>甲</b>", "", "", "乙"]
    assert _balance_rich_parts(["<script>甲", "乙</script>"]) == ["<script>甲", "乙</script>"]


def test_link_survives_when_trailing_punctuation_and_formatting_move_the_cross_page_cut():
    bounds = Rect.of([10, 20, 90, 40])
    blocks = [
        Block(
            id=f"p{i}-b0", page=i, type=BlockType.TEXT, bbox=bounds, order=0,
            style=BlockStyle(font_size=10), translatable=True,
        )
        for i in range(2)
    ]  # fmt: skip
    doc = Document(
        job_id="synthetic",
        pages=[Page(index=i, width=200, height=300, blocks=[b]) for i, b in enumerate(blocks)],
    )
    seg = Segment(
        id="s1", job_id=doc.job_id, block_ids=[b.id for b in blocks], source="A cross-page citation [12].",
        target="<b>前文（见[12]）</b>，！？后续内容",
        parts=[SegmentPart(block_id=b.id, chars=1) for b in blocks],
    )  # fmt: skip
    raw = PdfLink(page=1, rect=bounds, block_id=blocks[1].id, kind="cite", anchor="12", dest_page=0)
    (placed,) = place_links([raw], doc, [seg])
    assert placed.block_id == blocks[0].id and placed.text == "12" and raw.block_id == blocks[1].id


def _body(bid, bounds, page=0, kind=BlockType.TEXT):
    return Block(
        id=bid, page=page, type=kind, bbox=Rect.of(bounds), order=0,
        style=BlockStyle(font_size=8), translatable=kind is BlockType.TEXT,
    )  # fmt: skip


def _document(members, obstacles=()):
    doc = Document(
        job_id="synthetic",
        pages=[
            Page(index=i, width=600, height=300, blocks=[b for b in [*members, *obstacles] if b.page == i])
            for i in sorted({b.page for b in members})
        ],
    )
    seg = Segment(
        id="s", job_id=doc.job_id, block_ids=[b.id for b in members],
        parts=[SegmentPart(block_id=b.id, chars=8) for b in members],
        source="Synthetic [12] and [12].", target="同栏连接[12]，function<m1/>结束[12]。",
    )  # fmt: skip
    return doc, seg


def test_same_column_parts_share_layout_and_relocate_links_and_formula():
    members = [_body("a", [36, 40, 235, 64]), _body("b", [40, 68, 228, 90]), _body("c", [39, 92, 150, 102])]
    doc, seg = _document(members)
    ref = Rect.of([52, 93, 60, 100])
    seg.formula_refs["m1"] = FormulaRef(page=0, bbox=ref)
    assert block_pieces(doc, [seg]) == {"a": seg.target, "b": "", "c": ""}
    links = [PdfLink(page=0, rect=b.bbox, block_id=b.id, kind="cite", anchor="12", dest_page=0) for b in members[::2]]
    resolved = place_links(links, doc, [seg])
    assert [(link.block_id, link.text, link.nth) for link in resolved] == [("a", "12", 0), ("a", "12", 1)]
    with pymupdf.open() as src, pymupdf.open() as out:
        src.new_page(width=600, height=300).draw_rect(pymupdf.Rect(53, 94, 59, 99), fill=(0, 0, 0))
        out.new_page(width=600, height=300)
        placed, images, overflow = _typeset_all(src, out, doc, [seg], None)
        assert not overflow and placed["a"] is placed["b"] is placed["c"]
        assert images["a"] is images["b"] is images["c"] and len(images["a"]["m1"]) == 1
        assert "".join(r.text for r in placed["a"]) == "同栏连接[12]，function结束[12]。"
        assert len({run.baseline for run in placed["a"]}) == 1
        first, second = [_translated_rects(link, placed)[0] for link in resolved]
        assert first.x1 < second.x0 and second.y1 < members[1].bbox.y0


@pytest.mark.parametrize(
    ("second", "obstacles"),
    [
        (_body("b", [314, 67, 540, 90]), []),
        (_body("b", [36, 67, 230, 90], page=1), []),
        (_body("b", [36, 67, 230, 90]), [_body("figure", [80, 61, 100, 66], kind=BlockType.FIGURE)]),
        (_body("b", [36, 67, 150, 90]), [_body("table", [165, 75, 200, 85], kind=BlockType.TABLE)]),
        (_body("b", [36, 80, 230, 95]), []),
        (_body("b", [36, 50, 230, 75]), []),
        (_body("b", [50, 67, 230, 90]), []),
        (_body("b", [36, 67, 250, 90]), []),
        (_body("b", [36, 67, 230, 90], kind=BlockType.CAPTION), []),
    ],
)
def test_parts_stay_separate_across_columns_pages_obstacles_or_bad_geometry(second, obstacles):
    doc, seg = _document([_body("a", [36, 40, 230, 60]), second], obstacles)
    assert seg.target
    assert list(block_pieces(doc, [seg]).values()) == split_by_parts(seg.target, [8, 8])
