"""端到端融合：按夹具里的坐标生成 PDF，再与夹具版面结果融合。"""

import json
from pathlib import Path

import pymupdf
import pytest

from pdftr.domain.ir import BlockType
from pdftr.extraction.chars import extract
from pdftr.fusion.build import fuse
from pdftr.parsing.mineru_normalize import normalize

FIXTURE = Path(__file__).parent.parent / "fixtures" / "mineru_layout_min.json"

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
