"""Q4 Round 1 修复包 B 回归测试：公式边界回收、上标引文链接、原生符号/上标恢复、续接、纠序拆块与导出排版。"""

from pathlib import Path

import pymupdf
import pytest

from pdftr.domain.chars import Char, CharPage
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import (
    Block,
    BlockStyle,
    BlockType,
    Document,
    InlineFormula,
    Page,
    PageKind,
    Segment,
    SegmentPart,
    SegmentStatus,
    Span,
)
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutPage, LayoutResult, LayoutSpan
from pdftr.fusion.build import fuse
from pdftr.fusion.formula_align import _content_parts, clean_formula_lines
from pdftr.fusion.text_fix import recover_dropped_chars, restore_citation_superscripts
from pdftr.pipeline.usecases import EXPORT_CACHE_VERSION
from pdftr.rendering.background import make_background
from pdftr.rendering.export import _heading_align, export_pdf_to_path
from pdftr.rendering.links import VERSION as LINKS_VERSION
from pdftr.rendering.links import PdfLink, _link_anchor, _text_lines, extract_links, locate
from pdftr.rendering.typesetter import FontSet, Typesetter
from pdftr.translation.segmenter import front_matter_ids, segment, textual_formula


def _chars(text: str, *, x: float = 10.0, y: float = 0.0, size: float = 10.0, line: int = 0) -> list[Char]:
    return [
        Char(
            c=c,
            bbox=Rect.of([x + 5 * i, y, x + 5 * (i + 1), y + size]),
            font="Times-Roman",
            size=size,
            origin_y=y + 0.8 * size,
            line=line,
        )
        for i, c in enumerate(text)
    ]


def _layout_line(text: str, x: float, y: float, w: float = 220.0, h: float = 14.0) -> LayoutLine:
    box = Rect.of([x, y, x + w, y + h])
    return LayoutLine(bbox=box, spans=[LayoutSpan(kind="text", content=text, bbox=box)])


def _layout_block(
    order: int, kind: BlockType, x: float, y: float, text: str = "", *, w: float = 220.0, h: float = 14.0
) -> LayoutBlock:
    box = Rect.of([x, y, x + w, y + h])
    lines = [LayoutLine(bbox=box, spans=[LayoutSpan(kind="text", content=text, bbox=box)])] if text else []
    return LayoutBlock(type=kind, bbox=box, order=order, lines=lines)


# ---- 1. [P0-3] 公式边界吞正文回收 ----


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (r"$\operatorname{If}|\rho|=1$ holds", ["If ", r"|\rho|=1", " holds"]),
        (r"$\operatorname { I f } | \rho | = 1$ holds", ["If ", r"| \rho | = 1", " holds"]),
        (r"$(i.e., x = 1)$", ["(i.e., ", "x = 1", ")"]),
        (r"$\left( i . e . , x > 0 \right)$", ["(i.e., ", "x > 0", ")"]),
        (r"$e.g., \alpha = 1$", ["e.g., ", r"\alpha = 1", ""]),
        (r"$e . g . , \alpha = 1$", ["e.g., ", r"\alpha = 1", ""]),
        (r"$i . e . x = 1$", ["i.e. ", "x = 1", ""]),
        (r"$\mathrm{e.g.}x$", ["", r"\mathrm{e.g.}x", ""]),
        (r"where ${{q}_{\alpha}} =$ 4.037 holds", ["where ", r"{{q}_{\alpha}}", "= 4.037 holds"]),
        (r"Reject $H_0 \mathrm{if:} F \geq$ 3.84 here", ["Reject ", "H_0", " if: ", "F", "≥ 3.84 here"]),
        (r"Reject $H_0 if: F \geq$ $F_\alpha$", ["Reject ", "H_0", " if: ", r"F \geq", " ", r"F_\alpha", ""]),
        (
            r"$\begin{cases} 1 & \text{if: } x > 0 \end{cases}$",
            ["", r"\begin{cases} 1 & \text{if: } x > 0 \end{cases}", ""],
        ),
    ],
)
def test_formula_boundary_strips_operatorname_latin_condition_and_trailing_relation(content: str, expected: list[str]):
    assert _content_parts(content) == expected


