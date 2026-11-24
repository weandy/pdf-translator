"""Q4 Round 3 共性缺陷修复包 B 回归测试。

覆盖：
1. relocate.py 跨页坐标重合假命中拆分与跨整页图搜索 pi+2；
2. align.py + build.py 双栏超宽行尾（overfull hbox）安全右溢出回收与背景擦除；
3. background.py 垂直墨迹连通带清除厚残影带并保护真分式；
4. formula_align.py 整块视觉行中线防跨行、原生 if:/if 拆分与跨行尾关系符剥离；
5. build.py 移除 FOOTNOTE 续接阻断；
6. links.py 放行显式文献前缀及缓存版本号递增。
"""

from pathlib import Path

import pymupdf

from pdftr.domain.chars import Char, CharPage
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import BlockType, PageKind
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutPage, LayoutResult, LayoutSpan
from pdftr.extraction.chars import extract
from pdftr.fusion.align import align
from pdftr.fusion.build import fuse
from pdftr.fusion.formula_align import clean_formula_lines
from pdftr.fusion.relocate import relocate
from pdftr.pipeline.usecases import EXPORT_CACHE_VERSION
from pdftr.rendering.background import _trim_formula_edges, make_background
from pdftr.rendering.links import VERSION as LINKS_VERSION
from pdftr.rendering.links import PdfLink, locate


def _chars(
    text: str,
    *,
    x: float = 50.0,
    y: float = 100.0,
    size: float = 10.0,
    step: float = 5.0,
    line: int = 0,
    font: str = "Times-Roman",
    italic: bool = False,
    height: float | None = None,
) -> list[Char]:
    h = size if height is None else height
    return [
        Char(
            c=ch,
            bbox=Rect.of([x + i * step, y, x + (i + 1) * step, y + h]),
            font=font,
            size=size,
            origin_y=y + 0.8 * size,
            italic=italic,
            line=line,
        )
        for i, ch in enumerate(text)
    ]


def _line(text: str, x0: float, y0: float, x1: float, y1: float) -> LayoutLine:
    box = Rect.of([x0, y0, x1, y1])
    return LayoutLine(bbox=box, spans=[LayoutSpan(kind="text", content=text, bbox=box)])


# ---- 1. [R3-2] relocate.py 跨页坐标重合假命中 + 跨整页图搜索 pi+2 ----


