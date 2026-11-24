"""Q4 Round 5 终极共性缺陷收口回归测试（R5-1 ~ R5-5）。"""

import argparse
import asyncio
import json
from pathlib import Path

import pymupdf
import pytest

from pdftr import cli as cli_mod
from pdftr.domain.chars import Char, CharPage
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import (
    Block,
    BlockStyle,
    BlockType,
    Document,
    FormulaRef,
    Page,
    PageKind,
    Segment,
    SegmentPart,
    SegmentStatus,
    Span,
)
from pdftr.domain.job import Job, JobOptions
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutPage, LayoutResult, LayoutSpan
from pdftr.domain.llm import ChatOptions, ChatResult
from pdftr.fusion.align import align
from pdftr.fusion.build import fuse
from pdftr.fusion.formula_align import clean_formula_lines
from pdftr.glossary.extract import (
    PROMPT as EXTRACT_PROMPT,
)
from pdftr.glossary.extract import (
    _skip_extracted_term,
    extract_acronym_definitions,
    extract_terms,
)
from pdftr.glossary.extract import (
    parse as parse_terms,
)
from pdftr.pipeline.events import EventBus
from pdftr.pipeline.runner import Pipeline, PipelineDeps
from pdftr.pipeline.secrets import Secrets, SecretStore
from pdftr.pipeline.usecases import EXPORT_CACHE_VERSION
from pdftr.rendering.background import make_background
from pdftr.rendering.export import export_pdf
from pdftr.rendering.links import VERSION as LINKS_VERSION
from pdftr.rendering.links import PdfLink, extract_links, locate
from pdftr.storage.cache import SqliteTranslationCache
from pdftr.storage.files import FileArtifactStore
from pdftr.storage.sqlite import SqliteStore
from pdftr.translation.prompt import PROMPT_VERSION, RULES


def _char(
    c: str,
    x0: float,
    x1: float,
    y0: float = 100.0,
    y1: float = 110.0,
    *,
    line: int = 0,
    size: float = 10.0,
    font: str = "NimbusRomNo9L-Regu",
    italic: bool = False,
) -> Char:
    return Char(
        c=c,
        bbox=Rect(x0=x0, y0=y0, x1=x1, y1=y1),
        font=font,
        size=size,
        line=line,
        italic=italic,
    )


# ---- R5-1a: 右侧黑板粗体余域回收 ----


