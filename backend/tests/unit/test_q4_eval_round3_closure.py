"""Q4 Round 4 外科手术收口修复回归测试（纯合成数据）。

覆盖：
1. [R3-P0-1] background._trim_formula_edges 保护 <=/>=/\\tilde/\\bar 合法数学笔画，同时保留 >=6px 残影清除；
2. [R3-P1-1] align 回收句末标点、build 解耦单行溢出 Span 与 Block 框、background 单行擦除、export >=8pt 栏距；
3. [R3-P1-2] service 高公式批阈值 5 与上限 4 段 / 18 公式；
4. [R3-P1-3] typesetter 跨富文本标签与 caption_no 不吞空格，及公式图后中文标点微间距；
5. [R3-P1-4 & R3-P2-1] formula_align 跳过裸字体切换开关 \\sf 等、service <mN/>= 重复等号守卫与版本递增。
"""

import asyncio
from pathlib import Path

import pymupdf
import pytest

from pdftr.domain.chars import Char
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType, Document, Page, Segment, SegmentPart, SegmentStatus, Span
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutPage, LayoutResult, LayoutSpan
from pdftr.domain.llm import ChatOptions, ChatResult, ChatUsage
from pdftr.extraction.chars import extract
from pdftr.fusion.align import align
from pdftr.fusion.build import fuse
from pdftr.fusion.formula_align import _split_latex_at_if, clean_formula_lines
from pdftr.pipeline.usecases import EXPORT_CACHE_VERSION
from pdftr.rendering.background import _trim_formula_edges, make_background
from pdftr.rendering.export import _clamp_column_right, export_pdf
from pdftr.rendering.typesetter import FontSet, Token, Typesetter
from pdftr.translation.batching import VALIDATION_VERSION
from pdftr.translation.prompt import DocContext
from pdftr.translation.service import (
    _HIGH_FORMULA_MAX_FORMULAS,
    _HIGH_FORMULA_MAX_SEGMENTS,
    _HIGH_FORMULA_THRESHOLD,
    _chunk_high_formula,
    _target_problems,
    _Unit,
    translate,
)


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
) -> list[Char]:
    return [
        Char(
            c=ch,
            bbox=Rect.of([x + i * step, y, x + (i + 1) * step, y + size]),
            font=font,
            size=size,
            origin_y=y + 0.8 * size,
            italic=italic,
            line=line,
        )
        for i, ch in enumerate(text)
    ]


# ---- 1. [R3-P0-1] _trim_formula_edges 保护 <= / >= / \tilde / \bar，清除 >= 6px 残影 ----


@pytest.mark.parametrize(
    ("height", "runs"),
    [
        # r >= 0 / 0 <= t <= T：贴底 3px 横线，间隙 3px
        (44, [(10, 4, 70, 38), (20, 41, 45, 44)]),
        (48, [(10, 8, 70, 42), (18, 45, 62, 48)]),
        # \tilde{U}：贴顶 6px 波浪号，间隙 3px
        (45, [(25, 0, 55, 6), (15, 9, 65, 42)]),
        # \bar{g} 贴顶变体：贴顶 3px 横线，间隙 5px（< 6px）
        (44, [(25, 0, 55, 3), (15, 8, 65, 41)]),
    ],
)
def test_trim_formula_edges_preserves_legitimate_math_strokes_near_border(
    height: int, runs: list[tuple[int, int, int, int]]
):
    pix = _make_rgba_pix(80, height, runs)
    before_rows = _pix_ink_rows(pix)
    trimmed = _trim_formula_edges(pix, zoom=4.0)
    assert trimmed.height == height
    assert _pix_ink_rows(trimmed) == before_rows


def test_trim_formula_edges_still_clears_detached_ghosts_with_six_px_or_wider_gap():
    # 顶部 4px 贴边残影，间隙 6px -> 裁掉顶部 4px
    pix_top = _make_rgba_pix(80, 44, [(10, 0, 70, 4), (10, 10, 70, 42)])
    trimmed_top = _trim_formula_edges(pix_top, zoom=4.0)
    assert trimmed_top.height == 40
    assert all(_pix_ink_rows(trimmed_top)[6:38])

    # 底部 6px 贴边残影，间隙 6px -> 底部清透明保留基线高度
    pix_bot = _make_rgba_pix(80, 56, [(10, 10, 70, 44), (10, 50, 70, 56)])
    trimmed_bot = _trim_formula_edges(pix_bot, zoom=4.0)
    rows_bot = _pix_ink_rows(trimmed_bot)
    assert trimmed_bot.height == 56
    assert all(rows_bot[10:44]) and not any(rows_bot[44:])