def test_relocate_splits_overlapping_cross_page_lines_and_chains_to_pi_plus_2():
    p1_lines = [
        _line("Page two line one discussing cathode", 50, 570, 290, 582),
        _line("Page two line two cycling stability", 50, 640, 290, 652),
        _line("Page two bottom line before figure", 50, 740, 290, 752),
    ]
    # 第 3 页（pi+1）底部占位块坐标 y=639..738 巧合落在第 2 页 p1-b5 (y=570..753) 内
    p2_lines = [
        _line("consistent with previous studies.11–13", 50, 639, 290, 651),
        _line("and further confirms surface stability", 50, 720, 290, 732),
    ]
    # 第 4 页（pi+2）顶部占位块坐标 y=89..167
    p3_lines = [
        _line("across extended electrochemical cycles.", 50, 89, 290, 101),
        _line("This finishes the three-page paragraph.", 50, 150, 290, 162),
    ]
    p1_block = LayoutBlock(
        type=BlockType.TEXT,
        bbox=Rect.of([50, 570, 290, 753]),
        order=5,
        lines=[*p1_lines, *p2_lines, *p3_lines],
    )
    p2_fig = LayoutBlock(type=BlockType.FIGURE, bbox=Rect.of([50, 60, 550, 580]), order=0)
    p2_cap = LayoutBlock(
        type=BlockType.CAPTION,
        bbox=Rect.of([50, 590, 550, 620]),
        order=1,
        lines=[_line("Figure 1. Full page diagram.", 50, 590, 550, 620)],
    )
    p2_placeholder = LayoutBlock(type=BlockType.TEXT, bbox=Rect.of([50, 639, 290, 738]), order=8, placeholder=True)
    p3_placeholder = LayoutBlock(type=BlockType.TEXT, bbox=Rect.of([50, 89, 290, 167]), order=0, placeholder=True)

    layout = LayoutResult(
        parser="mineru",
        pages=[
            LayoutPage(index=0, width=600, height=800, blocks=[]),
            LayoutPage(index=1, width=600, height=800, blocks=[p1_block]),
            LayoutPage(index=2, width=600, height=800, blocks=[p2_fig, p2_cap, p2_placeholder]),
            LayoutPage(index=3, width=600, height=800, blocks=[p3_placeholder]),
        ],
    )

    # 纯几何模式（无 char_pages）也能靠行序纵坐标回跳 + 垂直重叠精准拆分并串链至 pi+2
    moved_geom, rel_geom = relocate(layout)
    assert rel_geom.moved_lines == 4
    assert rel_geom.unmatched_lines == 0
    assert rel_geom.links == {(2, 8): (1, 5), (3, 0): (2, 8)}
    assert len(moved_geom.pages[1].blocks[0].lines) == 3
    assert len(moved_geom.pages[2].blocks[2].lines) == 2
    assert len(moved_geom.pages[3].blocks[0].lines) == 2
    assert not moved_geom.pages[2].blocks[2].placeholder
    assert not moved_geom.pages[3].blocks[0].placeholder

    # 端到端 fuse（带原生 char_pages）：第 3 页 p2-b8 使用第 3 页字符自动恢复上标引文 studies.<sup>11–13</sup>
    def _sup_line_chars() -> list[Char]:
        out: list[Char] = []
        x = 50.0
        for text, sz, oy in [("consistent with previous studies.", 10.0, 649.0), ("11–13", 6.5, 644.5)]:
            for ch in text:
                out.append(
                    Char(
                        c=ch,
                        bbox=Rect.of([x, oy - sz, x + 0.5 * sz, oy]),
                        font="Times-Roman",
                        size=sz,
                        origin_y=oy,
                        line=0,
                    )
                )
                x += 0.5 * sz
        return out

    char_pages = [
        CharPage(index=0, width=600, height=800),
        CharPage(
            index=1,
            width=600,
            height=800,
            chars=(
                _chars("Page two line one discussing cathode", x=50, y=570, line=0)
                + _chars("Page two line two cycling stability", x=50, y=640, line=1)
                + _chars("Page two bottom line before figure", x=50, y=740, line=2)
            ),
        ),
        CharPage(
            index=2,
            width=600,
            height=800,
            chars=(
                _chars("Figure 1. Full page diagram.", x=50, y=590, line=0)
                + _sup_line_chars()
                + _chars("and further confirms surface stability", x=50, y=720, line=2)
            ),
        ),
        CharPage(
            index=3,
            width=600,
            height=800,
            chars=(
                _chars("across extended electrochemical cycles.", x=50, y=89, line=0)
                + _chars("This finishes the three-page paragraph.", x=50, y=150, line=1)
            ),
        ),
    ]
    doc, report = fuse("synthetic", layout, char_pages, used_cloud=False)
    b1 = doc.block("p1-b5")
    b2 = doc.block("p2-b8")
    b3 = doc.block("p3-b0")
    assert b1 is not None and b2 is not None and b3 is not None
    assert b1.translatable and b2.translatable and b3.translatable
    assert b2.continues_from == "p1-b5"
    assert b3.continues_from == "p2-b8"
    assert "studies.<sup>11–13</sup>" in b2.spans[0].text
    assert report.moved_lines == 4