def test_r5_1a_recover_right_blackboard_codomain_and_reject_normal_words():
    chars = [
        _char("g", 100.0, 106.0, 517.0, 529.0, size=12.0, font="NimbusRomNo9L-ReguItal", italic=True),
        _char(":", 107.0, 110.0, 517.0, 529.0, size=12.0),
        _char("X", 112.0, 120.0, 517.0, 529.0, size=12.0, font="NimbusRomNo9L-ReguItal", italic=True),
        _char("→", 123.0, 135.0, 517.0, 529.0, size=12.0, font="CMSY10"),
        _char("R", 137.8, 146.4, 517.0, 529.0, size=12.0, font="MSBM10"),
        _char("b", 149.4, 155.4, 517.0, 529.0, size=12.0),
        _char("y", 155.4, 161.4, 517.0, 529.0, size=12.0),
    ]
    line = LayoutLine(
        bbox=Rect(x0=60.0, y0=516.0, x1=162.0, y1=530.0),
        spans=[
            LayoutSpan(kind="text", content="Define ", bbox=Rect(x0=60.0, y0=516.0, x1=98.0, y1=529.0)),
            LayoutSpan(
                kind="inline_formula",
                content=r"\tilde{g} \colon X ",
                bbox=Rect(x0=99.0, y0=516.0, x1=134.0, y1=529.0),
                located=True,
            ),
            LayoutSpan(kind="text", content=" R by", bbox=Rect(x0=137.0, y0=516.0, x1=162.0, y1=529.0)),
        ],
    )
    block = LayoutBlock(type=BlockType.TEXT, bbox=line.bbox, order=0, lines=[line])
    cleaned = clean_formula_lines(block, chars)
    spans = cleaned.lines[0].spans
    assert len(spans) == 3
    assert spans[1].kind == "inline_formula"
    assert spans[1].content == r"\tilde{g} \colon X \to \mathbb{R}"
    assert spans[1].bbox.x1 >= 146.4
    assert spans[2].kind == "text"
    assert spans[2].content.strip() == "by"
    assert spans[2].bbox.x0 >= 146.4

    # 右侧 span 仅含被剥离的 R 时，应直接移除该空 span。
    line_only_r = LayoutLine(
        bbox=line.bbox,
        spans=[
            spans[0],
            LayoutSpan(
                kind="inline_formula",
                content=r"\tilde{g} \colon X",
                bbox=Rect(x0=99.0, y0=516.0, x1=134.0, y1=529.0),
                located=True,
            ),
            LayoutSpan(kind="text", content=" R", bbox=Rect(x0=137.0, y0=516.0, x1=147.0, y1=529.0)),
        ],
    )
    cleaned_only_r = clean_formula_lines(
        LayoutBlock(type=BlockType.TEXT, bbox=line.bbox, order=0, lines=[line_only_r]), chars
    )
    assert len(cleaned_only_r.lines[0].spans) == 2
    assert cleaned_only_r.lines[0].spans[1].content == r"\tilde{g} \colon X \to \mathbb{R}"

    # 负例 1：右侧是普通英文单词 "Result"（即使首字母 R 恰好用黑板字体），绝不误吞。
    line_word = LayoutLine(
        bbox=line.bbox,
        spans=[
            LayoutSpan(
                kind="inline_formula",
                content=r"g \colon X",
                bbox=Rect(x0=99.0, y0=516.0, x1=134.0, y1=529.0),
                located=True,
            ),
            LayoutSpan(kind="text", content=" Result shows", bbox=Rect(x0=137.0, y0=516.0, x1=210.0, y1=529.0)),
        ],
    )
    kept_word = clean_formula_lines(LayoutBlock(type=BlockType.TEXT, bbox=line.bbox, order=0, lines=[line_word]), chars)
    assert kept_word.lines[0].spans[0].content == r"g \colon X"
    assert kept_word.lines[0].spans[1].content == " Result shows"

    # 负例 2：右侧单字母 R 不是黑板粗体数学字体，绝不误吞。
    plain_chars = [c.model_copy(update={"font": "NimbusRomNo9L-Regu"}) if c.c == "R" else c for c in chars]
    kept_plain = clean_formula_lines(block, plain_chars)
    assert kept_plain.lines[0].spans[1].content == r"\tilde{g} \colon X "
    assert kept_plain.lines[0].spans[2].content == " R by"


# ---- R5-1b: 公式内部缺失映射箭头 \to 同步补齐 ----


