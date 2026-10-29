"""链接文字在译文里的定位：引用编号、作者-年份、断词、图表编号、跨块、网址。"""

from pdftr.domain.geometry import Rect
from pdftr.rendering.links import PdfLink, compact, locate


def _link(anchor: str, kind: str = "cite", block: str = "b1", y: float = 0, x: float = 0, uri: str = "") -> PdfLink:
    return PdfLink(page=0, rect=Rect(x0=x, y0=y, x1=x + 5, y1=y + 5), anchor=anchor, kind=kind, block_id=block,
                   uri=uri, dest_page=None if uri else 3)  # fmt: skip


def _found(links: list[PdfLink]) -> list[tuple[str, int]]:
    return [(x.text, x.nth) for x in links]


def test_citation_numbers_only_inside_brackets():
    # 正文里的 22 层不能配给引用 [22]；“[22,”“21]”带着标点
    pieces = {"b1": "我们用 22 层网络，见文献 [22, 21]。"}
    got = locate([_link("[22,", x=0), _link("21]", x=10)], pieces)
    text = compact(pieces["b1"])
    assert (
        _found(got) == [("22", 1), ("21", 0)]
        and text.find("[22") + 1 == [i for i in range(len(text)) if text.startswith("22", i)][1]
    )


def test_author_year_split_links_and_hyphenation():
    pieces = {"b1": "与以往工作（Peters et al., 2018a; Radford et al., 2018）不同"}
    got = locate([_link("Peters et al.", x=0), _link("2018a;", x=10), _link("Rad-", x=20), _link("ford et al.,", y=10),
                  _link("2018)", y=10, x=10)], pieces)  # fmt: skip
    assert _found(got) == [("Petersetal", 0), ("2018a", 0), ("Rad", 0), ("fordetal", 0), ("2018", 1)]


def test_figure_numbers_prefer_chinese_labels_and_skip_decimals():
    pieces = {"b1": "准确率 6.43%（见表 6 与图 2）"}
    got = locate([_link("6)", kind="table"), _link("2", kind="figure", x=10)], pieces)
    text = compact(pieces["b1"])
    assert [text[text.index("表") + 1]] == ["6"] and _found(got) == [("6", 1), ("2", 0)]


def test_footnotes_skipped_and_missing_text_left_empty():
    got = locate([_link("s1,", kind="Hfootnote"), _link("[99]")], {"b1": "没有这条引用 [12]"})
    assert _found(got) == [("", 0), ("", 0)]


def test_falls_back_to_sibling_blocks_of_the_same_paragraph():
    # 跨栏段落拆块后，引用落进了上一块的译文
    pieces = {"b1": "……（Howard and Ruder,", "b2": "2018）。"}
    got = locate([_link("(Howard and Ruder,", block="b2")], pieces, {"b1": ["b1", "b2"], "b2": ["b1", "b2"]})
    assert got[0].block_id == "b1" and got[0].text == "HowardandRuder"


def test_urls_cut_by_line_breaks_match_the_full_address():
    link = _link("http://mscoco.org/dataset/#de", kind="", uri="http://mscoco.org/dataset/#detections-challenge2015")
    pieces = {"b1": "见 http://mscoco.org/dataset/#detections-challenge2015。"}
    assert locate([link], pieces)[0].text == "http://mscoco.org/dataset/#detections-challenge2015"
    footnote = _link("1http://image-net.org/", kind="", uri="http://image-net.org/")  # 原文字带着脚注编号
    assert locate([footnote], {"b1": "1http://image-net.org/ 与"})[0].text == "http://image-net.org/"
    trailing = _link("google-research/bert.", kind="", uri="https://github.com/google-research/bert")
    assert locate([trailing], {"b1": "代码见 https://github.com/google-research/bert。"})[0].text == trailing.uri


def test_publisher_citation_dests_mailto_and_whole_block_links():
    # 出版社排版：引用跳转名是 CIT0057 一类（extract_links 归为 cite），年份在圆括号里
    got = locate([_link("2026)", kind="cite")], {"b1": "（Yao et al. 2026）。在 2026 年"})
    assert _found(got) == [("2026", 0)]
    mail = _link("a@b.cn\nc of", kind="", uri="mailto:a@b.cn")
    assert locate([mail], {"b1": "作者：A a@b.cn 某大学"})[0].text == "a@b.cn"
    button = _link("View related articles", kind="", uri="https://x.org/r").model_copy(update={"whole": True})
    assert locate([button], {"b1": "查看相关文章"})[0].text == "查看相关文章"