def test_relocate_crosses_figure_only_page_to_pi_plus_2_but_stops_at_unrelated_text_page():
    lead = LayoutBlock(
        type=BlockType.TEXT,
        bbox=Rect.of([50, 680, 290, 740]),
        order=0,
        lines=[
            _line("Starts before a full page figure", 50, 680, 290, 700),
            _line("continues after the full page figure.", 50, 80, 290, 100),
        ],
    )
    p1_figure_only = LayoutPage(
        index=1,
        width=600,
        height=800,
        blocks=[LayoutBlock(type=BlockType.FIGURE, bbox=Rect.of([40, 40, 560, 760]), order=0)],
    )
    p2_placeholder = LayoutBlock(type=BlockType.TEXT, bbox=Rect.of([50, 75, 290, 110]), order=0, placeholder=True)
    layout_fig = LayoutResult(
        parser="mineru",
        pages=[
            LayoutPage(index=0, width=600, height=800, blocks=[lead]),
            p1_figure_only,
            LayoutPage(index=2, width=600, height=800, blocks=[p2_placeholder]),
        ],
    )
    moved, rel = relocate(layout_fig)
    assert rel.links == {(2, 0): (0, 0)}
    assert rel.moved_lines == 1 and not moved.pages[2].blocks[0].placeholder

    # 负例：pi+1 存在独立正文且未命中 pi+1 占位块时，不得跨页远距离误配到 pi+2
    p1_with_text = LayoutPage(
        index=1,
        width=600,
        height=800,
        blocks=[
            LayoutBlock(
                type=BlockType.TEXT,
                bbox=Rect.of([50, 100, 290, 200]),
                order=0,
                lines=[_line("An independent body paragraph.", 50, 100, 290, 120)],
            )
        ],
    )
    layout_blocked = LayoutResult(
        parser="mineru",
        pages=[
            LayoutPage(index=0, width=600, height=800, blocks=[lead]),
            p1_with_text,
            LayoutPage(index=2, width=600, height=800, blocks=[p2_placeholder]),
        ],
    )
    blocked, rel_blocked = relocate(layout_blocked)
    assert rel_blocked.moved_lines == 0 and rel_blocked.unmatched_lines == 1
    assert blocked.pages[2].blocks[0].placeholder


# ---- 2. [R3-6] align.py + build.py 双栏超宽行尾（overfull hbox）安全右溢出回收 ----


def test_align_and_fuse_recover_overfull_hbox_tails_without_touching_right_column(tmp_path: Path):
    pdf_path = tmp_path / "overfull.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=800)
        page.insert_text((50, 120), "see https://github.com/org/bypass4netns.", fontsize=9.5, fontname="tiro")
        page.insert_text((50, 180), "invokes getpeername(2).", fontsize=9.5, fontname="tiro")
        page.insert_text((318, 120), "Right column first line.", fontsize=9.5, fontname="tiro")
        page.insert_text((318, 180), "Right column second line.", fontsize=9.5, fontname="tiro")
        pdf.save(pdf_path)

    (cp,) = extract(pdf_path)
    # 找出 "bypass4net" 与 "getpeername(" 的右边界，人为模拟 MinerU 切掉 "ns." 与 "2)."
    line0 = sorted([c for c in cp.chars if c.bbox.center[1] < 150 and c.bbox.x0 < 315], key=lambda c: c.bbox.x0)
    line1 = sorted([c for c in cp.chars if c.bbox.center[1] >= 150 and c.bbox.x0 < 315], key=lambda c: c.bbox.x0)
    cut0_x1 = line0[-4].bbox.x1  # 切掉 "ns."
    cut1_x1 = line1[-4].bbox.x1  # 切掉 "2)."
    b0_box = Rect.of([48, 108, cut0_x1, 126])
    b1_box = Rect.of([48, 168, cut1_x1, 186])
    right_box = Rect.of([318, 108, 540, 190])

    # 单元测试 align 回收
    per_block, orphans = align(cp.chars, [b0_box, b1_box, right_box])
    assert "".join(c.c for c in sorted(per_block[0], key=lambda c: c.bbox.x0)).endswith("bypass4netns.")
    assert "".join(c.c for c in sorted(per_block[1], key=lambda c: c.bbox.x0)).endswith("getpeername(2).")
    assert not [c for c in orphans if not c.c.isspace()]

    # 端到端 fuse + make_background 验证：原文恢复且背景擦除干净不留残尾，右栏完整保留
    layout = LayoutResult(
        parser="mineru",
        pages=[
            LayoutPage(
                index=0,
                width=600,
                height=800,
                blocks=[
                    LayoutBlock(
                        type=BlockType.TEXT,
                        bbox=b0_box,
                        order=0,
                        lines=[
                            LayoutLine(
                                bbox=b0_box,
                                spans=[
                                    LayoutSpan(
                                        kind="text", content="see https://github.com/org/bypass4net", bbox=b0_box
                                    )
                                ],
                            )
                        ],
                    ),
                    LayoutBlock(
                        type=BlockType.TEXT,
                        bbox=b1_box,
                        order=1,
                        lines=[
                            LayoutLine(
                                bbox=b1_box,
                                spans=[LayoutSpan(kind="text", content="invokes getpeername(", bbox=b1_box)],
                            )
                        ],
                    ),
                    LayoutBlock(
                        type=BlockType.TEXT,
                        bbox=right_box,
                        order=2,
                        lines=[
                            LayoutLine(
                                bbox=Rect.of([318, 108, 540, 126]),
                                spans=[
                                    LayoutSpan(
                                        kind="text",
                                        content="Right column first line.",
                                        bbox=Rect.of([318, 108, 540, 126]),
                                    )
                                ],
                            ),
                            LayoutLine(
                                bbox=Rect.of([318, 168, 540, 186]),
                                spans=[
                                    LayoutSpan(
                                        kind="text",
                                        content="Right column second line.",
                                        bbox=Rect.of([318, 168, 540, 186]),
                                    )
                                ],
                            ),
                        ],
                    ),
                ],
            )
        ],
    )
    doc, _ = fuse("synthetic", layout, [cp], used_cloud=False)
    left0, left1, _ = doc.pages[0].blocks[:3]
    assert left0.spans[0].text == "see https://github.com/org/bypass4netns."
    assert left1.spans[0].text == "invokes getpeername(2)."

    bg = tmp_path / "bg.pdf"
    make_background(pdf_path, [left0, left1], bg)
    with pymupdf.open(bg) as pdf:
        remaining = str(pdf[0].get_text())
        assert "ns." not in remaining
        assert "2)." not in remaining
        assert "Right column first line." in remaining
        assert "Right column second line." in remaining