def test_r5_1b_restore_native_mapping_arrow_only_when_native_has_arrow():
    chars_with_arrow = [
        _char("U", 71.0, 80.0, 448.0, 459.0, size=12.0, font="NimbusRomNo9L-ReguItal", italic=True),
        _char(":", 82.0, 86.0, 448.0, 459.0, size=12.0),
        _char("R", 89.0, 98.0, 448.0, 459.0, size=12.0, font="MSBM10"),
        _char("→", 101.0, 113.0, 448.0, 459.0, size=12.0, font="CMSY10"),
        _char("R", 115.0, 124.0, 448.0, 459.0, size=12.0, font="MSBM10"),
    ]
    box = Rect(x0=70.0, y0=447.0, x1=125.0, y1=460.0)
    for raw_latex, expected in [
        (r"U : \mathbb { R }  \mathbb { R }", r"U : \mathbb { R } \to \mathbb { R }"),
        (
            r"U _ { 1 } , U _ { 2 } \colon [ 0 , \infty )  \mathbb { R }",
            r"U _ { 1 } , U _ { 2 } \colon [ 0 , \infty ) \to \mathbb { R }",
        ),
        (r"U : [ 0 , \infty ) \mathbb { R }", r"U : [ 0 , \infty ) \to \mathbb { R }"),
    ]:
        line = LayoutLine(
            bbox=box, spans=[LayoutSpan(kind="inline_formula", content=raw_latex, bbox=box, located=True)]
        )
        out = clean_formula_lines(LayoutBlock(type=BlockType.TEXT, bbox=box, order=0, lines=[line]), chars_with_arrow)
        assert out.lines[0].spans[0].content == expected

    # 已有 \to 时不重复插入。
    existing = r"U : \mathbb { R } \to \mathbb { R }"
    line_ok = LayoutLine(bbox=box, spans=[LayoutSpan(kind="inline_formula", content=existing, bbox=box, located=True)])
    assert (
        clean_formula_lines(LayoutBlock(type=BlockType.TEXT, bbox=box, order=0, lines=[line_ok]), chars_with_arrow)
        .lines[0]
        .spans[0]
        .content
        == existing
    )

    # 原生字符不含 → 时不动。
    chars_no_arrow = [c for c in chars_with_arrow if c.c != "→"]
    missing = r"U : \mathbb { R }  \mathbb { R }"
    line_no = LayoutLine(bbox=box, spans=[LayoutSpan(kind="inline_formula", content=missing, bbox=box, located=True)])
    assert (
        clean_formula_lines(LayoutBlock(type=BlockType.TEXT, bbox=box, order=0, lines=[line_no]), chars_no_arrow)
        .lines[0]
        .spans[0]
        .content
        == missing
    )


# ---- R5-1c: 公式左缘擦伤截断的相邻英文短词恢复与虚假 \dot 剥离 ----


def test_r5_1c_recover_left_grazed_word_tightens_bbox_and_strips_spurious_dot():
    chars = [
        _char("i", 85.05, 87.05, 679.27, 689.32, size=9.0, font="ArialMT"),
        _char("f", 87.05, 89.55, 679.27, 689.32, size=9.0, font="ArialMT"),
        _char("F", 93.55, 99.05, 679.27, 689.32, size=9.0, font="ArialMT"),
        _char("≥", 103.05, 107.99, 679.27, 689.32, size=9.0, font="ArialMT"),
        _char("χ", 111.99, 116.71, 679.27, 689.32, size=9.0, font="ArialMT"),
        _char("2", 116.71, 119.72, 679.11, 685.14, size=5.4, font="ArialMT"),
    ]
    line = LayoutLine(
        bbox=Rect(x0=40.0, y0=679.0, x1=150.0, y1=690.0),
        spans=[
            LayoutSpan(kind="text", content="rejected i ", bbox=Rect(x0=40.0, y0=679.0, x1=88.0, y1=689.5)),
            LayoutSpan(
                kind="inline_formula",
                content=r"\dot { \mathsf { F } } \geq \mathsf { X } ^ { 2 }",
                bbox=Rect(x0=89.0, y0=679.12, x1=120.0, y1=689.0),
                located=True,
            ),
            LayoutSpan(kind="text", content=" critical value", bbox=Rect(x0=121.0, y0=679.0, x1=150.0, y1=689.5)),
        ],
    )
    cleaned = clean_formula_lines(LayoutBlock(type=BlockType.TEXT, bbox=line.bbox, order=0, lines=[line]), chars)
    spans = cleaned.lines[0].spans
    assert spans[0].content == "rejected if "
    assert spans[1].bbox.x0 == pytest.approx(93.05)
    assert spans[1].content == r"\mathsf { F } \geq \mathsf { X } ^ { 2 }"

    # 负例 1：原生确实含上方点字形时，绝不剥离合法的 \dot{x}。
    real_dot_chars = [
        _char("i", 85.05, 87.05, 679.27, 689.32, size=9.0, font="ArialMT"),
        _char("f", 87.05, 89.55, 679.27, 689.32, size=9.0, font="ArialMT"),
        _char("˙", 95.0, 97.0, 678.5, 680.5, size=6.0, font="CMSY10"),
        _char("x", 93.55, 99.05, 679.27, 689.32, size=9.0, font="CMMI10", italic=True),
    ]
    line_dot = LayoutLine(
        bbox=line.bbox,
        spans=[
            LayoutSpan(kind="text", content="let i ", bbox=Rect(x0=40.0, y0=679.0, x1=88.0, y1=689.5)),
            LayoutSpan(
                kind="inline_formula",
                content=r"\dot{x}",
                bbox=Rect(x0=89.0, y0=678.5, x1=100.0, y1=689.5),
                located=True,
            ),
        ],
    )
    kept_dot = clean_formula_lines(
        LayoutBlock(type=BlockType.TEXT, bbox=line.bbox, order=0, lines=[line_dot]), real_dot_chars
    )
    assert kept_dot.lines[0].spans[0].content == "let if "
    assert kept_dot.lines[0].spans[1].content == r"\dot{x}"

    # 负例 2：紧贴的字符属于斜体数学符号时，绝不误当作正文短词剥离。
    italic_math_chars = [
        _char("i", 85.05, 87.05, 679.27, 689.32, size=9.0, font="ArialMT"),
        _char("f", 87.05, 89.55, 679.27, 689.32, size=9.0, font="CMMI10", italic=True),
        _char("F", 93.55, 99.05, 679.27, 689.32, size=9.0, font="CMMI10", italic=True),
    ]
    untouched = clean_formula_lines(
        LayoutBlock(type=BlockType.TEXT, bbox=line.bbox, order=0, lines=[line]), italic_math_chars
    )
    assert untouched.lines[0].spans[0].content == "rejected i "
    assert untouched.lines[0].spans[1].bbox.x0 == 89.0
    assert untouched.lines[0].spans[1].content == r"\dot { \mathsf { F } } \geq \mathsf { X } ^ { 2 }"


