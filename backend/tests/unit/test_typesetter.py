"""导出 PDF 的中文排版器（clreq）：两端对齐、中西文间距、行首行尾禁则、长词断行。用内置字体，不依赖字体文件。"""

import re

import pymupdf
import pytest

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType
from pdftr.rendering.export import _box, _expand_title_box, _place
from pdftr.rendering.typeset import drop_fake_scripts
from pdftr.rendering.typesetter import MAX_JUSTIFY_EXTRA, FontSet, Style, Typesetter

TS = Typesetter(FontSet.load(None))
SIZE = 10.0


def _lines(text: str, width: float, indent: bool = False):
    tokens = TS.tokenize(text, "body", SIZE, lambda key: None)
    lines, _, _ = TS.layout(tokens, width, SIZE, 1.5, indent)
    return lines


def _text(line) -> str:
    return "".join(r.text for t in line.tokens for r in t.runs)


@pytest.mark.parametrize("text", ["甲乙丙丁戊己", "甲乙丙丁戊己庚"])
def test_heading_rebalances_one_or_two_han_without_resizing(text):
    tokens = TS.tokenize(text, "heading", SIZE, lambda _: None)
    lines, bases, height = TS.layout(tokens, 5 * SIZE, SIZE, 1.5, False, balance_heading=True)
    original, old_bases, old_height = TS.layout(tokens, 5 * SIZE, SIZE, 1.5, False)
    assert len(original) == len(lines) == 2 and len(_text(original[-1])) in (1, 2)
    assert len(_text(lines[-1])) == 3 and "".join(map(_text, lines)) == text
    assert (bases, height) == (old_bases, old_height)
    assert all(r.size == SIZE for line in lines for tok in line.tokens for r in tok.runs)


@pytest.mark.parametrize(
    ("role", "text", "width", "formula"),
    [
        ("body", "甲乙丙丁戊己", 5 * SIZE, None),
        ("caption", "甲乙丙丁戊己", 5 * SIZE, None),
        ("heading", "甲乙丙丁戊己庚辛", 5 * SIZE, None),
        ("heading", "甲乙丙丁（戊己", 6 * SIZE, None),
        ("heading", "甲乙丙丁戊，己", 5 * SIZE, None),
        ("heading", "甲乙丙丁戊<m1/>己", 5 * SIZE, (38.0, 9.0)),
        ("heading", "甲乙丙丁戊longsample己", 5 * SIZE, None),
        ("heading", "甲乙丙丁戊己。", 3 * SIZE, None),
    ],
)
def test_heading_rebalance_respects_role_content_and_line_break_rules(role, text, width, formula):
    tokens = TS.tokenize(text, role, SIZE, lambda _: formula)
    initial = TS.break_lines(tokens, width, SIZE, 0)
    lines, _, _ = TS.layout(tokens, width, SIZE, 1.5, False, balance_heading=role == "heading")
    assert lines == initial


def test_glue_between_han_and_latin_is_quarter_em():
    tokens = TS.tokenize("使用Adam优化器", "body", SIZE, lambda key: None)
    lines, _, _ = TS.layout(tokens, 500, SIZE, 1.5, False)
    assert lines[0].glue == [0.0, 0.0, SIZE / 4, SIZE / 4, 0.0, 0.0]  # 用|Adam 与 Adam|优 之间各 1/4 字宽
    assert _text(lines[0]) == "使用Adam优化器"  # 复制出的文字不多空格


def _block(bid, kind, bounds):
    return Block(
        id=bid, page=0, type=kind, bbox=Rect.of(bounds), order=0,
        style=BlockStyle(font_size=7.97), translatable=kind != BlockType.FIGURE,
    )  # fmt: skip


def _draw_title(title, neighbors):
    tokens = TS.tokenize("甲乙丙丁戊己庚辛壬癸甲乙丙丁戊己庚", "heading", 7.97, lambda _: None)
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=400)
        assert not _place(TS, page, title, tokens, False, {}, neighbors=neighbors)
        blocks = page.get_textpage().extractDICT()["blocks"]
        return [span for block in blocks for line in block["lines"] for span in line["spans"]]