def test_align_rejects_right_overflow_touching_neighbor_or_exceeding_max_width():
    left_box = Rect.of([50, 100, 290, 112])
    right_box = Rect.of([294.5, 100, 500, 112])
    base = _chars("abcde", x=265, y=100, size=10.0, step=5.0, line=0)
    # 中心落在两栏缝隙（orphan）但右缘 x1=293.6 > 294.5 - 1.5 = 293.0，触碰右栏边界必须被拦住
    touching = _chars("x", x=291.6, y=100, size=10.0, step=2.0, line=0)
    assigned, orphans = align([*base, *touching], [left_box, right_box])
    assert len(assigned[0]) == len(base)
    assert len(orphans) == len(touching)

    # 超过 32pt 总溢出的远端字符必须停止回收
    far_tail = _chars("1234567890", x=291.0, y=100, size=10.0, step=4.0, line=0)
    assigned2, orphans2 = align([*base, *far_tail], [left_box])
    assert max(c.bbox.x1 for c in assigned2[0]) <= left_box.x1 + 32.0
    assert len(orphans2) >= 2


# ---- 3. [R3-4a] background.py 垂直墨迹连通带清除厚残影带并保护真分式 ----


def _make_rgba_pix(width: int, height: int, filled_runs: list[tuple[int, int, int, int]]) -> pymupdf.Pixmap:
    buf = bytearray(width * height * 4)
    for x0, y0, x1, y1 in filled_runs:
        for y in range(y0, y1):
            for x in range(x0, x1):
                offset = (y * width + x) * 4
                buf[offset : offset + 4] = b"\x00\x00\x00\xff"
    return pymupdf.Pixmap(pymupdf.csRGB, width, height, bytes(buf), True)


def _pix_ink_rows(pix: pymupdf.Pixmap) -> list[bool]:
    alpha = pix.samples[3::4]
    return [any(alpha[y * pix.width : (y + 1) * pix.width]) for y in range(pix.height)]


def test_trim_formula_edges_clears_thick_detached_bottom_ghost_and_keeps_baseline_height():
    # 复现 01_ai_resnet s12 m2：4x 裁图高 70px，主体 y=1..40 (40px)，缝隙 8px，底部残影 y=49..69 (21px)
    pix = _make_rgba_pix(80, 70, [(10, 1, 70, 41), (10, 49, 70, 70)])
    trimmed = _trim_formula_edges(pix, zoom=4.0)
    rows = _pix_ink_rows(trimmed)
    assert trimmed.height == 70  # 底部只清透明不裁高，保留基线
    assert all(rows[1:41])
    assert not any(rows[41:])