# ---- R5-2: Prompt v7 关系符结尾衔接规则 ----


def test_r5_2_prompt_v7_contains_trailing_relation_attachment_rule():
    assert PROMPT_VERSION == "v7"
    assert "以比较符或关系符结尾" in RULES
    assert "与右操作数直接衔接" in RULES
    assert "禁止写成“<m2/>在……处的临界值”" in RULES


# ---- R5-3a: 上标脚注链接精准恢复 ----


def test_r5_3a_footnote_links_match_superscripts_only():
    assert LINKS_VERSION == 5

    def fn(anchor: str, x: float, kind: str = "Hfootnote") -> PdfLink:
        return PdfLink(
            page=0,
            rect=Rect(x0=x, y0=10.0, x1=x + 6.0, y1=22.0),
            anchor=anchor,
            kind=kind,
            block_id="b1",
            dest_page=0,
        )

    pieces = {
        "b1": (
            "我们测试了 1 种方法、2 组模型、3 个数据集和 4 次实验，"
            "并在基础方案<sup>1</sup>、复杂函数<sup>2</sup>、收敛率<sup>3</sup>与瓶颈设计<sup>4</sup>处加注。"
        )
    }
    links = [
        fn("s1,", 10.0, "Hfootnote"),
        fn("2", 20.0, "footnote"),
        fn("r3.", 30.0, "Hfootnote"),
        fn("n4.", 40.0, "Hfootnote"),
    ]
    got = locate(links, pieces)
    assert [(lk.text, lk.nth) for lk in got] == [("1", 1), ("2", 1), ("3", 1), ("4", 1)]

    # 正文里只有普通数字、没有 <sup> 时，绝不误配给脚注链接。
    no_sup = locate([fn("s1,", 10.0), fn("2", 20.0, "footnote")], {"b1": "第 1 种方法共 2 组实验。"})
    assert [(lk.text, lk.nth) for lk in no_sup] == [("", 0), ("", 0)]


# ---- R5-3b: 公式内 1pt 扁平上标引文热点几何映射（单语/双语 + 跨块） ----