def test_clean_formula_lines_recomputes_bbox_for_trailing_equals_and_falls_back_when_unlocatable():
    chars = _chars("where qα = 4.037")
    bbox = Rect.of([10, 0, 100, 10])
    formula_box = Rect.of([40, 0, 60, 10])
    spans = [
        LayoutSpan(kind="text", content="where", bbox=Rect.of([10, 0, 35, 10])),
        LayoutSpan(kind="inline_formula", content=r"{{q}_{\alpha}} =", bbox=formula_box),
        LayoutSpan(kind="text", content="4.037", bbox=Rect.of([65, 0, 90, 10])),
    ]
    block = LayoutBlock(type=BlockType.TEXT, bbox=bbox, order=0, lines=[LayoutLine(bbox=bbox, spans=spans)])
    cleaned = clean_formula_lines(block, chars)
    out_spans = cleaned.lines[0].spans
    assert [(s.kind, s.content) for s in out_spans] == [
        ("text", "where"),
        ("inline_formula", r"{{q}_{\alpha}}"),
        ("text", "= 4.037"),
    ]
    # 裁剪框精确收缩到 qα，不再包含 '=' 字符。
    assert out_spans[1].bbox.x1 < chars[9].bbox.x0

    # 当原 PDF 中的 '=' 无法作为字符定位（如 PUA 符号）时，回退保留原行，绝不破坏裁剪框。
    pua_chars = [c.model_copy(update={"c": "\ue012"}) if c.c == "=" else c for c in chars]
    fallback = clean_formula_lines(block, pua_chars)
    assert fallback.lines[0].spans == spans


@pytest.mark.parametrize(
    ("latex", "expected"),
    [
        ("e.g.", "e.g."),
        ("i.e.", "i.e."),
        ("e . g .", "e.g."),
        ("i . e . ,", "i.e.,"),
        (r"\mathrm { e . g . , }", "e.g.,"),
        (r"a l . ^ { 3 2 }", "al.<sup>32</sup>"),
        (r"a l . ^ { 4 6 }", "al.<sup>46</sup>"),
        (r"a l . ^ { 3 2 - 3 5 }", "al.<sup>32-35</sup>"),
        (r"\mathrm{al.}^{2,22–25},", "al.<sup>2,22–25</sup>,"),
        (r"a_l^{32}", None),
    ],
)
def test_textual_formula_recovers_latin_abbr_and_et_al_superscripts(latex: str, expected: str | None):
    assert textual_formula(latex) == expected


def test_segment_recovers_et_al_formula_to_plain_superscript_text():
    box = Rect.of([0, 0, 200, 12])
    block = Block(
        id="p0-b0",
        page=0,
        type=BlockType.TEXT,
        bbox=box,
        order=0,
        spans=[
            Span(text="Jarry et", line=0, bbox=box, size=10),
            Span(kind="formula", formula="m1", line=0, bbox=box, size=10),
            Span(text="and", line=0, bbox=box, size=10),
            Span(kind="formula", formula="m2", line=0, bbox=box, size=10),
            Span(text="showed results.", line=0, bbox=box, size=10),
        ],
        formulas=[
            InlineFormula(key="m1", latex=r"a l . ^ { 3 2 }", bbox=box),
            InlineFormula(key="m2", latex=r"e . g . ,", bbox=box),
        ],
        style=BlockStyle(font_size=10),
        translatable=True,
    )
    (seg,) = segment(Document(job_id="synthetic", pages=[Page(index=0, width=600, height=800, blocks=[block])]))
    assert seg.source == "Jarry et al.<sup>32</sup> and e.g., showed results."
    assert seg.formulas == {}


# ---- 2. [P0-4] 上标引文链接 + 扁框自适应垂直重叠与邻行擦边过滤 ----


