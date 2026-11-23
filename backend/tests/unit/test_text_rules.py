from pdftr.domain.chars import Char
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import PageKind
from pdftr.domain.text import is_bad_char
from pdftr.extraction.chars import _font_is_bold, _font_is_italic
from pdftr.extraction.classify import classify
from pdftr.fusion.align import align
from pdftr.fusion.style import block_style, is_serif
from pdftr.fusion.text_fix import choose, recover_dropped_chars, strip_fake_scripts, text_of


def _ch(
    c: str, x: float, y: float = 100, line: int = 0, font: str = "Times-Roman", size: float = 10, bold=False
) -> Char:
    return Char(c=c, bbox=Rect.of([x, y, x + 5, y + 10]), font=font, size=size, bold=bold, line=line)


def test_align_prefers_smallest_box_and_reports_orphans():
    outer, inner = Rect.of([0, 0, 200, 200]), Rect.of([10, 90, 60, 120])
    chars = [_ch("a", 20), _ch("b", 150), _ch("z", 500)]
    per, orphans = align(chars, [outer, inner])
    assert [c.c for c in per[0]] == ["b"]
    assert [c.c for c in per[1]] == ["a"]
    assert [c.c for c in orphans] == ["z"]


def test_text_of_orders_and_spaces():
    chars = [_ch("b", 6), _ch("a", 0), _ch("c", 30), _ch("ﬁ", 0, y=120, line=1)]
    assert text_of(chars) == "ab c fi"


def test_choose_only_fixes_broken_text():
    # 正常文字一律保留解析器结果，即使 PDF 文字不同
    assert choose("L weight decay", "L2 weight decay") == ("L weight decay", False)
    assert choose("BERT<sub>BASE</sub>", "BERTBASE") == ("BERT<sub>BASE</sub>", False)
    # 含控制字符的才改用 PDF 文字
    assert choose("dominated by the \x0f in", "dominated by the ε in") == ("dominated by the ε in", True)
    # PDF 文字差异太大时仍保留
    assert choose("dominated by the \x0f in", "totally different text here") == ("dominated by the \x0f in", False)
    # 解析器没给文字时用 PDF 文字
    assert choose("", "hello") == ("hello", True)


def test_strip_fake_scripts_preserves_footnotes_and_mathematical_scripts():
    text = "de<sub>m</sub>and<sup>;</sup> H<sub>2</sub>O n<sup>norm</sup> Oak<sup>∗</sup><sup>[40]</sup><sup>1</sup>"
    expected = "demand; H<sub>2</sub>O n<sup>norm</sup> Oak<sup>∗</sup><sup>[40]</sup><sup>1</sup>"
    assert strip_fake_scripts(text, []) == expected
    assert strip_fake_scripts("com<sup>pu</sup>ter", [_ch(c, i * 5) for i, c in enumerate("computer")]) == "computer"
    assert strip_fake_scripts("x<sub>i</sub>y", []) == "x<sub>i</sub>y"
    plain = [_ch(c, i * 5) for i, c in enumerate("A normal word")]
    assert strip_fake_scripts("A <sub>normal</sub> word", plain) == "A normal word"
    # 原生页同字号同基线：词首、词尾、短词、字母加逗号假标均应剥除，多行容差不把行距误判为上下标。
    multiline = [
        *[_ch(c, i * 5, y=100, line=0).model_copy(update={"origin_y": 108.0}) for i, c in enumerate("by study,")],
        *[_ch(c, i * 5, y=114, line=1).model_copy(update={"origin_y": 122.0}) for i, c in enumerate("proposed when")],
    ]
    assert strip_fake_scripts(
        "b<sub>y</sub> stud<sub>y,</sub> <sub>p</sub>ro<sub>p</sub>osed w<sup>h</sup>en", multiline
    ) == ("by study, proposed when")
    # 真正的缩字英文上标（含小字数量不少于基字符的短 span）由字号几何保护，而非词名硬编码。
    real_sup = [
        _ch("x", 0, size=10).model_copy(update={"origin_y": 108.0}),
        *[_ch(c, (i + 1) * 5, size=7.5).model_copy(update={"origin_y": 104.0}) for i, c in enumerate("norm")],
    ]
    assert strip_fake_scripts("x<sup>norm</sup>", real_sup) == "x<sup>norm</sup>"
    short_sub = [
        _ch("x", 0, size=10).model_copy(update={"origin_y": 108.0}),
        _ch("i", 5, size=8.0).model_copy(update={"origin_y": 108.0}),
    ]
    assert strip_fake_scripts("x<sub>i</sub>", short_sub) == "x<sub>i</sub>"