def test_r5_3b_formula_internal_links_mapped_in_mono_and_dual_exports(tmp_path: Path):
    assert EXPORT_CACHE_VERSION == 14

    orig_pdf = tmp_path / "orig.pdf"
    bg_pdf = tmp_path / "bg.pdf"
    with pymupdf.open() as src:
        p0 = src.new_page(width=400, height=300)
        p0.insert_text((40, 80), "Cathode (LiMn2O4)1 and LNMO2 studied.", fontsize=11)
        p1 = src.new_page(width=400, height=300)
        p1.insert_text((40, 60), "[1] Ref one. [2] Ref two.", fontsize=11)
        # 1pt 高扁平上标链接框，第一条轻微超出公式右缘 1.2pt（模拟 APS/REVTeX 场景）。
        src[0].insert_link(
            {
                "kind": pymupdf.LINK_GOTO,
                "from": pymupdf.Rect(145.0, 72.0, 151.2, 73.0),
                "page": 1,
                "to": pymupdf.Point(40, 60),
            }
        )
        src[0].insert_link(
            {
                "kind": pymupdf.LINK_GOTO,
                "from": pymupdf.Rect(210.0, 72.0, 215.0, 73.0),
                "page": 1,
                "to": pymupdf.Point(40, 80),
            }
        )
        src.save(orig_pdf)

    blk1 = Block(
        id="p0-b0",
        page=0,
        type=BlockType.TEXT,
        bbox=Rect(x0=35.0, y0=60.0, x1=360.0, y1=110.0),
        order=0,
        spans=[
            Span(text="Cathode <m1/> and <m2/> studied.", bbox=Rect(x0=35.0, y0=60.0, x1=360.0, y1=110.0), size=11.0)
        ],
        style=BlockStyle(font_size=11.0),
        translatable=True,
    )
    blk2 = Block(
        id="p0-b1",
        page=0,
        type=BlockType.TEXT,
        bbox=Rect(x0=35.0, y0=130.0, x1=360.0, y1=180.0),
        order=1,
        continues_from="p0-b0",
        spans=[Span(text="continued", bbox=Rect(x0=35.0, y0=130.0, x1=360.0, y1=180.0), size=11.0)],
        style=BlockStyle(font_size=11.0),
        translatable=True,
    )
    doc = Document(
        job_id="syn",
        pages=[
            Page(index=0, width=400, height=300, kind=PageKind.NATIVE, blocks=[blk1, blk2]),
            Page(index=1, width=400, height=300, kind=PageKind.NATIVE, blocks=[]),
        ],
    )
    seg = Segment(
        id="s0",
        job_id="syn",
        block_ids=["p0-b0", "p0-b1"],
        source="Cathode <m1/> and <m2/> studied.",
        formulas={"m1": r"\mathrm{(LiMn_2O_4)^1}", "m2": r"\mathrm{LNMO^2}"},
        formula_refs={
            "m1": FormulaRef(page=0, bbox=Rect(x0=85.0, y0=68.0, x1=150.0, y1=82.0)),
            "m2": FormulaRef(page=0, bbox=Rect(x0=175.0, y0=68.0, x1=216.0, y1=82.0)),
        },
        # 让 m1 落在第一块、m2 落到跨块兄弟第二块，验证同块与跨块均能精准映射。
        parts=[SegmentPart(block_id="p0-b0", chars=14), SegmentPart(block_id="p0-b1", chars=18)],
        target="正极材料<m1/>性能优异，后续实验进一步研究了<m2/>体系。",
        status=SegmentStatus.DONE,
    )
    make_background(orig_pdf, [blk1, blk2], bg_pdf)
    raw_links = extract_links(orig_pdf, doc, {"p0-b0", "p0-b1"})
    assert len(raw_links) == 2

    mono_bytes = export_pdf(orig_pdf, bg_pdf, doc, [seg], mode="mono", links=raw_links)
    with pymupdf.open(stream=mono_bytes, filetype="pdf") as mono:
        mono_links = [lk for lk in mono[0].get_links() if lk.get("page") == 1]
        assert len(mono_links) == 2
        for lk in mono_links:
            rect = lk["from"]
            assert rect.width >= 4.0 and rect.height >= 4.0
        # 一条落在第一块 y∈[60,110]，一条跨块落在第二块 y∈[130,180]
        ys = sorted(lk["from"].y0 for lk in mono_links)
        assert 55.0 <= ys[0] <= 115.0 and 125.0 <= ys[1] <= 185.0

    dual_bytes = export_pdf(orig_pdf, bg_pdf, doc, [seg], mode="dual", side="right", links=raw_links)
    with pymupdf.open(stream=dual_bytes, filetype="pdf") as dual:
        dual_links = [lk for lk in dual[0].get_links() if lk.get("page") == 1]
        left_links = [lk for lk in dual_links if lk["from"].x0 < 400.0]
        right_links = [lk for lk in dual_links if lk["from"].x0 >= 400.0]
        assert len(left_links) == 2 and len(right_links) == 2
        for lk in right_links:
            assert lk["from"].width >= 4.0 and lk["from"].height >= 4.0