def test_superscript_citation_links_and_version_bump():
    assert LINKS_VERSION == 4

    def cite(anchor: str, x: float) -> PdfLink:
        return PdfLink(
            page=0,
            rect=Rect.of([x, 10, x + 5, 11]),
            anchor=anchor,
            kind="cite",
            block_id="b1",
            dest_page=5,
        )

    pieces = {
        "b1": (
            "我们测试了 4 组样品和 14 个循环，"
            "见前期研究<sup>4–10</sup>、综述<sup>14</sup>、文献<sup>37</sup>及报道<sup>2,22–25</sup>。"
        )
    }
    links = [
        cite(".4", 0),
        cite("–10", 6),
        cite(",14", 12),
        cite("s37", 15),  # 1pt 扁框擦到前词尾字母 s
        cite("2", 18),
        cite("22", 24),
        cite("–25", 30),
    ]
    located = locate(links, pieces)
    assert [(lk.text, lk.nth) for lk in located] == [
        ("4", 2),  # 跳过正文里的 "4 组" 与 "14 个" 中的 '4'
        ("10", 0),
        ("14", 1),  # 跳过正文里的 "14 个"
        ("37", 0),
        ("2", 0),
        ("22", 0),
        ("25", 0),
    ]


def test_link_anchor_keeps_1pt_superscript_box_and_filters_adjacent_line_graze(tmp_path: Path):
    pdf_path = tmp_path / "links-geom.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=400, height=200)
        # 行 1：正文 + 上标引文 "14"
        page.insert_text((40, 50), "Previous studies.", fontsize=10)
        page.insert_text((118, 45), "14", fontsize=6)
        sup_rect = page.search_for("14")[0]
        # 模拟 APS/REVTeX 约 1pt 高的扁平上标链接框
        thin_box = pymupdf.Rect(sup_rect.x0, sup_rect.y1 - 1.5, sup_rect.x1, sup_rect.y1 - 0.5)
        page.insert_link({"kind": pymupdf.LINK_GOTO, "from": thin_box, "page": 0, "to": pymupdf.Point(0, 150)})

        # 行 2 + 行 3：正常高度 "(2.5)" 链接框底边擦到下一行的 "*"
        page.insert_text((40, 100), "Equation (2.5) holds", fontsize=10)
        page.insert_text((82, 111), "*", fontsize=9)
        eq_rect = page.search_for("(2.5)")[0]
        star_rect = page.search_for("*")[0]
        # 构造底边轻微擦过下一行 "*" 顶边 1pt 的链接框
        grazed = pymupdf.Rect(eq_rect.x0, eq_rect.y0, max(eq_rect.x1, star_rect.x1), star_rect.y0 + 1.0)
        page.insert_link({"kind": pymupdf.LINK_GOTO, "from": grazed, "page": 0, "to": pymupdf.Point(0, 150)})
        pdf.save(pdf_path)

    block = Block(
        id="p0-b0",
        page=0,
        type=BlockType.TEXT,
        bbox=Rect.of([20, 20, 360, 140]),
        order=0,
        style=BlockStyle(font_size=10),
        translatable=True,
    )
    doc = Document(job_id="synthetic", pages=[Page(index=0, width=400, height=200, blocks=[block])])
    extracted = extract_links(pdf_path, doc, ["p0-b0"])
    assert [lk.anchor for lk in extracted] == ["14", "(2.5)"]

    # 额外验证跨视觉行擦入 2.5pt 时只保留主视觉行，以及 Unicode 装饰星号 "∗" 边缘过滤
    with pymupdf.open(pdf_path) as pdf:
        lines = _text_lines(pdf[0].get_textpage())
        deeper_graze = pymupdf.Rect(eq_rect.x0, eq_rect.y0, max(eq_rect.x1, star_rect.x1), star_rect.y0 + 2.5)
        assert _link_anchor(lines, deeper_graze) == "(2.5)"

    located = locate(
        [
            extracted[1].model_copy(update={"kind": "equation"}),
            extracted[1].model_copy(update={"kind": "equation", "anchor": "(2.5)\n∗"}),
        ],
        {"p0-b0": "公式 (2.5) 在该条件下与式 (2.5) 等价。"},
    )
    assert [(lk.text, lk.nth) for lk in located] == [("2.5", 0), ("2.5", 1)]


# ---- 3. [P1-1] 原生 PDF 数学/代码符号、粘连空格与漏标数字上标恢复 ----