def test_trim_formula_edges_preserves_true_fraction_with_bar():
    # 真分式：分子 y=1..22、分数线 y=26..29、分母 y=33..54（高 56px）
    pix = _make_rgba_pix(80, 56, [(25, 1, 55, 22), (10, 26, 70, 29), (25, 33, 55, 55)])
    trimmed = _trim_formula_edges(pix, zoom=4.0)
    rows = _pix_ink_rows(trimmed)
    assert trimmed.height == 56
    assert any(rows[1:22]) and any(rows[26:29]) and any(rows[33:55])


# ---- 4. [R3-4b/c] formula_align.py 视觉行中线约束、原生 if:/if 拆分与跨行尾 '=' 剥离 ----


def test_clean_formula_lines_clamps_tall_cmsy_formula_to_block_visual_row_midpoints():
    # 三行段落，中行含高 17.29pt 的 CMSY10 符号框扎入上下行
    row0 = _chars("Previous line of paragraph", x=50, y=100, size=10.0, line=0)
    row1_l = _chars("with ", x=50, y=112, size=10.0, line=1)
    tall = Char(
        c="∈",
        bbox=Rect.of([75, 107.0, 82, 124.29]),
        font="CMSY10",
        size=10.0,
        origin_y=120.0,
        line=1,
    )
    row1_r = _chars(" set", x=83, y=112, size=10.0, line=1)
    row2 = _chars("Next line of paragraph", x=50, y=124, size=10.0, line=2)
    chars = [*row0, *row1_l, tall, *row1_r, *row2]

    block = LayoutBlock(
        type=BlockType.TEXT,
        bbox=Rect.of([50, 100, 250, 134]),
        order=0,
        lines=[
            LayoutLine(
                bbox=Rect.of([50, 100, 250, 110]),
                spans=[
                    LayoutSpan(kind="text", content="Previous line of paragraph", bbox=Rect.of([50, 100, 250, 110]))
                ],
            ),
            LayoutLine(
                bbox=Rect.of([50, 107, 130, 124.29]),
                spans=[
                    LayoutSpan(kind="text", content="with", bbox=Rect.of([50, 112, 74, 122])),
                    LayoutSpan(kind="inline_formula", content=r"\in", bbox=tall.bbox, located=True),
                    LayoutSpan(kind="text", content="set", bbox=Rect.of([83, 112, 103, 122])),
                ],
            ),
            LayoutLine(
                bbox=Rect.of([50, 124, 250, 134]),
                spans=[LayoutSpan(kind="text", content="Next line of paragraph", bbox=Rect.of([50, 124, 250, 134]))],
            ),
        ],
    )
    cleaned = clean_formula_lines(block, chars)
    formula = cleaned.lines[1].spans[1]
    # 行中线分别为 105, 117, 129 -> 上下中线边界为 111.0 与 123.0
    assert formula.bbox.y0 >= 111.0
    assert formula.bbox.y1 <= 123.0