def test_short_title_expands_to_its_column_at_original_font_size():
    title = _block("title", BlockType.TITLE, [36, 100, 122, 110])
    body = _block("body", BlockType.TEXT, [36, 112, 286, 180])
    abstract = _block("abstract", BlockType.TEXT, [36, 35, 560, 70])
    right = _block("right", BlockType.TEXT, [314, 110, 564, 150])
    neighbors = [abstract, title, right, body]
    assert _expand_title_box(title, _box(title), 600, neighbors).x1 == 286
    assert _expand_title_box(title, _box(title), 600, [abstract, right]) == _box(title)
    spans = _draw_title(title, neighbors)
    assert spans and all(s["size"] == pytest.approx(7.97, abs=0.1) for s in spans)
    assert len({s["origin"][1] for s in spans}) == 1


def test_title_avoids_right_obstacle_and_preserves_centered_box():
    title = _block("title", BlockType.TITLE, [36, 100, 122, 110])
    body = _block("body", BlockType.TEXT, [36, 160, 286, 180])
    figure = _block("figure", BlockType.FIGURE, [160, 101, 280, 150])
    assert _expand_title_box(title, _box(title), 600, [body, figure]).x1 == 152
    spans = _draw_title(title, [body, figure])
    assert len({s["origin"][1] for s in spans}) == 2
    assert all(s["size"] == pytest.approx(7.97, abs=0.1) and s["bbox"][2] <= 152.01 for s in spans)
    centered = _block("center", BlockType.TITLE, [150, 100, 450, 110])
    wide = _block("wide", BlockType.TEXT, [36, 160, 564, 180])
    assert _expand_title_box(centered, _box(centered), 600, [wide]) == _box(centered)


def test_single_column_short_title_uses_full_body_width_at_original_size():
    title = _block("title", BlockType.TITLE, [70, 100, 156, 110])
    body = _block("body", BlockType.TEXT, [70, 120, 525, 180])
    assert _expand_title_box(title, _box(title), 595, [title, body]).width == 455
    spans = _draw_title(title, [title, body])
    assert len({s["origin"][1] for s in spans}) == 1
    assert all(s["size"] == pytest.approx(7.97, abs=0.1) for s in spans)


def test_title_borrows_only_when_two_lines_have_clearance():
    title = _block("title", BlockType.TITLE, [36, 100, 122, 110])
    clear = _draw_title(title, [])
    assert len({s["origin"][1] for s in clear}) == 2
    assert all(s["size"] == pytest.approx(7.97, abs=0.1) for s in clear)
    below = _block("below", BlockType.FIGURE, [36, 119.5, 140, 150])
    blocked = _draw_title(title, [below])
    assert blocked and max(s["size"] for s in blocked) < 7.87
    assert max(s["bbox"][3] for s in blocked) < below.bbox.y0


@pytest.mark.parametrize("expr", ["<i>U</i>(1<i>,</i> 3)", "N(0, 1)", "<i>Be</i>ta(1, 2)"])
def test_short_parameter_group_moves_whole_across_styles(expr):
    tokens = TS.tokenize("均匀分布" + expr + "生成", "body", 8, lambda _: None)
    lines = list(map(_text, TS.break_lines(tokens, 47, 8, 0)))
    assert lines[0] == "均匀分布"
    assert lines[1].startswith(re.sub(r"</?i>", "", expr))
    assert all(not line.startswith(",") for line in lines)


def test_long_parentheses_break_inside_without_leading_styled_comma():
    text = "前置<i>F</i>(111111<i>,</i>222222,333333,444444)结束"
    tokens = TS.tokenize(text, "body", 8, lambda _: None)
    lines = list(map(_text, TS.break_lines(tokens, 37, 8, 0)))
    assert len(lines) >= 4
    assert any(line.endswith("111111,") for line in lines)
    assert any(line.endswith("222222,") for line in lines)
    assert all(not line.startswith(",") for line in lines)
    assert "".join(lines) == "前置F(111111,222222,333333,444444)结束"


@pytest.mark.parametrize("expr", ["N0(0, 1)", "f_1(x, y)", "log2(x, y)"])
def test_short_function_with_digit_or_underscore_wraps_as_a_group(expr):
    tokens = TS.tokenize("甲乙丙丁戊" + expr, "body", 8, lambda _: None)
    # 第一行剩余宽度刚好能放到逗号，只有整体分组才会把函数名也移到第二行。
    prefix = TS.tokenize("甲乙丙丁戊" + expr.split(",")[0] + ",", "body", 8, lambda _: None)
    width = sum(t.width for t in prefix) + 8 / 4 + 0.1
    assert list(map(_text, TS.break_lines(tokens, width, 8, 0))) == ["甲乙丙丁戊", expr]