@pytest.mark.parametrize(
    ("parser_text", "pdf_text", "expected"),
    [
        ("8 deeper than VGG nets", "8×deeper than VGG nets", "8×deeper than VGG nets"),
        ("152 layers—8 deeper than VGG", "152 layers—8× deeper than VGG", "152 layers—8× deeper than VGG"),
        (
            "with 3 3 filters and 224 224 crops",
            "with 3×3 filters and 224×224 crops",
            "with 3×3 filters and 224×224 crops",
        ),
        ("uses 3 3 conv kernels", "uses 3 × 3 conv kernels", "uses 3 × 3 conv kernels"),
        ("at layer conv3 1 here", "at layer conv3_1 here", "at layer conv3_1 here"),
        ("calls SECCOMP macro", "calls SECCOMP_ macro", "calls SECCOMP_ macro"),
        ("scales 224, 256, 384 are tested", "scales {224, 256, 384} are tested", "scales {224, 256, 384} are tested"),
        ("works [21[35] confirm", "works [21][35] confirm", "works [21][35] confirm"),
        ("invokes getpeername(", "invokes getpeername(2).", "invokes getpeername(2)."),
        (
            "see https://github.com/org/bypass4net",
            "see https://github.com/org/bypass4netns.",
            "see https://github.com/org/bypass4netns.",
        ),
        ("gain accuracyfrom this", "gain accuracy from this", "gain accuracy from this"),
        ("speedup ofup to 2x", "speedup of up to 2x", "speedup of up to 2x"),
        ("a <i>fullyconvolutional</i> net", "a fully convolutional net", "a <i>fully convolutional</i> net"),
        # 负例：不拆专名、不拆复合词、不采纳单字母 kerning 碎片、不反向粘连
        ("on ImageNet data", "on Image Net data", "on ImageNet data"),
        ("an end-to-end model", "an end to end model", "an end-to-end model"),
        ("learn from data", "learn f rom data", "learn from data"),
        ("accuracy from this", "accuracyfrom this", "accuracy from this"),
    ],
)
def test_recover_dropped_chars_symbols_tails_and_glued_spaces(parser_text: str, pdf_text: str, expected: str):
    assert recover_dropped_chars(parser_text, pdf_text) == expected


def test_restore_citation_superscripts_recovers_only_true_superscript_citations():
    def make_line(segments: list[tuple[str, float, float]]) -> list[Char]:
        out: list[Char] = []
        x = 10.0
        for text, size, origin_y in segments:
            for ch in text:
                out.append(
                    Char(
                        c=ch,
                        bbox=Rect.of([x, origin_y - size, x + 0.5 * size, origin_y]),
                        font="Times-Roman",
                        size=size,
                        origin_y=origin_y,
                        line=0,
                    )
                )
                x += 0.5 * size
        return out

    # 真实上标引文：字号缩小且基线上抬，端到端通过 fuse 补回 <sup>
    chars = make_line(
        [("studies.", 10.0, 100.0), ("11–13", 6.5, 96.0), (" and effect,", 10.0, 100.0), ("14", 6.5, 96.0)]
    )
    assert (
        restore_citation_superscripts("studies.11–13 and effect,14", chars)
        == "studies.<sup>11–13</sup> and effect,<sup>14</sup>"
    )
    box = Rect.of([8.0, 86.0, 260.0, 104.0])
    lb = LayoutBlock(
        type=BlockType.TEXT,
        bbox=box,
        order=0,
        lines=[LayoutLine(bbox=box, spans=[LayoutSpan(kind="text", content="studies.11–13 and effect,14", bbox=box)])],
    )
    doc, _ = fuse(
        "synthetic",
        LayoutResult(parser="synthetic", pages=[LayoutPage(index=0, width=600, height=800, blocks=[lb])]),
        [CharPage(index=0, width=600, height=800, chars=chars)],
        used_cloud=False,
    )
    assert doc.pages[0].blocks[0].spans[0].text == "studies.<sup>11–13</sup> and effect,<sup>14</sup>"

    # 负例：小数 3.14、章节号 2.1、带空格数字 in 11-13、化学下标 H2O、普通数字 ResNet50 绝不误标
    neg_chars = make_line(
        [
            ("Section 2.1 uses 3.14 in 11-13 with H", 10.0, 100.0),
            ("2", 6.5, 102.5),  # 下标：origin_y 更大（下移）
            ("O and ResNet50", 10.0, 100.0),
        ]
    )
    neg_text = "Section 2.1 uses 3.14 in 11-13 with H2O and ResNet50"
    assert restore_citation_superscripts(neg_text, neg_chars) == neg_text