# ---- R5-4: 双栏行尾闭合标点垂直空白夹缝回收与真碰撞拦截 ----


def test_r5_4_align_recovers_closing_bracket_in_right_column_vertical_gap_without_expanding_block():
    left_box = Rect(x0=49.0, y0=217.0, x1=312.0, y1=565.0)
    right_top = Rect(x0=315.0, y0=388.0, x1=560.0, y1=495.0)
    right_bottom = Rect(x0=315.0, y0=502.0, x1=511.0, y1=515.0)

    # ']' 的 y0=494.23 仅因 TOL=1.5 微擦 right_top.y1=495.0，其垂直中心 498.69 位于两块之间的纯空白栏隙。
    chars = [
        _char("3", 303.14, 308.13, 494.23, 503.14, line=47, size=9.0),
        _char("5", 308.13, 313.11, 494.23, 503.14, line=47, size=9.0),
        _char("]", 313.11, 316.42, 494.23, 503.14, line=47, size=9.0),
    ]
    assigned, orphans = align(chars, [left_box, right_top, right_bottom])
    assert [c.c for c in assigned[0]] == ["3", "5", "]"]
    assert orphans == []

    # 通过 fuse 验证 Block.bbox.x1 保持 312.0 不膨胀，仅单行 Span.bbox.x1 扩到 316.42 供去字擦除。
    layout = LayoutResult(
        parser="mineru",
        pages=[
            LayoutPage(
                index=0,
                width=600.0,
                height=800.0,
                blocks=[
                    LayoutBlock(
                        type=BlockType.TEXT,
                        bbox=left_box,
                        order=0,
                        lines=[
                            LayoutLine(
                                bbox=Rect(x0=49.0, y0=494.0, x1=312.0, y1=503.5),
                                spans=[
                                    LayoutSpan(
                                        kind="text", content="[35]", bbox=Rect(x0=300.0, y0=494.0, x1=312.0, y1=503.5)
                                    )
                                ],
                            )
                        ],
                    ),
                    LayoutBlock(type=BlockType.TEXT, bbox=right_top, order=1, lines=[]),
                    LayoutBlock(type=BlockType.TITLE, bbox=right_bottom, order=2, lines=[]),
                ],
            )
        ],
    )
    cp = CharPage(index=0, width=600.0, height=800.0, kind=PageKind.NATIVE, chars=chars)
    doc, _ = fuse("syn", layout, [cp], used_cloud=True)
    left_block = doc.pages[0].blocks[0]
    assert left_block.bbox.x1 == 312.0
    assert left_block.spans[0].bbox.x1 == pytest.approx(316.42)

    # 负例：若右栏同一行真有块覆盖垂直中心 498.69，则依然拦截左栏回收。
    right_same_row = Rect(x0=315.0, y0=490.0, x1=560.0, y1=508.0)
    assigned_blocked, _ = align(chars, [left_box, right_same_row])
    assert [c.c for c in assigned_blocked[0]] == ["3", "5"]
    assert [c.c for c in assigned_blocked[1]] == ["]"]