# ---- 2. [R3-P1-1] 句末标点回收 + 解耦溢出擦除与 Block 排版框 + 双栏安全栏距 ----


def test_align_recovers_trailing_period_near_right_column_without_stealing_right_column_chars():
    left_box = Rect.of([50.0, 115.0, 296.0, 140.0])
    right_box = Rect.of([314.0, 115.0, 560.0, 168.0])

    # 情形 A：句末 '.' 中心落在右栏 TOL 容差带内（cx=312.78 < 314.0, x1=314.02 <= 314.5），与左栏同 line=6
    left_base = _chars("bypass4net", x=250.0, y=121.0, size=9.5, step=5.0, line=6)
    tail_ns = _chars("ns", x=300.0, y=121.0, size=9.5, step=5.7, line=6)  # 300..311.4
    dot_in_tol = Char(
        c=".",
        bbox=Rect.of([311.53, 121.35, 314.02, 130.26]),
        font="Times-Roman",
        size=9.5,
        origin_y=128.1,
        line=6,
    )
    right_line = _chars("The next column.", x=317.95, y=121.0, size=9.5, step=5.0, line=66)
    assigned, orphans = align([*left_base, *tail_ns, dot_in_tol, *right_line], [left_box, right_box])
    assert "".join(c.c for c in sorted(assigned[0], key=lambda c: c.bbox.x0)) == "bypass4netns."
    assert "".join(c.c for c in sorted(assigned[1], key=lambda c: c.bbox.x0)) == "The next column."
    assert orphans == []

    # 情形 B：右栏略远，句末 '.' 落在 orphans 但 x1 > right_box.x0 - TOL（313.5 > 312.5）
    dot_orphan = dot_in_tol.model_copy(update={"bbox": Rect.of([311.4, 121.35, 313.5, 130.26])})
    right_far = Rect.of([314.5, 115.0, 560.0, 168.0])
    assigned_b, orphans_b = align([*left_base, *tail_ns, dot_orphan, *right_line], [left_box, right_far])
    assert "".join(c.c for c in sorted(assigned_b[0], key=lambda c: c.bbox.x0)) == "bypass4netns."
    assert orphans_b == []

    # 负例 1：句号真正侵入右栏内部（cx=314.6 >= 314.0）仍被拦住
    dot_inside = dot_in_tol.model_copy(update={"bbox": Rect.of([313.8, 121.35, 315.4, 130.26])})
    assigned_c, _ = align([*left_base, *tail_ns, dot_inside, *right_line], [left_box, right_box])
    assert "".join(c.c for c in sorted(assigned_c[0], key=lambda c: c.bbox.x0)) == "bypass4netns"

    # 负例 2：右栏自己行首略突出左缘的标点（同 line=66）绝不被左栏抢走
    right_lead_punct = Char(
        c=")",
        bbox=Rect.of([312.8, 121.35, 314.2, 130.26]),
        font="Times-Roman",
        size=9.5,
        origin_y=128.1,
        line=66,
    )
    assigned_d, _ = align([*left_base, *tail_ns, right_lead_punct, *right_line], [left_box, right_box])
    assert "".join(c.c for c in sorted(assigned_d[0], key=lambda c: c.bbox.x0)) == "bypass4netns"
    assert any(c is right_lead_punct for c in assigned_d[1])