# ---- 4. [P1-2] 跨栏/跨页/绕图与同栏相邻块续接增强 ----


def test_continuation_over_large_floating_figure_and_unfinished_proper_nouns():
    # 页面上半部被大图+图注占满（超过 0.35 * 800 = 280），下方左栏首个正文块应续接上页末尾，
    # 且前块以 the/in/_ 结尾时，后块以大写专名/Table 2/全大写宏开头的长段也能续接。
    p0_blocks = [
        _layout_block(0, BlockType.TEXT, 50, 650, "achieves lower top-5 error on the"),
        _layout_block(1, BlockType.FIGURE, 320, 40, "", w=230, h=340),
        _layout_block(2, BlockType.CAPTION, 320, 385, "Figure 1. Large chart.", w=230, h=20),
        _layout_block(
            3,
            BlockType.TEXT,
            320,
            420,
            "ImageNet <i>test</i> set across all evaluated model depths and resolutions.",
            w=230,
        ),
        _layout_block(4, BlockType.TEXT, 320, 680, "results are summarized and shown in", w=230),
    ]
    p1_blocks = [
        _layout_block(0, BlockType.TABLE, 50, 40, "", w=500, h=350),
        _layout_block(1, BlockType.CAPTION, 50, 395, "Table 2. Detailed benchmark results.", w=500, h=18),
        _layout_block(
            2,
            BlockType.TEXT,
            50,
            430,
            "Table 2, which demonstrates consistent gains on every evaluated dataset split.",
            w=230,
        ),
        # 同栏紧邻两 TEXT 块续接（SECCOMP_ + IOCTL_NOTIF_ADDFD）
        _layout_block(3, BlockType.TEXT, 50, 460, "The kernel supports SECCOMP_", w=230),
        _layout_block(4, BlockType.TEXT, 50, 480, "IOCTL_NOTIF_ADDFD for forwarding file descriptors.", w=230),
        # 同栏紧邻两 TEXT 块防误合并：前块已句号收尾
        _layout_block(5, BlockType.TEXT, 50, 500, "This paragraph finishes cleanly.", w=230),
        _layout_block(6, BlockType.TEXT, 50, 520, "Next paragraph starts independently.", w=230),
        # 同栏紧邻两 TEXT 块防误合并：后块首行缩进
        _layout_block(7, BlockType.TEXT, 50, 545, "An unfinished lead without punctuation in", w=230),
        _layout_block(8, BlockType.TEXT, 70, 565, "indented new paragraph starts here.", w=210),
    ]
    layout = LayoutResult(
        parser="synthetic",
        pages=[
            LayoutPage(index=0, width=600, height=800, blocks=p0_blocks),
            LayoutPage(index=1, width=600, height=800, blocks=p1_blocks),
        ],
    )
    pages = [
        CharPage(index=0, width=600, height=800, kind=PageKind.SCANNED),
        CharPage(index=1, width=600, height=800, kind=PageKind.SCANNED),
    ]
    doc, report = fuse("synthetic", layout, pages, used_cloud=False)
    assert doc.pages[0].blocks[3].continues_from == doc.pages[0].blocks[0].id
    assert doc.pages[1].blocks[2].continues_from == doc.pages[0].blocks[4].id
    assert doc.pages[1].blocks[4].continues_from == doc.pages[1].blocks[3].id
    assert doc.pages[1].blocks[6].continues_from is None
    assert doc.pages[1].blocks[8].continues_from is None
    assert report.continuations == 3


# ---- 5. [P2-3] 扫描页同栏短 TITLE 几何纠序 + Keywords. 独立拆块 ----