def test_fallback_formula_keeps_link_boundary_before_italic_text():
    formula, text = TS.tokenize("<m1/><i>next</i>", "body", 8, lambda _: None, formulas={"m1": "x"})
    assert not formula.track_links and text.track_links
    assert not text.joined_prev and not text.attach


@pytest.mark.parametrize("mark", [",", ")", "]", "}", ";", ":", "%", ")]"])
def test_styled_ascii_closing_marks_hang_on_previous_line(mark):
    lines = list(map(_text, _lines(f"甲乙<i>{mark}</i>丙", 20)))
    assert lines == ["甲乙" + mark, "丙"]


@pytest.mark.parametrize("mark", ["(", "[", "{"])
def test_styled_ascii_opening_marks_move_with_next_word(mark):
    lines = list(map(_text, _lines(f"甲乙<i>{mark}</i>abc", 28)))
    assert lines == ["甲乙", mark + "abc"]


def test_short_parentheses_respect_whitespace_newlines_and_available_width():
    adjacent = TS.tokenize("<i>U</i>(1<i>,</i> 3)", "body", 8, lambda _: None)
    separated = TS.tokenize("<i>U</i> (1<i>,</i> 3)", "body", 8, lambda _: None)
    assert adjacent[1].joined_prev and not separated[2].joined_prev
    assert len(TS.break_lines(adjacent, 12, 8, 0)) > 1
    tokens = TS.tokenize("均匀分布<i>U</i>(1, \n3)生成", "body", 8, lambda _: None)
    assert _text(TS.break_lines(tokens, 47, 8, 0)[0]).startswith("均匀分布U")


def test_no_line_starts_with_closing_punctuation():
    # 宽度恰好放 5 个字：第 6 个是逗号，不能出现在下一行行首，悬挂在本行行尾
    lines = _lines("一二三四五，六七八九十。", 5 * SIZE)
    assert _text(lines[0]) == "一二三四五，"
    assert all(not _text(line).startswith(("，", "。")) for line in lines)


def test_opening_bracket_not_at_line_end():
    lines = _lines("一二三四（五六七八）", 5 * SIZE)
    assert not _text(lines[0]).endswith("（")


def test_long_word_breaks_after_slash():
    # 某些宽度下整个 Python/OR-Tools 放不下：允许在 / 或连字符后断开，而不是整词挪到下一行
    ends = {_text(_lines("生成可执行的Python/OR-Tools代码", w * SIZE)[0])[-1] for w in range(7, 13)}
    assert ends & {"/", "-"}


def test_underscore_is_a_break_between_latin_letters_only():
    tokens = TS.tokenize("long_variable_2 _private", "body", SIZE, lambda key: None)
    assert ["".join(r.text for r in token.runs) for token in tokens] == ["long_", "variable_", "2", " ", "_private"]


def test_justification_stops_above_quarter_em():
    line = _lines("甲乙丙丁", 2 * SIZE)[0]
    natural = sum(line.glue) + sum(t.width for t in line.tokens)
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        for extra, expected in [(0, SIZE), (SIZE * MAX_JUSTIFY_EXTRA, SIZE * 1.25), (SIZE * 0.251, SIZE)]:
            placed = []
            TS.draw(
                page,
                pymupdf.TextWriter(page.rect),
                (0, 0),
                natural + extra,
                [line],
                [10],
                SIZE,
                "justify",
                False,
                lambda _: None,
                placed=placed,
            )
            assert placed[1].x - placed[0].x == expected


def test_last_line_not_justified_and_indent():
    lines = _lines("这是一个比较长的段落，用来检查段首缩进和末行。" * 2, 12 * SIZE, indent=True)
    assert lines[-1].last and not lines[0].last
    first_width = 2 * SIZE + sum(lines[0].glue) + sum(t.width for t in lines[0].tokens)
    assert first_width <= 12 * SIZE + SIZE  # 缩进两字后仍在宽度内（允许悬挂一个标点）