def test_build_decouples_overflow_span_from_block_bbox_and_background_erases_overflow_tail(tmp_path: Path):
    pdf_path = tmp_path / "decouple.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=612, height=792)
        page.insert_text((50, 120), "Normal first line inside left column.", fontsize=9.5, fontname="tiro")
        page.insert_text((50, 134), "github.com/rootless-containers/bypass4netns.", fontsize=9.5, fontname="tiro")
        page.insert_text((318, 120), "Right column top line.", fontsize=9.5, fontname="tiro")
        page.insert_text((318, 134), "Right column second line.", fontsize=9.5, fontname="tiro")
        pdf.save(pdf_path)

    (cp,) = extract(pdf_path)
    url_chars = sorted([c for c in cp.chars if c.bbox.center[1] > 125 and c.bbox.x0 < 316], key=lambda c: c.bbox.x0)
    cut_x1 = url_chars[-4].bbox.x1  # 切掉末尾 "ns."
    l0_box = Rect.of([48, 108, cut_x1, 124])
    l1_box = Rect.of([48, 124, cut_x1, 138])
    left_box = Rect.of([48, 108, cut_x1, 138])
    right_box = Rect.of([314, 108, 560, 140])

    layout = LayoutResult(
        parser="mineru",
        pages=[
            LayoutPage(
                index=0,
                width=612,
                height=792,
                blocks=[
                    LayoutBlock(
                        type=BlockType.TEXT,
                        bbox=left_box,
                        order=0,
                        lines=[
                            LayoutLine(
                                bbox=l0_box,
                                spans=[
                                    LayoutSpan(
                                        kind="text", content="Normal first line inside left column.", bbox=l0_box
                                    )
                                ],
                            ),
                            LayoutLine(
                                bbox=l1_box,
                                spans=[
                                    LayoutSpan(
                                        kind="text",
                                        content="github.com/rootless-containers/bypass4net",
                                        bbox=l1_box,
                                    )
                                ],
                            ),
                        ],
                    ),
                    LayoutBlock(
                        type=BlockType.TEXT,
                        bbox=right_box,
                        order=1,
                        lines=[
                            LayoutLine(
                                bbox=Rect.of([314, 108, 560, 124]),
                                spans=[
                                    LayoutSpan(
                                        kind="text",
                                        content="Right column top line.",
                                        bbox=Rect.of([314, 108, 560, 124]),
                                    )
                                ],
                            ),
                            LayoutLine(
                                bbox=Rect.of([314, 124, 560, 138]),
                                spans=[
                                    LayoutSpan(
                                        kind="text",
                                        content="Right column second line.",
                                        bbox=Rect.of([314, 124, 560, 138]),
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
    left_blk, _ = doc.pages[0].blocks
    assert left_blk.spans[1].text.endswith("bypass4netns.")
    # Block.bbox.x1 保持原栏宽不膨胀，仅溢出单行 Span.bbox.x1 覆盖尾字符
    assert left_blk.bbox.x1 == pytest.approx(cut_x1)
    assert left_blk.spans[1].bbox.x1 > left_blk.bbox.x1

    bg = tmp_path / "bg.pdf"
    count = make_background(pdf_path, [left_blk], bg)
    assert count == 1
    with pymupdf.open(bg) as pdf:
        remaining = str(pdf[0].get_text())
        assert "bypass4net" not in remaining and "ns." not in remaining
        assert "Right column top line." in remaining
        assert "Right column second line." in remaining


def test_export_clamps_wide_left_column_to_preserve_minimum_eight_pt_gutter(tmp_path: Path):
    left = Block(
        id="p0-b0",
        page=0,
        order=0,
        type=BlockType.TEXT,
        bbox=Rect.of([50, 100, 310, 145]),
        style=BlockStyle(font_size=10),
        spans=[Span(text="Left column paragraph.", bbox=Rect.of([50, 100, 310, 145]), size=10)],
        translatable=True,
    )
    right = Block(
        id="p0-b1",
        page=0,
        order=1,
        type=BlockType.TEXT,
        bbox=Rect.of([313, 105, 560, 180]),
        style=BlockStyle(font_size=10),
        spans=[Span(text="Right column paragraph.", bbox=Rect.of([313, 105, 560, 180]), size=10)],
        translatable=True,
    )
    clamped = _clamp_column_right(left, pymupdf.Rect(50, 100, 310, 145), [left, right])
    assert clamped.x1 == pytest.approx(305.0)
    assert right.bbox.x0 - clamped.x1 == pytest.approx(8.0)

    # 已有 >=8pt 栏距、不垂直重叠、或 FIGURE_TEXT 不应被改动
    right_far = right.model_copy(update={"id": "p0-b2", "bbox": Rect.of([325, 105, 560, 180])})
    assert _clamp_column_right(left, pymupdf.Rect(50, 100, 310, 145), [left, right_far]).x1 == pytest.approx(310.0)
    right_below = right.model_copy(update={"id": "p0-b3", "bbox": Rect.of([313, 200, 560, 260])})
    assert _clamp_column_right(left, pymupdf.Rect(50, 100, 310, 145), [left, right_below]).x1 == pytest.approx(310.0)
    fig_label = right.model_copy(update={"id": "p0-b4", "type": BlockType.FIGURE_TEXT})
    assert _clamp_column_right(left, pymupdf.Rect(50, 100, 310, 145), [left, fig_label]).x1 == pytest.approx(310.0)

    # 端到端导出：左栏长中文译文不会越过 305.0 撞向右栏 313.0
    original = tmp_path / "cols.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page(width=612, height=792)
        pdf.save(original)
    doc = Document(job_id="cols", pages=[Page(index=0, width=612, height=792, blocks=[left, right])])
    seg_left = Segment(
        id="s0",
        job_id="cols",
        block_ids=["p0-b0"],
        parts=[SegmentPart(block_id="p0-b0", chars=22)],
        source="Left column paragraph.",
        target="左侧栏需要保持足够的安全栏距以避免两端对齐译文直接撞向右侧栏正文内容。",
        status=SegmentStatus.DONE,
    )
    seg_right = Segment(
        id="s1",
        job_id="cols",
        block_ids=["p0-b1"],
        parts=[SegmentPart(block_id="p0-b1", chars=23)],
        source="Right column paragraph.",
        target="右侧栏正文起始文字。",
        status=SegmentStatus.DONE,
    )
    out_bytes = export_pdf(original, original, doc, [seg_left, seg_right], "mono")
    with pymupdf.open(stream=out_bytes, filetype="pdf") as pdf:
        words = pdf[0].get_text("words")
        left_x1 = max(float(w[2]) for w in words if float(w[0]) < 306)
        right_x0 = min(float(w[0]) for w in words if float(w[0]) >= 306)
        assert right_x0 - left_x1 >= 7.0


# ---- 3. [R3-P1-2] 高公式批阈值 5 与上限 4 段 / 18 公式 ----


def test_high_formula_batching_constants_and_four_formula_plain_segment():
    assert _HIGH_FORMULA_THRESHOLD == 5
    assert _HIGH_FORMULA_MAX_SEGMENTS == 4
    assert _HIGH_FORMULA_MAX_FORMULAS == 18

    # 直接校验 _chunk_high_formula 的 4 段上限（例如 5 个各 4 公式单元在单独测试 chunk 函数时切为 [4, 1]）
    dummy_units = [
        _Unit(
            source=f"u{i}",
            formulas={f"m{j}": "x" for j in range(4)},
            section="",
            role=BlockType.TEXT,
            prev_context="",
            seg_indexes=[i],
            key=f"k{i}",
            user_terms={},
            reference_terms={},
        )
        for i in range(5)
    ]
    assert [len(c) for c in _chunk_high_formula(dummy_units)] == [4, 1]

    class ThinkingProbe:
        model = "deepseek-flash"

        def __init__(self) -> None:
            self.modes: list[str] = []

        async def list_models(self) -> list[str]:
            return [self.model]

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            self.modes.append(opts.thinking)
            return ChatResult(
                text="1|译:<m1/> <m2/> <m3/> <m4/>。",
                usage=ChatUsage(prompt_tokens=10, completion_tokens=10),
            )

    class _Cache:
        def get(self, key: str) -> str | None:
            return None

        def put(self, key: str, target: str) -> None:
            pass

        def purge_older_than(self, hours: int) -> int:
            return 0

    seg4 = Segment(
        id="s4",
        job_id="j",
        block_ids=["p0-b0"],
        source="Given <m1/> <m2/> <m3/> <m4/>.",
        formulas={"m1": "a", "m2": "b", "m3": "c", "m4": "d"},
    )
    chat = ThinkingProbe()
    out, _ = asyncio.run(translate([seg4], chat=chat, cache=_Cache(), ctx=DocContext()))
    assert out[0].status is SegmentStatus.DONE
    assert chat.modes == ["off"]


# ---- 4. [R3-P1-3] typesetter 跨标签与 caption_no 不吞空格 + 公式图后中文标点微间距 ----


def test_typesetter_preserves_space_across_tags_and_caption_no_without_doubling():
    ts = Typesetter(FontSet.load(None))

    def _rendered_text(text: str, *, role: str = "body", caption_no: str = "") -> str:
        tokens = ts.tokenize(text, role, 10.0, lambda _: None, caption_no)
        return "".join(r.text for t in tokens for r in t.runs)

    assert _rendered_text("<b>bypass4netns</b> bypass4netns管理容器。") == "bypass4netns bypass4netns管理容器。"
    assert _rendered_text("<i>alpha</i> beta") == "alpha beta"
    assert _rendered_text("图1. 20层和56层网络", role="caption", caption_no="图1.") == "图1. 20层和56层网络"
    assert _rendered_text("表1 Friedman检验数值", role="caption", caption_no="表1") == "表1 Friedman检验数值"
    assert _rendered_text("图2 TCMs两两比较", role="caption", caption_no="图2") == "图2 TCMs两两比较"

    # 不产生重复空格，且段首前导空格依然丢弃
    assert _rendered_text("<b>foo </b> bar") == "foo bar"
    assert _rendered_text("<b>foo</b>  bar") == "foo bar"
    assert _rendered_text("  <b>foo</b> bar") == "foo bar"
    assert _rendered_text("<b>  foo</b> bar") == "foo bar"


def test_typesetter_adds_subtle_glue_between_formula_image_and_cjk_punctuation():
    ts = Typesetter(FontSet.load(None))
    img = Token("image", width=18.0, height=10.0, image="m1")
    period = Token("cjk", width=10.0, char="。")
    comma = Token("cjk", width=10.0, char="，")
    han = Token("cjk", width=10.0, char="的")
    space = Token("space", width=3.0)

    assert ts._glue_before(img, period, 10.0) == pytest.approx(0.8)
    assert ts._glue_before(img, comma, 10.0) == pytest.approx(0.8)
    assert ts._glue_before(img, han, 10.0) == pytest.approx(2.5)
    assert ts._glue_before(img, space, 10.0) == 0.0
    assert ts._glue_before(han, period, 10.0) == 0.0


# ---- 5. [R3-P1-4 & R3-P2-1] formula_align 跳过裸字体开关 + <mN/>= 重复等号守卫 + 版本号 ----


def test_formula_align_ignores_bare_font_switches_and_splits_s44_if_colon():
    latex = r"{ \sf H } _ { 0 } \mathrm { i } \sf t : \sf F \geq"
    assert _split_latex_at_if(latex, has_colon=True, has_left=True) == (r"{ \sf H } _ { 0 }", r"\sf F \geq")
    assert _split_latex_at_if(r"\rm H_0 \bf i \sf t : \displaystyle F \geq", has_colon=True, has_left=True) == (
        r"\rm H_0",
        r"\displaystyle F \geq",
    )

    # 端到端 clean_formula_lines（原生字符含 H0 if: F ≥）
    c_reject = _chars("Reject ", x=10, y=80, size=9.0, step=4.5, line=0)
    c_h0 = [
        Char(c="H", bbox=Rect.of([149.8, 80.0, 156.3, 89.0]), font="ArialMT", size=9.0, origin_y=87.5, line=0),
        Char(c="0", bbox=Rect.of([156.3, 84.0, 159.3, 90.0]), font="ArialMT", size=5.4, origin_y=89.0, line=0),
    ]
    c_if = [
        Char(c="i", bbox=Rect.of([161.6, 80.0, 163.6, 89.0]), font="ArialMT", size=9.0, origin_y=87.5, line=0),
        Char(c="f", bbox=Rect.of([163.6, 80.0, 166.1, 89.0]), font="ArialMT", size=9.0, origin_y=87.5, line=0),
        Char(c=":", bbox=Rect.of([166.1, 80.0, 168.6, 89.0]), font="ArialMT", size=9.0, origin_y=87.5, line=0),
    ]
    c_rhs = [
        Char(c="F", bbox=Rect.of([171.1, 80.0, 176.6, 89.0]), font="ArialMT", size=9.0, origin_y=87.5, line=0),
        Char(c="≥", bbox=Rect.of([179.1, 80.0, 184.0, 89.0]), font="ArialMT", size=9.0, origin_y=87.5, line=0),
    ]
    c_tail = _chars(" critical value", x=186.0, y=80.0, size=9.0, step=4.5, line=0)
    block = LayoutBlock(
        type=BlockType.TEXT,
        bbox=Rect.of([10, 80, 260, 91]),
        order=0,
        lines=[
            LayoutLine(
                bbox=Rect.of([10, 80, 260, 91]),
                spans=[
                    LayoutSpan(kind="text", content="Reject", bbox=Rect.of([10, 80, 42, 91])),
                    LayoutSpan(kind="inline_formula", content=latex, bbox=Rect.of([147, 80, 184, 91]), located=True),
                    LayoutSpan(kind="text", content="critical value", bbox=Rect.of([186, 80, 260, 91])),
                ],
            )
        ],
    )
    cleaned = clean_formula_lines(block, [*c_reject, *c_h0, *c_if, *c_rhs, *c_tail])
    assert [(s.kind, s.content) for s in cleaned.lines[0].spans] == [
        ("text", "Reject"),
        ("inline_formula", r"{ \sf H } _ { 0 }"),
        ("text", "if:"),
        ("inline_formula", r"\sf F \geq"),
        ("text", "critical value"),
    ]


def test_duplicate_formula_equals_guard_catches_leak_and_allows_legitimate_cases():
    assert VALIDATION_VERSION == 8
    assert EXPORT_CACHE_VERSION == 17

    formulas = {"m1": r"{ { q } _ { \alpha } }", "m2": r"\mathtt { C D } = 0 , 6 9 4"}
    source = "We get that <m1/> = 4.037 and <m2/>."

    # 命中：m2 本身含等号，源文 <m2/> 后无等号，译文重复写 <m2/>=0.694
    bad_problems = _target_problems(source, "可得<m1/>=4.037，<m2/>=0.694。", formulas=formulas)
    assert bad_problems == ["公式 <m2/> 已包含等号，请勿在占位符后重复写出等号和数值"]
    assert _target_problems(source, "可得<m1/>=4.037，<m2/> = 0.694。", formulas=formulas) == bad_problems

    # 合法：m1 不含等号且源文后有等号，m2 后不重复等号 -> 通过
    assert _target_problems(source, "可得<m1/>=4.037，<m2/>。", formulas=formulas) == []

    # 反例 1：等号仅出现在下标花括号内（如 \sum_{i=1}^n），绝不误报
    assert (
        _target_problems(
            "Let <m1/> equal zero.",
            "令<m1/>=0。",
            formulas={"m1": r"\sum_{i=1}^{n} x_i"},
        )
        == []
    )

    # 反例 2：源文本身在 <m1/> 后就有 '='，绝不误报
    assert (
        _target_problems(
            "We have <m1/> = 0.",
            "我们有<m1/>=0。",
            formulas={"m1": r"a = b"},
        )
        == []
    )

    # 端到端 translate 自动重译并回传反馈
    class DuplicateOnceChat:
        model = "deepseek-flash"

        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def list_models(self) -> list[str]:
            return [self.model]

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            self.prompts.append(user)
            if len(self.prompts) == 1:
                return ChatResult(text="1|可得<m1/>=4.037，<m2/>=0.694。")
            return ChatResult(text="1|可得<m1/>=4.037，<m2/>。")

    class _Cache:
        def __init__(self) -> None:
            self.data: dict[str, str] = {}

        def get(self, key: str) -> str | None:
            return self.data.get(key)

        def put(self, key: str, target: str) -> None:
            self.data[key] = target

        def purge_older_than(self, hours: int) -> int:
            return 0

    seg = Segment(id="s65", job_id="j", block_ids=["p0-b0"], source=source, formulas=formulas)
    chat = DuplicateOnceChat()
    out, report = asyncio.run(translate([seg], chat=chat, cache=_Cache(), ctx=DocContext()))
    assert report.requests == 2 and out[0].status is SegmentStatus.DONE
    assert out[0].target == "可得<m1/>=4.037，<m2/>。"
    assert "公式 <m2/> 已包含等号" in chat.prompts[1]