def test_displaced_short_title_is_reordered_before_column_text_and_preserves_two_column_order():
    blocks = [
        _layout_block(0, BlockType.TITLE, 120, 60, "A Scanned Systems Paper", w=360, h=24),
        _layout_block(1, BlockType.TEXT, 180, 100, "Alice Smith, University of Example", w=240, h=14),
        # MinerU 将摘要正文排在了 ABSTRACT 之前
        _layout_block(2, BlockType.TEXT, 80, 223, "This paper presents a reliable storage engine.", w=440, h=60),
        _layout_block(3, BlockType.TEXT, 80, 290, "Evaluation confirms low latency under load.", w=440, h=60),
        _layout_block(4, BlockType.TITLE, 260, 205, "ABSTRACT", w=80, h=14),
        # 右栏靠下的标题不应越过左栏正文
        _layout_block(5, BlockType.TEXT, 50, 420, "Left column introduction text.", w=220, h=80),
        _layout_block(6, BlockType.TITLE, 330, 405, "2. Design", w=120, h=14),
        _layout_block(7, BlockType.TEXT, 330, 425, "Right column design text.", w=220, h=80),
    ]
    layout = LayoutResult(parser="synthetic", pages=[LayoutPage(index=0, width=600, height=800, blocks=blocks)])
    doc, _ = fuse(
        "synthetic", layout, [CharPage(index=0, width=600, height=800, kind=PageKind.SCANNED)], used_cloud=False
    )
    ordered_texts = [b.spans[0].text for b in doc.pages[0].blocks]
    assert ordered_texts.index("ABSTRACT") < ordered_texts.index("This paper presents a reliable storage engine.")
    assert ordered_texts.index("Left column introduction text.") < ordered_texts.index("2. Design")
    assert doc.meta.abstract == "This paper presents a reliable storage engine."
    assert front_matter_ids(doc) == {doc.pages[0].blocks[1].id}


@pytest.mark.parametrize(
    "kw_line",
    ["Keywords. reinforcement learning, finance", "Key words: battery, cathode", "Index Terms—Neural networks"],
)
def test_split_layout_separates_non_first_keywords_line(kw_line: str):
    lines = [
        _layout_line("Abstract. We propose a policy optimization method.", 60, 200, w=460),
        _layout_line("Experiments confirm strong out-of-sample returns.", 60, 218, w=460),
        _layout_line(kw_line, 60, 240, w=460),
    ]
    merged = LayoutBlock(type=BlockType.TEXT, bbox=Rect.of([60, 200, 520, 254]), order=0, lines=lines)
    layout = LayoutResult(parser="synthetic", pages=[LayoutPage(index=0, width=600, height=800, blocks=[merged])])
    doc, _ = fuse(
        "synthetic", layout, [CharPage(index=0, width=600, height=800, kind=PageKind.SCANNED)], used_cloud=False
    )
    assert len(doc.pages[0].blocks) == 2
    segs = segment(doc)
    assert len(segs) == 2
    assert (
        segs[0].source.startswith("Abstract.")
        and "Keywords" not in segs[0].source
        and "Index Terms" not in segs[0].source
    )
    assert segs[1].source == kw_line


# ---- 6. [P1-4] 单栏居中主标题自动居中 + CAPTION 防孤字平衡 + 缓存版本 ----