def test_script_geometry_in_a_mixed_line_distinguishes_the_payloads():
    chars = [_ch(c, i * 5).model_copy(update={"origin_y": 110.0}) for i, c in enumerate("faster alpha")]
    assert strip_fake_scripts("fa<sup>s</sup>ter al<sub>p</sub>ha", chars) == "faster alpha"
    chars[9].origin_y = 112
    assert strip_fake_scripts("fa<sup>s</sup>ter al<sub>p</sub>ha", chars) == "faster al<sub>p</sub>ha"
    chars[9].origin_y = None
    chars[9].size = 8.8
    assert strip_fake_scripts("fa<sup>s</sup>ter al<sub>p</sub>ha", chars) == "faster al<sub>p</sub>ha"


def test_recovers_only_letter_omissions_inside_existing_words():
    assert recover_dropped_chars("We compare colr value.", "We compare color values.") == "We compare color values."
    assert recover_dropped_chars("The h brid roblem raph.", "The hybrid problem graph.") == "The hybrid problem graph."
    for bad_pdf in (
        "We compare cold value.",
        "We compare colr value. rate",
        "We compare colr ∑value.",
        "New We compare colr value.",
    ):
        assert recover_dropped_chars("We compare colr value.", bad_pdf) == "We compare colr value."
    assert recover_dropped_chars("H<sub>2</sub>O colr", "H2O color") == "H<sub>2</sub>O colr"
    assert recover_dropped_chars("A colr", "A color\U000f0000") == "A colr"


def test_bad_glyph_repair_can_keep_a_unique_unmapped_symbol_for_cropping():
    pua = "\U00100001"
    assert is_bad_char(pua) and is_bad_char("\U000f0001") and not is_bad_char("φ")
    assert choose(f"Values {pua} remain �valid.", f"Values {pua} remain invalid.") == (
        f"Values {pua} remain invalid.",
        True,
    )
    assert choose("��", "ab") == ("ab", True)
    assert choose("��", "abc") == ("��", False)
    assert choose("alpha\x1cbeta", "alphabeta") == ("alphabeta", True)
    assert choose("H<sub>2</sub>O remains �valid.", "H2O remains invalid.") == ("H<sub>2</sub>O remains invalid.", True)
    assert choose("H<sub>2</sub>O remains �valid.", "H3O remains invalid.") == ("H<sub>2</sub>O remains �valid.", False)


def test_block_style():
    chars = [_ch("a", 0, size=10, bold=True), _ch("b", 6, size=10, bold=True), _ch("c", 12, size=12)]
    style = block_style(chars)
    assert style.font_size == 10 and not style.bold and style.serif  # 混合字重不再按多数票整块加粗
    assert block_style(chars, title=True).font_size == 12  # small-caps 标题取首字母等较大字形
    assert block_style([c.model_copy(update={"bold": True}) for c in chars]).bold
    assert not is_serif("Helvetica-Bold")
    assert block_style([]).font_size == 10.0


def test_title_size_ignores_anomalous_outlier_but_keeps_small_caps_lead():
    normal = [_ch(chr(97 + i), i * 6, size=8) for i in range(14)]
    lead = _ch("I", 0, size=10)
    outlier = _ch("*", 6, size=20)
    assert block_style([lead, *normal], title=True).font_size == 10
    assert block_style([lead, *normal, outlier], title=True).font_size == 10


def test_block_style_only_marks_fully_italic_blocks():
    chars = [_ch("a", 0), _ch("b", 6)]
    italic = [c.model_copy(update={"italic": True}) for c in chars]
    mixed = [italic[0], chars[1]]
    assert block_style(italic).italic
    assert not block_style(mixed).italic


def test_medium_font_name_is_not_bold():
    assert not _font_is_bold("NotoSansSC-Medium")
    assert _font_is_bold("NotoSansSC-Bold")
    assert _font_is_bold("noto-sans-sc-bold")
    assert _font_is_bold("NimbusRoman-Black")
    assert _font_is_italic("nimbusroman-italic")
    assert _font_is_italic("NotoSans-OBLIQUE")
    assert not _font_is_italic("NotoSans-Regular")


def test_classify():
    text = [_ch(chr(97 + i % 26), i * 6) for i in range(40)]
    assert classify(text, image_coverage=0.0) is PageKind.NATIVE
    assert classify([], image_coverage=0.95) is PageKind.SCANNED
    assert classify([], image_coverage=0.0) is PageKind.NATIVE  # 空白页
    garbled = [_ch("", i * 6) for i in range(30)] + text[:10]
    assert classify(garbled, image_coverage=0.0) is PageKind.GARBLED