def test_clean_formula_lines_splits_native_misread_if_colon_and_dropped_f():
    # 复现 05_med_struct s44：原 PDF "Reject H0 if: F ≥ critical value"，MinerU 错识成 it:
    c_reject = _chars("Reject ", x=10, y=100, size=10.0, step=5.0, line=0)
    c_h0 = _chars("H0", x=48, y=100, size=10.0, step=5.0, line=0)
    c_if_colon = _chars("if:", x=62, y=100, size=10.0, step=5.0, line=0)
    c_fgeq = _chars("F≥", x=81, y=100, size=10.0, step=5.0, line=0)
    c_crit = _chars(" critical value", x=93, y=100, size=10.0, step=5.0, line=0)
    chars_s44 = [*c_reject, *c_h0, *c_if_colon, *c_fgeq, *c_crit]
    line_box = Rect.of([10, 100, 170, 110])
    block_s44 = LayoutBlock(
        type=BlockType.TEXT,
        bbox=line_box,
        order=0,
        lines=[
            LayoutLine(
                bbox=line_box,
                spans=[
                    LayoutSpan(kind="text", content="Reject", bbox=Rect.of([10, 100, 44, 110])),
                    LayoutSpan(
                        kind="inline_formula",
                        content=r"{\sf H}_0 \mathrm{i}{\sf t}: {\sf F} \geq",
                        bbox=Rect.of([48, 100, 91, 110]),
                        located=True,
                    ),
                    LayoutSpan(kind="text", content="critical value", bbox=Rect.of([93, 100, 170, 110])),
                ],
            )
        ],
    )
    cleaned_s44 = clean_formula_lines(block_s44, chars_s44)
    assert [(s.kind, s.content) for s in cleaned_s44.lines[0].spans] == [
        ("text", "Reject"),
        ("inline_formula", r"{\sf H}_0"),
        ("text", "if:"),
        ("inline_formula", r"{\sf F} \geq"),
        ("text", "critical value"),
    ]
    assert cleaned_s44.lines[0].spans[1].bbox.x1 <= c_h0[-1].bbox.x1 + 0.1
    assert cleaned_s44.lines[0].spans[3].bbox.x0 >= c_fgeq[0].bbox.x0 - 0.1

    # 复现 05_med_struct s61：原 PDF "is rejected if F ≥ χ²"，MinerU 吞掉 f 识为 i F \geq \chi^2
    c_pre = _chars("is rejected ", x=10, y=100, size=10.0, step=5.0, line=0)
    c_if = _chars("if", x=72, y=100, size=10.0, step=5.0, line=0, italic=False)
    c_rhs = _chars("F≥χ2", x=86, y=100, size=10.0, step=5.0, line=0)
    chars_s61 = [*c_pre, *c_if, *c_rhs]
    block_s61 = LayoutBlock(
        type=BlockType.TEXT,
        bbox=Rect.of([10, 100, 120, 110]),
        order=0,
        lines=[
            LayoutLine(
                bbox=Rect.of([10, 100, 120, 110]),
                spans=[
                    LayoutSpan(kind="text", content="is rejected", bbox=Rect.of([10, 100, 68, 110])),
                    LayoutSpan(
                        kind="inline_formula",
                        content=r"i F \geq \chi^2",
                        bbox=Rect.of([72, 100, 106, 110]),
                        located=True,
                    ),
                ],
            )
        ],
    )
    cleaned_s61 = clean_formula_lines(block_s61, chars_s61)
    assert [(s.kind, s.content) for s in cleaned_s61.lines[0].spans] == [
        ("text", "is rejected"),
        ("text", "if"),
        ("inline_formula", r"F \geq \chi^2"),
    ]

    # 负例：斜体数学变量相乘 i f（CMMI / italic）绝不误拆
    c_if_math = _chars("if", x=72, y=100, size=10.0, step=5.0, line=0, font="CMMI10", italic=True)
    untouched = clean_formula_lines(block_s61, [*c_pre, *c_if_math, *c_rhs])
    assert untouched.lines[0].spans == block_s61.lines[0].spans


def test_clean_formula_lines_strips_cross_line_trailing_equals_before_next_line_scalar():
    row0 = _chars("where qα =", x=10, y=100, size=10.0, step=5.0, line=0)
    row1 = _chars("4.037 holds", x=10, y=116, size=10.0, step=5.0, line=1)
    chars = [*row0, *row1]
    block = LayoutBlock(
        type=BlockType.TEXT,
        bbox=Rect.of([10, 100, 120, 126]),
        order=0,
        lines=[
            LayoutLine(
                bbox=Rect.of([10, 100, 65, 110]),
                spans=[
                    LayoutSpan(kind="text", content="where", bbox=Rect.of([10, 100, 35, 110])),
                    LayoutSpan(
                        kind="inline_formula",
                        content=r"{{q}_{\alpha}} =",
                        bbox=Rect.of([40, 100, 60, 110]),
                        located=True,
                    ),
                ],
            ),
            LayoutLine(
                bbox=Rect.of([10, 116, 70, 126]),
                spans=[LayoutSpan(kind="text", content="4.037 holds", bbox=Rect.of([10, 116, 70, 126]))],
            ),
        ],
    )
    cleaned = clean_formula_lines(block, chars)
    assert [(s.kind, s.content) for s in cleaned.lines[0].spans] == [
        ("text", "where"),
        ("inline_formula", r"{{q}_{\alpha}}"),
        ("text", "="),
    ]
    assert cleaned.lines[0].spans[1].bbox.x1 < row0[-1].bbox.x0