def test_heading_align_centers_symmetric_single_column_title_and_keeps_left_column_titles_left(tmp_path: Path):
    assert EXPORT_CACHE_VERSION == 12

    def title_block(bid: str, bbox: list[float], text: str) -> Block:
        r = Rect.of(bbox)
        return Block(
            id=bid,
            page=0,
            type=BlockType.TITLE,
            bbox=r,
            order=0,
            spans=[Span(text=text, bbox=r, size=14)],
            style=BlockStyle(font_size=14, bold=True),
            translatable=True,
        )

    centered = title_block("p0-b0", [100, 40, 500, 70], "Deep Residual Learning")
    wide_main = title_block("p0-b1", [50, 80, 550, 110], "A Wide Two-Line Centered Paper Title")
    left_sec = title_block("p0-b2", [50, 140, 200, 160], "1. Introduction")
    right_sec = title_block("p0-b3", [320, 140, 460, 160], "2. Related Work")
    wide_numbered = title_block("p0-b4", [50, 180, 550, 200], "3.2 Full Width Numbered Section Heading")

    assert _heading_align(centered, 600.0) == "center"
    assert _heading_align(wide_main, 600.0) == "center"
    assert _heading_align(left_sec, 600.0) == "left"
    assert _heading_align(right_sec, 600.0) == "left"
    assert _heading_align(wide_numbered, 600.0) == "left"

    # 端到端导出验证居中主标题与左对齐小标题的实际绘制横坐标
    original = tmp_path / "titles.pdf"
    background = tmp_path / "bg.pdf"
    out = tmp_path / "out.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page(width=600, height=400).insert_text((100, 60), "Deep Residual Learning", fontsize=14)
        pdf.save(original)
    doc = Document(job_id="synthetic", pages=[Page(index=0, width=600, height=400, blocks=[centered, left_sec])])
    segs = [
        Segment(
            id="s0",
            job_id="synthetic",
            block_ids=["p0-b0"],
            source="Deep Residual Learning",
            target="深度残差学习",
            status=SegmentStatus.DONE,
            parts=[SegmentPart(block_id="p0-b0", chars=22)],
        ),
        Segment(
            id="s1",
            job_id="synthetic",
            block_ids=["p0-b2"],
            source="1. Introduction",
            target="1. 引言",
            status=SegmentStatus.DONE,
            parts=[SegmentPart(block_id="p0-b2", chars=15)],
        ),
    ]
    make_background(original, doc.blocks(), background)
    export_pdf_to_path(original, background, doc, segs, "mono", links=[], out=out)
    with pymupdf.open(out) as pdf:
        (center_rect,) = pdf[0].search_for("深度残差学习")
        (left_rect,) = pdf[0].search_for("1. 引言")
        assert abs((center_rect.x0 + center_rect.x1) / 2 - 300.0) < 5.0
        assert left_rect.x0 < 60.0


def test_caption_orphan_balance_eliminates_single_han_without_overflow():
    ts = Typesetter(FontSet.load(None))
    size = 10.0
    # 情况 1：普通中文图注末行只剩 1 个孤字“络。”
    target_text = "图1.用于深度残差特征提取网络。"
    tokens = ts.tokenize(target_text, "caption", size, lambda _: None, "图1.")
    width = next(
        w / 2
        for w in range(120, 400)
        if len(ls := ts.break_lines(tokens, w / 2, size, 0.0)) == 2
        and "".join(r.text for t in ls[-1].tokens for r in t.runs) == "络。"
    )
    base_lines, _, base_h = ts.layout(tokens, width, size, 1.4, False)
    assert len(base_lines) == 2
    assert "".join(r.text for t in base_lines[-1].tokens for r in t.runs) == "络。"
    bal_lines, _, bal_h = ts.layout(tokens, width, size, 1.4, False, balance_caption=True)
    assert len(bal_lines) == len(base_lines) == 2
    assert bal_h == pytest.approx(base_h)
    last_text = "".join(r.text for t in bal_lines[-1].tokens for r in t.runs)
    assert len(last_text.rstrip("。")) >= 3

    # 情况 2：倒数第二行末尾紧邻西文词 + 单汉字（无法纯移 2 个 CJK），触发温和断行宽度微调且不增行不溢出
    caption2 = "图2.该方法对比深度残差卷积特征表示并评估标准ResNet网" + "络。"
    tokens2 = ts.tokenize(caption2, "caption", size, lambda _: None, "图2.")
    # 定位一个使末行恰好为“络。”的宽度
    chosen_w = next(
        w / 2
        for w in range(180, 500)
        if len(ls := ts.break_lines(tokens2, w / 2, size, 0.0)) >= 2
        and "".join(r.text for t in ls[-1].tokens for r in t.runs) == "络。"
    )
    base2, _, base2_h = ts.layout(tokens2, chosen_w, size, 1.4, False)
    bal2, _, bal2_h = ts.layout(tokens2, chosen_w, size, 1.4, False, balance_caption=True)
    assert len(bal2) == len(base2)
    assert bal2_h <= base2_h + 0.01
    assert "".join(r.text for t in bal2[-1].tokens for r in t.runs) != "络。"