# ---- R5-5: 文内显式缩写定义提取与注入 + 单复数留英拦截 ----


def test_r5_5a_extract_acronym_definitions_and_prompt_injection(tmp_path: Path):
    texts = [
        "We call TCM (Typed Comparison Method) a pair of information type and string matcher.",
        "The Solid Electrolyte Interphase (SEI) grows on the anode surface.",
        "We build a deep Residual Network (ResNet) for image recognition.",
        "Graph Neural Networks (GNNs) are evaluated for each element (see Fig. 2) and other cases (e.g., baselines).",
    ]
    defs = extract_acronym_definitions(texts, candidates=("ResNet", "TCMs"))
    assert defs == {
        "TCM": "Typed Comparison Method",
        "ResNet": "Residual Network",
        "SEI": "Solid Electrolyte Interphase",
        "GNN": "Graph Neural Networks",
    }
    # 命中 candidates 的缩写（TCM、ResNet）优先排在前列，即使 SEI 在原文中出现更早。
    assert list(defs.keys())[:2] == ["TCM", "ResNet"]

    seen_prompts: list[str] = []

    class CaptureChat:
        model = "fake"

        async def list_models(self) -> list[str]:
            return []

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            seen_prompts.append(user)
            return ChatResult(text="领域：医疗信息学\nAI公共词库：停用\nTCM=类型化比较方法")

    found = asyncio.run(
        extract_terms(
            CaptureChat(),
            title="SNOMED CT Binding",
            abstract="Study of TCM.",
            intro="",
            headings=["Methods"],
            candidates=("TCM",),
            definitions=defs,
        )
    )
    assert found.terms == {"TCM": "类型化比较方法"}
    assert "文中明确定义的缩写" in seen_prompts[0]
    assert "TCM = Typed Comparison Method" in seen_prompts[0]

    # Pipeline._glossary 端到端透传 definitions 到 extract_terms。
    seen_prompts.clear()
    artifacts = FileArtifactStore(tmp_path / "artifacts")
    store = SqliteStore(tmp_path / "app.db")
    cache = SqliteTranslationCache(tmp_path / "app.db")
    job = Job(id="0123456789abcdef", filename="paper.pdf", page_count=1, options=JobOptions(cloud_consent=True))
    store.create_job(job)
    blk = Block(
        id="p0-b0",
        page=0,
        type=BlockType.TEXT,
        bbox=Rect(x0=10, y0=10, x1=200, y1=40),
        order=0,
        style=BlockStyle(font_size=10),
        translatable=True,
    )
    doc = Document(job_id=job.id, pages=[Page(index=0, width=300, height=400, blocks=[blk])])
    artifacts.path(job.id, "ir.json").write_text(doc.model_dump_json(), encoding="utf-8")
    store.replace_segments(
        job.id,
        [
            Segment(
                id="s0",
                job_id=job.id,
                block_ids=["p0-b0"],
                source="We call TCM (Typed Comparison Method) our core matcher. TCM improves recall.",
            )
        ],
    )
    deps = PipelineDeps(
        artifacts=artifacts,
        store=store,
        cache=cache,
        events=EventBus(),
        secrets=SecretStore(),
        parser_factory=lambda *_: None,  # type: ignore[arg-type,return-value]
        chat_factory=lambda _: CaptureChat(),
        term_matcher=lambda: None,
    )
    pipeline = Pipeline(deps)
    try:
        asyncio.run(
            pipeline._glossary(
                job,
                Secrets(llm_base_url="http://localhost", llm_api_key="k", llm_model="fake", parser_token=""),
            )
        )
    finally:
        cache.close()
        store.close()
    assert seen_prompts and "TCM = Typed Comparison Method" in seen_prompts[0]


