"""导出 PDF 的中文排版器（clreq）：两端对齐、中西文间距、行首行尾禁则、长词断行。用内置字体，不依赖字体文件。"""

from pdftr.rendering.typeset import drop_fake_scripts
from pdftr.rendering.typesetter import FontSet, Typesetter

TS = Typesetter(FontSet.load(None))
SIZE = 10.0


def _lines(text: str, width: float, indent: bool = False):
    tokens = TS.tokenize(text, "body", SIZE, lambda key: None)
    lines, _, _ = TS.layout(tokens, width, SIZE, 1.5, indent)
    return lines


def _text(line) -> str:
    return "".join(r.text for t in line.tokens for r in t.runs)


def test_glue_between_han_and_latin_is_quarter_em():
    tokens = TS.tokenize("使用Adam优化器", "body", SIZE, lambda key: None)
    lines, _, _ = TS.layout(tokens, 500, SIZE, 1.5, False)
    assert lines[0].glue == [0.0, 0.0, SIZE / 4, SIZE / 4, 0.0, 0.0]  # 用|Adam 与 Adam|优 之间各 1/4 字宽
    assert _text(lines[0]) == "使用Adam优化器"  # 复制出的文字不多空格


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


def test_last_line_not_justified_and_indent():
    lines = _lines("这是一个比较长的段落，用来检查段首缩进和末行。" * 2, 12 * SIZE, indent=True)
    assert lines[-1].last and not lines[0].last
    first_width = 2 * SIZE + sum(lines[0].glue) + sum(t.width for t in lines[0].tokens)
    assert first_width <= 12 * SIZE + SIZE  # 缩进两字后仍在宽度内（允许悬挂一个标点）


def test_fake_subscript_is_plain_text():
    assert (
        drop_fake_scripts("• <sub>LLM-based FJSP modeling is formulated</sub> as")
        == "• LLM-based FJSP modeling is formulated as"
    )
    assert drop_fake_scripts("d<sub>model</sub>") == "d<sub>model</sub>"