def test_fake_subscript_is_plain_text():
    assert (
        drop_fake_scripts("• <sub>A synthetic first line of a list item</sub> continues")
        == "• A synthetic first line of a list item continues"
    )
    assert drop_fake_scripts("d<sub>model</sub>") == "d<sub>model</sub>"


def test_inline_bold_selects_both_chinese_and_latin_bold_fonts_without_leaking():
    # 用不同字体实例辨认选中的角色，不依赖本机安装思源字体。
    f = FontSet(
        song=pymupdf.Font("cjk"), hei=pymupdf.Font("cjk"),
        times=pymupdf.Font("tiro"), times_bold=pymupdf.Font("tibo"),
        song_bold=pymupdf.Font("cjk"), hei_bold=pymupdf.Font("cjk"),
    )  # fmt: skip
    ts = Typesetter(f)
    tokens = ts.tokenize("普<b>粗 Bold</b>通", "body", SIZE, lambda _: None)
    runs = [r for t in tokens for r in t.runs]
    assert [(r.text, r.font is f.song_bold, r.font is f.times_bold) for r in runs] == [
        ("普", False, False), ("粗", True, False), (" ", False, True),
        ("Bold", False, True), ("通", False, False),
    ]  # fmt: skip
    assert runs[0].font is f.song and runs[-1].font is f.song
    heading = ts.tokenize("标题", "heading", SIZE, lambda _: None)
    assert all(r.font is f.hei_bold for t in heading for r in t.runs)
    caption = ts.tokenize("图 1. 普通图注", "caption", SIZE, lambda _: None, "图 1.")
    assert caption[0].runs[0].font is f.hei_bold


class _GlyphFont:
    def __init__(self, glyphs: str):
        self.glyphs = set(glyphs)

    def has_glyph(self, codepoint: int) -> bool:
        return chr(codepoint) in self.glyphs


def test_bold_missing_glyph_keeps_bold_fallback():
    regular_song = _GlyphFont("汉")
    regular_hei = _GlyphFont("")
    regular_times = _GlyphFont("A")
    bold_times = _GlyphFont("")
    bold_song = _GlyphFont("A")
    bold_hei = _GlyphFont("汉")
    fallback = _GlyphFont("汉A")
    ts = Typesetter(  # type: ignore[arg-type]
        FontSet(regular_song, regular_hei, regular_times, bold_times, bold_song, bold_hei, fallback)  # type: ignore[arg-type]
    )

    assert ts._font_for("汉", Style("body", bold=True)) is bold_hei
    assert ts._font_for("A", Style("body", bold=True)) is bold_song


def test_inline_italic_uses_real_italic_fonts():
    f = FontSet(
        song=pymupdf.Font("cjk"),
        hei=pymupdf.Font("cjk"),
        times=pymupdf.Font("tiro"),
        times_bold=pymupdf.Font("tibo"),
        song_bold=pymupdf.Font("cjk"),
        hei_bold=pymupdf.Font("cjk"),
        times_italic=pymupdf.Font("tiro"),
        times_bold_italic=pymupdf.Font("tibo"),
    )
    ts = Typesetter(f)
    tokens = ts.tokenize("plain <i>Italic</i>", "body", SIZE, lambda _: None)
    runs = [r for t in tokens for r in t.runs]
    assert runs[0].font is f.times
    assert runs[-1].font is f.times_italic


def test_drop_fake_scripts_preserves_technical_subscripts():
    # 与前端 typeset.test.ts 使用相同用例。
    assert drop_fake_scripts("X<sub>steady_state_1</sub>") == "X<sub>steady_state_1</sub>"
    assert (
        drop_fake_scripts("e<sup>lowercase_long_identifier_12345</sup>")
        == "e<sup>lowercase_long_identifier_12345</sup>"
    )
    assert drop_fake_scripts("X<sub>abcdefghijk l</sub>") == "Xabcdefghijk l"
    assert drop_fake_scripts("X<sub>abcdefghij l</sub>") == "X<sub>abcdefghij l</sub>"
    assert (
        drop_fake_scripts("X<sup>这是一个没有空格但显然应当作为普通文字展示的长中文句子。</sup>")
        == "X这是一个没有空格但显然应当作为普通文字展示的长中文句子。"
    )
    assert drop_fake_scripts("X<sup>n + 1</sup>") == "X<sup>n + 1</sup>"