# ---- 5. [R3-8a] build.py 移除 FOOTNOTE 续接阻断 ----


def test_footnote_does_not_break_cross_page_continuation_while_title_still_blocks():
    def _blk(order: int, kind: BlockType, x: float, y: float, text: str) -> LayoutBlock:
        box = Rect.of([x, y, x + 220, y + 14])
        return LayoutBlock(
            type=kind,
            bbox=box,
            order=order,
            lines=[LayoutLine(bbox=box, spans=[LayoutSpan(kind="text", content=text, bbox=box)])],
        )

    layout = LayoutResult(
        parser="synthetic",
        pages=[
            LayoutPage(
                index=0,
                width=600,
                height=800,
                blocks=[
                    _blk(0, BlockType.TEXT, 320, 640, "In our experiments, the 34-layer"),
                    _blk(1, BlockType.FOOTNOTE, 50, 740, "1 We use standard ImageNet preprocessing."),
                ],
            ),
            LayoutPage(
                index=1,
                width=600,
                height=800,
                blocks=[
                    _blk(0, BlockType.TEXT, 50, 80, "ResNet reduces top-1 error significantly."),
                    _blk(1, BlockType.TEXT, 320, 640, "Another unfinished sentence in"),
                    _blk(2, BlockType.TITLE, 320, 680, "4. Experiments"),
                ],
            ),
            LayoutPage(
                index=2,
                width=600,
                height=800,
                blocks=[_blk(0, BlockType.TEXT, 50, 80, "this new section starts after a heading.")],
            ),
        ],
    )
    pages = [CharPage(index=i, width=600, height=800, kind=PageKind.SCANNED) for i in range(3)]
    doc, report = fuse("synthetic", layout, pages, used_cloud=False)
    p0_body, p0_fn = doc.pages[0].blocks
    p1_cont = doc.pages[1].blocks[0]
    p2_after_title = doc.pages[2].blocks[0]
    assert p1_cont.continues_from == p0_body.id
    assert p0_fn.continues_from is None and p0_fn.translatable
    assert p2_after_title.continues_from is None
    assert report.continuations == 1


# ---- 6. [R3-8b] links.py 放行显式引文前缀 + 缓存版本递增 ----


def test_cite_links_match_explicit_chinese_and_english_prefixes_only():
    assert LINKS_VERSION == 4
    assert EXPORT_CACHE_VERSION == 12

    def cite(anchor: str, x: float) -> PdfLink:
        return PdfLink(
            page=0,
            rect=Rect.of([x, 10, x + 5, 20]),
            anchor=anchor,
            kind="cite",
            block_id="b1",
            dest_page=6,
        )

    pieces = {
        "b1": ("我们测试了39组样品并使用href39参数，详见文献39、40，以及参考文献41、文献第42和Ref. 43、Refs.44,45。")
    }
    links = [cite(n, i * 10) for i, n in enumerate(["39,", "40", "41", "42", "43", "44", "45"])]
    located = locate(links, pieces)
    assert [(lk.text, lk.nth) for lk in located] == [
        ("39", 2),  # 跳过 "39组" 和 "href39"，精准命中 "文献39"
        ("40", 0),
        ("41", 0),
        ("42", 0),
        ("43", 0),
        ("44", 0),
        ("45", 0),
    ]

    # 负例：既无括号、无上标、也无显式引文前缀的裸数字绝不挂 cite 链接
    no_hit = locate([cite("39", 0)], {"b1": "我们测试了39组样品并使用href39参数。"})
    assert [(lk.text, lk.nth) for lk in no_hit] == [("", 0)]