def test_r5_5a_cli_translate_passes_definitions_to_extract_terms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}
    pdf = tmp_path / "syn.pdf"
    with pymupdf.open() as src:
        src.new_page(width=300, height=400).insert_text(
            (20, 40), "We call TCM (Typed Comparison Method) a matcher.", fontsize=10
        )
        src.save(pdf)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(json.dumps({"pdf_info": []}), encoding="utf-8")

    blk = Block(
        id="p0-b0",
        page=0,
        type=BlockType.TEXT,
        bbox=Rect(x0=10, y0=20, x1=280, y1=60),
        order=0,
        style=BlockStyle(font_size=10),
        translatable=True,
    )
    fake_doc = Document(job_id="cli-r5", pages=[Page(index=0, width=300, height=400, blocks=[blk])])
    fake_seg = Segment(
        id="s0",
        job_id="cli-r5",
        block_ids=["p0-b0"],
        source="We call TCM (Typed Comparison Method) a matcher. TCM improves recall.",
    )

    class DummyChat:
        model = "fake"

        def __init__(self, **_kwargs: object) -> None:
            pass

        async def list_models(self) -> list[str]:
            return []

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            return ChatResult(text="")

    async def fake_extract_terms(chat, **kwargs):
        captured.update(kwargs)
        from pdftr.glossary.extract import Extraction

        return Extraction(domain="医疗信息学", terms={"TCM": "类型化比较方法"})

    async def fake_translate(segments, **_kwargs):
        from pdftr.translation.service import TranslateReport

        return segments, TranslateReport()

    monkeypatch.setattr(
        cli_mod,
        "Settings",
        lambda: argparse.Namespace(
            glossary_sources="",
            data_dir=tmp_path,
            llm_protocol="openai",
            llm_base_url="http://localhost",
            llm_api_key="k",
            llm_model="fake",
            llm_concurrency=1,
        ),
    )
    monkeypatch.setattr(cli_mod, "normalize", lambda _raw: LayoutResult(parser="mineru", pages=[]))
    monkeypatch.setattr(cli_mod, "extract", lambda _pdf: [])
    monkeypatch.setattr(cli_mod, "fuse", lambda *_a, **_kw: (fake_doc, None))
    monkeypatch.setattr(cli_mod, "segment", lambda *_a, **_kw: [fake_seg])
    monkeypatch.setattr(cli_mod, "OpenAICompatChat", DummyChat)
    monkeypatch.setattr(cli_mod, "extract_terms", fake_extract_terms)
    monkeypatch.setattr(cli_mod, "translate", fake_translate)

    args = argparse.Namespace(pdf=str(pdf), layout=str(layout_path), refs=False, quality="default", out=None)
    asyncio.run(cli_mod._translate_async(args))
    assert captured["definitions"] == {"TCM": "Typed Comparison Method"}


def test_r5_5b_skip_singular_plural_self_mapping_and_keep_legitimate_terms():
    assert "sockets=sockets、sockets=socket" in EXTRACT_PROMPT
    assert _skip_extracted_term("sockets", "socket")
    assert _skip_extracted_term("sockets", "sockets")
    assert _skip_extracted_term("containers", "container")
    assert not _skip_extracted_term("ResNet", "ResNet")
    assert not _skip_extracted_term("TCM", "TCM")
    assert not _skip_extracted_term("cgroups", "cgroups")
    assert not _skip_extracted_term("getpeername", "getpeername")
    assert not _skip_extracted_term("root privileges", "root 权限")

    parsed = parse_terms(
        "\n".join(
            [
                "领域：系统网络",
                "AI公共词库：停用",
                "sockets=socket",
                "containers=container",
                "ResNet=ResNet",
                "TCM=TCM",
                "cgroups=cgroups",
                "root privileges=root 权限",
            ]
        )
    )
    assert parsed.terms == {
        "ResNet": "ResNet",
        "TCM": "TCM",
        "cgroups": "cgroups",
        "root privileges": "root 权限",
    }
